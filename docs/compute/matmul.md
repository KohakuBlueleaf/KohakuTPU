# Matmul unit — logic design

Logic level only: structure, dataflow, formats and operations. No RTL, no
resource numbers, no timing. Implementation choices are deliberately deferred.

Related: [`noc/spec.md`](../noc/spec.md) for the packet layer,
[`arithmetic.md`](arithmetic.md) for the FP primitives,
[`costs.md`](costs.md) for measured costs of the existing units.

---

## 1. The organizing principle

**Systolic in the small, NoC in the large.**

A systolic array is very good at one thing: making accumulation free. Partial
sums move through dedicated links and are added where they land, so there is no
adder tree and no accumulator storage in the hot path. It is bad at everything
else — rigid shape, long fill and drain, poor utilisation on small or irregular
matrices, and it scales all-or-nothing.

A NoC is the opposite: flexible composition, independent nodes, graceful
scaling, at the cost of packet overhead per hop.

This design takes the accumulation mechanism from the first and the composition
model from the second, and puts the boundary **where a dedicated wire stops
being cheaper than a packet**. That boundary turns out to sit at K = 32.

```
   systolic  <--------------------+--------------------> NoC
                                  |
   free accumulation,             |    flexible composition,
   fixed shape,                   |    independent nodes,
   no packets                     |    packet cost per hop
                                  |
                              K = 32
```

Everything below follows from choosing that one point.

---

## 2. The reduction hierarchy

Four levels. Each uses the cheapest mechanism that exists at its scale.

```
 level   span        data      reduction mechanism           what it costs
 -----   ---------   -------   ---------------------------   ------------------
  L0     K = 8       int       DSP PCOUT -> PCIN cascade     dedicated silicon
  L1     K = 32      int       CU-to-CU wire inside cluster  wires + registers
  L2     K = 32n     FP        accumulator CU, one adder     one add per 32 MAC
  L3     unbounded   FP        accumulator <-> accumulator   one packet per tile
```

L0 and L1 are **exact integer** — no alignment, no normalisation, no rounding.
That is the whole point of the design and §3 explains why it is possible.

L2 is where floating point first appears. It is reached once per 32 MACs, so a
comparatively expensive adder is amortised into irrelevance.

L3 is how large problems compose, and it never touches the inner loop.

---

## 3. Formats, and why L0/L1 are exact

### 3.0 What to call this: AMP FP16-MXFP7, and why the unit is FLOPS

The element format is **MXFP7** — a microscaling format in the OCP style: a
scale shared by a block of 32, and a 7-bit signed element. The pair is a
*floating-point* value:

```
   value(i,k)  =  scaleA[i]  x  a_int[i][k]

   scaleA[i]   =  2^(E - 20) x (1 + M/8)      field = { E[4:0], M[2:0] }
                  \__ E5M3, 8 bits, the same width an E8M0 field would be __/
```

**The scale is E5M3, not E8M0**, and that is a deliberate departure from OCP.
A power-of-two scale can only land the block peak somewhere in `[32,64)` of the
int7 range, so between zero and a full bit of the significand goes unused, and
which it is depends on where the peak happens to fall inside its binade. Three
mantissa bits put the peak in `[56,63]` every time. Measured per element on
correlated operands, relative error p50 `0.54% -> 0.38%` and p99 `48% -> 23%`.

E5 because the output is FP16: FP16's normal range spans 30 binades and E5
covers 31 — it just fits, and E4 (16) does not. The three extra exponent bits
an E8M0 field spends buy range this datapath cannot express anyway.

The cost is one multiply at each end. `mx_quant.v` divides by the scale using
an eight-entry reciprocal table instead of shifting; `mx_acu_fp.v` multiplies
the integer partial by `m8a*m8b` and takes the `/64` off the exponent, which
keeps it exact. The field stays 8 bits, so nothing about the flit format, the
NoC or L1 changes — only the interpretation.

