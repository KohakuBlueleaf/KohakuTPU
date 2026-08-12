---
title: The accumulator
summary: A resident output tile that creates operand reuse, a float add reached once per 32 MACs, and the pacing contract that replaced three rotating banks.
tags:
  - kohakutpu
  - accumulator
  - numerics
---

# The accumulator

The fifth unit in a cluster, the only one that talks to the mesh, and the block
the whole cluster closes timing on. It is where floating point starts, where the
output tile lives, and where the block scale is finally applied.

Its design is driven by one number that is not about arithmetic at all: **the
size of the tile it holds resident decides how many mesh ports a cluster needs.**
§1 is that argument, and everything after it is consequence.

Measured figures are in [results.md](results.md) §2 and §6.2.

---

## 1. Why the buffer is working storage, not a staging pipe

The arithmetic intensity of an output tile is

```
        MACs        M*N*K          M*N
      --------  =  ---------  =  -------      <-- K cancels
      operands     M*K + K*N      M + N
```

**K cancels.** Chaining more compute units raises MACs and operand demand
together, so the cluster chain buys DSP density and shared control — it buys no
bandwidth at all. Only M and N do.

For a cluster running at 512 MAC/cycle:

```
     refill  =  512 * ( 1/M + 1/N )   elements per cycle

      output tile      refill        implication
      -----------      ----------    -----------------------------
        4  x  4        256 el/cyc    many ports, unaffordable
       16  x 16         64 el/cyc    workable
       64  x 64         16 el/cyc    about half of one port
```

So the accumulator's capacity is not a convenience. **It is the knob that sets
the cluster's port count**, and on-chip memory is far cheaper than mesh
endpoints. A cluster ships with one mesh port, and the reason it can is that the
tile is deep enough to make the operand demand fit in it.

In the instruction's own terms — `Gm` row groups by `Gn` column groups of 4x4
sub-tiles — the demand is `4(Gm + Gn)/(Gm·Gn)` words per cycle, which is 1.000 at
8x8 and 0.375 at 16x32. A port supplies one word per cycle, so 8x8 is exactly
break-even and every cycle lost to latency or arbitration comes straight off the
result. The machine ran at that break-even point for a long time and it is most
of why it measured 6–7% of its own datapath peak ([results.md](results.md) §8.1).

### 1.1 Depth is free until the primitive runs out

A sub-tile is 16 values of `ACC_MW + 8` bits — **352 bits** at the default
`ACC_MW = 14`. Against a 72-bit memory port that is `ceil(352/72) = 5`
primitives, **whatever the depth**, because width sets the primitive count.

The tile is the third of a cluster's memory shapes and shares nothing with the
other two — 352 bits against the operand RAMs' 928, and read latency 2 against
their 1 ([matmul.md](matmul.md) §4.1). Its primitive is a parameter threaded from
the mesh generator, and it is **named rather than inferred**, which is the one
convention in this area that is not a free choice.

| primitive | primitives | depth that comes with them | best power-of-two tile | intensity |
|---|---|---|---|---|
| BRAM36 | 5 | 512 sub-tiles | 16 x 32 | 21.3 |
| URAM288 | 5 | 4096 sub-tiles | 64 x 64 | 64.0 |

Measured on the 6-cluster mesh, moving the tile to URAM read **30 URAM in and
BRAM down 254 → 224** — a 1:1 exchange, because five primitives is five
primitives and only the depth behind them changes. The resident output block went
from 64x128 to 256x256 for no net memory.

**URAM costs no pipeline stage here, and that is the whole reason it is free.**
`READ_LAT = 2` is already the operating point (§4), so the align stage already
begins from a register and URAM's extra beat is a beat that was already being
taken. The vector core's L1 is the opposite case and pays for it
([vector-core.md](vector-core.md) §6).

> **A ceiling is not a shape.** `TILES = 4096` *permits* a 256x256 output block;
> it does not impose one. The compiler's `choose_tile` ranks candidates by
> intensity **discounted by padding** ([compiler.md](compiler.md) §2.1), so a
> small problem still picks a small tile out of a deep accumulator, while a
> shallow accumulator cannot offer a large tile to a problem that wants one.
> This was rejected once on the grounds that it would pad every dimension up to
> 256, and that objection was right about the wrong layer.

---

## 2. The format: `S1 E7 M<ACC_MW>`, default MW = 14

| bits | field |
|---|---|
| `[MW+7]` | sign |
| `[MW+6:MW]` | exponent, 7 bits, bias 63 |
| `[MW-1:0]` | mantissa, implicit leading 1 |

All-zero is the zero encoding.

