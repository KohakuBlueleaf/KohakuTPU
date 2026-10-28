/* The shell: commands from the console, one per line.
 *   ps                threads on every node
 *   stat              kernel counters on every node
 *   units             the units the board names, with CU_CTRL caps/counters
 *   nodes             nodes, meshes, memory
 *   run APP ARGS [@N] start APP on node N and wait for it
 *   exit [CODE]       leave the shell */
#include <kos/user.h>

static const char *state_name(unsigned s)
{
    static const char *const n[] = {"free", "ready", "run", "blocked", "zombie"};
    return s < 5 ? n[s] : "?";
}

static int ps(int argc, char **argv)
{
    (void)argc;
    (void)argv;
    struct kos_thread_stat st[16];
    int64_t b = kos_stat(KOS_S_THREADS, st, sizeof st);
    for (int64_t i = 0; i < b / (int64_t)sizeof st[0]; ++i) {
        kos_printf("  node %u tid %4x %-8s %-10s cycles %lu switches %lu\n", kos_node(), st[i].tid,
                   state_name(st[i].state), st[i].name, st[i].cycles, st[i].switches);
    }
    return 0;
}

static int kstat_app(int argc, char **argv)
{
    (void)argc;
    (void)argv;
    struct kos_kernel_stat k;
    if (kos_stat(KOS_S_KERNEL, &k, sizeof k) != sizeof k) {
        return 1;
    }
    kos_printf("  node %u: traps %lu syscalls %lu irqs %lu ticks %lu msgs %lu/%lu (waits %lu, channel full %lu) idle "
               "%lu polls\n",
               kos_node(), k.traps, k.syscalls, k.irqs, k.ticks, k.msgs_sent, k.msgs_rcvd, k.msg_waits, k.chan_full,
               k.idle_polls);
    kos_printf("  node %u: frames %lu/%lu free, L0 maps %lu tables %lu traps %lu, dispatches %lu completions %lu faults %lu\n",
               kos_node(), k.frames_free, k.frames_total, k.l0_maps, k.l0_tables, k.l0_traps, k.dispatches,
               k.completions, k.faults);
    return 0;
}

static void on_every_node(const char *app)
{
    for (unsigned n = 0; n < kos_nodes(); ++n) {
        int64_t tid = kos_run(app, "", n);
        if (tid >= 0) {
            kos_join(tid, 0);
        }
    }
}

int kos_shell(void)
{
    char line[128];
    kos_printf("kos shell on node %u; 'exit' leaves\n", kos_node());
    for (;;) {
        kos_printf("kos> ");
        unsigned n = 0;
        for (;;) {
            int64_t r = kos_read(line + n, sizeof line - 1 - n);
            if (r <= 0) {
                break;
            }
            n += (unsigned)r;
            if (line[n - 1] == '\n' || n + 1 >= sizeof line) {
                break;
            }
        }
        line[n] = 0;
        while (n && (line[n - 1] == '\n' || line[n - 1] == '\r' || line[n - 1] == ' ')) {
            line[--n] = 0;
        }
        kos_printf("%s\n", line);
        char *args = line;
        while (*args && *args != ' ') {
            ++args;
        }
        if (*args) {
            *args++ = 0;
        }
        if (!strcmp(line, "exit")) {
            const char *q = args;
            return (int)kos_strtoul(&q);
        } else if (!strcmp(line, "ps")) {
            on_every_node("ps");
        } else if (!strcmp(line, "stat")) {
            on_every_node("kstat");
        } else if (!strcmp(line, "units")) {
            int64_t tid = kos_run("units", "", 0);
            if (tid >= 0) {
                kos_join(tid, 0);
            }
        } else if (!strcmp(line, "nodes")) {
            kos_printf("  %u nodes; this is node %u (mesh %lu); shared %lx+%lx; %lu units; flags %lx\n",
                       kos_nodes(), kos_node(), kos_info(KOS_I_MESH), kos_info(KOS_I_SHARED),
                       kos_info(KOS_I_SHSIZE), kos_info(KOS_I_NUNITS), kos_info(KOS_I_FLAGS));
        } else if (!strcmp(line, "run")) {
            char *app = args;
            while (*args && *args != ' ') {
                ++args;
            }
            if (*args) {
                *args++ = 0;
            }
            unsigned node = 0;
            for (char *p = args; *p; ++p) {
                if (*p == '@') {
                    const char *q = p + 1;
                    node = (unsigned)kos_strtoul(&q);
                    *p = 0;
                    break;
                }
            }
            int64_t tid = kos_run(app, args, node);
            int64_t code = -1;
            if (tid >= 0) {
                kos_join(tid, &code);
            }
            kos_printf("  %s on node %u: exit %ld\n", app, node, code);
        } else if (*line) {
            kos_printf("  ps | stat | units | nodes | run APP ARGS [@N] | exit [CODE]\n");
        }
    }
}

KOS_APP("ps", ps);
KOS_APP("kstat", kstat_app);
