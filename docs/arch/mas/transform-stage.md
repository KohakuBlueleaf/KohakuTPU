---
title: The transform stage
summary: The first addon slot — where a format conversion sits, how it is selected, what is fixed about it, and what the reference project plugs in.
tags:
  - architecture
  - mas
  - addon
---

# The transform stage

**The slot is fixed protocol; what plugs into it is an addon.** The framework
fixes where the stage sits, how it is selected and how it is driven. What it
*does* is a property of the accelerator you are building, and the reference
project's transform is one example of a thing that goes there — not a part of
the framework that happens to be configurable.

The memory agent is flexible on purpose. This stage and the staging described in
[edge-and-control](edge-and-control.md#staging-inside-the-memory-agent) are its
two named addon slots.

## Where the stage sits

There are two of them, one on each memory path:

```
   host --> upload window --> [ transform ] --> AXI write --> memory
   memory --> AXI read --> [ transform ] --> emit buffer --> flits --> unit
```

One instance per memory port on the fetch path, and one for the upload path. The
upload one is separate rather than shared under a mutex, because a mutex is the
only reason an upload would ever have to wait for a fetch.

## What the framework fixes about it

| Fixed | Why |
|---|---|
| its position — between AXI read data and the emit buffer, and between host write data and the AXI master | so it is one instance per port rather than one per compute unit |
| **per-transfer selection** — a flag bit in the read request, an address bit in the host upload | AXI carries no field for it and host DMA engines expose none, so the marker rides where a host can always put it. Nothing here remembers which region is in which format |
| its handshake — start, a stream of accepted beats, done, a fixed number of output words | so the read engine's control does not change when the transform does |
| that it may change the byte count | source and destination burst lengths are unrelated, and neither side has to know the other's entry size |
| that it is **entry-granular** | a transform with a cross-element dependency cannot emit until the whole entry has arrived. The engine is written for that case, so a streaming transform is also fine |

## What you supply

The transform itself. The framework does not know or care whether it converts a
numeric format, permutes lanes, decompresses, or does nothing.

Two architectural claims justify the stage existing at all, and both are
independent of what the transform is:

- **Converting on the way in divides the cost by the number of later reads.** A
  tensor uploaded once and read many times pays the transform once per upload
  rather than once per pass.
- **Converting at the port means one instance per port**, not one per compute
  unit — and the port count is small and set by memory service, while the unit
  count is large and set by the die.

## The reference instance is an addon, not a fixture

KohakuTPU plugs a numeric-format quantiser into this slot: FP16 held in memory
becomes a block-scaled 7-bit format on the way to the compute unit — chosen
because it is materially denser on the fabric, which is the resource that
machine is short of, and because software then never has to construct an
internal format.

That is **one** transform, supplied by one project. A project with different
arithmetic writes a different one and changes nothing else in this system. A
project that wants no transform leaves the slot empty and the flag unset.

The format, and why its scale encoding is what it is, is
[projects/kohakutpu/number-format](../../projects/kohakutpu/number-format.md).

## Convention

**Put format conversion in the transform slot, not in your unit.** *(Free.)*
One instance per memory port rather than one per compute unit, and converting on
upload divides the cost by the number of later reads. A unit that converts
internally works; it just pays for it once per unit and once per pass.

## Where today's source disagrees

**The transform addon is welded into the slot.** `mx_quant.v` — the reference
project's block-scaled quantiser — is an **addon**, and it lives in
`src/kohakumas/` and is instantiated by name in both `mag.v` and
`mag_mem_port.v`. The slot described above is real: its position is real, its
per-transfer selection is real, and its handshake is real. What is missing is
the indirection between the slot and the thing in it.

Today a second accelerator would edit two framework modules to change its number
format. The fix is a named module boundary the project supplies, with the flag
bits that select it reserved here rather than given a meaning here — and
`mx_quant.v` itself moving to the project alongside the rest of KohakuTPU.
