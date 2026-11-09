# What the data says

Node cycles at mid, 200/400 MHz, on v9t6 2+2, node 0, unless a row says otherwise.
Every number comes from `data/`; each section's figures carry the same numbers. Every
run has all four dies at one clock, because idle dies change node 0's cycles. Runs that
did not are kept in `data/old/` and used nowhere.

The first section is the output statistics of every model. Round `i1` runs ViT-B
across one, two and four sysnodes, and reruns the round `h1` correctness probes. Round `h1`
measures the multi-sysnode primitives on the card with hand-written L1
(`software/tests/perf/h/`). The next six are rounds `g1`, at commit 9da806c4, `f1`, at commit 97a4b0a7, `e1`, at commit de2c9b1e with the
silicon flaw registry and its handlings, `d2`, at
commit a5e68412 with the performance batch on top, `d1`, at commit 1abcfb30, and `c1`,
at commit 56021fd4. The sections after them hold 56021fd4's zoo, kept as the baseline,
except the rows that name `c1`.

## Output statistics

One TinyJit run a model on v9t8 4+2 at mid, d2's library, commit 60defd91 with the
statistics check (`data/<model>.ckj.multimesh_v9t8.1n/meta.json`, `check`). Each run's
outputs are compared with the same model on the CPU in f32, and with the same CPU model
whose every matrix product reads both operands as the clusters do, FP16 then MXFP7 in
blocks of 32 (`checks/accuracy.py`). The MXFP7 run against f32 is the format's footprint.
Errors are absolute, and relative per element (so near-zero logits make the relative max
large), over every logit of 8 test images (6 for ViT-B, 9 for ViT).

| Model | Card − f32 abs max, mean, median | Card − MXFP7 abs max, mean, median | MXFP7 − f32 abs max, mean, median | Card − f32 rel mean, median | MXFP7 − f32 rel mean, median | Outside |
|---|---|---|---|---|---|---|
| ViT-B | 0.1008, 0.0211, 0.0180 | 0.1061, 0.0217, 0.0181 | 0.1040, 0.0223, 0.0191 | 0.409, 0.047 | 0.490, 0.051 | 0 of 6000 |
| ViT | 0.0340, 0.0073, 0.0060 | 0.0412, 0.0080, 0.0068 | 0.0437, 0.0084, 0.0071 | 0.161, 0.032 | 0.199, 0.037 | 0 of 9000 |
| GPT | 0.0221, 0.0063, 0.0052 | 0.0107, 0.0019, 0.0012 | 0.0247, 0.0069, 0.0049 | 0.021, 0.010 | 0.023, 0.011 | 0 of 80 |
| LeNet | 0.0123, 0.0039, 0.0037 | 0.0124, 0.0040, 0.0033 | 0.0147, 0.0040, 0.0034 | 0.079, 0.010 | 0.054, 0.012 | 0 of 80 |
| Mixer | 0.0072, 0.0023, 0.0019 | 0.0078, 0.0020, 0.0015 | 0.0076, 0.0024, 0.0020 | 0.038, 0.012 | 0.049, 0.016 | 0 of 80 |
| MLP | 0.0060, 0.0021, 0.0019 | 0.0020, 0.0002, 0.0001 | 0.0060, 0.0021, 0.0019 | 0.023, 0.007 | 0.024, 0.007 | 0 of 80 |
| wide | 0.0051, 0.0008, 0.0006 | 0.0013, 0.0003, 0.0002 | 0.0050, 0.0009, 0.0006 | 0.016, 0.006 | 0.017, 0.006 | 0 of 80 |
| GRU | 0.0040, 0.0008, 0.0007 | 0.0015, 0.0003, 0.0002 | 0.0037, 0.0007, 0.0005 | 0.033, 0.012 | 0.031, 0.009 | 0 of 80 |

Every model's card error against f32 is the size of the format's own: its largest is
under the MXFP7 run's largest or at most 8% over it (GRU, 0.0040 against 0.0037), and the
absolute means and medians are within 15% of the MXFP7 run's. MLP, wide, GRU and GPT sit on the
MXFP7 run, 2.6× to 14× closer to it than to f32 in mean absolute error; ViT, ViT-B, LeNet
and Mixer sit as far from it as from f32, an error of the same size from the sources the
MXFP7 run leaves out (FP16 storage, E8M15 vector math, FP22 accumulation). "Outside"
counts values further than the footprint's largest error from both f32 and the MXFP7
run: none. These runs were recorded with a one-sided count, so their `outside_footprint`
reads 1 for MLP, GRU and wide (against f32) and ViT-B (against MXFP7); the listed value
(`worst`) is inside the footprint of the other reference each time, and every other
value is inside both, since the card's largest error against the other is under the
footprint's largest. Top-1 agrees with f32 on every image except ViT's 6 of 9; the MXFP7 run also
agrees on 6 of 9: the nine images' f32 top-1 to top-2 gaps are 0.0001 to 0.079.

## Round i1

ViT-B, width 768 and 12 blocks, at batch 1, 4 and 16 on one, two and four
sysnodes; v9t6 has 2 MG + 2 VC per sysnode, v9t8 has 4 MG + 2 VC. All dies
were at mid, per the run setup. Runner: `software/tests/perf/record.py vitb
--card`. Each complete run has two inputs. Paths below are under
`data/i1/vitb.i11.<path>.b<batch>.p<sysnodes>.<board>.<n>n[.<backend>]`.
Configuration, input count and output checks are in each directory's `meta.json`
and `report.json`; `meta.json` does not retain a commit hash or measured clocks.
The supplied run notes associate the final runs with 948d9a29 and the preceding
runs with its uncommitted precursor; these files do not independently establish
that provenance or byte-identical compiled packages.

Here **second input**, not retained-graph steady state, is the reporting interval.
Firmware-backend and KAOS rows sum that input's `card.jsonl.gz.max_node_cycles`,
the maximum node-local duration of each completed launch. This is not global
multi-sysnode makespan and excludes host time between launches. Firmware measures
a package; KAOS measures KDS submit-to-reply (`software/tests/perf/backends.py:19-39`).
The files predate the explicit `cycle_scope` fields now emitted by that source.
Eager rows below are reconstructed from `units.jsonl.gz`, as explained below.

The three execution configurations are:
- **firmware** (`--backend firmware`, normal firmware image): TinyJit enabled;
  the second input executes launch packages while making the first KTPUGraph
  recording. It does **not** replay a retained whole-model bound artifact.
- **eager** (`--measure --no-jit`, measure image): host-submitted launches with
  per-node package and unit-counter records. It has no KTPUGraph recording.
- **KAOS** (`--backend kaos`, `shared_phys`): TinyJit enabled; the second input
  executes launches lowered to dispatcher tasks while making the first recording.

This follows `record.py:463-475`, `backends.py:108-138`, and
`ktpugrad/device.py:459-506`. A later matching recording is needed before the
prepared replay path invokes `runtime/graph/backend.py:12-75`. Completed backend
runs have one recording, zero replays and history `recorded: first`. The two
interrupted KAOS runs also increment `recorded` to one, but have empty histories:
the counter increments before execution completes. Their metadata is not evidence
of successful recording. The matching completed eager/firmware runs have identical
captured L2 dictionaries (`kernels.l2.json.gz`); this does not prove identical
firmware/package instrumentation.

`launches.jsonl.gz` marks the second input as `capture` for the backend runs.
Its first zero-based sequence is 309/321 for batch 1 (v9t6/v9t8), 371/383 for
batches 4 and 16 on one sysnode, and 372/384 for those batches on multiple sysnodes.
The second-input launch counts are respectively 248/260, 321/333 and 322/334.
Completed eager traces have the same ordered launch names and kinds as the
corresponding firmware trace; that correspondence supplies their input boundary.

Errors below are absolute card-minus-CPU-f32 logit statistics over **both inputs**,
from `meta.json.check`, also retained in `report.json.check`. The reference
MXFP7-minus-f32 maximum/mean is batch-dependent: batch 1 0.1040/0.0228, batch 4
0.1040/0.0219, batch 16 0.1174/0.0216 (the corresponding firmware P1/v9t6
`report.json`, `check.mxfp7_f32.absolute`).

### Firmware path

Source for each cycle cell: the indicated `firmware` trace's `card.jsonl.gz`
joined on `seq` with `launches.jsonl.gz`. Factors divide the one-sysnode cell
by the indicated cell. Error ranges are across the listed sysnode counts.

| board | batch | 1 sysnode | 2 sysnodes | 4 sysnodes | error max, mean |
|---|---|---|---|---|---|
| v9t6 2+2 | 1 | 42,072,185 | 28,264,935 (1.49×) | 19,556,346 (2.15×) | 0.0989–0.0991, 0.0203–0.0205 |
| v9t6 2+2 | 4 | 317,388,189 | 113,453,616 (2.80×) | 74,894,300 (4.24×) | 0.1005–0.1022, 0.0217 |
| v9t6 2+2 | 16 | 802,695,223 | 408,087,586 (1.97×) | 278,152,559 (2.89×) | 0.1121–0.1134, 0.0218–0.0219 |
| v9t8 4+2 | 1 | 40,249,814 | 27,434,280 (1.47×) | 19,594,407 (2.05×) | 0.0932–0.1025, 0.0203–0.0207 |
| v9t8 4+2 | 4 | 305,669,137 | 111,285,418 (2.75×) | 72,728,174 (4.20×) | 0.1034–0.1061, 0.0216–0.0217 |
| v9t8 4+2 | 16 | 743,392,803 | 388,701,297 (1.91×) | 259,237,555 (2.87×) | P1/P2: 0.1221, 0.0217; P4: 3.1698, 0.0414 |

Batch-4 / batch-1 cycle ratios are 7.54 and 7.59 on one sysnode, and
3.71–4.06 on two/four sysnodes, calculated from the preceding cells. The errors
are not numerically identical across sysnode counts.

