/* Card memory at its own address in this process: no alias bit to spell. */
#ifndef KA_HAL_MEM_H
#define KA_HAL_MEM_H

#include <stdint.h>

#define KA_UNCACHED (1UL << 38)

static inline uint64_t ka_ld64(uint64_t ga)
{
    return *(volatile uint64_t *)(uintptr_t)(ga & ~KA_UNCACHED);
}

static inline void ka_st64(uint64_t ga, uint64_t v)
{
    *(volatile uint64_t *)(uintptr_t)(ga & ~KA_UNCACHED) = v;
}

#endif
