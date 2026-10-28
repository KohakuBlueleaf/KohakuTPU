"""The debug runner's offline package producers retain their frozen bytes."""

import json
from pathlib import Path
from types import SimpleNamespace

from runners import dbg

from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.language import machine as MF

GOLDEN = Path(__file__).resolve().parents[2] / "rust/golden/bindings/package.json"
CASES = json.loads(GOLDEN.read_text(encoding="utf-8"))["cases"]


def test_debug_packages_match_frozen_callers():
    name = "multimesh_v9t6"
    machine = machine_of(name)
    card = SimpleNamespace(
        machine=machine,
        machine_json=machine.machine_json,
        nodes=[0],
        fetch=machine.fetch,
    )
    doc = MF.load(name)
    want = {
        c["case"]: c["output"]["bytes"]
        for c in CASES
        if c["case"].startswith(("build-dbg-", "stream-stalled-"))
    }
    assert dbg.failing(card, doc, False).hex() == want["build-dbg-failing"]
    assert dbg.failing(card, doc, True).hex() == want["build-dbg-failing-lowered"]
    for writes in range(9):
        assert dbg.stalled(card, doc, writes).hex() == want[f"stream-stalled-{writes}"]
