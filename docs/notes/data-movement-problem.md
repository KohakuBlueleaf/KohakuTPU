---
title: Layout transformation on a chain of scratchpads
summary: An abstract statement of the data-organisation problem on N chained units with bounded local memory, push-only interconnect, and affine-descriptor movement. Costs are relative credits; no absolute rates appear.
tags:
  - notes
  - memory
  - research
  - open-problem
---

# Layout transformation on a chain of scratchpads

A problem statement, not a solution. It is deliberately context-free: no
hardware knowledge is needed, and **no absolute rates appear anywhere**. All
costs are relative credits, defined in §5.

## 1. The object

`N` units in a chain. Each owns a small fast memory and a large slow one. A
movement engine transfers data between any two locations, but its addressing is
restricted to affine index arithmetic, and communication between units is
**one-directional**.

Given a tensor in one layout, distributed one way, produce it in another layout,
distributed another way, at minimum cost.

## 2. The machine

### 2.1 Units and memory

`N` units indexed `0..N-1`. Each unit `u` has

- **`S(u)`** — *fast* memory of capacity `C`, uniform access cost, **no penalty
  for non-sequential access**;
- **`M(u)`** — *slow* memory, effectively unbounded (`|M| ≫ N·C`), with a
  **locality penalty**: sequential access is cheap, non-sequential access is
  much more expensive.

The asymmetry between `S` and `M` is the point. `S` is where irregular access is
free; `M` is where it is punished.

For the instance that motivates this: `N = 4` and `M/C ≈ 1800`, i.e. slow memory
is about three orders of magnitude larger than fast memory per unit.

### 2.2 Naming

A single flat address space over all units. An address names
`(unit, tier, offset)` with `tier ∈ {S, M}`. Any descriptor may *name* any
unit's `S` or `M`. The restriction below is on **direction**, not on naming.

### 2.3 Interconnect

The units form an **open path** — not a ring, not a complete graph:

```
u_0 --- u_1 --- u_2 --- ... --- u_{N-1}
```

Let `h(u,v)` be the hop distance along this path.

Three properties, all load-bearing:

1. **Push-only.** Unit `u` may WRITE to `S(v)` or `M(v)` for any `v`; it may
   **not READ** from any `v ≠ u`. A read path would require a return channel,
   tags, reordering and deadlock avoidance; it was judged not worth the cost.
2. **Routed.** A transfer between non-adjacent units traverses the intervening
   ones, and cost scales with `h`.
3. **Narrower than either memory.** Per unit time the link carries less than
   `M`, which carries less than `S` (see §5 for the credits).

**Corollary, and the most important structural fact in this document.** A
permutation is a bijection, so it can always be expressed as a **scatter** —
each source pushes its data to where that data belongs — rather than as a
**gather**, where each destination pulls what it needs. Push-only therefore does
**not** restrict which redistributions are achievable. It forbids only the
gather *formulation*. Any claim that some reshard is impossible here is a claim
about formulation, not about reachability.

### 2.4 The movement primitive

One command is a pair of **affine descriptors**, one for the source and one for
the destination. A descriptor of depth `d` is

```
D = { (c_1, s_1), ..., (c_d, s_d) },     d ≤ d_max
```

denoting the nested iteration

```
for i_1 in 0..c_1-1: ... for i_d in 0..c_d-1:
    address = base + Σ_k i_k · s_k
```

with counts `c_k ≥ 0` and **signed** strides `s_k`. Rules:

- **`d_max = 6`.**
- **The destination defines the iteration space**; source and destination are
  walked in lockstep, one element per step.
- **A source stride of `0` broadcasts** that axis.
- Movement is word-granular; a misaligned destination is an error.

*One exception, in the instance this note is written against: a move that also
converts format inverts the first rule, because the entry size is the
converter's rather than the mover's — the SOURCE defines the iteration space and
the destination steps once per entry. That also costs it the bound axes, since a
padded element issues no read and the converter would wait for a beat that never
comes. It does not change anything below: a converting move is still one affine
map, and the analysis in §4 onward is about the map.*

Therefore: **the set of one-pass transformations is exactly the set expressible
as a single affine map between index spaces of rank ≤ `d_max`.**

