// Two RV64 control complexes, one fabric memory, both running at once.
//
// ONE MEMORY FOR BOTH, and the two units are loaded and kicked INTERLEAVED.
// Loading A fully and then B would pass even if the units shared internal
// state; interleaving means anything shared lands one image on top of the other.
// The shared memory is deliberate -- it is how the mailbox will work, and it is
// what catches an address decode that ignores the high bits and aliases the two
// units onto the same lines.
//
// Each unit runs a DIFFERENT program so a crossed console is visible rather
// than symmetric.

#include "Vrv64_syscore_pair.h"
#include "verilated.h"

#include <cstdint>
#include <cstdio>
#include <cstring>
#include <map>
#include <set>
#include <string>
#include <unordered_map>
#include <vector>

static const uint32_t H_IMEM = 0x0000'0000u;
static const uint32_t H_SPAD = 0x1000'0000u;
static const uint32_t H_CTRL = 0x2000'0000u;
static const uint8_t  HR_BOOT = 0x00, HR_PC = 0x08, HR_STATUS = 0x18,
                      HR_EXIT = 0x20;
static const uint64_t SPAD_BASE = 0x0001'0000ull;

static VerilatedContext *ctx;
static Vrv64_syscore_pair *dut;
static uint64_t cycles = 0;
static int LATENCY = 6;

static std::unordered_map<uint64_t, uint64_t> node;   // shared, by 8-byte word

static uint64_t rd_node(uint64_t w) {
    auto it = node.find(w);
    return it == node.end() ? 0 : it->second;
}

// One AXI port's worth of state, instantiated twice.
struct Port {
    int r_wait = -1, w_wait = -1;
    uint64_t r_addr = 0, w_addr = 0;
    bool got_aw = false, got_w = false;
    uint32_t wdata[8] = {0};
    uint32_t wstrb = 0;
    uint64_t reads = 0, writes = 0;
    // The exact words touched. A min/max span is useless here: each unit writes
    // three separate regions, so the spans interleave even when disjoint.
    std::set<uint64_t> touched;
};
static Port pa, pb;

template <typename A>
static void serve(Port &p, A &a) {
    *a.arready = (p.r_wait < 0);
    if (*a.arvalid && p.r_wait < 0) { p.r_addr = *a.araddr; p.r_wait = LATENCY; ++p.reads; }
    *a.rvalid = 0;
    if (p.r_wait == 0) {
        for (int l = 0; l < 4; ++l) {
            uint64_t v = rd_node((p.r_addr + 8 * l) >> 3);
            a.rdata[2 * l]     = (uint32_t)v;
            a.rdata[2 * l + 1] = (uint32_t)(v >> 32);
        }
        *a.rvalid = 1; *a.rlast = 1; p.r_wait = -1;
    } else if (p.r_wait > 0) --p.r_wait;

    *a.awready = (p.w_wait < 0);
    *a.wready  = (p.w_wait < 0);
    if (*a.awvalid && p.w_wait < 0 && !p.got_aw) { p.w_addr = *a.awaddr; p.got_aw = true; }
    if (*a.wvalid && p.w_wait < 0 && !p.got_w) {
        for (int i = 0; i < 8; ++i) p.wdata[i] = a.wdata[i];
        p.wstrb = *a.wstrb;
        p.got_w = true;
    }
    *a.bvalid = 0;
    if (p.got_aw && p.got_w && p.w_wait < 0) { p.w_wait = LATENCY; ++p.writes; }
    if (p.w_wait == 0) {
        for (int l = 0; l < 4; ++l) {
            uint64_t v = ((uint64_t)p.wdata[2 * l + 1] << 32) | p.wdata[2 * l];
            uint8_t s = (uint8_t)(p.wstrb >> (8 * l));
            if (!s) continue;
            uint64_t &w = node[(p.w_addr + 8 * l) >> 3];
            for (int b = 0; b < 8; ++b)
                if (s & (1 << b)) {
                    uint64_t m = 0xffull << (b * 8);
                    w = (w & ~m) | (v & m);
                }
        }
        *a.bvalid = 1; p.w_wait = -1; p.got_aw = p.got_w = false;
    } else if (p.w_wait > 0) --p.w_wait;
}

// Verilator gives plain members, not pointers, so bind them by hand per port.
struct PortA {
    uint8_t *arready, *arvalid, *rvalid, *rlast, *awready, *wready, *awvalid,
            *wvalid, *bvalid;
    uint64_t *araddr, *awaddr;
    uint32_t *rdata, *wdata, *wstrb;
};

static std::string con_a, con_b;

