/* The package interpreter: host-bound payloads and steps, one package at a time. */
#include <stddef.h>

#include <ka/boot/args.h>
#include <ka/dispatch/de.h>
#include <ka/dispatch/engine.h>
#include <ka/hal/cpu.h>
#include <ka/hal/mem.h>
#include <ka/hal/node.h>
#include <ka/lib/stdio.h>
#include <ka/os/task.h>
#include <ka/package/format.h>
#include <ka/package/debug.h>
#include <ka/package/interp.h>
#include <ka/package/status.h>

static struct ka_engine eng;

/* The package being run, read once from its header. */
static struct {
    uint64_t base, total;
    uint32_t npay, nrel, nbuf, nstep, nunit, ackres;
    uint64_t ounit, obuf, ostep, orel, opay;
    int cached;   /* read through the L1 */
    int local;    /* copied whole into `stage.copy` */
    uint32_t foreign; /* units of another mesh: in the table, never sent to */
} pk;

/* The package's registers (LOAD, ALU, JUMP, PATCH) and the step run next. */
static uint64_t reg[KA_PKG_REGS];
static uint32_t pc_next;

/* A package read during a FILL waits out the stream: the read is served by the
 * MAG engine in order behind it (MBOX_TRACE, v9: each GO ~180 cycles after the
 * target's FILL ended). A cached package that fits is copied here up front, a
 * line fill per 32 bytes; an uncached one stays where it is, since four 8-byte
 * round trips per word up front cost a small package more than it saves. */
#ifndef KA_PKG_COPY_BYTES /* a build knob (build.py DEFINES): 0 compiles the copy out */
#define KA_PKG_COPY_BYTES 10752
#endif
#define KA_PKG_COPY_WORDS (KA_PKG_COPY_BYTES / 8)
/* Whether the running package is read from the copy. */
#define LOCAL (KA_PKG_COPY_BYTES && pk.local)

/* The copy, or the words of the queue entry after the running one, read
 * ahead (L0.NQ.053) only while the running package is not local. */
static union {
    uint64_t copy[KA_PKG_COPY_WORDS + !KA_PKG_COPY_WORDS];
    uint64_t next[8];
} stage;
static uint64_t entry_at = ~0UL, t_end;

void ka_package_ahead_reset(void) { entry_at = ~0UL; }

const uint64_t *ka_package_ahead(uint64_t seq)
{
    if (seq != entry_at) {
        return 0;
    }
    entry_at = ~0UL;
    return stage.next;
}

/* The file is built for size; the dispatch-engine path, what a v9 node runs
 * per step, is built for speed (all -Os: silu 14,211 node cycles, not 13,111). */
#ifdef KA_DEBUG
#define KA_HOT /* the debug image is built for size throughout */
#elif __has_attribute(optimize)
#define KA_HOT __attribute__((optimize("O2")))
#else
#define KA_HOT /* clang has no per-function level: the file's level applies */
#endif

static KA_HOT uint64_t rd(uint64_t off)
{
    if (LOCAL) {
        return stage.copy[off >> 3];
    }
    if (pk.cached) {
        return *(volatile uint64_t *)(uintptr_t)(pk.base + off);
    }
    return ka_ld64(pk.base + off);
}

/* Whether a unit fetches its words itself (§4.7): its payloads are never read
 * here, so copying them costs a line fill each for nothing. */
static int any_fetch(void)
{
    if (pk.nrel || (ka_boot.flags & KA_BOOT_F_NOFETCH)) {
        return 0;
    }
    for (uint32_t u = 0; u < pk.nunit; ++u) {
        if (rd(pk.ounit + (uint64_t)u * KA_PKG_UNIT_BYTES + 8) >> 48) {
            return 1;
        }
    }
    return 0;
}

static void localise(void)
{
    pk.local = 0;
    if (!KA_PKG_COPY_BYTES || !pk.cached || pk.total > KA_PKG_COPY_BYTES || any_fetch()) {
        return;
    }
    for (uint64_t off = 0; off < pk.total; off += 8) {
        stage.copy[off >> 3] = rd(off);
    }
    pk.local = 1;
}

/* rv64_syscore's cached range: some bit of pa[39:28] set, not the special half. */
static int cacheable(uint64_t ga)
{
    return !(ga >> 39) && ((ga >> 28) & 0x7ff) && !(ga & KA_UNCACHED);
}

uint64_t ka_fnv_word(uint64_t h, uint64_t word)
{
    for (int b = 0; b < 8; ++b) {
        h ^= (word >> (8 * b)) & 0xff;
        h *= 0x100000001b3UL;
    }
    return h;
}

/* The unit table is fixed from boot, so its signature is computed once. */
static uint64_t sig_cache;
static int sig_ok;

uint64_t ka_machine_signature(void)
{
    if (sig_ok) {
        return sig_cache;
    }
    uint64_t w[KA_MAX_UNITS];
    unsigned n = (unsigned)ka_boot.nunits;
    for (unsigned i = 0; i < n; ++i) {
        uint64_t v = ka_boot.units[i];
        unsigned j = i;
        for (; j && w[j - 1] > v; --j) {
            w[j] = w[j - 1];
        }
        w[j] = v;
    }
    uint64_t h = KA_FNV_BASIS;
    for (unsigned i = 0; i < n; ++i) {
        h = ka_fnv_word(h, w[i]);
    }
    sig_cache = h;
    sig_ok = 1;
    return h;
}

/* Bits [bit, bit+width) of the 256-bit word `w` := v. */
static void insert(uint64_t w[4], unsigned bit, unsigned width, uint64_t v)
{
    uint64_t mask = width >= 64 ? ~0UL : ((1UL << width) - 1);
    unsigned lo = bit / 64, sh = bit % 64;
    v &= mask;
    w[lo] = (w[lo] & ~(mask << sh)) | (v << sh);
    if (sh && sh + width > 64) {
        unsigned up = 64 - sh;
        w[lo + 1] = (w[lo + 1] & ~(mask >> up)) | (v >> up);
    }
}

static KA_HOT int payload(uint32_t idx, uint64_t w[4])
{
    if (idx >= pk.npay) {
        return ka_engine_fail(&eng, KA_ST_BAD_LAYOUT, idx, pk.npay);
    }
    uint64_t off = pk.opay + (uint64_t)idx * KA_PKG_PAY_BYTES;
    for (int k = 0; k < 4; ++k) {
        w[k] = rd(off + 8 * k);
    }
    return 0;
}

/* `count` words for `unit` streamed by its fetch port from address `at`. */
static int fetch(unsigned unit, uint64_t at, uint32_t count)
{
    if (!(eng.u[unit].fetch >> 16)) {
        return ka_engine_fail(&eng, KA_ST_BAD_STEP, KA_OP_FETCH, unit);
    }
    uint32_t most = eng.u[unit].credit < 255 ? eng.u[unit].credit : 255;
    most = most ? most : 1;
    while (count) {
        uint32_t n = count < most ? count : most;
        if (ka_engine_fetch(&eng, unit, eng.u[unit].fetch & 0xffffu, at, n)) {
            return eng.status;
        }
        at += (uint64_t)n * KA_PKG_PAY_BYTES;
        count -= n;
    }
    return 0;
}

/* One DISPATCH step: `count` payloads from `first`, each host-bound and sent.
 * MEASURED (RV_PC_PROF, card_v8t8_2n): through `payload` a word cost ~167 cycles
 * of interpreter around four ~7-cycle loads; this walks the pointer instead. */
static int dispatch(unsigned unit, uint32_t first, uint32_t count)
{
    if (first > pk.npay || count > pk.npay - first) {
        return ka_engine_fail(&eng, KA_ST_BAD_LAYOUT, first, pk.npay);
    }
    /* A unit with a fetch port pulls a relocation-free step from the package
     * itself: one request per <= 255 words instead of a mailbox send each. */
    if ((eng.u[unit].fetch >> 16) && !pk.nrel) {
        return fetch(unit, (pk.base & ~KA_UNCACHED) + pk.opay + (uint64_t)first * KA_PKG_PAY_BYTES,
                     count);
    }
    uint64_t at = pk.base + pk.opay + (uint64_t)first * KA_PKG_PAY_BYTES;
    if (LOCAL) {
        at = (uintptr_t)&stage.copy[(pk.opay + (uint64_t)first * KA_PKG_PAY_BYTES) >> 3];
    }
    else if (!pk.cached) {
        at |= KA_UNCACHED;
    }
    for (uint32_t k = 0; k < count; ++k, at += KA_PKG_PAY_BYTES) {
        const volatile uint64_t *p = (const volatile uint64_t *)(uintptr_t)at;
        uint64_t w[4] = {p[0], p[1], p[2], p[3]};
        if (ka_engine_send_now(&eng, unit, w) && ka_engine_send(&eng, unit, w)) {
            return eng.status;
        }
    }
    return 0;
}