**E7 is not the tunable.** The accumulator holds `int × scaleA × scaleB` where
the scales are E5M3 block scales; for FP16 sources that exponent sum spans
roughly −48…+30, so an E5 field overflows on ordinary data rather than on edge
cases. E7 is required, not chosen.

**The mantissa is the tunable**, and the reason to look is that results leave as
FP16 — 11 significand bits. Anything the accumulator carries beyond what survives
that conversion only has to keep rounding from compounding across the K sweep.

Measured across widths on a 32-block K sweep — 32 roundings deep, which is what a
real K=1024 matmul does — there is **a cliff between 22 and 20 bits, not between
24 and 20**. MW=14 is indistinguishable from MW=16: both land at about a third of
an FP16 ULP, so the accumulator is not what limits the answer. Below 14 the error
jumps by an order of magnitude and exceeds one FP16 ULP, meaning the accumulator
has become the dominant error source rather than the output format. The table is
[results.md](results.md) §6.2.

MW=14 is therefore the operating point: identical accuracy, less area, more
timing slack. That contradicted the expectation that FP20 and FP24 would be
equivalent — **the K depth, not the output format, sets the floor**, and a
shallower sweep would move the cliff down.

> **Naming, because three names are in circulation.** The RTL parameter is
> `ACC_MW`; the built default is 14, giving a 22-bit float, which these pages
> call **FP22**. The compiler's type system names a dtype `ACC24` for `S1E7M16`,
> the MW=16 variant. Older pages say FP24 for the same thing. They are the same
> family at different mantissa widths, and where a page says FP24 it was written
> before MW=14 became the default.

---

## 3. Applying the block scale, exactly

Two E5M3 fields arrive with every command — `sa` and `sb`, four lanes packed into
32 bits each. Their product has to be applied to the integer partial sum, and the
way it is split is what keeps the operation exact:

```
   val   = part * (m8a * m8b)              m8 = 8 + M,   product in [64, 225]
   exp   = ea[i] + eb[j] - anchor - 6
```

**The exponent halves add; the mantissas multiply.** No shifter, no rounding, no
precision lost — which is the whole reason to split it this way rather than
converting each scale to a float first. The `-6` undoes the `/64` the mantissa
product carries, and the anchor cancels the stored biases
([number-format.md](number-format.md) §4).

**The magnitude is taken inside that multiply, not after it.** `mm = m8a*m8b` is
unsigned, so the product's sign is the chain value's, and

```
   |v + r| * mm  ==  ((v ^ {W{s}}) + (s ^ r)) * mm,       s = v[W-1]
```

which is the same `(v + bit) * mm` shape plus one XOR level, and maps onto the
DSP's `(D+A)*B` mode. Taken *after* the multiply instead, it put a 30-bit two's
complement carry chain between the DSP's output register and the leading-one
search — 0.952 ns of a 3.401 ns path, and four of its twelve logic levels. Moving
it took twelve levels to nine, and the normaliser now receives an **unsigned**
magnitude with the sign travelling beside the data.

A partial sum from the chain is `8 chains x 48 bits = 384 bits`, two packed
fixed-point fields per chain. The normaliser is **22 bits wide, not 29**: a K=32
block can only reach ±131,072, which is 18 bits and a sign, and a 29-bit
leading-one search and shifter per lane, sixteen times over, would be built for
range that cannot occur.

---

## 4. One bank, and the contract that replaced three

A pipelined adder cannot close a single-cycle accumulate loop: the result of
cycle N is not available to cycle N+1. When K was the **inner** loop that was the
common case — a stream of back-to-back accumulations into one address — and it
forced three rotating banks, a two-step fold on `EMIT`, and a per-address zero
mask so `LOAD` could clear the other banks in time. All of that was machinery to
survive a recurrence.

The instruction set sweeps K **outermost** ([isa.md](isa.md) §4.1):

```
   for kb:  for g:  for h:        an address recurs every Gm*Gn cycles
```

which is 64 at the smallest useful tiling and 512 at the balanced one. There is
no tight recurrence left, so read latency is free and one bank suffices:

```
   3 banks -> 1     4x less tile memory
   EMIT fold        deleted -- EMIT just reads the tile
   zero mask        deleted -- LOAD writes the whole value
```

**What replaces them is a contract.** Consecutive commands to the same tile
address must be at least `REUSE_MIN = 5` cycles apart. Removing the banks turned
a structural guarantee into a requirement on the caller, so it is checked in
simulation rather than left implicit: a caller that sweeps K on the inside fails
loudly instead of quietly accumulating into stale data. The check caught a real
violation the moment it existed — the older single-port CU emitted a tile 2–3
cycles after the last accumulate into it.

