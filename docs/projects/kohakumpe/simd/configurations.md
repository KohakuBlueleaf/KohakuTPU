---
title: Configurations
summary: Which axes of this PE are free and which are not, the one issue-rate limit the float tier still has, how to choose a feature mix, and why the SIMD PEs of one mesh need not be the same build.
tags:
  - architecture
  - pe
  - simd
  - configuration
---

# Configurations

> **Kind: Yours — every axis here is this project's own parameter.** Which
> features a build carries, and in what counts, is chosen per instance, and the
> framework neither knows nor constrains the set. The one thing not free is the
> port each configuration presents
> ([spec/compute-unit-port](../../../spec/compute-unit-port.md)), which is
> identical whatever the widths.

Every compute feature of this PE is an independent unit count, so a
configuration is a set of numbers rather than a choice from a menu.
[configurable-widths](../configurable-widths.md) is the mechanism and the
elaboration rules; [unit-counts](../unit-counts.md) is what each unit costs.
This page is what to *do* with those two.

## The axes are not alike

```
   register width   <-  the memory granule  (8 x 32 bit = 256 bit = one flit)
                        SIMD, effectively fixed at 8
   integer lanes    <-  how many lanes serve those slots per pass
                        ILANES, a width: cycles only
   float units      <-  arithmetic demand, throughput against LUT
                        FLOAT_LANES, a width
   float groups     <-  which arithmetic exists at all
                        HAS_FALU, HAS_FACC, FSFU_UNITS, FCVT_UNITS
```

**The register width is the address path.** A contiguous 32-bit load by eight
slots is exactly one memory read request, one native memory entry, one flit
payload. Narrowing `SIMD` breaks single-request coalescing permanently, for
every kernel. Narrowing `ILANES` does not: the register stays 256 bits and an
integer operation takes `SIMD / ILANES` passes instead of one.

That distinction is the whole reason `SIMD` is not offered as a tuning knob
while `ILANES` is.

## The one rate limit the float tier still has

**One elementwise float instruction is in flight at a time**, so the issue
interval is the tier's latency plus its pass count rather than the pass count
alone:

```
   II  =  ALAT + passes        7 cycles at full width with no seed units,
                               11 with them
```

The cause is the writeback, not the arithmetic. Each pass places its results
into a **single staging register** and the whole register is written to the
vector file when the last pass lands. That makes the write port a mux rather
than an arbitration and needs no per-element write enable — but the staging
register is shared, so two instructions in flight overwrite each other. The
scoreboard does not catch it, because it only blocks *dependent* instructions.

Serialising is the correct fix and the cheap one, and it is what ships. The fast
fix is to make the writeback **per pass** with a per-element write enable on the
register file, which removes the staging register entirely and takes the
interval back to the pass count. That is the first performance work this group
needs, and it is larger than a parameter.

**The SIMT PE does not have this limit**, and the difference is exactly the
missing write enable: its register file has one, so a pass writes straight into
it with a constant source and there is no staging register to share. The same
asymmetry is why a fractional rate costs more on SIMD than on SIMT
([unit-counts](../unit-counts.md)).

The accumulator group is separate and does **not** have this limit: a
`vfmacc` issues back to back, including into the same accumulator, because the
rotation breaks the recurrence ([float](float.md#what-waits-and-what-does-not)).

## Choosing a feature mix

Two properties guide the choice, and both are measured rather than assumed.

**Feature count dominates unit count.** Each processing element pays a fixed
base cost once — the core, the windows, the L1, the requestor, the fabric port.
Meeting a throughput target with fewer, wider elements is cheaper than with
more, narrower ones, because the base is paid fewer times.

**A feature at zero is still available.** A width of 0 removes hardware from one
build; the knob remains, so a part aimed at a different workload turns it on. No
configuration is a deletion.

### Graphics and general float work

Float throughput is the machine. Address arithmetic runs on the scalar RV32IM
core rather than the vector lanes, so vector integer work reduces to bit packing
and small index arithmetic — a narrow integer tier relative to the float tier.
Transcendentals are provisioned at 1:4 to 1:8 of the float units in contemporary
GPU practice, but on this fabric the seed count is **not monotonic in LUT**:
full rate and one unit are both cheaper than the middle, so read
[unit-counts](../unit-counts.md#4a-a-fractional-rate-is-worst-in-the-middle)
before picking a ratio.

The cross-lane permute serves shader swizzles and transposes and is retained;
its unit count is set from measurement, and narrowing it to one or two units is
the largest cycles-only saving on this PE.

The packed shifter's characteristic instruction is the requantising rounding
shift, which belongs to integer inference rather than to shading, and the
reduction trees have no counterpart on the SIMT core. Both are candidates for
zero on a float-oriented part — but note that **narrowing the shifter to two
units saves more than deleting it**, and keeps every shift instruction.

### Quantised inference

The reverse: the shifter and the reduction trees carry the requantise and
accumulate steps, the integer tier stays wide, and the float tier and its seed
units narrow or go to zero. `HAS_SHROUND` matters here and nowhere else — it is
the rounding step of the requantise primitive, and without it `vsrari` truncates.

## Why the SIMD PEs of a mesh should differ

Every feature is a parameter, so the PEs in one mesh need not be the same build.
That turns the feature mix into an axis of the balance study rather than one
global choice, and it is cheaper than it sounds: giving the seed units only to
the PEs that will run transcendental-heavy work, and the extra float units to
the ones that will not, buys more total float width per LUT than widening every
PE equally.

A mesh does not have to answer "how much float does a SIMD PE need" once. It can
carry several answers and let the dispatcher route to them.

## Verification state

| | |
|---|---|
| the encoding, all four consumers agreeing | **pass** — 77 instructions, 56 integer and 21 float |
| the multiply-add against the model, on the bits | **pass** |
| the four seeds against the same integer table the RTL reads | **pass**, with the specials pinned exactly and the finite paths to a measured tolerance |
| the accumulator partials, in the rotation's own order | **pass** |
| the fold, in index order | **pass** |
| `khs_unit`, integer stream | **pass** |
| `khs_unit`, elementwise float | **pass** |
| `khs_unit`, float with the accumulator | one element, 1 ulp, on one multiply case — open |
| the assembled PE and its kernels | **pass** |
| elaboration, every configuration measured | **pass** |
| **a float workload** | **none exists** — see [gates](gates.md#what-the-list-does-not-cover) |

The bench and the generator take the same feature switches, so a configuration
is verified **as itself** rather than against a default.

## What is not measured

- **Frequency, for any feature mix.** Every frequency figure this project has is
  out-of-context synthesis of one configuration, and they move by tens of
  megahertz between rows that differ in nothing that should matter. No claim
  about a configuration's clock is made here.
- **Any configuration of the current float tier.** The published price list was
  measured before the tier was rebuilt in binary32, and before the converter
  group gained its datapath. [unit-counts](../unit-counts.md) says exactly what
  each of its tables does and does not cover.
- **`FCVT_UNITS` at any value.** The group is built now and has never been
  priced.
- **The per-pass writeback**, which is the row that would decide whether the
  elementwise issue interval above can come down to the pass count.
