# Vector core: the ALU

**The ALU is built and measured.** `src/kohakutpu/vector/vec_alu.v`, one lane,
three DSPs, **324.8 MHz / 1,249 LUT / 3 DSP / 0 BRAM, latency 14, II = 1**, with
the FMA correctly rounded and all four transcendental seeds faithful. §13 has the
numbers and how they were taken.

**The core around it — §6 to §11 — is design, not description.** The instruction
set is [../isa/vector.md](../isa/vector.md).

Where a number is derived it says so; where it was measured it says that
instead.

The vector core is a programmable elementwise-and-reduction engine. Software
sees **FP32 or FP16 in memory** and nothing else. Everything below is internal.

This document covers **the ALU** — the arithmetic. Core organisation (how many
ALUs, what modes, the register file, the address generator) is §7, sketched
only, and deliberately not settled here: the control flow is the easy half and
it should be designed against a datapath that already exists.

Bit maps below follow the conventions of [`arithmetic.md`](arithmetic.md), which
documents the FP8 / FP16 / FP24 constructions this design grew out of.

---

## 1. The format: E8M15

```
   23  22            15 14                     0
  ┌───┬────────────────┬────────────────────────┐
  │ S │   E  (8 bits)  │      M  (15 bits)      │      24 bits
  └───┴────────────────┴────────────────────────┘

  1 <= E <= 254   value = (-1)^S * 2^(E-127) * 1.M
  E == 0          zero            (no subnormals -- see 1.3)
  E == 255        inf (M==0) / NaN (M!=0)

  significand  sig = {1'b1, M}    16 bits, always normalised
```

### 1.1 Why E8: conversion becomes wiring

An 8-bit exponent covers FP32's range **exactly**. That makes conversion into
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
  FP32 value outside FP16's range is destroyed rather than rounded. With E8 the
  in-convert is a rebias-add and a shift.
- **FP16 round-trips exactly.** A kernel that reads FP16, computes, and writes
  FP16 loses precision only to the arithmetic, never to the format.

### 1.2 Why M15: the 48-bit C port fits the alignment range exactly

This is the load-bearing argument, and it is the reason the choice is M15 rather
than M17.

An FMA has to align the addend against the product. The product of two 16-bit
significands is 32 bits. The DSP's addend port `C` is 48 bits. Lay the product
at `[31:0]` and ask how far the addend must travel:

```
 bit 47                                    31                              0
  │                                         │                              │
  ├─────────── addend headroom ─────────────┼─── product  sig_a*sig_b ──────┤
  │            16 bits                      │        32 bits               │

  addend below product  (cs > 0) : shift right, gone by cs = 32  -> sticky
  addend above product  (cs < 0) : shift left; product is entirely below the
                                   addend's LSB once -cs = 17, and 17 left
                                   shifts puts the addend's MSB at bit 47.
```

`16 significand bits + 32 product bits = 48`. The addend's largest useful
position is bit 47 and the C port's top bit is bit 47. **The fit is exact, with
nothing wasted and nothing missing.**

Now the same table for the alternatives:

| significand | product | headroom needed | C port | outcome |
|---|---|---|---|---|
| 11 (FP16) | 22 | 12 | 48 | 14 bits wasted |
| **16 (E8M15)** | **32** | **17** | **48** | **exact** |
| 17 (E8M16) | 34 | 18 | 48 | overflows by 4 |
| 18 (E8M17) | 36 | 19 | 48 | overflows by 7 |
| 24 (FP32) | 48 | 25 | 48 | overflows by 25 |

And a second, independent wall at the same place — the **B port is 18-bit
*signed***, so it holds 17 significand bits, not 18:

```
  18-bit unsigned significand   1mmmmmmmmmmmmmmmmm    MSB set -> read as NEGATIVE
  correction is  + sig_a * 2^17 , and the only free ALU input is C,
  which the addend already owns.
```

So **E8M17 costs a second DSP or a 48-bit fabric adder in series with the
alignment shifter, twice over** — once for the B-port sign correction, once for
the alignment headroom. E8M15 costs neither. The 4× accuracy M17 buys is real
(3.8e-6 vs 1.5e-5) but it is not free, and it is bought at the one place in the
datapath that is already the critical path.

**E8M16** (17-bit significand) is the honest middle: it fills the B port exactly
and costs nothing there, but still overflows the C port headroom by 4 bits. It
is listed because if the alignment ever moves out of the DSP for another reason,
M16 becomes free and M15 stops being the right answer.

### 1.3 No subnormals, and why that is free rather than a compromise

E8's range is so much wider than either source format that subnormals never
arrive:

- an **FP16 subnormal** (2^-24 … 2^-15) normalises into an ordinary E8M15 value
  — the conversion is a leading-zero count and a shift, done once at the edge
- an **FP32 subnormal** is below 2^-126, which no NN activation or weight
  reaches; flushed on entry
- an **E8M15 result** that underflows past E=1 is flushed to zero

So the whole datapath sees `sig = {1'b1, M}` unconditionally. **The implicit-bit
mux, the subnormal exponent fixup and the denormal shifter disappear from every
operation** — not just from the FMA, but from the comparator, the normaliser and
all four transcendentals. In `fp_fma.v` the FP16 subnormal handling is roughly a
third of the control logic; here it is zero.

### 1.4 Precision, honestly

