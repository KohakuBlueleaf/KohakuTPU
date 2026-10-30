"""The contraction kernel knobs on the card model: each knob's kernel computes
the bytes the kernel without it computes, for host-packed operands."""

import json

import numpy as np
import pytest

from application.tests.runtime import zero_block as Z
from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels import matmul as MM
from kohakutpu.application.kernels.vector import Operand
from kohakutpu.application.plan.emit import expr as table
from kohakutpu.stack import rs


def source(dev, args):
    return rs._rs.generate(
        "matmul.mm_kernel", json.dumps(args), rs.machine_json(dev.machine), None
    )


def port_bytes(src):
    """Each port's bytes in the generated graph `src`."""
    item = {"f16": 2, "mx7a": 1, "mx7b": 1}
    return {
        p["name"]: int(np.prod(p["spec"]["shape"])) * item[p["spec"]["dtype"]]
        for p in json.loads(src)["ports"]
    }


def product(dev, args, data, rows, n):
    """The generated `matmul.mm_kernel` of `args` over the bytes `data` by
    port (each port bound to their first bytes, zero-extended to its
    extent): its f16 output's first [rows, n] bytes."""
    src = source(dev, args)
    comp, _ = Z.compiled(dev, src)
    sizes = port_bytes(src)
    binds = {name: dev.alloc(size) for name, size in sizes.items()}
    try:
        for name, at in binds.items():
            fill = bytes([0xD3]) if name == "out" else bytes(1)
            got = data.get(name, b"")[: sizes[name]]
            dev.write(at, got + fill * (sizes[name] - len(got)))
        dev.run(comp, binds, kind="matmul")
        return dev.read(binds["out"], rows * n * 2)
    finally:
        for at in binds.values():
            dev.free(at)


def call(mm, e, extras, out, **knobs):
    plan = dict(
        bt=mm.bt,
        m=mm.m,
        n=mm.n,
        k=mm.k,
        shared=mm.shared,
        quantum=mm.quantum,
        **MM.pairing_arg(),
        **MM.plan_knobs(),
    )
    return dict(
        mm=dict(made_by=dict(gen="matmul.plan", args=plan)),
        e=table(e),
        extras=[vars(o) for o in extras],
        out=vars(out),
        **knobs,
    )


