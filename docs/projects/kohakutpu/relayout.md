---
title: Relayout on the card
summary: Executing a byte-order conversion on a vector core instead of through the host — the granularity wall that made it look impossible, the 4x4 granule transpose that closes it, and the measured relayout counts before and after.
tags:
  - kohakutpu
  - compiler
  - memory
---

# Relayout on the card

`lang/backend.py:_conversions` computes every byte-order change a kernel implies
and records it on `Compiled.conversions`. Until now the only thing that executed
one was `kohakuaccel.rt.Runtime.convert`: read the buffer to the host, repack it
in numpy, upload it again. That is what `Device.counters["relayouts"]` counts,
and [sdxl-requirements.md](sdxl-requirements.md) §4 prices it at **37.7 GB per
UNet forward and 2.3 TB per image**.

This page is what it took to run those conversions on a vector core instead.

Every number is labelled:

| tag | meaning |
|---|---|
| **MEASURED** | run here, on the unit models (`kohakutpu.model`) or on the layout packers |
| **SOURCE** | read out of a file in this repository |
| **ARITHMETIC** | derived from the two above, with the derivation shown |

**Nothing on this page has been run on silicon.**

---

## 1. The headline

MEASURED, `SimDevice`, comparing the host path against the vector-core path on
the same call. `link` is operand and result bytes over the host link; `flits` is
the instruction stream, which crosses the same link at 40 bytes each (five
64-bit words per 288-bit flit).

| call | relayouts, host | relayouts, on card | link KB, host | link KB, card | flits, host | flits, card |
|---|---|---|---|---|---|---|
| `mlp` 64x128x256x64 | 1 | **0** | 216.0 | **152.1** | 432 | 640 |
| `flash_attention` 4 heads, L=64 | 3 | **0** | 352.0 | **160.0** | 5,080 | 5,448 |
| `flash_attention` 4 heads, L=128 | 6 | **0** | 1,056.0 | **288.0** | 10,048 | 11,000 |
| `flash_attention` 4 heads, L=256 | 12 | **0** | 3,616.0 | **544.0** | 24,672 | 27,056 |

The relayout counts are lower than this page first reported — §8.1 found that
half of `flash_attention`'s were moving bytes nobody read, and dropping them
helps the host path identically.

**Every conversion these kernels record now runs on the card.** The `relayouts`
counter — a host round trip, and nothing else — goes to zero, and the new
`relayouts_device` counter carries the same number.

**The numbers do not move at all.** A permutation is a permutation, and the
vector pass performs it with two instructions that move lanes and compute
nothing, so the results are bit-identical:

```
   mlp        p50 4.37e-03 p90 1.07e-02 p99 1.65e-02 max 2.36e-02  >1% 503  >10% 0
   flash L=256 p50 2.41e-03 p90 6.01e-03 p99 9.13e-03 max 1.42e-02  >1% 265  >10% 0
```

— identical for both paths, digit for digit, at every shape above. Those grades
are the kernels' own error against a float64 reference; the relayout contributes
none of it.

Both terms crossing the link, ARITHMETIC at 40 bytes a flit:

| call | host total | on-card total | |
|---|---|---|---|
| `mlp` | 232.9 KB | 178.0 KB | 1.31x |
| `flash` L=64 | 550.4 KB | 378.0 KB | 1.46x |
| `flash` L=128 | 1,832.5 KB | 746.0 KB | 2.46x |
| `flash` L=256 | 6,883.8 KB | 1,693.2 KB | 4.07x |

The gap widens with `Lkv` because the data a conversion moves grows with the
tensor while the program that moves it does not.

### 1.2 The instruction stream, and what it took to halve it

The first working version cost 824 / 6,372 / 14,140 / 35,228 flits on the four
calls above. Three changes, all MEASURED here:

| change | why it is free | flits it removed, `flash` L=256 |
|---|---|---|
| a word permutation needs **no lane pass at all** — VFILL contiguous into L1, VDRAIN contiguous out of it, and the walk on one of the two descriptors | the identity chain through the lanes was moving bytes that never needed to be in a register: 70 image words against 8 | 2,200 |
| the granule transpose at **VL=128**, so one register covers EIGHT groups and 36 instructions move 32 words | `VSHUF` rotates each 16-lane chunk independently and every group wants the SAME rotation, so the wider vector is free | 3,150 |
| reset only the descriptors the program **wrote** | each kernel here states all four dimensions of every descriptor it uses, so it inherits nothing and dirties nothing else | 460 |

Relayout flits, `flash` L=256: **10,556 -> 4,746**, a 55% cut. On the whole
transformer block: **10,336 -> 3,984**, 61%.

What was NOT done, and why: keeping the relayout image RESIDENT in instruction
memory across conversions. It cannot be cached at pc 0, because the kernel
stages between two conversions run their own programs there. It could live high
instead — but MEASURED, `flash_attention`'s own vector programs reach **314 of
the core's 512 instruction words**, so a 164-word relayout image fits above them
by 34 words and only for this kernel. The saving would be ~2,500 flits of 42,396
on the block, against a mechanism whose failure mode is a stale image running
and returning wrong bytes.

### 1.1 One whole `BasicTransformerBlock`

MEASURED at `DIM=256, HEADS=4, TOKENS=64, CTX=256` — the same call
[sdxl-requirements.md](sdxl-requirements.md) §4.2 measured, and the host figures
reproduce it exactly, which is what makes the two comparable.

| | host relayouts | on-card relayouts |
|---|---|---|
| dispatches | 79 | 103 |
| rounds | 377 | 417 |
| flits | 38,412 | 42,396 (43,044 routed through 2 MB `S`) |
| sent | 12.51 MB | 11.58 MB |
| fetched | 9.05 MB | 8.27 MB |
| **compiler relayouts, host round trips** | **24** | **0** |
| host permutes (`nn.RELAYOUTS`) | 9 | 9 |

