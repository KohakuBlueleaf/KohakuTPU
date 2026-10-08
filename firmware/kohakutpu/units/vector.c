/* KohakuTPU's vector core, 'VC': the generic signal reading and a printer for
 * the fault code in a SIG_FAULT's arg[7:0]. */
#include <ka/lib/stdio.h>
#include <ka/unit/unit.h>

static const char *const faults[] = {
    "?",       "F_DTYPE", "F_VSRC", "F_CHAIN", "F_OPCODE",
    "F_LEN",   "F_LOOP",  "F_VL",   "F_REDVL", "F_CUDATA",
};

static void describe(unsigned code, uint32_t arg)
{
    unsigned f = arg & 0xff;
    ka_printf("[VC] fault code %u (%s), signal %u\n", f,
              f < sizeof faults / sizeof faults[0] ? faults[f] : "?", code);
}

KA_UNIT_CLASS(kt_vector_class) = {
    .type = 0x5643, /* 'VC' */
    .name = "VC",
    .credit = 0,
    .classify = ka_unit_generic_classify,
    .describe = describe,
};
