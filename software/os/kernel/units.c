/* Compute units, the mover and the dispatch engine: generic. A unit is a
 * coordinate, a CU_TYPE and a credit from the boot table; an instruction is
 * four opaque words. Completions are counted per unit from the completion
 * queue (or mirrored from the engine's counters once the engine owns a unit),
 * CU_CTRL replies are taken from the RX queue. Nothing here knows what any
 * unit computes. */
#include "kernel.h"

#define NM(r) (HW_R_NM + 8 * (r))
#define RX(r) (HW_R_RX + 8 * (r))
#define MV(r) (HW_R_MV + (r))

struct kunit kunits[KOS_MAX_UNITS];
unsigned knunits;
uint64_t k_events;

/* per unit: retired = done + faults; engine-owned units mirror a counter */
#ifdef KOS_EVENT_ASM
uint64_t k_retired[KOS_MAX_UNITS];
#define retired k_retired
_Static_assert(sizeof(struct kunit) == 80 && offsetof(struct kunit, faults) == 32,
               "event.S unit layout");
#else
static uint64_t retired[KOS_MAX_UNITS];
#endif
static int de_mapped[KOS_MAX_UNITS];
static uint32_t de_seen[KOS_MAX_UNITS];
static int de_ready;

#ifdef KOS_SMALL_CALL
#define UNIT_SMALL __attribute__((noinline, minsize))
#define UNIT_MINSIZE __attribute__((minsize))
#else
#define UNIT_SMALL
#define UNIT_MINSIZE
#endif

static __attribute__((noinline)) int find(unsigned x, unsigned y)
{
    for (unsigned i = 0; i < knunits; ++i) {
        if (kunits[i].local && kunits[i].u.x == x && kunits[i].u.y == y) {
            return (int)i;
        }
    }
    return -1;
}

static void pending_init(void);

/* The mover's index buffer: IDX_WORDS (256) words of eight 32-bit indices
 * (mm_mover.v:46,330). */
#define MV_INDEX_MAX 2048u

/* The kernel's copy of a GATHER/SCATTER index vector (mv_index), as large as
 * the mover's index buffer: private, written back once at boot so no cached
 * line covers it, then written only through the uncached alias. */
static uint64_t mv_ixbuf;

UNIT_MINSIZE void k_units_init(void)
{
    mv_ixbuf = (uint64_t)(uintptr_t)k_alloc(4 * MV_INDEX_MAX);
    hw_dcache(HW_DC_FLUSH);
    pending_init();
    knunits = k_boot.nunits;
    for (unsigned i = 0; i < knunits; ++i) {
        kunits[i].u = k_boot.unit[i];
        kunits[i].local = kunits[i].u.mesh == kn.mesh;
    }
    hw_wr(NM(HW_NM_DST), 0);
    k_units_poll();
}

void k_units_poll(void)
{
    uint64_t w;
    while (HW_NM_COUNT(hw_rd(NM(HW_NM_STAT)))) {
        w = hw_rd(NM(HW_NM_HEAD));
        hw_wr(NM(HW_NM_HEAD), 0);
        int i = find(HW_CQ_X(w), HW_CQ_Y(w));
        if (i < 0) {
            continue;
        }
        struct kunit *u = &kunits[i];
        switch (HW_CQ_CODE(w)) {
        case HW_SIG_INST:
        case HW_SIG_BATCH:
            ++u->done;
            ++retired[i];
            ++kstat.completions;
            ++k_events;
            break;
        case HW_SIG_FAULT_CODE:
            ++u->faults;
            ++retired[i];
            u->fault_arg = HW_CQ_ARG(w);
            ++kstat.faults;
            ++k_events;
            break;
        case HW_SIG_DATA:
            ++u->acks;
            break;
        default:
            ++u->misc;
            break;
        }
    }
    while ((w = hw_rd(RX(HW_RX_HDR))) >> 63) {
        uint64_t p2 = hw_rd(RX(HW_RX_P2)), p3 = hw_rd(RX(HW_RX_P3));
        hw_wr(RX(HW_RX_HDR), 0);
        unsigned sx = (w >> 20) & 0xf, sy = (w >> 16) & 0xf, ty = (w >> 12) & 0xf;
        int i = find(sx, sy);
        if (ty == HW_T_CU_CTRL && (p3 >> 56) == 2 && i >= 0) {
            kunits[i].ctrl_value = ((p3 & 0xffffffffffffUL) << 16) | (p2 >> 48);
            kunits[i].ctrl_ready = 1;
        }
    }
    if (de_ready) {
        for (unsigned i = 0; i < knunits; ++i) {
            if (!de_mapped[i]) {
                continue;
            }
            uint32_t c = (uint32_t)hw_rd(HW_R_DE_CTR + 8 * i) & 0xfff;
            uint32_t d = (c - de_seen[i]) & 0xfff;
            de_seen[i] = c;
            kunits[i].done += d;
            retired[i] += d;
            kstat.completions += d;
            k_events += d;
        }
    }
}