| | rel. error (½ ulp) |
|---|---|
| FP16 | 4.9e-4 |
| **E8M15** | **1.5e-5** |
| FP32 | 6.0e-8 |

**This is not an FP32 core.** It accepts FP32 and immediately sits at 1.5e-5 —
32× better than FP16, 256× worse than FP32. Anything that genuinely needs FP32
accuracy needs the extended mode in §5, which is half rate on 1 DSP or full rate
on 3.

---

## 2. Three DSPs per ALU

The DSP is the unit that decides everything else, so the allocation is named
first and the rest of the design follows it.

| | role in FMA mode | role in transcendental mode | role in extended (FP32) mode |
|---|---|---|---|
| **DSP-E** | exponent sum + alignment shift, in one pass | range reduction, segment index | exponent |
| **DSP-M** | `sig_a*sig_b + aligned_c` | Horner stage 2 | low partial product + addend |
| **DSP-P** | idle | Horner stage 1 | high partial product |

This is the same split `fp_fma.v` already uses — **one DSP for the exponent, one
for the significand** — with a third added for polynomials. `arithmetic.md`
documents the FP16 and FP24 exponent maps; §3.1 below is the E8 version of the
same trick.

**What the third DSP actually buys**, in order of value:

1. **Transcendentals at full rate.** With DSP-P doing Horner stage 1 and DSP-M
   doing stage 2, `exp2`, `log2`, `inv` and `rsqrt` are **one pass, II=1** — the
   same throughput as an add. A GPU SFU runs these at a quarter rate. This is
   the item that matters most for NN work, where softmax, every normalisation
   and every activation is transcendental-bound rather than FMA-bound.
2. **Full-rate FP32.** DSP-M and DSP-P as the two halves of a 24×24 product (§5).
3. Roughly 35 LUTs and one logic level of exponent arithmetic (DSP-E).

Item 3 is thin on its own. **If DSP columns ever bind, DSP-E is the one to
drop** — the exponent path is ~35 LUTs of adders in fabric and the design
degrades to 2 DSPs/ALU with transcendentals still at full rate. That is the
graceful direction, and it is why the exponent went on the DSP that is *also*
the least load-bearing.

**Budget.** 128 ALUs × 3 = **384 DSPs, 12.5% of an SLR's 3,072.** Each ALU's
three DSPs sit in one DSP column; the only hard placement constraint is that
DSP-P and DSP-M must be cascade-adjacent (`PCOUT → PCIN`) for the extended mode.
A 16-ALU core is 48 DSPs — half a column — which is compact enough that the
routing risk is the operand buses, not the DSPs.

---

## 3. The FMA

### 3.1 DSP-E — exponent sum and alignment shift, one DSP, one pass

Two values are needed and they are different linear combinations of the same
three exponents:

```
  e_ab = e_a + e_b - 127            the product's biased exponent
  cs   = e_ab - e_c                 how far the addend must be shifted
```

The two-tap `B` trick from `arithmetic.md` produces both at once: `B = 2^12 + 1`
makes the multiplier emit two copies of the pre-adder sum at two positions, and
`C` then biases each field independently.

```
B(18):  000001000000000001                             2^12 + 1  -- two taps
*
(
A(30):  00000000000000000000000eeeeeeee                e_a
+
D(27):  000000000000000000eeeeeeee                     e_b
)                                                      pre-adder: A+D = e_a+e_b
+
C(48):  ...0000 000010000001 0ttttttttt                C[23:12] = 129
                                                       C[11:0]  = 385 - e_c
=
P(48):  ...0000 uuuuuuuuuuuu ssssssssssss

        P[23:12] = e_a + e_b + 129        = u          -> e_ab = u   - 256
        P[11:0]  = e_a + e_b + 385 - e_c               -> cs   = P[11:0] - 512
```

Why those two constants, and why the fields do not collide:

- `e_a, e_b ∈ [1,254]`, so `u = e_a+e_b+129 ∈ [131, 637]` — **always positive**,
  which is what keeps the low copy's sign extension from eating the high field.
  The `+129` costs nothing: it rides in on `C`, not on a fabric adder.
- low field max `508 + 385 - 1 = 892 < 4096`, min `2 + 385 - 254 = 133 > 0`.
  **No carry into bit 12 and no borrow out of bit 0**, so the two fields are
  independent for every legal input.
- `385 - e_c = 130 + ~e_c` — a 9-bit add against a constant, ~5 LUTs. `A` and
  `D` are pure wiring.

Everything downstream reads `u` and `cs` from one 48-bit register.

### 3.2 DSP-M — the significand FMA, one DSP

```
B(18):  00 1mmmmmmmmmmmmmmm                            sig_a = 1.M   (16 bits)
*
A(30):  00000000000000 1mmmmmmmmmmmmmmm                sig_b = 1.M   (16 bits)
+
C(48):  aligned addend, see below
=
P(48):  M +/- C

  M = sig_a * sig_b  in [2^30, 2^32),  at bits [31:0]
  OPMODE  = 9'b000110101      X=M, Y=M, Z=C, W=0
  ALUMODE = neg ? 4'b0011 : 4'b0000       Z-(W+X+Y) : Z+W+X+Y
  neg     = sign_a ^ sign_b ^ sign_c
```

**The alignment is one unidirectional barrel shifter**, which is the payoff of
picking a format whose headroom is exactly 48 bits:

```
  s        = 17 + cs                        clamp to [0, 48]
  aligned  = ({sig_c, 32'b0}) >> s          48-bit right shift, 6-bit amount
  sticky   = |({sig_c, 32'b0} & ~(~48'b0 << s))

  s = 0    addend at [47:32]     product entirely below its LSB
  s = 17   addend at [30:15]     exponents equal (cs = 0)
  s = 48   addend gone           product dominates, sticky only
```

A bidirectional shifter — the shape `fp_fma.v` uses, `c_shift>=0 ? >> : <<` —
is two barrel shifters and a mux. Biasing the shift by the headroom turns it
into one. That is worth roughly 90 LUTs and a logic level per ALU, and it exists
only because 16+32 landed on 48.

**The sign of the result** never needs a magnitude comparison:

```
  res_neg = neg && (s != 0) && P[47]
```

- `neg = 0`: `P = aligned + SP`, always positive, read as unsigned 48.
- `neg = 1, s >= 1`: `aligned < 2^47` and `SP < 2^32`, so `P ∈ (-2^32, 2^47)` —
  `P[47]` is a genuine sign bit and the two's complement is valid.
- `neg = 1, s == 0`: `aligned >= 2^47 > SP`, so the result is **always** positive
  and `P[47]` is a value bit, not a sign. Excluded by the `s != 0` term.

That last line is the one non-obvious case, and it is why the guard is on `s`
rather than on `cs`.

Because a negative `P` has `|P| < 2^32`, the magnitude recovery is a **33-bit**
two's complement, not a 48-bit one:

```
  mag = res_neg ? {15'b0, (~P[32:0] + 1'b1)} : P
```

### 3.3 Normalise, round, and the one bypass

```
  p       = lead1(mag)                      mx_lead1, log depth -- see below
  norm    = mag << (47 - p)
  sig_out = norm[47:32]   guard = norm[31]   sticky |= |norm[30:0]
  e_out   = p + u - 286
```

`e_out = p + u - 286` is the whole exponent result: the product's weight is
`2^(e_ab-157)`, the leading one is at `p`, and the output bias is 127, so
`e_out = p + e_ab - 30 = p + u - 286`. Sanity: a pure multiply with
`sig_a*sig_b < 2` gives `p = 30` and `e_out = e_ab`. ✔

**One bypass, and only one:** `cs <= -18`. There the product is more than half an
ulp below the addend, so the correctly rounded result *is* the addend, and the
output is `c` verbatim. Above that (`cs = -17`) the product is still a guard bit
and the normal path handles it.

`mx_lead1` from `mx_fpacc.v` is reused unchanged — smear, isolate, encode. **Not
a search loop.** A `for` loop carrying a `found` flag over 48 bits synthesises as
a 48-deep LUT chain; that pattern cost this project ~68 MHz once already and the
reasoning is in `accumulator.md` §4.1.

### 3.4 What the FMA covers

Three in, one out, with every operand port selectable from lane input, chain
predecessor, broadcast scalar or immediate:

```
  add    a*1 + c        mul    a*b + 0        affine  a*b + c
  sub    a*1 - c        neg    a*(-1) + 0     fnma    -(a*b) + c
```

And a reduction tree built from FMA nodes rather than adders computes more than
sums — `a*a+c` is a sum of squares (variance in one pass), `a*b+c` is a dot
product. That capability is free from choosing a 3-input primitive.

---

## 4. Transcendentals

The stated priority is that `exp`, `log`, `sqrt`, `inv` — the functions NN work
actually leans on — are **fast**, not merely present. Fast here means **one pass,
II=1, same throughput as an add**.

### 4.1 Four seeds, chosen in base 2

The seeds are `exp2`, `log2`, `inv`, `rsqrt`. **Base 2, not base e**, and that
choice is worth its own paragraph:

```
  log2(x) = (E - 127) + log2(1.M)        E is an exact integer -- field slicing
  exp2(x) = 2^k * 2^f                    k = floor(x)          -- field slicing
```

Range reduction in base 2 is **free and exact**: it is a bit slice. Base e needs
`k = round(x * log2(e))` and `r = x - k*ln2`, which is two extra FMA passes
*and* introduces its own rounding error before the table is even consulted.
Nothing is lost: `ln(x) = log2(x) * ln2` and `e^x = exp2(x * log2 e)`, and in
every kernel that matters the constant folds into a neighbouring scale that
already exists — attention already multiplies by `1/sqrt(d)`, softmax already
subtracts a max, cross-entropy already has a `1/N`.

`rsqrt` is a **fourth table rather than a composition**, because every
normalisation in a transformer hits it once per row. `sqrt = x * rsqrt(x)` and
`div = a * inv(b)` are 2 passes each, which is the right place to pay.

### 4.2 The table: 32 segments, quadratic, one LUT6 per coefficient bit

For a smooth function approximated by degree-*d* minimax polynomials over
segments, reaching *n* bits needs `segments ∝ 2^(n/(d+1))`. The pure-LUT column
is there to show why it is not an option:

| target bits | pure LUT | linear (d=1) | quadratic (d=2) |
|---|---|---|---|
| 11 (FP16) | 2,048 | 64 × 2 | 16 × 3 |
| **16 (E8M15)** | **65,536** | 256 × 2 | **32 × 3** |
| 24 (FP32) | 16.8 M | 4,096 × 2 | 256 × 3 |

