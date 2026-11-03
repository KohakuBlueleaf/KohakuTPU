"""A relayout's output left to its reader (`forms.defer`): a packing reads it through
the relayout, anything else has it made first, and every write keeps what was read."""

from typing import cast

import numpy as np
import pytest
from tinygrad.device import Buffer
from tinygrad.tensor import Tensor

from kohakutpu.application.kernels.vector import Operand
from kohakutpu.application.ktpugrad.run import kernels as K
from kohakutpu.application.runtime import forms as FM
from kohakutpu.application.runtime.memory.storage import Storage, StorageView

R = np.random.default_rng(1)


def same(monkeypatch, body, *arrays):
    """`body(*arrays)`'s outputs with the relayout deferred equal, byte for byte,
    the same steps with it made first: a relayout is an exact permutation."""
    got = [o.tobytes() for o in body(*arrays)]
    monkeypatch.setattr(FM, "DEFERRED", False)
    want = [o.tobytes() for o in body(*arrays)]
    monkeypatch.setattr(FM, "DEFERRED", True)
    assert got == want


def made(a):
    """`a` as a buffer a kernel computed (an upload's bytes are kept unchanged)."""
    return (Tensor(a.astype(np.float16), device="KTPU") * 2.0).realize()


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


#: [6, 5, 7] read as [7, 6 * 5]: an element walk no word move takes.
SHAPE, ORDER = (6, 5, 7), (2, 0, 1)


def permuted(a):
    return a.transpose(ORDER).reshape(SHAPE[2], -1)


def test_a_packing_reads_a_relayout_through_it(dev, composed, monkeypatch):
    a, b = R.normal(size=SHAPE), R.normal(size=(30, 32))

    def body(a, b):
        x, w = made(a), Tensor(b.astype(np.float16), device="KTPU").realize()
        y = x.permute(ORDER).reshape(7, 30).contiguous().realize()
        if FM.DEFERRED:
            assert FM.DEFER in cast(Buffer, y.uop.buffer)._buf.valid
            out = []
            seen = kinds(dev, lambda: out.append((y @ w).numpy()))
            assert "convert" not in seen, seen
            return out
        return [(y @ w).numpy()]

    same(monkeypatch, body, a, b)


def test_a_padded_packing_of_a_deferred_form_reads_its_pad_as_zero(dev, composed):
    """8 rows of 92 values a relayout makes, packed 96 wide: the composed read
    of the deferred form equals the packing of the made form byte for byte,
    though the source is followed by non-finite bytes."""
    owner, rows, made_rows = Storage(dev, 2 * 768 + 8192), [], []
    values = R.normal(size=768).astype(np.float16)
    tail = np.full(4096, np.nan, np.float16)
    dev.write(owner.addr, np.concatenate([values, tail]).tobytes())
    step = FM.Step(owner.addr, 768, ((8, 96), (92, 1)), 0, ((8, 92), (92, 1)), 0, 736)
    args = (np.float16, Operand("full", pitch=92), 8, 92, 8, 96, "a", 2, 1)
    for out in (rows, made_rows):
        dst = Storage(dev, 2 * 736)
        FM.defer(dev, dst, owner, step)
        if out is made_rows:
            dst.forms[FM.DEFER].make()
        out.append(dev.read(FM.packed(dev, dst, *args), 8 * 96))
    assert rows == made_rows


@pytest.mark.parametrize(
    "choice, part", [("tabled", "_uploaded"), ("materialise", "_materialise")]
)
def test_a_plan_that_becomes_other_plans_leaves_deferred_reads_to_them(
    monkeypatch, choice, part
):
    calls = []
    monkeypatch.setattr(K, "_vector_choice", lambda plan: (choice, plan))
    monkeypatch.setattr(K, "_made_reads", lambda plan, bufs: calls.append("made"))
    monkeypatch.setattr(K, part, lambda dev, plan, bufs: calls.append(part) or [])
    K._vector(None, cast(K.Plan, object()), {})
    assert calls == [part]


def test_a_relayout_without_deferral_is_its_own_launch(dev, composed, monkeypatch):
    monkeypatch.setattr(FM, "DEFERRED", False)
    a = R.normal(size=SHAPE)
    x = made(a)
    seen = kinds(dev, lambda: x.permute(ORDER).reshape(7, 30).contiguous().realize())
    assert "convert" in seen, seen


def relayout(a, b):
    x, w = made(a), Tensor(b.astype(np.float16), device="KTPU").realize()
    return x, w, x.permute(ORDER).reshape(7, 30).contiguous().realize()


def test_a_write_of_the_source_keeps_what_the_relayout_read(dev, composed, monkeypatch):
    def body(a, b):
        x, w, y = relayout(a, b)
        x.assign(x + 1.0).realize()
        return [(y @ w).numpy(), x.numpy()]

    same(monkeypatch, body, R.normal(size=SHAPE), R.normal(size=(30, 32)))


def test_a_write_in_place_of_the_output_reads_it_made(dev, composed, monkeypatch):
    def body(a, b):
        _, w, y = relayout(a, b)
        y.assign(y * 3.0 + 1.0).realize()
        return [(y @ w).numpy()]

    same(monkeypatch, body, R.normal(size=SHAPE), R.normal(size=(30, 32)))


def test_a_partial_upload_to_the_output_keeps_the_rest(dev, composed, monkeypatch):
    def body(a, b):
        _, w, y = relayout(a, b)
        buf = cast(Buffer, y.uop.buffer)
        raw = np.full(30, 5.0, np.float16).tobytes()
        buf.allocator._copyin(StorageView(buf._buf, 60, 60), memoryview(raw))
        return [(y @ w).numpy(), y.numpy()]

    same(monkeypatch, body, R.normal(size=SHAPE), R.normal(size=(30, 32)))


def test_two_packings_and_a_read_back_see_one_relayout(dev, composed, monkeypatch):
    def body(a, b, c):
        _, w, y = relayout(a, b)
        v = Tensor(c.astype(np.float16), device="KTPU").realize()
        return [(y @ w).numpy(), (y @ v).numpy(), y.numpy(), (y + 1.0).numpy()]

    a = R.normal(size=SHAPE)
    same(monkeypatch, body, a, R.normal(size=(30, 32)), R.normal(size=(30, 48)))


def test_an_upload_to_the_source_keeps_what_the_relayout_read(
    dev, composed, monkeypatch
):
    def body(a, b):
        x, w, y = relayout(a, b)
        buf = cast(Buffer, x.uop.buffer)
        raw = np.zeros(SHAPE, np.float16).tobytes()
        buf.allocator._copyin(buf._buf, memoryview(raw))
        return [(y @ w).numpy()]

    same(monkeypatch, body, R.normal(size=SHAPE), R.normal(size=(30, 32)))


def test_a_freed_source_stays_until_its_reader_runs(dev, composed, monkeypatch):
    def body(a, b, junk):
        w = Tensor(b.astype(np.float16), device="KTPU").realize()
        y = made(a).permute(ORDER).reshape(7, 30).contiguous().realize()
        kept = [made(j) for j in junk]
        out = [(y @ w).numpy()]
        assert kept
        return out

    junk = [R.normal(size=SHAPE) for _ in range(4)]
    same(monkeypatch, body, R.normal(size=SHAPE), R.normal(size=(30, 32)), junk)
