# Accumulator: format study and status

How wide the accumulator needs to be, measured rather than assumed, and what it
costs at each width.

Design intent: [`matmul.md`](matmul.md) §6. Primitive-level: [`matmul-circuit.md`](matmul-circuit.md) §6.2.

---

## 1. What is fixed and what is tunable

The accumulator is `S1 E7 M<MW>`. **E7 is not the tunable.** It holds
`int × scaleA × scaleB` where the scales are E5M3 block scales; for FP16 sources
that exponent sum spans roughly −48…+30, so an E5 field overflows on ordinary
data, not on edge cases.

Mantissa is the tunable, and the reason to look is that results leave as **FP16**
— 11 significand bits. Anything the accumulator carries beyond what survives
that conversion only has to keep rounding from compounding across the K sweep.

```
   MW = 16  ->  24 bits (FP24)
   MW = 14  ->  22 bits
   MW = 12  ->  20 bits
   MW = 11  ->  19 bits   (TF32-ish, but E7 rather than E8)
   MW = 10  ->  18 bits
```

---

## 2. Precision

`tests/matmul/mx_acu_fp_tb.v`, 384 checks per width. The demanding case is a
32-block K sweep accumulated into one resident sub-tile — 32 roundings deep,
which is what a real K=1024 matmul does.

| MW | width | worst rel. error | in FP16 ULP | verdict |
|---|---|---|---|---|
| **16** | FP24 | 3.34e-4 | **0.34** | pass |
| **14** | FP22 | 3.37e-4 | **0.35** | pass |
| 12 | FP20 | 4.27e-3 | 4.4 | marginal |
| 11 | FP19 | 2.04e-3 | 2.1 | marginal |
| 10 | FP18 | 5.40e-3 | 5.5 | fails |

**There is a cliff between 22 and 20 bits, not between 24 and 20.**

MW=14 is indistinguishable from MW=16 — both land at a third of an FP16 ULP, so
the accumulator is not what limits the answer. Below 14 the error jumps by an
order of magnitude and exceeds one FP16 ULP, meaning the accumulator has become
the dominant error source rather than the output format.

> The ordering among MW=10/11/12 is not meaningful — they sit near the check's
> tolerance and the differences are data-dependent. The signal is the step
> between 14 and 12, which is ~13x.

This partly contradicts the expectation that FP20 and FP24 would be equivalent.
They are not, for a 32-block sweep: the K depth, not the output format, sets the
floor. A shallower sweep would move the cliff down.

---

## 3. Cost

Out-of-context, `xcvu13p-fhgb2104-2L-e`, 16 lanes, `ACC_MW=14`, `DEPTH=16`,
block-RAM tile, against a 320 MHz target:

```
   mx_acu_fp    343.4 MHz   9,901 LUT   5,585 FF   5 BRAM36   48 DSP
```

Sixteen of those DSPs are the block-scale multiply, one per lane and each mapped
`(D+A)*B` — §4.4 showing up in the primitive count. The other 32 are §4.5's
normalising shift, two per lane. The same run one step earlier, with that shift
still a barrel shifter in fabric, measured **327.7 MHz, 10,616 LUT, 5,928 FF,
16 DSP**.

MW=14 (FP22) is the operating point rather than MW=16 (FP24): it measures
identically against FP64 and exact-int, costs less, and carries more slack. It
is the default in `mx_acu_fp`, `mx_cluster_cu` and `mx_matmul_cu`.

> **The MW=16 comparison is not current.** MW=16 has not been synthesised since
> MW=14 became the default; its last figure was 302.3 MHz, from a 300 MHz-target
> run several steps of §4 ago. "Costs less and carries more slack" is sound on
> the evidence that chose the operating point, and it is *not* a claim about
> what FP24 would measure on today's block. The precision comparison in §2 is
> unaffected — that is a bench result, not a synthesis result.

In context the whole two-port cluster closes at **346.6 MHz**, 15,306 LUT,
17,754 FF, 5 BRAM36, 304 DSP. Run back to back on the same tree with §4.5
reverted it is 325.6 MHz and 17,629 LUT / 272 DSP, so in context that step is
**−2,323 LUT (−13%) and +21.0 MHz** — a larger LUT win than the 715 the block
shows standing alone, because the cluster was tight enough that Vivado had been
replicating logic to hold the frequency.