uint64_t k_unit_retired(unsigned u) { return retired[u]; }

/* Units [0, n): retired and fault counts, two words each, into a user buffer. */
#ifndef KOS_EVENT_ASM
int k_event_out(uint64_t ubuf, uint64_t n)
{
    uint64_t w[2 * KOS_MAX_UNITS];
    n = n < knunits ? n : knunits;
    for (unsigned i = 0; i < n; ++i) {
        w[2 * i] = retired[i];
        w[2 * i + 1] = kunits[i].faults;
    }
    return n ? k_copy_out(ubuf, w, 16 * n) : KOS_OK;
}
#endif

/* One flit to unit u: `dst` adds the typed bits to its coordinate. Once the
 * dispatch engine runs, it owns the mailbox registers, so every flit goes
 * through its queue: six entries, DST, ARG0..3, GO (mailbox codes 0..5). */
static __attribute__((noinline)) void mailbox(const struct kunit *u, uint64_t dst, const uint64_t w[4])
{
    dst |= (uint64_t)u->u.y << 8 | u->u.x;
    if (de_ready) {
        while (((hw_rd(HW_R_DE_WAIT) & 0xffff) + 6) > KOS_DE_QUEUE_DEPTH) {
        }
        hw_wr(HW_R_DE_Q + 8 * 0, dst);
        for (unsigned k = 0; k < 4; ++k) {
            hw_wr(HW_R_DE_Q + 8 * (1 + k), w[k]);
        }
        hw_wr(HW_R_DE_Q + 8 * 5, 1);
        return;
    }
    while (hw_rd(NM(HW_NM_STAT)) & HW_NM_OFFERED) {
    }
    hw_wr(NM(HW_NM_DST), dst);
    hw_wr(NM(HW_NM_ARG0), w[0]);
    hw_wr(NM(HW_NM_ARG1), w[1]);
    hw_wr(NM(HW_NM_ARG2), w[2]);
    hw_wr(NM(HW_NM_ARG3), w[3]);
    hw_wr(NM(HW_NM_GO), 1);
}

/* Hand unit i to the dispatch engine: its completions are counted by engine
 * counter i from now on. 0, or KOS_EINVAL when there is no engine. */
static __attribute__((noinline)) int engine_own(unsigned i)
{
    if (i >= KOS_DE_MAX_UNITS || !kn.engine) {
        return KOS_EINVAL;
    }
    if (!de_ready) {
        if (!HW_DE_MAGIC(hw_rd(HW_R_DE_WAIT))) {
            return KOS_EINVAL;
        }
        hw_wr(HW_R_DE_CTL, 2);
        while (hw_rd(HW_R_DE_WAIT) & (1UL << 34)) {
        }
        hw_wr(HW_R_DE_CTL, 1);
        de_ready = 1;
    }
    if (!de_mapped[i]) {
        k_units_poll();
        hw_wr(HW_R_DE_MAP, (1UL << 31) | ((uint64_t)i << 16) | (uint64_t)kunits[i].u.y << 8 | kunits[i].u.x);
        de_seen[i] = (uint32_t)hw_rd(HW_R_DE_CTR + 8 * i) & 0xfff;
        de_mapped[i] = 1;
    }
    return KOS_OK;
}

