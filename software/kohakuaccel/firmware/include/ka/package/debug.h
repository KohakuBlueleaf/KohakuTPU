/* The debug image's records (docs/spec/stack/debug/firmware.md s2, RUL.074,
 * RUL.077, RUL.079, RUL.081): binary records in the boot block's debug region,
 * written before the completion is posted. The host's decoder is the Rust
 * crate ktpu-debug. Without KA_DEBUG every hook is empty. */
#ifndef KA_PACKAGE_DEBUG_H
#define KA_PACKAGE_DEBUG_H

#include <stdint.h>

/* Features an image carries, in its .ka_abi words. */
#define KA_DEBUG_F_STEPS 1u /* a STEP record per step of a traced run */
#define KA_DEBUG_F_FAIL  2u /* UNIT and FAIL records for a failed run */
#define KA_DEBUG_F_LAST  4u /* the last completion words per package unit */

#ifdef KA_DEBUG
#define KA_DEBUG_FEATURES (KA_DEBUG_F_STEPS | KA_DEBUG_F_FAIL | KA_DEBUG_F_LAST)
#else
#define KA_DEBUG_FEATURES 0u
#define KA_DEBUG_LAST 0
#endif

/* The region: a header line, then each package unit's last completions, then
 * a ring of records. Offsets in bytes. */
#define KA_DBG_MAGIC   0x31474244414bUL /* "KADBG1" */
#define KA_DBG_VERSION 1
enum {
    KA_DBG_H_MAGIC   = 0x00,
    KA_DBG_H_VERSION = 0x08,
    KA_DBG_H_LAST    = 0x10, /* N: completion words kept per unit */
    KA_DBG_H_UNITS   = 0x18, /* unit blocks (KA_MAX_PKG_UNITS) */
    KA_DBG_H_REC     = 0x20, /* offset of the record ring */
    KA_DBG_H_RECCAP  = 0x28, /* its bytes, whole records */
    KA_DBG_H_WR      = 0x30, /* fw: record bytes produced, ever */
    KA_DBG_H_LOST    = 0x38, /* fw: record bytes dropped on a full ring */
    KA_DBG_H_RD      = 0x40, /* host: record bytes consumed */
    KA_DBG_LASTS     = 0x80, /* per unit: count of completions, then N words */
};
#define KA_DBG_REC_BYTES 64

/* Record word 0: type | flags << 8 | op << 16 | unit << 32; word 1 the
 * submission tag. */
enum { KA_DBG_STEP = 1, KA_DBG_UNIT = 2, KA_DBG_FAIL = 3 };
/* STEP flags: the cycle is when the step was queued to the dispatch engine. */
#define KA_DBG_STEP_ENGINE 1u
/* FAIL flags: the run was on the dispatch engine; the quiesce drained. A FAIL
 * record's word 7 is, with KA_DBG_FAIL_ENGINE, the engine's issue count when
 * the failure was recorded (DBG.FAIL.005). */
#define KA_DBG_FAIL_ENGINE  1u
#define KA_DBG_FAIL_DRAINED 2u

#ifdef KA_DEBUG
struct ka_engine;

/* At queue setup: find the region, write or resume its header. */
void ka_dbg_boot(void);
/* Before a run: its tag and flags; the package units' completion counts reset. */
void ka_dbg_begin(uint64_t tag, unsigned run_flags);
/* Whether this run is traced (boot STEPS or run TRACE) / kept off the engine. */
int ka_dbg_traced(void);
int ka_dbg_noengine(void);
/* One step (or dispatch group first..last) ended at `cycle`. */
void ka_dbg_step(uint64_t w0, uint32_t first, uint32_t last, uint64_t cycle, int engine,
                 const struct ka_engine *e);
/* A completion word from package unit `u`. */
void ka_dbg_completion(unsigned u, uint64_t w);
/* A run's first failure is being recorded: the engine's issue count now. */
void ka_dbg_failed(void);
/* The failure: UNIT records now, before the quiesce. */
void ka_dbg_units(const struct ka_engine *e);
/* The failure record, after the quiesce. */
void ka_dbg_fail(uint64_t w0, uint32_t step, int engine, const struct ka_engine *e);
#endif

#endif
