// A card-shaped harness for rv64_syscore: the host loads over the slave port,
// and the node port is answered by a sparse memory the way MAG would answer it.
//
// THE NODE MEMORY IS A C++ MAP. That is the whole point of this bench -- the
// unit's address space above NODE_BASE is 40 bits, which no Verilog array can
// elaborate, and it is exactly the space SysCore has to reach and the compute
// unit does not.

#include "Vrv64_syscore.h"
#include "verilated.h"
#if VM_TRACE
#include "verilated_vcd_c.h"
#endif

#include <cstdint>
#include <cstdio>
#include <cstring>
#include <map>
#include <string>
#include <unordered_map>
#include <vector>

static const uint32_t H_IMEM = 0x0000'0000u;
static const uint32_t H_SPAD = 0x1000'0000u;
static const uint32_t H_CTRL = 0x2000'0000u;
static const uint8_t  HR_BOOT = 0x00, HR_PC = 0x08, HR_STATUS = 0x18,
                      HR_EXIT = 0x20, HR_HALTPC = 0x28, HR_STDIN = 0x40;
static const uint64_t SPAD_BASE = 0x0001'0000ull;

static VerilatedContext *ctx;
static Vrv64_syscore *dut;
static uint64_t cycles = 0;
static std::string console;
#if VM_TRACE
static VerilatedVcdC *tfp = nullptr;   // opened only for a --trace build
static bool     trace_on = false;      // gated to the run phase, not the load
static uint64_t vcd_time = 0;          // half-cycle granularity
#endif

// ---- the node's memory, and its latency ------------------------------------
static std::unordered_map<uint64_t, uint64_t> node;   // by 8-byte word
static int  ar_delay = 0, aw_delay = 0;
static int  LATENCY = 6;                              // beats of fabric latency
static uint64_t node_reads = 0, node_writes = 0;

static void node_service() {
    // AR: accept, then answer LATENCY cycles later.
    static int r_wait = -1;
    static uint64_t r_addr = 0;
    dut->cp_arready = (r_wait < 0);
    if (dut->cp_arvalid && r_wait < 0) {
        r_addr = dut->cp_araddr;
        r_wait = LATENCY;
        ++node_reads;
    }
    dut->cp_rvalid = 0;
    if (r_wait == 0) {
        for (int l = 0; l < 4; ++l) {
            auto it = node.find(((r_addr + 8 * l) >> 3));
            uint64_t v = (it == node.end()) ? 0 : it->second;
            dut->cp_rdata[2 * l]     = (uint32_t)v;
            dut->cp_rdata[2 * l + 1] = (uint32_t)(v >> 32);
        }
        dut->cp_rvalid = 1;
        dut->cp_rlast = 1;
        r_wait = -1;
    } else if (r_wait > 0) --r_wait;

    // AW/W: take both, then B after LATENCY.
    static int  w_wait = -1;
    static uint64_t w_addr = 0;
    static bool got_aw = false, got_w = false;
    static uint32_t wdata[8];
    static uint32_t wstrb;
    dut->cp_awready = (w_wait < 0);
    dut->cp_wready  = (w_wait < 0);
    if (dut->cp_awvalid && w_wait < 0 && !got_aw) { w_addr = dut->cp_awaddr; got_aw = true; }
    if (dut->cp_wvalid && w_wait < 0 && !got_w) {
        for (int i = 0; i < 8; ++i) wdata[i] = dut->cp_wdata[i];
        wstrb = dut->cp_wstrb;
        got_w = true;
    }
    dut->cp_bvalid = 0;
    if (got_aw && got_w && w_wait < 0) { w_wait = LATENCY; ++node_writes; }
    if (w_wait == 0) {
        static int shown_wb = 0;
        if (w_addr >= 0x80000000ull && shown_wb++ < 4)
            printf("  WB#%llu addr %llx  lanes %llu %llu %llu %llu\n",
                   (unsigned long long)node_writes, (unsigned long long)w_addr,
                   (unsigned long long)(((uint64_t)wdata[1] << 32) | wdata[0]),
                   (unsigned long long)(((uint64_t)wdata[3] << 32) | wdata[2]),
                   (unsigned long long)(((uint64_t)wdata[5] << 32) | wdata[4]),
                   (unsigned long long)(((uint64_t)wdata[7] << 32) | wdata[6]));
        for (int l = 0; l < 4; ++l) {
            uint64_t v = ((uint64_t)wdata[2 * l + 1] << 32) | wdata[2 * l];
            uint8_t  s = (uint8_t)(wstrb >> (8 * l));
            if (!s) continue;
            uint64_t &w = node[(w_addr + 8 * l) >> 3];
            for (int b = 0; b < 8; ++b)
                if (s & (1 << b)) {
                    uint64_t m = 0xffull << (b * 8);
                    w = (w & ~m) | (v & m);
                }
        }
        // FENCE.I demo: when the guest sets the mailbox GO, copy DRAM code from
        // src to dst so the code the I-cache holds no longer matches memory.
        static bool swapped = false;
        if (!swapped) {
            auto rd = [&](uint64_t a) {
                auto it = node.find(a >> 3);
                return it == node.end() ? 0ull : it->second;
            };
            if (rd(0x10001018)) {                        // H_GO
                uint64_t src = rd(0x10001000), dst = rd(0x10001008),
                         len = rd(0x10001010);
                for (uint64_t i = 0; i < len; i += 8)
                    node[(dst + i) >> 3] = rd(src + i);
                node[0x10001020 >> 3] = 1;               // H_ACK
                swapped = true;
                printf("  [host] copied %llu bytes of DRAM code %llx -> %llx\n",
                       (unsigned long long)len, (unsigned long long)src,
                       (unsigned long long)dst);
            }
        }
        dut->cp_bvalid = 1;
        w_wait = -1;
        got_aw = got_w = false;
    } else if (w_wait > 0) --w_wait;
}