Quadratic at 32 segments. The **predicted** column is the minimax expression
`2·(h/4)^3·max|f'''|/6`; the **measured** column is `scripts/py/vec_tables.py`
sweeping the actual fixed-point circuit — both Horner roundings and all three
quantised coefficients — against the real function, and printing the worst case:

| function | domain | h | predicted | measured | margin over 2^-16 |
|---|---|---|---|---|---|
| `exp2(f)` | [0,1) | 2^-5 | 2^-23.2 | **2^-19.9** | 3.9 bits |
| `log2(m)` | [1,2) | 2^-5 | 2^-21.1 | **2^-19.5** | 3.5 bits |
| `inv(m)` | [1,2) | 2^-5 | 2^-20.0 | **2^-19.4** | 3.4 bits |
| `rsqrt(m)` | [1,2)+[2,4) | 2^-5 | 2^-21.7 | **2^-19.8** | 3.8 bits |

Measured is consistently ~1.5 bits worse than predicted, and that gap is the
point of measuring: **the approximation is no longer what limits these
functions — the coefficient and Horner quantisation is.** Adding segments would
therefore buy almost nothing, which is the opposite of the intuition that
"more segments = more accuracy" and the reason the count stops at 32.

That leaves 3.4 bits of margin, so the pre-rounding error is under 0.1 ulp and
the final round to a 16-bit significand dominates. Measured end to end in
`tests/vector/vec_alu_tb.v`: **0.51 ulp for exp2, 0.55 for inv and rsqrt** —
faithful, and close to correctly rounded.

**32 segments is also the fabric sweet spot.** A 32-entry constant table is a
LUT5, i.e. half a LUT6, per output bit; `rsqrt` needs both octave parities so it
is 64 entries, exactly one LUT6 per bit. The tables are generated by
`scripts/py/vec_tables.py`, which is also the bit-exact golden model.

They are **explicitly `rom_style = "distributed"`, and that is required rather
than decorative.** Synthesised on its own the case statement infers a *block
RAM* — confirmed, it came back with `ADDRARDADDR` on its output path. Inside
`vec_alu` the heuristic happens to pick LUTs (0 BRAM measured), but relying on
that is exactly what the project's rule against inferred memory primitives
forbids: a BRAM here adds a cycle of read latency and silently breaks the
pipeline's stage arithmetic. It is also wrong on the merits — every lane needs
its own copy at a *different* index on the same cycle, which no block RAM port
count can serve.

That per-lane property is the one to remember: whatever the table costs in one
lane, 128 lanes pay. §9 has the measured figure.

This does kill the "pick a narrow format to save table area" argument, though:
going from 11 bits to 16 costs 2× the table, and the table is not what makes a
lane expensive.

### 4.3 The Horner bit maps

Fixed-point layout, uniform across all four functions, chosen so that **both**
stages take the same bit slice:

```
  u   10-bit unsigned, scale 2^-10      the within-segment offset
  c2  22-bit signed,   scale 2^-28
  c1  22-bit signed,   scale 2^-24
  c0  22-bit signed,   scale 2^-20
  F   22-bit signed,   scale 2^-20      the result, 20 fraction bits
```

```
DSP-P -- Horner stage 1:  h = c2*u + c1

B(18):  00000000 uuuuuuuuuu                            u          (10b)
*
A(30):  00000 cccccccccccccccccccccc                   c2         (22b signed)
+
C(48):  0000000000000 cccccccccccccccccccccc 00000000000000        c1 << 14
=
P(48):  ................hhhhhhhhhhhhhhhhhhhhhh..............

        c2*u   <= 2^31   at weight 2^-38
        c1<<14 <= 2^35   at weight 2^-38
        h = P[35:14]     22-bit signed, weight 2^-24
```

```
DSP-M -- Horner stage 2:  F = h*u + c0        (the FMA's own DSP, reused)

B(18):  00000000 uuuuuuuuuu                            u          (10b)
*
A(30):  00000 hhhhhhhhhhhhhhhhhhhhhh                   h          (22b signed)
+
C(48):  0000000000000 cccccccccccccccccccccc 00000000000000        c0 << 14
=
P(48):  ................FFFFFFFFFFFFFFFFFFFFFF..............

        h*u    <= 2^31   at weight 2^-34
        c0<<14 <= 2^35   at weight 2^-34
        F = P[35:14]     22-bit signed, weight 2^-20
```

Both stages take `P[35:14]`. That is not a coincidence, it is what the scale
choice was for: **one slice constant, one adder-free shift, and the two stages
are the same circuit with different port sources.**

### 4.4 Assembling each result

The point of picking these four seeds is that **three of them need no normaliser
at all** — the result arrives pre-normalised and the assembly is a concatenation.

