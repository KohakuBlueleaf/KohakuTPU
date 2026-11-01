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
| F3 | The OS on the card over XDMA: primitives, trap path, dispatch paths, one die at low, mid, ship, both boards, and four dies at mid; two tinygrad captures on every path beside firmware, both boards, all clocks; projection a step | LeNet on the OS path, `ISSUES.md` 24; OS across dies on 2+2; replay across dies |
| U1 | Every unit, every launch | |
| U2 | Vector stalls and cluster sequencer states on the traced model, over whole runs of small kernels | Stalls per kernel; any kernel past 30 k cycles |
| M1 | Stride, footprint, direction, agents, four start offsets, three clocks | Mover against vector at once |
| Z1 | MLP, LeNet, LeNet-BN, LeNet no norm, ViT, Mixer, GPT, GRU, wide at mid; MLP, LeNet, ViT at three clocks; five models under TinyJit | ResNet-S, ConvNet-W, UNet: libraries never finished |
| Z2 | MLP at batch 8, 32, 128; LeNet with prepared packages off, composed path off | |
| Z3 | MLP, LeNet, ViT, Mixer, GRU, GPT, wide on 2+2 and 4+2, at low, mid and ship, eager and TinyJit, 84 runs; vector memory grid; tinygrad models on four nodes; six L1 multi-node examples; the OS on four dies | 6+2, which has no bitstream; distributed tinygrad, which the stack does not do, `ISSUES.md` 13 |
| W1 | Serial floors and detectors for every run | Hand L1 floors |
| E1 | LeNet and ViT by kernel and kind | |

## Rules of the round

- Each configuration runs once. Noise comes from at least 8 warm steps in one run.
- Scoring is by kind and by kernel, weighted by cycles, never the whole step alone.
- A kernel count, cycle or MFU that does not reconcile with the run total is a
  harness bug and is fixed before any figure uses it.
- Changes are limited to measurement scripts, the additive measure firmware image,
  and minimum bug fixes needed to keep measuring.
- Clock steps and card runs never share a batch.
