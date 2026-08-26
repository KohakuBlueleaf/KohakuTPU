---
title: The instruction set you inherit
summary: Who owns which bits of an instruction flit, what a read, a write and a mover command can express, and what that constrains in your compiler.
tags:
  - architecture
  - mas
  - isa
---

# The instruction set you inherit

This is the part of the framework that most changes what a compute-unit author
has to do, and it is easy to miss because it is spread over two systems.

## The instruction space is shared three ways

An instruction reaching a compute unit is one **flit** — one fixed-width word of
on-chip network traffic, a routing header plus a payload, 288 bits in the
reference build. That flit's bits belong to three different owners:

| Field | Owner | Fixed? |
|---|---|---|
| routing header — destination, source, type, transaction id, last | [noc](../noc/) | fixed protocol |
| memory descriptors, entry geometry, transform selection, mover commands | **this system** | fixed protocol; the flag bits selecting a transform are reserved for the addon |
| the instruction payload a compute unit executes | **you** | yours entirely |

So the machine already has an instruction set before your compute unit exists.
It knows how to say *fetch this region, in these entries, converting it this
way, and deliver it to these three nodes*. It knows how to say *write this back*
and how to acknowledge it. It knows how to say *rearrange this region of memory
into that one*.

**A compute unit adds compute semantics to an instruction set that already
handles memory.** You are not designing a way to move data; you are designing
what happens to data once it arrives. That is a large fraction of what building
on a framework buys, and it is worth saying explicitly because the alternative —
each unit inventing its own memory request format — is what a
non-frameworkised design looks like.

Two practical consequences:

- **Instruction bits are a shared budget.** The header takes its fixed slice
  before you see the flit. The normative allocation is
  [spec/flit-format](../../spec/flit-format.md) and
  [spec/memory-protocol](../../spec/memory-protocol.md); how to spend what is
  left is [integrate/instruction-set](../../integrate/instruction-set.md).
- **Your compiler emits memory instructions it did not define.** The back end
  you write is responsible for *scheduling* fetches and writes, not for
  inventing their encoding.

This says nothing about what your unit does with the data once it arrives — how
many memories it has, how wide they are, what their read latency is, how they are
banked. That is your design, and this system has no opinion on it.

## What the memory instruction set covers

Not a field list — that is
[spec/memory-protocol](../../spec/memory-protocol.md) — but the shape of what is
expressible, because that shape is what constrains your compiler:

- **A read** names a byte address and an entry geometry, and optionally a run of
  consecutive entries, a transform, and up to a few extra destinations.
- **A write** is a descriptor followed by data flits, acknowledged
  fire-and-forget.
- **A mover command** names a source and a destination as N-dimensional strided
  descriptors with bound axes, plus a mode: copy, transpose, gather, generate,
  fill. Because the descriptors have bound axes, an element outside the tensor
  is *padding* rather than a special case — the source's low `valid` injects a
  constant and the destination's low `valid` suppresses a write, so a padded
  traversal needs no border handling anywhere else in the machine.

The descriptor walker underneath the mover is a general affine address generator
with no multipliers: each dimension carries its own partial sum, incremented on
step and zeroed on wrap, so the address is an adder tree rather than a product.
That is what makes a strided N-dimensional walk cost one element per cycle.

## Who issues a mover command

A read or a write is issued by a compute unit, as a flit. **A mover command is
not** — it is a store into an address range the node's control processor
decodes, and no compute unit can reach it. Nothing outside the node addresses
the mover at all; the host's own path to it is a window on the node's control
slave, arbitrated against the processor's stores.

That is why the mover's command set is described with the processor rather than
with the flit protocol — [simd-model](simd-model.md#one-front-door) — and why
the six modes are the *processor's* instruction set rather than a compute unit's.

> **Two of the mover's nine registers are not reachable from the RV64
> processor.** Its control region maps the mover's config offsets
> `0x00`–`0x3F`, and the fill immediate (`0x40`) and the gather pitch and word
> count (`0x50`) fall outside that window. A `FILL` or `GATHER` move therefore
> cannot be fully programmed from a program running on it; both remain reachable
> from the host's config window, and from the default RV32 processor, whose
> descriptor form replays an arbitrary `{offset, value}` list. See
> [control-processor](control-processor.md#where-todays-source-disagrees).

## Where today's source disagrees

**`mx_tdesc.v` carries a project prefix it has outgrown.** The descriptor walker
is a general N-dimensional affine address generator with bound axes and nothing
project-specific in it, and it lives with the mover that uses it at
`src/kohakuaccel/sysnode/mover/mx_tdesc.v`. Only the `mx_` in its name still
says otherwise.
