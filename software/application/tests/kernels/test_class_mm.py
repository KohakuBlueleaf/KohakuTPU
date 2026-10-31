"""Fixed node-loop matrix products: packed fills, accumulated chunks, tile drain."""

import json
import re
from types import SimpleNamespace

import pytest

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels import vector
from kohakutpu.application.kernels.classes.policy import ShapeClass
from kohakutpu.application.runtime import generated as G
from kohakutpu.application.runtime.offline import OfflineCard
from kohakutpu.language import dsl as kt
from kohakutpu.stack import kernel as K
from kohakutpu.stack import rs


@pytest.mark.parametrize("shared", [True, False])
@pytest.mark.parametrize("policy", ["all", "min"])
@pytest.mark.parametrize("kind", ["full", "col", "scalar"])
def test_product_class_epilogue(shared, policy, kind):
    machine = OfflineCard("multimesh_v9t6").machine
    extra = vector.Operand(
        kind,
        pitch=128 if kind == "full" else 0,
        offset=16 if kind != "scalar" else 7,
    )
    made = {"bt": 2, "m": 9, "n": 96, "k": 65, "shared": shared, "quantum": 4}
    source, _ = G.classed(
        SimpleNamespace(machine=machine),
        "matmul.mm_kernel",
        {
            "mm": {"made_by": {"gen": "matmul.plan", "args": made}},
            "e": G.expr(X.op("add", X.Load(0), X.Load(1))),
            "extras": [vars(extra)],
            "out": vars(vector.Operand("full", pitch=128, offset=16)),
        },
        ShapeClass(32, 256, cores=policy),
    )
    result = rs._rs.compile(source, rs.machine_json(machine), cache_dir="", fetch=True)
    assert result["bound"] is None
    assert not result["verdict"]["refused"], result["verdict"]
    assert len(re.findall(r"gemm \d+x\d+x\d+", result["l1"])) == machine.count("MG")


@pytest.mark.parametrize("shared", [True, False])
@pytest.mark.parametrize(
    "batches,rows,cols,chunks", [(1, 8, 32, 1), (1, 24, 96, 3), (2, 16, 64, 5)]
)
def test_tiled_product_has_fixed_cluster_body(shared, batches, rows, cols, chunks):
    machine = OfflineCard("multimesh_v9t6").machine
    tm, tn, nk = 8, 32, 1
    k = chunks * 32
    tile_rows = batches * rows * (cols // tn)
    pa, pb, pc = (
        {"gm": tm // 4, "nk": nk},
        {"gn": tn // 4, "nk": nk},
        {"tile": (tm // 4, tn // 4)},
    )
    ports = {
        "a": kt.In(batches * rows, k, dtype="mx7a", layout=pa),
        "b": kt.In(cols if shared else batches * cols, k, dtype="mx7b", layout=pb),
        "c": kt.Out(tile_rows, tn, dtype="f16", layout=pc),
    }
    given = {
        "a": kt.TensorSpec.make((batches * rows, k), "mx7a", pa),
        "b": kt.TensorSpec.make((cols if shared else batches * cols, k), "mx7b", pb),
        "c": kt.TensorSpec.make((tile_rows, tn), "f16", pc),
    }

    def body(a, b, c):
        kt.gemm_tiles(
            c, a, b, batches=batches, rows=rows, cols=cols, clusters=2, shared=shared
        )

    kernel = kt.Kernel.build("tile_loop", body, ports)
    graph = kernel.trace(given, machine=K.dsl_machine(machine)).graph
    result = rs._rs.compile(
        json.dumps(graph), rs.machine_json(machine), cache_dir="", fetch=True
    )
    assert result["bound"] is None
    assert not result["verdict"]["refused"], result["verdict"]
    assert result["l1"].count("gemm 2x8x1") == 2
    assert result["l1"].count("acc=r5") == 2
    if chunks > 1:
        broken = rs._rs.compile(
            json.dumps(graph),
            rs.machine_json(machine),
            cache_dir="",
            fetch=True,
            mutant="l2.cluster/no-idle-post",
        )
        assert broken["verdict"]["refused"]
        assert any(f["clause"] == "L1.VER.009" for f in broken["verdict"]["findings"])
