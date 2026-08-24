---
title: Cache and staging
summary: Four candidate designs for what should sit between DRAM and the compute units, and why the answer is probably explicit staging rather than a cache.
tags:
  - notes
  - memory
  - research
---

# Cache and staging: the design space

Status: **two of the four are built, benched, and in the ship tops** --
`noc-staging` form 2 as `noc/endpoint/noc_l2_adapter.v`, and `mag-staging` as
`sysnode/core/mag_stage.v` behind `sysnode/core/mag_stage_port.v`, both under
`src/kohakuaccel/`. Each is optional and they are selected independently
(`gen_mesh.py --l2-mag / --l2-cu / --l2-vec`). The four-mesh design carries
8 URAM per CU adapter and 64 per agent in 4 banks.

**The control plane is verified on silicon and the data path is not.** All ten
NoC L2 adapters on v7 mesh 0 answer their capability, base, enable and counter
registers and take a written base; the store is disabled at reset, so an adapter
nobody configured claims no address. No compute unit has issued a staging
address on the card.

Numbers marked MEASURED come from the placed multi-mesh run of 2026-08-12 or
from out-of-context synthesis; everything else is arithmetic or assumption and
is labelled. The canonical copies of the measured figures are in
[projects/kohakutpu/results.md](../../projects/kohakutpu/results.md).

## The question

DDR4 is ~30-40 ns away (ASSUMED, user-supplied) -- 9-12 cycles at 300 MHz --
while URAM is 2 cycles and **9.38% used** (MEASURED: 120 of 1,280). Something
should live in between. What, and where?

## The four candidates

| doc | what | where | risk |
|---|---|---|---|
| [axi-tlb](axi-tlb.md) | address translation + cache on AXI | the AXI library, in front of the DDR4 controller | low, but wrong layer for operands |
| [mag-staging](mag-staging.md) | reserved address range backed by URAM | inside the memory agent | low |
| [noc-staging](noc-staging.md) | URAM node on a spare mesh local port | a mesh endpoint | low, reuses everything |
| [noc-auto](noc-auto.md) | routers snoop and cache in flight | inside the router | **high** |
| [endpoint-tagged](endpoint-tagged.md) | a tag array behind the CU port | a mesh endpoint | low — routing untouched |

## Shape of the answer

**Build the TLB in the AXI library unconditionally** -- it is a general fabric
feature every project gains from, and it is where such a thing belongs. Keep it
off the operand path here, with a bypass.

**For operands, prefer explicit staging over caching**, because the access
pattern is not something to discover at runtime: a GEMM sweep walks
`for kb: for g: for h` over addresses the compiler already computed. A cache
spends tags and comparators rediscovering what was written down.

**Between memory-agent staging and mesh staging, the deciding factor is reach,
not capacity.** An L2 wants a few hundred KB per pass; even a conservative budget
gives 3.5-5.9 MB per SLR. What is scarce is the ability of one centralised block
to reach URAM columns spread across the die, with the most crowded SLR already at
**95.80% CLB** (MEASURED). Mesh staging sidesteps this by distributing.

**Treat [noc-auto](noc-auto.md) as research.** It is the only option that changes
the mesh's character, and the only one that risks deadlock. It is also the only
one that could make this machine's interconnect genuinely unusual -- which is why
it is worth writing down, not why it should be built first.

## The thing that is already true

A cluster already has **shared fetch**: a fill descriptor names up to three other
compute units sharing one operand, the lowest-numbered one issues a single
descriptor, and the memory agent multicasts the result to all of them
([projects/kohakutpu/isa.md](../../projects/kohakutpu/isa.md) §3). That is
precisely the broadcast a shared cache would exist to provide, done with compiler
knowledge and without arbitration or coherence.

**Any caching proposal must say what it adds beyond shared fetch.** For
[noc-auto](noc-auto.md) in particular that is the central question, not the tag
array.

**[endpoint-tagged](endpoint-tagged.md) answers it differently from the rest, and
it is why the "prefer explicit staging" conclusion above is narrower than it
reads.** That conclusion holds for units whose addresses the compiler computed —
matmul and vector. It does not hold for a client whose addresses are discovered
at run time, and an application core is exactly that. What a tag array buys there
is not bandwidth but **integration reach**: a core nobody here wrote emits AXI
reads and will never emit a `MEM_RD_REQ` flit, so a tagged layer is the only way
it can join a mesh at all.

> One caveat on that argument as of this writing: the shared-fetch mechanism is
> decoded by the hardware and **the driver does not set it**, because a follower
> cannot yet tell which fill an arriving entry belongs to. So "already true" means
> the mechanism exists and is one rendezvous away from being usable, not that the
> traffic reduction is being measured today.
