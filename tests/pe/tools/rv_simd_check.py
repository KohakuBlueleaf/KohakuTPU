"""Check each workload against an INDEPENDENT Python implementation.

    python tests/pe/tools/rv_simd_check.py

The golden model executing the suite's assembly proves the assembly is
self-consistent. It does not prove the assembly computes a dot product. This
recomputes every kernel's answer straight from the xorshift stream in Python and
compares against the scratchpad the model left behind, so a kernel that is
subtly wrong -- a tap offset, a sign extension, a saturation bound -- fails here
rather than silently becoming the denominator of every later speedup claim.
"""

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import rv_simd_asm  # noqa: F401
import rv_simd_kernels as K
from rv_asm import assemble
from rv_gen import MASK32, SYMS, zero_regs
from rv_simd_model import VSPAD_BASE, DspMachine

SPAD_WORDS = 2048
SIMD = 8
VSPAD_ENTRIES = 1024
ALL_SYMS = dict(SYMS, VSPAD=VSPAD_BASE)


def xorshift_stream(seed, n):
    x = seed
    out = []
    for _ in range(n):
        x ^= (x << 13) & MASK32
        x ^= x >> 17
        x ^= (x << 5) & MASK32
        x &= MASK32
        out.append(x)
    return out


def s8(v):
    v &= 0xFF
    return v - 256 if v & 0x80 else v


def s16(v):
    v &= 0xFFFF
    return v - 65536 if v & 0x8000 else v


def s32(v):
    v &= MASK32
    return v - (1 << 32) if v & 0x8000_0000 else v


def bytes_of(words):
    out = []
    for w in words:
        for b in range(4):
            out.append((w >> (8 * b)) & 0xFF)
    return out


def halves_of(words):
    out = []
    for w in words:
        out.append(w & 0xFFFF)
        out.append((w >> 16) & 0xFFFF)
    return out


def run(name):
    K.SIMD = SIMD
    src = zero_regs() + K.build_case(name)
    words, _ = assemble(src, base=0, symbols=ALL_SYMS)
    m = DspMachine(
        simd=SIMD,
        vspad_entries=VSPAD_ENTRIES,
        imem_words=2048,
        spad_words=SPAD_WORDS,
        arg=0,
        coreid=0x11,
    )
    m.imem[: len(words)] = words
    m.run(limit=4_000_000)
    return m


