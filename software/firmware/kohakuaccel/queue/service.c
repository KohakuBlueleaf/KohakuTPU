/* The queue service: submissions in, completions out, stdio both ways, all
 * through uncached accesses to the queue region. */
#include <ka/boot/args.h>
#include <ka/hal/cpu.h>
#include <ka/hal/mem.h>
#include <ka/hal/node.h>
#include <ka/lib/stdio.h>
#include <ka/os/task.h>
#include <ka/package/debug.h>
#include <ka/package/interp.h>
#include <ka/package/status.h>
#include <ka/queue/layout.h>
#include <ka/queue/service.h>
#include <ka/unit/unit.h>

static struct {
    uint64_t q;
    uint32_t sq_n, cq_n, so_n, si_n;
    uint64_t sq, cq, so, si, rec;
    uint64_t cq_tail, so_wr, so_lost, si_rd, si_wr_seen;
    uint64_t so_word; /* the stdout word being filled, as stored so far */
    uint64_t done, epoch, list_index, item_tag;
} Q;

static void fw_state(uint64_t state, uint64_t code)
{
    uint64_t q = Q.q ? Q.q : ka_boot.queue;
    if (!q || (q & (KA_Q_LINE - 1))) {
        return;
    }
    ka_st64(q + KA_Q_FW + 8 * KA_QF_ABI, KA_Q_VERSION);
    ka_st64(q + KA_Q_FW + 8 * KA_QF_DONE, Q.done);
    ka_st64(q + KA_Q_FW + 8 * KA_QF_FATAL, code);
    ka_st64(q + KA_Q_LIST_ABI, KA_Q_LIST_VERSION);
    ka_st64(q + KA_Q_FW + 8 * KA_QF_STATE, state);
}

void ka_on_fatal(uint64_t code, uint64_t a, uint64_t b)
{
    (void)a;
    (void)b;
    if (Q.q) {
        ka_flush();
    }
    fw_state(KA_Q_ST_FATAL, code);
}

/* stdout: bytes into the ring, whole 8-byte words, then the produced count.
 * A full ring waits for the host one timeout, then drops and counts. */
