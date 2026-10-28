/* libkos: system-call wrappers and the user-level pieces built on them. */
#include <kos/user.h>

static int shared_cached(void);

/* ---- entry points the kernel starts threads at ---- */
/* A thread starts after an acquire and ends after a release, so what its
 * creator wrote before kos_spawn is visible to it, and what it wrote is
 * visible to whoever joins it. */
void __kos_thread_start(int64_t (*fn)(uint64_t), uint64_t arg)
{
    if (shared_cached()) {
        kos_acquire();
    }
    int64_t r = fn(arg);
    if (shared_cached()) {
        kos_release();
    }
    kos_exit(r);
}

extern const struct kos_app __kos_apps_start[];

void __kos_app_start(uint64_t app, char *args)
{
    char *argv[16];
    int argc = 0;
    argv[argc++] = (char *)__kos_apps_start[app].name;
    for (char *p = args; *p && argc < 15;) {
        while (*p == ' ') {
            *p++ = 0;
        }
        if (!*p) {
            break;
        }
        argv[argc++] = p;
        while (*p && *p != ' ') {
            ++p;
        }
    }
    argv[argc] = 0;
    if (shared_cached()) {
        kos_acquire();
    }
    int r = __kos_apps_start[app].main(argc, argv);
    if (shared_cached()) {
        kos_release();
    }
    kos_exit(r);
}

/* ---- threads and nodes ---- */
void kos_exit(int64_t code)
{
    kos_sys(SYS_EXIT, (uint64_t)code, 0, 0, 0);
    for (;;) {
    }
}

void kos_yield(void) { kos_sys(SYS_YIELD, 0, 0, 0, 0); }

int64_t kos_spawn(int64_t (*fn)(uint64_t), uint64_t arg, unsigned node)
{
    if (shared_cached()) {
        kos_release();
    }
    struct kos_r r = kos_sys(SYS_SPAWN, (uint64_t)fn, arg, node, 0);
    return r.st < 0 ? r.st : (int64_t)r.val;
}

int64_t kos_join(int64_t tid, int64_t *code)
{
    struct kos_r r = kos_sys(SYS_JOIN, (uint64_t)tid, 0, 0, 0);
    if (code) {
        *code = (int64_t)r.val;
    }
    if (shared_cached()) {
        kos_acquire();
    }
    return r.st;
}

int64_t kos_run(const char *app, const char *args, unsigned node)
{
    struct kos_r r = kos_sys(SYS_RUN, (uint64_t)app, (uint64_t)args, node, 0);
    return r.st < 0 ? r.st : (int64_t)r.val;
}

uint64_t kos_info(unsigned key) { return (uint64_t)kos_sys(SYS_INFO, key, 0, 0, 0).st; }
unsigned kos_node(void) { return (unsigned)kos_info(KOS_I_NODE); }
unsigned kos_nodes(void) { return (unsigned)kos_info(KOS_I_NODES); }
uint64_t kos_time(void) { return (uint64_t)kos_sys(SYS_TIME, 0, 0, 0, 0).st; }
void kos_sleep(uint64_t cycles) { kos_sys(SYS_SLEEP, cycles, 0, 0, 0); }

void kos_shutdown(int64_t code)
{
    kos_sys(SYS_SHUTDOWN, (uint64_t)code, 0, 0, 0);
    for (;;) {
    }
}

/* ---- console ---- */
int64_t kos_write(const void *buf, uint64_t len) { return kos_sys(SYS_WRITE, (uint64_t)buf, len, 0, 0).st; }
int64_t kos_read(void *buf, uint64_t len) { return kos_sys(SYS_READ, (uint64_t)buf, len, 0, 0).st; }

int kos_printf(const char *fmt, ...)
{
    char b[256];
    va_list ap;
    va_start(ap, fmt);
    int n = kos_vsnprintf(b, sizeof b, fmt, ap);
    va_end(ap);
    kos_write(b, n < (int)sizeof b ? (uint64_t)n : sizeof b - 1);
    return n;
}

/* ---- memory ---- */
void *kos_mmap(uint64_t size)
{
    int64_t r = kos_sys(SYS_MMAP, size, 0, 0, 0).st;
    return r < 0 ? 0 : (void *)r;
}

int kos_shared_ok(void) { return kos_info(KOS_I_SHARED) != 0; }

/* Per node (a plain global): whether shared memory is cached here. */
static int cached_known, cached_val;
static int shared_cached(void)
{
    if (!cached_known) {
        cached_val = kos_info(KOS_I_SHARED) && !(kos_info(KOS_I_FLAGS) & KOS_F_SHARED_UNCACHED);
        cached_known = 1;
    }
    return cached_val;
}

void kos_release(void) { kos_sys(SYS_CACHE, KOS_C_RELEASE, 0, 0, 0); }
void kos_acquire(void) { kos_sys(SYS_CACHE, KOS_C_ACQUIRE, 0, 0, 0); }

void *kos_shalloc(uint64_t size)
{
    uint64_t base = kos_info(KOS_I_SHARED), heap = kos_info(KOS_I_SHHEAP);
    uint64_t lim = kos_info(KOS_I_SHSIZE);
    if (!base) {
        return 0;
    }
    uint64_t off = kos_fetch_add((volatile uint64_t *)heap, KOS_ALIGN_UP(size, 64));
    if (off + size > lim) {
        return 0;
    }
    return (void *)(base + off);
}