The ACU still sets the cluster's frequency, but on a different path: the binding
one is now `u_acu/val_r_reg` (a DSP M register) into `u_acu/b_phi_reg` (a DSP A
port) — the leading-one search feeding the shift DSPs, with nothing else in it.
The tile is 5 BRAM36 whether `DEPTH` is 16 or 512, because width sets the
primitive count and depth is then free up to 512.

> `mm_mesh` is not quoted here. Both halves of a before/after pair were run, but
> `vec_lanes.v` changed between them, and the baseline half's critical path
> landed in the vector register file rather than the ACU — it measures that work
> in flight, not this change. The `mm_mesh` run carrying §4.5 met 320 MHz at
> 324.6 MHz on an ACU path, for what a single point is worth.

All of this is out-of-context: no placement, estimated route, so every Fmax here
is an upper bound rather than a promise.

### 3.1 The tile is in URAM on the shipped tops, and `TILES` is 4096

`mx_acu_fp` has taken a `TILE_PRIM` parameter since the tile became an explicit
memory (§4.3). **`mx_cluster_node` never passed it**, so from the top of the
design it was unreachable — the module read `"block"` and no generated mesh
could say otherwise, which is why the trade below had never been measured. It is
now threaded `gen_mesh.py -> mx_cluster_cu -> mx_cluster_node -> mx_acu_fp`, the
same path `L1_PRIM` already took.

**URAM costs no pipeline stage here, and that is the whole reason it is free.**
`READ_LAT=2` is already the operating point — §4.3 attributes ~70 MHz to that
alone, because `READ_LAT=1` starts the path at the RAM's `CLKARDCLK` rather than
at a flip-flop — so the align stage already begins from a register, and URAM's
extra beat is the beat that is already being taken. The vector core's L1 is the
opposite case and pays for it: `vec_core` runs `READ_LAT=1` and `"ultra"` adds a
wait state to every `VLD` and `VDRAIN`
([vector-core.md](vector-core.md) §9).

Measured on the 6+0 mesh, `TILE_PRIM = "ultra"`, `TILES = 4096`:

| | BRAM36 | URAM288 |
|---|---|---|
| `TILE_PRIM = "block"`, `TILES = 512` | 254 | 0 |
| `TILE_PRIM = "ultra"`, `TILES = 4096` | **224** | **30** |

**5 URAM per cluster, and the BRAM fell 1:1** — 30 tiles out, 30 URAMs in. Both
counts are set by *width*: a sub-tile is 352 bits, against a 72-bit port either
way, so `ceil(352/72) = 5` primitives whichever primitive it is. What changes is
the depth that comes with them — 512 for BRAM36, 4096 for URAM288 — so the
resident output block went **512 to 4096 sub-tiles for nothing**.

What that buys is arithmetic intensity, because `gm*gn <= TILES` is the only
thing bounding it:

| `TILES` | best power-of-two tile | `2*gm*gn/(gm+gn)` | output block |
|---|---|---|---|
| 512 | 16 x 32 | 21.3 | 64 x 128 |
| **4096** | **64 x 64** | **64.0** | **256 x 256** |

> **[`../perf.md`](../perf.md) §4 rejected exactly this, and the objection was
> right about the wrong layer.** A 256x256 output block does pad every dimension
> of every problem up to 256, and that cost is real. But `TILES` is a *ceiling*,
> not a shape: `choose_tile` ranks candidates by intensity **discounted by
> padding** ([`../isa/kernel.md`](../isa/kernel.md) §1), so a small problem still
> picks a small tile out of a deep accumulator, while a shallow one cannot offer
> a large tile to a problem that wants it. The original argument treated the
> parameter as if it selected the block. The other half of the objection —
> "do not spend the URAM on matmul" — is answered by measurement: the shipped
> meshes used **0 of 320 URAM per SLR**, and 5 per cluster is not what will make
> URAM binding.

Timing was not re-measured in context. The standalone probe puts 352x4096 in
URAM at **585 MHz** (`.plan/measurements/memory-primitives.md`) against a cluster
that closes at 344, and the pipeline argument above says the seam does not move —
but URAM's clock-to-out is worse than BRAM's and the ACU is what the cluster
closes on, so treat the in-context figure as unmeasured rather than unchanged.

