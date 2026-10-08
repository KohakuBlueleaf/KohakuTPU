/* The queue service: submissions in, completions out, stdio both ways, all
 * through uncached accesses to the queue region. */
#include <ka/boot/args.h>
#include <ka/hal/cpu.h>
#include <ka/hal/mem.h>
#include <ka/hal/node.h>
#include <ka/lib/stdio.h>
#include <ka/os/mem.h>
#include <ka/os/task.h>
#include <ka/package/interp.h>
#include <ka/package/status.h>
#include <ka/queue/layout.h>
#include <ka/queue/service.h>
#include <ka/unit/unit.h>

static struct {
    uint64_t q;
    uint32_t sq_n, cq_n, so_n, si_n;
    uint64_t sq, cq, so, si;
    uint64_t cq_tail, so_wr, so_lost, si_rd, si_wr_seen;
    uint64_t so_word; /* the stdout word being filled, as stored so far */
    uint64_t done;
} Q;

static void fw_state(uint64_t state, uint64_t code)
{
    ka_st64(Q.q + KA_Q_FW, state);
    ka_st64(Q.q + KA_Q_FW + 8, KA_Q_VERSION);
    ka_st64(Q.q + KA_Q_FW + 16, Q.done);
    ka_st64(Q.q + KA_Q_FW + 24, code);
}

void ka_on_fatal(uint64_t code, uint64_t a, uint64_t b)
{
    (void)a;
    (void)b;
    if (Q.q) {
        ka_flush();
        fw_state(KA_Q_ST_FATAL, code);
    }
}

/* stdout: bytes into the ring, whole 8-byte words, then the produced count.
 * A full ring waits for the host one timeout, then drops and counts. */
static void so_sink(const char *buf, unsigned n)
{
    uint64_t rd = ka_ld64(Q.q + KA_Q_SO_RD);
    for (unsigned i = 0; i < n; ++i) {
        if (Q.so_wr - rd >= Q.so_n) {
            uint64_t t0 = ka_cycles();
            while (Q.so_wr - (rd = ka_ld64(Q.q + KA_Q_SO_RD)) >= Q.so_n &&
                   ka_cycles() - t0 < ka_boot.timeout) {
            }
            if (Q.so_wr - rd >= Q.so_n) {
                Q.so_lost += n - i;
                break;
            }
        }
        uint64_t at = Q.so_wr % Q.so_n;
        unsigned lane = at & 7;
        if (!lane) {
            Q.so_word = 0;
        }
        Q.so_word |= (uint64_t)(unsigned char)buf[i] << (8 * lane);
        ka_st64(Q.so + (at & ~7UL), Q.so_word);
        ++Q.so_wr;
    }
    ka_st64(Q.q + KA_Q_SO_WR, Q.so_wr);
    ka_st64(Q.q + KA_Q_SO_WR + 8, Q.so_lost);
}

static int si_source(void)
{
    if (Q.si_rd == Q.si_wr_seen) {
        Q.si_wr_seen = ka_ld64(Q.q + KA_Q_SI_WR);
        if (Q.si_rd == Q.si_wr_seen) {
            return -1;
        }
    }
    uint64_t at = Q.si_rd % Q.si_n;
    int c = (int)((ka_ld64(Q.si + (at & ~7UL)) >> (8 * (at & 7))) & 0xff);
    ++Q.si_rd;
    ka_st64(Q.q + KA_Q_SI_RD, Q.si_rd);
    return c;
}

static void push_cq(uint64_t tag, uint64_t status, uint64_t detail, uint64_t step,
                    uint64_t cycles, uint64_t value)
{
    while (Q.cq_tail - ka_ld64(Q.q + KA_Q_CQ_HEAD) >= Q.cq_n) {
        ka_yield();
    }
    uint64_t e = Q.cq + (Q.cq_tail % Q.cq_n) * KA_Q_CQ_BYTES;
    ka_st64(e, tag);
    ka_st64(e + 8, (status & 0xffff) | (detail & 0xffff) << 16 | step << 32);
    ka_st64(e + 16, cycles);
    ka_st64(e + 24, value);

    ++Q.cq_tail;
    ka_st64(Q.q + KA_Q_CQ_TAIL, Q.cq_tail);
}

