"""Every shipped kernel lowers, at the shapes a real model asks for.

No card and no simulator: this compiles and encodes, which is where a tiling
mistake, a missing instruction field or a bad extent shows up. What it cannot
see is arithmetic -- that needs hardware.

Shapes are taken from the two models the stack exists to run: a 7B-style
transformer layer and an SDXL attention block.
"""

import pytest
from kohakuaccel.lang import iface
from kohakuaccel.machinespec import MachineSpec
from kohakutpu.isa import ISA
from kohakutpu.lang import BACKEND

from kohakutpu import kernels as K
from kohakutpu import ops as O

#: Six clusters and two vector cores, which is what mesh_0 of the v2 card is.
MACHINE = MachineSpec(
    name="ship_3x2",
    units={
        "MG": ((1, 1), (2, 1), (1, 2), (2, 2), (1, 3), (2, 3)),
        "VC": ((2, 0), (2, 4)),
    },
    inst_depth=512,
    agent=(1, 0),
)


class Shaped:
    """What `solve` needs of an argument, without a device to put it on."""

    def __init__(self, shape) -> None:
        self.shape = shape


def compile_kernel(fn, bound: dict, **knobs):
    """Compile `fn` for `bound` and encode every stage. Returns the compiled form."""
    got = fn.compile(MACHINE, iface.solve(fn.signature, bound), **knobs)
    for stage in got.stages:
        words = BACKEND.encode(
            got,
            stage,
            {
                n: 0x1000 * (i + 1)
                for i, n in enumerate(
                    [p.name for p in fn.signature.ports]
                    + list(got.temps)
                    + list(BACKEND.constants(got))
                )
            },
        )
        assert words, f"{fn.name} stage {stage.index} encoded nothing"
        for inst in words.values():
            for word in inst:
                ISA.set.decode(word)
    return got


# ---------------------------------------------------------------- elementwise
#: A single-operator pass is an OP, not a fused kernel -- `kohakutpu.ops`.
ELEMENTWISE = [O.silu, O.gelu, O.relu]


@pytest.mark.parametrize("fn", ELEMENTWISE, ids=lambda f: f.name)
@pytest.mark.parametrize("shape", [(4096,), (32, 4096), (2, 8, 64, 64)])
def test_elementwise_lowers_at_any_rank(fn, shape):
    """`In(...)` takes one contiguous block whatever its rank."""
    compile_kernel(fn, {"x": Shaped(shape)})


# --------------------------------------------------------------------- matmul
@pytest.mark.parametrize(
    ("m", "k", "n"),
    [
        (32, 64, 64),  # the shape that exposed the L1 bank bug
        (64, 4096, 4096),  # a 7B-style projection
        (77, 2048, 64),  # SDXL cross-attention, head dim 64
        (256, 1024, 4096),  # an MLP up-projection
    ],
)
def test_matmul_lowers(m, k, n):
    compile_kernel(O.matmul, {"a": Shaped((m, k)), "b": Shaped((n, k))})


def test_batched_matmul_lowers():
    """`ops.matmul` takes `...`, so the batched case is the same kernel."""
    compile_kernel(O.matmul, {"a": Shaped((8, 64, 64)), "b": Shaped((64, 64))})


# ----------------------------------------------------------------- activation
#: Every contraction whose epilogue rides the resident tile.
EPILOGUES = [K.linear_silu, K.linear_relu, K.linear_gelu, K.linear_scale]


@pytest.mark.parametrize("fn", EPILOGUES, ids=lambda f: f.name)
def test_a_linear_epilogue_lowers(fn):
    compile_kernel(fn, {"x": Shaped((32, 64)), "w": Shaped((64, 64))})


@pytest.mark.parametrize(
    "fn",
    [K.linear_add, K.linear_add_relu, K.linear_add_gelu, K.linear_add_silu],
    ids=lambda f: f.name,
)
def test_a_biased_linear_lowers(fn):
    """The bias arrives at full shape; a fused epilogue could not read it."""
    compile_kernel(
        fn, {"x": Shaped((32, 64)), "w": Shaped((64, 64)), "b": Shaped((32, 64))}
    )


