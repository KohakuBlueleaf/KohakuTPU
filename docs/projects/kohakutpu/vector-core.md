---
title: The vector core
summary: A programmable elementwise and reduction engine — E8M15 chosen so an FMA fits one DSP exactly, four base-2 transcendental seeds at full rate, and sixteen lanes because that is where two mesh ports meet a depth-2 chain.
tags:
  - kohakutpu
  - vector
  - numerics
---

# The vector core

KohakuTPU's second compute unit. Where the cluster runs one macro-op, the vector
core runs *kernels* — it is the part of the machine that is programmed rather
than parameterised, and the only one whose instruction set can branch.

Software sees **FP32 or FP16 in memory** and nothing else. Everything below is
internal.

**Status matters here more than anywhere else in this project.** The ALU is built
and measured: one lane at 324.8 MHz, 1,249 LUT, 3 DSP, no BRAM, latency 14 with
II = 1, the FMA within one ulp — correctly rounded everywhere except one
subtractive-alignment corner, stated exactly in §3 — and all four transcendental
seeds faithful. The
assembled `vec_lanes` and `vec_cu` are also built and measured. The instruction
set around them is specified and partly built; the split-K path in §7 is design.
Every figure is in [results.md](results.md) §3 and §6.3, and where a claim below
is derived rather than measured it says so.

---

## 1. The format: E8M15

```
   23  22            15 14                     0
  +---+----------------+------------------------+
  | S |   E  (8 bits)  |      M  (15 bits)      |      24 bits
  +---+----------------+------------------------+

  1 <= E <= 254   value = (-1)^S * 2^(E-127) * 1.M
  E == 0          zero            (no subnormals -- see 1.3)
  E == 255        inf (M==0) / NaN (M!=0)

  significand  sig = {1'b1, M}    16 bits, always normalised
```

### 1.1 Why E8: conversion becomes wiring

An 8-bit exponent covers FP32's range **exactly**, which makes conversion into
the core range-lossless from both source formats:

| conversion | exponent | significand | result |
|---|---|---|---|
| FP16 → E8M15 | E5 ⊂ E8, rebias by +112 | 10 → 15, zero-extend | **exact** |
| FP32 → E8M15 | E8 = E8, **no change at all** | 23 → 15, round | 1.5e-5 |
| E8M15 → FP16 | rebias, range-check | 15 → 10, round | 4.9e-4 |
| E8M15 → FP32 | **no change at all** | 15 → 23, zero-extend | **exact** |

Two consequences worth stating plainly:

- **There is no overflow, underflow or saturation logic on the way in.** With an
  E5 internal format every convert needs range checks in both directions, and an
  FP32 value outside FP16's range is destroyed rather than rounded.
- **FP16 round-trips exactly.** A kernel that reads FP16, computes, and writes
  FP16 loses precision only to the arithmetic, never to the format.

### 1.2 Why M15: the 48-bit C port fits the alignment range exactly

This is the load-bearing argument, and it is why the choice is M15 rather than
M17.

An FMA has to align the addend against the product. The product of two 16-bit
significands is 32 bits. The DSP's addend port `C` is 48 bits. Lay the product at
`[31:0]` and ask how far the addend must travel:

```
 bit 47                                    31                              0
  |                                         |                              |
  |----------- addend headroom -------------|--- product  sig_a*sig_b -----|
  |            16 bits                      |        32 bits               |
```

`16 significand bits + 32 product bits = 48`. The addend's largest useful
position is bit 47 and the C port's top bit is bit 47. **The fit is exact, with
nothing wasted and nothing missing.**

| significand | product | headroom needed | C port | outcome |
|---|---|---|---|---|
| 11 (FP16) | 22 | 12 | 48 | 14 bits wasted |
| **16 (E8M15)** | **32** | **17** | **48** | **exact** |
| 17 (E8M16) | 34 | 18 | 48 | overflows by 4 |
| 18 (E8M17) | 36 | 19 | 48 | overflows by 7 |
| 24 (FP32) | 48 | 25 | 48 | overflows by 25 |

There is a second, independent wall at the same place: **the B port is 18-bit
*signed***, so it holds 17 significand bits, not 18. An 18-bit unsigned
significand has its MSB set and is read as negative; the correction is
`+ sig_a * 2^17`, and the only free ALU input is `C`, which the addend already
owns.

