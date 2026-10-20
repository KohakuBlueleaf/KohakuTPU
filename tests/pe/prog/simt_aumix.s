# LDS and DRAM loads in flight together, from every wave, into one port.
#
# Each iteration loads a DRAM word, stores a value to the wave's LDS slot and
# loads it straight back. With the address stage (MEM_AU) the LDS load returns
# through the same register-file port as kht_lsu's DRAM returns and the pipe's
# ALU writes; a return lost or written to the wrong wave, register or lane shows
# in the sum stored at the end, and an LDS load that passed its store shows too.
#
#   python tests/pe/tools/rv_simt_run.py tests/pe/prog/simt_aumix.s \
#       --arg 0x80000000 --dram ramp --launch 16 -d KHT_LSUD=4 -d KHT_SCORE=1 -d KHT_MAU=4

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
        addi    x7, x0, 0
        addi    x13, x0, 0
        saddi   s9, s0, 8
loop:
        lw      x3, 0(x2)       # DRAM
        add     x16, x7, x5
        sw      x16, 0(x14)     # LDS store
        lw      x4, 0(x14)      # LDS load: must see the store
        addi    x7, x7, 7
        add     x13, x13, x3
        add     x13, x13, x4
        xor     x13, x13, x7
        addi    x2, x2, 4
        saddi   s9, s9, -1
        sbnez   s9, loop

        vsinw2  x13, s8
        addi    x10, x0, 0x55
        ecall
