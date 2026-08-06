# Documentation

Design intent and reasoning for KohakuTPU. Each subsystem has a folder mirroring
its source package; the two documents at this level are the ones that span all of
them.

```
docs/
├── arch-design.md      the machine as a whole
├── simulation.md       how to run and write benches (all suites)
│
├── compute/    <->  src/kohakutpu/     what a compute unit does
├── noc/        <->  src/kohakunoc/     how units talk to each other
└── axi/        <->  src/kohakuaxi/     how the host talks to the machine
```

## Start here

- **[Architecture](arch-design.md)** — the machine top to bottom, how a matmul
  flows through it, and every headline number in one place. Read this first.
- [1×5 and 2×2 systems](system.md) — the machine actually running, end to end
- [Pipeline, cycles and resources](compute/timing.md) — every latency and
  measured figure for the compute path

## [Compute](compute/README.md) — `src/kohakutpu/`

- [Tensor Core ISA](compute/tensor-isa.md) — **cluster manager instruction set**: tensor descriptors, L1/L2, and convolution as a memory request
- [Matmul unit](compute/matmul.md) — **current design**: tensor CU, cluster chain, accumulator network
- [Matmul circuit](compute/matmul-circuit.md) — DSP48E2 packing and cascade
- [Matmul implementation](compute/matmul-impl.md) — built, verified, measured
- [Accumulator](compute/accumulator.md) — precision and cost vs mantissa width, and the 85 → 312 MHz timing history
- [Arithmetic](compute/arithmetic.md) — FP8/FP16/FP24 constructions, division, log, exp
- [DSP usage](compute/dsp.md) — DSP48E2 modes, pipelining, operand packing
- [Costs](compute/costs.md) — measured resource cost and throughput
- [Controller](compute/controller.md) — CU instruction set and state machines

## [NoC](noc/README.md) — `src/kohakunoc/`

- [Specification](noc/spec.md) — packet format, routing, CU interface, AXI interworking
- [CU framework](noc/cu-framework.md) — what every compute unit must have, and what it gets free
- [Resource budget](noc/resource-budget.md) — measured NoC cost, and what is left for compute
- [Simulation](noc/simulation.md) — mesh, orchestrator, multi-CU benches

## AXI — `src/kohakuaxi/`

- [Bring-up](axi/bringup.md) — why a slave can pass its own testbench and hang on hardware
- [Simulation](axi/simulation.md) — hostile-master slave bench, burst-legality master bench

## Process

- [Simulation setup](simulation.md) — tools, bench conventions, tool traps
