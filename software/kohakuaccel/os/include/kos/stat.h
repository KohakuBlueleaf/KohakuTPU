/* What SYS_STAT reports. */
#ifndef KOS_STAT_H
#define KOS_STAT_H

#include <stdint.h>

struct kos_thread_stat {
    uint32_t tid;
    uint32_t state; /* KOS_T_* */
    uint64_t cycles; /* user cycles accounted to it */
    uint64_t switches;
    char name[16];
};

struct kos_kernel_stat {
    uint64_t traps, syscalls, irqs, ticks;
    uint64_t msgs_sent, msgs_rcvd, msg_waits, chan_full;
    uint64_t idle_polls, idle_cycles, uptime;
    uint64_t frames_free, frames_total;
    uint64_t l0_maps, l0_tables, l0_traps;
    uint64_t dispatches, completions, faults;
    /* Descriptor streams that ran whole; their cycles add up to each stream's
     * span: emptying and mapping the engine, validating and queuing entries,
     * waits for the engine within the stream, and the drain after its last entry. */
    uint64_t de_streams, de_entries, cyc_de_open, cyc_de_load, cyc_de_room, cyc_de_tail;
    uint64_t moves, cyc_move; /* k_move's copies */
};

struct kos_unit_stat {
    uint16_t type;
    uint8_t mesh, x, y, local, pad[2];
    uint64_t sent, done, faults, fault_arg;
};

enum { KOS_T_FREE, KOS_T_READY, KOS_T_RUN, KOS_T_BLOCKED, KOS_T_ZOMBIE };

#endif
