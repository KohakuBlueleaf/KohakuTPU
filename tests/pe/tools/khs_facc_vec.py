"""Vectors for `khs_facc_tb` and `khs_ffold_tb`.

    python tests/pe/tools/khs_facc_vec.py <ops.txt> <exp.txt> [nops] [npart]

TWO FILES, ONE RECORD SHAPE EACH. `$fscanf("%h %h")` treats a newline as
ordinary whitespace, so a single-value expectation line following the operand
pairs is read AS an operand pair -- the operand loop runs past the end and
every expectation comes back X.
"""

import pathlib
import random
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import rv_simd_f16 as F                                           # noqa: E402

#: E8M15 1.0 -- the fold multiplies a partial by it, so the fold and the
#: accumulate share one lane and one rounding.
E8_ONE = (127 << 15)


def ops(n, seed=0xFACC):
    """`n` (a, b) FP16 pairs, exponents banded so no product overflows."""
    rng = random.Random(seed)
    out = []
    for _ in range(n):
        # e5 in [9, 20] is 2^-6 .. 2^5, so 200 products of them stay far inside
        # E8M15's range and the accumulation never saturates.
        a = (rng.getrandbits(1) << 15) | (rng.randrange(9, 21) << 10) \
            | rng.getrandbits(10)
        b = (rng.getrandbits(1) << 15) | (rng.randrange(9, 21) << 10) \
            | rng.getrandbits(10)
        out.append((a, b))
    return out


def deviation(pairs, npart):
    """Accumulate steps where the lane differs from correct rounding.

    The lane carries a plain sticky through a subtractive alignment, so it can
    be one ulp high; the adversarial lane stream oversamples exactly that case.
    On ordinary magnitudes the alignment rarely reaches the end of the window,
    and the number a user needs is this one, not that one.
    """
    part_hw = [0] * npart
    part_ex = [0] * npart
    off = 0
    for i, (a, b) in enumerate(pairs):
        k = i % npart
        ae, be = F.f16_to_e8(a), F.f16_to_e8(b)
        if F.e8_fma_hw(ae, be, part_hw[k]) != F.e8_fma(ae, be, part_ex[k]):
            off += 1
        part_hw[k] = F.e8_fma_hw(ae, be, part_hw[k])
        part_ex[k] = F.e8_fma(ae, be, part_ex[k])
    return off


def partials(pairs, npart):
    """What the rotating accumulator holds after the stream, partial by partial.

    `e8_fma_hw` and not `e8_fma`: the RTL is the machine, and the deviation
    between the machine and the definition is the lane bench's measurement, not
    this one's. Mixing the two here would fail the accumulator for the lane's
    rounding.
    """
    part = [0] * npart
    for i, (a, b) in enumerate(pairs):
        k = i % npart
        part[k] = F.e8_fma_hw(F.f16_to_e8(a), F.f16_to_e8(b), part[k])
    return part


def fold(part):
    """Partial 0 first, then 1, and so on -- the order is the ISA's.

    Float addition does not associate, so the loop direction is contract and
    `khs_ffold` walks it the same way.
    """
    total = 0
    for p in part:
        total = F.e8_fma_hw(p, E8_ONE, total)
    return total


def main():
    op_path = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "khs_facc_ops.txt")
    ex_path = pathlib.Path(sys.argv[2] if len(sys.argv) > 2 else "khs_facc_exp.txt")
    nops = int(sys.argv[3]) if len(sys.argv) > 3 else 200
    npart = int(sys.argv[4]) if len(sys.argv) > 4 else 16

    op_path.parent.mkdir(parents=True, exist_ok=True)
    pairs = ops(nops)
    part = partials(pairs, npart)
    tot = fold(part)

    with op_path.open("w") as fh:
        for a, b in pairs:
            fh.write("%04x %04x\n" % (a, b))
    with ex_path.open("w") as fh:
        for p in part:
            fh.write("%06x\n" % p)
        fh.write("%06x\n" % tot)

    print("  wrote %d ops to %s" % (len(pairs), op_path))
    print("  wrote %d partials + the fold to %s" % (npart, ex_path))
    off = deviation(pairs, npart)
    print("  %d of %d accumulate steps (%.2f%%) differ from correct rounding"
          % (off, len(pairs), 100.0 * off / max(1, len(pairs))))


if __name__ == "__main__":
    main()