```
exp2(x)     xfix  = float2fix(x)              reuses the FMA's align shifter
            k     = xfix[int]                 exponent, exact
            idx   = xfix[frac 14:10]          segment
            u     = xfix[frac  9: 0]
            F     = poly(idx, u)   in [0,1)   2^f is ALWAYS in [1,2)
    ---->   out   = { 0, k+127, F[19:5] }     NO normaliser, NO leading-zero count

log2(x)     idx   = M[14:10]   u = M[9:0]
            F     = poly(idx, u)   in [0,1)
            val   = (E - 127) + F             s9.20 fixed point
    ---->   out   = fix2float(val)            reuses the FMA's normaliser
            (the E term is an integer ADD, not a multiply: base 2 is why)

inv(x)      idx   = M[14:10]   u = M[9:0]
            F     = poly(idx, u)   in (0.5, 1]
    ---->   out   = { S, 253 - E, F[19:4] }   1 bit of normalise, folded into
                                              the slice.  F == 2^20 (x = 1.0)
                                              is the one special case.

rsqrt(x)    K     = (E-127) >>> 1             arithmetic shift = floor, both signs
            idx   = { (E-127)[0], M[14:11] }  parity picks the octave table
            u     = M[10:0] truncated to 10
            F     = poly(idx, u)   in (0.5, 1]
    ---->   out   = { 0, 126 - K, F[19:4] }
```

`exp2` skipping the normaliser is the single biggest reason base 2 wins: `2^f`
for `f ∈ [0,1)` is in `[1,2)` **by construction**, so the significand is already
normalised and the exponent is already an integer. No leading-zero count, no
variable shift, no rounding carry path — the two most expensive stages of a
float pipeline both vanish.

### 4.5 Cost of every op that matters

| op | passes | note |
|---|---|---|
| `fma`, `add`, `mul`, `max`, `min`, `select`, `abs`, `neg` | 1 | |
| `exp2`, `log2`, `inv`, `rsqrt` | **1** | full rate, II=1 |
| `sqrt` = `x*rsqrt(x)`, `div` = `a*inv(b)` | 2 | |
| `exp` = `exp2(x*log2e)`, `ln` = `log2(x)*ln2` | 2 | 1 if the constant folds |
| **`sigmoid`** = `inv(1 + exp2(-x*log2e))` | **4** | exactly one depth-4 chain |
| `tanh` = `2*sigmoid(2x) - 1` | 5 | |
| `silu`/`swish` = `x*sigmoid(x)` | 5 | |
| `gelu` (tanh form) | ~9 | two chain traversals |
| `softmax` | 3 elementwise + 2 tree + 1 scalar | max-tree, sub+exp2, sum-tree, inv, mul |
| `layernorm`/`rmsnorm` | 1 elementwise + 2 tree + 1 scalar | `γ·r` and `β−μγr` fold into one FMA |

`sigmoid` landing on exactly four is not luck but it is a useful coincidence: it
is the strongest single piece of evidence for a chain depth of 4 in the core
(§7).

### 4.6 The comparator, and why `max` is the best value per LUT in the design

**E8M15 is sign-magnitude with the exponent above the mantissa**, so ordering by
magnitude is the *unsigned integer order of bits [22:0]* — no decode, no
unpacking. A full signed compare is that plus the sign bits:

```
  a_gt_b = (sa ^ sb) ? sb : ((a[22:0] > b[22:0]) ^ sa)
```

~20 LUTs, zero DSP, and it drives `max`, `min`, `select`, `clamp`, `relu`, and
the predicate output. Without it, a `max` is a subtract, a sign test and two
blended multiply-adds — **three passes for one max**, paid by every max
reduction, every clamp, every ReLU and the first pass of every softmax.

### 4.7 Newton refinement stays in software

`1/a: y' = y(2-ay)` is 2 FMAs and `rsqrt: y' = y(1.5-0.5ay^2)` is 3, and each
step doubles the correct bits. At a 2^-21 table the native result is already
better than the format, so refinement buys nothing in native mode — **it exists
for the extended mode**, where a 15-bit seed plus one step reaches 30 bits, i.e.
FP32-accurate `inv` in 3 passes.

It stays an instruction sequence rather than hardware. Accuracy should be a
program choice, not a synthesis choice: a softmax denominator does not need
refinement, an `rsqrt` feeding fifty layers of normalisation might.

---

## 5. The extended mode

Full FP32 (24-bit significand) needs two partial products. With three DSPs
already allocated, both variants are available from the same silicon:

| mode | DSPs | significand | passes | rate | rel. error |
|---|---|---|---|---|---|
| native | 1 (M) | 16 | 1 | **1×** | 1.5e-5 |
| extended, sequential | 1 (M) | 24 | 2 | ½× | 6.0e-8 |
| extended, parallel | 2 (M+P) | 24 | 1 | **1×** | 6.0e-8 |

The parallel form is the reason DSP-P and DSP-M must be cascade-adjacent. It is
the standard 17-bit split, and the mechanism is the `W` mux — DSP48E2 only,
DSP48E1 cannot do it:

```
  sig_b(24)  =  b_hi(7) << 17  |  b_lo(17)

DSP-M:  P1 = sig_a(24, on A) * b_lo(17, on B)                    -> PCOUT

DSP-P:  P2 = sig_a * b_hi          (Z = PCIN >> 17, W = C)
           + (P1 >> 17)            <- the cascade, right-shifted by the DSP
           + aligned_c             <- the addend, on C

  full product + addend  =  { P2, P1[16:0] }
```

Three simultaneous ALU operands — `M`, `PCIN>>17` and `C` — is exactly what the
`W` mux exists for, and it is what makes the addend free rather than a third
DSP.

**Whether to build it at all is still open.** It costs control complexity and a
wider normaliser, and nothing has yet demanded 6.0e-8. The bit map is recorded
here so the decision stays a decision.

---

## 6. The instruction set

