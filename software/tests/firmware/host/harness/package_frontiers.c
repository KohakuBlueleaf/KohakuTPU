/* Saved completion targets through both package dispatch modes. */
#include <assert.h>
#include <stdint.h>

#include <ka/boot/args.h>
#include <ka/unit/unit.h>

#include "hw_double.h"
#include "../../../../kohakuaccel/firmware/package/interp.c"

volatile struct ka_bootargs ka_boot;

void ka_yield(void) { dbl_cycles(); }

int ka_printf(const char *fmt, ...)
{
    (void)fmt;
    return 0;
}

enum ka_verdict ka_unit_generic_classify(unsigned code, uint32_t arg)
{
    (void)arg;
    return code == KA_SIG_FAULT ? KA_V_FAULT : code == KA_SIG_DATA_RECEIVED ? KA_V_ACK : KA_V_DONE;
}

static const struct ka_unit_class generic = {.classify = ka_unit_generic_classify};

const struct ka_unit_class *ka_unit_class(uint16_t type)
{
    (void)type;
    return &generic;
}

int main(void)
{
    uint64_t memory[8] = {0};
    struct ka_pkg_run run = {0};
    int end = 0;
    pk.base = (uintptr_t)memory;
    pk.cached = 1;
    pk.local = 0;
    pk.ostep = 0;
    pk.opay = 32;
    pk.npay = 1;
    ka_boot.timeout = 64;
    eng.n = 1;
    for (unsigned mode = 0; mode < 2; ++mode) {
        eng.status = 0;
        eng.u[0].sent = 1;
        eng.u[0].retired = 1;
        eng.u[0].expected = 0;
        de.exp[0] = 0;
        de.top[0] = 0;
        de.room = 8;
        dbl_engine = (int)mode;
        int (*execute)(const struct ka_pkg_run *, uint32_t, uint64_t, int *) =
            mode ? de_step : step;
        assert(execute(&run, 0, 23, &end) == 0);
        eng.u[0].sent = 3;
        assert(execute(&run, 0, 23 | (1u << 8), &end) == 0);
        assert((mode ? de.exp[0] : eng.u[0].expected) == 1);
        /* A newer wait cannot be moved backwards by an older saved target. */
        eng.u[0].retired = 3;
        eng.u[0].expected = 3;
        de.exp[0] = 3;
        assert(execute(&run, 0, 23 | (1u << 8), &end) == 0);
        assert((mode ? de.exp[0] : eng.u[0].expected) == 3);
        /* A reused slot captures the next live frontier, not its former value. */
        assert(execute(&run, 0, 23, &end) == 0);
        eng.u[0].sent = 5;
        assert(execute(&run, 0, 23 | (1u << 8), &end) == 0);
        assert((mode ? de.exp[0] : eng.u[0].expected) == 3);
        assert(execute(&run, 0, 23 | (2u << 8) | (1UL << 32), &end) == 0);
        assert(execute(&run, 0, 23 | (1u << 8), &end) == KA_ST_BAD_STEP);
    }
    return 0;
}
