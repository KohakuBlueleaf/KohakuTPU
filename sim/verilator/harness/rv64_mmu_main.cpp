// A component bench for rv64_mmu: real Sv39 page tables in a behavioural
// memory, walked by the DUT, with every translation checked against the tables.
//
// This module had no bench, which is how a TLB read decode that disagrees with
// its own write decode survived: walk and tag compare are both correct.

#include "Vrv64_mmu.h"
#include "verilated.h"

#include <cstdint>
#include <cstdio>
#include <map>
#include <vector>

static VerilatedContext *ctx;
static Vrv64_mmu *dut;
static uint64_t cycles = 0;
static int failures = 0;

// ---- the memory the walker reads, by 8-byte word ---------------------------
static std::map<uint64_t, uint64_t> mem;

// Sv39 PTE bits. A leaf needs A, and a store needs D as well.
static const uint64_t PTE_V = 1u << 0, PTE_R = 1u << 1, PTE_W = 1u << 2;
static const uint64_t PTE_X = 1u << 3, PTE_U = 1u << 4;
static const uint64_t PTE_A = 1u << 6, PTE_D = 1u << 7;

static uint64_t pte(uint64_t ppn, uint64_t perm) { return (ppn << 10) | perm; }

// ---- table allocation ------------------------------------------------------
static uint64_t next_page = 0x20;                 // in 4 KB pages
static uint64_t alloc_page() { return next_page++; }

static uint64_t root_ppn;

// Map one 4 KB VA to one 4 KB PA, creating tables as needed.
static void map_page(uint64_t va, uint64_t pa, uint64_t perm) {
    uint64_t vpn[3] = {(va >> 12) & 0x1ff, (va >> 21) & 0x1ff, (va >> 30) & 0x1ff};
    uint64_t tbl = root_ppn;
    for (int lvl = 2; lvl > 0; --lvl) {
        uint64_t slot = (tbl << 9) | vpn[lvl];    // word index = (ppn<<12 | v*8)/8
        auto it = mem.find(slot);
        uint64_t next;
        if (it == mem.end() || !(it->second & PTE_V)) {
            next = alloc_page();
            mem[slot] = pte(next, PTE_V);          // pointer: V only, no R/W/X
        } else {
            next = it->second >> 10;
        }
        tbl = next;
    }
    mem[(tbl << 9) | vpn[0]] = pte(pa >> 12, perm | PTE_V);
}

// A leaf at level 1 covers 2 MB; `pa` must be 2 MB aligned or the walk faults.
static void map_2m(uint64_t va, uint64_t pa, uint64_t perm) {
    uint64_t vpn[3] = {(va >> 12) & 0x1ff, (va >> 21) & 0x1ff, (va >> 30) & 0x1ff};
    uint64_t slot = (root_ppn << 9) | vpn[2];
    auto it = mem.find(slot);
    uint64_t l1;
    if (it == mem.end() || !(it->second & PTE_V)) {
        l1 = alloc_page();
        mem[slot] = pte(l1, PTE_V);
    } else {
        l1 = it->second >> 10;
    }
    mem[(l1 << 9) | vpn[1]] = pte(pa >> 12, perm | PTE_V);
}

// ---- clocking --------------------------------------------------------------
static int w_delay = -1;
static uint64_t w_word = 0;
static const int WALK_LATENCY = 3;

static uint64_t pte_reads = 0;
static bool w_prev = false;

static void serve_walker() {
    dut->w_ack = 0;
    if (dut->w_req && !w_prev) ++pte_reads;
    w_prev = dut->w_req;
    if (dut->w_req && w_delay < 0) {
        auto it = mem.find(dut->w_addr >> 3);
        w_word = (it == mem.end()) ? 0 : it->second;
        w_delay = WALK_LATENCY;
    }
    if (w_delay == 0) {
        dut->w_data = w_word;
        dut->w_ack = 1;
        w_delay = -1;
    } else if (w_delay > 0) {
        --w_delay;
    }
}

static void tick() {
    dut->clk = 0; dut->eval();
    serve_walker();
    dut->eval();
    dut->clk = 1; dut->eval();
    ++cycles;
    ctx->timeInc(1);
}

