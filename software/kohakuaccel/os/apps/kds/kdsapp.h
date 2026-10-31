/* Shared pieces of the dispatcher apps: server flags from the arguments and
 * the start/stop around each app. */
#ifndef KDS_APP_H
#define KDS_APP_H

#include "../apps.h"
#include <kos/kds.h>

/* "-e" engine batches, "-u" uncached arenas, "-b" best fit; numbers are
 * returned in order through num[]. */
static inline unsigned kds_args(int argc, char **argv, uint64_t *num, unsigned max, unsigned *nnum)
{
    unsigned flags = 0;
    *nnum = 0;
    for (int i = 1; i < argc; ++i) {
        if (argv[i][0] == '-') {
            for (const char *p = argv[i] + 1; *p; ++p) {
                flags |= *p == 'e' ? KDS_F_ENGINE : *p == 'u' ? KDS_F_UNCACHED : *p == 'b' ? KDS_F_BESTFIT : 0;
            }
        } else if (*nnum < max) {
            const char *p = argv[i];
            num[(*nnum)++] = kos_strtoul(&p);
        }
    }
    return flags;
}

/* What this node's server did between two snapshots. */
static inline void kds_print_server(const char *what, const struct kds_server_stat *a, const struct kds_server_stat *b)
{
    kos_printf("  %s: server passes %lu retire-wakes %lu tasks %lu unit calls %lu msgs %lu/%lu; cycles msgs %lu run %lu "
               "issue %lu send %lu event %lu retire %lu\n",
               what, b->passes - a->passes, b->waits - a->waits, b->tasks - a->tasks, b->unit_batches - a->unit_batches,
               b->msgs_in - a->msgs_in, b->msgs_out - a->msgs_out, b->cyc_msgs - a->cyc_msgs, b->cyc_run - a->cyc_run,
               b->cyc_issue - a->cyc_issue, b->cyc_send - a->cyc_send, b->cyc_event - a->cyc_event,
               b->cyc_retire - a->cyc_retire);
}

#define KDS_ALL_NODES ((1u << kos_nodes()) - 1)
#define KDS_ARENA     (64UL * 1024) /* the smallest board span (core4s) is 512 KB */

#endif
