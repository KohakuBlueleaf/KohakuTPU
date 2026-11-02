/* The control region as calls into the hardware double. */
#ifndef KA_HAL_NODE_H
#define KA_HAL_NODE_H

#include <stdint.h>

#include <ka/hal/control.h>

uint64_t dbl_rd(unsigned off);
void dbl_wr(unsigned off, uint64_t v);

static inline uint64_t ka_ctrl_rd(unsigned off) { return dbl_rd(off); }
static inline void ka_ctrl_wr(unsigned off, uint64_t v) { dbl_wr(off, v); }
static inline uint64_t ka_nm_rd(unsigned reg) { return ka_ctrl_rd(KA_R_NM + 8 * reg); }
static inline void ka_nm_wr(unsigned reg, uint64_t v) { ka_ctrl_wr(KA_R_NM + 8 * reg, v); }
static inline uint64_t ka_rx_rd(unsigned reg) { return ka_ctrl_rd(KA_R_RX + 8 * reg); }
static inline void ka_rx_pop(void) { ka_ctrl_wr(KA_R_RX, 0); }
static inline void ka_dcache(unsigned op) { (void)op; }

#endif
