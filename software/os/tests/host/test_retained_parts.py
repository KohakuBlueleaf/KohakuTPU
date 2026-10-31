"""Native C witnesses and mutation checks for KDS coherence, DE stream safety, and C-Python ABI parity."""

import pathlib
import re
import shutil
import struct
import subprocess

from kohakutpu.application.runtime import osabi as A
from kohakutpu.application.runtime.osgraph import Task

OS_DIR = pathlib.Path(__file__).resolve().parents[2]
FW_INC = OS_DIR.parent / "firmware/kohakuaccel/include"
GRAPH = OS_DIR / "dispatch/graph.c"
SERVER = OS_DIR / "dispatch/server.c"
UNITS = OS_DIR / "kernel/units.c"
MEM = OS_DIR / "kernel/mem.c"


def function(source, signature):
    start = source.index(signature)
    opening = source.index("{", start)
    depth = 1
    end = opening + 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start:end]


def compile_and_run(tmp_path, name, code, includes=(), defines=()):
    path = tmp_path / f"{name}.c"
    exe = tmp_path / name
    path.write_text(code, encoding="utf-8")
    inc_flags = [f"-I{p}" for p in includes]
    def_flags = [f"-D{d}" for d in defines]
    subprocess.run(
        [
            "cc",
            "-std=c11",
            "-Wall",
            "-Werror",
            *def_flags,
            *inc_flags,
            str(path),
            "-o",
            str(exe),
        ],
        check=True,
    )
    return subprocess.run([str(exe)], capture_output=True, check=False)


def run_mutants(tmp_path, header, witness, real, mutants, includes=(), defines=()):
    """The real code passes the witness; each mutant (name, body, defines,
    tag) changes it and fails at the check `tag` names: a CHECK tag or a
    failed assert expression, read from stderr."""
    res = compile_and_run(tmp_path, "real", header + real + witness, includes, defines)
    assert res.returncode == 0, res.stderr
    for name, body, extra, tag in mutants:
        assert body != real or extra, name
        res = compile_and_run(
            tmp_path, name, header + body + witness, includes, (*defines, *extra)
        )
        assert res.returncode != 0 and tag in res.stderr.decode(), (
            name,
            tag,
            res.stderr,
        )


def test_cached_part_is_inactive_and_reuse_resets_dependencies(tmp_path):
    source = GRAPH.read_text(encoding="utf-8")
    pieces = "\n".join(
        function(source, s)
        for s in (
            "static struct kds_part *find(",
            "static void push_ready(",
            "int kds_part_reuse(",
            "static void release_part(",
        )
    )
    header = """
#include <stdint.h>
#include <assert.h>
#define KDS_GRAPHS 4
enum {LG_FREE, LG_LOADING, LG_LOADED, LG_RUN, LG_ENDED, LG_CACHED};
struct kds_task {unsigned npred;};
struct kds_part {unsigned state, gid, n, done, rhead, rtail, inflight;
 uint64_t busy, mem; int status; const struct kds_task *source;
 struct kds_task *t; uint16_t *remain, *ready;};
struct kds_node {struct kds_part part[4]; const struct kds_task *retained; int cpu_dirty, hw_dirty;};
static int frees;
static void kds_arena_free(struct kds_node *k, uint64_t ref) {(void)k; (void)ref; ++frees;}
static void kds_trace(unsigned p, uint64_t a, uint64_t b) {(void)p; (void)a; (void)b;}
"""
    witness = """
int main(void) {
 struct kds_task tasks[2] = {{0}, {1}};
 uint16_t remain[2] = {9,9}, ready[3] = {9,9,9};
 struct kds_node k = {0}; k.retained = tasks;
 struct kds_part *p = &k.part[0];
 *p = (struct kds_part){.state=LG_ENDED,.gid=11,.n=2,.done=2,.rhead=2,.rtail=2,
  .busy=100,.source=tasks,.t=tasks,.remain=remain,.ready=ready};
 release_part(&k,p);
 assert(p->state == LG_CACHED && frees == 0);
 assert(find(&k,11) == 0);
 assert(kds_part_reuse(&k,12,tasks,2) == 1);
 assert(p->state == LG_LOADED && p->gid == 12 && p->done == 0 && p->busy == 0);
 assert(remain[0] == 0 && remain[1] == 1 && p->rhead == 0 && p->rtail == 1 && ready[0] == 0);
 assert(k.cpu_dirty == 1 && k.hw_dirty == 1 && find(&k,12) == p);
 p->state = LG_ENDED; p->status = -1; p->inflight = 1;
 release_part(&k,p); assert(frees == 0);
 p->inflight = 0; release_part(&k,p); assert(frees == 1 && p->state == LG_FREE);
 return 0;
}
"""
    mutants = [
        (
            "mutant_cached_found",
            pieces.replace("k->part[i].state != LG_CACHED && ", ""),
            (),
            "find(&k,11) == 0",
        ),
    ]
    run_mutants(tmp_path, header, witness, pieces, mutants)


def test_answer_skips_acquire_when_hw_clean_and_mutant_fails(tmp_path):
    source = SERVER.read_text(encoding="utf-8")
    ans = function(source, "static void answer(")
    header = """
#include <stdint.h>
#include <assert.h>
#define KDS_F_UNCACHED 2u
enum {HG_FREE, HG_RUN};
struct kds_home {unsigned state, client_node, reply, parts, reported, failed;
 int64_t status, detail; uint64_t t0; int answered;};
struct kds_node {unsigned flags; int hw_dirty, cpu_dirty;};
static int acquires, releases;
static void kos_acquire(void) { ++acquires; }
static void kos_release(void) { ++releases; }
static uint64_t kos_now(void) { return 100; }
static void reply(struct kds_node *k, unsigned n, unsigned c, uint64_t p0,
 uint64_t p1, uint64_t p2, uint64_t p3) {(void)k;(void)n;(void)c;(void)p0;(void)p1;(void)p2;(void)p3;}
"""
    witness = """
int main(void) {
 struct kds_node k = {.flags=0, .hw_dirty=0, .cpu_dirty=0};
 struct kds_home h = {.state=HG_RUN, .parts=1, .reported=1};
 answer(&k, &h);
 assert(acquires == 0 && releases == 0 && h.state == HG_FREE);
 k.hw_dirty = 1;
 h = (struct kds_home){.state=HG_RUN, .parts=1, .reported=1};
 answer(&k, &h);
 assert(acquires == 1 && releases == 0 && k.hw_dirty == 0);
 k.cpu_dirty = 1;
 h = (struct kds_home){.state=HG_RUN, .parts=1, .reported=1};
 answer(&k, &h);
 assert(acquires == 1 && releases == 1 && k.cpu_dirty == 0);
 return 0;
}
"""
    mutants = [
        (
            "mutant_ans_always",
            ans.replace("(k->hw_dirty || k->cpu_dirty)", "1"),
            (),
            "acquires == 0 && releases == 0 && h.state == HG_FREE",
        ),
        (
            "mutant_ans_nocpu",
            ans.replace(" || k->cpu_dirty", ""),
            (),
            "acquires == 1 && releases == 1 && k.cpu_dirty == 0",
        ),
    ]
    run_mutants(tmp_path, header, witness, ans, mutants)


