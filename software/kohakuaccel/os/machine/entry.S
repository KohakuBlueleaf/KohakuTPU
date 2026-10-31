/* L0 entry: reset lands here in machine mode on every node. */
#include <kos/abi.h>

    .section .text.init, "ax"
    .globl _start
_start:
    la      sp, __m_stack_top
    la      t0, m_trap_entry
    csrw    mtvec, t0
    la      t0, m_frame
    csrw    mscratch, t0
    /* the scratchpad bss: L0's and the kernel's (the user bss is in DRAM, the kernel's to clear) */
    la      a0, __l0_bss_start
    la      a1, __l0_bss_end
    call    l0_zero
    la      a0, __k_bss_start
    la      a1, __k_bss_end
    call    l0_zero
    call    l0_main
2:  j       2b

/* zero [a0, a1), 8 bytes at a time */
l0_zero:
1:  bgeu    a0, a1, 3f
    sd      zero, 0(a0)
    addi    a0, a0, 8
    j       1b
3:  ret

/* Machine trap: the frame is m_frame (mscratch), the stack L0's own. */
    .align  4
    .globl  m_trap_entry
m_trap_entry:
    csrrw   sp, mscratch, sp
#ifdef KOS_L0_FAST
    sd      t0, 40(sp)
    sd      t1, 48(sp)
    csrr    t0, mcause
    li      t1, 9
    bne     t0, t1, .Lmfull
    li      t0, KOS_M_NOP
    beq     a7, t0, .Lmnop
    li      t0, KOS_M_SET_TIMER
    beq     a7, t0, .Lmtimer
    li      t0, KOS_M_QUERY
    beq     a7, t0, .Lmquery
.Lmfull:
    ld      t0, 40(sp)
    ld      t1, 48(sp)
#endif
    sd      x1, 8(sp)
    sd      x3, 24(sp)
    sd      x4, 32(sp)
    sd      x5, 40(sp)
    sd      x6, 48(sp)
    sd      x7, 56(sp)
#ifndef KOS_L0_SPLIT
    sd      x8, 64(sp)
    sd      x9, 72(sp)
#endif
    sd      x10, 80(sp)
    sd      x11, 88(sp)
    sd      x12, 96(sp)
    sd      x13, 104(sp)
    sd      x14, 112(sp)
    sd      x15, 120(sp)
    sd      x16, 128(sp)
    sd      x17, 136(sp)
#ifndef KOS_L0_SPLIT
    sd      x18, 144(sp)
    sd      x19, 152(sp)
    sd      x20, 160(sp)
    sd      x21, 168(sp)
    sd      x22, 176(sp)
    sd      x23, 184(sp)
    sd      x24, 192(sp)
    sd      x25, 200(sp)
    sd      x26, 208(sp)
    sd      x27, 216(sp)
#endif
    sd      x28, 224(sp)
    sd      x29, 232(sp)
    sd      x30, 240(sp)
    sd      x31, 248(sp)
    csrrw   t0, mscratch, sp
    sd      t0, 16(sp)
    mv      a0, sp
    la      sp, __m_stack_top
    call    l0_trap
    la      sp, m_frame
    ld      x1, 8(sp)
    ld      x3, 24(sp)
    ld      x4, 32(sp)
    ld      x5, 40(sp)
    ld      x6, 48(sp)
    ld      x7, 56(sp)
#ifndef KOS_L0_SPLIT
    ld      x8, 64(sp)
    ld      x9, 72(sp)
#endif
    ld      x10, 80(sp)
    ld      x11, 88(sp)
    ld      x12, 96(sp)
    ld      x13, 104(sp)
    ld      x14, 112(sp)
    ld      x15, 120(sp)
    ld      x16, 128(sp)
    ld      x17, 136(sp)
#ifndef KOS_L0_SPLIT
    ld      x18, 144(sp)
    ld      x19, 152(sp)
    ld      x20, 160(sp)
    ld      x21, 168(sp)
    ld      x22, 176(sp)
    ld      x23, 184(sp)
    ld      x24, 192(sp)
    ld      x25, 200(sp)
    ld      x26, 208(sp)
    ld      x27, 216(sp)
#endif
    ld      x28, 224(sp)
    ld      x29, 232(sp)
    ld      x30, 240(sp)
    ld      x31, 248(sp)
    ld      x2, 16(sp)
    mret

#ifdef KOS_L0_FAST
.Lmquery:
    sd      ra, 8(sp)
    sd      a2, 96(sp)
    sd      t2, 56(sp)
    la      a1, l0_root
    ld      a1, 0(a1)
    la      a2, l0_table_alias
    ld      a2, 0(a2)
    call    kos_walk
    mv      a1, a0
    ld      ra, 8(sp)
    ld      a2, 96(sp)
    ld      t2, 56(sp)
    j       .Lmdone
.Lmtimer:
    csrw    0x7c0, a0
.Lmnop:
    li      a1, 0
.Lmdone:
    li      a0, 0
    la      t0, l0_traps
    ld      t1, 0(t0)
    addi    t1, t1, 1
    sd      t1, 0(t0)
    csrr    t0, mepc
    addi    t0, t0, 4
    csrw    mepc, t0
    ld      t0, 40(sp)
    ld      t1, 48(sp)
    csrrw   sp, mscratch, sp
    mret
#endif

/* Enter the kernel: a0 = boot table copy, a1 = node, at k_entry in S, satp 0. */
    .globl  l0_enter_kernel
l0_enter_kernel:
    la      t0, k_entry
    csrw    mepc, t0
    li      t0, 0x1800
    csrc    mstatus, t0
    li      t0, 0x0800          /* MPP = S */
    csrs    mstatus, t0
    li      sp, 0
    mret

    .section .l0bss, "aw", @nobits
    .align  4
    .globl  m_frame
m_frame:
    .space  256
