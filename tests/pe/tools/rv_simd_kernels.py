"""The DSP-class workload suite, as RV32I source: the denominators.

Every speedup claimed for a packed instruction is measured against the scalar
sequence here, running on the shipped controller PE.

Three structural properties:

* **Only the kernel is timed.** A case reads `CTL_CYCLE` either side of its
  kernel and halts with the difference in `a0`. Data generation, the checksum,
  the DRAM store and the flush are outside the bracket. `nullkern` measures the
  bracket itself.
* **Correctness rides on DRAM, not the halt word.** `a0` is a cycle count and
  the model returns 0 for `CTL_CYCLE`, so the model cannot predict it. What it
  predicts exactly is the rotate-xor checksum the program computes over its own
  results and stores to DRAM; one wrong element changes it.
* **Data lives in the scratchpad** -- one cycle, always hits. A kernel bound by
  a fill round trip measures the memory agent, not arithmetic specialization.

## The multiply problem, which is a finding

The base core has no multiplier, so `a*b` for two runtime values is eight
unrolled shift-add steps -- 48 instructions. A naive "SIMD PE vs scalar" speedup
would therefore mostly measure *owning a multiplier* rather than SIMD width.

Every multiplying kernel has a `_nomul` twin: same loop, same loads, same
traffic, the multiply replaced by one `add`. It computes a different (still
model-checked) answer, and its cycles are what the kernel would cost if a
multiply were one instruction. The two separate the two effects.

`fir_i16` needs no twin -- its taps are compile-time constants, strength-reduced
to shifts and adds, so no software multiply appears in it at all.
"""

# ---------------------------------------------------------------- memory map
# Byte offsets from SPAD base. Every array is 32-byte aligned: a granule is the
# machine's word and a vector load is line-aligned by contract.
A_OFF = 0x0000          # input 1        1024 B
B_OFF = 0x0400          # input 2        1024 B
O_OFF = 0x0800          # output         1024 B
S_OFF = 0x0C00          # scratch        1024 B

CK_DRAM = 0x1000        # where the result checksum lands, byte offset in DRAM

XORSHIFT_SEED = 0x1234_5678

# ---------------------------------------------------------- the vector side
#: Lanes in the build being generated for. Set by rv_simd_gen; the vector
#: kernels are written against it because a vector's byte count and its element
#: count are both consequences of it, and a kernel generated for the wrong width
#: is a program that faults rather than a program that is slow.
SIMD = 8

#: Byte offsets in the vector scratchpad. Held apart from the scalar map on
#: purpose: the two are different address regions, and a kernel that confuses
#: them faults on the region decode rather than reading the wrong data.
VA_OFF = 0x0000         # input 1
VB_OFF = 0x0400         # input 2
VC_OFF = 0x0600         # input 3, for the two-accumulator dot
VO_OFF = 0x0800         # output
VH_OFF = 0x0C00         # two rows, for the store-then-load hazard case


def vbytes():
    return 4 * SIMD


def fill_at(base, off, nwords, seed=XORSHIFT_SEED, tag="f"):
    """Fill `nwords` words from `base`+`off` with an xorshift stream.

    The SAME stream the scalar kernels use, so a vector kernel and its scalar
    twin compute over identical data and their answers are comparable rather
    than merely both plausible.
    """
    return f"""
    li   t0, {base}+{off}
    li   t1, {nwords}
    li   t2, {seed}
{tag}_loop:
{_xorshift("t2", "t3")}
    sw   t2, 0(t0)
    addi t0, t0, 4
    addi t1, t1, -1
    bnez t1, {tag}_loop
"""


def _xorshift(reg, tmp):
    """One xorshift32 step in place: six instructions, no branch, no table."""
    return f"""
    slli {tmp}, {reg}, 13
    xor  {reg}, {reg}, {tmp}
    srli {tmp}, {reg}, 17
    xor  {reg}, {reg}, {tmp}
    slli {tmp}, {reg}, 5
    xor  {reg}, {reg}, {tmp}
"""


def fill(off, nwords, seed=XORSHIFT_SEED, tag="f"):
    """Fill `nwords` scratchpad words from `off` with an xorshift stream."""
    return f"""
    li   t0, SPAD+{off}
    li   t1, {nwords}
    li   t2, {seed}
{tag}_loop:
{_xorshift("t2", "t3")}
    sw   t2, 0(t0)
    addi t0, t0, 4
    addi t1, t1, -1
    bnez t1, {tag}_loop
"""