def test_b1_b2_b3_coherence_invariants_and_mutants(tmp_path):
    g_src = GRAPH.read_text(encoding="utf-8")
    s_src = SERVER.read_text(encoding="utf-8")
    pieces = "\n".join(
        [
            function(g_src, "static struct kds_part *find("),
            function(g_src, "static void push_ready("),
            function(g_src, "int kds_part_reuse("),
            function(g_src, "void kds_part_go("),
            function(g_src, "static void succ_done("),
            function(g_src, "static void release_part("),
            function(g_src, "static void end_part("),
            function(g_src, "static void complete("),
            function(g_src, "static void fail("),
            function(g_src, "static void put_bits("),
            function(g_src, "static int cached("),
            function(g_src, "static void acquire("),
            function(g_src, "static int64_t run_move("),
            function(g_src, "int kds_parts_run("),
            function(s_src, "static struct kds_home *home_of("),
            function(s_src, "static void answer("),
            function(s_src, "void kds_home_ndone("),
            function(s_src, "static void submit("),
        ]
    )
    header = """
#include <stdint.h>
#include <string.h>
#include <assert.h>
#define KDS_GRAPHS 4
#define KDS_HOMES 4
#define KDS_MAX_NODES 4
#define KDS_MAX_UNITS 32
#define KDS_MAXPRED 4
#define KDS_MAXPATCH 3
#define KDS_CHAN 0x4B445300u
#define KDS_MUT 0
#define KDS_F_UNCACHED 2u
#define KOS_OK 0
#define KOS_EINVAL (-1)
#define KOS_EAGAIN (-6)
#define KOS_EFAULT (-7)
#define KOS_ETIME (-8)
#define KOS_NOWAIT (~0UL)
#define KOS_LAND_SIZE 0x10000UL
#define KOS_MV_LAND(n) (0x100u | (n))
#define KDS_REF_NODE(r) ((unsigned)((r) >> 56))
#define KDS_REF_OFF(r) ((r) & ((1UL << 56) - 1))
#define NONE 0xffffu
typedef uint64_t kds_ref;
enum {KDS_NOP, KDS_UNIT, KDS_MOVE, KDS_CPU, KDS_STREAM, KDS_STAMP, KDS_DE_STREAM, KDS_LAUNCH_MARK};
enum {LG_FREE, LG_LOADING, LG_LOADED, LG_RUN, LG_ENDED, LG_CACHED};
enum {HG_FREE, HG_QUEUED, HG_LOADING, HG_RUN};
enum {KM_NDONE = 6};
struct kds_patch {kds_ref ref; uint16_t lo; uint8_t width, shift; uint32_t pad;};
struct kds_task {uint8_t kind, node, npred, npatch; uint16_t unit, succ_nodes; uint16_t pred[4]; uint64_t a[4]; struct kds_patch patch[3]; uint32_t id, pad32; uint64_t pad[3];};
struct kds_part {uint32_t gid, state; unsigned home; const struct kds_task *source; uint32_t n, loaded, done, inflight; int64_t status; uint32_t failed; int64_t detail; uint64_t busy, t0; kds_ref mem; struct kds_task *t; uint16_t *remain, *next; uint64_t *ticket; uint32_t *pairs, npairs; uint16_t *ready; uint32_t rhead, rtail; uint16_t phead[32], ptail[32];};
struct kds_home {uint32_t gid, state; struct kds_task *t; uint32_t n; unsigned client_node, reply; uint64_t t0, deadline; uint32_t parts, contacted, acked, reported, aborted; uint32_t count[4], sent[4], scan[4]; int64_t status; uint32_t failed; int64_t detail; int answered;};
struct kds_msg {uint8_t type, flag; uint16_t cnt; uint32_t gid; uint64_t p[4];};
struct kds_node {unsigned me, n, flags, mask, nunits; uint64_t spansz[4]; uint64_t counts[64], seen_faults[32]; unsigned fcount[32]; int cpu_dirty, hw_dirty, any_ready, any_pending, loading; uint32_t seq; const struct kds_task *retained; struct kds_part part[4]; struct kds_home home[4]; struct {uint64_t tasks, unit_batches;} st;};
static uint64_t dram_word = 0x1111, l1_cache_word = 0x1111;
static int releases, acquires;
static void kos_release(void) { ++releases; dram_word = l1_cache_word; }
static void kos_acquire(void) { ++acquires; l1_cache_word = dram_word; }
static uint64_t kos_now(void) { return 100; }
static int kos_event(uint64_t t, uint64_t w, uint64_t *c, unsigned n, unsigned ch, uint64_t *now) {(void)t;(void)w;(void)c;(void)n;(void)ch;*now=0;return 0;}
static void kds_arena_free(struct kds_node *k, kds_ref m) {(void)k;(void)m;}
static void *kds_arena_va(struct kds_node *k, kds_ref r) {(void)k; return (void *)(uintptr_t)r;}
static uint64_t kds_resolve(struct kds_node *k, kds_ref r) {(void)k; return r;}
static int64_t kos_move(const void *s, uint64_t d, uint64_t w, unsigned f) {(void)s;(void)d;(void)w;(void)f;return 0;}
static uint64_t de_observed_dram;
static int64_t kos_de_run(const uint64_t *e, unsigned c) {(void)e;(void)c; de_observed_dram = dram_word; dram_word = de_observed_dram + 0x5000; return 0;}
static void kds_trace(unsigned p, uint64_t a, uint64_t b) {(void)p;(void)a;(void)b;}
static void kds_dep_flush(struct kds_node *k) {(void)k;}
static void kds_dep_add(struct kds_node *k, unsigned j, uint32_t g, uint16_t id) {(void)k;(void)j;(void)g;(void)id;}
static void kds_post(struct kds_node *k, unsigned n, unsigned c, const struct kds_msg *m) {(void)k;(void)n;(void)c;(void)m;}
static void reply(struct kds_node *k, unsigned n, unsigned c, uint64_t p0, uint64_t p1, uint64_t p2, uint64_t p3) {(void)k;(void)n;(void)c;(void)p0;(void)p1;(void)p2;(void)p3;}
static void home_fail(struct kds_node *k, struct kds_home *h, int64_t st, uint32_t f, int64_t d) {(void)k;(void)h;(void)st;(void)f;(void)d;}
static int64_t check(struct kds_node *k, struct kds_task *t, uint32_t n, uint32_t *bad) {(void)k;(void)t;(void)n;*bad=0;return 0;}
static void start_next(struct kds_node *k) {(void)k;}
void kds_home_ndone(struct kds_node *k, unsigned node, uint32_t gid, int64_t status, uint32_t failed, int64_t detail, uint64_t busy);
static uint64_t cpu_observed_l1;
static int64_t cpu_primer(const uint64_t *a) {(void)a; l1_cache_word = 0x2222; return 0;}
static int64_t cpu_reader(const uint64_t *a) {(void)a; cpu_observed_l1 = l1_cache_word; return 0;}
"""
    witness = """
int main(void) {
 /* B1 witness: KDS_CPU (acquires & clears hw_dirty, writes 0x2222) -> KDS_DE_STREAM -> KDS_CPU */
 struct kds_task tasks[3] = {
  {.kind = KDS_CPU, .node = 0, .npred = 0, .a = {(uint64_t)(uintptr_t)cpu_primer, 0, 0, 0}, .id = 0},
  {.kind = KDS_DE_STREAM, .node = 0, .npred = 1, .pred = {0}, .a = {0x1000, 3, 0, 0}, .id = 1},
  {.kind = KDS_CPU, .node = 0, .npred = 1, .pred = {1}, .a = {(uint64_t)(uintptr_t)cpu_reader, 0, 0, 0}, .id = 2},
 };
 uint16_t remain[3] = {0, 1, 1}, ready[4] = {0, 0, 0, 0};
 uint32_t pairs[2] = {(0u << 16) | 1u, (1u << 16) | 2u};
 struct kds_node k = {.me = 0, .n = 2, .nunits = 2, .loading = -1, .retained = tasks};
 k.part[0] = (struct kds_part){
  .state = LG_CACHED, .home = 0, .source = tasks, .n = 3,
  .t = tasks, .remain = remain, .pairs = pairs, .npairs = 2, .ready = ready
 };
 dram_word = 0x1111; l1_cache_word = 0x1111;
 struct kds_msg m = {.gid = 1, .p = {(uint64_t)(uintptr_t)tasks, 3, 0, 0}};
 submit(&k, 0, &m);
 kds_parts_run(&k);
 assert(de_observed_dram == 0x2222);
 assert(cpu_observed_l1 == 0x7222);

 /* B3 witness: 1-task retained KDS_DE_STREAM flushes CPU L1 writes made between runs */
 struct kds_task de_only[1] = {
  {.kind = KDS_DE_STREAM, .node = 0, .npred = 0, .a = {0x1000, 3, 0, 0}, .id = 0},
 };
 uint16_t rem1[1] = {0}, rdy1[2] = {0, 0};
 k.retained = de_only;
 k.part[1] = (struct kds_part){
  .state = LG_CACHED, .home = 0, .source = de_only, .n = 1,
  .t = de_only, .remain = rem1, .ready = rdy1
 };
 dram_word = 0x3333; l1_cache_word = 0x4444;
 struct kds_msg m2 = {.gid = 1, .p = {(uint64_t)(uintptr_t)de_only, 1, 0, 0}};
 submit(&k, 0, &m2);
 kds_parts_run(&k);
 assert(de_observed_dram == 0x4444);

 /* B2 witness: remote node 1 reports KM_NDONE without KM_DEP -> home acquires before answering */
 k.hw_dirty = 0; acquires = 0;
 k.home[2] = (struct kds_home){.gid = 99, .state = HG_RUN, .parts = 3, .reported = 1};
 kds_home_ndone(&k, 1, 99, KOS_OK, 0, 0, 0);
 assert(acquires == 1);
 return 0;
}
"""
    mutants = [
        (
            "mutant_b1",
            pieces.replace(
                "int64_t r = kos_de_run(kds_arena_va(k, t->a[0]), (unsigned)t->a[1]);\n                k->hw_dirty = 1;",
                "int64_t r = kos_de_run(kds_arena_va(k, t->a[0]), (unsigned)t->a[1]);",
            ),
            (),
            "cpu_observed_l1 == 0x7222",
        ),
        (
            "mutant_b2",
            pieces.replace("if (node != k->me) {\n        k->hw_dirty = 1;\n    }", ""),
            (),
            "acquires == 1",
        ),
        (
            "mutant_b3",
            pieces.replace(
                "k->cpu_dirty = k->hw_dirty = 1;",
                "k->hw_dirty = 1;",
                1,
            ),
            (),
            "de_observed_dram == 0x4444",
        ),
    ]
    run_mutants(tmp_path, header, witness, pieces, mutants)


