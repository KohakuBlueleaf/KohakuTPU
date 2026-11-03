"""A buffer updated in place (a parameter) has its consumers' MXFP7 packings
written by its writer (`launch.emit.packings`): learned at the buffer
(`forms.updated_site`) on one step, from the next its consumers' quant
launches are gone, each packing byte-equal to the quant's. Every weight
shape, under each writer form (`emit.WRITER_PACK_FORM`): packed rows ("a",
where they fit) and quant steps ("b")."""

from contextlib import contextmanager
from typing import cast

import numpy as np
import pytest
from tinygrad.device import Buffer
from tinygrad.engine.jit import TinyJit
from tinygrad.helpers import Context
from tinygrad.tensor import Tensor

from kohakutpu.application import ktpugrad
from kohakutpu.application.kernels.vector import Operand
from kohakutpu.application.ktpugrad.device import KTPUGraph
from kohakutpu.application.ktpugrad.launch import emit as E
from kohakutpu.application.ktpugrad.nn import Adam16
from kohakutpu.application.runtime import device as card
from kohakutpu.application.runtime import forms as FM

BOARD = "multimesh_v9t6"

#: Weight [N, K]: whole bands and chunks, K off the chunk (784 whole words, 92
#: not), N off the band.
SHAPES = [(128, 64), (128, 784), (64, 92), (10, 128), (10, 64)]

#: (shape, form) forced: "a" only where packed rows fit (whole bands and chunks).
CASES = [(s, "b") for s in SHAPES] + [((128, 64), "a")]


@pytest.fixture(params=CASES, ids=lambda c: f"{c[0][0]}x{c[0][1]}-{c[1]}")
def case(request, monkeypatch):
    shape, form = request.param
    monkeypatch.setattr(E, "WRITER_PACK_FORM", form)
    monkeypatch.setattr(E, "CHOSEN", {})
    return shape


@pytest.fixture
def dev():
    ktpugrad.install()
    d = card.Device(kind="double", board=BOARD, nodes=1)
    with card.attached(d):
        yield d
    d.close()


@contextmanager
def apart(monkeypatch):
    """A device of its own (no recipe recorded with packings) whose writers
    write no packing: the separate quant path."""
    with monkeypatch.context() as m:
        m.setattr(E, "packings", lambda *a: None)
        d = card.Device(kind="double", board=BOARD, nodes=1)
        with card.attached(d):
            yield d
        d.close()


def tensor(x, dtype=np.float16):
    return Tensor(x.astype(dtype), device="KTPU").realize()


def storage(t: Tensor):
    return FM.base_of(cast(Buffer, t.uop.buffer)._buf)[0]


def spied(dev, run):
    """`run()`, and the kinds and L2 of the launches it submits."""
    seen = []
    submit = dev.run

    def spy(compiled, bindings, kind, info):
        seen.append((kind, compiled.l2_text))
        return submit(compiled, bindings, kind, info)

    dev.run = spy
    try:
        got = run()
    finally:
        dev.run = submit
    return got, seen


def operands(shape: tuple, seed: int = 0):
    r = np.random.default_rng(seed)
    x = tensor(r.normal(size=(37, shape[1])))
    w = tensor(r.normal(size=shape) / 4)
    g = tensor(r.normal(size=shape) / 8)
    return x, w, g


def steps(dev, x, w, g, n: int, extra=None) -> list:
    """`n` steps of a product reading `w` as its B, then `w` updated in place
    by `g`: each step's product output, its launches, and `w` after it."""
    out = []
    for _ in range(n):

        def step():
            y = (x @ w.T).numpy()
            if extra is not None:
                extra(storage(w))
            w.assign(w - g * 0.25).realize()
            return y

        y, seen = spied(dev, step)
        out.append((y, seen, w.numpy()))
    return out


def key(dev, w) -> tuple:
    (k,) = FM.demands(dev)[FM.updated_site(storage(w))]
    return k


