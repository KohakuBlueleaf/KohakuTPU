"""The contraction kernel knobs on the card model: each knob's kernel computes
the bytes the kernel without it computes, for host-packed operands."""

import json

import numpy as np
import pytest

from application.tests.runtime import zero_block as Z
from kohakutpu.application.kernels import matmul as MM
from kohakutpu.application.kernels.vector import Operand
from kohakutpu.stack import rs


def product(dev, args, a, b, rows, n):
    """The generated `matmul.mm_kernel` of `args` over packed `a`, `b`: its
    f16 [rows, n] output bytes."""
    source = rs._rs.generate(
        "matmul.mm_kernel", json.dumps(args), rs.machine_json(dev.machine), None
    )
    comp, _ = Z.compiled(dev, source)
    binds = {"a": dev.alloc(len(a)), "b": dev.alloc(len(b))}
    binds["out"] = dev.alloc(rows * n * 2)
    try:
        dev.write(binds["a"], a)
        dev.write(binds["b"], b)
        dev.write(binds["out"], bytes([0xD3]) * (rows * n * 2))
        dev.run(comp, binds, kind="matmul")
        return dev.read(binds["out"], rows * n * 2)
    finally:
        for at in binds.values():
            dev.free(at)


@pytest.mark.parametrize("bt,m,n,k", [(2, 8, 32, 64), (3, 20, 64, 96)])
def test_batch_tiles_computes_the_batched_product(dev, bt, m, n, k):
    mt = rs.machine_json(dev.machine)
    mm = MM.plan(bt, m, n, k, False, machine_json=mt)
    rng = np.random.default_rng(bt + m)
    a = Z.padded(rng.standard_normal((bt * mm.mp, k)), bt * mm.mp, mm.kp)
    b = Z.padded(rng.standard_normal((bt * n, k)), bt * n, mm.kp)
    qa = Z.host(a, "a", mm.tm // 4, mm.nk, mt)
    qb = Z.host(b, "b", mm.tn // 4, mm.nk, mt)
    plan = dict(bt=bt, m=m, n=n, k=k, shared=False, **MM.pairing_arg())
    args = dict(
        mm=dict(made_by=dict(gen="matmul.plan", args=plan)),
        e={"nodes": [{"load": 0}], "root": 0},
        extras=[],
        out=vars(Operand("full", pitch=n, rows=bt * mm.mp)),
    )
    rows = bt * mm.mp
    off = product(dev, {**args, "batch_tiles": False}, qa, qb, rows, n)
    on = product(dev, {**args, "batch_tiles": True}, qa, qb, rows, n)
    assert on == off
    got = np.frombuffer(off, "<f2").reshape(bt, mm.mp, n)
    for i in range(bt):
        ai, bi = a[i * mm.mp : (i + 1) * mm.mp], b[i * n : (i + 1) * n]
        assert Z.error(got[i], ai, bi, m, n)["max_rel_of_peak"] <= 1e-2
