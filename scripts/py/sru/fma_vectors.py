"""Vectors for tests/sru/lane/sru_fma_tb.v: `a b c want` per line, hex.

    python scripts/py/sru/fma_vectors.py            # both lane kinds
    python scripts/py/sru/fma_vectors.py --n 200000

Writes build/sru/fma_add1.hex (a*b + c) and build/sru/fma_add0.hex (a*b).
The cases aim at every path of a single-rounding FMA: the whole alignment
range, cancellation, the addend-dominant edge, ties, the exponent bounds and
the specials.
"""

import argparse
import pathlib
import random

import e8

ROOT = pathlib.Path(__file__).resolve().parents[3]


def word(s, e, m):
    return (s << 23) | (e << 15) | m


def rnd_word(rng, elo=1, ehi=254):
    return word(rng.getrandbits(1), rng.randint(elo, ehi), rng.getrandbits(15))


def mant(rng):
    """A mantissa, biased toward the patterns rounding cares about."""
    k = rng.randrange(6)
    if k == 0:
        return 0
    if k == 1:
        return 0x7FFF
    if k == 2:
        return rng.getrandbits(15) & ~rng.getrandbits(15)
    return rng.getrandbits(15)


def special(rng):
    return rng.choice(
        [
            0x000000,
            0x800000,
            word(0, 255, 0),
            word(1, 255, 0),
            e8.NAN,
            word(1, 255, 1),
            word(0, 0, rng.getrandbits(15)),
            word(rng.getrandbits(1), 1, mant(rng)),
            word(rng.getrandbits(1), 254, mant(rng)),
            word(rng.getrandbits(1), 127, 0),
        ]
    )


def case(rng, add):
    k = rng.randrange(10)

    if k == 0:
        # anything, specials mixed in
        ops = [special(rng) if rng.random() < 0.3 else rnd_word(rng) for _ in range(3)]
        return ops

    ea = rng.randint(100, 154)
    eb = rng.randint(100, 154)
    a = word(rng.getrandbits(1), ea, mant(rng))
    b = word(rng.getrandbits(1), eb, mant(rng))
    if k == 1:
        b = word(rng.getrandbits(1), eb, 0)  # power of two: product exact

    # the addend's position, s = ea + eb - ec - 110 over the whole window
    s = rng.randint(-22, 60)
    ec = ea + eb - 110 - s
    if not 1 <= ec <= 254:
        ec = rng.randint(1, 254)
    c = word(rng.getrandbits(1), ec, mant(rng))

    if k == 2:
        # cancellation: c near -a*b
        p = e8.fma(a, b, 0, add=False)
        if not (e8.is_zero(p) or e8.is_inf(p)):
            ps, pe, pm = e8.fields(p)
            pm = (pm + rng.randint(-3, 3)) & 0x7FFF
            c = word(
                ps ^ 1, pe + rng.choice([0, 0, 1, -1]) if 2 <= pe <= 253 else pe, pm
            )
    elif k == 3:
        # addend-dominant edge: power-of-two addend, effective subtract, s = -1
        ec = ea + eb - 109
        if 1 <= ec <= 254:
            sp = (a >> 23) ^ (b >> 23)
            c = word(sp ^ 1, ec, 0)
    elif k == 4:
        # exponent bounds
        lo = rng.random() < 0.5
        ea = rng.randint(1, 70) if lo else rng.randint(190, 254)
        eb = rng.randint(1, 70) if lo else rng.randint(190, 254)
        a = word(rng.getrandbits(1), ea, mant(rng))
        b = word(rng.getrandbits(1), eb, mant(rng))
        ec = max(1, min(254, ea + eb - 110 - rng.randint(-20, 40)))
        c = word(rng.getrandbits(1), ec, mant(rng))
    elif k == 5:
        c = special(rng)
    elif k == 6:
        # addend partly below the window under an effective subtract: an exact
        # product (b a power of two) minus an addend with low bits set puts
        # ties right at the borrow
        b = word(rng.getrandbits(1), eb, 0)
        s = rng.randint(33, 48)
        ec = ea + eb - 110 - s
        if 1 <= ec <= 254:
            sp = (a >> 23) ^ (b >> 23)
            m = rng.choice([1, 3, 0x4001, 0x2001, rng.getrandbits(15) | 1])
            c = word(sp ^ 1, ec, m)
    elif k == 7:
        # signed zeros on both terms
        a = word(rng.getrandbits(1), 0, 0)
        c = word(rng.getrandbits(1), 0, 0)

    return [a, b, c]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=120000)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--out", default=str(ROOT / "build" / "sru"))
    args = ap.parse_args()

    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    for add in (1, 0):
        rng = random.Random(args.seed * 2 + add)
        lines = []
        for _ in range(args.n):
            a, b, c = case(rng, add)
            want = e8.fma(a, b, c, add=bool(add))
            lines.append(f"{a:06x} {b:06x} {c:06x} {want:06x}")
        path = out / f"fma_add{add}.hex"
        path.write_text("\n".join(lines) + "\n")
        print(f"{path} {len(lines)} vectors")


if __name__ == "__main__":
    main()