Output bit-identical: `max 0.00e+00` over 16,384 elements.

**Read this honestly.** At this toy shape the 24 conversions were only ~1.7 MB of
a 21.6 MB bill, so removing them is a 1.07x saving on total host traffic once
the instruction stream is counted. What it *does* remove is 24 synchronous host
round trips from the middle of the block. The nine host permutes are a different
problem — a head split and a chunk, not a byte order — and
[sdxl-requirements.md](sdxl-requirements.md) §5.2 is the answer to those.

---

## 2. The granularity wall

This is the finding the rest of the page follows from, and it is why the existing
`entry_walk` and `ElementwiseKernel(drain=)` cover only a third of the problem.

**`vec_agu` walks 32-byte WORDS.** A VFILL step reads one whole word from memory
into L1 and a VDRAIN step writes one whole word out; the stride is in bytes but
the granule is not. So a conversion the AGU can perform on its own must be a
permutation of whole words.

MEASURED, by packing an identity image through each layout's own `pack` and
asking how many destination words exist whole in the source:

| conversion | shape | words | destination words found whole in the source |
|---|---|---|---|
| `flat -> entry:8x2` | (4, 128, 64) | 2,048 | **2,048 of 2,048** |
| `entry:8x2 -> flat` | (4, 128, 64) | 2,048 | **2,048 of 2,048** |
| `tile:2x8:8x8 -> entry:8x2` | (64, 256) | 1,024 | **0 of 1,024** |
| `tile:4x2:8x8 -> flat` | (4, 128, 64) | 2,048 | **0 of 2,048** |
| `flat -> tile:8x2:8x8` | (4, 256, 64) | 4,096 | **0 of 4,096** |

The reason is in the layouts. SOURCE `layout.py`:

* a `Flat` word is sixteen consecutive elements of ONE row;
* an `Entry` word is sixteen consecutive K elements of one lane, which is the
  same sixteen elements at a different address;
* a **`Tile` word is a 4x4 sub-tile** — four runs of four elements taken from
  four DIFFERENT rows.

So `Tile` disagrees with the other two **below the word**, at the 8-byte granule,
and no walk of any stride can split a word. That is not an efficiency problem; it
is the statement that a third of the conversions had no expression at all.

MEASURED counts of what each kernel records, so the split is not a guess:

| kernel | conversions | word permutations | needing sub-word work |
|---|---|---|---|
| `mlp` 64x128x256x64 | 1 | 0 | **1** |
| `flash_attention` 4x64 | 3 | 1 | **2** |
| `flash_attention` 4x128 | 9 | 3 | **6** |
| `flash_attention` 4x256 | 21 | 7 | **14** |

`entry_walk` and `ElementwiseKernel(drain=)` were written for the first column
and are correct for it. They cannot reach the second, and neither can the memory
mover — see §6.

---

## 3. What closes it: the 4x4 granule transpose

Take the tile image four words at a time. Word `j` of a group holds granule `i`
of sub-tile row `i`; the flat word the conversion wants at position `i` holds
granule `j` of each of the four. That is exactly a **transpose of the 4x4 array
of 8-byte granules inside each group of four words** — and what is left over
after it IS a permutation of whole words, which the AGU takes.

MEASURED, over every shape the shipped kernels record: after the granule
transpose, 100% of destination words exist whole in the source, for both
directions and for `gn` of 8 and of 2.

The lanes do it with ONE instruction, and it is not arithmetic:

```
   out_i = rot(v_0, 4i)                            VSHUF, unpredicated
   for j in 1..3:
       out_i = rot(v_j, 4(i-j) mod 16)  where P_j  VSHUF, pm=1 pr=j
```

`VSHUF` is `vd[l] = va[(l + k) % 16]` with `k` from a scalar register, so granule
`i` reaches granule `j` when `4j + k == 4i`; the predicate then gates the write
to granule `j`'s own four lanes. Nothing computes, which is why `0 * inf` cannot
appear in a lane nobody chose. MEASURED: an image carrying `+inf`, `-inf` and
`NaN` comes back byte-identical, with the vector core's saturation counter at
zero.

It was a rotate and a `VSEL` merge until `veccore` made `VSHUF` predicable —
§12 is the ask, the RTL reading behind it, and what it was worth.

**Eight groups per register.** `VSHUF` rotates within each 16-lane chunk
independently, and every group wants the same rotation, so one register at
VL=128 carries eight groups and 24 instructions move 32 words. ARITHMETIC: 24
instructions per 512 elements = **0.047 instructions per element**, against one
host round trip.

---

## 4. The two kernels, and their bounds

SOURCE `compiler/kohakutpu/isa/relayout.py`.

| | `Strided` | `Subtile` |
|---|---|---|
| what it performs | a permutation of whole words | the granule transpose, plus that permutation |
| body per unit of work | **nothing** — the walk is on a descriptor | 4 `VLD`, 16 predicated `VSHUF`, 4 `VST` per 32 words |
| VL | 128 (unused) | **128** — eight groups of four words |
| descriptors used | 2 of 8 | 5 of 8 |
| image | **8 words**, whatever the run | 28 + 24 per 32-word block |
| L1 footprint | `words` | `words + 1` index word |

`Strided` has no lane pass because it needs none: a `VFILL` lands its walk
contiguously in L1 from `l1off` and a `VDRAIN` reads contiguously from `l1off`
onto its own walk, so stating the permutation on one of the two descriptors
performs it outright. `Subtile` writes its output back OVER its input in L1 — a
block's four loads all precede its four stores — which is what lets one
descriptor serve both.

Bounds, SOURCE from the RTL constants and MEASURED against them:

