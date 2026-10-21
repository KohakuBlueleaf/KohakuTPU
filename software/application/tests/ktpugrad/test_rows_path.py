"""The runner's rows path (`run._rows`) without a card: the view it picks,
how each read is bound (as it is, or laid out W' wide), where the result is
placed, and the forms it leans on. Decisions are read off the plan and the
runs; a layout the mover alone makes is checked for values on the mover's
behaviour model (`simulation.behave`)."""

import numpy as np
import pytest
from tinygrad.dtype import dtypes
from tinygrad.uop.ops import AxisType, UOp

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels.vector import Operand
from kohakutpu.application.ktpugrad import run as R
from kohakutpu.application.ktpugrad.lower import Access, Plan
from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime import forms as FM
from kohakutpu.application.runtime.offline import OfflineCard
from kohakutpu.application.runtime.storage import FLAT, Storage, StorageView
from kohakutpu.simulation.behave.card import Card

LOOP, REDUCE = AxisType.LOOP, AxisType.REDUCE


def rng(n: int, i: int, kind=LOOP):
    return UOp.range(n, i, kind)


def plan(out, expr, reads, red=(), sets=None, strides=None, dtype=dtypes.half):
    return Plan(
        "vector",
        None,
        "t",
        0,
        dtype,
        tuple(r.src[0].arg for r in out),
        tuple(out),
        expr=expr,
        accesses=list(reads),
        reduce_ranges=tuple(red),
        reductions=sets or {},
        out_strides=strides,
    )


def read(slot, *coef, off=0, dtype=dtypes.half, table=None):
    return Access(slot, dtype, tuple(coef), off, table)


def summed(arg, red) -> tuple:
    e = X.Reduce("sum", arg)
    return e, {id(e): tuple(red)}


@pytest.fixture
def dev():
    """An offline card whose packages run on the behaviour model."""
    d = D.Device(kind="offline")
    assert isinstance(d.card, OfflineCard)
    card = Card(d.card.mem, d.card.machine)

    def execute(_, built) -> dict:
        for pkg, bindings in built.values():
            card.run(pkg, bindings)
        return {
            "cycles": 0,
            "steps": [],
            "phases": {},
            "pkg": next(iter(built.values()))[0],
        }

    d.card.execute = execute
    return d


def put(dev, arr) -> Storage:
    s = Storage(dev, max(32, arr.nbytes))
    dev.write(s.addr, arr.tobytes())
    s.host, s.dtype = arr.tobytes(), arr.dtype
    return s


def f16(dev, at: int, n: int) -> np.ndarray:
    return np.frombuffer(dev.read(at, 2 * n), np.float16)


# ------------------------------------------------------------------ takes
@pytest.mark.parametrize(
    "o,Wp,start,want",
    [
        (Operand("full", pitch=64), 64, 0, True),
        (Operand("full", pitch=48), 64, 0, False),  # rows closer than W'
        (Operand("full", pitch=64, offset=8), 64, 0, False),  # off a word
        (Operand("full", pitch=64, offset=16), 48, 0, True),
        (Operand("full", pitch=64, offset=16), 64, 0, False),  # past its pitch
        (Operand("full", pitch=40), 32, 0, False),  # pitch off words
        (Operand("full", pitch=64), 64, 8, False),  # its view off a word
        (Operand("col", offset=16), 64, 0, True),
        (Operand("col", offset=8), 64, 0, False),
        (Operand("col"), 64, 24, False),
        (Operand("row", offset=3), 64, 5, True),
        (Operand("scalar", offset=7), 32, 0, True),
        (None, 32, 0, False),
    ],
)
def test_takes(o, Wp, start, want):
    assert R._takes(o, Wp, start) is want


# ------------------------------------------------------------------- view
def test_a_row_reduction_is_along_the_rows():
    r, c = rng(16, 0), rng(64, 1, REDUCE)
    e, sets = summed(X.Load(0), [c])
    kind, rows, cols = R._view(plan([r], e, [read(1, (r, 64), (c, 1))], [c], sets))
    assert (kind, rows, cols) == ("along", [r], [c])