// Hold req until the MMU stops asking for time, then read what it produced.
// Returns false if it faulted.
static bool translate(uint64_t va, bool store, bool fetch, uint64_t *pa_out) {
    dut->req = 1;
    dut->va = va;
    dut->is_store = store;
    dut->is_fetch = fetch;
    bool faulted = false;
    bool settled = false;
    uint64_t pa = 0;
    // `fault` is registered and lands the cycle AFTER `busy` falls, so a
    // consumer that samples on !busy alone never sees a permission fault.
    for (int i = 0; i < 400 && !faulted; ++i) {
        tick();
        if (dut->fault) faulted = true;
        if (!dut->busy && !settled) { pa = dut->pa; settled = true; }
        if (settled) { tick(); if (dut->fault) faulted = true; break; }
    }
    dut->req = 0;
    tick();
    if (faulted) return false;
    *pa_out = pa;
    return settled;
}

static void expect_pa(const char *what, uint64_t va, uint64_t want) {
    uint64_t got = 0;
    uint64_t r0 = pte_reads;
    bool ok = translate(va, false, false, &got);
    uint64_t used = pte_reads - r0;
    if (!ok) {
        printf("FAIL %-22s va=%#llx faulted, expected pa=%#llx\n", what,
               (unsigned long long)va, (unsigned long long)want);
        ++failures;
    } else if (got != want) {
        printf("FAIL %-22s va=%#llx  pa=%#llx  want %#llx  (delta %+lld pages)\n",
               what, (unsigned long long)va, (unsigned long long)got,
               (unsigned long long)want,
               (long long)((int64_t)(got >> 12) - (int64_t)(want >> 12)));
        ++failures;
    } else {
        printf("ok   %-22s va=%#llx -> pa=%#llx  idx=%2llu  %llu PTE read(s)\n",
               what, (unsigned long long)va, (unsigned long long)got,
               (unsigned long long)((va >> 12) & 31), (unsigned long long)used);
    }
}

static void expect_fault(const char *what, uint64_t va, bool store) {
    uint64_t got = 0;
    bool ok = translate(va, store, false, &got);
    if (ok) {
        printf("FAIL %-22s va=%#llx did NOT fault, gave pa=%#llx\n", what,
               (unsigned long long)va, (unsigned long long)got);
        ++failures;
    } else {
        printf("ok   %-22s va=%#llx faulted as expected\n", what,
               (unsigned long long)va);
    }
}

