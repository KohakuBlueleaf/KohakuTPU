// kos_cluster harness: load each node's image, boot every node, run until all
// have exited or the cycle bound, and report each node's console and exit.
//
//   vsim --elf os.elf [--elf-N 1 other.elf] [--blob 0:0x10000:boot0.bin]
//        [--stdin 0 "text"] [--expect 0 "ok"] [--max-cycles N]
//        [--dump 0:0x10100000:4096:out.bin] [--quiet]
//        [--boots K] [--blob-at 1 0:0x10000:boot0.bin] [--hold 1]
//
// Image bytes are placed by physical address (p_paddr): below 0x1_0000 the
// instruction window, 0x1_0000..0x1_FFFF the scratchpad, at or above 2^28 the
// node's DRAM through the RAM backdoor (the one RAM when SHARED). Segments at
// or above 2^40 are not machine addresses (an OS image's per-node part); the
// caller places them with --blob.
//
// --boots K runs the images K times in one model: between boots every node is
// held in reset (HR_BOOT 0), as a host does before reloading a card, and its
// images and blobs are placed again; everything outside the processors (the
// interlink, staging, DRAM) keeps its state. --blob-at B places a blob before
// boot B only (a fresh boot table per boot), --blob before every boot.
// --hold N leaves node N in reset (its blobs are still placed).

#include "Vkos_cluster.h"
#include "verilated.h"

#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <map>
#include <string>
#include <type_traits>
#include <vector>

#ifndef KOS_NODES
#define KOS_NODES 2
#endif
#ifndef KOS_SHARED
#define KOS_SHARED 0
#endif
#ifndef KOS_DRAM_WORDS
#define KOS_DRAM_WORDS 65536
#endif

static const int N = KOS_NODES;
static const uint32_t H_IMEM = 0x0000'0000u, H_SPAD = 0x1000'0000u, H_CTRL = 0x2000'0000u;
static const uint8_t HR_BOOT = 0x00, HR_PC = 0x08, HR_STATUS = 0x18, HR_EXIT = 0x20,
                     HR_HALTPC = 0x28, HR_CYCLES = 0x30, HR_RETIRED = 0x38, HR_STDIN = 0x40;
static const uint64_t SPAD_BASE = 0x1'0000ull, SPAD_END = 0x2'0000ull, NODE_BASE = 0x1000'0000ull;

double sc_time_stamp() { return 0.0; }

static VerilatedContext *ctx;
static Vkos_cluster *dut;
static uint64_t cycles = 0;
static std::string console[N], line[N];
static bool live = true;

// Verilator ports are plain integers up to 64 bits and VlWide above.
template <typename W> static void set_bits(W &w, int lo, int width, uint64_t v) {
    if constexpr (std::is_integral<W>::value) {
        uint64_t m = (width == 64 ? ~0ull : ((1ull << width) - 1)) << lo;
        w = (W)(((uint64_t)w & ~m) | ((v << lo) & m));
    } else {
        for (int b = 0; b < width; ++b) {
            int bit = lo + b;
            uint32_t mk = 1u << (bit % 32);
            w[bit / 32] = ((v >> b) & 1) ? (w[bit / 32] | mk) : (w[bit / 32] & ~mk);
        }
    }
}
template <typename W> static uint64_t get_bits(const W &w, int lo, int width) {
    if constexpr (std::is_integral<W>::value) {
        uint64_t m = width == 64 ? ~0ull : ((1ull << width) - 1);
        return ((uint64_t)w >> lo) & m;
    } else {
        uint64_t v = 0;
        for (int b = 0; b < width; ++b) {
            int bit = lo + b;
            v |= (uint64_t)((w[bit / 32] >> (bit % 32)) & 1) << b;
        }
        return v;
    }
}

static void tick() {
    dut->clk = 0;
    dut->eval();
    for (int n = 0; n < N; ++n) {
        if (!get_bits(dut->console_we, n, 1)) continue;
        char c = (char)get_bits(dut->console, 8 * n, 8);
        console[n].push_back(c);
        if (!live) continue;
        if (c == '\n') {
            printf("[%d] %s\n", n, line[n].c_str());
            fflush(stdout);
            line[n].clear();
        } else {
            line[n].push_back(c);
        }
    }
    dut->clk = 1;
    dut->eval();
    ctx->timeInc(1);
    ++cycles;
}

