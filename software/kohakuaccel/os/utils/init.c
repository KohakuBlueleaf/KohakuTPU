/* init: node 0's first thread. Its arguments are the boot table's command
 * line, ';'-separated commands, each `app args [@node]`, run in order and
 * joined; `shell` reads further commands from the console. At the end every
 * node is shut down with the number of failed commands as the exit code. */
#include <kos/user.h>

int kos_shell(void);

static int run_line(char *line)
{
    while (*line == ' ') {
        ++line;
    }
    if (!*line) {
        return 0;
    }
    char *end = line + strlen(line);
    while (end > line && end[-1] == ' ') {
        *--end = 0;
    }
    unsigned node = 0;
    char *at = 0;
    for (char *p = line; *p; ++p) {
        if (*p == '@') {
            at = p;
        }
    }
    if (at) {
        const char *q = at + 1;
        node = (unsigned)kos_strtoul(&q);
        *at = 0;
    }
    char *args = line;
    while (*args && *args != ' ') {
        ++args;
    }
    if (*args) {
        *args++ = 0;
    }
    if (!strcmp(line, "shell")) {
        return kos_shell();
    }
    uint64_t t0 = kos_time();
    int64_t tid = kos_run(line, args, node);
    if (tid < 0) {
        kos_printf("init: %s: cannot start (%ld)\n", line, tid);
        return 1;
    }
    int64_t code = 0;
    int64_t st = kos_join(tid, &code);
    kos_printf("init: %s exit %ld in %lu cycles%s\n", line, code, kos_time() - t0,
               st ? " (join failed)" : "");
    return st || code;
}

static int init_main(int argc, char **argv)
{
    char cmd[KOS_CMDLINE];
    unsigned n = 0;
    for (int i = 1; i < argc; ++i) {
        for (const char *p = argv[i]; *p && n + 2 < sizeof cmd; ++p) {
            cmd[n++] = *p;
        }
        if (n + 2 < sizeof cmd) {
            cmd[n++] = ' ';
        }
    }
    cmd[n] = 0;
    kos_printf("init: %u nodes, commands: %s\n", kos_nodes(), cmd);
    int fails = 0, ran = 0;
    for (char *p = cmd; *p;) {
        char *s = p;
        while (*p && *p != ';') {
            ++p;
        }
        if (*p) {
            *p++ = 0;
        }
        fails += run_line(s);
        ++ran;
    }
    kos_printf("init: %d commands, %d failed\n", ran, fails);
    kos_shutdown(fails);
}

KOS_APP("init", init_main);
