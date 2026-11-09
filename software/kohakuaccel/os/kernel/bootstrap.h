/* Boot-only routines in the per-node DRAM image. Call their physical address:
 * private base + (linked address - KOS_UV_TEXT). Their code and local helpers
 * are position independent; resident state and operations arrive as arguments. */
#ifndef KOS_BOOTSTRAP_H
#define KOS_BOOTSTRAP_H

#include "kernel.h"
#include "../machine/l0.h"

struct k_boot_machine {
    struct l0_mem *mem;
    volatile struct kos_boot *boot;
    struct kos_boot *copy;
    struct knode *node;
    void (*mmu_init)(void);
    uint64_t text_end, data_off, image_size;
    const char *shared_start, *shared_end;
    void (*say)(const char *, ...);
    void (*halt)(uint64_t);
    int (*map)(uint64_t, uint64_t, uint64_t, unsigned, int);
    uint64_t (*maps)(void), (*tables)(void), (*satp)(void);
    void (*enter_kernel)(uint64_t, uint64_t);
};

struct k_boot_memory {
    void (*init)(uint64_t frames, uint64_t image_pages, uint64_t mmap_base);
    uint64_t max_frames;
};
extern const struct k_boot_memory k_boot_memory;

struct pend;
struct k_boot_units {
    void (*init)(uint64_t index_buffer, struct pend *pending);
    uint64_t index_bytes, pending_bytes;
    struct kunit *units;
    unsigned *count;
};
extern const struct k_boot_units k_boot_units;

struct chanmsg {
    uint32_t chan, src, len, pad;
    uint8_t data[48];
};
struct kbar {
    uint64_t id, parties, count;
    struct {
        uint32_t node;
        uint64_t req;
    } w[K_BAR_WAITERS];
};
#ifdef KOS_CHAN_FIFO
struct channel {
    unsigned key, head, tail, count, next;
};
#endif
struct k_boot_messages {
    void (*init)(struct kmsg *, struct chanmsg *, struct kbar *, uint64_t, uint32_t);
#ifdef KOS_CHAN_FIFO
    struct channel *table;
#endif
};
extern const struct k_boot_messages k_boot_messages;

struct k_boot_kernel {
    struct knode *node;
    const struct kos_boot *boot;
    const struct k_boot_memory *memory;
    void (*log_init)(void);
    volatile uint64_t *(*log_header)(void);
    const struct k_boot_units *units;
    const struct k_boot_messages *messages;
    uint64_t *user_satp;
    uint64_t data_off, image_size;
    const char *bss_start, *bss_end, *shared_start, *shared_end;
    void *(*zero)(void *, int, size_t);
    void *(*alloc)(uint64_t);
    /* Command and busy-edge polling execute together in IMEM. */
    void (*dcache)(unsigned);
    int (*map)(uint64_t, uint64_t, uint64_t, unsigned);
    void (*print)(const char *, ...);
    void (*panic)(const char *, ...);
    void (*where)(uint64_t, uint64_t, uint64_t);
    struct sbiret (*l0)(uint64_t, uint64_t, uint64_t, uint64_t, uint64_t);
    void (*poll_units)(void);
    int (*find_app)(const char *);
    int64_t (*run_local)(unsigned, const char *);
    struct thread *(*schedule)(void);
    void (*resume)(struct thread *);
};

void k_boot_units_init(const struct k_boot_kernel *c);

typedef void k_boot_machine_fn(const struct k_boot_machine *c);
typedef void k_boot_kernel_fn(const struct k_boot_kernel *c);
extern k_boot_machine_fn k_boot_machine;
extern k_boot_kernel_fn k_boot_kernel;

#endif
