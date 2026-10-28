/* M0 probes' runtime: machine mode, one trap entry for every privilege, the
 * frame saved on the machine stack and handed to m0_trap(frame). */

    .section .text.init, "ax"
    .globl _start
_start:
    la      sp, __stack_top
    la      t0, m0_trap_entry
    csrw    mtvec, t0
    la      t0, __bss_start
    la      t1, __bss_end
1:  bgeu    t0, t1, 2f
    sd      zero, 0(t0)
    addi    t0, t0, 8
    j       1b
2:  call    main
    li      t0, 0x20000
    sd      a0, 0(t0)
3:  j       3b

/* Frame: x1..x31 at 8*i, mepc at 8*32. The trap stack is the top of the
 * machine stack region (__trap_top), so code running below it keeps its own. */
    .align  4
    .globl  m0_trap_entry
m0_trap_entry:
    csrw    mscratch, sp
    la      sp, __trap_top
    addi    sp, sp, -272
    sd      x1, 8(sp)
    sd      x3, 24(sp)
    sd      x4, 32(sp)
    sd      x5, 40(sp)
    sd      x6, 48(sp)
    sd      x7, 56(sp)
    sd      x8, 64(sp)
    sd      x9, 72(sp)
    sd      x10, 80(sp)
    sd      x11, 88(sp)
    sd      x12, 96(sp)
    sd      x13, 104(sp)
    sd      x14, 112(sp)
    sd      x15, 120(sp)
    sd      x16, 128(sp)
    sd      x17, 136(sp)
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
    sd      x28, 224(sp)
    sd      x29, 232(sp)
    sd      x30, 240(sp)
    sd      x31, 248(sp)
    csrr    t0, mscratch
    sd      t0, 16(sp)
    csrr    t0, mepc
    sd      t0, 256(sp)
    mv      a0, sp
    call    m0_trap
    ld      t0, 256(sp)
    csrw    mepc, t0
    ld      x1, 8(sp)
    ld      x3, 24(sp)
    ld      x4, 32(sp)
    ld      x5, 40(sp)
    ld      x6, 48(sp)
    ld      x7, 56(sp)
    ld      x8, 64(sp)
    ld      x9, 72(sp)
    ld      x10, 80(sp)
    ld      x11, 88(sp)
    ld      x12, 96(sp)
    ld      x13, 104(sp)
    ld      x14, 112(sp)
    ld      x15, 120(sp)
    ld      x16, 128(sp)
    ld      x17, 136(sp)
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
    ld      x28, 224(sp)
    ld      x29, 232(sp)
    ld      x30, 240(sp)
    ld      x31, 248(sp)
    ld      x2, 16(sp)
    mret

/* Supervisor trap entry for the probes: a0..a3 = scause, sepc, stval, a7 to
 * m0_strap on the supervisor's own stack; returns to sepc + (a0 returned). */
    .align  4
    .globl  m0_strap_entry
m0_strap_entry:
    addi    sp, sp, -128
    sd      ra, 0(sp)
    sd      a0, 8(sp)
    sd      a1, 16(sp)
    sd      a2, 24(sp)
    sd      a3, 32(sp)
    sd      a4, 40(sp)
    sd      a5, 48(sp)
    sd      a6, 56(sp)
    sd      a7, 64(sp)
    sd      t0, 72(sp)
    sd      t1, 80(sp)
    sd      t2, 88(sp)
    sd      t3, 96(sp)
    sd      t4, 104(sp)
    sd      t5, 112(sp)
    sd      t6, 120(sp)
    csrr    a0, scause
    csrr    a1, sepc
    csrr    a2, stval
    mv      a3, a7
    call    m0_strap
    csrr    t0, sepc
    add     t0, t0, a0
    csrw    sepc, t0
    ld      ra, 0(sp)
    ld      a0, 8(sp)
    ld      a1, 16(sp)
    ld      a2, 24(sp)
    ld      a3, 32(sp)
    ld      a4, 40(sp)
    ld      a5, 48(sp)
    ld      a6, 56(sp)
    ld      a7, 64(sp)
    ld      t0, 72(sp)
    ld      t1, 80(sp)
    ld      t2, 88(sp)
    ld      t3, 96(sp)
    ld      t4, 104(sp)
    ld      t5, 112(sp)
    ld      t6, 120(sp)
    addi    sp, sp, 128
    sret

/* Enter `fn(arg)` at privilege `mpp` (1 = S, 0 = U) on stack `sp`; it returns
 * by ecall, which m0_trap turns back into a return from m0_enter. */
    .globl  m0_enter
m0_enter:                 /* a0 fn, a1 arg, a2 sp, a3 mpp */
    addi    sp, sp, -112
    sd      ra, 0(sp)
    sd      s0, 8(sp)
    sd      s1, 16(sp)
    sd      s2, 24(sp)
    sd      s3, 32(sp)
    sd      s4, 40(sp)
    sd      s5, 48(sp)
    sd      s6, 56(sp)
    sd      s7, 64(sp)
    sd      s8, 72(sp)
    sd      s9, 80(sp)
    sd      s10, 88(sp)
    sd      s11, 96(sp)
    la      t0, m0_saved_sp
    sd      sp, 0(t0)
    csrw    mepc, a0
    li      t0, 0x1800
    csrc    mstatus, t0
    slli    a3, a3, 11
    csrs    mstatus, a3
    mv      sp, a2
    mv      a0, a1
    la      ra, m0_lower_return
    mret
m0_lower_return:          /* the lower-privilege fn returned: ecall 0xFF */
    li      a7, 0xFF
    ecall
1:  j       1b

    .globl  m0_resume
m0_resume:                /* a0 = value; back on the machine stack */
    la      t0, m0_saved_sp
    ld      sp, 0(t0)
    ld      ra, 0(sp)
    ld      s0, 8(sp)
    ld      s1, 16(sp)
    ld      s2, 24(sp)
    ld      s3, 32(sp)
    ld      s4, 40(sp)
    ld      s5, 48(sp)
    ld      s6, 56(sp)
    ld      s7, 64(sp)
    ld      s8, 72(sp)
    ld      s9, 80(sp)
    ld      s10, 88(sp)
    ld      s11, 96(sp)
    addi    sp, sp, 112
    ret

    .section .bss
    .align 3
    .globl m0_saved_sp
m0_saved_sp: .dword 0
