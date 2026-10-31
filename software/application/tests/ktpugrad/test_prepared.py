"""Operands made inside the kernel that reads them (`forms.Pending`).

A matmul side laid out (a transpose), a window's padded copy and lay, a
packing's column padding (`forms.packed`), and a rows kernel's laid read
(`run._bind`), down the rows read twice over (`tree.column`): each a
relayout step of its consumer's kernel, not a launch of its own.

Offline: launch kinds -- with `forms.PREPARED` and admitted tree recipes,
every case runs no convert and no move; without preparation the expressions
run the lays as their own launches. On the card model: values equal numpy, and equal the
unprepared run within the same numerical budget; the mutants that skip
steps, read stale flat storage, or alias the two halves of a twice-read
buffer are caught there.
"""

from collections import Counter
from dataclasses import replace
from math import prod

import numpy as np
import pytest
from tinygrad.tensor import Tensor

from kohakutpu.application.ktpugrad import run as R
from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime import forms as FM
from kohakutpu.application.runtime import rows as RW

RNG = np.random.default_rng(1)
A = RNG.standard_normal((40, 48)).astype(np.float16)
B = RNG.standard_normal((48, 72)).astype(np.float16)
X = RNG.standard_normal((36, 40)).astype(np.float16)
Y = RNG.standard_normal((40, 36)).astype(np.float16)
# A sum down the rows of a permuted read (`tree.column`, its first level
# reading the made buffer's two halves); the halves differ, so a sum reading
# one of them twice is off by 4 rows x 4 x 3 in every column.
XP = RNG.standard_normal((4, 8, 64)).astype(np.float16)
XP[:, 4:] += 4
IMG = RNG.standard_normal((2, 3, 10, 10)).astype(np.float16)
WT = (RNG.standard_normal((4, 3, 3, 3)) / 4).astype(np.float16)
G = RNG.standard_normal((8, 6, 28, 28)).astype(np.float16)


def T(a):
    return Tensor(a, device="KTPU")


def f(a):
    return np.asarray(a, np.float32)


def conv_ref(img, w, pad):
    x = np.pad(f(img), ((0, 0), (0, 0), (pad, pad), (pad, pad)))
    n, _c, h, wd = x.shape
    o, _, kh, kw = w.shape
    out = np.zeros((n, o, h - kh + 1, wd - kw + 1), dtype=np.float32)
    for i in range(kh):
        for j in range(kw):
            patch = x[:, :, i : i + out.shape[2], j : j + out.shape[3]]
            out += np.einsum("nchw,oc->nohw", patch, f(w)[:, :, i, j])
    return out


#: name -> (tinygrad expression, numpy reference, MXFP7 product: compared
#: within the attention ruling's 0.03, else within f16 sums' 2e-3)
CASES = {
    "matmul_b_transposed": (lambda: T(A) @ T(B), lambda: f(A) @ f(B), True),
    "matmul_a_transposed": (lambda: T(Y).T @ T(X).T, lambda: f(Y).T @ f(X).T, True),
    "conv_padded": (
        lambda: T(IMG).conv2d(T(WT), padding=1),
        lambda: conv_ref(IMG, WT, 1),
        True,
    ),
    "rows_transposed_sum": (
        lambda: (T(X).T * 2).sum(-1),
        lambda: (f(X).T * 2).sum(-1),
        False,
    ),
    "rows_along_sum": (lambda: (T(Y) * 3).sum(0), lambda: (f(Y) * 3).sum(0), False),
    "rows_down_sum": (
        lambda: (T(XP).permute(1, 0, 2) * 3).sum(0),
        lambda: (f(XP).transpose(1, 0, 2) * 3).sum(0),
        False,
    ),
    "channel_sum": (
        lambda: (T(G) * T(G)).sum(axis=(0, 2, 3)),
        lambda: (f(G) * f(G)).sum(axis=(0, 2, 3)),
        False,
    ),
}


def kinds(monkeypatch, make, prepared: bool) -> Counter:
    """Launch kinds of one run of `make` on an offline device."""
    monkeypatch.setattr(FM, "PREPARED", prepared)
    seen = Counter()
    run = D.Device.run

    def counted(self, compiled, binds, kind="dsl", info=None):
        seen[kind] += 1
        return run(self, compiled, binds, kind=kind, info=info)

    monkeypatch.setattr(D.Device, "run", counted)
    dev = D.Device(kind="offline", board="multimesh_v9t6")
    with D.attached(dev):
        make().realize()
        dev.drain()
    return seen


@pytest.mark.parametrize("name", sorted(CASES))
def test_lays_run_inside_their_consumers(monkeypatch, name):
    if CASES[name][2]:
        pytest.skip("quant-side fusion is owned by QPACK and disabled")
    # Exercise prepared-tree emission with the cost decision admitted. Cost
    # admission/rejection is independently witnessed in test_pipe_folding.
    monkeypatch.setattr(
        FM.costs, "choose", lambda kind, recipe, board: kind == "prepared_tree"
    )
    made = kinds(monkeypatch, CASES[name][0], True)
    assert made["convert"] == 0 and made["move"] == 0, made


@pytest.mark.parametrize("name", sorted(CASES))
def test_mutant_unprepared_lays_are_launches(monkeypatch, name):
    """Without `PREPARED` the same expressions launch their lays."""
    alone = kinds(monkeypatch, CASES[name][0], False)
    assert alone["convert"] + alone["move"] > 0, alone


def run_card(make, prepared: bool, monkeypatch) -> np.ndarray:
    monkeypatch.setattr(FM, "PREPARED", prepared)
    return make().numpy()