__attribute__((noinline)) int64_t k_dispatch_local(unsigned i, const uint64_t w[4])
{
    if (i >= knunits || !kunits[i].local) {
        return KOS_EINVAL;
    }
    if ((kn.flags & KOS_F_ENGINE) && !de_mapped[i]) {
        engine_own(i); /* no engine: the mailbox */
    }
    struct kunit *u = &kunits[i];
    uint64_t credit = u->u.depth ? u->u.depth : 16, f0 = u->faults, t0 = hw_cycles();
    while (u->sent - retired[i] >= credit) {
        uint64_t r0 = retired[i];
#ifdef KOS_LAUNCH_RING
        uint64_t tw = hw_cycles();
#endif
        k_units_poll();
#ifdef KOS_LAUNCH_RING
        k_lring_cur.cyc_wait += hw_cycles() - tw;
#endif
        if (u->faults != f0) {
            return KOS_EFAULT;
        }
        if (retired[i] != r0) {
            t0 = hw_cycles();
        } else if (hw_cycles() - t0 > KOS_DE_TIMEOUT) {
            return KOS_ETIME;
        }
    }
#ifdef KOS_LAUNCH_RING
    uint64_t ts = hw_cycles();
#endif
    mailbox(u, 0, w);
#ifdef KOS_LAUNCH_RING
    k_lring_cur.cyc_submit += hw_cycles() - ts;
#endif
    ++kstat.dispatches;
    return (int64_t)++u->sent;
}

UNIT_MINSIZE int k_unit_ctrl_start(unsigned i, unsigned index)
{
    if (i >= knunits || !kunits[i].local) {
        return KOS_EINVAL;
    }
    struct kunit *u = &kunits[i];
    if (u->ctrl_busy) {
        return KOS_EBUSY;
    }
    u->ctrl_busy = 1;
    u->ctrl_ready = 0;
    uint64_t w[4] = {0, 0, 0, (uint64_t)index << 48};
    mailbox(u, HW_NM_TYPED(HW_T_CU_CTRL), w);
    if (!de_ready) {
        while (hw_rd(NM(HW_NM_STAT)) & HW_NM_OFFERED) {
        }
        hw_wr(NM(HW_NM_DST), 0);
    }
    return KOS_OK;
}

/* Hand unit i to the dispatch engine (once) and queue `count` instructions;
 * the ticket of the last, as k_dispatch_local's. */
int64_t k_engine_run(unsigned i, const uint64_t *w, unsigned count)
{
    if (i >= knunits || !kunits[i].local || engine_own(i)) {
        return KOS_EINVAL;
    }
    int64_t t = 0;
    for (unsigned k = 0; k < count && t >= 0; ++k) {
        t = k_dispatch_local(i, &w[4 * k]);
    }
    return t;
}

/* The mover's address registers as the RTL applies one user's writes
 * (mm_mover.v:793-847, mx_tdesc.v:99-111): HDR, DIM and WINDOW select a walker,
 * DIM stages one level and AXIS loads the staged level into the selected
 * walker. s[8w + l] is level l's span (count - 1) * stride, s[8w + 6] the
 * base, s[8w + 7] the level count, n[8w + l] level l's count; bit i of `ok`
 * says s[i] was written by this stream, bit 7 that a DIM is staged, bits 16
 * and 17 that `ix` (INDEX, 0x30) and `pw` (pitch and row words, 0x50) were. A
 * GO is checked against these values only, never against a register an
 * earlier writer left. */
static struct {
    int64_t s[16], staged;
    uint64_t ix, pw;
    uint32_t n[16], scount;
    unsigned ok, sel, slvl;
} mv;

/* An XFORM writes each entry's XF_OUT_WORDS (4) output words from the
 * destination element (mm_mover.v:63,681). */
#define MV_XFORM_OUT_BYTES 128u

/* GATHER reads, and SCATTER writes, row r at base + (index * pitch mod 2^32),
 * gath_words words from there (mm_mover.v:742-746, 1050-1051), with the index
 * from buffer entry r mod 2048. A move has ceil(elements / gath_words) rows,
 * its elements those of the walker it iterates (the destination for GATHER,
 * the source for SCATTER). The kernel copies the first L = min(rows, 2048)
 * entries, rounded up to whole words of 8, of the vector the INDEX write named
 * (those the mover would load; zero past them, where the mover would read
 * entries an earlier move left) into its own buffer, so every entry a row
 * reads is one it checked, and returns L: the caller points INDEX at that
 * buffer, L entries. A float index (bit 17 of `go`, the CTRL value) converts as
 * the RTL does (mm_mover.v:409-418, idx_c), its fault ignored: the converted
 * value bounds the row either way. */