/* Bits [bit, bit+width) of the 256-bit word `w`. */
static uint64_t extract(const uint64_t w[4], unsigned bit, unsigned width)
{
    uint64_t mask = width >= 64 ? ~0UL : ((1UL << width) - 1);
    unsigned lo = bit / 64, sh = bit % 64;
    uint64_t v = w[lo] >> sh;
    if (sh && sh + width > 64) {
        v |= w[lo + 1] << (64 - sh);
    }
    return v & mask;
}

/* One DISPATCH or REPEAT step as a source of words: template word `j` of
 * repetition `r`, with the repetition's increments added. */
struct src {
    unsigned unit;
    uint32_t first, count, repeats, ninc;
    uint32_t j, r, ic;
    uint64_t base;
};

static int src_open(struct src *q, uint64_t w0, uint64_t arg)
{
    q->unit = (w0 >> 16) & 0xffff;
    q->count = (uint32_t)(w0 >> 32);
    q->first = (uint32_t)arg;
    int rep = (w0 & 0xff) == KA_OP_REPEAT;
    q->repeats = rep ? (uint32_t)((arg >> 32) & 0xffff) : 1;
    q->ninc = rep ? (uint32_t)(arg >> 48) : 0;
    uint32_t span = q->count + (q->ninc + 1) / 2;
    if (q->first > pk.npay || span > pk.npay - q->first) {
        return ka_engine_fail(&eng, KA_ST_BAD_LAYOUT, q->first, pk.npay);
    }
    q->j = q->r = q->ic = 0;
    q->base = pk.base + pk.opay + (uint64_t)q->first * KA_PKG_PAY_BYTES;
    if (LOCAL) {
        q->base = (uintptr_t)&stage.copy[(pk.opay + (uint64_t)q->first * KA_PKG_PAY_BYTES) >> 3];
    }
    else if (!pk.cached) {
        q->base |= KA_UNCACHED;
    }
    uint32_t prev = 0;
    for (uint32_t k = 0; k < q->ninc; ++k) {
        uint64_t off = pk.opay + (uint64_t)(q->first + q->count) * KA_PKG_PAY_BYTES + 16 * k;
        uint64_t head = rd(off);
        unsigned bit = (head >> 32) & 0xff, width = (head >> 40) & 0xff;
        if ((uint32_t)head >= q->count || (uint32_t)head < prev || !width || width > 64 ||
            bit + width > 256) {
            return ka_engine_fail(&eng, KA_ST_BAD_STEP, k, head);
        }
        prev = (uint32_t)head;
    }
    return 0;
}

static int src_left(const struct src *q) { return q->r < q->repeats && q->count; }

/* The next word into `w` from the local copy: each increment is one limb
 * add with its carry (a REPEAT never wraps a
 * field, so a field add is the plain 256-bit add). */
static inline const uint64_t *src_next_local(struct src *q, uint64_t w[4])
{
    const uint64_t *p = (const uint64_t *)(uintptr_t)q->base + 4 * q->j;
    if (q->r) {
        const uint64_t *inc = (const uint64_t *)(uintptr_t)q->base + 4 * q->count;
        for (; q->ic < q->ninc && (uint32_t)inc[2 * q->ic] == q->j; ++q->ic) {
            uint32_t k = q->ic;
            uint64_t head = inc[2 * k];
            if (p != w) {
                w[0] = p[0];
                w[1] = p[1];
                w[2] = p[2];
                w[3] = p[3];
                p = w;
            }
            unsigned bit = (head >> 32) & 0xff, limb = bit >> 6, sh = bit & 63;
            uint64_t v = (uint64_t)q->r * inc[2 * k + 1];
            uint64_t lo = v << sh, old = w[limb];
            w[limb] = old + lo;
            if (limb < 3) {
                w[limb + 1] += (sh ? v >> (64 - sh) : 0) + (w[limb] < old);
            }
        }
    }
    if (++q->j == q->count) {
        q->j = 0;
        q->ic = 0;
        ++q->r;
    }
    return p;
}

/* The next word: the template itself when nothing changes it, else built in
 * `w`. NULL on an invalid increment. */
static const uint64_t *src_next(struct src *q, uint64_t w[4])
{
    if (LOCAL) {
        return src_next_local(q, w);
    }
    const volatile uint64_t *p =
        (const volatile uint64_t *)(uintptr_t)(q->base + (uint64_t)q->j * KA_PKG_PAY_BYTES);
    w[0] = p[0];
    w[1] = p[1];
    w[2] = p[2];
    w[3] = p[3];
    if (q->r) {
        const volatile uint64_t *inc =
            (const volatile uint64_t *)(uintptr_t)(q->base + (uint64_t)q->count * KA_PKG_PAY_BYTES);
        for (uint32_t k = 0; k < q->ninc; ++k) {
            uint64_t head = inc[2 * k];
            if ((uint32_t)head != q->j) {
                continue;
            }
            unsigned bit = (head >> 32) & 0xff, width = (head >> 40) & 0xff;
            if (!width || width > 64 || bit + width > 256) {
                ka_engine_fail(&eng, KA_ST_BAD_STEP, q->j, head);
                return NULL;
            }
            insert(w, bit, width, extract(w, bit, width) + (uint64_t)q->r * inc[2 * k + 1]);
        }
    }
    if (++q->j == q->count) {
        q->j = 0;
        ++q->r;
    }
    return w;
}

/* One REPEAT step, sent whole. */
static int repeat(uint64_t w0, uint64_t arg)
{
    struct src q;
    if (src_open(&q, w0, arg)) {
        return eng.status;
    }
    uint64_t w[4];
    while (src_left(&q)) {
        const uint64_t *p = src_next(&q, w);
        if (!p || (ka_engine_send_now(&eng, q.unit, p) && ka_engine_send(&eng, q.unit, p))) {
            return eng.status;
        }
    }
    return 0;
}

static uint64_t step_word(uint32_t s, unsigned k)
{
    return rd(pk.ostep + (uint64_t)s * KA_PKG_STEP_BYTES + 8 * k);
}

static int is_send(uint64_t w0)
{
    return (w0 & 0xff) == KA_OP_DISPATCH || (w0 & 0xff) == KA_OP_REPEAT;
}

/* The run of consecutive DISPATCH/REPEAT steps from `s0`, sent one word per unit
 * in turn, each unit walking its own steps in order, so every unit starts within
 * one pass rather than after the whole programs ahead of it. Returns the steps
 * taken, 0 when the run reaches fewer than two units (the caller then steps
 * normally). */