Thirty-two opcodes in a 32-bit word, specified in
**[../isa/vector.md](../isa/vector.md)** — the sixth instruction set, and the
first in the machine that can branch. Everything above it is unchanged: the
vector core is a sixth *consumer* on the NoC, not a sixth layer.

The shape worth carrying into the rest of this document: every arithmetic
opcode is the same FMA with different operand sources, and each of the three
source selectors picks vector, scalar, **chain** or constant. The chain source
is what §7 is about.

---

## 7. The core: 16 ALUs

### 7.1 Modes are a factorisation

A mode is a choice of **W lanes × D chain depth**, W·D = 16, plus a reduction
tree:

| mode | shape | results/cycle | ALUs used | for |
|---|---|---|---|---|
| `FLAT` | 16 × 1 | 16 | 16 | elementwise at max rate |
| `D2` | 8 × 2 | 8 | 16 | mul-add-mul, scale-and-bias |
| `D4` | 4 × 4 | 4 | 16 | a whole `sigmoid` in one pass |
| `TREE` | 8+4+2+1 + acc | 16 in → 1 out | 16 | `sum max dot sumsq` |

`8+4+2+1` is a 15-ALU pipelined reduction tree consuming 16 elements per cycle;
the 16th ALU is the accumulator. **Every ALU is used in every mode, and 16 is
the smallest N for which that is true.** Below 16 the tree wastes ALUs; above
it the tree needs a fifth level and depth-4 chains stop dividing evenly.

The switchability costs a 4:1 mux on each of three operand ports per ALU.

### 7.2 Why chaining is mandatory, not an optimisation

A NoC flit is 288 bits: a 32-bit header and a **256-bit payload**
(`src/kohakunoc/noc_pkt.vh`). A vector core is a two-port endpoint like a
cluster, so **512 payload bit/cycle**, which is 32 FP16 elements. A flat
elementwise op reads two vectors and writes one — **3 elements of traffic per
result**:

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
NoC bandwidth and a depth-2 chain meet.

Halving the pass count is worth exactly as much as doubling the ALU count, and
costs far less — a `D4` chain writes no intermediate to the register file at
all.

### 7.3 The accumulator recurrence — the one thing that would silently halve throughput

The ALU is **14 cycles deep** (§13). So `acc = acc + x` has a **14-cycle
loop-carried dependency**, and a naive accumulator runs at II=14, not II=1. This
is not a detail; it is a 14× throughput cliff on every reduction, and it does
not show up until the design is built.

The fix is standard and cheap: **16 rotating partial accumulators**, one per
pipeline slot, then one final tree pass to combine them.

```
  cycle t     acc[t mod 16] += tree_out(t)      no dependency within 16 cycles
  tail        combine the 16 partials           4 more tree levels
```

Cost: 16 × 24-bit registers per core — 384 bits, nothing. A `sum` of V elements
is then `V/16` cycles plus a fixed tail, at II=1 throughout.

It also **improves** accuracy: 16 partial sums of length V/16 accumulate
rounding like `sqrt(V/16)` rather than `sqrt(V)`.

### 7.4 Throughput

At 300 MHz, `2 flop` per FMA:

| | native E8M15 | extended FP32 (§5) |
|---|---|---|
| one core (16 ALU) | 9.6 GF/s | 4.8 GF/s |
| 8 cores (128 ALU) | **76.8 GF/s** | 38.4 GF/s |

---

## 8. The register file

Striped **by lane**: lane *i* holds elements *i*, *i+16*, *i+32*, … That is what
makes the file lane-local, which is what makes it affordable — a monolithic file
serving 16 lanes × 3 ports would need 48 read ports.

```
  V0..V15    16 vector registers x VLMAX 128 elements
             -> 8 elements per lane per register, 128 entries of 24 bit per lane
  S0..S15    scalar, 24-bit, broadcast, in flops
  P0..P3     predicate, VLMAX bits
  K0..K3     constants: 0.0, 1.0, -1.0, one program-set
```

3R1W per lane, built as **three duplicated copies in distributed RAM**:
`128 deep × 24 bit × 3 ≈ 144 LUT per lane`, so **~2,300 LUT per core** — about
11% on top of the ALUs, which measured 1,249 LUT each.

`VL` is a register, not a constant, so one kernel body handles the tail without
a second code path.

**Anything that crosses lanes must say so** — that is what `VSHUF` is for. A
striped file makes lane-local access free and cross-lane access explicit, which
is the right way round: elementwise work never crosses lanes and reductions
cross them in a fixed tree.

---

## 9. L1: a scratchpad, not a cache

**No tags.** The access pattern is strided streaming produced by the AGU (§10),
which means every address is known in advance — tags would buy nothing and cost
a lookup in the load path. The CU's L1 is explicitly managed for the same
reason.

```
  2 banks x 256 words x 256 bit          double buffered, ~4 BRAM36 per core
  one 256-bit word = 16 FP16 elements = exactly one FLAT-mode cycle
```

That last line is a coincidence worth keeping: the NoC word width, the lane
count and the native element size line up so that a fill beat feeds exactly one
cycle of work. At FP32 a word is 8 elements and loads run at half rate, which is
the honest cost of the wider input format rather than a surprise.

**Filled and drained explicitly** by `VFILL`/`VDRAIN` against an address
descriptor, with `VBAR` to wait. Double buffering means a kernel fills bank 1
while computing from bank 0; the fill engine is the only thing that talks to
MAG.

