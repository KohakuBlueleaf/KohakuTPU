/* M0: what the hardware does under the questions the OS design depends on.
 * One image on every node; the boot table's cmdline names the probes:
 *   lat    access and maintenance latencies (node 0)
 *   ring   doorbell slots, counts and the round trip (nodes 0, 1)
 *   sc     store-buffering and message-passing litmus over uncached shared DRAM
 *   amo    concurrent AMOs and LR/SC on one shared word from every node
 *   walk   the Sv39 walker: A/D, page tables in DRAM, TLB staleness, superpages
 *   trap   S<->M and U<->S round trips, an injected S interrupt, the S timer
 *   itext  the same loops fetched from IMEM and from DRAM (2-line I-cache)
 * Lines "@ name value" are measurements; "FAIL" lines are broken expectations. */
#include <kos/boot.h>
#include <kos/hw.h>
#include <kos/lib.h>
#include <kos/abi.h>
#include <kos/start.h>

#define BOOT ((volatile struct kos_boot *)KOS_BOOT_PA)

static unsigned me, nn;
static uint64_t priv_pa, priv_size, shared_pa, shared_size;
static int fails;

/* ---------------------------------------------------------------- console */
/* Every byte also goes to the kernel's log ring format at the private span's
 * log offset (abi.h KOS_LOG_*: header, bytes from +64), written uncached,
 * which the host reads back after the run or a hang: a console the host
 * drains slower than this prints keeps only part. */
static volatile uint64_t *log_hdr;
static volatile uint8_t *log_bytes;
static volatile int lower; /* a probe runs in S or U: the log is not mapped there */
static uint64_t ecall1(uint64_t fn, uint64_t a0);
#define M0_ECALL_PUTCH 5

static void putch(char c)
{
    if (lower) {
        ecall1(M0_ECALL_PUTCH, (unsigned char)c);
        return;
    }
    hw_wr(HW_R_CONSOLE, (unsigned char)c);
    if (log_hdr) {
        uint64_t k = log_hdr[KOS_LOG_COUNT];
        log_bytes[k % (KOS_LOG_SIZE - 64)] = (uint8_t)c;
        log_hdr[KOS_LOG_COUNT] = k + 1;
    }
}
static void puts_(const char *s)
{
    while (*s) {
        putch(*s++);
    }
}
static void say(const char *fmt, ...)
{
    char b[160];
    va_list ap;
    va_start(ap, fmt);
    kos_vsnprintf(b, sizeof b, fmt, ap);
    va_end(ap);
    puts_(b);
}
static void report(const char *name, uint64_t v) { say("@ %s %lu\n", name, v); }
static void expect(const char *what, uint64_t got, uint64_t want)
{
    if (got != want) {
        ++fails;
        say("FAIL %s got %lx want %lx\n", what, got, want);
    }
}

/* ------------------------------------------------------- interlink helpers */
static uint32_t rings_written;
static void ring(unsigned node, unsigned slot, unsigned amount, int fence)
{
    uint64_t w = hw_sig_word();
    if (((rings_written - HW_SIG_SENT(w)) & 0xffffu) >= HW_RING_DEPTH) {
        kos_where(log_hdr, KOS_W_RING, rings_written, w);
        while (((rings_written - HW_SIG_SENT(hw_sig_word())) & 0xffffu) >= HW_RING_DEPTH) {
        }
    }
    hw_wr(HW_R_IL + HW_IL_RING, ((uint64_t)(fence != 0) << 32) | ((uint64_t)amount << 16) |
                                    ((uint64_t)slot << 8) | BOOT->mesh_of[node]);
    ++rings_written;
}

#define SLOT_BAR 15
static uint32_t bar_epoch;
/* Every node rings every other, fenced (a release), then waits for theirs. */
static void barrier(void)
{
    ++bar_epoch;
    kos_where(log_hdr, KOS_W_BAR, bar_epoch, hw_slot(SLOT_BAR));
    for (unsigned j = 0; j < nn; ++j) {
        if (j != me) {
            ring(j, SLOT_BAR, 1, 1);
        }
    }
    while ((int32_t)(hw_slot(SLOT_BAR) - bar_epoch * (nn - 1)) < 0) {
    }
}