/* `head` is step s0's first word, which the caller has already read. */
static uint32_t dispatch_group(uint32_t s0, uint64_t head)
{
    struct src g[KA_MAX_PKG_UNITS];
    uint32_t at[KA_MAX_PKG_UNITS];
    uint32_t n = 0, s1 = s0;
    for (; s1 < pk.nstep; ++s1) {
        uint64_t w0 = s1 == s0 ? head : step_word(s1, 0);
        if (!is_send(w0)) {
            break;
        }
        unsigned unit = (w0 >> 16) & 0xffff;
        if (unit < eng.n && (((eng.u[unit].fetch >> 16) && !pk.nrel) || ((pk.foreign >> unit) & 1))) {
            break; /* fetched, not sent (`dispatch`), or refused (`step`) */
        }
        uint32_t j = 0;
        while (j < n && g[j].unit != unit) {
            ++j;
        }
        if (j == n) {
            if (n == KA_MAX_PKG_UNITS) {
                break;
            }
            g[n].unit = unit;
            at[n++] = s1;
        }
    }
    if (n < 2) {
        return 0;
    }
    for (uint32_t j = 0; j < n; ++j) {
        if (src_open(&g[j], step_word(at[j], 0), step_word(at[j], 1))) {
            return s1 - s0;
        }
    }
    uint64_t w[4];
    int fast = LOCAL;
    for (uint32_t live = n; live && !eng.status;) {
        live = 0;
        for (uint32_t j = 0; j < n; ++j) {
            while (!src_left(&g[j]) && at[j] < s1) {
                uint32_t s = at[j] + 1;
                while (s < s1 && ((step_word(s, 0) >> 16) & 0xffff) != g[j].unit) {
                    ++s;
                }
                at[j] = s;
                if (s < s1 && src_open(&g[j], step_word(s, 0), step_word(s, 1))) {
                    return s1 - s0;
                }
            }
            if (!src_left(&g[j])) {
                continue;
            }
            const uint64_t *p = fast ? src_next_local(&g[j], w) : src_next(&g[j], w);
            if (!p) {
                return s1 - s0;
            }
            if (ka_engine_send_now(&eng, g[j].unit, p) && ka_engine_send(&eng, g[j].unit, p)) {
                return s1 - s0;
            }
            ++live;
        }
    }
    if (!eng.status) {
        ka_engine_drain(&eng);
    }
    return s1 - s0;
}

/* What a wait on the mover waits for. */
enum { MV_UPTO, MV_IDLE, MV_ROOM };

/* Until `n` moves since `s0` are done (MV_UPTO; the mover may be busy with a
 * later one), and the mover is idle too (MV_IDLE), or until its configuration
 * queue has room (MV_ROOM). The bound is on PROGRESS, not on the step: many
 * moves may take far longer than one wait's timeout, and only a mover that
 * stops finishing moves is hung. */
static int mv_wait(uint64_t s0, uint32_t n, int what)
{
    uint64_t t0 = ka_cycles(), s;
    uint32_t seen = KA_MV_DONE(s0);
    for (;;) {
        s = ka_ctrl_rd(KA_R_MVSTAT);
        if (KA_MV_FAULT(s)) {
            return ka_engine_fail(&eng, KA_ST_MOVER_FAULT, KA_MV_FAULT(s), s);
        }
        if (what == MV_ROOM ? KA_MV_ROOM(s) != 0
                            : ((KA_MV_DONE(s) - KA_MV_DONE(s0)) & 0x0fffffff) >= n &&
                                  !(what == MV_IDLE && KA_MV_BUSY(s))) {
            return 0;
        }
        if (KA_MV_DONE(s) != seen) {
            seen = KA_MV_DONE(s);
            t0 = ka_cycles();
        }
        if (ka_cycles() - t0 > eng.timeout) {
            return ka_engine_fail(&eng, KA_ST_TIMEOUT, KA_WAIT_MOVER, s);
        }
        ka_yield();
    }
}

/* The package's moves: MVSTAT at its start, GOs issued since, and whether a GO
 * went out since the last wait for all of them. */
static struct {
    uint64_t s0;
    uint32_t gos;
    int fresh;
} mv;

static int moves_all_done(void)
{
    if (!mv.fresh) {
        return 0;
    }
    mv.fresh = 0;
    return mv_wait(mv.s0, mv.gos, MV_IDLE);
}

/* Writes the mover's configuration queue takes before ROOM is read again. */
static uint32_t mv_credit;

/* Until the configuration queue has room; it drains only as moves finish. */
static int mover_room(void)
{
    if (!mv_credit) {
        if (mv_wait(mv.s0, 0, MV_ROOM)) {
            return eng.status;
        }
        mv_credit = KA_MV_ROOM_WRITES;
    }
    --mv_credit;
    return 0;
}

/* Mover register writes, two (register, value) pairs per payload, into the
 * mover's configuration queue: a later move's registers wait in the queue
 * behind an earlier move's GO. Unless `posted`, the step then waits for the
 * moves it started; posted, the node goes on while they run. */
static int mover(uint32_t first, uint32_t count, int posted)
{
    for (uint32_t k = 0; k < count; ++k) {
        uint64_t w[4];
        if (payload(first + k, w)) {
            return eng.status;
        }
        for (int p = 0; p < 4; p += 2) {
            if (w[p] == KA_MOVER_SKIP) {
                continue;
            }
            if (w[p] >= KA_MV_SPAN || (w[p] & 7)) {
                return ka_engine_fail(&eng, KA_ST_NO_REACH, (unsigned)w[p], w[p + 1]);
            }
            if (mover_room()) {
                return eng.status;
            }
            ka_ctrl_wr(KA_R_MV + (unsigned)w[p], w[p + 1]);
            if (w[p] == 0 && (w[p + 1] & (1UL << 16))) {
                ++mv.gos;
                mv.fresh = 1;
            }
        }
    }
    return posted ? 0 : moves_all_done();
}

/* ------------------------------------------------------------------------
 * Engine mode (docs/spec/dispatch-engine.md): each step is QUEUED as the
 * register writes and WAITs it stands for, and the dispatch engine issues them
 * while this thread reads ahead. Completions are counted in hardware; only a
 * fault or a code the engine does not claim reaches the completion queue. */
static struct {
    int on;
    unsigned cap, room;
    uint64_t mb[KA_NM_GO];   /* DST, ARG0..3 as last queued */
    unsigned mbv;            /* which of them `mb` holds */
    uint32_t exp[KA_MAX_PKG_UNITS];
    unsigned issued;         /* STAT's issue count when it last moved */
    uint64_t t_moved;
    uint16_t mapped[KA_MAX_PKG_UNITS]; /* coordinates the map holds, y << 8 | x */
    unsigned nmapped;
} de;

/* Faults and unclaimed signals, and no progress for a timeout. A faulted move
 * never completes, so the engine's WAIT for it would hold until the timeout:
 * the mover's fault is read here. */
static int de_watch(uint64_t stat)
{
    if (KA_NM_STAT_COUNT(ka_nm_rd(KA_NM_STAT)) && ka_engine_drain(&eng)) {
        return eng.status;
    }
    uint64_t m = ka_ctrl_rd(KA_R_MVSTAT);
    if (KA_MV_FAULT(m)) {
        return ka_engine_fail(&eng, KA_ST_MOVER_FAULT, KA_MV_FAULT(m), m);
    }
    if (KA_DE_STAT_OVF(stat)) {
        return ka_engine_fail(&eng, KA_ST_BAD_STEP, 0xde, stat);
    }
    uint64_t now = ka_cycles();
    if (KA_DE_STAT_ISSUED(stat) != de.issued) {
        de.issued = KA_DE_STAT_ISSUED(stat);
        de.t_moved = now;
    }
    else if (now - de.t_moved > eng.timeout) {
        return ka_engine_fail(&eng, KA_ST_TIMEOUT, KA_WAIT_ENGINE, stat);
    }
    return 0;
}

/* Out of line: only a full queue gets here, and inlined it spilled six
 * registers on every entry (PC profile: 25 cycles a push). */
static __attribute__((noinline)) int de_refill(void)
{
    while (!de.room) {
        uint64_t s = ka_ctrl_rd(KA_R_DE_STAT);
        unsigned used = KA_DE_STAT_USED(s);
        de.room = used < de.cap ? de.cap - used : 0;
        if (!de.room && de_watch(s)) {
            return eng.status;
        }
    }
    return 0;
}

static inline __attribute__((always_inline)) int de_push(unsigned code, uint64_t v)
{
    if (__builtin_expect(!de.room, 0) && de_refill()) {
        return eng.status;
    }
    --de.room;
    ka_de_queue(code, v);
    return 0;
}

/* Mailbox register `r`, skipped when it already holds `v`. */
static inline __attribute__((always_inline)) int de_mb(unsigned r, uint64_t v)
{
    if ((de.mbv >> r) & 1u && de.mb[r] == v) {
        return 0;
    }
    de.mb[r] = v;
    de.mbv |= 1u << r;
    return de_push(KA_DE_MB(r), v);
}

static int de_wait(unsigned src, uint32_t want)
{
    return want ? de_push(KA_DE_WAIT, (uint64_t)src << 56 | (want & KA_DE_CTR_MASK)) : 0;
}

