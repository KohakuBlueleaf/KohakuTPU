"""FP16 <-> E8M15 vectors for the SRU's format benches.

    python scripts/py/sru/unpack_vectors.py

build/sru/unpack.hex and unpack_acc.hex: 4,096 lines `in want` each, every
FP16 bit pattern 16 to a 256-bit word and the 16 E8M15 they convert to,
exactly: IEEE (inf and NaN keep class and payload) and the clusters'
accumulator form (exponent 0 a signed zero, 1..31 ordinary binades), for
tests/sru/stream/sru_unpack_tb.v.

build/sru/e8f16_acc.hex: lines `in want`, 16 E8M15 and the accumulator FP16
mx_fpacc_to_fp16 would emit for them (round to nearest even at the value's
binade, a signed zero below 2^-14, +-65504 at 2^16 or above, inf and NaN
included): every mantissa of exponents 100..150 and a stride of the rest,
for tests/sru/stream/sru_e8f16_tb.v.
"""

import pathlib
import random
from fractions import Fraction

import e8

ROOT = pathlib.Path(__file__).resolve().parents[3]


def f16_to_e8(h):
    s, e, m = h >> 15, (h >> 10) & 0x1F, h & 0x3FF
    if e == 31:
        return (s << 23) | (0xFF << 15) | (m << 5)
    if e == 0:
        v = Fraction(m, 1 << 24)
    else:
        v = Fraction(0x400 + m, 1 << 10) * Fraction(2) ** (e - 15)
    return e8.encode(-v if s else v, s)


def f16_to_e8_acc(h):
    s, e, m = h >> 15, (h >> 10) & 0x1F, h & 0x3FF
    if e == 0:
        return s << 23
    v = Fraction(0x400 + m, 1 << 10) * Fraction(2) ** (e - 15)
    return e8.encode(-v if s else v, s)


def e8_to_f16_acc(w):
    s, e, _ = e8.fields(w)
    if e == 0:
        return s << 15
    if e == 255:
        return (s << 15) | 0x7BFF
    a = abs(e8.value(w))
    # 11-bit significand at the value's binade, nearest even
    big = a.numerator.bit_length() - a.denominator.bit_length()
    if Fraction(2) ** big > a:
        big -= 1
    q = a / Fraction(2) ** (big - 10)
    n, r = divmod(q.numerator, q.denominator)
    if 2 * r > q.denominator or (2 * r == q.denominator and n & 1):
        n += 1
    if n == 2048:
        n, big = 1024, big + 1
    if big < -14:
        return s << 15
    if big > 15:
        return (s << 15) | 0x7BFF
    return (s << 15) | ((big + 15) << 10) | (n - 1024)


def words(conv, width_in, width_out, values):
    lines = []
    for k in range(0, len(values), 16):
        wi = wo = 0
        for j, v in enumerate(values[k : k + 16]):
            wi |= v << (width_in * j)
            wo |= conv(v) << (width_out * j)
        lines.append(f"{wi:0{width_in * 4}x} {wo:0{width_out * 4}x}")
    return lines


def main():
    out = ROOT / "build" / "sru"
    out.mkdir(parents=True, exist_ok=True)
    every = list(range(1 << 16))
    (out / "unpack.hex").write_text("\n".join(words(f16_to_e8, 16, 24, every)) + "\n")
    (out / "unpack_acc.hex").write_text(
        "\n".join(words(f16_to_e8_acc, 16, 24, every)) + "\n"
    )

    rng = random.Random(3)
    vals = []
    for e in range(256):
        ms = range(1 << 15) if 100 <= e <= 150 else range(rng.randrange(97), 1 << 15, 97)
        for m in ms:
            vals.append((rng.getrandbits(1) << 23) | (e << 15) | m)
    vals += [0] * (-len(vals) % 16)
    (out / "e8f16_acc.hex").write_text(
        "\n".join(words(e8_to_f16_acc, 24, 16, vals)) + "\n"
    )
    print(f"{out} unpack.hex unpack_acc.hex 65536 values, e8f16_acc.hex {len(vals)}")


if __name__ == "__main__":
    main()