---

## 4. Timing: 84.7 MHz → 349 MHz, and what actually mattered

The accumulator started at **84.7 MHz** against a 300 MHz target. The fourteen
steps below took it to 349.4 MHz; §4.4 then rebuilt its front end (327.7 MHz)
and §4.5 moved the normalising shift into DSPs, so it measures **343.4 MHz**
today, clearing 300 by 14%. Every step below is measured,
out-of-context, with the full 384-check suite re-run after each. The worst
relative error stayed at 3.339790e-04 throughout — bit-identical, step for step
— so none of this was bought with precision.

| Step | Fmax | LUT | FF |
|---|---|---|---|
| unpipelined: normalise + add in one cycle | 84.7 | 13,037 | — |
| split `normalise \| add` | 129.7 | 11,787 | — |
| narrow the normaliser input 30 → 22 bits | 136.3 | 11,185 | — |
| split the adder `align \| round`, **2 banks** | 217.5 | 13,912 | — |
| move the add across the align/round seam | 208.6 | 14,344 | — |
| split the normaliser `leading-one \| assemble` | 233.7 | 13,654 | 17,497 |
| resident tile as LUTRAM, LOAD mux off the tail | 219.7 | 11,086 | 5,210 |
| split the round stage, **3 banks** | 242.4 | 11,091 | 6,724 |
| register the align-stage selects | 238.9 | 11,263 | 6,744 |
| **parallel leading-one and sticky** | 234.3 | 11,708 | 6,744 |
| **one-level operand mux, zero-ness as control** | 293.2 | 11,310 | 6,764 |
| **concatenated `{exp,mant}` compare** | 302.3 | 11,369 | 6,758 |
| explicit BRAM tile, 4 banks, `READ_LAT=1` | 241.2 | 10,469 | 6,310 |
| **explicit BRAM tile, 1 bank, `READ_LAT=2`** | **349.4** | **9,945** | **6,232** |

> The table is honest about the dead ends. Six of the fourteen steps moved Fmax
> by less than 10%, and three moved it **backwards**. The reasons are in §4.1
> (serialisation) and §4.3 (blaming the memory for the logic around it).

### 4.1 The mistake that cost eight of those steps

**Pipelining could not fix this design, because the depth was inside a single
stage, not spread across stages.** Three combinational blocks were written as
loops that carry a value between iterations:

```verilog
for (b = VW-1; b >= 0; b = b-1)
    if (!found && mag[b]) begin msb = b; found = 1'b1; end   // 22 levels

for (i = 0; i < SW; i = i+1)
    if (i < diff) lost = lost | sml[i];        // 25 levels, comparator per link
```

The `found` and `lost` carries make iteration *n* depend on iteration *n−1*.
Synthesis builds literally that: a ~25-deep LUT chain, which at this device is
most of a 3.33 ns period on its own. No seam cut anywhere else in the pipeline
can reach into a chain like this — which is exactly the observed behaviour, six
splits for +150 MHz, and then +68 MHz from fixing the loops and the muxes.

The replacements are the standard constructions:

- **Leading one** (`mx_lead1`) — smear the leading bit rightwards with
  `y |= y>>1, y>>2, y>>4 …` (log₂W levels), isolate it with `y & ~(y>>1)`, and
  encode with six balanced OR reductions. 8 levels rather than 25, at any width.
- **Sticky** — generate the mask of dropped bits with a shift running *beside*
  the alignment shift, then reduce once: `|(sml & ~(~0 << diff))`. FPGAs have no
  wide OR gate, so the reduction still costs a couple of levels, but it is a
  tree and it is off the shifter's critical path rather than behind it.
- **Magnitude compare** — `{ea, ma} >= {eb, mb}` is exactly
  `(ea > eb) || (ea == eb && ma >= mb)`, but it is one carry chain instead of a
  7-bit compare feeding a 16-bit compare feeding an AND/OR. This one step was
  worth 9 MHz and took the block from missing to meeting.

Two more that were structural rather than arithmetic:

- **Operand select was three chained 384-bit muxes** (bank, then fold, then
  LOAD). All three selects are knowable a stage early, so it became one 4:1 on a
  registered 2-bit select — a single LUT6 per bit.