The machine as a whole is **AMP (automatic mixed precision) FP16-MXFP7**:

```
   operands in memory        FP16          software-visible
   multiply                  MXFP7         hardware-quantised on the way in
   accumulate                FP22  S1E7M14 one add per 32 multiplies
   result out                FP16          software-visible again
```

So the throughput unit is **FLOPS, not IOPS**. The integer datapath inside the
DSP is an implementation detail of an MXFP7 multiply — the exponent is simply
factored out of the block and applied once, which is exactly what makes L0/L1
exact. Software never sees an integer, and the numbers it puts in and gets out
are floats.

Throughout these documents "int7" refers specifically to the 7-bit **significand
field** as it appears inside the DSP packing, never to the format software sees.

> One MAC is counted as 2 FLOPs, a multiply and an add, which is the usual
> convention.

### 3.1 The quantisation block is K = 32

Microscaling shares a scale **along the reduction dimension only**. For
`C[M,N] = A[M,K] · B[K,N]`:

```
   A block = 1 row    x 32 K   ->  one E5M3 scale  sA[i]
   B block = 32 K     x 1 col  ->  one E5M3 scale  sB[j]
```

Output element `(i,j)` accumulated over one K = 32 block is

```
   C[i][j] = ( sum over k of  a_int[i][k] * b_int[k][j] )  *  2^(sA[i] + sB[j])
             \_______________ exact integer _____________/    \___ constant ___/
```

The scale factor is **constant across the entire block**, so every product
entering that accumulator has the same scale. No alignment is needed, and the
sum is exact. This is the property the cluster is built around, and it is why
the block size and the cluster's K span are the same number.

> Choosing a smaller block (say K = 8, one per tensor CU) would force a rescale
> between every CU in the chain and collapse L1 into floating point. Block size
> and cascade depth are not independent parameters — they are one parameter.

### 3.2 Operand payload

A tensor CU consumes a K = 8 slice per cycle. One slice of a 4-row operand is
32 elements; the block's 4 scales ride along with it.

```
  256-bit operand payload

   255                                                  32  31           0
  +-------------------------------------------------------+--------------+
  |                32 x int7   (224 bit)                   | 4 x E5M3 (32)|
  +-------------------------------------------------------+--------------+
       element (i,k)  i = 0..3, k = 0..7                     scale per row i
                                                             (shared by all 4
                                                              K-slices of the
                                                              K=32 block)
```

int7 is the width that fills the payload exactly. The scales are identical
across the four K-slices of a block; repeating them costs 12.5% of the payload
and makes every flit self-contained. The same 256 bits reads as 16 x FP16 when
the buffer is holding float data.

### 3.3 Format at each stage

```
   DRAM / NoC        FP16 / FP32 / int8        normal dtypes, software-visible
        |
        |  MAS quantiser (max-tree -> E5M3, shift+round -> int7)
        v
   L1 (tensor CU)    int7 + E5M3              dense, feeds MAC array at rate
        |
        |  L0 + L1 : exact integer accumulation
        v
   cluster output    int (19 bit) + scale     exact result of one K=32 block
        |
        |  normalise once
        v
   accumulator       FP22  S1E7M14            one add per 32 MACs
        |
        v
   NoC / DRAM        FP16                     software-visible again
```

**int7 never appears in memory and never appears to software.** It exists only
between MAS and the MAC array.

---

## 4. Tensor CU — 4x8x4

The atom. Consumes two 256-bit operands per cycle, produces 16 integer partials,
has **no output port to the NoC** — its result goes down the cluster chain.

```
   L1-A  256 bit/cycle                  L1-B  256 bit/cycle
   +----------------------+             +----------------------+
   | 32 x int7  A[0:4,k]  |             | 32 x int7  B[k,0:4]  |
   | 4  x E5M3  sA[0:4]   |             | 4  x E5M3  sB[0:4]   |
   +----------+-----------+             +-----------+----------+
              |                                     |
              +------------------+------------------+
                                 |
                    +------------v-------------+
                    |   16 dot-product chains  |
                    |                          |
                    |   out(i,j) = sum over    |
                    |     k=0..7 of A[i,k]B[k,j]
                    |                          |
                    |   L0: each chain is a    |
                    |   DSP cascade, K=8 deep  |
                    +------------+-------------+
                                 |
                    16 x int  +  carry-in from previous CU
                                 |
                                 v
                          to next CU in chain
```

