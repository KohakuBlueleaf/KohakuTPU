"""Class mover and transpose compile witnesses; no value execution model."""

import json
from types import SimpleNamespace

import pytest

from kohakutpu.application.kernels.classes.policy import ShapeClass
from kohakutpu.application.kernels.vector import Operand
from kohakutpu.application.runtime import generated as G
from kohakutpu.application.runtime.offline import OfflineCard
from kohakutpu.language import dsl as kt
from kohakutpu.stack import kernel as K
from kohakutpu.stack import rs

MACHINE = OfflineCard("multimesh_v9t5").machine


def compile_source(source, mutant=None, *, allow_line_refusal=False):
    machine = MACHINE
    result = rs._rs.compile(
        source,
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


def classed(generator, arguments, shape_class):
    return G.classed(
        SimpleNamespace(machine=MACHINE), generator, arguments, shape_class
    )


@pytest.mark.parametrize("side", ["a", "b"])
@pytest.mark.parametrize("batches", [1, 3])
@pytest.mark.parametrize("rows,cols", [(1, 16), (8, 64), (17, 80)])
def test_quant_fixed_entry_loop(side, batches, rows, cols):
    rp, cp = -(-rows // 8) * 8, -(-cols // 64) * 64
    # nk=1 entries: the mover quantises them (an nk >= 2 quant is RUL.099's).
    source, _ = classed(
        "matmul.quant_kernel",
        {
            "src": vars(Operand("full", pitch=cols + 16, offset=16)),
            "rows": rows,
            "cols": cols,
            "rp": rp,
            "cp": cp,
            "side": side,
            "g": 2,
            "nk": 1,
            "batches": batches,
        },
        ShapeClass(32, 256),
    )
    package, text = compile_source(source)
    assert text.count("    quant ") == 1
    assert any(s[0] == 17 for s in package["steps"])
    assert any(s[0] == 18 and s[4] & (1 << 26) for s in package["steps"])
    broken, _ = compile_source(
        source, mutant="l0.program/advance-replace", allow_line_refusal=True
    )
    assert not any(s[0] == 18 and s[4] & (1 << 26) for s in broken["steps"])


@pytest.mark.parametrize("counts", [(1, 1), (2, 7), (3, 17000)])
def test_walk_mixed_rank_uses_bounded_line_owning_streams(counts):
    a, b = counts
    source, _ = classed(
        "convert.walk_kernel",
        {
            "src_bytes": a * b * 32,
            "levels": [[a, b * 32], [b, 32]],
            "at": 0,
            "dst_bytes": a * b * 32,
            "dlevels": [[a * b, 32]],
            "dat": 0,
        },
        ShapeClass(32, 256),
    )
    package, text = compile_source(source)
    assert 1 <= text.count("    copy ") <= a * -(-b // (1 << 14))
    assert "linear_move" not in text
    assert not any(s[0] in (16, 17) for s in package["steps"])


@pytest.mark.parametrize("policy", ["all", "min"])
@pytest.mark.parametrize("batch,a,b", [(1, 16, 16), (2, 16, 48), (1, 48, 80)])
def test_transpose_idle_and_tail(policy, batch, a, b):
    source, info = classed(
        "transpose.transpose_kernel",
        {"nb": batch, "A": a, "B": b},
        ShapeClass(32, 256, cores=policy),
    )
    package, text = compile_source(source)
    assert text.count("    gt4 ") == 2
    assert any(s[0] == 17 for s in package["steps"])
    assert info["padded_rows"] >= 0


def test_gt4_class_depth_refuses_reserved_counter_alias():
    def body(x, y):
        kt.gt4(
            y, x, walk=((1, 0),) * 9 + ((4, 32),), dwalk=((1, 0),) * 9, node_loop=True
        )

    ports = {"x": kt.In(4, 16), "y": kt.Out(4, 16)}
    given = {key: kt.TensorSpec.make((4, 16)) for key in ports}
    kernel = kt.Kernel.build("depth", body, ports)
    source = json.dumps(kernel.trace(given, machine=K.dsl_machine(MACHINE)).graph)
    with pytest.raises(ValueError, match="at most eight entry axes"):
        compile_source(source)
    compile_source(source, mutant="l2.mover/unchecked-class-depth")
