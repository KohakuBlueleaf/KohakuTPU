---
title: KohakuAccel compiler framework
summary: A frameworkized three-level IR for MAG + NoC-mesh accelerators, plus the tools that make a frontend, a backend and an IR easier to design.
tags:
  - compiler
  - framework
---

# The compiler stack

Independent of the driver. The compiler produces an **artifact**; the driver
executes one. Neither imports the other.

## What we ship, and what "framework" means here

Shipping a working middle is not enough. A framework is judged by what it makes
possible, so the question this package answers is:

> **What would otherwise stop you building a compiler on top of MAG + NoC mesh,
> when your workload is inside our scope?**

Three answers, and they are the three halves of this package:

1. **A working middle.** Placement, round packing, coalescing, completion
   accounting, emission. Machine-determined, identical for every workload.
2. **An IR you inherit rather than invent.** Three levels, with traversal,
   verification, printing and a pass manager already written. You define what
   your nodes MEAN; you do not write a compiler infrastructure.
3. **Tools for the two ends.** Builders that make an L3 graph without hand-wiring
   it, and a declarative ISA toolkit that turns a field table into an encoder, a
   decoder, a validator and a disassembler.

Point 3 is the one people skip and then regret. Hand-rolled bit packing is
exactly the defect class `noc_pkt.vh` demonstrates in RTL — one layout restated
in seven places, correct only by agreement — and a unit ISA invites the same
mistake in Python.

## The pipeline

    your frontend      L3 graph        L2 schedule       L1 program      your backend
    tensor ops    ->   what the   ->   where and     ->  instruction ->  bits on
    scene              work is         when                streams        the wire
    filter graph
    task set
      PROJECT          FRAMEWORK IR    FRAMEWORK       FRAMEWORK IR     PROJECT
                       + your nodes    (all of it)     + your encoding

Three levels, and the claim is that **every workload in scope has all three** —
only the content differs.

| | L3: graph | L2: schedule | L1: program |
|---|---|---|---|
| **tensor** | shaped tensor ops, fusion | passes over tiles, on clusters | GEMM/FILL/DRAIN instructions |
| **ray tracing** | scene, BVH build, bounce stages | tiles x bounces, on units | trace/shade instruction per tile |
| **DSP** | a filter graph | stage x block, pinned pipeline | filter opcodes and coefficients |
| **CPU mesh** | parallelizable task decomposition | sub-kernel per core, per superstep | the sub-kernel's compiled code |

For a CPU mesh the chain reads: *complex parallelizable task* -> *a graph of how
it splits into parallel stages* -> *a schedule binding stages to cores and
supersteps* -> *one sub-kernel per core* -> your own compiler turns that
sub-kernel into code. The last arrow is a backend we do not own, and the ISA
toolkit is aimed exactly there.

## What the topology forces — the reason a middle exists at all

Six constraints, none from a workload.

**1. Work must be placed on coordinates.** Endpoints live at `(x, y)`.

**2. Distance is computable.** XY dimension-order routing makes hops between two
endpoints exactly `|Δx| + |Δy|`, so a placement cost function exists without
knowing what is placed.

**3. Memory is reached by descriptor, ahead of time.** No demand fetch, so every
compiler emits explicit movement and every task has a statically known footprint
or does not fit.

**4. Dispatch is in bounded rounds.** `stage_flits` and `ncmd` bound one round;
packing is the same arithmetic for a GEMM or a bounce.

**5. Credit bounds in-flight instructions per unit.** Exceeding `INST_DEPTH` does
not slow the machine, it wedges it: a full instruction FIFO backpressures the
link carrying the memory responses that unit is waiting for. A scheduler that
does not model this emits programs that hang.

**6. Completion is counted, not named.** Knowing how many completions a round
produces is a compile-time obligation.

## What the topology gives — and why it generalises

**Multi-destination reads.** A read request carries extra destinations, so one
fetch, one pass through the transform stage, serves several units. Usually
described as a tensor trick — every cluster sweeps the same rows of A — but it is
nothing of the kind:

- tensor: shared A-operand rows
- ray tracing: BVH top levels, which every tile reads
- CPU mesh: a shared code page
- DSP: a shared coefficient table

**So coalescing is a framework pass.** It depends only on two tasks declaring the
same region, never on what the region holds. The *communication* optimisations
generalise even though the *computation* does not — that is the payoff of a NoC
substrate, and it is most of why the middle is worth having.

## The upper seam: builders, so a frontend is not hand-wired

Four shapes cover every workload above:

| builder | shape | used by |
|---|---|---|
| `spread` | one domain, N independent pieces | GEMM tiles, ray tiles, DSP blocks, SPMD cores |
| `chain` | stage k feeds stage k+1 | DSP pipelines, multi-pass rendering |
| `gather` | many pieces reduce into one | K-reduction, ray accumulation, histogram merge |
| `iterate` | repeat a body, barrier between | bounces, solver iterations, CPU supersteps |

A ray-tracing frontend is roughly `iterate(bounces, lambda k: spread(tiles,
trace(k)))`. A CPU-mesh frontend is `spread` with `policy=PINNED`. A DSP frontend
is `chain`. They compose, and composing them is what a frontend is at this layer.

## The lower seam: an ISA you declare rather than pack

The backend contract is four methods, one required. But the work behind `encode`
is where projects lose time, so the framework ships a field-table toolkit:

    LOAD = InstFormat("LOAD", [
        Field("op",   8, const=0x01),
        Field("dst",  4),
        Field("addr", 34),
        Field("len",  16),
    ])

From that one declaration you get `encode(**kwargs)` with range checking on every
field, `decode(word)`, a disassembler, and a round-trip test. Overlapping or
over-wide fields raise at construction rather than producing traffic that routes
plausibly and means something else.

## Where this stops

- **Software pipelining across rounds does not fit.** A barrier separates rounds.
  A DSP chain wanting stage `k` of block `b+1` overlapped with stage `k+1` of
  block `b` wants what the round model forbids — such a workload emits ONE round
  of long-running tasks that stream unit-to-unit and pipelines inside the units.
- **Data-dependent dispatch does not fit.** A footprint must be known before
  staging; discovering what to read by reading means splitting into rounds and
  paying a host round trip.
- **Dynamic work stealing does not fit.** Placement is compile-time. Uneven ray
  tiles will straggle; the answer is smaller tasks and more rounds.
- **Tiling is not ours.** Tile shape needs capacities, reuse and a cost model that
  are project-specific. It happens at L3.

## Layout

    compiler/
      kohakuaccel/
        ir/          base.py l3.py l2.py l1.py verify.py printer.py
        passes/      manager.py infer.py place.py pack.py coalesce.py emit.py
        frontend/    build.py domain.py
        backend/     isa.py slots.py
        machine.py   where units are, what bounds a round, hop cost
        artifact.py  the symbolic schedule a driver executes
        compile.py   the default pipeline
      kohakutpu/     the tensor frontend and backend
      examples/      saxpy, readable in one sitting
      tests/