static KA_HOT int de_send_word(unsigned unit, const uint64_t w[4])
{
    struct ka_unit_state *s = &eng.u[unit];
    if (s->sent + 1 > s->credit && de_wait(unit, s->sent + 1 - s->credit)) {
        return eng.status;
    }
    if (de_mb(KA_NM_DST, ((uint64_t)s->y << 8) | s->x) || de_mb(KA_NM_ARG0, w[0]) ||
        de_mb(KA_NM_ARG1, w[1]) || de_mb(KA_NM_ARG2, w[2]) || de_mb(KA_NM_ARG3, w[3]) ||
        de_push(KA_DE_MB(KA_NM_GO), 1)) {
        return eng.status;
    }
    ++s->sent;
    ++eng.sent;
    return 0;
}

static KA_HOT int de_fetch(unsigned unit, uint64_t at, uint32_t count)
{
    struct ka_unit_state *s = &eng.u[unit];
    if (!(s->fetch >> 16)) {
        return ka_engine_fail(&eng, KA_ST_BAD_STEP, KA_OP_FETCH, unit);
    }
    uint32_t room = s->credit < KA_FETCH_DEPTH ? s->credit : KA_FETCH_DEPTH;
    uint32_t most = room < 255 ? room : 255;
    most = most ? most : 1;
    while (count) {
        uint32_t n = count < most ? count : most;
        if ((s->sent + n > room && de_wait(unit, s->sent + n - room)) ||
            de_mb(KA_NM_DST, KA_NM_TYPED(KA_T_MEM_RD_REQ) | (s->fetch & 0xffffu)) ||
            de_mb(KA_NM_ARG0, 0) || de_mb(KA_NM_ARG1, 0) ||
            de_mb(KA_NM_ARG2, ((uint64_t)((s->y << 4) | s->x) << 40) | (1UL << 30)) ||
            de_mb(KA_NM_ARG3, (at << 24) | (0x60UL << 8) | n) ||
            de_push(KA_DE_MB(KA_NM_GO), 1)) {
            return eng.status;
        }
        s->sent += n;
        eng.sent += n;
        at += (uint64_t)n * KA_PKG_PAY_BYTES;
        count -= n;
    }
    return 0;
}

static int de_dispatch(unsigned unit, uint32_t first, uint32_t count)
{
    if (first > pk.npay || count > pk.npay - first) {
        return ka_engine_fail(&eng, KA_ST_BAD_LAYOUT, first, pk.npay);
    }
    if ((eng.u[unit].fetch >> 16) && !pk.nrel) {
        return de_fetch(unit, (pk.base & ~KA_UNCACHED) + pk.opay + (uint64_t)first * KA_PKG_PAY_BYTES,
                        count);
    }
    for (uint32_t k = 0; k < count; ++k) {
        uint64_t w[4];
        if (payload(first + k, w) || de_send_word(unit, w)) {
            return eng.status;
        }
    }
    return 0;
}

static int de_repeat(uint64_t w0, uint64_t arg)
{
    struct src q;
    if (src_open(&q, w0, arg)) {
        return eng.status;
    }
    uint64_t w[4];
    while (src_left(&q)) {
        const uint64_t *p = src_next(&q, w);
        if (!p || de_send_word(q.unit, p)) {
            return eng.status;
        }
    }
    return 0;
}

static int de_mover(uint32_t first, uint32_t count, int posted)
{
    if (first > pk.npay || count > pk.npay - first) {
        return ka_engine_fail(&eng, KA_ST_BAD_LAYOUT, first, pk.npay);
    }
    for (uint32_t k = 0; k < count; ++k) {
        uint64_t w[4];
        if (pk.nrel) {
            if (payload(first + k, w)) {
                return eng.status;
            }
        }
        else {
            uint64_t off = pk.opay + (uint64_t)(first + k) * KA_PKG_PAY_BYTES;
            for (int j = 0; j < 4; ++j) {
                w[j] = rd(off + 8 * j);
            }
        }
        for (int p = 0; p < 4; p += 2) {
            if (w[p] == KA_MOVER_SKIP) {
                continue;
            }
            if (w[p] >= KA_MV_SPAN || (w[p] & 7)) {
                return ka_engine_fail(&eng, KA_ST_NO_REACH, (unsigned)w[p], w[p + 1]);
            }
            if (de_push(KA_DE_MV(w[p]), w[p + 1])) {
                return eng.status;
            }
            if (w[p] == 0 && (w[p + 1] & (1UL << 16))) {
                ++mv.gos;
            }
        }
    }
    return posted ? 0 : de_wait(KA_DE_MOVER, mv.gos);
}

/* An ENGINE step: entries the host compiled, copied into the engine as read.
 * A code past WAIT would land on the engine's own control registers. */
static KA_HOT int de_stream(uint32_t first, uint32_t count)
{
    if (first > pk.npay || (count + 2) / 3 > pk.npay - first) {
        return ka_engine_fail(&eng, KA_ST_BAD_LAYOUT, first, pk.npay);
    }
    uint64_t rel = ((pk.base & ~KA_UNCACHED) + pk.opay) << 24;
    uint64_t off = pk.opay + (uint64_t)first * KA_PKG_PAY_BYTES;
    while (count) {
        uint64_t codes = rd(off);
        for (unsigned j = 1; j <= 3 && count; ++j, --count, codes >>= 8) {
            unsigned c = codes & 0xff;
            uint64_t v = rd(off + 8 * j);
            if (c & KA_ENGINE_REL) {
                v += rel;
            }
            c &= ~KA_ENGINE_REL;
            if (c > KA_DE_WAIT) {
                return ka_engine_fail(&eng, KA_ST_BAD_STEP, KA_OP_ENGINE, codes);
            }
            if (de_push(c, v)) {
                return eng.status;
            }
        }
        off += KA_PKG_PAY_BYTES;
    }
    return 0;
}

/* Every unit has retired what it was sent and every AWAIT holds; every move
 * is done. */
static int de_barrier(void)
{
    for (unsigned u = 0; u < eng.n; ++u) {
        uint32_t t = eng.u[u].sent > de.exp[u] ? eng.u[u].sent : de.exp[u];
        if (de_wait(u, t)) {
            return eng.status;
        }
    }
    return de_wait(KA_DE_MOVER, mv.gos);
}

/* Until every queued entry has issued. */
static int de_drain(void)
{
    for (;;) {
        uint64_t s = ka_ctrl_rd(KA_R_DE_STAT);
        if (!KA_DE_STAT_USED(s)) {
            de.room = de.cap;
            return 0;
        }
        if (de_watch(s)) {
            return eng.status;
        }
    }
}

/* Whether this package runs through the engine, and if so the engine set up
 * for it: its units mapped to counters 0..n-1, counters and queue cleared. */
static int de_begin(void)
{
    de.on = 0;
    uint64_t s = ka_ctrl_rd(KA_R_DE_STAT);
    if ((ka_boot.flags & KA_BOOT_F_NOENGINE) || KA_DE_STAT_MAGIC(s) != KA_DE_MAGIC ||
        eng.n > KA_DE_UNITS) {
        return 0;
    }
#ifdef KA_DEBUG
    if (ka_dbg_noengine()) {
        return 0;
    }
#endif
    for (unsigned u = 0; u < eng.n; ++u) {
        if (!eng.u[u].plain) {
            return 0;
        }
    }
    for (unsigned i = 0; i < de.nmapped; ++i) {
        ka_ctrl_wr(KA_R_DE_MAP, de.mapped[i]);
    }
    de.nmapped = 0;
    for (unsigned u = 0; u < eng.n; ++u) {
        uint16_t at = (uint16_t)((eng.u[u].y << 8) | eng.u[u].x);
        ka_ctrl_wr(KA_R_DE_MAP, (1UL << 31) | ((uint64_t)u << 16) | at);
        de.mapped[de.nmapped++] = at;
        de.exp[u] = 0;
    }
    ka_ctrl_wr(KA_R_DE_CTL, 3);
    de.cap = KA_DE_STAT_DEPTH(s);
    de.room = de.cap;
    de.mbv = 0;
    de.issued = 0;
    de.t_moved = ka_cycles();
    de.on = 1;
    return 0;
}