static volatile uint64_t *unc(uint64_t pa) { return (volatile uint64_t *)(pa | HW_UNC); }

static void spin(uint64_t n)
{
    for (volatile uint64_t i = 0; i < n; ++i) {
    }
}

/* ---------------------------------------------------------------- lat */
static void p_lat(void)
{
    if (me != 0) {
        return;
    }
    volatile uint64_t *u = unc(priv_pa + 0x10000);
    volatile uint64_t *c = (volatile uint64_t *)(priv_pa + 0x20000);
    volatile uint64_t *s = (volatile uint64_t *)(HW_STAGING(BOOT->mesh_of[0]) + KOS_STG_END);
    uint64_t t, acc = 0;

    t = hw_cycles();
    for (int i = 0; i < 64; ++i) {
        acc += u[i];
    }
    report("lat.unc_load", (hw_cycles() - t) / 64);
    t = hw_cycles();
    for (int i = 0; i < 64; ++i) {
        u[i] = i;
    }
    report("lat.unc_store", (hw_cycles() - t) / 64);
    t = hw_cycles();
    for (int i = 0; i < 64; ++i) {
        acc += s[i];
    }
    report("lat.staging_load", (hw_cycles() - t) / 64);
    t = hw_cycles();
    for (int i = 0; i < 64; ++i) {
        s[i] = i;
    }
    report("lat.staging_store", (hw_cycles() - t) / 64);

    hw_dcache(HW_DC_FLUSH | HW_DC_INVAL);
    t = hw_cycles();
    for (int i = 0; i < 64; ++i) {
        acc += c[i * 4]; /* one line each: clean misses */
    }
    report("lat.cached_miss", (hw_cycles() - t) / 64);
    t = hw_cycles();
    for (int i = 0; i < 64; ++i) {
        acc += c[i * 4];
    }
    report("lat.cached_hit", (hw_cycles() - t) / 64);
    t = hw_cycles();
    for (int i = 0; i < 64; ++i) {
        c[i * 4] = i;
    }
    report("lat.cached_store_hit", (hw_cycles() - t) / 64);
    t = hw_cycles();
    for (int i = 0; i < 64; ++i) {
        acc += c[256 + i * 4]; /* the next 2 KB: each miss evicts a dirty line */
    }
    report("lat.cached_miss_dirty", (hw_cycles() - t) / 64);

    for (int i = 0; i < 64; ++i) {
        c[i * 4] = i; /* 64 dirty lines */
    }
    t = hw_cycles();
    hw_dcache(HW_DC_FLUSH);
    uint64_t tf = hw_cycles() - t;
    report("lat.flush_64_dirty", tf);
    expect("a flush lasts at least its 64-line scan", tf >= 64, 1);
    t = hw_cycles();
    hw_dcache(HW_DC_FLUSH);
    report("lat.flush_clean", hw_cycles() - t);
    t = hw_cycles();
    hw_dcache(HW_DC_INVAL);
    report("lat.invalidate", hw_cycles() - t);
    for (int i = 0; i < 64; ++i) {
        c[i * 4] = i;
    }
    t = hw_cycles();
    hw_dcache(HW_DC_FLUSH | HW_DC_INVAL);
    report("lat.flush_inval_64_dirty", hw_cycles() - t);
    expect("flushed word reads back uncached", unc(priv_pa + 0x20000)[4 * 63], 63);

    t = hw_cycles();
    for (int i = 0; i < 64; ++i) {
        acc += hw_rd(HW_R_MVSTAT);
    }
    report("lat.ctrl_read", (hw_cycles() - t) / 64);
    t = hw_cycles();
    for (int i = 0; i < 64; ++i) {
        acc += hw_slot((unsigned)i & 15);
    }
    report("lat.slot_select_read", (hw_cycles() - t) / 64);
    if (acc == 0x5a5a5a5a5a5a5a5aULL) {
        putch('?');
    }
}

