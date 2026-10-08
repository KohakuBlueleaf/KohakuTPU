/* The package interpreter (docs/spec/package-format.md): header, bindings,
 * relocation and steps, one package at a time. */
#include <ka/boot/args.h>
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
    int cached; /* read through the L1 */
} pk;

static uint64_t rd(uint64_t off)
{
    if (pk.cached) {
        return *(volatile uint64_t *)(uintptr_t)(pk.base + off);
    }
    return ka_ld64(pk.base + off);
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

uint64_t ka_machine_signature(void)
{
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
 * so a forward walk needs one cursor; a backward step restarts it. */
static int relocate(uint32_t idx, uint64_t w[4])
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

static int payload(uint32_t idx, uint64_t w[4])
{
    if (idx >= pk.npay) {
        return ka_engine_fail(&eng, KA_ST_BAD_LAYOUT, idx, pk.npay);
    }
    uint64_t off = pk.opay + (uint64_t)idx * KA_PKG_PAY_BYTES;
    for (int k = 0; k < 4; ++k) {
        w[k] = rd(off + 8 * k);
    }
    return relocate(idx, w);
}

/* Mover register writes, two (register, value) pairs per payload, then a wait
 * for the moves they start. */
static int mover(uint32_t first, uint32_t count)
{
    uint64_t s0 = ka_ctrl_rd(KA_R_MVSTAT);
    uint32_t gos = 0;
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
            ka_ctrl_wr(KA_R_MV + (unsigned)w[p], w[p + 1]);
            gos += (w[p] == 0 && (w[p + 1] & (1UL << 16))) ? 1 : 0;
        }
    }
    uint64_t t0 = ka_cycles(), s;
    for (;;) {
        s = ka_ctrl_rd(KA_R_MVSTAT);
        if (KA_MV_FAULT(s)) {
            return ka_engine_fail(&eng, KA_ST_MOVER_FAULT, KA_MV_FAULT(s), s);
        }
        if (!KA_MV_BUSY(s) && ((KA_MV_DONE(s) - KA_MV_DONE(s0)) & 0x0fffffff) >= gos) {
            return 0;
        }
        if (ka_cycles() - t0 > eng.timeout) {
            return ka_engine_fail(&eng, KA_ST_TIMEOUT, KA_WAIT_MOVER, s);
        }
        ka_yield();
    }
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

static int step(const struct ka_pkg_run *r, uint32_t s, int *end)
{
    uint64_t w0 = rd(pk.ostep + (uint64_t)s * KA_PKG_STEP_BYTES);
    uint64_t arg = rd(pk.ostep + (uint64_t)s * KA_PKG_STEP_BYTES + 8);
    unsigned op = w0 & 0xff, unit = (w0 >> 16) & 0xffff;
    uint32_t count = (uint32_t)(w0 >> 32);
    switch (op) {
    case KA_OP_END:
        *end = 1;
        return 0;
    case KA_OP_DISPATCH:
        for (uint32_t k = 0; k < count; ++k) {
            uint64_t w[4];
            if (payload((uint32_t)arg + k, w) || ka_engine_send(&eng, unit, w)) {
                return eng.status;
            }
        }
        return ka_engine_drain(&eng);
    case KA_OP_AWAIT:
        return ka_engine_await(&eng, unit, count);
    case KA_OP_BARRIER:
        return ka_engine_barrier(&eng);
    case KA_OP_MOVER:
        return mover((uint32_t)arg, count);
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

int ka_package_run(const struct ka_pkg_run *r, struct ka_pkg_result *out)
{
    uint64_t t0 = ka_cycles();

    /* cq_depth 0: no node-wide bound. */
    uint32_t cap = ka_boot.cq_depth ? (uint32_t)ka_boot.cq_depth : 0xffffffffu;
    pk.base = r->pkg;
    pk.rcur = pk.rlast = 0;
    pk.rpeek_ok = 0;
    pk.cached = cacheable(r->pkg);
    if (pk.cached) {
        ka_dcache(KA_DCACHE_INVAL);
    }
    ka_engine_reset(&eng, cap, r->timeout ? r->timeout : ka_boot.timeout);
    uint32_t s = 0;
    if (!header(r)) {
        if (ka_boot.cq_depth) {
            eng.cap = pk.ackres < cap ? cap - pk.ackres : 1;
        }
        for (uint32_t u = 0; u < pk.nunit && !eng.status; ++u) {
            uint64_t off = pk.ounit + (uint64_t)u * KA_PKG_UNIT_BYTES;
            if (ka_engine_add(&eng, rd(off), (uint32_t)rd(off + 8)) < 0) {
                ka_engine_fail(&eng, KA_ST_TOO_LARGE, u, 0);
            }
        }
        if (!eng.status) {
            bindings(r);
        }
        int end = 0;
        for (; s < pk.nstep && !eng.status && !end; ++s) {
            step(r, s, &end);
        }
        if (!eng.status) {
            ka_engine_barrier(&eng);
        }
    }
    if (eng.status) {
        ka_engine_quiesce(&eng);
        s = s ? s - 1 : 0;
    }
    out->status = eng.status;
    out->detail = eng.detail;
    out->value = eng.value;
    out->step = s;
    out->sent = eng.sent;
    out->cycles = ka_cycles() - t0;
    return eng.status;
}