static UNIT_SMALL int64_t mv_index(uint64_t go)
{
    uint64_t c = (mv.ix >> 40) & 0xffff, src = mv.ix & ((1UL << 40) - 1);
    uint64_t words = (((mv.pw >> 32) - 1) & 0xffff) + 1; /* gath_words 0 reads as 65536 */
    unsigned w = (go & 7) != 6, have = c ? (unsigned)((c + 7) & ~7UL) : 8, top = 0;
    uint64_t e = 1;
    for (unsigned l = 0; l < (unsigned)mv.s[8 * w + 7]; ++l) {
        e *= mv.n[8 * w + l];
        e = e > (1UL << 32) ? 1UL << 32 : e;
    }
    e = (e + words - 1) / words;
    unsigned load = e < MV_INDEX_MAX ? (unsigned)((e + 7) & ~7UL) : MV_INDEX_MAX;
    if (c > MV_INDEX_MAX || !k_pa_ok(src, 4 * (uint64_t)(have < load ? have : load))) {
        return c > MV_INDEX_MAX ? KOS_EINVAL : KOS_EFAULT;
    }
    for (unsigned i = 0; i < load; ++i) {
        uint32_t v = i < have ? *k_unc32(src + 4 * i) : 0, x = v;
        *k_unc32(mv_ixbuf + 4 * i) = v;
        if (go & (1UL << 17)) {
            x = (v & 0x7fffffff) ?
                (uint32_t)(((uint64_t)(0x800000 | (v & 0x7fffff)) << ((((v >> 23) & 0xff) - 127) & 31)) >> 23) : 0;
        }
        x *= (uint32_t)mv.pw;
        top = x > top ? x : top;
    }
    return k_pa_ok((uint64_t)mv.s[w ? 6 : 14], top + 32 * words) ? (int64_t)load : KOS_EFAULT;
}

/* One write to mover register `r` (byte offset / 8). A GATHER or SCATTER GO
 * returns 1: k_de_stream bounds its rows through mv_index. */
static UNIT_SMALL int64_t mv_reg(uint64_t r, uint64_t val)
{
    unsigned w = val & 1;
    if (r > 10 || !((0x5fdu >> r) & 1)) {
        return KOS_EINVAL;
    }
    if (!r) {
        unsigned mode = val & 7;
        if (!(val & KOS_MV_CTRL_GO)) {
            return KOS_OK;
        }
        /* GATHER, GENERATE and FILL walk the destination only, SCATTER the
         * source only (mm_mover.v:284-286, 428-430) */
        for (w = (0x1cu >> mode) & 1; w < 2 - (mode == 6); ++w) {
            const int64_t *s = &mv.s[8 * w];
            unsigned n = (unsigned)s[7], need = ((1u << n) - 1) | 0x40;
            if (((mv.ok >> (8 * w)) & need) != need) {
                return KOS_EINVAL;
            }
            uint64_t lo = (uint64_t)s[6], len = w && mode == 5 ? MV_XFORM_OUT_BYTES : 32;
            while (n--) {
                if (s[n] < 0) {
                    lo += (uint64_t)s[n];
                }
                len += (uint64_t)(s[n] < 0 ? -s[n] : s[n]);
            }
            if (!k_pa_ok(lo, len)) {
                return KOS_EFAULT;
            }
        }
        if ((0x44u >> mode) & 1) {
            /* the indexed side's header, INDEX and pitch, all from this stream */
            unsigned need = (mode == 6 ? 0x4000u : 0x40u) | 0x30000u;
            return (mv.ok & need) == need ? 1 : KOS_EINVAL;
        }
        return KOS_OK;
    }
    if (r == 6 || r == 10) {
        *(r == 6 ? &mv.ix : &mv.pw) = val;
        mv.ok |= r == 6 ? 0x10000u : 0x20000u;
        return KOS_OK;
    }
    if (r != 4 && r < 6) {
        mv.sel = w;
    }
    if (r == 2) {
        unsigned n = (val >> 44) & 7;
        mv.s[8 * w + 6] = (int64_t)((val >> 4) & ((1UL << 40) - 1));
        mv.s[8 * w + 7] = n > 6 ? 6 : n; /* 7 walks six levels (mx_tdesc.v:120) */
        mv.ok |= 0x40u << (8 * w);
    } else if (r == 3) {
        uint64_t c = (val >> 4) & 0xffff;
        mv.scount = c ? (uint32_t)c : 1; /* count 0 reads as 1 */
        mv.staged = (int64_t)(mv.scount - 1) * (int32_t)(val >> 20);
        mv.slvl = (val >> 1) & 7;
        /* a level's partial sum is 32-bit and wraps (mx_tdesc.v:126,188) */
        if (mv.slvl > 5 || mv.staged != (int32_t)mv.staged) {
            return KOS_EINVAL;
        }
        mv.ok |= 0x80;
    } else if (r == 4) {
        unsigned i = 8 * mv.sel + mv.slvl;
        if (!(mv.ok & 0x80)) {
            return KOS_EINVAL;
        }
        mv.s[i] = mv.staged;
        mv.n[i] = mv.scount;
        mv.ok |= 1u << i;
    }
    return KOS_OK;
}

