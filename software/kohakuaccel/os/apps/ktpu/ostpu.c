/* Resident host graphs, one mailbox submission for N executions through KDS. */
#include "../kds/kdsapp.h"

#define MAILBOX KDS_LZ_USER

_Static_assert(sizeof(struct kds_task) == OSTPU_TASK_BYTES, "task size");
_Static_assert(offsetof(struct kds_task, pred) == KDS_TASK_OFF_PRED, "task pred offset");
_Static_assert(offsetof(struct kds_task, a) == KDS_TASK_OFF_WORDS, "task words offset");
_Static_assert(offsetof(struct kds_task, patch) == KDS_TASK_OFF_PATCH, "task patch offset");
_Static_assert(sizeof(struct kds_patch) == KDS_PATCH_BYTES, "patch size");
_Static_assert(offsetof(struct ostpu_mailbox, magic) == OSTPU_OFF_READY, "ready granule");
_Static_assert(offsetof(struct ostpu_mailbox, request) == OSTPU_OFF_REQ, "request granule");
_Static_assert(offsetof(struct ostpu_mailbox, op) == OSTPU_OFF_CMD, "command granule");
_Static_assert(offsetof(struct ostpu_mailbox, done) == OSTPU_OFF_DONE, "completion granule");
_Static_assert(offsetof(struct ostpu_mailbox, status) == OSTPU_OFF_STATUS, "status offset");
_Static_assert(offsetof(struct ostpu_mailbox, completed) == OSTPU_OFF_CAP, "capacity offset");
_Static_assert(offsetof(struct ostpu_mailbox, stat) == OSTPU_OFF_STAT, "statistics offset");
_Static_assert(sizeof(struct kos_launch_rec) == KOS_LRING_REC_BYTES, "launch ring record size");

static int64_t ostpu_cpu_rw(const uint64_t *a)
{
    volatile uint64_t *dst = kds_va(a[0]);
    if (!dst) {
        return KOS_EINVAL;
    }
    if (!a[1]) {
        for (unsigned i = 0; i < 4; ++i) {
            dst[i] = a[2] + i;
        }
        return 0;
    }
    const volatile uint64_t *src = kds_va(a[1]);
    if (!src) {
        return KOS_EINVAL;
    }
    for (unsigned i = 0; i < 4; ++i) {
        dst[i] = src[i] ^ a[2];
    }
    return 0;
}