int main(int argc, char **argv) {
    ctx = new VerilatedContext;
    ctx->commandArgs(argc, argv);
    dut = new Vrv64_mmu{ctx};

    root_ppn = alloc_page();

    // A spread of VAs so a wrong slice of the entry shows up as a wrong page
    // rather than as a coincidence: the low bits of the PPN must matter.
    struct { uint64_t va, pa; } maps[] = {
        {0x0000'1000ULL, 0x0000'4000ULL},
        {0x0000'2000ULL, 0x0000'5000ULL},
        {0x0040'0000ULL, 0x0001'F000ULL},
        {0x8000'0000ULL, 0x0002'3000ULL},
        {0x0000'3000ULL, 0x00AB'C000ULL},   // PPN with bits set low AND high
        {0x0000'4000ULL, 0x0000'1000ULL},   // PPN whose low 5 bits are non-zero
    };
    for (auto &m : maps) map_page(m.va, m.pa, PTE_R | PTE_W | PTE_A | PTE_D);

    uint64_t ro_va = 0x0000'8000ULL, ro_pa = 0x0003'1000ULL;
    map_page(ro_va, ro_pa, PTE_R | PTE_A);          // no W, no D

    // ---- bring up ----------------------------------------------------------
    dut->resetn = 0;
    dut->req = 0; dut->is_store = 0; dut->is_fetch = 0;
    dut->sfence = 0; dut->sum = 0; dut->mxr = 0;
    dut->w_ack = 0; dut->w_data = 0;
    dut->satp = ((uint64_t)8 << 60) | root_ppn;     // MODE = Sv39
    dut->priv = 1;                                  // supervisor: translation on
    for (int i = 0; i < 8; ++i) tick();
    dut->resetn = 1;
    for (int i = 0; i < 128; ++i) tick();           // the power-on TLB sweep

    printf("-- cold walks (every one a TLB miss) --\n");
    for (auto &m : maps) expect_pa("cold", m.va, m.pa);

    printf("-- warm, served from the TLB --\n");
    // Two of these VPNs collide in a 32-entry direct-mapped array, so a warm
    // pass legitimately re-walks those; the rest must cost no PTE read at all.
    uint64_t walks_before = pte_reads;
    for (auto &m : maps) expect_pa("warm", m.va, m.pa);
    uint64_t warm_walks = pte_reads - walks_before;
    printf("     %llu PTE reads on the warm pass (%zu translations)\n",
           (unsigned long long)warm_walks, sizeof(maps) / sizeof(maps[0]));
    if (warm_walks == 0) {
        printf("ok   every warm translation came from the TLB\n");
    } else if (warm_walks <= 6) {
        printf("ok   only the index collision re-walked (%llu reads)\n",
               (unsigned long long)warm_walks);
    } else {
        printf("FAIL the TLB is not retaining entries: %llu PTE reads\n",
               (unsigned long long)warm_walks);
        ++failures;
    }

    printf("-- permissions --\n");
    expect_pa("read of a RO page", ro_va, ro_pa);
    expect_fault("store to a RO page", ro_va, true);
    expect_fault("unmapped page", 0x7000'0000ULL, false);

    printf("-- superpages: a 2 MB leaf at level 1 --\n");
    // Two slices of one 2 MB mapping, far apart inside it, so taking the PTE's
    // PPN unmodified would send both to the superpage's first page.
    uint64_t sp_va = 0x0060'0000ULL, sp_pa = 0x0140'0000ULL;
    map_2m(sp_va, sp_pa, PTE_R | PTE_W | PTE_A | PTE_D);
    expect_pa("2M slice 0", sp_va, sp_pa);
    expect_pa("2M slice 1", sp_va + 0x1000, sp_pa + 0x1000);
    expect_pa("2M slice 300", sp_va + 0x12C000, sp_pa + 0x12C000);

    // A 2 MB leaf whose PPN is not 2 MB aligned is a malformed table.
    uint64_t bad_va = 0x0080'0000ULL;
    map_2m(bad_va, 0x0140'1000ULL, PTE_R | PTE_W | PTE_A | PTE_D);
    expect_fault("misaligned 2M leaf", bad_va, false);

    printf("-- machine mode does not translate --\n");
    dut->priv = 3;
    uint64_t got = 0;
    if (translate(0x0000'1000ULL, false, false, &got) && got == 0x0000'1000ULL)
        printf("ok   machine mode passes the address through\n");
    else {
        printf("FAIL machine mode gave %#llx\n", (unsigned long long)got);
        ++failures;
    }
    dut->priv = 1;

    // ---- one MMU, two requesters ------------------------------------------
    // The wrapper switches the port from a fetch to a data access the cycle
    // one arrives, mid-walk if need be. The walk must finish for the address
    // it started with, the newcomer must not ride the old request's hit, and
    // a fault must stay with its owner.
    printf("-- shared port: a data access pre-empts a fetch walk --\n");
    uint64_t f_va = 0x0000'C000ULL, f_pa = 0x0004'2000ULL;   // vpn1 = 0
    uint64_t d_va = 0x00A0'3000ULL, d_pa = 0x0005'7000ULL;   // vpn1 = 5
    map_page(f_va, f_pa, PTE_R | PTE_X | PTE_A);
    map_page(d_va, d_pa, PTE_R | PTE_W | PTE_A | PTE_D);
    dut->sfence = 1; tick(); dut->sfence = 0;
    for (int i = 0; i < 40; ++i) tick();                    // the sweep
    dut->req = 1; dut->va = f_va; dut->is_fetch = 1; dut->is_store = 0;
    for (int i = 0; i < 6; ++i) tick();                     // level-2 PTE in flight
    if (!dut->w_req && dut->busy) {
        // not yet asking for a PTE: give it a moment more
        for (int i = 0; i < 4 && !dut->w_req; ++i) tick();
    }
    dut->va = d_va; dut->is_fetch = 0;                      // data takes the port
    {
        bool settled = false; uint64_t pa = 0;
        for (int i = 0; i < 400 && !settled; ++i) {
            tick();
            if (dut->fault) {
                printf("FAIL pre-empting data access faulted\n"); ++failures; break;
            }
            if (!dut->busy) { pa = dut->pa; settled = true; }
        }
        if (settled && pa == d_pa)
            printf("ok   data access translated across the switch -> %#llx\n",
                   (unsigned long long)pa);
        else if (settled) {
            printf("FAIL data access got pa=%#llx, want %#llx\n",
                   (unsigned long long)pa, (unsigned long long)d_pa);
            ++failures;
        }
    }
    dut->req = 0; tick(); tick();
    {
        // The pre-empted fetch walk must have installed ITS address, not a
        // hybrid: served now from the TLB, with no PTE read.
        uint64_t r0 = pte_reads, pa = 0;
        bool ok = translate(f_va, false, true, &pa);
        if (ok && pa == f_pa && pte_reads == r0)
            printf("ok   the pre-empted fetch walk installed its own entry\n");
        else if (ok && pa == f_pa)
            printf("ok   fetch re-walked and translated (%llu reads)\n",
                   (unsigned long long)(pte_reads - r0));
        else {
            printf("FAIL fetch after pre-emption: ok=%d pa=%#llx want %#llx\n",
                   ok, (unsigned long long)pa, (unsigned long long)f_pa);
            ++failures;
        }
    }

    printf("-- shared port: a newcomer must not ride the old hit --\n");
    // Warm f_va (hits), then present a cold VA on the very next cycle: `busy`
    // must not drop on the previous request's resolution.
    uint64_t c_va = 0x00B0'5000ULL, c_pa = 0x0006'1000ULL;
    map_page(c_va, c_pa, PTE_R | PTE_W | PTE_A | PTE_D);
    dut->req = 1; dut->va = f_va; dut->is_fetch = 1; dut->is_store = 0;
    tick(); tick();                                         // hit resolves
    dut->va = c_va; dut->is_fetch = 0;
    {
        bool early = false, settled = false; uint64_t pa = 0;
        for (int i = 0; i < 400 && !settled; ++i) {
            tick();
            if (!dut->busy) {
                pa = dut->pa; settled = true;
                if (i < 2) early = true;
            }
        }
        if (settled && !early && pa == c_pa)
            printf("ok   cold request waited for its own walk -> %#llx\n",
                   (unsigned long long)pa);
        else {
            printf("FAIL newcomer: early=%d pa=%#llx want %#llx\n", early,
                   (unsigned long long)pa, (unsigned long long)c_pa);
            ++failures;
        }
    }
    dut->req = 0; tick(); tick();

    printf("-- shared port: a fetch fault stays with the fetch --\n");
    dut->req = 1; dut->va = 0x7100'0000ULL; dut->is_fetch = 1; dut->is_store = 0;
    {
        bool seen = false;
        for (int i = 0; i < 400 && !seen; ++i) { tick(); seen = dut->fault; }
        if (seen && dut->fault_fetch && dut->cause == 12)
            printf("ok   fetch fault: fault_fetch=1 cause=12\n");
        else {
            printf("FAIL fetch fault: seen=%d fault_fetch=%d cause=%d\n", seen,
                   dut->fault_fetch, dut->cause);
            ++failures;
        }
    }
    // The fault is latched for the fetch; a data request arriving now must
    // not be released by it and must clear it.
    dut->va = d_va; dut->is_fetch = 0;
    tick();
    if (dut->fault) {
        printf("FAIL fetch fault still latched against a data request\n");
        ++failures;
    } else {
        printf("ok   data request displaced the fetch fault\n");
    }
    {
        bool settled = false; uint64_t pa = 0;
        for (int i = 0; i < 400 && !settled; ++i) {
            tick();
            if (!dut->busy) { pa = dut->pa; settled = true; }
        }
        if (settled && pa == d_pa)
            printf("ok   data access after a fetch fault -> %#llx\n",
                   (unsigned long long)pa);
        else {
            printf("FAIL data access after fetch fault: pa=%#llx\n",
                   (unsigned long long)pa);
            ++failures;
        }
    }
    dut->req = 0; tick(); tick();

    printf("\n%s  %d failure(s), %llu cycles\n",
           failures ? "FAILED" : "PASSED", failures,
           (unsigned long long)cycles);
    dut->final();
    delete dut;
    delete ctx;
    return failures ? 1 : 0;
}
