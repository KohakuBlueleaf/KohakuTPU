/* kdsalloc OPS [-flags]: the arenas.
 *   On every node: OPS random allocations and frees; each block is 64-byte
 *   aligned, inside an arena chunk, and overlaps no live block; blocks here
 *   carry a pattern that must survive every other operation; every node's
 *   CPU task checks that SYS_VTOP of each of its blocks is the physical
 *   address any node computes from the reference (bind-time translation).
 *   Freeing everything leaves one free extent per chunk (coalescing), a
 *   block of the largest free size then fits without a new chunk; a block
 *   bigger than any free extent takes a new chunk, which kds_trim returns; a
 *   thread's blocks go with kds_free_owner. A stopped server leaves this
 *   node's free frames as they were before it started.
 *   On a board with a shared region: a system call copying a user buffer in
 *   shared memory beyond its first page reads that buffer (the region is one
 *   2 MB leaf when it is aligned). */
#include "kdsapp.h"

#define LIVE 48

struct blk {
    kds_ref ref;
    uint64_t size;
};

static uint64_t pattern(kds_ref r, uint64_t i) { return (r * 0x9E3779B97F4A7C15UL) ^ (i + 1); }

static int overlaps(const struct blk *b, unsigned n, kds_ref r, uint64_t size)
{
    for (unsigned i = 0; i < n; ++i) {
        if (KDS_REF_NODE(b[i].ref) == KDS_REF_NODE(r) && KDS_REF_OFF(r) < KDS_REF_OFF(b[i].ref) + b[i].size &&
            KDS_REF_OFF(b[i].ref) < KDS_REF_OFF(r) + size) {
            return 1;
        }
    }
    return 0;
}

/* On the blocks' node: SYS_VTOP of each block against the reference's
 * physical address. a[0]: user address of the list (the client's node), a[1]
 * count; the list is in a landing zone slice copied by the graph's move. */
static int64_t vtop_check(const uint64_t *a)
{
    volatile uint64_t *refs = kds_va(a[0]);
    for (uint64_t i = 0; i < a[1]; ++i) {
        kds_ref r = refs[i];
        void *va = kds_va(r);
        if (!va || kos_vtop(va, 64) != (int64_t)kds_pa(r)) {
            return (int64_t)i + 1;
        }
    }
    return 0;
}

static int one_node(unsigned node, uint64_t ops, uint64_t *seed, int *fails)
{
    struct blk b[LIVE];
    unsigned n = 0, here = node == kos_node();
    for (uint64_t op = 0; op < ops; ++op) {
        uint64_t x = kos_mix(seed);
        if (n == LIVE || (n && (x & 3) == 0)) {
            unsigned i = (unsigned)((x >> 8) % n);
            if (here) {
                const uint64_t *p = kds_va(b[i].ref);
                for (uint64_t w = 0; w < b[i].size / 8; ++w) {
                    if (p[w] != pattern(b[i].ref, w)) {
                        CHECK(*fails, 0, "kdsalloc: node %u block %lx word %lu clobbered", node, b[i].ref, w);
                        break;
                    }
                }
            }
            CHECK(*fails, kds_free(b[i].ref) == 0, "kdsalloc: free %lx", b[i].ref);
            b[i] = b[--n];
            continue;
        }
        uint64_t size = 1 + (x >> 16) % 4096;
        kds_ref r;
        int64_t st = kds_alloc(node, size, &r);
        if (st) {
            CHECK(*fails, 0, "kdsalloc: node %u alloc %lu: %ld", node, size, st);
            return n;
        }
        size = KOS_ALIGN_UP(size, 64);
        CHECK(*fails, KDS_REF_NODE(r) == node && !(KDS_REF_OFF(r) & 63) && !overlaps(b, n, r, size),
              "kdsalloc: node %u block %lx+%lu misplaced", node, r, size);
        if (here) {
            uint64_t *p = kds_va(r);
            CHECK(*fails, p != 0, "kdsalloc: no address for %lx", r);
            for (uint64_t w = 0; p && w < size / 8; ++w) {
                p[w] = pattern(r, w);
            }
        }
        b[n++] = (struct blk){r, size};
    }
    /* the survivors: their translation on their node, then free */
    kds_ref list;
    uint64_t *l = 0;
    if (!kds_alloc(kos_node(), LIVE * 8, &list)) {
        l = kds_va(list);
    }
    for (unsigned i = 0; l && i < n; ++i) {
        l[i] = b[i].ref;
    }
    if (l && n) {
        kds_ref tr;
        kds_alloc(kos_node(), 2 * sizeof(struct kds_task), &tr);
        struct kds_graph g;
        kds_graph_init(&g, kds_va(tr), 2);
        kds_ref slot = kds_land(node, KDS_LZ_USER);
        int mv = kds_move(&g, list, slot, LIVE * 8 / 32, 0, 0);
        kds_cpu(&g, node, vtop_check, slot, n, 0, &mv, 1);
        struct kds_result r;
        int64_t st = kds_run(&g, 1UL << 25, &r);
        CHECK(*fails, st == 0, "kdsalloc: node %u translation: status %ld, block %ld", node, st, r.detail);
        kds_free(tr);
        kds_free(list);
    }
    for (unsigned i = 0; i < n; ++i) {
        CHECK(*fails, kds_free(b[i].ref) == 0, "kdsalloc: free %lx", b[i].ref);
    }
    return n;
}

