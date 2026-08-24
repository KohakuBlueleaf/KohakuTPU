# A REAL gather: every lane loads from its own base with an ordinary RV32I lw.
#
# There is no gather opcode -- an `lw` whose base register differs per lane IS
# one, and the coalescer sits under the ordinary load path. Lane i reads word
# 5i, so the eight lanes land on FIVE distinct 32-byte lines:
#
#   lane        0    1    2    3    4    5    6    7
#   byte     +  0   20   40   60   80  100  120  140
#   line 0 [  0.. 31]: lanes 0,1      line 3 [ 96..127]: lanes 5,6
#   line 1 [ 32.. 63]: lanes 2,3      line 4 [128..159]: lane  7
#   line 2 [ 64.. 95]: lane  4
#
# That is the coalescer's witness case: the serial LSU issues EIGHT requests
# for one gather today, and a coalescer must bring it to FIVE. Run it before
# and after G5 -- the ratio in the bench report is the whole measurement.
#
#   python tests/pe/tools/rv_simt_run.py tests/pe/prog/gpu_gather.s \
#       --arg 0x80000000 --dram ramp

        rdctl   s1, 0           # the DRAM base
        saddi   s2, s1, 1024    # the output, clear of what we read
        s2v     x1, s1          # the base, uniform in every lane
        vlaneid x5

        slli    x6, x5, 2       # lane * 4
        add     x6, x6, x5      # lane * 5
        slli    x6, x6, 2       # byte offset = lane * 20
        add     x2, x1, x6      # PER-LANE base address

        lw      x3, 0(x2)       # the gather
        add     x3, x3, x5      # touch it, so a lane reading the wrong word shows

        vsinw2  x3, s2          # lane-linear store back, one line
        addi    x10, x0, 0x55
        ecall
