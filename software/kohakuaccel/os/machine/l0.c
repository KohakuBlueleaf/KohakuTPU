/* L0, the machine layer: the boot of every node, the soft MMU's owner, and
 * the calls the kernel makes into machine mode. It runs untranslated and
 * never preempts: each call is a short, bounded piece of work. */
#include <kos/lib.h>

#include "l0.h"
#include "../kernel/bootstrap.h"

volatile struct kos_boot *const l0_boot = (volatile struct kos_boot *)KOS_BOOT_PA;
struct l0_mem l0_mem;

uint64_t l0_traps;
#define traps l0_traps

extern char __k_text_end[], __u_img_size[], __u_data_off[], __k_spad_start[], __k_spad_end[];
extern char __ushared_start[], __ushared_end[];
extern void l0_enter_kernel(uint64_t boot, uint64_t node) __attribute__((noreturn));

static const struct k_boot_machine boot = {
    .mem = &l0_mem, .boot = (volatile struct kos_boot *)KOS_BOOT_PA,
    .copy = &k_boot, .node = &kn, .mmu_init = mmu_init,
    .text_end = (uint64_t)__k_text_end, .data_off = (uint64_t)__u_data_off,
    .image_size = (uint64_t)__u_img_size,
    .shared_start = __ushared_start, .shared_end = __ushared_end,
    .say = l0_say, .halt = l0_halt, .map = mmu_map,
    .maps = mmu_maps, .tables = mmu_tables, .satp = mmu_satp,
    .enter_kernel = l0_enter_kernel,
};

#ifdef KOS_SMALL_CALL
#define L0_COLD __attribute__((minsize))
#else
#define L0_COLD
#endif

L0_COLD void l0_puts(const char *s)
{
    while (*s) {
        hw_wr(HW_R_CONSOLE, (unsigned char)*s++);
    }
}

L0_COLD void l0_say(const char *fmt, ...)
{
    char b[160];
    va_list ap;
    va_start(ap, fmt);
    kos_vsnprintf(b, sizeof b, fmt, ap);
    va_end(ap);
    l0_puts(b);
}

void l0_halt(uint64_t code)
{
    hw_wr(HW_R_EXIT, code);
    for (;;) {
    }
}

static L0_COLD void need(int ok, const char *what)
{
    if (!ok) {
        l0_say("L0 node %u: %s\n", (unsigned)l0_boot->node, what);
        l0_halt(0xB0);
    }
}

L0_COLD void l0_main(void)
{
    volatile struct kos_boot *b = l0_boot;
    if (b->magic != KOS_BOOT_MAGIC || b->version != KOS_BOOT_VERSION) {
        l0_puts("L0: no boot table\n");
        l0_halt(0xB0);
    }
    need(b->nnodes >= 1 && b->nnodes <= KOS_MAX_NODES && b->node < b->nnodes, "bad node count");
    need(b->nregions <= KOS_MAX_REGIONS && b->nunits <= KOS_MAX_UNITS, "bad table sizes");
    unsigned me = b->node;
    for (unsigned i = 0; i < b->nregions; ++i) {
        volatile struct kos_region *r = &b->region[i];
        if (r->kind == KOS_RG_DRAM_PRIV && r->owner == me) {
            l0_mem.priv = r->base;
            l0_mem.priv_size = r->size;
        }
    }
    need(l0_mem.priv_size >= 0x40000, "no private DRAM span");
    ((k_boot_machine_fn *)(l0_mem.priv + (uint64_t)k_boot_machine - KOS_UV_TEXT))(&boot);
}

/* ---- calls from the kernel ---- */
static int64_t call(struct mframe *f, uint64_t *val)
{
    uint64_t a0 = f->x[10], a1 = f->x[11], a2 = f->x[12], a3 = f->x[13];
    switch (f->x[17]) {
    case KOS_M_PUTC:
        hw_wr(HW_R_CONSOLE, a0 & 0xff);
        return KOS_OK;
    case KOS_M_WRITE: {
        if (a0 < (uint64_t)__k_spad_start || a1 > 4096 || a0 + a1 > (uint64_t)__k_spad_end) {
            return KOS_EFAULT;
        }
        for (uint64_t i = 0; i < a1; ++i) {
            hw_wr(HW_R_CONSOLE, ((volatile uint8_t *)a0)[i]);
        }
        return KOS_OK;
    }
#ifndef KOS_L0_FAST
    case KOS_M_SET_TIMER:
        csr_write(CSR_MTIMECMP, a0);
        return KOS_OK;
#endif
    case KOS_M_MAP:
        return mmu_map(a0, a1, a2, (unsigned)a3, 1);
    case KOS_M_UNMAP:
        return mmu_unmap(a0, a1);
#ifndef KOS_L0_FAST
    case KOS_M_QUERY:
        *val = mmu_query(a0);
        return KOS_OK;
#endif
    case KOS_M_EXIT:
        l0_halt(a0);
    case KOS_M_INFO:
        switch (a0) {
        case KOS_MI_MAPS:
            *val = mmu_maps();
            return KOS_OK;
        case KOS_MI_TABLES:
            *val = mmu_tables();
            return KOS_OK;
        case KOS_MI_TRAPS:
            *val = traps;
            return KOS_OK;
        case KOS_MI_POOL:
            *val = l0_mem.pool_pages;
            return KOS_OK;
        case KOS_MI_SATP:
            *val = mmu_satp();
            return KOS_OK;
        default:
            return KOS_EINVAL;
        }
#ifndef KOS_L0_FAST
    case KOS_M_NOP:
        return KOS_OK;
#endif
    default:
        return KOS_EINVAL;
    }
}

void l0_trap(struct mframe *f)
{
    uint64_t cause = csr_read(mcause);
    ++traps;
    if (cause == 9) {
        uint64_t val = 0;
        int64_t st = call(f, &val);
        f->x[10] = (uint64_t)st;
        f->x[11] = val;
        csr_write(mepc, csr_read(mepc) + 4);
        return;
    }
    l0_say("L0 node %u: machine trap cause %lx epc %lx tval %lx; mstatus %lx scause %lx sepc %lx stval %lx "
           "ra %lx sp %lx\n",
           (unsigned)l0_boot->node, cause, csr_read(mepc), csr_read(mtval), csr_read(mstatus), csr_read(scause),
           csr_read(sepc), csr_read(stval), f->x[1], f->x[2]);
    l0_halt(cause == (CAUSE_IRQ | 7) ? 0xE7 : 0xE0);
}
