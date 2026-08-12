---
title: The tensor core
summary: Two int7 MACs per DSP48E2 sharing an activation through the pre-adder, a cascade that reduces K=32 without touching the fabric, and the packing offset that decides how deep it can go.
tags:
  - kohakutpu
  - matmul
  - dsp
---

# The tensor core

KohakuTPU's compute unit. A **cluster** is four tensor CUs chained into an
accumulator; a tensor CU is a 4x8x4 block of 64 DSP48E2s. Per cycle a cluster
computes `4 x 32 x 4` — 512 MACs, 1,024 FLOP.

The device is UltraScale+, so **DSP48E2** — not DSP58, which has native INT8
SIMD this part does not have. Everything below is arithmetic arranged to fit a
primitive that was not designed for it.

Measured resources and frequencies are in [results.md](results.md) §2. This page
is why the circuit is shaped the way it is.

---

## 1. The organising principle

**Systolic in the small, mesh in the large.**

A systolic array is very good at one thing: making accumulation free. Partial
sums move through dedicated links and are added where they land, so there is no
adder tree and no accumulator storage in the hot path. It is bad at everything
else — rigid shape, long fill and drain, poor utilisation on small or irregular
matrices, and it scales all-or-nothing.

A mesh is the opposite: flexible composition, independent nodes, graceful
scaling, at the cost of packet overhead per hop.

This design takes the accumulation mechanism from the first and the composition
model from the second, and puts the boundary **where a dedicated wire stops
being cheaper than a packet**. That boundary sits at K = 32.

```
   systolic  <--------------------+--------------------> mesh
                                  |
   free accumulation,             |    flexible composition,
   fixed shape,                   |    independent nodes,
   no packets                     |    packet cost per hop
                                  |
                              K = 32
```

Everything else follows from choosing that one point — including the block size
of the number format, which is the same number ([number-format.md](number-format.md) §1.1).

### 1.1 The reduction hierarchy

Four levels, each using the cheapest mechanism that exists at its scale.

| level | span | data | mechanism | what it costs |
|---|---|---|---|---|
| L0 | K = 8 | int | DSP `PCOUT -> PCIN` cascade | dedicated silicon |
| L1 | K = 32 | int | CU-to-CU, over the DSP's `C` port | one 48-bit bus |
| L2 | K = 32n | FP | accumulator CU, one adder | one add per 32 MACs |
| L3 | unbounded | FP | accumulator to accumulator | one packet per tile |

**L0 and L1 are exact integer** — no alignment, no normalisation, no rounding —
because every product inside a K=32 block carries the same block scale. That is
the whole point of the design. L2 is where floating point first appears, reached
once per 32 MACs, so a comparatively expensive adder is amortised into
irrelevance. L3 is how large problems compose and never touches the inner loop.

---

## 2. Two MACs per DSP, and the packing offset

A DSP48E2 computes `(A + D) * B + {C | PCIN}`. The pre-adder is the lever: two
int7 weights are laid at different bit positions in the same 27-bit operand, and
one shared activation multiplies both at once.

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

No fabric adder forms the packed operand: the pre-adder does it, and both port
loads are wiring.

### 2.1 Choosing S

int7 is signed, so elements are `[-64, +63]`. Two constraints fight each other.
The packed operand must fit 27 bits signed, and the lower product field needs
guard bits above the product width so that many products can be summed into it
before it overflows into its neighbour.

```
   packed operand fits:     |w1*2^S + w0|  <=  2^26
                            worst case w1 = w0 = -64

   fields do not overlap:   S >= 14        (products are 14 bits signed)

   accumulation depth:      guard = S - 14
```

| S | packs in 27 b? | guard | cascade depth |
|---|---|---|---|
| 19 | yes, `-33,554,496` of `-67,108,864` | 5 | **32** |
| 20 | **no** — `-67,108,928` overflows by 64 | 6 | — |

**S = 20 does not work.** `-64 · 2^20 = -2^26` exactly consumes the range, and
the second weight's `-64` pushes it over. S = 19 is therefore the maximum, and it
gives exactly the depth needed:

```
   product range        [-4032, +4096]        14 bits signed
   sum of 32            [-129,024, +131,072]  18 bits + sign
   lower field [18:0]   +/- 262,144           fits, with about 1 bit of margin
```

**Cascade depth 32 is exactly one K=32 block.** Nothing has to be extracted
mid-cluster. `S = 19` is a parameter on `mx_mac`, and it is the number the whole
format hangs off.