Per cycle: 4 x 8 x 4 = **128 MACs**.

Because all four CUs in a cluster share one K = 32 block, the chain input and
chain output are plain integers — a CU adds its 16 partials to the 16 arriving
from upstream and passes them on. No scale travels with them.

---

## 5. Cluster — 4 tensor CUs + 1 accumulator CU

```
        K 0..7          K 8..15         K 16..23        K 24..31
       A0     B0       A1     B1       A2     B2       A3     B3
        |     |         |     |         |     |         |     |
        v     v         v     v         v     v         v     v
     +---------+     +---------+     +---------+     +---------+
     |  TCU 0  |====>|  TCU 1  |====>|  TCU 2  |====>|  TCU 3  |
     |  4x8x4  |     |  4x8x4  |     |  4x8x4  |     |  4x8x4  |
     +---------+     +---------+     +---------+     +---------+
                  16 x int, exact, no rescale
                  (one shared scale pair for the whole chain)
                                                          ||
                                                          || 16 x int19
                                                          || + sA[i]+sB[j]
                                                          vv
                                                   +---------------+
                                    NoC  <-------->|  ACCUM CU     |
                                    peer / result  |  FP24 tile    |
                                                   +---------------+
```

Per cycle the cluster computes **4 x 32 x 4**. K is swept in multiples of 32;
that is the only constraint the hardware imposes. M and N are multiples of 4.

```
   supported shape:   M = 4a ,  N = 4b ,  K = 32c
```

A K = 512 sweep is 16 cycles — an example, not a requirement.

The chain is only 4 deep, so fill and drain are 4 cycles rather than the
hundreds a monolithic systolic array would need. That is what keeps the cluster
usable on small matrices.

---

## 6. Accumulator CU

Where floating point starts, where the output tile lives, and the only part of
the cluster that talks to the NoC.

```
      from cluster chain                     from NoC
      16 x int19 + scale                     peer partial (FP)
              |                                      |
      +-------v--------+                             |
      |   normalise    |   int + E5M3 -> FP24        |
      |   (once per    |                             |
      |    K=32 block) |                             |
      +-------+--------+                             |
              |                                      |
              +------------------+-------------------+
                                 |
                        +--------v---------+
                        |   FP24 add       |  16 lanes
                        +--------+---------+
                                 |
                    +------------v-------------+
                    |   ACCUMULATOR BUFFER     |   the output tile,
                    |   M_tile x N_tile, FP24  |   held resident
                    +------------+-------------+
                                 |
                        +--------v---------+
                        |  round -> FP16   |
                        +--------+---------+
                                 |
                                NoC
```

**The buffer is working storage, not a staging pipe.** Holding a large output
tile resident is what creates operand reuse — see §8.2, it is the single
decision that determines how many NoC ports a cluster needs.

### 6.1 Operations

```
   ACC_LOAD      acc[t]  = f24(chain)        first K block of a sweep
   ACC_ADD       acc[t] += f24(chain)        subsequent K blocks
   ACC_ADD_PEER  acc[t] += peer_in           absorb a remote partial
   ACC_SEND      peer_out = acc[t]           ship a partial to a named peer
   ACC_EMIT      out = fp16(acc[t])          final result to NoC / memory
   ACC_FWD       peer_out = f24(chain)       pass through, no local accumulate
```

`ACC_SEND` and `ACC_ADD_PEER` are the pair that makes §7 work. `ACC_SEND` names
a destination accumulator, not a memory address — partials never round-trip
through DRAM.

### 6.2 Precision note

