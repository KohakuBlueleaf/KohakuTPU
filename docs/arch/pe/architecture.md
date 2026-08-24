---
title: PE architecture
summary: The contract — the ISA as implemented, the memory model and its ordering rules, halting, and the kick/completion protocol. Everything an implementation change must preserve.
tags:
  - architecture
  - pe
  - rv32
---

# PE architecture

What software and the surrounding system may rely on. Everything on this page
is contract: an implementation is free to change anything else, and nothing
here, without a spec change.

## The instruction set

RV32I, unprivileged, executed by an in-order single-issue core. Ordinary
compilers and hand-written assembly work unmodified. The deviations from a
full-featured hart are themselves architectural:

| Feature | Status |
|---|---|
| RV32I base integer ISA | implemented, co-simulated instruction by instruction |
| CSRs, `Zicsr` | **absent.** No CSR file exists; counters are memory-mapped |
| interrupts, traps, trap vectors | **absent.** The unit halts; it is never interrupted |
| `FENCE` | executes as NOP — one core, one memory port, already ordered |
| `FENCE.I` | not needed: the instruction window is not writable from the data side |
| misaligned load/store | **faults** (RV32I permits either fixup or fault) |
| `ECALL`, `EBREAK` | halt the unit — see [Halting](#halting) |
| **`RV32M` — `mul`, `mulh`, `div`, `rem`** | **absent on this core, and the encodings fault.** `rv_id.v` accepts `funct7` of `0000000` and `0100000` on the register-register group and nothing else, so RV32M's `0000001` raises an illegal-instruction halt at the offending PC. The SIMT PE decodes the same `funct7` and *does* build `mul`/`mulh`/`mulhsu`/`mulhu` per thread — [below](#no-scalar-datapath-in-this-machine-has-a-multiplier) |
| **floating point — `F`, `D`, `Zfh`** | **absent in the scalar core.** No `f0..f31`, no `fcsr`, no rounding mode. Not "not yet": there is no float register file to name |
| atomics, `A` | absent; exclusive access between cores is ownership and push, not locks |

Counters that would be CSRs elsewhere — cycle, instructions retired, core id
— are words in the local control region
([programming](programming.md#local-control)).

### No *scalar* datapath in this machine has a multiplier

Worth stating once, plainly, because the two easy readings — "the machine has
float" and "the machine cannot multiply" — are each wrong about a different
class:

- the **controller PE** has no multiply, no divide and no float. `mul` faults;
- the **SIMD PE** is this core plus a vector unit. Its scalar half is unchanged —
  the multiply and the float live behind custom-0 and custom-1, in the *vector*
  register file, and reaching them means putting operands in `v0..v7`;
- the **SIMT PE** has **`RV32M` per thread**. `mul`, `mulh`, `mulhsu` and `mulhu`
  execute on the *per-thread* register file, addressed by the standard `OP`
  encoding, one product per lane — so a shader that writes `a * b` gets one
  instruction, not a libgcc call. Its *uniform* ALU, on custom-2, is still the
  same ten register-register operations RV32I has, and has no multiply.

So: a **scalar** register-register multiply exists nowhere. `div` and `rem`
fault on every class, per-thread included.

**Float exists in this machine, is measured, and is in both wide classes** —
one E8M15 fused multiply-add per element, 4 float lanes on the SIMD PE and 8 on
the SIMT PE, both built and both measured
([the PE classes](README.md#8-int--4-float-is-the-dsp-reference-8-int--8-float-is-the-gpu-reference)).
None of it is reachable from a scalar register on any class.

Whether *this core* should change, and what each option would cost here, is
[The arithmetic the EX stage does not have](microarchitecture.md#the-arithmetic-the-ex-stage-does-not-have)
— which now also records where the multiplier that did get built went, and why
that was the cheap place for it. Nothing enters this baseline because it is
normally found in a CPU: each addition is a separate experiment with a
measurement attached.

## The memory model

Software sees ordinary RV32 addresses. The top four address bits select a
region, and each region's semantics are fixed:

| Region | Semantics |
|---|---|
| instruction window | **not addressable from the data side** — a load or store faults. Self-modifying code is a fault, not a race |
| scratchpad | ordinary read/write memory, always one cycle. Writable by this core and by the NoC; a NoC write landing in a word being read returns the **new** bytes |
| local control | word registers; some reads, some stores with side effects |
| peer windows | **store only** — a store becomes a push to another unit's window; a load faults |
| global DRAM | ordinary cached read/write memory through the internal L1 |
| everything else | faults |

The concrete map, encodings and register list are in
[programming](programming.md); the region *semantics* above are the contract.

### Ordering

Four rules, and every communication idiom in this machine reduces to them:

1. **Program order is arrival order, per destination.** The requestor emits
   one burst at a time and the mesh preserves order between one source and
   one destination. Two stores to the same peer window arrive in the order
   the program issued them. No rule relates pushes to *different*
   destinations.
2. **A write is in memory when it is acknowledged, not when it leaves.**
   Completion, flush, and any dependence on "the data is there" wait on the
   acknowledgement.
3. **A store to `CTL_FLUSH` completes only after every dirty line is written
   back and acknowledged.** The instruction after it therefore cannot
   overtake the data — flush-then-doorbell needs no barrier machinery.
4. **A store to `CTL_INVAL` completes only after every line is dropped.** A
   load after it cannot hit a stale line.

Loads and stores inside one core are always self-consistent — the scratchpad
bypass, the cache and the forwarding network exist so that a program reading
its own writes never observes anything but program order.

### What is deliberately absent

No coherence, by construction rather than omission: the only externally
writable memory is the home of its own addresses (never a copy), and the only
cached memory is never externally written. No atomics: exclusive access
between cores is done by ownership and push, not by locks. No ordering
between a peer push and a DRAM write except through rule 3 — which is exactly
what the [DRAM hand-off](programming.md#dram-hand-off-between-units)
sequence exercises.

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
younger than the halting instruction commits, and everything older already
has. Software's convention for what `a0` carries — a result, an error code —
is its own.

The cause and word are readable at `CTL_CAUSE` after the halt, and travel in
the completion signal below.

## The unit protocol

The PE is a compute unit first. Its externally visible life is the same as
every unit on this fabric:

- **Windows are written from outside** as `CU_DATA` — the program image into
  the instruction window, data into the scratchpad. Boot is not a mechanism:
  it is these ordinary writes plus a kick
  ([programming](programming.md#boot)).
- **A kick** is `CU_INST` with `op = 1`, a start PC and one argument word.
  Any other opcode retires immediately without running anything. **A kick
  never overtakes the data it announces**: the unit holds a kick until its
  receive path is quiet, so an image still being written when the kick
  arrives is finished before fetch begins. The hold clears by the unit's own
  progress and cannot deadlock.
- **Completion** is `CU_SIGNAL` to whoever kicked, carrying the halt word.
  Code `0x00` is an `ECALL` halt; `0x04` is `EBREAK` or a fault. Completion
  asserts three things at once: the pipeline is empty, the requestor is
  idle, and **every write the program issued has been acknowledged by
  memory**. A host that reads DRAM on seeing the completion finds the
  program's results there — the completion is the host's sequencing point,
  and it would mean nothing weaker.
