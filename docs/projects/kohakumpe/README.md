---
title: KohakuMPE
summary: A project whose compute units are processors rather than a fixed datapath — two PE classes, what each costs, what work selects which, and which parts of it fill framework slots.
tags:
  - projects
  - overview
  - mpe
---

# KohakuMPE

`src/kohakumpe/` — what KohakuAccel builds when the compute units on the mesh are
**processors** rather than a fixed datapath. Where
[KohakuTPU](../kohakutpu/README.md) puts a matmul cluster and a vector core on
the port, this project puts machines that fetch and execute instructions.

Both are projects on the same framework, and neither is documented here as
framework mechanism: how a router forwards or how the memory agent turns a
descriptor into bursts is [arch/](../../arch/README.md).

## Two PE classes

Each is the framework's [controller PE](../../arch/pe/README.md) with a wide
datapath behind it. Everything the base core is stays true in both — RV32I, one
port on the fabric, the same kick and the same completion.

| | | |
|---|---|---|
| [simd/](simd/README.md) | `src/kohakumpe/simd/` | One instruction over eight integer lanes and four float lanes. One program counter, one address stream. |
| [simt/](simt/README.md) | `src/kohakumpe/simt/` | One instruction stream over many threads, each with its own address and its own path through a branch. |

The distinction is not lane count. It is **whether the lanes may disagree** — on
an address, or on which side of a branch they are executing. SIMD says no and is
cheaper for it; SIMT says yes and pays for an active mask, an IPDOM stack and a
lane-serialising load/store unit.

### What selects which

| the property | the unit |
|---|---|
| every element treated the same, one address stream, arithmetic dense | **SIMD PE** |
| lane 3 takes the `if` and lane 4 the `else`; an address per lane | **SIMT PE** |
| a fixed dataflow, operands resident across many passes, no control flow | **not a PE at all** — that is a systolic array, and neither class pretends to be one |
| deciding what happens next | the plain **CPU PE**, or the system node's control processor if it is dispatch rather than compute |

## What each one measures

Out-of-context synthesis on `xcvu13p-fhgb2104-2L-e`, Vivado 2024.2, **synthesis
only — nothing here is placed or routed**, so every frequency is an upper bound
on what a placed design would achieve, not a closed result.

| | LUT | FF | BRAM | DSP48 | Fmax | conditions |
|---|---|---|---|---|---|---|
| SIMD PE, 8 int + 4 float | 13,772 | 10,126 | 13 | 72 | 353.4 MHz | assembled `rv_pe`, 2.857 ns ask |
| SIMT PE, 8 lanes / 16 waves | 20,086 | 17,282 | 30.5 | 48 | 392.0 MHz | `kht_pe`, 2.857 ns ask, `-flatten_hierarchy rebuilt` |

The SIMT figure is at `rebuilt` because that is what the ship's synthesis run
takes; the same design at `none` reads 636 LUT high. Each page carries its own
provenance and its own retractions — the SIMT one keeps
[status](simt/status.md) as the single page saying what has actually been run.

**[unit-counts](unit-counts.md) prices the two classes feature by feature** —
the marginal LUT cost of one more lane, one more wave, one more float unit,
taken as the difference between two synthesised rows that differ in one count.
It is what makes a configuration answerable without synthesising it.

## Where it meets the framework

**The SIMD unit fills a framework slot.** `src/kohakuaccel/pe/rv32/` names
`khs_unit` and `khs_scalar_decode` behind `SIMD_EN`, which is 0 by default, so a
framework-only build never elaborates either and the names need not resolve.
KohakuMPE supplies them. The framework's rv32 is the base core bit for bit
without it.

That shape exists because the unit's float tier computes in KohakuTPU's E8M15
format and instantiates that project's arithmetic. Moving the arithmetic *into*
the framework would have put one project's number format there; the whole unit
went to a project instead, where project→project is the allowed direction.
`scripts/py/deps.py` holds the rule and is in the standard check suite.

The SIMT PE inherits `khs_float_lane` from the SIMD tier verbatim and never
forks it — the same edge, within one project.

## Reading order

Start with whichever class the work selects.

- **[simd/](simd/README.md)** — what is a parameter and what is not, then
  [lanes](simd/lanes.md), [float](simd/float.md), the
  [accumulator](simd/accumulator.md), [memory](simd/memory.md), the
  [pipeline](simd/pipeline.md), [programming](simd/programming.md),
  [configurations](simd/configurations.md), [performance](simd/performance.md),
  and [gates](simd/gates.md) for what must pass before a number is quotable.
- **[simt/](simt/README.md)** — [status](simt/status.md) first, then
  [isa](simt/isa.md), [microarchitecture](simt/microarchitecture.md),
  [ladder](simt/ladder.md) for how each figure was arrived at, and
  [comparison](simt/comparison.md) for what the numbers are worth in industry
  terms.
