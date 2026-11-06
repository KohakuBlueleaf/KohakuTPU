/* Package validation over the host hardware double. */
#define _GNU_SOURCE
#define KA_SPEC_MEMORY_MUTANT
#define KA_MEASURE
#include <assert.h>
#include <stdint.h>
#include <setjmp.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>

#include <ka/boot/args.h>
#include <ka/unit/unit.h>
#include "hw_double.h"
static uint64_t read_extent;
static unsigned reads;
static void read_boundary(uint64_t off)
{
    if (read_extent) {
        assert(off <= read_extent && 8 <= read_extent - off);
        ++reads;
    }
}
#include APP_SOURCE

static jmp_buf stopped;
static uint64_t fatal_code;
void ka_on_fatal(uint64_t code, uint64_t a, uint64_t b)
{
    (void)a; (void)b;
    fatal_code = code;
}
void ka_exit(uint64_t code)
{
    assert(code == fatal_code);
    longjmp(stopped, 1);
}

volatile struct ka_bootargs ka_boot;
void ka_yield(void) { dbl_cycles(); }
int ka_printf(const char *fmt, ...) { (void)fmt; return 0; }

enum ka_verdict ka_unit_generic_classify(unsigned code, uint32_t arg)
{
    (void)arg;
    return code == KA_SIG_FAULT ? KA_V_FAULT : code == KA_SIG_DATA_RECEIVED ? KA_V_ACK : KA_V_DONE;
}
static enum ka_verdict custom(unsigned code, uint32_t arg)
{
    return ka_unit_generic_classify(code, arg);
}
static const struct ka_unit_class generic = {.classify = ka_unit_generic_classify};
static const struct ka_unit_class special = {.classify = custom};
const struct ka_unit_class *ka_unit_class(uint16_t type)
{
    return type ? &special : &generic;
}

static uint64_t data[64];
static void reset(void)
{
    memset(data, 0, sizeof data);
    memset(&pk, 0, sizeof pk);
    memset(&de, 0, sizeof de);
    memset(&mv, 0, sizeof mv);
    ka_engine_reset(&eng, 16, 32);
    assert(ka_engine_add(&eng, 0x0201, 4, 0) == 0);
    pk.base = (uintptr_t)data;
    pk.cached = 1;
    pk.npay = 1;
    de.room = 64;
}

static void raw_header(uint64_t *h, uint32_t total)
{
    memset(h, 0, KA_PKG_HEADER);
    h[KA_PH_MAGIC] = (uint64_t)KA_PKG_HEADER << 48 | (uint64_t)KA_PKG_VERSION << 32 | KA_PKG_MAGIC;
    h[KA_PH_SIZE] = total;
}

static void table_extents(void)
{
    const unsigned fields[] = {KA_PH_OFF_UNIT, KA_PH_OFF_BUF, KA_PH_OFF_STEP, KA_PH_OFF_REL, KA_PH_OFF_PAY};
    const unsigned sizes[] = {KA_PKG_UNIT_BYTES, KA_PKG_BUF_BYTES, KA_PKG_STEP_BYTES, KA_PKG_REL_BYTES, KA_PKG_PAY_BYTES};
    struct ka_pkg_run run = {.bytes = sizeof data};
    for (unsigned i = 0; i < 5; ++i) {
        for (unsigned nonempty = 0; nonempty < 2; ++nonempty) {
            reset();
            raw_header(data, 128);
            data[fields[i]] = nonempty ? 0UL - sizes[i] : 136;
            if (nonempty) {
                if (i == 0) data[KA_PH_COUNTS2] = 1UL << 32;
                if (i == 1) data[KA_PH_COUNTS2] = 1;
                if (i == 2) data[KA_PH_COUNTS2] = 1UL << 16;
                if (i == 3) data[KA_PH_COUNTS] = 1UL << 32;
                if (i == 4) data[KA_PH_COUNTS] = 1;
            }
            assert(header(&run) == KA_ST_BAD_LAYOUT);
        }
    }
}

