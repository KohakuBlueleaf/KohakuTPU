"""Serialized graph-library packages preserve neighboring live data at page seams."""

import json
import os
from pathlib import Path

import pytest

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels import matmul as MM
from kohakutpu.application.kernels import relayout as RL
from kohakutpu.application.kernels.vector import Operand
from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime import packages as B
from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.language import machine as MF
from kohakutpu.stack import kernel as K
from kohakutpu.stack import rs


def test_cluster_scratch_burst_alignment(tmp_path):
    machine = machine_of("multimesh_v9t6", nodes=1, packing_backend="vector_mx7")
    hw = RL.hw_of(MF.load("multimesh_v9t6"), 2)
    plan = RL.plan(84, ((84, 1),), 0, ((84, 1),), 0, 96, False, hw)
    mm = MM.Mm(
        bt=1, m=8, n=96, k=120, mp=8, kp=128, tm=8, tn=32, nk=2, shared=True, quantum=4
    )
    kernel, given = MM.mm_kernel(
        mm,
        X.Op("add", (X.Load(0), X.Load(1))),
        (Operand("col"),),
        Operand("full", pitch=96, rows=8),
        2,
        2,
        made={1: (plan,)},
        hw=hw,
    )
    graph = kernel.trace(given, machine=K.dsl_machine(machine)).graph_json
    result = rs._rs.library_prebuild(str(tmp_path), graph, rs.machine_json(machine))
    key = json.loads(result["key"])
    c = json.loads((tmp_path / key["machine"] / (key["class"] + ".json")).read_text())[
        "compiled"
    ]
    burst = MF.load("multimesh_v9t6").mg.wburst * hw.word
    assert dict(c["scratch"][0]["members"])["h4"] % burst == 0
    assert not c["verdict"]["refused"]


def test_prepared_epilogue_bound_arena_page_seam():
    if not os.environ.get("KTPU_PIPE_ARENA_ARTIFACTS"):
        pytest.skip(
            "opt-in serialized-library replay; export_arena_witness.py builds the artifact"
        )
    artifacts = Path(os.environ["KTPU_PIPE_ARENA_ARTIFACTS"])
    entry = json.loads(Path(os.environ["KTPU_PIPE_ARENA_LIBRARY"]).read_text())
    captured = json.loads((artifacts / "site49.json").read_text())["0"]
    c = entry["compiled"]
    package = c["packages"]["0"]
    board = os.environ["KOHAKUTPU_BOARD"]
    dev = D.Device(
        kind="sim", board=board, build=Path(os.environ["KOHAKUTPU_SIM_BUILD"]), nodes=1
    )
    dev.machine = machine_of(board, nodes=1, packing_backend="vector_mx7")
    translations = list(map(tuple, captured["translations"]))
    translations[0] = (*translations[0][:2], c["scratch"][0]["bytes"])
    try:
        # Reserve physical replay memory against package spill allocations.
        dev.alloc(16 * 1024 * 1024)
        start, size = 0x300000, 0x20000
        sentinel = bytes([0xD3]) * size
        dev.card.write(start, sentinel)
        for (_, address, _), raw in zip(translations[1:], captured["data"][1:]):
            dev.card.write(address, bytes.fromhex(raw))
        prog = B.Prebuilt(
            bytes.fromhex(package["bytes"]),
            tuple(captured["bindings"]),
            rs.machine_json(dev.machine),
            0,
            (),
            False,
            rs.fetch(dev.machine),
            translations=tuple(translations),
            images_json=json.dumps(package["images"]),
        )
        dev.card.run({0: prog})
        after = dev.card.get(start, size)
        lo = translations[0][1] - start
        hi = lo + c["scratch"][0]["bytes"]
        assert after[:lo] == sentinel[:lo]
        assert after[hi:] == sentinel[hi:]
    finally:
        dev.close()
