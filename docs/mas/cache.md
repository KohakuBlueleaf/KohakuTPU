# The memory system

Two caches, and only one of them has tags. This document is about why.

Supersedes [`../noc/spec.md`](../noc/spec.md) §9, which made the CU-side and
MAS-side levels the same parameterised cache module.

Status: **design, no RTL** — for the two cache levels. The rest of the hierarchy
below them is built: `mx_cluster_mgr` holds two 928-bit L1 memories (one per
operand, `kohaku_sdpram`, `READ_LAT=1`) filled explicitly by `FILL`, and
`mx_acu_fp` holds the resident output tile in 5 BRAM36. **There is no L2 today**
— a `FILL` reads straight from MAG, so a cluster re-reads an operand from DRAM
every pass it needs one. That is exactly the reuse an L2 would capture, and
[`../arch-design.md`](../arch-design.md) §4.2 measures what it currently costs:
the machine spends 57–72% of its time filling.

What the built L1 looks like from the instruction side is
[`../isa/cluster.md`](../isa/cluster.md) §3.

---

## 1. The hierarchy

```
                                        who knows the access pattern?
   DDR4 / URAM scratch
        ▲
        │  AXI
   ┌────┴─────────────────┐
   │ MAS: TLB + cache     │   nobody -- so pay for tags            SHARED
   │ URAM, tagged         │   captures sharing BETWEEN clusters
   └────┬─────────────────┘
        │  NoC   (~0.375 word/cycle per cluster)
   ┌────┴─────────────────┐
   │ cluster L2           │   the manager does -- so no tags       PRIVATE
   │ URAM, no tags        │   captures reuse WITHIN one cluster
   ├──────────────────────┤
   │ L1  LUTRAM, 1024 b   │   wide and shallow, feeds the chain
   ├──────────────────────┤
   │ resident tile  BRAM  │   the accumulator's output
   └──────────────────────┘
```

Each level is a different **shape**, and shape is what picks the primitive:

| level | width × depth | primitive | why |
|---|---|---|---|
| MAS cache | 256 b × deep | URAM | deep and narrow; 4096×72 is exactly this |
| cluster L2 | 256 b × deep | URAM | same shape, private copy |
| cluster L1 | 928 b × `GA` / `GB` | **LUTRAM** | wide and shallow — a ~1024-bit BRAM port is 15 BRAM36 at 2% utilisation |
| resident tile | 352 b × 512 | **BRAM** | 5 BRAM36 at 98% full |

As built, `mx_cluster_mgr` instantiates two `kohaku_sdpram`s at `WIDTH = 928`
and `MEM_PRIM = "distributed"`, `GA` deep for A and `GB` deep for B —
128 and 256 in `mag_driver_tb` ([`../isa/cluster.md`](../isa/cluster.md) §4.7).
The primitive is **named**, not inferred, because `READ_LAT` is part of the
contract: the manager delays control by two stages to match `READ_LAT = 1`.

Measured in `.plan/measurements/memory-primitives.md`. The rule that falls out:
**width sets the primitive count, depth is then free** — up to 512 for BRAM,
4096 for URAM.

---

## 2. Why the CU-side level has no tags

A cache exists to guess. Inside a matmul cluster there is nothing to guess: the
`FILL` instruction carries a tensor descriptor that states exactly which bytes
are wanted, and the manager issues the memory requests itself.

```
   transparent cache            explicitly managed L2
   ------------------           ---------------------
   tag lookup on every access   none
   miss -> stall                fill is an instruction, overlapped with compute
   replacement policy           the program decides
   hit rate is emergent         residency is a scheduling decision
```

Three concrete consequences.

**Latency stops mattering.** A miss in a transparent cache stalls the consumer.
An explicit `FILL` for K-block `n+1` runs *during* the 512 cycles the sweep
spends on block `n`, so the memory latency is hidden by construction rather than
by luck. Double-buffering L1 is what makes this work, and `SYNC` is the only
ordering the program has to state.

**The bandwidth shape is wrong for a cache anyway.** The chain wants 2048
bits/cycle. That is not a tag-lookup-shaped access; it is a wide register file
read. Putting a cache there would mean 8 parallel tag comparisons per cycle to
answer a question the manager already knew.

**It matches what is already built.** L1 has no tag array, no hit/miss and no
eviction policy: `FILL` names a base address and a count, and the manager writes
what comes back into consecutive entries
([`../isa/cluster.md`](../isa/cluster.md) §3). The L2 is the same argument one
level up — it is a bigger staging buffer, not a cache.

> This is the part that changed from the original plan. §9 of the NoC spec had
> L2 as a transparent cache between the NIC and the CU, sharing a module with
> L3. That is a reasonable default for a CU whose access pattern is unknown —
> and it is the wrong choice for one that states the access pattern in the
> instruction, which is what the built `FILL` does and what a tensor descriptor
> would do more expressively.

### 2.1 L2 size is the same decision as the MAS port count

For an L2-resident `Ma × Nb` output tile, the cluster reads `(Ma+Nb)·K`
operand elements and performs `Ma·Nb·K` MACs, so:

```
   words/cycle from the NoC  =  16 (Ma + Nb) / (Ma · Nb)
```

| L2-resident tile | words/cyc | 32 clusters | URAM/cluster | MAS ports |
|---|---|---|---|---|
| 64 × 128 | 0.375 | 115 GB/s | ~6 | 12 |
| **128 × 256** | **0.188** | **57.6 GB/s** | **~10** | **6** |
| 256 × 256 | 0.125 | 38.4 GB/s | ~13 | 4 |

