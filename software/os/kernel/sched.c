/* Threads, the run queue, waits, and the trap path. One kernel stack per
 * node; a thread's state is its frame. A wait is a condition the poll loop
 * tests (poll-or-sleep): completions, messages, memory words, time. */
#include "kernel.h"

struct thread kthreads[K_THREADS];
struct thread *kcur;
struct kos_kernel_stat kstat;

static int rq[K_THREADS + 1];
static unsigned rq_head, rq_tail;

static void rq_push(int slot)
{
    rq[rq_tail] = slot;
    rq_tail = (rq_tail + 1) % (K_THREADS + 1);
}

static int rq_pop(void)
{
    if (rq_head == rq_tail) {
        return -1;
    }
    int s = rq[rq_head];
    rq_head = (rq_head + 1) % (K_THREADS + 1);
    return s;
}

static int rq_empty(void) { return rq_head == rq_tail; }

void k_ready(struct thread *t)
{
    t->state = KOS_T_READY;
    t->wkind = W_NONE;
    rq_push((int)t->slot);
}

void k_block(struct thread *t, uint32_t kind, uint64_t a, uint64_t b, uint64_t timeout)
{
    t->state = KOS_T_BLOCKED;
    t->wkind = kind;
    t->wa = a;
    t->wb = b;
    t->deadline = timeout ? hw_time() + timeout : 0;
    t->result = 0;
}

void k_wake(struct thread *t, int64_t result)
{
    if (t->state != KOS_T_BLOCKED) {
        return;
    }
    t->x[10] = (uint64_t)result;
    k_ready(t);
}

struct thread *k_new_thread(uint64_t entry, uint64_t a0, uint64_t a1, const char *name)
{
    struct thread *t = 0;
    for (unsigned i = 0; i < K_THREADS; ++i) {
        if (kthreads[i].state == KOS_T_FREE) {
            t = &kthreads[i];
            break;
        }
    }
    if (!t) {
        return 0;
    }
    unsigned slot = (unsigned)(t - kthreads);
    memset(t, 0, sizeof *t);
    t->slot = slot;
    t->tid = KOS_TID(kn.me, slot);
    t->rjoin_node = -1;
    strncpy(t->name, name ? name : "thread", sizeof t->name - 1);
    if (k_stack_map(t)) {
        t->state = KOS_T_FREE;
        return 0;
    }
    t->x[32] = entry;
    t->x[10] = a0;
    t->x[11] = a1;
    k_ready(t);
    return t;
}

void k_exit_thread(struct thread *t, int64_t code)
{
    t->exit_code = code;
    t->cycles += t->state == KOS_T_RUN ? hw_cycles() - t->since : 0;
    k_stack_unmap(t);
    int taken = 0;
    for (unsigned i = 0; i < K_THREADS; ++i) {
        struct thread *w = &kthreads[i];
        if (w->state == KOS_T_BLOCKED && w->wkind == W_JOIN && w->wa == t->slot) {
            w->x[11] = (uint64_t)code;
            k_wake(w, KOS_OK);
            taken = 1;
        }
    }
    if (t->rjoin_node >= 0) {
        k_reply((unsigned)t->rjoin_node, t->rjoin_req, KOS_OK, (uint64_t)code);
        taken = 1;
    }
    t->state = taken ? KOS_T_FREE : KOS_T_ZOMBIE;
    if (kcur == t) {
        kcur = 0;
    }
}