def test_a_column_reduction_is_down_the_rows():
    r, c = rng(16, 0, REDUCE), rng(64, 1)
    e, sets = summed(X.Load(0), [r])
    kind, rows, cols = R._view(plan([c], e, [read(1, (r, 64), (c, 1))], [r], sets))
    assert (kind, rows, cols) == ("down", [r], [c])


def test_a_reduction_of_everything_has_no_down_view():
    r = rng(40, 0, REDUCE)
    e, sets = summed(X.Load(0), [r])
    one = rng(1, 1)
    kind, _, cols = R._view(plan([one], e, [read(1, (r, 1))], [r], sets))
    assert (kind, cols) == ("along", [r])


def test_elementwise_takes_the_split_laying_out_fewest():
    """(4,1,8) + (1,5,8): at [a | b c] the second read is one row, the first
    alone is laid out; every other split lays out both."""
    a, b, c = rng(4, 0), rng(5, 1), rng(8, 2)
    x, y = read(1, (a, 8), (c, 1)), read(2, (b, 8), (c, 1))
    p = plan([a, b, c], X.op("add", X.Load(0), X.Load(1)), [x, y])
    assert R._view(p) == ("rows", [a], [b, c])
    assert R._laid(p, [a], [b, c], 64) == 1


def test_elementwise_prefers_whole_blocks_on_a_tie():
    """One value broadcast over (4, 20, 8): every split takes it; [a | b c]
    (160 columns) beats [a b | c] (8)."""
    a, b, c = rng(4, 0), rng(20, 1), rng(8, 2)
    p = plan([a, b, c], X.Load(0), [read(1)])
    assert R._view(p) == ("rows", [a], [b, c])


# ------------------------------------------------------------------- bind
def test_a_read_the_kernel_takes_binds_as_it_is(dev):
    r, c = rng(8, 0), rng(64, 1)
    s = put(dev, np.arange(512, dtype=np.float16))
    a = read(1, (r, 64), (c, 1))
    tmps: list = []
    at, o, runs = R._bind(
        dev, plan([r, c], X.Load(0), [a]), a, [r], [c], 64, {1: s}, tmps
    )
    assert (at, o, runs, tmps) == (s.addr, Operand("full", pitch=64), [], [])


def test_rows_closer_than_w_prime_are_laid_out_by_the_mover(dev):
    """Rows of 48 (whole words) under a 64-wide kernel: copied dense, then
    spread 64 apart -- mover walks only, values exact."""
    r, c = rng(6, 0), rng(48, 1)
    x = (np.arange(6 * 48) % 251).astype(np.float16)
    s = put(dev, x)
    a = read(1, (r, 48), (c, 1))
    tmps: list = []
    at, o, runs = R._bind(
        dev, plan([r, c], X.Load(0), [a]), a, [r], [c], 64, {1: s}, tmps
    )
    assert o == Operand("full", pitch=64) and len(tmps) == 1 and at == tmps[0].addr
    assert runs and all(run.kind == "move" for run in runs)
    got = f16(dev, at, 6 * 64).reshape(6, 64)[:, :48]
    assert np.array_equal(got, x.reshape(6, 48))


def test_a_read_off_a_word_is_gathered(dev):
    r, c = rng(4, 0), rng(64, 1)
    s = put(dev, np.zeros(4 * 128, np.float16))
    a = read(1, (r, 128), (c, 1), off=8)
    dev.card.execute = None  # the gather runs vector kernels: decisions only
    tmps: list = []
    _, o, _ = R._bind(dev, plan([r, c], X.Load(0), [a]), a, [r], [c], 64, {1: s}, tmps)
    assert o == Operand("full", pitch=64)
    kinds = [run.kind for run in dev.runs]
    assert "convert" in kinds and "move" not in kinds