static void so_sink(const char *buf, unsigned n)
{
    uint64_t rd = ka_ld64(Q.q + KA_Q_SO_RD);
    /* One divide a write, not one a byte (66 cycles each). */
    uint64_t at = Q.so_wr % Q.so_n;
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
        unsigned lane = at & 7;
        if (!lane) {
            Q.so_word = 0;
        }
        Q.so_word |= (uint64_t)(unsigned char)buf[i] << (8 * lane);
        ka_st64(Q.so + (at & ~7UL), Q.so_word);
        ++Q.so_wr;
        if (++at == Q.so_n) {
            at = 0;
        }
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

_Static_assert(KA_QR_IDLE == KA_QR_HEAD + 8 * KA_PT_IDLE, "record words follow KA_PT_*");
_Static_assert(KA_QR_HEAD + 8 * KA_PT_N <= KA_Q_REC_BYTES, "a record holds its words");
_Static_assert(KA_Q_EPOCH == KA_Q_INFO + 8 * KA_QI_EPOCH, "epoch offset matches INFO word");
_Static_assert(KA_Q_EPOCH == KA_Q_INFO + KA_Q_LINE / 2, "epoch sits at INFO half-line");
_Static_assert(KA_Q_LIST_ABI == KA_Q_FW + KA_Q_LINE / 2, "list ABI sits at FW half-line");

/* The completion's record (L0.NQ.056), then its CQ entry, then the tail.
 * `time` is a RUN's timing words, or 0. */
static void push_cq(uint64_t tag, uint64_t status, uint64_t detail, uint64_t step,
                    uint64_t cycles, uint64_t value, const uint64_t *time)
{
    while (Q.cq_tail - ka_ld64(Q.q + KA_Q_CQ_HEAD) >= Q.cq_n) {
        ka_yield();
    }
    uint64_t slot = Q.cq_tail % Q.cq_n;
    uint64_t rec = Q.rec + slot * KA_Q_REC_BYTES;
    ka_st64(rec + KA_QR_TAG, tag);
    ka_st64(rec + KA_QR_SO_WR, Q.so_wr);
    for (unsigned i = 0; i < KA_PT_N; ++i) {
        ka_st64(rec + KA_QR_HEAD + 8 * i, time ? time[i] : 0);
    }
    ka_st64(rec + KA_QR_LIST_INDEX, Q.list_index);
    ka_st64(rec + KA_QR_ITEM_TAG, Q.item_tag);
    ka_st64(rec + KA_QR_EPOCH, Q.epoch);
    uint64_t e = Q.cq + slot * KA_Q_CQ_BYTES;
    ka_st64(e + KA_CQ_TAG, tag);
    ka_st64(e + KA_CQ_STATUS,
            (status & KA_CQ_STATUS_MASK) | (detail & KA_CQ_STATUS_MASK) << 16 | step << 32);
    ka_st64(e + KA_CQ_CYCLES, cycles);
    ka_st64(e + KA_CQ_VALUE, value);

    ++Q.cq_tail;
    ka_st64(Q.q + KA_Q_CQ_TAIL, Q.cq_tail);
}

static uint64_t run_tag;

static uint64_t *entry(uint64_t at, uint64_t w[KA_SQ_WORDS])
{
    uint64_t e = Q.sq + (at % Q.sq_n) * KA_Q_SQ_BYTES;
    for (int k = 0; k < KA_SQ_WORDS; ++k) {
        w[k] = ka_ld64(e + 8 * k);
    }
    return w;
}

static uint64_t head;

/* The entry after the running one, when the host has queued it (L0.NQ.053). */
static uint64_t peek(uint64_t w[KA_SQ_WORDS])
{
    if (head + 1 == ka_ld64(Q.q + KA_Q_SQ_TAIL)) {
        return ~0UL;
    }
    entry(head + 1, w);
    return head + 1;
}

static void on_signal(void *ctx, uint32_t step, uint32_t v32, uint64_t v64)
{
    (void)ctx;
    ka_flush();
    push_cq(run_tag, KA_ST_PROGRESS, v32, step, ka_cycles(), v64, 0);
}

static int setup(void)
{
    uint64_t region_size = 0;
    uint64_t q = ka_boot_region(KA_REGION_QUEUE, &region_size);
#ifndef KA_SPEC_MEMORY_MUTANT
    if (!region_size || q != ka_boot.queue || region_size < KA_Q_SQ || (q & (KA_Q_LINE - 1)))
        return 3;
#endif
    if (ka_ld64(q + KA_Q_INFO) != KA_Q_MAGIC ||
        ka_ld64(q + KA_Q_INFO + 8) != KA_Q_VERSION) {
        ka_printf("[ka] no queue at %lx\n", q);
        return 1;
    }
    uint64_t g = ka_ld64(q + KA_Q_INFO + 16), s = ka_ld64(q + KA_Q_INFO + 24);
    Q.sq_n = g & 0xffff;
    Q.cq_n = (g >> 16) & 0xffff;
    Q.so_n = (uint32_t)s;
    Q.si_n = (uint32_t)(s >> 32);
    if (!Q.sq_n || !Q.cq_n || !Q.so_n || !Q.si_n || (Q.so_n & (KA_Q_LINE - 1)) ||
        (Q.si_n & (KA_Q_LINE - 1))) {
        ka_printf("[ka] bad queue geometry %lx %lx\n", g, s);
        return 2;
    }
    Q.sq = q + KA_Q_SQ;
    Q.cq = Q.sq + (uint64_t)Q.sq_n * KA_Q_SQ_BYTES;
    /* The rings start on a line: the host writes the stdin ring in whole lines. */
    Q.so = (Q.cq + (uint64_t)Q.cq_n * KA_Q_CQ_BYTES + KA_Q_LINE - 1) & ~(uint64_t)(KA_Q_LINE - 1);
    Q.si = Q.so + Q.so_n;
    Q.rec = Q.si + Q.si_n;
#ifndef KA_SPEC_MEMORY_MUTANT
    if (Q.rec - q + (uint64_t)Q.cq_n * KA_Q_REC_BYTES > region_size)
        return 3;
#endif
    Q.q = q;
    Q.epoch = ka_ld64(q + KA_Q_EPOCH);

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

/* HOSTL ABI 1 base mode: FIFO RUN items, one terminal CQ acknowledgment. */
static void run_list(const uint64_t *w)
{
    uint64_t at = w[KA_SQ_LIST_ADDR], count = w[KA_SQ_LIST_COUNT];
    uint64_t epoch = w[KA_SQ_LIST_EPOCH], version = w[KA_SQ_LIST_VERSION];
    struct ka_pkg_result res = {0};
    uint64_t time[KA_PT_N] = {0}, cycles = 0, sent = 0;
    unsigned irq = 0;
    if (epoch != Q.epoch || version != KA_Q_LIST_VERSION || w[KA_SQ_LIST_FLAGS]) {
        push_cq(run_tag, KA_ST_BAD_ARG, epoch != Q.epoch ? 1 : 2, 0, 0, epoch, 0);
        return;
    }
    ka_package_ahead_reset();
    for (uint64_t i = 0; i < count; ++i) {
        uint64_t item[KA_SQ_WORDS];
        for (unsigned k = 0; k < KA_SQ_WORDS; ++k)
            item[k] = ka_ld64(at + i * KA_Q_SQ_BYTES + 8 * k);
        Q.list_index = i;
        Q.item_tag = item[KA_SQ_RUN_TAG];
        unsigned op = (unsigned)(item[KA_SQ_RUN_OP] & KA_SQ_OP_MASK);
        if (op != KA_SQ_RUN) {
            res = (struct ka_pkg_result){.status = KA_ST_BAD_OP, .detail = op};
        } else {
            struct ka_pkg_run r = {
                .pkg = item[KA_SQ_RUN_PKG], .bytes = item[KA_SQ_RUN_BYTES],
                .bind = item[KA_SQ_RUN_BIND],
                .nbind = (unsigned)((item[KA_SQ_RUN_OP] >> KA_SQ_NBIND_SHIFT) & 0xffff),
                .timeout = item[KA_SQ_RUN_TIMEOUT], .flags = (unsigned)item[KA_SQ_RUN_FLAGS],
                .signal = on_signal, .ctx = 0, .peek = 0,
            };
#ifdef KA_DEBUG
            ka_dbg_begin(item[KA_SQ_RUN_TAG], r.flags);
#endif
            irq |= r.flags & KA_RUN_F_IRQ;
            ka_package_run(&r, &res);
            cycles += res.cycles;
            sent += res.sent;
            for (unsigned k = 0; k < KA_PT_N; ++k)
                time[k] += res.time[k];
        }
        if (res.status) {
            ka_printf("[ka] package %lx failed: status %x detail %x step %u value %lx\n",
                      Q.item_tag, res.status, res.detail, res.step, res.value);
            break;
        }
        Q.list_index = i + 1;
    }
    ka_package_ahead_reset();
    ka_flush();
    push_cq(run_tag, res.status, res.detail, res.step, cycles,
            res.status ? res.value : sent, time);
    if (irq)
        ka_ctrl_wr(KA_R_IRQ, 1);
}

uint64_t ka_queue_serve(void)
{
    if (setup()) {
        ka_on_fatal(KA_EXIT_BOOTARGS | KA_BOOT_ERR_QUEUE, 0, 0);
        return KA_EXIT_BOOTARGS | KA_BOOT_ERR_QUEUE;
    }
    ka_set_sink(so_sink);
    ka_set_source(si_source);
    ka_package_boot();
#ifdef KA_DEBUG
    ka_dbg_boot();
#endif
    /* The units this node serves, given or found, before it says READY. */
    for (unsigned i = 0; i < ka_boot.nunits; ++i) {
        ka_st64(Q.q + KA_Q_UNITS + KA_Q_LINE + 8 * i, ka_boot.units[i]);
    }
    ka_st64(Q.q + KA_Q_UNITS, ka_boot.nunits);
    fw_state(KA_Q_ST_READY, 0);
    banner();
    head = ka_ld64(Q.q + KA_Q_SQ_HEAD);
    for (;;) {
        if (head == ka_ld64(Q.q + KA_Q_SQ_TAIL)) {
            ka_wait(sq_moved, &head, 0);
            continue;
        }
        /* `w` may be the read-ahead copy, which the run overwrites: past
         * `ka_package_run` the tag is `run_tag`. */
        uint64_t buf[KA_SQ_WORDS];
        const uint64_t *w = ka_package_ahead(head);
        w = w ? w : entry(head, buf);
        unsigned op = w[0] & KA_SQ_OP_MASK;
        run_tag = w[1];
        Q.epoch = ka_ld64(Q.q + KA_Q_EPOCH);
        Q.list_index = Q.item_tag = 0;
        if (op == KA_SQ_RUN) {
            struct ka_pkg_run r = {
                .pkg = w[2],
                .bytes = w[3],
                .bind = w[4],
                .nbind = (unsigned)((w[0] >> KA_SQ_NBIND_SHIFT) & 0xffff),
                .timeout = w[5],
                .flags = (unsigned)w[6],
                .signal = on_signal,
                .ctx = 0,
                .peek = (ka_boot.flags & KA_BOOT_F_SERIALQ) ? 0 : peek,
            };
#ifdef KA_DEBUG
            ka_dbg_begin(w[1], (unsigned)w[6]);
#endif
            struct ka_pkg_result res;
            ka_package_run(&r, &res);
            /* Every report line before the completion, so the record's stdout
             * count ends this run's text (DBG.FAIL.002, L0.NQ.052). */
            if (res.status) {
                ka_printf("[ka] package %lx failed: status %x detail %x step %u value %lx\n",
                          run_tag, res.status, res.detail, res.step, res.value);
            }
            ka_flush();
            push_cq(run_tag, (uint64_t)res.status, res.detail, res.step, res.cycles,
                    res.status ? res.value : res.sent, res.time);
            if (r.flags & KA_RUN_F_IRQ) {
                ka_ctrl_wr(KA_R_IRQ, 1);
            }
        } else if (op == KA_SQ_LIST) {
            run_list(w);
        } else if (op == KA_SQ_NOP) {
            push_cq(w[1], KA_ST_OK, 0, 0, 0, 0, 0);
        } else if (op == KA_SQ_STOP) {
            struct ka_task *me = ka_task_self();
            struct ka_sched_stats ss;
            ka_sched_stats(&ss);
            ka_printf("[ka] stop: %lu entries, task stack %u B used, %lu switches, "
                      "%lu idle passes\n",
                      Q.done, me ? ka_task_stack_used(me) : 0, ss.switches, ss.idle_passes);
            ka_flush();
            push_cq(w[1], KA_ST_OK, 0, 0, 0, 0, 0);
            ++head;
            ka_st64(Q.q + KA_Q_SQ_HEAD, head);
            ka_flush();
            fw_state(KA_Q_ST_STOPPED, 0);
            return KA_EXIT_STOP | (w[2] & 0xffffffffUL);
        } else {
#ifdef KA_SPEC_MEMORY_MUTANT
            push_cq(w[1], op >= 3 && op <= 5 ? KA_ST_OK : KA_ST_BAD_OP, op, 0, 0, 0, 0);
#else
            push_cq(w[1], KA_ST_BAD_OP, op, 0, 0, 0, 0);
#endif
        }
        ++Q.done;
        ka_st64(Q.q + KA_Q_FW + 8 * KA_QF_DONE, Q.done);
        ++head;
        ka_st64(Q.q + KA_Q_SQ_HEAD, head);
    }
}