def test_de_stream_safety_and_mutants(tmp_path):
    """k_de_stream, the mover shadow, k_move and k_dispatch_local against a
    simulated node. Each mutant must fail at the witness named for it."""
    u_src = UNITS.read_text(encoding="utf-8")
    m_src = MEM.read_text(encoding="utf-8")
    shadow = u_src[
        u_src.index("/* The mover's address registers") : u_src.index(
            "/* Wait until the mover is idle"
        )
    ]
    pieces = "\n".join(
        [
            function(m_src, "int k_pa_ok("),
            function(u_src, "static __attribute__((noinline)) int find("),
            function(u_src, "static __attribute__((noinline)) void mailbox("),
            function(u_src, "static __attribute__((noinline)) int engine_own("),
            function(u_src, "__attribute__((noinline)) int64_t k_dispatch_local("),
            shadow,
            function(u_src, "static UNIT_SMALL int64_t mover_finish("),
            function(u_src, "int64_t k_move("),
            function(u_src, "UNIT_SMALL void k_mover_clear("),
            function(u_src, "static UNIT_SMALL int64_t de_wait_q("),
            function(u_src, "static UNIT_SMALL void de_map_all("),
            function(u_src, "UNIT_SMALL int64_t k_de_stream("),
        ]
    )
    header = """
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <kos/abi.h>
#include <kos/boot.h>
#include <kos/hw.h>
#define NM(r) (HW_R_NM + 8 * (r))
#define RX(r) (HW_R_RX + 8 * (r))
#define MV(r) (HW_R_MV + (r))
#define MAX_FRAMES 8192
#define CHECK(tag, cond) do { if (!(cond)) { fprintf(stderr, "WITNESS %s\\n", tag); exit(1); } } while (0)
static uint64_t umap[MAX_FRAMES / 64];
static uint64_t nframes = 256;
struct knode {uint32_t flags; uint64_t priv, priv_size, shared, shared_size, land_off; int engine;} kn = {
 0, 0x20000000UL, 0x100000UL, 0, 0, 0xF0000UL, 1
};
struct kunit {struct kos_unit u; int local; uint64_t sent, done, faults, fault_arg;} kunits[32];
unsigned knunits = 2;
uint64_t k_events;
struct {uint64_t completions, faults, dispatches;} kstat;
static uint64_t retired[32];
static int de_mapped[32];
static uint32_t de_seen[32];
static int de_ready;
#define UNIT_SMALL
static uint64_t sim_cycles = 100, sim_de_wait = (0xDE01UL << 48), sim_mvstat = (1UL << 33), sim_nm_stat = 0;
static uint32_t sim_smap[256], sim_de_ctr[16], sim_issued, sim_queued;
static int poll_inject_inflight_retire, n1_fault_mode, stall_de_queue, sim_go_fault;
static int n1_u0_pending_cq;
static uint32_t sim_mem[0x20000 / 4]; /* 0x20000000.. : user frame 0x20001000, index copy 0x20010000 */
static volatile uint32_t *k_unc32(uint64_t pa) { return &sim_mem[(((pa & ~HW_UNC) - 0x20000000UL) / 4) % (sizeof sim_mem / 4)]; }
#define MV_INDEX_MAX 2048u
static uint64_t mv_ixbuf = 0x20010000UL;
static uint64_t sim_last_index;
static uint64_t sim_hw_cycles(void) { sim_cycles += 1000000; return sim_cycles; }
static uint64_t sim_hw_rd(unsigned off) {
 if (off == HW_R_DE_WAIT) return sim_de_wait | ((uint64_t)sim_issued << 16) | sim_queued;
 if (off >= HW_R_DE_CTR && off < HW_R_DE_CTR + 128) return sim_de_ctr[(off - HW_R_DE_CTR) / 8];
 if (off == HW_R_MVSTAT) return sim_mvstat;
 if (off == NM(HW_NM_STAT)) return sim_nm_stat;
 return 0;
}
static void sim_hw_wr(unsigned off, uint64_t v) {
 if (off == HW_R_DE_MAP) {
  unsigned xy = (unsigned)(v & 0xffff);
  sim_smap[xy & 0xff] = (uint32_t)(v >> 31);
 } else if (off == HW_R_DE_CTL) {
  if (v & 2) {
   sim_issued = 0; sim_queued = 0;
   memset(sim_de_ctr, 0, sizeof sim_de_ctr);
  }
  sim_de_wait = (0xDE01UL << 48) | ((v & 1) << 32);
 } else if (off >= HW_R_DE_Q && off <= HW_R_DE_WAIT) {
  if (off == HW_R_DE_Q + 8 * KOS_DE_MV_INDEX) sim_last_index = v;
  if (stall_de_queue) {
   sim_queued = 64u;
  } else if (n1_fault_mode) {
   /* Issue the first 4 entries (u0 DST+GO, u1 DST+GO), hold subsequent entries unissued in queue */
   ++sim_queued;
   if (sim_issued < 4) { ++sim_issued; --sim_queued; }
  } else {
   ++sim_issued;
   if (off == HW_R_DE_Q + 8 * KOS_DE_MB_GO) ++sim_de_ctr[0];
  }
 } else if (off == MV(0) && (v & KOS_MV_CTRL_GO)) {
  /* mm_mover.v: a GO replaces the latched fault with its own (sim_go_fault,
   * one GO's); a move that runs counts done */
  uint64_t done = (HW_MV_DONE(sim_mvstat) + !sim_go_fault) & 0x0fffffffu;
  sim_mvstat = (sim_mvstat & ~0xffffffffUL) | ((uint64_t)sim_go_fault << 28) | done;
  sim_go_fault = 0;
 }
}
#define hw_cycles sim_hw_cycles
#define hw_rd sim_hw_rd
#define hw_wr sim_hw_wr
static void k_units_poll(void) {
 if (poll_inject_inflight_retire) { retired[0] = kunits[0].sent; ++k_events; poll_inject_inflight_retire = 0; }
 if (de_ready) {
  for (unsigned i = 0; i < knunits; ++i) {
   if (!de_mapped[i]) continue;
   uint32_t c = sim_de_ctr[i] & 0xfff, d = (c - de_seen[i]) & 0xfff;
   de_seen[i] = c; kunits[i].done += d; retired[i] += d; k_events += d;
  }
 }
 if (n1_fault_mode == 1 && sim_issued == 4) {
  /* Unit 1 faults while unit 0 has 1 issued instruction still running in hardware */
  ++kunits[1].faults; ++kstat.faults; ++retired[1]; ++k_events;
  n1_u0_pending_cq = 1;
  n1_fault_mode = 2;
 } else if (!de_ready && n1_u0_pending_cq) {
  /* After DE is disabled, unit 0's in-flight instruction finishes via the completion queue */
  ++kunits[0].done; ++retired[0]; ++k_events;
  n1_u0_pending_cq = 0;
 }
}
"""
    witness = """
static uint64_t S[4 * 32];
static unsigned S_n;
static void s_reset(void) { memset(S, 0, sizeof S); S_n = 0; sim_last_index = 0; }
static void s_put(unsigned code, uint64_t val) {
 S[4 * (S_n / 3)] |= (uint64_t)code << (8 * (S_n % 3));
 S[4 * (S_n / 3) + 1 + S_n % 3] = val;
 ++S_n;
}
static int64_t s_run(void) { return k_de_stream(S, S_n); }
static void mv_hdr(unsigned w, uint64_t base, unsigned levels) {
 s_put(KOS_DE_MV_HDR, ((uint64_t)levels << 44) | (base << 4) | w);
}
static void mv_dim(unsigned w, unsigned level, uint64_t count, int32_t stride) {
 s_put(KOS_DE_MV_DIM, ((uint64_t)(uint32_t)stride << 20) | (count << 4) | (level << 1) | w);
}
static void mv_level(unsigned w, unsigned level, uint64_t count, int32_t stride) {
 mv_dim(w, level, count, stride);
 s_put(KOS_DE_MV_AXIS, 0);
}
static void mv_walk(unsigned w, uint64_t base, uint64_t words) { mv_hdr(w, base, 1); mv_level(w, 0, words, 32); }
static void mv_go(unsigned mode) { s_put(KOS_DE_MV_CTRL, KOS_MV_CTRL_GO | (1UL << 3) | mode); }
#define FRAME 0x20001000UL /* the one user frame; frames 0 and 2 are the kernel's */
#define IXT (FRAME + 0x400) /* the caller's index table */
static void mv_ix(uint64_t table, unsigned count) { s_put(KOS_DE_MV_INDEX, ((uint64_t)count << 40) | table); }
static void mv_pitch(uint32_t pitch, unsigned words) { s_put(KOS_DE_MV_BASE + (0x50 >> 3), ((uint64_t)words << 32) | pitch); }
static void ix_set(unsigned i, uint32_t v) { *k_unc32(IXT + 4 * i) = v; }
static uint32_t ix_copy(unsigned i) { return *k_unc32(mv_ixbuf + 4 * i); }
/* GATHER: rows from FRAME (source header, no levels) into a destination walk of
 * `elems` words with `stride` (0: every row onto one word) */
static int64_t gather_walk(unsigned elems, int32_t stride, unsigned count, uint32_t pitch, unsigned words, uint64_t flags) {
 s_reset(); mv_hdr(0, FRAME, 0); mv_hdr(1, FRAME + 0x800, 1); mv_level(1, 0, elems, stride);
 mv_ix(IXT, count); mv_pitch(pitch, words);
 s_put(KOS_DE_MV_CTRL, KOS_MV_CTRL_GO | (1UL << 3) | 2 | flags);
 return s_run();
}
static int64_t gather(unsigned count, uint32_t pitch, unsigned words, uint64_t flags) {
 return gather_walk(8, 32, count, pitch, words, flags);
}
#define LOADED(n) (sim_last_index == (mv_ixbuf | ((uint64_t)(n) << 40)))
#define FETCH_ARG2(xy) (((uint64_t)(xy) << KOS_DE_FETCH_COORD_SHIFT) | KOS_DE_FETCH_REPLY)
#define FETCH_ARG3(pa, n) (((uint64_t)(pa) << KOS_DE_FETCH_SHIFT) | KOS_DE_FETCH_TAG | (n))

int main(void) {
 /* k_pa_ok: the whole [pa, pa + bytes) in user frames, overflow-safe */
 umap[0] = 1UL << 1;
 CHECK("pa kernel frame", k_pa_ok(0x20000000UL, 32) == 0);
 CHECK("pa whole frame", k_pa_ok(FRAME, 4096) == 1);
 CHECK("pa one byte past", k_pa_ok(FRAME, 4097) == 0);
 CHECK("pa last word", k_pa_ok(FRAME + 0xfe0, 32) == 1);
 CHECK("pa straddle", k_pa_ok(FRAME + 0xfe0, 64) == 0);
 CHECK("pa wrap", k_pa_ok(FRAME, ~0UL) == 0);
 CHECK("pa landing zone", k_pa_ok(0x200F8000UL, 32) == 1);
 CHECK("pa outside", k_pa_ok(0x90000000UL, 32) == 0);

 /* B4: quiesce for unit 0's in-flight instruction, run, unmap every unit */
 kunits[0] = (struct kunit){.u = {.x = 1, .y = 1}, .local = 1, .sent = 5};
 kunits[1] = (struct kunit){.u = {.x = 2, .y = 1}, .local = 1, .sent = 2};
 retired[0] = 4; retired[1] = 2;
 poll_inject_inflight_retire = 1;
 uint64_t stream[4] = {(uint64_t)KOS_DE_MB_DST | ((uint64_t)KOS_DE_MB_GO << 8), 0x0101, 1, 0};
 CHECK("b4 run", k_de_stream(stream, 2) == KOS_OK);
 CHECK("b4 counts", kunits[0].sent == 6 && retired[0] == 6 && retired[1] == 2);
 CHECK("b4 unmapped", sim_smap[1] == 0 && sim_smap[2] == 0 && de_mapped[0] == 0 && de_mapped[1] == 0);

 /* N1 (reviews/ostpu/zn1_leak.py): u0 GO, u1 GO, 60 ARG2, then a u1 GO at room == 0 while u1 faults */
 uint64_t s65[4 * 22] = {0};
 unsigned codes[65];
 uint64_t vals[65];
 codes[0] = KOS_DE_MB_DST; vals[0] = 0x0101;
 codes[1] = KOS_DE_MB_GO;  vals[1] = 1;
 codes[2] = KOS_DE_MB_DST; vals[2] = 0x0102;
 codes[3] = KOS_DE_MB_GO;  vals[3] = 1;
 for (unsigned i = 4; i < 64; ++i) { codes[i] = KOS_DE_MB_ARG2; vals[i] = i; }
 codes[64] = KOS_DE_MB_GO; vals[64] = 1;
 for (unsigned i = 0; i < 65; ++i) {
  s65[4 * (i / 3)] |= (uint64_t)codes[i] << (8 * (i % 3));
  s65[4 * (i / 3) + 1 + i % 3] = vals[i];
 }
 n1_fault_mode = 1;
 CHECK("n1 room fault", k_de_stream(s65, 65) == KOS_EFAULT);
 n1_fault_mode = 0;
 k_units_poll();
 CHECK("n1 room u0", kunits[0].sent == 7 && retired[0] == 7);
 CHECK("n1 room u1", kunits[1].sent == 3 && retired[1] == 3);
 CHECK("n1 room next stream", k_de_stream(stream, 2) == KOS_OK);
 CHECK("n1 room next counts", kunits[0].sent == 8 && retired[0] == 8);
 /* a KDS ticket (k_dispatch_local's ++sent) after the fault is the next completion's,
  * so KDS retires it and releases its part (graph.c kds_units_retire) */
 uint64_t n1_w[4] = {0, 0, 0, 0};
 CHECK("n1 room next ticket", k_dispatch_local(1, n1_w) == (int64_t)retired[1] + 1);
 ++retired[1]; /* that instruction's completion */

 /* N1 + B5: two units in flight and an unissued u0 GO in the queue when u1 faults */
 n1_fault_mode = 1;
 uint64_t n1_stream[8] = {
  (uint64_t)KOS_DE_MB_DST | ((uint64_t)KOS_DE_MB_GO << 8) | ((uint64_t)KOS_DE_MB_DST << 16),
  0x0101, 1, 0x0102,
  (uint64_t)KOS_DE_MB_GO | ((uint64_t)KOS_DE_MB_DST << 8) | ((uint64_t)KOS_DE_MB_GO << 16),
  1, 0x0101, 1
 };
 CHECK("n1 rollback fault", k_de_stream(n1_stream, 6) == KOS_EFAULT);
 n1_fault_mode = 0;
 k_units_poll();
 CHECK("n1 rollback u0", kunits[0].sent == 9 && retired[0] == 9);
 CHECK("n1 rollback u1", kunits[1].sent == 5 && retired[1] == 5);

 /* B5: no progress for KOS_DE_TIMEOUT */
 stall_de_queue = 1;
 uint64_t big[4] = {KOS_DE_WAIT_CODE, (1UL << 56) | 1, 0, 0};
 CHECK("b5 timeout", k_de_stream(big, 1) == KOS_ETIME);
 stall_de_queue = 0;

 /* Waits: k_move and a DE stream end on a mover fault or after KOS_DE_TIMEOUT, then recover;
  * the poll clears a fault they returned (it holds the node's interrupt line) and moves nothing otherwise */
 sim_go_fault = 4;
 CHECK("move fault", k_move(FRAME, FRAME + 32, 1) == -4);
 CHECK("move fault latched", HW_MV_FAULT(sim_mvstat) == 4);
 k_mover_clear();
 CHECK("move fault cleared", HW_MV_FAULT(sim_mvstat) == 0);
 uint32_t clear_done = HW_MV_DONE(sim_mvstat);
 k_mover_clear();
 CHECK("clear moves nothing", HW_MV_DONE(sim_mvstat) == clear_done);
 sim_mvstat = (1UL << 33) | (1UL << 32);
 CHECK("move busy", k_move(FRAME, FRAME + 32, 1) == KOS_ETIME);
 sim_mvstat = (1UL << 33);
 CHECK("move after", k_move(FRAME, FRAME + 32, 1) == KOS_OK);
 s_reset(); mv_walk(0, FRAME, 1); mv_walk(1, FRAME + 32, 1); mv_go(0);
 sim_mvstat = (1UL << 33) | (4UL << 28);
 CHECK("de mover fault", s_run() == KOS_EFAULT);
 k_mover_clear();
 CHECK("de mover fault cleared", HW_MV_FAULT(sim_mvstat) == 0);
 sim_mvstat = (1UL << 33) | (1UL << 32);
 CHECK("de mover busy", s_run() == KOS_ETIME);
 sim_mvstat = (1UL << 33);
 CHECK("de mover after", s_run() == KOS_OK);
 kunits[0].sent = retired[0] + 16;
 uint64_t dummy_w[4] = {0, 0, 0, 0};
 CHECK("dispatch credit", k_dispatch_local(0, dummy_w) == KOS_ETIME);
 kunits[0].sent = retired[0];

 /* The mover shadow: every walk a GO starts lies in the caller's frames */
 s_reset(); mv_walk(0, FRAME, 4); mv_walk(1, FRAME + 0x800, 4); mv_go(0);
 CHECK("mv copy", s_run() == KOS_OK);
 s_reset(); mv_walk(0, FRAME, 2); mv_hdr(1, FRAME, 1); mv_level(1, 0, 2, -32); mv_go(0);
 CHECK("mv negative stride", s_run() == KOS_EFAULT);
 s_reset(); mv_walk(0, FRAME, 1); mv_hdr(1, FRAME, 2); mv_level(1, 0, 2, 2048); mv_level(1, 1, 65, 32); mv_go(0);
 CHECK("mv two levels", s_run() == KOS_EFAULT);
 s_reset(); mv_walk(0, FRAME, 1); mv_hdr(1, FRAME, 2); mv_level(1, 0, 2, 32); mv_go(0);
 CHECK("mv level not loaded", s_run() == KOS_EINVAL);
 s_reset(); mv_hdr(1, FRAME, 1); s_put(KOS_DE_MV_AXIS, 0); mv_go(4);
 CHECK("mv axis without dim", s_run() == KOS_EINVAL);
 s_reset(); mv_walk(0, FRAME, 1); mv_hdr(1, FRAME + 0x800, 1); mv_dim(0, 0, 65, 32);
 s_put(KOS_DE_MV_WINDOW, 1); s_put(KOS_DE_MV_AXIS, 0); mv_go(0);
 CHECK("mv window selects", s_run() == KOS_EFAULT);
 s_reset(); mv_walk(0, FRAME, 1); mv_hdr(1, FRAME, 1); mv_dim(1, 0, 3, 0x40000000); s_put(KOS_DE_MV_AXIS, 0); mv_go(0);
 CHECK("mv partial sum wraps", s_run() == KOS_EINVAL);

 /* GATHER/SCATTER: every row the index buffer can name lies in the caller's frames */
 for (unsigned i = 0; i < 8; ++i) ix_set(i, i);
 CHECK("gather", gather(8, 32, 1, 0) == KOS_OK);
 /* 8 rows load 8 entries, however long the caller's vector: entry 100 is never read */
 ix_set(100, 200);
 CHECK("gather loads rows", gather(2048, 32, 1, 0) == KOS_OK);
 ix_set(100, 0);
 CHECK("gather kernel copy", LOADED(8));
 CHECK("gather copied", ix_copy(7) == 7);
 ix_set(3, 200);
 CHECK("gather index past frame", gather(8, 32, 1, 0) == KOS_EFAULT);
 ix_set(3, 3);
 CHECK("gather row words fit", gather(8, 32, 121, 0) == KOS_OK);
 CHECK("gather row words", gather(8, 32, 122, 0) == KOS_EFAULT);
 CHECK("gather words zero", gather(8, 32, 0, 0) == KOS_EFAULT);
 CHECK("gather count", gather(2049, 32, 1, 0) == KOS_EINVAL);
 for (unsigned i = 0; i < 16; ++i) ix_set(i, i & 7);
 CHECK("gather sixteen", gather_walk(16, 32, 16, 32, 1, 0) == KOS_OK && LOADED(16) && ix_copy(15) == 7);
 /* 16 rows from an 8-index vector: rows 8..15 read zeros, not the caller's entry 8 */
 ix_set(8, 200);
 CHECK("gather pad", gather_walk(16, 32, 8, 32, 1, 0) == KOS_OK && LOADED(16) && ix_copy(8) == 0 && ix_copy(15) == 0);
 ix_set(8, 8);
 /* more rows than the index buffer: rows wrap onto its 2048 entries, all loaded */
 CHECK("gather 2049 rows", gather_walk(2049, 0, 8, 32, 1, 0) == KOS_OK && LOADED(2048) && ix_copy(2047) == 0);
 /* two levels, 2 x 8: 16 rows */
 s_reset(); mv_hdr(0, FRAME, 0); mv_hdr(1, FRAME + 0x800, 2);
 mv_level(1, 0, 2, 32); mv_level(1, 1, 8, 64);
 mv_ix(IXT, 8); mv_pitch(32, 1); mv_go(2);
 CHECK("gather two levels", s_run() == KOS_OK && LOADED(16));
 /* six levels of 32768 rows: 2^90, past 64 bits, still loads all 2048 */
 s_reset(); mv_hdr(0, FRAME, 0); mv_hdr(1, FRAME + 0x800, 6);
 for (unsigned l = 0; l < 6; ++l) mv_level(1, l, 32768, 0);
 mv_ix(IXT, 8); mv_pitch(32, 1); mv_go(2);
 CHECK("gather rows saturate", s_run() == KOS_OK && LOADED(2048));
 ix_set(5, 200);
 CHECK("gather 4096 rows past frame", gather_walk(4096, 0, 8, 32, 1, 0) == KOS_EFAULT);
 ix_set(5, 5);
 CHECK("gather two-word rows", gather_walk(16, 32, 8, 32, 2, 0) == KOS_OK && LOADED(8));
 ix_set(0, 0x80000000u);
 CHECK("gather wrap", gather(8, 2, 1, 0) == KOS_OK);
 ix_set(0, 0x40400000u); /* 3.0f */
 CHECK("gather float", gather(8, 32, 1, 1UL << 17) == KOS_OK);
 ix_set(0, 0x43480000u); /* 200.0f */
 CHECK("gather float past frame", gather(8, 32, 1, 1UL << 17) == KOS_EFAULT);
 ix_set(0, 0);
 s_reset(); mv_hdr(0, FRAME, 0); mv_walk(1, FRAME + 0x800, 8); mv_ix(0x90000000UL, 8); mv_pitch(32, 1); mv_go(2);
 CHECK("gather table outside", s_run() == KOS_EFAULT);
 s_reset(); mv_hdr(0, FRAME, 0); mv_walk(1, FRAME + 0x800, 8); mv_pitch(32, 1); mv_go(2);
 CHECK("gather no index", s_run() == KOS_EINVAL);
 s_reset(); mv_walk(1, FRAME + 0x800, 8); mv_ix(IXT, 8); mv_pitch(32, 1); mv_go(2);
 CHECK("gather no source header", s_run() == KOS_EINVAL);
 /* rows at FRAME (the destination header) + up to 3,232 bytes, not at the source walk's FRAME + 0x800 */
 ix_set(5, 100);
 s_reset(); mv_walk(0, FRAME + 0x800, 8); mv_hdr(1, FRAME, 0); mv_ix(IXT, 8); mv_pitch(32, 1); mv_go(6);
 CHECK("scatter", s_run() == KOS_OK);
 ix_set(5, 200);
 CHECK("scatter index past frame", s_run() == KOS_EFAULT);
 ix_set(5, 5);

 s_reset(); mv_walk(0, FRAME, 1); mv_hdr(1, FRAME + 0xfa0, 0); mv_go(0);
 CHECK("mv copy last word", s_run() == KOS_OK);
 s_reset(); mv_walk(0, FRAME, 1); mv_hdr(1, FRAME + 0xfa0, 0); mv_go(5);
 CHECK("mv xform entry", s_run() == KOS_EFAULT);
 s_reset(); mv_walk(1, FRAME, 4); mv_go(4);
 CHECK("mv fill", s_run() == KOS_OK);
 s_reset(); mv_walk(1, FRAME, 4); mv_go(0);
 CHECK("mv no source header", s_run() == KOS_EINVAL);
 s_reset(); mv_walk(0, 0x90000000UL, 1); mv_walk(1, FRAME, 1); mv_go(0);
 CHECK("mv source outside", s_run() == KOS_EFAULT);

 /* Instruction fetch: only the fetch's MEM_RD_REQ, its whole descriptor pinned */
 s_reset(); s_put(KOS_DE_MB_DST, KOS_DE_FETCH_TYPED | 0x0101); s_put(KOS_DE_MB_ARG2, FETCH_ARG2(0x11));
 s_put(KOS_DE_MB_ARG3, FETCH_ARG3(FRAME, 1)); s_put(KOS_DE_MB_GO, 1);
 CHECK("fetch", s_run() == KOS_OK);
 s_reset(); s_put(KOS_DE_MB_DST, KOS_DE_FETCH_TYPED | (1UL << 20) | 0x0101); s_put(KOS_DE_MB_ARG2, FETCH_ARG2(0x11));
 s_put(KOS_DE_MB_ARG3, FETCH_ARG3(FRAME, 1)); s_put(KOS_DE_MB_GO, 1);
 CHECK("dst write type", s_run() == KOS_EINVAL);
 s_reset(); s_put(KOS_DE_MB_DST, KOS_DE_FETCH_TYPED | 0x0101); s_put(KOS_DE_MB_ARG2, FETCH_ARG2(0x11) | (4UL << 30));
 s_put(KOS_DE_MB_ARG3, FETCH_ARG3(FRAME, 1)); s_put(KOS_DE_MB_GO, 1);
 CHECK("fetch entry words", s_run() == KOS_EINVAL);
 s_reset(); s_put(KOS_DE_MB_DST, KOS_DE_FETCH_TYPED | 0x0101); s_put(KOS_DE_MB_ARG2, FETCH_ARG2(0x11));
 s_put(KOS_DE_MB_ARG3, FETCH_ARG3(FRAME + 0xfe0, 2)); s_put(KOS_DE_MB_GO, 1);
 CHECK("fetch extent", s_run() == KOS_EFAULT);
 s_reset(); s_put(KOS_DE_IL_RING, 3UL << 8);
 CHECK("ring slot", s_run() == KOS_EINVAL);

 /* A GO before any DST names no mailbox state of this stream: refused, even with a unit at (0, 0) */
 kunits[2] = (struct kunit){.u = {.x = 0, .y = 0}, .local = 1};
 knunits = 3;
 s_reset(); s_put(KOS_DE_MB_GO, 1);
 CHECK("go before dst", s_run() == KOS_EINVAL);
 return 0;
}
"""
    mutants = [
        ("mutant_n1_leak_before_room", pieces, ("KOS_N1_MUT=3",), "n1 room fault"),
        ("mutant_n1_fabricate", pieces, ("KOS_N1_MUT=1",), "n1 room u0"),
        ("mutant_n1_no_rollback", pieces, ("KOS_N1_MUT=2",), "n1 rollback fault"),
        (
            "mutant_b4_nounmap",
            pieces.replace(
                "hw_wr(HW_R_DE_MAP, valid | ((uint64_t)u << 16) |\n                  ((uint64_t)kunits[u].u.y << 8) | kunits[u].u.x);",
                "if (valid) hw_wr(HW_R_DE_MAP, valid | ((uint64_t)u << 16) | ((uint64_t)kunits[u].u.y << 8) | kunits[u].u.x);",
            ),
            (),
            "b4 unmapped",
        ),
        (
            "mutant_b5_nofault",
            pieces.replace("kstat.faults != faults0 || ", ""),
            (),
            "n1 room fault",
        ),
        (
            "mutant_b5_notimeout",
            pieces.replace("return KOS_ETIME;", "return KOS_OK;"),
            (),
            "b5 timeout",
        ),
        (
            "mutant_move_no_timeout",
            pieces.replace(
                "        k_units_poll();\n        if (hw_cycles() - t0 > KOS_DE_TIMEOUT) {\n            return KOS_ETIME;",
                "        k_units_poll();\n        if (hw_cycles() - t0 > KOS_DE_TIMEOUT) {\n            return KOS_OK;",
            ),
            (),
            "move busy",
        ),
        (
            "mutant_mover_no_clear",
            pieces.replace("k_move(mv_ixbuf, mv_ixbuf, 1);", ""),
            (),
            "move fault cleared",
        ),
        (
            "mutant_mover_clear_always",
            pieces.replace("if (HW_MV_FAULT(hw_rd(HW_R_MVSTAT))) {", "if (1) {"),
            (),
            "clear moves nothing",
        ),
        (
            "mutant_dispatch_no_timeout",
            pieces.replace(
                "        } else if (hw_cycles() - t0 > KOS_DE_TIMEOUT) {\n            return KOS_ETIME;",
                "        } else if (hw_cycles() - t0 > KOS_DE_TIMEOUT) {\n            break;",
            ),
            (),
            "dispatch credit",
        ),
        (
            "mutant_pa_priv_all",
            pieces.replace("(umap[i / 64] >> (i % 64) & 1)", "1"),
            (),
            "pa kernel frame",
        ),
        (
            "mutant_pa_base_only",
            pieces.replace("p <= end", "p <= (pa & ~0xfffUL)"),
            (),
            "pa one byte past",
        ),
        (
            "mutant_mv_negative",
            pieces.replace("lo += (uint64_t)s[n];", "(void)0;"),
            (),
            "mv negative stride",
        ),
        (
            "mutant_mv_first_level_only",
            pieces.replace(
                "while (n--) {", "n = n > 1 ? 1 : n;\n            while (n--) {"
            ),
            (),
            "mv two levels",
        ),
        (
            "mutant_mv_stale_level",
            pieces.replace("need = ((1u << n) - 1) | 0x40;", "need = 0x40;"),
            (),
            "mv level not loaded",
        ),
        (
            "mutant_mv_unstaged_axis",
            pieces.replace("if (!(mv.ok & 0x80)) {", "if (0) {"),
            (),
            "mv axis without dim",
        ),
        (
            "mutant_mv_window_no_select",
            pieces.replace("if (r != 4 && r < 6) {", "if (r == 2 || r == 3) {"),
            (),
            "mv window selects",
        ),
        (
            "mutant_mv_wrap",
            pieces.replace(" || mv.staged != (int32_t)mv.staged", ""),
            (),
            "mv partial sum wraps",
        ),
        (
            "mutant_ix_unchecked",
            pieces.replace(
                "return k_pa_ok((uint64_t)mv.s[w ? 6 : 14], top + 32 * words) ? (int64_t)load : KOS_EFAULT;",
                "return (int64_t)load;",
            ),
            (),
            "gather index past frame",
        ),
        (
            "mutant_ix_row_one_word",
            pieces.replace("top + 32 * words", "top + 32"),
            (),
            "gather row words",
        ),
        (
            "mutant_ix_no_pad",
            pieces.replace(
                "i < have ? *k_unc32(src + 4 * i) : 0", "*k_unc32(src + 4 * i)"
            ),
            (),
            "gather pad",
        ),
        (
            "mutant_ix_user_table",
            pieces.replace("mv_ixbuf | ((uint64_t)st << 40)", "mv.ix"),
            (),
            "gather kernel copy",
        ),
        (
            "mutant_ix_load_all",
            pieces.replace("unsigned load = e < MV_INDEX_MAX ?", "unsigned load = 0 ?"),
            (),
            "gather loads rows",
        ),
        (
            "mutant_ix_rows_ignore_words",
            pieces.replace("e = (e + words - 1) / words;", ""),
            (),
            "gather two-word rows",
        ),
        (
            "mutant_ix_rows_one_level",
            pieces.replace("e *= mv.n[8 * w + l];", "e *= mv.n[8 * w];"),
            (),
            "gather two levels",
        ),
        (
            "mutant_ix_rows_wrap",
            pieces.replace("e = e > (1UL << 32) ? 1UL << 32 : e;", ""),
            (),
            "gather rows saturate",
        ),
        (
            "mutant_ix_float_raw",
            pieces.replace("if (go & (1UL << 17)) {", "if (0) {"),
            (),
            "gather float",
        ),
        (
            "mutant_ix_table_unchecked",
            pieces.replace(
                "|| !k_pa_ok(src, 4 * (uint64_t)(have < load ? have : load))", ""
            ),
            (),
            "gather table outside",
        ),
        (
            "mutant_ix_stale_index",
            pieces.replace("| 0x30000u;", "| 0x20000u;"),
            (),
            "gather no index",
        ),
        (
            "mutant_ix_indexed_header",
            pieces.replace("(mode == 6 ? 0x4000u : 0x40u) | 0x30000u", "0x30000u"),
            (),
            "gather no source header",
        ),
        (
            "mutant_ix_scatter_base",
            pieces.replace("mv.s[w ? 6 : 14]", "mv.s[6]"),
            (),
            "WITNESS scatter\n",
        ),
        (
            "mutant_mv_xform_word",
            pieces.replace("MV_XFORM_OUT_BYTES : 32", "32 : 32"),
            (),
            "mv xform entry",
        ),
        (
            "mutant_fetch_any_type",
            pieces.replace(
                "if ((val >> 16) && (val >> 16) != (KOS_DE_FETCH_TYPED >> 16)) {",
                "if (0) {",
            ),
            (),
            "dst write type",
        ),
        (
            "mutant_fetch_descriptor",
            re.sub(
                r"\(f && \(\(\(arg2 \^ KOS_DE_FETCH_REPLY\).*?0xffff00\)\)\)",
                "0",
                pieces,
                count=1,
                flags=re.DOTALL,
            ),
            (),
            "fetch entry words",
        ),
        (
            "mutant_dst_unwritten",
            pieces.replace("uint64_t dst = ~0UL,", "uint64_t dst = 0,"),
            (),
            "go before dst",
        ),
    ]
    run_mutants(
        tmp_path, header, witness, pieces, mutants, includes=(OS_DIR / "include",)
    )


