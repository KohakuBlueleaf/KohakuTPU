/* A KohakuAccel system node as its RV64 processor sees it: the control region
 * (docs/spec/control-registers.md s7, docs/arch/sysnode/abilities.md), the
 * address bits, and the CSRs this core implements. Generic to every project:
 * nothing here names a compute unit's instruction set. */
#ifndef KOS_HW_H
#define KOS_HW_H

#include <stdint.h>

/* ---- the processor's physical address map ---- */
#define HW_IMEM_PA   0x00000000UL /* fetch only, host-loaded */
#define HW_IMEM_SIZE 0x8000UL
#define HW_SPAD_PA   0x00010000UL
#define HW_SPAD_SIZE 0x8000UL
#define HW_CTRL_PA   0x00020000UL
#define HW_NODE_BASE 0x10000000UL /* at or above: the node port (DRAM, staging) */
#define HW_UNC       (1UL << 38)  /* the uncached alias of DRAM */
#define HW_SPECIAL   (1UL << 39)  /* staging and apertures, always uncached */
#define HW_MESH_SHIFT 36
#define HW_STAGING(m) (HW_SPECIAL | ((uint64_t)(m) << HW_MESH_SHIFT))

/* ---- control region offsets ---- */
enum {
    HW_R_EXIT    = 0x00,
    HW_R_CONSOLE = 0x08,
    HW_R_DBELL   = 0x10,
    HW_R_SATP    = 0x18,
    HW_R_MVSTAT  = 0x20, /* [33] room [32] busy [31:28] fault [27:0] moves done */
    HW_R_DBCNT   = 0x28, /* inbound rings, mesh n at [16n+15:16n] */
    HW_R_STDIN   = 0x30, /* R: {valid[8], byte}; W: pop */
    HW_R_IRQ     = 0x38, /* W: toggle the host interrupt */
    HW_R_NM      = 0x40, /* mailbox, 8 registers */
    HW_R_IL      = 0xC0, /* interlink register k at 0xC0 + k */
    HW_R_SIG     = 0xE0, /* R: the selected signal slot's word */
    HW_R_MV      = 0x100, /* mover register k at 0x100 + k */
    HW_R_RX      = 0x180, /* RX queue: HDR P0 P1 P2 P3 USED */
    HW_R_DCACHE  = 0x1C8, /* W: [0] flush [1] invalidate; R: busy */
    HW_R_DE_Q    = 0x200, /* dispatch engine: queue a write of register c at +8c */
    HW_R_DE_WAIT = 0x300, /* W: queue a WAIT; R: STAT */
    HW_R_DE_CTL  = 0x308, /* W: [0] enable [1] clear; R: MOVES */
    HW_R_DE_MAP  = 0x310, /* W: [31] valid [19:16] counter [11:8] y [3:0] x */
    HW_R_DE_CTR  = 0x380, /* R: counter k at +8k */
};

enum {
    HW_IL_CTL     = 0x00, /* [0] enable [1] clear doorbells [2] clear faults [3] clear slots */
    HW_IL_MESH    = 0x08,
    HW_IL_RING    = 0x10, /* [1:0] mesh [15:8] slot [31:16] amount [32] fence */
    HW_IL_CONSUME = 0x20, /* [3:0] slot [63:32] amount */
    HW_IL_SELECT  = 0x28,
};
#define HW_SIG_COUNT(w) ((uint32_t)(w))
#define HW_SIG_SENT(w)  ((uint32_t)(((w) >> 32) & 0xffffu))
#define HW_SIG_SLOT(w)  ((unsigned)(((w) >> 50) & 0xfu))
#define HW_SIG_FAULT(w) ((unsigned)((w) >> 56))
#define HW_SIG_SLOTS     16u
#define HW_RING_DEPTH    4u

enum { HW_NM_DST, HW_NM_ARG0, HW_NM_ARG1, HW_NM_ARG2, HW_NM_ARG3, HW_NM_GO, HW_NM_STAT, HW_NM_HEAD };
#define HW_NM_OFFERED    (1UL << 15)
#define HW_NM_COUNT(s)   ((unsigned)((s) & 0xffu))
#define HW_NM_TYPED(t)   ((1UL << 24) | ((uint64_t)((t) & 0xf) << 20))
#define HW_T_CU_CTRL     0x7
#define HW_CQ_DEPTH      16u
#define HW_CQ_Y(w)       ((unsigned)(((w) >> 52) & 0xfu))
#define HW_CQ_X(w)       ((unsigned)(((w) >> 48) & 0xfu))
#define HW_CQ_CODE(w)    ((unsigned)(((w) >> 40) & 0xffu))
#define HW_CQ_ARG(w)     ((uint32_t)(((w) >> 8) & 0xffffffffu))
enum { HW_SIG_INST = 0, HW_SIG_BATCH = 1, HW_SIG_BARRIER = 2, HW_SIG_DATA = 3, HW_SIG_FAULT_CODE = 4 };

