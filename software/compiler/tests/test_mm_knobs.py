"""The contraction knobs have one source, the native generator's: Python's
plan is the native plan of the call it would record, its knobs those the
`KTPU_MM_*` overrides resolve natively."""

import json

import pytest

from kohakutpu.application.kernels import matmul as MM
from kohakutpu.compiler import _rs
from kohakutpu.stack import rs

MACHINE = rs.machine_json("multimesh_v9t6")


@pytest.fixture(autouse=True)
def no_overrides(monkeypatch):
    for env, _ in MM.KNOBS.values():
        monkeypatch.delenv(env, raising=False)


def test_the_knob_table_is_the_native_one():
    assert set(MM.KNOBS) == set(json.loads(_rs.mm_knobs()))
    assert {g for _, g in MM.KNOBS.values()} <= {"plan", "kernel"}
    assert MM.plan_knobs() == {"pad_m": False, "n_tiles": 1, "dense_n": False}


def test_an_override_moves_the_plan_through_the_native_knobs(monkeypatch):
    base = MM.plan(1, 1576, 768, 768, machine_json=MACHINE)
    assert (base.tm, base.mp) == (8, 1576)
    monkeypatch.setenv("KTPU_MM_PAD_M", "1")
    assert MM.plan_knobs()["pad_m"] is True
    padded = MM.plan(1, 1576, 768, 768, machine_json=MACHINE)
    assert (padded.tm, padded.mp) == (228, 1596)
    assert MM.plan(1, 1576, 768, 768, machine_json=MACHINE, pad_m=False) == base


def test_the_plan_is_the_recorded_calls_replay(monkeypatch):
    monkeypatch.setenv("KTPU_MM_DENSE_N", "1")
    mm = MM.plan(2, 64, 96, 40, False, 16, machine_json=MACHINE)
    args = {
        "bt": 2,
        "m": 64,
        "n": 96,
        "k": 40,
        "shared": False,
        "quantum": 16,
        **MM.pairing_arg(),
        **MM.plan_knobs(),
    }
    assert vars(mm) == json.loads(_rs.mm_plan(json.dumps(args), MACHINE))
    assert mm.tn == 96


def test_an_n_not_whole_blocks_is_refused():
    with pytest.raises(ValueError, match="API.OP.006"):
        MM.plan(1, 64, 40, 64, machine_json=MACHINE)
