"""Cycle parity of the L3 -> L2 compiler against the hand-written L2
schedules (`kohakutpu.ir.l2.ops`), on a Verilated card, same inputs.

    python scripts/py/l3_parity.py --build build/v9pt/vlt_card_v9_1n -o DIR [--case NAME ...]

Each case runs once on cores holding none of its images (cold; the L3 program
compiled with `installs=1`), then twice more (warm, the second run reported;
compiled with `installs=0`), for the hand schedule and the compiled program:
node cycles (the completions'), each warm result's rel_l2 against the
quantised float64 reference, and the elements where the two differ.
`DIR/parity.json` holds every row.
"""

import argparse
import json
import pathlib
import sys

import numpy as np
from kohakutpu.hw.mxfp7 import value_fp16
from kohakutpu.ir import l2, l3
from kohakutpu.ir.l1.kernels import attention as AT
from kohakutpu.ir.l1.kernels import conv2d as CV
from kohakutpu.ir.l1.kernels import matmul as MM
from kohakutpu.ir.l2.layouts import BandLane, ConvB, Flat, MxA, MxB, Rows, Tiles
from kohakutpu.ir.l3 import lower
from kohakutpu.ir.numerics import _Rig
from numerics_rtl import CardModel

MATMUL = """level l3
{pre}
program {name}(x: mx7[M, K], w: mx7[N, K]{bias})
    tile bm = {bm}
    tile bn = {bn}
    y = output : f16[M, N]
    map i in tiles(M, bm), j in tiles(N, bn)
        c = mmt x[i, :], w[j, :] : f16[bm, bn]
{body}"""
STORE = "        store y[i, j] = c\n"
BIAS = "        r = add c, bias[j] : f16[bm, bn]\n        store y[i, j] = r\n"
SILU = "        a = inline silu(c)\n        store y[i, j] = a\n"
SILU_FN = """fn silu(x: f16[M, N]) -> f16[M, N]
    t = mul x, -1.4426950408889634 : f32[M, N]
    e = exp2 t : f32[M, N]
    d = add e, 1.0 : f32[M, N]
    r = inv d : f32[M, N]
    y = mul x, r : f16[M, N]
    return y
"""
ADD = """level l3
program add(a: f16[N], b: f16[N])
    y = output : f16[N]
    s = add a, b : f16[N]
    store y = s
"""


def f16(x):
    return np.asarray(x, np.float16).astype(np.float64)


def rel(got, want) -> float:
    return float(np.linalg.norm(got - want) / np.linalg.norm(want))


class Bench:
    def __init__(self, build) -> None:
        self.card = CardModel(build)
        self.rows: list = []

    def runs(self, cold, warm) -> tuple:
        """`cold(target)` on cores holding none of its images, then `warm`
        twice; the second result, its node cycles and the cold run's."""
        self.card.resident.clear()
        t = self.card.fresh()
        cold(t)
        first = t.cycles
        warm(self.card.fresh())
        t = self.card.fresh()
        out = warm(t)
        return out, t.cycles, first

    def hand(self, build) -> tuple:
        def run(t):
            r = _Rig(t)
            get = build(r)
            for p in l2.compile(r.s):
                t.run(p)
            return get(t)

        return self.runs(run, run)

    def l3(self, module, program, arrays, out, **knobs) -> tuple:
        """Compiled for cores that do not hold its images (`installs=1`) for
        the cold run, for cores that do (`installs=0`) for the warm."""
        shapes = {k: np.shape(v) for k, v in arrays.items()}

        def run(installs):
            def go(t):
                comp = lower.compile(
                    module, program, shapes, t, installs=installs, **knobs
                )
                return comp.run(t, arrays)[out]

            return go

        return self.runs(run(1.0), run(0.0))

    def report(self, name, want, hand, l3_, extra="") -> None:
        (h, th, hc), (c, tc, cc) = hand, l3_
        row = {
            "case": name + extra,
            "hand": th,
            "l3": tc,
            "ratio": tc / th,
            "hand_cold": hc,
            "l3_cold": cc,
            "ratio_cold": cc / hc,
            "hand_rel": rel(h, want),
            "l3_rel": rel(c, want),
            "ndiff": int(np.count_nonzero(h != c)),
            "n": int(np.size(c)),
        }
        self.rows.append(row)
        print(
            f"PARITY {row['case']}: hand {th} l3 {tc} ratio {row['ratio']:.3f} "
            f"cold {hc} / {cc} ratio {row['ratio_cold']:.3f} "
            f"rel {row['hand_rel']:.2e}/{row['l3_rel']:.2e} "
            f"diff {row['ndiff']}/{row['n']}",
            flush=True,
        )


