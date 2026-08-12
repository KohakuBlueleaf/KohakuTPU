---
title: Addon slots
summary: What a slot is, what it costs to define one, and a worked extraction from real source — the MAG transform stage.
tags:
  - integrate
  - addon
  - frameworkization
---

# Addon slots

An **addon** is a part the framework ships working and expects you to replace.
[what-you-own](what-you-own.md) says which parts those are. This page says what
a slot actually *is*, using the one the reference project fills.

A slot is not a module you swap. It is four things:

1. a **port contract** — signals, directions, handshake;
2. a **geometry contract** — what the occupant tells the host module about its
   own shape, because the host has arithmetic that depends on it;
3. a **selection mechanism** — how a request says *use this transform*, without
   the framework knowing what the transform is;
4. a **default occupant** — something correct that does nothing, so a project
   with no use for the slot pays nothing and still elaborates.

Miss any of the four and you have a hook, not a slot. The extraction below is
worth reading because the current source has the *right idea* — a transform
stage on the memory read path is exactly where such a stage belongs — and still
misses all four.

## The example: the MAG transform stage

The memory agent converts data on the way out of DRAM, once per byte fetched
rather than once per consumer. That stage is framework. The transform in it is
KohakuTPU's: FP16 to MXFP7 block quantisation.

Today the framework names that transform directly, in two places:

    src/kohakumas/mag_mem_port.v:307     mx_quant u_quant (
    src/kohakumas/mag.v:673              mx_quant u_hquant (

Renaming that module breaks the memory agent. That is the surface symptom. The
coupling underneath it has five distinct kinds, and only the first is fixed by
moving a file.

### 1. The module is named

Both sites instantiate `mx_quant` by name with a fixed port list. A second
accelerator with a different numeric format edits two framework modules.

### 2. The framework hardcodes the transform's geometry

`mag_mem_port.v:325-330`:

    localparam integer Q_ENTRY_BITS  = 2048;
    localparam integer P_ENTRY_BITS  = 1024;
    localparam [33:0]  Q_ENTRY_BYTES = Q_ENTRY_BITS / 8;          // 256
    localparam [33:0]  P_ENTRY_BYTES = P_ENTRY_BITS / 8;          // 128
    localparam [7:0]   Q_ARLEN       = Q_ENTRY_BITS / DATA_W - 1; // 7 at 256b
    localparam [7:0]   P_ARLEN       = P_ENTRY_BITS / DATA_W - 1; // 3 at 256b

`mag.v:692-694` repeats the second half. These are not incidental: `Q_ARLEN` is
the AXI burst length the framework issues, and `Q_ENTRY_BYTES` is the stride it
steps by between entries. **The memory agent's address arithmetic encodes the
transform's 2:1 compression ratio.**

This is the part a "just move the file" refactor gets wrong. A transform is not
only a function on data — it changes how much data a fetch must read, which the
host module needs before the transform has run.

### 3. The selection bits are named after one transform

`mag_mem_port.v:195-196` decodes the request flags:

    wire in_quant  = in_flags[4];
    wire in_blay   = in_flags[5];

and `mag.v:700-701` carries the same choice into the host upload window, where
AXI has no field for it:

    localparam integer HW_QUANT = ADDR_W - 1;
    localparam integer HW_BLAY  = ADDR_W - 2;

`QUANT` and `BLAYOUT` are MXFP7 concepts occupying the framework's flag
namespace. The *mechanism* — reserved request bits that select a transform mode
— is correct and general. The *names and meanings* are one project's.

### 4. A transform-specific mode reached the framework's port

`b_layout` selects A-operand packing or B-operand packing. That is a matmul
concept. It appears in MAG's request decode, in both instantiations, and in the
host window's address map. The framework has no use for it and cannot act on
it; it only carries it.

### 5. The framework's correctness depends on the occupant's internals

This is the one that makes the current arrangement not a slot at all.
`mag_mem_port.v:311-315`:

    // Tied to the ACTUAL handshake, not just to r_valid. With the next
    // entry's read issued early its data can be waiting on the R channel
    // while `start` is still asserting, and mx_quant's start branch takes
    // priority over its store -- so a beat accepted on that cycle would be
    // consumed and dropped.

and `mag.v:709-711` works around the same behaviour a second time, in the
`sm_wready` expression. **Two framework modules contain logic whose correctness
depends on an undocumented internal priority of the plug-in.** Any replacement
transform must reproduce that priority exactly, and nothing tells its author so.

A slot with this property is worse than no slot: it advertises replaceability
while requiring the replacement to be bug-compatible.

## What the slot has to be

Everything above is fixable, and the fix is a contract rather than a rewrite.
The transform stays where it is and does what it does; what changes is what the
framework is allowed to know about it.

**Ports.** The signal set is already right — it is what `mx_quant` exposes,
generalised:

| signal | dir | meaning |
|---|---|---|
| `clk`, `rst` | in | as elsewhere in the agent |
| `start` | in | begin one entry |
| `mode` | in | opaque; the reserved request bits, carried not interpreted |
| `beat` | in | one source beat, `DATA_W` wide |
| `beat_valid` | in | asserted on an accepted beat, not merely an offered one |
| `need_beat` | out | occupant can accept another beat |
| `done` | out | one-cycle pulse; outputs valid from this edge |
| `word[OUT_WORDS]` | out | the produced entry |

**Geometry, declared by the occupant.** The two parameters the host module
needs before it can issue a read:

    parameter integer IN_BITS    // source bits consumed per entry
    parameter integer OUT_WORDS  // words produced per entry

`Q_ENTRY_BITS` becomes `IN_BITS`, `P_ENTRY_BITS` becomes
`OUT_WORDS * DATA_W`, and `Q_ARLEN` / `P_ARLEN` derive from them exactly as
they already do. The literals `2048` and `1024` leave the framework; the
arithmetic around them does not change.

**The start/beat rule becomes normative.** The behaviour described in the
comment above must be stated as a requirement on the occupant — *a beat
presented with `beat_valid` in the same cycle as `start` must be consumed, not
discarded* — or the inverse, that the host will never present one. Either is
implementable. What is not acceptable is the current arrangement, where the
rule exists only as a workaround duplicated at two call sites.

**Selection stays, naming goes.** Request `flags[4]` and `flags[5]` become
reserved transform-select bits with no assigned meaning, and the same for the
two host-window address bits. `QUANT` and `BLAYOUT` move to the project as the
names *KohakuTPU* gives those bits. The framework routes them to `mode` and
never reads them.

**A default occupant.** An identity transform — `IN_BITS = OUT_WORDS * DATA_W`,
beats copied through, `done` after the last one — so a project that wants no
transform instantiates it and the read path is a wire. This is the pattern
`l2_adapter.v` already uses, where `PASS=1` reduces the adapter to a straight
connection between its two faces. A slot whose empty state costs nothing is a
slot people will leave in.

## What this costs, and where it stops

The extraction moves one file, adds two parameters, renames four bit
definitions, and promotes one comment to a stated rule. It changes no
arithmetic and no handshake. It does not make the transform stage *general* —
the stage still converts whole entries, still buffers, still runs once per
fetched byte, and a transform that wanted to stream or to change entry count
would not fit. Widening it that far is a different design and is not proposed
here.

What it buys is that the sentence "the transform stage is a customizable addon"
becomes true. Today that sentence describes an intention.

## The general shape

The transform stage is the clearest case, not a special one. Every addon slot
in the framework has the same four obligations, and it is worth checking a
proposed slot against them before calling it one:

| obligation | transform stage today | what it needs |
|---|---|---|
| port contract | signals exist, informally | name them, fix directions, state the handshake |
| geometry contract | **absent** — host hardcodes 2048/1024 | occupant declares `IN_BITS`, `OUT_WORDS` |
| selection | exists, named for one project | reserved bits, opaque `mode` |
| default occupant | **absent** | identity transform |

The [endpoint L2 adapter](../notes/cache/noc-staging.md) satisfies three of the
four already: identical signal sets on both faces, and `PASS=1` as the identity
occupant. It has no geometry contract because it needs none — it neither
changes the size of what passes through it nor tells anyone else what it holds.
That is why it reads as a drop-in and the transform stage does not.

## Related

- [what-you-own](what-you-own.md) — which parts are addons at all
- [arch/mas/transform-stage](../arch/mas/transform-stage.md) — what the stage
  does and where it sits in the read path
- [projects/kohakutpu/number-format](../projects/kohakutpu/number-format.md) —
  the occupant, as one project's answer
- [spec/memory-protocol](../spec/memory-protocol.md) — the request flags the
  selection bits live in
