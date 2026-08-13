"""A statement says what it reads and writes, and still does after unroll.

`check_hazards` refuses a stage whose statements depend on each other: a unit
runs a stage's programs without waiting between them, so the second reads stale
bytes -- 79% error, measured, and no crash to point at. It could never fire, for
two independent reasons, and fixing either alone left it silent: the cluster's
fill, gemm and drain declared nothing, and `record._bind` dropped `writes` and
`reads` when a loop unrolled, which cost the vector statements theirs too.

Hence the three groups: the declaration, its survival through unroll, and the
survey -- every shipped kernel compiles, which now means no stage of one reads
what it just wrote.
"""

import pytest
from kohakuaccel.lang import Loop, Stmt, dims, iface, loop, units
from kohakuaccel.lang.kernel import KernelError
from kohakuaccel.lang.record import Grid
from kohakuaccel.machinespec import MachineSpec
from kohakutpu.lang import kernel
from kohakutpu.lang.vector import Ref

from kohakutpu import kernels as K
from kohakutpu import lang as L
from kohakutpu import ops as O

M, KD, N = dims("M, K, N")

MACHINE = MachineSpec(
    name="hazards",
    units={
        "MG": ((1, 1), (2, 1), (1, 2), (2, 2), (1, 3), (2, 3)),
        "VC": ((2, 0), (2, 4)),
    },
    inst_depth=512,
    agent=(1, 0),
)


class Shaped:
    def __init__(self, shape) -> None:
        self.shape = shape


def statements(body) -> list:
    """Every statement in a traced body, loops and grids flattened."""
    out: list = []
    for item in body:
        if isinstance(item, Stmt):
            out.append(item)
        elif isinstance(item, (Grid, Loop)):
            out += statements(item.body)
    return out


def traced(fn, **knobs) -> list:
    return statements(fn.trace(**knobs).top)


# ------------------------------------------------------------- the declaration
def test_a_cluster_fill_declares_the_operand_it_reads():
    """Without this the guard sees a stage of statements touching nothing."""
    fills = [s for s in traced(O.matmul) if s.kind == "fill"]
    assert fills, "matmul traced no fill"
    for s in fills:
        assert s.reads == (s.args["operand"],), s.args


def test_a_cluster_drain_declares_the_result_it_writes():
    drains = [s for s in traced(O.matmul) if s.kind == "drain"]
    assert drains, "matmul traced no drain"
    for s in drains:
        assert s.writes == s.args["result"], s.args


def test_a_gemm_names_no_buffer():
    """It contracts what the fills placed; naming one would be a false edge."""
    for s in traced(O.matmul):
        if s.kind == "gemm":
            assert s.reads == () and s.writes is None


def test_a_fused_epilogue_declares_both_halves():
    """The drain hands a tile to a vector core and the epilogue reads it there."""
    got = traced(K.linear_silu)
    assert any(s.kind == "drain" and s.writes for s in got)
    assert any(s.kind == "apply" and s.writes for s in got)


# ------------------------------------------------------- surviving the unroll
def test_a_fill_still_declares_its_operand_after_the_loop_unrolls():
    """`record._bind` rebuilds each statement per iteration; the fields must ride.

    This is the half that made the guard dead for the VECTOR statements too,
    which had declared correctly all along.
    """
    got = O.matmul.compile(
        MACHINE,
        iface.solve(
            O.matmul.signature, {"a": Shaped((64, 128)), "b": Shaped((32, 128))}
        ),
    )
    fills = [s for s in got.stages[0].instances[0].stmts if s.kind == "fill"]
    assert fills, "matmul compiled no fill"
    assert all(s.reads == (s.args["operand"],) for s in fills)
    drains = [s for s in got.stages[0].instances[0].stmts if s.kind == "drain"]
    assert all(s.writes == s.args["result"] for s in drains)


def test_a_vector_statement_keeps_its_operands_after_the_unroll():
    """A reduce names its source, and an apply names every buffer leaf it reads.

    A STAGED softmax, defined here: the fused one that ships folds INSIDE a
    chain, so it emits no `reduce` statement for this to look at.
    """

    @kernel
    def softmax_staged(x=L.In(..., M, N), y=L.Out(..., M, N)):
        hi, ex, total = L.temp(M, N), L.temp(M, N), L.temp(M, N)
        hi <<= L.row_max(x)
        ex <<= L.exp2((x - hi) * 1.4426950408889634)
        total <<= L.row_sum(ex)
        y <<= ex / total

    got = softmax_staged.compile(
        MACHINE, iface.solve(softmax_staged.signature, {"x": Shaped((64, 64))})
    )
    seen = [s for st in got.stages for s in st.instances[0].stmts]
    reduces = [s for s in seen if s.kind == "reduce"]
    assert reduces, "softmax_staged compiled no reduction"
    assert all(s.reads == (s.args["source"],) for s in reduces)
    for s in [s for s in seen if s.kind == "apply"]:
        want = tuple(le.name for le in s.args["leaves"] if isinstance(le, Ref))
        assert s.reads == want, s.args


