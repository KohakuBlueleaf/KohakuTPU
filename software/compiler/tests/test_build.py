"""A kernel compiled from L2 or L3 builds into a node program, and the
compiled flash is the hand-written one step for step."""

import pytest
from kohakutpu.compiler import build
from kohakutpu.compiler.layout.buffer import Buffer
from kohakutpu.language import kernels
from kohakutpu.language.lower.l2.emit import LowerError
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


#: L3 problem sizes other than the hand L2's, each planned, lowered and built.
SIZES = [
    ("silu", {"x": (6144,)}),
    ("add", {"x": (2048,), "y": (2048,)}),
    ("softmax", {"x": (96, 128)}),
    ("layernorm", {"x": (64, 128), "g": (128,), "b": (128,)}),
    ("mlp", {"x": (256, 256), "w1": (512, 256), "w2": (256, 512)}),
    (
        "swiglu",
        {"x": (512, 256), "wg": (256, 256), "wu": (256, 256), "wd": (256, 256)},
    ),
    ("attention", {"q": (512, 64), "k": (1024, 64), "v": (1024, 64)}),
    ("flash", {"q": (2048, 64), "k": (2048, 64), "v": (2048, 64)}),
]


@pytest.mark.parametrize(
    "kernel, shapes", SIZES, ids=[f"{k}-{next(iter(s.values()))}" for k, s in SIZES]
)
def test_compiled_l1_builds_at_other_sizes(kernel, shapes):
    text = build.l1_text(load(kernel), kernel, "l3", shapes=shapes)
    got = module.read(text)
    prog = build.program(got, kernel, addresses(got, kernel))
    assert prog.steps[-1] == ("barrier",)
    params = [Buffer.of(t).host_shape for _, t in got.body(kernel, "l1").params]
    assert params[: len(shapes)] == list(shapes.values())
    assert params[len(shapes)][0] == next(iter(shapes.values()))[0]


def test_attention_lowers_only_its_library_images_key_count():
    shapes = {"q": (512, 64), "k": (512, 64), "v": (512, 64)}
    with pytest.raises(LowerError, match="256 rows of 1024 columns"):
        build.l1_text(load("attention"), "attention", "l3", shapes=shapes)
