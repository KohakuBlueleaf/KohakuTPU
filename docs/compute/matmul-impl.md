# Matmul unit — implementation status

What exists, what it was tested against, and what it costs. Design intent is in
[`matmul.md`](matmul.md); the primitive-level reasoning is in
[`matmul-circuit.md`](matmul-circuit.md).

Status: **one cluster works and is verified.** 4 tensor CUs + 1 accumulator CU,
4x32x4 per cycle, exact integer, against both a behavioural model and the real
DSP48E2.

---

## 1. Modules

`src/kohakutpu/matmul/`

| File | Role |
|---|---|
| `mx_mac.v` | one DSP48E2, two int7 MACs sharing an activation |
| `mx_tcu.v` | 4x8x4 tensor CU — 8 chains of 8, 128 MACs/cycle |
| `mx_cluster_core.v` | 4 TCU chained — 512 MACs/cycle, no accumulator |
| `mx_acu.v` | exact fixed-point accumulation — the bring-up instrument |
| `mx_cluster.v` | core + exact ACU, for bit-for-bit datapath checking |
| `mx_fpacc.v` | accumulator float S1E7M\<MW\>: normalise, add, convert to FP16 |
| `mx_acu_fp.v` | the real ACU — FP accumulation, resident tile, peer transfer |
| `mx_matmul_cu.v` | the whole thing as a NoC endpoint: framework, sequencer, datapath |

The core is shared between the two accumulator pairings, so the datapath that
`mx_cluster_tb` checks bit-for-bit is the same one that ships.

```
   K 0..7        K 8..15       K 16..23      K 24..31
   +-------+     +-------+     +-------+     +-------+
   | TCU 0 |--C->| TCU 1 |--C->| TCU 2 |--C->| TCU 3 |--> ACU --> tile
   +-------+     +-------+     +-------+     +-------+
    64 DSP        64 DSP        64 DSP        64 DSP
```

Latency, operands to `acc_valid`: **19 cycles**. Throughput: one 4x32x4 per
cycle, sustained.

---

## 2. Verification

`tests/run_matmul_sim.ps1` — two benches, each run against **both** models.

| Bench | Checks | MODEL=1 | MODEL=0 |
|---|---|---|---|
| `mx_tcu_tb` — one TCU, raw packed partials | 1,520 | pass | pass |
| `mx_cluster_tb` — full cluster, extracted and scaled | 4,176 | pass | pass |
| `mx_fp24_tb` — accumulator float primitives | 13,208 | pass | n/a |
| `mx_acu_fp_tb` — ACU ops, resident tile, peer | 384 | pass | n/a |
| `mx_system_tb` — 1×5 NoC, `C[4,4]=A[4,256]·B[256,4]` | 35 | pass | pass |
| `mx_system32_tb` — 1×5 NoC, `C[32,32]=A·B` | 2,051 | pass | pass |

The system benches are the important ones — see [`../system.md`](../system.md).
It runs `C[4,4] = A[4,256] × B[256,4]` through the orchestrator, the mesh, the
memory node and back, and lands within **0.4 of an FP16 ULP** of an FP64 model.

Everything is exact integer arithmetic checked bit-for-bit against a model
computed in the bench. No tolerances.

Coverage, and what each part is for:

- **packing worst case** `w_hi = w_lo = act = -64` — the case that rules out
  S = 20
- **full-scale sums** every element extreme, so a K=32 sum reaches +/-131,072
  and uses all five guard bits
- **borrow correction** lower field forced negative on all eight chains, the
  only thing `+P[18]` fixes
- **streaming** a new tile every cycle, which is the only way the per-stage skew
  and the cross-TCU W path are actually exercised
- **scales** non-uniform E8M0 per row and column, accumulated across blocks

### 2.1 Why two models earned their keep

`mx_mac` builds either a real `DSP48E2` or a behavioural equivalent. The split
makes a failure attributable — and it immediately paid for itself.

**`BREG` had to be 2, not 1.** With `AMULTSEL="AD"` the A/D path reaches the
multiplier through `AREG` *and* `ADREG` — two register stages — while `BREG=1`
gives B only one. B therefore arrives a cycle early and multiplies against the
wrong operand.

This is invisible with stable operands: every stage happens to be looking at the
same tile, so the misalignment cancels. It only appears when a new tile enters
every cycle. MODEL=1 passed and MODEL=0 failed **only** in the streaming
section, which pointed straight at the DSP configuration rather than the
arithmetic.

