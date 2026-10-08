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
};

#define KA_RUN_F_CHECKSUM 1u /* verify the checksum whether or not the package asks */

struct ka_pkg_result {
    int status;
    unsigned detail;
    uint32_t step;   /* the step that failed, or the step count */
    uint64_t value;
    uint64_t cycles; /* from the first header read to the result */
    uint32_t sent;   /* instructions dispatched */
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