static uint64_t mv_writes = 0, db_writes = 0;

// ---- a peer compute unit on the mesh ---------------------------------------
// Accepts a dispatch and answers it as a unit would: one CU_SIGNAL back, after
// a delay, carrying the code and the argument the test expects.
static uint64_t disp_seen = 0;
static uint64_t last_disp_arg0 = 0, last_disp_dst = 0;
static int      sig_delay = -1;
static uint32_t sig_src_x = 0, sig_src_y = 0;

static const uint32_t T_CU_INST = 0x5, T_CU_SIGNAL = 0x6;

static void noc_service() {
    dut->noc_out_busy = 0;
    if (dut->noc_out_valid && !dut->noc_out_busy) {
        uint32_t hi = dut->noc_out_data[8];       // flit bits [287:256]
        uint32_t ty = (hi >> 12) & 0xf;
        if (ty == T_CU_INST) {
            sig_src_x = (hi >> 28) & 0xf;         // reply from where it went
            sig_src_y = (hi >> 24) & 0xf;
            last_disp_dst  = (sig_src_y << 4) | sig_src_x;
            last_disp_arg0 = ((uint64_t)dut->noc_out_data[1] << 32)
                           | dut->noc_out_data[0];
            ++disp_seen;
            sig_delay = 12;
        }
    }

    dut->noc_in_valid = 0;
    if (sig_delay == 0) {
        for (int i = 0; i < 9; ++i) dut->noc_in_data[i] = 0;
        // code 0x00 (INST_COMPLETE) at payload[7:0], arg at payload[39:8]
        uint32_t arg = (uint32_t)last_disp_arg0;
        dut->noc_in_data[0] = (arg << 8) | 0x00;
        dut->noc_in_data[1] = arg >> 24;
        dut->noc_in_data[8] = (0u << 28) | (0u << 24)            // dst = the PE
                            | (sig_src_x << 20) | (sig_src_y << 16)
                            | (T_CU_SIGNAL << 12) | (0u << 4) | (1u << 3);
        dut->noc_in_valid = 1;
        sig_delay = -1;
    } else if (sig_delay > 0) {
        --sig_delay;
    }
}

static void tick() {
    dut->clk = 0;
    node_service();
    noc_service();
    dut->eval();
#if VM_TRACE
    if (tfp && trace_on) tfp->dump(vcd_time++);
#endif
    if (dut->mv_cfg_en) ++mv_writes;
    if (dut->db_en) ++db_writes;
    if (dut->dbg_console_we) console.push_back((char)dut->dbg_console);
    dut->clk = 1;
    dut->eval();
#if VM_TRACE
    if (tfp && trace_on) tfp->dump(vcd_time++);
#endif
    ctx->timeInc(1);
    ++cycles;
}

static void hwrite(uint32_t addr, uint64_t data, uint8_t strb = 0xff) {
    dut->hs_addr = addr;
    dut->hs_wdata = data;
    dut->hs_wstrb = strb;
    dut->hs_wr = 1;
    tick();
    dut->hs_wr = 0;
    dut->hs_wstrb = 0;
}

static uint64_t hread(uint32_t addr) {
    dut->hs_addr = addr;
    dut->hs_rd = 1;
    tick();
    dut->hs_rd = 0;
    return dut->hs_rdata;
}

