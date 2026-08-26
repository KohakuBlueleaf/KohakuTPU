---
title: Addon slots
summary: What a slot is, the four obligations one has to meet, and the transform slot as a worked example of meeting them.
tags:
  - integrate
  - addon
  - frameworkization
---

# Addon slots

> **Kind: Customizable addon**, which is the whole subject. The four obligations
> below are what makes something an addon rather than a hook, and the *interfaces*
> they describe are Fixed protocol — the normative form is
> [spec/transform-slot.md](../spec/transform-slot.md). What goes inside a slot is
> Yours.

An **addon** is a part the framework ships working and expects you to replace.
[what-you-own](what-you-own.md) says which parts those are. This page says what
a slot actually *is*, using the one the reference project fills.

A slot is not a module you swap. It is four things:

1. a **port contract** — signals, directions, handshake;
2. a **geometry contract** — what the occupant tells the host module about its
   own shape, because the host has arithmetic that depends on it;
3. a **selection mechanism** — how a request says *use this one*, without the
   framework knowing what it is;
4. a **default occupant** — something correct that does nothing, so a project
   with no use for the slot pays nothing and still elaborates.

Miss any of the four and you have a hook, not a slot.

## The worked example: the transform slot

The memory agent converts data on the way between memory and memory, once per
tensor rather than once per read. That stage is framework. The transform in it
is KohakuTPU's: FP16 to MXFP7 block quantisation.

### 1. Port contract

`mag_xform.v` drives the occupant with `start` / `id` / `mode` / `beat` /
`beat_valid`, and takes `done` / `word0..3` back. The full table, the three hard
rules and the timing conventions are
[spec/transform-slot](../spec/transform-slot.md).

One rule is normative rather than incidental: **a beat is never presented in the
same cycle as `start`.** The engine issues `start` when it issues its read and
the beats arrive later, so the hazard cannot occur. It used to exist only as a
workaround duplicated at two call sites, each depending on an undocumented
internal priority of the plug-in — which is the property that made the old
arrangement worse than no slot, because it advertised replaceability while
requiring the replacement to be bug-compatible.

### 2. Geometry contract

The occupant declares:

    parameter integer IN_BITS     // source bits consumed per entry
    parameter integer OUT_WORDS   // words produced per entry

A transform is not only a function on data — it changes how much data a move
must read and how far apart entries sit, and the engine needs both before the
transform has run. KohakuTPU declares 2048 and 4; the literals `2048` and `1024`
that used to sit in the memory port and the upload window are gone.

### 3. Selection

**An id, not a bit per transform.** `0` is bypass, `k` routes to occupant `k`.
Occupants are all resident in fabric — nothing reconfigures per request — so
more slots cost more area and buy a *choice*, not concurrency.

`XFORM_MODE` rides alongside, `MODE_W` wide, and is **opaque**: the framework
carries it to the occupant and never reads it. KohakuTPU's occupant reads
`mode[0]` as its A/B packing select — what the protocol used to call `BLAYOUT`
— and nothing in framework code is named after a number format.

The two retired names are not the same kind of thing, which is worth keeping
straight: `BLAYOUT` became an occupant's `mode[0]`, while `QUANT` selected the
transform *from a memory request* and has no successor at all. A request cannot
select a transform; only a mover descriptor can.

The id and mode ride in the mover descriptor's reserved space, not in a flags
byte that was already full.

### 4. Default occupant

`xform_bank` at id 0 copies beats through and raises `done` after the last one,
so a project with no transform gets a read path that is a wire and still obeys
the fixed output shape. This is the pattern `noc_l2_adapter` already uses, where
`PASS=1` reduces the adapter to a straight connection. A slot whose empty state
costs nothing is a slot people leave in.

### The fifth thing, which is not an obligation but is worth knowing

**A slot's control path can be present without being connected.** The transform
slot carries an occupant register port through `mag_xform` — write strobe, id,
offset, data, and a combinational read back — and whether anything drives it
depends on which control processor the node was built with. With the RV32 complex
it is decoded out of the processor's node range. With the RV64 complex
(`CPU_RV64`) it is **tied off**, and an occupant's registers are unreachable.

The consequence for you: **an occupant that needs configuration is not portable
across that parameter, and one with no registers is.** The shipping occupant
derives everything it needs from its per-move `mode` bits and from the data
itself, which is why a zero-register occupant is a complete one — and why that is
worth aiming for rather than a coincidence.
[spec/transform-slot.md](../spec/transform-slot.md) has the contract and the
divergence.

## Where the module boundary falls

The framework names **exactly one module**: `xform_bank`. It holds the project's
occupants and demuxes the id internally.

| file | side | what it is |
|---|---|---|
| `src/kohakuaccel/sysnode/core/mag_xform.v` | framework | arbitration, beat mux, registered stage, geometry parameters, the register pass-through |
| `src/kohakuaccel/sysnode/mover/mm_mover.v` | framework | drives the slot from its read-return path, mode 5 |
| `src/templates/transform/xform_bank.v` | framework | the identity bank: every id bypass, for a project with no transform |
| `src/kohakutpu/transform/xform_bank.v` | project | id 0 bypass, id 1 the quantiser |
| `src/kohakutpu/transform/mx_quant.v` | project | the occupant itself |

Renaming `mx_quant` now breaks one project file and nothing in the framework.

A build takes **one** `xform_bank`, the project's or the template's — they share
a module name on purpose, which is what lets the framework compile alone.

## Checking a proposed slot

| obligation | transform slot | endpoint L2 adapter |
|---|---|---|
| port contract | named, directions fixed, handshake stated | identical signal sets on both faces |
| geometry contract | `IN_BITS`, `OUT_WORDS` | needs none — it changes neither size nor content |
| selection | id + opaque mode, in reserved descriptor space | a `CU_CTRL` instruction |
| default occupant | id 0 bypass | `PASS=1` |

The adapter satisfied three of four from the start and read as a drop-in; the
transform stage satisfied none and did not. Both do now.

## What is still open

**Geometry is declared per agent, not per occupant.** `IN_BITS`/`OUT_WORDS` are
single parameters on `mag_xform`, so a bank with two occupants of different
shapes cannot describe itself — id 0's bypass already wants 1024 bits in where
id 1 wants 2048. With one real occupant this is correct; a multi-slot bank needs
the geometry indexed by id, and that is a protocol change rather than a wiring
one.

## Related

- [what-you-own](what-you-own.md) — which parts are addons at all
- [arch/sysnode/transform-stage](../arch/sysnode/transform-stage.md) — what the stage
  does and where it sits
- [spec/transform-slot](../spec/transform-slot.md) — the contract in full
- [spec/memory-protocol](../spec/memory-protocol.md) §10 — the retired request
  flags
