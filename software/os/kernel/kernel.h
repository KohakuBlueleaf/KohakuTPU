/* L1, the kernel: one instance per node, in supervisor mode. */
#ifndef KOS_KERNEL_H
#define KOS_KERNEL_H

#include <kos/abi.h>
#include <kos/boot.h>
#include <kos/hw.h>
#include <kos/lib.h>
#include <kos/stat.h>

#define K_THREADS 16
#define K_INBOX   128
#define K_CHANQ   64
#define K_PENDING 32
#define K_BAR     8
#define K_BAR_WAITERS 64

/* ---- the node ---- */
struct knode {
    unsigned me, n, mesh;
    unsigned mesh_of[KOS_MAX_NODES];
    uint32_t flags;
    uint64_t tick;
    uint64_t priv, priv_size;           /* own span, physical */
    uint64_t peer_priv[KOS_MAX_NODES];  /* each node's span as this node's mover names it */
    uint64_t peer_priv_size[KOS_MAX_NODES];
    uint64_t shared, shared_size;
    uint64_t frames_end;                /* frame pool: [0, frames_end) of the span */
    uint64_t land_off, log_off;         /* offsets in the span */
    int engine;
};
extern struct knode kn;
extern struct kos_boot k_boot;
extern struct kos_kernel_stat kstat;

/* ---- threads ---- */
enum {
    W_NONE,
    W_JOIN,     /* a: slot */
    W_REPLY,    /* a: request id */
    W_MEM,      /* a: address (uncached view), b: value */
    W_SLEEP,
    W_UNIT,     /* a: unit, b: ticket */
    W_CTRL,     /* a: unit */
    W_RECV,     /* a: channel */
    W_READ,
    W_ENGINE,   /* a: counter, b: target */
    W_SEND,     /* a: node << 32 | channel, result: length; the bytes in sendbuf */
};

struct thread {
    uint64_t x[33]; /* x0..x31, x[32] = pc: the trap frame, first */
    uint32_t state, slot;
    uint32_t tid;
    uint32_t wkind;
    uint64_t wa, wb, deadline; /* deadline 0 = none */
    int64_t result;            /* what the wait returns */
    int64_t exit_code;
    uint64_t stack_pages;
    uint64_t cycles, switches, since;
    int next;                  /* ready queue */
    int joiners;               /* local threads waiting */
    int rjoin_node;            /* a remote joiner: node, request id; -1 none */
    uint64_t rjoin_req;
    char name[16];
    uint8_t sendbuf[40];       /* a send waiting for room (W_SEND) */
};
extern struct thread kthreads[K_THREADS];
extern struct thread *kcur;

struct thread *k_new_thread(uint64_t entry, uint64_t a0, uint64_t a1, const char *name);
void k_ready(struct thread *t);
void k_block(struct thread *t, uint32_t kind, uint64_t a, uint64_t b, uint64_t timeout);
void k_wake(struct thread *t, int64_t result);
void k_exit_thread(struct thread *t, int64_t code);
struct thread *k_schedule(void);
void k_poll(void);
__attribute__((noreturn)) void k_resume(struct thread *t);
__attribute__((noreturn)) void k_panic(const char *fmt, ...);

/* ---- memory ---- */
void k_mem_init(void);
uint64_t k_frame(void);           /* a zeroed private page, physical; 0 when out */
void *k_alloc(uint64_t bytes);    /* zeroed kernel tables in private DRAM (cached), at boot */
void k_frame_free(uint64_t pa);
uint64_t k_frames_free(void);
int k_map(uint64_t va, uint64_t pa, uint64_t size, unsigned attr);
uint64_t k_shared_unc(uint64_t uva); /* the uncached physical address of a user shared address, 0 if not shared */
int k_copy_in(void *dst, uint64_t uva, uint64_t n);
int k_copy_out(uint64_t uva, const void *src, uint64_t n);
int64_t k_copy_str(char *dst, uint64_t uva, uint64_t max);
uint64_t k_uimg(uint64_t va);     /* physical address of a user-image address */
extern uint64_t k_user_satp;      /* what user mode runs under (k_resume) */
int k_user_range(uint64_t va, uint64_t len);
int k_stack_map(struct thread *t);
void k_stack_unmap(struct thread *t);
int64_t k_mmap(uint64_t size, unsigned flags);
uint64_t k_user_pa(uint64_t va);  /* physical address of a user page's byte, 0 if unmapped */

/* ---- L0 calls ---- */
struct sbiret {
    int64_t st;
    uint64_t val;
};
struct sbiret k_l0(uint64_t fn, uint64_t a0, uint64_t a1, uint64_t a2, uint64_t a3);

