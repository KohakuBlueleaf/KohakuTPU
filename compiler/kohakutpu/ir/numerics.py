"""The numerics report (docs/projects/kohakutpu/ir/l3.md §4): every reference
kernel's result against two host references, both computed in float32 --

- ``fp32``: the operation as written, no quantisation;
- ``mx``: the hardware's quantisation simulated on the host (MXFP7 operands as
  `mx_quant.v` makes them, attention's P quantised before P @ v), nothing else.

-- for each SOURCE of the result: ``interp`` (the L3 reference interpreter on
``kernels/*.l3``), ``model`` (the hand-written L2 schedule compiled to L1 and
run on the unit models) and, given a card target, ``rtl`` (the same packages on
the Verilated card: `scripts/py/numerics_rtl.py`). A ``mx-ref`` row is the
mx reference against the fp32 one: the quantisation's own error, the floor any
source is judged beside; an ``rtl`` row against ``model`` counts the elements
that differ (`ndiff`).
"""

import json
import pathlib
from dataclasses import dataclass

import numpy as np
from kohakuaccel.analysis.numerics import FIELDS, errors
from kohakuaccel.ir.l2 import Schedule
from kohakuaccel.sim import MEM_BASE
from kohakutpu.hw.mxfp7 import value_fp16
from kohakutpu.ir import l2, l3
from kohakutpu.ir.l1.kernels import attention as AT
from kohakutpu.ir.l1.kernels import conv2d as CV
from kohakutpu.ir.l1.kernels import matmul as MM
from kohakutpu.ir.l1.model import L1Model
from kohakutpu.ir.l2.layouts import BandLane, ConvB, Flat, MxA, MxB, Rows, Tiles

LOG2E = float(np.log2(np.e))


def f32(a):
    return np.asarray(a, np.float32)


def mx(a):
    """MXFP7 as the hardware quantises it, along the last axis, as float32."""
    return value_fp16(a).astype(np.float32)


# --------------------------------------------------------------- references
def matmul_ref(x, w, quant: bool):
    a, b = (mx(x), mx(w)) if quant else (f32(x), f32(w))
    return a @ b.T


def silu_ref(h):
    h = f32(h)
    return h / (np.float32(1) + np.exp(-h))


def softmax_ref(x):
    x = f32(x)
    e = np.exp(x - x.max(1, keepdims=True))
    return e / e.sum(1, keepdims=True)


def layernorm_ref(x, g, b, eps=1e-5):
    x = f32(x)
    mu = x.mean(1, keepdims=True)
    var = x.var(1, keepdims=True)
    return (x - mu) / np.sqrt(var + np.float32(eps)) * f32(g) + f32(b)


def attention_ref(q, k, v, quant: bool, block: int = 64):
    """Base-2 softmax(q k^T) v; quantised: the flash form the hardware runs, its
    operands and each block's P in MXFP7, the arithmetic in float32."""
    if not quant:
        s = f32(q) @ f32(k).T
        p = np.exp2(s - s.max(1, keepdims=True))
        return (p / p.sum(1, keepdims=True)) @ f32(v)
    qq, kq, vtq = mx(q), mx(k), mx(np.ascontiguousarray(v.T))
    m = np.full((q.shape[0], 1), -np.inf, np.float32)
    lsum = np.zeros((q.shape[0], 1), np.float32)
    o = np.zeros((q.shape[0], v.shape[1]), np.float32)
    for j in range(0, k.shape[0], block):
        s = qq @ kq[j : j + block].T
        mn = np.maximum(m, s.max(1, keepdims=True))
        corr = np.exp2(m - mn)
        p = np.exp2(s - mn)
        lsum = lsum * corr + p.sum(1, keepdims=True)
        o = o * corr + mx(p) @ vtq[:, j : j + block].T
        m = mn
    return o / lsum


def conv_ref(x, wt, quant: bool):
    """3x3 same conv, `wt` (O, Cin, 3, 3); quantised along Cin per tap."""
    h, w, cin = x.shape
    taps = np.ascontiguousarray(wt.transpose(0, 2, 3, 1))
    xq, wq = (mx(x), mx(taps)) if quant else (f32(x), f32(taps))
    xp = np.zeros((h + 2, w + 2, cin), np.float32)
    xp[1:-1, 1:-1] = xq
    return sum(
        xp[dy : dy + h, dx : dx + w] @ wq[:, dy, dx, :].T
        for dy in range(3)
        for dx in range(3)
    )


