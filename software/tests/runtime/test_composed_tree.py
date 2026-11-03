"""A reduction down the rows reads an expanded operand through its relayout
(`tree.column`, composed): the expansion is never written, and the sums keep
the error statistics of the sums over the written expansion."""

import numpy as np
import pytest
from tinygrad.tensor import Tensor

from kohakutpu.application.runtime import forms as FM
from kohakutpu.application.runtime import tree

R = np.random.default_rng(2)


def kinds(dev, run):
    """The kinds of the launches `run` submits."""
    seen = []
    submit = dev.run

    def counted(compiled, bindings, kind, info):
        seen.append(kind)
        return submit(compiled, bindings, kind, info)

    dev.run = counted
    try:
        run()
    finally:
        dev.run = submit
    return seen


@pytest.fixture
def composed(monkeypatch):
    monkeypatch.setattr(FM, "COMPOSED", "on")
    monkeypatch.setattr(tree, "COMPOSED", True)


#: [rows, columns, copies]: each column read `copies` times in a row.
SHAPES = [(16, 64, 8), (16, 128, 64), (12, 96, 16), (7, 64, 4)]


def expanded(dev, a, b, copies) -> tuple:
    """``(sums, launch kinds)`` of the rows of `a`, each column `copies` times,
    times `b`."""
    rows, cols = a.shape
    x = Tensor(a.astype(np.float16), device="KTPU").realize()
    v = Tensor(b.astype(np.float16), device="KTPU").realize()
    wide = x.reshape(rows, cols, 1).expand(rows, cols, copies).reshape(rows, -1)
    got = {}
    seen = kinds(dev, lambda: got.update(y=(wide * v).sum(0).numpy()))
    return got["y"], seen


def stats(got, a, b, copies) -> dict:
    """Error statistics against the f32 sums."""
    want = (np.repeat(a, copies, axis=1) * b).sum(0)
    rel = np.abs(got.astype(np.float64) - want) / np.abs(want).max()
    return dict(max=rel.max(), mean=rel.mean(), median=float(np.median(rel)))


def test_the_decision_compiles_nothing_and_several_composed_reads_lower(
    dev, composed, monkeypatch
):
    def never(*_):
        raise AssertionError("a composed tree decided by a compile")

    monkeypatch.setattr(FM, "_decided", never)
    rows, cols, copies = 16, 32, 4
    a, c = R.normal(size=(rows, cols)), R.normal(size=(rows, cols))
    b = R.normal(size=cols * copies)

    def wide(m):
        t = Tensor(m.astype(np.float16), device="KTPU").realize()
        return t.reshape(rows, cols, 1).expand(rows, cols, copies).reshape(rows, -1)

    v = Tensor(b.astype(np.float16), device="KTPU").realize()
    got = {}
    seen = kinds(dev, lambda: got.update(y=(wide(a) * wide(c) * v).sum(0).numpy()))
    assert "convert" not in seen, seen
    want = (np.repeat(a, copies, axis=1) * np.repeat(c, copies, axis=1) * b).sum(0)
    rel = np.abs(got["y"].astype(np.float64) - want) / np.abs(want).max()
    assert rel.max() <= 2.0**-11 * rows * 4, dict(max=rel.max(), mean=rel.mean())


@pytest.mark.parametrize("shape", SHAPES)
def test_an_expanded_operand_is_read_composed(dev, composed, monkeypatch, shape):
    rows, cols, copies = shape
    a, b = R.normal(size=(rows, cols)), R.normal(size=cols * copies)
    got, seen = expanded(dev, a, b, copies)
    monkeypatch.setattr(tree, "COMPOSED", False)
    written, before = expanded(dev, a, b, copies)
    assert "convert" in before, before
    assert "convert" not in seen and len(seen) < len(before), (seen, before)
    # The trees fold in different orders: each within the f16 sums' error of
    # the other (f16's half ulp a fold), never a misread column.
    ours, theirs = stats(got, a, b, copies), stats(written, a, b, copies)
    assert ours["max"] <= 2 * max(theirs["max"], 2.0**-11 * rows), (ours, theirs)
