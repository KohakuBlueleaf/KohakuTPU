# dotsum -- a worked example for tests/pe/tools/rv_run.py.
#
#   python tests/pe/tools/rv_run.py tests/pe/prog/dotsum.s
#
# Sums 64 words of global memory, writes the total back to DRAM, flushes, and
# halts with the total in a0. It touches every part of the memory map a real
# program uses: the internal L1 over DRAM, the local control region for the
# flush, and ecall to report.
#
# SPAD, DRAM, CTL_FLUSH and the rest are supplied by the assembler's symbol
# table (tests/pe/tools/rv_gen.py SYMS), so the memory map is never written
# twice.

    li   t0, DRAM              # the cursor
    li   t1, 64                # words remaining
    li   t2, 0                 # running total

sum_loop:
    lw   t3, 0(t0)
    add  t2, t2, t3
    addi t0, t0, 4
    addi t1, t1, -1
    bnez t1, sum_loop

    li   t4, DRAM+4096         # somewhere the loop did not touch
    sw   t2, 0(t4)

# The flush is a BLOCKING store: it does not complete until every dirty line has
# been written back AND acknowledged. Without it the total is still sitting in
# the cache when the unit reports completion.
    li   t6, CTL_FLUSH
    sw   x0, 0(t6)

    mv   a0, t2
    ecall
