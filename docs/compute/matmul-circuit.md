# Matmul unit — circuit design

How [`matmul.md`](matmul.md) maps onto DSP48E2 and fabric. Primitive level:
port packing, cascade structure, what costs LUTs and what does not.

Device is UltraScale+ (`xcvu13p`), so DSP48E2 — **not** DSP58, which has native
INT8 SIMD this part does not.

Reference for the primitive: [`dsp.md`](dsp.md).

---

## 1. What we are trying to avoid

The existing FP8 tensor core measures 12,731 LUT / 64 DSP for 128 MACs
([`costs.md`](costs.md)), and **10,656 of those LUTs are the 32 `FPVectorAdd`
units** — 84% of the core is its adder tree.

Two causes, and both go away together:

```
  1. the tree is FLOATING POINT       every node aligns, adds, normalises
  2. the packing leaves 1 guard bit   four 3-bit mantissas at 8-bit spacing
                                      produce 7-bit products with one spare
                                      bit, so the DSP can accumulate exactly
                                      TWO terms and everything else must
                                      leave for the fabric
```

Dense packing is what *forces* the expensive tree. The design below trades one
packed product per DSP for enough guard bits to accumulate an entire K=32 block
inside the DSP cascade, and the tree disappears entirely.

---

## 2. Operand packing — the arithmetic, checked

Two int7 weights share one int7 activation. The pre-adder builds the packed
operand, so no fabric adder is needed to form it:

```
              A (27) = w1 << S          pure wiring, no logic
              D (27) = w0               pure wiring, no logic
                        |
                  (A + D)  = w1*2^S + w0        DSP pre-adder
                        *
              B (18) = a                one activation, shared
                        =
              P (48) = w1*a*2^S + w0*a  two products, disjoint fields
```

### 2.1 Choosing S

int7 is signed: elements are `[-64, +63]`. Two constraints fight each other —
the packed operand must fit 27 bits signed, and the lower product field needs
guard bits to accumulate.

```
   packed operand fits:     |w1*2^S + w0|  <=  2^26
                            worst case w1 = w0 = -64
                            -64*2^S - 64   >= -2^26

   fields do not overlap:   S >= 14        (products are 14 bits signed)

   accumulation depth:      guard = S - 14
```

| S | packs in 27b? | guard | cascade depth |
|---|---|---|---|
| 19 | yes, `-33,554,496` of `-67,108,864` | 5 | **32** |
| 20 | **no** — `-67,108,928` overflows by 64 | 6 | — |

**S = 20 does not work.** `-64 · 2^20 = -2^26` exactly consumes the range, and
the second weight's `-64` pushes it over. S = 19 is the maximum, and it gives
exactly the depth needed:

```
   product range        [-4032, +4096]        14 bits signed
   sum of 32            [-129,024, +131,072]  18 bits + sign
   lower field [18:0]   +/- 262,144           fits, ~1 bit margin
```

**Cascade depth 32 = exactly one K=32 block.** No mid-cluster extraction.

### 2.2 Field map

```
   bit  47                    33 32              19 18            0
       +------------------------+------------------+--------------+
       |    sign extension      |  sum of w1*a     | sum of w0*a  |
       +------------------------+------------------+--------------+
                                 upper output        lower output
                                 (row i+1)           (row i)
```

---

## 3. Mapping a 4x8x4 tile

For fixed `k` and `j`, the activation `B[k,j]` is shared across all four rows of
A. That is exactly the shape the packing wants: **two rows of A per DSP,
one B element on the shared port.**

```
     one DSP  =  one k, one j, two rows of A

        A[0,k] --+                        +--> out(0,j)   lower field
                 |--> DSP -- x B[k,j] --> |
        A[1,k] --+                        +--> out(1,j)   upper field
```

Tile: 2 row-pairs x 4 columns = **8 chains**, each 8 DSPs deep (one per k):

```
   j = 0                     j = 1              j = 2       j = 3
   rows 0,1   rows 2,3       rows 0,1  ...
   +------+   +------+
   | k=0  |   | k=0  |
   +--||--+   +--||--+       PCOUT -> PCIN, dedicated routing,
   | k=1  |   | k=1  |       no fabric, no LUTs
   +--||--+   +--||--+
   | ...  |   | ...  |
   +--||--+   +--||--+
   | k=7  |   | k=7  |
   +------+   +------+
      \\         \\
       vv         vv
     partial to the next CU in the cluster

   8 chains x 8 DSPs = 64 DSP per tensor CU, 128 MACs/cycle
```

---

## 4. Crossing CU boundaries for free

The cascade `PCOUT -> PCIN` only reaches a physically adjacent DSP in the same
column. A 32-deep chain spanning four CUs would force all four to be adjacent —
a floorplanning constraint worth avoiding.

It is not needed. **The `C` port is unused** — integer elements have no implied
`1`, so none of the `(1+Ma)(1+Mb)` correction the FP8 design needs exists here.
So the first DSP of each CU takes the upstream partial on `C`:

