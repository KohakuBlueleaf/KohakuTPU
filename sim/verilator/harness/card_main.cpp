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

#ifdef _WIN32
// A sampling profiler for the model (`PS` starts, `PX <file>` stops and writes
// "base <hex>", one "mod <base> <size> <path>" per loaded module, then one
// sampled instruction address per line): gprof gets no samples under MinGW.
// Symbolise with `nm` on vsim.exe.
#define PSAPI_VERSION 2  // the K32* entry points in kernel32: no -lpsapi
#include <windows.h>
#include <psapi.h>
static HANDLE prof_target;
static std::vector<uint64_t> prof_pcs;
static volatile bool prof_on = false;
static DWORD WINAPI prof_loop(LPVOID) {
    while (prof_on) {
        if (SuspendThread(prof_target) != (DWORD)-1) {
            CONTEXT c;
            c.ContextFlags = CONTEXT_CONTROL;
            if (GetThreadContext(prof_target, &c)) prof_pcs.push_back(c.Rip);
            ResumeThread(prof_target);
        }
        // Spin ~100 us: Sleep(1) sleeps a scheduler tick (~15 ms).
        LARGE_INTEGER f, a, b;
        QueryPerformanceFrequency(&f);
        QueryPerformanceCounter(&a);
        do { QueryPerformanceCounter(&b); } while ((b.QuadPart - a.QuadPart) * 10000 < f.QuadPart);
    }
    return 0;
}
static void prof_start() {
    DuplicateHandle(GetCurrentProcess(), GetCurrentThread(), GetCurrentProcess(), &prof_target,
                    0, FALSE, DUPLICATE_SAME_ACCESS);
    prof_pcs.clear();
    prof_on = true;
    CreateThread(nullptr, 0, prof_loop, nullptr, 0, nullptr);
}
static void prof_stop(const std::string &path) {
    prof_on = false;
    Sleep(20);
    FILE *f = fopen(path.c_str(), "w");
    if (!f) return;
    fprintf(f, "base %llx\n", (unsigned long long)(uintptr_t)GetModuleHandle(nullptr));
    HMODULE mods[512];
    DWORD need = 0;
    if (EnumProcessModules(GetCurrentProcess(), mods, sizeof mods, &need)) {
        for (DWORD i = 0; i < need / sizeof(HMODULE) && i < 512; ++i) {
            MODULEINFO mi;
            char name[MAX_PATH];
            if (!GetModuleInformation(GetCurrentProcess(), mods[i], &mi, sizeof mi)) continue;
            if (!GetModuleFileNameA(mods[i], name, sizeof name)) continue;
            fprintf(f, "mod %llx %lx %s\n", (unsigned long long)(uintptr_t)mi.lpBaseOfDll,
                    (unsigned long)mi.SizeOfImage, name);
        }
    }
    for (uint64_t pc : prof_pcs) fprintf(f, "%llx\n", (unsigned long long)pc);
    fclose(f);
}
#endif

// Legacy hook verilated.cpp names (weak on Linux, undefined under mingw). Not
// ctx->time(): that falls back to this hook and the two recurse.
double sc_time_stamp() { return 0.0; }

// Elements over every unpacked dimension (1..udims), so a 2-D array counts whole.
static uint64_t var_elements(const VerilatedVar &v) {
    uint64_t n = 1;
    for (int d = 1; d <= v.udims(); ++d) n *= (uint64_t)v.elements(d);
    return n;
}

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
    bool     mat2x;
    bool     dead;      // drives no logic in this card: never scheduled
};
static std::vector<Clk> clks;
static std::vector<int> live;  // indices of the clocks that are not dead
static uint64_t now_ps = 0;
static uint64_t evals = 0;  // dut->eval() calls, for `S`: the model's cost is per eval
static int ctrl_idx, bus1_idx, sys0_idx, ddr_idx[4];

static int add_clk(const char *name, uint64_t period_ps, CData *vec, int bit, int die = -1, int out = -1) {
    clks.push_back({name, vec, bit, period_ps / 2, period_ps / 2, 0, die, out, 0,
                    strcmp(name, "mat2x") == 0, false});
    *vec &= (CData)~(1u << bit);
    return (int)clks.size() - 1;
}

static void drive(Clk &c) {
    if (c.level) *c.vec |= (CData)(1u << c.bit);
    else         *c.vec &= (CData)~(1u << c.bit);
}

// The wizard state the clocks below are timed from; retiming runs only when it
// changes, which after lock is never.
struct WizState {
    uint64_t locked, rstn, divclk, mult, div[4];
    bool operator==(const WizState &o) const {
        return locked == o.locked && rstn == o.rstn && divclk == o.divclk && mult == o.mult &&
               div[0] == o.div[0] && div[1] == o.div[1] && div[2] == o.div[2] && div[3] == o.div[3];
    }
};
static WizState wiz_seen;
static bool wiz_valid = false;