/* Wait until the mover is idle (done0 ~0u) or has finished the move after
 * done count done0, whose fault it returns negated; KOS_ETIME once
 * KOS_DE_TIMEOUT cycles pass. */
static UNIT_SMALL int64_t mover_finish(uint32_t done0)
{
    uint64_t t0 = hw_cycles(), s;
    for (;;) {
        s = hw_rd(HW_R_MVSTAT);
        if (!HW_MV_BUSY(s) && (done0 == ~0u || HW_MV_DONE(s) != done0 || HW_MV_FAULT(s))) {
            return (done0 != ~0u && HW_MV_FAULT(s)) ? -(int64_t)HW_MV_FAULT(s) : KOS_OK;
        }
        k_units_poll();
        if (hw_cycles() - t0 > KOS_DE_TIMEOUT) {
            return KOS_ETIME;
        }
    }
}

static UNIT_SMALL int64_t de_wait_q(uint64_t faults0, uint64_t *prog, unsigned max_used, int mode)
{
    uint64_t t0 = hw_cycles();
    for (;;) {
        k_units_poll();
        uint64_t s = hw_rd(HW_R_DE_WAIT), m = hw_rd(HW_R_MVSTAT);
        if (mode != 2 && (kstat.faults != faults0 || HW_MV_FAULT(m))) {
            return KOS_EFAULT;
        }
        if (s & (1UL << 35)) {
            return KOS_EINVAL;
        }
        int busy = ((unsigned)(s & 0xffff) > max_used) || (mode && HW_MV_BUSY(m));
        if (mode == 2) {
            busy |= (hw_rd(NM(HW_NM_STAT)) & HW_NM_OFFERED) != 0;
            for (unsigned u = 0; u < knunits; ++u) {
                busy |= kunits[u].local && kunits[u].sent != retired[u];
            }
        }
        if (!busy) {
            return KOS_OK;
        }
        uint64_t cur = k_events + ((s >> 16) & 0xffff) + HW_MV_DONE(m);
        uint64_t now = hw_cycles();
        if (cur != *prog) {
            *prog = cur;
            t0 = now;
        } else if (now - t0 > KOS_DE_TIMEOUT) {
            return KOS_ETIME;
        }
    }
}

static UNIT_SMALL void de_map_all(uint64_t valid)
{
    hw_wr(HW_R_DE_CTL, valid ? 3 : 2);
    while (hw_rd(HW_R_DE_WAIT) & (1UL << 34)) {
    }
    de_ready = valid != 0;
    for (unsigned u = 0; u < knunits && u < KOS_DE_MAX_UNITS; ++u) {
        if (kunits[u].local) {
            hw_wr(HW_R_DE_MAP, valid | ((uint64_t)u << 16) |
                  ((uint64_t)kunits[u].u.y << 8) | kunits[u].u.x);
            de_mapped[u] = valid != 0;
            de_seen[u] = 0;
        }
    }
}