For v9t8 batch 16/P4, `report.json.check` records 876 of 32,000 logits outside
the two-reference footprint, absolute maximum 3.1698489 and mean 0.0414006.
The retained worst entries refer to image index 9 of the first input; their card
values match `outputs.jsonl.gz` decoded as 16 FP16 rows with storage pitch 1,024
and 1,000 logical logits. Against the f32 reference `data/i1/references/vit.w768.b12.i224.s17.n16x2.npz` (the run's own, copied from the host), image 9 of the first input has 702 of 1,000 logits more than 0.3 from f32, the other 15 images of that input at most 0.1221, and the 16 images of the second input at most 0.1003. The second-input timing is retained, but
the run has an output fault (ISSUES 90).

### Eager path — corrected aggregation

**The multi-sysnode eager `card.jsonl.gz` is misjoined.** The recorder appends
one `_result` per node (`record.py:369-372`; `runtime/card/collection.py:232-278`),
then associates successive records with successive launches (`record.py:315-355`).
It consumes only a prefix of the per-node completion stream. Thus the old eager
sums and kind breakdowns do not measure the named second-input launches.

The following cells instead consume each active node's `units.jsonl.gz` in its
own run order, using `launches.jsonl.gz.info.active_nodes`, then sum the maximum
participating-node `cycles` per second-input launch. Node 0 participates in every
launch; the larger-batch single-node launches advance only its cursor. All ring
records are accounted for. Flattening these reconstructed completions in
launch/node order reproduces every original eager card row's cycles, confirming
the misjoin. One-sysnode cells are unchanged. `cycles` is package duration recorded
before counter collection (`firmware/package/interp.c:1752-1775`), not unit busy.

| board | batch | 1 sysnode | 2 sysnodes | 4 sysnodes |
|---|---|---|---|---|
| v9t6 | 1 | 41,418,292 | 27,582,744 | 19,358,031 |
| v9t6 | 4 | 312,414,982 | 112,731,541 | 75,661,156 |
| v9t6 | 16 | 780,442,828 | stopped twice, `BAD_STEP` (ISSUES 85) | 276,322,537 |
| v9t8 | 1 | 41,248,289 | 27,582,126 | 20,410,862 |
| v9t8 | 4 | 299,143,459 | 111,779,623 | stopped, engine TIMEOUT (ISSUES 82) |
| v9t8 | 16 | 731,539,956 | 389,703,991 | 260,362,365 |

Completed eager errors range from 0.0989 to 0.1221 maximum and 0.0203 to
0.0220 mean (`fw` directories' `meta.json.check.card_f32.absolute`). Against the
corresponding firmware cells, the corrected eager differences are −2.41% to
+4.17% at batch 1, −2.13% to +1.02% at batch 4, and −2.77% to +0.43% at batch 16.
These are observations of different images/instrumentation, not isolated dispatch
savings. In particular, neither the earlier batch-1 P2 slowdown nor the claimed
13–22% batch-4 eager penalty survives correct aggregation.

### KAOS path

Source: `kaos` directories' `card.jsonl.gz`, `launches.jsonl.gz` and `meta.json`.

| board | batch | 1 sysnode | 2 sysnodes | 4 sysnodes |
|---|---|---|---|---|
| v9t6 | 1 | 52,591,480 | 40,064,521 | stopped at zero-based seq 337, second input (ISSUES 89) |
| v9t8 | 1 | 51,865,841 | stopped at zero-based seq 484, `KDS status -7` (ISSUES 86) | not run |

The completed rows' error maxima are 0.0989–0.1008 and means 0.0203. Their cycle
ratios to firmware are 1.2500, 1.4175 and 1.2886 respectively (rounded range
1.25–1.42×), comparing KDS-submit-to-reply with firmware-package timing, not
identical scopes. The v9t6 P4 trace has 309 completed first-input launches and
28 completed second-input launches before seq 337 stalls. The v9t8 P2 trace has
321 and 163 respectively before seq 484 fails. Partial sums are not whole-input
performance results. Offline, before the card runs, every launch of one batch-1 input was translated into its
KAOS tasks and packed against the resident's task capacity on one, two and four v9t6
sysnodes: 309 of 309 on each, none refused, a DE feed and a DE finish a sysnode
(`data/i1/kaos_preflight/kpre.multimesh_v9t6.p{1,2,4}.b1.jsonl.gz`, script `preflight.py`
there). Translation is host-side only; it does not exercise the card.

### Correctness reruns

Round h1 probes on v9t6 at mid. Counts below are recomputed from
`data/i1/<directory>/runs.jsonl.gz`, `verdict.structural`, except S0, whose
`cases.jsonl.gz` retains numerical checks. PASS does not establish the absence
of rare faults outside these samples.

| probe | round i1 | directory / ISSUES |
|---|---|---|
| C1, serial → overlap → serial triples, reps 0–49 | 5,850/5,850 PASS | `c1.multimesh_v9t6.mid` / 31 |
| C4, release frontier, normal and delayed | 20/20 each, 40/40 PASS; output mismatches zero | `c4.multimesh_v9t6.mid` / 75 |
| S0 attention, 10 heads, R=1,8,64; 20 replay calls per R after four non-replay calls | R=1/8: zero outside footprint; R=64: 64 outside; retained worst entries include −781.5 vs +0.0063385 and +639.5 vs −0.0056093, so signs are not preserved | `s0.multimesh_v9t6.mid/attn.h10/cases.jsonl.gz` / 77 |
| C2, one-beat-tail variants, fresh/stamp | 256: 30/30 PASS each; 254+2: 9/30 and 4/30 FAIL; 255+1: 4/30 and 3/30 FAIL; 128+128, 252+4 and 253+3: 30/30 PASS each variant | `c2.multimesh_v9t6.mid.i79` / 79 |
| P12, shared all-gather | 960/960 PASS | `p12.multimesh_v9t6.mid` / 79 |
| P13, including 63/65-entry B fills | 520/520 PASS | `p13.multimesh_v9t6.mid` / 80 |
| C3, stream + compute | 317/319 PASS: one 64-byte foreign line and one engine TIMEOUT. The trace ends at `c3_mgvc_none` rep 18; it contains no `c3_mgvc_p5_disjoint` rerun | `c3.multimesh_v9t6.mid.steps` / 81,82 |
| P19, matmul shards alone and together | 7,999/8,000 PASS; one failure with 200 wrong packed u64 words. Layout audit maps these to logical columns 136–139 across 200 rows, not 50 logical rows | `p19.multimesh_v9t6.mid` / 82 |

## Round h1

The primitives a tensor-parallel kernel over the four sysnodes is built from, measured on
both boards with all four dies at mid: quarter shards, rings, phases, dependency hops,
peer drains, shared reads, movers, dispatch feed, replayed four-sysnode graphs, and
serial → overlap → serial correctness triples. Runner `software/tests/perf/hrun.py`, fns
generated by `software/tests/perf/h/gen/<row>.py`; one directory a row and board under
`data/h1/<row>.<board>.mid[.<variant>]` (`runs.jsonl.gz` a run a line with its verdict,
outputs, ledger, interlink counters and cycles; `meta.json` the firmware, clocks, boot
flags and commit). Each row ran after a retune of every die (ISSUES 78). Structural
verdicts (PASS / FAIL / EXPECTED) are separate from the numerical comparison. Cycles are
sysnode cycles at 200 MHz, median over the row's runs, maximum over the sysnodes of a run.

| What | v9t6 2+2 | v9t8 4+2 | Data |
|---|---|---|---|
| Quarter shard cost f, N split (quarter / full, ideal 0.25) | FC1 0.27, QKV 0.36, FC2 0.36, projection 0.40; LayerNorm 49–52 rows 0.39–0.40; softmax 12 heads × 49–52 rows 0.28–0.30; 3-head attention 0.34 | FC1 0.31, QKV 0.33, FC2 0.47, projection 0.53; LayerNorm 0.39; softmax 0.28–0.29; attention 0.33 | `s0.*` |
| Quarter shard cost f, K split (partial products, no reduction) | FC2 K=768 0.33, projection K=192 0.49 | FC2 0.34, projection 0.55 | `s0.*` `kalt.*` |
| Producer-packed FC1 (full width / quarter) | 1.224 M vs 1.217 M without; 0.320 M vs 0.343 M | 1.183 M vs 0.984 M (+20%); 0.349 M vs 0.316 M | `s0.*` |
| Ring + consume, 0 → 1 / 2 / 3 | 4.49 / 4.97 / 5.44 k (+~475 a hop) | | `q0.*` |
| Four-way phase (slope over 2–16 phases) | all-to-all 3.1 k, gather + broadcast 3.5 k, chain 5.4 k | | `p3.*` |
| Dependency hop | 6.2 k local (VC kernel + wait); +3.6 k a cross-sysnode hop | | `p16.*` |
| Dispatch feed, one sysnode | empty 1.34 k; 122 a send, 400 a wait, 1,420 a mover entry, 183 a repeat | | `p15.*` |
| Four feeders at once | 512 sends 100.2 k vs 65.3 k alone (1.53× gross; 1.04× after each empty baseline) | | `p15.*` |
| Replayed four-sysnode graph, a replay | empty 4.1 k, 4 shards + join 38.7 k, chain of 4 44.5 k, fan-out 22.7 k, fan-in 30.0 k | same within 0.5% | `p18.*` |
| Peer drain to another die | cluster → far VC 4.3 B/cycle; VC → far VC 3.8 B/cycle | | `p5.*`, `p7.*` |
| Shared read, one reader | VC 9.6 B/cycle on every sysnode and stripe phase; MG fill 3.5–4.0 B/cycle | | `p10.*` |
| Mover, 1 MiB to canonical DRAM | sysnode 0 71.7 k; sysnodes 1–3 424–427 k (5.9×) | | `p8.*` |
| Matmul shards alone vs together (γ) | FC1 quarter 1.07 (R=8 1.14), QKV 1.04; 2 sysnodes 1.01; pack interferer 1.49 | | `p19.*` |
| FP22 peer reduction, 200×768 | one add 170 k, tree of four 234 k; structural PASS, numeric comparison unresolved | | `p6.*` |
| Overlap triples C1 | 5,850 runs PASS; overlap within 0.3 k of alone | 5,850 PASS | `c1.*` |
| Stream + compute C3 (link only / + VC / + MG + VC, one link) | 333.4 / 367.0 / 392.0 k | 333.8 / 366.7 / 389.5 k | `c3.*.steps` |

| Correctness | Evidence |
|---|---|
| Release frontier (ISSUES 75) | A delayed producer's consumer reads POISON 20 of 20 on both boards (`c4.*`) |
| Xache one-beat read handover (79) | Fills ending in a 64-byte one-beat read wrong in 27/180 (v9t6) and 34/180 (v9t8); multi-beat tails 0/180 each; 128 + 128 is 4.2% faster than 256 (`c2.*.i79`). Shared all-gather 1–35% of runs at large shapes (`p12.*`) |
| Doorbell egress stop (78) | Rings stay stuck across processes until a retune; from a clean retune P3 passes 480 of 480 |
| Output gap at 63 / 65 entries (80) | 128 bytes unwritten 20 of 20 (`p13.*`) |
| ACK0 on remote VC bursts over 32 words (81) | 33+ words flagged in every run on both boards, 31 and 32 clean, data exact (`p7.*.ack`, `c3.*.steps`) |
| Gross wrong output under overlap (31, 77, 82) | S0 attention h10 one row (v9t6); P19 2 of 8,000 (v9t8); C3 cluster stream + compute 2 of 20 (v9t6) |
| Whole-L1 invalidate without writeback | Drops the dispatching CPU's dirty state; later rings time out (`p14.*`): acquire is flush + invalidate |
| Package-path rings (boot flag NOENGINE) | Every ring times out (`p1.*`); the dispatch-engine path passes 3,520 of 3,520 |

## Round g1

103 of 103 runs exited, every output inside the MXFP7 footprint (`outside_footprint` 0),
no F-001 split command (`f001_split` 0): eight models, eager and TinyJit, 2+2 and 4+2, all
four dies at mid, the measure image, plus nine TinyJit knob variants on 2+2 (71 runs; the
composed tree is run on seven models, its LeNet library never filled: one kernel compiles
past the 100 s fill bound). Tables under `rounds/g1`; `rounds/g1/data/zoo.csv` holds every
column. The batch: one byte-span join test (ISSUES 51), deferred forms ended or made inside
a TinyJit record (50, 52), F-001 proven at the mover command boundary (53, 57), chain
renaming (54), raw RING credits (55), nested launch events (56), a checked MXFP7 pack in the
vector lowering (L2.VC.028) with producers writing their consumer's packing (ring epilogues,
`KTPU_PRODUCER_PACKS`) and optimizer writers writing the weight's (`KTPU_WRITER_PACKS`),
fused rows members sharing a mapped read (`KTPU_FUSE_MAPPED`), the cost model priced from
machine facts, the N tile over every legal divisor and batched tiles where batches
underfill the clusters. Zoo inference is ViT and ViT-B (forward, batch 1); the other six
models train (batch 8).

| At mid, f1 → g1 | TinyJit ms, 2+2 | TinyJit M cycles, 2+2 | Eager M cycles, 2+2 | TinyJit ms, 4+2 | TinyJit M cycles, 4+2 |
|---|---|---|---|---|---|
| ViT-B | 264.9 → 263.7 | 49.5 → 49.3 | 49.2 → 49.0 | 232.9 → 248.6 | 44.6 → 46.5 (+4%) |
| ViT | 88.1 → 84.3 | 14.1 → 13.4 (−5%) | 14.1 → 13.3 | 79.9 → 81.1 | 12.5 → 12.8 (+2%) |
| GPT | 77.3 → 74.9 | 12.4 → 11.5 (−7%) | 11.8 → 11.3 | 78.0 → 74.4 | 12.4 → 11.5 (−7%) |
| LeNet | 259.8 → 102.4 | 20.9 → 17.2 (−18%) | 17.6 → 17.1 | 258.2 → 107.6 | 22.0 → 18.5 (−16%) |
| wide | 23.5 → 17.5 | 3.0 → 2.6 (−14%) | 2.9 → 2.5 | 21.8 → 19.9 | 3.0 → 2.6 (−13%) |
| Mixer | 318 (ordinary) → 61.9 | 9.8 → 8.7 (−11%) | 9.7 → 9.4 | 319 (ordinary) → 61.2 | 9.8 → 8.7 (−11%) |
| MLP | 9.5 → 8.7 | 1.1 → 0.9 (−17%) | 1.0 → 0.9 | 9.7 → 8.8 | 1.1 → 0.9 (−16%) |
| GRU | 35.0 → 32.7 | 4.0 → 3.6 (−10%) | 3.9 → 3.7 | 34.8 → 32.9 | 4.0 → 3.7 (−8%) |

| What | Size | Figure |
|---|---|---|
| TinyJit replay | Every TinyJit run records three or four calls then replays the rest: Mixer 7 replays of 11 calls, LeNet 8 (f1: 2 and 0); per-call outcomes in each run's `meta.json` `graphs.history`. Mixer's step 318 → 62 ms and LeNet's 259 → 102 ms are the replays coming back | `zoo.csv`, `data/*.g1j.*/meta.json` |
| Eager | Within ±2% of f1 except v9t8 ViT-B (+4.1% cadence) and GRU (+2.2%); every model stays host-bound, step 2.1× (ViT-B) to 14.0× (GRU) the device time | `zoo.csv` `cadence` |
| Inference | ViT −5% cycles on 2+2, ViT-B flat; on 4+2 ViT-B +4.2% (+1.87 M) and ViT +2.5% (+0.31 M). Cause: the packed FC1+GELU producer. On v9t8 ViT-B it grows 5.73 → 10.17 M and removes a 2.03 M quant, a 2.41 M boundary loss (ViT 0.38 M); MG work is unchanged, VC busy 8.5 → 14.4 M. On 2+2 the removed quant costs 4.76 M, so the same producer nearly breaks even. Batched P×V tiles save 0.51 M (ViT-B) and 0.07 M (ViT) (ISSUES 64) | `zoo.csv`, `mfu/*.csv`, `units.jsonl` |
| Producer packing | Off against on (`pp`, 2+2): off is cheaper for ViT-B (−0.6%) and dearer for ViT (+1.7%), others within 0.2%. The epilogue repeats the quant work on rounded f16 (unpack, classify, checked pack, status records) instead of removing it; no 4+2 `pp` run exists | variant rows |
| Writer packing | Off against on (`wp`): GRU +4.0%, Mixer +1.5%, others within 0.7%. Learning the demand records one more call, so a 12-step run replays once less (MLP 3 recorded 8 replayed off, 4 and 7 on). Replay lists are shorter with packing on: MLP 30 against 32 runs, GRU 229 against 258 | variant rows, `meta.json` |
| Mapped fusion | Off against on (`fm`): wide +13.5%, MLP +12.8%, GRU +5.9%, GPT +2.9%, Mixer +2.7%, LeNet +2.6% cycles | variant rows |
| Range fusion | On (`fr`, off by default): LeNet −10.8%, ViT −7.2%, ViT-B −3.9%, Mixer −2.3%, GPT −2.1% cycles; other changes within 0.2%. For ViT-B the QKV product absorbs its bias row kernel (3.97 + 1.28 → 3.29 M) | variant rows |
| Composed tree | On (`ct`, off by default): Mixer −25.5%, GPT −2.2% cycles; LeNet does not fill (a kernel over the 100 s compile bound) | variant rows, `compile.csv` |
| Chaining | On (`ch`, off by default): within ±0.3% cycles on every model | variant rows |
| Deferral | Off (`d0`): LeNet +5.9%, Mixer +8.2%; dead deferred forms made (`dd`): LeNet +17.3%, Mixer +9.3% | variant rows |
| Shared walk once | `ts` (`KTPU_THROUGH_SHARED=once`): LeNet +87%, wide +7.1%, GPT −4.2%; stays `compose` | variant rows |
| Compile | Fills from empty, eight models side by side, summed durations: 679 kernels 860.0 s on 2+2 (slowest 84.9 s), 680 kernels 882.6 s on 4+2 (88.8 s); TinyJit adds 43–44 kernels a board; variants add 8–108 | `rounds/g1/data/compile.csv` |
| Where ViT-B's step goes (v9t8 TinyJit, 46.5 M) | Packed FC1 21.9%; mapped Q/K/Vᵀ packs 20.5% (K→B pack wall 3.86 M against 1.96 M busy on each VC: jobs wait for every core after each pair); QKV product + bias 11.1%; softmax and norm output quants 9.2%; remaining matmul 17.9% | `mfu/vitb.g1j.multimesh_v9t8.1n.csv`, `units.jsonl` |
| Metrics | "TinyJit cycles" is the selected ordinary warm step; replay-list medians agree within 0.3% (ViT-B 4+2 +4.1% against +4.2%). ViT-B runs have two replay starts, so its cadence is one interval | `fig_zoo.py`, `card.jsonl` |
| Next | Every model runs on node 0 only. Next: tensor-parallel kernels over all four sysnodes (needs ISSUES 31 fixed so nodes overlap), the ViT-B batch-size sweep, producer packing admitted only when cheaper than producer plus quant (ISSUES 64), and range fusion on by default | |

## Round f1

32 of 32 runs exited: eight models, eager and TinyJit, 2+2 and 4+2, all four dies at
mid, the measure image, every launch an exact library hit. Tables and figures are under
`rounds/f1`; `rounds/f1/data/zoo.csv` holds every column below. The commit adds F-003's
ring path (every RING a firmware step), F-001's burst proof (`L2.MOV.020`: a copy with a
mover command not proven free of a chained one-beat write burst runs on the vector cores),
copy alias staging (`L2.MOV.021`), fused optimizer groups, deferred relayouts read
through by packings, range fusion and the per-product N tile (`KTPU_MM_N_TILE=auto`), and
a 22% faster cold compile. Defaults: fused norm/MLP chaining (`KTPU_MM_CHAIN`), range
fusion (`KTPU_MM_FUSE_RANGES`) and the composed column tree stay off.