static int ostpu(int argc, char **argv)
{
    uint64_t num[3] = {256, 4096, 0};
    unsigned nn;
    unsigned flags = kds_args(argc, argv, num, 3, &nn);
    if (!num[0] || num[0] > KDS_MAXTASKS || !num[1]) {
        return KOS_EINVAL;
    }
    volatile struct ostpu_mailbox *m = (volatile void *)((uintptr_t)kos_land() + MAILBOX);
    memset((void *)m, 0, sizeof *m);
    int st = kds_start(flags, KDS_ARENA, KDS_ALL_NODES);
    if (st) {
        return st;
    }
    kds_ref tasks, data;
    st = (int)kds_alloc(kos_node(), num[0] * sizeof(struct kds_task), &tasks);
    if (st) {
        kds_stop();
        return st;
    }
    st = (int)kds_alloc(kos_node(), num[1], &data);
    if (st) {
        kds_free(tasks);
        kds_stop();
        return st;
    }
    struct kds_graph g;
    kds_graph_init(&g, kds_va(tasks), (unsigned)num[0]);
    m->tasks_pa = kds_pa(tasks);
    m->data_pa = kds_pa(data);
    m->data_ref = data;
    m->task_cap = num[0];
    m->data_bytes = num[1];
    m->dpad[0] = (uint64_t)(uintptr_t)ostpu_cpu_rw;
    kos_release();
    __asm__ volatile("fence rw, rw" ::: "memory");
    m->magic = OSTPU_MAGIC;
    uint64_t seen = 0, ret_epoch = 0, ret_tasks = 0;
    for (;;) {
        while (m->request == seen) {
            kos_yield();
        }
        seen = m->request;
        __asm__ volatile("fence rw, rw" ::: "memory");
        if (m->op == OSTPU_OP_STOP) {
            break;
        }
        struct kds_result r = {0};
        struct kds_server_stat a, b;
        uint64_t completed = 0, cycles = 0;
        if (m->op != OSTPU_OP_RUN || !m->tasks || m->tasks > g.cap || !m->repeats) {
            r.status = KOS_EINVAL;
        } else {
            uint64_t ep = m->rpad[OSTPU_REQ_EPOCH_WORD - 1];
            int hit = num[2] && ep && ep == ret_epoch && m->tasks == ret_tasks;
#ifdef KOS_LAUNCH_RING
            uint64_t t_step0 = kos_cycles();
            kds_lring(KOS_LRING_STEP_SLOT)->cyc_walk = 0;
#endif
            if (!hit) {
                kos_acquire();
#ifdef KOS_LAUNCH_RING
                kds_lring(KOS_LRING_STEP_SLOT)->cyc_walk = kos_cycles() - t_step0;
#endif
            }
            g.n = (unsigned)m->tasks;
            if (num[2] && !hit) {
                kds_retain(&g);
                ret_epoch = ep;
                ret_tasks = m->tasks;
#ifdef KOS_LAUNCH_RING
                t_step0 = kos_cycles();
#endif
            }
            if (m->rpad[OSTPU_REQ_WRITE_REF_WORD - 1]) {
                uint64_t *p = kds_va(m->rpad[OSTPU_REQ_WRITE_REF_WORD - 1]);
                if (p) {
                    for (unsigned i = 0; i < 4; ++i) {
                        p[i] = m->rpad[OSTPU_REQ_WRITE_VAL_WORD - 1] + i;
                    }
                }
            }
#ifdef OSTPU_PROFILE_SYS
            kos_info_of(KOS_I_PROF, 0x100 + OSTPU_PROFILE_SYS);
#endif
            kds_server_stat(&a);
            for (; completed < m->repeats; ++completed) {
                kds_run(&g, m->timeout, &r);
                cycles += r.cycles;
                if (r.status) {
                    break;
                }
            }
#ifdef KOS_LAUNCH_RING
            kds_lring(KOS_LRING_STEP_SLOT)->cyc_total = kos_cycles() - t_step0;
#endif
            kds_server_stat(&b);
#ifdef OSTPU_PROFILE_SYS
            volatile uint64_t *profile = (volatile uint64_t *)((uintptr_t)m + OSTPU_OFF_PROF);
            for (unsigned i = 0; i < OSTPU_PROF_WORDS; ++i) {
                profile[i] = (uint64_t)kos_info_of(KOS_I_PROF, i);
            }
#endif
            if (num[2] && (!ep || r.status)) {
                kds_retain(0);
                ret_epoch = 0;
            }
            const uint64_t *ap = (const uint64_t *)&a, *bp = (const uint64_t *)&b;
            volatile uint64_t *out = (volatile uint64_t *)&m->stat;
            for (unsigned i = 0; i < sizeof a / 8; ++i) {
                out[i] = bp[i] - ap[i];
            }
        }
        m->status = r.status;
        m->failed = r.failed;
        m->detail = (uint64_t)r.detail;
        m->cycles = cycles;
        m->completed = completed;
        m->failed_repeat = completed;
        __asm__ volatile("fence rw, rw" ::: "memory");
        m->done = seen;
    }
    if (num[2] && ret_epoch) {
        kds_retain(0);
    }
    kds_free(data);
    kds_free(tasks);
    st = kds_stop();
    m->status = st;
    __asm__ volatile("fence rw, rw" ::: "memory");
    m->done = seen;
    return st;
}
KOS_APP("ostpu", ostpu);
