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

#: `count:sha256[:16]`, captured 2026-08-13. `mm` and `residual` carry over
#: from the shipped library's own witness unchanged.
BEFORE = {
    "chained": "466:185ede3a4bf1beae",
    "masked": "132:fe2c2b0e0baf99b7",
    "mm": "16:c8c20d2e748a004a",
    "mm_silu": "300:88ed0def5a4883a3",
    "residual": "66:236cedb85ea340e5",
    "rownorm": "522:bcd19a2f514f1bd0",
    "scale": "66:e8e97306f3491402",
    "staged_norm": "850:9b2e77f6dfb3aec1",
}


@pytest.mark.parametrize("name", sorted(WITNESSED))
def test_the_fixtures_emit_what_they_always_emitted(name):
    """The witness. A digest that moves means machine code changed."""
    kern, shapes, knobs = WITNESSED[name]
    assert digest(kern, operands(*shapes), **knobs) == BEFORE[name]


def test_the_witness_carried_over_from_the_shipped_library_unchanged():
    """Corroboration that a fixture is a faithful stand-in, not a weaker one.

    These two digests were captured against `kohakutpu.kernels.matmul` and
    `.residual` before this file was repointed. Same DSL text emits the same
    words, so isolating the test cost no coverage on them.
    """
    assert BEFORE["mm"] == "16:c8c20d2e748a004a"
    assert BEFORE["residual"] == "66:236cedb85ea340e5"