def test_a_table_is_laid_out_on_the_host(dev):
    """A [3, 5] table read transposed under [5 | 3] rows, 32 wide."""
    i, j = rng(5, 0), rng(3, 1)
    t = np.arange(15, dtype=np.float16).reshape(3, 5)
    a = read(None, (j, 5), (i, 1), table=t.reshape(-1))
    tmps: list = []
    at, o, runs = R._bind(dev, plan([i, j], X.Load(0), [a]), a, [i], [j], 32, {}, tmps)
    assert o == Operand("full", pitch=32) and runs == []
    assert np.array_equal(f16(dev, at, 5 * 32).reshape(5, 32)[:, :3], t.T)


def test_a_broadcast_over_some_rows_is_laid_over_every_row(dev):
    a_, b_, c_ = rng(2, 0), rng(3, 1), rng(32, 2)
    s = put(dev, np.arange(64, dtype=np.float16))
    a = read(1, (a_, 32), (c_, 1))  # not along b
    tmps: list = []
    p = plan([a_, b_, c_], X.Load(0), [a])
    at, o, _ = R._bind(dev, p, a, [a_, b_], [c_], 32, {1: s}, tmps)
    assert o == Operand("full", pitch=32)
    got = f16(dev, at, 6 * 32).reshape(2, 3, 32)
    assert np.array_equal(
        got, np.repeat(np.arange(64, dtype=np.float16).reshape(2, 1, 32), 3, 1)
    )


@pytest.mark.parametrize("dtype", [np.float16, np.float32])
def test_a_statistic_read_as_rows_is_flattened(dev, dtype):
    """f16 into its bytes, an f32 statistic into its f16 form."""
    r, c = rng(2, 0), rng(32, 1)
    s = Storage(dev, np.dtype(dtype).itemsize * 64)
    s.form(FM.ROWSTAT, FM.stat_bytes(64))
    s.wrote(FM.ROWSTAT)
    s.dtype = np.dtype(dtype)
    dev.card.execute = None
    a = read(
        1, (r, 32), (c, 1), dtype=dtypes.half if dtype == np.float16 else dtypes.float
    )
    at, _, _ = R._bind(dev, plan([r, c], X.Load(0), [a]), a, [r], [c], 32, {1: s}, [])
    want = FLAT if dtype == np.float16 else FM.F16
    assert want in s.valid and at == s.forms[want].addr


# ------------------------------------------------------------------ place
def test_along_writes_the_statistic_form(dev):
    r, c = rng(8, 0), rng(64, 1, REDUCE)
    e, sets = summed(X.Load(0), [c])
    out = Storage(dev, 16)
    p = plan([r], e, [read(1, (r, 64), (c, 1))], [c], sets)
    at, o, done = R._place(dev, p, {0: out}, "along", [r], [c], 64, [])
    assert o == Operand("row") and at == out.forms[FM.ROWSTAT].addr
    assert done() == [] and out.valid == {FM.ROWSTAT}


def test_whole_rows_are_written_in_place(dev):
    r, c = rng(8, 0), rng(64, 1)
    out = Storage(dev, 2 * 8 * 64)
    p = plan([r, c], X.Load(0), [read(1, (r, 64), (c, 1))])
    at, o, done = R._place(dev, p, {0: out}, "rows", [r], [c], 64, [])
    assert (at, o) == (out.addr, Operand("full", pitch=64))
    done()
    assert out.valid == {FLAT}


def test_narrow_rows_of_a_whole_buffer_go_to_its_padded_form(dev):
    r, c = rng(8, 0), rng(48, 1)
    out = Storage(dev, 2 * 8 * 48)
    p = plan([r, c], X.Load(0), [read(1, (r, 48), (c, 1))])
    at, o, done = R._place(dev, p, {0: out}, "rows", [r], [c], 64, [])
    key = FM.rows_key(48, 64)
    assert (at, o) == (out.forms[key].addr, Operand("full", pitch=64))
    done()
    assert out.valid == {key}