UNIT_SMALL int64_t k_de_stream(const uint64_t *entries, unsigned count)
{
    if (!kn.engine || !HW_DE_MAGIC(hw_rd(HW_R_DE_WAIT))) {
        return KOS_EINVAL;
    }
    uint64_t prog = 0;
    int64_t st = de_wait_q(kstat.faults, &prog, de_ready ? 0 : 0xffff, 2);
    if (st) {
        return st;
    }
    de_map_all(1UL << 31);
    mv.ok = 0;
    /* the mailbox registers as this stream wrote them; a GO before any DST
     * reads as a fetch with no descriptor and is refused */
    uint64_t faults0 = kstat.faults, base = (uint64_t)(uintptr_t)entries;
    uint64_t dst = ~0UL, arg2 = 0, arg3 = 0;
    uint16_t q_go[KOS_DE_QUEUE_DEPTH];
    uint32_t pushed = 0;
    unsigned room = KOS_DE_QUEUE_DEPTH;
#ifdef KOS_LAUNCH_RING
    uint64_t ts = hw_cycles();
#endif
    for (unsigned left = count; left; entries += 4) {
        uint64_t codes = entries[0];
        for (unsigned j = 1; j <= 3 && left; ++j, --left, codes >>= 8) {
            unsigned code = (unsigned)codes & 0xff;
            uint64_t val = entries[j];
            if (code & (KOS_DE_RELOC_FETCH | KOS_DE_RELOC_MOVE)) {
                val += (code & KOS_DE_RELOC_FETCH) ? (base << KOS_DE_FETCH_SHIFT) : (base << KOS_DE_MOVE_SHIFT);
                code &= KOS_DE_CODE_MASK;
            }
            if (code == KOS_DE_CHECK_ZERO) {
                if ((val & 7) || !k_pa_ok(val, 8)) {
                    st = (val & 7) ? KOS_EINVAL : KOS_EFAULT;
                    goto out;
                }
                st = de_wait_q(faults0, &prog, 0, 0);
                if (st || *(const volatile uint64_t *)(val | HW_UNC)) {
                    st = st ? st : KOS_EFAULT;
                    goto out;
                }
                room = KOS_DE_QUEUE_DEPTH;
                continue;
            }
            if (code > KOS_DE_MB_GO && code != KOS_DE_WAIT_CODE) {
                st = code == KOS_DE_IL_RING ?
                     ((unsigned)((val >> 8) & 0xff) - KOS_SLOT_USER < HW_SIG_SLOTS - KOS_SLOT_USER ? KOS_OK : KOS_EINVAL) :
                     mv_reg((uint64_t)code - KOS_DE_MV_BASE, val);
                if (st > 0) {
                    /* an indexed GO reads the vector once everything before it
                     * has reached the mover; INDEX then names the kernel's copy */
                    st = de_wait_q(faults0, &prog, 0, 1);
                    st = st ? st : mv_index(val);
                    if (st > 0) {
                        q_go[pushed++ & (KOS_DE_QUEUE_DEPTH - 1)] = 0;
                        hw_wr(HW_R_DE_Q + 8 * KOS_DE_MV_INDEX, mv_ixbuf | ((uint64_t)st << 40));
                        room = KOS_DE_QUEUE_DEPTH - 1;
                        st = KOS_OK;
                    }
                }
                if (st) {
                    goto out;
                }
            }
            uint16_t go = 0;
            if (code == KOS_DE_MB_DST) {
                /* untyped (CU_INST to a unit) or the instruction fetch's
                 * MEM_RD_REQ; never another flit type (control-registers.md M_DST) */
                if ((val >> 16) && (val >> 16) != (KOS_DE_FETCH_TYPED >> 16)) {
                    st = KOS_EINVAL;
                    goto out;
                }
                dst = val;
            } else if (code == KOS_DE_MB_ARG2) {
                arg2 = val;
            } else if (code == KOS_DE_MB_ARG3) {
                arg3 = val;
            } else if (code == KOS_DE_MB_GO) {
                unsigned f = (dst >> 16) != 0;
                unsigned xy = f ? (unsigned)(arg2 >> KOS_DE_FETCH_COORD_SHIFT) : (unsigned)(((dst >> 4) & 0xf0) | (dst & 0xf));
                int u = find(xy & 0xf, (xy >> 4) & 0xf);
                unsigned n = f ? (unsigned)(arg3 & 0xff) : 1;
                /* a fetch is one streamed run of n one-word entries to peer 0:
                 * entry_words 1, no other peer, len 0, flags INST|STREAM
                 * (flit-format.md 4.1), so it reads exactly n * 32 bytes */
                if (u < 0 || !n || (f && (((arg2 ^ KOS_DE_FETCH_REPLY) & ((1UL << 40) - 1)) ||
                                          ((arg3 ^ KOS_DE_FETCH_TAG) & 0xffff00)))) {
                    st = KOS_EINVAL;
                    goto out;
                }
                if (f && !k_pa_ok((arg3 >> KOS_DE_FETCH_SHIFT) & ((1UL << 40) - 1), (uint64_t)n * 32)) {
                    st = KOS_EFAULT;
                    goto out;
                }
#if defined(KOS_N1_MUT) && KOS_N1_MUT == 3
                kunits[u].sent += n;
#endif
                go = (uint16_t)(((u + 1) << 8) | n);
            }
            if (!room) {
                st = de_wait_q(faults0, &prog, KOS_DE_QUEUE_DEPTH - 1, 0);
                if (st) {
                    goto out;
                }
                room = KOS_DE_QUEUE_DEPTH - (unsigned)(hw_rd(HW_R_DE_WAIT) & 0xffff);
            }
            q_go[pushed++ & (KOS_DE_QUEUE_DEPTH - 1)] = go;
#if !defined(KOS_N1_MUT) || KOS_N1_MUT != 3
            if (go) {
                kunits[(go >> 8) - 1].sent += go & 0xff;
            }
#endif
            --room;
            hw_wr(HW_R_DE_Q + 8 * code, val);
        }
    }
#ifdef KOS_LAUNCH_RING
    uint64_t tw = hw_cycles();
    k_lring_cur.cyc_submit += tw - ts;
#endif
    st = de_wait_q(faults0, &prog, 0, 1);
#ifdef KOS_LAUNCH_RING
    k_lring_cur.cyc_wait += hw_cycles() - tw;
#endif
out:
    hw_wr(HW_R_DE_CTL, 0);
    k_units_poll();
    for (uint32_t iss = (uint32_t)(hw_rd(HW_R_DE_WAIT) >> 16) & 0xffff; iss < pushed; ++iss) {
        uint16_t g = q_go[iss & (KOS_DE_QUEUE_DEPTH - 1)];
        if (g) {
#if !defined(KOS_N1_MUT) || KOS_N1_MUT != 2
            kunits[(g >> 8) - 1].sent -= g & 0xff;
#else
            (void)g;
#endif
        }
    }
    de_map_all(0);
#if defined(KOS_N1_MUT) && KOS_N1_MUT == 1
    for (unsigned u = 0; u < knunits; ++u) {
        retired[u] = kunits[u].sent;
    }
    int64_t qst = 0;
#else
    int64_t qst = de_wait_q(faults0, &prog, 0xffff, 2);
#endif
    return qst ? qst : st ? st : (kstat.faults != faults0 ? KOS_EFAULT : KOS_OK);
}