| bound | value | consequence |
|---|---|---|
| `vec_agu` dimensions | 4 | a walk needing five is SPLIT into runs, not truncated |
| `vec_core` F_LEN | 256 words a walk | a run is at most 256 words |
| `L1_SAFE` | 320 words | `2 x words (+4)`, so a run is at most 128 words in practice |
| instruction memory | 512 words | `Subtile` unrolls every quad, so 128 words a run is 308 |

`entry_walk` remains the pinned reference for `Flat -> Entry`: the planner's
runs COMPOSE into exactly the permutation it describes, checked in
`compiler/tests/test_relayout.py`. `ElementwiseKernel(drain=)` is the shipped
strided-drain path and is NOT what runs here, for two measured reasons: it steps
both bases by whole batches, so it cannot express a walk whose outer axes
interleave; and its chain moves every byte through a register, which for a
permutation is 70 image words of work that the two descriptors already do.

### 4.1 Where the orders come from

The planner does **not** restate the byte orders. It packs an identity image
through each layout's own `pack` and reads the permutation off the result, so
`layout.py` stays the single authority and `compiler/tests/test_layout.py`'s SHA digests
still hold it. The identity numbers every element from one across **two** fp16
images, high half and low half, because sixteen bits cannot number a buffer past
65,535 elements and a repeated identity would silently identify two words as one.

A permutation the planner cannot factor into four dimensions at any run width
returns None, and the caller relayouts through the host — slow and correct, where
a truncated walk would be fast and wrong.

---

## 5. In place, staging, and the L2 tier

A permutation is **not** in general performable in place: a run writes words a
later run has still to read.

It is when every run permutes only its OWN words. The fill has brought all of
them into L1 behind a `VBAR` before the drain starts, so the memory they came
from is free. The planner tests exactly that — the run's walk is a bijection onto
`[0, words)` and the runs step together on both sides.

**MEASURED: every conversion `mlp` and `flash_attention` record is in place.**
So the shipped kernels pay one pass, no staging span, and no extra DRAM traffic
at all. A conversion that is not walks into a staging span and a second pass
copies it home — and whether one is in place is a property of the SHAPE as much
as the pair: `entry:8x2 -> flat` is in place at (64, 64) and moves words between
runs at (64, 128).

**The planner prefers a run width that CAN go in place**, because a machine with
no staging store has no other option; the width is otherwise chosen on flits, and
the widest is not the cheapest — MEASURED, a 1,024-word transpose over four batch
elements is 317 flits at 128 words a run and 413 at 256. Whether the in-place
route is USED is then a cost question, and §6.3 says it usually should not be.

### 5.2 Against §4.3 of the notes, which says in-place is impossible

`docs/notes/data-movement-problem.md` §4.3 states that **"in-place is generally
impossible, because a non-square permutation decomposes into cycles and an
affine walker cannot follow a cycle."** That is correct, and this page's
measurement is not a counter-example — the two are about different engines.

* §4.3's engine is the **mover**, which streams `M -> M` one element at a time
  under two walkers. There is no buffer between them, so writing the destination
  destroys a source element that a later step still needs. Following the cycle
  instead is precisely what an affine walker cannot do.
* This page's engine is a **vector core**, and the fill has brought the whole run
  into L1 **behind a `VBAR`** before the drain issues its first word. The run is
  therefore out-of-place *through `S`*, and in-place only as `M` sees it.

So the condition is not "the permutation has no cycles" — it is "the cycles are
contained in something that fits `C`". The run width IS that container, and the
planner's test is exactly this: the run's walk is a bijection onto `[0, words)`.

**And this does NOT imply the shard-axis invariant, which I assumed it did until
I computed it.** In place at width `w` gives shard-locality only when the shard
block is a whole number of runs. MEASURED, `entry:2x2 -> entry:8x2` over
(64, 128) is in place at 256 words a run and is NOT shard-local at four ways —
the 128-word block is narrower than the run, so a word can move between blocks
without leaving its run. At two ways, where the block IS a run, it is local.

### 5.3 The staging tier

That staging span is what the MAG L2 is for, and it is now allocatable.

SOURCE `mag_stage.v:74`: the store is reached BY ADDRESS and never by an
instruction — `addr[39] && !addr[38] && addr[37:36]==MESH && addr[35:32]==AP_STAGE`.
`machinespec.stage_addr` forms it and `kohakutpu.staging.stage_arena` hands it
out: **2 MB per mesh**, `STAGE_BANKS 4 x STAGE_ENTRIES 16,384 x 256 b`.

Two surfaces:

```python
    L.temp(M, F, tier="l2")        # a kernel's intermediate lives in staging
    dev.alloc(nbytes, "l2")        # and so does a relayout's staging span
```

MEASURED end to end on the models: the temp's address comes back as
`0x8000000000` — bit 39 set, aperture 0 — and the cluster's FILL and DRAIN carry
it. A runtime with no staging arena places the same temp in DRAM **silently** and
returns the same numbers, which is what lets a kernel name a tier a machine may
not carry.

**The design rule, unchanged from [sdxl-requirements.md](sdxl-requirements.md)
§5.3: L2 holds the operand that is RE-READ, not the one that is streamed.**
Nothing here changes the fit arithmetic — one head's K and V at `Lkv=4096` is
1.0 MB and fits; the three attention temps at `span=4096` are 15.4 MB and do not.

**The CONTROL plane is verified on silicon; the DATA path is not.** All ten NoC
L2 adapters answer `L2_CAPS`/`L2_BASE`/`L2_EN`/`L2_COUNTERS` and take a written
base, measured on v7 mesh 0. What is verified HERE is only that a staging address
can be FORMED, that the compiler and the unit models accept it, and that the
arena bounds it at 2 MB — an offset past that WRAPS onto another entry rather
than faulting. **No compute unit has issued a staging address on the card.**

A REMOTE staging address is now traced, and the answer is no: `mag_ilink`'s AXI
slave side is wired to the **mover's** write channel alone (`mag.v:973-977`), so
nothing a compute unit or the host issues reaches the forwarder. Only the mover
crosses. `docs/address-map.md` has the path.

