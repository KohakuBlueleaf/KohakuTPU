/* Physical DMA admission and the distinct user-visible shared aliases. */
#include <assert.h>
#include <stdlib.h>

#include "../../../../kohakuaccel/os/kernel/kernel.h"

char __u_img_size[1];

struct sbiret k_l0(uint64_t fn, uint64_t a0, uint64_t a1, uint64_t a2, uint64_t a3)
{
    (void)fn;
    (void)a0;
    (void)a1;
    (void)a2;
    (void)a3;
    abort();
}

void k_panic(const char *fmt, ...)
{
    (void)fmt;
    abort();
}

int main(void)
{
    kn.stage_user[1] = HW_STAGING(1) + KOS_STG_END;
    kn.stage_size[1] = 0x200000 - KOS_STG_END;
    assert(k_pa_ok(HW_STAGING(1) + 0x100000, 256));
    assert(!k_pa_ok(HW_STAGING(1) + KOS_STG_PT, 256));
    assert(!k_pa_ok(HW_STAGING(1) + 0x200000 - 128, 256));
    assert(!k_pa_ok(HW_STAGING(2) + 0x100000, 256));
    kn.shared = 0x50000000;
    kn.shared_size = 1UL << 31;
    kn.shared_user_size = 0;
    assert(k_pa_ok(kn.shared + (1UL << 30), 256));
    assert(!k_shared_unc(KOS_UV_MMAP));
    kn.shared_route_mask = 3UL << HW_MESH_SHIFT;
    for (unsigned mesh = 0; mesh < KOS_MAX_NODES; ++mesh) {
        uint64_t route = (uint64_t)mesh << HW_MESH_SHIFT;
        assert(k_pa_ok(kn.shared | route, 32));
        assert(k_pa_ok((kn.shared + kn.shared_size - 32) | route, 32));
        assert(!k_pa_ok((kn.shared + kn.shared_size - 32) | route, 33));
        assert(!k_pa_ok((kn.shared - 32) | route, 32));
        assert(!k_pa_ok(0x20000000 | route, 32));
    }
    assert(!k_pa_ok((1UL << HW_MESH_SHIFT) - 16, 32));
    assert(!k_pa_ok(HW_STAGING(2) + 0x100000, 32));
    kn.shared_route_mask = 0;
    assert(!k_pa_ok(kn.shared | (1UL << HW_MESH_SHIFT), 32));
    assert(!k_pa_ok(kn.shared, 0));
    assert(!k_pa_ok(UINT64_MAX - 15, 32));
    /* With no declared alias bits a region across the numeric boundary is
     * ordinary shared memory, whole. */
    kn.shared = (1UL << HW_MESH_SHIFT) - 4096;
    kn.shared_size = 8192;
    kn.shared_route_mask = 0;
    assert(k_pa_ok(kn.shared, 8192));
    assert(!k_pa_ok(kn.shared, 8193));
    kn.shared = 0x50000000;
    kn.shared_size = 1UL << 31;
    kn.shared_user_size = 4096;
    assert(k_shared_unc(KOS_UV_SHARED + 128) == ((kn.shared + 128) | HW_UNC));
    return 0;
}