This is the crux. Every index permutation *is* affine — a transpose merely
reorders the `(c_k, s_k)` pairs — so permutations are one-pass **iff their rank
fits**. Transformations that are not affine in this sense (data-dependent
gathers, non-affine reindexing) are not expressible at all and must be
decomposed, or performed by something other than this engine.

### 2.5 Staging

`S → S` transfers within a unit are legal. A transformation may therefore be
staged through a deliberately non-final intermediate layout, entirely inside one
unit. The price is capacity — two live copies — and an extra pass.

## 3. Data model

A tensor `T` of logical shape `(n_1, ..., n_r)`. A **layout** is an injective
affine map from index space to offset,

```
L(i_1..i_r) = base + Σ_k i_k · σ_k
```

with stride vector `σ`. A **sharding** `π : indices → {0..N-1}` assigns each
index to a unit; in practice a partition of one axis into `N` contiguous blocks,
though the general case is open.

A **placement** is a pair `(L, π)`. The problem is to realise `(L, π) → (L', π')`.

## 4. The problems

### 4.1 Reachability

For which `(L, π) → (L', π')` does a **single** command per unit suffice?

Known necessary conditions:

- the combined index space has rank ≤ `d_max`;
- for each unit `u`, everything it must *read* lies in `S(u)` or `M(u)`, since
  it cannot read remotely;
- the per-unit destination footprint fits `C`, when the target is `S`.

The valuable sub-question: **for which `(π, π')` is the transfer purely local?**
If the shard axis is preserved by the layout change, no cross-unit traffic is
needed at all; if it is destroyed, the traffic is all-to-all. Characterise the
boundary exactly.

### 4.2 Decomposition

When one pass does not suffice — rank exceeds `d_max`, capacity is exceeded, or
the map is not single-affine — find a minimal chain

```
(L_0, π_0) → (L_1, π_1) → ... → (L_m, π_m)
```

with each step realisable per §4.1, minimising the §5 cost.

Is there a normal form? For the classical analogue — factoring a permutation
into moves realisable by a restricted engine — there is substantial theory. How
much of it survives the capacity bound and the path topology is unknown.

### 4.3 Capacity-constrained staging

A working tensor may exceed `C`, forcing **tiling**: partition the index space
into blocks that each fit, and order them so each is complete before use.

Interactions to characterise:

- out-of-place needs two live copies; **in-place is generally impossible**,
  because a non-square permutation decomposes into cycles and an affine walker
  cannot follow a cycle;
- tiling multiplies passes, and can convert a sequential `M` pattern into a
  non-sequential one — trading a cheap credit for an expensive one;
- tiling consumes loop levels, interacting with the `d_max` bound.

### 4.4 Scheduling and completion

Cross-unit writes are **posted**: the sender learns of completion when the
transfer is accepted, not when it lands. A consumer therefore **cannot** infer
completion from the producer falling idle.

The available guarantee: inbound traffic to a unit is a **single in-order
stream**, so a marker written last is observed last. That marker is the only
fence.

For an all-to-all reshard each unit is both producer to `N-1` peers and consumer
from `N-1`. Order the pushes so that

- link contention is spread rather than bursty (all units targeting one peer
  simultaneously is the bad case);
- each consumer determines completion with a bounded number of fences;
- finite per-hop buffering cannot deadlock.

## 5. Cost model — credits

Costs are **relative**, expressed in credits per byte moved. They are derived
from two ratios only: the **width** of each path, and the **locality penalty**
of slow memory. No absolute rate is used or needed.

Let `w_S`, `w_M`, `w_L` be the per-cycle widths of fast memory, slow memory and
the link, at a common clock. Cost per byte is inversely proportional to width.
In the motivating instance the widths stand roughly as

```
w_S : w_M : w_L  =  4 : 2 : 1
```

Let `ρ` be the **locality penalty** of slow memory: the factor by which a
non-sequential access costs more than a sequential one, set by the ratio of a
row-cycle time to a burst transfer time. Take `ρ ≈ 15` for the motivating
instance.

This yields the credit table:

| operation | credits / byte | origin |
|---|---|---|
| `S → S`, same unit | **1** | baseline, widest path, no locality penalty |
| `M` sequential | **2** | `w_S / w_M` |
| cross-unit, per hop | **4 · h** | `w_S / w_L`, times hop distance |
| `M` non-sequential | **30** | `(w_S / w_M) · ρ` |

