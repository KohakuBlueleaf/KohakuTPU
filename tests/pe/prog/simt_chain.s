# G7's witness: a dependency chain, which is the case interleaving actually pays.
#
# Every `add x6, x6, x6` depends on the one before it, so with ONE wave the
# distance-1 hazard stalls a cycle on every single instruction and the pipeline
# runs at half rate. With two or more waves the instruction behind it belongs to
# a DIFFERENT wave, the hazard does not exist, and the bubbles fill.
#
# So the measurement is: doubling the wave count should NOT double the cycles.
# `gpu_waves.s` cannot show this -- it is memory-bound on the serial LSU, which
# interleaving does not help until G6 lets a stalled wave step aside.
#
#   for n in 1 2 4; do
#     python tests/pe/tools/rv_simt_run.py tests/pe/prog/gpu_chain.s \
#         --arg 0x80000000 --dram zero --launch $n
#   done

        rdctl   s1, 0
        rdctl   s3, 5           # this wave's id
        sslli   s4, s3, 5
        sadd    s2, s1, s4      # this wave's slice

        vlaneid x5
        addi    x6, x5, 1

        add     x6, x6, x6      # a chain of twenty, each on the last
        add     x6, x6, x6
        add     x6, x6, x6
        add     x6, x6, x6
        add     x6, x6, x6
        add     x6, x6, x6
        add     x6, x6, x6
        add     x6, x6, x6
        add     x6, x6, x6
        add     x6, x6, x6
        add     x6, x6, x6
        add     x6, x6, x6
        add     x6, x6, x6
        add     x6, x6, x6
        add     x6, x6, x6
        add     x6, x6, x6
        add     x6, x6, x6
        add     x6, x6, x6
        add     x6, x6, x6
        add     x6, x6, x6

        add     x6, x6, x5      # fold the lane in so a lane mix-up still shows
        vsinw2  x6, s2
        addi    x10, x0, 0x55
        ecall