def test_b7_default_kds_behaviors_and_mutants(tmp_path):
    g_src = GRAPH.read_text(encoding="utf-8")
    s_src = SERVER.read_text(encoding="utf-8")
    need_fn = function(g_src, "uint64_t kds_units_need(")
    retire_fn = function(g_src, "void kds_units_retire(")
    end_fn = function(g_src, "static void end_part(")
    submit_fn = function(s_src, "static void submit(")
    header = """
#include <stdint.h>
#include <string.h>
#include <assert.h>
#define KDS_GRAPHS 4
#define KDS_HOMES 4
#define KDS_INFLIGHT 32
#define KOS_MAX_NODES 4
#define KOS_MAX_UNITS 32
#define KDS_CHAN 0x4B445300u
#define NONE 0xffffu
#define KOS_OK 0
#define KOS_EINVAL (-1)
#define KOS_EAGAIN (-6)
#define KOS_EFAULT (-7)
enum {KDS_NOP, KDS_UNIT, KDS_MOVE, KDS_CPU, KDS_STREAM};
enum {LG_FREE, LG_LOADING, LG_LOADED, LG_RUN, LG_ENDED, LG_CACHED};
enum {HG_FREE, HG_QUEUED, HG_LOADING, HG_RUN};
enum {KM_NDONE = 6};
struct kds_task {uint8_t kind, node, npred, npatch; uint16_t unit, succ_nodes; uint64_t a[4]; uint32_t id;};
struct kds_part {uint32_t gid, state; unsigned home; const struct kds_task *source; uint32_t n, done, inflight, rhead, rtail; int64_t status; uint32_t failed; int64_t detail; uint64_t busy; struct kds_task *t; uint64_t *ticket; uint16_t phead[32], ptail[32];};
struct kds_home {uint32_t gid, state; struct kds_task *t; uint32_t n; unsigned client_node, reply; uint64_t t0, deadline; uint32_t parts, contacted, acked, reported, aborted; uint32_t count[4], sent[4], scan[4]; int64_t status; uint32_t failed; int64_t detail; int answered;};
struct kds_msg {uint8_t type, flag; uint16_t cnt; uint32_t gid; uint64_t p[4];};
struct kds_node {unsigned me, n, nunits, in_flight; int hw_dirty, loading; uint32_t seq; const struct kds_task *retained; uint64_t counts[64], seen_faults[32]; struct {uint8_t part; uint16_t idx;} fly[32][32]; unsigned fhead[32], fcount[32], fwords[32]; struct kds_part part[4]; struct kds_home home[4];};
static void kds_home_ndone(struct kds_node *k, unsigned n, uint32_t g, int64_t s, uint32_t f, int64_t d, uint64_t b) {(void)k;(void)n;(void)g;(void)s;(void)f;(void)d;(void)b;}
static void kds_dep_flush(struct kds_node *k) {(void)k;}
static void kds_post(struct kds_node *k, unsigned n, unsigned c, const struct kds_msg *m) {(void)k;(void)n;(void)c;(void)m;}
static void release_part(struct kds_node *k, struct kds_part *p) {(void)k;(void)p;}
static void complete(struct kds_node *k, struct kds_part *p, unsigned i) {(void)k; ++p->done; (void)i;}
static void fail(struct kds_node *k, struct kds_part *p, unsigned i, int64_t st, int64_t d);
static uint64_t kos_now(void) { return 100; }
static void reply(struct kds_node *k, unsigned n, unsigned c, uint64_t p0, uint64_t p1, uint64_t p2, uint64_t p3) {(void)k;(void)n;(void)c;(void)p0;(void)p1;(void)p2;(void)p3;}
static int kds_part_reuse(struct kds_node *k, uint32_t g, const struct kds_task *s, uint32_t n) {(void)k;(void)g;(void)s;(void)n;return 0;}
static void kds_part_go(struct kds_node *k, uint32_t g) {(void)k;(void)g;}
static void kds_trace(unsigned p, uint64_t a, uint64_t b) {(void)p;(void)a;(void)b;}
static int check_status;
static int64_t check(struct kds_node *k, struct kds_task *t, uint32_t n, uint32_t *bad) {(void)k;(void)t;(void)n;*bad=0;return check_status;}
static void start_next(struct kds_node *k) {(void)k;}
"""
    fail_fn = "static void fail(struct kds_node *k, struct kds_part *p, unsigned i, int64_t st, int64_t d) { end_part(k, p, st, p->t[i].id, d); }\n"
    witness = """
int main(void) {
 struct kds_task t[1] = {{.kind = KDS_STREAM, .succ_nodes = 2, .a = {0, 8, 0, 0}, .id = 0}};
 uint64_t tickets[1] = {10};
 struct kds_node k = {.me = 0, .n = 2, .nunits = 2, .in_flight = 1, .loading = -1};
 k.part[0] = (struct kds_part){.gid = 1, .state = LG_RUN, .home = 0, .n = 1, .inflight = 1, .t = t, .ticket = tickets};
 k.part[0].phead[0] = 5; k.part[0].ptail[0] = 5;
 k.fly[0][0].part = 0; k.fly[0][0].idx = 0;
 k.fcount[0] = 1; k.fwords[0] = 8;
 k.counts[0] = 2; /* 2 retired so far, ticket is 10 -> delta is 8, not position m=1 */
 assert(kds_units_need(&k) == 8);
 /* Fault before ticket 10 is reached fails the part immediately and resets phead */
 k.counts[1] = 1;
 kds_units_retire(&k);
 assert(k.part[0].state == LG_ENDED && k.part[0].status == KOS_EFAULT && k.part[0].phead[0] == NONE && k.hw_dirty == 1);
 /* Now ticket 10 retires: fwords drops by 8 to 0 */
 k.counts[0] = 10;
 kds_units_retire(&k);
 assert(k.fwords[0] == 0 && k.fcount[0] == 0);

 /* submit() seq ordering: failed check() must not advance k.seq */
 k.seq = 7;
 check_status = KOS_EINVAL;
 struct kds_msg m = {.gid = 1, .p = {(uint64_t)(uintptr_t)t, 1, 0, 0}};
 submit(&k, 0, &m);
 assert(k.seq == 7);
 check_status = KOS_OK;
 submit(&k, 0, &m);
 assert(k.seq == 8 && k.home[0].gid == 8);
 return 0;
}
"""
    body = end_fn + "\n" + fail_fn + need_fn + "\n" + retire_fn + "\n" + submit_fn
    retired = "k.part[0].state == LG_ENDED && k.part[0].status == KOS_EFAULT && k.part[0].phead[0] == NONE && k.hw_dirty == 1"
    mutants = [
        (
            "mutant_need_pos",
            body.replace(
                "uint64_t delta = rem > 0 ? (uint64_t)rem : 1;",
                "uint64_t delta = m; (void)rem;",
            ),
            (),
            "kds_units_need(&k) == 8",
        ),
        (
            "mutant_retire_nofault",
            body.replace(
                "if (nf && p->state == LG_RUN)", "if (0 && nf && p->state == LG_RUN)"
            ),
            (),
            retired,
        ),
        (
            "mutant_end_nophead",
            body.replace("p->phead[u] = p->ptail[u] = NONE;", "(void)u;"),
            (),
            retired,
        ),
        (
            "mutant_submit_seq_early",
            body.replace(
                "uint32_t gid = (uint32_t)k->me << 24 | ((k->seq + 1) & 0xffffff);",
                "uint32_t gid = (uint32_t)k->me << 24 | (++k->seq & 0xffffff);",
            ),
            (),
            "k.seq == 7",
        ),
    ]
    run_mutants(tmp_path, header, witness, body, mutants)