def operands(dev, mm, seed):
    """Host-packed A and B of `mm` and their f16 values."""
    mt = rs.machine_json(dev.machine)
    rng = np.random.default_rng(seed)
    a = Z.padded(rng.standard_normal((mm.bt * mm.mp, mm.k)), mm.bt * mm.mp, mm.kp)
    nb = mm.n if mm.shared else mm.bt * mm.n
    b = Z.padded(rng.standard_normal((nb, mm.k)), nb, mm.kp)
    qa = Z.host(a, "a", mm.tm // 4, mm.nk, mt)
    qb = Z.host(b, "b", mm.tn // 4, mm.nk, mt)
    return a, b, {"a": qa, "b": qb}


@pytest.mark.parametrize("bt,m,n,k", [(2, 8, 32, 64), (3, 20, 64, 96)])
def test_batch_tiles_computes_the_batched_product(dev, bt, m, n, k):
    mm = MM.plan(bt, m, n, k, False, machine_json=rs.machine_json(dev.machine))
    a, b, data = operands(dev, mm, bt + m)
    load = X.Load(0)
    out = Operand("full", pitch=n, rows=bt * mm.mp)
    rows = bt * mm.mp
    off = product(dev, call(mm, load, [], out, batch_tiles=False), data, rows, n)
    on = product(dev, call(mm, load, [], out, batch_tiles=True), data, rows, n)
    assert on == off
    got = np.frombuffer(off, "<f2").reshape(bt, mm.mp, n)
    for i in range(bt):
        ai, bi = a[i * mm.mp : (i + 1) * mm.mp], b[i * n : (i + 1) * n]
        assert Z.error(got[i], ai, bi, m, n)["max_rel_of_peak"] <= 1e-2


#: Each epilogue form: (expression, extras, quantum).
EPILOGUES = {
    "relu": (X.op("max", X.Load(0), 0.0), [], 4),
    "full": (X.op("add", X.Load(0), X.Load(1)), [Operand("full", pitch=64)], 4),
    "col": (X.op("add", X.Load(0), X.Load(1)), [Operand("col", offset=32)], 4),
    "stat": (X.op("mul", X.Load(0), X.Load(1)), [Operand("row")], 16),
    "scalar": (X.op("add", X.Load(0), X.Load(1)), [Operand("scalar", offset=3)], 4),
    "sum": (X.Reduce("sum", X.Load(0)), [], 4),
}

#: The ring's knob sets.
RINGS = [
    dict(ring_depth=1),
    dict(ring_depth=2, split_vc=True),
    dict(ring_depth=2, tail_bands=True),
]


@pytest.mark.parametrize("name", list(EPILOGUES))
def test_the_ring_computes_the_product_without_it(dev, name):
    """bt = 2 batches of M = 20 (pad_m: a padded tail tile) by N = 64: each
    ring's output rows below M hold the bytes the DRAM product's do."""
    e, extras, quantum = EPILOGUES[name]
    mt = rs.machine_json(dev.machine)
    bt, m, n = 2, 20, 64
    mm = MM.plan(bt, m, n, 64, False, quantum, machine_json=mt, pad_m=True)
    _, _, data = operands(dev, mm, 7)
    rng = np.random.default_rng(11)
    for i in range(len(extras)):
        data[f"in{i + 1}"] = rng.standard_normal(1 << 14).astype(np.float16).tobytes()
    out = Operand("full", pitch=n, rows=bt * mm.mp)
    rows = bt * mm.mp
    base = call(mm, e, extras, out)
    assert MM.resolved(base, mt)["plain"]
    want = np.frombuffer(product(dev, base, data, rows, n), "<f2").reshape(bt, mm.mp, n)
    for knobs in RINGS:
        args = call(mm, e, extras, out, **knobs)
        resolved = MM.resolved(args, mt)
        assert not resolved["plain"]
        assert resolved["knobs"]["ring_depth"] == knobs["ring_depth"]
        got = np.frombuffer(product(dev, args, data, rows, n), "<f2")
        got = got.reshape(bt, mm.mp, n)
        assert got[:, :m].tobytes() == want[:, :m].tobytes(), (name, knobs)


#: An f16 A's knob sets: quantised first, and each quant ring.
F16 = {
    "first": dict(),
    "ring1": dict(ring_depth=2, quant_ring=1),
    "ring2": dict(ring_depth=2, quant_ring=2),
}


@pytest.mark.parametrize("name", list(F16))
def test_an_f16_a_computes_the_packed_product(dev, monkeypatch, name):
    """bt = 2 batches of M = 20 (a padded tail tile) over K = 112 (a padded
    K tail), K pairs quantised on the vector cores: the kernel quantising its
    f16 A computes the bytes the kernel of A quantised by the quant kernel
    computes."""
    monkeypatch.setenv("KTPU_MX7_PAIRING", "vector")
    knobs = F16[name]
    mt = rs.machine_json(dev.machine)
    bt, m, n, k = 2, 20, 64, 112
    mm = MM.plan(bt, m, n, k, False, machine_json=mt, pad_m=True)
    on = {**MM.kernel_knobs(), "activation_fp16": True}
    assert MM.takes_f16(mm.k, mm.nk, on, mt) and mm.kp > k, mm
    rng = np.random.default_rng(5)
    a = rng.standard_normal((bt, m, k)).astype(np.float16)
    padded = np.zeros((bt, mm.mp, mm.kp), np.float16)
    padded[:, :m, :k] = a
    qa, _, _ = Z.quant(dev, padded.reshape(bt * mm.mp, mm.kp), "a", mm.tm // 4, mm.nk)
    _, _, data = operands(dev, mm, 9)
    out = Operand("full", pitch=n, rows=bt * mm.mp)
    rows = bt * mm.mp
    want = product(dev, call(mm, X.Load(0), [], out), {**data, "a": qa}, rows, n)
    args = call(mm, X.Load(0), [], out, activation_fp16=True, **knobs)
    resolved = MM.resolved(args, mt)["knobs"]
    assert resolved["activation_fp16"]
    assert resolved["quant_ring"] == knobs.get("quant_ring", 0)
    got = product(dev, args, {**data, "a": a.tobytes()}, rows, n)
    got, want = (np.frombuffer(x, "<f2").reshape(bt, mm.mp, n) for x in (got, want))
    assert got[:, :m].tobytes() == want[:, :m].tobytes(), name
