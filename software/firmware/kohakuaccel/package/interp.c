/* The package interpreter (docs/spec/package-format.md): header, bindings,
 * relocation and steps, one package at a time. */
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
#include <ka/package/interp.h>
#include <ka/package/status.h>

static struct ka_engine eng;
static uint64_t bind[KA_MAX_BUFFERS];

/* The package being run, read once from its header. */
static struct {
    uint64_t base, total;
    uint32_t npay, nrel, nbuf, nstep, nunit, ackres;
    uint64_t ounit, obuf, ostep, orel, opay;
    /* relocation cursor: the next entry to look at, and its first word */
    uint32_t rcur, rlast;
    uint64_t rpeek;
    int rpeek_ok;
    int cached;   /* read through the L1 */
    int local;    /* copied whole into `pk_copy` */
    int prereloc; /* every relocation already applied to `pk_copy` */
} pk;

/* A package read during a FILL waits out the stream: the read is served by the
 * MAG engine in order behind it (MBOX_TRACE, v9: each GO ~180 cycles after the
 * target's FILL ended). A cached package that fits is copied here up front, a
 * line fill per 32 bytes; an uncached one stays where it is, since four 8-byte
 * round trips per word up front cost a small package more than it saves. */
#define KA_PKG_COPY_WORDS 1408
static uint64_t pk_copy[KA_PKG_COPY_WORDS];

