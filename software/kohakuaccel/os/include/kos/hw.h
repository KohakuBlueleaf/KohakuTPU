/* A KohakuAccel system node as its RV64 processor sees it: the control region
 * (docs/spec/control-registers.md s7, docs/arch/sysnode/abilities.md), the
 * address bits, and the CSRs this core implements. Generic to every project:
 * nothing here names a compute unit's instruction set. */
#ifndef KOS_HW_H
#define KOS_HW_H

#include <stdint.h>

#include <ka/hal/control.h>

/* ---- the processor's physical address map ---- */
#define HW_IMEM_PA   0x00000000UL /* fetch only, host-loaded */
#define HW_IMEM_SIZE 0x8000UL
#define HW_SPAD_PA   0x00010000UL
#define HW_SPAD_SIZE 0x8000UL
#define HW_CTRL_PA   KA_CTRL_BASE
#define HW_NODE_BASE 0x10000000UL /* at or above: the node port (DRAM, staging) */
#define HW_UNC       (1UL << 38)  /* the uncached alias of DRAM */
#define HW_SPECIAL   (1UL << 39)  /* staging and apertures, always uncached */
#define HW_MESH_SHIFT 36
#define HW_STAGING(m) (HW_SPECIAL | ((uint64_t)(m) << HW_MESH_SHIFT))

/* ---- control region offsets: `ka/hal/control.h` under the OS's names ---- */
enum {
    HW_R_EXIT    = KA_R_EXIT,
    HW_R_CONSOLE = KA_R_CONSOLE,
    HW_R_DBELL   = KA_R_DBELL,
    HW_R_SATP    = KA_R_SATP,
    HW_R_MVSTAT  = KA_R_MVSTAT,
    HW_R_DBCNT   = KA_R_DBCNT,
    HW_R_STDIN   = KA_R_STDIN,
    HW_R_IRQ     = KA_R_IRQ,
    HW_R_NM      = KA_R_NM,
    HW_R_IL      = KA_R_IL,
    HW_R_SIG     = KA_R_SIG,
    HW_R_MV      = KA_R_MV,
    HW_R_RX      = KA_R_RX,
    HW_R_DCACHE  = KA_R_DCACHE,
    HW_R_DE_Q    = KA_R_DE,
    HW_R_DE_WAIT = KA_R_DE_STAT,
    HW_R_DE_CTL  = KA_R_DE_CTL,
    HW_R_DE_MAP  = KA_R_DE_MAP,
    HW_R_DE_CTR  = KA_R_DE_CTR,
};

enum {
    HW_IL_CTL     = KA_IL_CTL,
    HW_IL_MESH    = KA_IL_MESH,
    HW_IL_RING    = KA_IL_RING,
    HW_IL_CONSUME = KA_IL_CONSUME,
    HW_IL_SELECT  = KA_IL_SELECT,
};
#define HW_SIG_COUNT(w) KA_SIG_COUNT(w)
#define HW_SIG_SENT(w)  KA_SIG_SENT(w)
#define HW_SIG_SLOT(w)  KA_SIG_SLOT(w)
#define HW_SIG_FAULT(w) ((unsigned)((w) >> 56))
#define HW_SIG_SLOTS     16u
#define HW_RING_DEPTH    KA_IL_RING_DEPTH

enum {
    HW_NM_DST  = KA_NM_DST,
    HW_NM_ARG0 = KA_NM_ARG0,
    HW_NM_ARG1 = KA_NM_ARG1,
    HW_NM_ARG2 = KA_NM_ARG2,
    HW_NM_ARG3 = KA_NM_ARG3,
    HW_NM_GO   = KA_NM_GO,
    HW_NM_STAT = KA_NM_STAT,
    HW_NM_HEAD = KA_NM_HEAD,
};
#define HW_NM_OFFERED    KA_NM_STAT_OFFERED
#define HW_NM_COUNT(s)   KA_NM_STAT_COUNT(s)
#define HW_NM_TYPED(t)   KA_NM_TYPED(t)
#define HW_T_CU_CTRL     KA_T_CU_CTRL
#define HW_CQ_DEPTH      KA_NM_CQ_DEPTH
#define HW_CQ_Y(w)       KA_CQ_Y(w)
#define HW_CQ_X(w)       KA_CQ_X(w)
#define HW_CQ_CODE(w)    KA_CQ_CODE(w)
#define HW_CQ_ARG(w)     KA_CQ_ARG(w)
enum {
    HW_SIG_INST       = KA_SIG_INST_COMPLETE,
    HW_SIG_BATCH      = KA_SIG_BATCH_COMPLETE,
    HW_SIG_BARRIER    = KA_SIG_BARRIER,
    HW_SIG_DATA       = KA_SIG_DATA_RECEIVED,
    HW_SIG_FAULT_CODE = KA_SIG_FAULT,
};

enum {
    HW_RX_HDR  = KA_RX_HDR,
    HW_RX_P0   = KA_RX_P0,
    HW_RX_P1   = KA_RX_P1,
    HW_RX_P2   = KA_RX_P2,
    HW_RX_P3   = KA_RX_P3,
    HW_RX_USED = KA_RX_USED,
};

#define HW_MV_BUSY(s)  KA_MV_BUSY(s)
#define HW_MV_ROOM(s)  KA_MV_ROOM(s)
#define HW_MV_FAULT(s) KA_MV_FAULT(s)
#define HW_MV_DONE(s)  KA_MV_DONE(s)

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
#define HW_DC_FLUSH KA_DCACHE_FLUSH
#define HW_DC_INVAL KA_DCACHE_INVAL
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
