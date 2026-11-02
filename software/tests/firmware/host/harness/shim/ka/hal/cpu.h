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

#endif
