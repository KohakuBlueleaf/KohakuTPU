# G9: the float tier. FP32 is the only compute type, so vfma/vfmul/vfadd/vfsub
# are binary32 and there are no other forms.
#
# TWO CASES HERE ARE NOT ARITHMETIC CHECKS, they are FORMAT checks:
#
#   RANGE   2^100 * 2^-100 = 1.0. Neither operand exists in a narrower format,
#           so a datapath that quietly narrowed would answer NaN.
#   PRECISION  (1.0 + 1ulp) * 1.0 must come back as EXACTLY 1.0 + 1ulp. A
#           datapath carrying fewer than 24 mantissa bits drops that ulp; this
#           is what the migration off E8M15 bought.
#
# CONSTANTS COME FROM INTEGER IMMEDIATES: an FP32 bit pattern is a 32-bit
# integer, and every constant below is a 12-bit immediate shifted left 20.
#
#   0x3F800000 = 1.0    0x40000000 = 2.0    0x40400000 = 3.0
#   0x40C00000 = 6.0    0x40A00000 = 5.0    0xBF800000 = -1.0
#   0x71800000 = 2^100  0x0D800000 = 2^-100
#
#   python tests/pe/tools/rv_simt_run.py tests/pe/prog/gpu_f32.s \
#       --arg 0x80000000 --dram zero

        rdctl   s1, 0           # the kick argument: the DRAM base

        addi    x1, x0, 0x400
        slli    x1, x1, 20      # x1 = 2.0
        addi    x2, x0, 0x404
        slli    x2, x2, 20      # x2 = 3.0

        # vfmul: 2.0 * 3.0 = 6.0
        vfmul   x3, x1, x2
        vsinw2  x3, s1          # slot 0

        # vfadd: 2.0 + 3.0 = 5.0
        vfadd   x4, x1, x2
        saddi   s2, s1, 32
        vsinw2  x4, s2          # slot 1

        # vfsub: 2.0 - 3.0 = -1.0
        vfsub   x5, x1, x2
        saddi   s2, s1, 64
        vsinw2  x5, s2          # slot 2

        # vfma: vd += vs1 * vs2, so seed the destination first.
        # 2.0 + 2.0*3.0 = 8.0
        addi    x6, x0, 0x400
        slli    x6, x6, 20      # x6 = 2.0
        vfma    x6, x1, x2
        saddi   s2, s1, 96
        vsinw2  x6, s2          # slot 3

        # RANGE. 2^100 * 2^-100 = 1.0, over 200 binades apart.
        addi    x11, x0, 0x718
        slli    x11, x11, 20    # x11 = 2^100
        addi    x12, x0, 0x0D8
        slli    x12, x12, 20    # x12 = 2^-100
        vfmul   x13, x11, x12
        saddi   s2, s1, 128
        vsinw2  x13, s2         # slot 4, must be 0x3F800000

        # PRECISION. (1.0 + 1ulp) * 1.0 KEEPS the ulp: 24 mantissa bits.
        addi    x14, x0, 0x3F8
        slli    x14, x14, 20
        addi    x14, x14, 1     # x14 = 0x3F800001
        addi    x15, x0, 0x3F8
        slli    x15, x15, 20    # x15 = 1.0
        vfmul   x16, x14, x15
        saddi   s2, s1, 160
        vsinw2  x16, s2         # slot 5, must be 0x3F800001

        # A DEPENDENT CHAIN: each vfma waits the whole tier's latency for the
        # one before it and the wave is unrunnable throughout.
        # 1.0, then +1*1 three times = 4.0
        addi    x7, x0, 0x3F8
        slli    x7, x7, 20      # x7 = 1.0
        addi    x8, x0, 0x3F8
        slli    x8, x8, 20      # x8 = 1.0
        vfma    x7, x8, x8
        vfma    x7, x8, x8
        vfma    x7, x8, x8
        saddi   s2, s1, 192
        vsinw2  x7, s2          # slot 6, must be 4.0 = 0x40800000

        # PER-LANE DATA, not a uniform: lane i adds its own selector into the
        # exponent, so every lane must differ or a broadcast bug reads as a pass.
        vlaneid x9
        andi    x9, x9, 1       # 0 or 1 per lane
        slli    x9, x9, 23      # one exponent step
        add     x9, x9, x7      # 4.0 or 8.0, per lane
        saddi   s2, s1, 224
        vsinw2  x9, s2          # slot 7

        addi    x10, x0, 0x9F   # the halt word, read from lane 0
        ecall
