---
title: Configurable compute widths
summary: The specification for SIMD and SIMT compute widths — every feature is a unit count from 0 to 8, a narrower count costs cycles rather than instructions, and 0 means the feature is not built and its encodings fault.
tags:
  - architecture
  - pe
  - specification
---

# Configurable compute widths

> **Kind: Yours — these are this project's parameters on this project's units.**
> Every width is a value a builder chooses, and its range and meaning are set by
> this project's own specification rather than by the framework. The framework's
> parameter rule does not reach these: nothing in
> [spec/parameters](../../spec/parameters.md) constrains any of them.

This page is the **specification** for how both KohakuMPE processing elements
are configured. It says what a width is, which features have one, what a width
of zero means, and which configurations are refused. It carries no measurements;
[unit-counts](unit-counts.md) prices what is specified here.

Three terms, before anything else. A **lane** or **unit** is one copy of the
arithmetic. An **element** is one value inside a vector register, and how many
there are follows from the register width divided by the element width — nobody
chooses it. A **pass** is one cycle in which the built units serve one slice of
the elements.

## 1. Principle

**The vector register is `32 × SIMD` bits and holds one binary32 or int32
element per 32-bit slot.** At the reference `SIMD` of 8 that is 256 bits and
eight elements, which is also one flit payload, one memory-agent entry and one
L1 line — the alignment the integer side exists to preserve.

A feature's width sets how many units are built, and therefore how many cycles
an operation takes. It never changes what the instruction set contains.

    units W over SIMD elements  ->  SIMD/W passes, one issued per cycle

Legal values are **0, 1, 2, 4 and 8**, plus **−1** meaning full rate — one unit
per element, whatever `SIMD` is. Writing `-1` rather than the number is what
keeps a caller correct when `SIMD` changes.

Three properties are required of every width:

**A width costs cycles, never encodings.** Narrowing a feature makes its
instructions slower. It must never make them illegal, and it must never change
the result. The same binary and the same golden memory image run at every width.

**A width at zero means the feature is not built.** No units are instantiated,
and every encoding that would need them faults at decode. Faulting is part of the
contract: a feature that is decoded but has no datapath returns a plausible wrong
answer, which is worse than refusing.

**A width at full costs nothing.** At `W == SIMD` the hardware is the plain
un-walked array. The sequencing logic exists only in the narrow branch, so a
build that does not use a width does not pay for it.

**Counts, not booleans.** A feature that can only be present or absent is still
spelled as a count with the values 0 and 1. One vocabulary, one way to say none.

## 2. The integer ALU is one unit, and it multiplies

RV32IM is the instruction set, not an option. The integer ALU is therefore an
**IM unit**: add, subtract, compare, bitwise and multiply are one unit with one
operand path and one result path, not an ALU beside a multiplier array with a
dispatch mux between them.

This is an FPGA decision as much as an architectural one. **Multiplexers are the
expensive primitive here, not logic.** Two units serving one issue port need an
operand mux in front and a result mux behind, and both are wider than the logic
they arbitrate. One fused unit has neither.

The consequence is that **the multiplier count is not a knob on either core**:

- on SIMD it follows `ILANES`, because a lane is an IM unit
- on SIMT it is `LANES`, because a thread is an IM unit

The packed multiply covers every element type the instruction set defines. It
occupies DSP columns rather than fabric, and these parts are LUT-bound, so there
is nothing to gain by narrowing it and a mux to lose by making it separable.

## 3. SIMD widths — `khs_unit`

`rv_pe` prefixes each of these with `SIMD_` and passes it down, so a build sets
`SIMD_ILANES`, `SIMD_FLOAT_LANES` and so on. `SIMD_EN = 0` elaborates none of
the unit and leaves the base controller PE bit for bit.

