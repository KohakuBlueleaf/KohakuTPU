/* units: every unit the board names, enumerated through the kernel. For each
 * one the four mandatory CU_CTRL words (caps, status, counters, debug) are
 * read through SYS_UNIT_CTRL — from this node's mailbox for a unit on this
 * mesh, through the unit's node for one on another — and checked against the
 * board: CU_CAPS' type and depth must be what the boot table says. */
#include "../apps.h"

static int units(int argc, char **argv)
{
    (void)argc;
    (void)argv;
    int fails = 0;
    struct kos_unit u[KOS_MAX_UNITS];
    int64_t n = kos_units(u, KOS_MAX_UNITS);
    for (int64_t i = 0; i < n; ++i) {
        uint64_t v[4] = {0, 0, 0, 0};
        int st = 0;
        uint64_t t = kos_time();
        for (unsigned k = 0; k < 4 && !st; ++k) {
            st = kos_unit_ctrl((unsigned)i, k, &v[k]);
        }
        t = kos_time() - t;
        unsigned type = (unsigned)(v[0] >> 48), depth = (unsigned)(v[0] >> 20) & 0xffff;
        kos_printf("  unit %ld '%c%c' mesh %u (%u,%u): caps %016lx status %016lx counters %016lx dbg %016lx, "
                   "%lu cycles for 4 reads\n",
                   i, u[i].type >> 8, u[i].type & 0xff, u[i].mesh, u[i].x, u[i].y, v[0], v[1], v[2], v[3], t);
        CHECK(fails, st == 0 && type == u[i].type && (!u[i].depth || depth == u[i].depth),
              "units: unit %ld status %d type %x want %x depth %u want %u", i, st, type, u[i].type, depth,
              u[i].depth);
        if (i == 0) {
            REPORT("units.ctrl_read_cycles", t / 4);
        }
    }
    REPORT("units.count", n);
    kos_printf("units: %ld units, %s\n", n, fails ? "FAIL" : "ok");
    return fails;
}

KOS_APP("units", units);
