"""All-zero MXFP7 blocks in K pairs (NUM.ACC.008, RUL.099): the operands, the
independent packer and the generated quant / product runs the zero-block model
tests and `scripts/py/measure/zeroblk/run.py` share."""

import json

import numpy as np
from tinygrad.nn.datasets import mnist

from kohakutpu.application.kernels import matmul as MM
from kohakutpu.application.kernels.vector import Operand
from kohakutpu.application.runtime import generated
from kohakutpu.language.numerics import mxfp7
from kohakutpu.stack import kernel as K
from kohakutpu.stack import rs

# ----------------------------------------------------------------- operands


def mnist8():
    return mnist()[0][:8].numpy().astype(np.float32) / 255.0


def im2col(x, k, pad):
    bt, c, h, w = x.shape
    xp = np.pad(x, ((0, 0), (0, 0), (pad, pad), (pad, pad)))
    oh, ow = h + 2 * pad - k + 1, w + 2 * pad - k + 1
    cols = np.empty((bt, oh, ow, c, k, k), np.float32)
    for i in range(k):
        for j in range(k):
            cols[:, :, :, :, i, j] = xp[:, :, i : i + oh, j : j + ow].transpose(
                0, 2, 3, 1
            )
    return cols.reshape(bt * oh * ow, c * k * k), oh, ow


def col2im(cols, shape, k, pad):
    bt, c, h, w = shape
    oh, ow = h + 2 * pad - k + 1, w + 2 * pad - k + 1
    cols = cols.reshape(bt, oh, ow, c, k, k)
    xp = np.zeros((bt, c, h + 2 * pad, w + 2 * pad), np.float32)
    for i in range(k):
        for j in range(k):
            xp[:, :, i : i + oh, j : j + ow] += cols[:, :, :, :, i, j].transpose(
                0, 3, 1, 2
            )
    return xp[:, :, pad : pad + h, pad : pad + w]


