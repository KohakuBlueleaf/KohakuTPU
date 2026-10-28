/* The package interpreter: bind, relocate, and run a package's steps. */
#ifndef KA_PACKAGE_INTERP_H
#define KA_PACKAGE_INTERP_H

#include <stdint.h>

#define KA_MAX_BUFFERS 64

/* A SIGNAL step reaching the host while the package still runs. */
typedef void (*ka_signal_fn)(void *ctx, uint32_t step, uint32_t v32, uint64_t v64);

struct ka_pkg_run {
    uint64_t pkg;     /* unit-global address of the package */
    uint64_t bytes;   /* how many bytes the host says it wrote there */
    uint64_t bind;    /* unit-global address of nbind addresses, or 0 */
    unsigned nbind;
    uint64_t timeout; /* cycles any one wait may take */
    unsigned flags;   /* KA_RUN_F_* */
    ka_signal_fn signal;
    void *ctx;
    /* The words of the entry queued after this one into `entry`, and its
     * queue index; ~0 when none is queued. Called once this run's last
     * engine entry is queued, to read it ahead (L0.NQ.053). */
    uint64_t (*peek)(uint64_t entry[8]);
};

/* Entry `seq`'s words when they were read ahead, or 0. Valid until the next
 * `ka_package_run`. */
const uint64_t *ka_package_ahead(uint64_t seq);

#define KA_RUN_F_CHECKSUM 1u /* verify the checksum whether or not the package asks */
#define KA_RUN_F_IRQ      2u /* raise the host interrupt once the completion is posted */
/* Debug image only (ka/package/debug.h): trace this run; keep it off the
 * dispatch engine (RUL.075's re-run). */
#define KA_RUN_F_TRACE    4u
#define KA_RUN_F_NOENGINE 8u

/* A run's timing words (L0.BOOT.010, L0.NQ.056), in this order: phase cycles,
 * whether the package was copied, its bytes, the first and last GO from the
 * step loop's start, words sent, cycles since the previous run ended. */
enum {
    KA_PT_HEAD,
    KA_PT_COPY,
    KA_PT_BIND,
    KA_PT_STEPS,
    KA_PT_BARRIER,
    KA_PT_LOCAL,
    KA_PT_TOTAL,
    KA_PT_FIRST,
    KA_PT_LAST,
    KA_PT_SENT,
    KA_PT_IDLE,
    KA_PT_N,
};

struct ka_pkg_result {
    int status;
    unsigned detail;
    uint32_t step;   /* the step that failed, or the step count */
    uint64_t value;
    uint64_t cycles; /* from the first header read to the result */
    uint32_t sent;   /* instructions dispatched */
    uint64_t time[KA_PT_N];
};

int ka_package_run(const struct ka_pkg_run *r, struct ka_pkg_result *out);

/* Once, before the first package: the doorbell counts from here on are new. */
void ka_package_boot(void);

/* FNV-1a 64 over the boot unit table, sorted (docs/spec/package-format.md s3). */
uint64_t ka_machine_signature(void);

/* FNV-1a 64 helpers. */
#define KA_FNV_BASIS 0xcbf29ce484222325UL
uint64_t ka_fnv_word(uint64_t h, uint64_t word);

#endif