def test_a_gated_residual_lowers():
    """`r + g * (x @ w.T)` -- three full-shape operands, eight of eight slots."""
    compile_kernel(
        K.linear_gate_add,
        {
            "x": Shaped((32, 64)),
            "w": Shaped((64, 64)),
            "g": Shaped((32, 64)),
            "r": Shaped((32, 64)),
        },
    )


def test_the_clamp_over_a_bias_is_one_lifted_chain():
    """`(t + |t|) * 0.5` is a TREE, so the biased tile is held in a register.

    A chain keeps ONE running result. Reading `t` twice out of memory instead
    would be a second statement, a second pass and a second store.
    """
    got = compile_kernel(
        K.linear_add_relu,
        {"x": Shaped((32, 64)), "w": Shaped((64, 64)), "b": Shaped((32, 64))},
    )
    vector = [
        s
        for st in got.stages
        if st.unit == "VC"
        for inst in st.instances
        for s in inst.stmts
    ]
    assert len(vector) == 1
    assert len(vector[0].args["lifted"]) == 1


def test_the_tanh_gelu_is_a_chain_and_not_a_tree():
    """`gelu_tanh` folds `0.5 x (1 + tanh(..))` to `x * sigmoid(..)`.

    Written as tinygrad spells it the top MUL has two computed arguments, which
    a FUSED epilogue cannot hold -- it keeps one running result and raises.
    """
    got = compile_kernel(K.linear_gelu, {"x": Shaped((32, 64)), "w": Shaped((64, 64))})
    assert got.temps == {}


@pytest.mark.parametrize("fn", EPILOGUES, ids=lambda f: f.name)
def test_the_fused_form_declares_no_temp(fn):
    """If a temp appears, the epilogue went through DRAM after all.

    There is no hand-written separated twin to compare against any more: a grid
    the cores cannot hold is STAGED by the compiler, which is
    `test_staging_fallback.py`.
    """
    bound = {"x": Shaped((32, 64)), "w": Shaped((64, 64))}
    assert compile_kernel(fn, bound).temps == {}


# ----------------------------------------------------------------------- norm
@pytest.mark.parametrize("fn", [K.rmsnorm, K.softmax])
def test_row_wise_lowers(fn):
    compile_kernel(
        fn,
        (
            {"x": Shaped((64, 64)), "w": Shaped((64, 64))}
            if fn is K.rmsnorm
            else {"x": Shaped((64, 64))}
        ),
    )


def test_layernorm_lowers():
    compile_kernel(
        K.layernorm,
        {"x": Shaped((64, 64)), "w": Shaped((64, 64)), "b": Shaped((64, 64))},
    )


def test_residual_lowers():
    compile_kernel(O.residual, {"a": Shaped((64, 64)), "b": Shaped((64, 64))})


# ------------------------------------------------------------------ attention
@pytest.mark.parametrize("causal", [False, True])
def test_flash_attention_lowers(causal):
    bound = {
        "q": Shaped((64, 64)),
        "k": Shaped((64, 64)),
        "v": Shaped((64, 64)),
    }
    compile_kernel(K.flash_attention, bound, block=64, causal=causal)


@pytest.mark.parametrize("shape", [(1, 64, 64), (4, 64, 64), (2, 4, 64, 64)])
def test_flash_attention_takes_a_head_axis(shape):
    """Heads ride the ellipsis, so one trace serves (L,D), (H,L,D) and (B,H,L,D).

    A leading ONE is still a leading axis, and the layout takes two.
    """
    *lead, length, dim = shape
    bound = {
        "q": Shaped((*lead, length, dim)),
        "k": Shaped((*lead, length, dim)),
        "v": Shaped((*lead, dim, length)),
    }
    compile_kernel(K.flash_attention, bound, block=64)
