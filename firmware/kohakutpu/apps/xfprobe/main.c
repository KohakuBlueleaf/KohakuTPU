/* xfprobe: the transform bank's register space and the mover's status word,
 * read by the node processor.
 *
 * Stores at `queue` (scripts/py/sw_xfprobe.py checks them):
 *   [queue + 0x200] MVSTAT, idle
 *   [queue + 0x208 + 8*i] XF_DATA at {id i, addr 4} (geometry), i = 0..3
 *   [queue + 0x228] XF_DATA at {id 0, addr 0} (the bank's fault word)
 */
#include <ka/boot/args.h>
#include <ka/hal/mem.h>
#include <ka/hal/node.h>
#include <ka/lib/stdio.h>

static uint64_t xf_read(unsigned id, unsigned addr)
{
    ka_ctrl_wr(KA_R_XF_SEL, ((uint64_t)id << 8) | addr);
    return ka_ctrl_rd(KA_R_XF_DATA);
}

int main(void)
{
    if (ka_boot_check()) {
        return 1;
    }
    uint64_t q = ka_boot.queue;
    uint64_t st = ka_ctrl_rd(KA_R_MVSTAT);
    ka_st64(q + 0x200, st);
    for (unsigned id = 0; id < 4; ++id) {
        uint64_t v = xf_read(id, 4);
        ka_st64(q + 0x208 + 8 * id, v);
        ka_printf("xf id %u geometry %lx\n", id, v);
    }
    ka_st64(q + 0x228, xf_read(0, 0));
    ka_printf("mvstat %lx room %lu\n", st, (uint64_t)KA_MV_ROOM(st));
    return 0;
}
