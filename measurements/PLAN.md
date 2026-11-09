# Measurement plan

The question: where does the time go between a tinygrad or DSL line and a card
unit, seen from every layer, so a bottleneck can be located from the data alone.

Every experiment ends in a picture under `figures/` and its raw table under
`data/`. `README.md` is the entry point; it shows the pictures in the order below.

## The path and what counts each layer

| Layer | What happens | Counted by | Clock | Experiment |
|---|---|---|---|---|
| tinygrad | graph, rewrite, schedule, autograd | cProfile by module | host s | H1 |
| ktpugrad and DSL | UOp to kernel, trace, plan, cache | cProfile, phase timers | host s | H1 |
| Compiler | L2 passes, L2 to L1, L1 to L0, check | `compile_speed` per stage | host s | H2 |
| Runtime | bind, package, submit, collect, memory | phase timers per launch | host s | H1 |
| Transport | XDMA and BAR traffic | byte and access counts | host s | H1 |
| Host to node seam | a kernel ended, the next one not started | run record `idle` | node cycles | F1 |
| Firmware | head, copy, bind, steps, barrier | run record phases | node cycles | F1 |
| Dispatch engine | word issue, mailbox, fetch | `first`, `last`, `sent`, debug image steps | node cycles | F2 |
| KohakuAccel OS | the same step on the OS path | OS launch ring | node cycles | F3 |
| Units | cluster, vector core, mover | `CU_COUNTERS`, `CU_DBG`, `MVSTAT` per launch | node, unit | U1 |
| Unit inside | stall states, sequencer, overlap | Verilator traces on small kernels | unit | U2 |
| Memory | Xache, DDR4, stride, home, contention | anchor matrix on the card | node cycles | M1 |

## Experiments

| Id | Run | Output picture |
|---|---|---|
| H1 | Each model on the card, cProfile from a cold and from a warm step | whole-path time waterfall, host time by module |
| H2 | Compile corpus and each model's own kernels | stage bars, scaling scatter, slowest kernels |
| F1 | Run record of every launch | launch timeline, firmware phase bars, seam histogram |
| F2 | Debug image traces of the top kernels | step timeline, firmware against engine |
| F3 | OS replay beside firmware replay of one step | per-launch cost comparison |
| U1 | Measure image: unit counters per launch | unit busy Gantt, overlap, fill against compute |
| U2 | Traced Verilator model on small kernels | stall state bars, cluster sequencer Gantt |
| M1 | Footprint, stride, direction, agent, concurrency, home, clock | cycles per word heatmaps |
| Z1 | Model zoo by clock by eager and TinyJit on 2+2 | model comparison bars, cycle scaling with clock |
| Z2 | Batch sizes and stack knobs | fixed and per-element split, knob deltas |
| Z3 | Same core on 4+2 (v9t8), then multi-node kernels | 2+2 against 4+2, node scaling |
| W1 | Detectors over each trace; hand L1 floors for the top kernels | waste by type, claimed against achieved |
| E1 | Timed model against the card by kernel | error scatter, error by kind |

## Coverage

| Id | Measured | Not measured |
|---|---|---|
| H1 | LeNet and ViT by module, warm; LeNet by module, cold; run and submit time for every run | Cold path by module for the other models |
| H2 | Stage bars over the corpus; per-kernel prebuild times | Scaling scatter |
| F1 | Every launch of every run | |
| F2 | LeNet and ViT step records | |
| F3 | The OS on the card over XDMA: primitives, trap path, dispatch paths, one die at low, mid, ship, both boards, and four dies at mid; two tinygrad captures on every path beside firmware, both boards, all clocks; projection a step; LeNet replay on the OS path, both boards; OS across four dies on 2+2 | Replay across dies; OS across dies on 4+2, `ISSUES.md` 21 |
| U1 | Every unit, every launch | |
| U2 | Vector stalls and cluster sequencer states on the traced model, over whole runs of small kernels | Stalls per kernel; any kernel past 30 k cycles |
| M1 | Stride, footprint, direction, agents, four start offsets, three clocks | Mover against vector at once |
| Z1 | MLP, LeNet, LeNet-BN, LeNet no norm, ViT, ViT-B (`vitb`, width 768), Mixer, GPT, GRU, wide at mid; MLP, LeNet, ViT at three clocks; five models under TinyJit | ResNet-S, ConvNet-W, UNet: libraries never finished |
| Z2 | MLP at batch 8, 32, 128; LeNet with prepared packages off, composed path off | |
| Z3 | MLP, LeNet, ViT, Mixer, GRU, GPT, wide on 2+2 and 4+2, at low, mid and ship, eager and TinyJit, 84 runs; vector memory grid; tinygrad models on four nodes; six L1 multi-node examples; the OS on four dies | 6+2, which has no bitstream; distributed tinygrad, which the stack does not do, `ISSUES.md` 13 |
| W1 | Serial floors and detectors for every run | Hand L1 floors |
| E1 | LeNet and ViT by kernel and kind | |

