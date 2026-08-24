"""Vectors for `khs_float_lane_tb`: a, b, c and what the model says a*b+c is.

    python tests/pe/tools/khs_float_vec.py <out.txt> [count]

The expected column comes from `rv_simd_f16.e8_fma`, which is written from the
definition -- multiply exactly, add exactly, round once -- and never from a
pipeline. So the bench is not checking that the RTL matches a transcription of
itself; it is checking that a fourteen-stage float lane computes the correctly
rounded answer.

The stream is deliberately unkind: ordinary values, then the cases float
datapaths get wrong -- zeros of both signs, the largest and smallest normals,
subnormal FP16 inputs (which convert to ORDINARY E8M15 values, so they must not
be special-cased anywhere), and operands whose exponents are far enough apart
that the addend falls off the end of the alignment window in both directions.
"""

import pathlib
import random
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import rv_simd_f16 as F                                           # noqa: E402

EDGE_F16 = [0x0000, 0x8000, 0x3C00, 0xBC00, 0x7BFF, 0xFBFF,
            0x0400, 0x8400, 0x0001, 0x8001, 0x03FF, 0x3555]


def gen(n, seed=0x5F16):
    rng = random.Random(seed)
    out = []

    # Every edge against every edge, with the addend at zero, one and an
    # ordinary value: the corners are where a pipeline and a definition part.
    for ea in EDGE_F16:
        for eb in EDGE_F16:
            for c16 in (0x0000, 0x3C00, 0x4900):
                out.append((ea, eb, F.f16_to_e8(c16)))

    # Exponent-distant addends: `c` far above and far below the product, which
    # is where the alignment window clamps at both ends.
    for _ in range(600):
        a = rng.randrange(1 << 16)
        b = rng.randrange(1 << 16)
        big = ((rng.randrange(0x60, 0xFE) << 15) | rng.getrandbits(15)) & 0x7FFFFF
        sml = ((rng.randrange(0x01, 0x30) << 15) | rng.getrandbits(15)) & 0x7FFFFF
        out.append((a, b, big | (rng.getrandbits(1) << 23)))
        out.append((a, b, sml | (rng.getrandbits(1) << 23)))

    while len(out) < n:
        a = rng.randrange(1 << 16)
        b = rng.randrange(1 << 16)
        c = F.f16_to_e8(rng.randrange(1 << 16))
        out.append((a, b, c))

    return out[:n]


def main():
    path = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "khs_float_vec.txt")
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 4000
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = gen(n)
    off = 0
    with path.open("w") as fh:
        for a, b, c in rows:
            ae, be = F.f16_to_e8(a), F.f16_to_e8(b)
            y = F.e8_fma_hw(ae, be, c)
            if y != F.e8_fma(ae, be, c):
                off += 1
            fh.write("%04x %04x %06x %06x\n" % (a, b, c, y))
    print("  wrote %d vectors to %s" % (len(rows), path))
    print("  %d of them (%.2f%%) are one ulp off correct rounding -- the lane's "
          "subtractive sticky" % (off, 100.0 * off / max(1, len(rows))))


if __name__ == "__main__":
    main()