/* ---- the wait conditions ---- */
static int64_t test_wait(struct thread *t, int *done)
{
    *done = 0;
    switch (t->wkind) {
    case W_MEM:
        if (*(volatile uint64_t *)t->wa != t->wb) {
            *done = 1;
            return KOS_OK;
        }
        break;
    case W_UNIT: {
        struct kunit *u = &kunits[t->wa];
        t->x[11] = u->faults;
        if (u->faults != (uint64_t)t->result) {
            *done = 1;
            return KOS_EFAULT;
        }
        if ((int64_t)(k_unit_retired((unsigned)t->wa) - t->wb) >= 0) {
            *done = 1;
            return KOS_OK;
        }
        break;
    }
    case W_CTRL: {
        struct kunit *u = &kunits[t->wa];
        if (u->ctrl_ready) {
            u->ctrl_ready = 0;
            u->ctrl_busy = 0;
            t->x[11] = u->ctrl_value;
            *done = 1;
            return KOS_OK;
        }
        break;
    }
    case W_RECV: {
        int len = k_chan_take(t);
        if (len != -1) {
            *done = 1;
            return len;
        }
        break;
    }
    case W_SEND:
        if (!k_chan_send(t->wa, t->sendbuf, (unsigned)t->result)) {
            *done = 1;
            return KOS_OK;
        }
        break;
    case W_READ: {
        uint64_t s = hw_rd(HW_R_STDIN);
        if (s & 0x100) {
            uint8_t buf[256];
            uint64_t n = 0, max = t->wb < sizeof buf ? t->wb : sizeof buf;
            while (n < max && (s & 0x100)) {
                buf[n++] = (uint8_t)s;
                hw_wr(HW_R_STDIN, 0);
                s = hw_rd(HW_R_STDIN);
                if (buf[n - 1] == '\n') {
                    break;
                }
            }
            *done = 1;
            return k_copy_out(t->wa, buf, n) ? KOS_EFAULT : (int64_t)n;
        }
        break;
    }
    case W_EVENT: {
        unsigned q = k_chan_count((uint32_t)((uint64_t)t->result >> 32));
        if ((int64_t)(k_events - t->wa) >= 0 || q) {
            *done = 1;
            t->x[11] = k_events;
            int st = k_event_out(t->wb, (uint64_t)t->result & 0xffffffff);
            return st ? st : q;
        }
        break;
    }
    case W_ENGINE: {
        uint64_t c = hw_rd(HW_R_DE_CTR + 8 * (unsigned)t->wa) & 0xfff;
        if (((c - t->wb) & 0xfff) < 0x800) {
            *done = 1;
            return KOS_OK;
        }
        break;
    }
    default:
        break;
    }
    if (t->deadline && (int64_t)(hw_time() - t->deadline) >= 0) {
        *done = 1;
        return t->wkind == W_SLEEP ? KOS_OK : KOS_ETIME;
    }
    return 0;
}

void k_poll(void)
{
    k_units_poll();
    k_msg_poll();
    k_pending_poll();
    for (unsigned i = 0; i < K_THREADS; ++i) {
        struct thread *t = &kthreads[i];
        if (t->state != KOS_T_BLOCKED || t->wkind == W_JOIN || t->wkind == W_REPLY) {
            continue;
        }
        int done;
        int64_t r = test_wait(t, &done);
        if (done) {
            k_wake(t, r);
        }
    }
}

static void arm_timer(void)
{
    if (!kn.tick) {
        return;
    }
    k_l0(KOS_M_SET_TIMER, rq_empty() ? ~0UL : hw_time() + kn.tick, 0, 0, 0);
}

static int idle_marked;

struct thread *k_schedule(void)
{
    for (;;) {
        if (kcur && kcur->state == KOS_T_RUN) {
            return kcur;
        }
        if (kcur && kcur->state == KOS_T_READY) {
            kcur = 0; /* preempted: it is in the queue */
        }
        int s = rq_pop();
        if (s >= 0) {
            struct thread *t = &kthreads[s];
            t->state = KOS_T_RUN;
            t->since = hw_cycles();
            ++t->switches;
            kcur = t;
            arm_timer();
            k_where(KOS_W_USER, t->tid, t->x[32]);
            k_switched();
            idle_marked = 0;
            return t;
        }
        if (!idle_marked) {
            k_where(KOS_W_IDLE, 0, 0);
            idle_marked = 1;
        }
        kcur = 0;
        uint64_t t0 = hw_cycles();
        k_poll();
        ++kstat.idle_polls;
        kstat.idle_cycles += hw_cycles() - t0;
    }
}