The manager guarantees it by construction, inserting idle cycles only below
`Gm*Gn = 5` ([isa.md](isa.md) §4.4), where the cost is nothing at any tiling
worth running.

> Making the constraint checkable was worth more than the area. It converted "we
> think nothing does this" into a property the whole regression suite verifies.

**`busy` has to mean more than "a command is in the pipeline."** It must mean
"not safe to take the control mux yet", and taking the mux means issuing an
`EMIT` that reads an address an in-flight command may be about to write. So
`busy` covers the write *and* the `REUSE_MIN` gap after it. A pipeline-only
version reads correct and fails only when the whole `GEMM` is short enough that
its tail has not cleared — every sub-tile then drains as zero, which looks
exactly like a compute bug.

---

## 5. The pipeline as built

```
   stage 1    extract the two packed fields per chain, apply the scale product
   stage 2a   leading-one search -> one-hot 2^k       (log depth)
   stage 2a2  the normalising shift, as 2 DSP multiplies per lane
   stage 2b   round and assemble -> accumulator float
   stage 3    read the tile, compare exponents, align      <- reads the tile
   stage 4    add, leading-one search, shift
   stage 5    round, assemble, write back                  <- writes the tile
   stage 6    (EMIT only) convert to FP16
```

Seven stages, one accumulate per cycle sustained. The tile address is presented
at stage 2a2 because `READ_LAT = 2` means data lands two cycles later, during
stage 3. **`REUSE_MIN` did not move when stage 2a2 was added** — it counts the
tile read to the tile write, and the new stage sits ahead of the read.

Two things in this block are deliberately not constants anyone has to keep in
sync:

- **The command rides a FIFO, not a matched delay.** The cascade is ~19 cycles
  deep and that depth depends on the CU count and the skew SRLs, so the manager
  pushes one `{op, addr, sa, sb, anchor}` per issue and pops one per valid
  partial. Order is preserved by construction, so alignment survives any change
  to the chain. The FIFO is 64 deep against a ~19-deep chain and must never fill;
  both overflow and underflow have simulation checks, because a dropped command
  corrupts exactly one output element.
- **Nothing counts cycles waiting for `emit_valid`.** The CU previously waited a
  fixed 10, which silently became too short when the pipeline deepened, and the
  system wrote a zero result while every unit test still passed.

### 5.1 A variable shift is a multiply, and the DSPs were idle

Stage 2a's normaliser left-justified a 30-bit magnitude with a two-direction
shift over 30 positions, plus a second shifter to build the sticky mask and a
30:1 mux for the guard bit — sixteen copies, 21% of the block's LUTs. Meanwhile
the accumulator measured 16 DSPs beside a 256-DSP MAC array.

`x << k` is `x * 2^k`, and a DSP48E2 is an idle 27x18 multiplier. Three things
had to line up before the accumulator could take the trade:

- **The one-hot is free.** `mag << k` with `k = VW-1-msb` wants `2^k`, and the
  leading-one unit already isolates `2^msb` on the way to encoding the position.
  Reversing that bit vector is wiring.
- **`k` spans 30 positions and the B port is 18 bits.** Its low four bits pick the
  one-hot; the fifth stays in fabric as a *slice select* on the product rather
  than a second shifter.
- **The magnitude is 30 bits and the A port is 27.** Split and multiplied by the
  same one-hot, the two halves land in disjoint bit ranges, so they reassemble
  with an OR — no adder — and the significand, guard and sticky bits are constant
  slices. Two DSPs per lane.

It was worth −6.7% LUT and +15.7 MHz standing alone, and −13% and +21.0 MHz
inside the cluster ([results.md](results.md) §2.2) — a larger LUT win in context
than standing alone, because the cluster was tight enough that the tools had been
replicating logic to hold the frequency.

**What was not done, with the numbers.** The alignment shifter in stage 3 and the
normalise shifter in stage 4 take the same trade and would save roughly 1,900 LUT
more for 32 DSPs. Both sit **inside the accumulate loop**, where a DSP needs
AREG+MREG+PREG to hold 320 MHz — which pushes the tile read-to-write distance
from 3 cycles to 5 and `REUSE_MIN` from 5 to 7, a contract two other modules
re-encode by hand and a change to which tilings are legal. A real saving behind a
real decision, not an oversight.

---

## 6. Peer transfer: a matmul that spans clusters

An accumulator can send its tile to, and receive a tile from, another
accumulator. That is what lets one matmul split K across clusters without going
through memory.

```
   CHAIN reduction                        N-1 hops, one port pair per node

     ACC0 --> ACC1 --> ACC2 --> ACC3 --> result

   TREE reduction                         log2(N) hops, lower latency

     ACC0 --+
            +--> ACC01 --+
     ACC1 --+            |
                         +--> result
     ACC2 --+            |
            +--> ACC23 --+
     ACC3 --+
```