| At mid, 2+2, TinyJit; eager | Cadence, ms, e1 → f1 | Device M cycles, e1 → f1 | MFU matrix, vector, memory, f1 | Bus | GFLOPS |
|---|---|---|---|---|---|
| ViT-B | 271.9 → 264.9; 521.9 → 523.3 | 51.30 → 49.49; 50.95 → 49.22 | 16.54, 13.50, 12.25%; 8.37, 6.84, 6.20% | 26.2%; 13.3% | 136.4; 69.0 |
| ViT | 91.7 → 88.1; 320.2 → 318.6 | 14.76 → 14.14; 14.70 → 14.10 | 3.60, 10.36, 6.71%; 0.99, 2.86, 1.86% | 17.6%; 4.9% | 30.1; 8.3 |
| GPT | 79.7 → 77.3; 452.5 → 362.5 | 12.48 → 12.39; 12.41 → 11.84 | 0.64, 4.90, 4.13%; 0.13, 1.05, 0.89% | 9.3%; 1.9% | 5.57; 1.11 |
| LeNet | 111.3 → 259.8; 326.7 → 344.0 | 19.17 → 20.87; 19.06 → 17.60 | 0.06, 0.67, 0.50%; 0.05, 0.50, 0.37% | 4.9%; 2.8% | 0.55; 0.41 |
| wide | 23.9 → 23.5; 79.7 → 75.8 | 3.16 → 3.00; 3.14 → 2.86 | 0.18, 7.08, 5.92%; 0.05, 2.19, 1.70% | 9.5%; 2.8% | 1.95; 0.53 |
| Mixer | 71.7 → 1,092.9; 497.5 → 411.5 | 10.30 → 9.84; 10.19 → 9.74 | 0.01, 0.17, 0.11%; 0.02, 0.45, 0.28% | 0.5%; 1.3% | 0.06; 0.16 |
| MLP | 9.6 → 9.5; 43.7 → 42.9 | 1.10 → 1.08; 1.08 → 1.01 | 0.13, 5.40, 4.29%; 0.02, 1.20, 0.85% | 7.1%; 1.4% | 1.41; 0.27 |
| GRU | 35.6 → 35.0; 281.8 → 253.6 | 4.18 → 4.04; 4.16 → 3.94 | 0.05, 1.04, 0.80%; 0.01, 0.14, 0.11% | 3.0%; 0.4% | 0.49; 0.07 |