def test_narrow_rows_into_a_sub_buffer_are_moved_in(dev):
    """Rows of 48 at a word offset of a larger buffer: a [6, 64] temporary,
    then one mover walk into the 48 columns; the buffer's rest kept."""
    r, c = rng(6, 0), rng(48, 1)
    base = put(dev, np.full(32 + 6 * 48 + 32, -1, np.float16))
    view = StorageView(base, 64, 2 * 6 * 48)
    p = plan([r, c], X.Load(0), [read(1, (r, 48), (c, 1))])
    tmps: list = []
    at, o, done = R._place(dev, p, {0: view}, "rows", [r], [c], 64, tmps)
    assert o == Operand("full", pitch=64) and at == tmps[0].addr
    vals = (np.arange(6 * 64) % 199).astype(np.float16)
    dev.write(at, vals.tobytes())
    runs = done()
    assert runs and all(run.kind == "move" for run in runs)
    got = f16(dev, base.addr, 32 + 6 * 48 + 32)
    assert np.all(got[:32] == -1) and np.all(got[32 + 6 * 48 :] == -1)
    assert np.array_equal(
        got[32 : 32 + 6 * 48].reshape(6, 48), vals.reshape(6, 64)[:, :48]
    )


def test_narrow_rows_into_a_wider_window_are_moved_in(dev):
    """48 columns of each 64-element row: the kernel's 64 wide rows would
    write over the other 16, so a temporary is moved in; they are kept."""
    r, c = rng(6, 0), rng(48, 1)
    out = put(dev, np.full(6 * 64, -1, np.float16))
    p = plan([r, c], X.Load(0), [read(1, (r, 48), (c, 1))], strides=(64, 1))
    tmps: list = []
    at, _, done = R._place(dev, p, {0: out}, "rows", [r], [c], 64, tmps)
    assert at == tmps[0].addr
    vals = (np.arange(6 * 64) % 113).astype(np.float16)
    dev.write(at, vals.tobytes())
    done()
    got = f16(dev, out.addr, 6 * 64).reshape(6, 64)
    assert np.array_equal(got[:, :48], vals.reshape(6, 64)[:, :48])
    assert np.all(got[:, 48:] == -1)


def test_an_f32_result_goes_to_the_f16_form(dev):
    r, c = rng(4, 0), rng(32, 1)
    out = Storage(dev, 4 * 4 * 32)
    p = plan([r, c], X.Load(0), [read(1, (r, 32), (c, 1))], dtype=dtypes.float)
    at, _, done = R._place(dev, p, {0: out}, "rows", [r], [c], 32, [])
    assert at == out.forms[FM.F16].addr
    done()
    assert out.valid == {FM.F16} and out.dtype == np.float32


def test_a_reduction_into_a_window_is_moved_in(dev):
    r, c = rng(8, 0), rng(64, 1, REDUCE)
    e, sets = summed(X.Load(0), [c])
    out = Storage(dev, 2 * 16)
    p = plan([r], e, [read(1, (r, 64), (c, 1))], [c], sets, strides=(2,))
    tmps: list = []
    at, o, _ = R._place(dev, p, {0: out}, "along", [r], [c], 64, tmps)
    assert o == Operand("row") and at == tmps[0].forms[FM.ROWSTAT].addr


# ------------------------------------------------------------------ rows
def test_two_reductions_down_the_rows_run_one_after_another(dev, monkeypatch):
    r, c = rng(16, 0, REDUCE), rng(64, 1)
    s1, s2 = X.Reduce("sum", X.Load(0)), X.Reduce("max", X.Load(0))
    p = plan([c], X.op("add", s1, s2), [read(1, (r, 64), (c, 1))], [r])
    p.reductions = {id(s1): (r,), id(s2): (r,)}
    seen = []
    monkeypatch.setattr(R, "_materialise", lambda d, q, b: seen.append(q) or [])
    R._rows(dev, p, {})
    assert seen == [p]


def test_an_interleaved_reduction_needing_a_layout_splits(dev, monkeypatch):
    """A channel's sum over batch and image of [n, c, hw]: no view reads it
    as it is, so the image is reduced first."""
    n, ch, hw = rng(4, 0, REDUCE), rng(6, 1), rng(64, 2, REDUCE)
    e, sets = summed(X.Load(0), [n, hw])
    p = plan([ch], e, [read(1, (n, 6 * 64), (ch, 64), (hw, 1))], [n, hw], sets)
    seen = []
    monkeypatch.setattr(R, "_materialise", lambda d, q, b: seen.append(q) or [])
    R._rows(dev, p, {})
    (split,) = seen
    assert sorted(len(v) for v in split.reductions.values()) == [1, 1]


