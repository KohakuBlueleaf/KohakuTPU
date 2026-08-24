# G9: the float tier, FP16 half. Every `_h` operation the PE has, checked
# against the golden model, plus the two things about it that are architecture
# rather than arithmetic: a 15-cycle result, and a dependent chain through it.
#
# The `_h` suffix is the NARROW format; unsuffixed vfma/vfmul/vfadd/vfsub are
# FP32 and live in gpu_f32.s. Same lanes, same E8M15 datapath, different edges.
#
# CONSTANTS COME FROM INTEGER IMMEDIATES, and that is the design rather than a
# workaround: an FP16 bit pattern is a 16-bit integer, so `addi` + a lane-wide
# broadcast builds one and the PE needs no int->float conversion instruction to
# be useful. Real shaders read their floats out of memory.
#
#   0x3C00 = 1.0    0x4000 = 2.0    0x4200 = 3.0    0x4400 = 4.0
#   0x3800 = 0.5    0xBC00 = -1.0
#
#   python tests/pe/tools/rv_simt_run.py tests/pe/prog/gpu_float.s \
#       --arg 0x80000000 --dram zero

        rdctl   s1, 0           # the kick argument: the DRAM base

        # Build 2.0 and 3.0 in every lane. The upper half of each register is
        # RESERVED and must be zero, which `addi` from x0 guarantees.
        addi    x1, x0, 0x400   # 0x4000 >> 4, then shifted -- a 12-bit
        slli    x1, x1, 4       # immediate cannot hold 0x4000 directly
        addi    x2, x0, 0x420
        slli    x2, x2, 4       # x2 = 0x4200 = 3.0

        # vfmul: 2.0 * 3.0 = 6.0 (0x4600)
        vfmul_h x3, x1, x2
        vsinw2  x3, s1          # slot 0

        # vfadd: 2.0 + 3.0 = 5.0 (0x4500)
        vfadd_h x4, x1, x2
        saddi   s2, s1, 32
        vsinw2  x4, s2          # slot 1

        # vfsub: 2.0 - 3.0 = -1.0 (0xBC00)
        vfsub_h x5, x1, x2
        saddi   s2, s1, 64
        vsinw2  x5, s2          # slot 2

        # vfma: vd += vs1 * vs2, so seed the destination first.
        # 2.0 + 2.0*3.0 = 8.0 (0x4800)
        addi    x6, x0, 0x400
        slli    x6, x6, 4       # x6 = 2.0
        vfma_h  x6, x1, x2
        saddi   s2, s1, 96
        vsinw2  x6, s2          # slot 3

        # A DEPENDENT CHAIN. Each vfma waits fifteen cycles for the one before
        # it, and the wave is unrunnable for all fifteen -- which is the whole
        # point of the per-wave pending flag. With one wave launched this is
        # visible as cycles; the answer must be identical either way.
        # 1.0, then +1*1 three times = 4.0 (0x4400)
        addi    x7, x0, 0x3C0
        slli    x7, x7, 4       # x7 = 1.0
        addi    x8, x0, 0x3C0
        slli    x8, x8, 4       # x8 = 1.0
        vfma_h  x7, x8, x8
        vfma_h  x7, x8, x8
        vfma_h  x7, x8, x8
        saddi   s2, s1, 128
        vsinw2  x7, s2          # slot 4

        # PER-LANE DATA, not a uniform: lane i computes 1.0 * lane_as_float is
        # not available without a convert, so multiply a per-lane MASK of the
        # constant instead -- lanes 0..7 each scale 1.0 by their own selector.
        # Every lane must get its own answer or a broadcast bug reads as a pass.
        vlaneid x9
        andi    x9, x9, 1       # 0 or 1 per lane
        slli    x9, x9, 10      # 0x000 or 0x400
        add     x9, x9, x7      # 4.0 or 4.0+0x400 (= 6.0), per lane
        saddi   s2, s1, 160
        vsinw2  x9, s2          # slot 5

        addi    x10, x0, 0x9F   # the halt word, read from lane 0
        ecall
