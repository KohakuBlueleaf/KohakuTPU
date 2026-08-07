# Pipeline, cycles and resources

Every latency and every measured number for the compute path, in one place.
Design intent is [`matmul.md`](matmul.md) and [`tensor-isa.md`](tensor-isa.md);
what the built machine actually executes is [`../isa/`](../isa/README.md). This
is the accounting.

All synthesis is out-of-context, `xcvu13p-fhgb2104-2L-e`. The cluster and
accumulator figures come from a re-measurement against a **310 MHz target
(3.2258 ns)**; the other rows are older runs at 300 MHz (3.3333 ns) and are
marked as such. Out-of-context means utilisation is reliable and every Fmax here
is an **upper bound** — nothing is placed and the route is estimated, so it
answers "is the logic deep enough to fail?", not "will it place".

---

## 1. Resources

| module | LUT | FF | BRAM36 | DSP | Fmax |
|---|---|---|---|---|---|
| `mx_mac` (one DSP48E2) | 0 | 0 | 0 | 1 | — |
| `mx_tcu` (4×8×4) ‡ | 336 | 790 | 0 | 64 | 1072.6 |
| `mx_cluster_core` (4 TCU) ‡ | ~2,186 | ~3,549 | 0 | 256 | — |
| `mx_acu_fp` (FP22 accumulator) † | — | — | 5 | 16 | **327.7** |
| `mx_cluster_mgr` + node | — | — | 0 | — | — |
| **`mx_cluster_cu`** (2-port cluster) | **17,521** | 17,612 | **5** | **272** | **325.6** |
| `mx_matmul_cu` (1-port baseline) ‡ | 12,973 | 11,486 | 5 | 256 | 306.4 |
| `noc_orchestrator` ‡ | 2,563 | 2,465 | 0 | 0 | 570.0 |

`mx_cluster_cu` closes with **WNS +0.155 ns** against the 3.2258 ns target, and
0 URAM.

> † The ACU's **Fmax and DSP count** come from the standalone re-measurement.
> Its **LUT and FF do not** — they were not re-measured, and the earlier figures
> are no longer current because the magnitude now goes through the DSP rather
> than the fabric ([`accumulator.md`](accumulator.md) §4.4). Its 16 DSPs are one
> per lane, each mapped `(D+A)*B`, which is exactly the 272 − 256 in the cluster
> row. For the record, the earlier run measured 9,945 LUT, 6,232 FF and 0 DSP at
> 349.4 MHz. The cluster row is a full re-measurement and contains the ACU.
>
> ‡ Older run, 300 MHz target, and not re-measured since. `mx_matmul_cu` is the
> superseded single-port design kept as a baseline; it is not on the current
> path. `mx_cluster_core` was never a run at all — it is the cluster minus its
> parts (§5), so it was inferred from the *earlier* cluster figure and is stale
> by the same amount the cluster moved.

Three things worth reading off this:

**Every `mx_mac` is 0 LUT, 0 FF, 1 DSP.** The multiply *and* the entire K=32
reduction happen inside the DSPs — the cascade for K=8, the `W` port across CUs.

**The accumulator is still the critical path** of the whole cluster, and after
all the timing work it is the block everything closes on. The cluster closes at
325.6 MHz and the ACU measures 327.7 MHz standing alone — 2.1 MHz apart, so
everything else in the cluster is effectively free of the frequency question.

**The resident tile is 5 BRAM36 at any depth up to 512.** A 352-bit port needs
`ceil(352/72) = 5` primitives; depth is then free. It was 22,845 LUT and missed
timing when inferred as LUTRAM.

### Scaling

| | per cluster | ×32 | ×45 | of device (×45) |
|---|---|---|---|---|
| LUT | 17,521 | 560,672 | 788,445 | **45.6%** |
| FF | 17,612 | 563,584 | 792,540 | 22.9% |
| BRAM36 | 5 | 160 | 225 | 8.4% |
| DSP | 272 | 8,704 | 12,240 | **99.6%** |
| NoC ports | 2 | 64 | 90 | — |

