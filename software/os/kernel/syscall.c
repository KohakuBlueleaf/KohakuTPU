/* System calls. a0 carries the result (or a status), a1 a value where the
 * result can be any 64-bit word (atomics, CU_CTRL reads, joins, tickets).
 * A call that waits blocks the thread; k_wake delivers a0 later. User
 * memory is reached through the copy helpers (the kernel is untranslated). */
#include "kernel.h"

extern char __ushared_start[], __ushared_end[];

#ifdef KOS_SMALL_CALL
#define BUFFER_CALL __attribute__((noinline, minsize))
#else
#define BUFFER_CALL __attribute__((always_inline)) inline
#endif

static unsigned napps(void) { return (unsigned)(__kos_apps_end - __kos_apps_start); }

/* The app table and its name strings live in the user image. */
static const char *app_name(unsigned i)
{
    const struct kos_app *a = (const struct kos_app *)k_uimg((uint64_t)&__kos_apps_start[i]);
    return (const char *)k_uimg((uint64_t)a->name);
}

int k_find_app(const char *name)
{
    for (unsigned i = 0; i < napps(); ++i) {
        if (!strcmp(app_name(i), name)) {
            return (int)i;
        }
    }
    return -1;
}

int64_t k_run_local(unsigned app, const char *args)
{
    if (app >= napps()) {
        return KOS_ENOENT;
    }
    struct thread *t = k_new_thread((uint64_t)__kos_app_start, app, 0, app_name(app));
    if (!t) {
        return KOS_EAGAIN;
    }
    /* the argument text at the top of the new stack */
    uint64_t len = strlen(args) + 1;
    uint64_t sp = (t->x[2] - KOS_ALIGN_UP(len, 16)) & ~15UL;
    k_copy_out(sp, args, len);
    t->x[2] = sp;
    t->x[11] = sp;
    return t->tid;
}

static uint64_t shared_off(uint64_t va, uint64_t len)
{
    for (int k = 0; k < 2; ++k) {
        uint64_t base = k ? KOS_UV_SHUNC : KOS_UV_SHARED;
        if (kn.shared_size && va >= base && va + len <= base + kn.shared_size) {
            return va - base;
        }
    }
    return ~0UL;
}

/* The physical address of a user range the mover may touch: one contiguous
 * piece of shared memory or of this node's pages. 0 if it is not. */
static uint64_t move_pa(uint64_t va, uint64_t len)
{
    uint64_t off = shared_off(va, len);
    if (off != ~0UL) {
        return kn.shared + off;
    }
    if (!k_user_range(va, len)) {
        return 0;
    }
    uint64_t pa = k_user_pa(va) & ~HW_UNC;
    if (!pa) {
        return 0;
    }
    for (uint64_t a = (va & ~0xfffUL) + 4096; a < va + len; a += 4096) {
        if ((k_user_pa(a) & ~HW_UNC) != pa + (a - va)) {
            return 0;
        }
    }
    return pa;
}

static void ret2(struct thread *t, int64_t st, uint64_t val)
{
    t->x[10] = (uint64_t)st;
    t->x[11] = val;
}

static int64_t stat_into(uint64_t what, void *buf, uint64_t max)
{
    if (what == KOS_S_THREADS) {
        struct kos_thread_stat *o = buf;
        uint64_t n = 0;
        for (unsigned i = 0; i < K_THREADS && (n + 1) * sizeof *o <= max; ++i) {
            struct thread *t = &kthreads[i];
            if (t->state == KOS_T_FREE) {
                continue;
            }
            o[n].tid = t->tid;
            o[n].state = t->state;
            o[n].cycles = t->cycles;
            o[n].switches = t->switches;
            memcpy(o[n].name, t->name, 16);
            ++n;
        }
        return (int64_t)(n * sizeof *o);
    }
    if (what == KOS_S_KERNEL && max >= sizeof(struct kos_kernel_stat)) {
#ifdef KOS_LOCAL_LOG
        k_log_sync();
#endif
        kstat.frames_free = k_frames_free();
        kstat.frames_total = kn.frames_end >> 12;
        kstat.uptime = hw_time();
        kstat.l0_maps = k_l0(KOS_M_INFO, KOS_MI_MAPS, 0, 0, 0).val;
        kstat.l0_tables = k_l0(KOS_M_INFO, KOS_MI_TABLES, 0, 0, 0).val;
        kstat.l0_traps = k_l0(KOS_M_INFO, KOS_MI_TRAPS, 0, 0, 0).val;
        memcpy(buf, &kstat, sizeof kstat);
        return sizeof kstat;
    }
    if (what == KOS_S_UNITS) {
        struct kos_unit_stat *o = buf;
        uint64_t n = 0;
        for (; n < knunits && (n + 1) * sizeof *o <= max; ++n) {
            struct kunit *u = &kunits[n];
            o[n] = (struct kos_unit_stat){u->u.type, u->u.mesh, u->u.x, u->u.y, (uint8_t)u->local,
                                          {0, 0}, u->sent, u->done, u->faults, u->fault_arg};
        }
        return (int64_t)(n * sizeof *o);
    }
    return KOS_EINVAL;
}

