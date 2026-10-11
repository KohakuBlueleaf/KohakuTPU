/* The unit plug-in table: one `struct ka_unit_class` per CU_TYPE, registered
 * with KA_UNIT_CLASS() into the linker-gathered `.ka_units`. */
#ifndef KA_UNIT_UNIT_H
#define KA_UNIT_UNIT_H

#include <stdint.h>

/* What one completion means to the dispatcher. */
enum ka_verdict {
    KA_V_DONE  = 0, /* an instruction this node sent has retired */
    KA_V_ACK   = 1, /* a peer acknowledging data another unit sent it */
    KA_V_FAULT = 2, /* the unit reported a fault: the package fails */
};

struct ka_unit_class {
    uint16_t type;    /* CU_TYPE, two ASCII characters */
    const char *name;
    /* Instructions this type may hold in flight; the package's figure and the
     * mailbox's depth bound it further. 0 = no bound of its own. */
    uint32_t credit;
    enum ka_verdict (*classify)(unsigned code, uint32_t arg);
    /* Print what a fault's argument means, or NULL. */
    void (*describe)(unsigned code, uint32_t arg);
};

#define KA_UNIT_CLASS(sym) \
    __attribute__((section(".ka_units"), used, aligned(8))) const struct ka_unit_class sym

/* The class for `type`, or the generic one. Never NULL. */
const struct ka_unit_class *ka_unit_class(uint16_t type);

/* The framework's reading of a signal code: 0/1/2 retire, 3 is an ack, 4 a fault. */
enum ka_verdict ka_unit_generic_classify(unsigned code, uint32_t arg);

/* Every registered class, for the boot banner. */
const struct ka_unit_class *ka_unit_classes(unsigned *n);

#endif
