// Harness for v8t_card: the simulated multimesh_v8t card. Owns the fifteen
// clocks at their real periods, plays the JTAG-to-AXI manager (host port 0,
// 64-bit, on clk_ctrl) and answers a line protocol on stdin/stdout that
// driver/kohakuaccel/transport/verilator.py speaks:
//
//   R <addr>              -> V <data>            one 64-bit read
//   W <addr> <data>       -> OK                  one 64-bit write
//   RB <addr> <nbytes>    -> V <hex bytes>       burst reads, 8-byte beats
//   WB <addr> <hex bytes> -> OK                  burst writes
//   BR <ch> <word>        -> V <128 hex>         DRAM backdoor, one 512-bit word
//   BW <ch> <word> <hex>  -> OK
//   T <n>                 -> OK                  advance n sysnode-clock cycles
//   S                     -> V ...               cycle counts, stat_decerr
//   Q                     -> (exit)
// Every number is hex. Replies are one line; a protocol or AXI error is
// "E <text>". Nothing here knows the address map: the Python side does.

#include "Vv8t_card.h"
#include "verilated.h"

#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>
#include <unistd.h>
#include <vector>

static VerilatedContext *ctx;
static Vv8t_card *dut;

// ---- clocks ----------------------------------------------------------------
struct Clk {
    const char *name;
    uint64_t half_ps;   // half period
    uint64_t next_ps;   // next edge
    int      level;
    CData   *sig;
    uint64_t rises;
};
static std::vector<Clk> clks;
static uint64_t now_ps = 0;
static int ctrl_idx = 0, sys_idx = 2;

static void add_clk(const char *name, uint64_t period_ps, CData *sig) {
    clks.push_back({name, period_ps / 2, period_ps / 2, 0, sig, 0});
    *sig = 0;
}

// Advance to the next clock edge (any clock), toggle it, evaluate. Returns the
// index of the clock that moved and whether it rose.
static int tick_one(bool *rose) {
    int k = 0;
    for (size_t i = 1; i < clks.size(); ++i)
        if (clks[i].next_ps < clks[k].next_ps) k = (int)i;
    now_ps = clks[k].next_ps;
    ctx->time(now_ps);
    clks[k].level ^= 1;
    *clks[k].sig = (CData)clks[k].level;
    if (clks[k].level) clks[k].rises++;
    dut->eval();
    clks[k].next_ps += clks[k].half_ps;
    *rose = clks[k].level != 0;
    return k;
}

// Run until the given clock is about to RISE: the sample point for that
// domain's AXI handshakes (outputs settled, the edge not yet taken).
static void until_before_rise(int idx) {
    for (;;) {
        int k = 0;
        for (size_t i = 1; i < clks.size(); ++i)
            if (clks[i].next_ps < clks[k].next_ps) k = (int)i;
        if (k == idx && clks[k].level == 0) return;
        bool r; tick_one(&r);
    }
}
static void take_rise(int idx) {   // the edge itself
    bool r; int k = tick_one(&r);
    (void)k; (void)r;
}
static void cycles(int idx, uint64_t n) {
    for (uint64_t i = 0; i < n; ++i) { until_before_rise(idx); take_rise(idx); }
}

// ---- host manager 0: 64-bit AXI4 on clk_ctrl -----------------------------
static const int TIMEOUT_CYC = 200000;