def close(got, want) -> float:
    """Largest error relative to the largest value; inf when a value is not
    finite (a NaN is a difference, never a match)."""
    g, w = np.asarray(got, np.float64), np.asarray(want, np.float64)
    if not np.isfinite(g).all():
        return float("inf")
    return float(np.abs(g - w).max() / max(np.abs(w).max(), 1e-9))


@pytest.mark.parametrize("name", sorted(CASES))
def test_prepared_on_the_card(dev, monkeypatch, name, record_property):
    """The card (model): numpy (float32) within the case's budget -- an
    MXFP7 product the attention ruling's 0.03, f16 sums 2e-3 -- and the
    unprepared run within the same budget of it (a rows kernel made from
    `prepared.rows_kernel` may sum in another order; the owner's ruling,
    2026-10-26: precision in a controlled range, not bit identity). Max
    abs / rel recorded."""
    make, want, mx7 = CASES[name]
    budget = 0.03 if mx7 else 2e-3
    got = run_card(make, True, monkeypatch)
    alone = run_card(make, False, monkeypatch)
    w = want()
    record_property(
        "error",
        {
            "budget": budget,
            "prepared_abs": float(np.abs(f(got) - w).max()),
            "separate_abs": float(np.abs(f(alone) - w).max()),
            "prepared_rel": close(got, w),
            "separate_rel": close(alone, w),
            "between_rel": close(got, alone),
            "between_abs": float(
                np.abs(
                    np.asarray(got, np.float64) - np.asarray(alone, np.float64)
                ).max()
            ),
        },
    )
    assert close(got, w) < budget
    assert close(alone, w) < budget
    assert close(got, alone) < budget


@pytest.mark.parametrize("stale", [False, True], ids=["fixed", "mutant"])
def test_ordered_laid_read_on_the_card(dev, monkeypatch, stale, record_property):
    """The original session order; mutant bypasses only the laid-read form check."""
    FM.hints(dev).clear()
    for name in (
        "channel_sum",
        "conv_padded",
        "matmul_a_transposed",
        "matmul_b_transposed",
    ):
        for prepared in (True, False):
            got = run_card(CASES[name][0], prepared, monkeypatch)
            assert close(got, CASES[name][1]()) < (0.03 if CASES[name][2] else 0.002)
    bare, f16 = R._bare, FM.f16
    made, bypassed = [], []

    def remember(device, plan, bufs):
        laid = isinstance(plan.out_slot, tuple) and plan.out_slot[0] == "laid"
        if laid:
            st = bufs[plan.out_slot]
            # Reused storage may contain anything; poison before the producer.
            device.write(st.addr, np.full(st.nbytes // 2, np.nan, np.float16).tobytes())
            made.append(st)
        return bare(device, plan, bufs)

    def stale_read(device, storage, dtype, n):
        if stale and any(storage is s for s in made):
            bypassed.append(FM.padded(storage))
            return storage.addr
        return f16(device, storage, dtype, n)

    monkeypatch.setattr(R, "_bare", remember)
    monkeypatch.setattr(FM, "f16", stale_read)
    # The checkpoint called this Y reduction rows_down_sum; now rows_along_sum.
    make, want, _ = CASES["rows_along_sum"]
    prepared = run_card(make, True, monkeypatch)
    separate = run_card(make, False, monkeypatch)
    err = close(separate, want())
    record_property("separate_rel", err)
    assert close(prepared, want()) < 0.002
    assert made
    if stale:
        assert any(key is not None for key in bypassed)
        assert err > 0.002
    else:
        assert err < 0.002


def test_mutant_steps_skipped_is_caught_on_the_card(dev, monkeypatch):
    """The steps' source walks lost: the consumer reads the source as it lies."""
    prepared = RW._prepared
    ran = []

    def lost(step):
        counts = [c for c, _ in step.walk]
        walk = tuple((c, prod(counts[i + 1 :])) for i, c in enumerate(counts))
        return replace(step, walk=walk, at=0)

    def as_it_lies(device, e, operands, binds, *rest):
        for v in binds.values():
            if isinstance(v, FM.Pending):
                v.steps = tuple(lost(s) for s in v.steps)
        ran.append(prepared(device, e, operands, binds, *rest))
        return ran[-1]

    monkeypatch.setattr(RW, "_prepared", as_it_lies)
    make, want, _ = CASES["rows_transposed_sum"]
    got = run_card(make, True, monkeypatch)
    assert ran and None not in ran
    assert close(got, want()) > 2e-3


def test_mutant_second_half_from_the_first_is_caught_on_the_card(dev, monkeypatch):
    """Down the rows level by level: the twice-read buffer's second reader at
    the first's offset (the alias reading the top half twice). One package
    reads no buffer twice, so this tree needs it off."""
    orig = FM.generated.launch
    moved = []

    def unshifted(device, generator, arguments, bindings, **kw):
        for i, j in arguments.get("alias", {}).items():
            arguments["operands"][int(i)] = arguments["operands"][j]
            moved.append(i)
        return orig(device, generator, arguments, bindings, **kw)

    monkeypatch.setenv("KTPU_SINGLE_PACKAGE_AST", "0")
    monkeypatch.setattr(FM.generated, "launch", unshifted)
    make, want, _ = CASES["rows_down_sum"]
    got = run_card(make, True, monkeypatch)
    assert moved
    assert close(got, want()) > 2e-3


def test_mutant_one_package_tree_reads_one_half_is_caught_on_the_card(dev, monkeypatch):
    """Down the rows in one package: the first level's second half read from
    the first's rows."""
    monkeypatch.setattr(RW, "MUTANT", "wrong_order")
    make, want, _ = CASES["rows_down_sum"]
    got = run_card(make, True, monkeypatch)
    assert close(got, want()) > 2e-3