static uint64_t run_tag;

static void on_signal(void *ctx, uint32_t step, uint32_t v32, uint64_t v64)
{
    (void)ctx;
    ka_flush();
    push_cq(run_tag, KA_ST_PROGRESS, v32, step, ka_cycles(), v64);
}

static int setup(void)
{
    Q.q = ka_boot.queue;
    if (ka_ld64(Q.q + KA_Q_INFO) != KA_Q_MAGIC ||
        ka_ld64(Q.q + KA_Q_INFO + 8) != KA_Q_VERSION) {
        ka_printf("[ka] no queue at %lx\n", Q.q);
        return 1;
    }
    uint64_t g = ka_ld64(Q.q + KA_Q_INFO + 16), s = ka_ld64(Q.q + KA_Q_INFO + 24);
    Q.sq_n = g & 0xffff;
    Q.cq_n = (g >> 16) & 0xffff;
    Q.so_n = (uint32_t)s;
    Q.si_n = (uint32_t)(s >> 32);
    if (!Q.sq_n || !Q.cq_n || !Q.so_n || !Q.si_n || (Q.so_n & 31) || (Q.si_n & 31)) {
        ka_printf("[ka] bad queue geometry %lx %lx\n", g, s);
        return 2;
    }
    Q.sq = Q.q + KA_Q_SQ;
    Q.cq = Q.sq + (uint64_t)Q.sq_n * KA_Q_SQ_BYTES;
    Q.so = Q.cq + (uint64_t)Q.cq_n * KA_Q_CQ_BYTES;
    Q.si = Q.so + Q.so_n;

    /* Resume the firmware-owned indices from the region. */
    Q.cq_tail = ka_ld64(Q.q + KA_Q_CQ_TAIL);
    Q.so_wr = ka_ld64(Q.q + KA_Q_SO_WR);
    Q.so_lost = ka_ld64(Q.q + KA_Q_SO_WR + 8);
    Q.si_rd = Q.si_wr_seen = ka_ld64(Q.q + KA_Q_SI_RD);
    if (Q.so_wr & 7) {
        Q.so_word = ka_ld64(Q.so + ((Q.so_wr % Q.so_n) & ~7UL));
    }
    Q.done = 0;
    return 0;
}

/* Region r's line: free bytes, largest free block, live | table entries << 32, failures. */
static void heap_line(unsigned r)
{
    struct ka_heap_stats s = {0};
    struct ka_heap *h = ka_mem_heap(r);
    if (h) {
        ka_heap_stats(h, &s);
    }
    uint64_t at = Q.q + KA_Q_HEAPS + 32 * r;
    ka_st64(at, s.free);
    ka_st64(at + 8, s.largest);
    ka_st64(at + 16, s.live | (uint64_t)s.blocks << 32);
    ka_st64(at + 24, s.fails);
}

/* HEAP / ALLOC / FREE: status, and ALLOC's address in *value. */
static int heap_op(unsigned op, const uint64_t w[8], uint64_t *value)
{
    unsigned r = (unsigned)w[2];
    int rc;
    *value = 0;
    if (op == KA_SQ_HEAP) {
        rc = ka_mem_configure(r, w[3], w[4], w[5] ? w[5] : 64);
    } else if (op == KA_SQ_ALLOC) {
        rc = ka_mem_alloc(r, w[3], w[4], (uint32_t)w[5], value);
    } else {
        rc = ka_mem_free(r, w[3]);
    }
    if (r < KA_MEM_REGIONS) {
        heap_line(r);
    }
    return rc;
}

static int sq_moved(void *ctx) { return *(uint64_t *)ctx != ka_ld64(Q.q + KA_Q_SQ_TAIL); }

