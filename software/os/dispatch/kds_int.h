/* kds internals: one server's state (per node: a plain global pointer, so
 * every thread of a node sees its own node's server), the messages between
 * clients and servers, and the pieces the server's files share. */
#ifndef KDS_INT_H
#define KDS_INT_H

#include <kos/kds.h>

#ifndef KDS_MUT
#define KDS_MUT 0 /* a mutant build for the checks: tests/host/test_kds.py */
#endif

#define KDS_CHAN       0x4B445300u              /* every server's request channel */
#define KDS_REPLY(tid) (0x4B520000u | (unsigned)(tid)) /* a client thread's reply channel */

#define KDS_CHUNKS   16  /* arena chunks per node */
#define KDS_EXTENTS  128 /* free and used extents each */
#define KDS_GRAPHS   4   /* graphs a node runs parts of at once */
#define KDS_HOMES    4   /* graphs a home tracks at once */
#define KDS_INFLIGHT 32  /* unit instructions in flight per unit */
#define KDS_OUTBOX   64  /* messages waiting for room */
#define KDS_CHUNK_TASKS ((unsigned)(KDS_LZ_SLICE / sizeof(struct kds_task))) /* per PART */
#define KDS_RETRY    20000 /* cycles before an outbox retry when no event comes */
#define KDS_BATCH    8     /* SYS_ENGINE's largest batch */
#define KDS_SERVER   0xffffffffu /* the owner of the server's own blocks (no thread's tid) */

/* every message: 40 bytes. A client request carries its reply channel in
 * gid; the reply is one KM_REPLY on it. */
struct kds_msg {
    uint8_t type, flag;
    uint16_t cnt;
    uint32_t gid;
    uint64_t p[4];
};
_Static_assert(sizeof(struct kds_msg) == 40, "a kds message fills a channel message");

enum {
    KM_SUBMIT = 1, /* client: p0 tasks, p1 count, p2 timeout -> p0 status, p1 failed, p2 detail, p3 cycles */
    KM_PART,       /* home: cnt records at landing-zone offset p1; p0 the part's total */
    KM_PART_ACK,   /* executor */
    KM_GO,         /* home */
    KM_DEP,        /* executor: cnt finished task ids, u16 each, in p */
    KM_NDONE,      /* executor -> home: p0 status, p1 failed, p2 detail, p3 busy cycles */
    KM_ABORT,      /* home */
    KM_ALLOC,      /* client: p0 bytes, p1 owner tid -> p0 status, p1 ref */
    KM_FREE,       /* client: p0 ref -> p0 status */
    KM_FREE_OWNER, /* client: p0 owner tid -> p0 count */
    KM_TRIM,       /* client -> p0 bytes */
    KM_STAT,       /* client -> p0 chunks, p1 bytes, p2 free, p3 largest; cnt used, flag free extents */
    KM_QUIT,
    KM_READY,      /* server -> starter: p0 status */
    KM_REPLY,      /* server -> client */
    KM_RETAIN,     /* local client: immutable graph pointer, or zero to release */
};

/* Messages between clients and servers carry no shared-memory data: plain
 * SYS_SEND / SYS_RECV, without libkos's release and acquire sweeps. */
static inline int kds_send(unsigned node, unsigned chan, const struct kds_msg *m, unsigned flags)
{
    return (int)kos_sys(SYS_SEND, node, chan, (uint64_t)m, sizeof *m | flags).st;
}

static inline int kds_recv(unsigned chan, struct kds_msg *m, uint64_t timeout, unsigned *src)
{
    struct kos_r r = kos_sys(SYS_RECV, chan, (uint64_t)m, timeout, 0);
    *src = (unsigned)r.val;
    return (int)r.st;
}

/* ---- the arena ---- */
struct kds_chunk {
    uint64_t va, off, size; /* off: in this node's span; size 0: unused */
};
struct kds_extent {
    uint32_t chunk, owner;
    uint64_t off, size; /* off in the chunk */
};

/* ---- a graph part on its executor ---- */
enum { LG_FREE, LG_LOADING, LG_LOADED, LG_RUN, LG_ENDED, LG_CACHED };
struct kds_part {
    uint32_t gid, state;
    unsigned home;
    const struct kds_task *source;
    uint32_t n, loaded, done, inflight; /* inflight: unit instructions not yet retired */
    int64_t status;
    uint32_t failed;
    int64_t detail;
    uint64_t busy, t0;
    kds_ref mem;               /* the arena block holding the arrays below */
    struct kds_task *t;        /* this node's tasks, in id order */
    uint16_t *remain;          /* predecessors still running */
    uint16_t *next;            /* per-unit pending lists */
    uint64_t *ticket;          /* a unit task's ticket */
    uint32_t *pairs, npairs;   /* (pred id << 16 | local index), sorted */
    uint16_t *ready;           /* ring of local indices, n + 1 */
    uint32_t rhead, rtail;
    uint16_t phead[KOS_MAX_UNITS], ptail[KOS_MAX_UNITS]; /* pending unit tasks, 0xffff none */
};