/* Built in a kernel buffer, then copied out. */
static BUFFER_CALL int64_t stat(uint64_t what, uint64_t ubuf, uint64_t max)
{
    union {
        struct kos_thread_stat t[K_THREADS];
        struct kos_kernel_stat k;
        struct kos_unit_stat u[KOS_MAX_UNITS];
    } b;
    int64_t n = stat_into(what, &b, max < sizeof b ? max : sizeof b);
    return n > 0 && k_copy_out(ubuf, &b, (uint64_t)n) ? KOS_EFAULT : n;
}

static unsigned unit_node(unsigned u)
{
    for (unsigned j = 0; j < kn.n; ++j) {
        if (kn.mesh_of[j] == kunits[u].u.mesh) {
            return j;
        }
    }
    return kn.me;
}

static BUFFER_CALL int64_t write_user(uint64_t addr, uint64_t len)
{
    char b[256];
    int64_t r = len > 4096 ? KOS_EFAULT : (int64_t)len;
    for (uint64_t at = 0; r >= 0 && at < len; at += sizeof b) {
        uint64_t n = len - at < sizeof b ? len - at : sizeof b;
        if (k_copy_in(b, addr + at, n)) {
            return KOS_EFAULT;
        }
        k_write(b, n);
    }
    return r;
}

static BUFFER_CALL void dispatch_user(struct thread *t, uint64_t unit, uint64_t addr)
{
    uint64_t w[4];
    if (unit >= knunits || k_copy_in(w, addr, sizeof w)) {
        ret2(t, KOS_EINVAL, 0);
        return;
    }
    if (kunits[unit].local) {
        int64_t tk = k_dispatch_local((unsigned)unit, w);
        ret2(t, tk < 0 ? tk : KOS_OK, tk < 0 ? 0 : (uint64_t)tk);
    } else {
        uint64_t p[6] = {0, unit, w[0], w[1], w[2], w[3]};
        k_rpc(unit_node((unsigned)unit), M_DISPATCH, p, 6);
    }
}

static BUFFER_CALL void engine_user(struct thread *t, uint64_t unit, uint64_t addr, uint64_t count)
{
    uint64_t w[8 * 4];
    if (count == 0 || count > 8 || k_copy_in(w, addr, count * 32)) {
        ret2(t, KOS_EINVAL, 0);
        return;
    }
#ifdef KOS_REMOTE_BATCH
    if (unit < knunits && !kunits[unit].local) {
        static uint64_t *staging;
        if (!staging) {
            staging = k_alloc(KOS_BATCH_SLOT);
        }
        unsigned node = unit_node((unsigned)unit);
        uint64_t off = KOS_BATCH_BASE + (kn.me * K_THREADS + t->slot) * KOS_BATCH_SLOT;
        memcpy(staging, w, count * 32);
        hw_dcache(HW_DC_FLUSH);
        int64_t st = k_move((uint64_t)staging,
                           kn.peer_priv[node] + kn.peer_priv_size[node] - KOS_LAND_SIZE + off,
                           count);
        if (st) {
            ret2(t, st, 0);
            return;
        }
        uint64_t p[4] = {0, unit, off, count};
        k_rpc(node, M_BATCH, p, 4);
        return;
    }
#endif
    int64_t tk = k_engine_run((unsigned)unit, w, (unsigned)count);
    ret2(t, tk < 0 ? tk : KOS_OK, tk < 0 ? 0 : (uint64_t)tk);
}

