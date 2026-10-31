/* Tasks: cooperative routines with their own stacks, and the round-robin run
 * queue that interleaves them on the node's one hart. */
#ifndef KA_OS_TASK_H
#define KA_OS_TASK_H

#include <stdint.h>

typedef int (*ka_task_fn)(void *arg);
typedef int (*ka_ready_fn)(void *ctx); /* nonzero: the wait is over */

enum ka_task_state { KA_TASK_READY = 1, KA_TASK_BLOCKED = 2, KA_TASK_DONE = 3 };

struct ka_task {
    uint64_t sp; /* saved while switched out */
    struct ka_task *next;
    ka_task_fn fn;
    void *arg;
    ka_ready_fn ready;
    void *ctx;
    uint64_t since, timeout; /* the wait's start and bound, cycles; 0 = none */
    uint64_t *stack;         /* lowest word: the canary */
    uint32_t stack_words;
    const char *name;
    int state, code, woke;
    uint32_t runs; /* times switched in */
};

/* Stack words below this are refused; a switch frame alone is 14. */
#define KA_TASK_MIN_STACK 1024u

/* Queue `t` to run fn(arg) on `stack` (16-byte aligned, at least
 * KA_TASK_MIN_STACK bytes). 0, or KA_ST_BAD_ARG. A task may spawn others. */
int ka_task_spawn(struct ka_task *t, const char *name, ka_task_fn fn, void *arg,
                  void *stack, uint32_t stack_bytes);

/* Run every queued task, round robin, until all have returned or called
 * ka_task_exit. Returns how many finished. */
uint32_t ka_sched_run(void);

void ka_yield(void);

/* Until ready(ctx) holds (0) or `timeout` cycles pass (KA_ST_TIMEOUT); a
 * timeout of 0 waits for ever. Returns at once, without switching, when the
 * predicate already holds. */
int ka_wait(ka_ready_fn ready, void *ctx, uint64_t timeout);

/* Let at least `cycles` pass, running others meanwhile. */
void ka_sleep(uint64_t cycles);

void ka_task_exit(int code) __attribute__((noreturn));

/* The running task, or 0 outside one. */
struct ka_task *ka_task_self(void);

/* Bytes of `t`'s stack ever written, from the fill pattern spawn laid down. */
uint32_t ka_task_stack_used(const struct ka_task *t);

/* Run-queue counters since boot. */
struct ka_sched_stats {
    uint64_t switches;    /* into a task */
    uint64_t idle_passes; /* full passes that found nothing runnable */
    uint32_t spawned, finished;
};
void ka_sched_stats(struct ka_sched_stats *s);

/* Exit word for a task whose stack canary was overwritten (| the task's runs). */
#define KA_EXIT_STACK 0xE200000000000000UL

#endif
