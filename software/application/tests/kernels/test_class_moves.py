"""Class mover and transpose compile witnesses; no value execution model."""

import json

import pytest

from kohakutpu.application.kernels import convert, matmul, transpose
from kohakutpu.application.kernels.classes.policy import ShapeClass
from kohakutpu.application.kernels.vector import Operand
from kohakutpu.application.runtime.offline import OfflineCard
from kohakutpu.language import dsl as kt
from kohakutpu.stack import kernel as K
from kohakutpu.stack import rs


def compile_kernel(kernel, given, mutant=None, *, allow_line_refusal=False):
    machine = OfflineCard("multimesh_v9t5").machine
    graph = kernel.trace(given, machine=K.dsl_machine(machine)).graph
    result = rs._rs.compile(
        json.dumps(graph),
        rs.machine_json(machine),
        cache_dir="",
        fetch=True,
        mutant=mutant,
    )
    assert result["bound"] is None
    if result["verdict"]["refused"]:
        assert allow_line_refusal, result["verdict"]
        assert all(
            f["clause"] == "L1.VER.017" for f in result["verdict"]["findings"]
        ), result["verdict"]
    return (
        rs._rs.package_read(result["packages"][0]["bytes"], rs.machine_json(machine)),
        result["l1"],
    )


@pytest.mark.parametrize("side", ["a", "b"])
@pytest.mark.parametrize("batches", [1, 3])
@pytest.mark.parametrize("rows,cols", [(1, 16), (8, 64), (17, 80)])
def test_quant_fixed_entry_loop(side, batches, rows, cols):
    rp, cp = -(-rows // 8) * 8, -(-cols // 64) * 64
    pair = matmul.quant_kernel(
        Operand("full", pitch=cols + 16, offset=16),
        rows,
        cols,
        rp,
        cp,
        side,
        2,
        2,
        batches,
        shape_class=ShapeClass(32, 256),
    )
    package, text = compile_kernel(*pair)
    assert text.count("    quant ") == 1
    assert any(s[0] == 17 for s in package["steps"])
    assert any(s[0] == 18 and s[4] & (1 << 26) for s in package["steps"])
    broken, _ = compile_kernel(
        *pair, mutant="l0.program/advance-replace", allow_line_refusal=True
    )
    assert not any(s[0] == 18 and s[4] & (1 << 26) for s in broken["steps"])


@pytest.mark.parametrize("counts", [(1, 1), (2, 7), (3, 17000)])
def test_walk_mixed_rank_uses_bounded_line_owning_streams(counts):
    a, b = counts
    pair = convert.walk_kernel(
        a * b * 32,
        ((a, b * 32), (b, 32)),
        0,
        a * b * 32,
        ((a * b, 32),),
        0,
        shape_class=ShapeClass(32, 256),
    )
    package, text = compile_kernel(*pair)
    assert 1 <= text.count("    copy ") <= a * -(-b // (1 << 14))
    assert "linear_move" not in text
    assert not any(s[0] in (16, 17) for s in package["steps"])


@pytest.mark.parametrize("policy", ["all", "min"])
@pytest.mark.parametrize("batch,a,b", [(1, 16, 16), (2, 16, 48), (1, 48, 80)])
def test_transpose_idle_and_tail(policy, batch, a, b):
    pair = transpose.transpose_kernel(
        batch, a, b, 2, shape_class=ShapeClass(32, 256, cores=policy)
    )
    package, text = compile_kernel(*pair)
    assert text.count("    gt4 ") == 2
    assert any(s[0] == 17 for s in package["steps"])
    assert pair[0].class_info["padded_rows"] >= 0


def test_gt4_class_depth_refuses_reserved_counter_alias():
    def body(x, y):
        kt.gt4(
            y, x, walk=((1, 0),) * 9 + ((4, 32),), dwalk=((1, 0),) * 9, node_loop=True
        )

    ports = {"x": kt.In(4, 16), "y": kt.Out(4, 16)}
    given = {key: kt.TensorSpec.make((4, 16)) for key in ports}
    kernel = kt.Kernel.build("depth", body, ports)
    with pytest.raises(ValueError, match="at most eight entry axes"):
        compile_kernel(kernel, given)
    compile_kernel(kernel, given, mutant="l2.mover/unchecked-class-depth")
