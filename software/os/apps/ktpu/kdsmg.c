/* kdsmg R [-flags]: KohakuTPU's MG units through the dispatcher.
 *   bind     for every MG unit, on the unit's node: a CPU task writes
 *            sentinels into an arena block (cached unless -u), FILL A, FILL B
 *            and DRAIN follow with the block's addresses patched in at bind
 *            time, then a CPU task checks that the DRAIN wrote exactly its
 *            one 32-byte word at the patched offset (release before the
 *            unit, acquire after); with more than one node the unit also
 *            DRAINs across the interlink into another node's landing zone
 *            (node 0's for a unit on another die, node 1's for a unit here),
 *            checked there
 *   rounds   R FILL, FILL, DRAIN rounds on the first MG unit of each node, in
 *            one graph, issued in order without waits: cycles per round */
#include "../kds/kdsapp.h"

#define MG_TYPE  0x4D47
#define FILL     1
#define DRAIN    3
#define SENTINEL 0xA5A5A5A5A5A5A5A5UL
/* A block of 16 L1 lines: FILL reads SRC (0x80 bytes of data), sentinels
 * fill the rest, DRAIN lands at DST; 32 words around DST are checked. */
#define BLOCK    0x200
#define SRC      0x000
#define DST      0x100
#define CHECKED  0x80

/* An MG instruction with its address left 0: patched at bind time. */
static void mg_inst(uint64_t w[4], unsigned op, unsigned sel)
{
    w[0] = w[1] = w[2] = w[3] = 0;
    w[252 / 64] |= (uint64_t)op << (252 % 64);
    w[202 / 64] |= 1UL << (202 % 64); /* n = 1 */
    w[201 / 64] |= (uint64_t)sel << (201 % 64);
    w[176 / 64] |= 1UL << (176 % 64); /* nk = 1 */
    w[168 / 64] |= 40UL << (168 % 64); /* anchor */
}

/* MG's address: bits 33..0 at 218, bits 39..34 at 63. */
static int mg_task(struct kds_graph *g, unsigned u, unsigned op, unsigned sel, kds_ref at, const int *pred,
                   unsigned npred)
{
    uint64_t w[4];
    mg_inst(w, op, sel);
    int t = kds_unit(g, u, w, pred, npred);
    kds_patch(g, t, at, 218, 34, 0);
    kds_patch(g, t, at, 63, 6, 34);
    return t;
}

static int64_t sentinels(const uint64_t *a)
{
    uint64_t *p = kds_va(a[0]);
    for (unsigned i = 0; i < BLOCK / 8; ++i) {
        p[i] = i < 16 ? (i + 1) * 0x9E3779B97F4A7C15UL : SENTINEL;
    }
    return 0;
}

/* a[0] the block, a[1] where DRAIN wrote, a[2] the first of 32 checked
 * words' byte offset: exactly the 4 at a[1] changed. 0, else 1 + how many did. */
static int64_t drained(const uint64_t *a)
{
    volatile uint64_t *p = kds_va(a[0]);
    unsigned at = (unsigned)a[1] / 8, from = (unsigned)a[2] / 8, changed = 0, right = 0;
    for (unsigned i = from; i < from + 32; ++i) {
        unsigned c = p[i] != SENTINEL;
        changed += c;
        right += c && i >= at && i < at + 4;
    }
    return changed == 4 && right == 4 ? 0 : 1 + changed;
}

static int64_t lz_sentinels(const uint64_t *a)
{
    volatile uint64_t *p = kds_va(a[0]);
    for (unsigned i = 0; i < 128; ++i) {
        p[i] = SENTINEL;
    }
    return 0;
}

