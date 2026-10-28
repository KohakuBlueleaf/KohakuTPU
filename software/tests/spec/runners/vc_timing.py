"""Tier-2 timing facts from the RTL replay, assembled only by Rust."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

from harness.registry import mutant, patched, runner

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "scripts" / "py"))

from measure import vc_timing as measure

FAULT = SimpleNamespace(machine_offset=0)


@runner("vc.timing.measured")
def measured(inp, ctx):
    name = inp["parameter"]
    record = json.loads((ROOT / "machines/measured/vc_timing.v21.mq1.json").read_text())
    machine = (ROOT / "machines" / f"{ctx['vector']['machine']}.json").read_text()
    assert measure.core_defines(record["image"]) == record["defines"]
    measure.build_model(ROOT / "build" / "r63-v9t6", record["defines"])
    probes = [
        measure.measure_series(
            kind,
            vl,
            dependent,
            machine,
            ROOT / "build" / "r63-v9t6",
            record["defines"],
        )
        for kind in measure.BENCHES[name]
        for vl in (16, 64, 128)
        for dependent in (True, False)
    ]
    value = measure.values(probes, (name,))[name]["value"]
    machine_value = json.loads(machine)["machine"]["vc"]["timing"][name]
    assert value == machine_value + FAULT.machine_offset
    assert value == record["parameters"][name]["value"]
    return {"value": value}


@mutant("vc.timing/machine-plus-one")
def machine_plus_one():
    with patched(FAULT, "machine_offset", 1):
        yield
