# G7's shader: many waves, each writing its own slice of DRAM.
#
# Every wave runs the SAME code and must reach a DIFFERENT place, which is what
# `rdctl 5` is for. Wave w writes 32 bytes at base + 32w, so the final image is
# only correct if every launched wave ran, none of them collided, and none of
# them was killed by another wave's ecall.
#
# It is also the register-file test the single-wave shaders cannot be: s3 and x6
# hold different values in every wave at the same time, so a wave id missing
# from a register address shows up as one wave's data in another's slice.
#
#   python tests/pe/tools/rv_simt_run.py tests/pe/prog/gpu_waves.s \
#       --arg 0x80000000 --dram zero --launch 8

        rdctl   s1, 0           # the DRAM base
        rdctl   s3, 5           # THIS wave's id

        sslli   s4, s3, 5       # wave * 32 bytes
        sadd    s2, s1, s4      # this wave's slice

        vlaneid x5
        sslli   s5, s3, 4       # wave * 16
        s2v     x6, s5
        add     x6, x6, x5      # wave*16 + lane, unique across the dispatch

        vsinw2  x6, s2
        addi    x10, x0, 0x55
        ecall
