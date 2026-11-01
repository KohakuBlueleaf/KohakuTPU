# Measurements

Open `index.html`. Findings: `REPORT.md`. Plan: `PLAN.md`. Issues: `ISSUES.md`.

## The whole path

One warm step from tinygrad to the units, in one picture.

![Each stage of a LeNet step, darkest where the time goes.](figures/runs/lenet.zp/diagram.png)

Each stage of a LeNet step, darkest where the time goes.

![LeNet: the same stages on one time axis. Host rows are under cProfile.](figures/runs/lenet.zp/path.png)

LeNet: the same stages on one time axis. Host rows are under cProfile.

![ViT: the same axis. The node waits on the host for most of the step.](figures/runs/vit.z/path.png)

ViT: the same axis. The node waits on the host for most of the step.

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

![Three clocks: cycles rise because memory stays in nanoseconds.](figures/zoo/clock.png)

Three clocks: cycles rise because memory stays in nanoseconds.

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

![LeNet on the debug image: step opcodes by kind, largest launches.](figures/firmware/steps_lenet.png)

LeNet on the debug image: step opcodes by kind, largest launches.

![ViT on the debug image: the mover drives transpose, dispatch the rest.](figures/firmware/steps_vit.png)

ViT on the debug image: the mover drives transpose, dispatch the rest.

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

![LeNet: each dot a kernel; bars by kind.](figures/model/lenet.z.png)

LeNet: each dot a kernel; bars by kind.

![ViT: each dot a kernel; bars by kind.](figures/model/vit.z.png)

ViT: each dot a kernel; bars by kind.

![Mixer: each dot a kernel; bars by kind.](figures/model/mixer.z.png)

Mixer: each dot a kernel; bars by kind.

![GRU: each dot a kernel; bars by kind.](figures/model/gru.z.png)

GRU: each dot a kernel; bars by kind.

## Each run

### lenet.z

