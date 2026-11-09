/* Boot-only, untranslated execution from the node's private DRAM image.
 * Text and local constants move together. Resident state, linker values and
 * callbacks arrive through immutable descriptors in scratchpad. */
#include "bootstrap.h"
#include <kos/start.h>

#define WATCHDOG 1000000

static void boot_node(struct knode *n, const struct kos_boot *b)
{
    n->me = b->node;
    n->n = b->nnodes;
    for (unsigned j = 0; j < n->n; ++j) {
        n->mesh_of[j] = b->mesh_of[j];
    }
    n->mesh = n->mesh_of[n->me];
    n->flags = b->flags;
    n->tick = b->tick;
    n->engine = b->engine != 0;
    for (unsigned i = 0; i < b->nregions; ++i) {
        const struct kos_region *r = &b->region[i];
        if (r->kind == KOS_RG_DRAM_PRIV) {
            n->peer_priv[r->owner] = r->base;
            n->peer_priv_size[r->owner] = r->size;
            if (r->owner == n->me) {
                n->priv = r->base;
                n->priv_size = r->size;
            }
        } else if (r->kind == KOS_RG_DRAM_SHARED) {
            n->shared = r->base;
            n->shared_size = r->size;
            n->shared_route_mask = r->attr & (3UL << HW_MESH_SHIFT);
        } else if (r->kind == KOS_RG_STAGING && r->size > KOS_STG_END) {
            unsigned mesh = n->mesh_of[r->owner];
            n->stage_user[mesh] = r->base + KOS_STG_END;
            n->stage_size[mesh] = r->size - KOS_STG_END;
        }
    }
    n->shared_user_size = (n->flags & KOS_F_SHARED_PHYS) ? 0 : n->shared_size;
}

static void need(const struct k_boot_machine *c, int ok, const char *what)
{
    if (!ok) {
        c->say("L0 node %u: %s\n", (unsigned)c->boot->node, what);
        c->halt(0xB0);
    }
}

static void map_or_halt(const struct k_boot_machine *c, uint64_t va, uint64_t pa,
                        uint64_t size, unsigned attr)
{
    int r = c->map(va, pa, size, attr, 0);
    if (r) {
        c->say("L0: map %lx -> %lx (%lx) failed %d\n", va, pa, size, r);
        c->halt(0xB1);
    }
}

void k_boot_machine(const struct k_boot_machine *c)
{
    volatile struct kos_boot *b = c->boot;
    struct l0_mem *m = c->mem;
    unsigned me = b->node, mesh = b->mesh_of[me];
    for (unsigned i = 0; i < b->nregions; ++i) {
        volatile struct kos_region *r = &b->region[i];
        if (r->kind == KOS_RG_DRAM_SHARED) {
            m->shared = r->base;
            m->shared_size = r->size;
        } else if (r->kind == KOS_RG_STAGING) {
            unsigned id = (unsigned)((r->base >> HW_MESH_SHIFT) & 3);
            m->staging[id] = r->base;
            m->staging_size[id] = r->size;
        }
    }
    need(c, m->staging_size[mesh] >= KOS_STG_END, "staging too small");
    if (b->flags & KOS_F_PT_DRAM) {
        m->pool_dram = 1;
        m->pool_pages = KOS_STG_PTSIZE >> 12;
        m->pool = m->priv + KOS_PRIV_PT_OFF(m->priv_size);
    } else {
        m->pool = m->staging[mesh] + KOS_STG_PT;
        m->pool_pages = KOS_STG_PTSIZE >> 12;
    }
    c->mmu_init();

    /* Trap entry and user return fetch kernel text while translation is on. */
    uint64_t ktext = KOS_ALIGN_UP(c->text_end, 0x1000);
    uint64_t udoff = c->data_off, uimg = c->image_size;
    need(c, uimg < KOS_PRIV_PT_OFF(m->priv_size), "user image larger than the private span");
    map_or_halt(c, 0, 0, ktext, KOS_MAP_R | KOS_MAP_X);
    map_or_halt(c, KOS_UV_TEXT, m->priv, udoff, KOS_MAP_R | KOS_MAP_X | KOS_MAP_U);
    if (uimg > udoff) {
        map_or_halt(c, KOS_UV_DATA, m->priv + udoff, uimg - udoff, KOS_MAP_R | KOS_MAP_W | KOS_MAP_U);
    }
    if (m->shared_size && !(b->flags & KOS_F_SHARED_PHYS)) {
        unsigned ua = KOS_MAP_R | KOS_MAP_W | KOS_MAP_U;
        if (b->flags & KOS_F_SHARED_UNCACHED) {
            ua |= KOS_MAP_UNCACHED;
        }
        map_or_halt(c, KOS_UV_SHARED, m->shared, m->shared_size, ua);
    }
    need(c, !m->shared_size || (uint64_t)(c->shared_end - c->shared_start) <= m->shared_size,
         "shared section larger than the shared region");

    volatile uint8_t *src = (volatile uint8_t *)b;
    uint8_t *dst = (uint8_t *)c->copy;
    for (unsigned i = 0; i < sizeof(struct kos_boot); ++i) {
        dst[i] = src[i];
    }
    boot_node(c->node, c->copy);

    csr_write(medeleg, (1UL << 0) | (1UL << 2) | (1UL << 3) | (1UL << 4) | (1UL << 6) | (1UL << 8) |
                           (1UL << 12) | (1UL << 13) | (1UL << 15));
    csr_write(mideleg, MIP_SOFT | MIP_TIMER | MIP_EXT);
    csr_write(mie, MIP_SOFT | MIP_TIMER | MIP_EXT);
    csr_write(CSR_MTIMECMP, ~0UL);
    csr_write(satp, 0);
    __asm__ volatile("sfence.vma" ::: "memory");
    if (b->flags & KOS_F_TRACE) {
        csr_write(mideleg, MIP_SOFT | MIP_EXT);
        csr_write(CSR_MTIMECMP, hw_time() + WATCHDOG);
        c->say("L0 node %u: %lu tables, %lu maps, satp %lx, entering the kernel\n",
               me, c->tables(), c->maps(), c->satp());
    }
    c->enter_kernel((uint64_t)c->copy, me);
}

