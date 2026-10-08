// Harness for a generated card (scripts/py/gen_card.py): host manager 0 (64-bit
// AXI on bus_clk[1]) and every clock (sim/verilator/docs/card-backend.md).
// Line protocol on stdin/stdout, every number hex, one reply line:
//
//   R <addr>              -> V <data>            one 64-bit read
//   W <addr> <data>       -> OK                  one 64-bit write
//   RB <addr> <nbytes>    -> V <hex bytes>       burst read, 8-byte beats
//   WB <addr> <hex bytes> -> OK                  burst write
//   BR <ch> <word>        -> V <128 hex>         DRAM backdoor, one 512-bit word
//   BW <ch> <word> <hex>  -> OK
//   SC <substr>           -> V <scope:var:n:bytes ...>  public arrays (sim/verilator/card.vlt)
//   PK <scope> <var> <idx> <hex> -> OK           write consecutive elements from idx
//   PE <scope> <var> <idx> <n>   -> V <hex>      read n elements from idx
//   T <n>                 -> OK                  advance n die-0 sysnode cycles
//   S                     -> V ...               cycle counts, decerr, resets, locks
//   Q                     -> (exit)
// An error is "E <text>".

#define HSTR(x) #x
#define HDR(x) HSTR(x.h)
#include HDR(VTOP)
#include "verilated.h"
#include "verilated_syms.h"

#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>
#include <unistd.h>
#include <vector>

static VerilatedContext *ctx;
static VTOP *dut;

// ---- clocks ----------------------------------------------------------------
static const uint64_t NEVER = ~0ull;
struct Clk {
    const char *name;
    CData   *vec;       // the port (a scalar or a 4-bit vector)
    int      bit;
    uint64_t half_ps;
    uint64_t next_ps;
    int      level;
    int      die, out;  // >= 0: mesh-wizard output `out` of die `die`
    uint64_t rises;
};
static std::vector<Clk> clks;
static uint64_t now_ps = 0;
static int ctrl_idx, bus1_idx, sys0_idx, ddr_idx[4];

static int add_clk(const char *name, uint64_t period_ps, CData *vec, int bit, int die = -1, int out = -1) {
    clks.push_back({name, vec, bit, period_ps / 2, period_ps / 2, 0, die, out, 0});
    *vec &= (CData)~(1u << bit);
    return (int)clks.size() - 1;
}

static void drive(Clk &c) {
    if (c.level) *c.vec |= (CData)(1u << c.bit);
    else         *c.vec &= (CData)~(1u << c.bit);
}

// A mesh-wizard output: f = 100 MHz * M / (D * div), period ps = 10000 * D * div / M;
// stopped while its wizard is unlocked.
static void retime_wizard_clocks() {
    for (auto &c : clks) {
        if (c.die < 0) continue;
        bool locked = (dut->wiz_locked >> c.die) & 1;
        if (!locked || !dut->rstn) {
            if (c.level) { c.level = 0; drive(c); }
            c.next_ps = NEVER;
            continue;
        }
        uint64_t d   = (dut->wiz_divclk >> (8 * c.die)) & 0xff;
        uint64_t m   = (dut->wiz_mult   >> (8 * c.die)) & 0xff;
        uint64_t div = (dut->wiz_div[c.die] >> (8 * c.out)) & 0xff;
        if (!d || !m || !div) continue;
        uint64_t half = (10000 * d * div / m) / 2;
        if (half == 0) half = 1;
        c.half_ps = half;
        if (c.next_ps == NEVER) c.next_ps = now_ps + half;
    }
}

static int next_clk() {
    int k = 0;
    for (size_t i = 1; i < clks.size(); ++i)
        if (clks[i].next_ps < clks[k].next_ps) k = (int)i;
    return k;
}

static void tick_one() {
    int k = next_clk();
    now_ps = clks[k].next_ps;
    ctx->time(now_ps);
    clks[k].level ^= 1;
    drive(clks[k]);
    if (clks[k].level) clks[k].rises++;
    dut->eval();
    clks[k].next_ps += clks[k].half_ps;
    retime_wizard_clocks();
}

// Stop just before `idx` rises: that domain's outputs settled, the edge not taken.
static void until_before_rise(int idx) {
    for (;;) {
        int k = next_clk();
        if (k == idx && clks[k].level == 0) return;
        tick_one();
    }
}

