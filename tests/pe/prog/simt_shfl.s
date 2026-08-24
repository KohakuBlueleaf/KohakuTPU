# G8's shader: the subgroup butterfly, both instructions it serves and the
# masked case that fixes its semantics.
#
# One network covers both, because lane i has to end up holding vs1[src]:
#
#   shflxor  src = i ^ m     m uniform, from a scalar register
#   bcast    src = L         L uniform, an immediate
#
# so the per-lane control is src ^ i either way, and log2(LANES) conditional
# swaps route it. Three xor masks are used -- 1, 2 and 4 -- so EVERY stage of
# the butterfly is exercised rather than just the first.
#
#   python tests/pe/tools/rv_simt_run.py tests/pe/prog/gpu_shfl.s \
#       --arg 0x80000000 --dram zero

        rdctl   s1, 0           # the DRAM base
        saddi   s2, s1, 0       # the output cursor

        vlaneid x5
        addi    x6, x5, 100     # lane + 100, distinct per lane

# ---- shflxor across each butterfly stage in turn --------------------------
        saddi   s4, s0, 1
        shflxor x7, x6, s4      # stage 0: neighbour swap
        vsinw2  x7, s2
        saddi   s2, s2, 32

        saddi   s4, s0, 2
        shflxor x7, x6, s4      # stage 1
        vsinw2  x7, s2
        saddi   s2, s2, 32

        saddi   s4, s0, 4
        shflxor x7, x6, s4      # stage 2: the widest swap at 8 lanes
        vsinw2  x7, s2
        saddi   s2, s2, 32

        saddi   s4, s0, 7
        shflxor x7, x6, s4      # all three stages at once: full reversal
        vsinw2  x7, s2
        saddi   s2, s2, 32

# ---- bcast: one lane's value to every lane --------------------------------
        bcast   x8, x6, 0       # 100 everywhere
        vsinw2  x8, s2
        saddi   s2, s2, 32

        bcast   x8, x6, 5       # 105 everywhere
        vsinw2  x8, s2
        saddi   s2, s2, 32

# ---- under a mask: a lane whose SOURCE is inactive reads its own value ----
# Lanes 0 and 1 are off, so with an xor of 1 lane 2 would read lane 3 (active,
# fine) but lane 3 reads lane 2 (active). Use an xor of 4: lane 4 reads lane 0,
# which is OFF, so lane 4 must keep its own value rather than take a masked
# lane's. That is the property the ISA fixes and the network resolves BEFORE
# its first stage.
        saddi   s8, s0, 252     # 0b11111100
        tmc     s8

        saddi   s4, s0, 4
        shflxor x9, x6, s4
        vsinw2  x9, s2
        saddi   s2, s2, 32

        saddi   s8, s0, 255
        tmc     s8

        addi    x10, x0, 0x55
        ecall
