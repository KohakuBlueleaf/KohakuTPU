/* Fatal publication may use only a validated queue extent. */
#define _GNU_SOURCE
#include <assert.h>
#include <stdint.h>
#include <string.h>
#include <sys/mman.h>

#include APP_SOURCE

#define MEMORY_BYTES 2048u
static uint64_t *memory;

int ka_printf(const char *fmt, ...) { (void)fmt; return 0; }
void ka_flush(void) {}
void ka_set_sink(ka_sink_fn fn) { (void)fn; }
void ka_set_source(ka_source_fn fn) { (void)fn; }
void ka_yield(void) {}
int ka_wait(ka_ready_fn ready, void *ctx, uint64_t timeout)
{
    (void)ready; (void)ctx; (void)timeout;
    assert(0);
    return 0;
}
struct ka_task *ka_task_self(void) { return 0; }
void ka_sched_stats(struct ka_sched_stats *s) { memset(s, 0, sizeof *s); }
uint32_t ka_task_stack_used(const struct ka_task *t) { (void)t; return 0; }
const struct ka_unit_class *ka_unit_classes(unsigned *n) { *n = 0; return 0; }
void ka_package_boot(void) {}
void ka_package_unchain(void) {}
void ka_package_ahead_reset(void) {}
const uint64_t *ka_package_ahead(uint64_t seq) { (void)seq; return 0; }
uint64_t ka_machine_signature(void) { return 0; }
int ka_package_run(const struct ka_pkg_run *r, struct ka_pkg_result *out)
{
    (void)r; (void)out;
    assert(0);
    return 0;
}

static void reset(void)
{
    memset(memory, 0xa5, MEMORY_BYTES);
    memset(&Q, 0, sizeof Q);
    memset((void *)&ka_boot, 0, sizeof ka_boot);
    ka_boot.magic = KA_BOOT_MAGIC;
    ka_boot.version = KA_BOOT_VERSION;
    ka_boot.queue = (uintptr_t)memory;
}

static void unchanged(void)
{
    for (unsigned i = 0; i < MEMORY_BYTES / sizeof memory[0]; ++i) {
        assert(memory[i] == 0xa5a5a5a5a5a5a5a5UL);
    }
}

int main(void)
{
    memory = mmap((void *)0x10000000UL, MEMORY_BYTES, PROT_READ | PROT_WRITE,
                  MAP_PRIVATE | MAP_ANONYMOUS | MAP_FIXED, -1, 0);
    assert(memory == (void *)0x10000000UL);
    reset();
    ka_on_fatal(123, 0, 0);
    unchanged();
    assert(ka_queue_serve() == (KA_EXIT_BOOTARGS | KA_BOOT_ERR_QUEUE));
    unchanged();
    for (unsigned bad = 0; bad < 5; ++bad) {
        reset();
        ka_boot.regions[0].use = KA_REGION_QUEUE;
        ka_boot.regions[0].base = (uintptr_t)memory;
        ka_boot.regions[0].size = KA_Q_FW + KA_Q_LINE;
        if (bad == 0) ka_boot.regions[0].size -= 8;
        if (bad == 1) ka_boot.regions[0].base += KA_Q_LINE;
        if (bad == 2) ka_boot.regions[0].size = ~0UL;
        if (bad == 3) {
            ka_boot.regions[1].use = KA_REGION_DEBUG;
            ka_boot.regions[1].base = (uintptr_t)memory;
            ka_boot.regions[1].size = 8;
        }
        if (bad == 4) ka_boot.version = 0;
        ka_on_fatal(123, 0, 0);
        unchanged();
    }
    reset();
    ka_boot.regions[0].use = KA_REGION_QUEUE;
    ka_boot.regions[0].base = (uintptr_t)memory;
    ka_boot.regions[0].size = KA_Q_FW + KA_Q_LINE;
    ka_on_fatal(123, 0, 0);
    assert(memory[(KA_Q_FW / 8) + KA_QF_STATE] == KA_Q_ST_FATAL);
    assert(memory[(KA_Q_FW / 8) + KA_QF_FATAL] == 123);
    assert(memory[(KA_Q_FW / 8) + KA_QF_ABI] == KA_Q_VERSION);
    assert(memory[KA_Q_LIST_ABI / 8] == KA_Q_LIST_VERSION);
    for (unsigned i = 0; i < MEMORY_BYTES / sizeof memory[0]; ++i) {
        if (i < KA_Q_FW / 8 || i >= (KA_Q_FW + KA_Q_LINE) / 8) {
            assert(memory[i] == 0xa5a5a5a5a5a5a5a5UL);
        }
    }
    return 0;
}