| At mid, 4+2, TinyJit; eager | Cadence, ms, e1 → f1 | Device M cycles, e1 → f1 | MFU matrix, vector, memory, f1 | Bus | GFLOPS |
|---|---|---|---|---|---|
| ViT-B | 241.5 → 232.9; 504.0 → 494.4 | 45.00 → 44.61; 44.70 → 44.32 | 9.40, 15.36, 13.93%; 4.43, 7.23, 6.56% | 29.8%; 14.0% | 155.1; 73.1 |
| ViT | 83.5 → 79.9; 308.4 → 299.9 | 13.44 → 12.49; 13.37 → 12.43 | 1.98, 11.42, 7.40%; 0.53, 3.04, 1.97% | 17.4%; 4.6% | 33.2; 8.9 |
| GPT | 79.6 → 78.0; 443.9 → 358.8 | 12.44 → 12.38; 12.38 → 11.81 | 0.32, 4.86, 4.10%; 0.06, 1.06, 0.90% | 9.2%; 1.9% | 5.52; 1.12 |
| LeNet | 116.9 → 258.2; 342.7 → 352.7 | 20.30 → 21.97; 20.21 → 18.88 | 0.03, 0.67, 0.50%; 0.02, 0.49, 0.36% | 4.6%; 2.4% | 0.55; 0.40 |
| wide | 24.1 → 21.8; 79.6 → 75.5 | 3.16 → 2.98; 3.15 → 2.89 | 0.10, 7.62, 6.38%; 0.02, 2.20, 1.70% | 10.2%; 2.8% | 2.09; 0.53 |
| Mixer | 69.4 → 1,093.4; 498.5 → 407.0 | 10.24 → 9.76; 10.21 → 9.63 | 0.00, 0.17, 0.11%; 0.01, 0.46, 0.29% | 0.5%; 1.4% | 0.06; 0.16 |
| MLP | 9.7 → 9.7; 43.5 → 43.5 | 1.10 → 1.08; 1.09 → 1.02 | 0.06, 5.31, 4.23%; 0.01, 1.18, 0.84% | 7.0%; 1.4% | 1.39; 0.27 |
| GRU | 32.9 → 34.8; 280.6 → 252.7 | 4.14 → 4.01; 4.13 → 3.91 | 0.03, 1.05, 0.80%; 0.00, 0.14, 0.11% | 3.0%; 0.4% | 0.49; 0.07 |

| What | Size | Figure |
|---|---|---|
| Device cycles | Lower than e1 on 30 of 32 rows: ViT −4.0% (2+2) and −7.1% (4+2), ViT-B −3.4% and −0.9%, wide −5% to −9%, Mixer −4.5% to −5.7%, GRU −3% to −5%, GPT eager −4.6%, LeNet eager −7.7% and −6.6%, MLP eager −6.5%. LeNet TinyJit is +8.9% and +8.2% | `zoo.csv` `cycles`, against `rounds/e1/data/zoo.csv` |
| Eager cadence | GPT −20%, Mixer −17% to −18%, GRU −10%: the fused optimizer groups (MLP 156 → 114 launches modelled) and fewer launches a step | `zoo.csv` `cadence` |
| TinyJit regression | The replay graph records 2 replays in a Mixer run against 8 in e1, so most steps run as ordinary launches; with `KTPU_DEFERRED=0` (4+2) it records 8 replays, 1,387 launches (e1 8 and 1,887, f1 2 and 3,274). Mixer's 1,093 ms cadence is the one interval between its two replay starts, which encloses ordinary steps: it is not a step time. Its selected ordinary window spans 318.1 ms (2+2) and 319.2 ms (4+2) against e1's 69–72 ms replay cadence. LeNet's 259.8 / 258.2 ms matches its ordinary window (258.8 / 256.8 ms) against e1's 111–117 ms. Deferral changes graph state and lifetimes across calls; which graph comparison misses is not in the traces. Open (ISSUES 50) | `zoo.csv`; `mixer.fdef0j` trace on the host |
| LeNet TinyJit cycles | +1.70 M (2+2) and +1.67 M (4+2): a new `quant` kernel (2.18 M and 2.16 M, once a step) while the old conversion (2.92 M and 2.91 M) stays in the TinyJit step; in the eager step that conversion is gone (−2.94 M and −2.96 M convert), which is why eager falls and TinyJit rises | `rounds/f1/data/mfu/lenet.*`, against e1 |
| Outputs | Every run inside the MXFP7 footprint (`outside_footprint` 0 on all 32). Card against f32 and against simulated MXFP7, absolute max, mean, median, TinyJit on 2+2: ViT-B 0.0989, 0.0209, 0.0175 and 0.0972, 0.0213, 0.0180 (MXFP7 against f32 max 0.1040); ViT 0.0336, 0.0073, 0.0061 and 0.0461, 0.0082, 0.0070 (0.0437); GPT 0.0193, 0.0061, 0.0056 and 0.0105, 0.0022, 0.0015 (0.0163); LeNet 0.0150, 0.0043, 0.0034 and 0.0120, 0.0040, 0.0035 (0.0154); Mixer 0.0105, 0.0029, 0.0023 and 0.0063, 0.0021, 0.0017 (0.0129); wide 0.0037, 0.0008, 0.0006 and 0.0018, 0.0004, 0.0001 (0.0048); MLP 0.0060, 0.0021, 0.0019 and 0.0020, 0.0001, 0.0001 (0.0060); GRU 0.0025, 0.0007, 0.0006 and 0.0013, 0.0004, 0.0003 (0.0018) | `zoo.csv` `card_*`, `mxfp7_*` |
| Compile | Each model's library filled from empty, eager then TinyJit, eight models at a time, then refilled for the grouped kernels after the fused-group recording fix; seconds are summed compile durations of competing processes: 719 kernels, 822.6 s on 2+2, slowest 53.4 s; 718 kernels, 834.7 s on 4+2, slowest 55.3 s (both a Mixer kernel). Single-threaded cold compile of e1's 669 distinct kernels: 1,434.8 → 1,118.8 s CPU, every output byte-identical | `rounds/f1/data/compile.csv` |
| Review | Astra's review of the batch's code: a copy joined into a fused loop skips the alias test (ISSUES 51), TinyJit can restore a released deferred form (52), the F-001 bind guard refuses legal views (53), nested chaining names collide (54), raw ENGINE RINGs bypass F-003 credits (55), launch event identity is not reentrant (56), F-001 handling covers proven affine copies only (57). `test_phases.py` bounds a fused cost from above only, and `test_fused_groups.py` compares values on an offline device that does not execute. None is attributed to a round f1 output | `ISSUES.md` |
| Next | Step shares, TinyJit 2+2: form packing (`quant` + `convert`) 70% of LeNet, 53% GRU, 42% Mixer, 40–43% GPT, ViT, ViT-B; vector work 67% MLP, 66% wide, 51% Mixer; matrix 34% ViT, 38% ViT-B, under 6% LeNet and Mixer. Order: deferred forms as graph effects and per-call measurement identity, then form packing, vector fusion after row 51, matrix tiling. Chaining, range fusion and the composed tree stay off until measured on and off | `rounds/f1/data/mfu/` |