enum { HW_RX_HDR, HW_RX_P0, HW_RX_P1, HW_RX_P2, HW_RX_P3, HW_RX_USED };

#define HW_MV_BUSY(s)  (((s) >> 32) & 1u)
#define HW_MV_ROOM(s)  (((s) >> 33) & 1u)
#define HW_MV_FAULT(s) ((unsigned)(((s) >> 28) & 0xfu))
#define HW_MV_DONE(s)  ((uint32_t)((s) & 0x0fffffffu))

#define HW_DE_MAGIC(s) ((unsigned)((s) >> 48) == 0xDE01u)

/* ---- CSRs this core implements beyond the standard set ---- */
#define CSR_MTIMECMP 0x7C0
/* The three interrupt sources sit at their machine positions; `sie`/`sip`
 * are windows on `mie`/`mip` through `mideleg` (rv64_csr.v), so supervisor
 * code enables and tests these same bits. A delegated one traps with the
 * supervisor cause (1, 5, 9). */
#define MIP_SOFT  (1UL << 3)
#define MIP_TIMER (1UL << 7)
#define MIP_EXT   (1UL << 11)
#define CAUSE_IRQ  (1UL << 63)
#define CAUSE_SSI  (CAUSE_IRQ | 1)
#define CAUSE_STI  (CAUSE_IRQ | 5)
#define CAUSE_SEI  (CAUSE_IRQ | 9)

/* `c` is a CSR name or a macro for its number (expanded before it is quoted) */
#define csr_read(c) csr_read_(c)
#define csr_write(c, v) csr_write_(c, v)
#define csr_set(c, v) csr_set_(c, v)
#define csr_clear(c, v) csr_clear_(c, v)
#define csr_read_(c) ({ uint64_t _v; __asm__ volatile("csrr %0, " #c : "=r"(_v)); _v; })
#define csr_write_(c, v) __asm__ volatile("csrw " #c ", %0" ::"r"((uint64_t)(v)) : "memory")
#define csr_set_(c, v) __asm__ volatile("csrs " #c ", %0" ::"r"((uint64_t)(v)) : "memory")
#define csr_clear_(c, v) __asm__ volatile("csrc " #c ", %0" ::"r"((uint64_t)(v)) : "memory")

/* The control region, untranslated: M mode, or S with satp 0. */
static inline uint64_t hw_rd(unsigned off)
{
    return *(volatile uint64_t *)(HW_CTRL_PA + off);
}
static inline void hw_wr(unsigned off, uint64_t v)
{
    *(volatile uint64_t *)(HW_CTRL_PA + off) = v;
}
static inline uint64_t hw_cycles(void)
{
    uint64_t c;
    __asm__ volatile("csrr %0, cycle" : "=r"(c)); /* mcycle is M-only; cycle reads in S and U too */
    return c;
}
static inline uint64_t hw_time(void)
{
    uint64_t c;
    __asm__ volatile("csrr %0, time" : "=r"(c));
    return c;
}

/* ---- D-cache maintenance: whole-L1 sweeps ---- */
#define HW_DC_FLUSH 1u
#define HW_DC_INVAL 2u
/* Busy rises only once the L1 starts its sweep, a few cycles after the write
 * (a read right after it returns 0: M0 measured an 8-cycle "flush" of 64 dirty
 * lines). Every sweep scans all 64 lines, so busy is high for longer than one
 * poll: wait for it to rise, then to fall. */
static inline void hw_dcache(unsigned op)
{
    hw_wr(HW_R_DCACHE, op);
    while (!hw_rd(HW_R_DCACHE)) {
    }
    while (hw_rd(HW_R_DCACHE)) {
    }
}

/* ---- the interlink: signals and rings ---- */
static inline uint64_t hw_sig_word(void) { return hw_rd(HW_R_SIG); }

/* The count of signal slot `slot` on this node. The select lands a few
 * registers deep, so the word is read until it names the slot -- and once
 * more: the slot field is the select register itself, the count is the slot
 * array read through one more register (mag_ilink.v `sig_q`), so the first
 * word naming the slot can still carry the previous slot's count. */
static inline uint32_t hw_slot(unsigned slot)
{
    hw_wr(HW_R_IL + HW_IL_SELECT, slot);
    while (HW_SIG_SLOT(hw_sig_word()) != slot) {
    }
    return HW_SIG_COUNT(hw_sig_word());
}

#endif
