"""DSL to L2: each small kernel on the evaluator against its L2 on the simulator."""

import json
import pathlib
import sys

import numpy as np
import pytest

import kohakutpu.language.dsl as kt
from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.application.runtime.offline import HostMemory
from kohakutpu.language.examples.dsl.cluster.linear import linear
from kohakutpu.language.examples.dsl.vector.activation import activation
from kohakutpu.language.examples.dsl.vector.glu import glu
from kohakutpu.language.examples.dsl.vector.norm import layernorm
from kohakutpu.language.examples.dsl.vector.softmax import softmax
from kohakutpu.stack import rs
from kohakutpu.stack.buffer import Buffer
from kohakutpu.stack.kernel import dsl_machine
from kohakutpu.stack.rs import _rs

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from evaluate import evaluate

BOARD = "multimesh_v9t8"
PAGE = 1 << 16
#: f16 math stays this near the ideal, of the output's largest value.
PLAIN = 1e-3
MX7 = 0.03


@pytest.fixture(scope="module")
def machine():
    return machine_of(BOARD)


def on_the_simulator(traced, inputs, text):
    """Output tensors of `text`, the bytes through each port's own layout."""
    json_machine = machine_of(BOARD).machine_json
    mem = HostMemory()
    binds, buffers = {}, {}
    at = PAGE
    for name, spec in traced.params:
        b = Buffer.of(spec.text(), json_machine)
        buffers[name] = b
        binds[name] = at
        at += -(-b.nbytes // PAGE) * PAGE
        mem.write_block(binds[name], b.pack(inputs.get(name, np.zeros(spec.shape))))
    _rs.simulate_l2(text, traced.name, binds, mem.read_block, mem.write_block)
    return {
        n: b.unpack(mem.read_block(binds[n], b.nbytes))
        for n, b in buffers.items()
        if traced.roles[n] == "out"
    }


def check(kernel, machine, given, inputs, bound=PLAIN, **consts):
    traced = kernel.trace(given, machine=dsl_machine(machine), **consts)
    graph = json.dumps(traced.graph)
    text = _rs.l2_of(graph, machine.machine_json, rs._MUTANT.get())
    want = evaluate(traced.graph, inputs)
    got = on_the_simulator(traced, inputs, text)
    for name, g in got.items():
        scale = max(float(np.abs(want[name]).max()), 1e-3)
        worst = float(np.abs(g - want[name]).max())
        assert worst <= bound * scale, f"{name}: {worst} of {scale}"


R = np.random.default_rng(0)


@pytest.mark.parametrize("act", ["none", "silu", "gelu", "relu"])
def test_activations(machine, act):
    x = R.normal(size=(16, 64))
    check(activation, machine, {"x": x.astype(np.float16)}, {"x": x}, act=act)


def test_softmax(machine):
    x = R.normal(size=(16, 64))
    check(softmax, machine, {"x": x.astype(np.float16)}, {"x": x})


def test_layernorm(machine):
    x, g, b = R.normal(size=(16, 64)), R.normal(size=64), R.normal(size=64)
    inputs = {"x": x, "g": g, "b": b}
    check(
        layernorm, machine, {k: v.astype(np.float16) for k, v in inputs.items()}, inputs
    )


def test_glu(machine):
    a, g = R.normal(size=(16, 64)), R.normal(size=(16, 64))
    inputs = {"a": a, "g": g}
    check(glu, machine, {k: v.astype(np.float16) for k, v in inputs.items()}, inputs)


def test_contraction(machine):
    x, w = R.normal(size=(32, 64)), R.normal(size=(64, 64))
    bias = R.normal(size=(64,))
    given = {
        "x": kt.TensorSpec.make((32, 64), "mx7a", {"gm": 8, "nk": 2}),
        "w": kt.TensorSpec.make((64, 64), "mx7b", {"gn": 16, "nk": 2}),
        "bias": bias.astype(np.float16),
    }
    check(
        linear,
        machine,
        given,
        {"x": x, "w": w, "bias": bias},
        MX7,
        tn=64,
        tm=32,
        rows=4,
    )
