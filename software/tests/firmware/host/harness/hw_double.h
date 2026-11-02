/* What a harness asks of the hardware double (hw_double.c). */
#ifndef HW_DOUBLE_H
#define HW_DOUBLE_H

#include <stdint.h>

/* Whether STAT reads the engine's magic, as on a node built with one. */
extern int dbl_engine;
/* Cycles of the first and the latest GO issued, and words sent. */
extern uint64_t dbl_first_go, dbl_last_go, dbl_gos;
/* Writes of mover register 0x28 since start, by {axis bit, side bit}. */
extern unsigned dbl_windows[4];

uint64_t dbl_cycles(void);
/* A retirement from (x, y) that no word of this node asked for. */
void dbl_peer_retires(unsigned x, unsigned y);

#endif
