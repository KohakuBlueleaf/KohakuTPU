# Documentation

Design intent and reasoning for KohakuTPU. Each subsystem has a folder mirroring
its source package; the documents at the top level are the ones that span all of
them, and `isa/` does the same — it cuts across the packages rather than
mirroring one.

```
docs/
├── arch-design.md      the machine as a whole
├── system.md           the machine actually running, end to end
├── perf.md             where the cycles go, at every shape and cluster count
├── optimization.md     every lever, taken or shelved, with the verdict
├── dataflow.md         the signal-level before/after for the levers taken
├── limits.md           what this machine cannot do, and which of those are bugs
├── simulation.md       how to run and write benches (all suites)
│
├── isa/                every instruction set, host down to accumulator
├── driver/             the compiler and runtime -- src/ktpu/
├── interlink/          four meshes, one machine -- the mesh-of-meshes
├── compute/    <->  src/kohakutpu/     what a compute unit does
├── noc/        <->  src/kohakunoc/     how units talk to each other
├── mas/        <->  src/kohakumas/     how units reach memory
├── memory-mover/                       DRAM-to-DRAM, and its own ISA
├── general_cores/                      the scalar side, and crossing SLRs
└── axi/        <->  src/kohakuaxi/     how the host talks to the machine
```

## Start here

- **[Architecture](arch-design.md)** — the machine top to bottom, how a matmul
  flows through it, and every headline number in one place. Read this first.
- **[Instruction sets](isa/README.md)** — the five ISAs and one worked pass from
  57 host writes to 320 accumulator commands. **This folder is the most accurate
  description of what the machine does today**; where anything else disagrees
  with it, it is right and the other document is behind.
- [Where the cycles go](perf.md) — **§0 is the current measured rate** at every
  shape and cluster count; the rest is the diagnosis that got it there
- [1×5, 2×2 and the driver bench](system.md) — the machine actually running
- [Pipeline, cycles and resources](compute/timing.md) — every latency and
  measured figure for the compute path

## [Instruction sets](isa/README.md)

Five levels, each narrower than the one above and none able to express the one
below, which is why there are five rather than one.

- [Control program](isa/orchestrator.md) — host → `main_orch`: `WR`, `POLL`, `DONE`
- [Dispatch registers](isa/agent.md) — orchestrator → agent: stage, kick, credit
- [CU instructions](isa/cluster.md) — agent → cluster: `FILL`, `GEMM`, `DRAIN`
- [Accumulator ops](isa/acu.md) — manager → ACU: `LOAD`, `ADD`, `EMIT`
- [Memory protocol](isa/memory.md) — CU ↔ MAG: read, write, and the quantiser
- [The driver-side contract](isa/kernel.md) — how a GEMM of arbitrary size
  becomes passes and rounds, and why operands are stored tile-major
- [Vector ISA](isa/vector.md) — **design**: agent → vector core, and the first
  instruction set in the machine that can branch

## [Compute](compute/README.md) — `src/kohakutpu/`

- [Tensor Core ISA](compute/tensor-isa.md) — the **agreed design** for tensor descriptors, L1/L2, and convolution as a memory request. Not what is built; [isa/cluster.md](isa/cluster.md) is
- [Matmul unit](compute/matmul.md) — **current design**: tensor CU, cluster chain, accumulator network
- [Matmul circuit](compute/matmul-circuit.md) — DSP48E2 packing and cascade
- [Matmul implementation](compute/matmul-impl.md) — built, verified, measured
- [Accumulator](compute/accumulator.md) — precision and cost vs mantissa width, and the 85 → 312 MHz timing history
- [Vector core](compute/vector-core.md) — the **ALU is built**: E8M15, 3 DSP,
  324.8 MHz, correctly-rounded FMA and full-rate exp2/log2/inv/rsqrt. The core,
  register file, L1 and split-K epilogue around it are design
- [Vector bring-up](compute/vector-bringup.md) — **design**: 4 cores, 4 MAG
  ports, no matmul in the machine at all, and the driver / codegen / cost model
  that gets one running before the core RTL exists
