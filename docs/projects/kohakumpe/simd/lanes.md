---
title: Lanes and packed elements
summary: What a lane is, how one instruction drives eight of them, how four int8 elements share a single 32-bit carry chain, the three operations that cross lanes, and why the float lane count is a separate number from the element count.
tags:
  - architecture
  - pe
  - simd
---

# Lanes and packed elements

Three ideas stack here and they are independent, which is why conflating any two
of them produces a wrong mental model of the machine.

**Lanes** are copies of the arithmetic, driven by one instruction. **Packing**
is several small numbers inside one lane's 32 bits. **Elements** are how many
values a vector register holds, which follows from the register width and the
element width and is nobody's choice.

Read them separately. They fail differently, they cost differently, and only
one and a half of them are parameters.

## What a lane is

A lane is **not** a small CPU. It has no program counter, no instruction of its
own, no branch, no address, and no way to sit an operation out. It is a slice of
a wide datapath: 32 bits of arithmetic, wired to the same 32 bits of every
register, with the control inputs shared with every other lane.

```
                 one instruction, decoded ONCE
                             |
      control  (operation, element width, shift amount, masks)
      ===============+=======+=======+=======+===============
                     |       |       |       |
                 +---v--+ +--v---+ +-v----+ +v-----+
      v1[255:0] ->|lane 0| |lane 1| |lane 2| ... |lane 7|
      v2[255:0] ->|      | |      | |      |     |      |
                 +---+--+ +--+---+ +-+----+ +-+----+
                     |       |       |        |
      vd[255:0]  <---+-------+-------+--------+
                  bits 31:0  63:32   95:64     255:224
```

Lane *L* reads bits `32L+31 .. 32L` of each source and writes the same bits of
the destination. Nothing is broadcast, nothing is muxed, no lane can see another
lane's data. That is why the array costs almost exactly `SIMD` times one lane,
and why frequency barely moves as `SIMD` grows: widening adds copies, not depth.

The control bus above is the reason this is a SIMD core and not a SIMT core. Every
lane gets the same operation because there is only one instruction being
decoded. There is no lane mask and no per-lane address, so there is nothing to
diverge and nothing to serialise — and equally, no way to express a kernel that
needs those.

### Why a lane is 32 bits and a register is 256

The register width comes from the machine around it: **256 bits is one flit, one
memory-agent entry, and one L1 line**, so a vector register is exactly the unit
that moves between DRAM and this PE in a single transaction. The vector
scratchpad's row is the same 256 bits ([memory](memory.md)).

The *lane* width comes from the accumulator. `vdot` reduces the elements inside
a lane into one int32 ([accumulator](accumulator.md)), so an accumulator holds
`SIMD` int32 values — exactly one vector register wide. A lane wider than 32 bits
would make the accumulator wider than a register and `vaccrd` a narrowing
operation instead of a move.

`SIMD_LANES` is a parameter — 2, 4 or 8 lanes, for 64, 128 or 256-bit registers —
but at anything below 8 the memory alignment above is gone, which is why the
reference is 8 and the narrower builds are priced rather than offered.

## The float lanes are a different count

Everything above is the **integer** lane array, where one lane per 32 bits of
register is forced. The float tier has no such tie, so its lane count is its own
parameter and the two numbers separate:

```
   integer lanes    SIMD_LANES             8, and fixed by the memory granule
   float ELEMENTS   2 x SIMD_LANES        16 at SIMD 8 -- a register width
                                        divided by an element width, not a
                                        choice anyone makes
   float LANES      SIMD_FLOAT_LANES      4 at the reference -- a knob
   passes           elements / lanes     4 -- the issue interval
```

A `vfmacc` at four lanes drives elements 3..0 into the lanes, then 7..4, then
11..8, then 15..12 — one pass per cycle — and **retires once**. The accumulator
is sixteen slots wide at every lane count, because it is sized in elements.

