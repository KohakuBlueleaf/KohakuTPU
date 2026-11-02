# What the data says

Node cycles at mid, 200/400 MHz, on v9t6 2+2, node 0, unless a row says otherwise.
Every number comes from `data/`; each section's figures carry the same numbers. Every
run has all four dies at one clock, because idle dies change node 0's cycles. Runs that
did not are kept in `data/old/` and used nowhere.

The first section is round `c1`, against commit 56021fd4. The sections after it hold
that commit's zoo, kept as the baseline, except the rows that name `c1`.

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
| A TinyJit step, node cadence | ms from one replayed step's first launch to the next's at mid, 2+2, against the baseline's warm span: GRU 30.4, 74.7; Mixer 86.1, 108.4; MLP 33.6, 50.0; wide 99.4, 115.5; GPT 126.0, 331.5; LeNet 124.3, 513.4; ViT 145.9, 548.3. ViT's baseline step ran another kernel set. `span` on a TinyJit row covers capture steps only | `compare/replay`, `compare.csv` |
| An eager step, node span | ms at mid, 2+2: GRU 468 to 346, Mixer 652 to 584, GPT 759 to 534, LeNet 755 to 377, wide 359 to 173, MLP 111 to 86. The host still holds most of a step on GRU and Mixer | `compare.csv` |
| Node idle in an eager step at mid, 2+2 | GRU 94%, Mixer 90%, GPT 82%, LeNet 71%, ViT 65%, MLP 65%, wide 56%. Cycles fell faster than the host's time, so the shares rise | `compare.csv` |
| Vector MFU floor over span at mid, 2+2 | ViT 4.4%, wide 2.5%, MLP 1.8%, GPT 1.2%, LeNet 0.6%, Mixer 0.5%, GRU 0.1%; matrix clusters ViT 1.2%, others under 0.2% | `rounds/c1/data/zoo.csv` |
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