## A round

The in-repo entry point is `measurements/tools/card_round.py`. It records one
bounded card attempt per matrix configuration, runs the offline KAOS preflight,
then calls `host_round.py` and `build_round.py`. It does not build binaries, load a
bitstream, change clocks, recover the card or retry a configuration. Provision
those prerequisites separately before starting the round.

1. Sync the complete tree and its prepared binaries/environment to the host.
   `PERF_COMMIT` (or `--commit`) must identify **that synced tree**, including any
   uncommitted overlay; do not substitute the host's unrelated git HEAD. Keep the
   same identity for the whole round. Prepare the timed-model executable at
   `software/target/stage/timed`, the measure firmware and, for KAOS, the OS image.
2. Select the physical board, set all four dies to the same clock in a separate
   operation, then use `bringup read-clocks`. Supply its actual JSON readback,
   `build/bringup/<board>/read-clocks.json`, via `--clocks` or `PERF_CLOCKS`.
   The driver requires four equal, locked die readbacks matching `--profile`.
   Recorder boot uses this profile; it also reads all dies after boot and before
   close and rejects disagreement with the supplied readback. No clock-changing step is part of the matrix.
3. Write a JSON matrix. An object expands its arrays as a Cartesian product;
   alternatively use a list of objects with exactly these seven scalar keys:

   ```json
   {
     "workload": ["vitb"],
     "board": ["multimesh_v9t6", "multimesh_v9t8"],
     "sysnodes": [1, 2, 4],
     "batch": [1, 4, 16],
     "path": ["eager", "firmware", "kaos"],
     "inputs": [16],
     "blocks": [12]
   }
   ```

   `firmware` and `kaos` mean retained bound-graph replay; `eager` means no
   TinyJit. Use `blocks: 1` for MNIST-zoo workloads, which have no block axis.
   Backend runs require `inputs >= min-replays + 4`, **and** the observed retained
   input count must meet `--min-replays` (default 8). More inputs are a budget for
   capture convergence, not proof of replay. A run with no retained replay fails
   measurement and is never replaced by a capture timing.
4. Run for the **currently attached/programmed board only**. The full matrix can
   contain both boards; `--board` filters it. Finish/safely provision the other
   board separately, take its readback and invoke the same round with that board.
   There must be only one driver using a card. Python/venv, privilege command,
   XDMA module, profile and OS board/image are explicit arguments or environment:

   ```sh
   export PERF_COMMIT='<identity of the synced source and overlay>'
   python measurements/tools/card_round.py r2 matrix.json \
     --board multimesh_v9t6 \
     --clocks build/bringup/multimesh_v9t6/read-clocks.json \
     --profile mid --python /path/to/venv/bin/python --sudo 'sudo -n' \
     --xdma-ko /path/to/xdma.ko --os-board card_v9t6_4n \
     --kos-image build/os/kos.elf --seconds 3600 --min-replays 8
   ```

   Omit `--sudo` when already authorized. Alternatives are `PERF_PYTHON`,
   `PERF_SUDO`, `XDMA_KO`, `XDMA_PARAMS`, `KOHAKUTPU_PROFILE`, `KOS_IMAGE`.
   Optional `--library` uses the recorder's existing prebuild-on-miss path.
   The timeout supervisor runs inside the privilege boundary, TERM/KILLs the
   recorder process group, and hands file ownership back to the invoking IDs.
   Bounds cover a whole card attempt; preflight has its own bound and never
   opens the card. Its per-launch and retained-region translation/capacity
   results are kept in each KAOS attempt's `preflight/`.
5. Raw attempts are under `build/perf/traces/<round>/`. Tags include workload,
   path, batch, inputs, blocks, board and P. `attempt.json` is an atomic claim and
   terminal receipt. Complete **and software-failed** points are skipped on
   restart. An unfinished claim stops rather than risking a duplicate run.
   A card/transport signature, even with exit status zero, or a timed-out card
   process writes durable `STOP.json` and stops the entire matrix. A software
   refusal is retained and the next configuration runs. Do not clear STOP to
   retry points; investigation/recovery belongs outside this round.
6. Every attempt keeps untruncated logs, `failure.txt` when failed, provenance,
   and KAOS klog/KDS diagnostics. If the recorder was killed before log capture,
   the driver makes one bounded read-only klog attempt; its outcome is recorded
   even when the log cannot be read. Firmware has no KAOS klog: its marker points
   to the firmware console/failure files. Packing includes interrupted attempts
   even without finalized recorder metadata and keeps failed trees byte-for-byte
   under `raw/*.gz` with hashes. No missing diagnostic is labeled captured.