static int bind(unsigned u, const struct kos_unit *unit, unsigned node, int *fails)
{
    kds_ref blk, tr;
    if (kds_alloc(node, BLOCK, &blk) || kds_alloc(kos_node(), 16 * sizeof(struct kds_task), &tr)) {
        CHECK(*fails, 0, "kdsmg: alloc for unit %u", u);
        return 0;
    }
    struct kds_graph g;
    kds_graph_init(&g, kds_va(tr), 16);
    int s = kds_cpu(&g, node, sentinels, blk, 0, 0, 0, 0);
    int a = mg_task(&g, u, FILL, 0, blk + SRC, &s, 1);
    int b = mg_task(&g, u, FILL, 1, blk + SRC, &a, 1);
    int d = mg_task(&g, u, DRAIN, 0, blk + DST, &b, 1);
    kds_cpu(&g, node, drained, blk, DST, CHECKED, &d, 1);
    int remote = node != kos_node();
    if (kos_nodes() > 1) {
        /* the same unit drains across the interlink into another node's
         * landing zone (this node's, or node 1's for a unit here: its
         * address carries mesh bits, past MG's 34-bit field) */
        unsigned to = remote ? kos_node() : 1;
        kds_ref lz = kds_land(to, KDS_LZ_USER);
        int l = kds_cpu(&g, to, lz_sentinels, lz, 0, 0, 0, 0);
        int pr[2] = {d, l};
        int d2 = mg_task(&g, u, DRAIN, 0, lz + 0x40, pr, 2);
        kds_cpu(&g, to, drained, lz, 0x40, 0, &d2, 1);
    }
    struct kds_result r;
    int64_t st = kds_run(&g, 1UL << 26, &r);
    kos_printf("  kdsmg unit %u (%c%c, node %u): status %ld task %lu detail %ld, %lu cycles\n", u, unit->type & 0xff,
               unit->type >> 8, node, st, r.failed, r.detail, r.cycles);
    CHECK(*fails, st == 0, "kdsmg: unit %u bind: status %ld task %lu detail %ld", u, st, r.failed, r.detail);
    REPORT(remote ? "kdsmg.bind_far_cycles" : "kdsmg.bind_cycles", r.cycles);
    kds_free(blk);
    kds_free(tr);
    return 1;
}

static void rounds(unsigned u, unsigned node, uint64_t nr, int *fails)
{
    kds_ref blk, tr;
    unsigned cap = (unsigned)(3 * nr);
    if (kds_alloc(node, BLOCK, &blk) || kds_alloc(kos_node(), cap * sizeof(struct kds_task), &tr)) {
        CHECK(*fails, 0, "kdsmg: alloc for rounds on unit %u", u);
        return;
    }
    struct kds_graph g;
    kds_graph_init(&g, kds_va(tr), cap);
    /* no predecessors: one unit runs its instructions in the order they are
     * issued, and the ready order is the build order */
    for (uint64_t i = 0; i < nr; ++i) {
        mg_task(&g, u, FILL, 0, blk + SRC, 0, 0);
        mg_task(&g, u, FILL, 1, blk + SRC, 0, 0);
        mg_task(&g, u, DRAIN, 0, blk + DST, 0, 0);
    }
    struct kds_result r;
    struct kds_server_stat a, b;
    kds_server_stat(&a);
    int64_t st = kds_run(&g, 1UL << 27, &r);
    kds_server_stat(&b);
    kds_print_server("kdsmg rounds", &a, &b);
    CHECK(*fails, st == 0, "kdsmg: rounds on unit %u: status %ld task %lu", u, st, r.failed);
    kos_printf("  kdsmg rounds: unit %u (node %u) %lu rounds in %lu cycles, %lu per round\n", u, node, nr, r.cycles,
               r.cycles / nr);
    REPORT(node == kos_node() ? "kdsmg.round_cycles" : "kdsmg.round_far_cycles", r.cycles / nr);
    kds_free(blk);
    kds_free(tr);
}

static int kdsmg(int argc, char **argv)
{
    uint64_t num[2] = {8, KOS_MAX_UNITS};
    unsigned nn;
    unsigned flags = kds_args(argc, argv, num, 2, &nn);
    int fails = kds_start(flags, KDS_ARENA, KDS_ALL_NODES) != 0;
    struct kos_unit unit[KOS_MAX_UNITS];
    int64_t nu = kos_units(unit, KOS_MAX_UNITS);
    unsigned seen = 0, node_of_mesh[8] = {0};
    for (unsigned j = 0; j < kos_nodes(); ++j) {
        node_of_mesh[kos_info_of(KOS_I_MESHOF, j) & 7] = j;
    }
    unsigned first_done = 0;
    for (int64_t i = 0; i < nu && !fails; ++i) {
        if (unit[i].type != MG_TYPE || (num[1] != KOS_MAX_UNITS && (uint64_t)i != num[1])) {
            continue;
        }
        unsigned node = node_of_mesh[unit[i].mesh & 7];
        seen += bind((unsigned)i, &unit[i], node, &fails);
        if (!(first_done >> node & 1) && num[0]) {
            rounds((unsigned)i, node, num[0], &fails);
            first_done |= 1u << node;
        }
    }
    REPORT("kdsmg.units", seen);
    fails += kds_stop() != 0;
    kos_printf("kdsmg: %u MG units, %s\n", seen, fails ? "FAIL" : "ok");
    return fails;
}
KOS_APP("kdsmg", kdsmg);