| knob | values | builds | faults at 0 |
|---|---|---|---|
| `ILANES` | 0,1,2,4,8,−1 | integer IM lanes: packed ALU and multiply | `vadd` `vsub` `vmin` `vmax` `vand` `vor` `vxor` `vandn` `vmul` |
| `SHIFT_UNITS` | 0,1,2,4,8,−1 | the packed shifter | `vslli` `vsrli` `vsrai` `vsrari` |
| `PERM_UNITS` | 0,1,2,4,8,−1 | the cross-lane permute: slide, pack, unpack | `vsldw`, pack, unpack |
| `RED_UNITS` | 0,1 | the horizontal sum and signed-max trees | `vredsum` `vredmax` |
| `FLOAT_LANES` | 0,1,2,4,8,−1 | binary32 fused multiply-add units | every float opcode |
| `FSFU_UNITS` | 0,1,2,4,8,−1 | seed units: `exp2`, `log2`, `rcp`, `rsqrt` | the seed opcodes |
| `FCVT_UNITS` | 0,1,2,4,8,−1 | binary32 ↔ int32 converters per pass | the FCVT group |
| `HAS_SHROUND` | 0,1 | `vsrari`'s round adder, one packed add per lane | `vsrari` rounds toward zero rather than faulting |

`HAS_SHROUND` is the one entry that does not fault at 0, because it does not
remove an instruction — it removes a rounding step from one. It is spelled as a
gate rather than a width because it is one adder per lane inside the lane, with
no separable array to walk.

Two float **groups** sit beside the counts and are gates rather than widths,
because each names a set of opcodes rather than an array that can be narrowed:
`HAS_FALU` — multiply, add, subtract, fused multiply-add, min, max and compare,
which is what a CPU SIMD ISA ships as its base — and `HAS_FACC`, the rotating
accumulator and its fold, which is the tier's extra and is off by default.

### There is no dot-product unit

A dot product is expressed with the units above: a packed multiply followed by a
reduction — `vmul` then `vredsum` — or a multiply whose partial sums the scalar
core accumulates.

A dedicated dot unit would add a second sum path (an adder tree or a DSP cascade
with its own latency pipeline) and a set of accumulator registers, and its
accumulate recurrence constrains every other width around it. Parts that need
high-rate dot products carry dedicated matrix units, which is where that work
belongs. The vector core covers the intermediate case.

### Formats

The compute format is **IEEE binary32** throughout and there is no knob for it.
A 32-bit word holds exactly one element, so the float tier's element count *is*
`SIMD` and nothing converts at either edge. Denormals flush to sign-preserved
zero on input and output.

`FLOAT_LANES` counts units against that element capacity, so a build with fewer
units than elements walks the difference in passes. A seed walks
`SIMD / FSFU_UNITS` passes where a fused multiply-add walks
`SIMD / FLOAT_LANES`.

**A nonzero seed count deepens the whole tier.** The multiply-add path is 6
cycles and the seed path is 10, so a tier that can issue a seed pads the
multiply-add to match and has one latency and one retire shadow rather than two.
The tier's latency is therefore 6 with `FSFU_UNITS = 0` and 10 otherwise, on
both cores, and each module checks the depth it was told against the depth it
built.

## 4. SIMT widths — `kht_pe` / `kht_core` / `kht_unit`

| knob | values | builds | faults at 0 |
|---|---|---|---|
| `WAVES` | 1,2,4,8,16 | wave slots and the scheduler's storage | — |
| `FLANES` | 0,1,2,4,8,−1 | binary32 fused multiply-add units | every float opcode |
| `FSFU_UNITS` | 0,1,2,4,8,−1 | seed units | the seed opcodes |
| `SHFL_UNITS` | 0,1,2,4,8,−1 | the subgroup shuffle's output lanes per pass | `shflxor`, `bcast` |
| `LDS_BANKS` | 0,1,2,4,8,−1 | banks in the shared local memory | — the LDS falls back to its serial path |
| `HAS_MASK`, `HAS_IPDOM` | 0,1 | the active mask, and the divergence stack | `tmc`; `split` and `join` |
| `IPDOM_D` | a depth | entries in the divergence stack | — |

`LDS_BANKS = 0` is the second entry that does not fault: the shared memory still
answers, one lane at a time, down the sequencer that a banked build also uses
when every lane hits one bank. Fewer banks is more conflicts and more passes,
which is what a width means, and forward progress is unchanged because the
resolver still serves the lowest outstanding lane.

### The thread ALU width is fixed

A SIMT processor is its threads: `LANES` threads means `LANES` integer units.
This is the one width on either core set by definition rather than by
measurement, and it is deliberately not configurable. The multiply count follows
it, per §2.

The mask, the divergence stack, the subgroup shuffle and the banked shared
memory are what make the core SIMT rather than SIMD. They have their own gates
and widths, and they are not part of the arithmetic tiers described here.

