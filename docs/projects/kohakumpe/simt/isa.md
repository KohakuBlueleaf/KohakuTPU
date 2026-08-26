---
title: SIMT PE — instruction set
summary: The register-class rule that is the whole design, the two custom opcode majors and their six groups, the float tier and RV32M, divergence, subgroup ops, the three addressing tiers, and the one field table all four consumers are generated from.
tags:
  - architecture
  - pe
  - gpu
  - simt
  - isa
---

# The instruction set

> **Kind: Yours throughout — one project's instruction set.** The register-class
> rule, the opcode majors, divergence, subgroup ops and the addressing tiers are
> this project's own encoding inside the payload the framework hands it.
> Generating four consumers from one field table is this project's tooling
> convention, and a good one, but it is not framework machinery.

**106 instructions in the custom space: 98 on custom-2, 8 on custom-3** — plus
the RV32I base, which is the per-thread half, and the four RV32M multiplies,
which ride RV32I's own encoding rather than the custom space.

| Group | funct3 | Count | What |
|---|---:|---:|---|
| `SALU` | custom-2, 0 | 10 | scalar ALU, register-register |
| `SMOV` | custom-2, 1 | 2 | `s2v`, `rdctl` |
| `DIV` | custom-2, 2 | 4 | `split`, `join`, `tmc`, `bar` |
| `SUB` | custom-2, 3 | 10 | subgroup: shuffle, broadcast, ballot, five reductions, `vreadfirst`, `vlaneid` |
| `VMEM` | custom-2, 4 | 64 | scalar base + vector offset, six op stems × widths × four scales |
| **`FLT`** | **custom-2, 5** | **8** | **`vfma`, `vfmul`, `vfadd`, `vfsub`, and the four seeds `vfexp2`, `vflog2`, `vfrcp`, `vfrsqrt`** |
| — | custom-3, 0–7 | 8 | scalar ALU immediate, and the two uniform branches |
| *(RV32M)* | *OP major, `funct7 = 0000001`* | *4* | *`mul`, `mulh`, `mulhsu`, `mulhu` — **not** in the custom table or its count* |

funct3 6 and 7 on custom-2 are reserved and fault.

## The register-class rule, which is the whole design

```
RV32I opcode space addresses the PER-THREAD (vector) file.
The scalar file and all control flow live in the custom space.
```

So `add x5, x3, x4` is, **for every active lane**, `x5[lane] = x3[lane] +
x4[lane]`.

This keeps shader code in ordinary encodings and preserves the pure-SIMT
property: `x1`–`x31` *are* the per-lane registers. No new register class exists
in the base ISA, so no compiler fork is needed to allocate one.

This is AMD GCN's SALU/VALU split **with the polarity inverted**. On GCN the
vector side is the addition; here the base ISA slot is spent on the per-thread
file, so the *scalar* side is what the custom space adds. A shader is mostly
per-thread work, so the per-thread half gets the cheap encoding.

## Two opcode majors, and why both

```
custom-2  0x5B   R-type groups: funct3 names the group, funct7 the operation
custom-3  0x7B   I-type groups: funct3 names ONE instruction, imm is 12 bits
```

An I-type layout has no funct7, so an I-type group holds exactly **one**
instruction — the SIMD tier hit this wall and spent a whole funct3 on `vld` and
another on `vst`. Splitting R from I across two majors buys eight of each
instead of eight in total.

custom-0 and custom-1 are left alone. A GPU build carries no SIMD tier, so they
would be free — but leaving them untouched means a hypothetical PE carrying both
never has to renumber either set. [opcode-map](../../../arch/cpu/rv32-pe/opcode-map.md) is the
authority.

## Arithmetic: the float tier and RV32M