static uint64_t rd(uint64_t off)
{
    if (pk.local) {
        return pk_copy[off >> 3];
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
    if (!pk.cached || pk.total > sizeof pk_copy || any_fetch()) {
        return;
    }
    for (uint64_t off = 0; off < pk.total; off += 8) {
        pk_copy[off >> 3] = rd(off);
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

/* Apply every relocation naming payload `idx`. Entries are sorted by payload,
 * so a forward walk needs one cursor; a backward step restarts it. Out of line:
 * inlined, its live values spilled 11 registers on every payload. */
static __attribute__((noinline)) int relocate(uint32_t idx, uint64_t w[4])
{
    if (idx < pk.rlast) {
        pk.rcur = 0;
        pk.rpeek_ok = 0;
    }
    pk.rlast = idx;
    while (pk.rcur < pk.nrel) {
        uint64_t off = pk.orel + (uint64_t)pk.rcur * KA_PKG_REL_BYTES;
        if (!pk.rpeek_ok) {
            pk.rpeek = rd(off);
            pk.rpeek_ok = 1;
        }
        uint64_t e0 = pk.rpeek;
        uint32_t at = (uint32_t)e0;
        if (at > idx) {
            break;
        }
        ++pk.rcur;
        pk.rpeek_ok = 0;
        if (at < idx) {
            continue;
        }
        unsigned bit = (e0 >> 32) & 0xff, width = (e0 >> 40) & 0xff;
        unsigned shift = (e0 >> 48) & 0xff, buf = (unsigned)(e0 >> 56);
        if (buf >= pk.nbuf || !width || width > 64 || bit + width > 256 || shift > 63) {
            return ka_engine_fail(&eng, KA_ST_BAD_RELOC, pk.rcur - 1, e0);
        }
        insert(w, bit, width, (bind[buf] + rd(off + 8)) >> shift);
    }
    return 0;
}

/* Whether payload `idx` may carry a relocation: the cursor's peeked entry
 * names it or one before it, or the walk has to restart. */
static inline int may_relocate(uint32_t idx)
{
    return pk.rcur < pk.nrel && (idx < pk.rlast || !pk.rpeek_ok || (uint32_t)pk.rpeek <= idx);
}

static int payload(uint32_t idx, uint64_t w[4])
{
    if (idx >= pk.npay) {
        return ka_engine_fail(&eng, KA_ST_BAD_LAYOUT, idx, pk.npay);
    }
    uint64_t off = pk.opay + (uint64_t)idx * KA_PKG_PAY_BYTES;
    for (int k = 0; k < 4; ++k) {
        w[k] = rd(off + 8 * k);
    }
    return pk.prereloc ? 0 : relocate(idx, w);
}

/* Every relocation applied to the local copy in one forward pass, so a send
 * reads final words and the cursor never restarts on an out-of-order send. */
static int prerelocate(void)
{
    for (uint32_t i = 0; i < pk.nrel; ++i) {
        uint64_t off = pk.orel + (uint64_t)i * KA_PKG_REL_BYTES;
        uint64_t e0 = rd(off);
        uint32_t at = (uint32_t)e0;
        unsigned bit = (e0 >> 32) & 0xff, width = (e0 >> 40) & 0xff;
        unsigned shift = (e0 >> 48) & 0xff, buf = (unsigned)(e0 >> 56);
        if (at >= pk.npay || buf >= pk.nbuf || !width || width > 64 || bit + width > 256 ||
            shift > 63) {
            return ka_engine_fail(&eng, KA_ST_BAD_RELOC, i, e0);
        }
        uint64_t *w = &pk_copy[(pk.opay + (uint64_t)at * KA_PKG_PAY_BYTES) >> 3];
        insert(w, bit, width, (bind[buf] + rd(off + 8)) >> shift);
    }
    pk.prereloc = 1;
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

/* One DISPATCH step: `count` payloads from `first`, each relocated and sent.
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
    if (pk.local) {
        at = (uintptr_t)&pk_copy[(pk.opay + (uint64_t)first * KA_PKG_PAY_BYTES) >> 3];
    }
    else if (!pk.cached) {
        at |= KA_UNCACHED;
    }
    for (uint32_t k = 0; k < count; ++k, at += KA_PKG_PAY_BYTES) {
        const volatile uint64_t *p = (const volatile uint64_t *)(uintptr_t)at;
        uint64_t w[4] = {p[0], p[1], p[2], p[3]};
        if (!pk.prereloc && may_relocate(first + k) && relocate(first + k, w)) {
            return eng.status;
        }
        pk.rlast = first + k;
        if (ka_engine_send_now(&eng, unit, w) && ka_engine_send(&eng, unit, w)) {
            return eng.status;
        }
    }
    return 0;
}

/* The first relocation entry naming payload `idx` or a later one. */
static uint32_t rel_lower(uint32_t idx)
{
    uint32_t lo = 0, hi = pk.nrel;
    while (lo < hi) {
        uint32_t mid = lo + (hi - lo) / 2;
        if ((uint32_t)rd(pk.orel + (uint64_t)mid * KA_PKG_REL_BYTES) < idx) {
            lo = mid + 1;
        }
        else {
            hi = mid;
        }
    }
    return lo;
}

/* Apply the entries from `*rc` that name payload `idx`, advancing `*rc`. */
static int rel_apply(uint32_t *rc, uint32_t idx, uint64_t w[4])
{
    for (; *rc < pk.nrel; ++*rc) {
        uint64_t off = pk.orel + (uint64_t)*rc * KA_PKG_REL_BYTES;
        uint64_t e0 = rd(off);
        if ((uint32_t)e0 != idx) {
            return 0;
        }
        unsigned bit = (e0 >> 32) & 0xff, width = (e0 >> 40) & 0xff;
        unsigned shift = (e0 >> 48) & 0xff, buf = (unsigned)(e0 >> 56);
        if (buf >= pk.nbuf || !width || width > 64 || bit + width > 256 || shift > 63) {
            return ka_engine_fail(&eng, KA_ST_BAD_RELOC, *rc, e0);
        }
        insert(w, bit, width, (bind[buf] + rd(off + 8)) >> shift);
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
 * repetition `r`, relocated, with the repetition's increments added. */
struct src {
    unsigned unit;
    uint32_t first, count, repeats, ninc;
    uint32_t j, r, rc, ic;
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
    if (pk.local) {
        q->base = (uintptr_t)&pk_copy[(pk.opay + (uint64_t)q->first * KA_PKG_PAY_BYTES) >> 3];
    }
    else if (!pk.cached) {
        q->base |= KA_UNCACHED;
    }
    q->rc = pk.prereloc ? 0 : rel_lower(q->first);
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

/* The next word into `w` from the relocated local copy: no relocation pass,
 * and each increment is one limb add with its carry (a REPEAT never wraps a
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
 * `w`. NULL on a bad relocation. */
static const uint64_t *src_next(struct src *q, uint64_t w[4])
{
    if (pk.local && pk.prereloc) {
        return src_next_local(q, w);
    }
    const volatile uint64_t *p =
        (const volatile uint64_t *)(uintptr_t)(q->base + (uint64_t)q->j * KA_PKG_PAY_BYTES);
    w[0] = p[0];
    w[1] = p[1];
    w[2] = p[2];
    w[3] = p[3];
    if (!pk.prereloc && rel_apply(&q->rc, q->first + q->j, w)) {
        return NULL;
    }
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
        q->rc = pk.prereloc ? 0 : rel_lower(q->first);
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
        if (unit < eng.n && (eng.u[unit].fetch >> 16) && !pk.nrel) {
            break; /* its words are fetched, not sent: `dispatch` */
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
    int fast = pk.local && pk.prereloc;
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

/* Until `gos` moves since `s0` are done and the mover is idle. The bound is on
 * PROGRESS, not on the step: many moves may take far longer than one wait's
 * timeout, and only a mover that stops finishing moves is hung. */
static int moves_done(uint64_t s0, uint32_t gos)
{
    uint64_t t0 = ka_cycles(), s;
    uint32_t seen = KA_MV_DONE(s0);
    for (;;) {
        s = ka_ctrl_rd(KA_R_MVSTAT);
        if (KA_MV_FAULT(s)) {
            return ka_engine_fail(&eng, KA_ST_MOVER_FAULT, KA_MV_FAULT(s), s);
        }
        if (!KA_MV_BUSY(s) && ((KA_MV_DONE(s) - KA_MV_DONE(s0)) & 0x0fffffff) >= gos) {
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
    return moves_done(mv.s0, mv.gos);
}

/* Until `n` of the package's moves are done; the mover may be busy with a
 * later one. */
static int moves_upto(uint32_t n)
{
    uint64_t t0 = ka_cycles(), s;
    uint32_t seen = KA_MV_DONE(mv.s0);
    for (;;) {
        s = ka_ctrl_rd(KA_R_MVSTAT);
        if (KA_MV_FAULT(s)) {
            return ka_engine_fail(&eng, KA_ST_MOVER_FAULT, KA_MV_FAULT(s), s);
        }
        if (((KA_MV_DONE(s) - KA_MV_DONE(mv.s0)) & 0x0fffffff) >= n) {
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

/* Writes the mover's configuration queue takes before ROOM is read again. */
static uint32_t mv_credit;

/* Until the configuration queue has room; bounded on progress like
 * moves_done, since the queue drains only as moves finish. */
static int mover_room(void)
{
    uint64_t t0 = ka_cycles(), s;
    uint32_t seen = KA_MV_DONE(mv.s0);
    while (!mv_credit) {
        s = ka_ctrl_rd(KA_R_MVSTAT);
        if (KA_MV_FAULT(s)) {
            return ka_engine_fail(&eng, KA_ST_MOVER_FAULT, KA_MV_FAULT(s), s);
        }
        if (KA_MV_ROOM(s)) {
            mv_credit = KA_MV_ROOM_WRITES;
            break;
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

static int mover_idle(void)
{
    uint64_t t0 = ka_cycles();
    while (KA_MV_BUSY(ka_ctrl_rd(KA_R_MVSTAT))) {
        if (ka_cycles() - t0 > eng.timeout) {
            return ka_engine_fail(&eng, KA_ST_TIMEOUT, KA_WAIT_MOVER, 0);
        }
        ka_yield();
    }
    return 0;
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

/* Faults and unclaimed signals, and no progress for a timeout. */
static int de_watch(uint64_t stat)
{
    if (KA_NM_STAT_COUNT(ka_nm_rd(KA_NM_STAT)) && ka_engine_drain(&eng)) {
        return eng.status;
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
        return ka_engine_fail(&eng, KA_ST_TIMEOUT, KA_WAIT_AWAIT, stat);
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

static int de_send_word(unsigned unit, const uint64_t w[4])
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

static int de_fetch(unsigned unit, uint64_t at, uint32_t count)
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
static int de_stream(uint32_t first, uint32_t count)
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

static int header(const struct ka_pkg_run *r)
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
    pk.npay = (uint32_t)c1;
    pk.nrel = (uint32_t)(c1 >> 32);
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

static int bindings(const struct ka_pkg_run *r)
{
    for (uint32_t b = 0; b < pk.nbuf; ++b) {
        uint64_t v = (r->bind && b < r->nbind) ? ka_ld64(r->bind + 8 * b) : 0;
        bind[b] = v ? v : rd(pk.obuf + (uint64_t)b * KA_PKG_BUF_BYTES + 16);
    }
    return 0;
}

static int step(const struct ka_pkg_run *r, uint32_t s, uint64_t w0, int *end)
{
    uint64_t arg = rd(pk.ostep + (uint64_t)s * KA_PKG_STEP_BYTES + 8);
    unsigned op = w0 & 0xff, unit = (w0 >> 16) & 0xffff;
    uint32_t count = (uint32_t)(w0 >> 32);
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
        return ka_engine_await(&eng, unit, count);
    case KA_OP_BARRIER:
        return ka_engine_barrier(&eng) ? eng.status : moves_all_done();
    case KA_OP_MOVER:
        return mover((uint32_t)arg, count, (w0 >> 8) & KA_STEP_F_POSTED);
    case KA_OP_MWAIT:
        return moves_upto(count);
    case KA_OP_FETCH:
        if (unit >= eng.n) {
            return ka_engine_fail(&eng, KA_ST_BAD_UNIT, unit, 0);
        }
        if (fetch(unit, arg, count)) {
            return eng.status;
        }
        return ka_engine_drain(&eng);
    case KA_OP_RING:
        if (mover_idle()) {
            return eng.status;
        }
        ka_ctrl_wr(KA_R_IL + 0x10, ((uint64_t)(count & 0xff) << 8) | (unit & 3));
        return 0;
    case KA_OP_WAIT_BELL:
        return wait_bell(unit, count);
    case KA_OP_SIGNAL:
        if (r->signal) {
            r->signal(r->ctx, s, count, arg);
        }
        return 0;
    case KA_OP_SETTLE:
        ka_delay(count);
        return 0;
    default:
        return ka_engine_fail(&eng, KA_ST_BAD_STEP, op, w0);
    }
}

/* One step in engine mode: queued, except the ones only the firmware can do,
 * which wait for the queue to drain and then run as `step`. */
static int de_step(const struct ka_pkg_run *r, uint32_t s, uint64_t w0, int *end)
{
    uint64_t arg = rd(pk.ostep + (uint64_t)s * KA_PKG_STEP_BYTES + 8);
    unsigned op = w0 & 0xff, unit = (w0 >> 16) & 0xffff;
    uint32_t count = (uint32_t)(w0 >> 32);
    if ((op == KA_OP_DISPATCH || op == KA_OP_AWAIT || op == KA_OP_FETCH) && unit >= eng.n) {
        return ka_engine_fail(&eng, KA_ST_BAD_UNIT, unit, 0);
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
        de.exp[unit] += count;
        return de_wait(unit, de.exp[unit]);
    case KA_OP_BARRIER:
        return de_barrier();
    case KA_OP_MOVER:
        return de_mover((uint32_t)arg, count, (w0 >> 8) & KA_STEP_F_POSTED);
    case KA_OP_MWAIT:
        return de_wait(KA_DE_MOVER, count);
    case KA_OP_RING:
        return de_push(KA_DE_IL(0x10), ((uint64_t)(count & 0xff) << 8) | (unit & 3));
    case KA_OP_ENGINE:
        return de_stream((uint32_t)arg, count);
    case KA_OP_FETCH:
        return de_fetch(unit, arg, count);
    default:
        return de_drain() ? eng.status : step(r, s, w0, end);
    }
}

int ka_package_run(const struct ka_pkg_run *r, struct ka_pkg_result *out)
{
    uint64_t t0 = ka_cycles();

    /* cq_depth 0: no node-wide bound. */
    uint32_t cap = ka_boot.cq_depth ? (uint32_t)ka_boot.cq_depth : 0xffffffffu;
    pk.base = r->pkg;
    pk.rcur = pk.rlast = 0;
    pk.rpeek_ok = 0;
    pk.cached = cacheable(r->pkg);
    pk.local = 0;
    pk.prereloc = 0;
    if (pk.cached) {
        ka_dcache(KA_DCACHE_INVAL);
    }
    ka_engine_reset(&eng, cap, r->timeout ? r->timeout : ka_boot.timeout);
    mv.s0 = ka_ctrl_rd(KA_R_MVSTAT);
    mv.gos = 0;
    mv.fresh = 0;
    mv_credit = 0;
    uint32_t s = 0;
    uint64_t t_head = 0, t_copy = 0, t_bind = 0, t_steps = 0;
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
            if (ka_engine_add(&eng, rd(off), (uint32_t)cw, fetch) < 0) {
                ka_engine_fail(&eng, KA_ST_TOO_LARGE, u, 0);
            }
        }
        if (!eng.status) {
            bindings(r);
        }
        if (!eng.status && pk.local) {
            prerelocate();
        }
        if (!eng.status) {
            de_begin();
        }
        t_bind = ka_cycles();
        int rr = !(ka_boot.flags & KA_BOOT_F_SERIAL);
        int end = 0;
        int trace = (ka_boot.flags & KA_BOOT_F_STEPS) != 0;
        uint64_t skew = 0;
        for (; s < pk.nstep && !eng.status && !end; ++s) {
            uint32_t first = s;
            uint64_t w0 = step_word(s, 0);
            uint32_t took = rr && !de.on && is_send(w0) ? dispatch_group(s, w0) : 0;
            if (took) {
                s += took - 1;
            }
            else if (de.on) {
                de_step(r, s, w0, &end);
            }
            else {
                step(r, s, w0, &end);
            }
            if (trace) {
                /* Steps first..s ended at this cycle, every earlier print removed. */
                uint64_t t = ka_cycles();
                ka_printf("PKGS %u %u %lu\n", first, s, t - t_bind - skew);
                skew += ka_cycles() - t;
            }
        }
        if (de.on && !eng.status && !de_barrier()) {
            de_drain();
        }
        de_end();
        t_steps = ka_cycles();
        if (!eng.status && !ka_engine_barrier(&eng)) {
            moves_all_done();
        }
    }
    if (eng.status) {
        /* Every unit's counters at the failure: which one stopped, and where. */
        for (unsigned i = 0; i < eng.n; ++i) {
            const struct ka_unit_state *u = &eng.u[i];
            ka_printf("PKGF u%u (%u,%u) sent %u recv %u want %u inflight %u\n", i, u->x, u->y,
                      u->sent, u->received, u->expected, u->inflight);
        }
        ka_printf("PKGF stray %u outstanding %u\n", eng.stray, eng.outstanding);
        ka_engine_quiesce(&eng);
        s = s ? s - 1 : 0;
    }
    out->status = eng.status;
    out->detail = eng.detail;
    out->value = eng.value;
    out->step = s;
    out->sent = eng.sent;
    out->cycles = ka_cycles() - t0;
    if (ka_boot.flags & KA_BOOT_F_TIMING) {
        ka_printf("PKGT head %lu copy %lu bind %lu steps %lu barrier %lu local %d total %lu "
                  "first %lu last %lu sent %u\n",
                  t_head - t0, t_copy - t_head, t_bind - t_copy, t_steps - t_bind,
                  t0 + out->cycles - t_steps, pk.local, pk.total,
                  eng.sent ? eng.t_first - t_bind : 0, eng.sent ? eng.t_last - t_bind : 0,
                  eng.sent);
    }
    return eng.status;
}