- [Arithmetic](compute/arithmetic.md) — FP8/FP16/FP24 constructions, division, log, exp
- [DSP usage](compute/dsp.md) — DSP48E2 modes, pipelining, operand packing
- [Costs](compute/costs.md) — measured resource cost and throughput, for the **superseded** FP8→FP12 units
- [Controller](compute/controller.md) — superseded by [isa/cluster.md](isa/cluster.md)

## [NoC](noc/README.md) — `src/kohakunoc/`

- [Specification](noc/spec.md) — packet format, routing, CU interface, AXI interworking
- [CU framework](noc/cu-framework.md) — what every compute unit must have, and what it gets free
- [Resource budget](noc/resource-budget.md) — measured NoC cost, and what is left for compute
- [Simulation](noc/simulation.md) — mesh, orchestrator, multi-CU benches

## [MAG](mas/README.md) — memory access gateway — `src/kohakumas/`

- [Specification](mas/spec.md) — the adapter shape, **built**; address slicing, TLB and bandwidth sizing, **design**
- [Memory system](mas/cache.md) — **design**: two caches, and why only one has tags
- [Driver interface](mas/driver.md) — superseded by [isa/](isa/README.md); kept for the measured concurrency numbers

## [The interlink](interlink/README.md) — four meshes, one machine

A mesh spanning three SLRs measured a worst path of **4.6 ns that was 98.3%
routing with zero logic levels**, so the machine is four independent meshes, one
per SLR, each with its own DDR4, joined MAG to MAG.

- [README](interlink/README.md) — why four, and why MAG is the boundary
- [Paths](interlink/paths.md) — the four cross-mesh paths, two built and two not
- [Boundary](interlink/boundary.md) — **what a driver must not assume**: how to
  tell one mesh from four, and why every new field reads zero on old silicon
- [Topology](interlink/topology.md) — ports, the second routing layer, the
  measured SLR floorplan, and the shipped mesh maps
- [Protocol](interlink/protocol.md) — packet format, credits, deadlock
- [Transfers](interlink/transfers.md) — the three kinds and who starts them

## [Driver and compiler](driver/README.md) — `src/ktpu/`

- [The stack](driver/README.md) — the three IR levels and why not MLIR
- [IR](driver/ir.md), [scheduling](driver/scheduling.md),
  [DSL](driver/dsl.md), [tinygrad](driver/tinygrad.md)
- [Hardware API](driver/hardware-api.md) — the transport contract, the four
  backends, and the guard that is not a cap
- [Simulators](driver/simulators.md) — what runs a schedule without hardware

## Other subsystems

- [Memory mover](memory-mover/README.md) — DRAM to DRAM without the NoC
- [General cores and SLRs](general_cores/README.md) — the scalar side, and
  [crossing SLRs](general_cores/slr.md), which is where the device's measured
  facts live
- [What this machine cannot do](limits.md) — the op-set gaps, each marked HW,
  SW or WORKAROUND

## AXI — `src/kohakuaxi/`

Also home to `main_orch.v`, the host-facing half of the orchestrator; its ISA and
register map are [isa/orchestrator.md](isa/orchestrator.md).

- [Bring-up](axi/bringup.md) — why a slave can pass its own testbench and hang on hardware
- [Simulation](axi/simulation.md) — hostile-master slave bench, burst-legality master bench

## Making it faster

- [Optimization](optimization.md) — every lever considered, with a verdict:
  taken, deferred, rejected, or **§J shelved by measurement** with the condition
  that would revive it
- [Dataflow](dataflow.md) — the signal-level before/after for the levers taken

## Process

- [Simulation setup](simulation.md) — the tiered `check.py` loop, tools, bench
  conventions, and the tool traps that have cost real time

```
   python scripts/py/check.py fast     ~5 s     run this after every edit
   python scripts/py/check.py unit     ~70 s    before believing anything works
   python scripts/py/check.py full     ~6 min   before calling something done
```