7. The driver posts and builds the report without another card run. To resume
   only postprocessing, use `--post-only`, or run the existing tools directly:

   ```sh
   python measurements/tools/host_round.py r2 measurements \
     --traces build/perf/traces/r2
   MEASURE_ROUND=r2 python measurements/tools/build_round.py
   ```

   `host_round.py` selects the machine from `meta.json.board`, including backend
   trace names, models the **actual compiled retained regions**, runs the waste
   detectors on selected steady inputs, and packs into `measurements/data/`.
   Model failures on selected programs are unscored, not zero cycles. Nonselected
   capture-model errors are retained as diagnostics. There are at most four post
   workers, each with one model worker. Postprocessing never builds missing tools.
   `build_round.py` discovers every schema-2 workload/board/batch/P/path from
   metadata and writes `rounds/<round>/data/{matrix,matrix-kinds}.csv`, figures,
   `README.md` and `index.html`. Failed points remain visible with reasons.
   Legacy zoo/fill traces retain their existing report and `compile.csv` path.

## Measures of a warm step

- `inputs.jsonl` defines input boundaries at the workload, independent of repeated
  transformer-block names. Eager excludes the first input of each group. Retained
  paths select only complete inputs containing a submitted retained bound region
  and no capture. `meta.json.replay_counts`, graph histories and `steady.json`
  record observed replay calls, retained launches and selected input counts.
- `card.jsonl` is one row per logical retirement, preserving each participating
  sysnode's durations, host launch kind and event identity, and `max_node_cycles`.
  Measurement-ring joins are per sysnode; loss, missing records and extra records
  fail attribution. Eager lists use their own item durations, not repeated list
  completion time. `retirements.jsonl` and `units.jsonl` keep the underlying evidence.
- **Launch-max sum:** sum of per-launch maximum node-local durations for one
  selected input. The per-kind table reconciles to the upper-median input's sum.
  Firmware-package and KAOS submit-to-reply durations have different scopes.
  A combined retained graph is measured as kind `graph`; its constituent kernel
  kinds are **not measured separately**. Capture timings are never substituted.
- **Global input interval:** all-node drain before the input, upload/execution/
  workload readback, all-node drain afterward. The host `perf_counter_ns` bound
  excludes the timebase and ring reads. HR_CYCLES has a local boot epoch on each
  sysnode; `timebase.jsonl` retains the h-runner's bracketed offsets and drift.
  When counter windows are available (firmware and KAOS), an enclosing cycle
  bound uses two reads of the **same reference sysnode**, spanning every node's
  work. It includes host gaps and recorder/boundary overhead, not just active
  hardware or uninstrumented production latency.
  Missing counter access uses the defined host bound, never max local cycles as
  global makespan. Global bounds are reported as medians, independently of the
  representative launch-max sum.
- P1 speedup compares the same workload, board, batch, blocks, tree, readback,
  profile, input group and path; eager speedup fixes P and changes path. Ambiguous
  baselines produce blank speedups. Global speedups use the common host-bound
  definition and have separate columns. Neither claims isolated device speedup.
- Timed-model cycles/waste are estimates of the actual selected compiled programs,
  including retained combined regions. Different `captured_split` programs get
  content-derived trace aliases (the saved entry declaration is renamed too),
  without changing runtime packages. The split compiler does not return L2;
  those regions retain their semantic graph JSON and explicitly list unavailable
  dataflow in `waste.json`. The detectors cannot inspect their internal boundaries
  or infer measured constituent-kernel times. The legacy zoo's cadence/MFU
  figures remain available for traces
  that have their required start stamps and unit counters.

## Rules of the round

- Each configuration runs once. Noise comes from at least 8 warm steps in one run.
- Scoring is by kind and by kernel, weighted by cycles, never the whole step alone.
- A kernel count, cycle or MFU that does not reconcile with the run total is a
  harness bug and is fixed before any figure uses it.
- Changes are limited to measurement scripts, the additive measure firmware image,
  and minimum bug fixes needed to keep measuring.
- Clock steps and card runs never share a batch.

## Tool witnesses

The CPU-only join, replay-selection, global-bound, matrix-stop and packing
witnesses live in `software/tests/perf/test_{retirement,steady,round,tool_edges}.py`.
They are in the `perf` stage of `software/tests/run.py`; changing a measurements
tool also selects that stage. They mock card/subprocess effects where needed.
Run them in the normal provisioned test environment before authorizing a card
round. A static review or a successful offline preflight does not qualify silicon
execution or establish a speedup.