static void tick() {
    dut->clk = 0;
    {
        // port A
        dut->a_arready = (pa.r_wait < 0);
        if (dut->a_arvalid && pa.r_wait < 0) { pa.r_addr = dut->a_araddr; pa.r_wait = LATENCY; ++pa.reads; }
        dut->a_rvalid = 0;
        if (pa.r_wait == 0) {
            for (int l = 0; l < 4; ++l) {
                uint64_t v = rd_node((pa.r_addr + 8 * l) >> 3);
                dut->a_rdata[2*l] = (uint32_t)v; dut->a_rdata[2*l+1] = (uint32_t)(v >> 32);
            }
            dut->a_rvalid = 1; dut->a_rlast = 1; pa.r_wait = -1;
        } else if (pa.r_wait > 0) --pa.r_wait;
        dut->a_awready = (pa.w_wait < 0);
        dut->a_wready  = (pa.w_wait < 0);
        if (dut->a_awvalid && pa.w_wait < 0 && !pa.got_aw) { pa.w_addr = dut->a_awaddr; pa.got_aw = true; }
        if (dut->a_wvalid && pa.w_wait < 0 && !pa.got_w) {
            for (int i = 0; i < 8; ++i) pa.wdata[i] = dut->a_wdata[i];
            pa.wstrb = dut->a_wstrb; pa.got_w = true;
        }
        dut->a_bvalid = 0;
        if (pa.got_aw && pa.got_w && pa.w_wait < 0) { pa.w_wait = LATENCY; ++pa.writes; }
        if (pa.w_wait == 0) {
            for (int l = 0; l < 4; ++l) {
                uint64_t v = ((uint64_t)pa.wdata[2*l+1] << 32) | pa.wdata[2*l];
                uint8_t s = (uint8_t)(pa.wstrb >> (8 * l));
                if (!s) continue;
                uint64_t &w = node[(pa.w_addr + 8 * l) >> 3];
                for (int b = 0; b < 8; ++b)
                    if (s & (1 << b)) { uint64_t m = 0xffull << (b*8); w = (w & ~m) | (v & m); }
            }
            for (int l = 0; l < 4; ++l)
                if ((uint8_t)(pa.wstrb >> (8 * l)))
                    pa.touched.insert((pa.w_addr + 8 * l) >> 3);
            dut->a_bvalid = 1; pa.w_wait = -1; pa.got_aw = pa.got_w = false;
        } else if (pa.w_wait > 0) --pa.w_wait;

        // port B
        dut->b_arready = (pb.r_wait < 0);
        if (dut->b_arvalid && pb.r_wait < 0) { pb.r_addr = dut->b_araddr; pb.r_wait = LATENCY; ++pb.reads; }
        dut->b_rvalid = 0;
        if (pb.r_wait == 0) {
            for (int l = 0; l < 4; ++l) {
                uint64_t v = rd_node((pb.r_addr + 8 * l) >> 3);
                dut->b_rdata[2*l] = (uint32_t)v; dut->b_rdata[2*l+1] = (uint32_t)(v >> 32);
            }
            dut->b_rvalid = 1; dut->b_rlast = 1; pb.r_wait = -1;
        } else if (pb.r_wait > 0) --pb.r_wait;
        dut->b_awready = (pb.w_wait < 0);
        dut->b_wready  = (pb.w_wait < 0);
        if (dut->b_awvalid && pb.w_wait < 0 && !pb.got_aw) { pb.w_addr = dut->b_awaddr; pb.got_aw = true; }
        if (dut->b_wvalid && pb.w_wait < 0 && !pb.got_w) {
            for (int i = 0; i < 8; ++i) pb.wdata[i] = dut->b_wdata[i];
            pb.wstrb = dut->b_wstrb; pb.got_w = true;
        }
        dut->b_bvalid = 0;
        if (pb.got_aw && pb.got_w && pb.w_wait < 0) { pb.w_wait = LATENCY; ++pb.writes; }
        if (pb.w_wait == 0) {
            for (int l = 0; l < 4; ++l) {
                uint64_t v = ((uint64_t)pb.wdata[2*l+1] << 32) | pb.wdata[2*l];
                uint8_t s = (uint8_t)(pb.wstrb >> (8 * l));
                if (!s) continue;
                uint64_t &w = node[(pb.w_addr + 8 * l) >> 3];
                for (int b = 0; b < 8; ++b)
                    if (s & (1 << b)) { uint64_t m = 0xffull << (b*8); w = (w & ~m) | (v & m); }
            }
            for (int l = 0; l < 4; ++l)
                if ((uint8_t)(pb.wstrb >> (8 * l)))
                    pb.touched.insert((pb.w_addr + 8 * l) >> 3);
            dut->b_bvalid = 1; pb.w_wait = -1; pb.got_aw = pb.got_w = false;
        } else if (pb.w_wait > 0) --pb.w_wait;
    }
    dut->eval();
    if (dut->a_console_we) con_a.push_back((char)dut->a_console);
    if (dut->b_console_we) con_b.push_back((char)dut->b_console);
    dut->clk = 1;
    dut->eval();
    ctx->timeInc(1);
    ++cycles;
}