---

## 6. What a conversion costs

`kohakutpu.cost` priced STATEMENTS, and a conversion is not one — so a plan
reported identical cycles with and without the byte order change it implies. A
cost model that cannot see the thing this page removed is a cost model that
would let it back in.

Two currencies, because they answer different questions. **Cycles** say what
this machine spends and are checkable against the simulator. **Credits** —
`docs/notes/data-movement-problem.md` §5 — say which PLAN is cheaper, and are
the only currency in which a route through a different memory tier can be
compared with one that stays put.

### 6.1 Cycles, against the simulator

`cost.relayouts` emits one stage per conversion, priced by walking the image that
would be emitted. It agrees with the simulator EXACTLY:

| call | analytic cycles | `SimMachine.busy` |
|---|---|---|
| `mlp` 64x128x256x64 | 9,783 | **9,783** |
| `flash_attention` 4x128 | 116,610 | **116,610** |
| `flash_attention` 4x256 | 541,114 | **541,114** |

Two independent routes to one number, which is the standard `ClusterUnit.cost`
already holds itself to. It closes because both now read one table:
`hw.vector.cycles(op, vl)` — a flit costs a cycle to take, a RUN costs what its
image ran, and a `VFILL` or `VDRAIN` is ISSUED in a cycle whatever it moves.

**And the number is alarming, which is the point of measuring it.** (Cycles here
are the no-staging route, which is what the default runtime runs.)

| call | total cycles | of which relayout |
|---|---|---|
| `mlp` 64x128x256x64 | 18,871 | **9,783 — 52%** |
| `flash_attention` 4x128 | 187,522 | **116,610 — 62%** |
| `flash_attention` 4x256 | 666,042 | **541,114 — 81%** |

ARITHMETIC for why: `Subtile` spends 24 instructions per 32 words, and at VL=128
each is 8 cycles — **6 cycles a word, 0.375 a element**, against `Strided`'s 14
cycles for a whole 256-word run. The transpose is ~110x the cost of the
permutation it accompanies, and it is 99.6% of the relayout bill.

That cost is **independent of VL**: a wider vector covers proportionally more
groups per instruction, so VL=128 bought flits and not cycles. Four rotations
per output word is the floor now that the merge is a predicate rather than an
instruction, and there is nothing under it with the instructions this core has.

The figures in the two tables above are the ones this page reported before §8
and §12 landed; §12 carries the current ones.

**So the conclusion this page ends on is not "relayout is solved".** It is that
executing a conversion on the card removes the host from the loop and costs real
cycles, and that the way to make attention fast is
[sdxl-requirements.md](sdxl-requirements.md) §5.2 — delete the conversions at the
kernel level — not to execute them faster. The one hardware change that would
move this: **a predicated `VSHUF`, or any cross-lane select with a lane-index
source**, would take seven ops per word to four. `vec_lanes` has the predicate
file already; whether `VSHUF` honours `pm`/`pr` is not something to guess at, and
`model.VectorUnit._shuffle` does not model it.

### 6.2 Credits, and the parameters they come from

`docs/notes/data-movement-problem.md` §5. Relative credits per byte, from two
ratios only — the path widths `w_S : w_M : w_L = 4 : 2 : 1` and slow memory's
locality penalty `ρ ≈ 15`:

| operation | credits / byte |
|---|---|
| `S -> S`, same unit | 1 |
| `M` sequential | 2 |
| cross-unit, per hop | 4·h |
| `M` non-sequential | **30** |

Mapping the abstract machine onto this one, which the notes deliberately leave
to the reader: `N = 4` units on an **open path** are the four meshes (SLR order
0-1-3-2, so hops go through `MachineSpec.mesh_hops`, not index arithmetic); `S`
is the **2 MB MAG staging store** and `M` one mesh's **4 GB of DRAM**, which is
exactly the notes' `M/C ≈ 1800`; `d_max = 6` is the mover's two `mx_tdesc`
walkers, against `vec_agu`'s four.

A conversion is two transfers — a fill and a drain — and whichever descriptor
carries the walk is the non-sequential one:

| route | transfers | credits / byte |
|---|---|---|
| over the buffer | fill `M` seq, drain `M` **ragged** | **32** |
| staged in `S` | fill `M` seq, drain `S`, fill `S`, drain `M` seq | **6** |
| staged in `M` | fill `M` seq, drain `M` **ragged**, fill `M` seq, drain `M` seq | 36 |

### 6.3 The route flip

**The credit model inverts the pass count, and that is its whole value.** Walking
over the buffer is one pass and puts a ragged access on slow memory; staging in
`S` is two passes and does not. The notes' own guidance is that a plan spending
one or two extra local passes to avoid a ragged slow-memory access is almost
always right — and here it is 32 credits a byte against 6.

`cost.route_for` therefore routes each conversion, and `rt.convert` follows it.
MEASURED over the shipped kernels' own conversions:

| call | credits/byte, no `S` | credits/byte, 2 MB `S` | |
|---|---|---|---|
| `mlp` 64x128x256x64 | 32.0 | **6.0** | 5.3x |
| `flash_attention` 4x64 | 32.0 | **6.0** | 5.3x |
| `flash_attention` 4x128 | 32.0 | **11.8** | 2.7x |
| `flash_attention` 4x256 | 32.0 | **13.4** | 2.4x |

It is not 5.3x everywhere because **staging absorbs the walk only in `drain`
mode**. A `fill` mode plan gathers from the buffer ragged whatever it writes to,
so staging buys a pass and removes nothing — the router knows that and leaves
those in place, which is why the routed figure beats a forced-staging one (11.8
against 12.2 at L=128).

The flit price of the flip is small, because a batch stages into one span per
element and is still two dispatches: `flash_attention` L=256 goes **29,418 to
30,498 flits, +3.7%**, for 2.4x fewer credits. Results stay bit-identical.

