"""Retained target runners compile through the Rust seam on an offline card."""

from types import SimpleNamespace

import pytest
import runners  # noqa: F401
from harness import vectors
from harness.fresh import fresh
from harness.registry import RUNNERS, patched

from kohakutpu.application.runtime.device import Device
from kohakutpu.language import machine
from kohakutpu.stack import rs

COMPILING = [
    ("isa.card.example", lambda v: v["input"]["case"].endswith(".l2"), "l2"),
    ("isa.card.example", lambda v: not v["input"]["case"].endswith(".l2"), "l1"),
    ("num.card.example", lambda v: v["input"]["source"] == "l2", "l2"),
    ("isa.card.kernel", lambda v: True, "l1"),
    ("l1.card", lambda v: True, "l1"),
    ("l0.card.example", lambda v: True, "l1"),
    ("l2.card", lambda v: v["input"].get("against") != "nopasses", "ref"),
    ("l2.card", lambda v: v["input"].get("against") == "nopasses", "nopasses"),
]
CASES = [
    next(v for v in vectors.load() if v["runner"] == r and pick(v))
    for r, pick, _ in COMPILING
]


def count_compiles(v):
    got = []
    original = rs.compile

    def counted(*args, **kwargs):
        got.append(kwargs)
        return original(*args, **kwargs)

    dev = Device(kind="offline", board=v["machine"])
    ctx = {
        "vector": v,
        "machine": machine.load(v["machine"]),
        "session": SimpleNamespace(dev=dev),
    }
    with fresh(None), patched(rs, "compile", counted):
        RUNNERS[v["runner"]](v["input"], ctx)
    return got


@pytest.mark.parametrize("v", CASES, ids=[f"{r}-{level}" for r, _, level in COMPILING])
def test_target_runner_compiles_through_rust(v):
    got = count_compiles(v)
    assert got
    if v["input"].get("against") == "nopasses":
        assert any(c.get("passes") is False for c in got)