Warm steps are found over single kernel launches; a TinyJit replay list is one
row of a recorded step's kernels and is counted in cadence, not in the per-kernel MFU.

## Round e1

32 of 32 runs exited: eight models, eager and TinyJit, 2+2 and 4+2, all four dies at
mid, the measure image, every launch an exact library hit. Tables and figures are under
`rounds/e1`; `rounds/e1/data/zoo.csv` holds every column below. The commit carries the
silicon flaw registry (`docs/spec/stack/flaws.md`): each handling is switched on by the
flaw it handles, and F-001's handling is the vector-core copy of `L2.MOV.020`.

| At mid, 2+2, TinyJit; eager | Cadence, ms, d2 → e1 | Device M cycles, d2 → e1 | MFU matrix, vector, memory, e1 | Bus | GFLOPS |
|---|---|---|---|---|---|
| ViT-B | 271.5 → 271.9; 531.0 → 521.9 | 51.22 → 51.30; 50.87 → 50.95 | 16.11, 13.15, 11.93%; 8.39, 6.85, 6.22% | 26.3%; 13.7% | 132.8; 69.2 |
| ViT | 89.7 → 91.7; 317.2 → 320.2 | 14.75 → 14.76; 14.73 → 14.70 | 3.46, 9.95, 6.45%; 0.99, 2.85, 1.85% | 18.0%; 5.2% | 28.9; 8.3 |
| GPT | 80.1 → 79.7; 452.1 → 452.5 | 12.48 → 12.48; 12.42 → 12.41 | 0.58, 4.76, 4.56%; 0.10, 0.84, 0.80% | 9.0%; 1.6% | 5.06; 0.89 |
| LeNet | 111.2 → 111.3; 332.4 → 326.7 | 19.12 → 19.17; 19.01 → 19.06 | 0.14, 1.56, 1.24%; 0.05, 0.53, 0.42% | 11.2%; 3.8% | 1.28; 0.44 |
| wide | 24.0 → 23.9; 80.2 → 79.7 | 3.16 → 3.16; 3.13 → 3.14 | 0.15, 6.95, 7.41%; 0.05, 2.08, 2.22% | 10.0%; 3.0% | 1.68; 0.50 |
| Mixer | 70.3 → 71.7; 506.3 → 497.5 | 10.30 → 10.30; 10.19 → 10.19 | 0.09, 2.60, 1.75%; 0.01, 0.37, 0.25% | 7.8%; 1.1% | 0.93; 0.13 |
| MLP | 9.6 → 9.6; 43.4 → 43.7 | 1.09 → 1.10; 1.08 → 1.08 | 0.11, 5.36, 5.57%; 0.02, 1.18, 1.22% | 7.6%; 1.7% | 1.22; 0.27 |
| GRU | 36.2 → 35.6; 284.6 → 281.8 | 4.18 → 4.18; 4.16 → 4.16 | 0.05, 1.01, 0.81%; 0.01, 0.13, 0.10% | 2.9%; 0.4% | 0.48; 0.06 |

| At mid, 4+2, TinyJit; eager | Cadence, ms, d2 → e1 | Device M cycles, d2 → e1 | MFU matrix, vector, memory, e1 | Bus | GFLOPS |
|---|---|---|---|---|---|
| ViT-B | 241.8 → 241.5; 505.0 → 504.0 | 45.02 → 45.00; 44.78 → 44.70 | 9.07, 14.81, 13.44%; 4.35, 7.10, 6.44% | 29.6%; 14.2% | 149.6; 71.7 |
| ViT | 84.6 → 83.5; 310.5 → 308.4 | 13.45 → 13.44; 13.39 → 13.37 | 1.90, 10.93, 7.08%; 0.51, 2.96, 1.92% | 18.3%; 4.9% | 31.8; 8.6 |
| GPT | 79.9 → 79.6; 445.4 → 443.9 | 12.44 → 12.44; 12.40 → 12.38 | 0.29, 4.76, 4.57%; 0.05, 0.85, 0.82% | 8.9%; 1.6% | 5.06; 0.91 |
| LeNet | 117.2 → 116.9; 345.7 → 342.7 | 20.37 → 20.30; 20.17 → 20.21 | 0.07, 1.48, 1.19%; 0.02, 0.51, 0.40% | 9.9%; 3.4% | 1.22; 0.42 |
| wide | 24.6 → 24.1; 79.1 → 79.6 | 3.15 → 3.16; 3.14 → 3.15 | 0.07, 6.88, 7.34%; 0.02, 2.09, 2.22% | 9.8%; 3.0% | 1.66; 0.50 |
| Mixer | 70.1 → 69.4; 492.4 → 498.5 | 10.24 → 10.24; 10.15 → 10.21 | 0.05, 2.68, 1.81%; 0.01, 0.37, 0.25% | 8.0%; 1.1% | 0.96; 0.13 |
| MLP | 9.7 → 9.7; 43.8 → 43.5 | 1.10 → 1.10; 1.09 → 1.09 | 0.05, 5.30, 5.51%; 0.01, 1.18, 1.23% | 7.5%; 1.7% | 1.21; 0.27 |
| GRU | 33.0 → 32.9; 282.0 → 280.6 | 4.15 → 4.14; 4.14 → 4.13 | 0.03, 1.10, 0.88%; 0.00, 0.13, 0.10% | 3.1%; 0.4% | 0.52; 0.06 |

The 4+2 matrix MFU is about half the 2+2 figure at the same cycles because its floor
counts twice the clusters.

| What | Size | Figure |
|---|---|---|
| Device cycles | Every row is within 0.6% of d2's, on both boards: the flaw handlings cost no cycles a step that the card can see | `rounds/e1/data/zoo.csv` `cycles`, against `rounds/d2/data/zoo.csv` |
| Cadence | Within 2.2% of d2's on every row: the host and the card did what they did in d2 | `zoo.csv` `cadence` |
| Host in a step | Eager steps at mid on 2+2 are 1.9× (ViT-B) to 7.9× (GRU) their TinyJit cadence. `gap_ms` is not a replay host gap: the launch log holds ordinary submissions only, and `runs.py` joins it to card rows by index, so a TinyJit row's gap holds the replay lists between two ordinary launches (Mixer 913 ms, LeNet 1,096 ms in one such gap on 2+2) | `zoo.csv` `gap_ms`, `cadence` |
| Outputs | Every run inside the MXFP7 footprint (`outside_footprint` 0 on all 32). Card against f32 and against simulated MXFP7, absolute max, mean, median, on 2+2: ViT-B 0.0989, 0.0209, 0.0175 and 0.0972, 0.0213, 0.0180 (MXFP7 against f32 max 0.1040); ViT 0.0336, 0.0073, 0.0061 and 0.0461, 0.0082, 0.0070 (0.0437); GPT 0.0193, 0.0061, 0.0056 and 0.0105, 0.0022, 0.0015 (0.0163); LeNet 0.0150, 0.0043, 0.0034 and 0.0120, 0.0040, 0.0035 (0.0154); Mixer 0.0105, 0.0029, 0.0023 and 0.0063, 0.0021, 0.0017 (0.0129); wide 0.0049, 0.0011, 0.0007 and 0.0017, 0.0004, 0.0002 (0.0051); MLP 0.0060, 0.0021, 0.0019 and 0.0020, 0.0001, 0.0001 (0.0060); GRU 0.0025, 0.0007, 0.0006 and 0.0013, 0.0004, 0.0003 (0.0018). Eager and TinyJit give the same values; 4+2 is in `zoo.csv` | `zoo.csv` `card_*`, `mxfp7_*` |
| Compile | The library was filled from empty, eager and TinyJit two processes at a time on one library, so each kernel compiled once in whichever process reached it first; the seconds are summed compile durations of competing processes, not wall time: 1,043 kernels, 1,780.0 s on 2+2, slowest 58.7 s (a Mixer kernel); 869 kernels, 847.9 s on 4+2, slowest 31.3 s (a wide kernel) | `rounds/e1/data/compile.csv` |

Cost of each flaw's handling a warm step, from the timed model: every kernel of the step
that moves data is compiled for the machine as built and for the same machine with that
one flaw marked absent, and both are timed (`software/tests/perf/flaws.py`, one
`flaws.<board>.json` beside each eager trace of the shipping image, kept on the host
under `build/perf/traces/*.e1n.*`). Only F-001 changes code the timed model reads.

| F-001 handling, a warm step | Kernels, launches | Cycles, 2+2; 4+2 | Share of the step, 2+2; 4+2 |
|---|---|---|---|
| MLP | 2, 2 | +9,384; +9,384 | +1.18%; +1.16% |
| wide | 2, 2 | +17,064; +17,064 | +0.77%; +0.64% |
| Mixer | 1, 3 | −77,873; −77,873 | −0.80%; −0.74% |
| LeNet | 9, 13 | −326,499; −326,499 | −1.76%; −1.69% |
| GPT, ViT, ViT-B, GRU | 0 | 0 | 0 |

A negative cost is a vector-core copy the model prices below the mover copy it replaces,
whose destination steps one-word runs. The card's step cycles above do not resolve
either sign. F-002 (L1 image encoder), F-003 and F-004 (package engine), F-005 to F-007
(KohakuAccel OS and firmware) are handled below what the timed model reads.

Row 31 (`ISSUES.md`) with per-node mover canaries, v9t6 at mid, four nodes: 44 of about
400 node results over 100 overlapped rounds differ from the serial bytes; every node's
canary is right before overlap and after every differing round; all 204 packages read back
right. Details in the row.