static void hwrite(int n, uint32_t addr, uint64_t data, uint8_t strb = 0xff) {
    set_bits(dut->hs_addr, 32 * n, 32, addr);
    set_bits(dut->hs_wdata, 64 * n, 64, data);
    set_bits(dut->hs_wstrb, 8 * n, 8, strb);
    set_bits(dut->hs_wr, n, 1, 1);
    tick();
    set_bits(dut->hs_wr, n, 1, 0);
}

static uint64_t hread(int n, uint32_t addr) {
    set_bits(dut->hs_addr, 32 * n, 32, addr);
    set_bits(dut->hs_rd, n, 1, 1);
    tick();
    set_bits(dut->hs_rd, n, 1, 0);
    return get_bits(dut->hs_rdata, 64 * n, 64);
}

static int ram_of(int n) { return KOS_SHARED ? 0 : n; }
static uint32_t word_of(uint64_t pa) { return (uint32_t)((pa >> 6) & (KOS_DRAM_WORDS - 1)); }

static void bd_read(int ram, uint32_t w, uint8_t out[64]) {
    dut->bd_ram = ram;
    dut->bd_addr = w;
    dut->bd_we = 0;
    dut->eval();
    for (int i = 0; i < 64; ++i) out[i] = (uint8_t)get_bits(dut->bd_rdata, 8 * i, 8);
}

static void bd_write(int ram, uint32_t w, const uint8_t in[64]) {
    dut->bd_ram = ram;
    dut->bd_addr = w;
    for (int i = 0; i < 64; ++i) set_bits(dut->bd_wdata, 8 * i, 8, in[i]);
    dut->bd_we = 1;
    tick();
    dut->bd_we = 0;
}

typedef std::map<uint64_t, uint8_t> Bytes;

static bool read_file(const std::string &path, std::vector<uint8_t> &out) {
    FILE *f = fopen(path.c_str(), "rb");
    if (!f) return false;
    fseek(f, 0, SEEK_END);
    out.resize(ftell(f));
    fseek(f, 0, SEEK_SET);
    bool ok = fread(out.data(), 1, out.size(), f) == out.size();
    fclose(f);
    return ok;
}

static bool load_elf(const std::string &path, Bytes &im, uint64_t &entry) {
    std::vector<uint8_t> b;
    if (!read_file(path, b) || b.size() < 64 || b[4] != 2) {
        printf("%s: not a readable 64-bit ELF\n", path.c_str());
        return false;
    }
    auto u16 = [&](size_t o) { return (uint16_t)(b[o] | (b[o + 1] << 8)); };
    auto u32 = [&](size_t o) {
        return (uint32_t)(b[o] | (b[o + 1] << 8) | (b[o + 2] << 16) | ((uint32_t)b[o + 3] << 24));
    };
    auto u64 = [&](size_t o) {
        uint64_t v = 0;
        for (int i = 7; i >= 0; --i) v = (v << 8) | b[o + i];
        return v;
    };
    entry = u64(24);
    uint64_t phoff = u64(32);
    uint16_t phes = u16(54), phnum = u16(56);
    for (int i = 0; i < phnum; ++i) {
        size_t p = phoff + (size_t)i * phes;
        if (u32(p) != 1) continue;
        uint64_t off = u64(p + 8), pa = u64(p + 24), fsz = u64(p + 32);
        if (pa >> 40) continue; // not a machine address: a per-node image the caller places (--blob)
        for (uint64_t k = 0; k < fsz; ++k) im[pa + k] = b[off + k];
    }
    return true;
}

static void place(int n, const Bytes &im) {
    std::map<uint64_t, uint32_t> imem;
    std::map<uint64_t, uint64_t> spad;
    std::map<uint32_t, std::map<int, uint8_t>> dram;
    for (auto &kv : im) {
        uint64_t a = kv.first;
        if (a < SPAD_BASE) imem[a & ~3ull] |= (uint32_t)kv.second << (8 * (a & 3));
        else if (a < SPAD_END) spad[(a - SPAD_BASE) & ~7ull] |= (uint64_t)kv.second << (8 * (a & 7));
        else if (a >= NODE_BASE) dram[word_of(a)][(int)(a & 63)] = kv.second;
    }
    for (auto &kv : imem) hwrite(n, H_IMEM | (uint32_t)kv.first, kv.second);
    for (auto &kv : spad) hwrite(n, H_SPAD | (uint32_t)kv.first, kv.second);
    for (auto &kv : dram) {
        uint8_t w[64];
        bd_read(ram_of(n), kv.first, w);
        for (auto &bv : kv.second) w[bv.first] = bv.second;
        bd_write(ram_of(n), kv.first, w);
    }
}