So **E8M17 costs a second DSP or a 48-bit fabric adder in series with the
alignment shifter, twice over**. E8M15 costs neither. The 4x accuracy M17 buys is
real (3.8e-6 against 1.5e-5) and it is bought at the one place in the datapath
that is already the critical path.

**E8M16** — 17 significand bits — is the honest middle: it fills the B port
exactly and costs nothing there, but still overflows the C port headroom by four
bits. It is recorded because if the alignment ever leaves the DSP for another
reason, M16 becomes free and M15 stops being the right answer.

### 1.3 No subnormals, and why that is free

E8's range is so much wider than either source format that subnormals never
arrive:

- an **FP16 subnormal** (2^-24 … 2^-15) normalises into an ordinary E8M15 value
  — a leading-zero count and a shift, done once at the edge;
- an **FP32 subnormal** is below 2^-126, which no activation or weight reaches;
  flushed on entry;
- an **E8M15 result** that underflows past E=1 is flushed to zero.

So the whole datapath sees `sig = {1'b1, M}` unconditionally. **The implicit-bit
mux, the subnormal exponent fixup and the denormal shifter disappear from every
operation** — not just the FMA, but the comparator, the normaliser and all four
transcendentals. In the older FP16 FMA the subnormal handling is roughly a third
of the control logic; here it is zero.

### 1.4 Precision, honestly

| | rel. error (½ ulp) |
|---|---|
| FP16 | 4.9e-4 |
| **E8M15** | **1.5e-5** |
| FP32 | 6.0e-8 |

**This is not an FP32 core.** It accepts FP32 and immediately sits at 1.5e-5 —
32x better than FP16, 256x worse than FP32. Anything that genuinely needs FP32
accuracy needs the extended mode in §3.1, which is designed and not built.

---

## 2. Three DSPs per ALU

The DSP allocation decides everything else, so it is named first.

| | FMA mode | transcendental mode | extended (FP32) mode |
|---|---|---|---|
| **DSP-E** | exponent sum + alignment shift, one pass | range reduction, segment index | exponent |
| **DSP-M** | `sig_a*sig_b + aligned_c` | Horner stage 2 | low partial product + addend |
| **DSP-P** | idle | Horner stage 1 | high partial product |

This is the same split the older FP16 FMA already used — one DSP for the
exponent, one for the significand — with a third added for polynomials. What the
third one buys, in order of value:

1. **Transcendentals at full rate.** With DSP-P doing Horner stage 1 and DSP-M
   doing stage 2, `exp2`, `log2`, `inv` and `rsqrt` are one pass, II=1 — the same
   throughput as an add. A GPU SFU runs these at a quarter rate, and softmax,
   every normalisation and every activation are transcendental-bound rather than
   FMA-bound.
2. **Full-rate FP32**, as the two halves of a 24x24 product.
3. Roughly 35 LUTs and one logic level of exponent arithmetic.

Item 3 is thin on its own. **If DSP columns ever bind, DSP-E is the one to
drop** — the exponent path is ~35 LUTs of adders in fabric and the design
degrades to 2 DSPs per ALU with transcendentals still at full rate. That is the
graceful direction, and it is why the exponent went on the DSP that is also the
least load-bearing.

Measured, that worry turns out to be misdirected: extrapolated to 128 lanes the
core is ~160k LUT and 384 DSP, which on this device is ~37% of an SLR's LUTs
against 12.5% of its DSPs. **The vector core is fabric-bound, not DSP-bound**
([results.md](results.md) §3.1), so the third DSP is not the thing to economise
on.

---

## 3. The FMA

Two values are needed from the exponents and they are different linear
combinations of the same three:

```
  e_ab = e_a + e_b - 127            the product's biased exponent
  cs   = e_ab - e_c                 how far the addend must be shifted
```

A two-tap `B` constant — `2^12 + 1` — makes the multiplier emit two copies of the
pre-adder sum at two positions, and `C` then biases each field independently, so
one DSP produces both in one pass. The two constants are chosen so the fields
cannot collide: the high copy is always positive, so its sign extension cannot
eat the low field, and the low field's range is `[133, 892]` against a 4096-wide
field, so there is no carry into bit 12 and no borrow out of bit 0 for any legal
input.