// ---- images ---------------------------------------------------------------
struct Img {
    std::map<uint64_t, uint8_t> b;
    uint64_t entry = 0, text = 0, spad = 0;
    std::string expect;
};

static bool load(Img &im, const char *path) {
    FILE *f = fopen(path, "rb");
    if (!f) { printf("  cannot open %s\n", path); return false; }
    std::vector<uint8_t> v;
    fseek(f, 0, SEEK_END); v.resize(ftell(f)); fseek(f, 0, SEEK_SET);
    if (fread(v.data(), 1, v.size(), f) != v.size()) { fclose(f); return false; }
    fclose(f);
    auto u16 = [&](size_t o) { return (uint16_t)(v[o] | (v[o+1] << 8)); };
    auto u64 = [&](size_t o) {
        uint64_t x = 0; for (int i = 7; i >= 0; --i) x = (x << 8) | v[o+i];
        return x;
    };
    im.entry = u64(24);
    uint64_t ph = u64(32);
    uint16_t pe = u16(54), pn = u16(56);
    for (int i = 0; i < pn; ++i) {
        size_t p = ph + (size_t)i * pe;
        uint32_t ty = (uint32_t)(v[p] | (v[p+1] << 8) | (v[p+2] << 16) | (v[p+3] << 24));
        if (ty != 1) continue;
        uint64_t off = u64(p+8), va = u64(p+16), fs = u64(p+32), ms = u64(p+40);
        for (uint64_t k = 0; k < fs; ++k) im.b[va + k] = v[off + k];
        for (uint64_t k = fs; k < ms; ++k) im.b[va + k] = 0;
    }
    for (auto &kv : im.b) {
        if (kv.first < SPAD_BASE) { if (kv.first + 1 > im.text) im.text = kv.first + 1; }
        else { uint64_t o = kv.first - SPAD_BASE; if (o + 1 > im.spad) im.spad = o + 1; }
    }
    return true;
}

static uint8_t byte_at(Img &im, uint64_t a) {
    auto it = im.b.find(a);
    return it == im.b.end() ? 0 : it->second;
}

static void hw_a(uint32_t addr, uint64_t data) {
    dut->a_hs_addr = addr; dut->a_hs_wdata = data; dut->a_hs_wstrb = 0xff;
    dut->a_hs_wr = 1;
}
static void hw_b(uint32_t addr, uint64_t data) {
    dut->b_hs_addr = addr; dut->b_hs_wdata = data; dut->b_hs_wstrb = 0xff;
    dut->b_hs_wr = 1;
}
static void hw_clear() {
    dut->a_hs_wr = 0; dut->a_hs_wstrb = 0;
    dut->b_hs_wr = 0; dut->b_hs_wstrb = 0;
}

static uint64_t hr(bool is_a, uint32_t addr) {
    if (is_a) { dut->a_hs_addr = addr; dut->a_hs_rd = 1; }
    else      { dut->b_hs_addr = addr; dut->b_hs_rd = 1; }
    tick();
    dut->a_hs_rd = 0; dut->b_hs_rd = 0;
    return is_a ? dut->a_hs_rdata : dut->b_hs_rdata;
}

