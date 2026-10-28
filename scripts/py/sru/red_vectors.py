"""Vectors for tests/sru/stream/sru_red_tb.v (KIND = "max"), from the
independent sign-magnitude max oracle (scripts/py/sru/oracle.py; ISA.SR.042,
ISA.SR.044, docs/projects/kohakutpu/sru.md s3, s5).

    python scripts/py/sru/red_vectors.py

build/sru/red_in.hex: one word a line, `first last w15 .. w0` (w0 = lane 0).
build/sru/red_max.hex: one sub-row a line, rows 3 .. 0, each row 58 bits
(the winning 24-bit E8M15 word).

Note: `red_sum.hex` (for `sru_red_sum`, which is not in the 33 tier-2 spec
vectors) checks the internal 58-bit `{SP[1:0], EM[7:0], A[47:0]}` bus prior to
`sru_sum2e8`; because that internal representation is not specified in `docs/`,
it is not generated here.
"""

import argparse
import pathlib
import random

import oracle

ROOT = pathlib.Path(__file__).resolve().parents[3]


def elem(rng, base, spread):
    k = rng.randrange(40)
    if k == 0:
        return rng.choice([0, 0x800000, 0x7F8000, 0xFF8000, oracle.E8_NAN, 0xFFC001])
    if k < 4:
        return rng.getrandbits(1) << 23
    e = max(1, min(254, base + rng.randint(-spread, spread)))
    return (rng.getrandbits(1) << 23) | (e << 15) | rng.getrandbits(15)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=6000)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--out", default=str(ROOT / "build" / "sru"))
    args = ap.parse_args()
    rng = random.Random(args.seed)

    words_out, max_out = [], []
    for _ in range(args.rows):
        n = rng.choice([1, 1, 2, 3, 4, 5, 8, 12, rng.randint(1, 40)])
        base = rng.randint(2, 252)
        spread = rng.choice([0, 1, 3, 8, 20, 40, 120])
        words = [[elem(rng, base, spread) for _ in range(16)] for _ in range(n)]
        for i, wd in enumerate(words):
            words_out.append(
                f"{int(i == 0)} {int(i == n - 1)} "
                + " ".join(f"{x:06x}" for x in reversed(wd))
            )
        mx = [oracle.row_max_e8(words, r) for r in range(4)]
        max_out.append(" ".join(f"{v:015x}" for v in reversed(mx)))

    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "red_in.hex").write_text("\n".join(words_out) + "\n")
    (out / "red_max.hex").write_text("\n".join(max_out) + "\n")
    print(f"{out} {len(words_out)} words, {args.rows} sub-rows")


if __name__ == "__main__":
    main()
