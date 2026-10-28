"""Native mutants change output or produce a numbered compiler diagnostic."""

import re

import pytest
from harness import vectors

from kohakutpu.stack import rs

PORTS = [
    v for v in vectors.load() if v["tier"] == 0 and v["runner"] in rs.spec_runners()
]


def run(v):
    return rs.spec_run(
        v["runner"], v["input"], v.get("machine"), vector_id=v.id, expect=v["expect"]
    )


@pytest.mark.parametrize("v", PORTS, ids=[v.id for v in PORTS])
def test_rust_port_mutants_change_output_or_clause_diagnostic(v):
    baseline = run(v)
    if v["status"] == "ruling":
        if baseline["result"] != baseline["expect"]:
            pytest.xfail("ruling not yet followed")
        pytest.fail("the stack follows this ruling now: mark it frozen")
    assert baseline["result"] == baseline["expect"]
    for name in v["mutants"]:
        assert name in rs.mutants()
        try:
            with rs.mutant(name):
                broken = run(v)
        except ValueError as exc:
            assert re.match(r"^L[012]\.[A-Z]+\.\d{3}: ", str(exc)), (name, str(exc))
        else:
            assert broken["expect"] == baseline["expect"]
            assert broken["result"] != baseline["expect"], name
        assert run(v) == baseline
