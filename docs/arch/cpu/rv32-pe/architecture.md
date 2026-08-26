---
title: RV32 PE architecture
summary: The contract — the ISA as implemented, the memory model and its ordering rules, halting, dispatch, and the kick/completion protocol.
tags:
  - architecture
  - cpu
  - rv32
---

# RV32 PE architecture

The [RV32 PE](README.md) is a small in-order RISC-V core packaged as a compute
unit on a KohakuAccel mesh. This page is the part of it that software and the
surrounding system may rely on: an implementation is free to change anything
else, and nothing here, without a spec change.

Where it sits: below it is one router's local port and, through that, the
memory agent that serves its row; above it is whoever kicks it — a host through
the control agent, or another PE. Nothing in the fabric knows it is a
processor.

## The instruction set

**RV32I plus the multiply half of `M`**, unprivileged, executed by an in-order
single-issue core. Ordinary compilers and hand-written assembly work
unmodified. The deviations from a full-featured hart are themselves
architectural:

| Feature | Status |
|---|---|
| RV32I base integer ISA | implemented, co-simulated instruction by instruction |
| **`mul`, `mulh`, `mulhsu`, `mulhu`** | **implemented.** `rv_id.v` accepts `funct7 = 0000001` on the register-register group with `funct3[2] = 0`; one shared 33 × 33 signed multiplier serves all four forms. A multiply costs 3 stall cycles — [microarchitecture](microarchitecture.md#the-multiplier) |
| **`div`, `divu`, `rem`, `remu`** | **absent, and refused by name.** The same decode raises an illegal-instruction halt on `funct3[2] = 1`, so the encoding is recognised and rejected rather than falling through to a multiply |
| CSRs, `Zicsr` | **absent.** No CSR file exists; counters are memory-mapped |
| interrupts, traps, trap vectors | **absent.** The unit halts; it is never interrupted |
| `FENCE` | executes as NOP — one core, one memory port, already ordered |
| `FENCE.I` | not needed: the instruction window is not writable from the data side |
| misaligned load/store | **faults** (RV32I permits either fixup or fault) |
| `ECALL`, `EBREAK` | halt the unit — see [Halting](#halting) |
| **floating point — `F`, `D`, `Zfh`** | **absent.** No `f0..f31`, no `fcsr`, no rounding mode. Not "not yet": there is no float register file to name |
| atomics, `A` | absent; exclusive access between units is ownership and push, not locks |

Counters that would be CSRs elsewhere — cycle, instructions retired, core id —
are words in the local control region
([programming](programming.md#local-control)).

### Where the arithmetic in this machine actually is

Two readings of the table above are each wrong about a different class, so it
is worth stating the whole picture once:

- the **RV32 PE** multiplies and does not divide. No float;
- the **SIMD PE** is this core plus a vector unit. Its scalar half is this
  core, unchanged — the vector multiply and the float tier live behind
  `custom-0` and `custom-1`, in the *vector* register file, and reaching them
  means putting operands in `v0..v7`;
- the **SIMT PE** builds the same multiply half of `RV32M` on its *per-thread*
  register file, one product per lane. Its *uniform* ALU, on `custom-2`, is
  still the ten register-register operations RV32I has and has no multiply.

`div` and `rem` fault on every class built on this core, per-thread included.
**No class has scalar float**; the wide classes' float tier is
[KohakuMPE's](../../../projects/kohakumpe/README.md) to describe, and none of it
is reachable from a scalar register.

`RV32M` cost none of the four custom opcode majors — it went into the standard
`OP` major where RISC-V already put it, which is why a compiler emits it with
`-march=rv32im` and nothing else ([opcode-map](opcode-map.md)).

## The memory model

Software sees ordinary RV32 addresses. **One decoder, deciding on the top four
address bits and nothing else**, and each region's semantics are fixed:

| Region | Semantics |
|---|---|
| instruction window | **not addressable from the data side** — a load or store faults. Self-modifying code is a fault, not a race |
| scratchpad | ordinary read/write memory, always one cycle. Writable by this core and by the fabric; a fabric write landing in a word being read returns the **new** bytes |
| local control | word registers; some reads, some stores with side effects |
| peer windows | **store only** — a store becomes a push into another unit's window; a load faults |
| dispatch | **store only** — three stores construct and fire a `CU_INST` at another compute unit; a load faults |
| vector scratchpad | **store only, and present only when the SIMD extension is built.** Without the extension the region is unmapped and both loads and stores fault |
| global DRAM | ordinary cached read/write memory through the internal L1 |
| everything else | faults |

The concrete map, encodings and register list are in
[programming](programming.md); the region *semantics* above are the contract.

**A push is never cached.** The peer and dispatch regions are decoded
separately from DRAM rather than being "DRAM that happens to live elsewhere":
the doorbell protocol needs stores on the wire in program order, and a dirty
cache line leaves whenever the cache chooses. The memory stage holds until the
requestor has taken the push, which is what makes program order equal arrival
order.

### Ordering

Four rules, and every communication idiom in this machine reduces to them:

1. **Program order is arrival order, per destination.** The requestor emits one
   burst at a time and the mesh preserves order between one source and one
   destination. Two stores to the same peer window arrive in the order the
   program issued them. No rule relates pushes to *different* destinations.
2. **A write is in memory when it is acknowledged, not when it leaves.**
   Completion, flush, and any dependence on "the data is there" wait on the
   acknowledgement.
3. **A store to `CTL_FLUSH` completes only after every dirty line is written
   back and acknowledged.** The instruction after it therefore cannot overtake
   the data — flush-then-doorbell needs no barrier machinery.
4. **A store to `CTL_INVAL` completes only after every line is dropped.** A
   load after it cannot hit a stale line.

Loads and stores inside one core are always self-consistent — the scratchpad
bypass, the cache and the forwarding network exist so that a program reading
its own writes never observes anything but program order.

### What is deliberately absent from the memory model

No coherence, by construction rather than omission: the only externally
writable memory is the home of its own addresses (never a copy), and the only
cached memory is never externally written. No atomics: exclusive access between
units is done by ownership and push, not by locks. No ordering between a peer
push and a DRAM write except through rule 3 — which is exactly what the
[DRAM hand-off](programming.md#dram-hand-off-between-units) sequence exercises.

## Halting

A halt is a redirect that also stops fetch. The halting instruction retires —
it is the one that raised the halt — but does not commit architectural state
beyond that.

| Cause | Raised by | Halt word |
|---|---|---|
| 1 | `ECALL` | `a0` |
| 2 | `EBREAK` | `a0` |
| 3 | illegal encoding, misaligned access, or an unmapped region | the offending PC |

`a0` in the halt word is the committed value: a halt redirects, so nothing
younger than the halting instruction commits, and everything older already has.
Software's convention for what `a0` carries — a result, an error code — is its
own.

Every fault this core has is raised in the EX stage and takes one path,
including the ones the memory stage's address decoder finds. That is why an
unmapped region halts at the offending PC rather than in a stage the redirect
path cannot reach.

The cause and word are readable at `CTL_CAUSE` after the halt, and travel in
the completion signal below.

## The unit protocol

The RV32 PE is a compute unit first. Its externally visible life is the same as
every unit on this fabric:

- **Windows are written from outside** as `CU_DATA` — the program image into
  the instruction window, data into the scratchpad. Boot is not a mechanism: it
  is these ordinary writes plus a kick ([programming](programming.md#boot)).
- **A kick** is `CU_INST` with `op = 1`, a start PC and one argument word. Any
  other opcode retires immediately without running anything. **A kick never
  overtakes the data it announces**: the unit holds a kick until its receive
  path is quiet, so an image still being written when the kick arrives is
  finished before fetch begins. The hold clears by the unit's own progress and
  cannot deadlock.
- **Completion** is `CU_SIGNAL` to whoever kicked, carrying the halt word. Code
  `0x00` is an `ECALL` halt; `0x04` is `EBREAK` or a fault. Completion asserts
  three things at once: the pipeline is empty, the requestor is idle, and
  **every write the program issued has been acknowledged by memory**. A host
  that reads DRAM on seeing the completion finds the program's results there —
  the completion is the host's sequencing point, and it would mean nothing
  weaker.

### The PE as a dispatcher

The same unit can be on the *sending* side of that protocol. Two facilities
make it a controller rather than only a worker, and both are contract:

- **Dispatch.** Three stores into the dispatch region write an argument word, a
  start PC, and then an opcode word that fires a `CU_INST` at a named
  destination coordinate. The opcode store is the doorbell and is the only one
  of the three that can stall; the other two write requestor registers and
  always accept. The transaction field is a program id locally, and — when the
  flit is marked remote — the final coordinate in the target mesh, so software
  owns which it is.
- **Completions in.** `CU_SIGNAL` flits addressed to this PE land in an
  **8-entry completion queue** readable through the control region: a count, a
  sticky overflow bit, the head's code and id, the head's argument word, and a
  store that retires the head. The queue is bounded, so a lost completion is
  **detectable** — the overflow bit says so — rather than silently absorbed.

A mesh can therefore execute a dependency graph with the host out of the loop:
each PE kicks its successors and waits on their completions. The encoding and
the idiom are in
[programming](programming.md#dispatch-and-completions).

## What this core deliberately does not do

Stated once, so a reader is not left inferring absence from silence:

| Not present | Why, and where the reasoning is |
|---|---|
| divide and remainder | a fixed ~35-cycle structure with its own subtractor, bought for an instruction a controller issues approximately never — [microarchitecture](microarchitecture.md#why-div-and-rem-are-a-different-answer) |
| scalar floating point | fifteen cycles into a three-source in-order forwarding network, and it would not be `F`, so no compiler would emit it — [microarchitecture](microarchitecture.md#why-minimal-scalar-float-is-the-wrong-purchase) |
| CSRs, privilege, interrupts | the unit halts and reports; there is no supervisor to trap to. A core that boots once and runs forever is a different processor — [rv64-sys](../rv64-sys/README.md) |
| an MMU | the PE's addresses are its windows plus a DRAM aperture fixed at elaboration |
| cache coherence | removed by construction: nothing cached is externally written |
| multiple outstanding misses | the internal L1 is blocking with one outstanding miss; latency tolerance comes from having many independent units |
| a loader | an image arrives on the same write path as any other data, so there is no second memory-write protocol to go wrong |
