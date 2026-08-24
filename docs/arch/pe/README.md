---
title: Controller PE
summary: A small RV32I processor whose memory interface was designed around KohakuAccel from the start — the controller for any accelerator built on this framework.
tags:
  - architecture
  - pe
  - rv32
---

# Controller PE

`src/kohakuaccel/pe/rv32/` — an RV32I processing element that attaches to the
fabric exactly like every other compute unit: one local port, one instruction
FIFO, the four `CU_CTRL` registers. A driver enumerates it without knowing it
is a processor. The only thing different is what it does with an instruction:
a `CU_INST` is a **kick**, and the unit retires when the program halts.

The layout is the architecture: `core/` is the pipeline, `mem/` the two L1s,
`noc/` the fabric attach, and `rv_pe.v` assembles them and holds nothing else.

## The problem it solves

Every accelerator built on this framework needs something that decides *what
to do next* — sequencing kernels, walking descriptors, reacting to
completions. Hand-written state machines do it until the day the policy
changes, and then they do it wrong. A small programmable core does it in
software, and the framework's own communication idiom — push and doorbell —
gives many of them a way to coordinate without locks and without coherence.

The design objectives, in order: RV32I compatibility so ordinary compilers
work; **very low LUT**, so dozens of PEs on one device are realistic; **high
Fmax**, so scalar control is never the slow component; and a memory frontend
that is part of the core rather than a generic CPU bus with an adapter bolted
on. LUT and frequency outrank latency everywhere — the core spends FF and
BRAM freely and prefers a pipeline stage over a bypass. One number locates
it: **2,477 LUT, 4,140 FF and 5 BRAM at 377.9 MHz** on
`xcvu13p-fhgb2104-2L-e` at a **3.333 ns ask**, a quarter of which is the port
logic every compute unit carries anyway — [performance](performance.md).