```
   CU 0            CU 1            CU 2            CU 3
   +------+        +------+        +------+        +------+
   | k=0  |        | k=8  |<--C    | k=16 |<--C    | k=24 |<--C
   |  ||  |        |  ||  |        |  ||  |        |  ||  |
   | k=7  |--P---->| k=15 |--P---->| k=23 |--P---->| k=31 |--> extract
   +------+        +------+        +------+        +------+

   Z = PCIN  within a CU        cascade routing
   Z = C     at CU entry        one 48-bit bus, added inside the DSP ALU
```

`OPMODE` selects `Z` from `{0, PCIN, P, C, ...}` and `X/Y` from the multiplier,
so `M + C` and `M + PCIN` are both single-DSP operations.

**The entire K=32 accumulation across all four CUs costs zero fabric adders.**
Each CU is an independent 8-deep cascade; only a 48-bit bus crosses between them.

---

## 5. Extraction

Once per K=32 block, per chain:

```
   lower = signed( P[18:0] )                 19-bit, no logic (wiring)
   upper = P[47:19] + P[18]                  29-bit increment, ~29 LUT
```

The `+ P[18]` is the borrow correction: the whole 48-bit word is one two's
complement accumulation, so a negative lower sum borrows from the upper field.
Adding back its sign bit undoes that.

8 chains per cluster -> **~232 LUT, once per 32 MACs.**

---

## 6. Where the LUTs actually go

```
   multiply                                    0 LUT     256 DSP
   accumulation, K=8 within a CU               0 LUT     DSP cascade
   accumulation, K=32 across CUs               0 LUT     DSP C port
   field extraction                          232 LUT     8 chains
   operand skew (SRL32, see 6.1)            ~250 LUT
   accumulator, 16 lanes (see 6.2)         ~3200 LUT
   control / staging                        ~500 LUT
   ------------------------------------------------------
   per cluster                              ~4200 LUT     256 DSP
```

At 48 clusters that is **~200k LUT (12%) and 12,288 DSP (100%)** — the design
becomes DSP-bound, which is the correct place to be bound on this part.

Per 128 MACs: **~1,050 LUT versus 12,731 today.** Roughly 12x, and essentially
all of it comes from moving accumulation out of the fabric.

### 6.1 Operand skew — use SRLs, and the DSP's own registers first

A cascade adds one pipeline stage per DSP, so the operand for stage `k` must
arrive `k` cycles after stage 0. Eight stages of skew on 7-bit operands.

```
   FF chain:   7 bit x 8 stages x 32 lanes  = 1792 FF
   SRL32:      1 LUT per bit at any depth <= 32
               7 bit x 32 lanes             =  224 LUT, no FFs
```

`dsp.md` notes the `A1/A2` and `B1/B2` input registers are barely used. They
absorb the first two stages of skew for free before any SRL is needed.

### 6.2 The accumulator is now the dominant LUT cost — and fixed point is not free

Two options for absorbing a K=32 block result into the running total. My earlier
assumption that fixed point wins is **wrong**, and the reason is instructive:

```
   FP24  S1E7M16     align shift is bounded by the MANTISSA width (17 bit)
                     ~200 LUT/lane, rounds the 19-bit block result by 2 bits

   fixed point       align shift spans the full accumulator width (48 bit)
                     5-stage barrel over 48 bits = ~240 LUT, + 48-bit add
                     ~290 LUT/lane, exact
```

Floating point normalises, so its shifter only ever moves a mantissa. Fixed
point does not, so its shifter is as wide as the accumulator. **FP24 is the
cheaper of the two**, not the more expensive one.

Fixed point becomes competitive only if the scale spread is clamped:

```
   clamp spread to 16 binades -> 35-bit accumulator, 4-stage shift
                              -> ~175 LUT/lane, exact within the clamp
```

That is a real option, but it silently discards contributions more than 16
binades below the running maximum. Worth measuring before choosing.

---

## 7. Element width is the throughput lever

Packing density is set by product width against the 27-bit A port and the
48-bit accumulator.

| element | product | packs | S | guard | depth | MACs/cycle (12,288 DSP) |
|---|---|---|---|---|---|---|
| int8 | 16b | 2 | 19 | 3 | 8 | 24,576 |
| **int7** | **14b** | **2** | **19** | **5** | **32** | **24,576** |
| int6 | 12b | 2 | 19 | 7 | 128 | 24,576 |
| int4 | 8b | 3 | 11 | 3 | 8 | **36,864** |

Two observations:

**int8 buys nothing.** Same 2 packs as int7, but only depth 8 — so a K=32 block
needs draining four times, adding fabric work for one extra bit of precision.
int7 is strictly better here, and it is also the width that fills the 256-bit
operand payload exactly.

**int4 is 1.5x the throughput** at 3 packs per DSP, but depth 8 means extraction
every K=8 rather than every K=32, and three rows per DSP maps awkwardly onto a
4-row tile — it wants an 8-row tile. A genuine option, but it changes the tile
geometry, not just a parameter.

---

## 8. Open

```
   accumulator          FP24 vs clamped fixed point -- measure both
   int4 variant         needs an 8-row tile; worth a separate estimate
   skew                 confirm A1/A2 + B1/B2 absorb two stages as expected
   chain bypass         mux so a cluster degrades to 4 independent CUs
   rounding             nearest-even vs truncate in the MAS quantiser
```

Nothing here is built yet. Next step is a single chain — 8 DSPs, one pair of
outputs — measured against these predictions before anything is replicated 48
times.
