/* Launch all before joining; unresolved child lifetimes fail the boot. */
#include <assert.h>
#include <setjmp.h>
#include <string.h>

#include APP_SOURCE

static unsigned started, joined, progress_count, last_step;
static uint64_t last_a;
static unsigned failure; /* 0: child exit, 1: second launch, 2: first join */
static jmp_buf shutdown_return;
static int64_t shutdown_code;

void kos_progress(unsigned where, uint64_t a, uint64_t b)
{
    static const unsigned expected[] = {
        KOS_W_PARALLEL_ARGS, KOS_W_PARALLEL_ARGS,
        KOS_W_RUN_ISSUE, KOS_W_RUN_ACK, KOS_W_RUN_ISSUE, KOS_W_RUN_ACK,
        KOS_W_JOIN_ISSUE, KOS_W_JOIN_ACK, KOS_W_JOIN_ISSUE, KOS_W_JOIN_ACK,
    };
    assert(progress_count < sizeof expected / sizeof expected[0]);
    assert(where == expected[progress_count++]);
    if (where == KOS_W_RUN_ACK) {
        if (failure == 1 && a == 1) {
            assert(!(started & (1u << a)));
            assert((int64_t)b == KOS_EAGAIN);
        } else {
            assert(started & (1u << a));
            assert(b == a + 1);
        }
    }
    if (where == KOS_W_JOIN_ACK) {
        if (failure == 2) {
            assert(!joined && a == 0 && (int64_t)b == KOS_EAGAIN);
        } else {
            assert(joined & (1u << a));
            assert((int64_t)b == (a ? KOS_EINVAL : 0));
        }
    }
    last_step = where;
    last_a = a;
}

unsigned kos_nodes(void) { return 2; }

int64_t kos_run(const char *app, const char *args, unsigned node)
{
    assert(last_step == KOS_W_RUN_ISSUE && last_a == node);
    assert(!joined);
    assert(!strcmp(app, "ostpu"));
    assert(!strcmp(args, "32 4096 1 1 -e"));
    if (failure == 1 && node == 1) {
        return KOS_EAGAIN;
    }
    started |= 1u << node;
    return node + 1;
}

int64_t kos_join(int64_t tid, int64_t *code)
{
    assert(last_step == KOS_W_JOIN_ISSUE && last_a == (uint64_t)(tid - 1));
    assert(started == 3);
    if (failure == 2) {
        return KOS_EAGAIN;
    }
    joined |= 1u << (tid - 1);
    *code = tid == 2 ? KOS_EINVAL : 0;
    return 0;
}

void kos_shutdown(int64_t code)
{
    shutdown_code = code;
    longjmp(shutdown_return, 1);
}

int main(void)
{
    char *args[] = {"parallel", "ostpu", "32", "4096", "1", "1", "-e"};
    assert(parallel(7, args) == KOS_EINVAL);
    assert(joined == started);
    assert(progress_count == 10);
    for (failure = 1; failure <= 2; ++failure) {
        started = joined = progress_count = last_step = 0;
        if (!setjmp(shutdown_return)) {
            parallel(7, args);
            assert(0 && "unresolved participant must not return to init");
        }
        assert(shutdown_code == KOS_EAGAIN);
        assert(started == (failure == 1 ? 1u : 3u));
        assert(!joined);
        assert(progress_count == (failure == 1 ? 6u : 8u));
    }
    return 0;
}
