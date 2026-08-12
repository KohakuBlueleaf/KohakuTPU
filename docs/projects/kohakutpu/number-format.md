---
title: MXFP7
summary: An int7 significand with an E5M3 scale shared by 32 elements — why this format, what it costs, and what the anchor is for.
tags:
  - kohakutpu
  - numerics
  - mxfp7
---

# MXFP7

The element format KohakuTPU's tensor core multiplies in. A **microscaling**
format in the OCP style — one scale shared by a block of elements — with two
deliberate departures from OCP: the block is 32 along the reduction axis only,
and the scale is **E5M3** rather than E8M0.

```
   value(i,k)  =  scaleA[i] x a_int[i][k]

   scaleA[i]   =  2^(E - 20) x (1 + M/8)      field = { E[4:0], M[2:0] }
                  \____ 8 bits, the same width an E8M0 field would be ____/
```

`SBIAS = 20`, `KBLOCK = 32` and `ANCHOR = 40` are the constants; they live in
`ktpu.hw.mxfp7` on the software side and are what the RTL is written against.

Software never sees this. The host uploads FP16, results come back FP16, and
throughout these pages **"int7" means the 7-bit significand field as it appears
inside the DSP packing** — never a type a program has.

---

## 1. Why a block-scaled integer format at all

The previous design multiplied FP8 elements and summed them in a floating-point
adder tree. That tree was 84% of the tensor core: 10,656 of 12,731 LUTs for 128
MACs ([results.md](results.md) §7). Every node in it aligned, added and
normalised, because every product arrived with its own exponent.

Factor the exponent out of a whole block and that stops being true. For
`C[M,N] = A[M,K] · B[K,N]` with the scale shared **along K only**:

```
   A block = 1 row  x 32 K   ->  one E5M3 scale  sA[i]
   B block = 32 K   x 1 col  ->  one E5M3 scale  sB[j]

   C[i][j] = ( sum over k of a_int[i][k] * b_int[k][j] ) * 2^(sA[i] + sB[j])
             \_______________ exact integer ____________/   \___ constant ___/
```

The scale factor is constant across the entire block, so every product entering
the sum has the same weight. **No alignment is needed and the sum is exact.**
That is the property the whole cluster is built around, and it is why the
adder tree disappears rather than getting cheaper: there is nothing left for it
to do.

The reason the block is shared along K and not along a tile is the same
property. A scale shared across rows would not cancel out of the reduction.

### 1.1 The block size is not an independent parameter

`KBLOCK = 32` is the same number as the DSP cascade depth, and that is not a
coincidence — it is one parameter wearing two names.

A K=32 block is exactly what one cluster's cascade accumulates before anything
has to leave the DSPs ([matmul.md](matmul.md) §3). Choosing a smaller block —
say K=8, one per tensor CU — would force a rescale between every CU in the
chain and collapse the exact integer level back into floating point. Choosing a
larger one would need guard bits the packing does not have.

So the block size was not chosen for numerical reasons and then implemented. It
fell out of the DSP arithmetic, and the numerics were checked against it
afterwards.

---

## 2. Why E5M3 and not E8M0

OCP microscaling formats use a power-of-two scale: an E8M0 field, 8 bits, no
mantissa. KohakuTPU spends the same 8 bits as `{E[4:0], M[2:0]}`.

**Three mantissa bits, because a power of two wastes up to a full bit of the
significand.** With `scale = 2^e` the best available scale can only land a
block's peak somewhere in `[32, 64)` of the int7 range — where inside that
binade depends on where the peak happens to fall, so the loss is between zero
and one whole bit and varies block to block. Three mantissa bits put the peak in
`[56, 63]` every time.

Measured per element on correlated operands, relative error improves p50
**0.54% → 0.38%** and p99 **48% → 23%** ([results.md](results.md) §6.1).

**Five exponent bits, because the output is FP16.** FP16's normal range spans 30
binades; E5 covers 31, so it just fits, and E4 covers 16 and does not. The three
extra exponent bits an E8M0 field would spend buy range this datapath cannot
express anyway — a value outside FP16's range could not have been an FP16 tensor
in the first place.

**The field stays 8 bits.** Nothing about the flit format, the mesh or L1
changes; only the interpretation does.

### 2.1 What the mantissa costs

One multiply at each end, and neither is on a critical path.

- **Quantising**, `mx_quant.v` divides by the scale using an eight-entry
  reciprocal table (`round(4096 * 8 / m8)`) rather than shifting. The divisor has
  exactly eight possible values, so a divider was never needed.
