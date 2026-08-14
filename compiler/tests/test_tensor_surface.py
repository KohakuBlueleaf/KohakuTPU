"""The tensor level: every operator, and what saying less costs.

`tests/test_spectrum.py` holds the DSL band open at level 4; this holds the
level above it open, for the same reason. The "near no control" path is the one
nobody writes a kernel against, so a rung of it rots unnoticed.

Two claims. An operator must AGREE with numpy, and spelling one op as an
operator must move the same bytes as spelling it as a kernel call -- a rung
that costs more than the one below it is a rung nobody uses.

The rest is memory. A span the arena has given away must be neither readable
nor freeable through the tensor that used to own it. Both are silent on the
machine and both return plausible numbers.
"""

import gc

import numpy as np
import pytest
from kohakuaccel.lang.kernel import KernelError
from kohakutpu.model import SimDevice
from kohakutpu.rt import ARENA_SIZE, Device

from kohakutpu import layout as LO
from kohakutpu import ops as O

M, N = 16, 64


@pytest.fixture
def dev():
    """A device with no card behind it, fresh for each test."""
    return SimDevice()


def operands(dev):
    """Two tensors and the host arrays they hold."""
    x = (np.arange(M * N).reshape(M, N) % 7).astype(np.float16)
    y = (np.arange(M * N).reshape(M, N) % 3).astype(np.float16)
    return dev.tensor(x), dev.tensor(y), x, y


# ------------------------------------------------------------------- operators
OPERATORS = [
    ("mul", lambda a, b: a * b, lambda a, b: a * b),
    ("add", lambda a, b: a + b, lambda a, b: a + b),
    ("sub", lambda a, b: a - b, lambda a, b: a - b),
    ("div", lambda a, b: a / (b + 1.0), lambda a, b: a / (b + 1.0)),
    ("neg", lambda a, b: -a, lambda a, b: -a),
    ("scale", lambda a, b: a * 2.0, lambda a, b: a * 2.0),
    ("rscale", lambda a, b: 2.0 * a, lambda a, b: 2.0 * a),
    ("rsub", lambda a, b: 3.0 - a, lambda a, b: 3.0 - a),
]


@pytest.mark.parametrize(
    ("name", "run", "want"), OPERATORS, ids=[o[0] for o in OPERATORS]
)
def test_an_operator_agrees_with_numpy(dev, name, run, want):
    """An operator that dispatches the wrong kernel still returns a tensor."""
    a, b, x, y = operands(dev)
    got = run(a, b).numpy()
    assert np.abs(got - want(x, y)).max() < 1e-2, name


def test_a_scalar_operand_is_materialised(dev):
    """`x * 2` has no kernel taking a scalar, so it becomes a whole tensor."""
    a, _, x, _ = operands(dev)
    assert np.abs((a * 2.0).numpy() - x * 2.0).max() < 1e-2


def test_a_bad_operand_names_the_fix(dev):
    """A host array on either side is the mistake worth a message."""
    a, _, x, _ = operands(dev)
    with pytest.raises(TypeError, match="dev.tensor"):
        a * "two"
    with pytest.raises(TypeError, match="dev.tensor"):
        x * a


def test_matmul_takes_the_batched_kernel_at_rank_three(dev):
    """`@` picks by rank, so a head axis must not fall through to the flat one."""
    a = dev.tensor(np.ones((2, M, N), np.float16))
    b = dev.tensor(np.ones((N, N), np.float16))
    assert (a @ b).shape == (2, M, N)


# ----------------------------------------------------------------- the rungs
def test_saying_less_costs_nothing(dev):
    """`x @ w`, `O.matmul(x, w)` and a retuned call must not differ in cost.

    The operator states no tiling and the kernel call states one; if the top of
    the band dispatched more rounds or moved more bytes than the bottom, the
    inference would be a tax and nobody would use it.
    """
    xa = (np.arange(M * N).reshape(M, N) % 5).astype(np.float16)
    wa = (np.arange(N * N).reshape(N, N) % 3).astype(np.float16)
    watch = ("dispatches", "rounds", "flits", "sent", "fetched", "relayouts")

    def cost(run):
        """One call on FRESH operands: a resident layout would flatter the second."""
        x, w = dev.tensor(xa), dev.tensor(wa)
        before = {k: dev.stats()[k] for k in watch}
        got = run(x, w).numpy()
        return {k: dev.stats()[k] - before[k] for k in watch}, got

    spent, said = cost(lambda x, w: x @ w), cost(lambda x, w: O.matmul(x, w))
    assert spent[0] == said[0], (spent[0], said[0])
    assert np.array_equal(spent[1], said[1])


def test_a_retuned_call_is_the_same_program_at_a_different_split(dev):
    """The knobs are the next rung down: same answer, different grid."""
    x = dev.tensor((np.arange(M * N).reshape(M, N) % 5).astype(np.float16))
    w = dev.tensor((np.arange(N * N).reshape(N, N) % 3).astype(np.float16))
    wide = O.matmul.plan(x, w, gm=8, gn=8)
    narrow = O.matmul.plan(x, w, gm=2, gn=1)
    assert wide.grid != narrow.grid
    assert np.abs(O.matmul(x, w, gm=2, gn=1).numpy() - (x @ w).numpy()).max() < 1e-2


def test_an_operand_from_another_device_is_refused(dev):
    """Two devices means two arenas, so one address means two different bytes."""
    other = SimDevice()
    a = dev.tensor(np.ones((M, N), np.float16))
    b = other.tensor(np.ones((N, N), np.float16))
    with pytest.raises(KernelError, match="different devices"):
        a @ b


