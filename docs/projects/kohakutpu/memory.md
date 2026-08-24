---
title: Memory: what binds a layout, and what is missing
summary: The card holds 16 GiB and a layout decides read speed, so this is not an allocator question alone — the two granules that bind every span, and the five gaps between here and a model-sized placement.
tags:
  - kohakutpu
  - compiler
  - kernels
---

# Memory: what exists, what binds it, what is missing

The card holds **16 GiB**, and a layout decides read speed — so this is not an
allocator question alone.

## 1. What binds the layout

**A drain burst is 256 bytes.** `WBURST = 8` granules of a 256-bit word, and a
`DRAIN` moves whole bursts whatever the sub-tile count says. So every span starts
on one and is rounded up to one, or a legal drain writes into the buffer after it
and reports success. `Arena.align` defaults to exactly this, and `rt.py` raises it
to `BATCH_BYTES` since a vector RUN stores a whole batch — two quanta, and the
allocator satisfies the larger.

**Byte ORDER is not the allocator's, but it is the read cost.** Two granules,
both falling out of `WORD_BYTES = 32`, `LANES = 4`, `KBLOCK = 32`:

| | shape | bytes | words |
|---|---|---|---|
| write, a DRAIN | 4x4 fp16 sub-tile | 32 | **1** |
| read, a FILL | 4 lanes x 32 K entry | 256 | 8 |

**A 4x4 sub-tile is EXACTLY one 256-bit word.** That is why the drained order
looks strange and is nonetheless the fast one for fp16 @ fp16 → fp16: row-major
would spread those sixteen values across four rows as four PARTIAL words, and a
partial word is a read-modify-write. The sub-tile order is not a quirk of the
drain — it is the only shape whose write granule is the memory granule.

A buffer in the wrong order costs a relayout pass. The allocator cannot fix that;
the conversion choice decides it.

**Which makes a fused epilogue a LAYOUT win, not only a latency one.** The tile
crosses the NoC into a vector core's L1 still in sub-tile order and is computed
there, so it never lands in memory in a shape something later has to convert. The
staging fallback pays that conversion; fusing avoids it. That is the real cost of
the shapes where fusion is refused — see [fused-epilogue.md](fused-epilogue.md).

**What is NOT modelled: DRAM row locality.** Nothing here knows a page size or a
bank, so two buffers read together may land in the same bank and serialise. The
one link figure ever measured is 98 MB/s at 3% of the interlink — superseded,
since it predates the mover rebuild (`multi-mesh.md` §8) — and `mag_dram_port`
crosses through `xpm_fifo_async`. DRAM was not the bottleneck at that rate and
nothing since has made it one, so this is correctly unbuilt rather than
forgotten. Revisit when a kernel is DRAM-bound, and re-measure first.

## 2. What exists now

| piece | where | state |
|---|---|---|
| free-list arena, coalescing, epochs | `kohakuaccel/memory.py` | best fit |
| fragmentation metric | `Arena.fragmentation` | free bytes not in the largest span |
| lifetimes and packing | `kohakuaccel/lifetime.py` | temps whose lives miss share bytes |
| alias guard | `lifetime.Writes` | refuses a READ of a span since overwritten |
| affordability | `Compiled.footprint` | refuses a call no ordering of this arena fits |
| band-adjacent placement | `lifetime.pack(groups=)` | opt-in; adjacency costs reuse |

## 3. Three failures worth keeping

A lifetime planner for whole forward passes was built once, against the previous
stack, and reached only from a scratch directory. It is gone with that tree, but
what it measured is not:

- **A plan whose DECLARATION order differs from the runner's ISSUE order verifies
  clean and reads an address the allocator has given away.** Measured: a per-head
  transformer block scored 1.38e-01 instead of 1.07e-03, with `verify()` passing.
- **Reuse aliasing is the one failure mode of a lifetime allocator, and it is
  completely silent on the machine.**
- **An unwritten line on this ECC DRAM is an uncorrectable error, not zeros.**

## 4. What is missing, and how to get it

1. **Nothing bounds a MODEL, only a call.** `footprint` prices one kernel; a
   transformer block is tens, and the peak is across them. The thing to build is
   lifetimes over a step list rather than over one kernel's stages.
2. **Weights are not distinguished from activations.** Pinning weights at the
   bottom in declaration order means adding a layer moves no weight already
   uploaded, and only activations are packed. At 16 GiB with SDXL's weights
   resident, that distinction is most of the budget.
3. **No cross-call reuse.** Every call allocates and frees its own temps; two
   kernels in a row re-pay. A scratch arena owned by the runtime and handed to
   successive calls would remove it.
4. **`fragmentation` is measured but never acted on.** `trim()` only reclaims at
   the top. With an epoch bump the arena can be reset wholesale between steps,
   which is the cheap defragmentation a forward pass can actually use.
5. **No test at 16 GiB.** Every test here runs on an arena of megabytes. A
   model-sized placement — weights plus a block's activations — has never been
   built, and the failure it would find is a peak, not a leak.

Order: 1, then 2, since both are lifetime planning over a step list rather than
anything invented; 3 and 4 are cheap once a plan owns the arena; 5 is the one that
says whether SDXL fits at all.
