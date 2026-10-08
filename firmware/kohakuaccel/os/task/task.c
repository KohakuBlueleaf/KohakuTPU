/* Tasks and the run queue (ka/os/task.h). The switch is switch_rv64.S. */
#include <ka/hal/cpu.h>
#include <ka/lib/stdio.h>
#include <ka/os/task.h>
#include <ka/package/status.h>

#define CANARY 0x4B41535441434B21UL /* "!KCATSAK" */
#define FILL   0x5A5A5A5A5A5A5A5AUL
#define FRAME_WORDS 14 /* ra, s0-s11, pad: switch_rv64.S */

void ka_ctx_switch(uint64_t *save_sp, uint64_t load_sp);
void ka_task_trampoline(void);

static struct ka_task *head, *tail, *cur, *rr;
static uint64_t sched_sp;
static struct ka_sched_stats st;

int ka_task_spawn(struct ka_task *t, const char *name, ka_task_fn fn, void *arg,
                  void *stack, uint32_t stack_bytes)
{
    if (!t || !fn || !stack || ((uintptr_t)stack & 15) || stack_bytes < KA_TASK_MIN_STACK) {
        return KA_ST_BAD_ARG;
    }
    uint32_t words = (stack_bytes & ~15u) / 8;
    uint64_t *s = (uint64_t *)stack;
    s[0] = CANARY;
    for (uint32_t i = 1; i < words; ++i) {
        s[i] = FILL;
    }
    uint64_t *frame = s + words - FRAME_WORDS;
    frame[0] = (uint64_t)(uintptr_t)ka_task_trampoline; /* ra */
    frame[1] = (uint64_t)(uintptr_t)t;                  /* s0: the trampoline's argument */
    *t = (struct ka_task){
        .sp = (uint64_t)(uintptr_t)frame,
        .fn = fn,
        .arg = arg,
        .stack = s,
        .stack_words = words,
        .name = name,
        .state = KA_TASK_READY,
    };
    if (tail) {
        tail->next = t;
    } else {
        head = t;
    }
    tail = t;
    ++st.spawned;
    return 0;
}

/* The first switch into a task returns here, through the trampoline. */
void ka_task_main(struct ka_task *t) __attribute__((noreturn));
void ka_task_main(struct ka_task *t) { ka_task_exit(t->fn(t->arg)); }

static void to_scheduler(void) { ka_ctx_switch(&cur->sp, sched_sp); }

void ka_task_exit(int code)
{
    if (!cur) {
        ka_exit((uint64_t)(uint32_t)code);
    }
    cur->code = code;
    cur->state = KA_TASK_DONE;
    to_scheduler();
    for (;;) {
    }
}

void ka_yield(void)
{
    if (cur) {
        to_scheduler();
    }
}

int ka_wait(ka_ready_fn ready, void *ctx, uint64_t timeout)
{
    if (ready && ready(ctx)) {
        return 0;
    }
    uint64_t t0 = ka_cycles();
    if (!cur) {
        while (!(ready && ready(ctx))) {
            if (timeout && ka_cycles() - t0 >= timeout) {
                return KA_ST_TIMEOUT;
            }
        }
        return 0;
    }
    cur->ready = ready;
    cur->ctx = ctx;
    cur->since = t0;
    cur->timeout = timeout;
    cur->state = KA_TASK_BLOCKED;
    to_scheduler();
    return cur->woke;
}

void ka_sleep(uint64_t cycles)
{
    if (cycles) {
        ka_wait(0, 0, cycles);
    }
}

struct ka_task *ka_task_self(void) { return cur; }

/* READY, or BLOCKED with its wait over (which makes it READY). */
static int runnable(struct ka_task *t)
{
    if (t->state == KA_TASK_READY) {
        return 1;
    }
    if (t->state != KA_TASK_BLOCKED) {
        return 0;
    }
    if (t->ready && t->ready(t->ctx)) {
        t->woke = 0;
    } else if (t->timeout && ka_cycles() - t->since >= t->timeout) {
        t->woke = KA_ST_TIMEOUT;
    } else {
        return 0;
    }
    t->state = KA_TASK_READY;
    return 1;
}

static struct ka_task *pick(void)
{
    struct ka_task *start = rr ? rr : head, *t = start;
    do {
        if (runnable(t)) {
            return t;
        }
        t = t->next ? t->next : head;
    } while (t != start);
    return 0;
}

static void unlink(struct ka_task *t)
{
    struct ka_task *p = 0;
    for (struct ka_task *q = head; q && q != t; q = q->next) {
        p = q;
    }
    if (p) {
        p->next = t->next;
    } else {
        head = t->next;
    }
    if (tail == t) {
        tail = p;
    }
}

uint32_t ka_sched_run(void)
{
    uint32_t done = 0;
    if (cur) {
        return 0; /* one run queue: a task cannot start a nested one */
    }
    rr = 0;
    while (head) {
        struct ka_task *t = pick();
        if (!t) {
            ++st.idle_passes;
            continue;
        }
        cur = t;
        ++t->runs;
        ++st.switches;
        ka_ctx_switch(&sched_sp, t->sp);
        cur = 0;
        if (t->stack[0] != CANARY) {
            ka_printf("\n[ka] task %s overran its stack\n", t->name ? t->name : "?");
            ka_on_fatal(KA_EXIT_STACK | t->runs, (uint64_t)(uintptr_t)t, 0);
            ka_exit(KA_EXIT_STACK | t->runs);
        }
        rr = t->next;
        if (t->state == KA_TASK_DONE) {
            unlink(t);
            ++done;
            ++st.finished;
        }
    }
    rr = 0;
    return done;
}

uint32_t ka_task_stack_used(const struct ka_task *t)
{
    uint32_t i = 1;
    while (i < t->stack_words && t->stack[i] == FILL) {
        ++i;
    }
    return (t->stack_words - i) * 8;
}

void ka_sched_stats(struct ka_sched_stats *s) { *s = st; }
