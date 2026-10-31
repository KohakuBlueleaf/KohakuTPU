/* mg: KohakuTPU's matmul cluster ('MG') driven through the generic dispatch
 * system calls — the one app that knows a unit's instruction set. For every
 * MG unit on the board, from node 0:
 *   mailbox  FILL A, FILL B, DRAIN through SYS_DISPATCH (to the unit's node
 *            when it is on another mesh), waited for with SYS_UNIT_WAIT
 *   engine   the same three through SYS_ENGINE, for a unit on this mesh
 * Each round checks: the wait returns OK with no fault; CU_COUNTERS' retired
 * count rose by exactly the instructions sent; the DRAIN overwrote exactly
 * its one 32-byte word of sentinels at its destination in this node's
 * landing zone (written across the interlink when the unit is on another
 * mesh) and nothing around it. */
#include "../apps.h"

#define MG_TYPE 0x4D47
#define FILL 1
#define DRAIN 3
#define SENTINEL 0xA5A5A5A5A5A5A5A5UL

/* bits [lo, lo + width) of a 256-bit payload (w[0] = bits 63..0) */
static void put(uint64_t w[4], unsigned lo, unsigned width, uint64_t v)
{
    for (unsigned b = 0; b < width; ++b) {
        unsigned bit = lo + b;
        w[bit / 64] = (w[bit / 64] & ~(1UL << (bit % 64))) | (((v >> b) & 1) << (bit % 64));
    }
}

static void mg_inst(uint64_t w[4], unsigned op, uint64_t addr, unsigned n, unsigned sel)
{
    w[0] = w[1] = w[2] = w[3] = 0;
    put(w, 252, 4, op);
    put(w, 218, 34, addr & ((1UL << 34) - 1));
    put(w, 63, 6, addr >> 34);
    put(w, 202, 16, n);
    put(w, 201, 1, sel);
    put(w, 176, 8, 1);  /* nk */
    put(w, 168, 8, 40); /* anchor */
}

static uint64_t retired(unsigned u, int *fails)
{
    uint64_t v = 0;
    int st = kos_unit_ctrl(u, 2, &v);
    CHECK(*fails, st == 0, "mg: unit %u CU_COUNTERS read status %d", u, st);
    return v >> 32;
}

/* One FILL, FILL, DRAIN round on unit u; through the engine if `engine`. */
static void round(unsigned u, int engine, int retired_reply, uint64_t round_no, int *fails)
{
    volatile uint64_t *land = kos_land();
    uint64_t pa = kos_info(KOS_I_LANDPA);
    uint64_t src_off = 0x8000, dst_off = 0x9000 + round_no * 0x400;
    for (unsigned i = 0; i < 16; ++i) {
        land[src_off / 8 + i] = (i + round_no + 1) * 0x9E3779B97F4A7C15UL;
    }
    for (unsigned i = 0; i < 128; ++i) {
        land[dst_off / 8 + i] = SENTINEL;
    }
    uint64_t w[12];
    mg_inst(&w[0], FILL, pa + src_off, 1, 0);
    mg_inst(&w[4], FILL, pa + src_off, 1, 1);
    mg_inst(&w[8], DRAIN, pa + dst_off, 1, 0);
    uint64_t before = retired(u, fails);
    uint64_t t = kos_time();
    int64_t ticket = 0;
    if (engine) {
        ticket = kos_engine(u, w, 3);
    } else {
        for (unsigned k = 0; k < 3 && ticket >= 0; ++k) {
            ticket = kos_dispatch(u, &w[4 * k]);
        }
    }
    uint64_t faults = 0;
    int st = ticket < 0 ? (int)ticket : retired_reply ? 0 :
             kos_unit_wait(u, (uint64_t)ticket, 1UL << 24, &faults);
    t = kos_time() - t;
    uint64_t after = retired(u, fails);
    unsigned changed = 0;
    for (unsigned i = 0; i < 128; ++i) {
        changed += land[dst_off / 8 + i] != SENTINEL;
    }
    kos_printf("  mg unit %u %s: ticket %ld wait %d faults %lu, retired %lu -> %lu, %u of 128 drained words written, "
               "%lu cycles\n",
               u, engine ? "engine" : "mailbox", ticket, st, faults, before, after, changed, t);
    /* DRAIN n = 1 writes one 32-byte word: exactly 4 of the sentinels */
    CHECK(*fails, ticket > 0 && st == 0 && after - before == 3 && changed == 4,
          "mg: unit %u %s round", u, engine ? "engine" : "mailbox");
    REPORT(engine ? "mg.engine_round_cycles" : "mg.mailbox_round_cycles", t);
}

static int mg(int argc, char **argv)
{
    unsigned selected = (unsigned)arg_u(argc, argv, 1, KOS_MAX_UNITS);
    int fails = 0;
    struct kos_unit u[KOS_MAX_UNITS];
    int64_t n = kos_units(u, KOS_MAX_UNITS);
    uint64_t mesh = kos_info(KOS_I_MESH), rounds = 0, seen = 0;
    for (int64_t i = 0; i < n; ++i) {
        if (u[i].type != MG_TYPE || (selected != KOS_MAX_UNITS && (unsigned)i != selected)) {
            continue;
        }
        ++seen;
        round((unsigned)i, 0, 0, rounds++, &fails);
        if ((u[i].mesh == mesh
#ifdef KOS_REMOTE_BATCH
             || 1
#endif
            ) && (kos_info(KOS_I_FLAGS) & KOS_F_ENGINE)) {
            round((unsigned)i, 1, u[i].mesh != mesh, rounds++, &fails);
        }
    }
    REPORT("mg.units", seen);
    kos_printf("mg: %lu MG units, %lu rounds, %s\n", seen, rounds, fails ? "FAIL" : "ok");
    return fails;
}

KOS_APP("mg", mg);
