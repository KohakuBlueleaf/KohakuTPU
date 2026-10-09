"""Every fixture's instruction words, hashed, so a compiler rewrite cannot drift.

Standing rule: a change that does not use a new feature emits a BYTE-IDENTICAL
program. A failure here is not a style question -- it means machine code moved,
and the diff is what `flits_of` returns.

The vehicle is `tests/fixtures.py`, not the shipped library (rule 2b), and is
no weaker for it: `mm` and `residual` hash to the SAME digests the shipped
`matmul` and `residual` witnessed before this file was repointed.

**The fixture corpus is frozen.** Editing a kernel there moves these digests,
which is a real signal only if the compiler moved instead.
"""

import hashlib

import numpy as np
import pytest
from kohakutpu.lang.backend import BACKEND
from kohakutpu.model import SimDevice

from tests import fixtures as F


def operands(*shapes, seed=11):
    """Deterministic fp16 arrays, so a digest depends only on the compiler."""
    rng = np.random.default_rng(seed)
    return [np.asarray(rng.normal(0, 1, s), np.float16) for s in shapes]


def flits_of(kern, arrays, **knobs) -> tuple[list[int], list]:
    """Every instruction word this kernel compiles to, and its folded scalars.

    Addresses are assigned by name rather than by the arena, so the digest
    covers the instruction stream and not where a buffer happened to land. The
    constants ride along because a folded scalar's VALUE is in a DRAM array
    rather than in any instruction -- two kernels differing only in a constant
    emit identical words.
    """
    dev = SimDevice(size=256 << 20)
    compiled = kern.plan(*[dev.tensor(a) for a in arrays], **knobs)
    consts = BACKEND.constants(compiled) or {}
    names = list(compiled.layouts) + list(consts)
    addrs = {n: 0x10000 * (i + 1) for i, n in enumerate(sorted(names))}
    out: list[int] = []
    for stage in compiled.stages:
        words = BACKEND.encode(compiled, stage, addrs)
        for at in sorted(words):
            out += words[at]
    folded = [(n, float(consts[n][0].reshape(-1)[0])) for n in sorted(consts)]
    return out, folded


def digest(kern, arrays, **knobs) -> str:
    words, folded = flits_of(kern, arrays, **knobs)
    body = b"".join(int(w).to_bytes(36, "little") for w in words)
    body += repr(folded).encode()
    return f"{len(words)}:{hashlib.sha256(body).hexdigest()[:16]}"


#: kernel -> (operand shapes, knobs). Fixed forever: the digest is only a
#: witness if the inputs never move.
WITNESSED = {
    "mm": (F.mm, [(64, 64), (64, 64)], {}),
    "mm_silu": (F.mm_silu, [(64, 64), (64, 64)], {}),
    "rownorm": (F.rownorm, [(64, 64)], {}),
    "chained": (F.chained, [(64, 64)], {}),
    "staged_norm": (F.staged_norm, [(64, 64)] * 3, {}),
    "masked": (F.masked, [(2, 64, 64)], {"block": 64}),
    "scale": (F.scale, [(64, 64)], {}),
    "residual": (F.residual, [(64, 64), (64, 64)], {}),
}

#: `count:sha256[:16]`. `residual` carries over from the shipped library's own
#: witness; `mm` and `mm_silu` are taken with MXFP7 fills (128-B entry steps).
BEFORE = {
    "chained": "251:7fa7ebd304581f4a",
    "masked": "316:05d44b764f5b4dda",
    "mm": "16:2ed39010efd4d399",
    "mm_silu": "316:2abce6f7816e8164",
    "residual": "158:791e5f27c85c7703",
    "rownorm": "288:39ff619bb5d7804c",
    "scale": "122:ea7d8be3e51246b2",
    "staged_norm": "481:524d3f0d39e704dc",
}


#: Digests with the fused drain on (the last GEMM before a memory DRAIN
#: re-encoded with emit=1, the DRAIN with fuse=1).
FUSED = {
    "mm": "16:681fdc65621ff869",
}


@pytest.mark.parametrize("name", sorted(WITNESSED))
def test_the_fixtures_emit_what_they_always_emitted(name, monkeypatch):
    """The witness. A digest that moves means machine code changed."""
    monkeypatch.setattr(type(BACKEND), "fuse_drain", False)
    kern, shapes, knobs = WITNESSED[name]
    assert digest(kern, operands(*shapes), **knobs) == BEFORE[name]


@pytest.mark.parametrize("name", sorted(WITNESSED))
def test_the_fused_drain_moves_only_what_it_fuses(name, monkeypatch):
    """With the fused drain on, a kernel without a GEMM -> DRAIN pair is unmoved."""
    monkeypatch.setattr(type(BACKEND), "fuse_drain", True)
    kern, shapes, knobs = WITNESSED[name]
    assert digest(kern, operands(*shapes), **knobs) == FUSED.get(name, BEFORE[name])


def test_the_fixture_emits_what_the_shipped_kernel_emits():
    """Corroboration that a fixture is a faithful stand-in, not a weaker one.

    Same statement as `kohakutpu.ops.residual`, so the same words: isolating the
    witness from the library cost no coverage on it.
    """
    from kohakutpu import ops

    arrays = operands((64, 64), (64, 64))
    shipped = digest(ops.residual, arrays)
    assert shipped == digest(F.residual, arrays) == BEFORE["residual"]