Chain maps onto neighbour routing and needs no extra buffering; tree halves the
latency and is worth it when many clusters split one K. Both are just sequences
of `SEND` and `ADD_PEER`, so **the topology is a scheduling decision, not a
hardware one**.

The value transferred is the accumulator's own float, two 256-bit granules per
sub-tile, so **the sub-tile keeps full accumulator precision across the
transfer**. That is the entire point: a K-split through memory would round to
FP16 in between, and a drain-and-refill round trip would cost the write, the read
and a quantiser pass.

Three contracts, none of them structural, all in [isa.md](isa.md) §5.3: the
receiving tile must already be open, a peer stream and a sweep may not overlap,
and `REUSE_MIN` still applies within a stream.

**Split M/N should still be the default**, because it costs nothing — each
cluster owns a disjoint output tile and writes it directly. Split K exists so
that a tall-thin or short-wide matmul can still fill the machine, and the peer
network is what makes it cheap when it is needed.

---

## 7. The one real range limit

The accumulator's own range is enormous — E7 with bias 63 reaches roughly 2^64 —
and the value survives the entire reduction intact. **It is destroyed on the way
out**, in the conversion to FP16 at stage 6, which saturates at 65,504 silently.

This matters more than it looks, because the growth is linear rather than
square-root. For `c = sum_k a_k b_k`:

| operand statistics | growth of `\|c\|` | K=256 | K=1024 | K=2048 |
|---|---|---|---|---|
| zero-mean, std σ | `~4.2 sqrt(K) σ_a σ_b` | 67σ² | 134σ² | 190σ² |
| **mean μ ≠ 0** | **`~K μ_a μ_b`** | **256μ²** | **1024μ²** | **2048μ²** |

**The second row is the one real workloads sit in** — every post-ReLU activation
is non-negative — so `K = 2048` overflows FP16 once `μ_a·μ_b > 32`. That is not
an exotic condition. And the first row implies something worth noticing in the
other direction: for zero-mean data, K=256 → 2048 costs only `sqrt(8)` = 2.8x of
headroom, so a shape that overflows at K=2048 was already within 3x of
overflowing at K=256. **A K sweep does not gradually erode headroom; a biased
operand distribution destroys it outright.**

Splitting K does not fix this — the final sum is the same number however K is
partitioned. What splitting K enables is **a different place to finish**: each
cluster emits a partial at accumulator width, and a vector core sums them in a
format with FP32's exponent range before converting once, on the store. That path
is described in [vector-core.md](vector-core.md) §7 and it is **not built**: the
conversion field exists in the vector ISA, but nothing emits accumulator-width
words into a vector core's L1 yet.

Until then, silent FP16 saturation is an open defect rather than a documented
limit, and nothing in the compiler mitigates it.

---

## 8. A feature that was built, measured, and cancelled

Worth recording, because the reasoning generalises better than the feature would
have.

Attention wants `s = (q @ k.T) * scale` and then `* log2(e)` — two full vector
passes over a score tile to multiply by two compile-time constants. The
accumulator can absorb both: the scale product reaches the stage-1 DSP on an
18-bit B port using only 8 bits, so a 9-bit constant mantissa folds into the same
multiply exactly, with the `/256` coming off the exponent beside the `/64`.

It worked, and it cost **+852 LUT and −12.7 MHz** with no extra DSP
([results.md](results.md) §2.3). Bit-identity was verified two ways.

**It was cancelled anyway, because the host can do it for nothing.** Scaling `Q`
by the constant before upload gives identical scores — `(C·Q) @ K.T = C·(Q@K.T)`
— and costs no accuracy at all: MXFP7 is block-scaled, so a uniform factor is
absorbed entirely into each block's exponent and never touches an int7
significand. Zero LUT, zero DSP, no instruction change, no quantisation contract.

Two things are worth keeping from it:

- **A width cannot be switched off at run time.** The first version made the
  scale always present with 1.0 as its neutral value. That is bit-identical and
  *not* cost-identical: it measured 10,297 LUT and 330.7 MHz with the scale at
  1.0, because widening the block-scale product widens the normaliser datapath
  from 30 to 39 bits whatever the value in it. A feature that changes a width has
  to be a compile-time parameter, not a neutral run-time value, or the build that
  does not use it still pays.
- **Ask where else the identity holds before spending fabric on it.** A constant
  factor on a matmul output can be folded into either operand, and operands are
  uploaded once while outputs are produced every cycle. The accumulator is the
  wrong end of that trade.