// ---- the image -------------------------------------------------------------
static std::map<uint64_t, uint8_t> image;
static uint64_t entry_pc = 0;

static bool load_elf(const char *path) {
    FILE *f = fopen(path, "rb");
    if (!f) { printf("  cannot open %s\n", path); return false; }
    std::vector<uint8_t> b;
    fseek(f, 0, SEEK_END); b.resize(ftell(f)); fseek(f, 0, SEEK_SET);
    if (fread(b.data(), 1, b.size(), f) != b.size()) { fclose(f); return false; }
    fclose(f);
    auto u16 = [&](size_t o) { return (uint16_t)(b[o] | (b[o+1] << 8)); };
    auto u64 = [&](size_t o) {
        uint64_t v = 0; for (int i = 7; i >= 0; --i) v = (v << 8) | b[o+i];
        return v;
    };
    entry_pc = u64(24);
    uint64_t phoff = u64(32);
    uint16_t phes = u16(54), phnum = u16(56);
    for (int i = 0; i < phnum; ++i) {
        size_t p = phoff + (size_t)i * phes;
        uint32_t ty = (uint32_t)(b[p] | (b[p+1] << 8) | (b[p+2] << 16) | (b[p+3] << 24));
        if (ty != 1) continue;
        uint64_t off = u64(p+8), va = u64(p+16), fsz = u64(p+32), msz = u64(p+40);
        for (uint64_t k = 0; k < fsz; ++k) image[va + k] = b[off + k];
        for (uint64_t k = fsz; k < msz; ++k) image[va + k] = 0;
    }
    return true;
}

static uint8_t img(uint64_t a) {
    auto it = image.find(a);
    return it == image.end() ? 0 : it->second;
}