/* ---------------------------------------------------------------- ring */
#define SLOT_PING 12
#define SLOT_PONG 13
static void p_ring(void)
{
    if (nn < 2 || me > 1) {
        barrier();
        barrier();
        return;
    }
    barrier();
    if (me == 0) {
        uint64_t db0 = hw_rd(HW_R_DBCNT);
        uint64_t t = hw_cycles();
        for (uint32_t k = 1; k <= 32; ++k) {
            ring(1, SLOT_PING, 1, 0);
            while (hw_slot(SLOT_PONG) < k) {
            }
        }
        report("ring.round_trip", (hw_cycles() - t) / 32);
        /* amounts and a consume */
        ring(1, SLOT_PING, 5, 0);
        while (hw_slot(SLOT_PONG) < 33) {
        }
        uint64_t db1 = hw_rd(HW_R_DBCNT);
        report("ring.dbcnt_from_mesh1_delta", ((db1 >> (16 * BOOT->mesh_of[1])) & 0xffff) -
                                                  ((db0 >> (16 * BOOT->mesh_of[1])) & 0xffff));
        /* 8 remote stores into node 1's staging, then a fenced ring */
        volatile uint64_t *r =
            (volatile uint64_t *)(HW_STAGING(BOOT->mesh_of[1]) + KOS_STG_END);
        t = hw_cycles();
        for (int i = 0; i < 8; ++i) {
            r[i] = 0x1111000000000000ULL | (uint64_t)i;
        }
        ring(1, SLOT_PING, 1, 1);
        uint64_t producer = hw_cycles() - t;
        while (hw_slot(SLOT_PONG) < 34) {
        }
        uint64_t rtt = hw_cycles() - t;
        report("ring.remote8_fenced_producer", producer);
        report("ring.remote8_round_trip", rtt);
    } else {
        uint64_t db0 = hw_rd(HW_R_DBCNT);
        for (uint32_t k = 1; k <= 32; ++k) {
            while (hw_slot(SLOT_PING) < k) {
            }
            ring(0, SLOT_PONG, 1, 0);
        }
        while (hw_slot(SLOT_PING) < 37) {
        }
        uint64_t db1 = hw_rd(HW_R_DBCNT);
        report("ring.dbcnt_on_node1_for_33_rings",
               ((db1 >> (16 * BOOT->mesh_of[0])) & 0xffff) - ((db0 >> (16 * BOOT->mesh_of[0])) & 0xffff));
        hw_wr(HW_R_IL + HW_IL_CONSUME, ((uint64_t)37 << 32) | SLOT_PING);
        expect("slot after consume", hw_slot(SLOT_PING), 0);
        ring(0, SLOT_PONG, 1, 0);
        while (hw_slot(SLOT_PING) < 1) {
        }
        volatile uint64_t *r =
            (volatile uint64_t *)(HW_STAGING(BOOT->mesh_of[1]) + KOS_STG_END);
        for (int i = 0; i < 8; ++i) {
            expect("remote store landed before the fenced ring", r[i],
                   0x1111000000000000ULL | (uint64_t)i);
        }
        ring(0, SLOT_PONG, 1, 0);
    }
    barrier();
}

