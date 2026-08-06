# Accumulator: format study and status

How wide the accumulator needs to be, measured rather than assumed, and what it
costs at each width.

Design intent: [`matmul.md`](matmul.md) §6. Primitive-level: [`matmul-circuit.md`](matmul-circuit.md) §6.2.

---

## 1. What is fixed and what is tunable

The accumulator is `S1 E7 M<MW>`. **E7 is not the tunable.** It holds
`int × 2^(sa+sb)` where `sa` and `sb` are E8M0 block scales; for FP16 sources
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

Out-of-context, `xcvu13p-fhgb2104-2L-e`, 300 MHz target, 16 lanes plus a
16-entry resident tile.

`ACC_MW=14`, `DEPTH=16`, block-RAM tile:

```
   mx_acu_fp        9,945 LUT   6,232 FF   5 BRAM36   349.4 MHz
```

MW=14 (FP22) is the operating point rather than MW=16 (FP24): it measures
identically against FP64 and exact-int, costs less, and carries more slack. It
is the default in `mx_acu_fp`, `mx_cluster_cu` and `mx_matmul_cu`.

In context the whole two-port cluster closes at **322.4 MHz**, and the path it
closes on is still the ACU's align stage — [`matmul-impl.md`](matmul-impl.md)
§3. The tile is 5 BRAM36 whether `DEPTH` is 16 or 512, because width sets the
primitive count and depth is then free up to 512.

---

## 4. Timing: 84.7 MHz → 349 MHz, and what actually mattered

The accumulator started at **84.7 MHz** against a 300 MHz target and now clears
it by 16%. Every step below is measured, out-of-context, with the full 384-check
suite re-run after each. The worst relative error stayed at 3.339790e-04
throughout — bit-identical, step for step — so none of this was bought with
precision.

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
```

All three synthesise to exactly what they say, and none of them can be fixed by
adding pipeline stages around the outside.

There is a fourth, subtler member of the family in §4.2: **structure kept for a
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

---

## 5. Pipeline as built

```
   stage 1    extract the two packed fields per chain
   stage 2a   leading-one search and shift          (mx_lead1, log depth)
   stage 2b   round and assemble -> accumulator float
   stage 3    read the tile, compare exponents, align      <- reads the tile
   stage 4    add, leading-one search, shift
   stage 5    round, assemble, write back                  <- writes the tile
   stage 6    (EMIT only) convert to FP16
```

Six stages, one accumulate per cycle sustained. The tile address is presented at
stage 2a because `READ_LAT=2` means data lands two cycles later, during stage 3.

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
