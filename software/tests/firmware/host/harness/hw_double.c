/* A node's mailbox, mover count and dispatch engine, after dispatch_engine.v: a
 * queue issued at one entry a cycle, a WAIT held while its counter is behind by
 * the top bit, retirements of a mapped source counted and every other signal
 * queued for the processor. A cycle passes at each access, and a word retires
 * RETIRE cycles after its GO. */
#include <ka/hal/control.h>

#include "hw_double.h"

#define DEPTH 64u
#define CODE_WAIT 32u
#define CODE_GO 5u
#define CODE_MOVER_CTRL 8u
#define MOVER_SRC 16u
#define CTR_MASK 0xfffu
#define MV_GO (1UL << 16)
#define MV_WINDOW 0x28u
#define RETIRE 16u
#define PEER 256u /* a stream's words are queued by then */
#define PENDING 4096u
#define CQ 1024u

int dbl_engine;
uint64_t dbl_first_go, dbl_last_go, dbl_gos;
unsigned dbl_windows[4];
unsigned dbl_retire = RETIRE, dbl_lost, dbl_peak, dbl_clears;

static uint64_t now, mb[5];
static struct {
    unsigned code;
    uint64_t value;
} queue[DEPTH];
static unsigned head, used, enabled, issued, moves, moves_base;
static unsigned ctr[32];
static unsigned smap[256]; /* {y, x}: 0, or 1 + counter */
static struct {
    uint64_t at;
    unsigned x, y;
} pending[PENDING];
static unsigned pending_head, pending_n;
static uint64_t cq[CQ];
static unsigned cq_head, cq_n;
static unsigned owed[256]; /* {y, x}: words not yet retired */

static void retire(unsigned x, unsigned y)
{
    unsigned c = smap[y << 4 | x];
    owed[y << 4 | x] -= owed[y << 4 | x] != 0;
    if (enabled && c) {
        ctr[c - 1] = (ctr[c - 1] + 1) & CTR_MASK;
        return;
    }
    cq[(cq_head + cq_n++) % CQ] = (uint64_t)y << 52 | (uint64_t)x << 48;
}

static void owe(unsigned x, unsigned y, uint64_t at)
{
    unsigned i = (pending_head + pending_n++) % PENDING;
    pending[i].at = at;
    pending[i].x = x;
    pending[i].y = y;
}

void dbl_peer_retires(unsigned x, unsigned y) { owe(x, y, now + PEER); }

unsigned dbl_pending(void) { return pending_n; }

static void go(void)
{
    uint64_t dst = mb[KA_NM_DST];
    int fetch = (dst >> 24) & 1;
    unsigned at = (unsigned)(mb[KA_NM_ARG2] >> 40);
    unsigned x = fetch ? at & 15 : dst & 15, y = fetch ? (at >> 4) & 15 : (dst >> 8) & 15;
    unsigned n = fetch ? mb[KA_NM_ARG3] & 255 : 1;
    for (unsigned k = 0; k < n; ++k) {
        if (dbl_lost) {
            --dbl_lost;
            continue;
        }
        owe(x, y, now + dbl_retire + k);
        dbl_peak = ++owed[y << 4 | x] > dbl_peak ? owed[y << 4 | x] : dbl_peak;
    }
    dbl_first_go = dbl_gos ? dbl_first_go : now;
    dbl_last_go = now;
    dbl_gos += n;
}

static void write_mover(unsigned off, uint64_t v)
{
    if (off == 0 && (v & MV_GO)) {
        ++moves;
    }
    if (off == MV_WINDOW) {
        ++dbl_windows[v & 3];
    }
}

static void issue(void)
{
    if (!enabled || !used) {
        return;
    }
    unsigned code = queue[head].code;
    uint64_t v = queue[head].value;
    if (code == CODE_WAIT) {
        unsigned src = (v >> 56) & 31;
        unsigned have = src < MOVER_SRC ? ctr[src] : (moves - moves_base) & CTR_MASK;
        if (((have - (unsigned)v) & CTR_MASK) > CTR_MASK / 2) {
            return;
        }
    }
    else if (code < CODE_GO) {
        mb[code] = v;
    }
    else if (code == CODE_GO) {
        go();
    }
    else if (code >= CODE_MOVER_CTRL && code < 24) {
        write_mover(8 * (code - CODE_MOVER_CTRL), v);
    }
    head = (head + 1) % DEPTH;
    --used;
    ++issued;
}

static void tick(void)
{
    ++now;
    while (pending_n && pending[pending_head].at <= now) {
        retire(pending[pending_head].x, pending[pending_head].y);
        pending_head = (pending_head + 1) % PENDING;
        --pending_n;
    }
    issue();
}

uint64_t dbl_cycles(void)
{
    tick();
    return now;
}

uint64_t dbl_rd(unsigned off)
{
    tick();
    if (off == KA_R_MVSTAT) {
        return 1UL << 33 | moves;
    }
    if (off == KA_R_DE_STAT) {
        return dbl_engine ? 0xDE01UL << 48 | 6UL << 40 | 4UL << 36 | (uint64_t)enabled << 32 |
                                (uint64_t)issued << 16 | used
                          : 0;
    }
    if (off == KA_R_DE_CTL) {
        return (moves - moves_base) & CTR_MASK;
    }
    if (off >= KA_R_DE_CTR && off < KA_R_DE_CTR + 8 * 32) {
        return ctr[(off - KA_R_DE_CTR) / 8];
    }
    if (off == KA_R_NM + 8 * KA_NM_STAT) {
        return cq_n;
    }
    if (off == KA_R_NM + 8 * KA_NM_HEAD) {
        return cq[cq_head];
    }
    return 0;
}

void dbl_wr(unsigned off, uint64_t v)
{
    tick();
    if (off >= KA_R_DE && off <= KA_R_DE_STAT) {
        unsigned tail = (head + used++) % DEPTH;
        queue[tail].code = (off - KA_R_DE) / 8;
        queue[tail].value = v;
    }
    else if (off == KA_R_DE_CTL) {
        enabled = v & 1;
        if (v & 2) {
            ++dbl_clears;
            head = used = issued = 0;
            moves_base = moves;
            for (unsigned i = 0; i < 32; ++i) {
                ctr[i] = 0;
            }
        }
    }
    else if (off == KA_R_DE_MAP) {
        unsigned at = (((v >> 8) & 15) << 4) | (v & 15);
        smap[at] = (v >> 31) & 1 ? ((v >> 16) & 31) + 1 : 0;
    }
    else if (off >= KA_R_NM && off < KA_R_NM + 8 * KA_NM_GO) {
        mb[(off - KA_R_NM) / 8] = v;
    }
    else if (off == KA_R_NM + 8 * KA_NM_GO) {
        go();
    }
    else if (off == KA_R_NM + 8 * KA_NM_HEAD) {
        cq_head = (cq_head + 1) % CQ;
        --cq_n;
    }
    else if (off >= KA_R_MV && off < KA_R_MV + KA_MV_SPAN) {
        write_mover(off - KA_R_MV, v);
    }
}
