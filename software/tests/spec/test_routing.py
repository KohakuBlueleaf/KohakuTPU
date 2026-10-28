"""Frontends use the Rust seam even when Python compiler imports are forbidden."""

import builtins
import contextlib
import importlib

import pytest
import runners
from harness import vectors
from harness.fresh import fresh
from harness.registry import MUTANTS, RUNNERS

from kohakutpu.language import machine
from kohakutpu.language.examples.dsl.vector.softmax import softmax

FRESH = importlib.import_module("harness.fresh")

assert runners.LAYERS
FRONTENDS = [
    v
    for v in vectors.load()
    if v["tier"] == 0
    and v["status"] == "frozen"
    and v["runner"].startswith(("dsl.", "api.", "tg."))
]


def prohibit_compiler(monkeypatch):
    original = builtins.__import__

    def guarded(name, globals=None, locals=None, fromlist=(), level=0):
        if name.startswith("kohakutpu.compiler") and not (
            name == "kohakutpu.compiler" and tuple(fromlist) == ("_rs",)
        ):
            raise AssertionError("front-end reached the Python compiler")
        return original(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarded)


@pytest.mark.parametrize("v", FRONTENDS, ids=[v.id for v in FRONTENDS])
def test_frontend_never_calls_the_python_compiler(v, monkeypatch):
    prohibit_compiler(monkeypatch)
    ctx = {
        "vector": v,
        "machine": machine.load(v["machine"]) if v.get("machine") else None,
        "session": None,
    }
    with fresh(None):
        assert RUNNERS[v["runner"]](v["input"], ctx) == ctx.get("expect", v["expect"])


def order_probe():
    v = next(v for v in FRONTENDS if v["clause"] == "DSL.KERN.010")
    ctx = {"vector": v, "machine": machine.load(v["machine"]), "session": None}
    with fresh(None), MUTANTS["dsl.kernel/require_off"]():
        broken = RUNNERS[v["runner"]](v["input"], ctx)
    assert broken != v["expect"]
    with fresh(None):
        return RUNNERS[v["runner"]](v["input"], ctx), v["expect"]


def test_require_mutant_cannot_poison_a_later_imported_kernel_trace(monkeypatch):
    monkeypatch.setattr(softmax, "_cache", {})
    got, expected = order_probe()
    assert got == expected
    assert softmax._cache == {}


def test_retaining_instance_cache_reproduces_the_order_failure(monkeypatch):
    monkeypatch.setattr(softmax, "_cache", {})
    monkeypatch.setattr(FRESH, "local_trace_caches", contextlib.nullcontext)
    got, expected = order_probe()
    assert got != expected
    assert got["softmax_rows"] == 16
    assert expected["softmax_rows"] == 8


def test_import_guard_has_a_mutation_witness(monkeypatch):
    prohibit_compiler(monkeypatch)
    with pytest.raises(AssertionError, match="Python compiler"):
        builtins.__import__("kohakutpu.compiler", fromlist=("build",))
