// A C++ harness driving rv64_regfile directly -- no Verilog testbench.
//
// This is the smallest thing that proves the --cc path: Verilator emits a C++
// class, this file owns main(), the clock and eval(), and the checks are
// ordinary C++. Everything larger in sim/verilator/docs/cc-harness.md is this
// loop with more ports on it.

#include "Vrv64_regfile.h"
#include "verilated.h"

#include <cstdint>
#include <cstdio>
#include <vector>

static VerilatedContext *ctx;
static Vrv64_regfile *dut;
static int errors = 0;
static int checks = 0;

static void step() {
    dut->clk = 0;
    dut->eval();
    ctx->timeInc(5);
    dut->clk = 1;
    dut->eval();
    ctx->timeInc(5);
}

static void chk(uint64_t got, uint64_t want, const char *what) {
    ++checks;
    if (got != want) {
        ++errors;
        if (errors <= 10)
            printf("  MISMATCH %-24s got %016llx want %016llx\n", what,
                   (unsigned long long)got, (unsigned long long)want);
    }
}

// READ LATENCY 1: present the address, step, then the data is at the port.
static uint64_t rd1(int a) {
    dut->rs1_addr = a;
    step();
    return dut->rs1_data;
}

static uint64_t rd2(int a) {
    dut->rs2_addr = a;
    step();
    return dut->rs2_data;
}

static void wr(int a, uint64_t d) {
    dut->wr_en = 1;
    dut->wr_addr = a;
    dut->wr_data = d;
    step();
    dut->wr_en = 0;
}

int main(int argc, char **argv) {
    ctx = new VerilatedContext;
    ctx->commandArgs(argc, argv);
    dut = new Vrv64_regfile(ctx);

    dut->wr_en = 0;
    dut->wr_addr = 0;
    dut->wr_data = 0;
    dut->rs1_addr = 0;
    dut->rs2_addr = 0;
    for (int i = 0; i < 4; ++i) step();

    std::vector<uint64_t> shadow(32, 0);

    // Every architectural register, a value that is wrong if any bit is lost.
    for (int r = 1; r < 32; ++r) {
        uint64_t v = 0x0123456789abcdefull ^ (uint64_t(r) << 56) ^ (uint64_t(r) * 0x9e37u);
        wr(r, v);
        shadow[r] = v;
    }
    for (int r = 1; r < 32; ++r) chk(rd1(r), shadow[r], "port1 readback");
    for (int r = 1; r < 32; ++r) chk(rd2(r), shadow[r], "port2 readback");

    // x0 IS NOT STORED, on either port, however hard it is written.
    wr(0, ~0ull);
    chk(rd1(0), 0, "x0 reads zero, port1");
    chk(rd2(0), 0, "x0 reads zero, port2");

    // The two ports are independent -- a shared address decode would pass the
    // loops above and fail here.
    dut->rs1_addr = 7;
    dut->rs2_addr = 19;
    step();
    chk(dut->rs1_data, shadow[7], "independent ports, rs1");
    chk(dut->rs2_data, shadow[19], "independent ports, rs2");

    // WRITE-DURING-READ RETURNS THE OLD VALUE, and the pipeline is expected to
    // forward. Asserting it here is what stops a later "fix" from changing it
    // silently: the forwarding network is built against this behaviour.
    dut->rs1_addr = 5;
    step();
    uint64_t before = dut->rs1_data;
    dut->wr_en = 1;
    dut->wr_addr = 5;
    dut->wr_data = 0xdeadbeefcafef00dull;
    dut->rs1_addr = 5;
    step();
    dut->wr_en = 0;
    chk(dut->rs1_data, before, "same-cycle write returns old");
    chk(rd1(5), 0xdeadbeefcafef00dull, "and the new value lands");
    shadow[5] = 0xdeadbeefcafef00dull;

    // Nothing else moved.
    for (int r = 1; r < 32; ++r) chk(rd1(r), shadow[r], "no aliasing");

    printf("========================================\n");
    printf("  %s -- %d checks, %d errors\n", errors ? "FAIL" : "PASS", checks, errors);
    printf("========================================\n");

    dut->final();
    delete dut;
    delete ctx;
    return errors ? 1 : 0;
}
