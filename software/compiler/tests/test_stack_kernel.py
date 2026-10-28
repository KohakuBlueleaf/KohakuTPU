"""Runtime-facing Rust seam: immutable packages and explicit execution nodes."""

import struct

import pytest

from kohakutpu.application.runtime import packages
from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.compiler import _rs
from kohakutpu.language.text import module as M
from kohakutpu.stack import kernel, rs

SOURCE = """target ktpu.v9
fn k.l1()
    signal 1, 2
"""


@pytest.mark.parametrize("mesh", range(4))
def test_l1_executes_on_the_requested_mesh(mesh):
    machine = machine_of("multimesh_v9t6")
    module = kernel.on_mesh(M.read(SOURCE), "k", mesh)
    compiled = kernel.compile(module, "k", machine, level="l1")
    progs = compiled.programs({})
    p = progs[None if mesh == 0 else mesh]
    assert p.mesh == mesh
    cores = _rs.Cores(machine.machine_json)
    out = packages.built({mesh: p}, machine, machine.fetch, {mesh: cores})
    plain, low, bindings = out[mesh]
    assert not bindings
    assert plain.raw == _rs.bind(p.package, [], machine.machine_json, mesh)
    assert low.raw == _rs.bind(p.package, [], machine.machine_json, mesh, True)
    assert plain.steps[0].op.name == "SIGNAL"
    assert (plain.steps[0].count, plain.steps[0].arg) == (1, 2)
    assert compiled.programs({}) is progs
    assert cores.digest() == _rs.Cores(machine.machine_json).digest()


def test_l1_bindings_reuse_one_unbound_compile(monkeypatch):
    machine = machine_of("vlt_v9t6_2n")
    source = """target ktpu.v9
fn reloc_once.l1(out: u64[16] @dram)
    store out <- 0x1234
    r1 = load out
    store out + 8 <- r1
"""
    compiled = kernel.compile(source, "reloc_once", machine, level="l1")
    assert compiled.bound is None
    assert compiled.packages[0]["params"] == ["out"]

    def forbidden(*args, **kwargs):
        pytest.fail("programs(binds) recompiled L1 instead of binding its package")

    monkeypatch.setattr(rs, "compile", forbidden)
    first = compiled.programs({"out": 0x200000})[None]
    second = compiled.programs({"out": 0x400000})[None]
    assert first.package == second.package
    assert first.bindings == (0x200000,)
    assert second.bindings == (0x400000,)
    assert rs.bind(
        first.package, first.bindings, first.machine_json, 0, False
    ) != rs.bind(second.package, second.bindings, second.machine_json, 0, False)


def test_l1_packages_bind_virtual_handles_through_the_mmu_snapshot():
    machine = machine_of("vlt_v9t6_2n")
    source = """target ktpu.v9
fn reloc_handle.l1(out: u64[16] @dram)
    store out <- 0x1234
"""
    compiled = kernel.compile(source, "reloc_handle", machine, level="l1")
    handle = 1 << 40
    program = compiled.programs({"out": handle})[None]
    for physical in (0, 0x900000):
        raw = rs.bind(
            program.package,
            program.bindings,
            program.machine_json,
            0,
            False,
            translations=[(handle, physical, 128)],
        )
        payload_at = struct.unpack_from("<Q", raw, 72)[0]
        assert struct.unpack_from("<4Q", raw, payload_at) == (
            physical,
            0x1234,
            (1 << 64) - 1,
            0,
        )


def test_cache_separates_mutants():
    machine = machine_of("multimesh_v9t6")
    normal = kernel.compile(SOURCE, "k", machine, level="l1")
    with rs.mutant("l0.bind/clobber"):
        other = kernel.compile(SOURCE, "k", machine, level="l1")
    assert other is not normal
    assert kernel.compile(SOURCE, "k", machine, level="l1") is normal