- **Accumulating**, the ACU multiplies the integer partial by `m8a*m8b` and takes
  the `/64` off the exponent, which keeps it exact — see §4.

Both are amortised: the quantiser runs once per operand fetch, and the scale
multiply runs once per 32 MACs.

### 2.2 Rounding: the significand rounds to nearest, the scale rounds up

The scale rounds **up** to the smallest representable value with
`peak/scale <= 63`. Rounding it down would put the block's peak past 63 and clip
it — damaging the largest element in the block, which is the one that matters
most. Rounding up costs at most a little of the range below the peak.

Two edge cases are handled rather than ignored:

- **A block whose peak is itself subnormal** wants a scale below E5's range. It
  clamps, which degrades the block — it keeps fewer bits — where letting the
  exponent wrap would corrupt it outright.
- **Subnormal elements** are decoded properly, not flushed. Flushing would zero
  most of any block whose peak is below ~2e-3, which would read as the format
  being poor on small tensors rather than as a dropped case.

---

## 3. The operand payload, and why the element is 7 bits

A tensor CU consumes a K=8 slice of a 4-row operand per cycle: 32 elements,
plus the block's four scales riding along with them.

```
  256-bit operand payload

   255                                                  32  31           0
  +-------------------------------------------------------+--------------+
  |                32 x int7   (224 bit)                   | 4 x E5M3 (32)|
  +-------------------------------------------------------+--------------+
       element (i,k)  i = 0..3, k = 0..7                     scale per row i
```

**int7 is the width that fills the payload exactly.** 32 x 7 = 224, and 4 x 8 =
32, and the framework's flit payload is 256 bits. That is one of two independent
reasons the element is seven bits rather than eight; the other is the guard-bit
budget in [matmul.md](matmul.md) §2, which arrives at the same answer from the
DSP side.

The four scales are identical across the four K-slices of a block. Repeating
them costs 12.5% of the payload and makes **every flit self-contained** — a
response word says what it is without reference to any other, which is what lets
responses arrive out of order.

The same 256 bits reads as 16 x FP16 when the buffer holds float data.

---

## 4. The anchor

The block scales are stored with their exponents biased by `SBIAS = 20`, so a
stored field of `E` means `2^(E-20)`. When two of them meet in the accumulator,
both biases have to come off:

```
   exp  =  ea[i] + eb[j] - anchor - 6          ANCHOR = 2 * SBIAS = 40
   val  =  part * (m8a * m8b)                  m8 = 8 + M
```

**The exponent halves add; the mantissas multiply.** `(1 + Ma/8)(1 + Mb/8)` is
`(m8a * m8b) / 64` with `m8a*m8b` in `[64, 225]`, so the partial sum is
multiplied by an 8-bit integer and the `/64` comes off the exponent as the `-6`.

That is **exact**: no shifter, no rounding, and no precision lost. Converting
each scale to a float and multiplying would have cost both. The product is 8 bits
wider than the partial sum, which is what the accumulator's `VWM = VW + 8` is
for.

So the anchor is a **constant of the format, not a tunable** — the driver
imports it from `mxfp7.ANCHOR` and every `GEMM` carries it. It is a field in the
instruction only because the accumulator has no other way to be told which bias
convention its operands were stored under.

> `DRAIN` also carries an `anchor` field and it is dead: during a drain the
> cluster forces `anchor`, `sa` and `sb` to zero, because `EMIT` reads the tile
> and converts without applying any scale. The field is decoded and discarded.
> See [isa.md](isa.md) §4.3.

---

## 5. Where the conversion happens

**Not in the compute unit.** The quantiser sits on the memory-agent side of the
mesh, in the agent's transform stage on the read and upload paths.

> **This is the clearest example of the addon category in the whole project.** The
> memory agent provides a *slot*: a place on the path between DRAM and the mesh
> where an operand can be transformed as it streams past, with the request
> carrying a flag saying whether to apply it. **The transform itself is
> KohakuTPU's.** MXFP7 is this project's number format, the max-tree and the
> shift-and-round are this project's arithmetic, and a different project would
> plug something else into the same slot — or nothing, and read its operands
> through untouched.
>
> A project should expect to write one of these. What it should not have to write
> is the descriptor walk, the burst engine, the response tagging or the flag
> plumbing that surrounds it.

The reasons it goes in that slot rather than in the compute unit are structural
rather than incidental:

- Every consumer gets the dense encoding for free, and there is one
  implementation to verify rather than one per cluster.