static void arena_checks(unsigned node, int *fails)
{
    struct kds_arena_stat s, t;
    kds_trim(node); /* chunks the random blocks grew, now free */
    kds_arena_stat(node, &s);
    /* all free but the server's own blocks: every chunk's free space is one extent */
    CHECK(*fails, s.free_extents <= s.chunks + s.used_extents,
          "kdsalloc: node %u: %lu free extents in %lu chunks with %lu blocks (not merged)", node, s.free_extents,
          s.chunks, s.used_extents);
    kds_ref r;
    CHECK(*fails, kds_alloc(node, s.largest, &r) == 0, "kdsalloc: node %u: largest %lu", node, s.largest);
    kds_arena_stat(node, &t);
    CHECK(*fails, t.chunks == s.chunks, "kdsalloc: node %u: the largest free block took a new chunk", node);
    kds_free(r);
    /* bigger than any free extent: a new chunk, returned by trim */
    uint64_t big = s.largest + 64;
    CHECK(*fails, kds_alloc(node, big, &r) == 0, "kdsalloc: node %u: block of %lu", node, big);
    kds_arena_stat(node, &t);
    CHECK(*fails, t.chunks == s.chunks + 1, "kdsalloc: node %u: %lu chunks after a big block", node, t.chunks);
    kds_free(r);
    int64_t back = kds_trim(node);
    kds_arena_stat(node, &t);
    CHECK(*fails, back >= (int64_t)big && t.chunks == s.chunks, "kdsalloc: node %u: trim %ld, %lu chunks", node, back,
          t.chunks);
    /* a thread's blocks */
    for (unsigned i = 0; i < 5; ++i) {
        kds_alloc(node, 100, &r);
    }
    CHECK(*fails, kds_free_owner(node) == 5, "kdsalloc: node %u: free_owner", node);
    REPORT("kdsalloc.extents", s.free_extents);
}

/* A syscall's copy of a shared buffer two pages into the region. */
static void shared_copy(int *fails)
{
    if (!kos_shared_ok()) {
        return;
    }
    uint8_t *p = kos_shalloc(3 * 4096);
    if (!p) {
        return;
    }
    uint8_t *q = (uint8_t *)KOS_ALIGN_UP((uint64_t)p + 4096, 4096);
    for (unsigned i = 0; i < 40; ++i) {
        q[i] = (uint8_t)(0xC0 + i);
    }
    uint8_t got[40];
    kos_send(kos_node(), 0x5348u, q, 40); /* SYS_SEND copies q in */
    int len = kos_recv(0x5348u, got, 1UL << 22, 0);
    CHECK(*fails, len == 40 && !memcmp(got, q, 40), "kdsalloc: shared buffer at %p copied wrong (%d bytes)", q, len);
    REPORT("kdsalloc.shared_copy", len == 40 && !memcmp(got, q, 40));
}

static int kdsalloc(int argc, char **argv)
{
    uint64_t num[1] = {64};
    unsigned nn, n = kos_nodes();
    unsigned flags = kds_args(argc, argv, num, 1, &nn);
    int fails = 0;
    shared_copy(&fails);
    struct kos_kernel_stat before, after;
    kos_stat(KOS_S_KERNEL, &before, sizeof before);
    fails += kds_start(flags, KDS_ARENA, KDS_ALL_NODES) != 0;
    uint64_t seed = 0xA110C;
    for (unsigned j = 0; j < n && !fails; ++j) {
        one_node(j, num[0], &seed, &fails);
        arena_checks(j, &fails);
    }
    fails += kds_stop() != 0;
    /* the stopped server gave every page back */
    kos_stat(KOS_S_KERNEL, &after, sizeof after);
    CHECK(fails, after.frames_free == before.frames_free, "kdsalloc: %lu free frames before the servers, %lu after",
          before.frames_free, after.frames_free);
    kos_printf("kdsalloc: %lu operations on each of %u nodes, %s\n", num[0], n, fails ? "FAIL" : "ok");
    return fails;
}
KOS_APP("kdsalloc", kdsalloc);
