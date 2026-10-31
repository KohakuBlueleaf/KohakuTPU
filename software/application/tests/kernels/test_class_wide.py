"""Wide rows use a fixed block-major stream, including broadcast operands."""

from types import SimpleNamespace

import pytest

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels import vector as V
from kohakutpu.application.kernels.classes.policy import ShapeClass
from kohakutpu.application.runtime import generated as G
from kohakutpu.application.runtime.offline import OfflineCard
from kohakutpu.stack import rs


@pytest.mark.parametrize("kind", ["full", "col", "row", "scalar"])
@pytest.mark.parametrize("core_policy", ["all", "min"])
@pytest.mark.parametrize("rows,width", [(1, 288), (3, 416), (17, 1344), (32, 2368)])
def test_wide_broadcasts_compile(kind, core_policy, rows, width):
    machine = OfflineCard("multimesh_v9t6").machine
    e = X.op("add", X.Load(0), X.Load(1))
    operands = (
        V.Operand("full", pitch=width),
        V.Operand(kind, pitch=width if kind == "full" else 0),
    )
    source, info = G.classed(
        SimpleNamespace(machine=machine),
        "vector.rows_kernel",
        G.rows_arguments(e, operands, rows, width, V.Operand("full", pitch=width)),
        ShapeClass(32, 256, cores=core_policy),
    )
    result = rs._rs.compile(source, rs.machine_json(machine), cache_dir="", fetch=True)
    assert not result["verdict"]["refused"], result["verdict"]
    assert info["blocks"] == width // 32
    assert result["packages"] and result["bound"] is None
    assert result["l1"].count("loop s15") == info["cores"]
    assert "linear_move" not in result["l1"]
    assert "    copy " in result["l1"]
    assert all(f["clause"] != "L1.VER.017" for f in result["verdict"]["findings"])
    assert "wait moves\n" not in result["l1"]
    broken = rs._rs.compile(
        source,
        rs.machine_json(machine),
        cache_dir="",
        fetch=True,
        mutant="l0.program/advance-replace",
    )
    broken_package = rs._rs.package_read(
        broken["packages"][0]["bytes"], rs.machine_json(machine)
    )
    assert not any(s[0] == 18 and s[4] & (1 << 26) for s in broken_package["steps"])