/* The engine off; after a failure, each unit's words not yet retired are
 * handed to the firmware's counters so its quiesce drains them. */
static void de_end(void)
{
    if (!de.on) {
        return;
    }
    if (eng.status) {
        for (unsigned u = 0; u < eng.n; ++u) {
            uint32_t got = (uint32_t)ka_ctrl_rd(KA_R_DE_CTR + 8 * u) & KA_DE_CTR_MASK;
            uint32_t left = (eng.u[u].sent - got) & KA_DE_CTR_MASK;
            eng.u[u].inflight = left;
            eng.outstanding += left;
        }
    }
    ka_ctrl_wr(KA_R_DE_CTL, 2);
    de.on = 0;
}

/* Rings consumed so far, per source mesh, as the hardware's 16-bit counts. */
static uint64_t bell_base;

void ka_package_boot(void) { bell_base = ka_ctrl_rd(KA_R_DBCNT); }

static int wait_bell(unsigned mesh, uint32_t count)
{
    uint64_t t0 = ka_cycles();
    unsigned sh = 16 * (mesh & 3);
    for (;;) {
        uint64_t c = ka_ctrl_rd(KA_R_DBCNT);
        uint32_t got = ((c >> sh) - (bell_base >> sh)) & 0xffff;
        if (got >= count) {
            /* Consume `count` rings. */
            uint64_t used = ((bell_base >> sh) + count) & 0xffff;
            bell_base = (bell_base & ~(0xffffUL << sh)) | used << sh;
            return 0;
        }
        if (ka_cycles() - t0 > eng.timeout) {
            return ka_engine_fail(&eng, KA_ST_TIMEOUT, KA_WAIT_BELL, c);
        }
        ka_yield();
    }
}

/* ------------------------------------------------------------------------
 * Interlink, memory, control-region and register steps (package-format.md
 * s4.9-4.14). Out of line and laid out cold: none sits in a dispatch loop. */
#if __has_attribute(optimize)
#define KA_COLD __attribute__((cold, noinline, optimize("Os")))
#else
#define KA_COLD __attribute__((cold, noinline))
#endif

/* Rings this node has queued, as the interlink's 16-bit sent count runs. */
static uint16_t rings;

/* A step's timeout check: TIMEOUT(what, value) once `t0` is a timeout old. */
static int expired(uint64_t t0, unsigned what, uint64_t value)
{
    return ka_cycles() - t0 > eng.timeout ? ka_engine_fail(&eng, KA_ST_TIMEOUT, what, value) : 0;
}

/* A ring once fewer than KA_IL_RING_DEPTH are queued: a fifth would be dropped.
 * Engine-issued rings are not counted here, so a sent count past ours resyncs. */
static KA_COLD int ring(unsigned mesh, uint32_t slot, uint64_t arg)
{
    uint64_t t0 = ka_cycles();
    for (;;) {
        uint16_t sent = (uint16_t)KA_SIG_SENT(ka_ctrl_rd(KA_R_SIG));
        int16_t ahead = (int16_t)(rings - sent);
        if (ahead < 0) {
            rings = sent;
            ahead = 0;
        }
        if ((unsigned)ahead < KA_IL_RING_DEPTH) {
            break;
        }
        if (expired(t0, KA_WAIT_RING, sent)) {
            return eng.status;
        }
    }
    ka_ctrl_wr(KA_R_IL + KA_IL_RING, (mesh & 3) | (uint64_t)(slot & 0xff) << 8 |
                                         (arg & 0xffff) << 16 | ((arg >> 16) & 1) << 32);
    ++rings;
    return 0;
}

/* Until signal slot `slot` holds `n`; CONSUME then subtracts `n`. A select or a
 * consume lands two registers deep, so two reads follow each. */
static KA_COLD int wait_slot(unsigned slot, uint32_t n, unsigned flags)
{
    if (slot >= 16) {
        return ka_engine_fail(&eng, KA_ST_BAD_STEP, KA_OP_WAIT_SLOT, slot);
    }
    ka_ctrl_wr(KA_R_IL + KA_IL_SELECT, slot);
    (void)ka_ctrl_rd(KA_R_SIG);
    (void)ka_ctrl_rd(KA_R_SIG);
    uint64_t t0 = ka_cycles(), w;
    while (w = ka_ctrl_rd(KA_R_SIG), KA_SIG_SLOT(w) != slot || KA_SIG_COUNT(w) < n) {
        if (expired(t0, KA_WAIT_SLOT, w)) {
            return eng.status;
        }
        ka_yield();
    }
    if (flags & KA_STEP_F_CONSUME) {
        ka_ctrl_wr(KA_R_IL + KA_IL_CONSUME, slot | (uint64_t)n << 32);
        (void)ka_ctrl_rd(KA_R_SIG);
        (void)ka_ctrl_rd(KA_R_SIG);
    }
    return 0;
}

/* The control-region offsets a package may write: the host interrupt, the
 * interlink, the D-cache and the transform bank. */
static int ctrl_ok(uint64_t o)
{
    return o == KA_R_IRQ || (o >= KA_R_IL && o < KA_R_MV && !(o & 7)) || o == KA_R_DCACHE ||
           o == KA_R_XF_SEL || o == KA_R_XF_DATA;
}

/* STORE and CTRL: (address or offset, value) pairs, two a payload. */
static KA_COLD int pairs(unsigned op, uint32_t first, uint32_t count, unsigned flags)
{
    for (uint32_t k = 0; k < count; ++k) {
        uint64_t w[4];
        if (payload(first + k, w)) {
            return eng.status;
        }
        for (int p = 0; p < 4; p += 2) {
            uint64_t at = w[p], v = w[p + 1];
            if (at == KA_MOVER_SKIP) {
                continue;
            }
            if (flags & KA_STEP_F_REG) {
                v = reg[v & (KA_PKG_REGS - 1)];
            }
            if (op == KA_OP_STORE) {
                if (at & 7) {
                    return ka_engine_fail(&eng, KA_ST_BAD_STEP, KA_OP_STORE, at);
                }
                ka_st64(at, v);
            }
            else if (!ctrl_ok(at)) {
                return ka_engine_fail(&eng, KA_ST_NO_REACH, (unsigned)at, v);
            }
            else if (at == KA_R_DCACHE) {
                ka_dcache((unsigned)v);
            }
            else {
                ka_ctrl_wr((unsigned)at, v);
            }
        }
    }
    return 0;
}

/* reg[rd] := the 64 bits at payload `arg`'s address (+ reg[count] INDEXED), or
 * the control register at that offset (CTRL). */
static KA_COLD int load(unsigned rd, uint32_t count, uint64_t arg, unsigned flags)
{
    uint64_t w[4];
    if (rd >= KA_PKG_REGS) {
        return ka_engine_fail(&eng, KA_ST_BAD_STEP, KA_OP_LOAD, rd);
    }
    if (payload((uint32_t)arg, w)) {
        return eng.status;
    }
    if ((flags & KA_STEP_F_IMM) && (flags != KA_STEP_F_IMM || count)) {
        return ka_engine_fail(&eng, KA_ST_BAD_STEP, KA_OP_LOAD, flags);
    }
    if (flags == KA_STEP_F_IMM) {
        reg[rd] = w[0];
        return 0;
    }
    uint64_t at = w[0] + ((flags & KA_STEP_F_INDEXED) ? reg[count & (KA_PKG_REGS - 1)] : 0);
    if ((flags & KA_STEP_F_CTRL) && at < KA_CTRL_SPAN && !(at & 7)) {
        reg[rd] = ka_ctrl_rd((unsigned)at);
    }
    else if (!(flags & KA_STEP_F_CTRL) && !(at & 7)) {
        reg[rd] = ka_ld64(at);
    }
    else {
        return ka_engine_fail(&eng, KA_ST_NO_REACH, KA_OP_LOAD, at);
    }
    return 0;
}