# -------------------------------------------------------------------- the survey
#: The shapes the suite already compiles these at, plus the two that stage a
#: temp across unit types -- which is where a hazard would actually appear.
LIBRARY = [
    ("matmul", O.matmul, {"a": Shaped((64, 128)), "b": Shaped((32, 128))}, {}),
    # `ops.matmul` takes `...`, so the batched case is the same kernel.
    ("batched_matmul", O.matmul, {"a": Shaped((4, 32, 64)), "b": Shaped((32, 64))}, {}),
    ("linear_silu", K.linear_silu, {"x": Shaped((32, 64)), "w": Shaped((64, 64))}, {}),
    ("rmsnorm", K.rmsnorm, {"x": Shaped((64, 64)), "w": Shaped((64, 64))}, {}),
    (
        "layernorm",
        K.layernorm,
        {"x": Shaped((64, 64)), "w": Shaped((64, 64)), "b": Shaped((64, 64))},
        {},
    ),
    ("softmax", K.softmax, {"x": Shaped((64, 64))}, {}),
    ("silu", O.silu, {"x": Shaped((4096,))}, {}),
    ("residual", O.residual, {"a": Shaped((64, 64)), "b": Shaped((64, 64))}, {}),
    (
        "group_norm",
        K.group_norm,
        {"x": Shaped((8, 128)), "w": Shaped((8, 128)), "b": Shaped((8, 128))},
        {},
    ),
    (
        "group_norm_silu",
        K.group_norm_silu,
        {"x": Shaped((8, 128)), "w": Shaped((8, 128)), "b": Shaped((8, 128))},
        {},
    ),
    (
        "flash_attention",
        K.flash_attention,
        {"q": Shaped((64, 64)), "k": Shaped((64, 64)), "v": Shaped((64, 64))},
        {"block": 64},
    ),
    (
        "flash_attention_causal",
        K.flash_attention,
        {"q": Shaped((128, 64)), "k": Shaped((128, 64)), "v": Shaped((64, 128))},
        {"block": 64, "causal": True},
    ),
]


@pytest.mark.parametrize(
    ("name", "fn", "bound", "knobs"), LIBRARY, ids=[row[0] for row in LIBRARY]
)
def test_no_shipped_kernel_reads_what_its_own_stage_wrote(name, fn, bound, knobs):
    """The survey, as a test: compiling at all now means the guard was satisfied.

    Surveyed before turning the guard on -- 33 cases including six tilings, both
    conv2d shapes and the causal and head-axis attention forms -- and nothing
    was refused. `.plan/measurements/hazard-survey.md`.
    """
    assert fn.compile(MACHINE, iface.solve(fn.signature, bound), **knobs).stages


def test_a_read_after_write_across_two_matmuls_is_refused():
    """Two matmuls in ONE `units` block, the second reading the first's result.

    Without this the survey above proves nothing: it would pass on a guard that
    never fired, which is exactly the state this replaced.
    """

    @kernel
    def chained(a=L.In(M, KD), b=L.In(N, KD), c=L.Out(M, N), *, gm=2, gn=1, nk=2):
        t = L.temp(M, N)
        with units(a.tiles(gm), b.tiles(gn)) as (i, j):
            acc = L.tile(gm, gn, nk)
            for k in loop(a.chunks32(nk)):
                acc += a[i, k] @ b[j, k]
            t[i, j] <<= acc
            acc2 = L.tile(gm, gn, nk)
            for k in loop(t.chunks32(nk)):
                acc2 += t[i, k] @ b[j, k]
            c[i, j] <<= acc2

    bound = {"a": Shaped((16, 64)), "b": Shaped((64, 64))}
    with pytest.raises(KernelError, match="reads 't1'"):
        chained.compile(MACHINE, iface.solve(chained.signature, bound))


def test_the_refusal_says_which_two_statements_clash():
    """A stage of 256 statements needs the message to name both, not just fail.

    The reader takes a ROW-OFFSET view, so it reads bytes the writer did not
    write and no register can carry them. That is the surviving vector hazard
    now that a band sequences a chain reading exactly what one before it wrote.
    """

    @kernel
    def deps(x=L.In(M, N), w=L.In(M, N), y=L.Out(M, N), *, part=8192):
        t = L.temp(M, N)
        with units(x.parts(part)) as e:
            t[e] <<= x[e] * w[e]
            y[e] <<= t.rows_from(1)[e] + x[e]

    bound = {"x": Shaped((16, 64)), "w": Shaped((16, 64))}
    with pytest.raises(KernelError) as why:
        deps.compile(MACHINE, iface.solve(deps.signature, bound))
    assert "statement 1 reads" in str(why.value)
    assert "statement 0 in the same stage wrote" in str(why.value)


def test_a_dependent_chain_on_one_unit_is_one_program():
    """The relaxation: the same pair reading exactly what was written is legal.

    A band runs its chains in order and hands `t` over in a register, so this
    is one stage and one image rather than two programs racing through DRAM.
    """

    @kernel
    def deps(x=L.In(M, N), w=L.In(M, N), y=L.Out(M, N), *, part=8192):
        t = L.temp(M, N)
        with units(x.parts(part)) as e:
            t[e] <<= x[e] * w[e]
            y[e] <<= t[e] + x[e]

    bound = {"x": Shaped((16, 64)), "w": Shaped((16, 64))}
    got = deps.compile(MACHINE, iface.solve(deps.signature, bound))
    assert len(got.stages) == 1
    assert [s.kind for s in got.stages[0].instances[0].stmts] == ["apply", "apply"]
