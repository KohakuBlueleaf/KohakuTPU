"""Column-loop folds retain f32 accumulators, with no nested vector loop."""

import json
import re

import pytest

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels import vector as V
from kohakutpu.application.kernels.classes.policy import ShapeClass
from kohakutpu.application.runtime.offline import OfflineCard
from kohakutpu.language import dsl as kt
from kohakutpu.stack import kernel as K
from kohakutpu.stack import rs


@pytest.mark.parametrize("rows", [1, 17, 33, 65])
@pytest.mark.parametrize("policy", ["all", "min"])
@pytest.mark.parametrize("op", ["sum", "max", "prod"])
def test_wide_fold_repeats_row_bands(rows, policy, op):
    machine = OfflineCard("multimesh_v9t6").machine
    expr = X.Reduce(op, X.op("add", X.Load(0), X.Load(1)))
    kernel, given, _ = V.rows_kernel(
        expr,
        (V.Operand("full", pitch=416), V.Operand("col")),
        rows,
        416,
        V.Operand("row"),
        2,
        shape_class=ShapeClass(32, 256, cores=policy),
    )
    graph = kernel.trace(given, machine=K.dsl_machine(machine)).graph
    result = rs._rs.compile(
        json.dumps(graph), rs.machine_json(machine), cache_dir="", fetch=True
    )
    assert not result["verdict"]["refused"], result["verdict"]
    assert "repeats=" in result["l1"]
    assert "advances=" in result["l1"]
    assert result["l1"].count("loop s15") == kernel.class_info["cores"]


@pytest.mark.parametrize("op", ["rsum", "rmax"])
@pytest.mark.parametrize("blocks", [1, 2, 13, 74])
def test_carried_column_statistic_compiles(op, blocks):
    machine = OfflineCard("multimesh_v9t6").machine

    def body(x, y):
        for _ in kt.par(1, on=kt.vc, classed=True):
            for b in kt.range(blocks):
                value = kt.unpack(kt.load(x[b * 16 : b * 16 + 16, :], gt4=True))
                stat = kt.reduce(op, value, carry=True)
                kt.store(y[:16], stat)

    kernel = kt.Kernel.build(
        "fold", body, {"x": kt.In(blocks * 16, 32), "y": kt.Out(16)}
    )
    given = {
        "x": kt.TensorSpec.make((blocks * 16, 32), "f16"),
        "y": kt.TensorSpec.make((16,), "f16"),
    }
    graph = kernel.trace(given, machine=K.dsl_machine(machine)).graph
    result = rs._rs.compile(
        json.dumps(graph), rs.machine_json(machine), cache_dir="", fetch=True
    )
    assert not result["verdict"]["refused"], result["verdict"]
    text = result["l1"]
    assert text.count("loop s15") == 1
    before, inside = text.split("loop s15", 1)
    if op == "rsum":
        assert len(re.findall(r"vmov v\d+, k0", before)) == 4
    else:
        assert "-inf" in before
    assert re.search(r"ainc a1 \+= 0", text)
    assert re.search(r"v(?:add|max) (v\d+), \1, v\d+", inside)
    broken = rs._rs.compile(
        json.dumps(graph),
        rs.machine_json(machine),
        cache_dir="",
        fetch=True,
        mutant="l2.class/carry-init",
    )
    if not broken["verdict"]["refused"]:
        before = broken["l1"].split("loop s15", 1)[0]
        assert (
            not re.search(r"vmov v\d+, k0", before)
            if op == "rsum"
            else "-inf" not in before
        )