**The alignment is one unidirectional barrel shifter**, which is the payoff of
picking a format whose headroom is exactly 48 bits:

```
  s        = 17 + cs                        clamp to [0, 48]
  aligned  = ({sig_c, 32'b0}) >> s          48-bit right shift, 6-bit amount

  s = 0    addend at [47:32]     product entirely below its LSB
  s = 17   addend at [30:15]     exponents equal (cs = 0)
  s = 48   addend gone           product dominates, sticky only
```

A bidirectional shifter — `c_shift >= 0 ? >> : <<` — is two barrel shifters and a
mux. Biasing the shift by the headroom turns it into one, worth roughly 90 LUTs
and a logic level per ALU, and it exists only because 16 + 32 landed on 48.

**The sign of the result never needs a magnitude comparison:**

```
  res_neg = neg && (s != 0) && P[47]
```

The `s != 0` term is the one non-obvious case. At `s == 0` the aligned addend is
at least `2^47` and the product is below `2^32`, so the result is always positive
and `P[47]` is a value bit rather than a sign bit. That case is unreachable by
random operands and is exactly what the bench's alignment sweep exists to hit.

**One bypass, and only one:** at `cs <= -18` the product is more than half an ulp
below the addend, so the correctly rounded result *is* the addend and the output
is `c` verbatim. At `cs = -17` the product is still a guard bit and the normal
path handles it.

**The rounding property, stated exactly.** Bits shifted out of the 48-bit
alignment window are carried as a plain sticky. For an effective addition that
is round-to-nearest-even — correct. For an effective subtraction the discarded
residue is a borrow, and a plain sticky rounds the wrong way in the cases
adjacent to the round boundary: the result reads **exactly one ulp high —
always high, never low**. Reaching it takes three things at once: `s >= 33`
(residue actually leaving the window), an effective subtraction, and a
boundary-adjacent bit pattern. A stream built to oversample exponent-distant
addends measures 19 in 4,000; a banded random suite measures 0 in 6,000. The
same construction, and therefore the same property, is in `mx_fpacc`'s split
path. Correcting it means complementing the residue on subtraction — small,
and natural at the next respin of either datapath; until then this paragraph
is the contract.

Every arithmetic opcode is this one FMA with different operand sources:

```
  add    a*1 + c        mul    a*b + 0        affine  a*b + c
  sub    a*1 - c        neg    a*(-1) + 0     fnma    -(a*b) + c
```

And a reduction tree built from FMA nodes rather than adders computes more than
sums — `a*a+c` is a sum of squares (variance in one pass), `a*b+c` is a dot
product. That capability is free from choosing a three-input primitive.

### 3.1 The extended mode, designed and not built

Full FP32 needs two partial products, and with three DSPs already allocated both
variants come from the same silicon:

| mode | DSPs | significand | passes | rate | rel. error |
|---|---|---|---|---|---|
| native | 1 (M) | 16 | 1 | **1x** | 1.5e-5 |
| extended, sequential | 1 (M) | 24 | 2 | ½x | 6.0e-8 |
| extended, parallel | 2 (M+P) | 24 | 1 | **1x** | 6.0e-8 |

The parallel form is why DSP-P and DSP-M must be cascade-adjacent. It is the
standard 17-bit split, and the mechanism is the `W` mux — DSP48E2 only, DSP48E1
cannot do it — because three simultaneous ALU operands (`M`, `PCIN>>17` and `C`)
is exactly what that mux exists for, and it is what makes the addend free rather
than a third DSP.

**Whether to build it is open.** It costs control complexity and a wider
normaliser, and nothing has yet demanded 6.0e-8.

---

## 4. Transcendentals

The stated priority is that `exp`, `log`, `sqrt` and `inv` are **fast**, not
merely present — one pass, II=1, the same throughput as an add.

### 4.1 Four seeds, chosen in base 2

The seeds are `exp2`, `log2`, `inv`, `rsqrt`. **Base 2, not base e**, and that
choice earns its own paragraph:

```
  log2(x) = (E - 127) + log2(1.M)        E is an exact integer -- field slicing
  exp2(x) = 2^k * 2^f                    k = floor(x)          -- field slicing
```