![One warm step on the node's time axis. Gaps are the node waiting.](figures/runs/lenet.z/gantt.png)

One warm step on the node's time axis. Gaps are the node waiting.

![Cycles by kernel kind, and how many had a unit busy.](figures/runs/lenet.z/kinds.png)

Cycles by kernel kind, and how many had a unit busy.

![What sits between kernels, and every warm step of the run.](figures/runs/lenet.z/seam.png)

What sits between kernels, and every warm step of the run.

![Largest launches; pale is no unit busy.](figures/runs/lenet.z/pareto.png)

Largest launches; pale is no unit busy.

### lenet.zj

![One warm step on the node's time axis. Gaps are the node waiting.](figures/runs/lenet.zj/gantt.png)

One warm step on the node's time axis. Gaps are the node waiting.

![Cycles by kernel kind, and how many had a unit busy.](figures/runs/lenet.zj/kinds.png)

Cycles by kernel kind, and how many had a unit busy.

![What sits between kernels, and every warm step of the run.](figures/runs/lenet.zj/seam.png)

What sits between kernels, and every warm step of the run.

![Largest launches; pale is no unit busy.](figures/runs/lenet.zj/pareto.png)

Largest launches; pale is no unit busy.

### lenet-bn.z

![One warm step on the node's time axis. Gaps are the node waiting.](figures/runs/lenet-bn.z/gantt.png)

One warm step on the node's time axis. Gaps are the node waiting.

![Cycles by kernel kind, and how many had a unit busy.](figures/runs/lenet-bn.z/kinds.png)

Cycles by kernel kind, and how many had a unit busy.

![What sits between kernels, and every warm step of the run.](figures/runs/lenet-bn.z/seam.png)

What sits between kernels, and every warm step of the run.

![Largest launches; pale is no unit busy.](figures/runs/lenet-bn.z/pareto.png)

Largest launches; pale is no unit busy.

### lenet-nonorm.z

![One warm step on the node's time axis. Gaps are the node waiting.](figures/runs/lenet-nonorm.z/gantt.png)

One warm step on the node's time axis. Gaps are the node waiting.

![Cycles by kernel kind, and how many had a unit busy.](figures/runs/lenet-nonorm.z/kinds.png)

Cycles by kernel kind, and how many had a unit busy.

![What sits between kernels, and every warm step of the run.](figures/runs/lenet-nonorm.z/seam.png)

What sits between kernels, and every warm step of the run.

![Largest launches; pale is no unit busy.](figures/runs/lenet-nonorm.z/pareto.png)

Largest launches; pale is no unit busy.

### vit.z

![One warm step on the node's time axis. Gaps are the node waiting.](figures/runs/vit.z/gantt.png)

One warm step on the node's time axis. Gaps are the node waiting.

![Cycles by kernel kind, and how many had a unit busy.](figures/runs/vit.z/kinds.png)

Cycles by kernel kind, and how many had a unit busy.

![What sits between kernels, and every warm step of the run.](figures/runs/vit.z/seam.png)

What sits between kernels, and every warm step of the run.

![Largest launches; pale is no unit busy.](figures/runs/vit.z/pareto.png)

Largest launches; pale is no unit busy.

### mlp.z

![One warm step on the node's time axis. Gaps are the node waiting.](figures/runs/mlp.z/gantt.png)

One warm step on the node's time axis. Gaps are the node waiting.

![Cycles by kernel kind, and how many had a unit busy.](figures/runs/mlp.z/kinds.png)

Cycles by kernel kind, and how many had a unit busy.

![What sits between kernels, and every warm step of the run.](figures/runs/mlp.z/seam.png)

What sits between kernels, and every warm step of the run.

![Largest launches; pale is no unit busy.](figures/runs/mlp.z/pareto.png)

Largest launches; pale is no unit busy.

### mlp.zj

![One warm step on the node's time axis. Gaps are the node waiting.](figures/runs/mlp.zj/gantt.png)

One warm step on the node's time axis. Gaps are the node waiting.

![Cycles by kernel kind, and how many had a unit busy.](figures/runs/mlp.zj/kinds.png)

Cycles by kernel kind, and how many had a unit busy.

![What sits between kernels, and every warm step of the run.](figures/runs/mlp.zj/seam.png)

What sits between kernels, and every warm step of the run.

![Largest launches; pale is no unit busy.](figures/runs/mlp.zj/pareto.png)

Largest launches; pale is no unit busy.

### mixer.z

![One warm step on the node's time axis. Gaps are the node waiting.](figures/runs/mixer.z/gantt.png)

One warm step on the node's time axis. Gaps are the node waiting.

![Cycles by kernel kind, and how many had a unit busy.](figures/runs/mixer.z/kinds.png)

Cycles by kernel kind, and how many had a unit busy.

![What sits between kernels, and every warm step of the run.](figures/runs/mixer.z/seam.png)

What sits between kernels, and every warm step of the run.

![Largest launches; pale is no unit busy.](figures/runs/mixer.z/pareto.png)

Largest launches; pale is no unit busy.

### mixer.zj

![One warm step on the node's time axis. Gaps are the node waiting.](figures/runs/mixer.zj/gantt.png)

One warm step on the node's time axis. Gaps are the node waiting.

![Cycles by kernel kind, and how many had a unit busy.](figures/runs/mixer.zj/kinds.png)

Cycles by kernel kind, and how many had a unit busy.

![What sits between kernels, and every warm step of the run.](figures/runs/mixer.zj/seam.png)

What sits between kernels, and every warm step of the run.

![Largest launches; pale is no unit busy.](figures/runs/mixer.zj/pareto.png)

Largest launches; pale is no unit busy.

### gpt.z

![One warm step on the node's time axis. Gaps are the node waiting.](figures/runs/gpt.z/gantt.png)

One warm step on the node's time axis. Gaps are the node waiting.

![Cycles by kernel kind, and how many had a unit busy.](figures/runs/gpt.z/kinds.png)

Cycles by kernel kind, and how many had a unit busy.

![What sits between kernels, and every warm step of the run.](figures/runs/gpt.z/seam.png)

What sits between kernels, and every warm step of the run.

![Largest launches; pale is no unit busy.](figures/runs/gpt.z/pareto.png)

Largest launches; pale is no unit busy.

### gpt.zj

![One warm step on the node's time axis. Gaps are the node waiting.](figures/runs/gpt.zj/gantt.png)

One warm step on the node's time axis. Gaps are the node waiting.

![Cycles by kernel kind, and how many had a unit busy.](figures/runs/gpt.zj/kinds.png)

Cycles by kernel kind, and how many had a unit busy.

![What sits between kernels, and every warm step of the run.](figures/runs/gpt.zj/seam.png)

What sits between kernels, and every warm step of the run.

![Largest launches; pale is no unit busy.](figures/runs/gpt.zj/pareto.png)

Largest launches; pale is no unit busy.

### gru.z

![One warm step on the node's time axis. Gaps are the node waiting.](figures/runs/gru.z/gantt.png)

One warm step on the node's time axis. Gaps are the node waiting.

![Cycles by kernel kind, and how many had a unit busy.](figures/runs/gru.z/kinds.png)

Cycles by kernel kind, and how many had a unit busy.

![What sits between kernels, and every warm step of the run.](figures/runs/gru.z/seam.png)

What sits between kernels, and every warm step of the run.

![Largest launches; pale is no unit busy.](figures/runs/gru.z/pareto.png)

Largest launches; pale is no unit busy.

### gru.zj

![One warm step on the node's time axis. Gaps are the node waiting.](figures/runs/gru.zj/gantt.png)

One warm step on the node's time axis. Gaps are the node waiting.

![Cycles by kernel kind, and how many had a unit busy.](figures/runs/gru.zj/kinds.png)

Cycles by kernel kind, and how many had a unit busy.

![What sits between kernels, and every warm step of the run.](figures/runs/gru.zj/seam.png)

What sits between kernels, and every warm step of the run.

![Largest launches; pale is no unit busy.](figures/runs/gru.zj/pareto.png)

Largest launches; pale is no unit busy.

### wide.z

![One warm step on the node's time axis. Gaps are the node waiting.](figures/runs/wide.z/gantt.png)

One warm step on the node's time axis. Gaps are the node waiting.

![Cycles by kernel kind, and how many had a unit busy.](figures/runs/wide.z/kinds.png)

Cycles by kernel kind, and how many had a unit busy.

![What sits between kernels, and every warm step of the run.](figures/runs/wide.z/seam.png)

What sits between kernels, and every warm step of the run.

![Largest launches; pale is no unit busy.](figures/runs/wide.z/pareto.png)

Largest launches; pale is no unit busy.

