---
title: KohakuAccel OS measured performance
summary: Measured costs of KohakuAccel OS and of the hardware under it, on KohakuTPU v9t6 silicon (four dies) and on the Verilator models, every number tagged with the run it came from.
tags:
  - os
  - performance
  - measurements
---

# Measured performance

All numbers are **node clock cycles** of node 0 (on silicon the node clock is
100 MHz: 1 000 cycles = 10 µs), as the applications report them (`@ name
value`, [use/apps.md](../use/apps.md)). Every table names the run each
column comes from; the runs are listed first. Every run below passed: every
node exited 0 and no `FAIL` line appeared.

## The runs

**Silicon** — KohakuTPU v9t6 (`multimesh_v9t6`), four dies at 100 MHz,
driven over JTAG by `run_card.py --jtag`; five loads in a row on one card,
no reset between them. The JSON of each run is
`software/os/tools/<run>.json`.

| run | board | image, command | flags | load / run (s) |
|---|---|---|---|---|
| `kos_solo` | `card_v9t6_4n_solo` | kos, `hello; units; mg` | — | 113.4 / 1.1 |
| `m0_card` | `card_v9t6_4n` | m0, `lat ring sc amo trap itext` | — | 285.6 / 8.2 |
| `m0w_card` | `card_v9t6_4n` | m0w, `walk` | — | 291.7 / 7.4 |
| `kos_card` | `card_v9t6_4n` | kos, `hello; units; mg; mover 32; pipeline 64; msgsum 1024; bench 8` | — | 451.4 / 10.0 |
| `kos_card_engine` | `card_v9t6_4n` | kos, the same command | `engine` | 445.5 / 10.9 |

**Verilated card models** (`run_card.py --model`):

| run | board (model) | command | flags |
|---|---|---|---|
| `solo1n` | `card_v9t6_1n` (1 die) | `hello; units; mg` | — |
| `doc_1n_engine` | `card_v9t6_1n` (1 die) | `units; mg; mover 32; bench 8` | `engine` |
| `t2s_diag` | `card_v9t6_2n` (2 dies) | the `kos_card` command | — |
| `doc_2n_engine` | `card_v9t6_2n` (2 dies) | the `kos_card` command | `engine` |

**Core-only models** (`run_core.py`; no compute units):

| run | board | image, command | flags |
|---|---|---|---|
| `doc_core2`, `doc_core4s` | `core2`, `core4s` | m0, `lat ring sc amo trap itext` | — |
| `doc_core2_walk`, `doc_core4s_walk` | `core2`, `core4s` | m0w, `walk` | — |
| `doc_c2` | `core2` | kos, `hello; pipeline 200; msgsum 2048; mover 32; bench 8` | — |
| `doc_c4_<knob>` | `core4s` | kos, `hello; preduce 4096; psort 2048; pmatmul 16; workq 32; stress 2 1 4; pipeline 200; msgsum 2048` | one knob each: none, `shared_uncached`, `pt_dram`, `lock_bakery`, `poll`, `tick` 200 000 |
| `doc_c4_bench` | `core4s` | kos, `bench 8` | — |
| `doc_c3s` | `core3s` | kos, `hello; ps; kstat; units; preduce 3072; workq 24; pipeline 90; msgsum 1536; ps` | — |

## The node and the interlink

The bare-metal probe image (`tests/m0`), no OS:

| probe | `doc_core2` / `doc_core4s` | silicon `m0_card` |
|---|---|---|
| uncached DRAM load / store | 20 / 21 | 31 / 30 |
| staging load / store | 19 / 15 | 19 / 17 |
| cached miss / hit / store hit | 31 / 8 / 8 | 49 / 8 / 8 |
| cached miss with a dirty victim | 49 | 73 |
| `DCACHE` flush, 64 dirty lines / clean | 922 / 144 | 1486 / 144 |
| `DCACHE` invalidate | 76 | 76 |
| `DCACHE` flush + invalidate, 64 dirty lines | 982 | 1552 |
| control-register read | 7 | 7 |
| signal-slot read (select, wait for the field, read again) | 19 | 19 |
| interlink ring round trip (node 0 ↔ node 1) | 85 | 121 |
| 33 rings → doorbell count | 33 | 33 |
| 8 remote stores + fenced ring: producer | 102 | 102 |
| … round trip (peer checks the 8 words, rings back) | 336 | 378 |
| S → M `ecall` round trip | 198 | 198 |
| injected supervisor software interrupt, latency | 258 | 258 |
| S timer lateness, average / worst | 65 / 71 | 65 / 71 |
| 1000-iteration tiny loop, from IMEM / DRAM | 10 038 / 10 274 | 10 036 / 10 449 |
| … wide loop body | 43 047 / 115 072 | 43 047 / 165 138 |
| … call-heavy loop | 28 092 / 74 134 | 28 092 / 104 197 |

`core2` and `core4s` give the same numbers. **Inside a node silicon and the
models agree to the cycle** (cache hits, control and slot reads, traps,
instruction fetch from IMEM, a clean sweep). **Everything that touches DRAM
costs more on silicon**: uncached access 1.5×, cached miss 1.6×, dirty-line
work 1.5–1.6×, instruction fetch from DRAM up to 1.4×. An interlink ring
round trip is 1.4× (121 vs 85); posted stores into staging and the fenced
producer are the same.

