/* L1 entry points. A user trap saves into the running thread's frame
 * (sscratch holds it while user code runs), the kernel runs on its own stack,
 * and k_resume restores whichever thread k_trap chose. The kernel itself is
 * never interrupted: it runs with sstatus.SIE clear and polls when idle.
 * Translation is on only in user mode: the trap entry turns it off before
 * its first memory access, k_resume turns it on after its last (abi.h). */

    .section .text.k_entry, "ax"
    .globl  k_entry
k_entry:                        /* from L0: a0 = boot table copy, a1 = node */
    la      sp, __k_stack_top
    la      t0, s_trap_entry
    csrw    stvec, t0
    csrw    sscratch, zero
    call    k_main
1:  j       1b

    .align  4
    .globl  s_trap_entry
s_trap_entry:
    csrw    satp, zero
    csrrw   tp, sscratch, tp
    beqz    tp, s_trap_kernel
    sd      x1, 8(tp)
    sd      x2, 16(tp)
    sd      x3, 24(tp)
    sd      x5, 40(tp)
    sd      x6, 48(tp)
    sd      x7, 56(tp)
    sd      x8, 64(tp)
    sd      x9, 72(tp)
    sd      x10, 80(tp)
    sd      x11, 88(tp)
    sd      x12, 96(tp)
    sd      x13, 104(tp)
    sd      x14, 112(tp)
    sd      x15, 120(tp)
    sd      x16, 128(tp)
    sd      x17, 136(tp)
    sd      x18, 144(tp)
    sd      x19, 152(tp)
    sd      x20, 160(tp)
    sd      x21, 168(tp)
    sd      x22, 176(tp)
    sd      x23, 184(tp)
    sd      x24, 192(tp)
    sd      x25, 200(tp)
    sd      x26, 208(tp)
    sd      x27, 216(tp)
    sd      x28, 224(tp)
    sd      x29, 232(tp)
    sd      x30, 240(tp)
    sd      x31, 248(tp)
    csrr    t0, sscratch
    sd      t0, 32(tp)
    csrr    t0, sepc
    sd      t0, 256(tp)
    csrw    sscratch, zero
    mv      a0, tp
    la      sp, __k_stack_top
    call    k_trap
    /* k_trap does not return: it ends in k_resume */
2:  j       2b

/* A trap taken in the kernel itself is a kernel bug. */
s_trap_kernel:
    csrrw   tp, sscratch, tp    /* put tp back */
    la      sp, __k_stack_top
    csrr    a0, scause
    csrr    a1, sepc
    csrr    a2, stval
    call    k_kernel_trap
3:  j       3b

/* k_resume(t): t's frame is at t. */
    .globl  k_resume
k_resume:
    ld      t0, 256(a0)
    csrw    sepc, t0
    li      t0, 0x100           /* SPP = U */
    csrc    sstatus, t0
    csrw    sscratch, a0
    ld      x1, 8(a0)
    ld      x2, 16(a0)
    ld      x3, 24(a0)
    ld      x4, 32(a0)
    ld      x5, 40(a0)
    ld      x6, 48(a0)
    ld      x7, 56(a0)
    ld      x8, 64(a0)
    ld      x9, 72(a0)
    ld      x11, 88(a0)
    ld      x12, 96(a0)
    ld      x13, 104(a0)
    ld      x14, 112(a0)
    ld      x15, 120(a0)
    ld      x16, 128(a0)
    ld      x17, 136(a0)
    ld      x18, 144(a0)
    ld      x19, 152(a0)
    ld      x20, 160(a0)
    ld      x21, 168(a0)
    ld      x22, 176(a0)
    ld      x23, 184(a0)
    ld      x24, 192(a0)
    ld      x25, 200(a0)
    ld      x26, 208(a0)
    ld      x27, 216(a0)
    ld      x28, 224(a0)
    ld      x29, 232(a0)
    ld      x30, 240(a0)
    ld      x31, 248(a0)
    ld      x10, 80(a0)        /* a0's own value, parked in stval */
    csrw    stval, x10
    la      x10, k_user_satp
    ld      x10, 0(x10)
    csrw    satp, x10          /* from here on no load or store */
    csrr    x10, stval
    sret

/* struct sbiret k_l0(fn, a0, a1, a2, a3) */
    .globl  k_l0
k_l0:
    mv      a7, a0
    mv      a0, a1
    mv      a1, a2
    mv      a2, a3
    mv      a3, a4
    ecall
    ret