/* ---------------------------------------------------------------- sc */
#define SC_ITERS 256
static void p_sc(void)
{
    if (!shared_size || nn < 2) {
        if (me == 0) {
            say("sc: no shared DRAM on this board\n");
        }
        return;
    }
    volatile uint64_t *x = unc(shared_pa + 0x0), *y = unc(shared_pa + 0x40);
    volatile uint64_t *res = unc(shared_pa + 0x80);
    volatile uint64_t *data = unc(shared_pa + 0x100), *flag = unc(shared_pa + 0x140);
    uint64_t st = 0x1234 + me, sb_both0 = 0, mp_bad = 0;
    for (unsigned it = 0; it < SC_ITERS; ++it) {
        if (me == 0) {
            *x = 0;
            *y = 0;
            *res = ~0ULL;
        }
        barrier();
        spin(kos_mix(&st) & 15);
        if (me == 0) {
            *x = 1;
            uint64_t r1 = *y;
            while (*res == ~0ULL) {
            }
            sb_both0 += (r1 == 0 && *res == 0);
        } else if (me == 1) {
            *y = 1;
            *res = *x;
        }
        barrier();
        if (me == 0) {
            *data = it * 3 + 1;
            *flag = it + 1;
        } else if (me == 1) {
            while (*flag != it + 1) {
            }
            mp_bad += (*data != it * 3 + 1);
        }
        barrier();
    }
    if (me == 0) {
        report("sc.store_buffering_both_zero", sb_both0);
    } else if (me == 1) {
        report("sc.message_passing_stale", mp_bad);
    }
    barrier();
}

/* ---------------------------------------------------------------- amo */
#define AMO_ITERS 2000
static void p_amo(void)
{
    if (!shared_size || nn < 2) {
        return;
    }
    volatile uint64_t *a = unc(shared_pa + 0x200), *l = unc(shared_pa + 0x240);
    uint64_t sc_fail = 0;
    if (me == 0) {
        *a = 0;
        *l = 0;
    }
    barrier();
    for (int i = 0; i < AMO_ITERS; ++i) {
        __atomic_fetch_add(a, 1, __ATOMIC_SEQ_CST);
    }
    for (int i = 0; i < AMO_ITERS; ++i) {
        uint64_t v;
        int bad;
        do {
            __asm__ volatile("lr.d %0, (%2)\n\taddi %0, %0, 1\n\tsc.d %1, %0, (%2)"
                             : "=&r"(v), "=&r"(bad)
                             : "r"(l)
                             : "memory");
            sc_fail += (bad != 0);
        } while (bad);
    }
    barrier();
    if (me == 0) {
        report("amo.expected", (uint64_t)AMO_ITERS * nn);
        report("amo.amoadd_total", *a);
        report("amo.lrsc_total", *l);
    }
    report("amo.sc_failures_this_node", sc_fail);
    barrier();
}

/* ---------------------------------------------------------------- walk */
static volatile uint64_t s_cause, s_tval, s_faults, t_hit, t_timer_at;

static uint64_t ecall1(uint64_t fn, uint64_t a0)
{
    register uint64_t r0 __asm__("a0") = a0;
    register uint64_t r7 __asm__("a7") = fn;
    __asm__ volatile("ecall" : "+r"(r0) : "r"(r7) : "memory");
    return r0;
}

/* Every supervisor trap of the probes: interrupts are the trap probe's,
 * a U ecall is a syscall (0xFF hands control back to M), anything else is a
 * fault the walk probe provoked and steps over. */
uint64_t m0_strap(uint64_t cause, uint64_t epc, uint64_t tval, uint64_t a7)
{
    (void)epc;
    if (cause == CAUSE_SSI) {
        t_hit = hw_cycles();
        ecall1(3, 0);
        return 0;
    }
    if (cause == CAUSE_STI) {
        t_timer_at = hw_time();
        ecall1(4, ~0UL);
        return 0;
    }
    if (cause == 8) {
        if (a7 == 0xFF) {
            ecall1(0xFF, 0);
        }
        return 4;
    }
    s_cause = cause;
    s_tval = tval;
    ++s_faults;
    return 4;
}

extern void m0_strap_entry(void);
extern uint64_t m0_enter(void (*fn)(uint64_t), uint64_t arg, uint64_t sp, uint64_t mpp);

/* A probe below M prints through M (putch). */
static void enter(void (*fn)(uint64_t), uint64_t arg, uint64_t sp, uint64_t mpp)
{
    lower = 1;
    m0_enter(fn, arg, sp, mpp);
    lower = 0;
}
extern void m0_resume(uint64_t v);