# ------------------------------------------------------------------ forms
def test_repitch_of_whole_words_is_one_mover_walk(dev):
    x = (np.arange(4 * 48) % 97).astype(np.float16)
    src, dst = put(dev, x), Storage(dev, 2 * 4 * 64)
    FM.repitch(dev, src.addr, 48, dst.addr, 64, 4, 48)
    assert [run.kind for run in dev.runs] == ["convert"]
    assert np.array_equal(
        f16(dev, dst.addr, 4 * 64).reshape(4, 64)[:, :48], x.reshape(4, 48)
    )


@pytest.mark.parametrize("w,pitch", [(20, 20), (16, 24)])
def test_repitch_off_words_goes_through_the_statistic_form(dev, w, pitch):
    dev.card.execute = None
    src, dst = put(dev, np.zeros(4 * pitch, np.float16)), Storage(dev, 2 * 4 * 32)
    FM.repitch(dev, src.addr, pitch, dst.addr, 32, 4, w)
    assert any(run.name.startswith("expand") for run in dev.runs)


@pytest.mark.parametrize("keep", [False, True])
def test_a_kept_gather_starts_from_the_destination(dev, keep):
    dev.card.execute = None
    src, dst = put(dev, np.zeros(64, np.float16)), Storage(dev, 2 * 64)
    FM.gather(dev, src.addr, 4, ((4, 1),), 0, ((4, 2),), 16, dst.addr, 0, keep)
    first = dev.runs[0].name
    assert first.startswith("expand" if keep else "fill")


def test_a_value_a_row_of_a_sub_buffer_reads_from_its_offset(dev):
    dev.card.execute = None
    base = Storage(dev, 2 * 64)
    view = StorageView(base, 2 * 16, 2 * 16)
    r = rng(16, 0)
    at, o = R._addr(dev, read(1, (r, 1)), Operand("row", offset=2), {1: view})
    assert (at, o) == (base.forms[FM.ROWSTAT].addr, Operand("row", offset=18))


def test_an_f32_result_into_a_sub_buffer_is_its_f16_forms(dev):
    base = put(dev, np.arange(64, dtype=np.float32))
    view = StorageView(base, 4 * 32, 4 * 16)
    r = rng(16, 0)
    p = plan([r], X.Load(0), [read(1, (r, 1))], dtype=dtypes.float)
    at, key = R._out(dev, p, {0: view}, False)
    assert (at, key) == (base.forms[FM.F16].addr + 64, FM.F16)
    assert np.array_equal(f16(dev, base.forms[FM.F16].addr, 64), np.arange(64))


def test_part_of_a_statistic_buffer_is_written_over_its_flat_bytes(dev):
    dev.card.execute = None
    s = Storage(dev, 2 * 64)
    s.form(FM.ROWSTAT, FM.stat_bytes(64))
    s.wrote(FM.ROWSTAT)
    s.dtype = np.dtype(np.float16)
    r = rng(8, 0)
    R._window_ok(dev, s, plan([r], X.Load(0), [read(1, (r, 1))], strides=(2,)))
    assert FLAT in s.valid


def test_a_flip_into_a_window_off_a_word_is_not_one(dev):
    r, c = rng(2, 0), rng(32, 1)
    src = put(dev, np.zeros(64, np.float16))
    out = put(dev, np.zeros(128, np.float16))
    view = StorageView(out, 2 * 8, 2 * 64)
    p = plan([r, c], X.Load(0), [read(1, (r, 32), (c, -1), off=31)])
    assert R._flip(dev, p, {0: view, 1: src}) is None and dev.runs == []