def pool(x):
    bt, c, h, w = x.shape
    return x.reshape(bt, c, h // 2, 2, w // 2, 2).mean((3, 5))


def unpool(d):
    return np.repeat(np.repeat(d, 2, 2), 2, 3) / 4


def lenet_relu_step(x, seed=0):
    """One LeNet b8 training step with ReLU (conv 6/16, fc 120/84/10): the
    contractions whose padded K is whole pairs of blocks, as (A, B)."""
    rng = np.random.default_rng(seed)

    def uni(shape, fan):
        return rng.uniform(-1, 1, shape).astype(np.float32) / np.sqrt(fan)

    w1, b1 = uni((6, 25), 25), uni(6, 25)
    w2, b2 = uni((16, 150), 150), uni(16, 150)
    f1, c1 = uni((120, 400), 400), uni(120, 400)
    f2, c2 = uni((84, 120), 120), uni(84, 120)
    f3, c3 = uni((10, 84), 84), uni(10, 84)
    labels = rng.integers(0, 10, 8)
    x = x.reshape(8, 1, 28, 28)
    cols1, _, _ = im2col(x, 5, 2)
    z1 = (cols1 @ w1.T + b1).reshape(8, 28, 28, 6).transpose(0, 3, 1, 2)
    p1 = pool(np.maximum(z1, 0))
    cols2, _, _ = im2col(p1, 5, 0)
    z2 = (cols2 @ w2.T + b2).reshape(8, 10, 10, 16).transpose(0, 3, 1, 2)
    h0 = pool(np.maximum(z2, 0)).reshape(8, 400)
    z3 = h0 @ f1.T + c1
    h1 = np.maximum(z3, 0)
    z4 = h1 @ f2.T + c2
    h2 = np.maximum(z4, 0)
    logits = h2 @ f3.T + c3
    p = np.exp(logits - logits.max(1, keepdims=True))
    p /= p.sum(1, keepdims=True)
    p[np.arange(8), labels] -= 1
    dl = p / 8
    dz4 = (dl @ f3) * (z4 > 0)
    dz3 = (dz4 @ f2) * (z3 > 0)
    dh0 = dz3 @ f1
    dz2 = unpool(dh0.reshape(8, 16, 5, 5)) * (z2 > 0)
    dcols2 = dz2.transpose(0, 2, 3, 1).reshape(800, 16) @ w2
    dp1 = col2im(dcols2, (8, 6, 14, 14), 5, 0)
    dz1 = unpool(dp1) * (z1 > 0)
    dz1f = dz1.transpose(0, 2, 3, 1).reshape(8 * 784, 6)
    return {
        "lenet_fc2_fwd": (h1, f2),
        "lenet_fc1_dx": (dz3, f1.T),
        "lenet_conv1_dw": (cols1.T, dz1f.T),
    }


def causal_av(t=256, d=64, seed=1):
    """Causal attention's P @ V: P's rows past the diagonal are zero."""
    rng = np.random.default_rng(seed)
    q, k = rng.standard_normal((2, t, d)).astype(np.float32)
    s = q @ k.T / np.sqrt(d)
    s[np.triu_indices(t, 1)] = -np.inf
    p = np.exp(s - s.max(1, keepdims=True))
    p /= p.sum(1, keepdims=True)
    v = rng.standard_normal((t, d)).astype(np.float32)
    return p, v.T


def minimal(seed=2):
    """A [8, 64] whose rows 0..3 hold one zero block beside small live values."""
    rng = np.random.default_rng(seed)
    a = rng.standard_normal((8, 64)).astype(np.float32) * 2.0**-6
    a[:, :32] = 0
    a[4:, :32] = rng.standard_normal((4, 32)) * 2.0**-6
    b = rng.standard_normal((32, 64)).astype(np.float32) * 0.5
    return a, b


def vit_tiny(seed=3, t=197, w=192, heads=3):
    """One ViT-tiny block forward (LN, MHA, GELU MLP) as (A, B) pairs."""
    rng = np.random.default_rng(seed)

    def ln(x):
        return (x - x.mean(1, keepdims=True)) / np.sqrt(x.var(1, keepdims=True) + 1e-6)

    def gelu(x):
        return 0.5 * x * (1 + np.tanh(0.7978845608 * (x + 0.044715 * x**3)))

    x = rng.standard_normal((t, w)).astype(np.float32)
    wqkv, wo = rng.standard_normal((2, 3 * w, w)).astype(np.float32) * 0.02
    w1 = rng.standard_normal((4 * w, w)).astype(np.float32) * 0.02
    w2 = rng.standard_normal((w, 4 * w)).astype(np.float32) * 0.02
    out = {}
    xa = ln(x)
    out["vit_qkv"] = (xa, wqkv)
    qkv = xa @ wqkv.T
    dh = w // heads
    o = np.zeros((t, w), np.float32)
    for h in range(heads):
        q = qkv[:, h * dh : (h + 1) * dh]
        k = qkv[:, w + h * dh : w + (h + 1) * dh]
        v = qkv[:, 2 * w + h * dh : 2 * w + (h + 1) * dh]
        s = q @ k.T / np.sqrt(dh)
        p = np.exp(s - s.max(1, keepdims=True))
        p /= p.sum(1, keepdims=True)
        out[f"vit_qk{h}"] = (q, k)
        out[f"vit_av{h}"] = (p, v.T)
        o[:, h * dh : (h + 1) * dh] = p @ v
    out["vit_proj"] = (o, wo[:w])
    x = x + o @ wo[:w].T
    xb = ln(x)
    out["vit_fc1"] = (xb, w1)
    g = gelu(xb @ w1.T)
    out["vit_fc2"] = (g, w2)
    return out


def padded(x, rows, cols):
    out = np.zeros((rows, cols), np.float16)
    out[: x.shape[0], : x.shape[1]] = x
    return out


def operands(a, b, machine_json, pairing):
    """The plan of A [M, K] x B [N, K] on the machine under `pairing`, and
    A, B as f16 padded to its mp, whole-32 N and kp."""
    a, b = a.astype(np.float16), b.astype(np.float16)
    m, k = a.shape
    n = -(-b.shape[0] // 32) * 32
    mm = MM.plan(1, m, n, k, pairing=pairing, machine_json=machine_json)
    return mm, padded(a, mm.mp, mm.kp), padded(b, n, mm.kp)


# ------------------------------------------------------------------ packing


def reference(x, side, groups, nk):
    """NUM.MX7.002/.003 by the Python quantiser, NUM.MX7.005 slot placement:
    the packing of `x` as it is (callers pass `mxfp7.lift(x, nk)`, NUM.MX7.008)."""
    q, es, m8 = mxfp7.quantise_fp16(x)
    rows, cols = x.shape
    field = ((es + mxfp7.SBIAS) << 3) | (m8 - 8)
    out = bytearray()
    for tile in range(rows // (4 * groups)):
        for chunk in range(cols // (32 * nk)):
            for group in range(groups):
                for block in range(nk):
                    r = tile * 4 * groups + group * 4
                    k = chunk * 32 * nk + block * 32
                    scales = sum(
                        int(field[r + lane, k // 32]) << (24 - 8 * lane)
                        for lane in range(4)
                    )
                    for word in range(4):
                        bits = scales
                        for lane in range(4):
                            for kk in range(8):
                                slot = lane * 8 + kk if side == "a" else kk * 4 + lane
                                bits |= (int(q[r + lane, k + word * 8 + kk]) & 127) << (
                                    249 - 7 * slot
                                )
                        out.extend(bits.to_bytes(32, "little"))
    return bytes(out)


def layout(side, groups, nk):
    return f"g{'m' if side == 'a' else 'n'}={groups}, nk={nk}"


def host(x, side, groups, nk, machine_json, mutant=None):
    """ktpu-l1 `tensor::pack` through the buffer binding."""
    rows, cols = x.shape
    ty = f"mx7{side}[{rows}, {cols}] @dram({layout(side, groups, nk)})"
    return rs._rs.buffer_pack(ty, x.tobytes(), machine_json, mutant)


# --------------------------------------------------------------- generated


def port_nk(source, port):
    """The `nk` of a generated graph's port (a layout value is `{"int": n}`)."""
    p = next(p for p in json.loads(source)["ports"] if p["name"] == port)
    return dict(p["spec"]["layout"])["nk"]["int"]


def compiled(dev, source, mutant=None):
    mt = rs.machine_json(dev.machine)
    output = rs._rs.compile(source, mt, cache_dir="", fetch=True, mutant=mutant)
    assert not output["verdict"]["refused"], output["verdict"]
    return generated.from_output(dev, json.loads(source), output), output


def mm_source(mm, pairing, machine_json, mutant=None):
    """The generated product kernel of `mm` (`matmul.mm_kernel`, its plan
    made by `matmul.plan` under `pairing`)."""
    call = dict(
        mm=dict(
            made_by=dict(
                gen="matmul.plan",
                args=dict(
                    bt=mm.bt,
                    m=mm.m,
                    n=mm.n,
                    k=mm.k,
                    shared=mm.shared,
                    quantum=mm.quantum,
                    pairing=pairing,
                ),
            )
        ),
        e={"nodes": [{"load": 0}], "root": 0},
        extras=[],
        out=vars(Operand("full", pitch=mm.n, rows=mm.mp)),
    )
    return rs._rs.generate("matmul.mm_kernel", json.dumps(call), machine_json, mutant)


def quant(dev, x, side, groups, nk, mutant=None, padded_to=None):
    """`x` quantised by the generated quant kernel on `dev.machine` into
    [rp, cp] (`padded_to`, else x's shape): the packed bytes, its cycles and
    its L1 text."""
    rows, cols = x.shape
    rp, cp = padded_to or x.shape
    call = dict(
        src=dict(kind="full", dtype="f16", pitch=cols, offset=0, rows=0),
        rows=rows,
        cols=cols,
        rp=rp,
        cp=cp,
        side=side,
        g=groups,
        nk=nk,
        batches=1,
        finite=True,
    )
    source = rs._rs.generate(
        "matmul.quant_kernel", json.dumps(call), rs.machine_json(dev.machine), None
    )
    comp, output = compiled(dev, source, mutant)
    xa, qa = dev.alloc(x.nbytes), dev.alloc(rp * cp)
    try:
        dev.write(xa, x.tobytes())
        run = dev.run(comp, {"x": xa, "q": qa}, kind="quant")
        return dev.read(qa, rp * cp), run.cycles, output["l1"]
    finally:
        dev.free(xa)
        dev.free(qa)


def fused_text(rows, cols, side, groups, nk):
    """An L2 vector-core `pack` (NUM.MX7.006) of `x` into `q`, a core per tile
    of `4 * groups` rows, a group of four rows a trip."""
    tile = 4 * groups
    return (
        "target ktpu.v9\n\n"
        f"fn zq.l2(x: f16[{rows}, {cols}] @dram, q: mx7{side}[{rows}, {cols}] "
        f"@dram({layout(side, groups, nk)}))\n"
        f"    par c in 0..{rows // tile} on vc[any]\n"
        f"        for t in 0..{groups}\n"
        f"            xl = load x[c*{tile} + t*4 : +4, 0 : +{cols}] : f16[4, {cols}] @l1(gt4)\n"
        f"            v = unpack xl : f32[4, {cols}] @reg(gt4)\n"
        f"            p = pack v : mx7{side}[4, {cols}] @l1\n"
        f"            store q[c*{tile} + t*4 : +4, 0 : +{cols}] <- p\n"
    )


def fused(dev, x, side, groups, nk, mutant=None):
    """`x` packed by `fused_text`'s kernel: the packed bytes and its cycles."""
    text = fused_text(*x.shape, side, groups, nk)
    with rs.mutant(mutant):
        c = K.compile(text, machine=dev.machine)
    xa, qa = dev.alloc(x.nbytes), dev.alloc(x.size)
    try:
        dev.write(xa, x.tobytes())
        run = dev.run(c, {"x": xa, "q": qa})
        return dev.read(qa, x.size), run.cycles
    finally:
        dev.free(xa)
        dev.free(qa)


def product(dev, mm, source, abytes, bbytes):
    """The generated product of packed A and B: f16 [mp, n] and its cycles."""
    comp, _ = compiled(dev, source)
    binds = {"a": dev.alloc(len(abytes)), "b": dev.alloc(len(bbytes))}
    binds["out"] = dev.alloc(mm.mp * mm.n * 2)
    try:
        dev.write(binds["a"], abytes)
        dev.write(binds["b"], bbytes)
        dev.write(binds["out"], bytes([0xD3]) * (mm.mp * mm.n * 2))
        run = dev.run(comp, binds, kind="matmul")
        raw = dev.read(binds["out"], mm.mp * mm.n * 2)
    finally:
        for at in binds.values():
            dev.free(at)
    return np.frombuffer(raw, "<f2").reshape(mm.mp, mm.n), run.cycles


def error(got, a, b, rows, cols):
    """`got`'s error against the exact product of the quantised operands:
    max and mean absolute, and max relative to the product's peak."""
    exact = (mxfp7.value_fp16(a) @ mxfp7.value_fp16(b).T)[:rows, :cols]
    err = np.abs(got[:rows, :cols].astype(np.float64) - exact)
    peak = np.abs(exact).max()
    return dict(
        max_abs=float(err.max()),
        mean_abs=float(err.mean()),
        max_rel_of_peak=float(err.max() / peak),
        peak=float(peak),
    )
