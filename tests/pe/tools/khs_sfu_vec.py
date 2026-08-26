"""Vectors for `khs_fp32_sfu_tb`: fsel, a, and the word the model says.

    python tests/pe/tools/khs_sfu_vec.py <outdir> [count]

18 HEX CHARS = 72 BITS. `$readmemh` right-aligns, so the bench's register is
exactly {6'b0, fsel, a, want}.

The stream is deliberately unkind: every special each function defines, the
segment ORIGINS (where C0 alone answers and the identity cases must be exact),
the segment ENDS, and then ordinary magnitudes.
"""

import pathlib
import random
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import khs_fp32 as K
import khs_seed_tab as T

SPECIAL = [
    0x0000_0000,  # +0
    0x8000_0000,  # -0
    0x0000_0001,  # a denormal, which FLUSHES to zero
    0x8000_0001,
    0x7F80_0000,  # +inf
    0xFF80_0000,  # -inf
    0x7FC0_0000,  # a quiet NaN
    0x7FA0_0000,  # a signalling NaN
    0x3F80_0000,  # 1.0 -- exp2(1)=2, log2(1)=0, rcp(1)=1, rsqrt(1)=1
    0xBF80_0000,
    0x4000_0000,  # 2.0
    0x4080_0000,  # 4.0
    0x0080_0000,  # the smallest normal
    0x7F7F_FFFF,  # the largest finite
    0xFF7F_FFFF,
    0x4300_0000,  # 128.0 -- exp2 overflows at exactly this exponent
    0xC300_0000,
    0x42FF_FFFF,
    0x437F_FFFF,
]


def gen(n, seed=0x5EED):
    rng = random.Random(seed)
    out = []

    for fsel in range(4):
        for a in SPECIAL:
            out.append((fsel, a))

        # THE SEGMENT ORIGINS. C0 alone answers there, so a wrong C0 or a wrong
        # index shows as an exact mismatch rather than a low-bit one.
        for idx in range(0, 256, 8):
            if fsel == K.SEED_EXP2:
                # frac = idx/256 with the integer part at 0, 1 and -1.
                for k in (0, 1, -1):
                    out.append((fsel, K.bits(k + idx / 256.0)))
            else:
                out.append((fsel, (127 << 23) | (idx << 15)))
                out.append((fsel, (128 << 23) | (idx << 15)))

        # The segment ENDS, where the quadratic is furthest from its origin.
        for idx in range(0, 256, 8):
            if fsel != K.SEED_EXP2:
                out.append((fsel, (127 << 23) | (idx << 15) | 0x7FFF))

    # Ordinary magnitudes, where a shader lives.
    while len(out) < n:
        fsel = rng.randrange(4)
        if fsel == K.SEED_EXP2:
            a = K.bits(rng.uniform(-40.0, 40.0))
        elif fsel == K.SEED_LOG2:
            a = (rng.randrange(0x40, 0xC0) << 23) | rng.getrandbits(23)
        else:
            a = (rng.randrange(0x30, 0xD0) << 23) | rng.getrandbits(23)
            if fsel == K.SEED_INV:
                a |= rng.getrandbits(1) << 31
        out.append((fsel, a & 0xFFFF_FFFF))
    return out[:n]


#: ABSOLUTE, so the default cannot depend on the caller's cwd -- as a relative
#: "fpu" it wrote the vectors into whatever directory invoked it, and running
#: from the repo root left sfu32.hex there.
DEFAULT_OUT = pathlib.Path(__file__).resolve().parents[1] / "build" / "fpu"


def main():
    out = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUT
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 8000
    out.mkdir(parents=True, exist_ok=True)

    rows = gen(n)
    with (out / "sfu32.hex").open("w") as fh:
        for fsel, a in rows:
            fh.write("%02x%08x%08x\n" % (fsel, a, K.seed(fsel, a, T.TAB)))
    (out / "sfu32n.hex").write_text("%08x\n" % len(rows), encoding="utf-8")
    print("  wrote %d vectors to %s" % (len(rows), out))


if __name__ == "__main__":
    main()