def test_a_transpose_whose_read_is_off_a_word_is_not_one(dev):
    a, b = rng(16, 0), rng(16, 1)
    src = put(dev, np.zeros(16 * 16 + 16, np.float16))
    out = Storage(dev, 2 * 256)
    p = plan([a, b], X.Load(0), [read(1, (a, 1), (b, 16), off=1)])
    assert R._transpose(dev, p, {0: out, 1: src}) is None and dev.runs == []


def test_the_f16_form_of_an_f32_sub_buffer_is_its_bases(dev):
    base = put(dev, np.arange(64, dtype=np.float32))
    view = StorageView(base, 4 * 8, 4 * 16)
    at = FM.f16(dev, view, np.float32, 16)
    assert at == base.forms[FM.F16].addr + 16
    assert np.array_equal(f16(dev, at, 16), np.arange(8, 24, dtype=np.float16))


def test_the_statistic_form_of_a_sub_buffer_is_its_bases(dev):
    base = Storage(dev, 2 * 64)
    base.form(FM.ROWSTAT, FM.stat_bytes(64))
    base.wrote(FM.ROWSTAT)
    view = StorageView(base, 2 * 16, 2 * 16)
    assert FM.rowstat(dev, view, np.float16, 64) == base.forms[FM.ROWSTAT].addr


def test_a_copy_off_a_word_is_the_bare_read_it_is(dev):
    """20 values from element 3 of a buffer: no word walk, gathered."""
    r = rng(20, 0)
    src = put(dev, (np.arange(64) % 29).astype(np.float16))
    out = Storage(dev, 64)
    p = plan([r], X.Load(0), [read(1, (r, 1))])
    p.kind = "copy"
    R._copy(dev, p, {0: out, 1: StorageView(src, 6, 40)})
    assert np.array_equal(
        f16(dev, out.addr, 20), (np.arange(3, 23) % 29).astype(np.float16)
    )


def test_a_copy_of_padded_rows_copies_that_form(dev):
    """A [10, 84] buffer held only as rows 96 apart (a product's output):
    the form copied whole, the copy's one valid form."""
    r, c = rng(10, 0), rng(84, 1)
    key = FM.rows_key(84, 96)
    src, out = Storage(dev, 2 * 840), Storage(dev, 2 * 840)
    vals = (np.arange(10 * 96) % 251).astype(np.float16)
    dev.write(src.form(key, vals.nbytes).addr, vals.tobytes())
    src.wrote(key)
    src.dtype = np.dtype(np.float16)
    p = plan([r, c], X.Load(0), [read(1, (r, 84), (c, 1))])
    p.kind = "copy"
    R._copy(dev, p, {0: out, 1: src})
    assert out.valid == {key}
    assert np.array_equal(f16(dev, out.forms[key].addr, 10 * 96), vals)


def test_a_fill_off_words_is_moved_in(dev):
    """A value every third element from element 1: the rest kept."""
    r = rng(5, 0)
    base = put(dev, np.full(32, -1, np.float16))
    p = plan([r], X.Const(7.0), [], strides=(3,))
    R._fill(dev, p, {0: StorageView(base, 2, 28)}, 7.0)
    got = f16(dev, base.addr, 32)
    want = np.full(32, -1, np.float16)
    want[1:16:3] = 7
    assert np.array_equal(got, want)


def test_a_gather_into_a_window_keeps_the_words_between(dev, monkeypatch):
    seen = []
    monkeypatch.setattr(FM, "gather", lambda *a: seen.append(a))
    dev.card.execute = None
    r = rng(5, 0)
    src = put(dev, np.arange(5, dtype=np.float16))
    out = put(dev, np.zeros(64, np.float16))
    view = StorageView(out, 2 * 3, 2 * 9)
    p = plan([r], X.Load(0), [read(1, (r, 1))], strides=(2,))
    assert R._gather(dev, p, {0: view, 1: src}) == []
    (args,) = seen
    walk, first, dwalk, m, at, dat, keep = args[3:]
    assert (walk, dwalk, at, dat, keep) == (((5, 1),), ((5, 2),), out.addr, 3, True)
    assert m == 16 and first == 0
