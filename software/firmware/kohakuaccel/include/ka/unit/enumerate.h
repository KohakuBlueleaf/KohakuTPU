/* The node finds its own units: a CU_CTRL caps read to every coordinate. */
#ifndef KA_UNIT_ENUMERATE_H
#define KA_UNIT_ENUMERATE_H

#include <stdint.h>

/* Cycles a coordinate gets to answer before it is taken to be empty. */
#define KA_ENUM_WAIT 1500

/* Read CU_CAPS at every (x, y) in 0..span, skipping (0, 0) -- this processor.
 * Fills `out` with unit words (type << 32 | mesh << 16 | y << 8 | x), returns
 * how many; `depth`, when given, gets each unit's inst_depth. */
unsigned ka_enumerate(unsigned span, unsigned mesh, uint64_t *out, uint32_t *depth,
                      unsigned max);

#endif