## 5. One width, one structure

Every width below full is built from the same five pieces.

1. **A pass counter**, running 0 to `SIMD/W − 1`.
2. **A hold** contributed to the stage's fold condition, derived from registered
   state only. It must not read the combined fold signal, which contains it.
3. **An operand mux.** Unit `u` on pass `p` serves element `p·W + u`. The cost is
   one mux per unit BUILT, which is what makes a narrow width pay: at one unit
   there is one mux against seven units removed.
4. **A staging register** holding passes 0 to `PASSES−2`. The final pass stays
   live, so the instruction retires on its own cycle and the register holds
   `SIMD − W` elements rather than `SIMD`.
5. **A reset on advance.** Back-to-back operations keep the group's issue signal
   asserted across the boundary; without an explicit reset the second operation
   begins at the count the first ended on.

At `W == SIMD` these five do not exist: the full-width array is its own branch,
selected at elaboration.

### Holds and the stage below

A hold that stops one pipeline stage must be applied to the stage below it in the
right form. A stall that stops two adjacent stages together may freeze the
register between them. **A hold that stops only the upper stage must send the
lower stage a bubble**, because the lower stage continues to advance and would
otherwise retire the instruction it holds once per stalled cycle.

### Results that arrive late

Where a unit's result is combinational, the staging register captures it on the
pass that issued it. Where a result is registered — the packed multiply is the
case in this design — the staging write must use a pass index delayed to match,
because the product belongs to the pass that presented its operands and not to
the pass in flight when it lands.

## 6. Elaboration rules

A configuration that cannot work is refused at elaboration, by instantiating a
module that does not exist so that the error names the rule that broke:

    ERROR: [VRFC 10-2063] Module <khs_unit_requires_PERM_UNITS_to_divide_SIMD>
    not found while processing module instance <g_bad_pu.u_bad>

That shape is deliberate. A width that does not divide the element count
otherwise elaborates cleanly, synthesises, reports a plausible frequency, and
fails only in a component bench — because the pass count truncates and the walk
covers some of the elements rather than all of them. A refusal at elaboration is
the only place the mistake is cheap.

The rules, on both cores:

    every width W                W == 0, W == -1, or W divides the element count
    W must not exceed the element count
    W < -1                       is not a width at all
    FSFU_UNITS <= FLOAT_LANES    a seed unit is a float unit
    a float GROUP needs units    HAS_FALU or HAS_FACC with no lanes is refused
    HAS_SHROUND requires SHIFT_UNITS > 0
    the tier's declared latency must equal the depth its array builds

## 7. Choosing a configuration

The widths are independent, so a part is configured by workload rather than by a
single profile. Two properties guide the choice.

**Feature count dominates unit count.** Each processing element pays a fixed base
cost once. Meeting a throughput target with fewer, wider elements is cheaper than
with more, narrower ones, because the base is paid fewer times.

**A feature at zero is still available.** A width of 0 removes hardware from one
build; the knob remains, so a part aimed at a different workload turns it on. No
configuration is a deletion.

### Graphics and general float workloads

Float throughput is the machine. Address arithmetic runs on the scalar RV32IM
core rather than the vector lanes, so vector integer work reduces to bit packing
and small index arithmetic — a narrow integer tier relative to the float tier.
Transcendentals are provisioned at 1:4 to 1:8 of the float units, matching
contemporary GPU practice.

Two measured properties should be read before setting either of those counts,
because both are counter-intuitive and both are in [unit-counts](unit-counts.md).
A **seed count in the middle of its range is the worst place to sit** — the
placement mux and the pass decode move in opposite directions with the unit
count, so full rate and one unit are both cheaper than two or four. And a
**cross-lane width pays only at one or two units**: on the SIMT shuffle, four
units of eight cost LUT rather than saving any.

The cross-lane permute serves shader swizzles and transposes and is retained; its
unit count is set from measurement rather than assumed.

The packed shifter's characteristic instruction is the requantizing rounding
shift, which belongs to integer inference rather than to shading, and the
reduction trees have no counterpart on the SIMT core. Both are candidates for
zero on a float-oriented part.

### Quantized inference workloads

The reverse: the shifter and the reduction trees carry the requantize and
accumulate steps, the integer tier widens, and the float tier and its seed units
narrow.
