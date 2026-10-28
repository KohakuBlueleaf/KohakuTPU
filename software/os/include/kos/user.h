/* The user-level API of KohakuAccel OS (L2 programs link libkos).
 *
 * Memory model. Every node runs the same image. Plain globals are PER NODE
 * (each node's private DRAM holds its own copy); globals marked KOS_SHARED and
 * memory from kos_shalloc() are ONE copy in shared DRAM, at the same address
 * on every node. Shared memory is cached unless the board's flags say
 * otherwise; the synchronisation calls (kos_barrier, kos_lock/unlock,
 * kos_send/recv pairs) write this node's cache back before they release and
 * drop it after they acquire, so data published before a release is seen
 * after the matching acquire. Words used by kos_atomic* are only ever
 * touched by kos_atomic* (they execute at the word's home node). */
#ifndef KOS_USER_H
#define KOS_USER_H

#include <kos/abi.h>
#include <kos/boot.h>
#include <kos/lib.h>
#include <kos/stat.h>
#include <stdarg.h>
#include <stdint.h>

struct kos_r {
    int64_t st;
    uint64_t val;
};

static inline struct kos_r kos_sys(uint64_t n, uint64_t a0, uint64_t a1, uint64_t a2, uint64_t a3)
{
    register uint64_t r0 __asm__("a0") = a0;
    register uint64_t r1 __asm__("a1") = a1;
    register uint64_t r2 __asm__("a2") = a2;
    register uint64_t r3 __asm__("a3") = a3;
    register uint64_t r7 __asm__("a7") = n;
    __asm__ volatile("ecall" : "+r"(r0), "+r"(r1) : "r"(r2), "r"(r3), "r"(r7) : "memory");
    return (struct kos_r){(int64_t)r0, r1};
}

#define KOS_SHARED __attribute__((section(".ushared")))

struct kos_app {
    const char *name;
    int (*main)(int argc, char **argv);
};
#define KOS_APP(name, fn)                                                                  \
    __attribute__((section(".kos_apps"), used)) static const struct kos_app __kos_app_##fn = \
        {name, fn}

/* threads and nodes */
__attribute__((noreturn)) void kos_exit(int64_t code);
void kos_yield(void);
int64_t kos_spawn(int64_t (*fn)(uint64_t), uint64_t arg, unsigned node);
int64_t kos_join(int64_t tid, int64_t *code);
int64_t kos_run(const char *app, const char *args, unsigned node);
unsigned kos_node(void);
unsigned kos_nodes(void);
uint64_t kos_info(unsigned key);
uint64_t kos_time(void);
void kos_sleep(uint64_t cycles);
__attribute__((noreturn)) void kos_shutdown(int64_t code);

/* console */
int64_t kos_write(const void *buf, uint64_t len);
int64_t kos_read(void *buf, uint64_t len);
int kos_printf(const char *fmt, ...);

/* memory */
void *kos_mmap(uint64_t size);
void *kos_shalloc(uint64_t size); /* shared, 64-byte aligned, never freed */
int kos_shared_ok(void);
void kos_release(void);
void kos_acquire(void);
/* the uncached view of a shared address */
static inline volatile uint64_t *kos_unc(const volatile void *p)
{
    return (volatile uint64_t *)((uintptr_t)p + (KOS_UV_SHUNC - KOS_UV_SHARED));
}

/* synchronisation */
uint64_t kos_atomic(volatile uint64_t *w, unsigned op, uint64_t a, uint64_t b);
static inline uint64_t kos_fetch_add(volatile uint64_t *w, uint64_t v) { return kos_atomic(w, KOS_A_ADD, v, 0); }
static inline uint64_t kos_load(volatile uint64_t *w) { return kos_atomic(w, KOS_A_LOAD, 0, 0); }
int kos_wait(volatile uint64_t *w, uint64_t value, uint64_t timeout);
int kos_barrier(uint64_t id, uint64_t parties);

#define KOS_LOCK_SLOTS 64
typedef struct {
    uint64_t next, serving;                 /* ticket: next at its home, serving by the holder */
    uint64_t choosing[KOS_LOCK_SLOTS];      /* bakery */
    uint64_t number[KOS_LOCK_SLOTS];
} kos_lock_t;
void kos_lock(kos_lock_t *l);
void kos_unlock(kos_lock_t *l);

/* channels: up to 40 bytes per message */
int kos_send(unsigned node, unsigned chan, const void *buf, unsigned len);
int kos_recv(unsigned chan, void *buf, uint64_t timeout, unsigned *src);

/* units, the mover, the engine */
int64_t kos_units(struct kos_unit *buf, unsigned max);
int64_t kos_dispatch(unsigned unit, const uint64_t w[4]);
int kos_unit_wait(unsigned unit, uint64_t ticket, uint64_t timeout, uint64_t *faults);
int kos_unit_ctrl(unsigned unit, unsigned index, uint64_t *value);
int64_t kos_engine(unsigned unit, const uint64_t *w, unsigned count);
int64_t kos_move(const void *src, uint64_t dst, uint64_t words, unsigned flags);
volatile void *kos_land(void); /* this node's landing zone (uncached); KOS_I_LANDPA names it for units */

int64_t kos_stat(unsigned what, void *buf, uint64_t max);

#endif