static void boot_memory(const struct k_boot_kernel *c)
{
    struct knode *n = c->node;
    const struct k_boot_memory *m = c->memory;
    n->land_off = KOS_PRIV_LAND_OFF(n->priv_size);
    n->log_off = KOS_PRIV_LOG_OFF(n->priv_size);
    n->frames_end = (n->flags & KOS_F_PT_DRAM) ? KOS_PRIV_PT_OFF(n->priv_size) : n->log_off;
    uint64_t frames = n->frames_end >> 12;
    if (frames > m->max_frames) {
        frames = m->max_frames;
    }
    m->init(frames, c->image_size >> 12, KOS_UV_MMAP + (uint64_t)n->me * 0x10000000UL);
}

static void boot_messages(const struct k_boot_kernel *c)
{
    const struct k_boot_messages *m = c->messages;
    struct kmsg *inbox = c->alloc(K_INBOX * sizeof *inbox);
    struct chanmsg *queue = c->alloc(K_CHANQ * sizeof *queue);
    struct kbar *barriers = 0;
#ifdef KOS_CHAN_FIFO
    for (unsigned i = 1; i < K_CHANQ; ++i) {
        m->table[i].next = i + 1 < K_CHANQ ? i + 1 : 0;
        queue[i].pad = m->table[i].next;
    }
#endif
    if (c->node->me == 0) {
        barriers = c->alloc(K_BAR * sizeof *barriers);
    }
    uint32_t rings;
    kos_start(c->boot, &rings, c->log_header());
    m->init(inbox, queue, barriers, (uint64_t)c->node->me << 56, rings);
}

void k_boot_kernel(const struct k_boot_kernel *c)
{
    struct knode *n = c->node;
    boot_memory(c);
    c->log_init();
    c->where(KOS_W_BOOT, 1, 0);
    *c->user_satp = c->l0(KOS_M_INFO, KOS_MI_SATP, 0, 0, 0).val;
    c->zero((void *)(n->priv + c->data_off + ((uint64_t)c->bss_start - KOS_UV_DATA)), 0,
            (uint64_t)(c->bss_end - c->bss_start));
    c->print("KohakuAccel OS: node %u of %u (mesh %u), private %lx+%lx, shared %lx+%lx, %u units, flags %x, tick %lu\n",
             n->me, n->n, n->mesh, n->priv, n->priv_size, n->shared, n->shared_size, c->boot->nunits, n->flags,
             n->tick);
    if (c->map(KOS_UV_LAND, n->priv + n->land_off, KOS_USER_LAND_SIZE,
               KOS_MAP_R | KOS_MAP_W | KOS_MAP_U | KOS_MAP_UNCACHED)) {
        c->panic("cannot map the landing zone");
    }
    if (n->shared_user_size) {
        if (c->map(KOS_UV_SHUNC, n->shared, n->shared_user_size,
                   KOS_MAP_R | KOS_MAP_W | KOS_MAP_U | KOS_MAP_UNCACHED)) {
            c->panic("cannot map the shared region uncached");
        }
        if (n->me == 0) {
            uint64_t used = (uint64_t)(c->shared_end - c->shared_start);
            uint64_t heap = KOS_ALIGN_UP(used, 64);
            volatile uint64_t *s = (volatile uint64_t *)(n->shared | HW_UNC);
            for (uint64_t i = 0; i < heap / 8 + 8; ++i) {
                s[i] = 0;
            }
            s[heap / 8] = heap + 64;
        }
    }
    c->where(KOS_W_BOOT, 2, 0);
    k_boot_units_init(c);
    c->where(KOS_W_BOOT, 3, 0);
    boot_messages(c);
    c->where(KOS_W_BOOT, 4, 0);
    uint64_t sie = 0;
    if (!(n->flags & KOS_F_POLL)) {
        sie |= MIP_EXT;
    }
    if (n->tick) {
        sie |= MIP_TIMER;
    }
    csr_set(sie, sie);
    if (n->me == 0) {
        int app = c->find_app("init");
        if (app < 0 || c->run_local((unsigned)app, c->boot->cmdline) < 0) {
            c->panic("no init");
        }
    }
    struct thread *t = c->schedule();
    t->since = hw_cycles();
    c->resume(t);
}
