"""RTL-vector producers against frozen bytes, without running RTL or models."""

import importlib.util
import json
import sys
from pathlib import Path

from kohakutpu.compiler import _rs

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "scripts/py"
CASES = json.loads(
    (ROOT / "software/rust/golden/bindings/isa.json").read_text(encoding="utf-8")
)["cases"]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cluster_program_bytes():
    script = load("gen_prog", SCRIPTS / "gen_prog.py")
    script.check_address_fields()
    want = [
        c["output"]["payload"]
        for c in CASES
        if c["case"].startswith(("mg-0-", "mg-1-", "mg-2-"))
    ]
    assert [hex(p) for _, p in script.PROGRAM] == want


def test_vector_flow_program_bytes():
    script = load("v2_flow", SCRIPTS / "v2_flow.py")
    for control in (False, True):
        case = next(
            c
            for c in CASES
            if c["case"] == f"v2-flow-{'control' if control else 'flow'}"
        )
        assert [hex(p) for p in script.program(control)] == case["output"]["payloads"]


def test_sru_header_and_job_words(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS / "sru"))
    cfg = load("cfg", SCRIPTS / "sru/cfg.py")
    monkeypatch.setitem(sys.modules, "cfg", cfg)
    script = load("core_vectors", SCRIPTS / "sru/core_vectors.py")
    want = next(c for c in CASES if c["case"] == "sru-config")
    assert cfg.header() == want["output"]["header"]
    for c in CASES:
        if not c["case"].startswith("sru-words") or "refused" in c["output"]:
            continue
        inp = c["input"]
        j = inp["job"]
        got = script.cfg_words(
            {"G": j["g"], "P": j["passes"], "OPS": j["ops"], "FIN": j["fin"]},
            inp["f_lanes"],
            inp["h_taps"],
        )
        assert sorted([a, v] for a, v in got.items()) == c["output"]["words"]
    assert cfg.pack(cfg.G, {"NW": 8}) == {0: 8}
    fields, ii, sigma = cfg.schedule([{"A": "X", "B": "K", "C": "K"}], 16)
    assert (ii, sigma) == (1, [0])
    assert fields[0]["KW"] == cfg.WLEAD
    try:
        _rs.sru_pack({"bad": [0, 0, 64]}, {"bad": 1}, cfg.MACHINE)
    except ValueError:
        pass
    else:
        raise AssertionError("oversized field accepted")