// After until_before_rise(idx), the next edge is idx's rise.
static void take_rise() { tick_one(); }

static void cycles(int idx, uint64_t n) {
    for (uint64_t i = 0; i < n; ++i) { until_before_rise(idx); take_rise(); }
}

// ---- manager 0: 64-bit AXI4 on bus_clk[1] ----------------------------------
static const int TIMEOUT_CYC = 400000;

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
        until_before_rise(bus1_idx);
        bool aw_hs = dut->h0_awvalid && dut->h0_awready;
        bool w_hs  = dut->h0_wvalid && dut->h0_wready;
        bool b_hs  = dut->h0_bvalid && dut->h0_bready;
        CData bresp = dut->h0_bresp;
        take_rise();
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
        until_before_rise(bus1_idx);
        bool ar_hs = dut->h0_arvalid && dut->h0_arready;
        bool r_hs  = dut->h0_rvalid && dut->h0_rready;
        uint64_t rdata = dut->h0_rdata;
        CData rresp = dut->h0_rresp;
        bool rlast = dut->h0_rlast;
        take_rise();
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
static void set_bd_addr(int ch, uint32_t word) {
    uint64_t a = dut->bd_addr;
    a &= ~(0xffffull << (ch * 16));
    a |= (uint64_t)(word & 0xffff) << (ch * 16);
    dut->bd_addr = a;
}

static void bd_read(int ch, uint32_t word, uint32_t *w16) {
    set_bd_addr(ch, word);
    dut->eval();
    for (int i = 0; i < 16; ++i) w16[i] = dut->bd_rdata[ch * 16 + i];
}

static void bd_write(int ch, uint32_t word, const uint32_t *w16) {
    set_bd_addr(ch, word);
    for (int i = 0; i < 16; ++i) dut->bd_wdata[ch * 16 + i] = w16[i];
    dut->bd_we = (CData)(1 << ch);
    cycles(ddr_idx[ch], 1);
    dut->bd_we = 0;
    cycles(ddr_idx[ch], 1);
}

// ---- direct array access (the public arrays of sim/verilator/card.vlt) -------
// Element k of an unpacked array sits at datap + k * entSize, little-endian.
static std::string scopes_with(const std::string &sub) {
    std::string o;
    const VerilatedScopeNameMap *m = ctx->scopeNameMap();
    if (!m) return o;
    for (const auto &kv : *m) {
        const VerilatedScope *sp = kv.second;
        if (!strstr(kv.first, sub.c_str()) || !sp->varsp()) continue;
        for (const auto &v : *sp->varsp()) {
            const VerilatedVar &var = v.second;
            uint64_t n = var.udims() ? (uint64_t)var.unpacked().elements() : 1;
            o += std::string(" ") + kv.first + ":" + v.first + ":" + std::to_string(n) + ":" +
                 std::to_string(var.entSize());
        }
    }
    return o;
}

static bool peek(const std::string &scope, const std::string &var, uint64_t idx,
                 uint64_t cnt, std::string &out, std::string &err) {
    const VerilatedScope *sp = ctx->scopeFind(scope.c_str());
    if (!sp) { err = "no scope " + scope; return false; }
    const VerilatedVar *vp = sp->varFind(var.c_str());
    if (!vp) { err = "no var " + var; return false; }
    uint64_t ent = vp->entSize();
    uint64_t n = vp->udims() ? (uint64_t)vp->unpacked().elements() : 1;
    if (idx + cnt > n) { err = "past the end"; return false; }
    const uint8_t *base = (const uint8_t *)vp->datap() + idx * ent;
    char b[4];
    for (uint64_t k = 0; k < cnt * ent; ++k) { snprintf(b, sizeof b, "%02x", base[k]); out += b; }
    return true;
}

static bool poke(const std::string &scope, const std::string &var, uint64_t idx,
                 const std::string &hex, std::string &err) {
    const VerilatedScope *sp = ctx->scopeFind(scope.c_str());
    if (!sp) { err = "no scope " + scope; return false; }
    const VerilatedVar *vp = sp->varFind(var.c_str());
    if (!vp) { err = "no var " + var; return false; }
    uint64_t ent = vp->entSize();
    uint64_t n = vp->udims() ? (uint64_t)vp->unpacked().elements() : 1;
    if (hex.size() % (2 * ent)) { err = "hex not whole elements"; return false; }
    uint64_t cnt = hex.size() / (2 * ent);
    if (idx + cnt > n) { err = "past the end"; return false; }
    uint8_t *base = (uint8_t *)vp->datap() + idx * ent;
    for (uint64_t b = 0; b < cnt * ent; ++b)
        base[b] = (uint8_t)strtoul(hex.substr(2 * b, 2).c_str(), nullptr, 16);
    return true;
}