int main(int argc, char **argv) {
    ctx = new VerilatedContext;
    ctx->commandArgs(argc, argv);
    const char *elf = nullptr;
    uint64_t max_cycles = 20000000;
    std::string expect = "syscore ok";
    std::string stdin_input;
    bool have_stdin = false;
    for (int i = 1; i < argc; ++i) {
        std::string a = argv[i];
        if (a == "--elf" && i + 1 < argc) elf = argv[++i];
        else if (a == "--max-cycles" && i + 1 < argc)
            max_cycles = strtoull(argv[++i], nullptr, 0);
        else if (a == "--latency" && i + 1 < argc) LATENCY = atoi(argv[++i]);
        else if (a == "--expect" && i + 1 < argc) expect = argv[++i];
        else if (a == "--stdin" && i + 1 < argc) {
            stdin_input = argv[++i];
            have_stdin = true;
        }
    }
    if (!elf) { printf("  --elf is required\n"); return 2; }
    if (!load_elf(elf)) return 2;

    uint64_t text_bytes = 0, spad_bytes = 0;
    for (auto &kv : image) {
        if (kv.first < SPAD_BASE) { if (kv.first + 1 > text_bytes) text_bytes = kv.first + 1; }
        else if (kv.first < 0x80000000ull) { uint64_t o = kv.first - SPAD_BASE;
               if (o + 1 > spad_bytes) spad_bytes = o + 1; }
        // else: DRAM code (.dram_text) -- placed into node memory below.
    }
    printf("  text %llu bytes, spad %llu bytes, node latency %d\n",
           (unsigned long long)text_bytes, (unsigned long long)spad_bytes, LATENCY);

    dut = new Vrv64_syscore(ctx);
#if VM_TRACE
    ctx->traceEverOn(true);
    tfp = new VerilatedVcdC;
    dut->trace(tfp, 99);
    tfp->open("trace.vcd");
#endif
    dut->resetn = 0;
    dut->hs_wr = dut->hs_rd = 0;
    dut->hs_wstrb = 0;
    dut->irq_summary = 0;
    dut->mv_busy = 0;
    dut->mv_fault = 0;
    dut->mv_done = 0;
    dut->db_status = 0;
    for (int i = 0; i < 8; ++i) tick();
    dut->resetn = 1;
    for (int i = 0; i < 4; ++i) tick();

    // The host writes the memories directly -- no CU_DATA, no buf_id map.
    for (uint64_t a = 0; a < text_bytes; a += 4) {
        uint32_t w = 0;
        for (int j = 0; j < 4; ++j) w |= (uint32_t)img(a + j) << (8 * j);
        hwrite(H_IMEM | (uint32_t)a, w);
    }
    for (uint64_t a = 0; a < spad_bytes; a += 8) {
        uint64_t w = 0;
        for (int j = 0; j < 8; ++j) w |= (uint64_t)img(SPAD_BASE + a + j) << (8 * j);
        hwrite(H_SPAD | (uint32_t)a, w);
    }
    // Code linked into DRAM (.dram_text) goes straight into node memory, so the
    // I-cache fills it as it would real DRAM.
    uint64_t dram_bytes = 0;
    for (auto &kv : image) {
        if (kv.first >= 0x80000000ull) {
            uint64_t &w = node[kv.first >> 3];
            int b = (int)(kv.first & 7);
            uint64_t m = 0xffull << (b * 8);
            w = (w & ~m) | ((uint64_t)kv.second << (b * 8));
            ++dram_bytes;
        }
    }
    uint64_t load_cycles = cycles;
    if (dram_bytes) printf("  dram-text  %llu bytes into node memory\n",
                           (unsigned long long)dram_bytes);

    // Preload stdin before boot -- the queue survives reset. \n/\t interpreted,
    // trailing newline appended so a line reader terminates.
    if (have_stdin) {
        std::string in;
        for (size_t i = 0; i < stdin_input.size(); ++i) {
            char c = stdin_input[i];
            if (c == '\\' && i + 1 < stdin_input.size()) {
                char n = stdin_input[++i];
                c = (n == 'n') ? '\n' : (n == 't') ? '\t' : n;
            }
            in.push_back(c);
        }
        if (in.empty() || in.back() != '\n') in.push_back('\n');
        for (unsigned char c : in) hwrite(H_CTRL | HR_STDIN, c);
        printf("  stdin      %zu bytes preloaded\n", (size_t)in.size());
    }

    hwrite(H_CTRL | HR_PC, entry_pc);
#if VM_TRACE
    trace_on = true;   // start the VCD at boot; the load phase is uninteresting
#endif
    hwrite(H_CTRL | HR_BOOT, 1);

    uint64_t run0 = cycles, st = 0;
    while (cycles < max_cycles) {
        for (int i = 0; i < 256; ++i) tick();
        st = hread(H_CTRL | HR_STATUS);
        if (st & 0xc) break;                   // exited or halted
    }

    uint64_t exit_word = hread(H_CTRL | HR_EXIT);
    uint64_t halt_pc = hread(H_CTRL | HR_HALTPC);
    if (st & 0x4)
        printf("  HALTED     cause %llu at pc %llx\n",
               (unsigned long long)(st & 3), (unsigned long long)halt_pc);
    printf("\n");
    if (!console.empty()) printf("  console    %s", console.c_str());
    printf("  load       %llu cycles (host slave port)\n",
           (unsigned long long)load_cycles);
    printf("  run        %llu cycles\n", (unsigned long long)(cycles - run0));
    printf("  core       %llu cycles, %llu retired\n",
           (unsigned long long)dut->dbg_cycles, (unsigned long long)dut->dbg_retired);
    if (dut->dbg_cycles)
        printf("  IPC        %.4f\n",
               (double)dut->dbg_retired / (double)dut->dbg_cycles);
    printf("  node       %llu reads, %llu writes\n",
           (unsigned long long)node_reads, (unsigned long long)node_writes);
    printf("  mover      %llu cfg writes    doorbell %llu\n",
           (unsigned long long)mv_writes, (unsigned long long)db_writes);
    printf("  dram[0..7] ");
    for (int i = 0; i < 8; ++i) {
        auto it = node.find((0x80000000ull >> 3) + i);
        printf("%llu ", (unsigned long long)(it == node.end() ? 0 : it->second));
    }
    printf("\n  expect     7 16777222 33554437 50331652 67108867 ...\n");
    printf("  exit       %llu\n", (unsigned long long)exit_word);
    printf("  dispatches %llu  last dst %llu arg0 %llx\n",
           (unsigned long long)disp_seen, (unsigned long long)last_disp_dst,
           (unsigned long long)last_disp_arg0);

    // NOT gated on node traffic. It was, to catch a run that never really
    // started -- but the program's own console string proves that better, and
    // a test of the mesh mailbox touches node memory zero times by design.
    bool ok = (exit_word == 0) && console.find(expect) != std::string::npos;
    printf("========================================\n");
    printf("  %s\n", ok ? "PASS" : "FAIL");
    printf("========================================\n");

#if VM_TRACE
    if (tfp) { tfp->close(); delete tfp; printf("  trace      trace.vcd\n"); }
#endif
    dut->final();
    delete dut;
    delete ctx;
    return ok ? 0 : 1;
}