void k_panic(const char *fmt, ...)
{
    char b[160];
    va_list ap;
    va_start(ap, fmt);
    kos_vsnprintf(b, sizeof b, fmt, ap);
    va_end(ap);
    k_printf("PANIC node %u: %s\n", kn.me, b);
    k_halt(0xDEAD);
}

void k_kernel_trap(uint64_t cause, uint64_t epc, uint64_t tval)
{
    k_panic("kernel trap cause %lx epc %lx tval %lx", cause, epc, tval);
}

#ifdef KOS_PROF
uint64_t k_prof_at[K_PROF_AT];
/* [0] the caller's stamp (its a0) to the entry, [1] the saves, [2] into
 * k_trap, [3] the system call, [4] the poll, [5] k_schedule, [6] to k_resume */
static uint64_t prof_sum[K_PROF_AT], prof_n, prof_nr = ~0UL;
static int prof_last;

void k_prof_trap(const struct thread *t, uint64_t cause, uint64_t start)
{
    if (prof_last) {
        for (unsigned i = 3; i < K_PROF_AT; ++i) {
            prof_sum[i] += k_prof_at[i] - k_prof_at[i - 1];
        }
    }
    k_prof_at[2] = start;
    prof_last = cause == 8 && t->x[17] == prof_nr;
    if (prof_last) {
        prof_sum[0] += k_prof_at[0] - t->x[10];
        prof_sum[1] += k_prof_at[1] - k_prof_at[0];
        prof_sum[2] += k_prof_at[2] - k_prof_at[1];
        ++prof_n;
    }
}

/* KOS_I_PROF: a < K_PROF_AT a sum, K_PROF_AT the count; 0x100 + nr clears
 * the sums and selects system call nr. */
int64_t k_prof_info(uint64_t a)
{
    if (a >= 0x100) {
        memset(prof_sum, 0, sizeof prof_sum);
        prof_n = 0;
        prof_nr = a - 0x100;
        prof_last = 0;
        return KOS_OK;
    }
    return a < K_PROF_AT ? (int64_t)prof_sum[a] : a == K_PROF_AT ? (int64_t)prof_n : KOS_EINVAL;
}
#endif

void k_trap(uint64_t *frame)
{
    struct thread *t = kcur;
    if (!t || frame != t->x) {
        k_panic("trap with no current thread");
    }
    uint64_t now = hw_cycles();
    t->cycles += now - t->since;
    ++kstat.traps;
    uint64_t cause = csr_read(scause);
#ifdef KOS_PROF
    k_prof_trap(t, cause, now);
#endif
    if (cause & CAUSE_IRQ) {
        unsigned code = (unsigned)(cause & 0x3f);
        if (code == 5) {
            ++kstat.ticks;
            k_l0(KOS_M_SET_TIMER, ~0UL, 0, 0, 0);
            if (!rq_empty()) {
                k_ready(t);
            }
        } else {
            ++kstat.irqs;
        }
        k_poll();
    } else if (cause == 8) {
        t->x[32] += 4;
        ++kstat.syscalls;
        k_syscall(t);
        K_PROF(3);
        if (kn.flags & KOS_F_POLL || t->state != KOS_T_RUN) {
            k_poll();
        }
    } else {
        k_printf("node %u thread %x (%s): fault cause %lx pc %lx tval %lx\n", kn.me, t->tid, t->name,
                 cause, t->x[32], csr_read(stval));
        k_exit_thread(t, -(int64_t)(0x100 + cause));
    }
    K_PROF(4);
    struct thread *n = k_schedule();
    K_PROF(5);
    n->since = hw_cycles();
    k_resume(n);
}