def zero(off, nwords, tag="z"):
    """Zero `nwords` scratchpad words from `off`.

    NOT optional, and not defensive. The scratchpad is block RAM: it powers up
    at zero and then RETAINS across a reset, so a case running after another one
    sees the previous case's leftovers, while the golden model gives every case
    a freshly zeroed scratchpad. A checksum reading one word past what the
    kernel wrote therefore compares leftovers against zero and fails -- which is
    exactly how `stencil_i16` failed, on a checksum length that was one word too
    long. Zeroing the checksummed region makes the comparison sound whatever the
    length is.
    """
    return f"""
    li   t0, SPAD+{off}
    li   t1, {nwords}
{tag}_loop:
    sw   x0, 0(t0)
    addi t0, t0, 4
    addi t1, t1, -1
    bnez t1, {tag}_loop
"""


def checksum(off, nwords, tag="ck"):
    """Rotate-xor a result array into `a1`.

    Position-sensitive without a multiply: two elements that swap places give a
    different answer, which is what a wrong writeback address needs and what a
    plain sum would miss.
    """
    return f"""
    li   t0, SPAD+{off}
    li   t1, {nwords}
    li   a1, 0
{tag}_loop:
    lw   t4, 0(t0)
    slli t5, a1, 1
    srli t6, a1, 31
    or   a1, t5, t6
    xor  a1, a1, t4
    addi t0, t0, 4
    addi t1, t1, -1
    bnez t1, {tag}_loop
"""


# s11 and s10 are reserved to the bracket; no kernel may touch them.
CYC_START = """
    li   t6, CTL_CYCLE
    lw   s11, 0(t6)
"""

CYC_END = """
    li   t6, CTL_CYCLE
    lw   s10, 0(t6)
"""


def report(ck_words, ck_off):
    """Checksum, store, flush, halt with the cycle delta.

    The flush is blocking and outside the bracket: memory-system cost, not
    kernel cost.
    """
    return checksum(ck_off, ck_words) + f"""
    li   t0, DRAM+{CK_DRAM}
    sw   a1, 0(t0)
    li   t6, CTL_FLUSH
    sw   x0, 0(t6)
    sub  a0, s10, s11
    ecall
"""


# ------------------------------------------------------------------ multiply

def mul8(dst, a, b, t0, t1, t2):
    """dst = a * b for a sign-extended int8 `a` and an int8 `b`.

    `b` is consumed UNSIGNED and the sign corrected at the end -- b_signed =
    b_unsigned - 256*b7 -- which keeps all eight steps identical and so keeps
    them branchless. A magnitude-and-sign form needs two conditional negates.

    Six instructions per bit; the four-deep chain forwards at distance 1, so it
    is 48 cycles at FWD_X=1 with no stalls. Clobbers t0, t1, t2; `a` and `b` are
    read but not written, so `b` is still intact for the sign correction.

    Emits the label `mulskip`, so one kernel may call this once.
    """
    out = [f"    andi {t1}, {b}, 0xFF", f"    mv   {t0}, {a}", f"    mv   {dst}, x0"]
    for _ in range(8):
        out += [
            f"    andi {t2}, {t1}, 1",
            f"    sub  {t2}, x0, {t2}",
            f"    and  {t2}, {t2}, {t0}",
            f"    add  {dst}, {dst}, {t2}",
            f"    slli {t0}, {t0}, 1",
            f"    srli {t1}, {t1}, 1",
        ]
    # `t0` is a<<8 exactly here -- eight shifts have already been applied to it,
    # which is why the sign correction costs no shift of its own.
    out += [
        f"    andi {t2}, {b}, 0x80",
        f"    beqz {t2}, mulskip",
        f"    sub  {dst}, {dst}, {t0}",
        "mulskip:",
    ]
    return "\n".join(out) + "\n"


def mul8_fake(dst, a, b, t0, t1, t2):
    """The same signature at one instruction. See the module docstring."""
    return f"    add  {dst}, {a}, {b}\n"


# ------------------------------------------------------------------- kernels
# Each returns (init, kernel, checksum words).

DOT_N = 128             # int8 elements per input vector