# ------------------------------------------------------------------ targets
class UnitModel:
    """The unit models (`L1Model`), a fresh memory each case."""

    def __init__(self) -> None:
        self.mdl = L1Model()
        self.machine = self.mdl.machine

    def alloc(self, nbytes: int) -> int:
        return self.mdl.alloc(nbytes)

    def write(self, at: int, raw: bytes) -> None:
        self.mdl.mem.write(at - MEM_BASE, raw)

    def get(self, at: int, nbytes: int) -> bytes:
        return self.mdl.get(at, nbytes)

    def run(self, program) -> None:
        self.mdl.run(program)


class _Rig:
    """A schedule over a target's memory, compiled and run."""

    def __init__(self, target) -> None:
        self.t = target
        self.s = Schedule(machine=target.machine)
        self.mgs = sorted(target.machine.units["MG"])
        self.vcs = sorted(target.machine.units["VC"])

    def buf(self, name, layout, data=None, nbytes=None):
        n = nbytes if nbytes is not None else layout.nbytes
        b = self.s.buffer(name, n, layout, base=self.t.alloc(n))
        if data is not None:
            raw = data if isinstance(data, (bytes, bytearray)) else layout.pack(data)
            self.t.write(b.base, bytes(raw))
        return b

    def run(self) -> None:
        for prog in l2.compile(self.s):
            self.t.run(prog)

    def f16(self, b, shape):
        raw = self.t.get(b.base, int(np.prod(shape)) * 2)
        return np.frombuffer(raw, np.float16).astype(np.float64).reshape(shape)


