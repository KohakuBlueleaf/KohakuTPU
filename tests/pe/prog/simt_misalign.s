# A misaligned per-lane word access faults before any lane takes effect
# (compute-unit.md s5.1). The witness is the halt cause: 3, never the ecall.
#
#   python tests/pe/tools/rv_simt_run.py tests/pe/prog/simt_misalign.s \
#       --arg 0x80000000 --dram zero -d KHT_LSUD=4

        rdctl   s1, 0
        s2v     x1, s1
        vlaneid x5
        slli    x6, x5, 2
        add     x2, x1, x6

        lw      x3, 2(x2)       # every lane two bytes off a word: must FAULT

        addi    x10, x0, 0x55   # never reached
        ecall
