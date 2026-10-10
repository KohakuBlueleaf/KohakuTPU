"""Card run of the L1 vector kernels: silu and row softmax.

    python scripts/py/l1/k_vector.py --build build/v9prof/vlt_card_v9_1n --silu 65536 --softmax 256,256

Each case runs twice (the second reported), is graded element by element, and
the vector cores' state/opcode breakdown is printed at the end.
"""

import argparse
import pathlib
import sys

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from card import L1Card, report
from check import grade
from kohakutpu.ir.l1 import Program
from kohakutpu.ir.l1.kernels import binary as BI
from kohakutpu.ir.l1.kernels import layernorm as LN
from kohakutpu.ir.l1.kernels import silu as SI
from kohakutpu.ir.l1.kernels import softmax as SM


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", required=True)
    ap.add_argument("--silu", type=int, action="append", default=[])
    ap.add_argument("--silu-cfg", default="256,3,2", help="words,group,sets")
    ap.add_argument("--softmax", action="append", default=[], help="rows,cols")
    ap.add_argument("--softmax-cfg", default="8,8", help="rows_per_run,block")
    ap.add_argument(
        "--binary", action="append", default=[], help="op,n (op: add, sub, mul)"
    )
    ap.add_argument("--layernorm", action="append", default=[], help="rows,cols")
    ap.add_argument("--layernorm-rows", type=int, default=8, help="rows a RUN")
    ap.add_argument(
        "--steps", action="store_true", help="print each package step's end"
    )
    a = ap.parse_args()
    card = L1Card(a.build, steps=a.steps)

    def timed(prog) -> int:
        card.run(prog)
        got = card.run(prog)
        for first, last, at, what in got["steps"]:
            print(f"    step {first}..{last} ends {at}: {what}")
        print(f"    phases {got['phases']}")
        return got["cycles"]

    rng = np.random.default_rng(3)
    for spec in a.binary:
        op, n = spec.split(",")
        n = int(n)
        xa = (rng.standard_normal(n) * 2).astype(np.float16)
        xb = (rng.standard_normal(n) * 2).astype(np.float16)
        pa, pb, dst = card.put(xa), card.put(xb), card.alloc(n * 2)
        prog = Program(card.machine)
        BI.binary(prog, op, pa, pb, dst, n)
        got = timed(prog)
        va, vb = xa.astype(np.float64), xb.astype(np.float64)
        want = {"add": va + vb, "sub": va - vb, "mul": va * vb}[op]
        y = np.frombuffer(card.get(dst, n * 2), np.float16).astype(np.float64)
        grade(f"{op} {n}", y, want)
        print(
            f"{op} {n}: {got} cycles  {n * 6 / got:.2f} B/cycle moved "
            f"(two reads, one write)",
            flush=True,
        )
    for spec in a.layernorm:
        r, c = (int(v) for v in spec.split(","))
        x = (rng.standard_normal((r, c)) * 2 + 0.5).astype(np.float16)
        g = (rng.standard_normal(c) * 0.5 + 1).astype(np.float16)
        b = (rng.standard_normal(c) * 0.5).astype(np.float16)
        src, gb, dst = (
            card.put(x),
            card.put(np.concatenate([g, b])),
            card.alloc(x.nbytes),
        )
        prog = Program(card.machine)
        LN.layernorm(prog, src, dst, gb, r, c, a.layernorm_rows)
        got = timed(prog)
        v = x.astype(np.float64)
        want = (v - v.mean(1, keepdims=True)) / np.sqrt(v.var(1, keepdims=True) + 1e-5)
        want = want * g.astype(np.float64) + b.astype(np.float64)
        y = np.frombuffer(card.get(dst, x.nbytes), np.float16).astype(np.float64)
        grade(f"layernorm {r}x{c}", y.reshape(r, c), want, tol=4e-3)
        print(f"layernorm {r}x{c}: {got} cycles", flush=True)
    words, group, sets = (int(v) for v in a.silu_cfg.split(","))
    for n in a.silu:
        x = (rng.standard_normal(n) * 2).astype(np.float16)
        v = x.astype(np.float64)
        src, dst = card.put(x), card.alloc(n * 2)
        prog = Program(card.machine)
        SI.silu(prog, src, dst, n, words, group, sets)
        got = timed(prog)
        y = np.frombuffer(card.get(dst, n * 2), np.float16).astype(np.float64)
        grade(f"silu {n}", y, v / (1 + np.exp(-v)))
        cores = len(prog.units("VC"))
        print(
            f"silu {n}: {got} cycles  lane {SI.ALU_OPS * n / 16 / (got * cores):.3f}",
            flush=True,
        )
    rows, block = (int(v) for v in a.softmax_cfg.split(","))
    for spec in a.softmax:
        r, c = (int(v) for v in spec.split(","))
        x = (rng.standard_normal((r, c)) * 2).astype(np.float16)
        v = x.astype(np.float64)
        e = np.exp(v - v.max(1, keepdims=True))
        src, dst = card.put(x), card.alloc(x.nbytes)
        prog = Program(card.machine)
        SM.softmax(prog, src, dst, r, c, rows, block)
        got = timed(prog)
        y = (
            np.frombuffer(card.get(dst, x.nbytes), np.float16)
            .astype(np.float64)
            .reshape(r, c)
        )
        grade(f"softmax {r}x{c}", y, e / e.sum(1, keepdims=True))
        print(f"softmax {r}x{c}: {got} cycles", flush=True)
    report(card.close())
    return 0


if __name__ == "__main__":
    sys.exit(main())
