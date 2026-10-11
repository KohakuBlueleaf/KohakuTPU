"""A kernel compiled from L2 or L3 builds into a node program, and the
compiled flash is the hand-written one step for step."""

import pytest
from kohakutpu.compiler import build
from kohakutpu.language import kernels
from kohakutpu.language.text import module


def load(name):
    return kernels.load(kernels.kernel(name))


def addresses(m, name):
    """Each L1 parameter bound to its own 16 MB window, for building."""
    return {p: (i + 1) << 24 for i, (p, _) in enumerate(m.body(name, "l1").params)}


def _norm(step):
    """A program step with its unit-op objects as their fields."""
    return tuple(
        (
            tuple(sorted(vars(e).items()))
            if hasattr(e, "__dict__")
            else (_norm(e) if isinstance(e, tuple) else e)
        )
        for e in step
    )


@pytest.mark.parametrize("level", ["l2", "l3"])
def test_compiled_flash_is_the_hand_program(level, compiled):
    m = load("flash")
    got = module.read(compiled("flash", level))
    mine = build.program(got, "flash", addresses(got, "flash"))
    hand = build.program(m, "flash", addresses(m, "flash"))
    assert [_norm(s) for s in mine.steps] == [_norm(s) for s in hand.steps]


@pytest.mark.parametrize("kernel", kernels.names())
@pytest.mark.parametrize("level", ["l2", "l3"])
def test_compiled_l1_builds(kernel, level, compiled):
    m = load(kernel)
    got = module.read(compiled(kernel, level))
    prog = build.program(got, kernel, addresses(got, kernel))
    assert prog.steps[-1] == ("barrier",)
    # The same leading parameters, in order, as the hand L1 body: a runner
    # binds by position.
    hand = [p for p, _ in m.body(kernel, "l1").params]
    mine = [p for p, _ in got.body(kernel, "l1").params]
    n_in = len(m.body(kernel, "l3").params) + 1
    assert mine[:n_in] == hand[:n_in]