**The sensitivity, stated because the conclusion depends on it.** `ρ` is the one
empirical input and it has NOT been measured on this machine. At `ρ ≈ 15` the
flip is 5.3x; at `ρ = 2` the two routes tie; below that, walking in place wins.
The notes say to trust the ordering rather than the constants, and the ordering —
ragged slow memory is the expensive thing — is what this rests on. The default
runtime has no staging arena attached and is unaffected either way.

### 6.4 The shard-axis test

§7 question 3 of the notes: *"Formalise 'the shard axis survives the layout
change'. This predicts zero link credits and is the single highest-value
compile-time test."*

`RL.shard_local(plan, shards)` answers it exactly and without search. A sharding
is a partition of the buffer into `shards` contiguous blocks; the layout change
preserves it **iff no word leaves its own block**, which is one comparison per
word against the permutation the planner already holds. `RL.crossing` returns the
hop-weighted bytes that do move, so a false answer is a NUMBER rather than a
verdict.

MEASURED at four ways over the layout pairs the kernels use:

| conversion | in place | shard-local | crossing |
|---|---|---|---|
| `tile:8x2:8x8 -> flat` (256, 64) | yes | **yes** | 0 B |
| `flat -> tile:8x2:8x8` (256, 64) | yes | **yes** | 0 B |
| `flat -> entry:8x2` (64, 64) | yes | **yes** | 0 B |
| `tile:2x8:8x8 -> entry:8x2` (64, 256) | yes | **yes** | 0 B |
| `entry:8x2 -> flat` (64, 128) | no | no | 8,192 B |
| `entry:2x2 -> entry:8x2` (64, 128) | **yes** | **no** | 8,192 B |

That last row is the one to keep: **in place does not imply shard-local**, and
§5.2 says why.

## 7. The memory mover: what it can and cannot be asked for

**A transpose is a DESCRIPTOR, not a mode, and `MODE_TRANSPOSE` is not a gap.**
`docs/notes/data-movement-problem.md` §2.4: every index permutation *is* affine —
a transpose merely reorders the `(c_k, s_k)` pairs — so it is `COPY` with the
source and the destination walked in different orders, six levels each. That the
RTL refuses mode 1 in `I_IDLE` is a canned convenience nobody built and nobody
needs; **do not file it as missing RTL and do not design around its absence.**

Two earlier readings on this page were wrong and are corrected here: "the
descriptor was never implemented in software" (it does not need one), and "mode 1
needs RTL" (nothing needs mode 1). `driver/tests/test_mover.py` builds a
`(rows, cols)` transpose out of two walkers to make the point concrete.

Three facts decide what the mover IS good for, all SOURCE `mm_mover.v`:

| fact | line | consequence |
|---|---|---|
| `lt_ma = |dst_addr[4:0]` faults `F_ALIGN`; `awsize` is 32 bytes | `:270` | **word granular** — it can perform the same word permutations the AGU can, and nothing finer |
| two `mx_tdesc` walkers, `NDIM(6)` | `:171`, `:184` | **six** affine dimensions, against `vec_agu`'s four, and no 256-word walk limit |
| "Nothing here masks the top four, so an aperture descriptor reaches MAG's L2 unchanged" | header | it can address the staging store |
| the DESTINATION defines the iteration space | `desc_next` on `!dst_last` | a source stride of 0 broadcasts; `src_valid` low injects the immediate, which is how padding works |

So the mover is a **better engine for the word-granular half** — six dimensions
and no walk cap — and no engine at all for the half that needs the granule
transpose. It is dispatched by the host, which is the design and not a defect:
`mm_mover.v` has a `cfg_*` port and no instruction port.

`driver/kohakuaccel/device/mover.py` is the command path: walkers, the register
program, and `status()`. Two traps are encoded in it because both are silent:

* **A dimension is staged by `0x18` and LATCHED by `0x20`.** `d_dim_en` is pulsed
  by the `0x20` write, so `0x18` on its own loads nothing.
* **`mx_tdesc` dimensions are OUTERMOST first** — the highest live index is the
  innermost loop — which is the opposite of `vec_agu`, where dimension 0 is the
  fastest. A list written the vector core's way is a legal descriptor over a
  plausible wrong tensor.

`TRANSPOSE` is refused in software, and the refusal says to build it as `COPY`
rather than to ask for a mode. `driver/tests/test_mover.py` checks the encoding
field by field against `mm_mover.v`'s decode.

**No behaviour is verified and NO RATE EXISTS TO QUOTE.** The status decode is
correct — `mag.v:331` builds `{mv_done[23:0], rd_sum, wr_sum, mv_fault, 3'd0,
mv_busy}`, matching `status()` field for field — but on silicon the engine
accepts a command byte-identical to a passing bench, reports done with no fault
and **zero reads and zero writes**, and the destination is wrong. Identically at
200 and 100 MHz, so it is not timing. It has never moved a byte on the card and
the gap is on the config path.

---

## 8. The cheapest conversion is the one that does not run

Two changes, and between them they are worth more than everything in §1 and §6.

### 8.1 Half of them moved bytes nobody read

`Compiled.conversions` is derived from the sequence of USES, and nothing asked
whether the OLD order was still live. `flash_attention` reuses its temps across
key blocks, so after one block reads `scores` as flat, the next block's DRAIN
rewrites the whole buffer as tile — and the conversion between them moved bytes
that were overwritten before anyone looked at them.

MEASURED, `3*blocks - 3` of the `6*blocks - 3`:

| shape | conversions | dropped | cycles before | after | |
|---|---|---|---|---|---|
| 4 heads, Lq=Lkv=128, 2 blocks | 9 | **3** | 196,482 | 157,612 | −19.8% |
| 4 heads, Lq=128 Lkv=256, 4 blocks | 21 | **9** | 414,938 | 298,328 | −28.1% |
| 4 heads, Lq=Lkv=256, 4 blocks | 21 | **9** | 739,258 | 507,352 | −31.4% |
| 20 heads, Lq=Lkv=256, 4 blocks | 21 | **9** | 3,680,442 | 2,526,168 | −31.4% |
| 10 heads, Lq=Lkv=512, 8 blocks | 45 | **21** | 7,240,810 | 4,547,504 | −37.2% |

It helps the HOST path identically — `flash_attention` at 4x256 goes from 21 host
round trips to 12 — so it is not a property of executing them on the card.

**The rule is WHOLE-BUFFER, not merely written.** A partial write leaves the rest
in the old order for something later to read, so `_covers` proves the coverage
from the statements and returns False for any write it cannot place — a fused
drain lands in a peer's L1 rather than in memory, and a reduction's extent is not
an interval this bounds. `_whole` is the interval union and refuses a gap.

The dropped conversions stay IN `Compiled.conversions`, because the buffer is
still HELD in that order and that is what sizes its span; `Compiled.dead` names
them, `before()` does not return them, and `touches()` counts them as a write and
not a read — a dead conversion that looked like a read started the buffer's life
a stage early, which a lifetime planner would have paid for.

Numerics are unchanged BIT for bit, which is the only acceptable result: the
bytes it declines to move are overwritten either way.

### 8.2 The converted temps belong in `S`, and then the conversion is nearly free

The remaining conversions were routed through staging by §6.3 — two passes to
avoid a ragged DRAM access. That was the right call given where the buffer was.
It is the wrong question.

**A temp whose byte order changes should LIVE in the staging store.** Then both
sides of every conversion are `S` accesses, where irregular access is free by
construction, and the walk needs no staging pass at all: it is one pass, in
place, over `S`. `TpuBackend.tiers` proposes exactly the converted temps and the
author's own `L.temp(tier=)` still wins.

MEASURED on `flash_attention`, over its own conversions:

| | credits/byte | flits, 4x256 |
|---|---|---|
| temps in DRAM | 32.0 | 27,384 |
| temps in `S` (2 MB) | **2.0** | **27,384** |

**16x in credits at no flit cost**, and the same answers. It also subsumes the
buffer-relocation idea this page used to carry as future work: relocating a
buffer mid-call reached 3 credits a byte, and placing it correctly to begin with
reaches 2 without changing an address, without a lifetime question, and without
`Kernel.__call__` learning to accept a new one.

**And it buys nothing at all against the term that actually dominates.** Credits
price MOVEMENT. What a `Tile` conversion costs is ALU work — MEASURED at 4x256,
the two `Tile` crossings are 99.6% of the remaining conversion cycles and
`flat <-> entry` is 0.4%:

| temp | conversion | each | live | share |
|---|---|---|---|---|
| `scores` | `tile <-> flat` | 26,534 | 4 | 49.8% |
| `part_o` | `tile <-> flat` | 26,534 | 4 | 49.8% |
| `weights` | `flat <-> entry` | 280 | 4 | **0.4%** |

Over (1024, 64) that is **95x** between the two kinds, and `cost.route_for`
returns None for both `Tile` pairs — no staging route beats walking in place,
because there is no ragged access to avoid, only lanes to spend. So §6.3's route
flip and §8.2's tier are both real and both aimed at the memory system; the
critical path is §12's, and deleting the conversion is §13's.

A tier is a PLACEMENT and not a semantic. A machine with no staging arena, or one
whose store is full, puts the temp in DRAM and runs — the fallback is counted as
`staging_full` rather than being silent, because a store that quietly stopped
being used is a cliff nobody would see. ARITHMETIC: three temps at 10 heads and
Lq=512 are 1.92 MB and fit; at SDXL level 1's Lq=4096 they are 15.4 MB and do not,
which is [sdxl-requirements.md](sdxl-requirements.md) §5.3's tiling question and
not this page's.

## 9. The cross-kernel relayout, which `conversions` never sees

`Compiled.conversions` is the INTRA-kernel list. A tensor one kernel drained and
another fills in a different order changes hands in `rt.Tensor.address`, between
two calls, and went to the host and back — MEASURED, exactly three per attention
call (`q`, `k`, `v`), and it does **not** grow with the key-block count.

**The two kernels cannot be made to agree, and that is silicon.** A cluster
DRAINS sub-tiles and FILLS entries; `Tile -> Entry` between a projection and an
attention is the same asymmetry §2 is about, not a choice either kernel made. So
this one is worth executing rather than designing away.

It is also the CHEAPEST case, not the hardest: the two buffers are different
memory, so no run can write a word a later run still has to read. One pass, no
staging, no in-place constraint on the run width. `Holder.reorder` is that path
and `Tensor.address` tries it before falling back to the host.

MEASURED, `(4, 128, 64)` `Tile -> Entry` on the models: host round trips 1 -> 0,
link traffic 256.0 -> 128.1 KB — the remainder is the operand's own upload, which
both paths pay. Bytes identical to the target layout's own `pack`.

A tensor whose contents are still on the HOST is left alone: packing there is one
upload against a read and a walk, and `relayouts` never counted it.

## 10. `Buffer.repeated()` wrote part of its result and reported success

The worst failure shape this project has, found by the `kernels` agent and fixed
here. MEASURED before the fix: a 2,048-element pass against a 512-element table
wrote 512 elements, **left 1,536 unwritten and returned success**; at more than
one grid instance it refused instead.

Two things had to be wrong together, and both are about a stride-0 read:

1. **`lang/backend.py:_span`** clamped the pass to `_reach` of every operand. A
   broadcast covers ANY length — `_agree` already exempted it and `_span` did
   not, so the pass was cut to the table.
2. **`isa/vecemit.py:Spread.dims`** stepped the walk's outer dimension by one
   period per group. That is right for a `per_group` read of a full-length
   buffer and walks off the end of one that IS one period.