Total plan cost:

```
cost = Σ_transfers [ 1·B_SS + 2·B_Mseq + 4·h·B_link + 30·B_Mrand ]
```

**The qualitative shape is what matters, and it is robust to the exact
constants:** non-sequential slow-memory access and multi-hop link traffic
dominate everything else. A plan that spends one or two extra local passes to
avoid either is almost always correct. Any proposed solution should be evaluated
under this ordering rather than under the specific integers, which are given so
that candidate plans can be compared numerically at all.

## 6. Worked instance

A rank-3 tensor of shape `(320, 64, 64)`, 2 bytes per element, total `2.5 C` —
so it **exceeds one unit's fast memory** but is well under the aggregate `N·C`.

Wanted: `(A, B, C) → (B, C, A)`, i.e. move the first axis last.

Two shardings, with sharply different consequences.

- **Shard on `B`.** Unit `u` holds `B ∈ [16u, 16u+16)` for all of `A` — a
  footprint of `0.28 C` per unit. The permutation never crosses a `B` boundary,
  so **every unit transforms its own slab with zero cross-unit traffic**; source
  and destination together are `0.56 C`, so it fits out-of-place in fast memory
  with no slow-memory traffic at all. Cost is `1 · B_total`, the floor.
- **Shard on `A`.** Unit `u` holds `A ∈ [80u, 80u+80)`. Every output position
  requires all of `A`, so the reshard is **all-to-all**. Under push-only this is
  still perfectly expressible as a scatter — each unit pushes its slice into the
  correct offsets of every peer's fast memory, with the permutation carried in
  the destination descriptor — but it spends link credits, which are expensive
  and hop-weighted.

Sharding on `A` is nonetheless wanted, because it enables the corresponding
compute to be parallelised over `A` (partition the reduction axis, sum partial
results). So the question is **not** which sharding is better. It is: **what is
the cheapest schedule for the fused permute-and-reshard**, given that it must be
a scatter, that the units form a path rather than a clique, and that a chain of
cheap local passes may beat one expensive global one.

## 7. Questions

1. **Characterise the one-pass class.** With `d_max` affine levels and the
   destination-defines-iteration-space rule, exactly which `(L, π) → (L', π')`
   are single-pass? A necessary-and-sufficient condition would let a compiler
   decide without search.
2. **Minimal decomposition.** For pairs outside that class, is there a canonical
   factorisation, and is minimum-cost decomposition under §5 tractable or
   NP-hard?
3. **Shard-axis invariants.** Formalise "the shard axis survives the layout
   change". This predicts *zero* link credits and is the single highest-value
   compile-time test.

   *Partly answered in the instance, and the answer is a computation rather than
   a theorem: `kohakutpu.cost.link_credits` walks every conversion a compilation
   implies and returns the cross-unit credits at a given shard count, and
   `isa.relayout.crossing` decides per conversion whether a shard's words stay
   within it. Zero is the survival case. What is still open is the
   CHARACTERISATION — a test on `(L, π)` rather than a walk over the words.*
4. **Intermediate layouts.** When is a deliberately non-final intermediate
   strictly cheaper? Both the `d_max` bound and the `30:1` penalty on
   non-sequential slow memory suggest it often is, but there is no theory for
   choosing one.
5. **Path topology.** How much changes because the units form an open path
   rather than a complete graph? Is there an ordering that makes all-to-all
   reshard cost `O(N)` rather than `O(N²)` in link credits, as ring and
   bucket-brigade collectives do?
6. **Completion.** With posted writes and one in-order inbound stream per unit,
   what is the minimum number of fences for an all-to-all reshard, and which
   orderings achieve it?

## 8. Parameters

| symbol | meaning | instance |
|---|---|---|
| `N` | units, open path | 4 |
| `C` | fast memory per unit | — |
| `M/C` | slow:fast size ratio per unit | ≈ 1800 |
| `d_max` | affine loop levels | 6 |
| — | cross-unit direction | write-only |
| `w_S : w_M : w_L` | path widths | 4 : 2 : 1 |
| `ρ` | slow-memory locality penalty | ≈ 15 |

`ρ` and the width ratios are the only empirical inputs, and the conclusions
should be checked for sensitivity to them. Everything else is structural.