Range reduction in base 2 is **free and exact**: it is a bit slice. Base e needs
`k = round(x * log2 e)` and `r = x - k·ln2`, which is two extra FMA passes *and*
introduces its own rounding error before the table is even consulted. Nothing is
lost — `ln(x) = log2(x)·ln2` and `e^x = exp2(x·log2 e)` — and in every kernel
that matters the constant folds into a neighbouring scale that already exists.

`rsqrt` is a **fourth table rather than a composition**, because every
normalisation in a transformer hits it once per row. `sqrt = x·rsqrt(x)` and
`div = a·inv(b)` are two passes each, which is the right place to pay.

### 4.2 Quadratic, 32 segments

For a smooth function approximated by degree-*d* minimax polynomials over
segments, reaching *n* bits needs `segments ∝ 2^(n/(d+1))`:

| target bits | pure LUT | linear (d=1) | quadratic (d=2) |
|---|---|---|---|
| 11 (FP16) | 2,048 | 64 x 2 | 16 x 3 |
| **16 (E8M15)** | **65,536** | 256 x 2 | **32 x 3** |
| 24 (FP32) | 16.8 M | 4,096 x 2 | 256 x 3 |

The measured accuracy of each seed against the *actual* fixed-point circuit —
both Horner roundings and all three quantised coefficients — is consistently
about 1.5 bits worse than the minimax prediction, and that gap is the point of
measuring: **the approximation is no longer what limits these functions, the
coefficient and Horner quantisation is.** Adding segments would therefore buy
almost nothing, which is the opposite of the intuition that more segments means
more accuracy, and it is why the count stops at 32. The table is
[results.md](results.md) §6.3.

**32 segments is also the fabric sweet spot.** A 32-entry constant table is a
LUT5 — half a LUT6 — per output bit; `rsqrt` needs both octave parities so it is
64 entries, exactly one LUT6 per bit. The tables are generated together with the
bit-exact golden model, which is what makes the measurement above possible at
all.

They are **block RAM and synchronous**, and both halves of that are load-bearing.
This was argued for distributed RAM on two grounds and both were wrong: the
address was already registered, so a synchronous ROM is a *register move* rather
than an added cycle; and "every lane needs its own copy" never implied a port
count problem, because each lane simply has its own ROM. Moving them saved 3,575
LUT per core with the measured error unchanged to three decimals.

### 4.3 Assembling each result

The point of picking these four seeds is that **three of them need no normaliser
at all** — the result arrives pre-normalised and assembly is a concatenation.

```
exp2(x)     F = poly(idx, u) in [0,1)   ->  { 0, k+127, F[19:5] }
                                            NO normaliser, NO leading-zero count
log2(x)     val = (E - 127) + F         ->  fix2float(val)
                                            the E term is an integer ADD
inv(x)      F = poly(idx, u) in (0.5,1] ->  { S, 253 - E, F[19:4] }
                                            1 bit of normalise, folded into the slice
rsqrt(x)    K = (E-127) >>> 1           ->  { 0, 126 - K, F[19:4] }
                                            parity of the exponent picks the octave table
```

`exp2` skipping the normaliser is the single biggest reason base 2 wins: `2^f`
for `f ∈ [0,1)` is in `[1,2)` **by construction**, so the significand is already
normalised and the exponent is already an integer. The two most expensive stages
of a float pipeline both vanish.

### 4.4 The comparator is the best value per LUT in the design

**E8M15 is sign-magnitude with the exponent above the mantissa**, so ordering by
magnitude is the unsigned integer order of bits `[22:0]` — no decode, no
unpacking. A full signed compare is that plus the sign bits:

```
  a_gt_b = (sa ^ sb) ? sb : ((a[22:0] > b[22:0]) ^ sa)
```

About 20 LUTs, zero DSP, and it drives `max`, `min`, `select`, `clamp`, `relu`
and the predicate output. Without it a `max` is a subtract, a sign test and two
blended multiply-adds — **three passes for one max**, paid by every max
reduction, every clamp, every ReLU and the first pass of every softmax.

### 4.5 What things cost