def case_matmul(b: Bench, m, k, n, gm, gn, bias=None) -> None:
    rng = np.random.default_rng(7)
    x = (rng.standard_normal((m, k)) * 0.5).astype(np.float16)
    w = (rng.standard_normal((n, k)) * 0.5).astype(np.float16)
    bv = (rng.standard_normal(n) * 4).astype(np.float16)
    want = value_fp16(x) @ value_fp16(w).T + (0 if bias is None else bv)

    def build(r):
        a = r.buf("a", MxA(m, k, gm, 2), x)
        bb = r.buf("b", MxB(n, k, gn, 2), w)
        c = r.buf("c", Tiles(m, n, gm, gn))
        extra = {}
        if bias is not None:
            extra = {
                "ones": r.buf("ones", None, MM.ones_block(gm), gm * MM.ENTRY),
                "bias": r.buf("bias", None, MM.bias_block(bv, gn), n // 4 * MM.ENTRY),
            }
        l2.ops.matmul(r.s, r.mgs, a, bb, c, **extra)
        return lambda t: c.layout.unpack(t.get, c.base)

    hand = b.hand(build)
    name = "linear" if bias else "matmul"
    text = MATMUL.format(
        pre="",
        name=name,
        bias=", bias: f16[N]" if bias else "",
        bm=4 * gm,
        bn=4 * gn,
        body=BIAS if bias else STORE,
    )
    arrays = {"x": x, "w": w} | ({"bias": bv} if bias else {})
    knobs = {"bias": bias} if bias else {}
    got = b.l3(l3.read(text), name, arrays, "y", **knobs)
    b.report(f"{name} {m}x{k}x{n}", want, hand, got, f" bias={bias}" if bias else "")


def case_conv(b: Bench, h, w, cin, cout, gm, gn, cbc) -> None:
    rng = np.random.default_rng(11)
    x = (rng.standard_normal((h, w, cin)) * 0.5).astype(np.float16)
    wt = (rng.standard_normal((cout, cin, 3, 3)) * 0.2).astype(np.float16)
    taps = np.ascontiguousarray(wt.transpose(0, 2, 3, 1))
    xq = np.zeros((h + 2, w + 2, cin))
    xq[1:-1, 1:-1] = value_fp16(x)
    wq = value_fp16(taps.reshape(cout, 9 * cin)).reshape(cout, 3, 3, cin)
    want = sum(xq[dy : dy + h, dx : dx + w] @ wq[:, dy, dx, :].T for dy, dx in CV.TAPS)

    def build(r):
        xb = r.buf("x", BandLane(h, w, cin, gm, cbc), x)
        wb = r.buf("w", ConvB(cout, cin, gn, cbc), wt)
        positions = CV.geometry(h, w, gm)[2]
        lay = Tiles(positions * CV.LANES, cout, gm, gn)
        cb = r.buf("c", lay)
        l2.ops.conv2d(r.s, r.mgs, xb, wb, cb)
        where = [
            (i * gm, gm, j, cb.base + lay.tile(i, j)[0])
            for i in range(positions // gm)
            for j in range(cout // (4 * gn))
        ]
        return lambda t: CV.unpack(t.get, where, h, w, cout, gm, gn)

    hand = b.hand(build)
    got = b.l3(
        l3.kernel("conv"), "conv_only", {"x": x, "w": taps}, "y", conv_tile=(gm, gn)
    )
    b.report(f"conv {h}x{w} {cin}->{cout}", want, hand, got)


def case_vector(b: Bench, kind, n=0, rows=0, cols=0) -> None:
    rng = np.random.default_rng(3)
    if kind in ("silu", "add"):
        x = (rng.standard_normal(n) * 2).astype(np.float16)
        z = (rng.standard_normal(n) * 2).astype(np.float16)
        shape, lay = (n,), Flat(n)
    else:
        x = (rng.standard_normal((rows, cols)) * 2 + 0.5).astype(np.float16)
        shape, lay = (rows, cols), Rows(rows * cols, cols)
    g = (rng.standard_normal(cols) * 0.5 + 1).astype(np.float16)
    bt = (rng.standard_normal(cols) * 0.5).astype(np.float16)
    v = x.astype(np.float64)
    if kind == "silu":
        want = v / (1 + np.exp(-v))
        module, prog, arrays = (
            l3.kernel("linear"),
            "silu_call",
            {"x": x.reshape(-1, 1024)},
        )
    elif kind == "add":
        want = v + z.astype(np.float64)
        module, prog, arrays = l3.read(ADD), "add", {"a": x, "b": z}
    elif kind == "softmax":
        e = np.exp(v - v.max(1, keepdims=True))
        want = e / e.sum(1, keepdims=True)
        module, prog, arrays = l3.kernel("rows"), "softmax", {"x": x}
    else:
        want = (v - v.mean(1, keepdims=True)) / np.sqrt(v.var(1, keepdims=True) + 1e-5)
        want = want * g.astype(np.float64) + bt.astype(np.float64)
        module, prog, arrays = l3.kernel("rows"), "layernorm", {"x": x, "g": g, "b": bt}

    def build(r):
        xb, yb = r.buf("x", lay, x.ravel() if kind in ("silu", "add") else x), r.buf(
            "y", lay
        )
        if kind == "silu":
            l2.ops.silu(r.s, r.vcs, xb, yb, n)
        elif kind == "add":
            l2.ops.binary(r.s, r.vcs, "add", xb, r.buf("z", lay, z), yb, n)
        elif kind == "softmax":
            l2.ops.softmax(r.s, r.vcs, xb, yb, rows, cols)
        else:
            gb = r.buf("gb", Flat(2 * cols), np.concatenate([g, bt]))
            l2.ops.layernorm(r.s, r.vcs, xb, yb, gb, rows, cols)
        return lambda t: r.f16(yb, shape)

    hand = b.hand(build)
    got = b.l3(module, prog, arrays, "y")
    b.report(
        f"{kind} {n or f'{rows}x{cols}'}",
        want,
        hand,
        (got[0].reshape(shape), *got[1:]),
    )


def case_matmul_silu(b: Bench, m, k, n, gm, gn) -> None:
    rng = np.random.default_rng(7)
    x = (rng.standard_normal((m, k)) * 0.5).astype(np.float16)
    w = (rng.standard_normal((n, k)) * 0.5).astype(np.float16)
    hq = value_fp16(x) @ value_fp16(w).T
    want = hq / (1 + np.exp(-hq))

    def build(r):
        a = r.buf("a", MxA(m, k, gm, 2), x)
        bb = r.buf("b", MxB(n, k, gn, 2), w)
        c = r.buf("c", Tiles(m, n, gm, gn))
        sinks = [r.buf(f"sink{i}", Flat(256 * 16)) for i in range(len(r.vcs))]
        l2.ops.matmul_silu(r.s, r.mgs, r.vcs, a, bb, c, sinks)
        return lambda t: c.layout.unpack(t.get, c.base)

    hand = b.hand(build)
    text = MATMUL.format(
        pre=SILU_FN, name="linear_silu", bias="", bm=4 * gm, bn=4 * gn, body=SILU
    )
    got = b.l3(l3.read(text), "linear_silu", {"x": x, "w": w}, "y")
    b.report(f"matmul->silu {m}x{k}x{n}", want, hand, got)


def case_attention(b: Bench, gm, blocks) -> None:
    rng = np.random.default_rng(1)
    rows, keys = 4 * gm, 64 * blocks
    q = (rng.standard_normal((rows, 64)) * np.log2(np.e) / 8 * 3).astype(np.float16)
    k = rng.standard_normal((keys, 64)).astype(np.float16)
    v = rng.standard_normal((keys, 64)).astype(np.float16)
    s = value_fp16(q) @ value_fp16(k).T
    p = np.exp2(s - s.max(1, keepdims=True))
    want = (p / p.sum(1, keepdims=True)) @ v.astype(np.float64)

    def build(r):
        qb = r.buf("q", MxA(rows, 64, gm, 2), q)
        kraw = b"".join(
            MM.pack_b(k[j * 64 : (j + 1) * 64], 16, 2) for j in range(blocks)
        )
        vraw = b"".join(
            MM.pack_b(np.ascontiguousarray(v[j * 64 : (j + 1) * 64].T), 16, 2)
            for j in range(blocks)
        )
        kb, vb = r.buf("k", None, kraw, len(kraw)), r.buf("v", None, vraw, len(vraw))
        ob = r.buf("o", Tiles(rows, 64, gm, 16))
        sc = r.buf("scratch", None, nbytes=4 * gm * 16 * 32)
        idx = r.buf("idx", Flat(32), AT.index_words())
        l2.ops.attention(r.s, r.mgs[0], r.vcs[0], qb, kb, vb, ob, sc, idx, gm, blocks)
        return lambda t: MM.unpack(t.get, [(0, gm, 0, ob.base)], rows, 64, gm, 16)

    hand = b.hand(build)
    arrays = {"q": q[None], "k": k[None], "vt": v.T[None]}
    got = b.l3(l3.kernel("attention"), "attention", arrays, "o")
    b.report(
        f"attention {rows}q x {blocks} key blocks",
        want,
        hand,
        (got[0][0], *got[1:]),
    )


CASES = {
    "matmul": lambda b: (
        case_matmul(b, 1024, 1024, 1024, 64, 64),
        case_matmul(b, 1024, 512, 1024, 64, 64),
    ),
    "bias": lambda b: (
        case_matmul(b, 1024, 512, 1024, 64, 64, bias="cluster"),
        case_matmul(b, 1024, 512, 1024, 64, 64, bias="core"),
    ),
    "conv": lambda b: (
        case_conv(b, 64, 64, 128, 128, 44, 32, 4),
        case_conv(b, 32, 32, 320, 320, 68, 40, 2),
    ),
    "silu": lambda b: case_vector(b, "silu", n=65536),
    "add": lambda b: case_vector(b, "add", n=262144),
    "softmax": lambda b: case_vector(b, "softmax", rows=256, cols=256),
    "layernorm": lambda b: case_vector(b, "layernorm", rows=256, cols=320),
    "fused": lambda b: case_matmul_silu(b, 1024, 1024, 1024, 64, 64),
    "attention": lambda b: case_attention(b, 8, 4),
}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", required=True)
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--case", action="append", default=[], choices=sorted(CASES))
    a = ap.parse_args(argv)
    b = Bench(a.build)
    try:
        for name in a.case or list(CASES):
            CASES[name](b)
    finally:
        b.card.close()
    out = pathlib.Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "parity.json").write_text(json.dumps(b.rows, indent=1), encoding="utf-8")
    print(
        "\nkernel | hand | l3 | ratio | hand cold | l3 cold | ratio | "
        "hand rel_l2 | l3 rel_l2 | differ"
    )
    for r in b.rows:
        print(
            f"{r['case']} | {r['hand']} | {r['l3']} | {r['ratio']:.3f} | "
            f"{r['hand_cold']} | {r['l3_cold']} | {r['ratio_cold']:.3f} | "
            f"{r['hand_rel']:.2e} | {r['l3_rel']:.2e} | {r['ndiff']}/{r['n']}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
