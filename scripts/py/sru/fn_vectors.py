"""Tables and function vectors for tests/sru/lane/sru_plane_tb.v, from the
independent function-lane oracle (scripts/py/sru/oracle.py; ISA.SR.043,
docs/projects/kohakutpu/sru.md s4).

    python scripts/py/sru/fn_vectors.py

Writes to build/sru/: tab_<name>.hex (1,024 x 36-bit entries) and, one
vector a line `x k mode pre cs tab neg mrom want`, in blocks of 1,000:
fn_ram.hex, fn_rom.hex, and fn_nofn.hex. Does not touch `src/`.
"""

import argparse
import math
import pathlib
import random
from fractions import Fraction

import oracle

ROOT = pathlib.Path(__file__).resolve().parents[3]

ORDER = ["exp2", "sigmoid", "phi", "recip", "rsqrt", "sqrt"]
PRE = {"exp2": 0, "sigmoid": 0, "phi": 1, "recip": 0, "rsqrt": 0, "sqrt": 0}
MODE = {
    "exp2": (oracle.FN_EXP, 0),
    "sigmoid": (oracle.FN_FIX, 0),
    "phi": (oracle.FN_FIX, 0),
    "recip": (oracle.FN_MANT, 1),
    "rsqrt": (oracle.FN_HALF, 1),
    "sqrt": (oracle.FN_HALF, 0),
}


def word(s, e, m):
    return (s << 23) | (e << 15) | m


def x_word(rng, mode):
    k = rng.randrange(8)
    if k == 0:
        return rng.choice(
            [0, 0x800000, word(0, 255, 0), word(1, 255, 0), oracle.E8_NAN]
        )
    if mode >= oracle.FN_MANT:
        return word(rng.getrandbits(1), rng.randint(1, 254), rng.getrandbits(15))
    if mode == oracle.FN_FIX:
        e = rng.randint(110, 132)
    else:
        e = rng.randint(100, 136)
    return word(rng.getrandbits(1), e, rng.getrandbits(15))


def k_word(rng):
    k = rng.randrange(6)
    if k == 0:
        return word(rng.getrandbits(1), rng.randint(0, 140), rng.getrandbits(15))
    if k == 1:
        return rng.choice([word(0, 255, 0), word(1, 255, 0), oracle.E8_NAN])
    return word(rng.getrandbits(1), rng.randint(120, 134), rng.getrandbits(15))


def line(x, k, mode, pre, cs, tab, neg, mrom, want):
    return f"{x:06x} {k:06x} {mode:x} {pre & 0x3F:02x} {cs:x} {tab:x} {neg:x} {mrom:x} {want:06x}"


def mrom_vector(rng, rom):
    mode = rng.choice([oracle.FN_MANT, oracle.FN_HALF])
    x, k = x_word(rng, mode), k_word(rng)
    want = oracle.fn_eval(x, k, mode, 0, None, neg=1, mrom=rom)
    return line(x, k, mode, rng.randrange(64), rng.randrange(4), len(ORDER), 1, 1, want)


def worst(name, table, f, lo, hi, fix, rom=False, pre=0):
    rel = ab = 0.0
    for i in range(40000):
        u = lo + (hi - lo) * (i + 0.5) / 40000
        x = oracle.e8_encode(Fraction(u).limit_denominator(1 << 30))
        y = float(oracle.e8_value(oracle.fn_eval(x, 0, int(fix), pre, table, rom=rom)))
        ref = f(float(oracle.e8_value(x)))
        ab = max(ab, abs(y - ref))
        if abs(ref) >= 2.0**-8:
            rel = max(rel, abs(y - ref) / abs(ref))
    print(
        f"  {name:10s} rel (|f| >= 2^-8) 2^{math.log2(rel):.2f}  abs 2^{math.log2(ab):.2f}"
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=60000)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--out", default=str(ROOT / "build" / "sru"))
    args = ap.parse_args()

    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    tabs = oracle.fn_tables()
    for name, (ents, cs) in tabs.items():
        (out / f"tab_{name}.hex").write_text("".join(f"{v:09x}\n" for v in ents))
        print(f"  {name:10s} cs {cs}")
    rom = oracle.fn_exp2_table(128)
    assert rom[1] == 0, "the ROM lane has no slope scale"
    mrom = oracle.MROM_TABLE

    worst("exp2 1K", tabs["exp2"], lambda u: 2.0**u, -8, 8, False)
    worst("exp2 ROM", rom, lambda u: 2.0**u, -8, 8, False, rom=True)
    worst("sigmoid", tabs["sigmoid"], oracle.fn_sigmoid, -15.9, 15.9, True)
    worst("phi", tabs["phi"], oracle.fn_phi, -8, 7.9, True, pre=PRE["phi"])

    rng = random.Random(args.seed)
    lines = []
    for blk in range(args.n // 1000):
        idx = blk % (len(ORDER) + 1)
        for _ in range(1000):
            if idx == len(ORDER):
                lines.append(mrom_vector(rng, mrom))
                continue
            name = ORDER[idx]
            mode, neg = MODE[name]
            table = tabs[name]
            x, k = x_word(rng, mode), k_word(rng)
            pre = rng.choice([0, 0, 0, 1, -1, 3, -3])
            if mode >= oracle.FN_MANT:
                neg = rng.getrandbits(1)
            want = oracle.fn_eval(x, k, mode, pre, table, neg=neg)
            lines.append(line(x, k, mode, pre, table[1], idx, neg, 0, want))
    (out / "fn_ram.hex").write_text("\n".join(lines) + "\n")

    lines = []
    for blk in range(args.n // 1000):
        for _ in range(1000):
            if blk % 3 == 2:
                lines.append(mrom_vector(rng, mrom))
                continue
            x, k = x_word(rng, oracle.FN_EXP), k_word(rng)
            pre = rng.choice([0, 0, 0, 1, -1])
            want = oracle.fn_eval(x, k, oracle.FN_EXP, pre, rom, rom=True)
            lines.append(line(x, k, oracle.FN_EXP, pre, 0, 0, 0, 0, want))
    (out / "fn_rom.hex").write_text("\n".join(lines) + "\n")

    lines = [mrom_vector(rng, mrom) for _ in range(args.n // 3)]
    (out / "fn_nofn.hex").write_text("\n".join(lines) + "\n")
    print(f"{out} fn_ram.hex fn_rom.hex {args.n}, fn_nofn.hex {args.n // 3} vectors")


if __name__ == "__main__":
    main()
