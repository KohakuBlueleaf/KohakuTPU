"""Cycle parity of the L2 -> L1 compiler against the hand-written L1 kernels,
on a card model, same inputs, both graded.

    python scripts/py/l2/parity.py --build build/v9prof/vlt_card_v9_1n [--case matmul ...]

Each case runs the hand-written L1 program and the compiled L2 schedule twice
(the second reported), grades both against the quantised reference, and prints
cycles and the ratio compiled / hand.
"""

import argparse
import pathlib
import sys

import numpy as np

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts/py/l1"))
sys.path.insert(0, str(ROOT / "compiler/tests/l1"))
from card import L1Card
from check import grade
from kohakuaccel.ir.l2 import Schedule
from kohakutpu.hw.mxfp7 import value_fp16
from kohakutpu.ir import l2
from kohakutpu.ir.l1 import Program
from kohakutpu.ir.l1.kernels import attention as AT
from kohakutpu.ir.l1.kernels import binary as BI
from kohakutpu.ir.l1.kernels import conv2d as CV
from kohakutpu.ir.l1.kernels import fused as FU
from kohakutpu.ir.l1.kernels import layernorm as LN
from kohakutpu.ir.l1.kernels import matmul as MM
from kohakutpu.ir.l1.kernels import silu as SI
from kohakutpu.ir.l1.kernels import softmax as SM
from kohakutpu.ir.l2.layouts import BandLane, ConvB, Flat, MxA, MxB, Rows, Tiles
from test_kernels import flash_reference


class Bench:
    def __init__(self, build) -> None:
        self.card = L1Card(build)
        self.machine = self.card.machine
        self.rows = []

    def reset(self) -> None:
        self.card.top = 0x0010_0000

    def buf(self, s, name, layout, data=None, nbytes=None):
        n = nbytes if nbytes is not None else layout.nbytes
        b = s.buffer(name, n, layout, base=self.card.alloc(n))
        if data is not None:
            raw = data if isinstance(data, (bytes, bytearray)) else layout.pack(data)
            self.card.write(b.base, np.frombuffer(bytes(raw), np.uint8))
        return b

    def time(self, progs) -> int:
        for p in progs:
            self.card.run(p)
        return sum(self.card.run(p)["cycles"] for p in progs)

    def report(self, name, hand, comp) -> None:
        r = comp / hand
        self.rows.append((name, hand, comp, r))
        print(f"PARITY {name}: hand {hand}  compiled {comp}  ratio {r:.3f}", flush=True)