static KA_COLD int alu(unsigned rd, uint32_t count, uint64_t arg, unsigned flags)
{
    uint64_t a = reg[(count >> 8) & 15];
    uint64_t b = (flags & KA_STEP_F_IMM) ? arg : reg[(count >> 12) & 15], v;
    switch (count & 0xff) {
    case KA_ALU_MOV: v = b; break;
    case KA_ALU_ADD: v = a + b; break;
    case KA_ALU_SUB: v = a - b; break;
    case KA_ALU_MUL: v = a * b; break;
    case KA_ALU_AND: v = a & b; break;
    case KA_ALU_OR:  v = a | b; break;
    case KA_ALU_XOR: v = a ^ b; break;
    case KA_ALU_SHL: v = a << (b & 63); break;
    case KA_ALU_SHR: v = a >> (b & 63); break;
    case KA_ALU_MIN: v = a < b ? a : b; break;
    case KA_ALU_MAX: v = a > b ? a : b; break;
    case KA_ALU_DIVU: v = b ? a / b : ~0UL; break;
    case KA_ALU_REMU: v = b ? a % b : a; break;
    default: return ka_engine_fail(&eng, KA_ST_BAD_STEP, KA_OP_ALU, count);
    }
    if (rd >= KA_PKG_REGS) {
        return ka_engine_fail(&eng, KA_ST_BAD_STEP, KA_OP_ALU, rd);
    }
    reg[rd] = v;
    return 0;
}

static KA_COLD int jump(unsigned target, uint32_t count, uint64_t arg, unsigned flags)
{
    uint64_t a = reg[(count >> 8) & 15];
    uint64_t b = (flags & KA_STEP_F_IMM) ? arg : reg[(count >> 12) & 15];
    int take;
    switch (count & 0xff) {
    case KA_JC_ALWAYS: take = 1; break;
    case KA_JC_EQ: take = a == b; break;
    case KA_JC_NE: take = a != b; break;
    case KA_JC_LTU: take = a < b; break;
    case KA_JC_GEU: take = a >= b; break;
    case KA_JC_LT: take = (int64_t)a < (int64_t)b; break;
    case KA_JC_GE: take = (int64_t)a >= (int64_t)b; break;
    default: return ka_engine_fail(&eng, KA_ST_BAD_STEP, KA_OP_JUMP, count);
    }
    if (target > pk.nstep) {
        return ka_engine_fail(&eng, KA_ST_BAD_STEP, KA_OP_JUMP, target);
    }
    if (take) {
        pc_next = target;
    }
    return 0;
}

/* One 64-bit word of the package, wherever later reads take it from: the local
 * copy, and the package itself (a fetch port reads that). */
static void pk_put(uint64_t off, uint64_t v)
{
    if (LOCAL) {
        stage.copy[off >> 3] = v;
    }
    if (pk.cached) {
        *(volatile uint64_t *)(uintptr_t)(pk.base + off) = v;
    }
    else {
        ka_st64(pk.base + off, v);
    }
}

/* Payload or step `index`'s field := reg[rs] >> shift, or += it (ADD). */
static KA_COLD int patch(unsigned rs, uint32_t index, uint64_t arg)
{
    unsigned bit = KA_PATCH_BIT(arg), width = KA_PATCH_WIDTH(arg), steps = KA_PATCH_STEPS(arg);
    unsigned shift = KA_PATCH_SHIFT(arg);
    if (rs >= KA_PKG_REGS || !width || width > 64 || shift > 63 ||
        bit + width > (steps ? 128u : 256u) || index >= (steps ? pk.nstep : pk.npay)) {
        return ka_engine_fail(&eng, KA_ST_BAD_STEP, KA_OP_PATCH, arg);
    }
    uint64_t off = steps ? pk.ostep + (uint64_t)index * KA_PKG_STEP_BYTES
                         : pk.opay + (uint64_t)index * KA_PKG_PAY_BYTES;
    uint64_t w[4] = {0, 0, 0, 0};
    unsigned lo = bit / 64, hi = (bit + width - 1) / 64;
    for (unsigned k = lo; k <= hi; ++k) {
        w[k] = rd(off + 8 * k);
    }
    uint64_t v = reg[rs] >> shift;
    insert(w, bit, width, KA_PATCH_ADD(arg) ? extract(w, bit, width) + v : v);
    for (unsigned k = lo; k <= hi; ++k) {
        pk_put(off + 8 * k, w[k]);
    }
    if (pk.cached) {
        ka_dcache(KA_DCACHE_FLUSH);
    }
    return 0;
}

/* One flit through the mailbox once the previous one is taken, draining
 * completions meanwhile: a full queue holds the hub that would take it. */
static int mb_go(uint64_t dst, const uint64_t w[4])
{
    uint64_t t0 = ka_cycles();
    while (ka_nm_rd(KA_NM_STAT) & KA_NM_STAT_OFFERED) {
        if (ka_engine_drain(&eng) || expired(t0, KA_WAIT_OFFER, dst)) {
            return eng.status;
        }
    }
    ka_nm_wr(KA_NM_DST, dst);
    ka_nm_wr(KA_NM_ARG0, w[0]);
    ka_nm_wr(KA_NM_ARG1, w[1]);
    ka_nm_wr(KA_NM_ARG2, w[2]);
    ka_nm_wr(KA_NM_ARG3, w[3]);
    ka_nm_wr(KA_NM_GO, 1);
    return 0;
}

/* `count` flits of type arg >> 32 to unit `u`: payloads from (uint32_t)arg,
 * or (MEM) that payload as a CU_DATA descriptor and `count` words from the
 * address in the next payload. ACK waits for the unit's DATA_RECEIVED. */
static KA_COLD int send(unsigned u, uint32_t count, uint64_t arg, unsigned flags)
{
    if (u >= eng.n || ((pk.foreign >> u) & 1)) {
        return ka_engine_fail(&eng, KA_ST_BAD_UNIT, u, arg);
    }
    struct ka_unit_state *s = &eng.u[u];
    uint64_t dst = KA_NM_TYPED((arg >> 32) & 0xf) | (uint64_t)s->y << 8 | s->x;
    uint32_t first = (uint32_t)arg, acks = s->acks;
    uint64_t w[4], src[4];
    eng.dst = ~0UL;
    if (flags & KA_STEP_F_MEM) {
        if (payload(first, w) || payload(first + 1, src) || mb_go(dst, w)) {
            return eng.status;
        }
        for (uint32_t k = 0; k < count; ++k) {
            for (int j = 0; j < 4; ++j) {
                w[j] = ka_ld64(src[0] + 32 * (uint64_t)k + 8 * (uint64_t)j);
            }
            if (mb_go(dst, w)) {
                return eng.status;
            }
        }
    }
    else {
        for (uint32_t k = 0; k < count; ++k) {
            if (payload(first + k, w) || mb_go(dst, w)) {
                return eng.status;
            }
        }
    }
    uint64_t t0 = ka_cycles();
    while ((flags & KA_STEP_F_ACK) && s->acks == acks) {
        if (ka_engine_drain(&eng) || expired(t0, KA_WAIT_ACK, u)) {
            return eng.status;
        }
    }
    return 0;
}

/* `count` RX flits, from unit `u` (0xffff: any), their 256-bit payloads to the
 * address in payload `arg`, one 32-byte word each. */
static KA_COLD int recv(unsigned u, uint32_t count, uint64_t arg)
{
    uint64_t w[4];
    if (payload((uint32_t)arg, w)) {
        return eng.status;
    }
    for (uint32_t k = 0; k < count; ++k) {
        uint64_t t0 = ka_cycles(), h;
        while (!((h = ka_rx_rd(KA_RX_HDR)) >> 63)) {
            if (ka_engine_drain(&eng) || expired(t0, KA_WAIT_RX, k)) {
                return eng.status;
            }
        }
        if (u != 0xffff && (u >= eng.n || ((h >> 20) & 0xf) != eng.u[u].x ||
                            ((h >> 16) & 0xf) != eng.u[u].y)) {
            return ka_engine_fail(&eng, KA_ST_BAD_UNIT, u, h);
        }
        for (unsigned j = 0; j < 4; ++j) {
            ka_st64(w[0] + 32 * (uint64_t)k + 8 * j, ka_rx_rd(KA_RX_P0 + j));
        }
        ka_rx_pop();
    }
    return 0;
}

