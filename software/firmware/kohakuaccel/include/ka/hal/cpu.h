/* The hart: cycle counter, delay, exit and the fatal-trap hooks. */
#ifndef KA_HAL_CPU_H
#define KA_HAL_CPU_H

#include <stdint.h>

static inline uint64_t ka_cycles(void)
{
    uint64_t v;
    __asm__ volatile("rdcycle %0" : "=r"(v));
    return v;
}

/* Spin `n` cycles, measured on the counter rather than counted in a loop. */
static inline void ka_delay(uint64_t n)
{
    uint64_t t0 = ka_cycles();
    while (ka_cycles() - t0 < n) {
    }
}

/* Store the exit word and stop: the host reads it at HR_EXIT. */
void ka_exit(uint64_t code) __attribute__((noreturn));

/* Called once by the trap vector with the three trap CSRs; never returns. */
void ka_fatal_trap(uint64_t cause, uint64_t epc, uint64_t tval) __attribute__((noreturn));

/* What a layer above wants done before a fatal stop (default: nothing). */
void ka_on_fatal(uint64_t code, uint64_t a, uint64_t b);

/* Storage crt0 does not zero (node.ld's .noinit): stacks and tables their owner
 * initialises. */
#define KA_NOINIT __attribute__((section(".noinit")))

/* Exit codes the framework itself raises; a firmware main returns 0. */
#define KA_EXIT_TRAP     0xE000000000000000UL /* | mcause */
#define KA_EXIT_BOOTARGS 0xE100000000000000UL /* | what was wrong */
#define KA_EXIT_STOP     0x5701000000000000UL /* | the host's stop value */

#endif