- Putting it in the CU would put a 32-element max-tree and a shift/round per
  element in 32 places instead of one.
- It would put FP16 on the mesh, throwing away the 2.2x density the format was
  chosen for. That density — 2048 bits of FP16 in, 1024 bits of int7 plus four
  scales out — is the whole reason the encoding exists between memory and the MAC
  array.

One quantised read converts exactly one L1 entry, 4 lanes x 32 K elements:

```
   in    8 beats x 256 bit  =  4 lanes x 32 FP16     2048 bit
   out   4 flits x 256 bit  =  4 lanes x 32 int7     1024 bit  + 4 x E5M3
```

The block scale is shared along K, so **nothing can be emitted until the whole
entry has arrived**. That is why the quantiser buffers an entry rather than
streaming it, and why the read is a fixed 8-beat burst rather than a `len`-beat
one.

Element slot assignment is the only difference between an A operand and a B
operand, which is where the transpose happens: `lane*8 + (k % 8)` for A,
`(k % 8)*4 + lane` for B. One circuit serves both, so the driver stores both
operands in the same shape.

### 5.1 Or on the way in, once per tensor

The same circuit can be claimed by the upload path instead of the read path.

| | where | source in memory | cost |
|---|---|---|---|
| online | on the read path, per fetch | FP16, 256 B/entry | once **per read** |
| pre-quantised | on the upload path, as it lands | int7+E5M3, 128 B/entry | once **per tensor** |

An operand is read once per output tile it participates in, so pre-quantising is
the online cost divided by the number of passes, and it halves the bytes the
fetch path moves for good.

**Which one applies is a property of the tensor, stated on every request that
touches it** (`preq` on a `FILL`). The memory system holds no map of which
addresses are which format and must not learn one: the driver is the only party
that knows which tensors are reused enough to be worth converting once. Because
the flag is per request, a `GEMM` can read pre-quantised weights and online
activations with no extra mechanism — which is the ordinary inference case.

**The driver never constructs int7+E5M3 itself.** It marks the upload and the
hardware converts, so the format stays entirely inside the machine and the
software model in `ktpu.hw.mxfp7` exists only as a golden reference for the
bench.

---

## 6. Format at each stage

```
   DRAM / mesh       FP16 / FP32 / int8       normal dtypes, software-visible
        |
        |  quantiser: max-tree -> E5M3, shift+round -> int7
        v
   L1 (tensor CU)    int7 + E5M3              dense, feeds the MAC array at rate
        |
        |  exact integer accumulation, K = 32
        v
   cluster output    int (19 bit) + scale     exact result of one K=32 block
        |
        |  normalise once
        v
   accumulator       FP22  S1E7M14            one add per 32 MACs
        |
        v
   mesh / DRAM       FP16                     software-visible again
```

The machine as a whole is **AMP FP16-MXFP7**: operands and results in memory are
FP16, the multiply is MXFP7, the accumulate is FP22.

**So the throughput unit is FLOPS, not IOPS.** The integer datapath inside the
DSP is an implementation detail of an MXFP7 multiply — the exponent is factored
out of the block and applied once, which is exactly what makes the reduction
exact. Software never sees an integer, and the numbers it puts in and gets out
are floats. One MAC counts as 2 FLOPs, the usual convention.

---

## 7. What the format does not cover

- **Range is FP16's, not FP32's.** The scale is E5M3, spanning FP16's ~30
  binades, so quantising an FP32 tensor is bounded by FP16's range. In practice
  that costs nothing, since data outside FP16's range could not have been an FP16
  tensor either — but it is why the format's value type is FP16.
- **FP32 operands are not supported on the read path.** The honest route if they
  are ever wanted is teaching the quantiser an FP32 mode: the block-peak
  reduction works unchanged, because FP32 is sign-magnitude with the exponent
  above the mantissa and the peak is still a plain unsigned max. The cost is
  halved fetch bandwidth, 8 elements per beat instead of 16. Not free, but not a
  new mechanism either.
- **Feeding FP32 into a matmul is precision theatre regardless.** Everything
  below ~7 bits plus a block scale is discarded before the first multiply, so a
  tensor destined for a matmul should be stored FP16 by whatever produced it.
- **Results saturate on the way out.** The accumulator's own range is far wider
  than FP16's, and the conversion at `EMIT` clamps at 65,504 silently. That is a
  real limit with a real fix, and it is
  [accumulator.md](accumulator.md) §7's problem rather than the format's.