/* ---- synchronisation ---- */
uint64_t kos_atomic(volatile uint64_t *w, unsigned op, uint64_t a, uint64_t b)
{
    return kos_sys(SYS_ATOMIC, (uint64_t)w, op, a, b).val;
}

int kos_wait(volatile uint64_t *w, uint64_t value, uint64_t timeout)
{
    return (int)kos_sys(SYS_WAIT, (uint64_t)w, value, timeout, 0).st;
}

int kos_barrier(uint64_t id, uint64_t parties)
{
    int cached = shared_cached();
    if (cached) {
        kos_release();
    }
    int r = (int)kos_sys(SYS_BARRIER, id, parties, 0, 0).st;
    if (cached) {
        kos_acquire();
    }
    return r;
}

static unsigned my_index(void)
{
    uint64_t tid = (uint64_t)kos_sys(SYS_SELF, 0, 0, 0, 0).st;
    return (unsigned)(KOS_TID_NODE(tid) * 16 + KOS_TID_SLOT(tid)) % KOS_LOCK_SLOTS;
}

void kos_lock(kos_lock_t *l)
{
    if (kos_info(KOS_I_FLAGS) & KOS_F_LOCK_BAKERY) {
        /* Lamport's bakery over the uncached view: plain loads and stores */
        unsigned i = my_index();
        volatile uint64_t *ch = kos_unc(l->choosing), *num = kos_unc(l->number);
        ch[i] = 1;
        uint64_t max = 0;
        for (unsigned j = 0; j < KOS_LOCK_SLOTS; ++j) {
            max = num[j] > max ? num[j] : max;
        }
        num[i] = max + 1;
        ch[i] = 0;
        for (unsigned j = 0; j < KOS_LOCK_SLOTS; ++j) {
            while (ch[j]) {
            }
            for (;;) {
                uint64_t nj = num[j];
                if (!nj || nj > num[i] || (nj == num[i] && j >= i)) {
                    break;
                }
            }
        }
    } else {
        uint64_t me = kos_fetch_add(&l->next, 1);
        volatile uint64_t *serving = kos_unc(&l->serving);
        uint64_t s;
        while ((s = *serving) != me) {
            kos_wait(&l->serving, s, 0);
        }
    }
    if (shared_cached()) {
        kos_acquire();
    }
}

void kos_unlock(kos_lock_t *l)
{
    if (shared_cached()) {
        kos_release();
    }
    if (kos_info(KOS_I_FLAGS) & KOS_F_LOCK_BAKERY) {
        kos_unc(l->number)[my_index()] = 0;
    } else {
        volatile uint64_t *serving = kos_unc(&l->serving);
        *serving = *serving + 1;
    }
}

/* ---- channels ---- */
int kos_send(unsigned node, unsigned chan, const void *buf, unsigned len)
{
    if (shared_cached()) {
        kos_release();
    }
    return (int)kos_sys(SYS_SEND, node, chan, (uint64_t)buf, len).st;
}

int kos_recv(unsigned chan, void *buf, uint64_t timeout, unsigned *src)
{
    struct kos_r r = kos_sys(SYS_RECV, chan, (uint64_t)buf, timeout, 0);
    if (src) {
        *src = (unsigned)r.val;
    }
    if (r.st >= 0 && shared_cached()) {
        kos_acquire();
    }
    return (int)r.st;
}

/* ---- units ---- */
int64_t kos_units(struct kos_unit *buf, unsigned max)
{
    return kos_sys(SYS_UNITS, (uint64_t)buf, max, 0, 0).st;
}

int64_t kos_dispatch(unsigned unit, const uint64_t w[4])
{
    struct kos_r r = kos_sys(SYS_DISPATCH, unit, (uint64_t)w, 0, 0);
    return r.st < 0 ? r.st : (int64_t)r.val;
}

int kos_unit_wait(unsigned unit, uint64_t ticket, uint64_t timeout, uint64_t *faults)
{
    struct kos_r r = kos_sys(SYS_UNIT_WAIT, unit, ticket, timeout, 0);
    if (faults) {
        *faults = r.val;
    }
    return (int)r.st;
}

int kos_unit_ctrl(unsigned unit, unsigned index, uint64_t *value)
{
    struct kos_r r = kos_sys(SYS_UNIT_CTRL, unit, index, 0, 0);
    if (value) {
        *value = r.val;
    }
    return (int)r.st;
}

int64_t kos_engine(unsigned unit, const uint64_t *w, unsigned count)
{
    struct kos_r r = kos_sys(SYS_ENGINE, unit, (uint64_t)w, count, 0);
    return r.st < 0 ? r.st : (int64_t)r.val;
}

int64_t kos_move(const void *src, uint64_t dst, uint64_t words, unsigned flags)
{
    return kos_sys(SYS_MOVE, (uint64_t)src, dst, words, flags).st;
}

volatile void *kos_land(void) { return (volatile void *)kos_sys(SYS_LAND, kos_node(), 0, 0, 0).st; }

int64_t kos_stat(unsigned what, void *buf, uint64_t max)
{
    return kos_sys(SYS_STAT, what, (uint64_t)buf, max, 0).st;
}
