# Divergence on real hardware: odd and even lanes take different paths and
# reconverge, and the witness is that DRAM holds BOTH values in the right lanes.
#
# This is the shader that actually exercises G3. The ladder measures what the
# IPDOM stack COSTS; only a run proves the split pushed a pair, the first join
# took the false half, the second join took the outer mask, and the stack pointer
# came back to zero.
#
#   python tests/pe/tools/rv_simt_run.py tests/pe/prog/gpu_diverge.s \
#       --arg 0x80000000 --dram zero

        rdctl   s1, 0           # the DRAM base
        vlaneid x5              # x5 = this lane's index
        andi    x6, x5, 1       # predicate: odd lanes are true

        split   x6              # push {outer, false}; continue on ODD lanes
        addi    x7, x0, 0xAA    #   the true body
        join                    # pop the FALSE half; continue on EVEN lanes
        addi    x7, x0, 0x55    #   the false body
        join                    # pop the OUTER mask; reconverged, all 8 live

        vsinw2  x7, s1          # DRAM[base + lane*4] <- x7, every lane
        addi    x10, x0, 0x55   # the halt word
        ecall
