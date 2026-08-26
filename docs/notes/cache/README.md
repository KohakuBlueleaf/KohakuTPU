---
title: Cache and staging
summary: Five candidate designs for what should sit between DRAM and the compute units — which two are built, which three are not, and why the answer for operands is explicit staging rather than a cache.
tags:
  - notes
  - memory
  - research
---

# Cache and staging: the design space

Status: **mixed — two of the five are built and shipping, three have never been
built.** The table below is the authority on which is which, and each page
repeats its own status in its first line.

**Nothing in this directory is normative.** Read
[notes/README](../README.md) before treating any page here as a description of
hardware.

## Which of the five exists

| candidate | status | where |
|---|---|---|
| [mag-staging](mag-staging.md) | **BUILT and shipping** | `src/kohakuaccel/sysnode/core/mag_stage.v`, arbitrated by `mag_stage_port.v`, benched by `tests/sysnode/mag_stage_tb.v`, selected with `gen_mesh.py --l2-mag` |
| [noc-staging](noc-staging.md) — **form 2 only** | **BUILT and shipping** | `src/kohakuaccel/noc/endpoint/noc_l2_adapter.v`, benched by `tests/noc/noc_l2_adapter_tb.v`, selected with `gen_mesh.py --l2-cu` / `--l2-vec` |
| [endpoint-tagged](endpoint-tagged.md) | **PROPOSAL — not built** | nothing exists. The port slot it would occupy is proven; the tag array, fill and evict are not written |
| [noc-auto](noc-auto.md) | **PROPOSAL — not built, and not recommended first** | nothing exists. It is the only candidate that changes what the mesh is, and the only one that risks deadlock |
| [axi-tlb](axi-tlb.md) | **PROPOSAL — not built** | nothing exists. The translation half is recommended unconditionally; the cache half should be compared against vendor IP before any RTL is written |

Both built options are **optional and selected independently** per mesh. The
reference four-mesh design carries 8 URAM per compute-unit adapter and 64 per
memory agent in 4 banks.

**The control plane is verified on silicon and the data path is not.** On the
current image, all ten mesh-endpoint adapters on one mesh answer their
capability, base, enable and counter registers and take a written base; the
store is disabled at reset, so an adapter nobody configured claims no address.
**No compute unit has issued a staging address on the card.**

## The question

DDR4 is roughly 30–40 ns away (ASSUMED, supplied rather than measured) — nine to
twelve cycles at 300 MHz — while URAM is two cycles and the part's URAM is
**9.38% used** (MEASURED: 120 of 1,280, placed multi-mesh run of 2026-08-12).
Something should live in between. What, and where?

## Shape of the answer

**Build the address translation in the AXI library unconditionally** — it is a
general fabric feature every project gains from, and it is where such a thing
belongs. Keep it off the operand path here, with a bypass.

**For operands, prefer explicit staging over caching**, because the access
pattern is not something to discover at run time: a matrix sweep walks a nest of
loops over addresses the compiler already computed. A cache spends tags and
comparators rediscovering what was written down.

**Between memory-agent staging and mesh staging, the deciding factor is reach,
not capacity.** A pass wants a few hundred kilobytes; even a conservative budget
gives 3.5–5.9 MB per SLR from free URAM alone, and this part has four of them —
so the on-chip store available for staging is in the mid-teens of megabytes
(arithmetic over the per-SLR figure, not a placed result). **"It does not fit"
is almost always a tiling question rather than a capacity one.** What is scarce
is the ability of one centralised block to reach URAM columns spread across the
die, with the most crowded SLR already at **95.80% CLB** (MEASURED). Mesh
staging sidesteps that by distributing.

**Treat [noc-auto](noc-auto.md) as research.** It is the only option that
changes the mesh's character, and the only one that risks deadlock. It is also
the only one that could make this interconnect genuinely unusual — which is why
it is worth writing down, not why it should be built first.

## The thing that is already true

A cluster already has **shared fetch**: a fill descriptor names up to three
other compute units sharing one operand, the lowest-numbered one issues a single
descriptor, and the memory agent multicasts the result to all of them. That is
precisely the broadcast a shared cache would exist to provide, done with
compiler knowledge and without arbitration or coherence.

**Any caching proposal must say what it adds beyond shared fetch.** For
[noc-auto](noc-auto.md) in particular that is the central question, not the tag
array.

> **One caveat on "already true".** The shared-fetch mechanism is decoded by the
> hardware and **the driver does not set it**, because a follower cannot yet
> tell which fill an arriving entry belongs to. So the mechanism exists and is
> one rendezvous away from being usable; the traffic reduction is not being
> measured today.

**[endpoint-tagged](endpoint-tagged.md) answers the "what does it add" question
differently from the rest, and it is why the "prefer explicit staging"
conclusion above is narrower than it reads.** That conclusion holds for units
whose addresses the compiler computed. It does not hold for a client whose
addresses are discovered at run time, and an application core is exactly that.
What a tag array buys there is not bandwidth but **integration reach**: a core
nobody here wrote emits AXI reads and will never emit this framework's memory
request flit, so a tagged layer is the only way it can join a mesh at all.