> The diagrams above say FP24 because that is what this section was written
> against. **The built accumulator is FP22, `S1E7M14`** — measured identical to
> FP24 against both FP64 and the exact-integer model, cheaper, and it carries the
> slack that takes the cluster past 300 MHz. See §10 and
> [`accumulator.md`](accumulator.md). Everything below about *why* E7 is
> required is unchanged; only the mantissa width moved.

A K = 32 block result is exactly 19 bits. FP24 as `S1E7M16` carries a 17-bit
significand, so the conversion rounds by 2 bits; FP22's 15-bit significand
rounds by 4. E7 is required either way, not optional: the accumulator's exponent
is `sA + sB` plus the integer magnitude, and for FP16 sources that sum spans
roughly -48..+30. An E5 field would overflow on ordinary data.

FP32 is the alternative if the bits matter. Cost is amortised 32:1 whichever is
chosen, so this is an accuracy decision, not an area one — which is exactly what
made it measurable, and the measurement said FP22.

---

## 7. Accumulator peer network

The requirement: **an accumulator must be able to send its tile to, and receive
a tile from, another accumulator.** That is what lets a matmul span clusters
without going through memory.

```
   CHAIN reduction                        N-1 hops, one port pair per node

     ACC0 --> ACC1 --> ACC2 --> ACC3 --> result
     K0..     K512..   K1024..  K1536..


   TREE reduction                         log2(N) hops, lower latency

     ACC0 --+
            +--> ACC01 --+
     ACC1 --+            |
                         +--> result
     ACC2 --+            |
            +--> ACC23 --+
     ACC3 --+
```

Chain maps naturally onto XY routing between neighbours and needs no extra
buffering. Tree halves the latency and is worth it when many clusters split one
K. Both are just sequences of `ACC_SEND` / `ACC_ADD_PEER`; the topology is a
scheduling decision, not a hardware one.

---

## 8. Mapping a large matmul

### 8.1 Two ways to split

```
   SPLIT M/N   -- independent, no communication at all

     +--------+--------+--------+        each cluster owns a disjoint
     |  C0    |  C1    |  C2    |        output tile and writes it out
     +--------+--------+--------+        directly. Preferred.
     |  C3    |  C4    |  C5    |
     +--------+--------+--------+


   SPLIT K     -- needs reduction across accumulators

     C0: K 0..511    --+
     C1: K 512..1023 --+--> accumulator reduction --> result
     C2: K 1024..    --+

     Use only when M x N is too small to keep every cluster busy.
```

Split M/N should be the default: it costs nothing. Split K exists so that a
tall-thin or short-wide matmul can still fill the machine, and the peer network
is what makes it cheap when it is needed.

### 8.2 Why the output tile size sets the NoC port count

Arithmetic intensity of a tile is

```
        MACs        M*N*K          M*N
      --------  =  ---------  =  -------      <-- K cancels
      operands     M*K + K*N      M + N
```

**K cancels.** Chaining more CUs raises MACs and operand demand together — the
cluster chain buys DSP density and shared control, not bandwidth. Only M and N
buy bandwidth.

NoC refill rate for a cluster running at 512 MAC/cycle:

```
     refill  =  512 * ( 1/M + 1/N )   elements per cycle

      output tile      refill        implication
      -----------      ----------    -----------------------------
        4  x  4        256 el/cyc    many ports, unaffordable
       16  x 16         64 el/cyc    workable
       64  x 64         16 el/cyc    about half of one port
```

So the accumulator buffer's capacity is not a convenience — it is the knob that
sets the cluster's port count, and BRAM is far cheaper than NoC endpoints.

### 8.3 Temporal before spatial

```
   TEMPORAL   one cluster sweeps K locally, accumulating in its own buffer.
              Output traffic is 512/K values per cycle -- nearly zero.

   SPATIAL    clusters split K and reduce over the peer network.
              Costs one tile transfer per cluster boundary.
```

Temporal is the default. Spatial is for when there is more machine than problem.