static void total_alignment(void)
{
    uint64_t *h = mmap((void *)0x10000000UL, 4096, PROT_READ | PROT_WRITE,
                       MAP_PRIVATE | MAP_ANONYMOUS | MAP_FIXED, -1, 0);
    assert(h == (void *)0x10000000UL);
    ka_boot.queue = (uintptr_t)h + 256 - KA_MRING_QUEUE_OFF;
    for (unsigned checksum = 0; checksum < 2; ++checksum) {
        reset();
        raw_header(h, 97);
        h[KA_PH_SIZE] |= (uint64_t)(checksum ? KA_PKG_F_CHECKSUM : 0) << 32;
        read_extent = 97;
        reads = 0;
        struct ka_pkg_run run = {.pkg = (uintptr_t)h, .bytes = 97};
        struct ka_pkg_result result;
        assert(ka_package_run(&run, &result) == KA_ST_BAD_LAYOUT);
        assert(reads > 0);
        read_extent = 0;
    }
    ka_boot.queue = 0;
    munmap(h, 4096);
}

static void engine_extent(void)
{
    reset();
    assert(de_stream(0, UINT32_MAX) == KA_ST_BAD_LAYOUT);
    assert(dbl_gos == 0);
}

static void repeat_extent(void)
{
    reset();
    struct src q;
    uint64_t head = KA_OP_REPEAT | (uint64_t)UINT32_MAX << 32;
    assert(src_open(&q, head, 1UL << 32 | 2UL << 48) == KA_ST_BAD_LAYOUT);
}

static void unit_references(void)
{
    for (unsigned op = 0; op < 2; ++op) {
        for (unsigned engine = 0; engine < 2; ++engine) {
            reset();
            pk.ostep = 64;
            data[9] = op ? 1UL << 32 : 0;
            uint64_t head = (op ? KA_OP_REPEAT : KA_OP_DISPATCH) | 0xffffUL << 16 | 1UL << 32;
            int end = 0;
            struct ka_pkg_run run = {0};
            int status = engine ? de_step(&run, 0, head, &end) : step(&run, 0, head, &end);
            assert(status == KA_ST_BAD_UNIT);
            assert(dbl_gos == 0);
        }
    }
    reset();
    pk.ostep = 64;
    pk.nstep = 2;
    data[8] = KA_OP_REPEAT | 0xffffUL << 16 | 1UL << 32;
    data[9] = 1UL << 32;
    data[10] = KA_OP_DISPATCH | 1UL << 32;
    dispatch_group(0, data[8]);
    assert(eng.status == KA_ST_BAD_UNIT && dbl_gos == 0);
}

static void patch_unit(void)
{
    reset();
    pk.nstep = 1;
    pk.ostep = 64;
    data[8] = KA_OP_DISPATCH | 1UL << 32;
    reg[0] = 65535;
    assert(patch(0, 0, 16 | 16UL << 8 | 1UL << 24) == 0);
    int end = 0;
    struct ka_pkg_run run = {0};
    assert(step(&run, 0, step_word(0, 0), &end) == KA_ST_BAD_UNIT);
    assert(dbl_gos == 0);
}

static void local_repeat_field_mask(void)
{
    reset();
    data[0] = 250;
    data[4] = data[6] = 8UL << 40;
    data[5] = data[7] = 3;
    pk.npay = 2;
    struct src q;
    uint64_t head = KA_OP_REPEAT | 1UL << 32;
    uint64_t arg = 2UL << 32 | 2UL << 48;
    assert(src_open(&q, head, arg) == 0);
    uint64_t w[4], nonlocal[4];
    assert(src_next(&q, w));
    assert(src_next(&q, nonlocal));
    assert((nonlocal[0] & 0x1ff) == 0);
    memcpy(stage.copy, data, 64);
    pk.local = 1;
    assert(src_open(&q, head, arg) == 0);
    assert(src_next(&q, w));
    const uint64_t *local = src_next(&q, w);
    assert(local && memcmp(local, nonlocal, sizeof w) == 0);
}

static void dispatch_guards(void)
{
    reset();
    uint64_t w[4] = {0};
    assert(ka_engine_send_now(&eng, 65535, w) != 0);
    assert(ka_engine_send(&eng, 65535, w) == KA_ST_BAD_UNIT);
    assert(dbl_gos == 0);
}