L1 holds data **in its memory format** — FP16, FP32 or `ACC24` — and converts on
the read path into the ALU. Storing converted would cost 24 bits for FP16 data
and would make the buffer smaller in elements for no gain.

**When tags would pay**, and why this is deferred rather than rejected: a tagged
cache earns its keep on *reuse with unpredictable addresses*. The vector core
has neither today. If gather/scatter with computed indices ever lands here (§12
says it should not), that changes.

---

## 10. Addressing

**This is what decides whether the core is generally programmable or just fast.**

An address descriptor is `base` plus four `(stride, bound)` pairs:

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
between a kernel language and a fixed function.**

---

## 11. The matmul interface: split-K and the FP16 ceiling

The vector core's first real job, and the one that decides its dtype list.

### 11.1 How big does a dot product get?

`c_ij = sum_k a_ik b_kj`. Two regimes, and they are not close to each other:

| operand statistics | growth of `|c|` | K=256 | K=1024 | K=2048 |
|---|---|---|---|---|
| zero-mean, std σ | `~4.2 sqrt(K) σ_a σ_b` | 67σ² | 134σ² | 190σ² |
| **mean μ ≠ 0** | **`~K μ_a μ_b`** | **256μ²** | **1024μ²** | **2048μ²** |

(4.2 is the expected maximum of 65,536 standard normals.)

**The second row is the one that matters, and it is linear in K.** Every
post-ReLU activation is non-negative, so real workloads sit there, not in the
zero-mean row. FP16's largest finite value is **65,504**, so:

```
  K = 2048 overflows FP16 once  mu_a * mu_b  >  32
```

That is not an exotic condition. And note what the √K row implies in the other
direction: for zero-mean data, going K=256 → 2048 costs only `sqrt(8)` = 2.8× of
headroom, so a shape that overflows at K=2048 was already within 3× of
overflowing at K=256. **A K sweep does not gradually erode headroom; a biased
operand distribution destroys it outright.**

### 11.2 Where the overflow actually happens

Not in the accumulator. The cluster accumulates in `S1 E7 M16` with `BIAS = 63`,
whose largest value is ~2^64 = 1.8e19 — nine orders of magnitude above FP16's
ceiling. The saturation is in **`mx_fpacc_to_fp16`**, on `EMIT`:

```verilog
    else if (e16 >= 31) fp16 = {s, 5'h1E, 10'h3FF};   // saturate
```

So the value survives the entire reduction intact and is destroyed on the way
out. That is a good place for the problem to be, because it means the fix does
not touch the accumulator.

### 11.3 Splitting K does not fix the range — it fixes *where the sum lands*

Worth stating plainly because it is easy to assume otherwise: **the final sum is
the same number however K is partitioned.** Splitting K into S chunks makes each
partial roughly `1/S` of the total, but adding them back gives the same value
and the same overflow.

What splitting K enables is a **different place to finish**:

```
  today      cluster sweeps all of K  ->  EMIT converts to FP16  ->  saturates
  with VC    S clusters sweep K/S each
             each EMITs a partial in ACC24 (S1E7M16), no conversion
             vector core loads S partials, converts ACC24 -> E8M15
             sums them with FP32's exponent range
             converts ONCE, on the store
```

`ACC24 → E8M15` is **range-lossless** — E7's exponent range is strictly inside
E8's — and costs one rounding of the bottom mantissa bit, 2^-17 relative. The
sum then has E8's range, so the ceiling is 3.4e38 instead of 65,504: **a factor
of 5e33.** Only the final store can saturate, and by then the value is final and
the driver can choose FP32 if it needs to.

### 11.4 What it costs

For an output tile of `Gm × Gn = 16 × 32 = 512` elements and S partials, the
epilogue is `S × 512` loads and `(S−1) × 512` adds — at 16 elements/cycle,
about `64 S` cycles per tile on one vector core. Against a K=1024 GEMM for the
same tile at ~1,024 cycles in a cluster, an S=4 epilogue spread over 8 vector
cores is **under 5% overhead**.

### 11.5 The other reason to split K

Range is the reason it is *needed*. Occupancy is the reason it is *useful*, and
the margin is thinner than it looks:

```
  Gm=16, Gn=32  ->  output block 64 x 128       (Gm/Gn count 4-element sub-tiles)
  256 x 1024 x 256  (M x K x N)  ->  4 x 2 = 8 output tiles
```

**Eight tiles across eight clusters is exactly saturated** — enough to fill
them, with no slack for imbalance and nothing spare. So the N-only dispatch
split was the proximate defect and a 2D grid fixes it, but this shape had no
headroom to begin with.

Where K-split buys occupancy outright is skinnier still: `64 × 4096 × 64` gives
`1 × 1 = 1` tile, so seven of eight clusters get nothing at any 2D grid, and
splitting K by 8 is the only way to use them.

This reopens **[optimization.md](../optimization.md) §J3**, which shelved
cross-cluster K-split for want of a place to do the final reduction. The vector
core is that place.

---

## 12. What the vector core should *not* do

Everything data-dependent or irregular, which is what a wide SIMD array is worst
at:

- integer, bitwise and logical scalar arithmetic
- data-dependent control: loop bounds, branching, early exit
- gather / scatter with computed indices
- sort, top-k, argsort, sampling
- shape and metadata arithmetic, allocation, scheduling
- pointer chasing

