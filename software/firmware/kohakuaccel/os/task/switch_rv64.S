/* void ka_ctx_switch(uint64_t *save_sp, uint64_t load_sp): push ra and s0-s11
 * (a 112-byte frame), park sp, load the other stack and return into it. */

    .section .text.ka_ctx_switch, "ax"
    .globl  ka_ctx_switch
ka_ctx_switch:
    addi    sp, sp, -112
    sd      ra,  0(sp)
    sd      s0,  8(sp)
    sd      s1,  16(sp)
    sd      s2,  24(sp)
    sd      s3,  32(sp)
    sd      s4,  40(sp)
    sd      s5,  48(sp)
    sd      s6,  56(sp)
    sd      s7,  64(sp)
    sd      s8,  72(sp)
    sd      s9,  80(sp)
    sd      s10, 88(sp)
    sd      s11, 96(sp)
    sd      sp,  0(a0)
    mv      sp, a1
    ld      ra,  0(sp)
    ld      s0,  8(sp)
    ld      s1,  16(sp)
    ld      s2,  24(sp)
    ld      s3,  32(sp)
    ld      s4,  40(sp)
    ld      s5,  48(sp)
    ld      s6,  56(sp)
    ld      s7,  64(sp)
    ld      s8,  72(sp)
    ld      s9,  80(sp)
    ld      s10, 88(sp)
    ld      s11, 96(sp)
    addi    sp, sp, 112
    ret

/* A new task's first switch returns here: hand s0 (the task) to C. */
    .section .text.ka_task_trampoline, "ax"
    .globl  ka_task_trampoline
ka_task_trampoline:
    mv      a0, s0
    call    ka_task_main
1:  j       1b
