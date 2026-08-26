"""Vectors for `khs_facc_tb` and `khs_ffold_tb`, in binary32.

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

import khs_fp32 as K


def ops(n, seed=0xFACC):
    """`n` (a, b) binary32 pairs, exponents banded so no product overflows."""
    rng = random.Random(seed)
    out = []
    for _ in range(n):
        # e8 in [117, 137] is 2^-10 .. 2^10, so 200 products of them stay far
        # inside binary32's range and the accumulation never saturates.
        a = (
            (rng.getrandbits(1) << 31)
            | (rng.randrange(117, 138) << 23)
            | rng.getrandbits(23)
        )
        b = (
            (rng.getrandbits(1) << 31)
            | (rng.randrange(117, 138) << 23)
            | rng.getrandbits(23)
        )
        out.append((a, b))
    return out


def deviation(pairs, npart):
    """Accumulate steps where the lane differs from correct rounding.

    The lane carries a plain sticky through a subtractive alignment, so it can
    be one ulp high. The number a user needs is how often that happens on
    ordinary magnitudes, which is this one.
    """
    part_hw = [0] * npart
    part_ex = [0] * npart
    off = 0
    for i, (a, b) in enumerate(pairs):
        k = i % npart
        hw = K.fpu(K.OP_FMA, a, b, part_hw[k])[0]
        ex = K.fma_exact(a, b, part_ex[k])
        if hw != ex:
            off += 1
        part_hw[k] = hw
        part_ex[k] = ex
    return off


def partials(pairs, npart):
    """What the rotating accumulator holds after the stream, partial by partial.

    `K.fpu` and not `K.fma_exact`: the RTL is the machine, and the deviation
    between the machine and the definition is measured above, not graded here.
    """
    part = [0] * npart
    for i, (a, b) in enumerate(pairs):
        k = i % npart
        part[k] = K.fpu(K.OP_FMA, a, b, part[k])[0]
    return part


def fold(part):
    """Partial 0 first, then 1, and so on -- the order is the ISA's.

    Float addition does not associate, so the loop direction is contract and
    `khs_ffold` walks it the same way.
    """
    total = 0
    for p in part:
        total = K.fpu(K.OP_FMA, p, K.F32_ONE, total)[0]
    return total


#: ABSOLUTE: a relative default writes into the caller's cwd, not the build.
DEFAULT_DIR = pathlib.Path(__file__).resolve().parents[1] / "build" / "khd" / "fp32"


def main():
    op_path = (
        pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DIR / "facc_ops.txt"
    )
    ex_path = (
        pathlib.Path(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_DIR / "facc_exp.txt"
    )
    nops = int(sys.argv[3]) if len(sys.argv) > 3 else 200
    npart = int(sys.argv[4]) if len(sys.argv) > 4 else 16

    op_path.parent.mkdir(parents=True, exist_ok=True)
    pairs = ops(nops)
    part = partials(pairs, npart)
    tot = fold(part)

    with op_path.open("w") as fh:
        for a, b in pairs:
            fh.write("%08x %08x\n" % (a, b))
    with ex_path.open("w") as fh:
        for p in part:
            fh.write("%08x\n" % p)
        fh.write("%08x\n" % tot)

    print("  wrote %d ops to %s" % (len(pairs), op_path))
    print("  wrote %d partials + the fold to %s" % (npart, ex_path))
    off = deviation(pairs, npart)
    print(
        "  %d of %d accumulate steps (%.2f%%) differ from correct rounding"
        % (off, len(pairs), 100.0 * off / max(1, len(pairs)))
    )


if __name__ == "__main__":
    main()
