/* The kernel's boot on each node: the node's view of the machine from the
 * boot table L0 copied, memory, units, messages, then the first thread
 * (init, on node 0; every other node waits for work). */
#include "kernel.h"

struct kos_boot k_boot;

extern char __ushared_start[], __ushared_end[], __u_data_off[], __u_bss_start[], __u_bss_end[];

void k_halt(uint64_t code)
{
    k_where(KOS_W_DONE, code, 0);
    k_rings_drain();
    k_l0(KOS_M_EXIT, code, 0, 0, 0);
    for (;;) {
    }
}

void k_main(struct kos_boot *b, uint64_t node)
{
    (void)b;
    kn.me = (unsigned)node;
    kn.n = k_boot.nnodes;
    for (unsigned j = 0; j < kn.n; ++j) {
        kn.mesh_of[j] = k_boot.mesh_of[j];
    }
    kn.mesh = kn.mesh_of[kn.me];
    kn.flags = k_boot.flags;
    kn.tick = k_boot.tick;
    kn.engine = k_boot.engine != 0;
    for (unsigned i = 0; i < k_boot.nregions; ++i) {
        struct kos_region *r = &k_boot.region[i];
        if (r->kind == KOS_RG_DRAM_PRIV) {
            kn.peer_priv[r->owner] = r->base;
            kn.peer_priv_size[r->owner] = r->size;
            if (r->owner == kn.me) {
                kn.priv = r->base;
                kn.priv_size = r->size;
            }
        } else if (r->kind == KOS_RG_DRAM_SHARED) {
            kn.shared = r->base;
            kn.shared_size = r->size;
        }
    }
    k_mem_init();
    k_log_init();
    k_where(KOS_W_BOOT, 1, 0);
    k_user_satp = k_l0(KOS_M_INFO, KOS_MI_SATP, 0, 0, 0).val;
    /* the user image's bss (its data came with the image) */
    memset((void *)(kn.priv + (uint64_t)__u_data_off + ((uint64_t)__u_bss_start - KOS_UV_DATA)), 0,
           (uint64_t)(__u_bss_end - __u_bss_start));
    k_printf("KohakuAccel OS: node %u of %u (mesh %u), private %lx+%lx, shared %lx+%lx, %u units, flags %x, tick %lu\n",
             kn.me, kn.n, kn.mesh, kn.priv, kn.priv_size, kn.shared, kn.shared_size, k_boot.nunits, kn.flags,
             kn.tick);
    if (k_map(KOS_UV_LAND, kn.priv + kn.land_off, KOS_USER_LAND_SIZE,
              KOS_MAP_R | KOS_MAP_W | KOS_MAP_U | KOS_MAP_UNCACHED)) {
        k_panic("cannot map the landing zone");
    }
    if (kn.shared_size) {
        if (k_map(KOS_UV_SHUNC, kn.shared, kn.shared_size,
                  KOS_MAP_R | KOS_MAP_W | KOS_MAP_U | KOS_MAP_UNCACHED)) {
            k_panic("cannot map the shared region uncached");
        }
        if (kn.me == 0) {
            uint64_t used = (uint64_t)(__ushared_end - __ushared_start);
            uint64_t heap = KOS_ALIGN_UP(used, 64);
            volatile uint64_t *s = (volatile uint64_t *)(kn.shared | HW_UNC);
            for (uint64_t i = 0; i < heap / 8 + 8; ++i) {
                s[i] = 0;
            }
            s[heap / 8] = heap + 64; /* the shared heap's next free offset */
        }
    }
    k_where(KOS_W_BOOT, 2, 0);
    k_units_init();
    k_where(KOS_W_BOOT, 3, 0);
    k_msg_init();
    k_where(KOS_W_BOOT, 4, 0);
    uint64_t sie = 0;
    if (!(kn.flags & KOS_F_POLL)) {
        sie |= MIP_EXT;
    }
    if (kn.tick) {
        sie |= MIP_TIMER;
    }
    csr_set(sie, sie);
    if (kn.me == 0) {
        int app = k_find_app("init");
        if (app < 0 || k_run_local((unsigned)app, k_boot.cmdline) < 0) {
            k_panic("no init");
        }
    }
    struct thread *t = k_schedule();
    t->since = hw_cycles();
    k_resume(t);
}