## Round d2

96 of 96 runs exited, the same matrix as d1: eight models, low, mid and ship, eager and
TinyJit, 2+2 and 4+2, all dies at one clock, every launch an exact library hit. Tables and
figures are under `rounds/d2`. The matrix MFU's floor is the matrix core's physical rate,
so d1's matrix MFU times 0.7246 is the like-for-like figure; d1's column below is that
product. Vector flops count the vector math beats a run issues, so a kernel that does its
work in fewer beats lowers its own vector MFU.

| At mid, 2+2, TinyJit; eager | Cadence, ms, d1 → d2 | Device M cycles, d1 → d2 | MFU matrix, vector, memory, d2 | Bus | GFLOPS |
|---|---|---|---|---|---|
| ViT-B | 661.3 → 271.5; 877.1 → 531.0 | 129.6 → 51.2; 131.7 → 50.9 | 16.14 (6.81), 13.17, 11.95%; 8.25, 6.74, 6.11% | 26.3%; 13.4% | 133.0; 68.0 |
| ViT | 146.3 → 89.7; 365.7 → 317.2 | 25.6 → 14.8; 25.7 → 14.7 | 3.53 (2.22), 10.17, 6.59%; 1.00, 2.88, 1.86% | 18.4%; 5.2% | 29.6; 8.4 |
| GPT | 126.5 → 80.1; 533.2 → 452.1 | 18.8 → 12.5; 18.7 → 12.4 | 0.58 (0.36), 4.73, 4.54%; 0.10, 0.84, 0.80% | 8.9%; 1.6% | 5.03; 0.89 |
| LeNet | 124.4 → 111.2; 373.8 → 332.4 | 21.7 → 19.1; 21.7 → 19.0 | 0.14 (0.14), 1.56, 1.25%; 0.05, 0.52, 0.42% | 11.2%; 3.7% | 1.28; 0.43 |
| wide | 99.4 → 24.0; 173.2 → 80.2 | 15.2 → 3.16; 15.4 → 3.13 | 0.15 (0.04), 6.91, 7.37%; 0.04, 2.07, 2.21% | 9.9%; 3.0% | 1.67; 0.50 |
| Mixer | 86.1 → 70.3; 582.0 → 506.3 | 11.9 → 10.3; 11.9 → 10.2 | 0.10 (0.08), 2.65, 1.78%; 0.01, 0.37, 0.25% | 7.9%; 1.1% | 0.95; 0.13 |
| MLP | 35.6 → 9.6; 85.6 → 43.4 | 5.98 → 1.09; 5.98 → 1.08 | 0.11 (0.03), 5.37, 5.58%; 0.02, 1.19, 1.23% | 7.6%; 1.7% | 1.23; 0.27 |
| GRU | 33.6 → 36.2; 345.7 → 284.6 | 4.31 → 4.18; 4.35 → 4.16 | 0.05 (0.04), 1.00, 0.80%; 0.01, 0.13, 0.10% | 2.9%; 0.4% | 0.47; 0.06 |

The bracket is d1's TinyJit matrix MFU on d2's floor. `rounds/d2/data/compare.csv` compares
against the 56021fd4 baseline runs (`.z`), not d1; the d1 columns here come from
`rounds/d1/data/zoo.csv`. The measurement firmware image turns list chaining off, so these
runs measure resident lists, not the resident chain.

| Bottleneck | Size | Figure |
|---|---|---|
| Device cycles | Every model's TinyJit step fell at mid on 2+2: MLP 0.18×, wide 0.21×, ViT-B 0.40×, ViT 0.58×, GPT 0.66×, Mixer 0.86×, LeNet 0.88×, GRU 0.97×. Eager steps fall by the same ratios. 4+2 matches: ViT-B 45.0 M against d1's 126.6 M | `rounds/d2/data/zoo.csv` `cycles` |
| Host in a TinyJit step | GRU's TinyJit cadence rose 33.6 to 36.2 ms at mid (29.4 to 33.0 on 4+2, 23.6 to 26.7 at ship) while its cycles fell 3%; Mixer at ship held at 55.2 ms against 54.5 while its cycles fell 14%. The host, not the card, sets these steps | `zoo.csv` `cadence`, `gap_ms` |
| Host in an eager step | Eager steps are 2.0× (ViT-B) to 7.9× (GRU) their TinyJit cadence at mid: GRU 284.6 ms for 4.16 M cycles, Mixer 506.3 for 10.2 M, GPT 452.1 for 12.4 M | `zoo.csv` |
| 4+2 against 2+2 | ViT-B TinyJit 241.8 ms against 271.5, 45.0 M cycles against 51.2: 5.82 M of the 6.20 M difference is `quant` (4+2 packs unpaired, nk 1), matrix launches 0.38 M; QK spreads heads over the clusters, PV runs every head on MG 0. ViT 13.4 M against 14.8 M; LeNet 20.4 M against 19.1 M, slower: one composed pack costs 2.25 M on 4+2 against 1.13 M for 2+2's relayout and pack; the rest within 1% | `zoo.csv`, `data/vitb.d2j.*`, `data/lenet.d2j.*` |
| Clock | TinyJit ms at low, mid, ship on 2+2: ViT-B 466, 272, 211; ViT 153, 90, 69; GPT 137, 80, 62; LeNet 189, 111, 86; Mixer 114, 70, 55; wide 37, 24, 18; GRU 57, 36, 27; MLP 16, 10, 8 | `zoo.csv` |
| Compile | The library was filled over three fill rounds, so this is not a fill from empty: the last fill compiled 167 kernels in 201.4 s over 14 runs, slowest 19.6 s (a LeNet kernel), and the Mixer fix 1 kernel a board | `rounds/d2/data/compile.csv` |
| Outputs | Largest error against CPU f32 over the largest f32 logit: every ViT run 0.042, ViT-B 0.061 on 2+2 and 0.057 on 4+2 (d1 0.073 and 0.063), LeNet 0.018 to 0.022 with eager at mid on 2+2 0.037, Mixer 0.028 on 2+2 and 0.022 on 4+2 (d1 0.036 and 0.027, before `ISSUES.md` 45). "Output statistics" above sets each model against simulated MXFP7 | `rounds/d2/data/zoo.csv` |

## Round d1

96 of 96 runs exited: eight models, ViT-B added, low, mid and ship, eager and TinyJit,
2+2 and 4+2, all dies at one clock, every launch an exact library hit. ViT and ViT-B run
12 blocks; ViT-B TinyJit runs 6 inputs, because a graph replays only after two equal
records. Tables and figures are under `rounds/d1`. Flops are 2 per MAC plus 16 per vector
math beat; MFU is over the cadence, matrix and vector against their math-only floors,
memory as useful bytes against 36 bytes a node cycle; `bus` is the bytes the bus moved.

| At mid, 2+2 | Cadence, ms | Device M cycles | MFU matrix, vector, memory | Bus | GFLOPS |
|---|---|---|---|---|---|
| ViT-B TinyJit; eager | 661.3; 877.1 | 132.3; 131.7 | 9.40, 10.37, 5.45%; 7.09, 7.82, 4.11% | 14.9%; 11.2% | 56.5; 42.6 |
| ViT | 146.3; 365.7 | 29.3; 25.7 | 3.06, 11.15, 4.11%; 1.22, 4.46, 1.65% | 11.0%; 4.4% | 18.9; 7.6 |
| GPT | 126.5; 533.2 | 25.3; 18.7 | 0.50, 5.21, 2.87%; 0.12, 1.24, 0.68% | 6.1%; 1.5% | 3.33; 0.79 |
| LeNet | 124.4; 373.8 | 24.9; 21.7 | 0.19, 1.80, 1.11%; 0.06, 0.60, 0.37% | 9.2%; 3.1% | 1.24; 0.41 |
| wide | 99.4; 173.2 | 19.9; 15.4 | 0.05, 4.35, 1.78%; 0.03, 2.49, 1.02% | 5.2%; 3.0% | 0.57; 0.33 |
| Mixer | 86.1; 582.0 | 17.2; 11.9 | 0.11, 3.69, 1.46%; 0.02, 0.55, 0.22% | 7.9%; 1.2% | 0.87; 0.13 |
| MLP | 35.6; 85.6 | 7.1; 6.0 | 0.04, 4.37, 1.50%; 0.02, 1.82, 0.62% | 5.6%; 2.3% | 0.52; 0.22 |
| GRU | 33.6; 345.7 | 6.7; 4.4 | 0.06, 1.17, 0.82%; 0.01, 0.11, 0.08% | 2.4%; 0.2% | 0.41; 0.04 |

Device M cycles are a TinyJit cadence and an eager step's device cycles. Every eager step's
device cycles equal c1's within 2%, on both boards and at all three clocks: this commit
changed correctness, not speed.

