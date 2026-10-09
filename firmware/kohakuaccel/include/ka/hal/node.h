/* The node processor's control region, 1 KB at 0x2_0000
 * (docs/spec/control-registers.md s7): registers and accessors. */
#ifndef KA_HAL_NODE_H
#define KA_HAL_NODE_H

#include <stdint.h>

#define KA_CTRL_BASE 0x00020000UL

enum {
    KA_R_EXIT    = 0x00, /* W: end the run, the word reaches HR_EXIT */
    KA_R_CONSOLE = 0x08, /* W: one console byte (the load window's 256-B FIFO) */
    KA_R_DBELL   = 0x10, /* R: the host's doorbell bit */
    KA_R_SATP    = 0x18, /* R: satp mirror */
    KA_R_MVSTAT  = 0x20, /* R: [32] busy, [31:28] fault, [27:0] moves done */
    KA_R_DBCNT   = 0x28, /* R: inbound doorbell counts, mesh n at [16n+15:16n] */
    KA_R_STDIN   = 0x30, /* R: {valid[8], byte}; W: pop */
    KA_R_IRQ     = 0x38, /* W: toggle the host interrupt line; R: its level */
    KA_R_NM      = 0x40,  /* the dispatch mailbox, 8 registers */
    KA_R_IL      = 0xC0,  /* interlink config register 0x80 + k at 0xC0 + k */
    KA_R_MV      = 0x100, /* mover register k at 0x100 + k, k < 0x80 */
    KA_R_RX      = 0x180, /* RX queue: HDR, P0..P3, USED (8 bytes apart) */
    KA_R_DCACHE  = 0x1C8, /* W: [0] flush, [1] invalidate; R: busy */
    KA_R_XF_SEL  = 0x1D0, /* transform bank config: {id[15:8], addr[7:0]} */
    KA_R_XF_DATA = 0x1D8, /* ... and its data; the write is the strobe */
};

#define KA_MV_SPAN 0x80

/* M_DST: [24] send the type in [23:20] instead of CU_INST. */
#define KA_NM_TYPED(t) ((1UL << 24) | ((uint64_t)((t) & 0xf) << 20))
#define KA_T_CU_CTRL   0x7
#define KA_T_MEM_RD_REQ 0x0

/* RX queue registers, index * 8 from KA_R_RX. HDR is {valid[63], header[31:0]}
 * with src x at [23:20], src y at [19:16], type at [15:12]; a write pops. */
enum { KA_RX_HDR = 0, KA_RX_P0 = 1, KA_RX_P1 = 2, KA_RX_P2 = 3, KA_RX_P3 = 4, KA_RX_USED = 5 };

/* Mailbox registers, index * 8 from KA_R_NM. */
enum {
    KA_NM_DST  = 0, /* x [3:0], y [11:8] */
    KA_NM_ARG0 = 1, /* payload [63:0] */
    KA_NM_ARG1 = 2,
    KA_NM_ARG2 = 3,
    KA_NM_ARG3 = 4, /* payload [255:192]; a unit's opcode is [63:60] */
    KA_NM_GO   = 5,
    KA_NM_STAT = 6, /* [7:0] queued, [15] offered */
    KA_NM_HEAD = 7, /* R: oldest completion; W: pop */
};

#define KA_NM_STAT_COUNT(s) ((unsigned)((s) & 0xffu))
#define KA_NM_STAT_OFFERED  (1UL << 15)

/* The completion queue's depth (rv64_syscore CQ_DEPTH). A FULL queue holds the
 * node's one inbound link in sn_hub, memory traffic behind it included. */
#define KA_NM_CQ_DEPTH 16u

/* A completion word: [55:52] src_y, [51:48] src_x, [47:40] code, [39:8] arg. */
#define KA_CQ_Y(w)    ((unsigned)(((w) >> 52) & 0xfu))
#define KA_CQ_X(w)    ((unsigned)(((w) >> 48) & 0xfu))
#define KA_CQ_CODE(w) ((unsigned)(((w) >> 40) & 0xffu))
#define KA_CQ_ARG(w)  ((uint32_t)(((w) >> 8) & 0xffffffffu))

/* Centrally allocated CU_SIGNAL codes (driver/kohakuaccel/device/registers.py). */
enum {
    KA_SIG_INST_COMPLETE  = 0x00,
    KA_SIG_BATCH_COMPLETE = 0x01,
    KA_SIG_BARRIER        = 0x02,
    KA_SIG_DATA_RECEIVED  = 0x03,
    KA_SIG_FAULT          = 0x04,
};

#define KA_MV_BUSY(s)  (((s) >> 32) & 1u)
#define KA_MV_FAULT(s) ((unsigned)(((s) >> 28) & 0xfu))
#define KA_MV_DONE(s)  ((uint32_t)((s) & 0x0fffffffu))

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
#define KA_DCACHE_FLUSH 1u
#define KA_DCACHE_INVAL 2u

#endif