#define PTE_V 1UL
#define PTE_R 2UL
#define PTE_W 4UL
#define PTE_X 8UL
#define PTE_U 16UL
#define PTE_G 32UL
#define PTE_A 64UL
#define PTE_D 128UL
static uint64_t pte(uint64_t pa, uint64_t flags) { return ((pa >> 12) << 10) | flags; }

/* VA layout of the walk probe: low 2 MB identity (imem, spad, ctrl) through
 * l1[0]; the test page at 2 MB through l1[1]; a 2 MB alias of the staging
 * page tables at 4 MB (l1[2]); a 2 MB alias of private DRAM at 6 MB (l1[3]). */
#define TV     0x200000UL
#define VA_STG 0x400000UL
#define VA_DRM 0x600000UL

static volatile uint64_t *s_l0t, *s_dram_l0;
static uint64_t frame_a, frame_b;

static void walk_s(uint64_t arg)
{
    (void)arg;
    volatile uint64_t *v = (volatile uint64_t *)TV;
    uint64_t got;
    s_l0t[0] = pte(frame_a, PTE_V | PTE_R | PTE_W);
    __asm__ volatile("sfence.vma" ::: "memory");
    s_faults = 0;
    got = *v;
    report("walk.load_A0_faults", s_faults);
    report("walk.load_A0_cause", s_faults ? s_cause : 0);
    report("walk.pte_after_load", s_l0t[0] & 0xff);
    s_l0t[0] = pte(frame_a, PTE_V | PTE_R | PTE_W | PTE_A);
    __asm__ volatile("sfence.vma" ::: "memory");
    s_faults = 0;
    *v = 0x77;
    report("walk.store_D0_faults", s_faults);
    report("walk.store_D0_cause", s_faults ? s_cause : 0);
    report("walk.pte_after_store", s_l0t[0] & 0xff);
    s_l0t[0] = pte(frame_a, PTE_V | PTE_R | PTE_W | PTE_A | PTE_D);
    __asm__ volatile("sfence.vma" ::: "memory");
    s_faults = 0;
    *v = 0xA5;
    got = *v;
    expect("walk.AD_store_load", got, 0xA5);
    expect("walk.AD_no_fault", s_faults, 0);
    hw_dcache(HW_DC_FLUSH | HW_DC_INVAL);
    s_l0t[0] = pte(frame_a | HW_UNC, PTE_V | PTE_R | PTE_W | PTE_A | PTE_D);
    __asm__ volatile("sfence.vma" ::: "memory");
    *v = 0xAAAA;
    s_l0t[0] = pte(frame_b | HW_UNC, PTE_V | PTE_R | PTE_W | PTE_A | PTE_D);
    __asm__ volatile("sfence.vma" ::: "memory");
    *v = 0xBBBB;
    s_l0t[0] = pte(frame_a | HW_UNC, PTE_V | PTE_R | PTE_W | PTE_A | PTE_D);
    __asm__ volatile("sfence.vma" ::: "memory");
    expect("walk.frame_a", *v, 0xAAAA);
    s_l0t[0] = pte(frame_b | HW_UNC, PTE_V | PTE_R | PTE_W | PTE_A | PTE_D);
    got = *v;
    report("walk.no_sfence_reads_old_frame", got == 0xAAAA);
    __asm__ volatile("sfence.vma" ::: "memory");
    got = *v;
    expect("walk.after_sfence_new_frame", got, 0xBBBB);
}

static void walk_dram_s(uint64_t arg)
{
    (void)arg;
    volatile uint64_t *v = (volatile uint64_t *)TV;
    s_faults = 0;
    s_dram_l0[0] = pte(frame_a, PTE_V | PTE_R | PTE_W | PTE_A | PTE_D);
    __asm__ volatile("sfence.vma" ::: "memory");
    (void)*v;
    report("walk.dram_pte_cached_unflushed_faults", s_faults);
    hw_dcache(HW_DC_FLUSH);
    __asm__ volatile("sfence.vma" ::: "memory");
    s_faults = 0;
    (void)*v;
    report("walk.dram_pte_after_flush_faults", s_faults);
}

