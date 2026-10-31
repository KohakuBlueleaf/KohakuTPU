/* The start of one boot across nodes. The interlink keeps its signal-slot
 * counts and its sent-ring count across a processor reset, so a node that
 * boots after an earlier run finds counts that run left (on silicon; a model
 * starts from power-on). Before any node rings another, every node clears its
 * own slots and takes the interlink's sent count as its own; node 0 (booted
 * last) waits until every other node says it has, then lets them go. The
 * handshake is posted stores into staging tagged with the boot table's run
 * id, which a previous boot's leftovers never match. */
#ifndef KOS_START_H
#define KOS_START_H

#include <kos/abi.h>
#include <kos/boot.h>
#include <kos/hw.h>

#define HW_SIG_SWEEP(w) (((w) >> 49) & 1u)

static inline volatile uint64_t *kos_start_area(const volatile struct kos_boot *b, unsigned node)
{
    return (volatile uint64_t *)(HW_STAGING(b->mesh_of[node]) + KOS_STG_START);
}

/* The log header's where words (abi.h KOS_LOG_*). */
static inline void kos_where(volatile uint64_t *log, uint64_t code, uint64_t a, uint64_t b)
{
    log[KOS_LOG_A] = a;
    log[KOS_LOG_B] = b;
    log[KOS_LOG_WHERE] = code;
}

/* `*rings`: the caller's count of rings it has queued, in the interlink's
 * 16-bit sent-count space. `log`: the caller's log header (uncached). */
static inline void kos_start(const volatile struct kos_boot *b, uint32_t *rings, volatile uint64_t *log)
{
    unsigned me = b->node, n = b->nnodes;
    uint64_t run = b->run;
    kos_where(log, KOS_W_START, run, 0);
    hw_wr(HW_R_IL + HW_IL_CTL, 1 | 8); /* keep the link enabled, clear this node's slots */
    while (HW_SIG_SWEEP(hw_sig_word())) {
    }
    *rings = HW_SIG_SENT(hw_sig_word()); /* nothing of this boot is queued yet */
    volatile uint64_t *own = kos_start_area(b, me);
    if (me == 0) {
        for (unsigned j = 1; j < n; ++j) {
            kos_where(log, KOS_W_START, run, 1 + j);
            while (own[j] != run) {
            }
        }
        for (unsigned j = 1; j < n; ++j) {
            kos_start_area(b, j)[4] = run;
        }
    } else {
        kos_start_area(b, 0)[me] = run;
        kos_where(log, KOS_W_START, run, 9);
        while (own[4] != run) {
        }
    }
}

#endif