static KA_HOT int header(const struct ka_pkg_run *r)
{
    uint64_t h = rd(KA_PH_MAGIC * 8);
    if ((uint32_t)h != KA_PKG_MAGIC) {
        return ka_engine_fail(&eng, KA_ST_BAD_MAGIC, 0, h);
    }
    if (((h >> 32) & 0xffff) != KA_PKG_VERSION || (h >> 48) != KA_PKG_HEADER) {
        return ka_engine_fail(&eng, KA_ST_BAD_VERSION, 0, h);
    }
    uint64_t size = rd(KA_PH_SIZE * 8), c1 = rd(KA_PH_COUNTS * 8), c2 = rd(KA_PH_COUNTS2 * 8);
    pk.total = (uint32_t)size;
    eng.keep_rx = ((size >> 32) & KA_PKG_F_RX) != 0;
    pk.npay = (uint32_t)c1;
    pk.nrel = (uint32_t)(c1 >> 32);
#ifndef KA_SPEC_MEMORY_MUTANT
    if (pk.nrel) {
        return ka_engine_fail(&eng, KA_ST_BAD_RELOC, 0, pk.nrel);
    }
#endif
    pk.nbuf = c2 & 0xffff;
    pk.nstep = (c2 >> 16) & 0xffff;
    pk.nunit = (c2 >> 32) & 0xffff;
    pk.ackres = (uint32_t)(c2 >> 48);
    pk.ounit = rd(KA_PH_OFF_UNIT * 8);
    pk.obuf = rd(KA_PH_OFF_BUF * 8);
    pk.ostep = rd(KA_PH_OFF_STEP * 8);
    pk.orel = rd(KA_PH_OFF_REL * 8);
    pk.opay = rd(KA_PH_OFF_PAY * 8);
    if (pk.total < KA_PKG_HEADER || pk.total > r->bytes ||
        pk.ounit + (uint64_t)pk.nunit * KA_PKG_UNIT_BYTES > pk.total ||
        pk.obuf + (uint64_t)pk.nbuf * KA_PKG_BUF_BYTES > pk.total ||
        pk.ostep + (uint64_t)pk.nstep * KA_PKG_STEP_BYTES > pk.total ||
        pk.orel + (uint64_t)pk.nrel * KA_PKG_REL_BYTES > pk.total ||
        pk.opay + (uint64_t)pk.npay * KA_PKG_PAY_BYTES > pk.total) {
        return ka_engine_fail(&eng, KA_ST_BAD_LAYOUT, 0, size);
    }
    if (pk.nunit > KA_MAX_PKG_UNITS || pk.nbuf > KA_MAX_BUFFERS) {
        return ka_engine_fail(&eng, KA_ST_TOO_LARGE, pk.nunit, pk.nbuf);
    }
    uint64_t sig = rd(KA_PH_SIG * 8);
    if (sig && sig != ka_machine_signature()) {
        return ka_engine_fail(&eng, KA_ST_BAD_SIGNATURE, 0, ka_machine_signature());
    }
    if (((size >> 32) & KA_PKG_F_CHECKSUM) || (r->flags & KA_RUN_F_CHECKSUM)) {
        uint64_t sum = KA_FNV_BASIS;
        for (uint64_t off = 0; off < pk.total; off += 8) {
            sum = ka_fnv_word(sum, off == KA_PH_CHECK * 8 ? 0 : rd(off));
        }
        if (sum != rd(KA_PH_CHECK * 8)) {
            return ka_engine_fail(&eng, KA_ST_BAD_CHECKSUM, 0, sum);
        }
    }
    return 0;
}

static int step(const struct ka_pkg_run *r, uint32_t s, uint64_t w0, int *end)
{
    uint64_t arg = rd(pk.ostep + (uint64_t)s * KA_PKG_STEP_BYTES + 8);
    unsigned op = w0 & 0xff, unit = (w0 >> 16) & 0xffff, flags = (w0 >> 8) & 0xff;
    uint32_t count = (uint32_t)(w0 >> 32);
    if ((op == KA_OP_DISPATCH || op == KA_OP_REPEAT || op == KA_OP_FETCH) && unit < eng.n &&
        ((pk.foreign >> unit) & 1)) {
        return ka_engine_fail(&eng, KA_ST_BAD_UNIT, unit, eng.u[unit].mesh);
    }
    switch (op) {
    case KA_OP_END:
        *end = 1;
        return 0;
    case KA_OP_DISPATCH:
        if (dispatch(unit, (uint32_t)arg, count)) {
            return eng.status;
        }
        return ka_engine_drain(&eng);
    case KA_OP_REPEAT:
        if (repeat(w0, arg)) {
            return eng.status;
        }
        return ka_engine_drain(&eng);
    case KA_OP_AWAIT:
        if (flags == KA_STEP_F_IMM) {
            if (unit >= eng.n || count || arg) {
                return ka_engine_fail(&eng, KA_ST_BAD_STEP, KA_OP_AWAIT, unit);
            }
            count = eng.u[unit].sent - eng.u[unit].expected;
        }
        return ka_engine_await(&eng, unit, count);
    case KA_OP_BARRIER:
        return ka_engine_barrier(&eng) ? eng.status : moves_all_done();
    case KA_OP_MOVER:
        return mover((uint32_t)arg, count, (w0 >> 8) & KA_STEP_F_POSTED);
    case KA_OP_MWAIT:
        return mv_wait(mv.s0, count, MV_UPTO);
    case KA_OP_FETCH:
        if (unit >= eng.n) {
            return ka_engine_fail(&eng, KA_ST_BAD_UNIT, unit, 0);
        }
        if (fetch(unit, arg, count)) {
            return eng.status;
        }
        return ka_engine_drain(&eng);
    case KA_OP_RING:
        return ring(unit, count, arg);
    case KA_OP_WAIT_BELL:
        return wait_bell(unit, count);
    case KA_OP_ASSERT_ZERO:
        if (unit >= KA_PKG_REGS || flags || arg) {
            return ka_engine_fail(&eng, KA_ST_BAD_STEP, KA_OP_ASSERT_ZERO, unit);
        }
        return reg[unit] ? ka_engine_fail(&eng, KA_ST_CONTRACT, count, reg[unit]) : 0;
    case KA_OP_SIGNAL:
        if (r->signal) {
            r->signal(r->ctx, s, count, arg);
        }
        return 0;
    case KA_OP_SETTLE:
        ka_delay(count);
        return 0;
    case KA_OP_WAIT_SLOT:
        return wait_slot(unit, count, flags);
    case KA_OP_STORE:
    case KA_OP_CTRL:
        return pairs(op, (uint32_t)arg, count, flags);
    case KA_OP_LOAD:
        return load(unit, count, arg, flags);
    case KA_OP_ALU:
        return alu(unit, count, arg, flags);
    case KA_OP_JUMP:
        return jump(unit, count, arg, flags);
    case KA_OP_PATCH:
        return patch(unit, count, arg);
    case KA_OP_SEND:
        return send(unit, count, arg, flags);
    case KA_OP_RECV:
        return recv(unit, count, arg);
    default:
        return ka_engine_fail(&eng, KA_ST_BAD_STEP, op, w0);
    }
}

/* One step in engine mode: queued, except the ones only the firmware can do,
 * which wait for the queue to drain and then run as `step`. */
