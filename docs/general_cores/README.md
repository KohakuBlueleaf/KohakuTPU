# General cores, the memory mover, and crossing SLRs

Three pieces of one argument. The machine computes well and addresses badly:
`limits.md` is an inventory of what it cannot do, and almost every entry on it
is an addressing problem wearing a different hat. These documents are the
proposed answer.

| | |
|---|---|
| [`../memory-mover/`](../memory-mover/) | a layout, gather and fill engine **inside MAG** -- its own folder |
| [`cores.md`](cores.md) | four general cores, two on MAG and two on the NoC |
| [`isa.md`](isa.md) | what they run: an existing soft core, or one of ours |
| [`programming.md`](programming.md) | how a kernel reaches one, and what codegen emits |
| [`slr.md`](slr.md) | what changes when the design stops fitting in one SLR |

Nothing here is built. This is a design record so the reasoning survives the
discussion that produced it.

---

## 1. The axis everything splits on

[`../limits.md`](../limits.md) §1 divides every gap by one question: **is the
index a scalar the host already knows between kicks, or is it per element and
decided inside the kernel?**

```
   SCALAR-DYNAMIC     KV-cache offset, sequence length, which bank this call uses
                      -> a descriptor field. PRESENT, at one host round trip per step.

   ELEMENT-DYNAMIC    embedding lookup, top-k routing, x[mask], scatter-add
                      -> needs a VECTOR of addresses. ABSENT at any cost.
```

A general core alone buys the first and **nothing** of the second, which needs a
vector of addresses. The mover alone can carry a vector of addresses but cannot
decide what should be in it.

**Together they buy both**, and that is the reason these are one document rather
than three. A general core runs the top-k or the `argsort`; the mover performs
the gather; the clusters compute on the grouped result. `limits.md` §3.4
concludes that sort and top-k "stay on the host" as "the right permanent
answer" -- a conclusion reached against a machine with neither of these in it,
and one that should be revisited if both land. See [`cores.md`](cores.md) §5.

## 2. Why the mover is ranked first

Because it unloads the engine that is already the bottleneck.

Measured on a real-size attention workload: **8 vector cores against 16 gives
about 2x total throughput**, with vector busy only falling 97% to 88%. Attention
is vector bound, and it is not close to saturating.

Now look at what the vector cores are being asked to do. Every one of these is
"the bytes are correct, the order is wrong":

- a DRAIN writes sub-tile order, a FILL reads L1-entry order, and **only a
  vector store converts** -- so a matmul feeding a matmul now costs a whole
  relayout band, a pass that multiplies by 1.0 purely to move data
- a constant read element for element against a drained tile must be *stored*
  in drain order (`Program.packing` role `tile`)
- `(L, H*D)` does not lower, because a head of it is an inner multi-column
  slice; the transpose happens on the host
- `permute` past the last two axes, `concat` and `pad` are all refused
- `limits.md` §6.1 offers `P @ A` -- a permutation MATMUL -- as the workaround
  for a transpose, at `n^3` work to move `n^2` data

That work is being paid out of the scarce resource. A mover in MAG turns all of
it into descriptors, and hands the vector cores back to arithmetic.

## 3. The proposed floorplan, single SLR

```
        gen  vec  vec  vec  vec  gen
        mag  mgr  acu  vec  mgr  acu  vec
        mag  mgr  acu  vec  mgr  acu  vec
        mag  mgr  acu  vec  mgr  acu  vec
        mag  mgr  acu  vec  mgr  acu  vec
        gen  vec  vec  vec  vec  gen
```

8 matmul clusters (a `mgr` + `acu` pair, two per row), 16 vector cores, 4 MAG
ports one per cluster row, 4 general cores at the corners. The vector cores are
**interleaved with the clusters** rather than blocked off to one side, which is
the fix for the first version of this sketch: MAG on the left edge and every
vector core on the right put the bottleneck engine furthest from memory, and
[`../perf.md`](../perf.md) §0.1 already showed a far smaller layout change
costing about three points of peak, with the counters ruling out the memory
system and leaving routing.

**The interleave requires a datapath that does not exist yet.** Today
[`../compute/vector-core.md`](../compute/vector-core.md) §9 says nothing moves a
tile from a cluster to a vector core, so the epilogue path is
`acu -> MAG -> memory -> MAG -> vec` however close the two sit. That is an
argument for adding the instruction, not for abandoning the interleave -- and
the two directions land exactly on the two places the compiler round-trips
through DRAM today:

| direction | what it removes |
|---|---|
| `acu` -> neighbouring `vec` | the folded epilogue's trip: a `consumes` band loads FP16 back out of the region the cluster just drained |
| `vec` -> neighbouring `acu` | the `feeds_matmul` trip: attention's `p @ v` stores `p` in entry order to DRAM purely so a FILL can read it back, and the relayout band exists for the same reason |

**Not every pair can be adjacent, and the design should not pretend otherwise.**
In any mesh some cluster sits far from some vector core. So this is a
nearest-neighbour fast path with the DRAM path kept as the general fallback, and
the placement decision at level 2 chooses between them. That is strictly better
than today, where the slow path is the only path.

It is also the thing the current stack is least equipped to decide: nothing in
level 2 or level 3 models locality at all, and `Target` carries `clusters` and
`vector_cores` as flat interchangeable counts.

## 4. Status

Design only. `16` vector cores is a **resources** choice -- throughput is still
near linear there, and what bounds it is leaving room for routing and the
cross-SLR mechanism, not a knee in the curve. The exact topology is open.