**The right-hand column is 45 clusters, not 48.** The cluster now measures 272
DSP rather than 256 — the cascade's 256 plus 16 more, because the ACU's scale
multiply maps into DSP48E2s instead of fabric — and 12,288 / 272 = 45. So the
DSP-bound configuration is **45 clusters, ~13.8 TFLOPS of AMP FP16-MXFP7** at
300 MHz, with LUTs at 46% and BRAM at 8%. The 32-cluster configuration, which is
what the four-partition floorplan actually builds, uses 71% of the DSPs.

**Everything right of the per-cluster column is that column multiplied out.**
One cluster is what was synthesised; no 32- or 45-cluster build exists, so these
are budgets, not results — and they say nothing about whether 45 of them place
and route on one die.

---

## 2. Pipeline stages

### 2.1 Accumulator — 6 stages

```
   1    extract the two packed fields per chain
   2a   leading-one search and shift            <- tile address presented here
   2b   round and assemble -> accumulator float
   3    read the tile, compare exponents, align <- tile data valid here
   4    add, leading-one search, shift
   5    round, assemble, write back             <- tile write
   6    (EMIT only) convert to FP16
```

`READ_LAT=2` on the tile, so the address leads the data by two cycles: presented
at stage 2a, valid at stage 3.

**Contract:** consecutive commands to the same tile address must be ≥ 5 cycles
apart. There is one bank, so a pipelined read-modify-write cannot serve
back-to-back hits. Checked in simulation (`REUSE_MIN`), not assumed.

### 2.2 Cluster manager — 3 stages to the cascade

```
   S0   counters produce (g, h, kb); L1 addresses presented
   S1   (L1 READ_LAT=1 -- data not yet valid)
   S2   L1 data valid; drive the cascade, push the ACU command FIFO
```

Two cycles of control delay, not one: the counters assign at T, the RAM sees the
address at T+1, data is valid at T+2. Consuming at T+1 shifts every result by
one sub-tile — silently.

### 2.3 The cascade — ~19 cycles, and nobody depends on the number

`mx_cluster_core` is ~19 cycles deep, a function of `NTCU` and the operand-skew
SRLs. **No module hardcodes it.** The manager pushes one `{op, addr, scales}`
per issue into a FIFO and pops one per `part_valid`; order is preserved by
construction, so alignment survives any change to the chain.

The same discipline applies to `emit_valid`: the CU waits on the flag, never on
a cycle count. It previously counted 10, which became too short when the
accumulator deepened, and the system wrote a zero result while every unit test
passed.

---

## 3. Throughput

### 3.1 Steady state

One 4×32×4 tile per cycle, sustained: **512 MACs/cycle = 1,024 FLOP/cycle**.

A `GEMM` over `Gm × Gn` sub-tiles and `NK` K-blocks issues one tile per cycle:

```
   cycles  =  Gm * Gn * NK  +  pipeline drain (~25)
```

For the balanced 16×32 tiling that is 512 cycles per K block, and the drain is
under 5% of it.

### 3.2 Operand bandwidth

```
   words/cycle from the NoC  =  4 (Gm + Gn) / (Gm · Gn)

   8 x 8    (32x32 out)   1.000   one port exactly saturated
   16 x 32  (64x128 out)  0.375   the balanced point
   32 x 32  (128x128 out) 0.250
```

The result port carries one 256-bit word per emitted sub-tile, `Gm·Gn` per full
K sweep — `1/NK` words per cycle, so it is never the constraint for K > 32.

### 3.3 What a program costs

`C[64,128] = A[64,K]·B[K,128]`, the balanced shape, per K block of 32:

```
   FILL A    16 entries x 4 words = 64 memory words
   FILL B    32 entries x 4 words = 128 words
   GEMM      512 cycles
   DRAIN     512 sub-tiles, once per full K sweep
```

192 operand words against 512 compute cycles is the 0.375 word/cycle figure
above — the port is 37.5% occupied, which is the headroom the vector and
general units need.

---

## 4. Measured end to end

| bench | shape | result |
|---|---|---|
| `mx_cluster_node_tb` | 32×32×32, one GEMM | 2,112 checks, 0.50 ULP worst |
| `mx_system_tb` | 4×256×4, 1×5 NoC | 35 checks, 0.41 ULP |
| `mx_system32_tb` | 32×32×32, 1×5 NoC | 2,051 checks, 0.50 ULP |
| `mag_system_tb` | 16×32×16, MAG + 2 clusters | 257 checks, 0.49 ULP |
| `mag_driver_tb` | up to 256×256×256, tiled by the driver | see §4.1 |