static KA_HOT int de_step(const struct ka_pkg_run *r, uint32_t s, uint64_t w0, int *end)
{
    uint64_t arg = rd(pk.ostep + (uint64_t)s * KA_PKG_STEP_BYTES + 8);
    unsigned op = w0 & 0xff, unit = (w0 >> 16) & 0xffff, flags = (w0 >> 8) & 0xff;
    uint32_t count = (uint32_t)(w0 >> 32);
    if ((op == KA_OP_DISPATCH || op == KA_OP_AWAIT || op == KA_OP_FETCH) && unit >= eng.n) {
        return ka_engine_fail(&eng, KA_ST_BAD_UNIT, unit, 0);
    }
    if ((op == KA_OP_DISPATCH || op == KA_OP_REPEAT || op == KA_OP_FETCH) && unit < eng.n &&
        ((pk.foreign >> unit) & 1)) {
        return ka_engine_fail(&eng, KA_ST_BAD_UNIT, unit, eng.u[unit].mesh);
    }
    switch (op) {
    case KA_OP_END:
        *end = 1;
        return 0;
    case KA_OP_DISPATCH:
        return de_dispatch(unit, (uint32_t)arg, count);
    case KA_OP_REPEAT:
        return de_repeat(w0, arg);
    case KA_OP_AWAIT:
        if (flags == KA_STEP_F_IMM) {
            if (unit >= eng.n || count || arg) {
                return ka_engine_fail(&eng, KA_ST_BAD_STEP, KA_OP_AWAIT, unit);
            }
            de.exp[unit] = eng.u[unit].sent;
        } else {
            de.exp[unit] += count;
        }
        return de_wait(unit, de.exp[unit]);
    case KA_OP_BARRIER:
        return de_barrier();
    case KA_OP_MOVER:
        return de_mover((uint32_t)arg, count, (w0 >> 8) & KA_STEP_F_POSTED);
    case KA_OP_MWAIT:
        return de_wait(KA_DE_MOVER, count);
    case KA_OP_RING:
        return de_push(KA_DE_IL(KA_IL_RING), (unit & 3) | (uint64_t)(count & 0xff) << 8 |
                                                 (arg & 0xffff) << 16 | ((arg >> 16) & 1) << 32);
    case KA_OP_ENGINE:
        return de_stream((uint32_t)arg, count);
    case KA_OP_FETCH:
        return de_fetch(unit, arg, count);
    case KA_OP_ALU:
        return alu(unit, count, arg, flags);
    case KA_OP_JUMP:
        return jump(unit, count, arg, flags);
    case KA_OP_PATCH:
        return patch(unit, count, arg);
    default:
        /* The rest wait for what the engine holds; a SEND or RECV writes the
         * mailbox registers the engine's shadow tracks. */
        if (de_drain() || step(r, s, w0, end)) {
            return eng.status;
        }
        de.mbv = 0;
        return 0;
    }
}

int ka_package_run(const struct ka_pkg_run *r, struct ka_pkg_result *out)
{
    uint64_t t0 = ka_cycles();
    uint64_t idle = t_end ? t0 - t_end : 0;

    /* cq_depth 0: no node-wide bound. */
    uint32_t cap = ka_boot.cq_depth ? (uint32_t)ka_boot.cq_depth : 0xffffffffu;
    pk.base = r->pkg;
    pk.cached = cacheable(r->pkg);
    pk.local = 0;
    if (pk.cached) {
        ka_dcache(KA_DCACHE_INVAL);
    }
    ka_engine_reset(&eng, cap, r->timeout ? r->timeout : ka_boot.timeout);
    mv.s0 = ka_ctrl_rd(KA_R_MVSTAT);
    mv.gos = 0;
    mv.fresh = 0;
    mv_credit = 0;
    pk.foreign = 0;
    for (unsigned i = 0; i < KA_PKG_REGS; ++i) {
        reg[i] = 0;
    }
    uint32_t s = 0, at = 0;
    uint64_t t_head = t0, t_copy = t0, t_bind = t0, t_steps = t0;
#ifdef KA_DEBUG
    uint64_t fw0 = 0; /* the step word of `at` */
    int de_was = 0;   /* the run was on the dispatch engine */
#endif
    if (!header(r)) {
        t_head = ka_cycles();
        localise();
        t_copy = ka_cycles();
        if (ka_boot.cq_depth) {
            eng.cap = pk.ackres < cap ? cap - pk.ackres : 1;
        }
        for (uint32_t u = 0; u < pk.nunit && !eng.status; ++u) {
            uint64_t off = pk.ounit + (uint64_t)u * KA_PKG_UNIT_BYTES;
            uint64_t cw = rd(off + 8);
            uint32_t fetch = (ka_boot.flags & KA_BOOT_F_NOFETCH) ? 0 : (uint32_t)(cw >> 32);
            int i = ka_engine_add(&eng, rd(off), (uint32_t)cw, fetch);
            if (i < 0) {
                ka_engine_fail(&eng, KA_ST_TOO_LARGE, u, 0);
            }
            else if (eng.u[i].mesh != (uint8_t)ka_boot.mesh) {
                pk.foreign |= 1u << i;
            }
        }
        if (!eng.status) {
            de_begin();
        }
#ifdef KA_DEBUG
        de_was = de.on;
#endif
        t_bind = ka_cycles();
        int rr = !(ka_boot.flags & KA_BOOT_F_SERIAL);
        int end = 0;
#ifdef KA_DEBUG
        int trace = ka_dbg_traced();
#else
        int trace = (ka_boot.flags & KA_BOOT_F_STEPS) != 0;
#endif
        uint64_t skew = 0;
        while (s < pk.nstep && !eng.status && !end) {
            at = s;
            uint64_t w0 = step_word(s, 0);
#ifdef KA_DEBUG
            fw0 = w0;
#endif
            pc_next = s + 1;
            uint32_t took = rr && !de.on && is_send(w0) ? dispatch_group(s, w0) : 0;
            if (took) {
                pc_next = s + took;
            }
            else if (de.on) {
                de_step(r, s, w0, &end);
            }
            else {
                step(r, s, w0, &end);
            }
            if (trace) {
                /* Steps at..pc_next-1 ended at this cycle, every earlier print removed. */
                uint64_t t = ka_cycles();
#ifdef KA_DEBUG
                ka_dbg_step(w0, at, took ? pc_next - 1 : at, t - t_bind - skew, de.on, &eng);
#else
                ka_printf("PKGS %u %u %lu\n", at, took ? pc_next - 1 : at, t - t_bind - skew);
#endif
                skew += ka_cycles() - t;
            }
            s = pc_next;
        }
        if (de.on && !eng.status) {
            de_barrier();
        }
        /* Only while the engine drains on its own: on the firmware path the
         * reading would delay this package's own completions. */
        if (r->peek && de.on && !eng.status && !pk.local) {
            entry_at = r->peek(stage.next);
        }
        if (de.on && !eng.status) {
            de_drain();
        }
        de_end();
        t_steps = ka_cycles();
        if (!eng.status && !ka_engine_barrier(&eng)) {
            moves_all_done();
        }
    }
    if (eng.status) {
        ka_engine_report(&eng);
#ifdef KA_DEBUG
        ka_dbg_units(&eng);
        ka_engine_quiesce(&eng);
        s = at;
        ka_dbg_fail(fw0, s, de_was, &eng);
#else
        /* Every unit's counters at the failure: which one stopped, and where. */
        for (unsigned i = 0; i < eng.n; ++i) {
            const struct ka_unit_state *u = &eng.u[i];
            ka_printf("PKGF u%u (%u,%u) sent %u recv %u want %u inflight %u\n", i, u->x, u->y,
                      u->sent, u->received, u->expected, u->inflight);
        }
        ka_printf("PKGF stray %u outstanding %u\n", eng.stray, eng.outstanding);
        ka_engine_quiesce(&eng);
        s = at;
#endif
    }
    out->status = eng.status;
    out->detail = eng.detail;
    out->value = eng.value;
    out->step = s;
    out->sent = eng.sent;
    out->cycles = ka_cycles() - t0;
    uint64_t *t = out->time;
    t[KA_PT_HEAD] = t_head - t0;
    t[KA_PT_COPY] = t_copy - t_head;
    t[KA_PT_BIND] = t_bind - t_copy;
    t[KA_PT_STEPS] = t_steps - t_bind;
    t[KA_PT_BARRIER] = t0 + out->cycles - t_steps;
    t[KA_PT_LOCAL] = (uint64_t)pk.local;
    t[KA_PT_TOTAL] = pk.total;
    t[KA_PT_FIRST] = eng.sent ? eng.t_first - t_bind : 0;
    t[KA_PT_LAST] = eng.sent ? eng.t_last - t_bind : 0;
    t[KA_PT_SENT] = eng.sent;
    t[KA_PT_IDLE] = idle;
    if (ka_boot.flags & KA_BOOT_F_TIMING) {
        ka_printf("PKGT head %lu copy %lu bind %lu steps %lu barrier %lu local %lu total %lu "
                  "first %lu last %lu sent %lu idle %lu\n",
                  t[0], t[1], t[2], t[3], t[4], t[5], t[6], t[7], t[8], t[9], t[10]);
    }
    t_end = t0 + out->cycles;
    return eng.status;
}