def requant(dev, st, k) -> bytes:
    """Packing `k` of `st`'s values made again by the quant kernel."""
    _, side, src, rows, cols, rp, cp, g, nk, _ = k
    st.valid.discard(k)
    FM.packed(dev, st, np.float16, src, rows, cols, rp, cp, side, g, nk)
    return form(dev, st, k)


def form(dev, st, k) -> bytes:
    return dev.read(st.forms[k].addr, k[5] * k[6])


def test_the_next_update_packs_the_weight_and_its_consumer_quants_no_more(dev, case):
    x, w, g = operands(case)
    got = steps(dev, x, w, g, 3)
    k = key(dev, w)
    assert (k[1], k[3], k[4]) == ("b", *case), k
    kinds = [[kind for kind, _ in seen] for _, seen, _ in got]
    assert "quant" in kinds[1], kinds
    assert "quant" not in kinds[2], kinds
    update = [text for kind, text in got[2][1] if kind == "dsl"]
    assert len(update) == 1, update
    assert E.CHOSEN[(case, (k,))] == E.WRITER_PACK_FORM
    if E.WRITER_PACK_FORM == "a":
        assert "status=" in update[0] and "quant_check" in update[0], update
    else:
        assert "quant " in update[0], update


@pytest.mark.parametrize("shape", SHAPES, ids=lambda s: f"{s[0]}x{s[1]}")
def test_auto_packs_at_the_writer_only_where_that_is_cheapest(dev, monkeypatch, shape):
    """Under "auto" a weight's packing is written by its writer exactly where
    a form was priced cheapest (`emit.CHOSEN`), else its consumer quants; the
    values byte-equal to the separate path either way."""
    monkeypatch.setattr(E, "WRITER_PACK_FORM", "auto")
    monkeypatch.setattr(E, "CHOSEN", {})
    x, w, g = operands(shape)
    got = steps(dev, x, w, g, 4)
    k = key(dev, w)
    served = (shape, (k,)) in E.CHOSEN
    kinds = [[kind for kind, _ in seen] for _, seen, _ in got]
    assert ("quant" in kinds[3]) != served, (kinds, E.CHOSEN)
    with apart(monkeypatch) as other:
        alone = steps(other, *operands(shape), 4)
    for (y, _, wn), (y2, _, wn2) in zip(got, alone, strict=True):
        assert y.tobytes() == y2.tobytes()
        assert wn.tobytes() == wn2.tobytes()


def test_the_packing_is_the_quants_and_the_steps_the_separate_paths(
    dev, case, monkeypatch
):
    """Byte-equal: the writer's packing to the quant kernel's of the same
    values (its zero columns K..kp and rows N..rp too), and every step's
    product and weight to the separate quant's."""
    x, w, g = operands(case)
    got = steps(dev, x, w, g, 4)
    st, k = storage(w), key(dev, w)
    assert k in st.valid
    written = form(dev, st, k)
    assert written == requant(dev, st, k)
    with apart(monkeypatch) as other:
        alone = steps(other, *operands(case), 4)
    for (y, _, wn), (y2, _, wn2) in zip(got, alone, strict=True):
        assert y.tobytes() == y2.tobytes()
        assert wn.tobytes() == wn2.tobytes()
    assert "quant" in [kind for kind, _ in alone[3][1]]


def test_a_nan_update_fails_its_writer(dev, case):
    """The update's checked packing fails the writer itself: the consumer's
    quant no longer runs to catch the NaN."""
    x, w, g = operands(case)
    steps(dev, x, w, g, 2)
    nan = np.zeros(case)
    nan[5, 7] = np.nan
    bad = tensor(nan)
    (x @ w.T).numpy()
    with pytest.raises(Exception, match="non-finite"):
        w.assign(w - bad).realize()


