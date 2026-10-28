/* The boot table: what the host writes at the scratchpad's base before boot,
 * one per node, built from a board description by tools/boot.py. The image is
 * the same on every node and every board; this table is the only thing that
 * differs. Layout is little-endian, offsets fixed (tools/boot.py packs it). */
#ifndef KOS_BOOT_H
#define KOS_BOOT_H

#include <stdint.h>

#define KOS_BOOT_MAGIC   0x31544f4f42534f4bULL /* "KOSBOOT1" */
#define KOS_BOOT_VERSION 1u
#define KOS_BOOT_PA      0x00010000UL
#define KOS_BOOT_BYTES   2048u

#define KOS_MAX_NODES    4u
#define KOS_MAX_REGIONS  16u
#define KOS_MAX_UNITS    32u
#define KOS_CMDLINE      512u

/* Region kinds. Every base is the physical address THIS node uses for the
 * region (the host applies the board's spelling per node). */
enum kos_region_kind {
    KOS_RG_NONE        = 0,
    KOS_RG_DRAM_PRIV   = 1, /* this node's private DRAM span (owner = this node) */
    KOS_RG_DRAM_SHARED = 2, /* DRAM every node maps at the same user address */
    KOS_RG_STAGING     = 3, /* a mesh's staging store (owner = that mesh's node) */
};

/* Flags: the knobs. Each selects between implementations built side by side. */
enum {
    KOS_F_SHARED_UNCACHED = 1u << 0, /* user shared memory uncached (else cached + sync sweeps) */
    KOS_F_PT_DRAM         = 1u << 1, /* page tables in private DRAM (else local staging) */
    KOS_F_LOCK_BAKERY     = 1u << 2, /* user locks: bakery over uncached memory (else ticket on home atomics) */
    KOS_F_POLL            = 1u << 3, /* kernel waits by polling only (else interrupt-driven) */
    KOS_F_ENGINE          = 1u << 4, /* unit dispatch through the dispatch engine where present */
    KOS_F_TRACE           = 1u << 5, /* kernel prints each syscall and message */
};

struct kos_region {
    uint64_t base;
    uint64_t size;
    uint32_t kind;
    uint32_t owner; /* node index */
    uint64_t attr;  /* reserved, 0 */
};

struct kos_unit {
    uint16_t type;  /* CU_TYPE, two ASCII characters */
    uint8_t mesh;
    uint8_t x;
    uint8_t y;
    uint8_t flags;
    uint16_t depth; /* instruction FIFO depth (credit) */
};

struct kos_boot {
    uint64_t magic;
    uint64_t version;
    uint32_t node;          /* this node's index */
    uint32_t nnodes;
    uint8_t mesh_of[KOS_MAX_NODES]; /* mesh id of node i */
    uint32_t flags;         /* KOS_F_* */
    uint64_t clock_hz;      /* the node clock */
    uint64_t tick;          /* preemption quantum in cycles; 0 = cooperative */
    uint64_t engine;        /* nonzero: this node has a dispatch engine */
    uint32_t nregions;
    uint32_t nunits;
    uint64_t run;           /* nonzero, the same on every node of one boot, new each boot (start.h) */
    uint64_t reserved[3];
    struct kos_region region[KOS_MAX_REGIONS];
    struct kos_unit unit[KOS_MAX_UNITS];
    char cmdline[KOS_CMDLINE];
};

_Static_assert(sizeof(struct kos_boot) <= KOS_BOOT_BYTES, "boot table outgrew its page");
/* tools/boot.py packs the same offsets (OFF_REGION, OFF_UNIT, OFF_CMDLINE). */
_Static_assert(__builtin_offsetof(struct kos_boot, region) == 96, "boot.py OFF_REGION");
_Static_assert(__builtin_offsetof(struct kos_boot, unit) == 608, "boot.py OFF_UNIT");
_Static_assert(__builtin_offsetof(struct kos_boot, cmdline) == 864, "boot.py OFF_CMDLINE");

#endif
