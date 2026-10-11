/* KohakuTPU's matmul cluster, 'MG': the generic signal reading and a fault
 * printer. */
#include <ka/lib/stdio.h>
#include <ka/unit/unit.h>

static void describe(unsigned code, uint32_t arg)
{
    ka_printf("[MG] fault, signal %u arg %x\n", code, arg);
}

KA_UNIT_CLASS(kt_matmul_class) = {
    .type = 0x4D47, /* 'MG' */
    .name = "MG",
    .credit = 0,
    .classify = ka_unit_generic_classify,
    .describe = describe,
};