| op | passes | note |
|---|---|---|
| `fma add mul max min select abs neg` | 1 | |
| `exp2 log2 inv rsqrt` | **1** | full rate, II=1 |
| `sqrt = x*rsqrt(x)`, `div = a*inv(b)` | 2 | |
| `exp = exp2(x*log2e)`, `ln = log2(x)*ln2` | 2 | 1 if the constant folds |
| **`sigmoid` = `inv(1 + exp2(-x*log2e))`** | **4** | exactly one depth-4 chain |
| `tanh = 2*sigmoid(2x) - 1` | 5 | |
| `silu`/`swish` = `x*sigmoid(x)` | 5 | |
| `gelu` (tanh form) | ~9 | two chain traversals |
| `softmax` | 3 elementwise + 2 tree + 1 scalar | |
| `layernorm`/`rmsnorm` | 1 elementwise + 2 tree + 1 scalar | `γ·r` and `β−μγr` fold into one FMA |

`sigmoid` landing on exactly four is not luck, but it is a useful coincidence: it
is the strongest single piece of evidence for a chain depth of 4.

**Newton refinement stays in software.** `1/a: y' = y(2-ay)` is two FMAs and
`rsqrt: y' = y(1.5-0.5ay²)` is three, and each step doubles the correct bits. At
this table accuracy the native result is already better than the format, so
refinement buys nothing in native mode — it exists for the extended mode, where a
15-bit seed plus one step reaches 30. Keeping it an instruction sequence makes
accuracy a *program* choice rather than a synthesis choice, which is the right
way round: a softmax denominator does not need refinement, an `rsqrt` feeding
fifty layers of normalisation might.

---

## 5. Sixteen ALUs, and why chaining is mandatory

A mesh flit payload is 256 bits, and a vector core is a two-port endpoint like a
cluster, so 512 payload bit/cycle — 32 FP16 elements. A flat elementwise op reads
two vectors and writes one: **3 elements of traffic per result.**

```
  bandwidth ceiling   32 / 3  =  10.7 results/cycle
  compute ceiling     16 ALUs =  16   results/cycle
```

| mode | ops/result | ops/cycle at the bandwidth ceiling | bound by |
|---|---|---|---|
| `FLAT` | 1 | 10.7 | **memory** |
| `D2` | 2 | 21.4 | **compute** |
| `D4` | 4 | 42.7 | **compute** |

**Flat mode is memory-bound and more lanes would not help it.** `D2` already
saturates the ALUs. So 16 lanes is not a round number: it is where two ports of
mesh bandwidth and a depth-2 chain meet.

Halving the pass count is worth exactly as much as doubling the ALU count and
costs far less — a `D4` chain writes no intermediate to the register file at all.
**Chaining is not an optimisation here; it is what makes the core compute-bound
at all**, and it is why the compiler's most valuable pass is fusion
([compiler.md](compiler.md) §2.3).

A mode is a factorisation, `W lanes × D chain depth` with `W·D = 16`, plus a
reduction tree:

| mode | shape | results/cycle | for |
|---|---|---|---|
| `FLAT` | 16 x 1 | 16 | elementwise at max rate |
| `D2` | 8 x 2 | 8 | mul-add-mul, scale-and-bias |
| `D4` | 4 x 4 | 4 | a whole `sigmoid` in one pass |
| `TREE` | 8+4+2+1 + acc | 16 in → 1 out | `sum max dot sumsq` |

**Every ALU is used in every mode, and 16 is the smallest N for which that is
true.** Below 16 the tree wastes ALUs; above it the tree needs a fifth level and
depth-4 chains stop dividing evenly.

### 5.1 A chain OR a tree, never both

The hard constraint on every fused reduction. `TREE` spends its sixteen ALUs as 8
leaves + 7 combine nodes + 1 accumulator — *the tree physically is the other
eight ALUs* — and an 8-wide by 2-deep chain also spends all sixteen. So a
reduction fused with a **two-stage** elementwise computation does not fit, and no
amount of wiring makes it fit.

That is the whole reason the fused exp-and-sum reduction is unary. `exp2(a)` is
one stage, so it sits in the leaf and the tree survives; `exp2(a - m)` is two,
and it would need a fused single-stage leaf op inside the ALU — measured at ~800
LUT per core for the bias alone, because the round and the negate adders are
parallel by design and both need the extra term.

The corollary is the useful one: **any unary elementwise op can be fused with a
reduction for almost nothing**, because slice *s* comes from leaf *s mod 8* and
below 8 that is the wire flat mode already selects.

