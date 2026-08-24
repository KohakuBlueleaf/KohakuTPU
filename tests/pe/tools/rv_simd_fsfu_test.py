"""The four seeds' SPECIALS, against the table the RTL is pinned to.

    python tests/pe/tools/rv_simd_fsfu_test.py

WHY THIS EXISTS. `rv_simd_model.fsfu_e8` and `tests/vector/vec_alu_tb.v`
section 9 describe the same fourteen cases, in two languages, and nothing made
them agree. They did not: the model ended in `max(min(y, 3.4e38), -3.4e38)`,
which turned every infinity and NaN into 0x7f7fca00 -- so the model could not
produce an infinity for any input, and `rsqrt(-1)` came out a large finite where
the RTL correctly returns NaN. Four of khs_gen's 62 float cases failed on that
and the RTL was right every time.

Section 9 is the authority because it is compared EXACTLY and it runs against the
hardware. This asserts the model reproduces it case for case. If the two ever
disagree again, one of them is wrong, and today says it will be the model.

The FINITE path is NOT checked here: vec_alu computes it from a 32-segment table
plus a range reduction, so the model is a float64 reference and the comparison is
by tolerance. That belongs where the tolerance is stated, not in an exactness
gate -- which is the whole reason these two halves are separated.
"""

import math
import pathlib
import struct
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import rv_simd_f16 as F                                          # noqa: E402
from rv_simd_model import fsfu_e8                                # noqa: E402

E8_ONE = F.f32_to_e8(0x3F800000)
E8_INF = F.f32_to_e8(0x7F800000)
E8_NINF = F.f32_to_e8(0xFF800000)
E8_ZERO, E8_NZERO = 0x000000, 0x800000


def e8_of(x):
    return F.f32_to_e8(struct.unpack("<I", struct.pack("<f", x))[0])


def is_nan(e8):
    return ((e8 >> 15) & 0xFF) == 0xFF and (e8 & 0x7FFF) != 0


#: (op, input, expected, what) -- transcribed from vec_alu_tb.v lines 470-483.
#: `NAN` is a marker: any quiet NaN passes, since the payload is not architecture.
NAN = "NAN"
SECTION_9 = [
    ("vfexp2",  E8_INF,        E8_INF,  "exp2(+inf) = +inf"),
    ("vfexp2",  E8_NINF,       E8_ZERO, "exp2(-inf) = 0"),
    ("vfexp2",  e8_of(500.0),  E8_INF,  "exp2(500) overflows to +inf"),
    ("vfexp2",  e8_of(-500.0), E8_ZERO, "exp2(-500) underflows to 0"),
    ("vfexp2",  E8_ZERO,       E8_ONE,  "exp2(0) = 1"),
    ("vflog2",  E8_ZERO,       E8_NINF, "log2(0) = -inf"),
    ("vflog2",  e8_of(-2.0),   NAN,     "log2 of a negative is NaN"),
    ("vflog2",  E8_INF,        E8_INF,  "log2(+inf) = +inf"),
    ("vfrcp",   E8_ZERO,       E8_INF,  "1/+0 = +inf"),
    ("vfrcp",   E8_NZERO,      E8_NINF, "1/-0 = -inf, the sign survives"),
    ("vfrcp",   E8_INF,        E8_ZERO, "1/inf = 0"),
    ("vfrsqrt", E8_ZERO,       E8_INF,  "rsqrt(0) = +inf"),
    ("vfrsqrt", e8_of(-4.0),   NAN,     "rsqrt of a negative is NaN"),
    ("vfrsqrt", E8_INF,        E8_ZERO, "rsqrt(inf) = 0"),
]

