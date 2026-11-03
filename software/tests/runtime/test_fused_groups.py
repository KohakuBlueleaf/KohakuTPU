"""Fused launch groups: a dependent elementwise chain over one domain is one
package, gathered past unrelated items, with what it computes unchanged."""

from types import SimpleNamespace

import numpy as np
import pytest
from tinygrad.dtype import dtypes
from tinygrad.helpers import Context
from tinygrad.tensor import Tensor

from kohakutpu.application.ktpugrad.captured import recipe
from kohakutpu.application.ktpugrad.launch import schedule
from kohakutpu.application.ktpugrad.launch.config import knobs
from kohakutpu.application.ktpugrad.nn import Adam16
from kohakutpu.application.runtime import device as card
from kohakutpu.application.runtime import generated
from kohakutpu.application.runtime.memory.storage import Storage

R = np.random.default_rng(11)


def tensor(x):
    return Tensor(x.astype(np.float16), device="KTPU").realize()


def launches(dev, run) -> int:
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
    return len(seen)


def _compensate(p, e, m) -> None:
    """The compensated update of `Adam16`: y rounded, w + y rounded, that
    write's excess kept (y and the new weight named by no tensor)."""
    y = (-(m * 0.01 + e)).contiguous()
    new = (p + y).contiguous()
    e.assign((new - p) - y)
    p.assign(new)


def compensated_step(p0, e0, m0):
    p, e, m = tensor(p0), tensor(e0), tensor(m0)

    def step():
        _compensate(p, e, m)
        Tensor.realize(e, p)

    return p, e, step


@pytest.fixture
def lowered(dev):
    """The offline device on the double's board: every package lowered to L2
    (the double does not)."""
    d = card.Device(kind="offline", board=dev.machine.board, nodes=1)
    with card.attached(d):
        yield d
    d.close()


def test_a_compensated_update_is_one_launch_with_the_same_values(lowered, monkeypatch):
    dev = lowered
    shape = (64, 256)
    p0, m0 = R.normal(size=shape), R.normal(size=shape)
    e0 = R.normal(size=shape) * 1e-3
    with knobs(horizontal_fusion=False):
        p_ref, e_ref, step = compensated_step(p0, e0, m0)
        apart = launches(dev, step)
    packages = []
    launch = generated.launch

    def spied(dev, generator, arguments, binds, info=None):
        if generator == "fused.kernel":
            packages.append(arguments)
        return launch(dev, generator, arguments, binds, info=info)

    monkeypatch.setattr(generated, "launch", spied)
    p, e, step = compensated_step(p0, e0, m0)
    fused = launches(dev, step)
    assert (apart, fused) == (4, 1)
    # One loop: y stored (read back by value), the new weight stored only as
    # the weight's copy, the excess and the weight.
    (package,) = packages
    assert [s["kept"] for s in package["steps"]] == [[True, False, True, True]]
    assert p.numpy().tobytes() == p_ref.numpy().tobytes()
    assert e.numpy().tobytes() == e_ref.numpy().tobytes()


def test_a_repeated_fused_group_replays_its_recipe(lowered, monkeypatch):
    dev = lowered
    cold = []
    run_group = recipe.run_group

    def counted(*a):
        cold.append(a)
        return run_group(*a)

    shape = (64, 256)
    p0, m0 = R.normal(size=shape), R.normal(size=shape)
    e0 = R.normal(size=shape) * 1e-3
    with knobs(horizontal_fusion=False):
        p_ref, e_ref, step = compensated_step(p0, e0, m0)
        for _ in range(3):
            step()
    monkeypatch.setattr(recipe, "run_group", counted)
    p, e, step = compensated_step(p0, e0, m0)
    planned = []
    for _ in range(3):
        assert launches(dev, step) == 1
        planned.append(len(cold))
    # The first step reads its uploads, the second its own writes: from the
    # third on, the recorded package replays.
    assert planned[2] == planned[1]
    assert p.numpy().tobytes() == p_ref.numpy().tobytes()
    assert e.numpy().tobytes() == e_ref.numpy().tobytes()


def _spied_packages(monkeypatch) -> list:
    packages = []
    launch = generated.launch

    def spied(dev, generator, arguments, binds, info=None):
        if generator == "fused.kernel":
            packages.append(arguments)
        return launch(dev, generator, arguments, binds, info=info)

    monkeypatch.setattr(generated, "launch", spied)
    return packages


def _assign_result(b, x) -> None:
    b.assign((x * 2 + 1).contiguous())


def test_a_copy_of_a_result_stores_its_pack_once_more(lowered, monkeypatch):
    dev = lowered
    x0, b0 = R.normal(size=(64, 256)), R.normal(size=(64, 256))
    with knobs(horizontal_fusion=False):
        x, b = tensor(x0), tensor(b0)
        apart = launches(dev, lambda: (_assign_result(b, x), b.realize()))
        want = b.numpy().tobytes()
    packages = _spied_packages(monkeypatch)
    x, b = tensor(x0), tensor(b0)
    fused = launches(dev, lambda: (_assign_result(b, x), b.realize()))
    assert (apart, fused) == (2, 1)
    # The result, dead after the loop, is stored only as its copy.
    (package,) = packages
    assert [s["kept"] for s in package["steps"]] == [[False, True]]
    assert b.numpy().tobytes() == want


