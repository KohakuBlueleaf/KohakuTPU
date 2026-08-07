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
├── simulation.md       how to run and write benches (all suites)
│
├── isa/                every instruction set, host down to accumulator
├── compute/    <->  src/kohakutpu/     what a compute unit does
├── noc/        <->  src/kohakunoc/     how units talk to each other
├── mas/        <->  src/kohakumas/     how units reach memory
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

## [Compute](compute/README.md) — `src/kohakutpu/`

- [Tensor Core ISA](compute/tensor-isa.md) — the **agreed design** for tensor descriptors, L1/L2, and convolution as a memory request. Not what is built; [isa/cluster.md](isa/cluster.md) is
- [Matmul unit](compute/matmul.md) — **current design**: tensor CU, cluster chain, accumulator network
- [Matmul circuit](compute/matmul-circuit.md) — DSP48E2 packing and cascade
- [Matmul implementation](compute/matmul-impl.md) — built, verified, measured
- [Accumulator](compute/accumulator.md) — precision and cost vs mantissa width, and the 85 → 312 MHz timing history
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
