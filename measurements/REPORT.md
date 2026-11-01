# What the data says

Node cycles at mid, 200/400 MHz, on v9t6 2+2, node 0, unless a row says otherwise.
Every number comes from `data/`; each section's figures carry the same numbers. Every
run has all four dies at one clock, because idle dies change node 0's cycles. Runs that
did not are kept in `data/old/` and used nowhere.

## Host

| Bottleneck | Size | Figure |
|---|---|---|
| Node idle waiting for the host, eager | GRU 94%, Mixer 90%, GPT 80%, wide 69%, MLP 58%, ViT 50%, LeNet 34% of a step's span | `zoo/shares`, `zoo/wall` |
| Eager step against TinyJit, node span | GRU 468 to 75 ms, Mixer 652 to 108, GPT 759 to 331, wide 359 to 115, MLP 111 to 50, ViT 806 to 548, LeNet 755 to 513. ViT on 4+2 gains nothing, 941 ms both ways | `zoo/wall` |
| Whole run stays host-bound at every clock | wall seconds eager at low, mid, ship: LeNet 42.9, 37.6, 34.7; ViT 13.1, 12.6, 12.2; GPT 16.1, 15.9, 15.5. Three times the clock saves under 20% | `zoo/wall_matrix` |
| Bind miss in warm steps | a warm kernel submit takes 1.35 s against 40 ms settled; four of LeNet's ten warm steps span about 405 M against 150 M, LeNet-BN eight of ten | `zoo/kinds`, `zoo/shares` |
| LeNet host time under cProfile | tinygrad 346 ms, Python 280, bind 182, XDMA 162, ktpugrad 107, runtime 106 | `runs/lenet.zp/path` |
| Cold first step, LeNet at low under cProfile | 26.9 s for 303 launches: Rust 14.7 s, Python 4.8, runtime 2.8, driver 0.7, tinygrad 0.7, ktpugrad 0.2 | `data/cold` |
| Compile | one `rows_` kernel 21 to 81 s; ResNet-S, ConvNet-W and UNet libraries never finished inside a 105 s round | `compiler/stages`, `ISSUES.md` 9 |

## Firmware and seam

| Bottleneck | Size | Figure |
|---|---|---|
| Firmware around a launch | 1.2 k cycles a launch; under 1% of a step except GRU 8%, Mixer 4%, GPT 1.8% | `firmware/dispatch_paths` |
| One tinygrad step on the OS path against firmware, 2+2 at mid | ViT, 40 launches: firmware 790 k, OS graph 1,366 k (1.73×), retained 1,120 k (1.42×), fused 814 k (1.03×), fused and retained 800 k (1.01×). ViT-QKV: 162 k, 257 k, 203 k, 170 k, 151 k (0.93×). Low and ship keep the ratios: ViT firmware 759 and 822 k, OS graph 1,328 and 1,405 k | `firmware/replay_paths`, `firmware/replay_paths_22` |
| The same step on 4+2 | ViT, 48 launches, at low, mid, ship: firmware 973, 1,041, 1,109 k; OS graph 2,068, 2,152, 2,236 k (2.1×); retained 1,565, 1,633, 1,705 k; fused and retained 1,121, 1,185, 1,248 k (1.14×). ViT-QKV at mid: 174, 325, 253, 165 k (0.95× fused and retained) | `firmware/replay_paths_42` |
| OS seam between launches | about 5 k cycles stamped, each stamp itself about 11 k; a step's OS overhead is 14 k a launch, 8 k retained, under 1 k fused | `firmware/replay_seam` |
| A launch on each OS path, one die | MG engine round 3.9 k, mailbox round 5.8 to 6.7 k, dispatcher independent task 5.1 k, chain task 6.6 k, one-task graph 25.2 k, MG round through the dispatcher 37.5 to 38.4 k, against firmware 1.2 k | `firmware/dispatch_paths` |
| Kernel trap path | null call 469 cycles, 209 in the call; send 791 in the call; yield 250 in the call, 252 polling, 187 scheduling | `firmware/os_kernel_stages` |
| OS against DDR4 | 2 times the clock leaves syscall and yield at 1.00× in cycles, chain task 1.03×, one-task graph 1.06×, mmap 1.04×; the OS is core work, not DDR4 | `firmware/os_clock` |
| OS on four dies, near against far, mid | atomic 1,044 here, 3,773 on another die; channel round trip 11.3 k; barrier 8.7 k; MG mailbox round 16.4 k against 6.8 k; dispatcher chain task 7.6 k, a hop to another die 23 k, a four-node chain step 56 k. Clock leaves all of these within 3% | `firmware/os_four_dies` |
| Workloads on four OS workers | reduce 3.75× at low, 3.22× at mid; CPU matmul 2.94× and 2.56×; the shared-memory work loses speedup as the clock rises | `firmware/os_four_dies` |
| Seam between launches, firmware path | the detector recovers under 1% of a step in every run | `waste/recoverable` |

