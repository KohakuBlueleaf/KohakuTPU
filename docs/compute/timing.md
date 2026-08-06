# Pipeline, cycles and resources

Every latency and every measured number for the compute path, in one place.
Design intent is [`tensor-isa.md`](tensor-isa.md) and [`matmul.md`](matmul.md);
this is the accounting.

All synthesis is out-of-context, `xcvu13p-fhgb2104-2L-e`, 300 MHz target
(3.3333 ns). Out-of-context means utilisation is reliable and timing is
optimistic — it answers "is the logic deep enough to fail?", not "will it place".

---

## 1. Resources

| module | LUT | FF | BRAM36 | DSP | Fmax |
|---|---|---|---|---|---|
| `mx_mac` (one DSP48E2) | 0 | 0 | 0 | 1 | — |
| `mx_tcu` (4×8×4) | 336 | 790 | 0 | 64 | 1072.6 |
| `mx_cluster_core` (4 TCU) | ~2,186 | ~3,549 | 0 | 256 | — |
| `mx_acu_fp` (FP22, DEPTH=16) | 9,945 | 6,232 | 5 | 0 | **349.4** |
| `mx_cluster_mgr` + node | — | — | 0 | — | — |
| **`mx_cluster_cu`** (2-port cluster) | **13,921** | 13,042 | **5** | **256** | **322.4** |
| `mx_matmul_cu` (1-port baseline) | 12,973 | 11,486 | 5 | 256 | 306.4 |
| `noc_orchestrator` | 2,563 | 2,465 | 0 | 0 | 570.0 |

Three things worth reading off this:

**Every `mx_mac` is 0 LUT, 0 FF, 1 DSP.** The multiply *and* the entire K=32
reduction happen inside the DSPs — the cascade for K=8, the `W` port across CUs.

**The accumulator is still the critical path** of the whole cluster, and after
all the timing work it is the block everything closes on.

**The resident tile is 5 BRAM36 at any depth up to 512.** A 352-bit port needs
`ceil(352/72) = 5` primitives; depth is then free. It was 22,845 LUT and missed
timing when inferred as LUTRAM.

### Scaling

| | per cluster | ×32 | ×48 | of device (×48) |
|---|---|---|---|---|
| LUT | 13,921 | 445,472 | 668,208 | **38.7%** |
| FF | 13,042 | 417,344 | 626,016 | 18.1% |
| BRAM36 | 5 | 160 | 240 | 8.9% |
| DSP | 256 | 8,192 | 12,288 | **100%** |
| NoC ports | 2 | 64 | 96 | — |

DSP-bound at 48 clusters: **~14.7 TFLOPS of AMP FP16-MXFP7** at 300 MHz, with
LUTs at 39% and BRAM at 9%.

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
| `mx_mesh2x2_tb` | 32×128×64, 2×2 NoC, 2 clusters | 4,096 checks, 0.18 ULP mean |

All identical under `MODEL=0` (real DSP48E2) and `MODEL=1` (behavioural).

---

## 5. Caveats

**Out-of-context timing is optimistic.** `mx_cluster_cu` has 0.232 ns of slack;
a device past ~70% full will erode that, and none of these numbers have been
through place-and-route on a populated die.

**`mx_cluster_core` is not measured standalone** — the figures above are
inferred from the cluster minus its parts, so treat them as approximate.

**The quantiser is not built.** Operands are preloaded as MXFP7; converting FP16
on the fill path will add logic that is not counted here.
