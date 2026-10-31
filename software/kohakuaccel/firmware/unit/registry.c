/* Unit class lookup over the linker-gathered `.ka_units` table. */
#include <ka/hal/node.h>
#include <ka/unit/unit.h>

extern const struct ka_unit_class __ka_units_start[];
extern const struct ka_unit_class __ka_units_end[];

enum ka_verdict ka_unit_generic_classify(unsigned code, uint32_t arg)
{
    (void)arg;
    if (code == KA_SIG_FAULT) {
        return KA_V_FAULT;
    }
    if (code == KA_SIG_DATA_RECEIVED) {
        return KA_V_ACK;
    }
    return KA_V_DONE;
}

static const struct ka_unit_class generic = {
    .type = 0,
    .name = "generic",
    .credit = 0,
    .classify = ka_unit_generic_classify,
    .describe = 0,
};

const struct ka_unit_class *ka_unit_class(uint16_t type)
{
    for (const struct ka_unit_class *c = __ka_units_start; c < __ka_units_end; ++c) {
        if (c->type == type) {
            return c;
        }
    }
    return &generic;
}

const struct ka_unit_class *ka_unit_classes(unsigned *n)
{
    *n = (unsigned)(__ka_units_end - __ka_units_start);
    return __ka_units_start;
}
