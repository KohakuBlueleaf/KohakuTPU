"""Runtime card binding fields and addresses, against machine facts."""

import json
from pathlib import Path

import pytest

from kohakutpu.compiler import _rs
from kohakutpu.language import machine as MF

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "software/rust/crates/ktpu-machine/tests/fixtures"
NAMES = sorted(p.stem for p in MF.MACHINES.glob("*.json") if p.stem != "measured")


def test_frozen_runtime_machine_views():
    path = ROOT / "software/rust/golden/bindings/card.json"
    for case in json.loads(path.read_text(encoding="utf-8"))["cases"]:
        text = (MF.MACHINES / f"{case['machine']}.json").read_text(encoding="utf-8")
        inp, want = case["input"], case["output"]
        got = _rs.machine_view(text, inp["nodes"], inp["units"])
        assert json.loads(json.dumps(got)) == want["view"], case["machine"]
        for mesh, offset, global_addr, stage_addr in want["addresses"]:
            assert _rs.global_addr(text, offset, mesh) == global_addr
            assert _rs.stage_addr(text, offset, mesh) == stage_addr


@pytest.mark.parametrize("name", NAMES)
def test_view_fields_and_addresses(name):
    text = (MF.MACHINES / f"{name}.json").read_text(encoding="utf-8")
    facts = json.loads(text)["machine"]
    for nodes in (None, 1, facts["card"]["nodes"]):
        expected = nodes if nodes is not None else facts["card"]["nodes"]
        enumeration = {k.upper(): v for k, v in facts["die"]["units"].items()}
        missing = {k: list(v) for k, v in enumeration.items()}
        next(v for v in missing.values() if v).pop()
        for units in (None, enumeration, missing):
            view = _rs.machine_view(text, nodes, units)
            assert len(view["meshes"]) == expected
            assert view["stage_bytes"] == 1 << 21
            assert view["board"] == name
            assert view["inst_depth"] == facts["unit"]["inst_depth"]
            assert view["params"]["VC"]["regs"] == facts["vc"]["regs"]
            assert view["params"]["VC"]["copies"] == facts["vc"]["rf_copies"]
            for mesh in view["meshes"]:
                assert mesh["units"] == {
                    k: [tuple(c) for c in v]
                    for k, v in (units or enumeration).items()
                    if v
                }
    for mesh in range(facts["card"]["nodes"]):
        for offset in (0, 1, 1 << 20, (1 << 21) - 1):
            assert _rs.global_addr(text, offset, mesh) == offset | (mesh << 36)
            assert _rs.stage_addr(text, offset, mesh) == offset | (mesh << 36) | (
                1 << 39
            )
    for fn, value, mesh in (
        (_rs.stage_addr, 1 << 21, 0),
        (_rs.global_addr, 1 << 36, 0),
        (_rs.stage_addr, 0, facts["card"]["nodes"]),
        (_rs.global_addr, 0, facts["card"]["nodes"]),
    ):
        with pytest.raises(ValueError):
            fn(text, value, mesh)


def test_card_fixture_mutants():
    text = (FIXTURES / "multimesh_v9t6+entry_words=2.json").read_text(encoding="utf-8")
    assert _rs.machine_view(text)["stage_bytes"] == 1 << 20
    assert (
        _rs.machine_view(text, mutant="machine.card/stage-banks")["stage_bytes"]
        != 1 << 20
    )
    text = (FIXTURES / "multimesh_v9t6+mesh_bits=1.json").read_text(encoding="utf-8")
    with pytest.raises(ValueError):
        _rs.machine_view(text)
    assert len(_rs.machine_view(text, nodes=2)["meshes"]) == 2
    assert _rs.stage_addr(text, 0, 1, nodes=2) == (1 << 39) | (1 << 36)
    assert _rs.global_addr(text, 0, 1, nodes=2) == 1 << 36
    for fn in (_rs.stage_addr, _rs.global_addr):
        with pytest.raises(ValueError):
            fn(text, 0, 1, nodes=1)
    assert len(_rs.machine_view(text, mutant="machine.card/mesh-bits")["meshes"]) == 4
    doc = json.loads(
        (FIXTURES / "multimesh_v9t6+mesh_shift=37,mesh_bits=1.json").read_text(
            encoding="utf-8"
        )
    )
    doc["machine"]["card"].update(nodes=2, chain=[0, 1])
    text = json.dumps(doc)
    assert _rs.stage_addr(text, 1, 1) == (1 << 39) | (1 << 37) | 1
    assert _rs.global_addr(text, 1, 1) == (1 << 37) | 1
    assert (
        _rs.stage_addr(text, 1, 1, mutant="machine.card/mesh-shift")
        != (1 << 39) | (1 << 37) | 1
    )