| Bottleneck | Size | Figure |
|---|---|---|
| 4+2 against 2+2 | TinyJit cadence the same at mid on every model (ViT-B 131.7 against 132.3 M); matrix MFU halves, ViT-B 4.72% against 9.40%. The two extra clusters do nothing | `rounds/d1/data/zoo.csv` |
| Clock | TinyJit ms at low, mid, ship on 2+2: ViT-B 1178, 661, 494; ViT 269, 146, 126; GPT 208, 127, 85; LeNet 213, 124, 95; wide 160, 99, 79; Mixer 147, 86, 55; MLP 79, 36, 28; GRU 59, 34, 24. Cycles rise with the clock (ViT 26.9, 29.3, 37.9 M): DRAM does not scale | `zoo.csv` |
| Host in an eager step | The host holds 25% (ViT-B) to 94% (GRU) of a step: GRU 346 ms against 33.6 TinyJit, Mixer 582 against 86.1, GPT 533 against 126.5 | `zoo.csv` `idle`, `gap_ms` |
| 4+2 eager cadence | Not comparable: these 24 runs shared the host with compiler builds, and their cadence rose (Mixer 1404 ms against 582 on 2+2) while device cycles stayed at c1's. A clean figure needs a host with nothing else on it | `zoo.csv` `cycles`, `cadence` |
| Compile | 16 library fills, 3,223.7 s; 2+2 LeNet 475.5 s for 428 kernels, GPT 369.8, Mixer 303.6, wide 226.2, ViT-B 165.2, ViT 150.2, MLP 147.0, GRU 104.4; slowest kernel 72.1 s | `rounds/d1/data/compile.csv` |
| Outputs | Largest error against CPU f32 over the largest f32 logit: every ViT run 0.042, ViT-B 0.073 on 2+2 and 0.063 on 4+2, Mixer 0.036 on 2+2 and 0.027 on 4+2. Eager and TinyJit give the same error. ViT and ViT-B traced stage by stage stay at the format's error, `ISSUES.md` 36 | `rounds/d1/data/zoo.csv` |

## Round c1

84 of 84 runs exited: seven models, low, mid and ship, eager and TinyJit, 2+2 and 4+2,
all dies at one clock, every launch an exact library hit. ViT runs 12 blocks for 9 steps.
Tables and figures are under `rounds/c1`.

| Warm step at mid | Baseline | c1 | Ratio | Figure |
|---|---|---|---|---|
| LeNet, M cycles, 2+2; 4+2 | 99.7; 107.4 | 21.7; 22.8 | 0.22×; 0.21× | `compare/cycles` |
| ViT TinyJit | 80.2; 90.5 | 25.5; 25.4 | 0.32×; 0.28× | `compare/cycles` |
| GPT | 30.7; 35.3 | 18.7; 18.3 | 0.61×; 0.52× | `compare/cycles` |
| MLP | 9.3; 9.9 | 6.0; 6.0 | 0.64×; 0.61× | `compare/cycles` |
| wide | 22.3; 23.7 | 15.1; 15.2 | 0.68×; 0.64× | `compare/cycles` |
| GRU | 5.4; 10.4 | 4.3; 4.2 | 0.81×; 0.40× | `compare/cycles` |
| Mixer | 13.5; 14.1 | 11.9; 11.7 | 0.88×; 0.83× | `compare/cycles` |

ViT's eager mode has no baseline row. 4+2 now costs what 2+2 costs in device cycles, so
it is no longer slower, and the two extra clusters still gain nothing.

| Bottleneck | Size | Figure |
|---|---|---|
| A TinyJit step, node cadence | ms from one step's first launch to the next's at mid, 2+2, c1 against the baseline: GRU 30.3, 75.6; Mixer 85.7, 108.5; MLP 33.6, 50.1; wide 99.3, 115.8; GPT 126.1, 167.7; LeNet 124.0, 513.4; ViT 145.7, 802.3. ViT's baseline step ran another kernel set | `compare/replay`, `compare.csv` |
| An eager step, node span | ms at mid, 2+2: GRU 468 to 346, Mixer 652 to 584, GPT 759 to 534, LeNet 755 to 377, wide 359 to 173, MLP 111 to 86. The host still holds most of a step on GRU and Mixer | `compare.csv` |
| Node idle in an eager step at mid, 2+2 | GRU 94%, Mixer 90%, GPT 82%, LeNet 71%, ViT 65%, MLP 65%, wide 56%. Cycles fell faster than the host's time, so the shares rise | `compare.csv` |
| MFU over the cadence at mid, 2+2, matrix, vector, memory | Eager: ViT 1.21, 4.41, 1.63%; wide 0.03, 2.50, 1.02%; MLP 0.02, 1.81, 0.62%; GPT 0.12, 1.24, 0.68%; LeNet 0.06, 0.59, 0.37%; Mixer 0.02, 0.54, 0.21%; GRU 0.01, 0.11, 0.08%. TinyJit: ViT 3.07, 11.19, 4.13%; GPT 0.51, 5.23, 2.88%; MLP 0.04, 4.63, 1.59%; wide 0.05, 4.35, 1.78%; Mixer 0.11, 3.70, 1.46%; LeNet 0.19, 1.80, 1.11%; GRU 0.06, 1.30, 0.91% | `rounds/c1/data/zoo.csv`, `data/mfu/` |
| OS replay, fused and retained, against firmware | 2+2 mid: ViT 159.9 k against 261.1 k (0.61×), LeNet 812.9 k against 739.4 k (1.10×). 4+2 mid: ViT 136.5 k against 225.3 k (0.61×), LeNet 912.7 k against 761.3 k (1.20×). Outputs byte-identical on every path. Stamps turn fusion off, so a stamped replay is another algorithm: ViT 633.5 k, LeNet 1,863.9 k, not a constant added to these rows. Firmware rows are package timers on 24 and 45 submissions and leave out the constant writes the OS rows carry, so the ratios are no pure OS cost | `data/os/c1.*`, `c1t8.*` |
| Four-die OS commands | v9t6 at mid: 12 commands, 0 failed; `kdsmulti` 1.70 M cycles, `kdschain` 56.1 k a step, `kdsreduce` 293 k, `pipeline` 6.2 k an item. `preduce` and `pmatmul` report that they need shared memory and did not run. v9t8: the load times out at low and mid, `ISSUES.md` 21 | `data/os/c1.4n` |
| Four nodes, ViT at 2 blocks, mid, v9t6 | TinyJit 30.2 ms a request on one node, 29.1 resident, 31.4 sharded; eager 70.1, 74.0, 73.7; error 0.029 in all of them. Nodes take turns, so about 1×; overlap is off, `ISSUES.md` 31 | `nodes.c1`, `nodes.c1e` |
| ViT passes under the debug image | TinyJit, 1,089 launches: `dsl` 324.5 M, `quant` 168.4 M, `matmul` 68.4 M, `move` 3.4 M; per-pass stamps in `steps/vit.json`. Those cycles are inflated by the image's step records, so they show shape, not size | `rounds/c1/data/steps/vit.json` |

Direction targets of the batch, unmeasured when set: ViT under 39 M, met at 25.5 M; MLP
about 5.5 M, reached 6.0 M; wide about 14.7 M, reached 15.1 M; GRU 4+2 about 5.4 M, met
at 4.2 M; LeNet about 65 M, met at 21.7 M; GRU step below 75 ms, met at 30.4 ms TinyJit,
eager still 346 ms; Mixer step below 109 ms, met at 86.1 ms TinyJit, eager still 584 ms;
four independent nodes above 1×, not met safely.

## Host

| Bottleneck | Size | Figure |
|---|---|---|
| Node idle waiting for the host, eager | GRU 94%, Mixer 90%, GPT 80%, wide 69%, MLP 58%, ViT 50%, LeNet 34% of a step's span | `zoo/shares`, `zoo/wall` |
| Eager step against TinyJit, node span | GRU 468 to 75 ms, Mixer 652 to 108, GPT 759 to 331, wide 359 to 115, MLP 111 to 50, LeNet 755 to 513. Step picked by run cycles. ViT has no eager row: `vit_tiny.py` ignored the mode, so both ViT rows are one TinyJit path, 2,024 launches of the same names | `zoo/wall` |
| Whole run stays host-bound at every clock | wall seconds eager at low, mid, ship: LeNet 42.9, 37.6, 34.7; ViT 13.1, 12.6, 12.2; GPT 16.1, 15.9, 15.5. Three times the clock saves under 20% | `zoo/wall_matrix` |
| Bind of one large kernel | `t_86c2cde33b`, 5.0 MB of L2 and 1.5 MB of L1, submits in 1,348 to 1,364 ms on a new address set against 8.4 to 11.7 ms settled, at launches 422, 671, 1418, 1916: 5.4 s over ten warm steps. Under cProfile, offline, `_rs.bind` takes 4.0 s of a two-step run. Four of LeNet's ten warm steps span about 405 M against 150 M, LeNet-BN eight of ten | `zoo/kinds`, `zoo/shares` |
| LeNet host time under cProfile | tinygrad 346 ms, Python 280, bind 182, XDMA 162, ktpugrad 107, runtime 106 | `runs/lenet.zp/path` |
| Cold first step, LeNet at low under cProfile | 26.9 s for 303 launches: Rust 14.7 s, Python 4.8, runtime 2.8, driver 0.7, tinygrad 0.7, ktpugrad 0.2 | `data/cold` |
| Compile | one `rows_` kernel 21 to 81 s; ResNet-S, ConvNet-W and UNet libraries never finished inside a 105 s round | `compiler/stages`, `ISSUES.md` 9 |

## Firmware and seam