RV32I means what it says: **no multiplier, no divider, no floating point**, and
each of those encodings faults rather than computing something. That is *this
core's* deliberate shape — the arithmetic in this machine lives in the wide
datapaths — and what each of them would cost to add here is costed in
[microarchitecture](microarchitecture.md#the-arithmetic-the-ex-stage-does-not-have).
**The SIMT PE does have a multiplier**, one per thread; why it was cheap there
and stays expensive here is on the same page.

It is framework, not project. KohakuMPE later adds SIMD and DSP on top of
this base; [integration](integration.md#where-extensions-attach) names the
seams and nothing anticipates them further than that.

## The pages

| Page | What is in it |
|---|---|
| [architecture](architecture.md) | the contract: the ISA as implemented, the memory model, ordering, halting, and the unit's kick/completion protocol |
| [microarchitecture](microarchitecture.md) | how it is built: the pipeline, the arithmetic EX does **not** have and what a multiplier, a divider or scalar float would each cost, the hazard rules, the predictor, the two L1s, the requestor — and why each has its shape |
| [programming](programming.md) | the programmer's guide: memory map, control words, the communication idioms as code, boot, and running a program |
| [integration](integration.md) | instantiating one: parameters, the attach, the constraint to ask of it, extension seams, and the test suite |
| [performance](performance.md) | what it costs and achieves: frequency, resources, instruction and memory timing, communication, multi-core scaling |

If you are writing PE software, [programming](programming.md) is the page,
and [architecture](architecture.md) is what it stands on. If you are choosing
a configuration or floorplanning PEs, [performance](performance.md) then
[integration](integration.md).

Two PE classes are built on this base and documented as their own sets:

- [**simd**](simd/) — a uniform-control wide-SIMD datapath behind the same six
  pipeline boundaries, enabled by a parameter and absent without it. **Two
  tiers, both built and measured**: SWAR packed integer on custom-0 — packed
  int8/int16/int32 add, compare, shift, `vmul` and the `vdot` accumulators —
  and an [E8M15 float tier](simd/float.md) on custom-1, one fused multiply-add
  per element, **four float lanes**.
- [**simt**](../../projects/kohakumpe/simt/) — the SIMT class: an ordinary RV32I opcode addresses a
  per-thread register file, and lanes may disagree on both their path and their
  address. A rebuild on this core's shape rather than a parameter on it. Its
  lane array now carries **per-thread `RV32M`** (`mul`, `mulh`, `mulhsu`,
  `mulhu`) and a float tier of **eight lanes**.
  [gpu/status](../../projects/kohakumpe/simt/status.md) is the current state.

[**unit-counts**](unit-counts.md) covers BOTH classes: every feature that has a
width now has its own independently settable count, and that file is the price
list — what one unit of each costs, so a configuration can be inferred rather
than synthesised, with the inference validated against synthesised combinations.

### What each class actually ships

Naming the LUT without naming the arithmetic is what let "the machine has
float" and "the machine has no multiply" both circulate as true. The
arithmetic is the first column here for that reason.

| class | the arithmetic it ships with | LUT | FF | BRAM | DSP48 | Fmax | ask |
|---|---|---:|---:|---:|---:|---:|---|
| **controller** `rv_pe` | RV32I only — no multiply, no divide, no float | **2,477** | 4,140 | 5 | 0 | 377.9 | 3.333 ns |
| **DSP** `rv_pe`, 8 int + 4 float | SWAR packed int8/16/32, `vmul`, `vdot`; a float tier of **4 lanes** | **13,772** | 10,126 | 13 | 72 | 353.4 | **2.857 ns** |
| **GPU** `kht_pe`, 8 int + 8 float | per-thread RV32I **+ `RV32M`**; a float tier of **8 lanes** | **21,586** | 17,268 | 30.5 | 48 | 365.6 | **2.857 ns** |

`xcvu13p-fhgb2104-2L-e`, Vivado 2024.2, out-of-context synthesis only. Both
wide classes report **48/48 component tests passing** on the build these figures
come from.

**Read the ask column before subtracting two rows** — the controller figure is
at the older 3.333 ns request and the two wide rows at 2.857 ns, and a tighter
ask buys LUT it cannot spend on megahertz
([performance](performance.md#frequency)). `div`/`rem` fault on every class,
per-thread included.

### 8 int + 4 float is the DSP reference; 8 int + 8 float is the GPU reference

**Both are buildable, both are measured, and float is not optional in either —
rendering needs it.** The integer number is the same on both classes and the
float number is not, and neither of those facts is arbitrary.

**Lane count and element count are separate numbers, and only one of them is a
build parameter.** How many *elements* a 256-bit register holds follows from the
operand width the instruction names — 8 at 32 bits, 16 at 16 — and the *lanes*
are the parameter. The issue interval is `elements / lanes`: four float lanes
over a sixteen-element vector is one vector every four cycles, which is not a
narrower machine but a slower one.

```
   integer lanes  <-  the memory granule  (8 x 32 bit = 256 bit = one flit)   8, fixed
   float lanes    <-  arithmetic demand   (throughput vs LUT)                 a knob
```

The integer lanes **are the address path**: a contiguous 32-bit load by eight
threads is exactly one `MEM_RD_REQ`, one native memory entry, one flit.
Narrowing them breaks single-request coalescing permanently — every coalesced
load becomes two or more requests, for every kernel, forever. Float carries no
such constraint, which is why it is the knob and the integer side is not. That
asymmetry is the whole justification for `int 8 / float 4` over `int 4 /
float 4`, and for rejecting `int < float` outright.

The two classes then land on different float counts because a lane costs LUT and
each class had a different amount to spend. The **width** differs; nothing else
about the float does.

### There is one dtype configuration, and it is the whole design

```
   FP32 or FP16 operands in  ->  E8M15 compute  ->  FP32 or FP16 out
```

**Operand width is a per-instruction property, not a build option**, and the
internal format is **always E8M15** — an 8-bit exponent and a 15-bit mantissa.
There is no build in which the datapath computes in anything else, and there is
no per-class float format to compare. Two PE classes carrying float carry the
*same* float, so a GPU float result and a DSP float result are comparable by
construction rather than by intent.

What legitimately varies between the classes is one thing only:

| a parameter | **not** a parameter |
|---|---|
| whether a float tier exists at all | which format the datapath computes in |
| how many float lanes, and the issue interval that follows | whether an operand is FP16 or FP32 |

So: the controller PE has no float tier; the SIMD PE has one at four lanes; the
SIMT PE has one at eight. All three statements are about **presence and width**.

**Precision is not the deficit it sounds like.** E8M15 is a 1.5e-5 relative
error — **32x better than the fp16** that mobile fragment shaders run at — with
no subnormals, one rounding mode and a documented one-ulp deviation on
subtractive alignment ([dsp/float](simd/float.md)). E8 is FP32's exponent field,
which is what lets one internal format serve both operand widths without a
second datapath.

### What a mesh of these is

```
   8 SIMD PEs x 4 float lanes  +  4 SIMT PEs x 8 float lanes  =  64 FP FMA / clock
```

which is **one Mali-G610 shader core's width, exactly**. The LUT for that
population, on the figures above:

```
   8 x 13,772  +  4 x 21,586  +  2 x 2,477  =  201,474 LUT      against ~350k usable
   8 x     72  +  4 x     48                =      768 DSP48    of 3,072
```

Every PE class draws its instruction encodings from RISC-V's four custom opcode
majors, and there are no more than four. [**opcode-map**](opcode-map.md) is the
single authority on which class owns which, and on where the room actually is —
it is not inside either tier's ISA module, because a table one tier owns is not
something the other can check itself against. **`RV32M` cost none of the four**:
it went into the standard `OP` major at `funct7 = 0000001`, where RISC-V already
put it.

## Fixed protocol, parameter, or yours

| Thing | Category |
|---|---|
| the compute-unit port, the flit, the control registers | **fixed protocol** — the fabric's, not this unit's: [spec](../../spec/) |
| the address regions, control words, and window encoding | **fixed protocol** of this unit — [architecture](architecture.md) |
| push-and-doorbell ordering: program order is arrival order, per destination | **fixed protocol** of this unit — software depends on it |
| the halt model: no CSRs, no interrupts, halt word by cause | **fixed protocol** of this unit until an extension says otherwise |
| `BTB_ENTRIES`, `FWD_X`, `L1_LINES`, `REGFILE_PRIM`, window sizes, `WR_MAX` | **parameters** — the defaults are the shipped configuration: [integration](integration.md#parameters) |
| what a program computes | **yours** |

## What this PE does not own

| Concern | Whose |
|---|---|
| the flit, the link, the router, the port handshake | [noc](../noc/) |
| descriptor encoding, write slots, response tagging | [sysnode](../sysnode/) |
| where the PE lands on the die and at what clock | [physical](../physical/) |
| the meaning of addresses beyond its own windows | [ship](../ship/) and [sysnode](../sysnode/) |
