/* The node processor's control region: accessors over `ka/hal/control.h`. */
#ifndef KA_HAL_NODE_H
#define KA_HAL_NODE_H

#include <stdint.h>

#include <ka/hal/control.h>

static inline uint64_t ka_ctrl_rd(unsigned off)
{
    return *(volatile uint64_t *)(KA_CTRL_BASE + off);
}

static inline void ka_ctrl_wr(unsigned off, uint64_t v)
{
    *(volatile uint64_t *)(KA_CTRL_BASE + off) = v;
}

static inline uint64_t ka_nm_rd(unsigned reg) { return ka_ctrl_rd(KA_R_NM + 8 * reg); }
static inline void ka_nm_wr(unsigned reg, uint64_t v) { ka_ctrl_wr(KA_R_NM + 8 * reg, v); }
static inline uint64_t ka_rx_rd(unsigned reg) { return ka_ctrl_rd(KA_R_RX + 8 * reg); }
static inline void ka_rx_pop(void) { ka_ctrl_wr(KA_R_RX, 0); }

/* D-cache maintenance: write back every dirty line / drop every line. */
static inline void ka_dcache(unsigned op)
{
    ka_ctrl_wr(KA_R_DCACHE, op);
    while (ka_ctrl_rd(KA_R_DCACHE)) {
    }
}

#endif