/* ---- a graph at its home ---- */
enum { HG_FREE, HG_QUEUED, HG_LOADING, HG_RUN };
struct kds_home {
    uint32_t gid, state;
    struct kds_task *t;  /* the client's records (this node) */
    uint32_t n;
    unsigned client_node, reply;
    uint64_t t0, deadline;
    uint32_t parts, contacted, acked, reported, aborted; /* node masks */
    uint32_t count[KOS_MAX_NODES], sent[KOS_MAX_NODES], scan[KOS_MAX_NODES];
    int64_t status;
    uint32_t failed;
    int64_t detail;
    int answered; /* the client has its reply */
};

struct kds_out {
    unsigned node, chan;
    struct kds_msg m;
};

struct kds_dep {
    uint32_t gid;
    unsigned cnt;
    uint16_t id[16];
};

struct kds_node {
    unsigned me, n, flags, mask, nunits;
    struct kos_unit units[KOS_MAX_UNITS];
    uint8_t unit_node[KOS_MAX_UNITS];
    uint16_t depth[KOS_MAX_UNITS];
    uint64_t span[KOS_MAX_NODES], spansz[KOS_MAX_NODES];
    uint64_t chunk_bytes;
    /* arena */
    struct kds_chunk chunk[KDS_CHUNKS];
    struct kds_extent fr[KDS_EXTENTS], used[KDS_EXTENTS];
    unsigned nfree, nused;
    /* units */
    uint64_t counts[2 * KOS_MAX_UNITS], seen_faults[KOS_MAX_UNITS], events;
    struct {
        uint8_t part;
        uint16_t idx;
    } fly[KOS_MAX_UNITS][KDS_INFLIGHT];
    unsigned fhead[KOS_MAX_UNITS], fcount[KOS_MAX_UNITS], fwords[KOS_MAX_UNITS];
    uint8_t noengine[KOS_MAX_UNITS]; /* SYS_ENGINE refused this unit: SYS_DISPATCH it */
    int cpu_dirty, hw_dirty;         /* the L1 holds CPU writes / units wrote DRAM behind it */
    /* what a loop pass has to visit: user code runs from DRAM, so a pass
     * fetches only the phases with work */
    int any_ready, any_pending, any_deps;
    unsigned in_flight;
    /* graphs */
    struct kds_part part[KDS_GRAPHS];
    struct kds_home home[KDS_HOMES];
    int loading; /* the home graph loading now, -1 none: one at a time (one landing slice per peer) */
    uint32_t seq;
    const struct kds_task *retained;
    struct kds_task *stage; /* KDS_CHUNK_TASKS records, contiguous: a part's next chunk for the mover */
    struct kds_dep dep[KOS_MAX_NODES];
    struct kds_out out[KDS_OUTBOX];
    unsigned ohead, ocount;
    int quit;
    struct kds_server_stat st;
};

extern struct kds_node *kds_k; /* this node's server, null when none */

/* Optional diagnostic sample in the application-owned landing area. */
static inline void kds_trace(unsigned phase, uint64_t a, uint64_t b)
{
#ifdef KDS_TRACE
    volatile uint64_t *v = (volatile uint64_t *)(KOS_UV_LAND + KDS_LZ_USER + OSTPU_OFF_TRACE);
    v[1] = phase;
    v[2] = a;
    v[3] = b;
    v[4] = kos_now();
    __asm__ volatile("fence rw, rw" ::: "memory");
    ++v[0];
#else
    (void)phase; (void)a; (void)b;
#endif
}

int kds_part_reuse(struct kds_node *k, uint32_t gid, const struct kds_task *source, uint32_t n);

/* arena.c */
int kds_arena_init(struct kds_node *k, uint64_t bytes);
int64_t kds_arena_alloc(struct kds_node *k, uint64_t bytes, unsigned owner, kds_ref *ref);
int kds_arena_free(struct kds_node *k, kds_ref ref);
int64_t kds_arena_free_owner(struct kds_node *k, unsigned owner);
int64_t kds_arena_trim(struct kds_node *k);
void kds_arena_stat_of(struct kds_node *k, struct kds_arena_stat *s);
void *kds_arena_va(struct kds_node *k, kds_ref ref);

/* graph.c: a node's parts */
int kds_part_open(struct kds_node *k, uint32_t gid, unsigned home, uint32_t n);
int kds_part_add(struct kds_node *k, uint32_t gid, const struct kds_task *t, unsigned cnt);
void kds_part_go(struct kds_node *k, uint32_t gid);
void kds_part_dep(struct kds_node *k, uint32_t gid, const uint16_t *id, unsigned cnt);
int kds_part_abort(struct kds_node *k, uint32_t gid); /* 0: no such part here */
int kds_parts_run(struct kds_node *k);
int kds_units_issue(struct kds_node *k);
void kds_units_retire(struct kds_node *k);
uint64_t kds_units_need(struct kds_node *k);

/* server.c */
void kds_post(struct kds_node *k, unsigned node, unsigned chan, const struct kds_msg *m);
void kds_dep_add(struct kds_node *k, unsigned node, uint32_t gid, uint16_t id);
void kds_home_ndone(struct kds_node *k, unsigned node, uint32_t gid, int64_t status, uint32_t failed,
                    int64_t detail, uint64_t busy);
void kds_dep_flush(struct kds_node *k);
uint64_t kds_resolve(struct kds_node *k, kds_ref ref); /* the physical address this node names it by */

#endif
