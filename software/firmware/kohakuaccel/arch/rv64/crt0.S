/* Reset entry: zero the registers, set the stack and trap vector, clear .bss,
 * call main and store its return value at EXIT_ADDR. */

    .section .text.init, "ax"
    .globl _start
_start:
    li      x1,  0
    li      x2,  0
    li      x3,  0
    li      x4,  0
    li      x5,  0
    li      x6,  0
    li      x7,  0
    li      x8,  0
    li      x9,  0
    li      x10, 0
    li      x11, 0
    li      x12, 0
    li      x13, 0
    li      x14, 0
    li      x15, 0
    li      x16, 0
    li      x17, 0
    li      x18, 0
    li      x19, 0
    li      x20, 0
    li      x21, 0
    li      x22, 0
    li      x23, 0
    li      x24, 0
    li      x25, 0
    li      x26, 0
    li      x27, 0
    li      x28, 0
    li      x29, 0
    li      x30, 0
    li      x31, 0

    la      sp, __stack_top
    la      t0, ka_trap_entry
    csrw    mtvec, t0

    la      t0, __bss_start
    la      t1, __bss_end
1:  bgeu    t0, t1, 2f
    sd      zero, 0(t0)
    addi    t0, t0, 8
    j       1b
2:
    call    main

    li      t0, EXIT_ADDR
    sd      a0, 0(t0)
3:  j       3b

/* The trap vector: mcause, mepc and mtval to ka_fatal_trap on a fresh stack. */
    .align  4
    .globl  ka_trap_entry
ka_trap_entry:
    la      sp, __stack_top
    csrr    a0, mcause
    csrr    a1, mepc
    csrr    a2, mtval
    call    ka_fatal_trap
4:  j       4b