Only the buffer's LENGTH tells the two apart, so `Spread` now carries it and
`Spread.wraps` decides. `per_group` is unchanged, which the tests pin directly.

A period that does not divide the pass is now REFUSED rather than truncated: the
RUN would start part way into the table and the slice it needs wraps, which no
single affine walk expresses — outside the one-pass class of
`docs/notes/data-movement-problem.md` §2.4.

## 11. Two decoder defects found on the way

Both are in `kohakutpu/model.py`, both are the same shape, and both are the
reason no L2 address had ever been formed even in simulation.

**A 40-bit address is SPLIT across two encoded fields** in both instruction sets
— `isa/vector.py:VecConfig.desc_value_hi_bits` and
`isa/cluster.py:IsaConfig.addr_hi_bits` — so that widening to 40 bits moved no
other field. The encoders split it. **The model's decoders read the low part
alone.**

| where | symptom |
|---|---|
| `VectorUnit._descriptor` | a descriptor base above `1 << 34` decoded as its low 34 bits — a staging base of `0x80_0000_0000` became `0`, and the fill read address 0 |
| `ClusterUnit._fill`, `_drain` | the same, for a FILL or DRAIN address |

Fixed by rejoining. This is worth recording beyond the L2 work: **an address
naming another MESH is also above bit 34**, so any multi-mesh instruction the
model executed decoded as local DRAM at the same offset — a legal read of the
wrong window, not a fault.

---

## 12. The predicated `VSHUF` ask — asked, verified, BUILT, measured

§6.1 asked for a predicated `VSHUF` on the strength of the transpose being 81%
of `flash_attention`'s cycles. §12.2 below is the RTL reading that said it was
two unconnected signals rather than new datapath. `veccore` then built it, and
this is what it was worth.

The merge is gone: four loads, four rotates per output word, four stores. **24
instructions per 32 words against 36**, and the twelve `VSEL` a block spent do
not exist any more — the rotate writes straight into its own granule's lanes.

MEASURED, one `tile <-> flat` crossing over (1024, 64): **38,511 -> 26,534
cycles, 1.45x.** Short of the 1.5x the instruction count implies, because a
run's preamble, descriptors and flits do not scale with its body.

Across the shapes, total call cycles — each column is the one before it plus one
change, and every one of them leaves the numbers bit-identical:

| shape | at the start | dead dropped (§8.1) | predicated `VSHUF` | |
|---|---|---|---|---|
| 4 heads, Lq=Lkv=256 | 739,258 | 507,352 | **338,320** | −54.2% |
| 20 heads, Lq=Lkv=256 | 3,680,442 | 2,526,168 | **2,048,400** | −44.3% |
| 10 heads, Lq=Lkv=512 | 7,240,810 | 4,547,504 | **3,591,968** | −50.4% |

The relayout's share of `flash_attention` at 4x256 went from **81% to 63%**.

The masks moved from vector registers into the predicate file, built with three
`VCMPEQ` against a lane-group index — granule 0 is the unconditional first write
and needs none. `VCMPEQ` already wrote a predicate, so no new instruction is
involved and the L1 table shrank from four words to one.

### 12.1 What the model had to stop ignoring

`model.VectorUnit._shuffle` ignored `pm`/`pr`. Three things about the RTL are
easy to get backwards and all three are now pinned by tests:

* **The predicate is indexed by the DESTINATION lane**, not by the source lane
  the rotate reads, and a lane it does not name KEEPS what the register held.
* **The VL tail mask is NOT applied.** A `VSHUF` writes whole chunks whatever VL
  is, because it takes the load/store write port where the enable is the
  predicate alone. A model that masked it would agree with silicon at every VL
  that is a multiple of 16 and disagree everywhere else.
* **Only `VSHUF` reads those bits.** `VLD`/`VST`/`VCVT`/`VBCAST` share the decode
  branch, but for them `ir[4:1]` is the low end of the descriptor offset, so the
  core forces `pm=0` — a model reading `pm` off any of the four would predicate
  a load by accident.

### 12.2 The reading that said it was two signals, not a datapath

Read from the RTL before it was built, **it was not new datapath — it was two
signals that were not connected.** Kept because the shape of the argument is the
reusable part: the file is the record of how the ask was priced.

**The predicate already drives a per-slice write enable.** `vec_lanes.v:436-441`:

```verilog
    wsl = p_ph * wwid + wg;
    nx_we[wsl] = ((p_pm == 2'd0) ? 1'b1
               :  (p_pm == 2'd1) ? p_pmask[wsl]
                                 : ~p_pmask[wsl]) & p_tmask[wsl];
```

`p_pmask` comes from the predicate file itself — `vec_lanes.v:364`,
`pmask_now = preg[q_pr][q_chunk*16 +: 16]`, over `reg [127:0] preg [0:3]` at
`:135`. So "write only the lanes a predicate selects" exists, per slice, today.

**`VSHUF` does not come down that path.** It writes through the load/store port,
and that branch hard-codes every lane — `vec_lanes.v:431-433`:

```verilog
    if (ls_we) begin
        nx_we = 16'hFFFF;
        nx_wa = ls_waddr;
```

`vec_core.v:760-763` is where the shuffle takes it (`lw_wdata <= shuf_out`), and
`vec_core.v:232` wires `lw_we`/`lw_waddr`/`lw_wdata` to those `ls_*` inputs. The
rotate itself is a dedicated two-stage network on the register read port
(`vec_core.v:336-356`), deliberately outside the ALU pipeline: "Two 4:1 stages
are one LUT6 per bit each; the flat 16:1 was four."