`glbl` also holds GSR asserted for the first 100 ns, so unisim registers ignore
everything before that regardless of the design's own reset. The benches wait
past it; without that the first tile silently produces nothing.

---

## 3. Measured resources

Out-of-context synthesis, `xcvu13p-fhgb2104-2L-e`, 300 MHz target.

| | LUT | FF | BRAM | DSP | Fmax |
|---|---|---|---|---|---|
| `mx_tcu` alone | 336 | 790 | 0 | 64 | 1072.6 MHz |
| `mx_cluster` (core + exact ACU) | 4,751 | 4,789 | 0 | 256 | 353.6 MHz |
| `mx_acu_fp` (FP22 accumulator) | 9,945 | 6,232 | 5 | 0 | 349.4 MHz |
| **`mx_cluster_cu`** (2-port cluster) | **13,921** | 13,042 | **5** | **256** | **322.4 MHz** |
| `mx_matmul_cu` (1-port baseline) | 12,973 | 11,486 | 5 | 256 | 306.4 MHz |

**`mx_cluster_cu` is the deliverable** — the architecture of
[`tensor-isa.md`](tensor-isa.md): manager with L1, the four-TCU cascade, the
accumulator, and two NoC ports. It meets 300 MHz with 0.232 ns of slack at
`ACC_MW=14`, sustaining 512 MXFP7 MACs per cycle, with a **256-deep resident
tile on five BRAM36**.

`mx_matmul_cu` is the earlier single-port design, kept as a measured baseline;
it cannot be fed at rate (§1) but it is what the two 1×5 system benches drive.

> The resident tile costs 5 BRAM36 whether `DEPTH` is 16 or 512: a 352-bit port
> needs `ceil(352/72) = 5` primitives, and depth is then free up to 512. Before
> the tile was made explicit it was inferred LUTRAM, which cost **22,845 LUT and
> missed timing at 287.3 MHz** — see [`accumulator.md`](accumulator.md) §4.3.

### 3.1 The operand buffer, and 32,000 LUTs

The CU measured **45,956 LUT** until one line changed. The flit counter was
inside a part-select on the write:

```verilog
a_buf[(i*32 + {fl,3'd0} + k)*7 +: 7] <= recv_flit[...];   // 45,956 LUT
```

A variable part-select tells synthesis that any of the 896 bits might come from
any position, so it builds a barrel mux across the entire buffer — twice, for A
and B. In fact each bit is written by exactly one value of `fl`. Unrolling the
loop over `fl` so the index is constant in each branch leaves every bit with one
fixed source and a plain enable:

```
   45,956 LUT  ->  13,664 LUT      -70%, 32,292 LUTs for one loop rewrite
   273.7 MHz   ->  292.9 MHz
```

This is the same mistake as the accumulator's search loops in a different
costume: **writing an index as a variable when it is structurally constant.**
Worth checking for anywhere a counter appears inside `[...+: N]` on the left of
a non-blocking assignment.

### 3.2 Cluster breakdown

| Component | LUT | Logic | SRL | FF | DSP |
|---|---|---|---|---|---|
| `mx_mac` x256 | **0** | 0 | 0 | **0** | **256** |
| TCU 0 | 336 | 112 | 224 | 784 | 64 |
| TCU 1 | 448 | 168 | 280 | 728 | 64 |
| TCU 2/3 | 476 each | 196 | 280 | 728 | 64 |
| operand delay (top) | 450 | 56 | 394 | 581 | 0 |
| **ACU** | **2,565** | 2,565 | 0 | 1,240 | 0 |

Three things worth reading off this:

**Every `mx_mac` is 0 LUT, 0 FF, 1 DSP.** The multiply *and* the entire K=32
accumulation happen inside the DSPs — the cascade for K=8, the W port across
CUs. That was the central claim of the design and it holds exactly.

**A TCU's LUTs are almost all operand skew.** 224 of 336 are SRLs; the routing
logic is ~112. Skew is the price of a cascade, and SRL32 makes it one LUT per
bit at any depth.

**The ACU is 54% of the cluster** and holds the critical path. It is the only
part that is real fabric arithmetic. That is still true of the FP accumulator in
the shipping CU — after all the timing work its path is the one the whole CU
closes on.

### 3.3 The shift clamp (exact ACU)

`shamt = sa + sb - anchor` is nominally 8 bits. Synthesised as such it builds a
0..255 barrel shifter on every one of 16 lanes:

