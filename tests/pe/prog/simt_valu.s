# The PER-THREAD integer ALU, every operation, on lane-varying data.
#
# WHY THIS EXISTS. Between them the other shaders reach add, addi, andi, slli
# and sub on the per-thread file and NOTHING ELSE -- no variable shift, no
# compare, no xor/or, no immediate compare. kht_valu is LANES copies of one
# datapath, so a mistake there is wrong in every lane of every shader at once,
# and until this file the suite could not have seen it. The scalar half's
# equivalents are all in gpu_isa; these are the other ALU.
#
# THE SIGNED/UNSIGNED PAIR IS THE POINT of the compares: slt and sltu are handed
# the SAME operands and must disagree. A compare folded onto the wrong carry is
# right for one of them.
#
#   python tests/pe/tools/rv_simt_run.py tests/pe/prog/simt_valu.s \
#       --arg 0x80000000 --dram zero

        rdctl   s1, 0           # the DRAM base
        smv     s2, s1          # the output cursor

        vlaneid x5              # 0 .. 7
        addi    x6, x5, 1       # 1 .. 8
        addi    x7, x5, -4      # -4 .. 3, so the sign bit varies across lanes
        slli    x9, x5, 2       # 4 * lane
        addi    x17, x0, -1     # 0xFFFFFFFF
        addi    x22, x0, 1
        slli    x22, x22, 31
        add     x22, x22, x5    # 0x80000000 + lane: sign set, low bits vary

# ---- register-register ----------------------------------------------------
        sub     x8, x9, x5      # 3 * lane
        vsinw2  x8, s2
        saddi   s2, s2, 32

        xor     x12, x9, x6
        vsinw2  x12, s2
        saddi   s2, s2, 32

        or      x13, x9, x6
        vsinw2  x13, s2
        saddi   s2, s2, 32

        and     x14, x9, x6
        vsinw2  x14, s2
        saddi   s2, s2, 32

# ---- the compares ---------------------------------------------------------
        slt     x10, x7, x0     # (lane-4) < 0 signed   -> 1,1,1,1,0,0,0,0
        vsinw2  x10, s2
        saddi   s2, s2, 32

        sltu    x11, x7, x0     # the same pair unsigned -> 0 in every lane
        vsinw2  x11, s2
        saddi   s2, s2, 32

        slt     x10, x5, x6     # lane < lane+1 -> 1
        vsinw2  x10, s2
        saddi   s2, s2, 32

        sltu    x11, x6, x5     # lane+1 < lane -> 0
        vsinw2  x11, s2
        saddi   s2, s2, 32

# ---- the variable shifter, both directions and both right-shift fills -----
        sll     x15, x6, x5     # (lane+1) << lane
        vsinw2  x15, s2
        saddi   s2, s2, 32

        srl     x16, x22, x5    # logical: the sign bit walks down
        vsinw2  x16, s2
        saddi   s2, s2, 32

        sra     x19, x22, x5    # arithmetic: it smears instead
        vsinw2  x19, s2
        saddi   s2, s2, 32

        srl     x16, x17, x5    # 0xFFFFFFFF >> lane
        vsinw2  x16, s2
        saddi   s2, s2, 32

# Only bits [4:0] of the operand are the shift amount, so 28..35 has to wrap to
# 28..31, 0..3 -- a shifter built 33 bits wide gets this wrong at the seam.
        addi    x25, x5, 28
        sll     x26, x6, x25
        vsinw2  x26, s2
        saddi   s2, s2, 32

        sra     x27, x22, x25
        vsinw2  x27, s2
        saddi   s2, s2, 32

# ---- the immediate forms --------------------------------------------------
        slti    x10, x7, 0      # -> 1,1,1,1,0,0,0,0
        vsinw2  x10, s2
        saddi   s2, s2, 32

        sltiu   x11, x7, 0      # -> 0 in every lane
        vsinw2  x11, s2
        saddi   s2, s2, 32

        xori    x12, x9, 85
        vsinw2  x12, s2
        saddi   s2, s2, 32

        ori     x13, x9, 85
        vsinw2  x13, s2
        saddi   s2, s2, 32

        srli    x16, x22, 5
        vsinw2  x16, s2
        saddi   s2, s2, 32

        srai    x19, x22, 5
        vsinw2  x19, s2
        saddi   s2, s2, 32

        addi    x10, x0, 0x55
        ecall
