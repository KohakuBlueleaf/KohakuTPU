/* Start one application on every node before joining any participant. */
#include <kos/user.h>

static int parallel(int argc, char **argv)
{
    if (argc < 2) {
        return KOS_EINVAL;
    }
    kos_progress(KOS_W_PARALLEL_ARGS, 0, 0);
    char args[KOS_CMDLINE];
    unsigned used = 0;
    for (int i = 2; i < argc; ++i) {
        for (const char *p = argv[i]; *p; ++p) {
            if (used + 1 >= sizeof args) {
                return KOS_EINVAL;
            }
            args[used++] = *p;
        }
        if (i + 1 < argc) {
            if (used + 1 >= sizeof args) {
                return KOS_EINVAL;
            }
            args[used++] = ' ';
        }
    }
    args[used] = 0;
    kos_progress(KOS_W_PARALLEL_ARGS, used, 1);
    int64_t tids[KOS_MAX_NODES];
    unsigned nodes = kos_nodes();
    for (unsigned node = 0; node < nodes; ++node) {
        kos_progress(KOS_W_RUN_ISSUE, node, 0);
        tids[node] = kos_run(argv[1], args, node);
        kos_progress(KOS_W_RUN_ACK, node, (uint64_t)tids[node]);
        if (tids[node] < 0) {
            /* A started child can be a permanent resident or wait for the
             * missing participant. There is no cancellation API: fail the boot
             * rather than join forever or let init run its next command. */
            if (node) {
                kos_shutdown(tids[node]);
            }
            return (int)tids[node];
        }
    }
    int result = 0;
    for (unsigned node = 0; node < nodes; ++node) {
        int64_t code = 0;
        kos_progress(KOS_W_JOIN_ISSUE, node, (uint64_t)tids[node]);
        int64_t status = kos_join(tids[node], &code);
        kos_progress(KOS_W_JOIN_ACK, node, (uint64_t)(status ? status : code));
        if (status < 0) {
            /* An unsuccessful join does not establish the child's lifetime. */
            kos_shutdown(status);
        }
        if (!result) {
            result = (int)(status ? status : code);
        }
    }
    return result;
}

KOS_APP("parallel", parallel);
