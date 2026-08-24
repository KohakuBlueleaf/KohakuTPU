# THE FLOAT PASS WALK: per-lane DISTINCT float operands, in both formats.
#
# Why this shader exists rather than a case added to gpu_float.s / gpu_f32.s:
# in both of those every float operand is UNIFORM across the lanes, and the one
# per-lane value is built by the INTEGER lanes after the float has retired. So a
# build whose float units serve the wrong threads -- FLANES < LANES with the
# walk's placement wrong, or the seed units' mapping crossed with the FMA
# units' -- writes the right answer into every lane and both shaders pass.
#
# Here lane i's operand is 2^i in FP16 and again in FP32, so every lane's answer
# differs from every other lane's and a crossed placement is a wrong word.
#
# THE MASKED FLOAT at the end is the second half of it: lanes 4..7 are inactive
# across a multi-pass instruction, so a pass whose write enable ignores the mask
# -- or one whose pass gating is wrong -- overwrites a value the ISA promises to
# leave alone.
#
#   0x3C00 = 1.0 (FP16)       0x4000 = 2.0 (FP16)
#   0x3F800000 = 1.0 (FP32)   0x40400000 = 3.0 (FP32)
#
#   python tests/pe/tools/rv_simt_run.py tests/pe/prog/simt_fwalk.s \
#       --arg 0x80000000 --dram zero

        rdctl   s1, 0           # the kick argument: the DRAM base

        vlaneid x1              # lane index 0..7

        # ---- FP16: lane i holds 2^i, one exponent step per lane ----
        slli    x2, x1, 10      # i, at FP16's exponent weight
        addi    x3, x0, 0x3C0
        slli    x3, x3, 4       # x3 = 1.0
        add     x4, x3, x2      # x4 = 2^i, per lane
        addi    x5, x0, 0x400
        slli    x5, x5, 4       # x5 = 2.0, uniform

        vfmul_h x6, x4, x5      # 2^(i+1)
        vsinw2  x6, s1          # slot 0

        vfadd_h x7, x4, x4      # 2^i + 2^i = 2^(i+1), BOTH operands per lane
        saddi   s2, s1, 32
        vsinw2  x7, s2          # slot 1

        vfsub_h x8, x4, x3      # 2^i - 1.0
        saddi   s2, s1, 64
        vsinw2  x8, s2          # slot 2

        # vfma reads its destination, so the ADDEND is per lane too.
        add     x9, x3, x2      # x9 = 2^i
        vfma_h  x9, x4, x5      # 2^i + 2^i*2 = 3 * 2^i
        saddi   s2, s1, 96
        vsinw2  x9, s2          # slot 3

        # ---- FP32: the same shape at the wide format's exponent weight ----
        slli    x11, x1, 23
        addi    x12, x0, 0x3F8
        slli    x12, x12, 20    # x12 = 1.0f
        add     x13, x12, x11   # x13 = 2^i, per lane
        addi    x14, x0, 0x404
        slli    x14, x14, 20    # x14 = 3.0f, uniform

        vfmul   x15, x13, x14   # 3 * 2^i
        saddi   s2, s1, 128
        vsinw2  x15, s2         # slot 4

        vfadd   x16, x13, x12   # 2^i + 1
        saddi   s2, s1, 160
        vsinw2  x16, s2         # slot 5

        add     x17, x12, x11   # x17 = 2^i
        vfma    x17, x13, x14   # 2^i + 2^i*3 = 4 * 2^i
        saddi   s2, s1, 192
        vsinw2  x17, s2         # slot 6

        # ---- a MASKED float across every pass ----
        addi    x18, x0, 0x5A   # what lanes 4..7 must still hold afterwards
        slti    x19, x1, 4      # 1 in lanes 0..3
        split   x19
        vfmul   x18, x13, x14
        join
        saddi   s2, s1, 224
        vsinw2  x18, s2         # slot 7

        addi    x10, x0, 0x9F   # the halt word, read from lane 0
        ecall
