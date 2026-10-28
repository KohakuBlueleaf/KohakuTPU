"""Analytical cost and derived-ledger checks, with deliberately corrupt inputs."""

import copy
import json

import pytest
from derive import parameters, with_parameters
from report import summarize
from work import work


def test_walk_coordinates_preserve_values_and_match_counts():
    args = {
        "src_bytes": 512,
        "dst_bytes": 448,
        "at": 32,
        "dat": 0,
        "levels": [[2, 256], [3, 32]],
        "dlevels": [[6, 32]],
    }
    values = parameters(args, class_walk=True)
    assert with_parameters(args, values) == args
    values["levels/0/0"] = 5
    got = with_parameters(args, values)
    assert got["dlevels"][0][0] == 15
    assert got["src_bytes"] == 1280
    bad = copy.deepcopy(values)
    del bad["levels/0/0"]
    assert with_parameters(args, bad)["dlevels"][0][0] != 15


def test_quant_coordinates_preserve_values_and_padding():
    args = {
        "src": {
            "type": "Operand",
            "fields": {
                "kind": "full",
                "dtype": "f16",
                "offset": 16,
                "pitch": 96,
                "rows": 0,
            },
        },
        "rows": 9,
        "cols": 80,
        "rp": 16,
        "cp": 128,
        "g": 2,
        "nk": 2,
        "batches": 3,
        "side": "a",
    }
    values = parameters(args, quant_kblock=32)
    assert with_parameters(args, values) == args
    values["quant/rows:units1"] = 17
    got = with_parameters(args, values)
    assert got["rp"] == 24
    assert got["src"]["fields"]["offset"] == 16


def test_linear_walk_cost_counts_every_word_and_axis():
    counts = work("walk", {"levels": ((2, 96), (3, 32)), "dlevels": ((6, 32),)}, {})
    assert counts == {"mover_submissions": 6, "address_alu": 108, "loop_jumps": 6}
    mutant = work("walk", {"levels": ((2, 96), (1, 32)), "dlevels": ((6, 32),)}, {})
    assert mutant != counts


def test_ledger_checks_shape_and_derived_parameter_names(tmp_path):
    root = tmp_path / "experimental-templates"
    root.mkdir()
    (root / "sample.bin").write_bytes(b"test")
    bounds = {"n": {"lo": 1, "hi": 3}}
    table = {"parameters": {**bounds, "trip": {"lo": 1, "hi": 2}}, "sites": []}
    path = root / "sample.json"
    path.write_text(json.dumps(table))
    report = {
        "artifacts": [
            {
                "generator": "copy",
                "bounds": bounds,
                "values": {"trip": {}},
                "meshes": {"0": "sample"},
                "launches": [0],
            }
        ],
        "generators": {
            "copy": {
                "launches": 1,
                "classes": 1,
                "experimental_templates": 1,
                "proven": 1,
                "structure_splits": 0,
                "other_splits": 0,
                "non_affine_fields": [],
                "direct_seconds": 0,
                "derivation_compile_seconds": 0,
            }
        },
        "excluded_lanes": 0,
    }
    ledger = tmp_path / "results.json"
    ledger.write_text(json.dumps(report))
    summarize(ledger)
    table["parameters"]["n"]["hi"] = 4
    path.write_text(json.dumps(table))
    with pytest.raises(ValueError, match="bounds differ"):
        summarize(ledger)
    table["parameters"]["n"]["hi"] = 3
    del table["parameters"]["trip"]
    path.write_text(json.dumps(table))
    with pytest.raises(ValueError, match="missing derived"):
        summarize(ledger)