/* One 1-D copy of `words` 32-byte words between physical addresses. */
int64_t k_move(uint64_t src, uint64_t dst, uint64_t words)
{
    if (!words || words > KOS_MOVE_MAX_WORDS || ((src | dst) & 31)) {
        return KOS_EINVAL;
    }
    int64_t st = mover_finish(~0u);
    if (st) {
        return st;
    }
    uint32_t done0 = HW_MV_DONE(hw_rd(HW_R_MVSTAT));
    for (unsigned sel = 0; sel < 2; ++sel) {
        uint64_t base = sel ? dst : src;
        hw_wr(MV(0x10), KOS_MV_HDR_VALID | (base << KOS_DE_MOVE_SHIFT) | sel);
        hw_wr(MV(0x18), KOS_MV_DIM_STRIDE | (words << KOS_MV_DIM_WORDS_SHIFT) | sel);
        hw_wr(MV(0x20), 0);
        hw_wr(MV(0x28), sel);
    }
    hw_wr(MV(0x00), KOS_MV_CTRL_1D);
    return mover_finish(done0);
}

/* A mover fault stays latched until the mover's next GO (mm_mover.v I_IDLE)
 * and drives the node's external interrupt (sysnode.v irq_summary), so left
 * set every return to user mode traps again. k_move and k_de_stream wait out
 * the mover and return its fault before a poll can run; here a one-word copy
 * within the kernel's index buffer clears it. */
