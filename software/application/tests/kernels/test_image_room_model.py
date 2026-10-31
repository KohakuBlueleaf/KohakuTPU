"""The image room on the card model (L1.SCHED.007, L2.SEARCH.001): a ViT step's
GELU gradient stream, whose first schedule leaves no image, and a stream that
fits until its throttle syncs, each against its L2 text evaluated in numpy.

    KOHAKUTPU_BOARD=vlt_v9t6s_1n KOHAKUTPU_SIM_BUILD=build/v9t6s/vlt_card_v9t6s_1n pytest ...
"""

import re
from pathlib import Path

import numpy as np
import pytest

from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.language.text.syntax import TextError
from kohakutpu.stack import kernel as K
from kohakutpu.stack import rs

ROWS = 24
VIEWS, WIDE_ROWS = 7, 48
FIXTURES = Path(__file__).parents[4] / "software/rust/crates/ktpu-l2/tests/fixtures"
TEXT = (FIXTURES / "gelu_grad.l2").read_text()
WIDE = (FIXTURES / "wide_mul.l2").read_text()
OPS = {
    "add": lambda a, b: a + b,
    "mul": lambda a, b: a * b,
    "exp2": lambda a: np.exp2(a),
    "inv": lambda a: 1 / a,
}


def evaluated(h: np.ndarray, x: np.ndarray) -> np.ndarray:
    """The fixture's value `e67`, from its text."""
    env = {"v1": x.astype(np.float32), "v2": h.astype(np.float32)}

    def term(name: str):
        return env[name] if name in env else np.float32(name)

    last = None
    for target, op, args in re.findall(r"(e\d+) = (\w+) ([^:]+):", TEXT):
        last = env[target] = OPS[op](*map(term, args.strip().split(", ")))
    return last.astype(np.float16)


def test_the_gelu_gradient_stream_runs_within_the_room(dev):
    machine = machine_of(dev.machine.board, nodes=1)
    c = K.compile(TEXT, name="gelu", machine=machine)
    rng = np.random.default_rng(ROWS)
    h = rng.standard_normal((ROWS, 128)).astype(np.float16)
    x = rng.standard_normal((ROWS, 128)).astype(np.float16)
    binds = {name: dev.alloc(h.nbytes) for name in ("h1", "in1", "out")}
    dev.write(binds["h1"], h.tobytes())
    dev.write(binds["in1"], x.tobytes())
    dev.run(c, binds)
    got = np.frombuffer(dev.read(binds["out"], h.nbytes), np.float16)
    want = evaluated(h, x)
    assert np.allclose(got.reshape(ROWS, 128), want, rtol=4e-3, atol=4e-3)
    for at in binds.values():
        dev.free(at)


def test_a_stream_that_fits_until_its_throttle_runs_within_the_room(dev):
    machine = machine_of(dev.machine.board, nodes=1)
    c = K.compile(WIDE, name="wide", machine=machine)
    rng = np.random.default_rng(VIEWS)
    inputs = [
        rng.uniform(0.75, 1.25, (WIDE_ROWS, 128)).astype(np.float16)
        for _ in range(VIEWS)
    ]
    binds = {f"x{i}": dev.alloc(x.nbytes) for i, x in enumerate(inputs)}
    for i, x in enumerate(inputs):
        dev.write(binds[f"x{i}"], x.tobytes())
    binds["y"] = dev.alloc(inputs[0].nbytes)
    dev.run(c, binds)
    got = np.frombuffer(dev.read(binds["y"], inputs[0].nbytes), np.float16)
    env = {f"v{i}": x.astype(np.float32) for i, x in enumerate(inputs)}
    for target, a, b in re.findall(r"(w\d+) = mul (\w+), (\w+)", WIDE):
        env[target] = env[a] * env[b]
    want = env["w12"].astype(np.float16)
    assert np.allclose(got.reshape(WIDE_ROWS, 128), want, rtol=4e-3, atol=4e-3)
    for at in binds.values():
        dev.free(at)


def test_the_stream_is_refused_without_the_fit():
    machine = machine_of("vlt_v9t6s_1n", nodes=1)
    with rs.mutant("l1.sched/unfit"), pytest.raises(TextError, match="VLOOP"):
        K.compile(TEXT, name="gelu", machine=machine)
