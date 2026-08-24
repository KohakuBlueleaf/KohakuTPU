# RV32M on the SIMT PE: mul, mulh, mulhsu, mulhu, per lane.
#
# THE SIGN CORNERS ARE THE POINT. mulh is signed x signed, mulhu unsigned x
# unsigned, mulhsu signed x unsigned -- three different high halves of the same
# two bit patterns, and getting the extension wrong produces a plausible number
# rather than an obvious one. 0x80000000 and 0xFFFFFFFF are the values that
# separate them, so every form is checked against both.
#
# It also runs `y * width + x` with a NON-CONSTANT width, which is the address
# computation the instruction exists for -- a compiler cannot strength-reduce it
# when the width arrives as a uniform.
#
#   python tests/pe/tools/rv_simt_run.py tests/pe/prog/gpu_mul.s \
#       --arg 0x80000000 --dram zero

        rdctl   s1, 0           # the kick argument: the DRAM base

        # ---- per-lane operands, so a broadcast bug cannot read as a pass ----
        vlaneid x5              # lane index 0..7
        addi    x6, x5, 1       # 1..8
        addi    x7, x0, -1      # 0xFFFFFFFF  (-1 signed)
        addi    x8, x0, 1
        slli    x8, x8, 31      # 0x80000000  (INT_MIN signed)

        # ---- mul: low half, extension-independent ----
        # lane i: (i+1) * 0x80000000 = (i+1)<<31, low 32 = (i&1) ? 0x80000000 : 0
        mul     x10, x6, x8
        vsinw2  x10, s1                 # slot 0

        # ---- mulh: signed x signed ----
        # (i+1) * INT_MIN, high half. Signed: negative, so high half is -(i+1)/2
        mulh    x11, x6, x8
        saddi   s2, s1, 32
        vsinw2  x11, s2                 # slot 1

        # ---- mulhu: unsigned x unsigned, SAME bit patterns ----
        # (i+1) * 0x80000000 unsigned, high half = (i+1)>>1
        mulhu   x12, x6, x8
        saddi   s2, s1, 64
        vsinw2  x12, s2                 # slot 2

        # ---- mulhsu: signed x unsigned. -1 * 0x80000000 ----
        # a signed = -1, b unsigned = 2^31 -> product -2^31, high half 0xFFFFFFFF
        mulhsu  x13, x7, x8
        saddi   s2, s1, 96
        vsinw2  x13, s2                 # slot 3

        # mulhu with the same operands is 0x7FFFFFFF -- the pair that proves the
        # sign of `a` is what mulhsu treats differently.
        mulhu   x14, x7, x8
        saddi   s2, s1, 128
        vsinw2  x14, s2                 # slot 4

        # ---- the address computation the instruction exists for ----
        # width arrives as a uniform, so this cannot strength-reduce.
        rdctl   s3, 1                   # coreid, a value the compiler cannot see
        andi    x15, x5, 3              # y = lane & 3
        addi    x16, x0, 37             # width = 37
        mul     x17, x15, x16
        add     x17, x17, x5            # y*width + x
        saddi   s2, s1, 160
        vsinw2  x17, s2                 # slot 5

        # ---- a MASKED multiply: lanes 0..3 only ----
        # The inactive half must keep its previous value, proving a masked lane
        # commits nothing through the multiplier's own retire slot.
        addi    x18, x0, 0x5A
        slti    x19, x5, 4              # 1 in lanes 0..3
        split   x19
        mul     x18, x6, x6             # (i+1)^2 in the active half only
        join
        saddi   s2, s1, 192
        vsinw2  x18, s2                 # slot 6

        addi    x10, x0, 0x4D           # the halt word, read from lane 0
        ecall