### 5.2 The accumulator recurrence

The ALU is **14 cycles deep**, so `acc = acc + x` has a 14-cycle loop-carried
dependency and a naive accumulator runs at II=14. That is a 14x throughput cliff
on every reduction and it does not show up until the design is built.

The fix is standard and cheap: **16 rotating partial accumulators**, one per
pipeline slot, then one final tree pass to combine them. Cost is 16 registers per
core — nothing — and it also *improves* accuracy, since 16 partial sums of length
V/16 accumulate rounding like `sqrt(V/16)` rather than `sqrt(V)`.

### 5.3 Predication, and the one non-ALU instruction that honours it

Every ALU op carries `pr` (which of four 128-bit predicate registers) and `pm`
(0 unconditional, 1 where the bit is set, 2 where it is clear) in `ir[4:1]`, and
they gate the per-slice write enable. **`VSHUF` now carries them too.**

It is the only load/store-port instruction that does. `VLD`, `VST`, `VCVT` and
`VBCAST` share the opcode branch, but for those four `ir[4:1]` is the low end of
the 14-bit descriptor offset — reading `pm` there would predicate a load by
accident, so the hardware forces `pm = 0` unless the opcode is `VSHUF`.

Two rules that differ from the ALU path, and both matter:

- **The predicate indexes the DESTINATION lane**, not the source lane the rotate
  reads. `VSHUF vd, va, k` under `pm=1` writes `vd[l] = va[(l+k) % 16]` only
  where `P[pr][l]` is set, `l` being the destination.
- **The VL tail mask is NOT applied.** An ALU write enable is
  `predicate & tail`; this one is the predicate alone, because a whole-chunk
  write is whole-chunk whatever `VL` says. Adding the tail here would corrupt
  silently at any `VL` that is not a multiple of 16.

**Why it exists.** The granule transpose — a 4x4 sub-tile of FP16 is exactly one
32-byte word, but its sixteen elements come from four different rows — is 52% /
62% / 81% of total cycles for `mlp` / `flash 4x128` / `flash 4x256`. The merge
was four rotates and three selects per output register; predicated, the selects
go away and the block drops from **36 instructions per 32 words to 24, a 1.5x**
on the dominant term. See [relayout.md](relayout.md) §12.

**What it cost**, `vec_core` out of context on `xcvu13p-fhgb2104-2L-e` at
3.333 ns, `MODEL=0`, `-flatten_hierarchy rebuilt`, two frozen source snapshots
differing only in these two files:

| | before | after | delta |
|---|---|---|---|
| CLB LUTs | 29,067 | 29,463 | **+396, +1.4%** |
| LUT as logic | 27,016 | 27,156 | +140 |
| LUT as distributed RAM | 1,504 | 1,760 | +256 |
| CLB registers | 24,704 | 24,702 | −2 |
| BRAM / DSP | 44.5 / 51 | 44.5 / 51 | 0 / 0 |
| control sets | 425 | 425 | 0 |
| WNS at 3.333 ns | +0.953 | +0.964 | +0.011 |

The 256 LUTRAM is the whole price: a **third read port** on the 4x128-bit
predicate file, because the load/store path shares neither the ALU pipeline's
`pr` nor its chunk index. The 140 logic LUTs are the 3:1 that replaced a
constant `16'hFFFF`. Timing did not move — both arms bind on the same 8-level
path — so the predicate lookup did not land on the write-enable decode, which
was the risk worth measuring.

---

## 6. The register file, L1, and how little of this resembles the cluster

**Nothing about this core's storage is shared with the matmul cluster except the
port it reaches the mesh through.** The cluster holds two 928-bit operand RAMs and
a 352-bit accumulator tile at two different read latencies
([matmul.md](matmul.md) §4.1). This core holds a **256-bit** flat operand
scratchpad, a **32-bit** instruction memory in distributed LUTRAM, and a register
file built as three mirrored memories.

That contrast is the point. A 256-bit L1 word is 16 FP16 elements, which is
exactly one flat-mode cycle of work for 16 lanes — the mesh word width, the lane
count and the native element size line up so that a fill beat feeds exactly one
cycle. At FP32 a word is 8 elements and loads run at half rate, which is the
honest cost of the wider input format rather than a surprise. None of those
numbers would mean anything to the cluster, and the cluster's 928 means nothing
here.

