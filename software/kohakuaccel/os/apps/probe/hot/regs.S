#include <kos/abi.h>
    .section .text.hot_registers, "ax"
    .globl hot_registers
hot_registers:
    addi sp, sp, -32
    sd s11, 0(sp)
    sd a0, 8(sp)
    mv s11, a0
    li a7, SYS_YIELD
    ecall
    li a7, SYS_YIELD
    ecall
    ld a0, 8(sp)
    xor a0, a0, s11
    ld s11, 0(sp)
    addi sp, sp, 32
    ret

/* Preserve user caller registers while EVENT copies records through a C helper. */
    .section .text.hot_event_registers, "ax"
    .globl hot_event_registers
hot_event_registers:
    addi sp, sp, -32
    sd s11, 0(sp)
    sd a0, 8(sp)
    sd a1, 16(sp)
    mv a2, a0
    mv a3, a1
    li a0, 0
    li a1, 0
    li a4, 104
    li a5, 105
    li a6, 106
    li a7, SYS_EVENT
    li t0, 110
    li t1, 111
    li t2, 112
    li t3, 113
    li t4, 114
    li t5, 115
    li t6, 116
    li s11, 117
    ecall
    xori t0, t0, 110
    or a0, a0, t0
    xori t1, t1, 111
    or a0, a0, t1
    xori t2, t2, 112
    or a0, a0, t2
    xori t3, t3, 113
    or a0, a0, t3
    xori t4, t4, 114
    or a0, a0, t4
    xori t5, t5, 115
    or a0, a0, t5
    xori t6, t6, 116
    or a0, a0, t6
    xori a4, a4, 104
    or a0, a0, a4
    xori a5, a5, 105
    or a0, a0, a5
    xori a6, a6, 106
    or a0, a0, a6
    xori a7, a7, SYS_EVENT
    or a0, a0, a7
    xori s11, s11, 117
    or a0, a0, s11
    ld t0, 8(sp)
    xor t0, t0, a2
    or a0, a0, t0
    ld t0, 16(sp)
    xor t0, t0, a3
    or a0, a0, t0
    ld s11, 0(sp)
    addi sp, sp, 32
    ret
