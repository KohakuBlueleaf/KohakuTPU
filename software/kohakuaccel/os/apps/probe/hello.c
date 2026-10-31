/* hello: one thread on every node; each says where it runs and returns a
 * value only that node computes. Proves spawn, remote spawn, join and exit
 * codes across nodes. */
#include "../apps.h"

static int64_t greet(uint64_t i)
{
    unsigned me = kos_node();
    kos_printf("hello from node %u of %u (mesh %lu), worker %lu\n", me, kos_nodes(), kos_info(KOS_I_MESH), i);
    return (int64_t)(0x100 * me + i);
}

static int hello(int argc, char **argv)
{
    (void)argc;
    (void)argv;
    int fails = 0;
    unsigned n = kos_nodes();
    int64_t tid[KOS_MAX_NODES];
    for (unsigned j = 0; j < n; ++j) {
        tid[j] = kos_spawn(greet, j, j);
    }
    for (unsigned j = 0; j < n; ++j) {
        int64_t code = -1;
        int64_t st = tid[j] < 0 ? tid[j] : kos_join(tid[j], &code);
        CHECK(fails, st == 0 && KOS_TID_NODE(tid[j]) == j && code == (int64_t)(0x101 * j),
              "hello: worker %u tid %lx status %ld exit %lx", j, tid[j], st, code);
    }
    kos_printf("hello: %u nodes, %s\n", n, fails ? "FAIL" : "ok");
    return fails;
}

KOS_APP("hello", hello);