int main(int argc, char **argv) {
    ctx = new VerilatedContext;
    ctx->commandArgs(argc, argv);
    const char *ea = nullptr, *eb = nullptr;
    uint64_t max_cycles = 20000000;
    Img A, B;
    A.expect = "syscore ok";
    B.expect = "syscore ok";
    for (int i = 1; i < argc; ++i) {
        std::string a = argv[i];
        if (a == "--elf-a" && i + 1 < argc) ea = argv[++i];
        else if (a == "--elf-b" && i + 1 < argc) eb = argv[++i];
        else if (a == "--expect-a" && i + 1 < argc) A.expect = argv[++i];
        else if (a == "--expect-b" && i + 1 < argc) B.expect = argv[++i];
        else if (a == "--latency" && i + 1 < argc) LATENCY = atoi(argv[++i]);
        else if (a == "--max-cycles" && i + 1 < argc)
            max_cycles = strtoull(argv[++i], nullptr, 0);
    }
    if (!ea || !eb) { printf("  --elf-a and --elf-b are required\n"); return 2; }
    if (!load(A, ea) || !load(B, eb)) return 2;
    printf("  A text %llu spad %llu   B text %llu spad %llu   latency %d\n",
           (unsigned long long)A.text, (unsigned long long)A.spad,
           (unsigned long long)B.text, (unsigned long long)B.spad, LATENCY);

    dut = new Vrv64_syscore_pair(ctx);
    dut->resetn = 0;
    hw_clear();
    dut->a_hs_rd = dut->b_hs_rd = 0;
    for (int i = 0; i < 8; ++i) tick();
    dut->resetn = 1;
    for (int i = 0; i < 4; ++i) tick();

    // INTERLEAVED, word by word.
    uint64_t tmax = A.text > B.text ? A.text : B.text;
    for (uint64_t a = 0; a < tmax; a += 4) {
        if (a < A.text) {
            uint32_t w = 0;
            for (int j = 0; j < 4; ++j) w |= (uint32_t)byte_at(A, a + j) << (8 * j);
            hw_a(H_IMEM | (uint32_t)a, w);
        }
        if (a < B.text) {
            uint32_t w = 0;
            for (int j = 0; j < 4; ++j) w |= (uint32_t)byte_at(B, a + j) << (8 * j);
            hw_b(H_IMEM | (uint32_t)a, w);
        }
        tick();
        hw_clear();
    }
    uint64_t smax = A.spad > B.spad ? A.spad : B.spad;
    for (uint64_t a = 0; a < smax; a += 8) {
        if (a < A.spad) {
            uint64_t w = 0;
            for (int j = 0; j < 8; ++j) w |= (uint64_t)byte_at(A, SPAD_BASE + a + j) << (8 * j);
            hw_a(H_SPAD | (uint32_t)a, w);
        }
        if (a < B.spad) {
            uint64_t w = 0;
            for (int j = 0; j < 8; ++j) w |= (uint64_t)byte_at(B, SPAD_BASE + a + j) << (8 * j);
            hw_b(H_SPAD | (uint32_t)a, w);
        }
        tick();
        hw_clear();
    }
    uint64_t load_done = cycles;

    hw_a(H_CTRL | HR_PC, A.entry); hw_b(H_CTRL | HR_PC, B.entry); tick(); hw_clear();
    hw_a(H_CTRL | HR_BOOT, 1);     hw_b(H_CTRL | HR_BOOT, 1);     tick(); hw_clear();

    uint64_t run0 = cycles;
    uint64_t sa = 0, sb = 0;
    while (cycles < max_cycles) {
        for (int i = 0; i < 256; ++i) tick();
        sa = hr(true,  H_CTRL | HR_STATUS);
        sb = hr(false, H_CTRL | HR_STATUS);
        if ((sa & 0xc) && (sb & 0xc)) break;
    }
    uint64_t xa = hr(true,  H_CTRL | HR_EXIT);
    uint64_t xb = hr(false, H_CTRL | HR_EXIT);

    printf("\n  load       %llu cycles (both, interleaved)\n",
           (unsigned long long)load_done);
    printf("  wall       %llu cycles for both to finish\n",
           (unsigned long long)(cycles - run0));
    printf("  A          %llu core cycles, %llu retired, exit %llu, node r%llu w%llu\n",
           (unsigned long long)dut->a_cycles, (unsigned long long)dut->a_retired,
           (unsigned long long)xa, (unsigned long long)pa.reads,
           (unsigned long long)pa.writes);
    printf("  B          %llu core cycles, %llu retired, exit %llu, node r%llu w%llu\n",
           (unsigned long long)dut->b_cycles, (unsigned long long)dut->b_retired,
           (unsigned long long)xb, (unsigned long long)pb.reads,
           (unsigned long long)pb.writes);
    printf("  A says     %s", con_a.c_str());
    printf("  B says     %s", con_b.c_str());

    bool oka = (xa == 0) && con_a.find(A.expect) != std::string::npos;
    bool okb = (xb == 0) && con_b.find(B.expect) != std::string::npos;
    // Disjoint write footprints prove the two units are actually independent,
    // rather than both happening to write the same values to the same place.
    uint64_t shared = 0;
    for (uint64_t w : pa.touched) if (pb.touched.count(w)) ++shared;
    bool disjoint = (shared == 0);
    printf("  A wrote    %zu words   B wrote %zu words   shared %llu   %s\n",
           pa.touched.size(), pb.touched.size(), (unsigned long long)shared,
           disjoint ? "DISJOINT" : "OVERLAPPING");

    bool busy = pa.reads > 0 && pb.reads > 0 && disjoint;
    printf("========================================\n");
    printf("  %s\n", (oka && okb && busy) ? "PASS" : "FAIL");
    printf("========================================\n");

    dut->final();
    delete dut;
    delete ctx;
    return (oka && okb && busy) ? 0 : 1;
}