def test_b8_c_python_binary_struct_and_lring_bound_witness(tmp_path):
    g_src = GRAPH.read_text(encoding="utf-8")
    parts_run_fn = function(g_src, "int kds_parts_run(")
    header = """
#include <stdio.h>
#include <stdint.h>
#include <string.h>
#include <stddef.h>
#include <assert.h>
#define KOS_USER_H
#include <kos/abi.h>
#include <kos/kds.h>
#include <ka/package/format.h>
#define KDS_GRAPHS 4
#define KDS_MUT 0
#define KOS_NOWAIT (~0UL)
#define NONE 0xffffu
enum {LG_FREE, LG_LOADING, LG_LOADED, LG_RUN, LG_ENDED, LG_CACHED};
struct kds_part {uint32_t gid, state; unsigned home; uint32_t n, done, rhead, rtail; uint64_t busy; struct kds_task *t; uint16_t *ready, *next; uint16_t phead[32], ptail[32];};
struct kds_node {unsigned me, n, flags, nunits; uint64_t spansz[4], counts[64], seen_faults[32]; unsigned fcount[32]; int cpu_dirty, hw_dirty, any_ready, any_pending; struct kds_part part[4]; struct {uint64_t tasks, unit_batches;} st;};
static struct kos_launch_rec sim_lring[KOS_LRING_SLOTS];
#undef kds_lring
#define kds_lring(slot) (&sim_lring[(slot)])
#define kos_cycles() 100UL
#define kos_now() 100UL
#define kos_release() ((void)0)
static int kos_event(uint64_t t, uint64_t w, uint64_t *c, unsigned n, unsigned ch, uint64_t *now) {(void)t;(void)w;(void)c;(void)n;(void)ch;*now=0;return 0;}
static void complete(struct kds_node *k, struct kds_part *p, unsigned i) {(void)k; ++p->done; (void)i;}
static void fail(struct kds_node *k, struct kds_part *p, unsigned i, int64_t s, int64_t d) {(void)k;(void)p;(void)i;(void)s;(void)d;}
static void put_bits(uint64_t w[4], unsigned lo, unsigned width, uint64_t v) {(void)w;(void)lo;(void)width;(void)v;}
static int cached(struct kds_node *k) { return !(k->flags & KDS_F_UNCACHED); }
static void acquire(struct kds_node *k) {(void)k;}
static int64_t run_move(struct kds_node *k, struct kds_task *t) {(void)k;(void)t;return 0;}
static void *kds_arena_va(struct kds_node *k, kds_ref r) {(void)k;(void)r;return 0;}
static uint64_t kds_resolve(struct kds_node *k, kds_ref r) {(void)k;return r;}
static void kds_trace(unsigned p, uint64_t a, uint64_t b) {(void)p;(void)a;(void)b;}
static int64_t kos_de_run(const uint64_t *p, unsigned c) {(void)p;(void)c;return 0;}
"""
    witness = """
int main(void) {
    memset(sim_lring, 0, sizeof sim_lring);
    sim_lring[KOS_LRING_STEP_SLOT].cyc_total = 0xABCD;
    struct kds_task marks[2] = {
        {.kind = KDS_LAUNCH_MARK, .a = {61, 0, 0, 0}, .id = 0},
        {.kind = KDS_LAUNCH_MARK, .a = {KOS_LRING_STEP_SLOT, 0, 0, 0}, .id = 1},
    };
    uint16_t ready[3] = {0, 1, 0};
    struct kds_node k = {0};
    k.part[0] = (struct kds_part){.state = LG_RUN, .n = 2, .rhead = 0, .rtail = 2, .t = marks, .ready = ready};
    kds_parts_run(&k);
    assert(sim_lring[61].id == 61);
    assert(sim_lring[KOS_LRING_STEP_SLOT].cyc_total == 0xABCD);

    struct kds_task t;
    memset(&t, 0, sizeof t);
    t.kind = KDS_UNIT;
    t.node = 2;
    t.npred = 2;
    t.npatch = 2;
    t.unit = 7;
    t.pred[0] = 3;
    t.pred[1] = 5;
    t.a[0] = 11; t.a[1] = 22; t.a[2] = 33; t.a[3] = 44;
    t.patch[0] = (struct kds_patch){0x123400, 218, 34, 0, 0};
    t.patch[1] = (struct kds_patch){0x123400, 63, 6, 34, 0};
    fwrite(&t, 1, sizeof t, stdout);
    return 0;
}
"""
    res = compile_and_run(
        tmp_path,
        "c_abi_bin",
        header + parts_run_fn + witness,
        includes=(OS_DIR / "include", FW_INC),
        defines=("KOS_LAUNCH_RING",),
    )
    assert res.returncode == 0, res.stderr
    py_bytes = Task(
        A.KDS_UNIT,
        unit=7,
        node=2,
        words=(11, 22, 33, 44),
        pred=(3, 5),
        patches=((0x123400, 218, 34, 0), (0x123400, 63, 6, 34)),
    ).pack()
    assert res.stdout == py_bytes
    assert struct.calcsize(A.LAUNCH_REC_FMT) == A.KOS_LRING_REC_BYTES
    assert struct.calcsize(A.STAT_FMT) == 8 * len(A.STAT_NAMES)
    # Mutate real kds_parts_run from graph.c: replacing KOS_LRING_MAX_LAUNCHES with KOS_LRING_SLOTS overwrites slot 62.
    mut_fn = parts_run_fn.replace("id < KOS_LRING_MAX_LAUNCHES", "id < KOS_LRING_SLOTS")
    assert mut_fn != parts_run_fn
    res_mut = compile_and_run(
        tmp_path,
        "c_abi_mut",
        header + mut_fn + witness,
        includes=(OS_DIR / "include", FW_INC),
        defines=("KOS_LAUNCH_RING",),
    )
    assert (
        res_mut.returncode != 0
        and b"sim_lring[KOS_LRING_STEP_SLOT].cyc_total == 0xABCD" in res_mut.stderr
    ), res_mut.stderr