// ---- hex helpers -----------------------------------------------------------
static std::string hex64(uint64_t v) { char b[32]; snprintf(b, sizeof b, "%016llx", (unsigned long long)v); return b; }

static std::string hexbytes(const std::vector<uint64_t> &beats) {
    std::string s;
    char b[4];
    for (uint64_t v : beats) for (int i = 0; i < 8; ++i) { snprintf(b, sizeof b, "%02x", (unsigned)((v >> (8 * i)) & 0xff)); s += b; }
    return s;
}

static uint64_t hx(const std::string &s) { return strtoull(s.c_str(), nullptr, 16); }

int main(int argc, char **argv) {
    ctx = new VerilatedContext;
    ctx->commandArgs(argc, argv);
    uint64_t settle = 2000;   // die-0 sysnode cycles after every reset releases
    for (int i = 1; i < argc; ++i) {
        std::string a = argv[i];
        if (a == "--settle" && i + 1 < argc) settle = strtoull(argv[++i], nullptr, 0);
    }
    dut = new VTOP(ctx);

    // Replies go to the ORIGINAL stdout, taken private; fd 1 becomes stderr so a
    // $display from the model is never read as a reply.
    FILE *proto = fdopen(dup(1), "w");
    dup2(2, 1);
    setvbuf(proto, nullptr, _IONBF, 0);
    setvbuf(stdout, nullptr, _IONBF, 0);

    // ---- clocks and reset ----
    dut->clk_ctrl = 0; dut->clk_xdma = 0;
    dut->bus_clk = 0; dut->ddr_clk = 0;
    dut->sys_clk = 0; dut->noc_clk = 0; dut->mat2x_clk = 0; dut->vec_clk = 0;
    // fixed clocks in ps; the four bus and DDR clocks each off their own MMCM
    ctrl_idx = add_clk("ctrl", 10000, &dut->clk_ctrl, 0);
    add_clk("xdma", 4000, &dut->clk_xdma, 0);
    static const uint64_t bus_ps[4] = {5000, 5010, 4990, 5020};
    static const uint64_t ddr_ps[4] = {3332, 3336, 3328, 3340};
    for (int d = 0; d < 4; ++d) {
        int b = add_clk("bus", bus_ps[d], &dut->bus_clk, d);
        if (d == 1) bus1_idx = b;
        ddr_idx[d] = add_clk("ddr", ddr_ps[d], &dut->ddr_clk, d);
    }
    for (int d = 0; d < 4; ++d) {
        add_clk("noc",   3334, &dut->noc_clk,   d, d, 0);
        add_clk("mat2x", 1666, &dut->mat2x_clk, d, d, 1);
        add_clk("vec",   3334, &dut->vec_clk,   d, d, 2);
        int s = add_clk("sys", 3334, &dut->sys_clk, d, d, 3);
        if (d == 0) sys0_idx = s;
    }

    dut->rstn = 0;
    dut->h0_awvalid = dut->h0_wvalid = dut->h0_bready = dut->h0_arvalid = dut->h0_rready = 0;
    dut->h1_awvalid = dut->h1_wvalid = dut->h1_bready = dut->h1_arvalid = dut->h1_rready = 0;
    dut->h2_awvalid = dut->h2_wvalid = dut->h2_bready = dut->h2_arvalid = dut->h2_rready = 0;
    dut->bd_we = 0; dut->bd_addr = 0;
    dut->eval();
    retime_wizard_clocks();
    cycles(ctrl_idx, 50);
    dut->rstn = 1;
    dut->eval();
    retime_wizard_clocks();
    for (int c = 0; c < 20000 && dut->sys_rstn != 0xf; ++c) cycles(ctrl_idx, 1);
    if (dut->sys_rstn != 0xf) {
        fprintf(proto, "E sysnode resets never released (sys_rstn=%x locked=%x)\n",
                (unsigned)dut->sys_rstn, (unsigned)dut->wiz_locked);
        return 2;
    }
    cycles(sys0_idx, settle);
    fprintf(proto, "READY sys_cycles=%llx sys_rstn=%x\n", (unsigned long long)clks[sys0_idx].rises,
            (unsigned)dut->sys_rstn);

    // ---- command loop ----
    static char line[1 << 17];
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
            std::vector<uint64_t> out;
            if (axi_read(hx(t[1]), 1, out, err)) fprintf(proto, "V %s\n", hex64(out[0]).c_str());
            else fprintf(proto, "E %s\n", err.c_str());
        }
        else if (t[0] == "W" && t.size() == 3) {
            if (axi_write(hx(t[1]), {hx(t[2])}, err)) fprintf(proto, "OK\n");
            else fprintf(proto, "E %s\n", err.c_str());
        }
        else if (t[0] == "RB" && t.size() == 3) {
            uint64_t a = hx(t[1]), n = hx(t[2]);
            std::vector<uint64_t> all, part;
            bool ok = (n % 8 == 0);
            uint64_t beats = n / 8;
            for (uint64_t done = 0; ok && done < beats;) {
                uint64_t chunk = beats - done; if (chunk > 256) chunk = 256;
                uint64_t to4k = (4096 - ((a + done * 8) & 4095)) / 8;   // never across 4 KB
                if (chunk > to4k) chunk = to4k;
                ok = axi_read(a + done * 8, (size_t)chunk, part, err);
                all.insert(all.end(), part.begin(), part.end());
                done += chunk;
            }
            if (ok) fprintf(proto, "V %s\n", hexbytes(all).c_str());
            else fprintf(proto, "E %s\n", err.empty() ? "nbytes" : err.c_str());
        }
        else if (t[0] == "WB" && t.size() == 3) {
            uint64_t a = hx(t[1]);
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
            if (ok) fprintf(proto, "OK\n");
            else fprintf(proto, "E %s\n", err.empty() ? "hex" : err.c_str());
        }
        else if (t[0] == "BR" && t.size() == 3) {
            uint32_t w16[16];
            bd_read((int)hx(t[1]), (uint32_t)hx(t[2]), w16);
            std::string o;
            char b[12];
            for (int i = 0; i < 16; ++i) { snprintf(b, sizeof b, "%08x", w16[i]); o = std::string(b) + o; }
            fprintf(proto, "V %s\n", o.c_str());
        }
        else if (t[0] == "BW" && t.size() == 4) {
            const std::string &h = t[3];
            if (h.size() != 128) { fprintf(proto, "E hex\n"); continue; }
            uint32_t w16[16];
            for (int i = 0; i < 16; ++i) w16[i] = (uint32_t)strtoul(h.substr(128 - 8 * (i + 1), 8).c_str(), nullptr, 16);
            bd_write((int)hx(t[1]), (uint32_t)hx(t[2]), w16);
            fprintf(proto, "OK\n");
        }
        else if (t[0] == "SC" && t.size() == 2) {
            fprintf(proto, "V %s\n", scopes_with(t[1]).c_str());
        }
        else if (t[0] == "PE" && t.size() == 5) {
            std::string o;
            if (peek(t[1], t[2], hx(t[3]), hx(t[4]), o, err)) fprintf(proto, "V %s\n", o.c_str());
            else fprintf(proto, "E %s\n", err.c_str());
        }
        else if (t[0] == "PK" && t.size() == 5) {
            if (poke(t[1], t[2], hx(t[3]), t[4], err)) { dut->eval(); fprintf(proto, "OK\n"); }
            else fprintf(proto, "E %s\n", err.c_str());
        }
        else if (t[0] == "T" && t.size() == 2) {
            cycles(sys0_idx, hx(t[1]));
            fprintf(proto, "OK\n");
        }
        else if (t[0] == "S") {
            fprintf(proto, "V sys=%llx ctrl=%llx decerr=%08x sys_rstn=%x locked=%x ps=%llx\n",
                    (unsigned long long)clks[sys0_idx].rises, (unsigned long long)clks[ctrl_idx].rises,
                    (unsigned)dut->stat_decerr, (unsigned)dut->sys_rstn, (unsigned)dut->wiz_locked,
                    (unsigned long long)now_ps);
        }
        else fprintf(proto, "E unknown\n");
    }
    dut->final();
    delete dut;
    delete ctx;
    return 0;
}