UNIT_SMALL void k_mover_clear(void)
{
    if (HW_MV_FAULT(hw_rd(HW_R_MVSTAT))) {
        k_move(mv_ixbuf, mv_ixbuf, 1);
    }
}

/* ---- requests from other nodes that finish later ---- */
struct pend {
    unsigned kind, node, started;
    uint64_t req, a, b, faults;
};
static struct pend *pend; /* in private DRAM */
#ifdef KOS_POLL_EVENTS
static unsigned pending_count;
#endif

static void pending_init(void) { pend = k_alloc(K_PENDING * sizeof *pend); }

#ifdef KOS_REMOTE_BATCH
__attribute__((noinline))
#endif
static struct pend *pending_new(unsigned kind, unsigned node, uint64_t req, uint64_t a, uint64_t b)
{
    for (unsigned i = 0; i < K_PENDING; ++i) {
        if (!pend[i].kind) {
            pend[i] = (struct pend){kind, node, 0, req, a, b, a < knunits ? kunits[a].faults : 0};
#ifdef KOS_POLL_EVENTS
            ++pending_count;
#endif
            return &pend[i];
        }
    }
    k_reply(node, req, KOS_EAGAIN, 0);
    return 0;
}

void k_pending_add(unsigned kind, unsigned node, uint64_t req, uint64_t a, uint64_t b)
{
    pending_new(kind, node, req, a, b);
}

#ifdef KOS_REMOTE_BATCH
__attribute__((minsize))
void k_batch_start(unsigned node, uint64_t req, unsigned u, const uint64_t *w, unsigned count)
{
    struct pend *p = pending_new(M_BATCH, node, req, u, 0);
    if (!p) {
        return;
    }
#if defined(KOS_HOT_MUT) && KOS_HOT_MUT == 7
    --count;
#endif
    int64_t ticket = k_engine_run(u, w, count);
    if (ticket < 0) {
        k_reply(node, req, ticket, 0);
        p->kind = 0;
#ifdef KOS_POLL_EVENTS
        --pending_count;
#endif
    } else {
        p->b = (uint64_t)ticket;
    }
}
#endif

UNIT_MINSIZE void k_pending_poll(void)
{
#ifdef KOS_POLL_EVENTS
    if (!pending_count) {
        return;
    }
#endif
    for (unsigned i = 0; i < K_PENDING; ++i) {
        struct pend *p = &pend[i];
        if (!p->kind) {
            continue;
        }
        if (p->a >= knunits || !kunits[p->a].local) {
            k_reply(p->node, p->req, KOS_EINVAL, 0);
            p->kind = 0;
#ifdef KOS_POLL_EVENTS
            --pending_count;
#endif
            continue;
        }
        struct kunit *u = &kunits[p->a];
        if (p->kind == M_UNIT_WAIT
#ifdef KOS_REMOTE_BATCH
            || p->kind == M_BATCH
#endif
        ) {
            if (u->faults != p->faults) {
                k_reply(p->node, p->req, KOS_EFAULT, u->fault_arg);
                p->kind = 0;
#ifdef KOS_POLL_EVENTS
            --pending_count;
#endif
            } else if ((int64_t)(retired[p->a] - p->b) >= 0) {
                k_reply(p->node, p->req, KOS_OK, p->kind == M_BATCH ? p->b : 0);
                p->kind = 0;
#ifdef KOS_POLL_EVENTS
            --pending_count;
#endif
            }
        } else if (p->kind == M_UNIT_CTRL) {
            if (!p->started) {
                p->started = k_unit_ctrl_start((unsigned)p->a, (unsigned)p->b) == KOS_OK;
            } else if (u->ctrl_ready) {
                u->ctrl_ready = 0;
                u->ctrl_busy = 0;
                k_reply(p->node, p->req, KOS_OK, u->ctrl_value);
                p->kind = 0;
#ifdef KOS_POLL_EVENTS
            --pending_count;
#endif
            }
        }
    }
}
