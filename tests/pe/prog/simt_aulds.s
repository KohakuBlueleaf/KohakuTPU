# LDS traffic only, from every wave: the address stage fills on LDS entries
# alone and no DRAM return ever arrives to free a refused wave.
#
# First a burst of eight stores with every lane on one bank: each takes LANES
# passes to drain and they issue back to back, so the stage fills from one wave
# alone. A wave refused by a full stage sleeps until an LDS entry leaves it, so
# a missed wake hangs the run rather than passing slowly. Every wave stores the
# same values to the burst's words, so the order between waves does not matter.
# Then each iteration stores twice to the wave's own LDS word and loads it back;
# the load must see the second store.
#
#   python tests/pe/tools/rv_simt_run.py tests/pe/prog/simt_aulds.s \
#       --arg 0x80000000 --dram zero --launch 16 -d KHT_LSUD=4 -d KHT_SCORE=1 -d KHT_MAU=4

        rdctl   s1, 0           # the DRAM base
        rdctl   s5, 5           # this wave's id
        sslli   s6, s5, 5       # 32 bytes a wave
        sadd    s7, s1, s6      # this wave's output
        vlaneid x5
        slli    x6, x5, 2
        lui     x14, 0x40000    # the LDS base
        s2v     x15, s6
        lui     x20, 0x40000
        slli    x21, x5, 5
        add     x20, x20, x21
        addi    x20, x20, 512   # LDS: 512 + lane * 32, every lane on one bank
        add     x14, x14, x15
        add     x14, x14, x6    # LDS: wave * 32 + lane * 4
        sw      x5, 0(x20)
        sw      x5, 4(x20)
        sw      x5, 8(x20)
        sw      x5, 12(x20)
        sw      x5, 16(x20)
        sw      x5, 20(x20)
        sw      x5, 24(x20)
        sw      x5, 28(x20)
        lw      x13, 28(x20)    # the lane's own id
        addi    x7, x0, 0
        saddi   s9, s0, 8
loop:
        add     x16, x7, x5
        sw      x16, 0(x14)
        addi    x17, x16, 3
        sw      x17, 0(x14)     # the load must see this one
        lw      x4, 0(x14)
        addi    x7, x7, 7
        add     x13, x13, x4
        xor     x13, x13, x7
        saddi   s9, s9, -1
        sbnez   s9, loop

        vsinw2  x13, s7
        addi    x10, x0, 0x55
        ecall
