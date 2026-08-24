---
title: A tagged cache at the endpoint
summary: The fifth candidate — a tag array behind noc_cu_base that turns the framework's IO memory model into a memory model, so a core nobody here wrote can be attached without modification.
tags:
  - notes
  - memory
  - integration
  - research
---

# A tagged cache at the endpoint

The other four candidates in [README](README.md) all ask *where should data sit
between DRAM and a compute unit*. This one asks a different question:

> What does a unit have to KNOW in order to reach memory?

and the answer changes what can be attached to the framework at all.

## Two models, and only one admits foreign silicon

**The IO model**, which is what ships. A unit is handed a protocol. It builds
`MEM_RD_REQ` descriptors, allocates transaction tags, matches responses to them,
respects the one-outstanding-write contract, and honours credits. Memory is a
**device it drives**. Every compute unit reimplements this;
`src/kohakuaccel/pe/rv32/noc/rv_noc_req.v` is 453 lines of exactly that, and its
header states the job plainly: *"everything about the framework memory protocol
that RV32 software must never see."*

**The memory model.** A unit issues a load and receives data. A tagged array
behind it decides hit or miss and owns the fill. Memory is a **space it
inhabits**.

The difference is not throughput. It is that **an application core cannot be
taught the IO model.** A Cortex-A53 or a NaxRiscv emits AXI reads and an L2
fill request; it will never emit a `MEM_RD_REQ` flit, and it cannot be modified
into one without owning its RTL. So the memory model is not a more convenient
IO model — it is the only model under which third-party silicon can join a mesh.

That is what this candidate adds beyond shared fetch, which
[README](README.md) requires any caching proposal to answer. The answer is not
bandwidth. It is **integration reach**.

## Why the "prefer explicit staging" conclusion still stands

[README](README.md) argues against caching operands because a GEMM sweep walks
addresses the compiler already computed, so a cache spends tags rediscovering
what was written down. That argument is correct and it is **specific to units
whose addresses are known at compile time.**

The two conclusions are about different clients and do not conflict:

| client | addresses known | right answer |
|---|---|---|
| matmul cluster, vector core | compile time | explicit staging — `mag_stage`, `noc_l2_adapter` |
| application core, control processor running a graph | run time | tagged cache |

A machine wants both, selected per endpoint, exactly as the staging options
already are (`gen_mesh.py --l2-mag / --l2-cu / --l2-vec`).

## Where it goes, and why not the router

At the **endpoint**, behind `noc_cu_base` — a "CU base with a tag array".

[noc-auto](noc-auto.md) puts caching inside the router and is flagged there as
**high risk, the only candidate that risks deadlock.** The endpoint is a third
location that note does not consider, and it carries none of that risk: routing
is untouched, no flit is inspected in flight, and the deadlock argument for XY
routing is unaffected.

The slot is already proven. `src/kohakuaccel/noc/endpoint/noc_l2_adapter.v` sits
at a CU's NoC port with URAM behind it, is selectable per endpoint, and is in the
ship tops at 8 URAM per CU adapter. **What is missing is only the tag array,
fill and evict — not the port contract, not the placement, not the URAM budget.**

## The pattern is already built one level down

`rv_l1` plus `rv_noc_req` is this design at L1, private to one PE: the core
issues `lw`, a tagged array decides hit or miss, a miss becomes a flit, and
software never sees one. It passes 116 checks in `rv_front`.

So the work is not inventing a mechanism. It is **lifting that pair out of the
PE, making it a framework block any endpoint can sit behind, and sizing it to
L2** — with the fill path targeting the explicit staging that already exists
rather than going straight to DRAM.

## The hierarchy this completes

    DRAM
      <AXI>     tagged L3            -- vendor AXI cache IP, not written here
      <AXI>     explicit L2 staging  -- mag_stage, in the system node      BUILT
      <NoC>     explicit L2 adapter  -- noc_l2_adapter, at an endpoint     BUILT
      <NoC>     tagged L2            -- this note                          MISSING
                unit

Everything below the tagged L2 is explicit-address. The tagged L2 is the layer
that converts explicit into transparent, which is why it is the one a foreign
core needs and the one that is absent.

## Attaching an application core

Two routes, and the second is why this note exists:

1. **Through the station bus.** An app core with an AXI master is a station
   manager like any other. It reaches DRAM and control windows, and it reaches
   nothing on the mesh. Works today, no new RTL.
2. **Through its own MMU into a translating cache.** The core's MMU walks to
   what it believes is an L2; that L2 is this block, and a miss becomes a NoC
   flit. The core now reaches compute units and remote meshes as **address
   space**, and its RTL is untouched.

Route 2 is the one that makes KohakuAccel an accelerator framework rather than
an accelerator: it is what lets someone bring their own core.

## Open questions

- **Coherence.** Almost certainly none: a single writer per line, software
  ordering, and an explicit flush — the same contract `rv_l1` already has. Say so
  and enforce it, rather than leaving it undefined.
- **Line size.** 32 bytes matches the mover's word, the flit payload and MAG's
  internal beat. Anything else introduces a fragmentation the whole machine has
  so far avoided.
- **Does the fill target staging or DRAM?** Staging, if the mesh has it — the
  hierarchy above only pays for itself if a tagged miss lands in an explicit
  layer rather than crossing to DDR4.
- **Cost.** Unmeasured. The tag array for 8 URAM of 32-byte lines is small, but
  no OOC run exists and nothing here should be quoted as a figure until one does.