def _model_matmul(target, x, w, bias=None, silu=False):
    m, k = x.shape
    n = w.shape[0]
    gm = gn = 16 if silu else 8
    r = _Rig(target)
    a = r.buf("a", MxA(m, k, gm, 2), x)
    b = r.buf("b", MxB(n, k, gn, 2), w)
    c = r.buf("c", Tiles(m, n, gm, gn))
    if silu:
        sinks = [r.buf(f"sink{i}", Flat(128 * 16)) for i in range(2)]
        l2.ops.matmul_silu(r.s, r.mgs, r.vcs, a, b, c, sinks, late=True, words=128)
    else:
        extra = {}
        if bias is not None:
            extra = {
                "ones": r.buf("ones", None, MM.ones_block(gm), gm * MM.ENTRY),
                "bias": r.buf("bias", None, MM.bias_block(bias, gn), n // 4 * MM.ENTRY),
            }
        l2.ops.matmul(r.s, r.mgs, a, b, c, **extra)
    r.run()
    return c.layout.unpack(r.t.get, c.base)


def _model_stream(target, kind, x, *extra):
    r = _Rig(target)
    if kind == "silu":
        xb, yb = r.buf("x", Flat(x.size), x.ravel()), r.buf("y", Flat(x.size))
        l2.ops.silu(r.s, r.vcs, xb, yb, x.size)
    else:
        rows, cols = x.shape
        xb, yb = r.buf("x", Rows(x.size, cols), x), r.buf("y", Rows(x.size, cols))
        if kind == "softmax":
            l2.ops.softmax(r.s, r.vcs, xb, yb, rows, cols)
        else:
            gb = r.buf("gb", Flat(2 * cols), np.concatenate(extra))
            l2.ops.layernorm(r.s, r.vcs, xb, yb, gb, rows, cols)
    r.run()
    return r.f16(yb, x.shape)


def _model_attention(target, q, k, v, gm=8):
    blocks = k.shape[0] // 64
    r = _Rig(target)
    qb = r.buf("q", MxA(q.shape[0], 64, gm, 2), q)
    kraw = b"".join(MM.pack_b(k[j * 64 : (j + 1) * 64], 16, 2) for j in range(blocks))
    vraw = b"".join(
        MM.pack_b(np.ascontiguousarray(v[j * 64 : (j + 1) * 64].T), 16, 2)
        for j in range(blocks)
    )
    kb = r.buf("k", None, kraw, len(kraw))
    vb = r.buf("v", None, vraw, len(vraw))
    ob = r.buf("o", Tiles(q.shape[0], 64, gm, 16))
    scratch = r.buf("scratch", None, nbytes=4 * gm * 16 * 32)
    idx = r.buf("idx", Flat(32), AT.index_words())
    l2.ops.attention(r.s, r.mgs[0], r.vcs[0], qb, kb, vb, ob, scratch, idx, gm, blocks)
    r.run()
    return MM.unpack(r.t.get, [(0, gm, 0, ob.base)], q.shape[0], 64, gm, 16)


def _model_conv(target, x, wt, gm=12, gn=8, cbc=2):
    h, w, cin = x.shape
    cout = wt.shape[0]
    r = _Rig(target)
    xb = r.buf("x", BandLane(h, w, cin, gm, cbc), x)
    wb = r.buf("w", ConvB(cout, cin, gn, cbc), wt)
    positions = CV.geometry(h, w, gm)[2]
    cb = r.buf("c", Tiles(positions * CV.LANES, cout, gm, gn))
    l2.ops.conv2d(r.s, r.mgs, xb, wb, cb)
    r.run()
    where = [
        (i * gm, gm, j, cb.base + cb.layout.tile(i, j)[0])
        for i in range(positions // gm)
        for j in range(cout // (4 * gn))
    ]
    return CV.unpack(r.t.get, where, h, w, cout, gm, gn)


# -------------------------------------------------------------------- cases
@dataclass
class Case:
    name: str
    shape: str
    fp32: object
    mx: object
    sources: dict


def cases(seed: int = 0) -> list:
    rng = np.random.default_rng(seed)

    def h(*shape, scale=1.0, shift=0.0):
        return (rng.standard_normal(shape) * scale + shift).astype(np.float16)

    out = []
    x, w = h(128, 128, scale=0.5), h(128, 128, scale=0.5)
    zero = np.zeros(128, np.float16)
    linear, rows = l3.kernel("linear"), l3.kernel("rows")
    out.append(
        Case(
            "matmul",
            "128x128x128",
            matmul_ref(x, w, False),
            matmul_ref(x, w, True),
            {
                "interp": lambda: l3.run(
                    linear, "linear", {"x": x, "w": w, "bias": zero}
                )["y"],
                "hw": lambda t: _model_matmul(t, x, w),
            },
        )
    )
    b = h(128, scale=2.0)
    out.append(
        Case(
            "linear+bias",
            "128x128x128",
            matmul_ref(x, w, False) + f32(b),
            matmul_ref(x, w, True) + f32(b),
            {
                "interp": lambda: l3.run(linear, "linear", {"x": x, "w": w, "bias": b})[
                    "y"
                ],
                "hw": lambda t: _model_matmul(t, x, w, bias=b),
            },
        )
    )
    xs, ws = h(256, 128, scale=0.3), h(256, 128, scale=0.3)
    out.append(
        Case(
            "matmul->silu",
            "256x128x256",
            silu_ref(matmul_ref(xs, ws, False)),
            silu_ref(matmul_ref(xs, ws, True)),
            {
                "interp": lambda: l3.run(linear, "linear_silu", {"x": xs, "w": ws})[
                    "y"
                ],
                "hw": lambda t: _model_matmul(t, xs, ws, silu=True),
            },
        )
    )
    xe = h(16, 1024, scale=2.0)
    out.append(
        Case(
            "silu",
            "16384",
            silu_ref(xe),
            silu_ref(xe),
            {
                "interp": lambda: l3.run(linear, "silu_call", {"x": xe})["y"],
                "hw": lambda t: _model_stream(t, "silu", xe),
            },
        )
    )
    xr = h(32, 128, scale=2.0)
    out.append(
        Case(
            "softmax",
            "32x128",
            softmax_ref(xr),
            softmax_ref(xr),
            {
                "interp": lambda: l3.run(rows, "softmax", {"x": xr})["y"],
                "hw": lambda t: _model_stream(t, "softmax", xr),
            },
        )
    )
    xl, g, bb = (
        h(32, 128, scale=2.0, shift=0.5),
        h(128, scale=0.5, shift=1.0),
        h(128, scale=0.5),
    )
    out.append(
        Case(
            "layernorm",
            "32x128",
            layernorm_ref(xl, g, bb),
            layernorm_ref(xl, g, bb),
            {
                "interp": lambda: l3.run(rows, "layernorm", {"x": xl, "g": g, "b": bb})[
                    "y"
                ],
                "hw": lambda t: _model_stream(t, "layernorm", xl, g, bb),
            },
        )
    )
    attn = l3.kernel("attention")
    for blocks in (2, 4):
        q = h(32, 64, scale=LOG2E / 8)
        k, v = h(64 * blocks, 64), h(64 * blocks, 64)
        out.append(
            Case(
                "attention",
                f"32q x {64 * blocks}k x 64",
                attention_ref(q, k, v, False),
                attention_ref(q, k, v, True),
                {
                    "interp": lambda q=q, k=k, v=v: l3.run(
                        attn, "attention", {"q": q[None], "k": k[None], "vt": v.T[None]}
                    )["o"][0],
                    "hw": lambda t, q=q, k=k, v=v: _model_attention(t, q, k, v),
                },
            )
        )
    xc, wc = h(16, 16, 64, scale=0.5), h(32, 64, 3, 3, scale=0.2)
    conv = l3.kernel("conv")
    taps = np.ascontiguousarray(wc.transpose(0, 2, 3, 1))
    zeros = np.zeros((16, 16, 32), np.float16)
    out.append(
        Case(
            "conv3x3",
            "16x16 64->32",
            conv_ref(xc, wc, False),
            conv_ref(xc, wc, True),
            {
                "interp": lambda: l3.run(
                    conv, "conv", {"x": xc, "w": taps, "x2": zeros}
                )["y"],
                "hw": lambda t: _model_conv(t, xc, wc),
            },
        )
    )
    return out


def report(seed: int = 0, rtl=None, only=None) -> list:
    """One row a (case, source, reference): the case, the source, the
    reference, and `FIELDS`. `rtl`: a card target (alloc/write/get/run and a
    `fresh()`, as `UnitModel`) to run the ``hw`` cases on as well; `only`:
    case names to keep."""
    rows = []
    for c in cases(seed):
        if only and c.name not in only:
            continue
        head = {"case": c.name, "shape": c.shape}
        rows.append(head | {"source": "mx-ref", "ref": "fp32"} | errors(c.mx, c.fp32))
        got = {"interp": c.sources["interp"](), "model": c.sources["hw"](UnitModel())}
        if rtl is not None:
            got["rtl"] = c.sources["hw"](rtl.fresh())
        for source, g in got.items():
            for ref, want in (("fp32", c.fp32), ("mx", c.mx)):
                rows.append(head | {"source": source, "ref": ref} | errors(g, want))
        if rtl is not None:
            ndiff = int(np.count_nonzero(got["rtl"] != got["model"]))
            rows.append(
                head
                | {
                    "source": "rtl",
                    "ref": "model",
                    "ndiff": ndiff,
                    "n": got["rtl"].size,
                }
                | errors(got["rtl"], got["model"])
            )
    return rows


def markdown(rows) -> str:
    head = ["case", "shape", "source", "ref", *FIELDS, "ndiff"]
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for r in rows:
        cells = [r["case"], r["shape"], r["source"], r["ref"]]
        cells += [f"{r[f]:.3e}" for f in FIELDS]
        cells.append(f"{r['ndiff']}/{r['n']}" if "ndiff" in r else "")
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def write(rows, out: pathlib.Path) -> list:
    """`numerics.json` and `numerics.md` in `out`; the paths."""
    out.mkdir(parents=True, exist_ok=True)
    js, md = out / "numerics.json", out / "numerics.md"
    js.write_text(json.dumps(rows, indent=1), encoding="utf-8")
    md.write_text(markdown(rows), encoding="utf-8")
    return [js, md]


__all__ = ["cases", "errors", "markdown", "report", "write"]