/* ---- console ---- */
void k_printf(const char *fmt, ...);
void k_write(const char *s, uint64_t len);
void k_log_init(void);
volatile uint64_t *k_log_hdr(void);
void k_where(uint64_t code, uint64_t a, uint64_t b);      /* what the node is doing (abi.h KOS_W_*) */
void k_where_wait(uint64_t code, uint64_t a, uint64_t b); /* a wait inside it ... */
void k_where_back(void);                                   /* ... that has ended */
void k_switched(void);
__attribute__((noreturn)) void k_halt(uint64_t code);

/* ---- messages between nodes ---- */
enum {
    M_SPAWN = 1, /* p0 req, p1 entry, p2 a0, p3 a1, p4.. name */
    M_REPLY,     /* p0 req, p1 status, p2 value */
    M_JOIN,      /* p0 req, p1 tid */
    M_ATOMIC,    /* p0 req, p1 shared offset, p2 op, p3 a, p4 b */
    M_CHAN,      /* p0 chan, p1 len, p2.. data (40 bytes) */
    M_DISPATCH,  /* p0 req, p1 unit, p2..p5 words */
    M_UNIT_WAIT, /* p0 req, p1 unit, p2 ticket */
    M_UNIT_CTRL, /* p0 req, p1 unit, p2 index */
    M_BAR,       /* p0 req, p1 id, p2 parties */
    M_SHUTDOWN,  /* p0 code */
    M_PING,      /* p0 req */
    M_RUN,       /* p0 req, p1 app index, p2..p6 argument text (40 bytes) */
};
struct kmsg {
    uint64_t hdr; /* [7:0] type, [15:8] source node */
    uint64_t p[7];
};
#define KMSG_TYPE(m) ((unsigned)((m)->hdr & 0xff))
#define KMSG_SRC(m)  ((unsigned)(((m)->hdr >> 8) & 0xff))

void k_msg_init(void);
void k_send(unsigned node, unsigned type, const uint64_t *p, unsigned np);
int k_send_room(unsigned node); /* a k_send to `node` would not wait for credits */
void k_msg_poll(void);
uint64_t k_req_id(void);
/* Send a request and block the current thread for its M_REPLY. */
void k_rpc(unsigned node, unsigned type, uint64_t *p, unsigned np);
void k_reply(unsigned node, uint64_t req, int64_t status, uint64_t value);
void k_ring(unsigned node, unsigned slot, unsigned amount, int fence);
void k_rings_drain(void); /* every ring queued has left */

/* ---- channels ---- */
int k_chan_put(unsigned src, unsigned chan, const void *data, unsigned len); /* -1: queue full */
int k_chan_take(struct thread *t);                                     /* -1: none yet */
int k_chan_send(uint64_t node_chan, const void *data, unsigned len); /* -1: no room yet */

/* ---- atomics at the home node ---- */
unsigned k_home(uint64_t off);
uint64_t k_atomic_local(uint64_t off, unsigned op, uint64_t a, uint64_t b);

/* ---- barriers (node 0 coordinates) ---- */
void k_bar_arrive(unsigned node, uint64_t req, uint64_t id, uint64_t parties);

/* ---- units ---- */
struct kunit {
    struct kos_unit u;
    int local;
    uint64_t sent, done, faults, fault_arg, acks, misc;
    uint64_t ctrl_value;
    int ctrl_ready, ctrl_busy;
};
extern struct kunit kunits[KOS_MAX_UNITS];
extern unsigned knunits;
void k_units_init(void);
void k_units_poll(void);
int64_t k_dispatch_local(unsigned u, const uint64_t w[4]);
uint64_t k_unit_retired(unsigned u);
int k_unit_ctrl_start(unsigned u, unsigned index);
int64_t k_engine_run(unsigned u, const uint64_t *w, unsigned count);
int64_t k_move(uint64_t src_pa, uint64_t dst_pa, uint64_t words);
void k_pending_add(unsigned kind, unsigned node, uint64_t req, uint64_t a, uint64_t b);
void k_pending_poll(void);

/* ---- syscalls ---- */
void k_syscall(struct thread *t);
int64_t k_run_local(unsigned app, const char *args); /* a thread running app `app`: its tid */
int k_find_app(const char *name);                    /* its index in the app table, -1 if none */

/* ---- registered apps (user rodata) ---- */
struct kos_app {
    const char *name;
    int (*main)(int argc, char **argv);
};
extern const struct kos_app __kos_apps_start[], __kos_apps_end[];
extern char __kos_app_start[]; /* user crt: __kos_app_start(app index, args) */
extern char __kos_thread_start[]; /* user crt: __kos_thread_start(fn, arg) */

#endif