static void ack_is_not_retirement(void)
{
    for (unsigned slow = 0; slow < 2; ++slow) {
        for (unsigned barrier = 0; barrier < 2; ++barrier) {
            reset();
            eng.u[0].plain = !slow;
            eng.u[0].cls = slow ? &special : &generic;
            eng.u[0].inflight = eng.outstanding = !barrier;
            dbl_signal(1, 2, KA_SIG_DATA_RECEIVED);
            assert(ka_engine_drain(&eng) == 0);
            assert(eng.u[0].acks == 1);
            if (barrier) {
                eng.u[0].expected = 1;
                assert(ka_engine_barrier(&eng) == KA_ST_TIMEOUT);
            } else {
                assert(ka_engine_await(&eng, 0, 1) == KA_ST_TIMEOUT);
            }
            eng.status = 0;
            dbl_signal(1, 2, 0);
            assert(ka_engine_drain(&eng) == 0);
            assert(ka_engine_await(&eng, 0, 0) == 0);
            assert(ka_engine_barrier(&eng) == 0);
        }
    }
}

static void enumeration_deadline(void)
{
    reset();
    uint64_t units[4];
    dbl_offer_until = UINT64_MAX;
    uint64_t start = ka_cycles();
    int count = (int)ka_enumerate(1, 0, units, 0, 4);
    assert(count < 0);
    assert(ka_cycles() - start < 2 * KA_ENUM_WAIT);
    assert(dbl_gos == 0);
    start = ka_cycles();
    assert(unit_ctrl(1, 0, 0) == ~0UL);
    assert(ka_cycles() - start < 2 * KA_ENUM_WAIT);
    assert(dbl_gos == 0);
    dbl_offer_until = ka_cycles() + 10;
    dbl_caps = 0x5643UL << 48;
    assert(ka_enumerate(1, 0, units, 0, 4) == 3);
    assert(units[0] == (0x5643UL << 32 | 1));
    assert(unit_ctrl(1, 0, 0) == dbl_caps);
}

static void source_mover_wait(void)
{
    for (unsigned raw = 0; raw < 2; ++raw) {
        reset();
        pk.ostep = 32;
        dbl_engine = 1;
        assert(de_begin() == 0);
        struct ka_pkg_run run = {0};
        int end = 0;
        for (unsigned i = 1; i <= 3000; ++i) {
            if (raw) {
                data[0] = KA_DE_MV(0) | (uint64_t)KA_DE_WAIT << 8;
                data[1] = 1UL << 16;
                data[2] = (uint64_t)KA_DE_MOVER << 56 | i;
                assert(de_step(&run, 0, KA_OP_ENGINE | 2UL << 32, &end) == 0);
            }
            else {
                data[0] = 0;
                data[1] = 1UL << 16;
                data[2] = KA_MOVER_SKIP;
                data[3] = 0;
                assert(de_step(&run, 0, KA_OP_MOVER | 1UL << 32, &end) == 0);
            }
            assert(de_drain() == 0);
        }
        assert(de_step(&run, 0, KA_OP_MWAIT | 1UL << 32, &end) == 0);
        assert(de_drain() == 0);
    }
}

int main(int argc, char **argv)
{
    assert(argc == 2);
    if (!strcmp(argv[1], "table_extents")) table_extents();
    else if (!strcmp(argv[1], "total_alignment")) total_alignment();
    else if (!strcmp(argv[1], "engine_extent")) engine_extent();
    else if (!strcmp(argv[1], "repeat_extent")) repeat_extent();
    else if (!strcmp(argv[1], "unit_references")) unit_references();
    else if (!strcmp(argv[1], "patch_unit")) patch_unit();
    else if (!strcmp(argv[1], "local_repeat_field_mask")) local_repeat_field_mask();
    else if (!strcmp(argv[1], "dispatch_guards")) dispatch_guards();
    else if (!strcmp(argv[1], "ack_is_not_retirement")) ack_is_not_retirement();
    else if (!strcmp(argv[1], "enumeration_deadline")) enumeration_deadline();
    else if (!strcmp(argv[1], "source_mover_wait")) source_mover_wait();
    else return 2;
    return 0;
}
