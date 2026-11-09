"""Runtime grants exclude resident queue storage from split admission."""

import json

import pytest

from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.stack.rs import _rs


def test_split_runtime_budget_includes_the_queue():
    machine = machine_of("multimesh_v9t8").machine_json
    arguments = {
        "mm": {
            "made_by": {
                "gen": "matmul.plan",
                "args": {
                    "bt": 4,
                    "m": 197,
                    "n": 3072,
                    "k": 768,
                    "shared": True,
                    "quantum": 4,
                    "waves": True,
                    "pad_m": False,
                    "n_tile": "auto",
                },
            }
        },
        "e": {
            "nodes": [{"load": 0}, {"load": 1}, {"op": "add", "args": [0, 1]}],
            "root": 2,
        },
        "extras": [{"kind": "col", "dtype": "f16", "offset": 0, "pitch": 0, "rows": 0}],
        "out": {
            "kind": "full",
            "dtype": "f16",
            "offset": 0,
            "pitch": 3072,
            "rows": 800,
        },
        "activation_any": False,
        "activation_fp16": False,
        "ring_depth": 2,
        "quant_ring": 0,
        "tail_bands": True,
    }
    # Two batch rings fit the physical store but not beside its resident queue.
    with pytest.raises(ValueError, match="DSL.KERN.020.*staging capacity:"):
        _rs.generate_split(
            "matmul.mm_kernel", json.dumps(arguments), machine, [3, 1], "m"
        )
