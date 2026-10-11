/* The boot block: what the host writes at the scratchpad's base, 0x1_0000,
 * before boot (docs/spec/node-queue.md s2). */
#ifndef KA_BOOT_ARGS_H
#define KA_BOOT_ARGS_H

#include <stdint.h>

#define KA_BOOT_MAGIC   0x31544f4f42414bULL /* "KABOOT1" */
#define KA_BOOT_VERSION 2
#define KA_MAX_UNITS    16

/* Send each DISPATCH step whole, in package order, rather than round-robin
 * across a run of consecutive DISPATCH steps to distinct units. */
#define KA_BOOT_F_SERIAL (1UL << 0)
/* Print each package run's phase cycles after it ends. */
#define KA_BOOT_F_TIMING (1UL << 1)
/* Ignore the package's fetch ports and send every word through the mailbox. */
#define KA_BOOT_F_NOFETCH (1UL << 2)
/* Print each step's end cycle as it ends (PKGS lines), the printing excluded. */
#define KA_BOOT_F_STEPS (1UL << 3)
/* Run every package through the firmware even where a dispatch engine exists. */
#define KA_BOOT_F_NOENGINE (1UL << 4)

struct ka_bootargs {
    uint64_t magic;
    uint64_t version;
    uint64_t queue;    /* unit-global address of this node's queue region */
    uint64_t mesh;     /* this node's mesh index */
    uint64_t scan;     /* nunits == 0: enumerate coordinates 0..scan, 0 = none */
    uint64_t timeout;  /* default bound on any one wait, cycles */
    uint64_t cq_depth; /* node-wide bound on outstanding completions, 0 = none */
    uint64_t flags;    /* KA_BOOT_F_* */
    uint64_t nunits;  /* 0: the firmware enumerates its own units */
    uint64_t units[KA_MAX_UNITS]; /* type << 32 | mesh << 16 | y << 8 | x */
};

extern volatile struct ka_bootargs ka_boot;

/* 0 when the block is usable, else a nonzero reason (also printed); stamps
 * ka_boot_main_cycle. Called first by main. */
int ka_boot_check(void);

/* The cycle counter as main started. */
extern uint64_t ka_boot_main_cycle;

#endif
