/* L1 entry points. A user trap saves into the running thread's frame
 * (sscratch holds it while user code runs), the kernel runs on its own stack,
 * and k_resume restores whichever thread k_trap chose. The kernel itself is
 * never interrupted: it runs with sstatus.SIE clear and polls when idle.
 * Translation is on only in user mode: the trap entry turns it off before
 * its first memory access, k_resume turns it on after its last (abi.h). */

#include <kos/abi.h>

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
#if defined(KOS_FAST) && !defined(KOS_PROF)
    sd      t0, 40(tp)
    sd      t1, 48(tp)
    csrr    t0, scause
    li      t1, 8
    bne     t0, t1, .Lslow
    la      t0, kn
    lw      t0, 28(t0)
    andi    t0, t0, 40
    bnez    t0, .Lslow
    li      t0, SYS_SELF
    beq     a7, t0, .Lself
    li      t0, SYS_TIME
    beq     a7, t0, .Ltime
#ifdef KOS_CHAN_FIFO
    li      t0, SYS_EVENT
    beq     a7, t0, .Levent
#endif
    li      t0, SYS_INFO
    bne     a7, t0, .Lslow
    li      t0, 3
    bgeu    a0, t0, .Lslow
    la      t0, kn
    slli    t1, a0, 2
    add     t0, t0, t1
    lwu     a0, 0(t0)
    j       .Lleaf_done
#ifdef KOS_CHAN_FIFO
.Levent:
#ifndef KOS_EVENT_ASM
    slli    t0, a3, 32
    bnez    t0, .Lslow
#endif
    sd      t2, 56(tp)
    srli    t0, a3, 32
    srli    t1, t0, 16
    xor     t0, t0, t1
    andi    t0, t0, 63
    slli    t0, t0, 2
    la      t1, k_chan_buckets
    add     t0, t0, t1
    lwu     t0, 0(t0)
.Levent_find:
    beqz    t0, .Levent_count
    slli    t1, t0, 2
    add     t0, t0, t1
    slli    t0, t0, 2
    la      t1, k_channels
    add     t0, t0, t1
    lwu     t1, 0(t0)
    srli    t2, a3, 32
    beq     t1, t2, .Levent_found
    lwu     t0, 16(t0)
    j       .Levent_find
.Levent_found:
    lwu     t0, 12(t0)
.Levent_count:
    bnez    t0, .Levent_ready
    la      t1, k_events
    ld      t1, 0(t1)
    sub     t1, t1, a0
    bgez    t1, .Levent_ready
    ld      t2, 56(tp)
    j       .Lslow
.Levent_ready:
#ifdef KOS_EVENT_ASM
    slli    t1, a3, 32
    beqz    t1, .Levent_nocopy
    sd      t0, 0(tp)          /* queued count, not an architectural register */
    csrr    t0, cycle
    ld      t1, 344(tp)
    sub     t0, t0, t1
    ld      t1, 328(tp)
    add     t1, t1, t0
    sd      t1, 328(tp)
    sd      ra, 8(tp)
    sd      sp, 16(tp)
    sd      a2, 96(tp)
    sd      a3, 104(tp)
    sd      a4, 112(tp)
    sd      a5, 120(tp)
    sd      a6, 128(tp)
    sd      a7, 136(tp)
    sd      t3, 224(tp)
    sd      t4, 232(tp)
    sd      t5, 240(tp)
    sd      t6, 248(tp)
    csrr    t0, sscratch
    sd      t0, 32(tp)
    csrw    sscratch, zero
    la      sp, __k_stack_top
    mv      a0, a2
    slli    a1, a3, 32
    srli    a1, a1, 32
    call    k_event_out
    bnez    a0, .Levent_result
    ld      a0, 0(tp)
.Levent_result:
    ld      ra, 8(tp)
    ld      sp, 16(tp)
    ld      a2, 96(tp)
    ld      a3, 104(tp)
    ld      a4, 112(tp)
    ld      a5, 120(tp)
    ld      a6, 128(tp)
    ld      a7, 136(tp)
    ld      t3, 224(tp)
    ld      t4, 232(tp)
    ld      t5, 240(tp)
    ld      t6, 248(tp)
    ld      t0, 32(tp)
    csrw    sscratch, t0
    la      t1, k_events
    ld      a1, 0(t1)
    ld      t2, 56(tp)
    j       .Lleaf_stats
