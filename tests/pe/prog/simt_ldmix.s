# Loads returning while other waves' ALU results write the same register file.
#
# Under SCORE a queued load writes its words on a cycle the pipe leaves free
# (compute-unit.md s4.3). The first load brings each wave's line in; the loop's
# loads then HIT and return within a few cycles, into a stream of independent
# ALU writes from sixteen waves. A return that took the port from the pipe loses
# that ALU result, and the sum stored at the end shows it.
#
#   python tests/pe/tools/rv_simt_run.py tests/pe/prog/simt_ldmix.s \
#       --arg 0x80000000 --dram ramp --launch 16 -d KHT_LSUD=4 -d KHT_SCORE=1

        rdctl   s1, 0           # the DRAM base
        rdctl   s5, 5           # this wave's id
        sslli   s6, s5, 6       # 64 bytes a wave
        sadd    s7, s1, s6      # this wave's input
        saddi   s8, s7, 1024    # and its output, clear of every input
        s2v     x1, s7
        vlaneid x5
        slli    x6, x5, 2
        add     x2, x1, x6

        lw      x3, 0(x2)       # brings the line in
        add     x13, x3, x0
        addi    x7, x0, 0
        addi    x8, x0, 1
        saddi   s9, s0, 12
loop:
        lw      x4, 0(x2)       # hits
        addi    x7, x7, 3
        xor     x8, x8, x7
        add     x9, x7, x8
        slli    x10, x9, 1
        sub     x11, x10, x7
        or      x12, x11, x5
        add     x13, x13, x4
        add     x13, x13, x12
        saddi   s9, s9, -1
        sbnez   s9, loop

        vsinw2  x13, s8
        addi    x10, x0, 0x55
        ecall
