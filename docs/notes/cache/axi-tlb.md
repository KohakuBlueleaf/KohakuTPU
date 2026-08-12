---
title: AXI TLB and cache
summary: Translation and caching on the AXI path in front of the DDR4 controller — build the translation unconditionally, and compare any cache against the vendor's before writing RTL.
tags:
  - notes
  - memory
  - axi
---

# AXI TLB and cache in the AXI library

Translation and caching on the AXI path, in front of the DDR4 controller. Build
the translation unconditionally -- it is a general fabric feature, not a
KohakuTPU one. For the cache, compare against Xilinx **System Cache (PG118)** in
§2 before writing any RTL.

Status: discussion, nothing built.

## 1. Why it belongs in the AXI library regardless

Every project that uses this AXI library gets it. Translation is the kind of
thing that is tedious to retrofit and cheap to have, and it is independent of
whether any particular project's operand path ever uses it.

**It should have a bypass.** KohakuTPU routes operand traffic around it (see
[README](README.md) for why), while control, debug and host traffic go through. A
TLB that cannot be bypassed forces its latency onto a path that does not want it.

## 2. Xilinx System Cache (PG118) is the thing to compare against

Not generic AXI IP -- a real AXI L2 cache: AXI slave ports in, one master out to
memory, tags and data in BRAM/URAM, configurable associativity and line size,
write-back with allocate policies. Dropping it in front of the memory controller
is genuinely a few hours of work.

**VERIFY THESE AGAINST PG118 FOR THE TOOL VERSION IN USE BEFORE DECIDING** --
they are from memory and the limits have moved between versions:

| property | believed value | why it matters here |
|---|---|---|
| max cache size | 512 KB | vs **3.5-5.9 MB per SLR** buildable from free URAM |
| line size | up to 128 B = 1,024 b | an L1 entry is **928 b**, so one line covers it |
| ports | small number of AXI slaves | one memory agent per mesh may fit |
| policy | write-back, configurable allocate | more than needed; operands are read-only |

The line size is the pleasant surprise: 128 B covers a 928-bit L1 entry with
room, so the natural transfer unit survives.

**The capacity gap is the real issue.** 512 KB against a few MB of otherwise idle
URAM (**MEASURED: 120 of 1,280 used, 9.38%**) is the difference between holding a
pass's working set and thrashing on it. If the working set fits in 512 KB, System
Cache is the right answer and there is nothing to build.

### 2.1 The structural objection stands regardless of size

System Cache sits on the AXI path, so it sees the memory agent's traffic **after**
arbitration through the interconnect. Two things are lost there:

- **Shared fetch.** The agent knows which compute units share an operand and
  multicasts one read to all of them. Downstream of that, the cache sees one
  request and cannot tell it served four consumers.
- **The descriptor.** The agent walks a whole run from one descriptor. A cache in
  front of the controller sees the resulting beats, not the intent, so it cannot
  prefetch the run it already knows is coming.

Both are arguments for putting the store **where the intent is**
([mag-staging](mag-staging.md), [noc-staging](noc-staging.md)), not against System
Cache as such.

### 2.2 When to use it anyway

- **Host and control traffic.** Host DMA and debug paths have no descriptor
  structure and no compiler knowledge -- exactly the workload a general cache
  suits. Put System Cache there and leave the operand path alone.
- **As a measurement.** Instantiating it is cheap and would answer "does caching
  DRAM help this workload at all?" without committing to a design. If a 512 KB
  general cache moves nothing, a bespoke one is unlikely to.

**Own-design is justified only by capacity and placement, not by cleverness.** If
a hand-built store cannot beat 512 KB of vendor cache by enough to matter, it
should not be built.

## 3. Shape

A small fully-associative TLB is the right starting point, not a large
set-associative one:

- Page count in the tens, not thousands -- this is a fixed accelerator address
  map, not a general OS workload.
- Fully associative at that size is a handful of comparators, one cycle, no
  index/tag split, no conflict misses.
- Miss handling can be a fault to the host rather than a hardware page walk. There
  is no demand paging here; a miss is a software bug or a deliberate remap.

That last point removes most of the complexity of a conventional TLB.

## 4. Write policy

Translation alone has no write policy -- it does not hold data. If a **cache** is
added alongside it, see [mag-staging](mag-staging.md) §1: the operand traffic is
read-only for the life of a pass, output tiles are written once and streamed, so a
read-only cache with write bypass avoids dirty state entirely.

Do not build a write-back cache here on the assumption that it is more general. It
is more general and it is the wrong trade for the only workload that exists.

## 5. Open questions

- Does the host DMA IP's own address handling already cover the host-side need,
  making this purely an agent/controller concern?
- Should the TLB be per-channel (four controllers, four TLBs, no sharing) or
  shared? Per-channel matches the existing structure and avoids a crossbar.
- Where does it sit relative to the interconnect hierarchy that was already
  restructured into leaf and root? Adding a stage in front of the controller is
  cheap; adding one at the root is not.