| Bottleneck | Size | Figure |
|---|---|---|
| Firmware around a launch | 1.3 k cycles a launch, median of MLP at low; under 1% of a step except GRU 8%, Mixer 4%, GPT 1.8% | `firmware/dispatch_paths` |
| One tinygrad step on the OS path against firmware, 2+2 at mid. Firmware rows exclude the constant writes the OS rows carry, so the multiples are upper bounds on OS overhead | ViT, 40 launches: firmware 790 k, OS graph 1,366 k (1.73×), retained 1,120 k (1.42×), fused 814 k (1.03×), fused and retained 800 k (1.01×). ViT-QKV: 162 k, 257 k, 203 k, 170 k, 151 k (0.93×). Low and ship keep the ratios: ViT firmware 759 and 822 k, OS graph 1,328 and 1,405 k | `firmware/replay_paths`, `firmware/replay_paths_22` |
| The same step on 4+2 | ViT, 48 launches, at low, mid, ship: firmware 973, 1,041, 1,109 k; OS graph 2,068, 2,152, 2,236 k (2.1×); retained 1,565, 1,633, 1,705 k; fused and retained 1,121, 1,185, 1,248 k (1.14×). ViT-QKV at mid: 174, 325, 253, 165 k (0.95× fused and retained) | `firmware/replay_paths_42` |
| OS seam between launches | about 5 k cycles stamped, each stamp itself about 11 k; a step's OS overhead is 14 k a launch, 8 k retained, under 1 k fused | `firmware/replay_seam` |
| A launch on each OS path, one die | MG engine round 3.9 k, mailbox round 5.8 to 6.7 k, dispatcher independent task 5.1 k, chain task 6.6 k, one-task graph 25.2 k, MG round through the dispatcher 37.5 to 38.4 k, against firmware 1.2 k | `firmware/dispatch_paths` |
| Kernel trap path | null call 469 cycles, 209 in the call; send 791 in the call; yield 250 in the call, 252 polling, 187 scheduling | `firmware/os_kernel_stages` |
| OS against DDR4 | 2 times the clock leaves syscall and yield at 1.00× in cycles, chain task 1.03×, one-task graph 1.06×, mmap 1.04×; the OS is core work, not DDR4 | `firmware/os_clock` |
| OS on four dies, near against far, mid | atomic 1,044 here, 3,773 on another die; channel round trip 11.3 k; barrier 8.7 k; MG mailbox round 16.4 k against 6.8 k; dispatcher chain task 7.6 k, a hop to another die 23 k, a four-node chain step 56 k. Clock leaves all of these within 3% | `firmware/os_four_dies` |
| Workloads on four OS workers | reduce 3.75× at low, 3.22× at mid; CPU matmul 2.94× and 2.56×; the shared-memory work loses speedup as the clock rises | `firmware/os_four_dies` |
| Seam between launches, firmware path | the detector names under 1% of a modelled step in every run | `waste/recoverable` |

## Node, by kind of work

| Bottleneck | Size | Figure |
|---|---|---|
| Node running, no compute unit busy | LeNet 51%, GRU 44%, Mixer 42%, ViT 40%, GPT 20%, wide 11%, MLP 10% of run cycles; on 4+2 LeNet 53%, ViT 74%, GRU 60%, Mixer 53%, GPT 41%, wide 20%, MLP 17% | `zoo/shares` |
| Work is on the vector cores | `dsl`, `quant` and `convert` run on the two vector cores; matrix clusters are busy only inside `matmul`, under 10% of any step. ViT 2.2 and 1.3 M of 80.2 M, GPT 0.7 M of 30.7 M, LeNet 0.3 M of 99.7 M | `zoo/unit_share` |
| LeNet `convert` | 70.8 M of 99.7 M; the largest kernel is 28.4 M. It lays out one read of a convolution's backward product, `sum(w[6, 5, 5, 16] * g[8, 16, 10, 10])`, which the planner refused as a matmul because N spans several ranges | `runs/lenet.z/kinds` |
| A contraction with N over several ranges runs as rows | LeNet's plan above: 92.5 M of 199.6 M over its first 503 launches. GPT's `sum(w[2, 64, 128] * x[8, 28, 128])`: 24.3 M of 63.1 M over its first 904 | `lenet.tr3`, `gpt.tr3` joined to `zma` |
| ViT form work | `quant` 35.8 M and `transpose` 20.0 M of 80.2 M | `runs/vit.z/kinds` |
| Math against the step | math floor over run cycles: LeNet 0.7%, GRU 0.7%, MLP 3.2%, ViT 3.4%, wide 3.7%, Mixer 5.7%, GPT 5.8%. MFU floor over span, vector cores: LeNet 0.5%, ViT 1.8%, GPT 1.3%, MLP 1.4%, wide 1.2%, Mixer 0.6%, GRU 0.1%; matrix clusters at most 0.54% | `waste/ladder`, `zoo/mfu` |
| A value made again in a form already held, by the detector | Mixer 5.5% of model cycles, GPT 3.6%, ViT 1.8%, GRU 1.5%, others under 1%; GPT under TinyJit 0.5%. The detector compares dtype and lineage only, not view or pack geometry, so it misses ViT's weights | `waste/recoverable` |
| ViT weights re-formed for every input | 49 transposes and 49 packs repeat on each input, 41.12 M of 80.18 M, on 49 source addresses that no launch writes | `vit.zr` |
| Batch does not move an MLP step | 9.3 M at 8, 9.4 M at 32, 10.0 M at 128: per-step fixed work dominates | `zoo/knobs` |
| Stack knobs off, LeNet | prepared packages +15%, composed path +17% of run cycles | `zoo/knobs` |

## Memory

| Bottleneck | Size | Figure |
|---|---|---|
| 64-byte stride against contiguous | warm read 26.2 against 2.8 cycles a word at every clock, cold 43.4 against 4.8 at mid; write 12.6 against 1.8 | `memory/grid_warm` |
| Past the cache | 64 MB read has warm equal to cold: contiguous 4.1, 4.8, 5.5 and 64-byte stride 37.5, 43.3, 48.9 at low, mid, ship | `memory/grid_warm` |
| Sparse pages | 64 KB stride over 256 KB, cold: 660, 697.5, 715 cycles a read word at low, mid, ship, about 46% of it launch overhead on so few words | `memory/grid_cold` |
| Clock | 3 times the clock gives 2+2 a warm step of LeNet 846 to 391 ms, ViT 730 to 293, MLP 90 to 32, Mixer 121 to 50, GRU 50 to 19, GPT 263 to 118, wide 212 to 79: 2.2 to 2.8×, not 3× | `zoo/clocks22`, `memory/clock_scaling` |
| Idle dies change node 0's cycles | Mixer step with node 0 at mid: others low 16.95 M, node 1 ship and others low 15.16 M, others mid 13.55 M, others ship 12.54 M. The idle-die clocks are inferred from the steps run: these runs carry no clock readback. Why an idle die reaches node 0's memory path is not known | `zoo/idle_dies`, `ISSUES.md` 27 |

## Scale

| Bottleneck | Size | Figure |
|---|---|---|
| Four nodes take turns | ViT, 2 blocks, TinyJit, mid: 30.2 ms a request on one node, 29.1 with a resident copy on each node (1.04×), 31.4 sharded by batch range (0.96×), error 0.029 in all three. Overlapping nodes gave 1.42 to 1.77× resident but failed about a third of runs, cause not found, so overlap is off | `ISSUES.md` 13, 31 |
| 4+2 is slower than 2+2 on every model | M cycles a warm step at mid, 2+2 then 4+2: MLP 9.3, 9.9; LeNet 99.7, 107.4; ViT 80.2, 90.5; Mixer 13.5, 14.1; GRU 5.4, 10.4; GPT 30.7, 35.3; wide 22.3, 23.7. Low and ship hold the same order. Not a clean cluster-count comparison: the preferred packer and the pairing differ between the boards, so the cycles also carry a different kernel set | `zoo/boards`, `zoo/clocks22`, `zoo/clocks42` |
| Extra clusters have nothing to run | `dsl`, `matmul` and `transpose` are unchanged on 4+2: ViT 8.5, 6.5, 20.0 M on both boards. ViT's `matmul` spreads onto four clusters and takes the same 6.5 M | `zoo/unit_share` |
| The whole 4+2 growth is `convert` | `convert` and `quant` at mid, 2+2 then 4+2: ViT 43.9, 54.0 M; GPT 9.0, 13.3 M; GRU 3.1, 8.1 M; LeNet 73.9, 81.2 M. `quant` falls, `convert` rises by more. Launches rise too: GRU 336 to 467, ViT 506 to 592 | `zoo/boards` |
| 4+2 by clock: cycles grow as the clock rises, because DDR4 does not | M cycles at low, mid, ship: MLP 9.4, 9.9, 10.4; LeNet 90.7, 107.4, 126.1; ViT 77.9, 90.5, 103.8; Mixer 12.5, 14.1, 15.9; GRU 9.6, 10.4, 11.4; GPT 29.3, 35.3, 41.7; wide 22.2, 23.7, 25.3. 3 times the clock gives 2.1 to 2.7 times the speed | `zoo/clocks42` |
| The timed model has no 4+2 calibration | ViT 224.1 M modelled against 90.5 M on the card, LeNet 159.7 M against 107.4 M, GRU 13.0 M against 10.4 M | `zoo/boards` |
| Memory is the same on both boards | 627 walks at three clocks: median difference 0.7%, mean -0.4%; 92 differ by over 2%, mostly at strides of 1 KB and up, largest 16%, in both directions. Mid warm read 4 MB 2.8 cycles a word on both, 64-byte stride 26.2 and 26.5, write 12.6 on both, two cores 1.4 on both | `memory/grid_42`, `memory/grid_warm` |
| Two vector cores share DRAM past the cache | contiguous read over two cores at mid 1.4 a word at 4 MB, 3.0 at 64 MB, on both boards | `memory/grid_warm`, `memory/grid_42` |
| What does not exist to measure | distributed tinygrad, 6+2 (no bitstream), a four-die firmware counterpart to the OS rows | `PLAN.md` |

## Correctness

| Bottleneck | Size | Figure |
|---|---|---|
| A kernel hangs the card | 32 runs queued on a vector core before its first `wait` never finish; 30 finish | `ISSUES.md` 12 |
| Four nodes overlapping | about a third of runs fail: wrong output, `BAD_STEP`, or a mesh window timing out. Cause not found; off by default | `ISSUES.md` 31 |
| One large C2H read of staging | a single 4 KB read hangs the engine, 3.6 KB passes; reads go in pieces of 1 KB | `ISSUES.md` 22, 32 |
| XDMA just after a retune | some transfers time out, then none a few link resets later; cause not found | `ISSUES.md` 32 |
| Timed model | LeNet step 140.7 M modelled against 99.7 M; `convert` +67%, `dsl` -25%, 27% of cycles misplaced | `model/lenet.z` |