**The register file is striped by lane**: lane *i* holds elements *i*, *i+16*,
*i+32*, … That is what makes the file lane-local, which is what makes it
affordable — a monolithic file serving 16 lanes x 3 ports would need 48 read
ports. **Anything that crosses lanes must say so**, which is what the shuffle
instruction is for: elementwise work never crosses lanes and reductions cross
them in a fixed tree.

It is 3R1W per lane, built as **three mirrored single-read memories** — three
physical copies written in lockstep, because that is how a three-read-port file is
synthesised out of primitives that have one read port each. **The three copies are
not the same primitive**, and that is the interesting part. Two feed ALU operands and get a whole cycle, so they
are block RAM. The third feeds the store converters, and a block RAM's
clock-to-out in series with a 16-lane E8→FP16 normalise measured **286.0 MHz**,
below the 300 floor — so that copy stays in LUTRAM. A whole-module primitive
parameter hid the fact that only one of three consumers could not afford the
trade; splitting it kept two thirds of the area win for one eighth of the memory.

**L1 is a scratchpad, not a cache.** No tags: the access pattern is strided
streaming produced by the address generator, so every address is known in
advance, and tags would buy nothing and cost a lookup in the load path. It is
filled and drained explicitly against an address descriptor, with a barrier
instruction to wait, and it holds data **in its memory format** — converting on
the read path into the ALU rather than on the way in, because storing converted
would cost 24 bits for FP16 data and make the buffer smaller in elements for no
gain.

`L1_PRIM` and `L1_DEPTH` are parameters and the walks around L1 derive from them
rather than assuming the shape they were written for. **URAM cannot do
`READ_LAT = 1` at all**, so the latency is a property of the primitive rather
than a tuning knob, and the states that absorb the extra beat are entered only
when it applies. Hardcoding either would fail in the quiet direction: a wrong
latency reads the beat before the data lands, and a narrow address wraps.

**It ships as block RAM**, which is the opposite verdict to the accumulator's and
for the opposite reason. The accumulator is already `READ_LAT = 2`, so URAM is
free there ([accumulator.md](accumulator.md) §1.1); the vector core runs
`READ_LAT = 1` and URAM would add a wait state to every load and drain — and the
core is schedule-bound rather than capacity-bound, so a cycle on the load path is
the wrong thing to spend.

> The trade only becomes interesting at a depth block RAM cannot reach, and **a
> deeper vector L1 is blocked somewhere else entirely**: the fill protocol's tag
> ports are 9 bits, so it cannot name an entry past 511 however wide the address
> inside the core is. That is a protocol change, not a memory one, and it is what
> to fix first if a deeper L1 is ever wanted.

---

## 7. The matmul interface: split-K and the FP16 ceiling

The vector core's first real job, and the one that decides its dtype list.

The problem is [accumulator.md](accumulator.md) §7: a dot product over biased
operands grows linearly in K, FP16 saturates at 65,504, and the value survives the
entire reduction and is destroyed on the way out. **Splitting K does not fix the
range** — the final sum is the same number however K is partitioned. What it
enables is a different place to finish:

```
  today      cluster sweeps all of K  ->  EMIT converts to FP16  ->  saturates
  with VC    S clusters sweep K/S each
             each emits a partial at accumulator width, no conversion
             vector core loads S partials, converts to E8M15
             sums them with FP32's exponent range
             converts ONCE, on the store
```

Accumulator-width → E8M15 is **range-lossless** — E7's exponent range is strictly
inside E8's — and costs one rounding of the bottom mantissa bit, 2^-17 relative.
The sum then has E8's range, so the ceiling is 3.4e38 rather than 65,504, and
only the final store can saturate — by which point the value is final and the
driver can choose FP32 if it needs to.

For an output tile of 512 elements and S partials the epilogue is `S x 512` loads
and `(S−1) x 512` adds — about `64 S` cycles per tile on one vector core, so
against a K=1024 GEMM at roughly 1,024 cycles in a cluster, an S=4 epilogue
spread over eight vector cores is under 5% overhead.

