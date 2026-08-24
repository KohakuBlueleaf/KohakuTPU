# G4's shader: the banked LDS, exercised at BOTH ends of its range.
#
# The banks are word-interleaved -- bank = addr[LNW-1:0] -- so the two cases
# are:
#
#   lane i touches word i        one lane per bank      ONE pass
#   lane i touches word 8i       every lane on bank 0   EIGHT passes
#
# Both are in here, and the bench's request count is what separates them: a
# serial walk costs LANES accesses for both, the resolver costs 1 and LANES.
# The reversed read is the third thing worth proving -- lane i reading word
# 7-i is still conflict-free, so the return crossbar has to route bank 7's
# word to lane 0 rather than lane 0 always taking bank 0.
#
#   python tests/pe/tools/rv_simt_run.py tests/pe/prog/gpu_lds.s \
#       --arg 0x80000000 --dram zero

        rdctl   s1, 0           # the DRAM base
        saddi   s2, s1, 1024    # a second DRAM output block
        saddi   s3, s0, 1
        sslli   s3, s3, 30      # the LDS base, 0x40000000

        vlaneid x5
        addi    x6, x5, 100     # the value this lane owns

# ---- conflict-free: lane i -> LDS word i, one lane per bank ---------------
        vsinw2  x6, s3

        addi    x7, x0, 7
        sub     x7, x7, x5      # 7 - lane
        vlw2    x8, s3, x7      # REVERSED, still one lane per bank
        vsinw2  x8, s1          # DRAM[base + 4i] = 107 - i

# ---- worst case: lane i -> LDS word 8i, every lane on bank 0 --------------
        slli    x9, x5, 3       # lane * 8
        vsw2    x6, s3, x9
        vlw2    x11, s3, x9
        vsinw2  x11, s2         # DRAM[base + 1024 + 4i] = 100 + i

        addi    x10, x0, 0x55
        ecall