### 2.2 The field map

```
   bit  47                    33 32              19 18            0
       +------------------------+------------------+--------------+
       |    sign extension      |  sum of w1*a     | sum of w0*a  |
       +------------------------+------------------+--------------+
                                 upper output        lower output
                                 (row i+1)           (row i)
```

The guard-bit budget is the entire argument for a **7-bit** element rather than
an 8-bit one, and it agrees with the payload-width argument in
[number-format.md](number-format.md) §3 from a completely different direction.

---

## 3. Mapping a 4x8x4 tile

For a fixed `k` and `j` the activation `B[k,j]` is shared across all four rows of
A. That is exactly the shape the packing wants: **two rows of A per DSP, one B
element on the shared port.**

```
     one DSP  =  one k, one j, two rows of A

        A[0,k] --+                        +--> out(0,j)   lower field
                 |--> DSP -- x B[k,j] --> |
        A[1,k] --+                        +--> out(1,j)   upper field
```

A tile is 2 row-pairs x 4 columns = **8 chains**, each 8 DSPs deep, one per k:

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

   8 chains x 8 DSPs = 64 DSP per tensor CU, 128 MACs/cycle
```

A tensor CU consumes two 256-bit operands per cycle, produces 16 integer
partials, and **has no port to the mesh at all** — its result goes down the
cluster chain.

### 3.1 Crossing CU boundaries for free

The cascade `PCOUT -> PCIN` only reaches a physically adjacent DSP in the same
column. A 32-deep chain spanning four CUs would force all four to be adjacent —
a floorplanning constraint worth avoiding.

It is not needed, because **the `C` port is unused**. Integer elements have no
implied leading 1, so none of the `(1+Ma)(1+Mb)` correction an FP8 design needs
exists here, and `C` is free to carry the upstream partial:

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

`OPMODE` selects `Z` from `{0, PCIN, P, C, …}` and `X/Y` from the multiplier, so
`M + C` and `M + PCIN` are both single-DSP operations. **The entire K=32
accumulation across all four CUs costs zero fabric adders**, and each CU stays an
independent 8-deep cascade that can be placed on its own.

Because all four CUs share one block scale, the chain input and output are plain
integers — a CU adds its 16 partials to the 16 arriving from upstream and passes
them on. No scale travels with them.

### 3.2 Extraction

Once per K=32 block, per chain:

```
   lower = signed( P[18:0] )                 19-bit, no logic (wiring)
   upper = P[47:19] + P[18]                  29-bit increment
```

The `+ P[18]` is the borrow correction. The whole 48-bit word is one two's
complement accumulation, so a negative lower sum borrows from the upper field;
adding back its sign bit undoes that. It is the only arithmetic in the extract
path, and it is one of the things the bench targets specifically
([results.md](results.md) §10).

### 3.3 Operand skew

A cascade adds one pipeline stage per DSP, so the operand for stage `k` must
arrive `k` cycles after stage 0 — eight stages of skew on 7-bit operands.

```
   FF chain:   7 bit x 8 stages x 32 lanes  = 1792 FF
   SRL32:      1 LUT per bit at any depth <= 32
               7 bit x 32 lanes             =  224 LUT, no FFs
```

SRL32 makes skew one LUT per bit at any depth up to 32, and the DSP's own
`A1/A2` and `B1/B2` input registers absorb the first two stages before any SRL is
needed. Measured, this is where a tensor CU's LUTs actually go: 224 of 336 are
SRLs and the routing logic is the remaining ~112 ([results.md](results.md) §2.1).

---

## 4. The cluster

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
                                                          ||
                                                          || 16 x int19
                                                          || + sA[i]+sB[j]
                                                          vv
                                                   +---------------+
                                      mesh <------>|  accumulator  |
                                                   +---------------+
```

The supported shape is exactly what that structure implies:

```
   M = 4a ,  N = 4b ,  K = 32c
```

K is swept in multiples of 32; that is the only constraint the hardware imposes,
and it is the block size again.

**The chain is only 4 deep**, so fill and drain are 4 cycles rather than the
hundreds a monolithic systolic array would need. That is what keeps the cluster
usable on small matrices, and it is the concrete payoff of putting the
systolic/mesh boundary at K=32 rather than at the whole problem.

The accumulator is the only part of a cluster that talks to the mesh, and it is
where the output tile lives — [accumulator.md](accumulator.md).

### 4.1 Five memories, and none of their shapes came from the framework

