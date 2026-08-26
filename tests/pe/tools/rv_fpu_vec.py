"""Vectors for `rv_fpu_tb`: op, a, b, c and the binary32 word the model says.

    python tests/pe/tools/rv_fpu_vec.py <outdir> [count]

WAS NOT IN THE TREE. The bench shipped with hand-made vectors in
`tests/pe/build/fpu`, so a fresh clone could not build them and nothing said
when they went stale -- the failure mode the DSP vectors already record.

34 HEX CHARS = 136 BITS. `$readmemh` right-aligns, so the bench's register is
exactly {op[7:0], a, b, c, want} and a wider one silently shifts every field.
"""

import pathlib
import random
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import khs_fp32 as K

#: Every opcode the unit carries. Compares included: their answer is 1.0 or 0.0
#: and the specials must NOT outrank them.
OPS = list(range(14))

EDGE = [
    0x0000_0000,  # +0
    0x8000_0000,  # -0
    0x3F80_0000,  # 1.0
    0xBF80_0000,  # -1.0
    0x7F7F_FFFF,  # the largest finite
    0xFF7F_FFFF,
    0x0080_0000,  # the smallest normal
    0x8080_0000,
    0x0000_0001,  # a denormal, which FLUSHES
    0x8000_0001,
    0x7F80_0000,  # +inf
    0xFF80_0000,  # -inf
    0x7FC0_0000,  # a quiet NaN
    0x7FA0_0000,  # a signalling NaN
    0x4049_0FDB,  # pi
    0x3333_3333,
    0x4B80_0000,  # 2^24, where the addend leaves the window
    0x3380_0000,  # 2^-24
]


def gen(n, seed=0x1F32):
    rng = random.Random(seed)
    out = []

    # Every edge against every edge, on the opcodes that read three operands.
    for a in EDGE:
        for b in EDGE:
            for c in (0x0000_0000, 0x3F80_0000, 0xC120_0000):
                out.append((K.OP_FMA, a, b, c))
    for a in EDGE:
        for b in EDGE:
            for op in OPS:
                out.append((op, a, b, 0x3F80_0000))

    # Exponent-distant addends: `c` far above and far below the product, which
    # is where the alignment window clamps at both ends.
    for _ in range(1500):
        a = rng.getrandbits(32)
        b = rng.getrandbits(32)
        big = (rng.randrange(0xC0, 0xFE) << 23) | rng.getrandbits(23)
        sml = (rng.randrange(0x01, 0x40) << 23) | rng.getrandbits(23)
        out.append((K.OP_FMA, a, b, big | (rng.getrandbits(1) << 31)))
        out.append((K.OP_FMA, a, b, sml | (rng.getrandbits(1) << 31)))

    # Ordinary magnitudes, where a shader lives.
    while len(out) < n:
        op = OPS[rng.randrange(len(OPS))]
        vals = [
            (rng.getrandbits(1) << 31)
            | (rng.randrange(0x60, 0xA0) << 23)
            | rng.getrandbits(23)
            for _ in range(3)
        ]
        out.append((op, vals[0], vals[1], vals[2]))
    return out[:n]


#: ABSOLUTE: as a relative "fpu" the default wrote into the caller's cwd.
DEFAULT_OUT = pathlib.Path(__file__).resolve().parents[1] / "build" / "fpu"


def main():
    out = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUT
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 10000
    out.mkdir(parents=True, exist_ok=True)

    rows = gen(n)
    off = 0
    with (out / "fp32.hex").open("w") as fh:
        for op, a, b, c in rows:
            y = K.fpu(op, a, b, c)[0]
            if op == K.OP_FMA and y != K.fma_exact(a, b, c):
                off += 1
            fh.write("%02x%08x%08x%08x%08x\n" % (op, a, b, c, y))
    (out / "fp32n.hex").write_text("%08x\n" % len(rows), encoding="utf-8")

    print("  wrote %d vectors to %s" % (len(rows), out))
    print(
        "  %d of them are one ulp off correct rounding -- the uncomplemented "
        "sticky on a subtractive alignment" % off
    )


if __name__ == "__main__":
    main()
