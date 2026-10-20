# Long results in flight while the pipe holds on memory, from every wave.
#
# Each iteration issues a float multiply that does not wait on the load beside
# it, a DRAM load whose miss holds the pipe, an LDS store and load, a float add
# on the loaded word and an integer multiply. A long result that lands while a
# held pipe instruction takes the register-file write port loses one of the
# two writes; the per-lane sum stored at the end shows it.
#
#   python tests/pe/tools/rv_simt_run.py tests/pe/prog/simt_fmix.s \
#       --arg 0x80000000 --dram ramp --launch 16 -d KHT_FLANES=2 -d KHT_SCORE=1 -d KHT_LSUD=4

        rdctl   s1, 0           # the DRAM base
        rdctl   s5, 5           # this wave's id
        sslli   s6, s5, 5       # 32 bytes a wave
        sadd    s7, s1, s6      # this wave's input
        saddi   s8, s7, 1024    # and its output, clear of every input
        s2v     x1, s7
        vlaneid x5
        slli    x6, x5, 2
        add     x2, x1, x6      # DRAM: input + lane * 4
        lui     x14, 0x40000    # the LDS base
        s2v     x15, s6
        add     x14, x14, x15
        add     x14, x14, x6    # LDS: wave * 32 + lane * 4
        lui     x20, 0x3F800    # 1.0
        lui     x21, 0x3FC00    # 1.5
        addi    x9, x20, 0      # last iteration's float, 1.0 at first
        addi    x7, x0, 0
        addi    x13, x0, 0
        saddi   s9, s0, 16
loop:
        vfmul   x8, x21, x9     # independent of this iteration's load
        lw      x3, 0(x2)       # DRAM
        add     x16, x7, x5
        sw      x16, 0(x14)     # LDS store
        andi    x9, x3, 0x7FF
        or      x9, x9, x20     # 1.0 plus the loaded word in ulps
        mul     x11, x16, x5
        lw      x4, 0(x14)      # LDS load: must see the store
        vfadd   x12, x9, x21
        add     x13, x13, x8
        xor     x13, x13, x12
        add     x13, x13, x11
        add     x13, x13, x4
        addi    x7, x7, 7
        addi    x2, x2, 4
        saddi   s9, s9, -1
        sbnez   s9, loop

        vsinw2  x13, s8
        addi    x10, x0, 0x55
        ecall
