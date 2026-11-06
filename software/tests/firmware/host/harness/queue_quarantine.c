/* A fatal engine run retains its SQ, package and terminal completion ownership. */
#define _GNU_SOURCE
#define KA_SPEC_MEMORY_MUTANT
#include <assert.h>
#include <setjmp.h>
#include <stdint.h>
#include <string.h>
#include <sys/mman.h>

#include "hw_double.h"
#include APP_SOURCE
#include "../../../../kohakuaccel/firmware/queue/service.c"

static jmp_buf stopped;
static uint64_t exit_code;
void ka_exit(uint64_t code)
{
    exit_code = code;
    longjmp(stopped, 1);
}
void ka_yield(void) { dbl_cycles(); }
int ka_printf(const char *fmt, ...) { (void)fmt; return 0; }
void ka_flush(void) {}
void ka_set_sink(ka_sink_fn fn) { (void)fn; }
void ka_set_source(ka_source_fn fn) { (void)fn; }
int ka_wait(ka_ready_fn ready, void *ctx, uint64_t timeout)
{
    (void)ready; (void)ctx; (void)timeout;
    assert(0);
    return 0;
}
struct ka_task *ka_task_self(void) { return 0; }
void ka_sched_stats(struct ka_sched_stats *s) { memset(s, 0, sizeof *s); }
uint32_t ka_task_stack_used(const struct ka_task *t) { (void)t; return 0; }
enum ka_verdict ka_unit_generic_classify(unsigned code, uint32_t arg)
{
    (void)arg;
    return code == KA_SIG_FAULT ? KA_V_FAULT : code == KA_SIG_DATA_RECEIVED ? KA_V_ACK : KA_V_DONE;
}
static const struct ka_unit_class generic = {.classify = ka_unit_generic_classify};
const struct ka_unit_class *ka_unit_class(uint16_t type) { (void)type; return &generic; }
const struct ka_unit_class *ka_unit_classes(unsigned *n) { *n = 1; return &generic; }

int main(void)
{
    uint64_t *memory = mmap((void *)0x10000000UL, 65536, PROT_READ | PROT_WRITE,
                            MAP_PRIVATE | MAP_ANONYMOUS | MAP_FIXED, -1, 0);
    assert(memory == (void *)0x10000000UL);
    uint64_t q = (uintptr_t)memory, pkg = q + 8192;
    ka_boot.magic = KA_BOOT_MAGIC;
    ka_boot.version = KA_BOOT_VERSION;
    ka_boot.queue = q;
    ka_boot.timeout = 4096;
    ka_boot.regions[0].use = KA_REGION_QUEUE;
    ka_boot.regions[0].base = q;
    ka_boot.regions[0].size = 8192;
    memory[KA_Q_INFO / 8] = KA_Q_MAGIC;
    memory[KA_Q_INFO / 8 + 1] = KA_Q_VERSION;
    memory[KA_Q_INFO / 8 + 2] = 2 | 2UL << 16;
    memory[KA_Q_INFO / 8 + 3] = 64 | 64UL << 32;
    uint64_t *h = (uint64_t *)(uintptr_t)pkg;
    h[KA_PH_MAGIC] = (uint64_t)KA_PKG_HEADER << 48 | (uint64_t)KA_PKG_VERSION << 32 | KA_PKG_MAGIC;
    h[KA_PH_SIZE] = 256;
    h[KA_PH_COUNTS] = 3;
    h[KA_PH_COUNTS2] = 1UL << 32 | 1UL << 16;
    h[KA_PH_OFF_UNIT] = 96;
    h[KA_PH_OFF_STEP] = 112;
    h[KA_PH_OFF_PAY] = 160;
    h[12] = 0x0201;
    h[13] = 4;
    h[14] = KA_OP_ENGINE | 9UL << 32;
    h[15] = 0;
    const unsigned codes[9] = {0, 1, 2, 3, 4, 5, KA_DE_WAIT, 5, KA_DE_WAIT};
    const uint64_t values[9] = {0x0201, 0, 0, 0, 0, 1, 1, 1, 2};
    for (unsigned i = 0; i < 9; ++i) {
        h[20 + 4 * (i / 3)] |= (uint64_t)codes[i] << (8 * (i % 3));
        h[21 + 4 * (i / 3) + i % 3] = values[i];
    }
    uint64_t *sq = memory + KA_Q_SQ / 8;
    sq[KA_SQ_RUN_OP] = KA_SQ_RUN;
    sq[KA_SQ_RUN_TAG] = 123;
    sq[KA_SQ_RUN_PKG] = pkg;
    sq[KA_SQ_RUN_BYTES] = 256;
    sq[8] = KA_SQ_NOP;
    sq[9] = 456;
    memory[KA_Q_SQ_TAIL / 8] = 2;
    dbl_engine = 1;
    dbl_retire = 1;
    dbl_fault_at = 2;
    if (!setjmp(stopped)) {
        ka_queue_serve();
        assert(0);
    }
    assert(exit_code == (KA_EXIT_TRAP | KA_ST_UNIT_FAULT));
    assert(memory[KA_Q_FW / 8 + KA_QF_STATE] == KA_Q_ST_FATAL);
    assert(memory[KA_Q_FW / 8 + KA_QF_FATAL] == exit_code);
    assert(memory[KA_Q_CQ_TAIL / 8] == 0);
    assert(memory[KA_Q_SQ_HEAD / 8] == 0);
    assert(memory[KA_Q_FW / 8 + KA_QF_DONE] == 0);
    assert(dbl_gos == 2 && dbl_clears == 1);
    assert((ka_ctrl_rd(KA_R_DE_CTR) & KA_DE_CTR_MASK) == 1);
    assert(pk.base == pkg && Q.q == q && head == 0);
    for (unsigned i = 0; i < 100; ++i) dbl_cycles();
    assert(dbl_gos == 2);
    return 0;
}
