# EXECUTION coverage: every built instruction that is not already exercised by
# the other four shaders, run once and its result stored where the model can be
# compared against it.
#
# WHY THIS EXISTS. rv_simt_isa_test.py proves the field table, the assembler, the
# model and the RTL header agree BIT FOR BIT -- and proves nothing whatever
# about whether the datapath can execute the instruction. Three instructions
# (s2v, shflxor, bcast) decoded cleanly, passed every encoding test, set a write
# enable, and had no datapath behind them. An encoding test cannot catch that.
# Only running each instruction and looking at what came out can.
#
# Each result is broadcast to a wave and stored as one 32-byte block, so the
# bench names the failing instruction by its byte offset.
#
#   python tests/pe/tools/rv_simt_run.py tests/pe/prog/gpu_isa.s \
#       --arg 0x80000000 --dram zero

        rdctl   s1, 0           # the DRAM base
        saddi   s2, s1, 1024    # the output cursor (imm is signed 12-bit)

        vlaneid x5              # x5 = lane

# ---- custom-3: the scalar immediates, and s0 reading as zero ---------------
        saddi   s3, s0, 100     # 100          <- also proves s0 is zero
        s2v     x20, s3
        vsinw2  x20, s2
        saddi   s2, s2, 32

        sandi   s4, s3, 12      # 100 & 12 = 4
        s2v     x20, s4
        vsinw2  x20, s2
        saddi   s2, s2, 32

        sori    s4, s3, 3       # 100 | 3 = 103
        s2v     x20, s4
        vsinw2  x20, s2
        saddi   s2, s2, 32

        sslli   s4, s3, 3       # 800
        s2v     x20, s4
        vsinw2  x20, s2
        saddi   s2, s2, 32

        ssrli   s4, s3, 2       # 25
        s2v     x20, s4
        vsinw2  x20, s2
        saddi   s2, s2, 32

        ssrai   s4, s3, 2       # 25
        s2v     x20, s4
        vsinw2  x20, s2
        saddi   s2, s2, 32

# ---- custom-2 SALU: register-register --------------------------------------
        saddi   s5, s0, 7
        sadd    s6, s3, s5      # 107
        s2v     x20, s6
        vsinw2  x20, s2
        saddi   s2, s2, 32

        ssub    s6, s3, s5      # 93
        s2v     x20, s6
        vsinw2  x20, s2
        saddi   s2, s2, 32

        ssll    s6, s5, s5      # 7 << 7 = 896
        s2v     x20, s6
        vsinw2  x20, s2
        saddi   s2, s2, 32

        sslt    s6, s5, s3      # 7 < 100 -> 1
        s2v     x20, s6
        vsinw2  x20, s2
        saddi   s2, s2, 32

        ssltu   s6, s3, s5      # 100 < 7 unsigned -> 0
        s2v     x20, s6
        vsinw2  x20, s2
        saddi   s2, s2, 32

        sxor    s6, s3, s5      # 100 ^ 7 = 99
        s2v     x20, s6
        vsinw2  x20, s2
        saddi   s2, s2, 32

        ssrl    s6, s3, s5      # 100 >> 7 = 0
        s2v     x20, s6
        vsinw2  x20, s2
        saddi   s2, s2, 32

        ssra    s6, s3, s5      # 100 >> 7 arithmetic = 0
        s2v     x20, s6
        vsinw2  x20, s2
        saddi   s2, s2, 32

        sor     s6, s3, s5      # 100 | 7 = 103
        s2v     x20, s6
        vsinw2  x20, s2
        saddi   s2, s2, 32

        sand    s6, s3, s5      # 100 & 7 = 4
        s2v     x20, s6
        vsinw2  x20, s2
        saddi   s2, s2, 32

# ---- the subgroup ops: every vector-to-scalar path -------------------------
        addi    x6, x5, 1       # x6 = lane + 1, non-zero in every lane

        ballot  s7, x6          # every lane non-zero -> 0xFF
        s2v     x20, s7
        vsinw2  x20, s2
        saddi   s2, s2, 32

        reduxadd s7, x6         # 1+2+..+8 = 36
        s2v     x20, s7
        vsinw2  x20, s2
        saddi   s2, s2, 32

        reduxmax s7, x6         # 8
        s2v     x20, s7
        vsinw2  x20, s2
        saddi   s2, s2, 32

        reduxmin s7, x6         # 1
        s2v     x20, s7
        vsinw2  x20, s2
        saddi   s2, s2, 32

        reduxand s7, x6         # 1&2&..&8 = 0
        s2v     x20, s7
        vsinw2  x20, s2
        saddi   s2, s2, 32

        reduxor s7, x6          # 1|2|..|8 = 15
        s2v     x20, s7
        vsinw2  x20, s2
        saddi   s2, s2, 32

        vreadfirst s7, x6       # the lowest ACTIVE lane -> 1
        s2v     x20, s7
        vsinw2  x20, s2
        saddi   s2, s2, 32

# ---- rdctl: the wave id slot ----------------------------------------------
        rdctl   s7, 5           # this wave's id -> 0
        s2v     x20, s7
        vsinw2  x20, s2
        saddi   s2, s2, 32

# ---- tmc, and the reductions under a NON-TRIVIAL mask ---------------------
# vreadfirst must name the lowest ACTIVE lane, never lane 0. With lanes 0 and 1
# masked off it must report lane 2's value, which is the property that makes it
# usable at all.
        saddi   s8, s0, 252     # 0b11111100 -- lanes 0 and 1 off
        tmc     s8

        vreadfirst s7, x6       # lane 2's value -> 3, NOT 1
        s2v     x20, s7
        vsinw2  x20, s2
        saddi   s2, s2, 32

        ballot  s7, x6          # only the active lanes -> 0xFC
        s2v     x20, s7
        vsinw2  x20, s2
        saddi   s2, s2, 32

        reduxadd s7, x6         # 3+4+..+8 = 33
        s2v     x20, s7
        vsinw2  x20, s2
        saddi   s2, s2, 32

        saddi   s8, s0, 255     # every lane back on
        tmc     s8

# ---- signed reductions over NEGATIVE data ---------------------------------
# reduxmax over 1..8 passes with a broken identity of zero, because the answer
# happens to be above it. All-negative data is what actually tests the identity.
        addi    x7, x5, -8      # -8 .. -1

        reduxmax s7, x7         # -1
        s2v     x20, s7
        vsinw2  x20, s2
        saddi   s2, s2, 32

        reduxmin s7, x7         # -8
        s2v     x20, s7
        vsinw2  x20, s2
        saddi   s2, s2, 32

        addi    x10, x0, 0x55
        ecall
