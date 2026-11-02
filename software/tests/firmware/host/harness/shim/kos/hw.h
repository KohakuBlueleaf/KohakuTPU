/* The OS's view of the node with its register, clock and cache calls on the
 * hardware double; the real file's names of them are compiled out of the way. */
#ifndef SHIM_KOS_HW_H
#define SHIM_KOS_HW_H

#define hw_rd real_hw_rd
#define hw_wr real_hw_wr
#define hw_cycles real_hw_cycles
#define hw_dcache real_hw_dcache
#include_next <kos/hw.h>
#undef hw_rd
#undef hw_wr
#undef hw_cycles
#undef hw_dcache

uint64_t dbl_rd(unsigned off);
void dbl_wr(unsigned off, uint64_t v);
uint64_t dbl_cycles(void);

static inline uint64_t hw_rd(unsigned off) { return dbl_rd(off); }
static inline void hw_wr(unsigned off, uint64_t v) { dbl_wr(off, v); }
static inline uint64_t hw_cycles(void) { return dbl_cycles(); }
static inline void hw_dcache(unsigned op) { (void)op; }

#endif