#: Not in section 9, but they follow from the same specials and are exactly the
#: shape the old clamp got wrong -- a NaN in must not become a finite.
EXTRA = [
    ("vfexp2",  F.E8_NAN, NAN,     "exp2(NaN) = NaN, not a large finite"),
    ("vflog2",  F.E8_NAN, NAN,     "log2(NaN) = NaN"),
    ("vfrcp",   F.E8_NAN, NAN,     "rcp(NaN) = NaN"),
    ("vfrsqrt", F.E8_NAN, NAN,     "rsqrt(NaN) = NaN"),
    ("vflog2",  E8_NZERO, E8_NINF, "log2(-0) = -inf, not NaN"),
    # DERIVED, not transcribed. This said `E8_INF, "rsqrt(-0) = +inf, not NaN"`
    # -- checked carefully along the inf-versus-NaN axis and never asked about
    # the SIGN, so it pinned vec_alu's defect as the specification.
    ("vfrsqrt", E8_NZERO, E8_NINF, "rsqrt(-0) = -inf, the sign survives"),
    ("vflog2",  E8_NINF,  NAN,     "log2(-inf) is NaN"),
    ("vfrsqrt", E8_NINF,  NAN,     "rsqrt(-inf) is NaN"),
    # NOT in section 9, which tests inv(+inf) only -- and the fix for the clamp
    # got it wrong on the first pass by returning a bare +0 for either infinity.
    ("vfrcp",   E8_NINF,  E8_NZERO, "1/-inf = -0, the sign survives"),
    ("vfrcp",   E8_INF,   E8_ZERO,  "1/+inf = +0"),
    ("vfexp2",  E8_NZERO, E8_ONE,   "exp2(-0) = 1"),
]


def main():
    checks = errors = 0
    for label, table in (("vec_alu_tb section 9", SECTION_9),
                         ("the same specials, extended", EXTRA)):
        print("--- %s ---" % label)
        for op, a, want, what in table:
            got = fsfu_e8(op, a)
            checks += 1
            ok = is_nan(got) if want is NAN else (got == want)
            if not ok:
                errors += 1
                shown = "NaN" if want is NAN else "%06x" % want
                print("  FAIL %-40s got %06x want %s" % (what, got, shown))

    # The clamp's fingerprint. 0x7f7fca00 is 3.4e38 through E8 and back -- it is
    # a CONSTANT, not an answer, so no input may ever produce it.
    print("--- no input produces the old clamp constant ---")
    clamp = F.f32_to_e8(struct.unpack("<I", struct.pack("<f", 3.4e38))[0])
    probes = [E8_ZERO, E8_NZERO, E8_INF, E8_NINF, F.E8_NAN, E8_ONE,
              e8_of(-1.0), e8_of(-4.0), e8_of(500.0), e8_of(-500.0),
              F.f32_to_e8(0x7F7FFFFF), F.f32_to_e8(0xFF7FFFFF)]
    for op in ("vfexp2", "vflog2", "vfrcp", "vfrsqrt"):
        for a in probes:
            got = fsfu_e8(op, a)
            checks += 1
            if got in (clamp, clamp | 0x800000):
                errors += 1
                print("  FAIL %s(%06x) returned the clamp constant %06x"
                      % (op, a, got))

    # A sanity floor on the finite path: it is toleranced, but it must at least
    # be the right VALUE to a few ulp, or the specials passing means nothing.
    print("--- the finite path is still approximately right ---")
    for op, ref in (("vfexp2", lambda t: 2.0 ** t),
                    ("vflog2", math.log2),
                    ("vfrcp", lambda t: 1.0 / t),
                    ("vfrsqrt", lambda t: 1.0 / math.sqrt(t))):
        for x in (0.5, 1.0, 1.5, 2.0, 3.0, 7.0, 100.0):
            got = fsfu_e8(op, e8_of(x))
            gv = struct.unpack("<f", struct.pack("<I", F.e8_to_f32(got)))[0]
            want = ref(x)
            checks += 1
            # log2(1) is exactly zero, so a RELATIVE bound has nothing to divide
            # by: an exact answer is the only acceptable one there.
            bad = (gv != 0.0) if want == 0.0 else \
                (abs(gv - want) / abs(want) > 1e-4)
            if bad:
                errors += 1
                print("  FAIL %s(%g) got %g want %g" % (op, x, gv, want))

    print("========================================")
    if errors == 0:
        print("  PASS -- %d checks, 0 errors" % checks)
    else:
        print("  FAIL -- %d checks, %d errors" % (checks, errors))
    print("========================================")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
