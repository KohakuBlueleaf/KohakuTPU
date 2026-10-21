"""Compiled kernels for their values, with nothing attached: test_compile
cases on the offline device, every package run on the behaviour model
(`simulation.behave.card`), against tinygrad on the CPU at test_compile's
tolerance. One case of each lowering path."""

import importlib.util
import pathlib

import pytest

from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime.offline import OfflineCard
from kohakutpu.simulation.behave.card import Card

_SPEC = importlib.util.spec_from_file_location(
    "test_compile", pathlib.Path(__file__).parents[1] / "ktpugrad" / "test_compile.py"
)
assert _SPEC is not None and _SPEC.loader is not None
TC = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(TC)

CASES = [
    "unary.gelu(2, 3, 17, 19)",
    "binary.where(33,)",
    "broadcast.add(4, 1, 8)x(1, 5, 8)",
    "reduce.sum(3, 17, 33)@(0, 2)",
    "norm.layernorm(7, 33)",
    "norm.softmax(16, 128)",
    "move.transpose",
    "move.offset_cols_sum_narrow",
    "move.cat1",
    "matmul.xw(32,64,96)",
    "matmul.batched(3,1,7,16,5)",
    "matmul.bias_offset",
    "conv.(2,1,28,28)o6k5s1p2d1g1",
    "pool.max(2, 16, 10, 10)k2sNone",
    "attn.causal(1,1,7,16)",
    "extra.triu_masked_fill",
    "extra.interp_nearest",
    "grad.conv",
]


@pytest.fixture(scope="module", autouse=True)
def behaviour():
    before = D._device
    dev = D.Device(kind="offline")
    assert isinstance(dev.card, OfflineCard)
    card = Card(dev.card.mem, dev.card.machine)

    def execute(_, built) -> dict:
        for pkg, bindings in built.values():
            card.run(pkg, bindings)
        return {
            "cycles": 0,
            "steps": [],
            "phases": {},
            "pkg": next(iter(built.values()))[0],
        }

    dev.card.execute = execute
    D.attach(dev)
    yield
    D._device = before


@pytest.mark.parametrize("name", CASES)
def test_the_card_computes_what_the_cpu_does(name):
    got = [t.numpy() for t in TC.build(name)]
    want = [t.numpy() for t in TC.build(name, "CPU")]
    for g, w in zip(got, want, strict=True):
        TC.agree(g, w)