---

## 9. Worked example

`C[64,64] = A[64,1024] · B[1024,64]` on one cluster.

```
   tiles:   M/4 = 16 row-groups
            N/4 = 16 col-groups
            K/32 = 32 blocks

   accumulator buffer holds the whole 64 x 64 output tile in FP24.

   for kb in 0..31:                       <- 32 K blocks
       for i in 0..15:                    <- 16 row groups
           for j in 0..15:                <- 16 col groups
               chain = TCU0..3 ( A[i, kb], B[kb, j] )     one cycle, 4x32x4
               acc[i][j] += f24(chain)                    ACC_ADD

   emit acc as FP16
```

Cycles: 32 x 16 x 16 = 8192, at 512 MAC/cycle = 4,194,304 MACs = `64*64*1024`. ✓

Operand traffic: A is 64x1024, B is 1024x64 = 131,072 elements over 8192 cycles
= **16 elements/cycle**, matching §8.2. Output traffic: 4096 values over 8192
cycles. Both comfortably inside one NoC port.

---

## 10. Parameters and open questions

Fixed by this design:

```
   tensor CU shape          4 x 8 x 4
   cluster                  4 tensor CU + 1 accumulator CU
   cluster throughput       4 x 32 x 4 per cycle
   quantisation block       K = 32
   element format           MXFP7: E5M3 scale per block of 32 + int7 element
   scale field              8 bit  = { E[4:0], M[2:0] }, bias 20, anchor 40
   operand payload          256 bit = 32 x int7 + 4 x E5M3
   L0 / L1 accumulation     exact integer
   supported shapes         M = 4a, N = 4b, K = 32c
   machine precision        AMP FP16-MXFP7, FP22 accumulate  (s3.0)
```

Settled since:

```
   accumulator format       FP22 S1E7M14 -- measured identical to FP24 against
                            FP64 and exact-int, cheaper, and carries the slack
                            that takes the CU past 300 MHz. See accumulator.md
   accumulator buffer size  a DEPTH parameter on mx_acu_fp, 5 BRAM36 at any
                            depth up to 512. mx_cluster_cu defaults to TILES =
                            256; the driver bench runs TILES = 512 sub-tiles =
                            Gm 16 x Gn 32 = a 64x128 resident output tile
   quantiser rounding       significand to nearest; the SCALE rounds UP, so a
                            block's peak lands at or below 63 and never clips.
                            Rounding the scale down would put the peak past 63
                            and damage the largest element in the block -- the
                            one that matters most
```

The buffer size is settled in the sense that it is now a number, and unsettled
in the sense that the number is small. §8.2 said the tile size sets the
cluster's operand bandwidth; measured end to end, it does. 64 sub-tiles against
32 L1 entries puts the machine at four tile-ops per L1 entry loaded, which is
**fill-bound at 6–7% of datapath peak**. No scheduling change moves that ratio;
only a bigger tile does. The numbers are in [`../system.md`](../system.md) §6
and the arithmetic is in [`../arch-design.md`](../arch-design.md) §4.2.

> **That was the state when this section was written, and the last sentence is
> wrong.** The tile did grow, and so did the rest: two clusters now reach 87.6%
> of datapath peak, and most of the distance came from overlap, residency and a
> memory port per mesh row rather than from tile size alone. Current figures are
> [`../perf.md`](../perf.md) §0; §2 there is why "it must be bandwidth" kept
> being the wrong answer.

Open:

```
   how large the resident tile should be    the ratio above is the argument for
                                            raising it; BRAM is at 8% while DSP
                                            is already 99.6% at 45 clusters
   peer topology            chain vs tree -- scheduling, not hardware
   chain bypass             should a cluster degrade to 4 independent CUs?
```

The last one is worth building in cheaply: a mux on the chain input lets one
cluster act as a fused 4x32x4 unit or four independent 4x8x4 units, so the chain
is not dead silicon when a workload does not want K = 32.
