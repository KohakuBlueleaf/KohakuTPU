# Nested divergence: a split inside a split, so the stack holds two pairs and
# the phase bit has to alternate correctly at every level.
#
# One flat if/else never exercises the phase bit past its first toggle -- this
# is the shader that does. Depth 8 permits four nested levels; this uses two.
#
#   lane:      0     1     2     3     4     5     6     7
#   expect:  0x33  0x22  0x33  0x11  0x33  0x22  0x33  0x11
#
#   python tests/pe/tools/rv_simt_run.py tests/pe/prog/gpu_nested.s \
#       --arg 0x80000000 --dram zero

        rdctl   s1, 0           # the DRAM base
        vlaneid x5
        andi    x6, x5, 1       # bit 0: odd lanes
        andi    x8, x5, 2       # bit 1

        split   x6              # A -> odd lanes
        split   x8              #   B -> odd AND bit1: lanes 3, 7
        addi    x7, x0, 0x11
        join                    #   pop B false -> odd AND !bit1: lanes 1, 5
        addi    x7, x0, 0x22
        join                    #   pop B outer -> back to all odd lanes
        join                    # pop A false -> even lanes
        addi    x7, x0, 0x33
        join                    # pop A outer -> reconverged, all 8

        vsinw2  x7, s1
        addi    x10, x0, 0x55
        ecall