def kos_tool(name):
    mm = pathlib.Path.home() / "micromamba/envs/kos/bin" / name
    if mm.exists():
        return str(mm)
    found = shutil.which(name)
    if not found:
        raise RuntimeError(f"missing {name}")
    return found


def test_kos_small_call_minsize_savings_witness(tmp_path):
    clang = kos_tool("clang")
    llvm_size = kos_tool("llvm-size")

    fmt_src = (OS_DIR / "lib/fmt.c").read_text(encoding="utf-8")
    assert "#ifdef KOS_SMALL_CALL" in fmt_src and "__attribute__((minsize))" in fmt_src
    sizes = {}
    for name, src in (
        ("with_minsize", fmt_src),
        ("mutant_no_minsize", fmt_src.replace("__attribute__((minsize))", "")),
    ):
        c_file = tmp_path / f"{name}.c"
        o_file = tmp_path / f"{name}.o"
        c_file.write_text(src, encoding="utf-8")
        subprocess.run(
            [
                clang,
                "--target=riscv64-unknown-elf",
                "-march=rv64ima_zicsr_zifencei",
                "-mabi=lp64",
                "-mcmodel=medany",
                "-O2",
                "-DKOS_SMALL_CALL",
                f"-I{OS_DIR / 'include'}",
                "-c",
                str(c_file),
                "-o",
                str(o_file),
            ],
            check=True,
        )
        out = subprocess.check_output([llvm_size, str(o_file)], text=True)
        sizes[name] = int(out.splitlines()[1].split()[0])
    assert sizes["with_minsize"] + 100 < sizes["mutant_no_minsize"], sizes
