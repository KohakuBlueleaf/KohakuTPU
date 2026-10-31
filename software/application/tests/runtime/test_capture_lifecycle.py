"""Whole-recording private lifetimes, host boundaries and EXP2 legalization."""

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels import vector as V
from kohakutpu.application.ktpugrad.device import KTPUAllocator
from kohakutpu.application.runtime.device import Device
from kohakutpu.application.runtime.graph import recorded as NR
from kohakutpu.application.runtime.graph.native import NativeLaunch, compose
from kohakutpu.application.runtime.storage import Storage, StorageView
from kohakutpu.stack import kernel as K


def row(dev, expression):
    k, given, _ = V.rows_kernel(
        expression,
        (V.Operand("full", pitch=128),),
        16,
        128,
        V.Operand("full", pitch=128),
        1,
    )
    return k.trace(given, machine=K.dsl_machine(dev.machine)).graph


def private_kernel(dev):
    a = NativeLaunch(row(dev, X.op("add", X.Load(0), 0.125)), {}, "dsl")
    b = NativeLaunch(row(dev, X.op("max", X.Load(0), 0)), {}, "dsl")
    return dev.kernel(json.dumps(compose(a, b, "out", ("in0",)).graph))


def test_capture_shares_its_stage_window_and_rolls_back_failed_admission():
    dev = Device("offline", board="vlt_v9t6s_1n")
    k = private_kernel(dev)
    x, y = dev.alloc(4096), dev.alloc(4096)
    dev.record()
    before = dev.memory.staging[0].free
    with (
        patch.object(type(k), "programs", side_effect=ValueError("binding")),
        pytest.raises(ValueError, match="binding"),
    ):
        dev.run(k, {"p_in0": x, "c_out": y})
    assert dev.memory.staging[0].free == before
    dev.run(k, {"p_in0": x, "c_out": y})
    dev.run(k, {"p_in0": x, "c_out": y})
    log = dev.recorded()
    slots = {at for arena, at in log.references if arena is dev.memory.staging[0]}
    assert len(slots) == 1
    native = NR.prepare(log, quant=False, elementwise=False)
    native.close()
    log.close()
    dev.close()


def test_host_write_hoist_requires_disjoint_prior_accesses():
    dev = Device("offline", board="vlt_v9t6s_1n")
    k = dev.kernel(json.dumps(row(dev, X.op("max", X.Load(0), 0))))
    x, h, y = [dev.alloc(4096) for _ in range(3)]
    dev.record()
    dev.run(k, {"in0": x, "out": h})
    dev.write(y, bytes(4096))
    dev.run(k, {"in0": y, "out": h})
    log = dev.recorded()
    off = NR.prepare(log, hoist=False, quant=False, elementwise=False)
    on = NR.prepare(log, hoist=True, quant=False, elementwise=False)
    assert [k for k, _ in off.segments] == ["run", "write", "run"]
    assert [k for k, _ in on.segments] == ["write", "run"]
    off.close()
    on.close()
    log.close()
    dev.record()
    dev.run(k, {"in0": x, "out": h})
    dev.write(h, bytes(4096))
    dev.run(k, {"in0": h, "out": y})
    log = dev.recorded()
    on = NR.prepare(log, hoist=True)
    assert [k for k, _ in on.segments] == ["run", "write", "run"]
    on.close()
    log.close()
    dev.close()


def test_full_owner_upload_needs_no_read_and_partial_view_preserves_neighbours():
    dev = Device("offline", board="vlt_v9t6s_1n")
    alloc = KTPUAllocator(SimpleNamespace(rt=dev))
    st = Storage(dev, 6)
    dev.record()
    alloc._copyin(st, memoryview(b"abcdef"))
    log = dev.recorded()
    assert [event[0] for event in log] == ["write"]
    assert dev.read(st.addr, 32) == b"abcdef" + bytes(26)
    log.close()
    dev.record()
    alloc._copyin(StorageView(st, 2, 2), memoryview(b"XY"))
    log = dev.recorded()
    assert [event[0] for event in log] == ["read", "write"]
    assert dev.read(st.addr, 6) == b"abXYef"
    log.close()
    st.free()
    dev.close()