# --------------------------------------------------------------------- memory
def test_empty_cache_keeps_what_is_still_referenced(dev):
    """`torch.cuda.empty_cache` returns CACHED blocks, never live ones.

    `pin()` promises the memory is kept, and the promise is the thing users
    build on. Reclaiming a live span made that promise false and the failure
    was silent: the next tensor got the address and both read it.
    """
    kept = dev.tensor(np.full((M, N), 3, np.float16)).pin()
    plain = dev.tensor(np.full((M, N), 5, np.float16))
    where = (kept.address(LO.Flat()), plain.address(LO.Flat()))
    dev.empty_cache()
    assert (kept.address(LO.Flat()), plain.address(LO.Flat())) == where
    assert np.array_equal(kept.numpy(), np.full((M, N), 3, np.float16))
    assert np.array_equal(plain.numpy(), np.full((M, N), 5, np.float16))


def test_empty_cache_returns_the_free_tail(dev):
    """What it CAN give back is the top of the arena once nothing holds it."""
    a = dev.tensor(np.ones((M, N), np.float16))
    a.address(LO.Flat())
    high = dev.arena.top
    del a
    gc.collect()
    assert dev.arena.top == high, "a freed span stays reserved until it is trimmed"
    dev.empty_cache()
    assert dev.arena.top < high


def test_a_reclaimed_tensor_raises_instead_of_reading_a_stranger(dev):
    """An arena RESET does free live spans, and the tensor that held one is gone.

    Its address now belongs to whatever was allocated next, and reading it
    returns that tensor's bytes: numbers that are plausible and wrong.
    """
    a = dev.tensor(np.ones((M, N), np.float16))
    b = dev.tensor(np.ones((N, N), np.float16))
    computed = O.matmul(a, b)
    dev.arena.reset()
    with pytest.raises(ValueError, match="arena reset"):
        computed.numpy()


def test_a_tensor_that_still_has_its_contents_re_uploads(dev):
    """One the host wrote can be rebuilt, so it lands somewhere new and reads back."""
    kept = dev.tensor(np.full((M, N), 3, np.float16)).pin()
    was = kept.address(LO.Flat())
    dev.arena.reset()
    intruder = dev.tensor(np.zeros((M, N), np.float16))
    took = intruder.address(LO.Flat())
    assert took == was, "the reclaimed span should have been handed out again"
    assert kept.address(LO.Flat()) != took
    assert np.array_equal(kept.numpy(), np.full((M, N), 3, np.float16))


def test_a_stale_tensor_does_not_free_a_live_span(dev):
    """The failure this costs is a use-after-free with no crash to point at."""
    stale = dev.tensor(np.ones((M, N), np.float16))
    was = stale.address(LO.Flat())
    dev.arena.reset()
    victim = dev.tensor(np.zeros((M, N), np.float16))
    assert victim.address(LO.Flat()) == was
    held = dict(dev.arena.live)

    del stale
    gc.collect()
    assert dev.arena.live == held, "dropping the stale tensor freed the victim"


# -------------------------------------------------------------- the script guard
def test_asking_for_nothing_never_means_the_card(monkeypatch):
    """Reaching hardware must be a decision, never a fallback.

    The card was reached twice through two different holes: a demo whose
    `--device` flag did not exist so `--device sim` was accepted and ignored,
    and examples that took no flag at all and opened a device on first
    `ktpu.tensor(...)`. Both are this default.
    """
    from kohakutpu import api

    monkeypatch.delenv(api.DEVICE_ENV, raising=False)
    assert api.target() == "sim"
    assert api.target(None) == "sim"
    assert api.target("card") == "card", "an explicit choice is still honoured"


def test_the_env_var_still_chooses(monkeypatch):
    """A whole session of runs is set once rather than per script."""
    from kohakutpu import api

    monkeypatch.setenv(api.DEVICE_ENV, "card")
    assert api.target() == "card"
    assert api.target("sim") == "sim", "the argument outranks the environment"


def test_an_unknown_target_names_the_choices(monkeypatch):
    """A typo must not fall through to whichever default is safer this week."""
    from kohakutpu import api

    monkeypatch.delenv(api.DEVICE_ENV, raising=False)
    with pytest.raises(ValueError, match="card"):
        api.target("hardware")


def test_a_card_that_will_not_open_raises_rather_than_simulating(monkeypatch):
    """The failure the safe default must not introduce.

    Falling back to the models would report simulated numbers as if they came
    off the silicon, which is worse than not running: the card can only be
    SLOW, never WRONG, so a wrong number must never be attributable to it.
    """
    from kohakutpu import api, rt

    monkeypatch.setattr(api, "_device", None)
    monkeypatch.setattr(
        rt, "_open_card", lambda: (_ for _ in ()).throw(OSError("no card"))
    )
    with pytest.raises(OSError, match="no card"):
        api.device(kind="card")


def test_a_flagless_script_and_a_flagged_one_agree(monkeypatch):
    """`add_device_flag` and `script_device` are one decision, not two."""
    import argparse

    from kohakutpu import api

    monkeypatch.delenv(api.DEVICE_ENV, raising=False)
    ap = argparse.ArgumentParser()
    api.add_device_flag(ap)
    assert api.target(ap.parse_args([]).device) == "sim"
    assert api.target(ap.parse_args(["--device", "card"]).device) == "card"


def test_an_arena_past_one_mesh_is_refused():
    """An address past a mesh's local space carries the NEXT mesh's id.

    That request does not fault, it routes: it lands in the other mesh's DRAM.
    The check is in the constructor because there is no later moment at which
    the top of the arena is wrong in a way anything can see. The boundary is
    [35:0] since the 40-bit widening, so the base has to clear 64 GB to trip it.
    """
    with pytest.raises(ValueError, match="past one mesh's"):
        Device(card=object(), base=(1 << 36) - 0x1000, size=ARENA_SIZE)
