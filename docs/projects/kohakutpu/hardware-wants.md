---
title: What the software stack wants from the next silicon
summary: Ten asks, each hit while writing the compiler or a kernel, each naming the level it was established at and what it costs today.
tags:
  - kohakutpu
  - compiler
  - kernels
  - hardware
---

# What the software stack wants from the next silicon

Everything here was hit while writing the compiler or a kernel, and each entry
says **where** it was established. Nothing is speculative: if a level is not
named, it is not on this list.

An entry here is a hardware ask. A compiler gap is a different thing even when it
feels like a wall — `<<= acc + bias[j]` looked like one for a whole afternoon and
turned out to be an emitter that had never been given a second operand. Four of
the ten below are marked **COMPILER** and are listed anyway, because the cost is
hardware capacity sitting idle.

## 1. A vector core cannot emit MXFP7, so VC → cluster L1 is shut

**Blocks** `linear → act → linear` with the activation never reaching DRAM, which
is the single largest block of an LLM layer.

**It also costs flash attention half its stages.** A key block is FOUR stages —
score GEMM, softmax band, `p @ v`, accumulate — and two of the four exist only
because the softmax runs on a vector core and `p @ v` needs `p` in a cluster's
L1. With this path open a key block would be two. At L=128, D=64 that is 10
stages against a floor of 6.

The NoC path exists and is RTL-tested ([isa.md](isa.md) §9, bench
`cluster_data`): a cluster DRAIN takes `dnode`/`dst_x`/`dst_y`, and the buffer is
"defined by whatever CU receives it". A cluster's `buf 0/1` is L1 A/B.

But §9.3: a payload for `buf 0/1` is byte-identical to a `MEM_RD_RESP` — 32 int7
plus 4 E5M3 scales — and "a sender into L1 is substituting for MAG and owes the
same format". A vector core's memory is FP32, FP16 and MXFP7 **read-only**, and
its peer path "lands RAW 256-bit words in L1: it moves whatever the sender put in
the flit". So a vector core would deliver fp16 bytes into a buffer the cluster
decodes as MXFP7 — a silent misread.

**The ask: a quantiser on the vector core's drain path, or an fp16 acceptance
mode on the cluster's L1 write port.**

## 2. One accumulator per cluster

**Costs:** two sweeps in one instance must run SEQUENTIALLY — all of a's chunks,
drain, then b's. Interleaving them compiles, runs, and returns a 123% error,
because the second GEMM's reset destroys the first tile. `ClusterUnit.acc` is a
single array; measured on the unit models.

It does **not** stop an epilogue reading two tiles. One drain hands over one
tile, so two tiles are two drains into two L1 slots of one vector core, and the
first is sequenced above the sweep that clears it — see
[fused-epilogue.md](fused-epilogue.md) §6. That is a gated MLP with no temp and
no MAG round trip, on today's silicon.

What is left is the *interleaving*: the two sweeps still run one after the
other, so the tile latency is serial. **The ask: a second accumulator, or a
tile-select on the GEMM's reset**, which would overlap them and halve the drain
latency rather than enable the fusion at all.

## 3. No path from an accumulator back into a sweep

`lang/cluster.py` refuses `acc @ x` and says why: a sweep contracts two L1
*regions*, an accumulator is neither, and no instruction feeds it back without a
drain. Combined with (1), this is what makes a truly fused MLP impossible today.

## 4. The bank bits are not wired through the planner

**COMPILER**, listed here because the cost is hardware capacity idle. In the
ISA's own words ([isa.md](isa.md) §4.6) the fields are decoded by the RTL and
`kernel.plan` writes zero into every one, "so every chunk lives in bank 0 and
**half of a 512-entry L1 is unreachable**". Wiring them "would restore `gn = 32`
at `nk >= 9`".

## 5. A tapped fill straddles a 4 KB boundary on 6 of 9 taps

