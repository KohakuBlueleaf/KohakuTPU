# One store whose lanes 0-3 are DRAM and 4-7 LDS faults whole (compute-unit.md
# s5.1): cause 3, and the DRAM lanes wrote nothing, which the golden DRAM checks.
#
#   python tests/pe/tools/rv_simt_run.py tests/pe/prog/simt_mixed.s \
#       --arg 0x80000000 --dram zero -d KHT_LSUD=4

        rdctl   s1, 0
        s2v     x1, s1          # the DRAM base
        vlaneid x5
        slli    x6, x5, 2
        srli    x7, x5, 2       # 0 for lanes 0-3, 1 for lanes 4-7
        sub     x7, x0, x7      # ... as a mask of all ones
        lui     x8, 0x40000     # the LDS base
        sub     x8, x8, x1
        and     x8, x8, x7      # LDS minus DRAM, upper lanes only
        add     x2, x1, x6
        add     x2, x2, x8

        sw      x5, 0(x2)       # must FAULT, and lanes 0-3 must not land

        addi    x10, x0, 0x55   # never reached
        ecall
