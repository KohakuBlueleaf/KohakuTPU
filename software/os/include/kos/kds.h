/* kds: the dispatcher. One server thread per node owns that node's units, an
 * arena of physically contiguous memory, and the parts of task graphs that
 * run there. A client builds a graph of tasks (unit instructions, mover
 * copies, CPU functions, joins), each naming up to KDS_MAXPRED predecessors,
 * and submits it to its own node's server (the graph's home), which loads
 * every node's part into that node's server and collects their completion.
 *
 * Memory: a buffer is a reference, node << 56 | offset in that node's
 * private span. Any node turns a reference into the physical address its
 * units and mover use (bind time), so the same graph binds on any die. */
#ifndef KOS_KDS_H
#define KOS_KDS_H

#include <kos/user.h>

typedef uint64_t kds_ref;
#define KDS_REF(node, off) (((uint64_t)(node) << 56) | (uint64_t)(off))
#define KDS_REF_NODE(r)    ((unsigned)((r) >> 56))
#define KDS_REF_OFF(r)     ((r) & ((1UL << 56) - 1))

/* task kinds */
enum {
    KDS_NOP,  /* a join or fan-out point: done when its predecessors are */
    KDS_UNIT, /* a[0..3]: one instruction to unit `unit`, after the patches */
    KDS_MOVE, /* a[0] src ref, a[1] dst ref, a[2] 32-byte words: on the source's node; dst
                 a buffer of that node or any node's landing zone */
    KDS_CPU,  /* a[0] int64_t (*)(const uint64_t args[3]), a[1..3] its arguments: on `node`;
                 a nonzero return fails the task */
};

#define KDS_MAXPRED  4
#define KDS_MAXPATCH 3
#define KDS_MAXTASKS 65535

/* Write bits [lo, lo + width) of the instruction with (physical address of
 * ref) >> shift. A field split over two places is two patches. */
struct kds_patch {
    kds_ref ref;
    uint16_t lo;
    uint8_t width, shift;
    uint32_t pad;
};

struct kds_task {
    uint8_t kind, node, npred, npatch; /* node: filled at submit for UNIT and MOVE */
    uint16_t unit, succ_nodes;         /* succ_nodes: filled at submit */
    uint16_t pred[KDS_MAXPRED];        /* indices of earlier tasks */
    uint64_t a[4];
    struct kds_patch patch[KDS_MAXPATCH];
    uint32_t id; /* its index, filled at submit */
    uint32_t pad32;
    uint64_t pad[3];
};
_Static_assert(sizeof(struct kds_task) == 128, "a task record is 128 bytes");

struct kds_graph {
    struct kds_task *t;
    unsigned n, cap;
    int err; /* a builder call failed: KOS_EINVAL (bad argument) or KOS_ENOMEM (full) */
};

struct kds_result {
    int64_t status;    /* KOS_OK, KOS_EINVAL (bad graph), KOS_EFAULT (a task failed), KOS_ETIME,
                          KOS_ENOMEM, KOS_ENOENT (no server where a task runs) */
    uint64_t failed;   /* the failed task's index */
    int64_t detail;    /* a CPU task's return, a unit's fault count, a mover's fault */
    uint64_t cycles;   /* at the home: submit to the last part's completion */
};

/* server flags */
#define KDS_F_ENGINE   (1u << 0) /* unit tasks in batches of up to 8 through SYS_ENGINE */
#define KDS_F_UNCACHED (1u << 1) /* arenas mapped uncached: no release/acquire sweeps */
#define KDS_F_BESTFIT  (1u << 2) /* the arena allocates best fit (else first fit) */

/* Start a server on every node in node_mask, each with an arena of
 * arena_bytes to begin with; returns when all are up. kds_stop on the same
 * node stops them. */
int kds_start(unsigned flags, uint64_t arena_bytes, unsigned node_mask);
int kds_stop(void);

/* Graph building: each call returns the new task's index, or -1 (g->err). */
void kds_graph_init(struct kds_graph *g, struct kds_task *buf, unsigned cap);
int kds_nop(struct kds_graph *g, unsigned node, const int *pred, unsigned npred);
int kds_unit(struct kds_graph *g, unsigned unit, const uint64_t w[4], const int *pred, unsigned npred);
int kds_patch(struct kds_graph *g, int task, kds_ref ref, unsigned lo, unsigned width, unsigned shift);
int kds_move(struct kds_graph *g, kds_ref src, kds_ref dst, uint64_t words, const int *pred, unsigned npred);
int kds_cpu(struct kds_graph *g, unsigned node, int64_t (*fn)(const uint64_t *args), uint64_t a0, uint64_t a1,
            uint64_t a2, const int *pred, unsigned npred);

/* Run a graph through this node's server; waits until every part has
 * finished, failed or timed out (timeout in cycles, 0 none). */
int64_t kds_run(struct kds_graph *g, uint64_t timeout, struct kds_result *r);

/* Memory, on any node with a server: 64-byte aligned, a multiple of 64. */
int64_t kds_alloc(unsigned node, uint64_t bytes, kds_ref *ref);
int kds_free(kds_ref ref);
int64_t kds_free_owner(unsigned node);       /* everything this thread took there: how many */
int64_t kds_trim(unsigned node);             /* return fully free chunks: bytes returned */
kds_ref kds_land(unsigned node, uint64_t off); /* node's landing zone + off */
void *kds_va(kds_ref ref);                   /* on its own node: the user address, else null */
uint64_t kds_pa(kds_ref ref);                /* the physical address this node's units use */

struct kds_arena_stat {
    uint64_t chunks, bytes, free, largest, used_extents, free_extents;
};
int kds_arena_stat(unsigned node, struct kds_arena_stat *s);

/* This node's server, counted since it started: where its cycles went. */
struct kds_server_stat {
    uint64_t passes, waits;              /* loop passes; SYS_EVENT returns for retirements (no message) */
    uint64_t tasks, unit_batches;        /* tasks finished here; SYS_DISPATCH / SYS_ENGINE calls */
    uint64_t msgs_in, msgs_out;
    uint64_t cyc_msgs, cyc_run, cyc_issue, cyc_retire, cyc_send, cyc_event;
};
int kds_server_stat(struct kds_server_stat *s); /* KOS_ENOENT: no server on this node */

/* The landing zone's first 32 KB belong to the servers while they run:
 * [home * 8 KB, + 8 KB) carries graph parts loaded by `home`. */
#define KDS_LZ_SLICE 0x2000UL
#define KDS_LZ_USER  (4 * KDS_LZ_SLICE) /* the first landing-zone byte left to applications */

#endif