static void misaligned_s(uint64_t arg)
{
    (void)arg;
    s_faults = 0;
    (void)*(volatile uint64_t *)0x40000000UL;
    report("walk.misaligned_gigapage_faults", s_faults);
}

static uint64_t stack_s[512] __attribute__((aligned(16)));
static uint64_t stack_u[512] __attribute__((aligned(16)));

static void p_walk(void)
{
    if (me != 0) {
        return;
    }
    uint64_t stg = HW_STAGING(BOOT->mesh_of[0]) + KOS_STG_PT;
    volatile uint64_t *root = (volatile uint64_t *)stg;
    volatile uint64_t *l1 = (volatile uint64_t *)(stg + 0x1000);
    volatile uint64_t *l0 = (volatile uint64_t *)(stg + 0x2000);
    for (int i = 0; i < 4 * 512; ++i) {
        root[i] = 0;
    }
    frame_a = priv_pa + 0x40000;
    frame_b = priv_pa + 0x41000;
    root[0] = pte(stg + 0x1000, PTE_V);
    l1[0] = pte(stg + 0x2000, PTE_V);
    l1[1] = pte(stg + 0x3000, PTE_V);
    for (uint64_t pa = 0; pa < 0x28000; pa += 0x1000) {
        l0[pa >> 12] = pte(pa, PTE_V | PTE_R | PTE_W | PTE_X | PTE_A | PTE_D | PTE_G);
    }
    l1[2] = pte(stg & ~0x1FFFFFUL, PTE_V | PTE_R | PTE_W | PTE_A | PTE_D);
    l1[3] = pte(priv_pa & ~0x1FFFFFUL, PTE_V | PTE_R | PTE_W | PTE_A | PTE_D);
    root[1] = pte(0x1000, PTE_V | PTE_R | PTE_W | PTE_A | PTE_D); /* a gigapage, PPN not 1 GB aligned */
    s_l0t = (volatile uint64_t *)(VA_STG + ((stg + 0x3000) & 0x1FFFFF));

    csr_write(stvec, (uint64_t)m0_strap_entry);
    csr_write(medeleg, (1UL << 12) | (1UL << 13) | (1UL << 15));
    csr_write(satp, (8UL << 60) | (stg >> 12));
    __asm__ volatile("sfence.vma" ::: "memory");

    enter(walk_s, 0, (uint64_t)&stack_s[512], 1);
    enter(misaligned_s, 0, (uint64_t)&stack_s[512], 1);

    uint64_t dl0 = priv_pa + 0x50000;
    for (int i = 0; i < 512; ++i) {
        unc(dl0)[i] = 0;
    }
    hw_dcache(HW_DC_FLUSH | HW_DC_INVAL);
    l1[1] = pte(dl0, PTE_V);
    s_dram_l0 = (volatile uint64_t *)(VA_DRM + (dl0 & 0x1FFFFF));
    __asm__ volatile("sfence.vma" ::: "memory");
    enter(walk_dram_s, 0, (uint64_t)&stack_s[512], 1);
    csr_write(satp, 0);
    __asm__ volatile("sfence.vma" ::: "memory");
    csr_write(medeleg, 0);
}

/* ---------------------------------------------------------------- trap */
static void trap_s(uint64_t arg)
{
    (void)arg;
    uint64_t t = hw_cycles();
    for (int i = 0; i < 64; ++i) {
        ecall1(1, 0);
    }
    report("trap.s_m_ecall_round_trip", (hw_cycles() - t) / 64);

    csr_set(sie, MIP_SOFT | MIP_TIMER);
    csr_set(sstatus, 2);
    uint64_t tot = 0;
    for (int i = 0; i < 16; ++i) {
        t_hit = 0;
        t = hw_cycles();
        ecall1(2, 0);
        while (!t_hit) {
        }
        tot += t_hit - t;
    }
    report("trap.injected_ssi_latency", tot / 16);
    tot = 0;
    uint64_t worst = 0;
    for (int i = 0; i < 16; ++i) {
        t_timer_at = 0;
        uint64_t target = hw_time() + 200;
        ecall1(4, target);
        while (!t_timer_at) {
        }
        uint64_t d = t_timer_at - target;
        tot += d;
        worst = d > worst ? d : worst;
    }
    report("trap.s_timer_late_avg", tot / 16);
    report("trap.s_timer_late_worst", worst);
    csr_clear(sstatus, 2);
    csr_clear(sie, MIP_SOFT | MIP_TIMER);
}

