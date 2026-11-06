/* What a harness asks of the hardware double (hw_double.c). */
#ifndef HW_DOUBLE_H
#define HW_DOUBLE_H

#include <stdint.h>

/* Whether STAT reads the engine's magic, as on a node built with one. */
extern int dbl_engine;
/* Optional outbound hold and CU_CTRL reply for enumeration tests. */
extern uint64_t dbl_offer_until, dbl_caps;
/* Queue an unclaimed signal directly to the firmware completion queue. */
void dbl_signal(unsigned x, unsigned y, unsigned code);
/* Cycles of the first and the latest GO issued, and words sent. */
extern uint64_t dbl_first_go, dbl_last_go, dbl_gos;
/* Writes of mover register 0x28 since start, by {axis bit, side bit}. */
extern unsigned dbl_windows[4];
/* Cycles from a GO to its word's retirement; the next GOs that never retire; the
 * most words any one unit held unretired; writes of the engine's clear bit. */
extern unsigned dbl_retire, dbl_lost, dbl_peak, dbl_clears;
/* One-based GO that faults instead of retiring; zero disables injection. */
extern unsigned dbl_fault_at;
/* Rings the interlink sent, and rings it dropped with KA_IL_RING_DEPTH queued. */
extern unsigned dbl_rings, dbl_ring_drops;

uint64_t dbl_cycles(void);
/* A retirement from (x, y) that no word of this node asked for. */
void dbl_peer_retires(unsigned x, unsigned y);
/* Retirements still to come. */
unsigned dbl_pending(void);

#endif