static BUFFER_CALL void run_user(struct thread *t, uint64_t uname, uint64_t uargs, uint64_t node)
{
    char name[24], args[41] = {0};
    if (k_copy_str(name, uname, sizeof name) < 0 || k_copy_str(args, uargs, sizeof args) < 0) {
        ret2(t, KOS_EFAULT, 0);
        return;
    }
    int app = k_find_app(name);
    if (app < 0) {
        ret2(t, KOS_ENOENT, 0);
        return;
    }
    if (node == kn.me || node >= kn.n) {
        int64_t tid = k_run_local((unsigned)app, args);
        ret2(t, tid < 0 ? tid : KOS_OK, tid < 0 ? 0 : (uint64_t)tid);
    } else {
        uint64_t p[7] = {0, (uint64_t)app, 0, 0, 0, 0, 0};
        memcpy(&p[2], args, 40);
        k_rpc((unsigned)node, M_RUN, p, 7);
    }
}

static BUFFER_CALL int64_t info_user(uint64_t a0, uint64_t a1)
{
    switch (a0) {
    case KOS_I_NODE: return kn.me;
    case KOS_I_NODES: return kn.n;
    case KOS_I_MESH: return kn.mesh;
    case KOS_I_FLAGS: return kn.flags;
    case KOS_I_SHARED: return kn.shared_size ? (int64_t)KOS_UV_SHARED : 0;
    case KOS_I_SHSIZE: return (int64_t)kn.shared_size;
    case KOS_I_SHHEAP:
        return kn.shared_size ? (int64_t)(KOS_UV_SHARED +
               KOS_ALIGN_UP((uint64_t)(__ushared_end - __ushared_start), 64)) : 0;
    case KOS_I_L0RTT: {
        uint64_t start = hw_cycles();
        for (int i = 0; i < 16; ++i) {
            k_l0(KOS_M_NOP, 0, 0, 0, 0);
        }
        return (int64_t)((hw_cycles() - start) / 16);
    }
    case KOS_I_SHPA: return (int64_t)(kn.shared_size ? kn.shared : 0);
    case KOS_I_LANDPA: return (int64_t)(kn.priv + kn.land_off);
    case KOS_I_CLOCK: return (int64_t)k_boot.clock_hz;
    case KOS_I_NUNITS: return knunits;
    case KOS_I_SPAN:
    case KOS_I_SPANSZ:
    case KOS_I_MESHOF:
        if (a1 >= kn.n) {
            return KOS_EINVAL;
        }
        return (int64_t)(a0 == KOS_I_SPAN ? kn.peer_priv[a1] :
                        a0 == KOS_I_SPANSZ ? kn.peer_priv_size[a1] : kn.mesh_of[a1]);
#ifdef KOS_PROF
    case KOS_I_PROF: return k_prof_info(a1);
#endif
    default: return KOS_EINVAL;
    }
}

static BUFFER_CALL void trace_user(const struct thread *t)
{
    k_printf("[k%u] %x sys %lu (%lx %lx %lx)\n", kn.me, t->tid,
             t->x[17], t->x[10], t->x[11], t->x[12]);
}

