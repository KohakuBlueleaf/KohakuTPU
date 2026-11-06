"""Semantic SRU requests use the physical machine's pool."""

import json

import pytest

from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.stack.rs import _rs


def test_pool_arguments_cannot_replace_physical_capabilities():
    machine = json.loads(machine_of("sru_v9t7", nodes=1).machine_json)
    profile = machine["machine"]["sr"]["profile"]
    lanes, taps = profile["F_LANES"], profile["H_TAPS"]
    job = json.dumps(
        {
            "g": {"NW": 8, "NSR": 1, "OUT": 1},
            "passes": [{"FINLEN": 1}],
            "fin": [{"OP": 3, "A": 6, "C": 7}],
        }
    )
    assert _rs.sru_words(job, lanes, taps, json.dumps(machine))
    profile["F_LANES"] = 0
    with pytest.raises(_rs.Refused, match="ISA.SR.024"):
        _rs.sru_words(job, lanes, taps, json.dumps(machine))
    with pytest.raises(_rs.Refused, match="ISA.SR.024"):
        _rs.sru_words(job, 0, taps, json.dumps(machine))
    profile["F_LANES"] = lanes
    with pytest.raises(_rs.Refused, match="ISA.SR.024"):
        _rs.sru_words(job, lanes, taps + 1, json.dumps(machine))