static bool axi_write(uint64_t addr, const std::vector<uint64_t> &beats, std::string &err) {
    size_t n = beats.size();
    if (n == 0 || n > 256) { err = "burst length"; return false; }
    dut->h0_awid = 0; dut->h0_awaddr = addr; dut->h0_awlen = (CData)(n - 1);
    dut->h0_awsize = 3; dut->h0_awburst = 1; dut->h0_awvalid = 1;
    dut->h0_bready = 1;
    size_t wi = 0;
    dut->h0_wdata = beats[0]; dut->h0_wstrb = 0xff; dut->h0_wlast = (n == 1); dut->h0_wvalid = 1;
    bool aw_done = false, w_done = false, b_done = false;
    for (int c = 0; c < TIMEOUT_CYC && !b_done; ++c) {
        until_before_rise(ctrl_idx);
        bool aw_hs = dut->h0_awvalid && dut->h0_awready;
        bool w_hs  = dut->h0_wvalid && dut->h0_wready;
        bool b_hs  = dut->h0_bvalid && dut->h0_bready;
        CData bresp = dut->h0_bresp;
        take_rise(ctrl_idx);
        if (aw_hs) { dut->h0_awvalid = 0; aw_done = true; }
        if (w_hs) {
            ++wi;
            if (wi < n) { dut->h0_wdata = beats[wi]; dut->h0_wlast = (wi == n - 1); }
            else { dut->h0_wvalid = 0; dut->h0_wlast = 0; w_done = true; }
        }
        if (b_hs) {
            b_done = true;
            if (bresp != 0) { err = "bresp " + std::to_string((int)bresp); dut->h0_bready = 0; return false; }
        }
    }
    dut->h0_bready = 0;
    if (!b_done) { err = aw_done ? (w_done ? "no B" : "W stalled") : "AW stalled"; return false; }
    return true;
}

static bool axi_read(uint64_t addr, size_t n, std::vector<uint64_t> &out, std::string &err) {
    if (n == 0 || n > 256) { err = "burst length"; return false; }
    dut->h0_arid = 0; dut->h0_araddr = addr; dut->h0_arlen = (CData)(n - 1);
    dut->h0_arsize = 3; dut->h0_arburst = 1; dut->h0_arvalid = 1;
    dut->h0_rready = 1;
    out.clear();
    bool last = false;
    for (int c = 0; c < TIMEOUT_CYC && !last; ++c) {
        until_before_rise(ctrl_idx);
        bool ar_hs = dut->h0_arvalid && dut->h0_arready;
        bool r_hs  = dut->h0_rvalid && dut->h0_rready;
        uint64_t rdata = dut->h0_rdata;
        CData rresp = dut->h0_rresp;
        bool rlast = dut->h0_rlast;
        take_rise(ctrl_idx);
        if (ar_hs) dut->h0_arvalid = 0;
        if (r_hs) {
            out.push_back(rdata);
            if (rresp != 0) { err = "rresp " + std::to_string((int)rresp); dut->h0_rready = 0; return false; }
            if (rlast) last = true;
        }
    }
    dut->h0_rready = 0;
    if (!last) { err = "read stalled after " + std::to_string(out.size()) + " beats"; return false; }
    if (out.size() != n) { err = "beat count"; return false; }
    return true;
}

// ---- DRAM backdoor ---------------------------------------------------------
static void bd_read(int ch, uint32_t word, uint32_t *w16) {
    uint64_t a = dut->bd_addr;
    a &= ~(0xffffull << (ch * 16));
    a |= (uint64_t)(word & 0xffff) << (ch * 16);
    dut->bd_addr = a;
    dut->eval();
    for (int i = 0; i < 16; ++i) w16[i] = dut->bd_rdata[ch * 16 + i];
}
static void bd_write(int ch, uint32_t word, const uint32_t *w16) {
    uint64_t a = dut->bd_addr;
    a &= ~(0xffffull << (ch * 16));
    a |= (uint64_t)(word & 0xffff) << (ch * 16);
    dut->bd_addr = a;
    for (int i = 0; i < 16; ++i) dut->bd_wdata[ch * 16 + i] = w16[i];
    dut->bd_we = (CData)(1 << ch);
    int ddr_idx = 7 + ch;   // clk_ddr0..3 follow ctrl, xdma, sys, bus0..3
    until_before_rise(ddr_idx); take_rise(ddr_idx);
    dut->bd_we = 0;
    until_before_rise(ddr_idx); take_rise(ddr_idx);
}

// ---- hex helpers -----------------------------------------------------------
static std::string hex64(uint64_t v) { char b[32]; snprintf(b, sizeof b, "%016llx", (unsigned long long)v); return b; }
static std::string hexbytes(const std::vector<uint64_t> &beats) {
    std::string s;
    char b[4];
    for (uint64_t v : beats) for (int i = 0; i < 8; ++i) { snprintf(b, sizeof b, "%02x", (unsigned)((v >> (8 * i)) & 0xff)); s += b; }
    return s;
}
static bool parse_hex(const std::string &s, uint64_t &v) {
    if (s.empty()) return false;
    v = strtoull(s.c_str(), nullptr, 16);
    return true;
}