- **Zero-ness was masking 384 bits of tile data** on the critical path. A bank
  that has never been written reads as zero; saying so by muxing the widest,
  latest signal in the block put a level in front of everything. It is now one
  control bit per operand travelling *beside* the data into the align stage.
  Together these two were worth **+59 MHz**.

The lesson generalises: on this datapath, *what the loop said* mattered far more
than *where the registers were*. The same class of mistake was then found one
level up, in the CU's operand buffer, where it was worth 32,292 LUTs — see
[`matmul-impl.md`](matmul-impl.md) §3.1. The common shape is **describing as
variable or sequential something that is structurally constant or parallel**:

```
   serial loop carrying a flag   ->  W-deep LUT chain
   variable part-select write    ->  barrel mux across the whole register
   nested ternaries on wide data ->  chained mux levels
   value computed where only a    ->  a full multiplier in front of a comparator
     predicate on it is used
   recomputed every cycle though  ->  that arithmetic inside the state
     decided once per instruction      machine's clock enable
```

All five synthesise to exactly what they say, and none of them can be fixed by
adding pipeline stages around the outside.

The last two were found one level up, in `mx_cluster_mgr` and `mx_cluster_cu`
rather than in the ACU — an 8×8 multiply built to answer "is it below 5?", and a
boolean recomputed every cycle although both its inputs are latched at decode.
Together they were worth 29.2 MHz on the whole cluster; §4.4 has both.

There is another, subtler member of the family in §4.2: **structure kept for a
constraint that no longer exists.** The banks were correct machinery for K-inner
and pure overhead for K-outer, and nothing failed when they became obsolete —
they just quietly cost 4× the tile memory and 70 MHz.

Two further pieces deserve explanation, because they are the transferable
pipeline structure rather than the arithmetic.

### 4.2 The banks are gone, because the loop order changed

A pipelined adder cannot close a single-cycle accumulate loop: the result of
cycle N is not available to cycle N+1. When K was the **inner** loop that was
the common case — a stream of back-to-back accumulations into one address — and
it forced three rotating banks, a two-step fold on `EMIT` to put them back
together, and a per-address zero mask so `LOAD` could clear the other banks in
time. All of that was machinery to survive a recurrence.

The ISA now sweeps K **outermost** ([`tensor-isa.md`](tensor-isa.md) §5.1):

```
   for kb:  for g:  for h:        an address recurs every Gm*Gn cycles
```

which is 64 at the smallest useful tiling and 512 at the balanced one. There is
no tight recurrence left, so read latency is free and **one bank suffices**:

```
   3 banks -> 1     4x less tile memory
   EMIT fold        deleted -- EMIT just reads the tile
   zero mask        deleted -- LOAD writes the whole value
```

What replaces them is a **contract**: consecutive commands to the same tile
address must be at least `REUSE_MIN` (5) cycles apart. It is checked in
simulation rather than left implicit, so a caller that sweeps K on the inside
fails loudly instead of quietly accumulating into stale data. That check
immediately caught the legacy `mx_matmul_cu`, which emits a tile 2–3 cycles
after the last accumulate into it; it now waits out the distance explicitly.

> Making the constraint checkable was worth more than the area. It converted "we
> think nothing does this" into a property the whole regression suite verifies.

### 4.3 The tile is a named block RAM, with its output register on

```
   inferred LUTRAM, 3 banks, async read     11,049 LUT   0 BRAM   312.3 MHz
   explicit BRAM,   4 banks, READ_LAT=1     10,469 LUT  20 BRAM   241.2 MHz
   explicit BRAM,   1 bank,  READ_LAT=2      9,945 LUT   5 BRAM   349.4 MHz
```

Two things in that middle row are worth keeping.

**`READ_LAT=1` puts the RAM array access on the critical path.** Without the
block RAM's output register the path begins at `CLKARDCLK` — about 1.2 ns of
clock-to-out — rather than at a flip-flop. That alone cost ~70 MHz.
`READ_LAT=2` enables the output register, and the align stage starts from a
register again.

**The primitive was never the problem.** The same 352-bit memory measures
**837 MHz standing alone** (`.plan/measurements/memory-primitives.md`). Anything
slower than that is this module's own logic. Blaming the RAM for the 241 MHz
result hid a loop-order question for two rounds.