static void banner(void)
{
    unsigned n;
    const struct ka_unit_class *c = ka_unit_classes(&n);
    ka_printf("[ka] node %lu ready: queue %lx sq %u cq %u, %lu units, sig %lx, classes",
              (uint64_t)ka_boot.mesh, Q.q, Q.sq_n, Q.cq_n, (uint64_t)ka_boot.nunits,
              ka_machine_signature());
    for (unsigned i = 0; i < n; ++i) {
        ka_printf(" %s", c[i].name);
    }
    ka_printf("; boot cycles: main at %lu, ready at %lu\n", ka_boot_main_cycle, ka_cycles());
}

uint64_t ka_queue_serve(void)
{
    if (setup()) {
        return KA_EXIT_BOOTARGS | 0x10;
    }
    ka_set_sink(so_sink);
    ka_set_source(si_source);
    ka_package_boot();
    /* The units this node serves, given or found, before it says READY. */
    for (unsigned i = 0; i < ka_boot.nunits; ++i) {
        ka_st64(Q.q + KA_Q_UNITS + 0x20 + 8 * i, ka_boot.units[i]);
    }
    ka_st64(Q.q + KA_Q_UNITS, ka_boot.nunits);
    for (unsigned r = 0; r < KA_MEM_REGIONS; ++r) {
        heap_line(r);
    }
    fw_state(KA_Q_ST_READY, 0);
    banner();
    uint64_t head = ka_ld64(Q.q + KA_Q_SQ_HEAD);
    for (;;) {
        if (head == ka_ld64(Q.q + KA_Q_SQ_TAIL)) {
            ka_wait(sq_moved, &head, 0);
            continue;
        }
        uint64_t e = Q.sq + (head % Q.sq_n) * KA_Q_SQ_BYTES, w[8];
        for (int k = 0; k < 8; ++k) {
            w[k] = ka_ld64(e + 8 * k);
        }
        unsigned op = w[0] & 0xff;
        run_tag = w[1];
        if (op == KA_SQ_RUN) {
            struct ka_pkg_run r = {
                .pkg = w[2],
                .bytes = w[3],
                .bind = w[4],
                .nbind = (unsigned)((w[0] >> 16) & 0xffff),
                .timeout = w[5],
                .flags = (unsigned)w[6],
                .signal = on_signal,
                .ctx = 0,
            };
            struct ka_pkg_result res;
            ka_package_run(&r, &res);
            ka_flush();
            push_cq(w[1], (uint64_t)res.status, res.detail, res.step, res.cycles,
                    res.status ? res.value : res.sent);
            if (r.flags & KA_RUN_F_IRQ) {
                ka_ctrl_wr(KA_R_IRQ, 1);
            }
            if (res.status) {
                ka_printf("[ka] package %lx failed: status %x detail %x step %u value %lx\n",
                          w[1], res.status, res.detail, res.step, res.value);
            }
        } else if (op == KA_SQ_NOP) {
            push_cq(w[1], KA_ST_OK, 0, 0, 0, 0);
        } else if (op == KA_SQ_HEAP || op == KA_SQ_ALLOC || op == KA_SQ_FREE) {
            uint64_t t0 = ka_cycles(), value;
            int rc = heap_op(op, w, &value);
            push_cq(w[1], (uint64_t)rc, w[2] & 0xffff, 0, ka_cycles() - t0, value);
        } else if (op == KA_SQ_STOP) {
            struct ka_task *me = ka_task_self();
            struct ka_sched_stats ss;
            ka_sched_stats(&ss);
            ka_printf("[ka] stop: %lu entries, task stack %u B used, %lu switches, "
                      "%lu idle passes\n",
                      Q.done, me ? ka_task_stack_used(me) : 0, ss.switches, ss.idle_passes);
            push_cq(w[1], KA_ST_OK, 0, 0, 0, 0);
            ++head;
            ka_st64(Q.q + KA_Q_SQ_HEAD, head);
            ka_flush();
            fw_state(KA_Q_ST_STOPPED, 0);
            return KA_EXIT_STOP | (w[2] & 0xffffffffUL);
        } else {
            push_cq(w[1], KA_ST_BAD_OP, op, 0, 0, 0);
        }
        ++Q.done;
        ka_st64(Q.q + KA_Q_FW + 16, Q.done);
        ++head;
        ka_st64(Q.q + KA_Q_SQ_HEAD, head);
    }
}
