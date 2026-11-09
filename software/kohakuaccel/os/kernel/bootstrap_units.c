/* Unit-table initialization in the private boot image. The host hardware
 * double uses the same entry with its allocator and register operations. */
#include "bootstrap.h"

void k_boot_units_init(const struct k_boot_kernel *c)
{
    const struct k_boot_units *u = c->units;
    uint64_t index_buffer = (uint64_t)(uintptr_t)c->alloc(u->index_bytes);
    c->dcache(HW_DC_FLUSH);
    struct pend *pending = c->alloc(u->pending_bytes);
    u->init(index_buffer, pending);
    *u->count = c->boot->nunits;
    for (unsigned i = 0; i < *u->count; ++i) {
        u->units[i].u = c->boot->unit[i];
        u->units[i].local = u->units[i].u.mesh == c->node->mesh;
    }
    hw_wr(HW_R_NM + 8 * HW_NM_DST, 0);
    c->poll_units();
}
