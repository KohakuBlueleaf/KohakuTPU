---
title: KohakuMPE
summary: A project whose compute units are processors rather than a fixed datapath — two processing-element classes, what each is for, how a configuration is priced, and which part of it fills a framework slot.
tags:
  - projects
  - overview
  - mpe
---

# KohakuMPE

> **Kind: mixed, and the split is the point.** Both PE classes are compute units
> like any other — **Yours** inside, behind the framework's **Fixed protocol**
> port. One part is different in kind: the SIMD unit also fills the framework's
> `SIMD_EN` slot, which makes it a **customizable addon** occupant rather than
> only a compute unit ([integrate/addon-slots](../../integrate/addon-slots.md)).

`src/kohakumpe/` — what [KohakuAccel](../../README.md) builds when the compute
units on a mesh are **processors** rather than a fixed datapath.

Three terms are needed before anything else on this page.

| term | meaning |
|---|---|
| **compute unit** | the block a project writes and the framework attaches. It hangs off a **mesh** — the on-chip network that carries instructions to it and results back — through one port whose signals and protocol the framework fixes. |
| **PE** (processing element) | a compute unit that fetches and executes instructions. Every PE here is the framework's RV32 controller PE with a wide datapath bolted to its execute stage. |
| **lane** | one copy of the arithmetic. A datapath *W* lanes wide serves *N* elements in `N/W` **passes**, one issued per cycle, sequenced by hardware that no instruction can see. |

Where [KohakuTPU](../kohakutpu/README.md) puts a matrix cluster and a vector
core on the port, this project puts machines with a program counter. Neither
project documents framework mechanism: how a router forwards a flit, or how the
memory agent turns a descriptor into DRAM bursts, is [arch/](../../arch/README.md).

## Two PE classes

Each is the framework's [controller PE](../../arch/cpu/rv32-pe/README.md) with a wide
datapath behind it. Everything the base core is stays true in both — RV32IM, one
port on the fabric, the same kick and the same completion.

| | source | what one instruction drives |
|---|---|---|
| [simd/](simd/README.md) | `src/kohakumpe/simd/` | one operation over eight 32-bit integer lanes and a separately-sized float tier. One program counter, one address stream. |
| [simt/](simt/README.md) | `src/kohakumpe/simt/` | one instruction stream over many threads, each with its own address and its own path through a branch. |

The distinction is not lane count. It is **whether the lanes may disagree** — on
an address, or on which side of a branch they are executing. SIMD says no and is
cheaper for it; SIMT says yes and pays for an active mask, a divergence stack
and a lane-serialising load/store unit.

### What selects which

| the property of the work | the unit |
|---|---|
| every element treated the same, one address stream, arithmetic dense | **SIMD PE** |
| lane 3 takes the `if` and lane 4 the `else`; an address per lane | **SIMT PE** |
| a fixed dataflow, operands resident across many passes, no control flow | **not a PE at all** — that is a systolic array, and neither class pretends to be one |
| deciding what happens next | the plain **controller PE**, or the system node's control processor if the work is dispatch rather than compute |

## One float format, and it is not a setting

```
   IEEE binary32 in   ->   binary32 compute   ->   binary32 out
```

Both classes compute in binary32 and nothing else. There is no second format,
no conversion at the operand edge, and no parameter in either PE that selects
one. Denormals flush to sign-preserved zero on input and output, which is
D3D11's functional requirement.

Both instantiate the same float units — `rv_fpu` for the fused multiply-add and
`khs_fp32_sfu` for the four seeds — so a SIMD float result and a SIMT float
result agree element for element, and only the addressing differs.

KohakuTPU's vector core computes in its own E8M15 format with its own modules
and shares none of this. **There is no E8M15 anywhere in KohakuMPE.**

## Every compute feature is a width

Both PEs are configured the same way, and it is the one idea the reader needs
to hold: **each feature that has a width is an independent unit count, and 0
means the feature is not built.** A narrower count costs cycles, never
encodings, and never changes an answer. [configurable-widths](configurable-widths.md)
is the specification; [unit-counts](unit-counts.md) prices it.

## Where it meets the framework

**The SIMD unit fills a framework slot.** `src/kohakuaccel/pe/rv32/` names
`khs_unit` and `khs_scalar_decode` behind the parameter `SIMD_EN`, which is 0 by
default, so a framework-only build never elaborates either and the names need
not resolve. KohakuMPE supplies them, and the framework's RV32 core is the base
core bit for bit without them.

The unit went to a project rather than into the framework because it carries a
whole ISA extension, its own register file and its own scratchpad — none of
which the framework has an opinion about. `scripts/py/deps.py` holds the rule
that a framework module may not instantiate a project one except through a named
slot, and it is in the standard check suite.

The SIMT PE inherits `khs_fp32_sfu` from the SIMD tier verbatim and never forks
it — the same edge, within one project.

## What is measured, and what is not

Every resource figure in these pages is **out-of-context synthesis** of one PE
on `xcvu13p-fhgb2104-2L-e` with Vivado 2024.2. Nothing here is placed or routed.

**No frequency figure in this project is a closed-timing result.** They are
synthesis estimates, they are the optimistic end — this repository has measured
a module lose 0.740 ns between synthesis and routing — and they move by tens of
megahertz between rows that differ in nothing that should matter. Treat them as
a screen for a structural problem, not as a result.

**Every published LUT total predates the current float tier.** The tier was
rebuilt from an E8M15 datapath with two operand formats into a binary32-only
one; at the same time the integer dot unit, its accumulator, the `MULS`
multiplier-depth knob and the `DOT_DSP` mapping knob were removed, and the
converter group gained the datapath it had been missing. The figures in
[unit-counts](unit-counts.md) and [width-cost](width-cost.md) each name the tree
they were taken on and are kept because the *shapes* they establish — what a
marginal lane costs, where a width pays, which knobs are not levers — are the
findings. **No absolute total on any page here describes a PE that can be built
from the RTL as it now stands**, and re-measurement against the current
parameter set has not been published.

## Reading order

Start with whichever class the work selects.

- **[configurable-widths](configurable-widths.md)** — the width mechanism, both
  cores, and the elaboration rules. Read this first if you are choosing a
  configuration.
- **[simd/](simd/README.md)** — then [lanes](simd/lanes.md),
  [float](simd/float.md), [memory](simd/memory.md), the
  [pipeline](simd/pipeline.md), [programming](simd/programming.md),
  [configurations](simd/configurations.md), [performance](simd/performance.md),
  and [gates](simd/gates.md) for what must pass before a number is quotable.
- **[simt/](simt/README.md)** — [status](simt/status.md) first for what is
  built, then [isa](simt/isa.md),
  [microarchitecture](simt/microarchitecture.md), [ladder](simt/ladder.md) for
  how the cost of SIMT itself is isolated, and [comparison](simt/comparison.md)
  for what the arithmetic width is worth against shipped mobile GPUs.
- **[unit-counts](unit-counts.md)** — the price list: the marginal LUT cost of
  one more unit of each feature, taken as the difference between two synthesised
  rows that differ in one count.
