# The SIMT PE's first shader: every lane computes lane*7+1 and stores it to
# DRAM at base + lane*4, lane-linear.
#
# It exercises the whole machine rather than a corner: the shader image arrives
# as a CU_DATA burst, the kick starts it, the scalar side reads its base pointer
# from a control slot, the per-thread side does RV32I arithmetic in eight lanes,
# and the store goes out through the real L1, the real MAG and into the AXI RAM.
# The witness is the dumped DRAM state.
#
#   python tests/pe/tools/rv_simt_run.py tests/pe/prog/gpu_smoke.s \
#       --arg 0x80000000 --dram zero

        rdctl   s1, 0           # the kick argument: the DRAM base
        vlaneid x5              # x5 = this lane's index
        slli    x7, x5, 3       # lane * 8
        sub     x7, x7, x5      # lane * 7
        addi    x7, x7, 1       # lane * 7 + 1
        vsinw2  x7, s1          # DRAM[base + lane*4] <- x7
        addi    x10, x0, 0x55   # the halt word, read from lane 0
        ecall