Both are **built**. The arithmetic is **inherited from the
[SIMD tier](../simd/float.md) and never forked** — every float unit is one
`rv_fpu` and a seed unit carries a `khs_fp32_sfu` beside it, which is what keeps
the cost identity in [ladder](ladder.md#what-this-means-for-g0) meaningful.

| group | encoding | what |
|---|---|---|
| `FLT` | custom-2 `funct3 = 5`, funct7 0–7 | `funct7[2]` selects the seed half; `funct7[1:0]` the operation within it |
| RV32M | the **existing** register-register group, `funct7 = 0000001` | `mul`, `mulh`, `mulhsu`, `mulhu` |

**No new opcode major was spent on either.** RV32M sits at its standard RISC-V
encoding inside the group that already existed, because all four custom majors
are spoken for; the float tier fits in a `funct3` slot custom-2 already had
spare.

### One format, and it is not encoded

```
   IEEE binary32 in   ->   binary32 compute   ->   binary32 out
```

Binary32 is the only compute type. A thread is a whole 32-bit slot, so there is
no format bit in the encoding, no conversion at either edge and no reserved half
of a register — one element fills `vreg[31:0]` and that is the whole layout.
Denormals flush to sign-preserved zero on input and output.

A build with `FLANES = 0` faults on all eight `FLT` encodings; a build with
`FSFU_UNITS = 0` faults on the four seeds and keeps the other four.

### What the eight operations are, and what they read

```
   vfma   vd, vs1, vs2       vd = vs1 * vs2 + vd      THREE reads, vd is a source
   vfmul  vd, vs1, vs2       vd = vs1 * vs2
   vfadd  vd, vs1, vs2       vd = vs1 + vs2
   vfsub  vd, vs1, vs2       vd = vs1 - vs2

   vfexp2 vd, vs1            the four seeds, one operand each. Newton
   vflog2 vd, vs1            refinement is an instruction sequence,
   vfrcp  vd, vs1            deliberately: 1/a is two multiply-adds and
   vfrsqrt vd, vs1           rsqrt is three
```

One datapath serves the first four. `vfadd` is the unit with its multiplier
forced to 1.0 and `vfmul` is the unit with its addend forced to 0.0; `vfsub`
inverts `vs2`'s **sign bit** rather than subtracting, because negating a float
is one bit. There is never a second adder or a second multiplier.

`vfma`'s third read is the **destination**, so it needs no new instruction field
— but it does mean `rd` is a source and is compared as one by the hazard logic,
and it means the vector register file carries a third mirrored read port
whenever float units are built.

### Both multi-cycle units share one latency, and that is deliberate

```
   float    ALAT cycles, one instruction per cycle
            ALAT = 6 with no seed units, 10 with them
   RV32M    the same, always: 3 real stages plus a pad to ALAT
```

The multiplier is padded to the float tier's exact latency so the two retire
through **one** write port with no arbitration: two results can only want that
port on the same cycle if they were issued on the same cycle, which cannot
happen because one instruction issues per cycle. A per-wave pending bit blocks
the issuing wave for both. See
[microarchitecture](microarchitecture.md#the-float-tier-and-the-multiplier-one-shadow-pipe-two-producers).

**There is no int ↔ float conversion instruction on this PE**, deliberately. It
would mean instantiating a converter this core does not otherwise need, and it
is not required: a float bit pattern *is* an integer, so constants come from a
scalar immediate broadcast and real data comes from memory, which is where a
shader's floats come from anyway. The [SIMD PE](../simd/float.md) has the
converter group, because its vector unit already has a use for one.

## What has no encoding, deliberately

Each of these is an absence with a reason, not an omission.

**No divide or remainder.** `funct3` `100`–`111` in the RV32M group stay illegal
and fault. Divide is a long sequential unit or a large combinational one, neither
of which suits a barrel-scheduled pipeline whose whole invariant is a fixed
latency; and divide-by-a-constant strength-reduces to `mulhu`.

**No atomics.** The A extension's major is not in the legal opcode set at all, so
an `amo*` word raises an illegal-instruction fault rather than being decoded into
something adjacent.

**No gather or scatter opcode.** An ordinary RV32I `lw` whose base register
differs per lane **is** a gather; the coalescer sits under the ordinary load
path. The `vmem` group is not a gather — it is the *uniform-base* case, handed
to the coalescer instead of rediscovered by it.

**RV32I conditional branches are reserved and illegal in shader code.** A branch
reading a masked per-thread condition into a single PC is undefined. Uniform
control uses `sbeqz`/`sbnez` on the scalar file; divergent control uses
`split`/`join`. *"Legal only when the compiler proved it uniform"* is an
unfalsifiable contract across four consumers, so **the encoding refuses
instead** and the hardware raises an illegal-instruction fault. `jal` and `jalr`
stay as ordinary RV32I and are wave-wide: a call is uniform by construction.

**No `tex`.** Address math is integer, the fetch is an ordinary load, filtering
is FMAs. There is no sampler to invoke, and reserving an encoding for a block
that may never exist is four files carrying a promise.

## Divergence

```
split vs1     diverge on the per-lane predicate in vs1 (non-zero = true)
join          pop: restore the saved mask, resume at the popping join's pc+4
tmc  ss1      the active mask <- ss1[LANES-1:0]; a mask of zero retires the wave
bar  ss1, ss2 workgroup barrier ss1 across ss2 waves
```

**A `split` pushes two entries and a `join` pops one.** The resume PC is always
the popping join's own `pc+4`, so a stack entry is `LANES` bits and carries no
PC — exact for structured control flow, which SPIR-V guarantees by naming a
merge block for every selection and loop.

> **A depth of D therefore permits D/2 nested levels, not D−1.**
> It is stated wherever the depth is — in the table, in the generated header, in
> the model and in the RTL — because the two ways of counting differ by a factor
> of two and both look reasonable.

Overflow is a **fault**. Not a wrap, not a mask merge, not a truncation — a
masked-off lane that silently reactivates is a wrong answer with no witness.
Underflow — a `join` on an empty stack, with the phase bit clear — is the same
fault.

`bar` is workgroup scope only: one workgroup is one PE, so only local barriers
exist, and the scope bit that would select a global barrier is reserved and
unimplemented.

> **`bar` ENCODES but does not EXECUTE, and it does not fault either.**
> `kht_predec` sets `C_BAR`, and nothing in `kht_core` or in the golden model
> reads it — so a `bar` retires as a no-op. Every other unbuilt thing in this
> ISA raises a fault — `shflxor` at `SHFL_UNITS = 0`, a float operation at
> `FLANES = 0`, a seed at `FSFU_UNITS = 0` — which is the rule this instruction
> is the single exception to. **Do not write a shader that relies on it**: with
> one wave per
> workgroup the no-op happens to be correct, and with more than one it is a race
> with no witness.

## Getting a value from the per-thread side to the scalar side

Three instructions that do different things rather than overlapping:

```
vreadfirst sd, vs1     the LOWEST ACTIVE lane's value.  One lane, selected.
ballot     sd, vs1     one bit per lane.                A predicate, across lanes.
redux*     sd, vs1     add/max/min/and/or.              A reduction, across lanes.
```

`vreadfirst` is what makes a **memory-resident uniform** reachable. `rdctl`
cannot read memory and the scalar ALU only computes from scalars, so without it
the only path is `redux.or` over a vector known to be uniform — three butterfly
passes to move one word, an idiom rather than a design.

It is fully defined under a mask because it names the **lowest active** lane,
never lane 0, which may be masked off. In the RTL that is structural rather than
ordered: the selection tree prefers its left subtree at every level, so the
lowest active lane wins by construction.

**An all-zero active mask is not a defined case.** The scheduler must never issue
a wave whose mask is zero. `kht_unit` asserts on it rather than reasoning that it
cannot happen.

`vlaneid` deserves its own note: a lane has no other way to learn which lane it
is, and *every* per-thread address ultimately derives from it. It costs no read
port and no storage, because the value is a constant per lane — a mux in front of
the write.

> **`shflxor` and `bcast` are built** on the subgroup butterfly —
> `log2(LANES)` conditional swaps, with a lane whose source is masked off
> reading its own value. `SHFL_UNITS` is a width like any other: below full
> width the network becomes a per-output-lane select taking more passes, and at
> **0** both encodings fault rather than writing the ALU result and returning a
> plausible wrong answer.

## The three addressing tiers

```
form                          hardware knows            requests
lane-linear  (vlin/vsin)      everything, at decode      ALWAYS 1
uniform base (vl/vs)          the high bits are equal    1..LANES
RV32I lw/sw, per-lane base    nothing                    1..LANES
```

**A uniform base does not imply contiguous offsets.** `s[ss1] + (v[vs2] <<
scale)` with arbitrary per-lane offsets is still a scatter and still needs the
full leader/follower pass. What the uniform-base form buys is narrower and real:
the coalescer compares **offset fields** rather than full computed addresses, and
it knows the high bits cannot differ. The compare is narrower, **never skipped**
— including when `ss1` is `s0`, which is legal and degenerates to a pure vector
address.

The lane-linear form is the one that is genuinely free: no vector operand at
all, addresses known at decode, one request by construction. It is also the
commonest access a shader makes — lane *i* reads `A + 4i`, eight lanes, one
32-byte line, one request.

Two semantics pinned so the RTL and the model cannot drift:

* **Offsets are signed.** Negative strides are real.
* **`s + (v << scale)` wraps at 32 bits**, defined, rather than being undefined.

### The funct7 packing

The vmem group packs three things into funct7, so the datapath **slices** it
rather than comparing against one constant per encoding:

```
funct7 = op<<4 | scale<<2 | width

op 0 vl     load, sign-extended         op 3 vlin   lane-linear load, signed
op 1 vlu    load, zero-extended         op 4 vlinu  lane-linear load, unsigned
op 2 vs     store                       op 5 vsin   lane-linear store
```

`op >= 3` is exactly the lane-linear predicate, which is one comparator. Widths
are `b`/`h`/`w`; scales are 0–3. `vlu`/`vlinu` at word width are not encoded —
a full word has nothing to extend.

## The control slots

`rdctl sd, cidx` reads **launch and dispatch state**: workgroup id, grid
dimensions, workgroup base and count, the shader and descriptor base pointers,
the deadline. That is about fifteen of the thirty-two.

**Bulk constants do not go here.** A uniform buffer arrives the way a real GPU
does it — a base pointer in a slot, and the data read from memory with
`vreadfirst` to bring one word to the scalar side.

Slots the hardware implements today:

| `cidx` | Reads |
|---:|---|
| 0 | the kick argument |
| 1 | the core id — `{POS_Y, POS_X}` |
| 2 | the cycle counter |
| 3 | the retired-instruction counter |
| 4 | outstanding writes |
| 5 | the current wave id — **the only way a wave learns which of the dispatch it is**, and every per-wave address derives from it |
| other | zero |

## One field table, four consumers

`tests/pe/tools/rv_simt_isa.py` is the table. Everything else is generated from
it or checked against it:

```
                      rv_simt_isa.py
                    (the field table)
                            |
        +---------+---------+---------+------------+
        |         |                   |            |
   assembler   golden model     kht_isa.vh    disassembler
 rv_simt_asm.py rv_simt_model.py  (GENERATED)   rv_simt_asm.py
```

`python tests/pe/tools/rv_simt_emit.py --check` **regenerates and compares**, so a
hand edit to `kht_isa.vh` fails a test instead of silently disagreeing with the
assembler and the model. `rv_simt_isa_test.py` round-trips the whole table
(**427 checks**) and `rv_simt_check.py` exercises the model's behaviour (32
checks) — and its expectations are **computed, never typed**, because typed
expectations were wrong twice while the model was right both times.

**RV32M is not in this table, and that is correct.** `mul` and its three high
halves are ordinary RISC-V, so they live in the base assembler (`rv_asm.py`)
beside every other RV32I opcode rather than in the GPU field table — which is
exactly what "no new opcode major was spent" means in practice. The 106-instruction
count above is the custom space only.

> **An encoding test proves nothing about execution.** An instruction can
> round-trip perfectly through every consumer, set a write enable, and have no
> datapath behind it; a reduction can have a datapath and the wrong identity for
> an inactive lane. Neither is visible to a table test.
> `tests/pe/prog/simt_isa.s` is the answer: it runs each instruction on the RTL
> and stores the result where the golden model is compared against it. The
> detection rule for the whole class is in
> [the SIMD PE's gates](../simd/gates.md#decode-without-datapath).

There is deliberately **no C intrinsic header**, unlike the SIMD tier's fourth
consumer: these programs are shaders through a frontend, not C through GCC, and
a `.insn` macro whose semantics are *"for each active lane"* has no meaning in a
C expression.

### Assembler note

`rv_simt_asm.py` accepts only an explicit `sN` for a scalar operand and refuses
ABI aliases. `s0` in RISC-V ABI naming is `x8` — a per-thread register here — so
accepting the alias would silently assemble the wrong register class.
