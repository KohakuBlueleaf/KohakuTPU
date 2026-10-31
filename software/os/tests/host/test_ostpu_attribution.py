"""Inclusive timing scopes, cycle-stamp ring summaries, and relocate/package witnesses."""

import importlib.util
import pathlib
import struct
from dataclasses import dataclass

import pytest

TOOLS_OSTPU = pathlib.Path(__file__).resolve().parents[2] / "tools/ostpu"

SPEC = importlib.util.spec_from_file_location(
    "ostpu_attribution", TOOLS_OSTPU / "attribution.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

SPEC_REL = importlib.util.spec_from_file_location(
    "ostpu_relocate", TOOLS_OSTPU / "relocate.py"
)
RELOC = importlib.util.module_from_spec(SPEC_REL)
SPEC_REL.loader.exec_module(RELOC)

SPEC_PKG = importlib.util.spec_from_file_location(
    "ostpu_package", TOOLS_OSTPU / "package.py"
)
PKG = importlib.util.module_from_spec(SPEC_PKG)
SPEC_PKG.loader.exec_module(PKG)


def test_inclusive_scopes_leave_unobserved_terms_unknown():
    os_run = dict(
        launches=2,
        results=[dict(cycles=100, cyc_msgs=70, cyc_issue=50, unit_batches=4, tasks=6)],
    )
    fw = dict(
        results=[
            dict(cycles=20, packages=[dict(phases=dict(head=2, steps=18, idle=9))])
        ]
    )
    result = MODULE.attribution(os_run, fw)
    assert result["os_phase_sum_minus_run_cycles"] == 20
    assert result["os_total_per_launch"] == 50
    assert result["firmware_package_total_per_launch"] == 10
    assert result["firmware_inclusive_per_launch"]["idle"] == 4.5
    assert all(v is None for v in result["requested_exclusive_os"].values())
    mutant = dict(result["requested_exclusive_os"], user_dram_fetch=-20)
    assert not all(v is None for v in mutant.values())


def test_ring_summary_splits_user_dram_and_kernel_subterms():
    records = [
        dict(
            cyc_total=1000,
            cyc_walk=200,
            cyc_dep=50,
            cyc_xlat=120,
            cyc_build=400,
            cyc_submit=80,
            cyc_wait=30,
            cyc_retire=100,
            cyc_sys=350,
            syscalls=5,
            batches=2,
            id=0,
        ),
        dict(
            cyc_total=600,
            cyc_walk=150,
            cyc_dep=30,
            cyc_xlat=40,
            cyc_build=200,
            cyc_submit=60,
            cyc_wait=10,
            cyc_retire=50,
            cyc_sys=200,
            syscalls=3,
            batches=1,
            id=1,
        ),
    ]
    out = MODULE.ring_summary(records)
    split = out["disjoint_cpu_split"]
    assert (
        split["user_dram_total"] + split["kernel_imem_total"]
        == out["sums"]["cyc_total"]
    )
    assert (
        split["kernel_xlat_total"]
        + split["kernel_submit_total"]
        + split["kernel_wait_total"]
        + split["kernel_trap_and_other_total"]
        == split["kernel_imem_total"]
    )
    assert (
        split["user_dram_total"] == 1050 and split["kernel_trap_and_other_total"] == 210
    )


def test_step_ring_summary_partitions_step_ipc_and_cache_subterms():
    raw = dict(
        step_total=30000,
        client_cache_acq=0,
        client_send=1800,
        server_wake_recv=1900,
        check_or_reuse=1400,
        cache_release=0,
        de_run=23500,
        cache_acquire=0,
        server_reply=1400,
        kds_run_cycles=26300,
        cyc_msgs=3300,
        retained_hit=1,
    )
    out = MODULE.step_ring_summary(raw)
    assert out["retained_hit"] is True
    assert out["ipc_and_cache_total"] == 6500
    assert out["ipc_and_cache_total"] + out["de_run_cycles"] == out["step_total"]
    assert sum(out["breakdown"].values()) == out["ipc_and_cache_total"]
    mutant = dict(raw, cache_acquire=1800)
    assert MODULE.step_ring_summary(mutant)["ipc_and_cache_total"] != 6500


def test_relocate_rebases_writes_and_programs_and_rejects_out_of_bounds():
    @dataclass
    class FakeProg:
        bindings: tuple
        translations: tuple | None = None

        def bound(self, _check):
            return self.translations if self.translations is not None else self.bindings

    events = [
        ("write", 0x10040, b"\x11" * 32),
        ("program", {0: FakeProg(bindings=(0x10000, 0x10100))}),
        (
            "program",
            {0: FakeProg(bindings=(), translations=((0x4000, 0x10080, 64),))},
        ),
    ]
    out = RELOC.relocate(events, 0x10000, 0x20000000, 0x1000)
    assert out[0] == ("write", 0x20000040, b"\x11" * 32)
    assert out[1] == ("run", {0: ((0x20000000, 0x20000100), ())})
    assert out[2] == ("run", {0: (((0x4000, 0x20000080, 64),), ())})
    with pytest.raises(ValueError, match="outside tensor arena"):
        RELOC.relocate([("write", 0x12000, b"\x00")], 0x10000, 0x20000000, 0x1000)


def test_package_operand_and_expected_output_witness():
    op1 = PKG.operand(1)
    op2 = PKG.operand(2)
    assert len(op1) == 128 and op1 != op2
    exp1 = struct.unpack("<16e", PKG.expected_output(1))
    exp2 = struct.unpack("<16e", PKG.expected_output(2))
    assert exp1 == (32.0,) * 16 and exp2 == (128.0,) * 16