static WizState wiz_now() {
    WizState w{};
    w.locked = dut->wiz_locked; w.rstn = dut->rstn;
    w.divclk = dut->wiz_divclk; w.mult = dut->wiz_mult;
    for (int d = 0; d < 4; ++d) w.div[d] = dut->wiz_div[d];
    return w;
}

// A mesh-wizard output: f = 100 MHz * M / (D * div), period ps = 10000 * D * div / M;
// stopped while its wizard is unlocked.
static void retime_wizard_clocks() {
    WizState w = wiz_now();
    if (wiz_valid && w == wiz_seen) return;
    wiz_seen = w;
    wiz_valid = true;
    for (auto &c : clks) {
        if (c.die < 0 || c.dead) continue;
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

static bool is_mat2x(const Clk &c) { return c.mat2x; }

// The earliest edge; at a tie, mat2x FIRST, as one edge per eval always ran it
// (its divided 1x edge must not follow the sysnode's in time). A dead clock's
// next_ps is NEVER, so it is never picked.
static int next_clk() {
    int k = live[0];
    for (size_t j = 1; j < live.size(); ++j) {
        int i = live[j];
        const Clk &c = clks[i], &b = clks[k];
        if (c.next_ps < b.next_ps || (c.next_ps == b.next_ps && c.mat2x && !b.mat2x)) k = i;
    }
    return k;
}

// Die d's noc/mat2x/vec clocks reach only its mesh (tests/system/card_v9_*.v); a
// die without one, a RAM stub, leaves them unconnected. A die has a mesh iff the
// model has a public scope under `u_mesh<d>.` (card.vlt makes the sysnode's
// scratchpad public). Without any such scope nothing is presumed dead.
static void mark_dead_clocks() {
    const VerilatedScopeNameMap *m = ctx->scopeNameMap();
    if (!m) return;
    bool mesh[4] = {false, false, false, false};
    for (const auto &kv : *m)
        for (int d = 0; d < 4; ++d)
            if (strstr(kv.first, (std::string(".u_mesh") + std::to_string(d) + ".").c_str()))
                mesh[d] = true;
    if (!(mesh[0] || mesh[1] || mesh[2] || mesh[3])) return;
    for (auto &c : clks) {
        if (c.die < 0 || mesh[c.die] || strcmp(c.name, "sys") == 0) continue;
        c.dead = true;
        c.next_ps = NEVER;
    }
}

// Edges due at one instant share ONE eval, except that `mat2x` shares only with
// `mat2x`: the matmul 1x clock is DIVIDED from it in RTL (ktpu_div2), so in an
// eval with a sysnode edge the derived edge lands after the sysnode flops have
// updated and samples their new values (a host read came back 1 beat of 4
// with RLAST). --one-edge: every edge its own eval.
static bool coalesce = false;  // --coalesce: measured 1.25x, and host reads fail

// --join a,b: only clocks so named may share an eval (bisecting a fault).
static std::string join_names;

static bool named(const Clk &c) {
    if (join_names.empty()) return true;
    return ("," + join_names + ",").find(std::string(",") + c.name + ",") != std::string::npos;
}

// Whether `c`, due at the same instant as `lead`, may share its eval. Both are
// read BEFORE either edge is applied: applying one advances its next_ps.
static bool joins(const Clk &lead, const Clk &c) {
    if (!coalesce || c.next_ps != lead.next_ps || !named(lead) || !named(c)) return false;
    return is_mat2x(lead) == is_mat2x(c);
}

static void apply_edge(Clk &c) {
    c.level ^= 1;
    drive(c);
    if (c.level) c.rises++;
    c.next_ps += c.half_ps;
}

static void tick_one() {
    int k = next_clk();
    now_ps = clks[k].next_ps;
    ctx->time(now_ps);
    if (coalesce) {
        // Decided before any edge is applied: applying one advances its next_ps.
        bool due[64] = {false};
        for (size_t i = 0; i < clks.size(); ++i) due[i] = (int)i == k || joins(clks[k], clks[i]);
        for (size_t i = 0; i < clks.size(); ++i)
            if (due[i]) apply_edge(clks[i]);
    } else {
        apply_edge(clks[k]);
    }
    dut->eval();
    ++evals;
    retime_wizard_clocks();
}

// Stop just before `idx` rises: that domain's outputs settled, the edge not taken.
static void until_before_rise(int idx) {
    for (;;) {
        int k = next_clk();
        bool due = k == idx || joins(clks[k], clks[idx]);
        if (due && clks[idx].level == 0) return;
        tick_one();
    }
}

// After until_before_rise(idx): tick until idx has risen (an edge that may not
// share its eval, mat2x, can be due at the same instant and goes first).
static void take_rise(int idx) {
    for (uint64_t r = clks[idx].rises; clks[idx].rises == r;) tick_one();
}

static void cycles(int idx, uint64_t n) {
    for (uint64_t i = 0; i < n; ++i) {
        until_before_rise(idx);
        take_rise(idx);
    }
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
        take_rise(bus1_idx);
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
        take_rise(bus1_idx);
        if (ar_hs) dut->h0_arvalid = 0;
        if (r_hs) {
            out.push_back(rdata);
            if (rresp != 0) { err = "rresp " + std::to_string((int)rresp); dut->h0_rready = 0; return false; }
            if (rlast) last = true;
        }
    }
    dut->h0_rready = 0;
    if (!last) { err = "read stalled after " + std::to_string(out.size()) + " beats"; return false; }
    if (out.size() != n) {
        err = "beat count " + std::to_string(out.size()) + " of " + std::to_string(n) +
              " at " + std::to_string(now_ps) + " ps";
        return false;
    }
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
            uint64_t n = var_elements(var);
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
    uint64_t n = var_elements(*vp);
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
    uint64_t n = var_elements(*vp);
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
    // Each bus and DDR clock at its own MMCM's period, as on the board.
    // --aligned: one period per family (a timing experiment, not the default).
    bool skew_bus = true, skew_ddr = true;
    uint64_t xdma_ps = 40000; // the PCIe clock: idle with the cable out; slow here
    bool all_clocks = false;  // --all-clocks: toggle the dead ones too
    for (int i = 1; i < argc; ++i) {
        std::string a = argv[i];
        if (a == "--all-clocks") all_clocks = true;
        if (a == "--settle" && i + 1 < argc) settle = strtoull(argv[++i], nullptr, 0);
        if (a == "--coalesce") coalesce = true;
        if (a == "--join" && i + 1 < argc) join_names = argv[++i];
        if (a == "--skew") skew_bus = skew_ddr = true;
        if (a == "--skew-ddr") skew_ddr = true;
        if (a == "--aligned") skew_bus = skew_ddr = false;
        if (a == "--ddr-sync") skew_ddr = false;  // DDR on the sysnode's edges only
        if (a == "--bus-sync") skew_bus = false;  // one bus period on every die
        if (a == "--xdma-ps" && i + 1 < argc) xdma_ps = strtoull(argv[++i], nullptr, 0);
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
    add_clk("xdma", xdma_ps, &dut->clk_xdma, 0);
    static const uint64_t bus_skew[4] = {5000, 5010, 4990, 5020};
    static const uint64_t ddr_skew[4] = {3332, 3336, 3328, 3340};
    for (int d = 0; d < 4; ++d) {
        int b = add_clk("bus", skew_bus ? bus_skew[d] : 5000, &dut->bus_clk, d);
        if (d == 1) bus1_idx = b;
        // Aligned: the sysnode's period as the wizard gives it, 2 x 1666 ps.
        ddr_idx[d] = add_clk("ddr", skew_ddr ? ddr_skew[d] : 3332, &dut->ddr_clk, d);
    }
    for (int d = 0; d < 4; ++d) {
        add_clk("noc",   3334, &dut->noc_clk,   d, d, 0);
        add_clk("mat2x", 1666, &dut->mat2x_clk, d, d, 1);
        add_clk("vec",   3334, &dut->vec_clk,   d, d, 2);
        int s = add_clk("sys", 3334, &dut->sys_clk, d, d, 3);
        if (d == 0) sys0_idx = s;
    }
    if (!all_clocks) mark_dead_clocks();
    for (size_t i = 0; i < clks.size(); ++i)
        if (!clks[i].dead) live.push_back((int)i);

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
            fprintf(proto, "V sys=%llx ctrl=%llx decerr=%08x sys_rstn=%x locked=%x ps=%llx evals=%llx\n",
                    (unsigned long long)clks[sys0_idx].rises, (unsigned long long)clks[ctrl_idx].rises,
                    (unsigned)dut->stat_decerr, (unsigned)dut->sys_rstn, (unsigned)dut->wiz_locked,
                    (unsigned long long)now_ps, (unsigned long long)evals);
        }
#ifdef _WIN32
        else if (t[0] == "PS") { prof_start(); fprintf(proto, "OK\n"); }
        else if (t[0] == "PX" && t.size() == 2) {
            prof_stop(t[1]);
            fprintf(proto, "V %zu\n", prof_pcs.size());
        }
#endif
        else if (t[0] == "C") {
            std::string o;
            char b[96];
            for (const auto &c : clks) {
                snprintf(b, sizeof b, " %s%d:%llu:%llu", c.name, c.die < 0 ? 0 : c.die,
                         (unsigned long long)(2 * c.half_ps),
                         (unsigned long long)(c.next_ps == NEVER ? 0 : c.next_ps % (2 * c.half_ps)));
                o += b;
            }
            fprintf(proto, "V%s\n", o.c_str());
        }
        else fprintf(proto, "E unknown\n");
    }
    dut->final();
    delete dut;
    delete ctx;
    return 0;
}