One entry burst in 16, and **untested on silicon** — `mag_mem_port.v` has no
split logic. Three taps are exempt because their offset is a multiple of 256 B,
and padding the row moves *which* three while leaving the count at 6: nine bases
64 B apart cannot all land on 256 B, so no packing removes it. It needs a burst
splitter or a slave that tolerates the crossing. Parked, and the first thing to
check when [conv2d](conv2d.md) is next run on the card.

## 6. Eight descriptors is what splits a band, and a VALUE costs none

**COMPILER-VISIBLE**, listed because it is the number every kernel is written
against. A band needs two descriptors per read operand plus its regions, so eight
is reached fast. The discipline that follows is not obvious and cost real time to
find:

> Keep an intermediate a **value**, not a temp. `c = L.exp2(top[e] - m2)` used
> twice is one register; writing `corr` and reading it back is a ninth descriptor
> and the band is REFUSED.

Measured on flash attention: the six elementwise stages of a key block collapse
to ONE band under this rule and refuse to compile without it. `kernels/fused.py`
is the same story — `layernorm_fused` sits at exactly 8 of 8.

## 7. A per-channel operand cannot be STAGED

`lang/cluster.py` refuses it: once the fusion falls back, the pass runs after the
tiling and the tile index `b[j]` needs no longer exists. Reseating it to a
period-N spread gives rel err 1.00, and pinning the pass to row order as well
gives 1.15 — measured, twice.

So `linear_bias` is fused-only, and `linear_add` keeps a full-shape operand for
the shapes that must stage. **The fix is a COMPILER one:** a staged pass over a
`tile:`-ordered result wants the bias walked as `(1 word, gn), (0, gm),
(gn words, gridJ), (0, gridI)` — a four-dimension descriptor with two stride-0
axes, which `vec_agu` already has and `ResidentEpilogueKernel.static_descs`
already uses one of.

## 8. The cost model does not price a MAG round trip

**COMPILER, and it hides the single biggest win the kernels have.** The timing
analysis charges each statement its unit's work and sums the stages. A stage
BARRIER also writes the whole result out and reads it back, and that is charged
nowhere.

Measured on flash attention at L=128 D=64: unblocked is 10 stages and
`qblock=64` is 20, and `Timing.cycles` says **72,704 for both**. The 2x in memory
traffic is invisible at level 2 and only `sim.observe(...).stages` sees it. Until
a stage carries `result bytes / bandwidth`, cycles must not be used to compare
two stagings of the same kernel.

**The runtime counters have the same hole, the other way up.** `flits` counts the
NoC and `sent`/`fetched` count the host link; traffic inside the mesh is counted
nowhere. Measured on `demos/kohakutpu/mlp.py`, 6 fused layers against 6 staged:
the FUSED form spends 1,872 flits against 852 and is still 3–5x faster, because
putting the tile on the NoC is the point and the staged form's real cost is the
MAG round trip neither counter sees.

**The ask (COMPILER): one counter for bytes moved through MAG**, charged at every
stage barrier and every fill that misses. Without it, two of the three ways to
measure this machine say fusing is worse.

**The machinery for that ask now exists, and is pointed the other way.**
`kohakutpu.cost` prices data movement in `data-movement-problem.md` §5 credits —
`move(nbytes, tier, walk, hops)` is the primitive, and `credits()` walks a
compilation's byte-order CONVERSIONS with it. A stage barrier is the same
question with a different set of bytes: result out, result back, at `M`
sequential. What is missing is the accounting of which bytes a barrier moves,
not the model that would price them.

## 9. A symbolic loop index has no modulo

`causal` with GQA needs key block `0..(i mod blocks_per_group)` for query block
`i`, and `i` is a runtime loop variable. `kernels/attention.py` refuses the
combination by name and tells the caller to call once per group.

## 10. `nk` is forced to 1 for a convolution

Two channel blocks of one pixel are `plane*64` bytes apart, so one fill covers
exactly one K-block and a conv pass cannot amortise: `3*(9C/32) + 1` flits. A
layout consequence rather than a gate, but it is why conv is fill-bound. See
[conv2d.md](conv2d.md) §4.