#ifdef KOS_SMALL_CALL
__attribute__((minsize))
#endif
void k_syscall(struct thread *t)
{
    if (kn.flags & KOS_F_TRACE) {
        trace_user(t);
    }
    uint64_t a0 = t->x[10], a1 = t->x[11], a2 = t->x[12], a3 = t->x[13];
    uint64_t nr = t->x[17];
    int64_t r = 0;
    switch (nr) {
    case SYS_EXIT:
        k_exit_thread(t, (int64_t)a0);
        return;
    case SYS_YIELD:
        k_ready(t);
        return;
    case SYS_WRITE:
        r = write_user(a0, a1);
        break;
    case SYS_READ:
        if (!k_user_range(a0, a1) || !a1) {
            r = KOS_EFAULT;
            break;
        }
        k_block(t, W_READ, a0, a1, 0);
        return;
    case SYS_SPAWN:
        if (a2 == kn.me || a2 >= kn.n) {
            struct thread *n = k_new_thread((uint64_t)__kos_thread_start, a0, a1, "thread");
            ret2(t, n ? KOS_OK : KOS_EAGAIN, n ? n->tid : 0);
            return;
        } else {
            uint64_t p[4] = {0, (uint64_t)__kos_thread_start, a0, a1};
            k_rpc((unsigned)a2, M_SPAWN, p, 4);
        }
        return;
    case SYS_JOIN: {
        unsigned node = KOS_TID_NODE(a0), slot = KOS_TID_SLOT(a0);
        if (node != kn.me) {
            if (node >= kn.n) {
                ret2(t, KOS_ENOENT, 0);
                return;
            }
            uint64_t p[2] = {0, a0};
            k_rpc(node, M_JOIN, p, 2);
            return;
        }
        struct thread *j = slot < K_THREADS ? &kthreads[slot] : 0;
        if (!j || j->tid != a0 || j->state == KOS_T_FREE || j == t) {
            ret2(t, KOS_ENOENT, 0);
        } else if (j->state == KOS_T_ZOMBIE) {
            j->state = KOS_T_FREE;
            ret2(t, KOS_OK, (uint64_t)j->exit_code);
        } else {
            k_block(t, W_JOIN, slot, 0, 0);
        }
        return;
    }
    case SYS_SELF:
        r = t->tid;
        break;
    case SYS_INFO:
        r = info_user(a0, a1);
        break;
    case SYS_SLEEP:
        k_block(t, W_SLEEP, 0, 0, a0 ? a0 : 1);
        return;
    case SYS_TIME:
        r = (int64_t)hw_time();
        break;
    case SYS_MMAP:
        r = k_mmap(a0, (unsigned)a1);
        break;
    case SYS_WAIT: {
        uint64_t kva = k_shared_unc(a0);
        if (!kva || (a0 & 7)) {
            r = KOS_EINVAL;
            break;
        }
        if (*(volatile uint64_t *)kva != a1) {
            r = KOS_OK;
            break;
        }
        k_block(t, W_MEM, kva, a1, a2);
        return;
    }
    case SYS_ATOMIC: {
        uint64_t off = shared_off(a0, 8);
        if (off == ~0UL || (a0 & 7)) {
            ret2(t, KOS_EINVAL, 0);
            return;
        }
        unsigned home = k_home(off);
        if (home == kn.me) {
            ret2(t, KOS_OK, k_atomic_local(off, (unsigned)a1, a2, a3));
        } else {
            uint64_t p[5] = {0, off, a1, a2, a3};
            k_rpc(home, M_ATOMIC, p, 5);
        }
        return;
    }
    case SYS_CACHE:
        hw_dcache(a0 == KOS_C_ACQUIRE ? (HW_DC_FLUSH | HW_DC_INVAL) : HW_DC_FLUSH);
        break;
    case SYS_UNITS: {
        uint64_t n = knunits < a1 ? knunits : a1;
        r = (int64_t)n;
        for (uint64_t i = 0; i < n; ++i) {
            if (k_copy_out(a0 + i * sizeof(struct kos_unit), &kunits[i].u, sizeof(struct kos_unit))) {
                r = KOS_EFAULT;
                break;
            }
        }
        break;
    }
    case SYS_DISPATCH:
        dispatch_user(t, a0, a1);
        return;
    case SYS_UNIT_WAIT: {
        if (a0 >= knunits) {
            ret2(t, KOS_EINVAL, 0);
            return;
        }
        struct kunit *u = &kunits[a0];
        if (!u->local) {
            uint64_t p[3] = {0, a0, a1};
            k_rpc(unit_node((unsigned)a0), M_UNIT_WAIT, p, 3);
            return;
        }
        if ((int64_t)(k_unit_retired((unsigned)a0) - a1) >= 0) {
            ret2(t, KOS_OK, u->faults);
            return;
        }
        k_block(t, W_UNIT, a0, a1, a2);
        t->result = (int64_t)u->faults;
        t->x[11] = u->faults;
        return;
    }
    case SYS_UNIT_CTRL: {
        if (a0 >= knunits) {
            ret2(t, KOS_EINVAL, 0);
            return;
        }
        if (!kunits[a0].local) {
            uint64_t p[3] = {0, a0, a1};
            k_rpc(unit_node((unsigned)a0), M_UNIT_CTRL, p, 3);
            return;
        }
        int st = k_unit_ctrl_start((unsigned)a0, (unsigned)a1);
        if (st) {
            ret2(t, st, 0);
            return;
        }
        k_block(t, W_CTRL, a0, 0, 0);
        return;
    }
    case SYS_MOVE: {
        uint64_t bytes = a2 * 32;
        uint64_t src = move_pa(a0, bytes), dst;
        if (a3 & 0x100) {
            unsigned node = a3 & 0xff;
            if (node >= kn.n || a1 + bytes > KOS_USER_LAND_SIZE || a1 + bytes < a1) {
                r = KOS_EINVAL;
                break;
            }
            dst = kn.peer_priv[node] + kn.peer_priv_size[node] - KOS_LAND_SIZE + a1;
        } else {
            dst = move_pa(a1, bytes);
        }
        if (!src || !dst) {
            r = KOS_EINVAL;
            break;
        }
        /* the mover reads and writes DRAM: the source's lines go out first, the
         * destination's stale lines are dropped after */
        hw_dcache(HW_DC_FLUSH);
        r = k_move(src, dst, a2);
        hw_dcache(HW_DC_FLUSH | HW_DC_INVAL);
        break;
    }
    case SYS_SEND: {
        uint64_t len = a3 & ~(uint64_t)KOS_SEND_NOWAIT;
        if (a1 > 0xffffffffUL || len > 40 || a0 >= kn.n || k_copy_in(t->sendbuf, a2, len)) {
            r = KOS_EINVAL;
            break;
        }
        if (k_chan_send(a0 << 32 | a1, t->sendbuf, (unsigned)len)) {
            if (a3 & KOS_SEND_NOWAIT) {
                r = KOS_EAGAIN;
                break;
            }
            k_block(t, W_SEND, a0 << 32 | a1, 0, 0);
            t->result = (int64_t)len;
            return;
        }
        r = KOS_OK;
        break;
    }
    case SYS_RECV:
        t->wa = a0;
        t->wb = a1;
        r = k_chan_take(t);
        if (r != -1) {
            break;
        }
        if (a2 == KOS_NOWAIT) {
            r = KOS_ETIME;
            break;
        }
        k_block(t, W_RECV, a0, a1, a2);
        return;
    case SYS_STAT:
        r = stat(a0, a1, a2);
        break;
    case SYS_RUN:
        run_user(t, a0, a1, a2);
        return;
    case SYS_BARRIER: {
        uint64_t req = k_req_id();
        k_block(t, W_REPLY, req, 0, 0);
        if (kn.me == 0) {
            k_bar_arrive(0, req, a0, a1);
        } else {
            uint64_t p[3] = {req, a0, a1};
            k_send(0, M_BAR, p, 3);
        }
        return;
    }
    case SYS_ENGINE:
        engine_user(t, a0, a1, a2);
        return;
    case SYS_SHUTDOWN: {
        uint64_t p[1] = {a0};
        for (unsigned j = 0; j < kn.n; ++j) {
            if (j != kn.me) {
                k_send(j, M_SHUTDOWN, p, 1);
            }
        }
        k_halt(a0);
    }
    case SYS_LAND:
        r = a0 == kn.me ? (int64_t)KOS_UV_LAND : KOS_EINVAL;
        break;
    case SYS_EVENT: {
        unsigned q = k_chan_count((uint32_t)(a3 >> 32));
        if ((int64_t)(k_events - a0) < 0 && !q) {
            k_block(t, W_EVENT, a0, a2, a1);
            t->result = (int64_t)a3;
            return;
        }
        int st = k_event_out(a2, a3 & 0xffffffff);
        ret2(t, st ? (int64_t)st : (int64_t)q, k_events);
        return;
    }
    case SYS_VTOP: {
        uint64_t pa = a1 ? move_pa(a0, a1) : 0;
        r = pa ? (int64_t)pa : KOS_EINVAL;
        break;
    }
    case SYS_MUNMAP:
        r = k_munmap(a0, a1 ? move_pa(a0, a1) : 0, a1);
        break;
    default:
        r = KOS_EINVAL;
        break;
    }
    t->x[10] = (uint64_t)r;
}