def k_dot_i8(mul=mul8):
    """int8 dot product into an int32 accumulator.

    Data times data: neither operand is known at compile time, so nothing can be
    strength-reduced and the whole software multiply is paid per element.
    """
    body = f"""
    li   s0, SPAD+{A_OFF}
    li   s1, SPAD+{B_OFF}
    li   s2, {DOT_N}
    li   s4, 0
dot_loop:
    lb   a3, 0(s0)
    lb   a4, 0(s1)
""" + mul("a5", "a3", "a4", "t0", "t1", "t2") + f"""
    add  s4, s4, a5
    addi s0, s0, 1
    addi s1, s1, 1
    addi s2, s2, -1
    bnez s2, dot_loop
    li   t0, SPAD+{O_OFF}
    sw   s4, 0(t0)
"""
    pre = fill(A_OFF, DOT_N // 4, tag="fa") + \
        fill(B_OFF, DOT_N // 4, seed=0x0BAD_F00D, tag="fb")
    return pre, body, 1


FIR_TAPS = [1, 3, 7, 5, 5, 7, 3, 1]     # strength-reducible, symmetric
FIR_OUT = 64


def _tap(dst, src, coeff, tmp):
    """One constant tap, as a compiler strength-reduces it."""
    if coeff == 1:
        return f"    mv   {dst}, {src}\n"
    if coeff == 3:
        return f"    slli {tmp}, {src}, 1\n    add  {dst}, {tmp}, {src}\n"
    if coeff == 5:
        return f"    slli {tmp}, {src}, 2\n    add  {dst}, {tmp}, {src}\n"
    if coeff == 7:
        return f"    slli {tmp}, {src}, 3\n    sub  {dst}, {tmp}, {src}\n"
    raise ValueError("no strength reduction written for coefficient %d" % coeff)


def k_fir_i16():
    """8-tap FIR over int16, int32 accumulate, CONSTANT taps.

    The hard baseline: two real instructions per tap, no software multiply, so a
    packed instruction has to beat two rather than fifty.
    """
    body = ["""
    li   s0, SPAD+%d
    li   s1, SPAD+%d
    li   s2, %d
fir_loop:
    li   s4, 0
""" % (A_OFF, O_OFF, FIR_OUT)]
    for i, c in enumerate(FIR_TAPS):
        body.append(f"    lh   a3, {2 * i}(s0)\n")
        body.append(_tap("a5", "a3", c, "t0"))
        body.append("    add  s4, s4, a5\n")
    body.append("""
    sw   s4, 0(s1)
    addi s0, s0, 2
    addi s1, s1, 4
    addi s2, s2, -1
    bnez s2, fir_loop
""")
    pre = fill(A_OFF, (FIR_OUT + len(FIR_TAPS)) // 2 + 4, tag="fa")
    return pre, "".join(body), FIR_OUT


IMG_W = 16              # int16 samples per row
IMG_H = 16


def k_stencil_i16():
    """3x3 Gaussian (separable 1-2-1, sum 16) over a 16x16 int16 image.

    Every coefficient is a power of two, so this is the pure address-and-add
    kernel and it is where a lane-crossing shift earns or loses its place.
    """
    rs = IMG_W * 2       # row stride, bytes
    body = ["""
    li   s0, SPAD+%d
    li   s1, SPAD+%d
    li   s3, %d
st_row:
    li   s2, %d
st_col:
    lh   a3, %d(s0)
    lh   a4, %d(s0)
    add  a3, a3, a4
    lh   a4, %d(s0)
    add  a3, a3, a4
    lh   a4, %d(s0)
    add  a3, a3, a4
    lh   a5, %d(s0)
    lh   a4, -2(s0)
    add  a5, a5, a4
    lh   a4, 2(s0)
    add  a5, a5, a4
    lh   a4, %d(s0)
    add  a5, a5, a4
    slli a5, a5, 1
    add  a3, a3, a5
    lh   a4, 0(s0)
    slli a4, a4, 2
    add  a3, a3, a4
    srai a3, a3, 4
    sh   a3, 0(s1)
    addi s0, s0, 2
    addi s1, s1, 2
    addi s2, s2, -1
    bnez s2, st_col
    addi s0, s0, 4
    addi s3, s3, -1
    bnez s3, st_row
""" % (A_OFF + rs + 2, O_OFF, IMG_H - 2, IMG_W - 2,
       -rs - 2, -rs + 2, rs - 2, rs + 2, -rs, rs)]
    pre = fill(A_OFF, IMG_W * IMG_H // 2, tag="fa")
    # 196 halfwords is exactly 98 words; a 99th would read past the kernel.
    return pre, "".join(body), (IMG_W - 2) * (IMG_H - 2) // 2


RED_N = 256


def k_reduce_i32():
    """Sum and signed max over int32, in one pass.

    The max is branchless (`slt`, then a masked xor-swap): a data-dependent
    branch would make the answer depend on the input distribution rather than on
    the ISA.
    """
    body = """
    li   s0, SPAD+%d
    li   s2, %d
    li   s4, 0
    li   s5, 0x80000000
red_loop:
    lw   a3, 0(s0)
    add  s4, s4, a3
    slt  t0, s5, a3
    sub  t0, x0, t0
    xor  t1, s5, a3
    and  t1, t1, t0
    xor  s5, s5, t1
    addi s0, s0, 4
    addi s2, s2, -1
    bnez s2, red_loop
    li   t0, SPAD+%d
    sw   s4, 0(t0)
    sw   s5, 4(t0)
""" % (A_OFF, RED_N, O_OFF)
    return fill(A_OFF, RED_N, tag="fa"), body, 2


EPI_N = 256
EPI_SHIFT = 7


def k_epilogue():
    """Requantise epilogue: bias, ReLU, arithmetic shift, saturate, pack to int8.

    The kernel 09B S4.4 asks about -- SIMD PE or ship it to a vector core. Every
    step has a packed instruction (`padd`, `pmax`, `psra`, saturating `pack`)
    and nothing multiplies, so the scalar baseline is already tight.

    It ROUNDS rather than truncates -- a half ulp added before the shift -- for
    two reasons: a real requantise rounds, and the vector twin's `vsrari` is one
    instruction either way, so a truncating scalar baseline would have made the
    two kernels compute different answers for no saving.
    """
    body = """
    li   s0, SPAD+%d
    li   s1, SPAD+%d
    li   s2, %d
    li   s6, 12345
    li   s7, 127
    li   s8, -128
    li   s9, 0
    li   s5, 0
epi_loop:
    lw   a3, 0(s0)
    add  a3, a3, s6
    srai t0, a3, 31
    not  t0, t0
    and  a3, a3, t0
    addi a3, a3, %d
    srai a3, a3, %d
    slt  t0, s7, a3
    sub  t0, x0, t0
    xor  t1, a3, s7
    and  t1, t1, t0
    xor  a3, a3, t1
    slt  t0, a3, s8
    sub  t0, x0, t0
    xor  t1, a3, s8
    and  t1, t1, t0
    xor  a3, a3, t1
    andi a3, a3, 0xFF
    sll  a3, a3, s5
    or   s9, s9, a3
    addi s5, s5, 8
    li   t0, 32
    bne  s5, t0, epi_next
    sw   s9, 0(s1)
    addi s1, s1, 4
    li   s9, 0
    li   s5, 0
epi_next:
    addi s0, s0, 4
    addi s2, s2, -1
    bnez s2, epi_loop
""" % (A_OFF, O_OFF, EPI_N, 1 << (EPI_SHIFT - 1), EPI_SHIFT)
    return fill(A_OFF, EPI_N, tag="fa"), body, EPI_N // 4


CPY_N = 256


def k_memcpy32():
    """A copy with no arithmetic: the pure data-width control.

    Unrolled by four so no load feeds the store one instruction behind it -- a
    distance-1 load-use is two stall cycles, and at 256 words that penalty would
    be most of the kernel and would flatter every wider alternative.
    """
    body = """
    li   s0, SPAD+%d
    li   s1, SPAD+%d
    li   s2, %d
cpy_loop:
    lw   a3, 0(s0)
    lw   a4, 4(s0)
    lw   a5, 8(s0)
    lw   a6, 12(s0)
    sw   a3, 0(s1)
    sw   a4, 4(s1)
    sw   a5, 8(s1)
    sw   a6, 12(s1)
    addi s0, s0, 16
    addi s1, s1, 16
    addi s2, s2, -1
    bnez s2, cpy_loop
""" % (A_OFF, O_OFF, CPY_N // 4)
    return fill(A_OFF, CPY_N, tag="fa"), body, CPY_N


def k_null():
    """Nothing between the two CTL_CYCLE reads: the bracket's own cost."""
    return "", "", 1


# ------------------------------------------------------- the vector kernels
# Each is the same computation over the same data as its scalar twin, so the
# cycle ratio is the specialization frontier and nothing else. Results land in
# the scalar scratchpad, because that is what the checksum and the DRAM
# comparison already reach.

def k_dot_i8_vec():
    """The same int8 dot, as one `vdot.s8` per 4*SIMD elements.

    The headline: `vdot.s8` replaces four multiplies AND their adds per 32-bit
    lane, so at SIMD 8 one instruction does 32 int8 multiply-accumulates that
    the scalar twin pays 48 instructions each for.
    """
    per = 4 * SIMD                      # int8 elements per vector
    body = """
    li   s0, VSPAD+%d
    li   s1, VSPAD+%d
    li   s2, %d
    vaccz acc0
dotv_loop:
    vld  v0, 0(s0)
    vld  v1, 0(s1)
    vdot.s8 acc0, v0, v1
    addi s0, s0, %d
    addi s1, s1, %d
    addi s2, s2, -1
    bnez s2, dotv_loop
    vaccrd v2, acc0
    vredsum a2, v2
    li   t0, SPAD+%d
    sw   a2, 0(t0)
""" % (VA_OFF, VB_OFF, DOT_N // per, vbytes(), vbytes(), O_OFF)
    pre = (fill_at("VSPAD", VA_OFF, DOT_N // 4, tag="va") +
           fill_at("VSPAD", VB_OFF, DOT_N // 4, seed=0x0BAD_F00D, tag="vb"))
    return pre, body, 1


def k_dot2_i8_vec():
    """One activation vector against TWO weight vectors: adjacent `vdot`s.

    The multi-output matvec shape, and the only case in the suite that can see
    the accumulator's issue interval -- `dot_i8_v` spaces its dots six
    instructions apart and would run the same at any II.
    """
    per = 4 * SIMD                      # int8 elements per vector
    body = """
    li   s0, VSPAD+%d
    li   s1, VSPAD+%d
    li   s3, VSPAD+%d
    li   s2, %d
    vaccz acc0
    vaccz acc1
dot2v_loop:
    vld  v0, 0(s0)
    vld  v1, 0(s1)
    vld  v2, 0(s3)
    vdot.s8 acc0, v0, v1
    vdot.s8 acc1, v0, v2
    addi s0, s0, %d
    addi s1, s1, %d
    addi s3, s3, %d
    addi s2, s2, -1
    bnez s2, dot2v_loop
    vaccrd v3, acc0
    vredsum a2, v3
    vaccrd v4, acc1
    vredsum a3, v4
    li   t0, SPAD+%d
    sw   a2, 0(t0)
    sw   a3, 4(t0)
""" % (VA_OFF, VB_OFF, VC_OFF, DOT_N // per,
       vbytes(), vbytes(), vbytes(), O_OFF)
    pre = (fill_at("VSPAD", VA_OFF, DOT_N // 4, tag="v2a") +
           fill_at("VSPAD", VB_OFF, DOT_N // 4, seed=0x0BAD_F00D, tag="v2b") +
           fill_at("VSPAD", VC_OFF, DOT_N // 4, seed=0x5EED_1234, tag="v2c"))
    return pre, body, 2


def k_vsw_hazard():
    """A scalar `sw` into the vector window with a `vld` of ANOTHER row behind it.

    Both use the port the vector unit owns, so the load must wait a cycle. The
    rows differ deliberately: with the same row the wrong answer equals the
    right one, and this case would pass a build that has no interlock at all.
    """
    body = """
    li   s0, VSPAD+%d
    li   s2, 16
zh_loop:
    sw   x0, 0(s0)
    addi s0, s0, 4
    addi s2, s2, -1
    bnez s2, zh_loop
    li   s0, VSPAD+%d
    li   t1, 7
    sw   t1, 0(s0)
    li   t1, 5
    sw   t1, %d(s0)
    li   t2, 3
    sw   t2, %d(s0)
    vld  v0, 0(s0)
    vredsum a2, v0
    li   t2, 4
    sw   t2, 4(s0)
    vld  v1, %d(s0)
    vredsum a3, v1
    li   t0, SPAD+%d
    sw   a2, 0(t0)
    sw   a3, 4(t0)
""" % (VH_OFF, VH_OFF, vbytes(), vbytes() + 4, vbytes(), O_OFF)
    return "", body, 2


def k_reduce_i32_vec():
    """Sum and signed max over int32, one vector at a time.

    The reduction is split: `vadd.s32` and `vmax.s32` keep SIMD running totals
    and one `vredsum` / `vredmax` finishes them. That is the standard shape and
    it is also why the accumulator is a vector register here rather than the
    dot accumulator -- these are lane-wise, not within-lane.
    """
    body = """
    li   s0, VSPAD+%d
    li   s2, %d
    li   t1, 0
    vsplat v2, t1
    li   t1, 0x80000000
    vsplat v3, t1
redv_loop:
    vld  v0, 0(s0)
    vadd.s32 v2, v2, v0
    vmax.s32 v3, v3, v0
    addi s0, s0, %d
    addi s2, s2, -1
    bnez s2, redv_loop
    vredsum a2, v2
    vredmax a3, v3
    li   t0, SPAD+%d
    sw   a2, 0(t0)
    sw   a3, 4(t0)
""" % (VA_OFF, RED_N // SIMD, vbytes(), O_OFF)
    return fill_at("VSPAD", VA_OFF, RED_N, tag="va"), body, 2


def k_memcpy32_vec():
    """A copy at the machine word: the pure data-width control."""
    body = """
    li   s0, VSPAD+%d
    li   s1, VSPAD+%d
    li   s2, %d
cpyv_loop:
    vld  v0, 0(s0)
    vst  v0, 0(s1)
    addi s0, s0, %d
    addi s1, s1, %d
    addi s2, s2, -1
    bnez s2, cpyv_loop
""" % (VA_OFF, VO_OFF, CPY_N // SIMD, vbytes(), vbytes())
    # The result is read back through the vector side and reduced, because the
    # scalar core cannot LOAD the vector scratchpad -- it is store-only there,
    # the same contract a peer window has.
    tail = """
    li   s0, VSPAD+%d
    li   s2, %d
    li   t1, 0
    vsplat v2, t1
ckv_loop:
    vld  v0, 0(s0)
    vxor v2, v2, v0
    addi s0, s0, %d
    addi s2, s2, -1
    bnez s2, ckv_loop
    vredsum a2, v2
    li   t0, SPAD+%d
    sw   a2, 0(t0)
""" % (VO_OFF, CPY_N // SIMD, vbytes(), O_OFF)
    return (fill_at("VSPAD", VA_OFF, CPY_N, tag="va"), body, tail, 1)


def k_fir_i16_vec():
    """The 8-tap FIR, vectorised: a sliding window from `vsldw`, constant taps
    still strength-reduced.

    The NARROWEST frontier point in the suite, and the honest one. Its scalar
    twin has no software multiply to remove -- the taps are compile-time
    constants a compiler already turns into a shift and an add -- so this kernel
    can only win the width, not the multiplier.

    Three things it needs that the dot product did not:

    * **int32 lanes.** `vsldw` slides by whole 32-bit lanes, which is one
      element only when elements are int32. So the int16 input is widened
      first -- inside the timed region, because its scalar twin's `lh`
      sign-extends for free and charging only one side would be a thumb on the
      scale.
    * **Two accumulators.** Eight taps accumulating into one vector register is
      eight back-to-back dependencies and eight stalls. Alternating between two
      and combining once at the end puts every dependence at distance 2, which
      is what the hazard rules need and what a compiler could not do for us --
      the intrinsics are `volatile` and the scheduler cannot see them.
    * **Only tap indices 0..7.** `vsldw` carries a 3-bit index, and an 8-tap
      filter at SIMD 8 needs exactly that range. A longer filter needs a second
      window pair, which is a real limit of the encoding and worth knowing.
    """
    # `vsldw` rotates a 2*SIMD-lane concatenation, so tap t reads a genuine
    # window only while t + SIMD - 1 < 2*SIMD, i.e. SIMD >= the tap count.
    # Below that it wraps -- and the RTL and the model wrap IDENTICALLY, so the
    # bench would pass on an answer that is not a FIR. Refuse instead.
    if SIMD < len(FIR_TAPS):
        raise ValueError(
            "fir_i16_v needs SIMD >= %d taps; at SIMD %d vsldw would wrap and "
            "the model would agree with the hardware about the wrong answer"
            % (len(FIR_TAPS), SIMD))
    vb = vbytes()
    nsamp = FIR_OUT + len(FIR_TAPS)
    # ---- pass 1: widen int16 -> int32, in place from A into scratch ----
    wide = """
    li   s0, VSPAD+%d
    li   s1, VSPAD+%d
    li   s2, %d
firw_loop:
    vld  v0, 0(s0)
    vunpkl.s16 v1, v0
    vunpkh.s16 v2, v0
    vst  v1, 0(s1)
    vst  v2, %d(s1)
    addi s0, s0, %d
    addi s1, s1, %d
    addi s2, s2, -1
    bnez s2, firw_loop
""" % (VA_OFF, VB_OFF, (nsamp + 2 * SIMD - 1) // (2 * SIMD),
       vb, vb, 2 * vb)

    # ---- pass 2: the filter itself ----
    body = ["""
    li   s0, VSPAD+%d
    li   s1, VSPAD+%d
    li   s2, %d
    li   t1, 0
firv_loop:
    vld  v0, 0(s0)
    vld  v1, %d(s0)
    vsplat v4, t1
    vsplat v7, t1
""" % (VB_OFF, VO_OFF, FIR_OUT // SIMD, vb)]
    for i, c in enumerate(FIR_TAPS):
        w, t = ("v2", "v3") if (i % 2 == 0) else ("v5", "v6")
        acc = "v4" if (i % 2 == 0) else "v7"
        body.append("    vsldw %s, v0, v1, %d\n" % (w, i))
        if c == 1:
            body.append("    vadd.s32 %s, %s, %s\n" % (acc, acc, w))
        else:
            sh, op = {3: (1, "vadd"), 5: (2, "vadd"), 7: (3, "vsub")}[c]
            body.append("    vslli.s32 %s, %s, %d\n" % (t, w, sh))
            body.append("    %s.s32 %s, %s, %s\n" % (op, t, t, w))
            body.append("    vadd.s32 %s, %s, %s\n" % (acc, acc, t))
    body.append("""
    vadd.s32 v4, v4, v7
    vst  v4, 0(s1)
    addi s0, s0, %d
    addi s1, s1, %d
    addi s2, s2, -1
    bnez s2, firv_loop
""" % (vb, vb))

    tail = """
    li   s0, VSPAD+%d
    li   s2, %d
    li   t1, 0
    vsplat v2, t1
firck_loop:
    vld  v0, 0(s0)
    vxor v2, v2, v0
    addi s0, s0, %d
    addi s2, s2, -1
    bnez s2, firck_loop
    vredsum a2, v2
    li   t0, SPAD+%d
    sw   a2, 0(t0)
""" % (VO_OFF, FIR_OUT // SIMD, vb, O_OFF)

    pre = fill_at("VSPAD", VA_OFF, (nsamp + 1) // 2 + 4, tag="va")
    return pre, wide + "".join(body), tail, 1


def k_epilogue_vec():
    """Bias, ReLU, rounding shift, saturate and pack, as five vector ops.

    The kernel 09B S4.4 asks about. Every step of the scalar twin's fifteen
    instructions has a packed form here, and the saturating `vpack` is the one
    that would otherwise be six instructions of branchless min/max.
    """
    body = """
    li   s0, VSPAD+%d
    li   s1, VSPAD+%d
    li   s2, %d
    li   t1, 12345
    vsplat v7, t1
    li   t1, 0
    vsplat v6, t1
epiv_loop:
    vld  v0, 0(s0)
    vld  v1, %d(s0)
    vadd.s32 v0, v0, v7
    vadd.s32 v1, v1, v7
    vmax.s32 v0, v0, v6
    vmax.s32 v1, v1, v6
    vsrari.s32 v0, v0, %d
    vsrari.s32 v1, v1, %d
    vpack.s32 v2, v0, v1
    vld  v0, %d(s0)
    vld  v1, %d(s0)
    vadd.s32 v0, v0, v7
    vadd.s32 v1, v1, v7
    vmax.s32 v0, v0, v6
    vmax.s32 v1, v1, v6
    vsrari.s32 v0, v0, %d
    vsrari.s32 v1, v1, %d
    vpack.s32 v3, v0, v1
    vpack.s16 v4, v2, v3
    vst  v4, 0(s1)
    addi s0, s0, %d
    addi s1, s1, %d
    addi s2, s2, -1
    bnez s2, epiv_loop
""" % (VA_OFF, VO_OFF, EPI_N // (4 * SIMD),
       vbytes(), EPI_SHIFT, EPI_SHIFT, 2 * vbytes(), 3 * vbytes(),
       EPI_SHIFT, EPI_SHIFT, 4 * vbytes(), vbytes())
    tail = """
    li   s0, VSPAD+%d
    li   s2, %d
    li   t1, 0
    vsplat v2, t1
epick_loop:
    vld  v0, 0(s0)
    vxor v2, v2, v0
    addi s0, s0, %d
    addi s2, s2, -1
    bnez s2, epick_loop
    vredsum a2, v2
    li   t0, SPAD+%d
    sw   a2, 0(t0)
""" % (VO_OFF, EPI_N // (4 * SIMD), vbytes(), O_OFF)
    return fill_at("VSPAD", VA_OFF, EPI_N, tag="va"), body, tail, 1


SUITE = [
    ("nullkern",     k_null,                          "the timing bracket alone"),
    ("memcpy32",     k_memcpy32,                      "pure width, no arithmetic"),
    ("dot_i8",       k_dot_i8,                        "int8 dot, software multiply"),
    ("dot_i8_nomul", lambda: k_dot_i8(mul8_fake),     "int8 dot, multiply costed at 1"),
    ("fir_i16",      k_fir_i16,                       "8-tap FIR, constant taps"),
    ("stencil_i16",  k_stencil_i16,                   "3x3 Gaussian, 16x16 int16"),
    ("reduce_i32",   k_reduce_i32,                    "sum and signed max"),
    ("epilogue",     k_epilogue,                      "bias, ReLU, shift, saturate, pack"),
    ("memcpy32_v",   k_memcpy32_vec,                  "VECTOR: pure width"),
    ("dot_i8_v",     k_dot_i8_vec,                    "VECTOR: int8 dot"),
    ("dot2_i8_v",    k_dot2_i8_vec,                   "VECTOR: int8 dot, two accumulators"),
    ("vsw_hazard",   k_vsw_hazard,                    "VECTOR: scalar store, then a load of another row"),
    ("reduce_i32_v", k_reduce_i32_vec,                "VECTOR: sum and signed max"),
    ("epilogue_v",   k_epilogue_vec,                  "VECTOR: requantise epilogue"),
    ("fir_i16_v",    k_fir_i16_vec,                   "VECTOR: 8-tap FIR, constant taps"),
]

#: A case that needs more lanes than the build has. `fir_i16_v` is the only one:
#: its window comes from `vsldw`, whose rotate is only a window while SIMD is at
#: least the tap count.
MIN_SIMD = {"fir_i16_v": len(FIR_TAPS)}


def cases_for(simd):
    """The suite this build can actually run, in order."""
    return [(n, b, d) for n, b, d in SUITE if simd >= MIN_SIMD.get(n, 2)]

#: Which cases need the extension. A build without it faults on them, so the
#: runner refuses to report them rather than reporting a fault as a cycle count.
VECTOR_CASES = {n for n, _, d in SUITE if d.startswith("VECTOR")}


# --------------------------------------------------- bench-driven cases
VSPAD_NOC_VECTORS = 16


def ix_vspad_noc():
    """Reduce a tile the NoC delivered into the vector scratchpad.

    The ONLY test of `buf_id` 2, and the path a mesh actually uses: a peer drain
    or the mover writes a tile into this PE's vector window and the program
    consumes it. A program CANNOT write that window this way -- the scalar side
    is store-only through a different port -- so nothing else exercises the
    window writer's third target, and the golden model cannot predict the answer
    because the data arrives while the program is stopped. The bench owns it.
    """
    return """
    li   s0, VSPAD+%d
    li   s2, %d
    li   t1, 0
    vsplat v2, t1
vn_loop:
    vld  v0, 0(s0)
    vxor v2, v2, v0
    addi s0, s0, %d
    addi s2, s2, -1
    bnez s2, vn_loop
    vredsum a0, v2
    ecall
""" % (VA_OFF, VSPAD_NOC_VECTORS, vbytes())


IX_CASES = [("vspad_noc", ix_vspad_noc)]

BUILDERS = dict((n, b) for n, b, _ in SUITE)
NOTES = dict((n, d) for n, _, d in SUITE)


def build_case(name):
    """Assemble one case's source, with the kernel bracketed by two labels.

    A builder may return a `post` section, which runs AFTER the bracket. The
    vector kernels need one: the scalar core cannot load the vector scratchpad,
    so reading results back is itself vector work -- and timing it would charge
    the kernel for a readback its scalar twin does outside the bracket.
    """
    got = BUILDERS[name]()
    if len(got) == 4:
        pre, kernel, post, ckw = got
    else:
        pre, kernel, ckw = got
        post = ""
    return (zero(O_OFF, ckw) + pre + CYC_START + "\nkern_start:\n" + kernel +
            "\nkern_end:\n" + CYC_END + post + report(ckw, O_OFF))