The budget for that work is **thousands of operations per token, not billions**.
The moment bulk arithmetic lands there it becomes the limit.

---

## 13. The ALU, measured

`src/kohakutpu/vector/vec_alu.v` is built. Out-of-context synthesis of one lane
on `xcvu13p-fhgb2104-2L-e`:

| | measured | estimated |
|---|---|---|
| **Fmax** | **324.8 MHz** (WNS +0.147 ns at 310) | — |
| LUT | **1,249** | ~750 |
| FF | **705** | — |
| **DSP** | **3** | 3 |
| BRAM / URAM | **0** | 0 |
| latency | **14 cycles, II = 1** | — |

**The LUT estimate was 40% low and the reason is worth recording**: a 14-stage
pipeline at II=1 has to carry every control signal from where it is produced to
where it is consumed, and there are about twenty such signals. The datapath is
roughly what was predicted; the delay lines are what was not.

Extrapolated to 128 lanes: **160k LUT and 384 DSP.** On this device that is
~37% of an SLR's LUTs against 12.5% of its DSPs — so the vector core is
**fabric-bound, not DSP-bound**, and the third DSP per ALU is not the thing to
economise on. If the budget binds, the levers in order are the delay lines
(shorten the pipeline, or re-derive rather than carry), then the per-lane
coefficient ROMs, then DSP-E.

For contrast, an FP32-internal core would cost ~40% more fabric on every shifter
and adder, 4× the table, 2 DSPs minimum for the multiply — and still write FP16
at 4.9e-4. The extra bits die at the store.

### 13.1 Accuracy, measured

`tests/vector/vec_alu_tb.v`, 26,897 checks, streamed at one instruction per
cycle, against both the behavioural DSP and a real `DSP48E2`:

| | result |
|---|---|
| `mov neg abs max min select cmp` | **bit exact** |
| products and sums of powers of two | **bit exact** |
| `a*b - a*b`, `x - x` (and `x - x` is **+0**) | **bit exact** |
| `exp2(k)`, `log2(2^k)`, `inv(2^k)`, `rsqrt(2^even)` | **bit exact** |
| **FMA**, incl. the full alignment sweep | **0.500 ulp — correctly rounded** |
| `exp2` | 0.509 ulp |
| `inv` | 0.546 ulp |
| `rsqrt` | 0.549 ulp |
| `log2` | 0.64× its limit (0.99 ulp or 2^-18 absolute) |

`log2` needs both bounds and neither alone is meetable by any implementation:
near `x = 1` the result approaches zero while its absolute error does not, so
one ulp shrinks without bound; at large `|x|` the result spans decades and an
absolute bound falls far below one ulp. This is why `log1p` exists.

The alignment sweep is the load-bearing test. It walks the exponent difference
across every barrel-shifter position, which is the only way to reach `s == 0`
— where `P[47]` is a value bit rather than a sign bit — and the bypass at
`s < 0`. Random operands never land on either.

---

## 14. Decided here, and what is still open

**Decided:**

- external FP32 / FP16 only; internal **E8M15**, chosen because 16 + 32 = 48 puts
  the entire alignment range inside one DSP's C port, and because the 18-bit
  *signed* B port stops at 17 significand bits
- **no subnormals** — E8's range makes that free rather than a compromise
- **3 DSPs per ALU**: E (exponent), M (significand), P (polynomial); DSP-E is the
  one to drop if DSP columns bind
- FMA as the sole arithmetic primitive, plus a sign-magnitude comparator
- four seeds — `exp2 log2 inv rsqrt` — **base 2**, so range reduction is a bit
  slice and `exp2` needs no normaliser
- quadratic, 32 segments, coefficients in LUT6 ROMs generated together with the
  golden model
- one unidirectional 48-bit align shifter; one bypass at `cs <= -18`

**Settled by building it:**

- the lane closes **324.8 MHz** against a 300 MHz target with 3 DSPs and no BRAM
- the FMA is **correctly rounded**; the four seeds are **faithful** at ~0.55 ulp
- the vector core is **fabric-bound, not DSP-bound** (§9) — which retires the
  worry that 3 DSPs per ALU was extravagant, and redirects it at the pipeline's
  delay lines
- more table segments would buy almost nothing: quantisation, not approximation,
  is what limits the seeds (§4.2)

**Open, and needing measurement rather than argument:**

- **E8M16 if the alignment ever leaves the DSP.** M15 is right only while the
  addend rides the C port.
- **Whether latency 14 can come down.** The delay lines are the largest single
  block of the lane and they exist only because the pipeline is 14 deep. Nothing
  has yet needed the depth reduced, and II=1 is what matters for throughput —
  but it is the first place to look if 128 lanes do not fit.
- **Whether the extended mode is worth building.** §5 records the bit map; nothing
  has demanded 6.0e-8 yet.
- **Chain depth 4** — `sigmoid` fits exactly, but the right way to settle it is to
  write the kernels that matter as op chains and look at the length histogram.
- **ALUs per core against cores.** The throughput table is known; the vector-length
  distribution of real kernels is not.
- **Accumulation width for long reductions.** Multiply width and accumulate width
  are independent, and a sum over thousands of terms compounds rounding far more
  than any single chain. A 48-bit fixed-point accumulate inside the DSP's own
  post-adder is the free option if a shared-exponent block structure exists.
