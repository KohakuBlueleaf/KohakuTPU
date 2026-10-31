"""A package on a chosen home runs on the model, bit-exact, from that home."""

import numpy as np
import pytest

from kohakuaccel.driver.transport.simdram import DramMap
from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels import vector as V
from kohakutpu.application.kernels.vector import Operand
from kohakutpu.application.runtime import forms as FM


@pytest.mark.parametrize("home, W", [(0, 32), (1, 64), (3, 96)])
def test_a_home_runs_its_package(dev, home, W, monkeypatch):
    spec = dev.card.board
    memory = dev.memory
    memory.stripes = spec["dram_interleave_kb"] << 10, spec["dram_homes"]
    memory.package_homes = {node: home for node in dev.card.nodes}
    a = np.arange(W, dtype=np.float16)
    at, out = dev.alloc(a.nbytes), dev.alloc(a.nbytes)
    dev.write(at, a.tobytes())
    placed = []
    alloc = memory.alloc

    def spy(nbytes, owner="runtime", near=None):
        got = alloc(nbytes, owner, near)
        if owner == "package":
            placed.append(got)
        return got

    monkeypatch.setattr(memory, "alloc", spy)
    try:
        operand = Operand("full", pitch=W, rows=1)
        e = X.op("add", X.Load(0), 1.0)
        k, given, _ = V.rows_kernel(
            e, (operand,), 1, W, Operand("full", pitch=W), dev.machine.count("VC")
        )
        FM.launch(dev, k, given, {"in0": at, "out": out})
        dev.drain()
        got = dev.read(out, a.nbytes)
    finally:
        memory.package_homes = {}
        for span in (at, out):
            dev.free(span)
    assert got == (a + 1).tobytes()
    assert placed
    where = DramMap.of_board(spec)
    assert {where.locate(package)[0] for package in placed} == {home}
