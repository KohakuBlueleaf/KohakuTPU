---
title: The transform stage
summary: The first addon slot — where a format conversion sits, how it is selected, what is fixed about it, and what the reference project plugs in.
tags:
  - architecture
  - sysnode
  - addon
---

# The transform stage

A **transform slot** is a socket in the datapath where a format conversion may
be plugged in: bytes go in on one side, converted bytes come out on the other,
and the machine around it does not change when the conversion does. The module
that fills the socket is its **occupant**.

**The slot is fixed protocol; what plugs into it is an addon.** The framework
fixes where the stage sits, how it is selected and how it is driven. What it
*does* is a property of the accelerator you are building, and the reference
project's transform is one example of a thing that goes there — not a part of
the framework that happens to be configurable.

The memory agent is flexible on purpose. This stage and the staging described in
[edge-and-control](edge-and-control.md#staging-inside-the-memory-agent) are its
two named addon slots.

## Where the stage sits

**One bank, ON the memory mover's read-return path, driven only by the mover:**

```
   mem / L2 --> [ transform ] --> mem / L2      pre-convert on card, once
   mem / L2 -------------------> port --> NoC --> unit
```

A compute unit's fetch is never transformed. It reads operands already in their
final format — written that way by the host, or converted in place by the mover.

Three earlier arrangements are retired: one instance per memory port, a separate
instance on the host upload window, and a separate engine (`mm_xfer.v`) muxed
onto the mover's AXI channel. All are gone, and
[spec/transform-slot](../../spec/transform-slot.md) carries the argument. The
short version is that a per-port transform is fed from that port's AXI R
channel, every port master converges onto one DRAM master, and a staged read
never transforms — so N instances could consume one beat per cycle between them,
N−1 idle by construction.

**One instance is 4,499 LUT and 32 DSP** — measured out-of-context on
`xcvu13p-fhgb2104-2L-e`, Vivado 2024.2, at 3.333 ns, `sysnode` whole at
`PORTS=2`, by `scripts/tcl/ooc_sysnode_rv64.tcl`. That figure is the framework's
arbiter plus the reference project's bank and its occupant, so it is what *this*
project's transform costs rather than what a slot costs. Moving the slot onto
the mover changed who drives it, not what it is;
[simd-model](simd-model.md#where-the-slot-sits) has the comparison.

## What the framework fixes about it

| Fixed | Why |
|---|---|
| its position — on the mover's **read-return path**, between R and the mover's staging FIFO | so it is one instance per agent rather than one per port or one per unit, and the FIFO holds converted words rather than source ones |
| **the mover selects it, per move**, by an id in the descriptor | a transform on a fetch is paid once per read; on a move it is paid once per tensor |
| its handshake — start, a stream of accepted beats, done, a fixed number of output words | so the mover's control does not change when the transform does |
| that it may change the byte count, and **declares the ratio** as `IN_BITS`/`OUT_WORDS`, with `OUT_WORDS` at most 4 | the mover has to size a converting move before the transform has run, and the bank presents four word outputs |
| that it is **entry-granular** | a transform with a cross-element dependency cannot emit until the whole entry has arrived. The engine is written for that case, so a streaming transform is also fine |
| **id 0 is bypass** | selection is an id, not a bit per transform, so a design with several transforms picks one rather than encoding a mask |

## What you supply

The transform itself. The framework does not know or care whether it converts a
numeric format, permutes lanes, decompresses, or does nothing.

Two architectural claims justify the stage existing at all, and both are
independent of what the transform is:

- **Converting before the fetch divides the cost by the number of later reads.**
  A tensor converted once and read many times pays the transform once, not once
  per pass. This is why the slot moved off the fetch path.
- **Converting in the agent means one instance per agent**, not one per compute
  unit and not one per port — and a compute unit that converts internally pays
  for it once per unit *and* once per pass.

## The reference instance is an addon, not a fixture

KohakuTPU plugs a numeric-format quantiser into this slot: FP16 held in memory
becomes a block-scaled 7-bit format on the way to the compute unit — chosen
because it is materially denser on the fabric, which is the resource that
machine is short of, and because software then never has to construct an
internal format.

That is **one** transform, supplied by one project. A project with different
arithmetic writes a different one and changes nothing else in this system. A
project that wants no transform compiles
`src/templates/transform/xform_bank.v`, where every id is bypass — there is no
flag to leave unset, because selection is an id.

That template is what keeps the framework free of any project: `mag_xform` names
`xform_bank`, so a framework-only build would not elaborate if the only bank in
the tree were a project's. `tests/sysnode/xform_identity_tb.v` builds that case
and nothing else.

The format, and why its scale encoding is what it is, is
[projects/kohakutpu/number-format](../../projects/kohakutpu/number-format.md).

## How the mover reaches it

**On the mover's own read-return path**, as mover mode 5 — one walker in the
system, and a transform is pre- or post-processing on a move. The reservation is
what it always was, a static count of destination words known before the AR goes
out; it is just `OUT_WORDS` per entry instead of one per read element.

**An occupant has registers**, reached from the control processor's control
range by ordinary load and store and indexed by occupant id — for configuration
a `mode` field is too narrow to carry, like a palette or a coefficient table,
and for the bank's own status.

> **The register path is connected on the default RV32 complex and tied off on
> the RV64 one**, so in the RV64 configuration the register space exists in the
> RTL and nothing can reach it —
> [simd-model](simd-model.md#occupant-registers). The reference occupant needs
> no registers, which is why the gap has not blocked anything; an occupant that
> needs a palette would be blocked by it.

> Both of these replaced an earlier shape, and the reason it was chosen is worth
> keeping: the slot used to be reached by a **separate engine** with no walker,
> split out because the mover's flow control is one word in per word out and a
> 2:1 transform breaks that. The cost was a gather pass into staging for any
> strided source, and an FSM serial enough that no entry's write overlapped the
> next entry's read.

[simd-model](simd-model.md) states both in full.

## Where the boundary now falls

The framework names exactly one module: `xform_bank`. It holds the project's
occupants and demuxes the id internally, so no framework module names a
transform. KohakuTPU's bank is `src/kohakutpu/transform/xform_bank.v`; a project
with none uses the identity bank, where every id is bypass.

`mag_xform.v` is the framework side — arbitration, the beat mux, the registered
stage and the geometry parameters. It instantiates `xform_bank` and nothing
else.

## Convention

**Put format conversion in the transform slot, not in your unit.** *(Free.)*
One instance per memory agent rather than one per compute unit, and converting
before the fetch divides the cost by the number of later reads. A unit that
converts internally works; it just pays for it once per unit and once per pass.