**The instruction word already carries the fields.** `vec_core.v:251`:
`wire [1:0] d_pr = ir[4:3], d_pm = ir[2:1];` — decoded for every opcode. They
reach `vec_lanes` only as `.iss_pm(g_pm), .iss_pr(g_pr)` (`vec_core.v:236`),
latched on the ALU issue branches (`:497`, `:547`, `:622`) and **not** on the
`VSHUF` branch (`:568`). `hw/vector.py:alu` already places `pr`/`pm` in a
`VSHUF` word, so the encoder needs no change at all.

So the delta is:

| where | change |
|---|---|
| `vec_core` | latch `d_pr`/`d_pm` on the `VSHUF` issue branch; two new output ports |
| `vec_lanes` | two new inputs; on the `ls_we` branch use the term already written at `:439-441`, indexed `preg[ls_pr][ls_waddr[2:0]*16 +: 16]` — the chunk is already the low three bits of `ls_waddr` |

**Not priced here, and deliberately.** The only genuinely new logic is a THIRD
read of `preg`: the existing two (`:363`, `:364`) are indexed by the ALU
pipeline's `q_pr`/`q_chunk`, which the `ls` path does not share, so it needs its
own 4:1 over a 4 x 128-bit array. Everything else is two ports and one 3:1
select replacing a constant. Whoever owns `vec_lanes` should put a number on it.

**One trap in the change.** The ALU term ANDs `p_tmask`, the tail mask. The
`ls` path must NOT inherit that: a `VST` or `VSHUF` writes whole chunks whatever
`VL` is, which is the documented behaviour and what the model implements.

**The win, as predicted then and measured now.** "Seven ops per word to four" is
right for the merge but the block is what matters: 4 `VLD` + 16 `VSHUF` + 12
`VSEL` + 4 `VST` = 36 instructions per 32 words, and predication deletes the
twelve `VSEL` — **36 to 24, 1.5x**, not 1.75x. Measured after the build: 1.45x,
the difference being the per-run overhead that does not scale with the body.

### 11.1 A risk this reading turned up

`vec_alu.v:66` has `parameter integer HAS_SEL = 1`, and `:219-220` builds `VSEL`
only when it is non-zero — at zero the `default: va = s1_a` arm at `:211` makes
`VSEL` return its FIRST operand, silently and with no fault. `Subtile` would then
return wrong lanes and report success.

CHECKED: `vec_lanes.v:170` instantiates `vec_alu` with `.MODEL` and `.PIPE_MUX`
only, so the vector core keeps the default of 1. Nothing else in `src/` overrides
it for this unit. **If a build ever gates it off, the granule transpose breaks
quietly** — that belongs with whoever owns the build configuration.

Also checked against the model, since `Subtile` depends on it: `vec_alu.v:201`
is `sel_nz = (HAS_SEL != 0) && (|s1_c[22:0])` — magnitude bits only, so `-0.0`
is FALSE. `model.VectorUnit`'s `np.where(c != 0.0, ...)` agrees, because numpy
also holds `-0.0 == 0.0`.

## 13. What is not done

| # | thing | why it stands |
|---|---|---|
| 1 | **Nothing has run on silicon.** | Every figure here is the unit models. The relayout programs use `VSHUF` and `VSEL` in a combination no shipped kernel has executed on the card. |
| 2 | The **granule transpose is still 63% of `flash_attention`'s cycles** at L=256, down from 81% | §12 built the predicated `VSHUF` and §8.1 deleted half the conversions; four rotations per output word is the floor for what is left. The remaining answer is at the KERNEL level — delete the conversion, do not speed it up. |
| 3 | The nine **host permutes** per transformer block | A head split and a chunk are not byte orders — `_conversions` never sees them. [sdxl-requirements.md](sdxl-requirements.md) §5.2 deletes them at the kernel level instead. |
| 4 | The instruction stream still **grows**, 24,672 to 27,056 flits at 4x256 | §1.2. The remaining term is one `Subtile` image per conversion, which cannot be held resident because kernel programs reach 314 of the core's 512 instruction words. |
| 4a | **A conversion is caused by a REDUCTION and only by a reduction** | MEASURED by `kernels`: a GEMM followed by an elementwise pass costs zero conversions — the drained temp stays `tile` and the other operands are assigned it. The same GEMM followed by a reduction costs one. So the compiler is already doing the only thing available at this level, and `flash_attention`'s floor is two granule transposes a key block. `sdxl-requirements.md` §5.2b. |
| 5 | A conversion whose walk needs five dimensions **at every run width** | Falls back to the host. None of the shipped kernels' conversions do, but the planner does not tile a refusal into two passes. |
| 6 | The mover is not wired to anything | The command path exists and is encoding-tested; nothing calls it, and nothing should until a mover appears in the simulator or someone measures one on the card. It is the better engine for the word-granular half — six dims, no walk cap, and it reaches the staging aperture. |
| 7 | `L.temp(tier="l2")` has **no caller** | The surface is built and tested; which kernel should use it is [sdxl-requirements.md](sdxl-requirements.md) §5.7's question, and the answer there is cross-attention's K and V. |
| 8 | `ρ` is not measured on this machine | §6.3. The route flip holds for `ρ > 2` and reverses below it. §8.2 does not depend on it: a buffer in `S` avoids the ragged access at any `ρ`. |
| 9 | A temp too big for `S` falls back to DRAM | §8.2. Three `flash_attention` temps fit at 10 heads and Lq=512 and do not at SDXL level 1's Lq=4096. Tiling them by head or query block is [sdxl-requirements.md](sdxl-requirements.md) §5.3's question. The fallback is counted, not silent. |
| 10 | The mover reports done and moves nothing | On silicon it accepts a command byte-identical to a passing bench, reports no fault with **zero reads and zero writes**, and the destination is wrong — identically at 200 and 100 MHz, so not timing. The engine has never moved a byte on the card; the gap is on the config path. Simulation says 88 -> 7,006 MB/s, so the rebuild is real. **There is no silicon mover rate to quote.** |
