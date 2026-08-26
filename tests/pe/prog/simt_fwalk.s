# THE FLOAT PASS WALK: per-lane DISTINCT float operands.
#
# Why this shader exists rather than a case added to simt_f32.s: there every
# float operand is UNIFORM across the lanes, and the one per-lane value is built
# by the INTEGER lanes after the float has retired. So a build whose float units
# serve the wrong threads -- FLANES < LANES with the walk's placement wrong, or
# the seed units' mapping crossed with the FMA units' -- writes the right answer
# into every lane and that shader passes.
#
# Here lane i's operand is 2^i, so every lane's answer differs from every other
# lane's and a crossed placement is a wrong word.
#
# THE MASKED FLOAT at the end is the second half of it: lanes 4..7 are inactive
# across a multi-pass instruction, so a pass whose write enable ignores the mask
# -- or one whose pass gating is wrong -- overwrites a value the ISA promises to
# leave alone.
#
#   0x3F800000 = 1.0   0x40000000 = 2.0   0x40400000 = 3.0
#
#   python tests/pe/tools/rv_simt_run.py tests/pe/prog/simt_fwalk.s \
#       --arg 0x80000000 --dram zero

        rdctl   s1, 0           # the kick argument: the DRAM base

        vlaneid x1              # lane index 0..7

        # Lane i holds 2^i: one exponent step per lane.
        slli    x11, x1, 23
        addi    x12, x0, 0x3F8
        slli    x12, x12, 20    # x12 = 1.0f
        add     x13, x12, x11   # x13 = 2^i, per lane
        addi    x14, x0, 0x404
        slli    x14, x14, 20    # x14 = 3.0f, uniform
        addi    x20, x0, 0x400
        slli    x20, x20, 20    # x20 = 2.0f, uniform

        vfmul   x15, x13, x14   # 3 * 2^i
        vsinw2  x15, s1         # slot 0

        vfadd   x16, x13, x13   # 2^i + 2^i, BOTH operands per lane
        saddi   s2, s1, 32
        vsinw2  x16, s2         # slot 1

        vfsub   x21, x13, x12   # 2^i - 1.0
        saddi   s2, s1, 64
        vsinw2  x21, s2         # slot 2

        # vfma reads its destination, so the ADDEND is per lane too.
        add     x17, x12, x11   # x17 = 2^i
        vfma    x17, x13, x14   # 2^i + 2^i*3 = 4 * 2^i
        saddi   s2, s1, 96
        vsinw2  x17, s2         # slot 3

        vfmul   x22, x13, x13   # 2^i * 2^i = 2^2i, both per lane
        saddi   s2, s1, 128
        vsinw2  x22, s2         # slot 4

        vfsub   x23, x12, x13   # 1.0 - 2^i, the per-lane operand SUBTRACTED
        saddi   s2, s1, 160
        vsinw2  x23, s2         # slot 5

        # A DEPENDENT CHAIN on per-lane data: each vfma waits for the one before
        # it and the wave is unrunnable throughout.
        add     x24, x12, x11   # x24 = 2^i
        vfma    x24, x13, x20
        vfma    x24, x13, x20
        saddi   s2, s1, 192
        vsinw2  x24, s2         # slot 6

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
