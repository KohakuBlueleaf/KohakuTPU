"""Every cluster FILL's address: the shipped kernels' flits, and the bound.

`Slice` can carry a lane offset that `TpuBackend._cluster` adds to the fill
address; no shipped kernel asks for one. The last test checks the bound.
"""

import hashlib

import pytest
from kohakuaccel.lang import iface
from kohakuaccel.machinespec import MachineSpec
from kohakutpu.hw import tensor as T
from kohakutpu.isa import ISA
from kohakutpu.lang import BACKEND
from kohakutpu.lang.errors import LangError

from kohakutpu import kernels as K
from kohakutpu import ops as O

MACHINE = MachineSpec(
    name="witness",
    units={
        "MG": ((1, 1), (2, 1), (1, 2), (2, 2), (1, 3), (2, 3)),
        "VC": ((2, 0), (2, 4)),
    },
    inst_depth=512,
    agent=(1, 0),
)

#: SHA-256 over every flit of every kernel below; the byte-identity claim.
#: With MXFP7 fills (128-B entry steps) and constant writes that read no buffer.
BASELINE = "cfd57aedd248dcd61b6df2469086e8148851efd6d825777f3dff152b18feca91"


class Shaped:
    def __init__(self, shape) -> None:
        self.shape = shape


#: Kernels and shapes covering both units and every fill this backend emits.
LIBRARY = [
    ("matmul", O.matmul, {"a": Shaped((64, 128)), "b": Shaped((32, 128))}, {}),
    (
        "batched_matmul",
        O.matmul,
        {"a": Shaped((4, 32, 64)), "b": Shaped((32, 64))},
        {},
    ),
    ("linear_silu", K.linear_silu, {"x": Shaped((32, 64)), "w": Shaped((64, 64))}, {}),
    ("rmsnorm", K.rmsnorm, {"x": Shaped((64, 64)), "w": Shaped((64, 64))}, {}),
    ("softmax", K.softmax, {"x": Shaped((64, 64))}, {}),
    ("silu", O.silu, {"x": Shaped((4096,))}, {}),
    ("residual", O.residual, {"a": Shaped((64, 64)), "b": Shaped((64, 64))}, {}),
    (
        "flash_attention",
        K.flash_attention,
        {"q": Shaped((64, 64)), "k": Shaped((64, 64)), "v": Shaped((64, 64))},
        {"block": 64},
    ),
]


def encoded(fn, bound, knobs):
    """Every flit `fn` compiles to at these shapes, in a settled order."""
    got = fn.compile(MACHINE, iface.solve(fn.signature, bound), **knobs)
    names = [p.name for p in fn.signature.ports] + list(got.temps)
    names += list(BACKEND.constants(got))
    addrs = {n: 0x1000 * (i + 1) for i, n in enumerate(names)}
    out = []
    for stage in got.stages:
        for at, words in sorted(BACKEND.encode(got, stage, addrs).items()):
            out.append((stage.index, at, tuple(words)))
    return got, out


#: BASELINE with the fused drain on: each last GEMM before a memory DRAIN carries
#: emit=1 and its address.
FUSED = "05aa453823c07833dcb633378384c6990903e65a661e97d44bd7c6d19b920f0c"


def library_digest() -> str:
    h = hashlib.sha256()
    for name, fn, bound, knobs in LIBRARY:
        h.update(name.encode())
        for index, at, words in encoded(fn, bound, knobs)[1]:
            # Hex, not bytes: a vector flit carries a routing header above the
            # 256-bit payload and does not fit a fixed width.
            h.update(f"{index}{at}{[f'{w:x}' for w in words]}".encode())
    return h.hexdigest()


def test_the_shipped_kernels_are_byte_identical(monkeypatch):
    """The witness. Every flit of every kernel, against a digest predating this.

    A failure means a kernel that asked for no offset moved, which is the one
    thing the offset promised not to do. It also fires when a kernel's own
    default tiling is retuned, which is legitimate -- re-baseline deliberately,
    after checking `test_a_fill_with_no_offset_keeps_the_old_address` is green,
    since that one holds across a retune and is the durable claim.
    """
    monkeypatch.setattr(type(BACKEND), "fuse_drain", False)
    assert library_digest() == BASELINE


def test_the_fused_drain_witness(monkeypatch):
    """With the fused drain on, the library is pinned to its own digest."""
    monkeypatch.setattr(type(BACKEND), "fuse_drain", True)
    assert library_digest() == FUSED


@pytest.mark.parametrize("name", [row[0] for row in LIBRARY])
def test_a_fill_with_no_offset_keeps_the_old_address(name):
    """The formula this replaced, written out and checked against the encoder.

    Robust where the digest is brittle: this keeps holding when a kernel changes
    shape, and it is the property that actually has to be true.
    """
    _, fn, bound, knobs = next(row for row in LIBRARY if row[0] == name)
    got, program = encoded(fn, bound, knobs)
    addrs = {}
    names = [p.name for p in fn.signature.ports] + list(got.temps)
    names += list(BACKEND.constants(got))
    for i, n in enumerate(names):
        addrs[n] = 0x1000 * (i + 1)

    seen = 0
    for stage in got.stages:
        if stage.unit != "MG":
            continue
        for inst in stage.instances:
            for s in inst.stmts:
                if s.kind != "fill":
                    continue
                span = s.args["groups"] * s.args["blocks"]
                at = (s.args["tile"] * s.args["chunks"] + s.args["chunk"]) * span
                # A cluster fills MXFP7 entries, so the step is theirs.
                want = addrs[s.args["operand"]] + at * T.MXFP7_ENTRY_BYTES
                word = ISA.fill(
                    addr=want,
                    n=span,
                    sel=s.args["sel"],
                    fbank=int(s.args.get("step", s.args["chunk"])) % 2,
                )
                assert word in {w for _, _, ws in program for w in ws}
                seen += 1
    if any(stage.unit == "MG" for stage in got.stages):
        assert seen, f"{name} encoded no fill to check"


# ------------------------------------------------------------------ the bound
def test_a_fill_that_reaches_past_the_operand_is_refused():
    """The bound check counts the offset, in bytes, or a tap reads the arena.

    Off by a quarter entry is exactly the size the old entry-based check could
    not see, so the pair either side of the boundary is the point.
    """
    from kohakutpu.lang.backend import LANE_BYTES, _within

    bound = {"a": Shaped((64, 128)), "b": Shaped((32, 128))}
    got = O.matmul.compile(MACHINE, iface.solve(O.matmul.signature, bound))
    held = got.layouts["a"].nbytes(got.block("a"))
    span = held // got.layouts["a"].entry_bytes

    _within(got, "a", 0, span, 0)
    with pytest.raises(LangError, match="reaches byte"):
        _within(got, "a", 0, span, LANE_BYTES)