| | LUT | Fmax |
|---|---|---|
| 8-bit shift | 6,109 | 396.1 MHz |
| **5-bit shift (`SH_MAX = 31`)** | **4,751** | 353.6 MHz |

**1,358 LUT, 22% of the cluster, for range that cannot be used** — a shift past
`ACCW` pushes the value out of the accumulator regardless. Correctness is
unchanged; both benches still pass.

---

## 4. Full-machine estimate

48 CUs exhausts the DSPs exactly. Using the measured **whole CU**, not the bare
cluster — this includes the NoC framework, the sequencer and the operand
buffers, so it is what the device actually has to fit.

| | per cluster | x32 | x48 | of device (x48) |
|---|---|---|---|---|
| LUT | 13,921 | 445,472 | 668,208 | **38.7%** |
| FF | 13,042 | 417,344 | 626,016 | 18.1% |
| BRAM36 | 5 | 160 | 240 | 8.9% |
| DSP | 256 | 8,192 | 12,288 | **100%** |
| URAM | 0 | 0 | 0 | 0% |
| NoC ports | 2 | 64 | 96 | — |
| MACs/cycle | 512 | 16,384 | 24,576 | — |

At 300 MHz, 48 CUs is **~14.7 TFLOPS of AMP FP16-MXFP7**, DSP-bound with LUTs
at 36%. The 32-CU configuration is ~9.8 TFLOPS on 24% of the LUTs, leaving
4,096 DSPs for the FP16 vector path.

> FLOPS, not IOPS. MXFP7 is a floating-point format — an E8M0 exponent shared
> across a block of 32 and a 7-bit significand — and the operands and results in
> memory are FP16. The integer datapath inside the DSP is how an MXFP7 multiply
> is implemented once the shared exponent is factored out. See
> [`matmul.md`](matmul.md) §3.0. One MAC counts as 2 FLOPs.

> The earlier version of this table used `mx_cluster` (4,751 LUT) and reported
> 13.2%. That undercounted, because it excluded everything between the cluster
> and the mesh. The figures above are the full endpoint.

For scale, against the existing FP8 core (`costs.md`: 12,731 LUT / 64 DSP for
128 MACs):

```
   per 128 MACs      old   12,731 LUT + 64 DSP
                     new    1,188 LUT + 64 DSP      ~10.7x fewer LUTs
```

Same DSP count, same MAC count, different numerics. Essentially all of the
difference is accumulation leaving the fabric.

> Out-of-context, so utilisation is reliable and timing is optimistic. The
> whole CU has 0.070 ns of slack against 3.333 ns, and the path is inside the
> ACU's align stage. That is a thin margin — a device past ~70% full will erode
> it, and this needs re-checking after place-and-route on a populated die.

---

## 5. Status

Built and verified:

```
   MAC packing, cascade, cross-CU W path      exact, both models
   accumulator float primitives               13,208 checks
   ACU: LOAD/ADD/ADD_PEER/SEND/EMIT/FWD       384 checks
   resident output tile (16 sub-tiles)        verified independently addressed
   peer transfer                              SEND -> ADD_PEER round trip
   NoC attachment                             mx_matmul_cu on noc_cu_base
   end-to-end matmul on a 1x5 NoC             35 checks, 0.4 FP16 ULP
```

```
   ACU at 300 MHz        312.3 MHz at MW=14, 302.3 at MW=16 -- meets timing
                         See accumulator.md s4 for the twelve measured steps
   whole CU at 300 MHz   306.4 MHz at ACC_MW=14, 12,973 LUT, 256 DSP
```

Not done:

```
   quantiser             lives in MAS; nothing here converts FP16 to int7
   peer link in a system only exercised in the ACU bench, not across clusters
   MEM_WR_ACK            the CU retires on send rather than waiting for the ack
   chain bypass          no mux to degrade a cluster to 4 independent TCUs
   large resident tile   16 sub-tiles is a register file; a big tile wants BRAM
```

The ACU is still the majority of the LUTs and still owns the critical path, but
it now **meets** the 300 MHz target at both widths.
[`accumulator.md`](accumulator.md) measures the precision and the cost across
accumulator widths, identifies MW=14 (FP22) as the better operating point than
FP24, and records the twelve-step timing history — including the mistake that
made eight of those steps nearly worthless: three combinational blocks written
as loops carrying a value between iterations, which synthesise to ~25-level
serial chains inside a single pipeline stage. Fixing the loops and the operand
muxes was worth +68 MHz; the six pipeline splits before it were worth +150 MHz
between them.