### 4.4 The magnitude before the multiply, and two multiplies nobody wanted

By the time the cluster around this block had grown to its current shape it
measured **294.9 MHz** against a 310 MHz target. Three changes took it to
**325.6 MHz**, each measured on its own:

| step | cluster Fmax |
|---|---|
| starting point | 294.9 |
| magnitude taken **before** the multiply rather than after | 296.4 |
| `tiles_w = Gm*Gn` replaced by the predicate its consumer wanted | 299.9 |
| `tiles_now >= 5` decoded once per instruction | **325.6** |

**This is now the whole machine's number.** `mm_mesh` -- MAG with the memory
mover, one matmul cluster, one vector core and two routers -- measures 325.6 MHz
against a 320 MHz target, on the path
`u_acu/val_r_reg/DSP_M_DATA_INST -> b_sig_reg`. That is this block, and the
assembled mesh has converged on it exactly: the vector core reaches 336.8 and
the mover 331.8, so neither contributes any more. **The next MHz has to come
from here** (`docs/memory-mover/arch.md` §10.2).

**The magnitude was taken after the multiply.** `mx_fpacc_norm_a` computed
`mag = val[VW-1] ? (~val + 1) : val`, putting a 30-bit two's-complement carry
chain between the DSP's output register and the leading-one search — 0.952 ns of
a 3.401 ns path, and 4 of its 12 logic levels (LUT2, CARRY8, LUT1, CARRY8).

It is taken before the multiply instead, inside the DSP that is already applying
the block scale. The mantissa product `mm` is unsigned, so the product's sign is
the chain value's, and

```
   |v + r| * mm  ==  ((v ^ {W{s}}) + (s ^ r)) * mm,       s = v[W-1]
```

which is the same `(v + bit) * mm` shape the odd lanes already had, plus one XOR
level. Vivado maps it to DSP mode `(D+A)*B` with `A` the one-bit correction and
`D` the XORed value. Twelve logic levels became nine, and `mx_fpacc_norm_a` now
takes an **unsigned** magnitude, with the sign travelling beside the data.

**The other two are one mistake in two places.** `mx_cluster_mgr` computed
`tiles_w = Gm * Gn` as a full 8×8 fabric multiply, and its only consumer asked a
five-way question of the answer (`==1`, `==2`, `>=5`) to pick a pacing value.
Both operands are ≥ 1, so `Gm*Gn < 5` is exactly the shapes (1,≤4), (≤4,1) and
(2,2) — established by exhaustive enumeration over all 65,536 `(Gm,Gn)` pairs
rather than by inspection, because this is precisely the kind of equivalence
that reads as obvious and is not.

`mx_cluster_cu` then had the same multiply again as `tiles_now`, feeding one
boolean, `gemm_wide = tiles_now >= 5`, consumed only in the `S_GEMM` transition
— i.e. arriving straight into the state machine's clock enable, 13 logic levels
deep. But `gm` and `gn` are latched at decode and hold for the whole sweep, so
that boolean is a property of the **instruction**, not of the cycle. It is now
decoded once into a register beside `gm_r`/`gn_r`. This single change moved both
violating timing groups, because both were rooted in it.

**Correctness is unchanged, which is the point of measuring it.** The 2-cluster
256×256×256 run is 18,701 cycles and bit-identical to the recorded baseline —
p50 1.70e-04, max 1.00e+00, 20 of 65,536 over 1% and 4 over 10%; against the
FP64 model, p50 3.88e-03 and p99 2.48e-01. The 4-cluster 256×512×256 run is
20,647 cycles at 975.1 GFLOP/s, 79.4% of peak, matching the baseline recorded in
`scripts/py/run_matmul.py`'s own comments exactly (max 2.43e+00, 7 of 131,072 over
10%).

### 4.5 A variable shift is a multiply, and the DSPs were sitting idle

§4.4 left the block bound on `val_r -> b_sig`: the normaliser's barrel shifter.
`mx_fpacc_norm_a` left-justified a 30-bit magnitude with a two-direction shift
over 30 positions, a *second* shifter to build the sticky mask and a 30:1 mux
for the guard bit — sixteen copies, 21% of the block's LUTs.

