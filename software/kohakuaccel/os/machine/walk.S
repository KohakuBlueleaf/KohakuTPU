/* Sv39 query: a0 VA, a1 physical root, a2 table-access alias.
 * Returns the 4-KB-equivalent leaf PTE, or zero for a hole. */
#if defined(KOS_XLAT) || defined(KOS_L0_FAST)
    .section .text.kos_walk, "ax"
    .globl kos_walk
kos_walk:
    li      t0, 30
.Lwalk:
    srl     t1, a0, t0
    andi    t1, t1, 511
    slli    t1, t1, 3
    or      a1, a1, a2
    add     t1, t1, a1
    ld      t1, 0(t1)
    andi    t2, t1, 1
    beqz    t2, .Lhole
    andi    t2, t1, 14
    bnez    t2, .Lleaf
    srli    a1, t1, 10
    slli    a1, a1, 36
    srli    a1, a1, 24
    addi    t0, t0, -9
    li      t2, 12
    bge     t0, t2, .Lwalk
.Lhole:
    li      a0, 0
    ret
.Lleaf:
    addi    t0, t0, -12
    li      t2, 1
    sll     t2, t2, t0
    addi    t2, t2, -1
    srli    a0, a0, 12
    and     a0, a0, t2
    slli    a0, a0, 10
#if defined(KOS_HOT_MUT) && KOS_HOT_MUT == 8
    li      a0, 0
#endif
    add     a0, a0, t1
    ret
#endif
