/* The ready scheduler on held tokens and held egress room. */
#include <assert.h>
#include <stdlib.h>
#include <string.h>

#define kos_de_run real_kos_de_run
#define kos_now real_kos_now
#define kos_cycles real_kos_cycles
#include "kds_int.h"
#undef kos_de_run
#define kos_de_run double_de_run
int64_t double_de_run(const uint64_t *entries, unsigned count);
#undef kos_now
#undef kos_cycles
static uint64_t tick;
#define kos_now() (++tick)
#define kos_cycles() (++tick)
#include APP_SOURCE

static unsigned tokens = 1, room, rings, produced, consumed, freed, reports;
static struct kds_node node;
static struct kds_task tasks[4];
static uint16_t remain[4], next_task[4], ready[5];
static uint32_t pairs[2];

int kos_slot(unsigned release, unsigned destination, unsigned slot, unsigned amount)
{
    assert(destination == 0 && slot == KOS_SLOT_USER && amount == 2);
    if (release) {
        if (!room) {
            return KOS_EAGAIN;
        }
        --room;
        ++rings;
    } else {
        if (tokens < amount) {
            return KOS_EAGAIN;
        }
        tokens -= amount;
    }
    return KOS_OK;
}

static int64_t producer(const uint64_t *args)
{
    (void)args;
    assert(tokens == 1);
    ++tokens;
    ++produced;
    return 0;
}

static int64_t consumer(const uint64_t *args)
{
    (void)args;
    assert(tokens == 0 && rings == 1);
    ++consumed;
    return 0;
}

void kos_release(void) {}
void kos_acquire(void) {}
void *kds_arena_va(struct kds_node *k, kds_ref ref)
{
    (void)k;
    return (void *)(uintptr_t)ref;
}
int kds_arena_free(struct kds_node *k, kds_ref ref)
{
    (void)k;
    (void)ref;
    ++freed;
    return KOS_OK;
}
uint64_t kds_resolve(struct kds_node *k, kds_ref ref)
{
    (void)k;
    return ref;
}
void kds_home_ndone(struct kds_node *k, unsigned at, uint32_t gid, int64_t status,
                    uint32_t failed, int64_t detail, uint64_t busy)
{
    (void)k; (void)at; (void)gid; (void)status;
    (void)failed; (void)detail; (void)busy;
    ++reports;
}
void kds_dep_add(struct kds_node *k, unsigned at, uint32_t gid, uint16_t id)
{
    (void)k; (void)at; (void)gid; (void)id;
    abort();
}
void kds_dep_flush(struct kds_node *k) { (void)k; }
void kds_post(struct kds_node *k, unsigned at, unsigned chan, const struct kds_msg *m)
{
    (void)k; (void)at; (void)chan; (void)m;
    abort();
}
int64_t kos_de_run(const uint64_t *entries, unsigned count)
{
    (void)entries; (void)count;
    abort();
}
int64_t kos_de_epoch(const uint64_t *entries, unsigned count, unsigned operation)
{
    (void)entries; (void)count; (void)operation;
    abort();
}

int64_t kos_move(const void *src, uint64_t dst, uint64_t words, unsigned flags)
{
    (void)src; (void)dst; (void)words; (void)flags;
    abort();
}
int kos_event(uint64_t target, uint64_t timeout, uint64_t *counts, unsigned n,
              unsigned chan, uint64_t *now)
{
    (void)target; (void)timeout; (void)counts; (void)n; (void)chan; (void)now;
    abort();
}

int main(void)
{
    node.n = 1;
    tasks[0] = (struct kds_task){.kind = KDS_WAIT_SLOT, .a = {KOS_SLOT_USER, 2}};
    tasks[1] = (struct kds_task){.kind = KDS_CPU, .id = 1,
                                .a = {(uint64_t)(uintptr_t)producer}};
    tasks[2] = (struct kds_task){.kind = KDS_RELEASE_SLOT, .id = 2,
                                .a = {KOS_SLOT_USER, 2, 0, 1}, .npred = 1, .pred = {0}};
    tasks[3] = (struct kds_task){.kind = KDS_CPU, .id = 3,
                                .a = {(uint64_t)(uintptr_t)consumer}, .npred = 1, .pred = {2}};
    pairs[0] = 2;
    pairs[1] = (2u << 16) | 3;
    remain[2] = remain[3] = 1;
    ready[0] = 0;
    ready[1] = 1;
    struct kds_part *p = &node.part[0];
    *p = (struct kds_part){.gid = 1, .state = LG_RUN, .n = 4, .t = tasks,
                           .remain = remain, .next = next_task, .pairs = pairs, .npairs = 2,
                           .ready = ready, .rtail = 2};
    kds_parts_run(&node);
    assert(produced == 1 && consumed == 0 && tokens == 2 && node.any_ready);
    kds_parts_run(&node);
    assert(tokens == 0 && rings == 0 && consumed == 0 && node.any_ready);
    room = 1;
    kds_parts_run(&node);
    assert(consumed == 1 && rings == 1 && reports == 1 && freed == 1);
    assert(!node.any_ready);

    p->state = LG_RUN;
    p->inflight = 1;
    p->rhead = 0;
    p->rtail = 1;
    assert(kds_part_abort(&node, 1));
    assert(p->rhead == p->rtail && freed == 1);
    release_part(&node, p);
    assert(freed == 1);
    p->inflight = 0;
    release_part(&node, p);
    assert(freed == 2);
    return 0;
}