int main(int argc, char **argv) {
    ctx = new VerilatedContext;
    ctx->commandArgs(argc, argv);
    uint64_t settle = 40000;   // sys cycles after reset: the Xache flushes 32,768 sets per home
    for (int i = 1; i < argc; ++i) {
        std::string a = argv[i];
        if (a == "--settle" && i + 1 < argc) settle = strtoull(argv[++i], nullptr, 0);
    }
    dut = new Vv8t_card(ctx);
    // The reply channel is the ORIGINAL stdout, taken private; fd 1 then
    // becomes stderr, so a $display from the model can never be read as a reply.
    FILE *proto = fdopen(dup(1), "w");
    dup2(2, 1);
    setvbuf(proto, nullptr, _IONBF, 0);
    setvbuf(stdout, nullptr, _IONBF, 0);

    // periods in ps: ctrl 100 MHz, xdma 250, sys 300, four buses ~200 (each
    // its own MMCM, so never exactly equal), four MIG ui clocks ~300
    add_clk("ctrl", 10000, &dut->clk_ctrl);
    add_clk("xdma", 4000,  &dut->clk_xdma);
    add_clk("sys",  3334,  &dut->clk_sys);
    add_clk("bus0", 5000, &dut->bus_clk0); add_clk("bus1", 5010, &dut->bus_clk1);
    add_clk("bus2", 4990, &dut->bus_clk2); add_clk("bus3", 5020, &dut->bus_clk3);
    add_clk("ddr0", 3332, &dut->clk_ddr0); add_clk("ddr1", 3336, &dut->clk_ddr1);
    add_clk("ddr2", 3328, &dut->clk_ddr2); add_clk("ddr3", 3340, &dut->clk_ddr3);
    ctrl_idx = 0; sys_idx = 2;

    dut->rstn = 0;
    dut->h0_awvalid = dut->h0_wvalid = dut->h0_bready = dut->h0_arvalid = dut->h0_rready = 0;
    dut->h1_awvalid = dut->h1_wvalid = dut->h1_bready = dut->h1_arvalid = dut->h1_rready = 0;
    dut->h2_awvalid = dut->h2_wvalid = dut->h2_bready = dut->h2_arvalid = dut->h2_rready = 0;
    dut->bd_we = 0; dut->bd_addr = 0;
    dut->eval();
    cycles(ctrl_idx, 50);
    dut->rstn = 1;
    // every domain out of reset, then the settle
    for (int c = 0; c < 10000 && dut->d_rstn_o != 0xf; ++c) cycles(sys_idx, 1);
    if (dut->d_rstn_o != 0xf) { fprintf(proto, "E reset copies never released (d_rstn_o=%x)\n", (unsigned)dut->d_rstn_o); return 2; }
    cycles(sys_idx, settle);
    fprintf(proto, "READY sys_cycles=%llx d_rstn=%x\n", (unsigned long long)clks[sys_idx].rises, dut->d_rstn_o);

    char line[1 << 16];
    while (fgets(line, sizeof line, stdin)) {
        std::string s(line);
        while (!s.empty() && (s.back() == '\n' || s.back() == '\r')) s.pop_back();
        std::vector<std::string> t;
        size_t p = 0;
        while (p < s.size()) {
            size_t q = s.find(' ', p);
            if (q == std::string::npos) q = s.size();
            if (q > p) t.push_back(s.substr(p, q - p));
            p = q + 1;
        }
        if (t.empty()) continue;
        std::string err;
        if (t[0] == "Q") break;
        else if (t[0] == "R" && t.size() == 2) {
            uint64_t a; parse_hex(t[1], a);
            std::vector<uint64_t> out;
            if (axi_read(a, 1, out, err)) fprintf(proto, "V %s\n", hex64(out[0]).c_str());
            else fprintf(proto, "E %s\n", err.c_str());
        }
        else if (t[0] == "W" && t.size() == 3) {
            uint64_t a, d; parse_hex(t[1], a); parse_hex(t[2], d);
            if (axi_write(a, {d}, err)) fprintf(proto, "OK\n"); else fprintf(proto, "E %s\n", err.c_str());
        }
        else if (t[0] == "RB" && t.size() == 3) {
            uint64_t a, n; parse_hex(t[1], a); parse_hex(t[2], n);
            std::vector<uint64_t> all, part;
            bool ok = (n % 8 == 0);
            uint64_t beats = n / 8;
            for (uint64_t done = 0; ok && done < beats;) {
                uint64_t chunk = beats - done; if (chunk > 256) chunk = 256;
                // a burst never crosses 4 KB
                uint64_t to4k = (4096 - ((a + done * 8) & 4095)) / 8;
                if (chunk > to4k) chunk = to4k;
                ok = axi_read(a + done * 8, (size_t)chunk, part, err);
                all.insert(all.end(), part.begin(), part.end());
                done += chunk;
            }
            if (ok) fprintf(proto, "V %s\n", hexbytes(all).c_str()); else fprintf(proto, "E %s\n", err.empty() ? "nbytes" : err.c_str());
        }
        else if (t[0] == "WB" && t.size() == 3) {
            uint64_t a; parse_hex(t[1], a);
            const std::string &h = t[2];
            bool ok = (h.size() % 16 == 0);
            std::vector<uint64_t> beats;
            for (size_t i = 0; ok && i < h.size(); i += 16) {
                uint64_t v = 0;
                for (int k = 0; k < 8; ++k) v |= (uint64_t)strtoul(h.substr(i + 2 * k, 2).c_str(), nullptr, 16) << (8 * k);
                beats.push_back(v);
            }
            for (size_t done = 0; ok && done < beats.size();) {
                size_t chunk = beats.size() - done; if (chunk > 256) chunk = 256;
                uint64_t to4k = (4096 - ((a + done * 8) & 4095)) / 8;
                if (chunk > to4k) chunk = (size_t)to4k;
                std::vector<uint64_t> part(beats.begin() + done, beats.begin() + done + chunk);
                ok = axi_write(a + done * 8, part, err);
                done += chunk;
            }
            if (ok) fprintf(proto, "OK\n"); else fprintf(proto, "E %s\n", err.empty() ? "hex" : err.c_str());
        }
        else if (t[0] == "BR" && t.size() == 3) {
            uint64_t ch, w; parse_hex(t[1], ch); parse_hex(t[2], w);
            uint32_t w16[16]; bd_read((int)ch, (uint32_t)w, w16);
            std::string o;
            char b[12];
            for (int i = 0; i < 16; ++i) { snprintf(b, sizeof b, "%08x", w16[i]); o = std::string(b) + o; }
            fprintf(proto, "V %s\n", o.c_str());
        }
        else if (t[0] == "BW" && t.size() == 4) {
            uint64_t ch, w; parse_hex(t[1], ch); parse_hex(t[2], w);
            const std::string &h = t[3];
            if (h.size() != 128) { fprintf(proto, "E hex\n"); continue; }
            uint32_t w16[16];
            for (int i = 0; i < 16; ++i) w16[i] = (uint32_t)strtoul(h.substr(128 - 8 * (i + 1), 8).c_str(), nullptr, 16);
            bd_write((int)ch, (uint32_t)w, w16);
            fprintf(proto, "OK\n");
        }
        else if (t[0] == "T" && t.size() == 2) {
            uint64_t n; parse_hex(t[1], n);
            cycles(sys_idx, n);
            fprintf(proto, "OK\n");
        }
        else if (t[0] == "S") {
            fprintf(proto, "V sys=%llx ctrl=%llx decerr=%08x d_rstn=%x\n",
                   (unsigned long long)clks[sys_idx].rises, (unsigned long long)clks[ctrl_idx].rises,
                   (unsigned)dut->stat_decerr, (unsigned)dut->d_rstn_o);
        }
        else fprintf(proto, "E unknown\n");
    }
    dut->final();
    delete dut;
    delete ctx;
    return 0;
}