All identical under `MODEL=0` (real DSP48E2) and `MODEL=1` (behavioural).

The two 1×5 benches drive `mx_matmul_cu`, the deliberately retained single-port
baseline of §1 — they are not on the cluster path, and they pass.

> **`mx_mesh2x2_tb` was deleted, not fixed.** It had fallen a generation behind
> on two interfaces at once: it packed the pre-widening instruction layout, and
> the memory stub it drove wrote a constant where the response word index
> belongs, so no L1 entry was ever committed and the CU sat in `S_FILL` for
> ever. The multi-cluster coverage it existed for is `mag_system_tb` and
> `mag_driver_tb` at `NCL=2` — green, and against the real MAG rather than a
> stub, which is the stronger test.

### 4.1 Where the cycles actually go

`mag_driver_tb` buckets every cycle by what each CU was doing, because an event
counter says a `GEMM` happened but not whether the machine spent its time
computing or waiting for operands — which is the whole question for a dataflow
design. Shares are of CU-occupancy across both clusters, so they sum to ~100%.

| shape | run cycles | fill | gemm | drain | idle | MAC/cyc | GFLOP/s |
|---|---|---|---|---|---|---|---|
| 64×64×128 | 8,213 | 56.7% | 17.3% | 12.1% | 13.8% | 63.8 | 38.3 |
| 128×128×128 | 32,361 | 61.3% | 17.1% | — | — | 64.8 | 38.9 |
| 256×256×256 | 239,786 | 71.9% | 13.4% | 6.7% | 7.9% | 70.0 | 42.0 |

> **Superseded — this is the baseline, not the machine.** The fill-bound
> diagnosis below was acted on, and the 256-cube now runs in **18,701 cycles at
> 538.3 GFLOP/s, 87.6% of peak**, with eight clusters at 1,856 GFLOP/s. Current
> figures for every shape and cluster count: [`../perf.md`](../perf.md) §0.

Two clusters peak at 1,024 MAC/cycle = 614 GFLOP/s at 300 MHz, so the machine
reached 6–7% of its own datapath. `fill` dominating is not a bug and not a
scheduling problem: the resident tile is small enough that a pass performs only
four tile-ops per L1 entry it loads. `idle` is the CU waiting on the dispatcher,
which is neither compute nor bandwidth but is real time; it shrinks as the
problem grows, because a bigger problem amortises the per-round `GO`.

The read figure that accompanies these (2.62 GB/s on the 256 case) is **demand**
measured against a bench RAM with no latency. Real memory would not reduce the
traffic, only lengthen the wait for it, so the fill share on hardware is a
floor.

---

## 5. Caveats

**Out-of-context timing is an upper bound.** `mx_cluster_cu` has 0.155 ns of
slack against the 3.2258 ns (310 MHz) target; nothing is placed and the route is
estimated, so a device past ~70% full will erode that, and none of these numbers
have been through place-and-route on a populated die.

**`mx_cluster_core` is not measured standalone** — the figures above are
inferred from the cluster minus its parts, so treat them as approximate.

**The quantiser is not in these numbers.** It is built —
`src/kohakumas/mx_quant.v` converts FP16 to int7 + E5M3 on the way out of MAG —
but it lives on the MAG side of the NoC, so none of the cluster figures above
include it. Measured separately, on the same 310 MHz-target run: **400.6 MHz,
4,267 LUT, 32 DSP, no BRAM and no URAM.** See
[`../isa/memory.md`](../isa/memory.md) §6.

**§3 is peak, not achieved.** Everything above answers "what can the datapath
sustain if operands are there". §4.1's table is the baseline, where they were
usually not — 64–70 MAC/cycle against a 1,024 MAC/cycle two-cluster peak,
fill-bound. It has since reached 897 MAC/cycle (87.6%);
[`../perf.md`](../perf.md) §0 is the current record and
[`../optimization.md`](../optimization.md) §I is what each change was worth.