A cluster's storage is worth naming explicitly, because it is easy to read a
project's operand buffer as though the framework had specified one. It did not.

| memory | width | primitive | read latency |
|---|---|---|---|
| `u_l1a` — the A operand | **928 bits** | named, not inferred | 1 |
| `u_l1b` — the B operand | **928 bits** | named, not inferred | 1 |
| the accumulator tile, one per node | 352 bits at `ACC_MW = 14` | named, not inferred | **2** |

**928 bits is a consequence of the format and the tile geometry**, not a
convention: one entry is 4 lanes x 32 K elements at 7 bits, which is 896, plus
four 8-bit block scales. Two separate RAMs rather than one, because a sweep reads
an A entry and a B entry in the same cycle. And the read latency differs *within
one unit* — 1 on the operand RAMs, 2 on the accumulator tile — because the
accumulator has slack in front of its read that the manager does not
([accumulator.md](accumulator.md) §5).

The vector core beside it reaches the same mesh port with a **256-bit** flat
scratchpad, a separate instruction memory in distributed LUTRAM, and a register
file mirrored three times to synthesise three read ports. **Neither shape is "the
L1".** Both are what their own datapath needed, and the framework's contribution
is that two units could differ that much and still be endpoints of the same kind.

The one thing here that *is* a convention rather than a free choice: **every
memory names its primitive** rather than letting synthesis infer one. That rule is
not aesthetic — the accumulator's tile cost 22,845 LUT and missed timing while it
was inferred LUTRAM, and the same array as a named block RAM with its output
register enabled is 5 primitives at 349.4 MHz ([results.md](results.md) §2.4).

---

## 5. Element width is the throughput lever

Packing density is set by product width against the 27-bit A port and the 48-bit
accumulator. The whole table is worth recording because it is the one parameter
that would change the machine's peak rate:

| element | product | packs | S | guard | depth | MACs/cycle at 12,288 DSP |
|---|---|---|---|---|---|---|
| int8 | 16 b | 2 | 19 | 3 | 8 | 24,576 |
| **int7** | **14 b** | **2** | **19** | **5** | **32** | **24,576** |
| int6 | 12 b | 2 | 19 | 7 | 128 | 24,576 |
| int4 | 8 b | 3 | 11 | 3 | 8 | **36,864** |

**int8 buys nothing.** Same two packs per DSP as int7, but only depth 8 — so a
K=32 block would need draining four times, adding fabric work for one extra bit
of precision. int7 is strictly better here, and it is also the width that fills
the operand payload exactly.

**int4 is 1.5x the throughput** at three packs per DSP. But depth 8 means
extraction every K=8 rather than every K=32, and three rows per DSP maps
awkwardly onto a 4-row tile — it wants an 8-row tile. That is a genuine option
and it changes the tile geometry, not just a parameter, so it is a different
machine rather than a setting.

---

## 6. Where the LUTs are not

The design's central claim was that accumulation would leave the fabric
entirely. Measured, per `mx_mac`: **0 LUT, 0 FF, 1 DSP**. The multiply *and* the
whole K=32 reduction happen inside the DSPs — the cascade for K=8, the `C` port
across CUs — and the claim holds exactly ([results.md](results.md) §2.1).

What that did not predict is where the LUTs went instead. The datapath budget
written before building came to ~4,200 LUT per cluster; the built endpoint,
including its manager, L1, sequencer and the framework's compute-unit port,
measures roughly four times that, and essentially all of the difference is
outside the datapath. The direction of the argument survives and the estimate
does not — which is why the budget is not quoted here as a utilisation figure.
[results.md](results.md) §2 and §5 have both numbers and the correction.

---

## 7. What is built, and what is not

Built and verified against both a behavioural model and the real DSP48E2 — the
packing, the cascade, the cross-CU `C` path, the extraction and the borrow
correction. The bench coverage and the two-model discipline that caught a DSP
register-configuration bug are in [results.md](results.md) §10.

Not built:

- **Chain bypass.** A mux on the chain input would let one cluster act either as
  a fused 4x32x4 unit or as four independent 4x8x4 units, so the chain is not
  dead silicon when a workload does not want K=32. Cheap to build in, and worth
  it; nothing depends on it today.
- **`FWD`** — the accumulator op that would pass a chain result straight to a
  peer without accumulating locally — has no command source. Peer transfer works,
  but through the drain queue rather than on a direct accumulator-to-accumulator
  wire ([isa.md](isa.md) §5).
