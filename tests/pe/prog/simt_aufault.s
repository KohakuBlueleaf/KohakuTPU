# A store behind a faulting access must not land, and one before it must.
#
# With the address stage (MEM_AU) the faulting load has already left EX when its
# fault is found, so the store behind it is in flight too; the stage drops it.
# The witness is the golden DRAM (the first store only) and the halt word, the
# faulting load's pc rather than the ecall's.
#
#   python tests/pe/tools/rv_simt_run.py tests/pe/prog/simt_aufault.s \
#       --arg 0x80000000 --dram zero -d KHT_LSUD=4 -d KHT_SCORE=1 -d KHT_MAU=4

        rdctl   s1, 0
        s2v     x1, s1
        vlaneid x5
        addi    x8, x5, 1
        slli    x6, x5, 2
        add     x2, x1, x6

        sw      x8, 0(x2)       # lands
        lw      x3, 2(x2)       # every lane two bytes off a word: must FAULT
        sw      x8, 64(x2)      # behind the fault: must not land

        addi    x10, x0, 0x55   # never reached
        ecall
