/* The hart's counter as the hardware double's clock. */
#ifndef KA_HAL_CPU_H
#define KA_HAL_CPU_H

#include <stdint.h>

uint64_t dbl_cycles(void);

static inline uint64_t ka_cycles(void) { return dbl_cycles(); }

static inline void ka_delay(uint64_t n)
{
    uint64_t t0 = ka_cycles();
    while (ka_cycles() - t0 < n) {
    }
}

void ka_exit(uint64_t code) __attribute__((noreturn));
void ka_on_fatal(uint64_t code, uint64_t a, uint64_t b);
#define KA_EXIT_TRAP     0xE000000000000000UL
#define KA_EXIT_BOOTARGS 0xE100000000000000UL
#define KA_EXIT_STOP     0x5701000000000000UL

#endif
