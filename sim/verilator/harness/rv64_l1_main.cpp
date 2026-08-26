// A component bench for rv64_l1: a reference memory, a reference cache model,
// and a random access stream checked word by word.
//
// THE POINT IS ISOLATION. This bug showed up as "a byte of DRAM is wrong after
// 8 KB of traffic" three modules up, where every candidate explanation was
// plausible and none was checkable. Here the line that goes out and the line
// that comes back are both visible.

#include "Vrv64_l1.h"
#include "verilated.h"

#include <cstdint>
#include <cstdio>
#include <cstring>
#include <map>
#include <vector>

static VerilatedContext *ctx;
static Vrv64_l1 *dut;
static uint64_t cycles = 0;

// The memory behind the cache, and the model of what it should hold.
static std::map<uint64_t, uint64_t> mem;     // by 8-byte word index
static std::map<uint64_t, uint64_t> ref;     // what the program has stored

static uint64_t rd_mem(uint64_t widx) {
    auto it = mem.find(widx);
    return it == mem.end() ? 0 : it->second;
}

// ---- the fill / writeback side --------------------------------------------
static int  fill_wait = -1;
static uint64_t fill_line = 0;
static int  LATENCY = 4;
static uint64_t fills = 0, wbs = 0, wb_bad = 0;
static uint64_t n_hit = 0, n_miss = 0, n_wr = 0;

static void mem_service() {
    // fill: one 32-byte line
    dut->fill_ready = (fill_wait < 0);
    if (dut->fill_valid && fill_wait < 0) {
        fill_line = dut->fill_addr;
        fill_wait = LATENCY;
        ++fills;
    }
    dut->resp_valid = 0;
    if (fill_wait == 0) {
        for (int l = 0; l < 4; ++l) {
            uint64_t v = rd_mem(fill_line * 4 + l);
            dut->resp_data[2 * l]     = (uint32_t)v;
            dut->resp_data[2 * l + 1] = (uint32_t)(v >> 32);
        }
        dut->resp_valid = 1;
        fill_wait = -1;
    } else if (fill_wait > 0) --fill_wait;

    // writeback: accept immediately, and CHECK it against the reference
    dut->wb_ready = 1;
    // Edge-detected: a level-sampled writeback records the same beat once per
    // cycle it is held, which reads as a duplicate transaction.
    static bool wb_prev = false;
    bool wb_rise = dut->wb_valid && !wb_prev;
    wb_prev = dut->wb_valid;
    if (wb_rise) {
        uint64_t line = dut->wb_addr;
        for (int l = 0; l < 4; ++l) {
            uint64_t v = ((uint64_t)dut->wb_data[2 * l + 1] << 32) | dut->wb_data[2 * l];
            uint64_t widx = line * 4 + l;
            auto it = ref.find(widx);
            uint64_t want = (it == ref.end()) ? rd_mem(widx) : it->second;
            if (v != want && wb_bad < 6) {
                printf("  WB MISMATCH line %llx word %d: got %llu want %llu\n",
                       (unsigned long long)line, l,
                       (unsigned long long)v, (unsigned long long)want);
                ++wb_bad;
            }
            mem[widx] = v;
        }
        ++wbs;
    }
    dut->wr_idle = (fill_wait < 0);
}

// Returns `stall` as the RTL sees it: BEFORE the edge. Sampling it after the
// edge reads the next cycle's value, and the access looks complete a cycle
// before the write enable it depends on has actually fired.
static bool tick() {
    dut->clk = 0;
    mem_service();
    dut->eval();
    bool s = dut->stall;
    if (dut->dbg_hit)  ++n_hit;
    if (dut->dbg_miss) ++n_miss;
    if (dut->dbg_wr)   ++n_wr;
    dut->clk = 1;
    dut->eval();
    ctx->timeInc(1);
    ++cycles;
    return s;
}

// One access, held until the cache stops stalling -- the same shape the wrapper
// uses: present the address, hold, take the data on the cycle stall drops.
static uint64_t access(uint64_t addr, bool we, uint64_t wdata, uint8_t be) {
    dut->probe_addr = addr;
    dut->addr       = addr;
    dut->req        = 1;
    dut->we         = we;
    dut->be         = we ? be : 0;
    dut->wdata      = wdata;

    // Hold until a cycle whose PRE-edge stall is low: that cycle is the one that
    // reads and, for a store, writes.
    for (int guard = 0; guard < 5000; ++guard)
        if (!tick()) break;
    uint64_t v = dut->rdata;
    dut->req = 0;
    dut->we  = 0;
    dut->be  = 0;
    tick();
    return v;
}

int main(int argc, char **argv) {
    ctx = new VerilatedContext;
    ctx->commandArgs(argc, argv);
    int words = 1024;
    for (int i = 1; i < argc; ++i) {
        std::string a = argv[i];
        if (a == "--words" && i + 1 < argc) words = atoi(argv[++i]);
        else if (a == "--latency" && i + 1 < argc) LATENCY = atoi(argv[++i]);
    }

    dut = new Vrv64_l1(ctx);
    dut->resetn = 0;
    dut->req = 0; dut->we = 0; dut->be = 0;
    dut->flush = 0; dut->inval = 0;
    dut->fill_ready = 0; dut->resp_valid = 0; dut->wb_ready = 0; dut->wr_idle = 1;
    for (int i = 0; i < 8; ++i) tick();
    dut->resetn = 1;
    // the power-on invalidate sweep
    for (int i = 0; i < 400; ++i) tick();

    const uint64_t BASE = 0x8000'0000ull;
    int fails = 0;

    // Pass 1: write a value to every word. 1024 words is 8 KB against a 2 KB
    // cache, so every line is evicted several times.
    for (int i = 0; i < words; ++i) {
        uint64_t v = (uint64_t)i * 0x1000003ull + 7;
        ref[(BASE >> 3) + i] = v;
        access(BASE + 8 * i, true, v, 0xff);
    }

    // Pass 2: read it all back.
    for (int i = 0; i < words; ++i) {
        uint64_t want = (uint64_t)i * 0x1000003ull + 7;
        uint64_t got = access(BASE + 8 * i, false, 0, 0);
        if (got != want) {
            if (fails < 6)
                printf("  READ  word %d: got %llu want %llu\n", i,
                       (unsigned long long)got, (unsigned long long)want);
            ++fails;
        }
    }

    printf("\n  cycles     %llu\n", (unsigned long long)cycles);
    printf("  hits %llu  misses %llu  array writes %llu\n",
           (unsigned long long)n_hit, (unsigned long long)n_miss,
           (unsigned long long)n_wr);
    printf("  fills      %llu   writebacks %llu\n",
           (unsigned long long)fills, (unsigned long long)wbs);
    printf("  read fails %d   writeback mismatches %llu\n",
           fails, (unsigned long long)wb_bad);
    bool ok = (fails == 0) && (wb_bad == 0) && (fills > 0) && (wbs > 0);
    printf("========================================\n");
    printf("  %s\n", ok ? "PASS" : "FAIL");
    printf("========================================\n");

    dut->final();
    delete dut;
    delete ctx;
    return ok ? 0 : 1;
}
