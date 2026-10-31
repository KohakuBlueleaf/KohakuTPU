/* The debug image's records (ka/package/debug.h); empty without KA_DEBUG. */
#include <ka/package/debug.h>

#ifdef KA_DEBUG
#include <ka/boot/args.h>
#include <ka/dispatch/de.h>
#include <ka/dispatch/engine.h>
#include <ka/hal/mem.h>
#include <ka/hal/node.h>
#include <ka/package/interp.h>

static struct {
    uint64_t base, rec, cap, wr, lost, tag;
    unsigned flags;
    unsigned issued; /* the engine's issue count at the run's first failure */
    uint16_t count[KA_MAX_PKG_UNITS];
} D;

void ka_dbg_boot(void)
{
    uint64_t size = 0;
    D.base = ka_boot_region(KA_REGION_DEBUG, &size);
    uint64_t rec = (KA_DBG_LASTS + KA_MAX_PKG_UNITS * (1 + KA_DEBUG_LAST) * 8 + 63) & ~63UL;
    if (!D.base || size < rec + KA_DBG_REC_BYTES) {
        D.base = 0;
        return;
    }
    D.rec = rec;
    D.cap = (size - rec) & ~(uint64_t)(KA_DBG_REC_BYTES - 1);
    if (ka_ld64(D.base + KA_DBG_H_MAGIC) == KA_DBG_MAGIC) {
        D.wr = ka_ld64(D.base + KA_DBG_H_WR);
        D.lost = ka_ld64(D.base + KA_DBG_H_LOST);
    }
    ka_st64(D.base + KA_DBG_H_VERSION, KA_DBG_VERSION);
    ka_st64(D.base + KA_DBG_H_LAST, KA_DEBUG_LAST);
    ka_st64(D.base + KA_DBG_H_UNITS, KA_MAX_PKG_UNITS);
    ka_st64(D.base + KA_DBG_H_REC, D.rec);
    ka_st64(D.base + KA_DBG_H_RECCAP, D.cap);
    ka_st64(D.base + KA_DBG_H_WR, D.wr);
    ka_st64(D.base + KA_DBG_H_LOST, D.lost);
    ka_st64(D.base + KA_DBG_H_MAGIC, KA_DBG_MAGIC);
}

void ka_dbg_begin(uint64_t tag, unsigned run_flags)
{
    D.tag = tag;
    D.flags = run_flags;
    D.issued = 0;
    for (unsigned u = 0; u < KA_MAX_PKG_UNITS; ++u) {
        D.count[u] = 0;
        if (D.base) {
            ka_st64(D.base + KA_DBG_LASTS + u * (1 + KA_DEBUG_LAST) * 8, 0);
        }
    }
}

int ka_dbg_traced(void)
{
    return D.base && ((ka_boot.flags & KA_BOOT_F_STEPS) || (D.flags & KA_RUN_F_TRACE));
}

int ka_dbg_noengine(void) { return (D.flags & KA_RUN_F_NOENGINE) != 0; }

/* One record; dropped, and counted, when the host has not consumed the ring. */
static void put(uint64_t w0, uint64_t w2, uint64_t w3, uint64_t w4, uint64_t w5, uint64_t w6,
                uint64_t w7)
{
    if (!D.base) {
        return;
    }
    if (D.wr - ka_ld64(D.base + KA_DBG_H_RD) >= D.cap) {
        D.lost += KA_DBG_REC_BYTES;
        ka_st64(D.base + KA_DBG_H_LOST, D.lost);
        return;
    }
    uint64_t a = D.base + D.rec + D.wr % D.cap;
    ka_st64(a, w0);
    ka_st64(a + 8, D.tag);
    ka_st64(a + 16, w2);
    ka_st64(a + 24, w3);
    ka_st64(a + 32, w4);
    ka_st64(a + 40, w5);
    ka_st64(a + 48, w6);
    ka_st64(a + 56, w7);
    D.wr += KA_DBG_REC_BYTES;
    ka_st64(D.base + KA_DBG_H_WR, D.wr);
}

/* Record word 0 of step word `w0`: op [7:0], unit [31:16]. */
static uint64_t head(unsigned type, unsigned flags, uint64_t w0)
{
    return type | (uint64_t)flags << 8 | (w0 & 0xff) << 16 | ((w0 >> 16) & 0xffff) << 32;
}

static uint64_t unit_word(const struct ka_unit_state *s)
{
    return (uint64_t)s->type << 32 | (uint64_t)s->mesh << 16 | (uint64_t)s->y << 8 | s->x;
}

void ka_dbg_step(uint64_t w0, uint32_t first, uint32_t last, uint64_t cycle, int engine,
                 const struct ka_engine *e)
{
    unsigned u = (unsigned)((w0 >> 16) & 0xffff);
    uint64_t c0 = 0, c1 = 0;
    if (u < e->n) {
        const struct ka_unit_state *s = &e->u[u];
        c0 = s->sent | (uint64_t)s->received << 32;
        c1 = s->expected | (uint64_t)s->inflight << 32;
    }
    put(head(KA_DBG_STEP, engine ? KA_DBG_STEP_ENGINE : 0, w0),
        first | (uint64_t)last << 32, cycle, c0, c1, 0, 0);
}

void ka_dbg_completion(unsigned u, uint64_t w)
{
    if (!D.base || u >= KA_MAX_PKG_UNITS) {
        return;
    }
    uint64_t at = D.base + KA_DBG_LASTS + u * (1 + KA_DEBUG_LAST) * 8;
    unsigned n = D.count[u]++;
    ka_st64(at + 8 + (n % KA_DEBUG_LAST) * 8, w);
    ka_st64(at, D.count[u]);
}

void ka_dbg_failed(void) { D.issued = KA_DE_STAT_ISSUED(ka_ctrl_rd(KA_R_DE_STAT)); }

void ka_dbg_units(const struct ka_engine *e)
{
    for (unsigned i = 0; i < e->n; ++i) {
        const struct ka_unit_state *s = &e->u[i];
        put(KA_DBG_UNIT | (uint64_t)i << 32, unit_word(s), s->sent | (uint64_t)s->received << 32,
            s->expected | (uint64_t)s->inflight << 32, s->acks | (uint64_t)s->credit << 32,
            D.count[i], 0);
    }
}

void ka_dbg_fail(uint64_t w0, uint32_t step, int engine, const struct ka_engine *e)
{
    unsigned flags = (engine ? KA_DBG_FAIL_ENGINE : 0) | (e->outstanding ? 0 : KA_DBG_FAIL_DRAINED);
    put(head(KA_DBG_FAIL, flags, w0),
        (uint64_t)(e->status & 0xffff) | (uint64_t)(e->detail & 0xffff) << 16 |
            (uint64_t)step << 32,
        e->value, ka_ctrl_rd(KA_R_MVSTAT), e->stray | (uint64_t)e->outstanding << 32, e->n,
        engine ? D.issued : 0);
}
#endif
