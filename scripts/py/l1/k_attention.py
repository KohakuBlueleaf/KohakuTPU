"""Card run of the L1 flash-attention key blocks (`kohakutpu.ir.l1.kernels.attention`).

    python scripts/py/l1/k_attention.py --build build/v9prof/vlt_card_v9_1n --gm 8 --blocks 1,2,4,8

One query block against `blocks` 64-key blocks; the cost of a key block is the
difference between block counts, so the fixed package and init/final RUNs
cancel. Graded against the kernel's own arithmetic (`flash_reference`) and
reported against exact fp64.
"""

import argparse
import pathlib
import sys
from itertools import pairwise

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(
    0, str(pathlib.Path(__file__).resolve().parents[3] / "compiler/tests/l1")
)
from card import L1Card, report
from check import grade
from kohakutpu.ir.l1 import Program
from kohakutpu.ir.l1.kernels import attention as AT
from kohakutpu.ir.l1.kernels import matmul as MM
from test_kernels import flash_reference


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", required=True)
    ap.add_argument("--gm", type=int, default=8)
    ap.add_argument("--blocks", default="1,2,4,8")
    a = ap.parse_args()
    card = L1Card(a.build)
    gm = a.gm
    rows = 4 * gm
    rng = np.random.default_rng(1)
    got_cycles = {}
    for blocks in (int(v) for v in a.blocks.split(",")):
        keys = 64 * blocks
        q = (rng.standard_normal((rows, 64)) * np.log2(np.e) / 8 * 3).astype(np.float16)
        k = rng.standard_normal((keys, 64)).astype(np.float16)
        v = rng.standard_normal((keys, 64)).astype(np.float16)
        card.top = 0x0010_0000
        q_at = card.put(np.frombuffer(MM.pack_a(q, gm, 2), np.uint8))
        kb = b"".join(MM.pack_b(k[j * 64 : (j + 1) * 64], 16, 2) for j in range(blocks))
        vb = b"".join(
            MM.pack_b(np.ascontiguousarray(v[j * 64 : (j + 1) * 64].T), 16, 2)
            for j in range(blocks)
        )
        k_at = card.put(np.frombuffer(kb, np.uint8))
        v_at = card.put(np.frombuffer(vb, np.uint8))
        o_at = card.alloc(gm * 16 * 32)
        scratch = card.alloc(4 * gm * 16 * 32)
        idx_at = card.put(AT.index_words())
        prog = Program(card.machine)
        AT.attention(prog, q_at, k_at, v_at, o_at, scratch, idx_at, gm, blocks)
        card.run(prog)
        cycles = card.run(prog)["cycles"]
        got_cycles[blocks] = cycles
        got = MM.unpack(card.get, [(0, gm, 0, o_at)], rows, 64, gm, 16)
        s = q.astype(np.float64) @ k.astype(np.float64).T
        p = np.exp2(s - s.max(1, keepdims=True))
        exact = (p / p.sum(1, keepdims=True)) @ v.astype(np.float64)
        grade(
            f"attention {rows}x{keys}",
            got,
            flash_reference(q, k, v, blocks),
            exact,
            # MEASURED: VEXP2 within 0.35% of exp2 can put a P element on the
            # other side of an MXFP7 rounding step (card row 14: 0.0088043 vs
            # 0.0087738 at a half step of 0.0088), moving O by a step times |v|.
            tol=2e-2,
        )
        print(
            f"attention q {rows} rows x {blocks} key blocks: {cycles} cycles",
            flush=True,
        )
    ns = sorted(got_cycles)
    for lo, hi in pairwise(ns):
        per = (got_cycles[hi] - got_cycles[lo]) / (hi - lo)
        print(f"  a key block ({lo}->{hi}): {per:.0f} cycles")
    report(card.close())
    return 0


if __name__ == "__main__":
    sys.exit(main())