## Node, by kind of work

| Bottleneck | Size | Figure |
|---|---|---|
| Node running, no compute unit busy | LeNet 51%, GRU 44%, Mixer 42%, ViT 40%, GPT 20%, wide 11%, MLP 10% of run cycles; on 4+2 LeNet 53%, ViT 74%, GRU 60%, Mixer 53%, GPT 41%, wide 20%, MLP 17% | `zoo/shares` |
| Work is on the vector cores | `dsl`, `quant` and `convert` run on the two vector cores; matrix clusters are busy only inside `matmul`, under 10% of any step. ViT 2.2 and 1.3 M of 80.2 M, GPT 0.7 M of 30.7 M, LeNet 0.3 M of 99.7 M | `zoo/unit_share` |
| LeNet `convert` | 70.8 M of 99.7 M; the largest kernel is 28.4 M | `runs/lenet.z/kinds` |
| ViT form work | `quant` 35.8 M and `transpose` 20.0 M of 80.2 M | `runs/vit.z/kinds` |
| Math against the step | math floor over run cycles: LeNet 0.7%, GRU 0.7%, MLP 3.2%, ViT 3.4%, wide 3.7%, Mixer 5.7%, GPT 5.8%. MFU floor over span, vector cores: LeNet 0.5%, ViT 1.8%, GPT 1.3%, MLP 1.4%, wide 1.2%, Mixer 0.6%, GRU 0.1%; matrix clusters at most 0.54% | `waste/ladder`, `zoo/mfu` |
| A value made again in a form already held | Mixer 5.5% of cycles, GPT 3.6%, ViT 1.8%, GRU 1.5%, others under 1%; GPT under TinyJit 0.5% | `waste/recoverable` |
| Batch does not move an MLP step | 9.3 M at 8, 9.4 M at 32, 10.0 M at 128: per-step fixed work dominates | `zoo/knobs` |
| Stack knobs off, LeNet | prepared packages +15%, composed path +17% of run cycles | `zoo/knobs` |

## Memory

| Bottleneck | Size | Figure |
|---|---|---|
| 64-byte stride against contiguous | warm read 26.2 against 2.8 cycles a word at every clock, cold 43.4 against 4.8 at mid; write 12.6 against 1.8 | `memory/grid_warm` |
| Past the cache | 64 MB read has warm equal to cold: contiguous 4.1, 4.8, 5.5 and 64-byte stride 37.5, 43.3, 48.9 at low, mid, ship | `memory/grid_warm` |
| Sparse pages | 64 KB stride over 256 KB: 654, 668, 683 cycles a read word at low, mid, ship | `memory/grid_cold` |
| Clock | 3 times the clock gives 2+2 a warm step of LeNet 846 to 391 ms, ViT 730 to 293, MLP 90 to 32, Mixer 121 to 50, GRU 50 to 19, GPT 263 to 118, wide 212 to 79: 2.2 to 2.8×, not 3× | `zoo/clocks22`, `memory/clock_scaling` |
| Idle dies change node 0's cycles | Mixer step with node 0 at mid: others low 16.95 M, node 1 ship and others low 15.16 M, others mid 13.55 M, others ship 12.54 M. Why an idle die reaches node 0's memory path is not known | `zoo/idle_dies`, `ISSUES.md` 27 |

## Scale

| Bottleneck | Size | Figure |
|---|---|---|
| Multi-node tinygrad is not distributed | `--nodes 4` runs every kernel on node 0: M cycles a step on one node, four nodes at mid: MLP 9.31, 9.28; LeNet 99.66, 99.71; ViT 80.18, 80.13; Mixer 13.55, 13.54; GRU 5.38, 5.38; GPT 30.72, 30.71; wide 22.30, 22.30 | `ISSUES.md` 13 |
| 4+2 is slower than 2+2 on every model | M cycles a warm step at mid, 2+2 then 4+2: MLP 9.3, 9.9; LeNet 99.7, 107.4; ViT 80.2, 90.5; Mixer 13.5, 14.1; GRU 5.4, 10.4; GPT 30.7, 35.3; wide 22.3, 23.7. Low and ship hold the same order | `zoo/boards`, `zoo/clocks22`, `zoo/clocks42` |
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
| ViT on the OS graph path on 2+2 gives other outputs | 175 of 256 values differ, by up to 0.048 on values up to 1.3; fused and firmware agree, and every path agrees on 4+2 | `ISSUES.md` 23 |
| Host reads that hang the card under the OS | kernel log and launch ring over C2H; mailbox, stamps and outputs read fine | `ISSUES.md` 19, 22 |
| Host writes to another die over XDMA | every H2C to mesh 1's DRAM window hangs, at 64 KB and 4 KB, on both bitstreams | `ISSUES.md` 21 |
| Timed model | LeNet step 140.7 M modelled against 99.7 M; `convert` +67%, `dsl` -25%, 27% of cycles misplaced | `model/lenet.z` |
