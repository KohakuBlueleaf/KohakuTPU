/* The dispatcher apps' graphs natively: `kdsmulti K` over a double of the client
 * library, each client run to its end in turn, each task run in index order on
 * its node's span. A node's landing zone is mapped as the kernel maps it, an
 * unmapped page behind it, so a slot past it dies here as it faults there.
 *   kds_app NODES K -> kdsmulti's return value */
#define _GNU_SOURCE
#define KOS_REMOTE_BATCH
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>

#include APP_SOURCE

#define SPAN(node) (0x40000000UL + (uint64_t)(node) * 0x1000000UL)
#define LAND_OFF 0x100000UL
#define PAGE 4096UL

static unsigned nodes, here;
static uint64_t next_off[KOS_MAX_NODES], clock_now;
static int64_t codes[KOS_MAX_NODES];

int kos_printf(const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    int n = vprintf(fmt, ap);
    va_end(ap);
    return n;
}

uint64_t kos_strtoul(const char **p) { return strtoul(*p, 0, 0); }
uint64_t kos_time(void) { return ++clock_now; }
unsigned kos_node(void) { return here; }
unsigned kos_nodes(void) { return nodes; }

int64_t kos_spawn(int64_t (*fn)(uint64_t), uint64_t arg, unsigned node)
{
    here = node;
    codes[node] = fn(arg);
    here = 0;
    return node;
}

int64_t kos_join(int64_t tid, int64_t *code)
{
    *code = codes[tid];
    return 0;
}

int kds_start(unsigned flags, uint64_t arena_bytes, unsigned node_mask)
{
    (void)flags;
    (void)arena_bytes;
    (void)node_mask;
    return 0;
}

/* The file's other apps link; none runs here. */
void kos_sleep(uint64_t cycles)
{
    (void)cycles;
    abort();
}

int kds_nop(struct kds_graph *g, unsigned node, const int *pred, unsigned npred)
{
    (void)g;
    (void)node;
    (void)pred;
    (void)npred;
    abort();
}

int kds_unit(struct kds_graph *g, unsigned unit, const uint64_t w[4], const int *pred, unsigned npred)
{
    (void)g;
    (void)unit;
    (void)w;
    (void)pred;
    (void)npred;
    abort();
}

int kds_stop(void) { return 0; }
int kds_free(kds_ref ref)
{
    (void)ref;
    return 0;
}

int64_t kds_alloc(unsigned node, uint64_t bytes, kds_ref *ref)
{
    *ref = KDS_REF(node, next_off[node]);
    next_off[node] += (bytes + 63) & ~63UL;
    return 0;
}

kds_ref kds_land(unsigned node, uint64_t off) { return KDS_REF(node, LAND_OFF + off); }

void *kds_va(kds_ref ref)
{
    return KDS_REF_NODE(ref) == here ? (void *)(SPAN(here) + KDS_REF_OFF(ref)) : 0;
}

void kds_graph_init(struct kds_graph *g, struct kds_task *buf, unsigned cap)
{
    *g = (struct kds_graph){.t = buf, .cap = cap};
}

static int task(struct kds_graph *g, unsigned kind, unsigned node, const uint64_t *a)
{
    struct kds_task *t = &g->t[g->n];
    *t = (struct kds_task){.kind = (uint8_t)kind, .node = (uint8_t)node};
    memcpy(t->a, a, sizeof t->a);
    return (int)g->n++;
}

int kds_cpu(struct kds_graph *g, unsigned node, int64_t (*fn)(const uint64_t *args), uint64_t a0,
            uint64_t a1, uint64_t a2, const int *pred, unsigned npred)
{
    (void)pred;
    (void)npred;
    uint64_t a[4] = {(uint64_t)(uintptr_t)fn, a0, a1, a2};
    return task(g, KDS_CPU, node, a);
}

int kds_move(struct kds_graph *g, kds_ref src, kds_ref dst, uint64_t words, const int *pred,
             unsigned npred)
{
    (void)pred;
    (void)npred;
    uint64_t a[4] = {src, dst, words, 0};
    return task(g, KDS_MOVE, KDS_REF_NODE(src), a);
}

int64_t kds_run(struct kds_graph *g, uint64_t timeout, struct kds_result *r)
{
    (void)timeout;
    unsigned home = here;
    *r = (struct kds_result){0, 0, 0, 0};
    for (unsigned i = 0; i < g->n && !r->status; ++i) {
        const struct kds_task *t = &g->t[i];
        here = t->node;
        if (t->kind == KDS_CPU) {
            int64_t (*fn)(const uint64_t *) = (int64_t (*)(const uint64_t *))(uintptr_t)t->a[0];
            r->detail = fn(&t->a[1]);
            r->status = r->detail ? KOS_EFAULT : KOS_OK;
            r->failed = i;
        }
        else {
            memcpy((void *)(SPAN(KDS_REF_NODE(t->a[1])) + KDS_REF_OFF(t->a[1])),
                   (void *)(SPAN(here) + KDS_REF_OFF(t->a[0])), 32 * t->a[2]);
        }
    }
    here = home;
    return r->status;
}

int main(int argc, char **argv)
{
    nodes = (unsigned)atoi(argv[1]);
    (void)argc;
    for (unsigned j = 0; j < nodes; ++j) {
        uint64_t land = SPAN(j) + LAND_OFF, bytes = KOS_USER_LAND_SIZE;
        void *got = mmap((void *)SPAN(j), LAND_OFF + bytes + PAGE, PROT_READ | PROT_WRITE,
                         MAP_PRIVATE | MAP_ANONYMOUS | MAP_FIXED, -1, 0);
        if (got != (void *)SPAN(j) || mprotect((void *)(land + bytes), PAGE, PROT_NONE)) {
            return 2;
        }
    }
    char *args[] = {"kdsmulti", argv[2], 0};
    printf("rc %d\n", kdsmulti(2, args));
    return 0;
}
