# Measurements

Open `index.html`. Findings: `REPORT.md`. Plan: `PLAN.md`. Issues: `ISSUES.md`.

## The whole path

One warm step from tinygrad to the units, in one picture.

![ViT: the same axis. The node waits on the host for most of the step.](figures/runs/vit.e1/path.png)

ViT: the same axis. The node waits on the host for most of the step.

## Against the baseline

This round against the zoo at commit 56021fd4, one row a like configuration.

![A warm step by kernel kind: the baseline's bar, then this round's.](figures/compare/cycles.png)

A warm step by kernel kind: the baseline's bar, then this round's.

![Launches and span of a warm step against the baseline's.](figures/compare/launches.png)

Launches and span of a warm step against the baseline's.

## Models against each other

Every run on the same axes. Eager is z, TinyJit zj, low zl, ship zs.

![Wall time of a warm step. Grey is the node waiting for the host.](figures/zoo/wall.png)

Wall time of a warm step. Grey is the node waiting for the host.

![The node's time: a unit busy, firmware, no unit busy, or idle.](figures/zoo/shares.png)

The node's time: a unit busy, firmware, no unit busy, or idle.

![Run cycles by kernel kind: what each architecture spends them on.](figures/zoo/kinds.png)

Run cycles by kernel kind: what each architecture spends them on.

![Busy cycles of each unit over the span; 4.0 is all four always busy.](figures/zoo/unit_busy.png)

Busy cycles of each unit over the span; 4.0 is all four always busy.

![MFU: math-only floor over the span, for clusters and vector cores.](figures/zoo/mfu.png)

MFU: math-only floor over the span, for clusters and vector cores.

![Batch size and stack knobs, one change at a time.](figures/zoo/knobs.png)

Batch size and stack knobs, one change at a time.

![2+2 against 4+2 on the card, with the timed model's step beside each.](figures/zoo/boards.png)

2+2 against 4+2 on the card, with the timed model's step beside each.

![Whole-run wall seconds, every model by clock and mode, on both boards.](figures/zoo/wall_matrix.png)

Whole-run wall seconds, every model by clock and mode, on both boards.

![2+2 at three clocks: cycles a warm step and device time.](figures/zoo/clocks22.png)

2+2 at three clocks: cycles a warm step and device time.

![4+2 at three clocks: cycles a warm step and device time.](figures/zoo/clocks42.png)

4+2 at three clocks: cycles a warm step and device time.

![How much of a warm step each unit is busy, on both boards at mid.](figures/zoo/unit_share.png)

How much of a warm step each unit is busy, on both boards at mid.

![Node 0 at mid against the clock of the dies that run nothing.](figures/zoo/idle_dies.png)

Node 0 at mid against the clock of the dies that run nothing.

## Memory

A vector core or the mover walking DRAM, on the card at three clocks.

![Node cycles a 32-byte word, second pass. Contiguous is the top row.](figures/memory/grid_warm.png)

Node cycles a 32-byte word, second pass. Contiguous is the top row.

![The same grid on the first pass: the gap to warm is the cache.](figures/memory/grid_cold.png)

The same grid on the first pass: the gap to warm is the cache.

![The same walks on 4+2, at low, mid and ship.](figures/memory/grid_42.png)

The same walks on 4+2, at low, mid and ship.

![Contiguous walks: small footprints cost more, then the curve is flat.](figures/memory/clock_scaling.png)

Contiguous walks: small footprints cost more, then the curve is flat.

![One line every 64 KB from four start offsets: cost differs by offset.](figures/memory/homes.png)

One line every 64 KB from four start offsets: cost differs by offset.

## Firmware and dispatch

What a launch waits on. OS rows are this zoo's silicon runs on one die.

![One launch on each path, firmware against the OS kernel and dispatcher.](figures/firmware/dispatch_paths.png)

One launch on each path, firmware against the OS kernel and dispatcher.

![The same steps on 2+2, at low, mid and ship.](figures/firmware/replay_paths_22.png)

The same steps on 2+2, at low, mid and ship.

![The same steps on 4+2, at low, mid and ship.](figures/firmware/replay_paths_42.png)

The same steps on 4+2, at low, mid and ship.

![One tinygrad step on every path on silicon, against firmware.](figures/firmware/replay_paths.png)

One tinygrad step on every path on silicon, against firmware.

![ViT on the OS path, launch by launch. Each stamp adds about 11 k.](figures/firmware/replay_seam.png)

ViT on the OS path, launch by launch. Each stamp adds about 11 k.

![The OS across four dies at mid; four-worker speedup at low and mid.](figures/firmware/os_four_dies.png)

The OS across four dies at mid; four-worker speedup at low and mid.

![OS operations at 200 against 100 MHz: above 1 means DDR4.](figures/firmware/os_clock.png)

OS operations at 200 against 100 MHz: above 1 means DDR4.

![OS kernel on silicon: a system call's cycles by stage of the trap path.](figures/firmware/os_kernel_stages.png)

OS kernel on silicon: a system call's cycles by stage of the trap path.

![OS on silicon: what each kernel primitive costs, log scale.](figures/firmware/os_primitives.png)

OS on silicon: what each kernel primitive costs, log scale.

![Launches a step times each path's cost, over run cycles. A projection.](figures/firmware/dispatch_projection.png)

Launches a step times each path's cost, over run cycles. A projection.

## Waste

What a step holds beyond the math, from the data flow of each trace.

![A step against its serial floors; grey has no floor that explains it.](figures/waste/ladder.png)

A step against its serial floors; grey has no floor that explains it.

![Share of a step each detector names as removable.](figures/waste/recoverable.png)

Share of a step each detector names as removable.

![Form-change sequences between two computations, by cost.](figures/waste/chains.png)

Form-change sequences between two computations, by cost.

## Inside the units

Traced Verilator model over small kernels only, never a card run.

![Vector dispatch stalls, vector DMA states, cluster sequencer states.](figures/units/traced.png)

Vector dispatch stalls, vector DMA states, cluster sequencer states.

## Compiler

Where a compile's time goes.

![Compile time by stage over the corpus.](figures/compiler/stages.png)

Compile time by stage over the corpus.

## Timed model

The model against the card, kernel by kernel.

![LeNet: each dot a kernel; bars by kind.](figures/model/lenet.e1.png)

LeNet: each dot a kernel; bars by kind.

![ViT: each dot a kernel; bars by kind.](figures/model/vit.e1.png)

ViT: each dot a kernel; bars by kind.

![Mixer: each dot a kernel; bars by kind.](figures/model/mixer.e1.png)

Mixer: each dot a kernel; bars by kind.

![GRU: each dot a kernel; bars by kind.](figures/model/gru.e1.png)

GRU: each dot a kernel; bars by kind.

## Each run

### lenet.e1

![One warm step on the node's time axis. Gaps are the node waiting.](figures/runs/lenet.e1/gantt.png)

One warm step on the node's time axis. Gaps are the node waiting.

![Cycles by kernel kind, and how many had a unit busy.](figures/runs/lenet.e1/kinds.png)

Cycles by kernel kind, and how many had a unit busy.

![What sits between kernels, and every warm step of the run.](figures/runs/lenet.e1/seam.png)

What sits between kernels, and every warm step of the run.

![Largest launches; pale is no unit busy.](figures/runs/lenet.e1/pareto.png)

Largest launches; pale is no unit busy.

### lenet.e1j

![One warm step on the node's time axis. Gaps are the node waiting.](figures/runs/lenet.e1j/gantt.png)

One warm step on the node's time axis. Gaps are the node waiting.

![Cycles by kernel kind, and how many had a unit busy.](figures/runs/lenet.e1j/kinds.png)

Cycles by kernel kind, and how many had a unit busy.

![What sits between kernels, and every warm step of the run.](figures/runs/lenet.e1j/seam.png)

What sits between kernels, and every warm step of the run.

![Largest launches; pale is no unit busy.](figures/runs/lenet.e1j/pareto.png)

Largest launches; pale is no unit busy.

### lenet-bn.e1

### lenet-nonorm.e1

### vit.e1

![One warm step on the node's time axis. Gaps are the node waiting.](figures/runs/vit.e1/gantt.png)

One warm step on the node's time axis. Gaps are the node waiting.

![Cycles by kernel kind, and how many had a unit busy.](figures/runs/vit.e1/kinds.png)

Cycles by kernel kind, and how many had a unit busy.

![What sits between kernels, and every warm step of the run.](figures/runs/vit.e1/seam.png)

What sits between kernels, and every warm step of the run.

![Largest launches; pale is no unit busy.](figures/runs/vit.e1/pareto.png)

Largest launches; pale is no unit busy.

### mlp.e1

![One warm step on the node's time axis. Gaps are the node waiting.](figures/runs/mlp.e1/gantt.png)

One warm step on the node's time axis. Gaps are the node waiting.

![Cycles by kernel kind, and how many had a unit busy.](figures/runs/mlp.e1/kinds.png)

Cycles by kernel kind, and how many had a unit busy.

![What sits between kernels, and every warm step of the run.](figures/runs/mlp.e1/seam.png)

What sits between kernels, and every warm step of the run.

![Largest launches; pale is no unit busy.](figures/runs/mlp.e1/pareto.png)

Largest launches; pale is no unit busy.

### mlp.e1j

![One warm step on the node's time axis. Gaps are the node waiting.](figures/runs/mlp.e1j/gantt.png)

One warm step on the node's time axis. Gaps are the node waiting.

![Cycles by kernel kind, and how many had a unit busy.](figures/runs/mlp.e1j/kinds.png)

Cycles by kernel kind, and how many had a unit busy.

![What sits between kernels, and every warm step of the run.](figures/runs/mlp.e1j/seam.png)

What sits between kernels, and every warm step of the run.

![Largest launches; pale is no unit busy.](figures/runs/mlp.e1j/pareto.png)

Largest launches; pale is no unit busy.

### mixer.e1

![One warm step on the node's time axis. Gaps are the node waiting.](figures/runs/mixer.e1/gantt.png)

One warm step on the node's time axis. Gaps are the node waiting.

![Cycles by kernel kind, and how many had a unit busy.](figures/runs/mixer.e1/kinds.png)

Cycles by kernel kind, and how many had a unit busy.

![What sits between kernels, and every warm step of the run.](figures/runs/mixer.e1/seam.png)

What sits between kernels, and every warm step of the run.

![Largest launches; pale is no unit busy.](figures/runs/mixer.e1/pareto.png)

Largest launches; pale is no unit busy.

### mixer.e1j

![One warm step on the node's time axis. Gaps are the node waiting.](figures/runs/mixer.e1j/gantt.png)

One warm step on the node's time axis. Gaps are the node waiting.

![Cycles by kernel kind, and how many had a unit busy.](figures/runs/mixer.e1j/kinds.png)

Cycles by kernel kind, and how many had a unit busy.

![What sits between kernels, and every warm step of the run.](figures/runs/mixer.e1j/seam.png)

What sits between kernels, and every warm step of the run.

![Largest launches; pale is no unit busy.](figures/runs/mixer.e1j/pareto.png)

Largest launches; pale is no unit busy.

### gpt.e1

![One warm step on the node's time axis. Gaps are the node waiting.](figures/runs/gpt.e1/gantt.png)

One warm step on the node's time axis. Gaps are the node waiting.

![Cycles by kernel kind, and how many had a unit busy.](figures/runs/gpt.e1/kinds.png)

Cycles by kernel kind, and how many had a unit busy.

![What sits between kernels, and every warm step of the run.](figures/runs/gpt.e1/seam.png)

What sits between kernels, and every warm step of the run.

![Largest launches; pale is no unit busy.](figures/runs/gpt.e1/pareto.png)

Largest launches; pale is no unit busy.

### gpt.e1j

![One warm step on the node's time axis. Gaps are the node waiting.](figures/runs/gpt.e1j/gantt.png)

One warm step on the node's time axis. Gaps are the node waiting.

![Cycles by kernel kind, and how many had a unit busy.](figures/runs/gpt.e1j/kinds.png)

Cycles by kernel kind, and how many had a unit busy.

![What sits between kernels, and every warm step of the run.](figures/runs/gpt.e1j/seam.png)

What sits between kernels, and every warm step of the run.

![Largest launches; pale is no unit busy.](figures/runs/gpt.e1j/pareto.png)

Largest launches; pale is no unit busy.

### gru.e1

![One warm step on the node's time axis. Gaps are the node waiting.](figures/runs/gru.e1/gantt.png)

One warm step on the node's time axis. Gaps are the node waiting.

![Cycles by kernel kind, and how many had a unit busy.](figures/runs/gru.e1/kinds.png)

Cycles by kernel kind, and how many had a unit busy.

![What sits between kernels, and every warm step of the run.](figures/runs/gru.e1/seam.png)

What sits between kernels, and every warm step of the run.

![Largest launches; pale is no unit busy.](figures/runs/gru.e1/pareto.png)

Largest launches; pale is no unit busy.

### gru.e1j

![One warm step on the node's time axis. Gaps are the node waiting.](figures/runs/gru.e1j/gantt.png)

One warm step on the node's time axis. Gaps are the node waiting.

![Cycles by kernel kind, and how many had a unit busy.](figures/runs/gru.e1j/kinds.png)

Cycles by kernel kind, and how many had a unit busy.

![What sits between kernels, and every warm step of the run.](figures/runs/gru.e1j/seam.png)

What sits between kernels, and every warm step of the run.

![Largest launches; pale is no unit busy.](figures/runs/gru.e1j/pareto.png)

Largest launches; pale is no unit busy.

### wide.e1

![One warm step on the node's time axis. Gaps are the node waiting.](figures/runs/wide.e1/gantt.png)

One warm step on the node's time axis. Gaps are the node waiting.

![Cycles by kernel kind, and how many had a unit busy.](figures/runs/wide.e1/kinds.png)

Cycles by kernel kind, and how many had a unit busy.

![What sits between kernels, and every warm step of the run.](figures/runs/wide.e1/seam.png)

What sits between kernels, and every warm step of the run.

![Largest launches; pale is no unit busy.](figures/runs/wide.e1/pareto.png)

Largest launches; pale is no unit busy.