At K=1024, `(Ma+Nb)·K·7 bits` is the capacity; 128×256 is 2.75 Mbit ≈ 10
URAM288, or 320 across 32 clusters — 25% of the device.

**Doubling L2 halves NoC traffic**, and that is the trade worth taking: URAM is
cheap and local, NoC ports are expensive and global. Sizing MAS without fixing
L2 first is meaningless, because the demand it must serve is whatever L2 fails
to capture.

What L2 captures is reuse *across the output tiles one cluster is scheduled to
compute* — temporal, private. The MAS cache captures something else entirely.

---

## 3. Why the MAS-side level does have tags

At MAS, nobody knows what comes next — requests arrive from 32 clusters with no
declared relationship. That is exactly the situation a cache is for.

What it captures is **simultaneous sharing**. With output tiling on an 8×4
cluster grid, the same B tile is read by the 8 clusters in its column band and
the same A tile by the 4 in its row band:

```
   read by all clusters   32 x 192 words per K block  =  6,144
   distinct words                                     =  1,024
   reuse factor                                       =  6x
```

A 6× reduction in DRAM traffic, from clusters that never coordinate with each
other. No descriptor could express that, because it is a property of the
*schedule across clusters*, not of any one cluster's access pattern.

```
   line          128 B (4 flits) -- matches one L1-entry fetch exactly
   organisation  4-way set associative
   storage       URAM
   write policy  write-through
```

**Line = 128 B because that is the request size.** A cluster's `FILL` fetches
one L1 entry as a 4-flit burst; making the line match means one miss per burst
and no partial-line traffic.

**Write-through, initially.** DRAM stays authoritative, and there is no dirty
tracking or eviction path to get wrong. The cost is real — a layer writes its
whole output tensor every time, and write-back would coalesce that — but it is
entirely internal to the cache module and can change later behind the same
interface.

---

## 4. Coherence: there isn't any

No snooping, no invalidation, no directory. Three properties combine to make the
question not arise:

1. **CU-side memory is explicitly managed.** Software knows when its copy is
   stale because software put it there. A cluster does not silently hold a line
   that someone else wrote.
2. **MAS is sliced by address** ([spec.md](spec.md) §2), so a given line lives in
   exactly one slice. Two clusters reading it reach the same cache.
3. **Write-through** keeps the backing store and the line in agreement.

The one thing this does *not* give you for free: a cluster that writes results
and another that later reads them must be ordered by the program, not by the
hardware. That is a `SYNC` in the instruction stream, and it is the compiler's
job — which is the right place for it, because the compiler scheduled both.

> Worth stating plainly because it is a large simplification, and because it is
> a consequence of the explicit-management decision rather than an independent
> choice. Making the CU-side level a transparent cache would have dragged
> coherence in with it.

---

## 5. What this costs

```
   cluster L2     ~10 URAM x 32 clusters      =   320 URAM   (25%)
   MAS cache      4 slices x 64-128 URAM      =   256-512    (20-40%)
   cluster L1     ~2,048 LUT x 32             =    66 kLUT   (3.8%)
   resident tile  5 BRAM36 x 32               =   160 BRAM   (6.0%)
```

> Only the resident tile's row is measured. The L1 LUT figure is a design-time
> estimate from the 1024×96 shape above, and the built L1 is deeper than that
> (`GA = 128`, `GB = 256`); the whole-cluster measurement that would replace it
> is `mx_cluster_cu`'s utilisation in [`../compute/timing.md`](../compute/timing.md),
> which does not break L1 out separately. Do not treat this line as measured.

URAM is the resource under pressure: 1,280 available and 576–832 committed. That
is affordable, and it is exactly why cluster **L1 must not be URAM** — it is the
wrong shape for it (1024 bits × 96, which a 72-bit port serves terribly) and the
budget could not spare it anyway.

Per SLR, with 8 clusters and one MAS slice: 80 URAM for L2 plus 64–128 for the
cache, against 320 available. Comfortable.

---

## 6. Open questions

```
   L2 sizing            6 URAM/cluster covers one pass's working set. Whether
                        that captures useful cross-tile reuse depends entirely
                        on the schedule -- needs a real workload, not a guess.

   MAS cache capacity   the 6x reuse figure assumes all 32 clusters are working
                        on the same K block at the same time. Real schedules
                        skew, and a cache too small to span the skew captures
                        much less. Measure before sizing.

   L2 fill policy       FILL is explicit, but who emits the FILLs -- the host
                        compiler, or a loop in the manager walking descriptors?

   eviction             no policy is written down yet for the MAS cache. LRU on
                        4 ways is cheap; whether it beats random here is
                        unknown.

   write-back           deferred, and the deferral is deliberate: it is internal
                        to the cache and can be changed once output-tensor
                        traffic is measurable.
```

---

## 7. Relationship to what exists

The `L1`/`L2`/`L3` naming in [`../noc/spec.md`](../noc/spec.md) §9 predates the
tensor ISA and does not survive it. The mapping:

| spec §9 | now |
|---|---|
| L1 — inside the CU, BRAM | cluster L1 (LUTRAM) + resident tile (BRAM), both explicit |
| L2 — CU-side transparent cache | **cluster L2, explicitly managed, no tags** |
| L3 — MAS-side, URAM, write-through | MAS cache + TLB — unchanged in spirit |

Nothing about the wire protocol changes. §5.1/§5.2 of the NoC spec is what MAS
implements, and `tests/noc/noc_fake_mem.v` already implements it well enough for
two system benches to pass — so MAS has a reference to be correct against from
the first commit.