struct Dump {
    int node;
    uint64_t pa, len;
    std::string file;
};

static bool parse_triple(const char *s, int &node, uint64_t &a, std::string &rest) {
    char *end;
    node = (int)strtol(s, &end, 0);
    if (*end != ':') return false;
    a = strtoull(end + 1, &end, 0);
    if (*end != ':') return false;
    rest = end + 1;
    return true;
}

int main(int argc, char **argv) {
    setvbuf(stdout, nullptr, _IOLBF, 0); // console lines reach a log file as they are printed
    ctx = new VerilatedContext;
    std::string elf[N];
    std::string expect[N];
    struct Blob {
        int boot, node;
        uint64_t pa;
        std::string file;
    };
    std::vector<Blob> blobs;
    std::vector<int> held;
    int boots = 1;
    std::vector<std::pair<int, std::string>> stdin_text;
    std::vector<Dump> dumps;
    uint64_t max_cycles = 50'000'000;
    for (int i = 1; i < argc; ++i) {
        std::string a = argv[i];
        bool more = i + 1 < argc;
        if (a == "--elf" && more) {
            std::string p = argv[++i];
            for (int n = 0; n < N; ++n) elf[n] = p;
        } else if (a == "--elf-N" && i + 2 < argc) {
            int n = atoi(argv[++i]);
            elf[n] = argv[++i];
        } else if (a == "--blob" && more) {
            int n;
            uint64_t pa;
            std::string f;
            if (!parse_triple(argv[++i], n, pa, f)) { printf("bad --blob\n"); return 2; }
            blobs.push_back({-1, n, pa, f});
        } else if (a == "--blob-at" && i + 2 < argc) {
            int b = atoi(argv[++i]), n;
            uint64_t pa;
            std::string f;
            if (!parse_triple(argv[++i], n, pa, f)) { printf("bad --blob-at\n"); return 2; }
            blobs.push_back({b, n, pa, f});
        } else if (a == "--boots" && more) {
            boots = atoi(argv[++i]);
        } else if (a == "--hold" && more) {
            held.push_back(atoi(argv[++i]));
        } else if (a == "--stdin" && i + 2 < argc) {
            int n = atoi(argv[++i]);
            stdin_text.push_back({n, argv[++i]});
        } else if (a == "--expect" && i + 2 < argc) {
            int n = atoi(argv[++i]);
            expect[n] = argv[++i];
        } else if (a == "--max-cycles" && more) {
            max_cycles = strtoull(argv[++i], nullptr, 0);
        } else if (a == "--dump" && more) {
            Dump d;
            std::string rest;
            if (!parse_triple(argv[++i], d.node, d.pa, rest)) { printf("bad --dump\n"); return 2; }
            size_t c = rest.find(':');
            d.len = strtoull(rest.substr(0, c).c_str(), nullptr, 0);
            d.file = rest.substr(c + 1);
            dumps.push_back(d);
        } else if (a == "--quiet") {
            live = false;
        }
    }

    dut = new Vkos_cluster(ctx);
    dut->resetn = 0;
    for (int i = 0; i < 16; ++i) tick();
    dut->resetn = 1;
    for (int i = 0; i < 300; ++i) tick();

    uint64_t entry[N] = {0};
    bool used[N] = {false};
    std::vector<Bytes> images(N);
    for (int n : held)
        if (n >= 0 && n < N) elf[n].clear();
    for (int n = 0; n < N; ++n) {
        if (elf[n].empty()) continue;
        if (!load_elf(elf[n], images[n], entry[n])) return 2;
        used[n] = true;
    }
    for (auto &s : stdin_text)
        for (unsigned char c : s.second) hwrite(s.first, H_CTRL | HR_STDIN, c);

    auto t0 = std::chrono::steady_clock::now();
    uint64_t start = cycles;
    uint64_t st[N] = {0};
    bool done[N];
    auto all = [&]() {
        for (int n = 0; n < N; ++n)
            if (!done[n]) return false;
        return true;
    };
    bool boot_ok = true;
    for (int boot = 0; boot < boots && boot_ok; ++boot) {
        for (int n = 0; n < N; ++n) {
            if (!used[n]) continue;
            hwrite(n, H_CTRL | HR_BOOT, 0);
            if (KOS_SHARED && n > 0 && elf[n] == elf[0]) {
                // One RAM: DRAM bytes are placed once, by node 0.
                Bytes local;
                for (auto &kv : images[n])
                    if (kv.first < NODE_BASE) local.insert(kv);
                place(n, local);
            } else {
                place(n, images[n]);
            }
        }
        for (auto &b : blobs) {
            if (b.boot >= 0 && b.boot != boot) continue;
            std::vector<uint8_t> bytes;
            if (!read_file(b.file, bytes)) {
                printf("cannot read %s\n", b.file.c_str());
                return 2;
            }
            Bytes im;
            for (size_t k = 0; k < bytes.size(); ++k) im[b.pa + k] = bytes[k];
            place(b.node, im);
        }
        for (int n = N - 1; n >= 0; --n) {
            if (!used[n]) continue;
            hwrite(n, H_CTRL | HR_PC, entry[n]);
            hwrite(n, H_CTRL | HR_BOOT, 1);
        }
        uint64_t b0 = cycles;
        for (int n = 0; n < N; ++n) done[n] = !used[n];
        while (cycles - b0 < max_cycles && !all()) {
            for (int i = 0; i < 1024; ++i) tick();
            for (int n = 0; n < N; ++n) {
                if (done[n]) continue;
                st[n] = hread(n, H_CTRL | HR_STATUS);
                if (st[n] & 0xc) done[n] = true;
            }
        }
        for (int n = 0; n < N; ++n)
            if (used[n]) boot_ok = boot_ok && done[n] && (st[n] & 0x8) && hread(n, H_CTRL | HR_EXIT) == 0;
        if (boots > 1) printf("boot %d: %s\n", boot, boot_ok ? "ok" : "FAILED");
    }
    double wall = std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count();

    bool ok = true;
    for (int n = 0; n < N; ++n) {
        if (!used[n]) continue;
        if (live && !line[n].empty()) printf("[%d] %s\n", n, line[n].c_str());
        uint64_t ex = hread(n, H_CTRL | HR_EXIT), pc = hread(n, H_CTRL | HR_HALTPC);
        uint64_t cyc = hread(n, H_CTRL | HR_CYCLES), ret = hread(n, H_CTRL | HR_RETIRED);
        printf("node %d: %s exit %llu status %llx haltpc %llx cycles %llu retired %llu\n", n,
               done[n] ? "done" : "NEVER FINISHED", (unsigned long long)ex, (unsigned long long)st[n],
               (unsigned long long)pc, (unsigned long long)cyc, (unsigned long long)ret);
        if (!live) {
            size_t a = 0;
            while (a < console[n].size()) {
                size_t b = console[n].find('\n', a);
                if (b == std::string::npos) b = console[n].size();
                printf("[%d] %s\n", n, console[n].substr(a, b - a).c_str());
                a = b + 1;
            }
        }
        bool node_ok = done[n] && (st[n] & 0x8) && ex == 0;
        if (!expect[n].empty() && console[n].find(expect[n]) == std::string::npos) {
            printf("node %d: expected \"%s\" on the console\n", n, expect[n].c_str());
            node_ok = false;
        }
        ok = ok && node_ok;
    }
    for (int n = 0; n < N; ++n) printf("link beats node %d: %llu\n", n, (unsigned long long)get_bits(dut->link_beats, 32 * n, 32));
    for (auto &d : dumps) {
        std::vector<uint8_t> out(d.len);
        for (uint64_t k = 0; k < d.len; k += 64) {
            uint8_t w[64];
            bd_read(ram_of(d.node), word_of(d.pa + k), w);
            for (int j = 0; j < 64 && k + j < d.len; ++j) out[k + j] = w[j];
        }
        FILE *f = fopen(d.file.c_str(), "wb");
        if (f) {
            fwrite(out.data(), 1, out.size(), f);
            fclose(f);
        }
    }
    printf("total %llu cycles, %.1f s wall, %.1f kcyc/s\n", (unsigned long long)(cycles - start), wall,
           wall > 0 ? (cycles - start) / wall / 1000.0 : 0.0);
    printf("%s\n", ok ? "PASS" : "FAIL");
    dut->final();
    delete dut;
    delete ctx;
    return ok ? 0 : 1;
}