def test_a_weight_with_two_consumer_packings_writes_both(dev, case):
    """The weight read as a product's B and packed as an A elsewhere (rows
    padded past a whole 64-row tile): both written by the update, each
    byte-equal to its quant (the padding rows packed zero)."""
    x, w, g = operands(case)
    n, k = case
    rp, kp = -(-n // 64) * 64 + 64, -(-k // 64) * 64

    def as_a(st):
        FM.packed(
            dev, st, np.float16, Operand("full", pitch=k), n, k, rp, kp, "a", 16, 2
        )

    got = steps(dev, x, w, g, 3, extra=as_a)
    keys = list(FM.demands(dev)[FM.updated_site(storage(w))])
    assert sorted(k[1] for k in keys) == ["a", "b"], keys
    assert "quant" not in [kind for kind, _ in got[2][1]]
    st = storage(w)
    for k in keys:
        assert k in st.valid, k
        assert form(dev, st, k) == requant(dev, st, k), k


def test_packed_rows_take_no_more_packings_than_the_descriptors_hold(dev, monkeypatch):
    """A [128, 64] weight read as the product's B and packed as nine A forms
    (rows padded 128..640): packed rows hold a view a packing beside the
    update's two reads and its output, so the writer takes 8 - 2 - 1 = 5 of
    the ten (the rest quanted by their consumers), each byte-equal to its
    quant."""
    monkeypatch.setattr(E, "WRITER_PACK_FORM", "a")
    monkeypatch.setattr(E, "CHOSEN", {})
    x, w, g = operands((128, 64))

    def as_a(st):
        for rp in range(128, 641, 64):
            FM.packed(
                dev,
                st,
                np.float16,
                Operand("full", pitch=64),
                128,
                64,
                rp,
                64,
                "a",
                16,
                2,
            )

    steps(dev, x, w, g, 3, extra=as_a)
    (taken,) = [keys for (shape, keys) in E.CHOSEN if shape == (128, 64)]
    assert len(taken) == 5, taken
    st = storage(w)
    for k in taken:
        assert k in st.valid, k
        assert form(dev, st, k) == requant(dev, st, k), k


def jitted(monkeypatch, shape: tuple) -> tuple:
    """An Adam16 step of a `shape` weight under TinyJit, called until it
    replays: its graph and the losses."""
    graphs = []
    init = KTPUGraph.__init__

    def counted(self, *args, **kwargs):
        graphs.append(self)
        init(self, *args, **kwargs)

    monkeypatch.setattr(KTPUGraph, "__init__", counted)
    r = np.random.default_rng(1)
    w = tensor(r.normal(size=shape) / 8).is_param_()
    opt = Adam16([w], lr=1e-2)

    @TinyJit
    def step(x):
        with Context(TRAINING=1):
            loss = (x @ w.T).relu().sum()
            opt.zero_grad()
            opt.scale(loss).backward()
            Tensor.realize(loss, *opt.schedule_step())
            return loss

    losses = [step(tensor(r.normal(size=(37, shape[1])))).numpy() for _ in range(8)]
    assert graphs and graphs[-1].counts["replayed"] > 0, graphs[-1].history
    return graphs[-1], losses


def quants(graph) -> int:
    log = graph.log[1]
    return sum(ev[0] == "run" and ev[2].name.startswith("quant") for ev in log)


@pytest.mark.parametrize(
    "shape, form", [((128, 64), "a"), ((128, 784), "b"), ((10, 128), "b")]
)
def test_a_jitted_training_step_replays_with_the_weight_packed(
    dev, monkeypatch, shape, form
):
    """The replayed log holds one quant fewer than the separate path's (the
    weight's), its losses byte-equal."""
    monkeypatch.setattr(E, "WRITER_PACK_FORM", form)
    graph, losses = jitted(monkeypatch, shape)
    with apart(monkeypatch):
        alone, losses2 = jitted(monkeypatch, shape)
    assert quants(graph) == quants(alone) - 1, (quants(graph), quants(alone))
    assert [x.tobytes() for x in losses] == [x.tobytes() for x in losses2]