static void u_loop(uint64_t n)
{
    uint64_t t = hw_cycles();
    for (uint64_t i = 0; i < n; ++i) {
        register uint64_t r7 __asm__("a7") = 0;
        __asm__ volatile("ecall" ::"r"(r7) : "memory");
    }
    report("trap.u_s_ecall_round_trip", (hw_cycles() - t) / n);
}

/* The machine side of every probe that drops below M. */
void m0_trap(uint64_t *f)
{
    uint64_t cause = csr_read(mcause);
    uint64_t a7 = f[17], a0 = f[10];
    if (cause == 9 || cause == 8) {
        f[32] += 4;
        switch (a7) {
        case 0xFF:
            m0_resume(a0);
            break;
        case 2:
            csr_set(mip, MIP_SOFT);
            break;
        case 3:
            csr_clear(mip, MIP_SOFT);
            break;
        case 4:
            csr_write(0x7C0, a0);
            break;
        case M0_ECALL_PUTCH:
            lower = 0;
            putch((char)a0);
            lower = 1;
            break;
        default:
            break;
        }
        return;
    }
    say("FAIL machine trap cause %lx epc %lx tval %lx ra %lx sp %lx mstatus %lx\n", cause, f[32],
        csr_read(mtval), f[1], f[2], csr_read(mstatus));
    hw_wr(HW_R_EXIT, 0xEE);
    for (;;) {
    }
}

static void p_trap(void)
{
    if (me != 0) {
        return;
    }
    csr_write(stvec, (uint64_t)m0_strap_entry);
    csr_write(mideleg, MIP_SOFT | MIP_TIMER);
    csr_set(mie, MIP_SOFT | MIP_TIMER);
    enter(trap_s, 0, (uint64_t)&stack_s[512], 1);
    csr_write(mideleg, 0);
    csr_clear(mie, MIP_SOFT | MIP_TIMER);
    csr_write(0x7C0, ~0UL);
    csr_write(medeleg, 1UL << 8);
    enter(u_loop, 64, (uint64_t)&stack_u[512], 0);
    csr_write(medeleg, 0);
}

/* ---------------------------------------------------------------- itext */
#define LOOPS(SFX, SEC)                                                                    \
    __attribute__((noinline, section(SEC))) uint64_t tiny_##SFX(uint64_t n)                \
    {                                                                                      \
        uint64_t a = 1;                                                                    \
        for (uint64_t i = 0; i < n; ++i) {                                                 \
            a = a * 3 + i;                                                                 \
        }                                                                                  \
        return a;                                                                          \
    }                                                                                      \
    __attribute__((noinline, section(SEC))) uint64_t wide_##SFX(uint64_t n)                \
    {                                                                                      \
        uint64_t a = 1, b = 2, c = 3, d = 4;                                               \
        for (uint64_t i = 0; i < n; ++i) {                                                 \
            a = a * 3 + i;                                                                 \
            b ^= a >> 3;                                                                   \
            c += b * 5;                                                                    \
            d = (d << 7) | (d >> 57);                                                      \
            a += d ^ c;                                                                    \
            b = b * 7 + (c >> 11);                                                         \
            c ^= a + b;                                                                    \
            d += a ^ (b << 2);                                                             \
            a ^= c >> 5;                                                                   \
            b += d * 9;                                                                    \
            c = c * 13 + a;                                                                \
            d ^= b >> 17;                                                                  \
        }                                                                                  \
        return a ^ b ^ c ^ d;                                                              \
    }                                                                                      \
    __attribute__((noinline, section(SEC))) static uint64_t leaf_##SFX(uint64_t x)         \
    {                                                                                      \
        return (x ^ (x >> 7)) * 0x9E37;                                                    \
    }                                                                                      \
    __attribute__((noinline, section(SEC))) uint64_t calls_##SFX(uint64_t n)               \
    {                                                                                      \
        uint64_t a = 1;                                                                    \
        for (uint64_t i = 0; i < n; ++i) {                                                 \
            a += leaf_##SFX(a + i);                                                        \
        }                                                                                  \
        return a;                                                                          \
    }