.Levent_nocopy:
#endif
    mv      a0, t0
.Levent_return:
    la      t1, k_events
    ld      a1, 0(t1)
    ld      t2, 56(tp)
    j       .Lleaf_done
#endif
.Lself:
#if defined(KOS_HOT_MUT) && KOS_HOT_MUT == 1
    li      a1, 0
#endif
    lwu     a0, 272(tp)
    j       .Lleaf_done
.Ltime:
    csrr    a0, time
.Lleaf_done:
    csrr    t0, cycle
    ld      t1, 344(tp)
    sub     t0, t0, t1
    ld      t1, 328(tp)
    add     t1, t1, t0
    sd      t1, 328(tp)
.Lleaf_stats:
    la      t0, kstat
    ld      t1, 0(t0)
    addi    t1, t1, 1
    sd      t1, 0(t0)
    ld      t1, 8(t0)
    addi    t1, t1, 1
    sd      t1, 8(t0)
    csrr    t0, sepc
    addi    t0, t0, 4
    csrw    sepc, t0
    csrr    t0, cycle
    sd      t0, 344(tp)
    csrw    stval, a0
    la      t0, k_user_satp
    ld      a0, 0(t0)
    ld      t0, 40(tp)
    ld      t1, 48(tp)
    csrrw   tp, sscratch, tp
    csrw    satp, a0
    csrr    a0, stval
    sret
.Lslow:
    ld      t0, 40(tp)
    ld      t1, 48(tp)
#endif
    sd      x1, 8(tp)
    sd      x2, 16(tp)
    sd      x3, 24(tp)
    sd      x5, 40(tp)
    sd      x6, 48(tp)
#ifdef KOS_PROF
    csrr    t0, cycle
    la      t1, k_prof_at
    sd      t0, 0(t1)
#endif
    sd      x7, 56(tp)
#ifndef KOS_SPLIT
    sd      x8, 64(tp)
    sd      x9, 72(tp)
#endif
    sd      x10, 80(tp)
    sd      x11, 88(tp)
    sd      x12, 96(tp)
    sd      x13, 104(tp)
    sd      x14, 112(tp)
    sd      x15, 120(tp)
    sd      x16, 128(tp)
    sd      x17, 136(tp)
#ifndef KOS_SPLIT
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
#endif
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
#ifdef KOS_PROF
    csrr    t0, cycle
    la      t1, k_prof_at
    sd      t0, 8(t1)
#endif
    call    k_trap
#ifdef KOS_SPLIT
    beq     a0, a1, .Lsame
    beqz    a1, k_resume
    sd      s0, 64(a1)
    sd      s1, 72(a1)
    sd      s2, 144(a1)
    sd      s3, 152(a1)
    sd      s4, 160(a1)
    sd      s5, 168(a1)
    sd      s6, 176(a1)
    sd      s7, 184(a1)
    sd      s8, 192(a1)
    sd      s9, 200(a1)
    sd      s10, 208(a1)
#if !defined(KOS_HOT_MUT) || KOS_HOT_MUT != 2
    sd      s11, 216(a1)
#endif
    j       k_resume
.Lsame:
    li      t2, 0
    j       .Lresume
#else
    j       k_resume
#endif

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
#ifdef KOS_SPLIT
    li      t2, 1
.Lresume:
#endif
#ifdef KOS_PROF
    csrr    t0, cycle
    la      t1, k_prof_at
    sd      t0, 48(t1)
#endif
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
#ifdef KOS_SPLIT
    beqz    t2, .Lcaller
#endif
    ld      x8, 64(a0)
    ld      x9, 72(a0)
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
.Lcaller:
    ld      x7, 56(a0)
    ld      x11, 88(a0)
    ld      x12, 96(a0)
    ld      x13, 104(a0)
    ld      x14, 112(a0)
    ld      x15, 120(a0)
    ld      x16, 128(a0)
    ld      x17, 136(a0)
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
