/* Run firmware/kohakuaccel/os/heap/heap.c natively over a trace on stdin.
 *
 *   i base bytes granule cap     init (cap <= 1024)
 *   a bytes align tag            alloc  -> "rc addr check n"
 *   f addr                       free   -> "rc 0 check n"
 *   d                            dump   -> "T off len used tag" per entry, then "E"
 *
 * driver/tests/test_node_heap.py drives it against driver/kohakuaccel/node/heap.py.
 */
#include <inttypes.h>
#include <stdio.h>

#include <ka/os/heap.h>

static struct ka_block table[1024];

int main(void)
{
    struct ka_heap h = {0};
    char op;
    while (scanf(" %c", &op) == 1) {
        uint64_t a = 0, b = 0, c = 0, d = 0, v = 0;
        int rc = 0;
        if (op == 'i') {
            if (scanf("%" SCNu64 " %" SCNu64 " %" SCNu64 " %" SCNu64, &a, &b, &c, &d) != 4) {
                return 2;
            }
            rc = ka_heap_init(&h, a, b, c, table, (uint32_t)d);
        } else if (op == 'a') {
            if (scanf("%" SCNu64 " %" SCNu64 " %" SCNu64, &a, &b, &c) != 3) {
                return 2;
            }
            rc = ka_heap_alloc(&h, a, b, (uint32_t)c, &v);
        } else if (op == 'f') {
            if (scanf("%" SCNu64, &a) != 1) {
                return 2;
            }
            rc = ka_heap_free(&h, a);
        } else if (op == 'd') {
            for (uint32_t k = 0; k < h.n; ++k) {
                printf("T %" PRIu64 " %" PRIu64 " %u %u\n", h.b[k].off, h.b[k].len, h.b[k].used,
                       h.b[k].tag);
            }
            printf("E\n");
            continue;
        } else {
            return 2;
        }
        printf("%d %" PRIu64 " %u %u\n", rc, v, ka_heap_check(&h), h.n);
    }
    return 0;
}