LOOPS(imem, ".text.loops")
LOOPS(dram, ".dtext")

static void p_itext(void)
{
    if (me != 0) {
        return;
    }
    struct {
        const char *name;
        uint64_t (*i)(uint64_t), (*d)(uint64_t);
    } k[] = {
        {"tiny", tiny_imem, tiny_dram},
        {"wide", wide_imem, wide_dram},
        {"calls", calls_imem, calls_dram},
    };
    for (unsigned j = 0; j < KOS_ARRAY(k); ++j) {
        uint64_t t = hw_cycles();
        uint64_t a = k[j].i(1000);
        uint64_t ti = hw_cycles() - t;
        t = hw_cycles();
        uint64_t b = k[j].d(1000);
        uint64_t td = hw_cycles() - t;
        expect("itext same result", a, b);
        char n1[32], n2[32];
        kos_snprintf(n1, sizeof n1, "itext.%s.imem_x1000", k[j].name);
        kos_snprintf(n2, sizeof n2, "itext.%s.dram_x1000", k[j].name);
        report(n1, ti);
        report(n2, td);
    }
}

/* ---------------------------------------------------------------- main */
int main(void)
{
    if (BOOT->magic != KOS_BOOT_MAGIC) {
        puts_("m0: no boot table\n");
        return 3;
    }
    me = BOOT->node;
    nn = BOOT->nnodes;
    for (unsigned i = 0; i < BOOT->nregions; ++i) {
        volatile struct kos_region *r = &BOOT->region[i];
        if (r->kind == KOS_RG_DRAM_PRIV && r->owner == me) {
            priv_pa = r->base;
            priv_size = r->size;
        } else if (r->kind == KOS_RG_DRAM_SHARED) {
            shared_pa = r->base;
            shared_size = r->size;
        }
    }
    log_hdr = unc(priv_pa + KOS_PRIV_LOG_OFF(priv_size));
    log_bytes = (volatile uint8_t *)(log_hdr + 8);
    for (unsigned i = 0; i < 8; ++i) {
        log_hdr[i] = 0;
    }
    say("m0 node %u of %u mesh %u priv %lx+%lx shared %lx+%lx\n", me, nn, BOOT->mesh_of[me],
        priv_pa, priv_size, shared_pa, shared_size);
    /* Fresh slots and ring count on every node before anyone rings (start.h). */
    kos_start(BOOT, &rings_written, log_hdr);
    barrier();
    char cmd[KOS_CMDLINE];
    for (unsigned i = 0; i < KOS_CMDLINE; ++i) {
        cmd[i] = BOOT->cmdline[i];
    }
    const char *names[] = {"lat", "ring", "sc", "amo", "walk", "trap", "itext"};
    void (*fns[])(void) = {p_lat, p_ring, p_sc, p_amo, p_walk, p_trap, p_itext};
    for (unsigned j = 0; j < KOS_ARRAY(names); ++j) {
        size_t n = strlen(names[j]);
        for (const char *p = cmd; *p; ++p) {
            if (!strncmp(p, names[j], n) && (p[n] == ' ' || p[n] == 0) &&
                (p == cmd || p[-1] == ' ')) {
                kos_where(log_hdr, KOS_W_PROBE, j, 0);
                fns[j]();
                barrier();
                break;
            }
        }
    }
    say("m0 node %u done, %d failures\n", me, fails);
    kos_where(log_hdr, KOS_W_DONE, (uint64_t)fails, 0);
    return fails;
}