def case_matmul(b: Bench, m, k, n, gm, gn, nk, bias=False) -> None:
    b.reset()
    rng = np.random.default_rng(7)
    x = (rng.standard_normal((m, k)) * 0.5).astype(np.float16)
    w = (rng.standard_normal((n, k)) * 0.5).astype(np.float16)
    bv = (rng.standard_normal(n) * 4).astype(np.float16)
    s = Schedule()
    a = b.buf(s, "a", MxA(m, k, gm, nk), x)
    bb = b.buf(s, "b", MxB(n, k, gn, nk), w)
    c_hand = b.buf(s, "ch", Tiles(m, n, gm, gn))
    c = b.buf(s, "c", Tiles(m, n, gm, gn))
    extra, kw = {}, {}
    model = value_fp16(x) @ value_fp16(w).T
    if bias:
        ones = b.buf(s, "ones", None, MM.ones_block(gm), nbytes=gm * MM.ENTRY)
        bias_b = b.buf(s, "bias", None, MM.bias_block(bv, gn), nbytes=n // 4 * MM.ENTRY)
        extra = {"ones": ones, "bias": bias_b}
        kw = {"ones_at": ones.base, "bias_at": bias_b.base}
        model = (
            model + value_fp16(np.pad(bv[:, None], ((0, 0), (0, MM.KBLOCK - 1))))[:, 0]
        )
    hand = Program(b.machine)
    where = MM.matmul(hand, a.base, bb.base, c_hand.base, m, n, k, gm, gn, nk, **kw)
    t_hand = b.time([hand])
    grade("hand", MM.unpack(b.card.get, where, m, n, gm, gn), model)
    l2.ops.matmul(s, sorted(b.machine.units["MG"]), a, bb, c, **extra)
    t_comp = b.time(l2.compile(s, b.machine))
    grade("compiled", c.layout.unpack(b.card.get, c.base), model)
    b.report(
        f"{'linear+bias' if bias else 'matmul'} {m}x{k}x{n} {gm}x{gn}x{nk}",
        t_hand,
        t_comp,
    )


def case_conv(b: Bench, h, w, cin, cout, gm, gn, cbc) -> None:
    b.reset()
    rng = np.random.default_rng(11)
    x = (rng.standard_normal((h, w, cin)) * 0.5).astype(np.float16)
    wt = (rng.standard_normal((cout, cin, 3, 3)) * 0.2).astype(np.float16)
    s = Schedule()
    xb = b.buf(s, "x", BandLane(h, w, cin, gm, cbc), x)
    wb = b.buf(s, "w", ConvB(cout, cin, gn, cbc), wt)
    positions = CV.geometry(h, w, gm)[2]
    lay = Tiles(positions * CV.LANES, cout, gm, gn)
    c_hand, c = b.buf(s, "ch", lay), b.buf(s, "c", lay)
    xq = np.zeros((h + 2, w + 2, cin))
    xq[1:-1, 1:-1] = value_fp16(x)
    wk = np.ascontiguousarray(wt.transpose(0, 2, 3, 1)).reshape(cout, 9 * cin)
    wq = value_fp16(wk).reshape(cout, 3, 3, cin)
    model = sum(xq[dy : dy + h, dx : dx + w] @ wq[:, dy, dx, :].T for dy, dx in CV.TAPS)
    hand = Program(b.machine)
    where = CV.conv(hand, xb.base, wb.base, c_hand.base, h, w, cin, cout, gm, gn, cbc)
    t_hand = b.time([hand])
    grade("hand", CV.unpack(b.card.get, where, h, w, cout, gm, gn), model)
    l2.ops.conv2d(s, sorted(b.machine.units["MG"]), xb, wb, c)
    t_comp = b.time(l2.compile(s, b.machine))
    tiles = [
        (i * gm, gm, j, c.base + lay.tile(i, j)[0])
        for i in range(positions // gm)
        for j in range(cout // (4 * gn))
    ]
    grade("compiled", CV.unpack(b.card.get, tiles, h, w, cout, gm, gn), model)
    b.report(f"conv {h}x{w} {cin}->{cout} {gm}x{gn} chunk {cbc}", t_hand, t_comp)


def case_vector(b: Bench, kind, n=0, rows=0, cols=0) -> None:
    b.reset()
    rng = np.random.default_rng(3)
    s = Schedule()
    vcs = sorted(b.machine.units["VC"])
    hand = Program(b.machine)
    if kind == "silu":
        x = (rng.standard_normal(n) * 2).astype(np.float16)
        xb, yh, y = (
            b.buf(s, "x", Flat(n), x),
            b.buf(s, "yh", Flat(n)),
            b.buf(s, "y", Flat(n)),
        )
        SI.silu(hand, xb.base, yh.base, n)
        l2.ops.silu(s, vcs, xb, y, n)
        v = x.astype(np.float64)
        want, size, shape = v / (1 + np.exp(-v)), n * 2, (n,)
    elif kind == "add":
        x = (rng.standard_normal(n) * 2).astype(np.float16)
        z = (rng.standard_normal(n) * 2).astype(np.float16)
        xb, zb = b.buf(s, "x", Flat(n), x), b.buf(s, "z", Flat(n), z)
        yh, y = b.buf(s, "yh", Flat(n)), b.buf(s, "y", Flat(n))
        BI.binary(hand, "add", xb.base, zb.base, yh.base, n)
        l2.ops.binary(s, vcs, "add", xb, zb, y, n)
        want, size, shape = x.astype(np.float64) + z.astype(np.float64), n * 2, (n,)
    elif kind == "softmax":
        x = (rng.standard_normal((rows, cols)) * 2).astype(np.float16)
        lay = Rows(rows * cols, cols)
        xb, yh, y = b.buf(s, "x", lay, x), b.buf(s, "yh", lay), b.buf(s, "y", lay)
        SM.softmax(hand, xb.base, yh.base, rows, cols)
        l2.ops.softmax(s, vcs, xb, y, rows, cols)
        v = x.astype(np.float64)
        e = np.exp(v - v.max(1, keepdims=True))
        want, size, shape = e / e.sum(1, keepdims=True), x.nbytes, (rows, cols)
    else:  # layernorm
        x = (rng.standard_normal((rows, cols)) * 2 + 0.5).astype(np.float16)
        g = (rng.standard_normal(cols) * 0.5 + 1).astype(np.float16)
        bt = (rng.standard_normal(cols) * 0.5).astype(np.float16)
        lay = Rows(rows * cols, cols)
        xb, yh, y = b.buf(s, "x", lay, x), b.buf(s, "yh", lay), b.buf(s, "y", lay)
        gb = b.buf(s, "gb", Flat(2 * cols), np.concatenate([g, bt]))
        LN.layernorm(hand, xb.base, yh.base, gb.base, rows, cols)
        l2.ops.layernorm(s, vcs, xb, y, gb, rows, cols)
        v = x.astype(np.float64)
        want = (v - v.mean(1, keepdims=True)) / np.sqrt(v.var(1, keepdims=True) + 1e-5)
        want = want * g.astype(np.float64) + bt.astype(np.float64)
        size, shape = x.nbytes, (rows, cols)
    t_hand = b.time([hand])
    read = lambda at: np.frombuffer(b.card.get(at, size), np.float16).astype(
        np.float64
    )
    grade("hand", read(yh.base).reshape(shape), want)
    t_comp = b.time(l2.compile(s, b.machine))
    grade("compiled", read(y.base).reshape(shape), want)
    b.report(f"{kind} {n or f'{rows}x{cols}'}", t_hand, t_comp)


def case_matmul_silu(b: Bench, m, k, n, gm, gn, nk) -> None:
    b.reset()
    rng = np.random.default_rng(7)
    x = (rng.standard_normal((m, k)) * 0.5).astype(np.float16)
    w = (rng.standard_normal((n, k)) * 0.5).astype(np.float16)
    s = Schedule()
    a = b.buf(s, "a", MxA(m, k, gm, nk), x)
    bb = b.buf(s, "b", MxB(n, k, gn, nk), w)
    c_hand, c = b.buf(s, "ch", Tiles(m, n, gm, gn)), b.buf(s, "c", Tiles(m, n, gm, gn))
    sinks = [b.buf(s, f"sink{i}", Flat(256 * 16)) for i in range(2)]
    h = value_fp16(x) @ value_fp16(w).T
    model = h / (1 + np.exp(-h))
    hand = Program(b.machine)
    where = FU.matmul_silu(
        hand,
        a.base,
        bb.base,
        c_hand.base,
        m,
        n,
        k,
        gm,
        gn,
        nk,
        sinks=[x.base for x in sinks],
    )
    t_hand = b.time([hand])
    grade("hand", MM.unpack(b.card.get, where, m, n, gm, gn), model)
    l2.ops.matmul_silu(
        s, sorted(b.machine.units["MG"]), sorted(b.machine.units["VC"]), a, bb, c, sinks
    )
    t_comp = b.time(l2.compile(s, b.machine))
    grade("compiled", c.layout.unpack(b.card.get, c.base), model)
    b.report(f"matmul->silu {m}x{k}x{n}", t_hand, t_comp)


def case_attention(b: Bench, gm, blocks) -> None:
    b.reset()
    rng = np.random.default_rng(1)
    rows, keys = 4 * gm, 64 * blocks
    q = (rng.standard_normal((rows, 64)) * np.log2(np.e) / 8 * 3).astype(np.float16)
    k = rng.standard_normal((keys, 64)).astype(np.float16)
    v = rng.standard_normal((keys, 64)).astype(np.float16)
    s = Schedule()
    qb = b.buf(s, "q", MxA(rows, 64, gm, 2), q)
    kraw = b"".join(MM.pack_b(k[j * 64 : (j + 1) * 64], 16, 2) for j in range(blocks))
    vraw = b"".join(
        MM.pack_b(np.ascontiguousarray(v[j * 64 : (j + 1) * 64].T), 16, 2)
        for j in range(blocks)
    )
    kb = b.buf(s, "k", None, kraw, nbytes=len(kraw))
    vb = b.buf(s, "v", None, vraw, nbytes=len(vraw))
    oh, ob = b.buf(s, "oh", Tiles(rows, 64, gm, 16)), b.buf(
        s, "o", Tiles(rows, 64, gm, 16)
    )
    sh = b.buf(s, "sh", None, nbytes=4 * gm * 16 * 32)
    sc = b.buf(s, "sc", None, nbytes=4 * gm * 16 * 32)
    idx = b.buf(s, "idx", Flat(32), AT.index_words())
    model = flash_reference(q, k, v, blocks)
    hand = Program(b.machine)
    AT.attention(
        hand, qb.base, kb.base, vb.base, oh.base, sh.base, idx.base, gm, blocks
    )
    t_hand = b.time([hand])
    get = lambda at: MM.unpack(
        b.card.get, [(0, gm, 0, at)], rows, 64, gm, 16
    )
    grade("hand", get(oh.base), model, tol=2e-2)
    l2.ops.attention(
        s,
        min(b.machine.units["MG"]),
        min(b.machine.units["VC"]),
        qb,
        kb,
        vb,
        ob,
        sc,
        idx,
        gm,
        blocks,
    )
    t_comp = b.time(l2.compile(s, b.machine))
    grade("compiled", get(ob.base), model, tol=2e-2)
    b.report(f"attention {rows}q x {blocks} key blocks", t_hand, t_comp)


CASES = {
    "matmul": lambda b: (
        case_matmul(b, 1024, 1024, 1024, 64, 64, 2),
        case_matmul(b, 1024, 512, 1024, 64, 64, 2),
    ),
    "bias": lambda b: case_matmul(b, 1024, 512, 1024, 64, 64, 2, bias=True),
    "conv": lambda b: (
        case_conv(b, 64, 64, 128, 128, 44, 32, 4),
        case_conv(b, 32, 32, 320, 320, 68, 40, 2),
    ),
    "vector": lambda b: (
        case_vector(b, "silu", n=65536),
        case_vector(b, "add", n=262144),
        case_vector(b, "softmax", rows=256, cols=256),
        case_vector(b, "layernorm", rows=256, cols=320),
    ),
    "fused": lambda b: case_matmul_silu(b, 1024, 1024, 1024, 64, 64, 2),
    "attention": lambda b: case_attention(b, 8, 4),
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", required=True)
    ap.add_argument("--case", action="append", default=[], choices=sorted(CASES))
    a = ap.parse_args()
    b = Bench(a.build)
    for name in a.case or list(CASES):
        CASES[name](b)
    b.card.close()
    print("\nkernel | hand | compiled | ratio")
    for name, hand, comp, r in b.rows:
        print(f"{name} | {hand} | {comp} | {r:.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