**This is not built, and the missing piece is narrow.** The load instruction has
a dtype field with an accumulator-width encoding in it, and the mesh path that
would carry the words exists — but it lands raw 256-bit words in L1 and moves
whatever the sender put in the flit, so it carries accumulator-width values only
once a sender emits them and the load learns to read them. Neither is true yet. A
drain writes FP16, so a partial in memory is FP16, and the decision still open is
whether to give the vector core a peer port or accept FP16 partials there and
keep the wide reduction in the accumulator where it already works.

Range is the reason split-K is *needed*. Occupancy is the reason it is *useful*,
and the margin is thinner than it looks: at the balanced tiling a `256 x 1024 x
256` problem has exactly eight output tiles, so eight clusters are exactly
saturated with no slack for imbalance. Where K-split buys occupancy outright is
skinnier still — a `64 x 4096 x 64` gives one output tile, so seven of eight
clusters get nothing at any 2D grid.

---

## 8. Addressing

**This is what decides whether the core is generally programmable or just fast.**

An address descriptor is a base plus four `(stride, bound)` pairs:

```
  addr = base + sum_i ( idx_i * stride_i ),   idx_i < bound_i
```

Reshape, permute, expand, pad, slice and broadcast are not arithmetic — they are
*views*. With strides they are all free: permute is a permutation of the stride
list, broadcast is stride 0, pad and slice are bounds and an offset. Without a
strided address generator every one of them becomes a physical copy, and
something has to perform those copies. That something should be a DMA engine —
never a scalar core.

**An address generator is therefore not an optimisation; it is the difference
between a kernel language and a fixed function**, and it is worth more than any
arithmetic feature on this page.

---

## 9. What the vector core should not do

Everything data-dependent or irregular, which is what a wide SIMD array is worst
at: integer, bitwise and logical scalar arithmetic; data-dependent control such
as loop bounds, branching and early exit; gather and scatter with computed
indices; sort, top-k, argsort and sampling; shape and metadata arithmetic,
allocation and scheduling; pointer chasing.

The budget for that work is **thousands of operations per token, not billions**.
The moment bulk arithmetic lands there it becomes the limit.

This is a real capability boundary rather than a preference. Element-dynamic
behaviour — where *which* element is touched depends on a value — is not
expressible on-device at any cost today, and that is the machine's largest single
gap. Scalar-dynamic behaviour is expressible, at the price of one host round trip
per step.

---

## 10. Decided, settled by building, and open

**Decided by argument:** external FP32/FP16 only with E8M15 internal, chosen
because 16 + 32 = 48 puts the entire alignment range inside one DSP's C port and
because the 18-bit signed B port stops at 17 significand bits; no subnormals;
three DSPs per ALU with DSP-E the one to drop if columns bind; FMA as the sole
arithmetic primitive plus a sign-magnitude comparator; four base-2 seeds; 32
quadratic segments; one unidirectional align shifter with one bypass.

**Settled by building it:** the lane closes 324.8 MHz against a 300 MHz target
with 3 DSPs and no BRAM; the FMA is correctly rounded and the four seeds are
faithful at ~0.55 ulp; the core is fabric-bound rather than DSP-bound, which
retires the worry that three DSPs per ALU was extravagant and redirects it at the
pipeline's delay lines; more table segments would buy almost nothing; a
predicated `VSHUF` is 1.5x on the granule transpose for **+396 LUT, +1.4%, no
flops, no DSP, no BRAM and no timing** (§5.3).

**Open, and needing measurement rather than argument:**

- **E8M16 if the alignment ever leaves the DSP.** M15 is right only while the
  addend rides the C port.
- **Whether latency 14 can come down.** The delay lines are the largest single
  block of the lane and exist only because the pipeline is 14 deep. II=1 is what
  matters for throughput, but this is the first place to look if 128 lanes do not
  fit.
- **Whether the extended mode is worth building.** Nothing has demanded 6.0e-8.
- **Chain depth 4.** `sigmoid` fits exactly; the right way to settle it is to
  write the kernels that matter as op chains and look at the length histogram.
- **ALUs per core against cores.** The throughput table is known; the
  vector-length distribution of real kernels is not.
- **Accumulation width for long reductions.** Multiply width and accumulate width
  are independent, and a sum over thousands of terms compounds rounding far more
  than any single chain.
- **An L1 footprint band that returns wrong data** is measured, unexplained and
  currently guarded rather than fixed — see [results.md](results.md) §9.
