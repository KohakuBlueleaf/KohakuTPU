/* Unit enumeration: a CU_CTRL read of CU_CAPS through the mailbox, the reply
 * read whole from the RX queue (control-registers.md s1). */
#include <ka/hal/cpu.h>
#include <ka/hal/node.h>
#include <ka/unit/enumerate.h>

static int caps_at(unsigned x, unsigned y, uint64_t *caps)
{
    while (ka_nm_rd(KA_NM_STAT) & KA_NM_STAT_OFFERED) {
    }
    ka_nm_wr(KA_NM_DST, KA_NM_TYPED(KA_T_CU_CTRL) | ((uint64_t)y << 8) | x);
    ka_nm_wr(KA_NM_ARG0, 0);
    ka_nm_wr(KA_NM_ARG1, 0);
    ka_nm_wr(KA_NM_ARG2, 0);
    ka_nm_wr(KA_NM_ARG3, 0); /* index 0, CU_CAPS */
    ka_nm_wr(KA_NM_GO, 1);
    uint64_t t0 = ka_cycles();
    while (ka_cycles() - t0 < KA_ENUM_WAIT) {
        uint64_t h = ka_rx_rd(KA_RX_HDR);
        if (!(h >> 63)) {
            continue;
        }
        uint64_t p2 = ka_rx_rd(KA_RX_P2), p3 = ka_rx_rd(KA_RX_P3);
        ka_rx_pop();
        unsigned sx = (h >> 20) & 0xf, sy = (h >> 16) & 0xf, ty = (h >> 12) & 0xf;
        unsigned op = (unsigned)(p3 >> 56), idx = (unsigned)((p3 >> 48) & 0xff);
        /* Only the asked coordinate's CAPS reply counts. */
        if (ty != KA_T_CU_CTRL || op != 2 || idx != 0 || sx != x || sy != y) {
            continue;
        }
        *caps = ((p3 & 0xffffffffffffUL) << 16) | (p2 >> 48);
        return 1;
    }
    return 0;
}

unsigned ka_enumerate(unsigned span, unsigned mesh, uint64_t *out, uint32_t *depth,
                      unsigned max)
{
    unsigned n = 0;

    /* Empty the RX queue first. */
    while (ka_rx_rd(KA_RX_HDR) >> 63) {
        ka_rx_pop();
    }
    ka_nm_wr(KA_NM_DST, 0); /* back to CU_INST for the dispatcher */
    for (unsigned y = 0; y <= span && n < max; ++y) {
        for (unsigned x = 0; x <= span && n < max; ++x) {
            uint64_t caps;
            if ((x == 0 && y == 0) || !caps_at(x, y, &caps)) {
                continue;
            }
            out[n] = (caps >> 48) << 32 | (uint64_t)(mesh & 0xff) << 16 | y << 8 | x;
            if (depth) {
                depth[n] = (uint32_t)((caps >> 20) & 0xffff);
            }
            ++n;
        }
    }
    ka_nm_wr(KA_NM_DST, 0);
    return n;
}