#: Parameter shapes of the zoo's GRU and of the MNIST MLP: weights, biases
#: shorter than a 128-value row, and a head.
SHAPES = {
    "gru": [(64, 92), (64,), (10, 64), (10,)],
    "mlp": [(128, 784), (128,), (10, 128), (10,)],
}


def _adam_steps(shapes, fused: bool, steps: int = 2) -> list:
    """Every parameter and Adam16 state's bytes after `steps` steps of a loss
    whose gradient is a fixed random tensor a parameter."""
    rng = np.random.default_rng(3)
    with knobs(horizontal_fusion=fused):
        ps = [tensor(rng.normal(size=s) * 0.1).is_param_() for s in shapes]
        cs = [tensor(rng.normal(size=s)) for s in shapes]
        opt = Adam16(ps, lr=1e-2)
        for _ in range(steps):
            with Context(TRAINING=1):
                loss = sum((p * c).sum() for p, c in zip(ps, cs))
                assert isinstance(loss, Tensor)
                opt.zero_grad()
                opt.scale(loss).backward()
                Tensor.realize(loss, *opt.schedule_step())
        return [t.numpy() for t in (*ps, *opt.m, *opt.rms, *opt.err)]


@pytest.mark.parametrize("model", sorted(SHAPES))
def test_an_adam_step_fused_writes_what_it_writes_apart(dev, model):
    """On the double (every L2 value computed), the fused step's parameters
    and states are the unfused step's bytes (the same arithmetic in the same
    order), every value finite."""
    apart = _adam_steps(SHAPES[model], fused=False)
    fused = _adam_steps(SHAPES[model], fused=True)
    for a, b in zip(apart, fused, strict=True):
        assert np.isfinite(a.astype(np.float32)).all()
        assert a.tobytes() == b.tobytes()


class _Recurrent:
    """The zoo GRU's shape: three gates over [input, state], seven rows, so
    each weight's gradient sums seven products and its Adam16 update reads it
    transposed (a launch made at once inside the group that stages the sums)."""

    def __init__(self, rng, width=64, rows=7):
        def weight(*s):
            return tensor(rng.normal(size=s) * 0.1).is_param_()

        self.rows, self.width = rows, width
        self.gates = [(weight(width, 28 + width), weight(width)) for _ in range(3)]
        self.head = (weight(10, width), weight(10))

    def params(self) -> list:
        return [t for pair in (*self.gates, self.head) for t in pair]

    def __call__(self, x: Tensor) -> Tensor:
        x = x.reshape(-1, 28, 28)
        h = Tensor.zeros(x.shape[0], self.width, device=x.device).contiguous()
        (gw, gb), (uw, ub), (fw, fb) = self.gates
        for t in range(self.rows):
            both = x[:, 4 * t].cat(h, dim=1)
            keep = both.linear(gw.T, gb).sigmoid()
            take = both.linear(uw.T, ub).sigmoid()
            h = keep.mul(h).add(take.mul(both.linear(fw.T, fb).tanh()))
        return h.linear(self.head[0].T, self.head[1])


def _recurrent_steps(dev, recipes: bool, steps: int = 3) -> list:
    rng = np.random.default_rng(9)
    model = _Recurrent(rng)
    opt = Adam16(model.params(), lr=2e-3)
    dev.eager_recipe_enabled = recipes
    for _ in range(steps):
        x = tensor(rng.normal(size=(8, 1, 28, 28)))
        y = Tensor(rng.integers(0, 10, 8).astype(np.int32), device="KTPU")
        with Context(TRAINING=1):
            loss = model(x).float().sparse_categorical_crossentropy(y)
            opt.zero_grad()
            opt.scale(loss).backward()
            Tensor.realize(loss, *opt.schedule_step())
    return [t.numpy() for t in (*model.params(), *opt.m, *opt.rms, *opt.err)]


def test_a_replayed_group_runs_what_its_cold_run_ran(dev):
    """On the double, a step replayed from the fused groups' recipes writes
    the bytes the same step planned cold writes: a launch the group makes at
    once runs after the staged members it reads, recorded in that order."""
    cold = _recurrent_steps(dev, recipes=False)
    replayed = _recurrent_steps(dev, recipes=True)
    for a, b in zip(cold, replayed, strict=True):
        assert np.isfinite(b.astype(np.float32)).all()
        assert a.tobytes() == b.tobytes()


def _plan(n: int):
    return SimpleNamespace(
        out_slot="out", out_n=n, out_shape=(n,), out_dtype=dtypes.half, accesses=()
    )


def test_a_member_moves_ahead_only_past_items_it_does_not_touch(dev):
    x, y, z, w, v, u = (Storage(dev, 256) for _ in range(6))
    a = (_plan(128), {"out": x})
    # Not eligible: it reads y and writes z.
    other = (_plan(128), {"out": z, "in0": y})
    # Writes y, which `other` reads first: stays after it.
    clash = (_plan(128), {"out": y, "in0": x})
    # Reads x (a's output) and writes w: joins a past `other`.
    free = (_plan(128), {"out": w, "in0": x})
    # Another domain.
    wide = (_plan(256), {"out": v, "in0": u})
    items = [a, other, clash, free, wide]
    eligible = {id(p) for p, _ in (a, clash, free, wide)}
    got = schedule.groups(items, lambda p: id(p) in eligible)
    assert [g for g, _ in got] == [[0, 3], [1], [2], [4]]
