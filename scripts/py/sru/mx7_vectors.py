"""Vectors for tests/sru/stream/sru_mx7_tb.v, from the MXFP7 spec
(kohakutpu.language.numerics.mxfp7, the model mx_quant.v is checked against).

    python scripts/py/sru/mx7_vectors.py

build/sru/mx7_in.hex: one sub-tile word a line, `first word` (FP16 lane 4r + c
at bits [16(4r+c) +: 16]). build/sru/mx7_a.hex, mx7_b.hex: the out words of
each entry in order, A and B layout.
"""

import argparse
import pathlib

import numpy as np
from kohakutpu.language.numerics import mxfp7

ROOT = pathlib.Path(__file__).resolve().parents[3]


def rows(rng, n):
    """n x 32 values, awkward on purpose (run_quant_check.py's mix)."""
    x = rng.standard_normal((n, 32)) * (10.0 ** rng.integers(-7, 5, (n, 1)))
    k = rng.random((n, 32))
    x[k < 0.03] = 0.0
    x[(k > 0.03) & (k < 0.05)] = 65504.0
    x[(k > 0.05) & (k < 0.06)] = 6.0e-8
    return x.astype(np.float16)


def pack(q, fields, b_layout):
    """4 out words of one entry: q[4, 32] int, fields[4]."""
    words = []
    for w in range(4):
        word = 0
        for slot in range(32):
            lane, kk = (slot % 4, slot // 4) if b_layout else (slot // 8, slot % 8)
            word |= (int(q[lane, w * 8 + kk]) & 0x7F) << (255 - slot * 7 - 6)
        for r in range(4):
            word |= int(fields[r]) << (31 - r * 8 - 7)
        words.append(word)
    return words


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--subrows", type=int, default=400)
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args()
    rng = np.random.default_rng(args.seed)

    ins, out_a, out_b = [], [], []
    for _ in range(args.subrows):
        n_ent = int(rng.choice([1, 1, 2, 3, 4]))
        for e in range(n_ent):
            h = rows(rng, 4)  # rows 0..3, K 32
            q, es, m8 = mxfp7.quantise_fp16(h.astype(np.float32))
            fields = (((es + mxfp7.SBIAS) << 3) | (m8 - 8)).reshape(4)
            bits = h.view(np.uint16)
            for w in range(8):
                word = 0
                for r in range(4):
                    for c in range(4):
                        word |= int(bits[r, 4 * w + c]) << (16 * (4 * r + c))
                ins.append(f"{int(e == 0 and w == 0)} {word:064x}")
            out_a += [f"{x:064x}" for x in pack(q, fields, False)]
            out_b += [f"{x:064x}" for x in pack(q, fields, True)]

    out = ROOT / "build" / "sru"
    out.mkdir(parents=True, exist_ok=True)
    (out / "mx7_in.hex").write_text("\n".join(ins) + "\n")
    (out / "mx7_a.hex").write_text("\n".join(out_a) + "\n")
    (out / "mx7_b.hex").write_text("\n".join(out_b) + "\n")
    print(f"{out} {len(ins)} words in, {len(out_a)} words out")


if __name__ == "__main__":
    main()