Meanwhile the accumulator measured **9,763 LUT / 16 DSP** inside `mm_mesh` while
the 256-DSP MAC array beside it cost 2,516 LUT. `x << k` is `x * 2^k`, and a
DSP48E2 is an idle 27×18 multiplier. Measured in isolation, sixteen copies of one
such shift (a 15-bit significand in a 23-bit guarded field, shifted 0..23, with
the sticky OR of what falls off):

| 16 copies of one variable shift | LUT | FF | DSP |
|---|---|---|---|
| fabric barrel shifter | 1,200 | 704 | 0 |
| multiply by a one-hot | **288** | 496 | 16 |

Three things had to line up before the accumulator could take it:

- **The one-hot is free.** `mag << k` with `k = VW-1-msb` wants `2^k`, and
  `mx_lead1` already isolates `2^msb` on the way to encoding `pos`. Reversing
  that bit vector is wiring, so `oh` became an output of `mx_lead1`.
- **`k` spans 30 positions and the B port is 18 bits.** Its low four bits pick
  the one-hot; the fifth stays in fabric as `hi`, a *slice select* on the
  product rather than a second shifter.
- **The magnitude is 30 bits and the A port is 27.** Split at `VW-MW-1` and
  multiplied by the same one-hot, the two halves land in disjoint bit ranges, so
  `mx_fpacc_norm_p` reassembles them with an OR — no adder — and reads the
  significand, the guard bit and the sticky bits as constant slices. Two DSPs
  per lane.

The seam moved with it: stage 2a is now the search alone and a new stage 2a2
holds the DSPs. **`REUSE_MIN` did not move**, because the new stage sits ahead of
the tile read and the contract counts read to write.

| | Fmax | LUT | FF | DSP |
|---|---|---|---|---|
| `mx_acu_fp`, barrel shifter in fabric | 327.7 | 10,616 | 5,928 | 16 |
| `mx_acu_fp`, shift as a DSP multiply | **343.4** | **9,901** | **5,585** | 48 |
| `mx_cluster_cu`, in fabric | 325.6 | 17,629 | 17,782 | 272 |
| `mx_cluster_cu`, as a DSP multiply | **346.6** | **15,306** | **17,754** | 304 |

−6.7% LUT and +15.7 MHz standing alone, −13% and +21.0 MHz inside the cluster,
and the critical path leaves `val_r -> b_sig` for the tile RAM's clock-to-out
into the align stage's sticky bit. That is the first time since this block was
written that the normaliser's shifter is not the limit.

Three smaller changes went with it, all *output*-identical rather than
approximate. `mx_fpacc_align` no longer masks `bg_o`/`sh_o`/`lost_o` to zero when
an operand is zero, and `op_a` is the tile output unmasked — both cases leave
through `zero_o`/`pass_o`, which `mx_fpacc_round_b` tests before it reads the sum
path. And the rounding-carry branch in `mx_fpacc_norm_b` and `mx_fpacc_round_b`
is an exponent increment rather than an MW-bit mux, because all-ones plus one
wraps the stored fraction to zero on its own.

> **Measure LUTs unflattened when the block is timing-critical.** Those three
> are −458 LUT of 9,060 with no clock constraint, and **+307** with one: at
> WNS +0.06 ns Vivado spends LUTs replicating logic, and the replication moves
> more than the logic does. The flattened, constrained number is the one that
> ships; the unflattened one is the one that says whether the *logic* shrank.

**What was NOT done, with the numbers.** The other two barrel shifters —
`mx_fpacc_align`'s alignment shift (the block's single largest cone, 117 LUT per
lane) and `mx_fpacc_round_a`'s normalise shift (75) — take the same trade and
would save roughly 1,900 LUT more for 32 DSPs. Both sit **inside the accumulate
loop**. A DSP there needs AREG+MREG+PREG to hold 320 MHz, which pushes the tile
read-to-write distance from 3 cycles to 5 and `REUSE_MIN` from 5 to 7 — a
contract `mx_cluster_mgr` and `mx_cluster_cu` each re-encode by hand, and a
change to which tilings are legal. It is a real saving behind a real decision,
not an oversight.

### 4.6 A per-tile output scale: built, measured, CANCELLED

