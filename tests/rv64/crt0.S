/* Reset entry for a SysCore bare-metal program.
 *
 * NO LIBRARY, NO RUNTIME. The core comes out of reset at 0 with every register
 * undefined; this sets a stack, clears bss, calls main, and reports the result
 * through the harness's exit register.
 */

    .section .text.init, "ax"
    .globl _start
_start:
    /* x0 is hardwired; start at x1 and leave nothing undefined behind us. */
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

    /* bss, eight bytes at a time -- the link script aligns both ends. */
    la      t0, __bss_start
    la      t1, __bss_end
1:  bgeu    t0, t1, 2f
    sd      zero, 0(t0)
    addi    t0, t0, 8
    j       1b
2:

    call    main

    /* main's return value becomes the run's exit code. The standalone harness
     * puts that register at 0x10000008; rv64_sys_pe puts it at CTRL_BASE. */
#ifndef EXIT_ADDR
#define EXIT_ADDR 0x10000008
#endif
    li      t0, EXIT_ADDR
    sd      a0, 0(t0)

    /* The harness stops on the store above; ECALL is the belt and braces. */
3:  ecall
    j       3b