Memory-model probes on `core4s` (`doc_core4s`, uncached shared DRAM, 256
iterations each): store buffering, both loads zero: 0; message passing, stale
data after the flag: 0. Four nodes × 2000 increments of one shared word
(expected 8000): `amoadd` 2009, LR/SC 2000 with 0 SC failures on every node
([constraints](constraints.md#atomics-are-not-atomic-across-nodes)).

The page-table walker (`walk`, S translated) gives the same ten values on
`doc_core2_walk`, `doc_core4s_walk` and silicon `m0w_card`: A-clear load
faults (cause 13, PTE stays `0x07`), D-clear store faults (cause 15, PTE
stays `0x47`), the old frame is read without `sfence.vma`, a misaligned
gigapage faults, a cached unflushed DRAM PTE faults and a flushed one does
not ([constraints](constraints.md#the-page-table-walker)).

## OS primitives

`bench` (averages over `iters`; spawn = spawn + join of an empty thread):

| primitive | silicon `kos_card` (4 dies) | silicon `kos_card_engine` | `t2s_diag` (2-die model) | `doc_2n_engine` | `doc_1n_engine` (1-die model) | `doc_c4_bench` (core4s) | `doc_c2` (core2) |
|---|---|---|---|---|---|---|---|
| null system call | 624 | 624 | 621 | 621 | 626 | 615 | 614 |
| kernel → L0 call | 232 | 232 | 232 | 232 | 232 | 232 | 232 |
| yield (two switches) | 5546 | 6107 | 5337 | 5723 | 5457 | 4952 | 4766 |
| spawn, this node | 88 578 | 89 081 | 81 928 | 82 254 | 86 238 | 54 408 | 50 978 |
| spawn, node 1 | 86 076 | 86 280 | 79 762 | 79 212 | — | 60 202 | 55 486 |
| atomic, home here / on another node | — | — | — | — | 1119 / — | 986 / 5212 | — |
| release / acquire sweep | — | — | — | — | 1056 / 1168 | 969 / 1060 | — |
| channel round trip to node 1 | 11 747 | 12 578 | 11 648 | 12 025 | — | 12 911 | 10 254 |
| barrier, one thread per node | 12 936 | 12 550 | 7178 | 7567 | 7056 | 12 167 | 6619 |
| `mmap` of 4 pages | 80 488 | 80 495 | 76 724 | 76 734 | 70 374 | 54 172 | 39 967 |

Atomics and the cache sweeps are measured only on boards with a shared
region. The system call and the L0 call cost the same everywhere (they never
leave the node). Spawn and `mmap` take and zero fresh pages, so they follow
DRAM speed: on the card they cost 1.5–2× what they cost on the core models. A barrier is one
message from each node to node 0 and one reply back, so it grows with the
node count, alike on silicon and the models: 6.6–7.2 k with 2 nodes
(`doc_c2`, `t2s_diag`), 12.2–12.9 k with 4 (`doc_c4_bench`, `kos_card`). An
atomic whose home is another node is a request and a reply: 5.2 k against
1.0 k at home (`doc_c4_bench`).

`hello` (init → exit, one thread spawned on every node and joined):
`kos_solo` 225 071, `kos_card` 496 532, `kos_card_engine` 497 631, `solo1n`
215 610, `t2s_diag` 289 623, `doc_c2` 181 298, `doc_c3s` 244 539,
`doc_c4_none` 300 043.

## Dispatch

| measure | silicon `kos_solo` (die 0) | silicon `kos_card` | silicon `kos_card_engine` | `solo1n` | `doc_1n_engine` | `t2s_diag` | `doc_2n_engine` |
|---|---|---|---|---|---|---|---|
| `CU_CTRL` read, unit on this die (4 reads, per unit) | 11 291–12 900 | 12 027–13 614 | 11 989–13 594 | 12 419–12 668 | 12 055–12 588 | 11 547–12 998 | 11 546–12 997 |
| `CU_CTRL` read, unit on another die (4 reads, per unit) | — | 29 108–33 057 | 29 104–33 435 | — | — | 29 116–31 591 | 29 122–31 595 |
| MG round (FILL, FILL, DRAIN + wait), mailbox, this die | 10 948–11 171 | 11 493–11 567 | — | 10 794–10 936 | — | 11 201–11 263 | — |
| … through the engine queue (`engine` flag) | — | — | 8538–8606 | — | 8048–8174 | — | 8184–8234 |
| … one `SYS_ENGINE` of 3 | — | — | 5839–5880 | — | 5541–5571 | — | 5589–5617 |
| MG round on a unit of another die | — | 24 109–28 419 | 26 209–31 669 | — | — | 23 191–24 436 | 25 901–27 857 |
| mover, 32 words (1 KB) on this node | — | 5493 (186 B/kcycle) | 5666 (180) | — | 5428 (188) | 5192 (197) | 5236 (195) |
| mover, 32 words into node 1's landing zone | — | 4240 | 4339 | — | — | 4002 | 3993 |

The core models have no units; their mover costs (`doc_c2`): 3768 cycles for
32 words locally (271 B/kcycle), 2999 into node 1's landing zone.

A unit on another die costs about two kernel messages more per call: a
`CU_CTRL` read goes from 3.0–3.4 k to 7.3–8.4 k cycles, an MG round from
~11.5 k to 24–32 k on silicon. **With the engine owning the local units**, the same
three `SYS_DISPATCH` calls take ~8.6 k cycles instead of ~11.5 k, and one
`SYS_ENGINE` of three ~5.9 k (silicon `kos_card_engine`).

## Applications

### Message passing (every board with two or more nodes)

| app | silicon `kos_card` (4 dies) | silicon `kos_card_engine` | `t2s_diag` (2 dies) | `doc_2n_engine` | `doc_c2` (2 nodes) | `doc_c3s` (3 nodes) | `doc_c4_none` (4 nodes) |
|---|---|---|---|---|---|---|---|
| `pipeline`: items × stages | 64 × 4 | 64 × 4 | 64 × 2 | 64 × 2 | 200 × 2 | 90 × 3 | 200 × 4 |
| … cycles per item | 9596 | 10 083 | 4525 | 4609 | 3843 | 9033 | 14 931 |
| `msgsum`: values, cycles | 1024, 401 428 | 1024, 401 825 | 1024, 311 958 | 1024, 311 298 | 2048, 261 617 | 1536, 310 886 | 2048, 357 374 |

### Shared memory (`core4s`, `core3s`)

One worker against one per node (`doc_c4_none`, 4 nodes; `doc_c3s`, 3
nodes):

| app | 1 worker | all workers | speedup |
|---|---|---|---|
| `preduce 4096` (core4s, 4 workers) | 78 918 | 32 146 | 2.45× |
| `psort 2048` (core4s, 4 workers) | 2 896 427 | 888 526 | 3.25× |
| `pmatmul 16` (core4s, 4 workers) | 165 447 | 89 087 | 1.85× (45 MAC/kcycle) |
| `workq 32` (core4s, 4 workers) | 1 929 554 | 782 008 | 2.46× |
| `preduce 3072` (core3s, 3 workers) | 60 721 | 31 319 | 1.93× |
| `workq 24` (core3s, 3 workers) | 1 445 375 | 700 963 | 2.06× |

`stress 2 1 4` (`doc_c4_none`): 32 lock operations from four nodes, 12 861
cycles per lock operation with the ticket lock (a home-node atomic, a wait,
and the acquire/release sweeps).

## Knobs

The same `core4s` command under each knob (`doc_c4_<knob>`; four workers;
cycles):

| knob | `preduce` | `psort` | `pmatmul` | `workq` | lock op | `pipeline`/item | `msgsum` | whole run |
|---|---|---|---|---|---|---|---|---|
| none | 32 146 | 888 526 | 89 087 | 782 008 | 12 861 | 14 931 | 357 374 | 15.54 M |
| `shared_uncached` | 36 405 | 1 115 442 | 121 061 | 818 527 | 11 424 | 12 156 | 351 067 | 15.94 M |
| `pt_dram` | 32 206 | 887 044 | 89 071 | 760 412 | 13 153 | 15 213 | 368 512 | 15.62 M |
| `lock_bakery` | 32 146 | 888 526 | 89 087 | 782 008 | 14 711 | 15 068 | 355 531 | 15.63 M |
| `poll` | 36 589 | 897 204 | 93 004 | 878 218 | 17 726 | 35 292 | 378 988 | 21.07 M |
| `tick` 200 000 | 32 346 | 890 319 | 89 510 | 755 648 | 13 567 | 15 399 | 356 692 | 15.72 M |

What they show:

- **`shared_uncached`** costs the compute-bound applications whose data
  lives in shared memory (`psort` +26 %, `pmatmul` +36 %, and their
  one-worker baselines more: `pmatmul` 237 603 against 165 447) and helps
  where time goes into the release/acquire sweeps (lock op −11 %, `pipeline`
  −19 %).
- **`poll`** (no external interrupt; the kernel polls after every system
  call) costs most where nodes wait on each other: `pipeline` 2.4×, lock op
  +38 %, the whole run +36 %.
- **`lock_bakery`** changes only lock-heavy code: lock op +14 % against the
  ticket lock at four nodes.
- **`pt_dram`** (page tables in DRAM) and **`tick`** (preemption every
  200 000 cycles) stay within a few percent of the default.
- **`engine`** (silicon `kos_card` against `kos_card_engine`): local MG
  dispatch −25 % through the engine queue and −49 % as one `SYS_ENGINE`
  batch; the rest of the run within a few percent.

## Silicon against the models

The OS on four silicon dies behaves as on the models: the same commands
pass, with the same checks. Costs that stay inside a node match the models to
the cycle (system call 621–626, L0 call 232, every trap). Costs that go
through DRAM or the interlink are higher on silicon — spawn and `mmap` ~5–10
% over the card models, a remote MG round 24–32 k against 23–28 k on the
2-die model, and the barrier and pipeline grow with the four dies' extra
hops.