def spad_words(m, off, n):
    return [m.spad[(off // 4) + i] for i in range(n)]


def vspad_words(m, off, n):
    all_words = m.vspad_words()
    return all_words[off // 4 : off // 4 + n]


def check_dot_i8():
    m = run("dot_i8")
    a = bytes_of(xorshift_stream(K.XORSHIFT_SEED, K.DOT_N // 4))
    b = bytes_of(xorshift_stream(0x0BAD_F00D, K.DOT_N // 4))
    want = sum(s8(a[i]) * s8(b[i]) for i in range(K.DOT_N)) & MASK32
    got = spad_words(m, K.O_OFF, 1)[0]
    return want, got


def check_fir_i16():
    m = run("fir_i16")
    nw = (K.FIR_OUT + len(K.FIR_TAPS)) // 2 + 4
    x = halves_of(xorshift_stream(K.XORSHIFT_SEED, nw))
    want = [
        sum(K.FIR_TAPS[t] * s16(x[i + t]) for t in range(len(K.FIR_TAPS))) & MASK32
        for i in range(K.FIR_OUT)
    ]
    got = spad_words(m, K.O_OFF, K.FIR_OUT)
    return want, got


def check_stencil_i16():
    m = run("stencil_i16")
    nw = K.IMG_W * K.IMG_H // 2
    px = halves_of(xorshift_stream(K.XORSHIFT_SEED, nw))
    W, H = K.IMG_W, K.IMG_H
    ker = [[1, 2, 1], [2, 4, 2], [1, 2, 1]]
    want = []
    for r in range(1, H - 1):
        for c in range(1, W - 1):
            acc = sum(
                ker[dr + 1][dc + 1] * s16(px[(r + dr) * W + (c + dc)])
                for dr in (-1, 0, 1)
                for dc in (-1, 0, 1)
            )
            want.append((acc >> 4) & 0xFFFF)
    # The kernel stores halfwords packed two per word, in raster order.
    nout = (W - 2) * (H - 2)
    got_words = spad_words(m, K.O_OFF, (nout + 1) // 2)
    got = halves_of(got_words)[:nout]
    return want, got


def check_reduce_i32():
    m = run("reduce_i32")
    xs = xorshift_stream(K.XORSHIFT_SEED, K.RED_N)
    want = [sum(xs) & MASK32, max(s8 for s8 in (s32(v) for v in xs)) & MASK32]
    got = spad_words(m, K.O_OFF, 2)
    return want, got


def check_epilogue():
    m = run("epilogue")
    xs = xorshift_stream(K.XORSHIFT_SEED, K.EPI_N)
    out = []
    for v in xs:
        y = s32((s32(v) + 12345) & MASK32)
        y = max(y, 0)
        y = (y + (1 << (K.EPI_SHIFT - 1))) >> K.EPI_SHIFT
        y = min(max(y, -128), 127)
        out.append(y & 0xFF)
    want = [
        sum(out[i * 4 + k] << (8 * k) for k in range(4)) & MASK32
        for i in range(K.EPI_N // 4)
    ]
    got = spad_words(m, K.O_OFF, K.EPI_N // 4)
    return want, got


def check_memcpy32():
    m = run("memcpy32")
    want = xorshift_stream(K.XORSHIFT_SEED, K.CPY_N)
    got = spad_words(m, K.O_OFF, K.CPY_N)
    return want, got


# ---- the vector kernels, against the SAME references as their scalar twins --
# This is what makes a speedup a speedup: not that the vector kernel is fast and
# self-consistent, but that it computes the identical answer from the identical
# data, checked against an implementation that shares no code with either.


def check_dot_i8_v():
    m = run("dot_i8_v")
    a = bytes_of(xorshift_stream(K.XORSHIFT_SEED, K.DOT_N // 4))
    b = bytes_of(xorshift_stream(0x0BAD_F00D, K.DOT_N // 4))
    want = sum(s8(a[i]) * s8(b[i]) for i in range(K.DOT_N)) & MASK32
    return want, spad_words(m, K.O_OFF, 1)[0]


def check_dot2_i8_v():
    m = run("dot2_i8_v")
    a = bytes_of(xorshift_stream(K.XORSHIFT_SEED, K.DOT_N // 4))
    b = bytes_of(xorshift_stream(0x0BAD_F00D, K.DOT_N // 4))
    c = bytes_of(xorshift_stream(0x5EED_1234, K.DOT_N // 4))
    want = [sum(s8(a[i]) * s8(w[i]) for i in range(K.DOT_N)) & MASK32 for w in (b, c)]
    return want, spad_words(m, K.O_OFF, 2)


def check_vsw_hazard():
    # 7 alone in row 0 when it is read, then 5+3 in row 1. Reading the wrong
    # row gives 8 and 11 -- which is what no interlock looks like.
    m = run("vsw_hazard")
    return [7, 8], spad_words(m, K.O_OFF, 2)


def check_reduce_i32_v():
    m = run("reduce_i32_v")
    xs = xorshift_stream(K.XORSHIFT_SEED, K.RED_N)
    want = [sum(xs) & MASK32, max(s32(v) for v in xs) & MASK32]
    return want, spad_words(m, K.O_OFF, 2)


def check_memcpy32_v():
    m = run("memcpy32_v")
    want = xorshift_stream(K.XORSHIFT_SEED, K.CPY_N)
    return want, vspad_words(m, K.VO_OFF, K.CPY_N)


def check_epilogue_v():
    m = run("epilogue_v")
    xs = xorshift_stream(K.XORSHIFT_SEED, K.EPI_N)
    out = []
    for v in xs:
        y = s32((s32(v) + 12345) & MASK32)
        y = max(y, 0)
        # The vector form rounds (vsrari) where the scalar form truncated; the
        # reference has to say which, and this is the arithmetic the ISA
        # defines: add half an ulp, then shift.
        y = (y + (1 << (K.EPI_SHIFT - 1))) >> K.EPI_SHIFT
        y = min(max(y, -128), 127)
        out.append(y & 0xFF)
    want = [
        sum(out[i * 4 + k] << (8 * k) for k in range(4)) & MASK32
        for i in range(K.EPI_N // 4)
    ]
    return want, vspad_words(m, K.VO_OFF, K.EPI_N // 4)


def check_fir_i16_v():
    m = run("fir_i16_v")
    nw = (K.FIR_OUT + len(K.FIR_TAPS) + 1) // 2 + 4
    x = halves_of(xorshift_stream(K.XORSHIFT_SEED, nw))
    want = [
        sum(K.FIR_TAPS[t] * s16(x[i + t]) for t in range(len(K.FIR_TAPS))) & MASK32
        for i in range(K.FIR_OUT)
    ]
    return want, vspad_words(m, K.VO_OFF, K.FIR_OUT)


def check_f32_dot():
    """A binary32 dot product, model-only: `vfredsum` has no RTL yet.

    THE ORDER IS THE CONTRACT. Each slot holds NPART rotating partials and
    consecutive accumulates land on successive ones, because that is what breaks
    the FMA's recurrence. Float addition does not associate, so a reference that
    summed serially would compute a different answer from a correct machine;
    this one rotates identically.

    IT SHARES THE ARITHMETIC PRIMITIVE WITH THE MODEL, AND THAT IS NOT CIRCULAR.
    What this checks is the KERNEL and the path through it -- the loop, the
    ordering, the instruction semantics, the model's execution. The primitive
    itself is checked bit for bit against the RTL in `rv_fpu_tb`.
    """
    import khs_fp32 as F

    n_vec, npart = 8, 16
    slots = SIMD

    xs = xorshift_stream(0xF32D_07, n_vec * SIMD * 2)
    # Finite and ordinary: a NaN or an infinity in the stream would test the
    # specials, which is a different kernel.
    fix = lambda v: (v & 0x807F_FFFF) | 0x3F00_0000
    a32 = [fix(w) for w in xs[: n_vec * SIMD]]
    b32 = [fix(w) for w in xs[n_vec * SIMD :]]

    src = [
        "    li   s0, VSPAD+0",
        "    li   s1, VSPAD+0x400",
        "    li   s2, %d" % n_vec,
        "    vfaccz acc0",
    ]
    for i in range(n_vec * SIMD):
        src.append("    li   t1, 0x%08x" % a32[i])
        src.append("    sw   t1, %d(s0)" % (4 * i))
        src.append("    li   t1, 0x%08x" % b32[i])
        src.append("    sw   t1, %d(s1)" % (4 * i))
    src += [
        "f32_loop:",
        "    vld  v0, 0(s0)",
        "    vld  v1, 0(s1)",
        "    vfmacc.f32 acc0, v0, v1",
        "    addi s0, s0, %d" % (4 * SIMD),
        "    addi s1, s1, %d" % (4 * SIMD),
        "    addi s2, s2, -1",
        "    bnez s2, f32_loop",
        "    vfredsum.f32 a2, acc0",
        "    li   t0, SPAD+%d" % K.O_OFF,
        "    sw   a2, 0(t0)",
        "    li   a0, 0",
        "    ecall",
    ]

    words, _ = assemble(zero_regs() + "\n".join(src) + "\n", base=0, symbols=ALL_SYMS)
    m = DspMachine(
        simd=SIMD,
        vspad_entries=VSPAD_ENTRIES,
        imem_words=2048,
        spad_words=SPAD_WORDS,
        arg=0,
        coreid=0x11,
        npart=npart,
    )
    m.imem[: len(words)] = words
    m.run(limit=4_000_000)

    part = [[0] * npart for _ in range(slots)]
    for v in range(n_vec):
        for s in range(slots):
            i = v * slots + s
            part[s][v % npart] = F.fpu(F.OP_FMA, a32[i], b32[i], part[s][v % npart])[0]
    tot = 0
    for parts in part:
        slot = 0
        for p in parts:
            slot = F.fpu(F.OP_FMA, p, F.F32_ONE, slot)[0]
        tot = F.fpu(F.OP_FMA, slot, F.F32_ONE, tot)[0]
    want = tot
    got = spad_words(m, K.O_OFF, 1)[0]
    return want, got


CHECKS = [
    ("dot_i8", check_dot_i8),
    ("fir_i16", check_fir_i16),
    ("stencil_i16", check_stencil_i16),
    ("reduce_i32", check_reduce_i32),
    ("epilogue", check_epilogue),
    ("memcpy32", check_memcpy32),
    ("dot_i8_v", check_dot_i8_v),
    ("dot2_i8_v", check_dot2_i8_v),
    ("vsw_hazard", check_vsw_hazard),
    ("f32_dot", check_f32_dot),
    ("reduce_i32_v", check_reduce_i32_v),
    ("memcpy32_v", check_memcpy32_v),
    ("epilogue_v", check_epilogue_v),
    ("fir_i16_v", check_fir_i16_v),
]


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--simd", type=int, default=8, choices=(2, 4, 8))
    a = ap.parse_args()
    global SIMD
    SIMD = a.simd

    bad = 0
    ran = 0
    for name, fn in CHECKS:
        if name in K.MIN_SIMD and SIMD < K.MIN_SIMD[name]:
            print("  --   %-14s skipped: needs SIMD >= %d" % (name, K.MIN_SIMD[name]))
            continue
        ran += 1
        want, got = fn()
        if not isinstance(want, list):
            want, got = [want], [got]
        diff = [(i, w, g) for i, (w, g) in enumerate(zip(want, got)) if w != g]
        if diff:
            bad += 1
            print(
                "  FAIL %-14s %d of %d elements differ" % (name, len(diff), len(want))
            )
            for i, w, g in diff[:6]:
                print("        [%3d] want %08x got %08x" % (i, w, g))
        else:
            print("  ok   %-14s %d elements" % (name, len(want)))
    print("=" * 40)
    print(
        "  %s -- %d kernels at SIMD %d, %d wrong"
        % ("FAIL" if bad else "PASS", ran, SIMD, bad)
    )
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
