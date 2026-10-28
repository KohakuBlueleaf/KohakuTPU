"""Padded rows read where they are (offline launch counts).

A product of N columns not whole words leaves its output as padded rows
(`forms.ROWS`). A product's epilogue reading those rows at least its padded
N wide takes them as they are (`run._rows_form`: no compaction, no
re-padding); an MXFP7 packing of them pads straight from them
(`forms._held_rows`: one relayout, no compaction). Each witness is killed
by its mutant (the helper answering None)."""

import numpy as np
import pytest
from tinygrad.tensor import Tensor

from kohakutpu.application.ktpugrad import run as RN
from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime import forms as FM

RNG = np.random.default_rng(4)
X = RNG.standard_normal((32, 64)).astype(np.float16)
Y = RNG.standard_normal((32, 64)).astype(np.float16)
W84 = (RNG.standard_normal((84, 64)) / 8).astype(np.float16)
W2 = (RNG.standard_normal((84, 48)) / 8).astype(np.float16)


def T(a):
    return Tensor(a, device="KTPU")


def epilogue():
    """``Y W84^T * a``, `a` a realized product of 84 columns."""
    a = (T(X) @ T(W84).T).relu().realize()
    return lambda: T(Y) @ T(W84).T * a


def packing():
    """`a` (84 columns) the A side of a product (K = 84)."""
    a = (T(X) @ T(W84).T).relu().realize()
    return lambda: a @ T(W2)


def moves(monkeypatch, case) -> tuple:
    """``(compactions, relayouts)`` of the second step of `case` offline."""
    seen = {"compact": 0, "relayout": 0}
    compact, relayout = FM.compact, FM.relayout

    def counted(name, fn):
        def call(*a, **k):
            seen[name] += 1
            return fn(*a, **k)

        return call

    dev = D.Device(kind="offline", board="multimesh_v9t6")
    with D.attached(dev):
        second = case()
        monkeypatch.setattr(FM, "compact", counted("compact", compact))
        monkeypatch.setattr(FM, "relayout", counted("relayout", relayout))
        second().realize()
        dev.drain()
    monkeypatch.setattr(FM, "compact", compact)
    monkeypatch.setattr(FM, "relayout", relayout)
    return seen["compact"], seen["relayout"]


def f(a):
    return np.asarray(a, np.float64)


A = np.maximum(f(X) @ f(W84).T, 0)
WANT = {"epilogue": (f(Y) @ f(W84).T) * A, "packing": A @ f(W2)}


@pytest.mark.parametrize("case", [epilogue, packing], ids=["epilogue", "packing"])
def test_padded_reads_on_the_card(dev, rel, case):
    """The same programs on the card (model) against float64 numpy (two
    MXFP7 products: the attention ruling's 0.03)."""
    got = case()().numpy()
    assert rel(got, WANT[case.__name__]) < 0.03


@pytest.mark.parametrize(
    "case, saved, module, helper",
    [(epilogue, 2, RN, "_rows_form"), (packing, 1, FM, "_held_rows")],
    ids=["epilogue", "packing"],
)
def test_padded_rows_are_read_in_place(monkeypatch, case, saved, module, helper):
    """No compaction; the mutant compacts and makes `saved` relayouts more
    (the epilogue's: compaction and re-padding; the packing's: compaction)."""
    compacted, relayouts = moves(monkeypatch, case)
    assert compacted == 0
    monkeypatch.setattr(module, helper, lambda *a, **k: None)
    assert moves(monkeypatch, case) == (1, relayouts + saved)