Fewer lanes cost an issue interval and buy LUT. They also **change the answers**,
because an element's accumulate chain becomes a shorter strided subset of the
partials and float addition does not associate — the full argument is in
[float](float.md#elements-lanes-and-passes).

## Packing: three ways to read 32 bits

Every arithmetic instruction carries a two-bit **element type** in its encoding,
and it says how to cut each lane up.

```
     bit  31          24 23          16 15           8 7            0
         +--------------+--------------+--------------+--------------+
   .s8   |      e3      |      e2      |      e1      |      e0      |
         +--------------+--------------+--------------+--------------+
   .s16  |             e1              |             e0              |
         +-----------------------------+-----------------------------+
   .s32  |                            e0                             |
         +-----------------------------------------------------------+
```

Elements are signed two's complement, and element 0 is at the *low* end — the
same order a little-endian byte array arrives in, so an int8 vector loaded from
memory is already packed correctly with no shuffling.

At eight lanes that is **32 int8, 16 int16, or 8 int32 per instruction**. The
element type is a field of the instruction word rather than a mode: the datapath
reads it straight off the instruction, and two adjacent instructions may use
different widths with no state to change.

## The packed adder

This is where a packed datapath is usually built wrong, so it is worth following
in full.

> **The obvious construction is the slow one, and it was built first.** Four byte
> adders per lane with the carry between them gated by the element width computes
> the right answer. Gating a carry means putting a LUT between the bytes, and **a
> LUT in the carry path stops the FPGA using its dedicated carry chain**: four
> gated bytes become seven chains in series, measured at **2.05 ns of a 4.72 ns
> critical path**.

The construction used instead gets the identical answer from **one native 32-bit
add**. Let `M` be each element's most significant bit — `0x80808080` for int8,
`0x80008000` for int16, `0x80000000` for int32:

```
   add    y = ((a & ~M) + (b & ~M)) ^ ((a ^ b) & M)
   sub    y = ((a |  M) - (b & ~M)) ^ ((a ^ ~b) & M)
```

Clearing every element's top bit in both operands makes each field at most
`0x7F`, so a field sum is at most `0xFE` and **no carry can leave an element**.
The top bits are then XOR-ed back in, which is exactly what they would have
contributed. For subtract, `a | M` sets the top bit instead: the field is then at
least `0x80`, so a subtrahend below that cannot borrow out.

Worked, on one lane of `vadd.s8`:

```
   a          8A   6D   F2   92      = -118  109  -14  -110
   b          01   14   3E   0B      =    1   20   62    11

   a & ~M     0A   6D   72   12
   b & ~M     01   14   3E   0B
   +          0B   81   B0   1D      ONE 32-bit carry chain, no carry crosses
   (a^b) & M  80   00   80   80
   ^          8B   81   30   9D      = -117 -127   48   -99
```

and −118+1 = −117, 109+20 wraps to −127, −14+62 = 48, −110+11 = −99. One adder,
one carry chain, any of three element widths chosen by a mask.

`mask` is an **input** to the lane, not derived in it: it depends only on the
element width, which is identical in every lane, so `khs_unit` builds it once in
EX and registers it. Deriving it per lane would put a mux in front of the adder
in the one cycle this module exists to keep short.

### Saturation and compare come out of the sign bits

The construction deliberately destroys the carry out of each element — which is
the bit saturation and signed comparison would normally be built from. It is
recoverable, and cheaply, from three sign bits per element:

| Quantity | From |
|---|---|
| carry **into** the element's MSB | `y_ms ^ a_ms ^ b_ms` |
| carry **out** of the element | majority of `a_ms`, `b_ms`, and that carry-in |
| signed overflow | the two carries differing |
| signed `a < b` (on the difference) | the result's sign, corrected by that overflow |

Continue the worked example at element 2, where 109 + 20 wrapped: `a_ms = 0`,
`b_ms = 0`, `y_ms = 1`. Carry-in is `1 ^ 0 ^ 0 = 1`, carry-out is
`majority(0,0,1) = 0`, they differ, so the element overflowed — and `vsadd.s8`
writes `0x7F` there instead of `0x81`. Toward the operands' sign, so a negative
overflow gives `0x80`.

The same overflow bit makes the signed compare, so `vmin`, `vmax` and the
max-reduction cost one mux each rather than a comparator array of their own.
**Every element-wise integer operation in the tier — add, subtract, saturating
add, saturating subtract, min, max, and the compare inside the reduction — comes
out of this one adder plus a mux.**

> **`e` is an argument and must stay one.** Read as a module-level net from
> inside `spread`, a continuous assignment calling it is not reliably sensitive
> to that net, so the spread keeps whatever width was current when its flag last
> changed. Measured: *a `vmin.s8` after a `vsrli.s16` spread an s8 compare with
> the s16 pattern and took four bytes from the wrong operand.*

## The packed shifter

A packed shift looks like it needs a left barrel shifter, a right barrel shifter,
and a per-element bit reversal to share one of them between the two. It needs
one rotate.

A right shift by `s` is a 32-bit rotate right by `s`. A left shift by `s` is a
rotate right by `32-s`. In both cases the bits that arrive from the wrong element
— including the ones the rotate carried around the end of the word — land exactly
where a per-element mask is already zero:

```
   x  rot>> s        bits from the element above have moved in at the top
   & keep            keep = the (EW - s) valid bits of each element -> they vanish
   | (~keep & sgn)   arithmetic shift only: each element's own sign, replicated
```

The masks depend only on the element width and the shift amount, both of which
are the same in every lane, so they are built **once for the whole unit** in EX
and registered rather than rebuilt `SIMD` times in MEM.

The bit reversal the base core's EX stage uses for the same trick is free there
because it is one 32-bit word; here it would be a three-way mux on 32 bits per
lane — **512 LUT at SIMD 8** — because the reversal has to happen *within* an
element and the element width is a runtime field.

`vsrari` is the rounding right shift — the requantise primitive, and the one
operation a plain `vsrai` gets subtly wrong. Round-half-up is
`(x >>> s) + bit s-1 of x`: an increment per element, not an addition of half an
ulp before the shift.

> **The round bit comes out of the ORIGINAL word.** The rotate lands
> `x[e·EW + s − 1]` at the top of the element *below* `e`, so picking it out of
> the rotated word reads the wrong element's bit. `rmask` selects it in place —
> one bit per element, built once per unit like the others — and an OR-reduce over
> each element puts it at that element's LSB, where it is a carry-in.

### Why the increment has its own adder

`vsrari` reads only one source vector, so the main adder's second input looked
free — and it borrowed it. That is the second-largest timing decision in the
unit, and it went the other way.

> The mux in front of the adder is in its cone **whether or not a shift is
> issued**: **~0.8 ns of a 4.72 ns path**, paid by add, min, max and every
> compare. `khs_lane` now instantiates the packed adder **twice** — once on the
> operands and once on the shifter's output — and the second one is four `CARRY8`
> and a handful of LUTs, which is cheaper than what sharing cost in delay.

> **Refusing the encoding is not removing the hardware.** With the shifter still
> instantiated, a build "without" it measured **32 LUT LARGER** than the one with
> it, because the only thing that changed was a decode term. `SIMD_SHIFT = 0` is a
> real removal; a decode change alone is not.

## When lanes must talk

Element-wise work never crosses lanes, which is what keeps the lane array cheap.
Three operations are the exceptions, and each exists because real kernels cannot
be written without it.

**`vsldw` — the slide.** Vector loads are line-aligned by contract, so a stencil
or a FIR cannot simply load "one element earlier". It loads two adjacent vectors
and slides them past each other. Lane *i* of the result takes lane `(k + i)` of
the concatenation `{vs2, vs1}`:

```
   vsldw3  vd, v1, v2

     v1 [ a0 a1 a2 a3 a4 a5 a6 a7 ]  v2 [ b0 b1 b2 b3 b4 b5 b6 b7 ]
                    |
                    +-- lane 0 of the result takes a3, lane 1 takes a4, ...

     vd [ a3 a4 a5 a6 a7 b0 b1 b2 ]
```

The index is a **lane** index, not an element index, so sliding int16 or int8
data by one element means widening it first. The slide is defined as a **rotate**
of the concatenation, so every index is defined at every width rather than
leaving a "what happens past the end" hole that the RTL and the golden model
could disagree about.

This is the one structure in the datapath whose cost grows with `SIMD` — each
output lane picks one of `2 × SIMD` inputs — and it is the reason the permute
network is a separate parameter.

> **Leave the slide an indexed select.** `idx` is three bits so `idx + i` never
> reaches `2 × SIMD` and the tool already prunes the modulo to an 8-way mux.
> Rewriting it as an explicit `if (idx == k)` loop to "help" measured
> **1,600 → 1,824 LUT**: a priority chain is not a mux.

**`vpack` — narrowing with saturation.** Two source vectors in, one out, with
signed saturation. It is how a requantise epilogue ends: int32 accumulators to
int16, or int16 results to int8.

```
   vpack.s16  vd, v1, v2       int16 -> int8, saturating

     v1 [ h15 ... h1 h0 ]   v2 [ g15 ... g1 g0 ]     16 int16 each
     vd [ g15 ... g1 g0 | h15 ... h1 h0 ]            32 int8, v1's in the low half
```

The saturation test is not two magnitude comparisons: a value fits when every
discarded bit is a copy of the sign bit that will be kept, which is one AND and
one NOR.

> **Both sources are consumed, so the pack loop runs to `VW/16` per source.**
> Running it to half that count leaves the top half of the result **undriven**,
> which reads as high-Z and then spreads X through everything downstream — a
> whole-vector failure whose first visible symptom is nowhere near this module.

**`vunpk` — widening.** The other direction, taking the low or the high half of a
vector's elements and sign-extending them. It is pure wiring plus a sign bit.

## Reductions

`vredsum` and `vredmax` collapse a vector's `SIMD` int32 lanes to one 32-bit
value in a scalar register — the last step of a dot product, and the step a loop
over accumulators ends with.

They are a **tree**, not a chain. Nodes are indexed heap-style, leaves at
`SIMD..2*SIMD-1` and node *n* combining *2n* and *2n+1*, so the depth is
structural rather than something the tool has to find: `log2(SIMD)` levels,
three at SIMD 8.

> A reduction written as a loop carrying a value between iterations synthesises
> as exactly that serial chain — `SIMD` adders deep in one stage. That is the
> shape that cost the matmul accumulator **~68 MHz** once.

> **And even log depth is too much for one cycle.** Measured once the lane adder
> stopped being the limit, the max tree became the critical path at **12 logic
> levels and 3.43 ns**, five `CARRY8` in series, because every node is a 32-bit
> signed compare and a mux. `RED_PIPE = 1` registers the level below the root,
> which halves the depth for one cycle of latency on an instruction that runs
> **once per reduction** rather than once per element — so it costs nothing a
> kernel can measure.

The `vextr` instruction is the single-lane form: lane `k` of a vector into a
scalar register, with `k` an immediate.