Attention wants `s = (q @ k.T) * scale` and then `* LOG2_E` — two full vector
passes over a `[64 x 64]` score tile to multiply by two compile-time constants.
The accumulator can absorb both: `mm = m8a*m8b` reaches the stage-1 DSP on an
**18-bit B port using only 8 bits**, so a 9-bit constant mantissa folds into the
same multiply, exactly, with the `/256` coming off the exponent beside the `/64`.

It works, and it was measured rather than argued:

| | Fmax | LUT | DSP |
|---|---|---|---|
| feature off | 343.4 | 9,821 | 48 |
| feature on | 330.7 | 10,673 | 48 |

**+852 LUT, −12.7 MHz, no DSP.** Bit-identity was verified two ways — the gate
off, and the gate on with the mantissa at 1.0 — both landing on 3.339790e-04.

**It was cancelled anyway, because the host can do it for nothing.** Scaling `Q`
by the constant before upload gives identical scores, `(C*Q) @ K.T = C*(Q@K.T)`,
and costs no accuracy at all: MXFP7 is block-scaled, so a uniform factor is
absorbed entirely into each block's exponent and never touches an int7
significand. Zero LUT, zero DSP, no ISA change, no quantisation contract.

Two things are worth keeping from it.

**A width cannot be switched off at run time.** The first version made the scale
always-present with 1.0 as its neutral value. That is bit-identical and *not*
cost-identical: it measured 10,297 LUT and 330.7 MHz with the scale at 1.0,
because widening the block-scale product widens the normaliser datapath from 30
to 39 bits whatever the value in it. A feature that changes a width has to be a
compile-time parameter, not a neutral run-time value — otherwise the legacy
build pays for it.

**Ask where else the identity holds before spending fabric on it.** A constant
factor on a matmul output can be folded into either operand, and operands are
uploaded once while outputs are produced every cycle. The accumulator is the
wrong end of that trade.

---

## 5. Pipeline as built

```
   stage 1    extract the two packed fields per chain
   stage 2a   leading-one search -> one-hot 2^k      (mx_lead1, log depth)
   stage 2a2  the normalising shift, as 2 DSP multiplies per lane
   stage 2b   round and assemble -> accumulator float
   stage 3    read the tile, compare exponents, align      <- reads the tile
   stage 4    add, leading-one search, shift
   stage 5    round, assemble, write back                  <- writes the tile
   stage 6    (EMIT only) convert to FP16
```

Seven stages, one accumulate per cycle sustained. The tile address is presented
at stage 2a2 because `READ_LAT=2` means data lands two cycles later, during
stage 3. **`REUSE_MIN` did not move when stage 2a2 was added** — it counts the
tile read to the tile write, and the new stage sits ahead of the read.

Nothing here is a fixed constant anyone has to keep in sync:

- **the ACU command rides a FIFO**, not a matched delay — the cascade is ~19
  cycles deep and that depth depends on `NTCU` and the skew SRLs, so the manager
  pushes one command per issue and pops one per `part_valid`. Order is preserved
  by construction.
- **the CU must not count cycles waiting for `emit_valid`.** It previously
  waited a fixed 10, which silently became too short when the pipeline deepened,
  and the 1×5 system wrote a zero result while every unit test still passed.

---

## 6. Method note

Every number here comes from the same bench run at different widths, checked
against **two** independent ground truths — an exact integer model and an FP64
model — with the bench asserting that those two agree before either is trusted.
That is what makes the reported error attributable to the accumulator rather
than to quantisation or to a drifting model.

Narrowing also exposed a real bug that the wide case hides: in the normaliser's
rounding-carry path the fraction was taken as `sig_r[SW:1]`, one bit too wide,
which overflows the output concatenation and pushes the sign bit out. At MW=16
nothing in the suite rounds up far enough to reach that path, so it only
appeared at MW=14. Sweeping a parameter is a test in its own right.

The timing work exposed two more that the benches could not have caught on their
own, both recorded above: the `LOAD`-then-accumulate hazard in §4.3, which every
bench hid by leaving gaps between operations, and the CU's fixed-cycle wait for
`emit_valid` in §5, which only failed once the pipeline grew. Both were found by
reasoning about the timing rather than by a failing test — the second one did
eventually fail the system bench, but silently, as a zero result.
