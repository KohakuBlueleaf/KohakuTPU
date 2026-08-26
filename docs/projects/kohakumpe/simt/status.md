---
title: SIMT PE — build status
summary: What exists, what has been run, and what is not built — as of a stated date, with the command that produces each verdict. Nothing is listed as built that has not been run.
tags:
  - architecture
  - pe
  - rv32
  - gpu
  - simt
  - status
---

# SIMT PE — build status

> **Kind: none — this page reports build status of parts labelled elsewhere.**
> Nothing here is a design surface. Its discipline — nothing listed as built
> that has not been run, each verdict naming the command that produces it — is a
> convention worth copying.

**As of 2026-08-26.** This page is dated by its nature: it separates what is in
the RTL and has been run from what is proposed and has not. A thing appears
under *built* when it exists and a named command exercises it; a thing that is
planned appears under [not built](#not-built) and nowhere else.

If a statement elsewhere in these pages disagrees with this one about whether
something exists, this page is the one to trust, and the RTL is the one to trust
over both.

## The configuration of record

`kht_pe` at **`LANES` 8, `WAVES` 16, eight binary32 units, RV32M**, with the
active mask, the divergence stack, the subgroup shuffle and the banked shared
memory all built. `FLANES` and `FSFU_UNITS` default to **0** in the RTL, so a
build that does not pass them gets a machine with no float tier — every figure
of record is at a nonzero `FLANES`, and a figure that does not name its
`FLANES` is not a result.

## Built and run

### Arithmetic

| | state |
|---|---|
| the per-thread integer ALU array, ten RV32I operations | built |
| **RV32M** — `mul`, `mulh`, `mulhsu`, `mulhu`, one 33×33 signed product per lane | built, `simt_mul.s` |
| divide and remainder | **not built** — they fault, deliberately |
| **the float tier** — `FLANES` binary32 fused multiply-add units | built, `simt_fwalk.s` / `simt_f32.s` |
| **the four seeds** — `FSFU_UNITS` units carrying `exp2`, `log2`, `rcp`, `rsqrt` | built |
| `FLANES < LANES` and `FSFU_UNITS < FLANES` | **built** — a pass walk, placed by the register file's per-lane write enable |
| int ↔ float conversion | **not built**, deliberately — [isa](isa.md) |
| a float accumulator | **not built**, and not wanted here — that structure is the [SIMD PE](../simd/float.md)'s |

One 33×33 signed multiply serves all four RV32M forms: only the extension bits
differ and the low half does not depend on them, so the sign variants are three
different *readings* of one product rather than three multipliers. It is padded
to the float tier's exact latency so both retire through one write port with no
arbitration, and it reuses the same per-wave pending bit rather than needing a
second mechanism.

### Divergence, scheduling and memory

| gate | what it adds | state |
|---|---|---|
| wave-indexed storage | many resident wave contexts | built |
| the active mask, `tmc` | predication as a write enable per bank | built |
| `split` / `join`, the bounded stack, the overflow fault | structured divergence | built, `simt_diverge.s`, `simt_nested.s` |
| the banked shared memory and its conflict resolver | divergent local addressing | built, `simt_lds.s` |
| the wave scheduler | waves genuinely **issuing**, not merely stored | built, `simt_waves.s`, `simt_chain.s` |
| the subgroup butterfly, at `SHFL_UNITS` width | `shflxor`, `bcast` | built, `simt_shfl.s` |
| the coalescer | one gather becomes one request when lanes agree | **not built** |
| multiple outstanding misses | more than one miss in flight | **not built** |

**Waves issue.** Each wave has its own fetch pointer, round-robin picks a live
wave every cycle, and the hazard compares waves — so a dependency between
*different* waves does not exist. The kick's operation field is the wave count,
clamped to what the build carries, so a launch needs no new protocol word.

Two witnesses, and they say different things:

- a dispatch where wave *w* writes its own slice is correct only if every wave
  ran, none collided and none was killed by another's exit. It scales close to
  linearly, because it is memory-bound on the serial load/store walk.
- a **20-deep dependency chain** is the witness that shows what interleaving
  buys: it stalls every instruction with one wave and none with two —
  **1.90 cycles per instruction against 0.90**, a 2.1× on the dependent section
  against a theoretical 2.0× for removing a one-cycle distance-1 hazard. The
  bubbles fill exactly as the design says they should.

**Wave contexts hide arithmetic latency, not memory latency.** One instruction
is in flight at a time, so a cache miss holds the whole front end. Until a
stalled wave can step aside, more waves buy hazard-free issue and nothing else.

### The banked shared memory, on hardware

| access | banks touched | passes |
|---|---|---|
| lane *i* → word *i* (conflict-free) | 8 distinct | **1** |
| lane *i* → word *7−i* (reversed) | 8 distinct | **1** |
| lane *i* → word 8*i* (worst case) | all one bank | **8** |

The reversed case is the one that proves the **return crossbar** — lane 0 must
take bank 7's word rather than always taking bank 0. The same shader, the same
correct answer, with the banked path off and on, differs only in the request
count: **48 against 34**, which is `8 × 6` against `1 + 1 + 8 + 8 + 8 + 8`
exactly. That is the witness as a **controlled difference** — one parameter, one
shader, one number — rather than an argument about what a resolver ought to do.

Fewer banks is a width like any other: more conflicts, more passes, the same
answer. At one bank every access serialises, which is what a one-bank memory
means, and forward progress is unchanged because the resolver still serves the
lowest outstanding lane.

### The shaders

Each runs through the real router, the real memory agent and an AXI RAM, and the
resulting DRAM is compared against the golden model exactly. **Divergence works
on hardware**, not only in the model.

Fourteen shaders, run in **34 cases** — several shaders run more than once, at a
different width or wave count, because that is what makes "the ISA knows no unit
count" a test rather than a claim: one image, one golden memory, only the
generic changes.

| shader | exercises |
|---|---|
| `simt_smoke.s` | the kick argument, `vlaneid`, RV32I per lane, a lane-linear store |
| `simt_diverge.s` | `split`/`join`: odd and even lanes take different paths and reconverge |
| `simt_nested.s` | a split inside a split — two stack pairs, the phase bit toggling at both levels |
| `simt_gather.s` | a real gather: a per-lane base load across five 32-byte lines |
| `simt_isa.s` | execution coverage — one result per instruction form, every scalar ALU form, every subgroup path, reductions under a non-trivial mask and over negative data |
| `simt_valu.s` | **the per-thread ALU itself.** Every other shader reaches only a handful of its operations, so without this the lane datapaths are nine tenths untested |
| `simt_lds.s` | the banked shared memory, at both ends of its range and at four bank counts including none |
| `simt_shfl.s` | the butterfly — every stage in turn, full reversal, `bcast`, a lane whose source is masked off, and four shuffle widths |
| `simt_waves.s` | a real dispatch, 1 and 16 waves |
| `simt_chain.s` | a 20-deep dependency chain, at 1 wave and 2 — the scheduler's witness |
| `simt_fault.s` | the region fault: a per-lane access to an unmapped region halts with the right cause at the faulting program counter |
| `simt_f32.s` | the format witnesses — an operand only binary32's exponent range holds, and a mantissa bit only its significand keeps |
| `simt_fwalk.s` | **the pass walk.** Per-lane *distinct* float operands, so a build whose units serve the wrong threads is a wrong word rather than a slow pass — every other float shader has uniform operands and would pass a crossed placement. Run at four unit counts and at 1 wave and 16 |
| `simt_mul.s` | RV32M's sign corners — `mulh`, `mulhu` and `mulhsu` are three different high halves of the same two bit patterns — and one row with **no float tier at all**, because `mul` does not depend on one |

**One wave is the worst case for the float tier, not the easy one**: with nothing
else runnable the tier's latency is exposed rather than hidden, so a dependent
chain that is right at one wave is right at any occupancy. Both are run.

The whole set runs from one command, which is the only way to get one verdict:

```
python tests/pe/tools/rv_simt_suite.py --gates
```

**It is serial by construction**, because the simulator names its build
directory after the bench and two concurrent runs destroy each other's work
area — which surfaces as a random shader failing, or as a permission error,
rather than as a collision.

### The RTL

| file | what it is |
|---|---|
| `kht_valu.v` | the per-lane integer ALU array. It has no multiplier — that is `kht_imul`, beside it, not inside it |
| `kht_imul.v` | RV32M: one 33×33 signed multiply per lane, padded to the float tier's latency |
| `kht_fpu.v` | `FLANES` binary32 units, `FSFU_UNITS` of them seed-capable, and the pass walk |
| `kht_vregfile.v` | the wave-indexed per-thread register file, primitive selectable, with a third read port when float units are built |
| `kht_unit.v` | the register file, active mask, divergence stack, lane arrays, writeback, and the shadow pipe both multi-cycle units retire through |
| `kht_lds.v` | the banked shared memory, its conflict resolver and its sequencer |
| `kht_predec.v` | decode, moved onto the image-load **write** path — a 60-bit control word per instruction |
| `kht_ctrl.vh` | the control word's bit layout, included by both the producer and the consumer |
| `kht_core.v` | the pipeline: per-wave program counters, the scalar half and its writeback stage, the lane-serialising load/store unit, the pending bits, halt-and-flush |
| `kht_pe.v` | the assembled unit: windows, predecode window, kick, completion, L1, fabric port |
| `generated/kht_isa.vh` | the decode header — **generated** from the field table; a hand edit fails a test |

### The toolchain

| file | what it is | state |
|---|---|---|
| `rv_simt_isa.py` | **the field table** — the single source for every consumer | **106 instructions**: 98 on custom-2, 8 on custom-3 |
| `rv_simt_model.py` | the golden model: waves, masks, divergence, coalescing, float, RV32M | built |
| `rv_simt_emit.py` | renders the RTL decode header; `--check` fails on drift | built |
| `rv_simt_asm.py` | the assembler extension and the disassembler | built |
| `rv_simt_isa_test.py` | encode/decode round-trip over the whole table | built |
| `rv_simt_check.py` | the model's own behaviour: divergence, coalescing, subgroup operations | built |
| `rv_simt_run.py` | assemble → model → image → simulate → compare | built |
| `rv_simt_suite.py` | every shader through the bench, one verdict | built |

RV32M is **not** in the field table and should not be: `mul` and its three high
halves are ordinary RISC-V, so they live in the base assembler alongside every
other RV32I opcode. That is what "no new opcode major was spent" looks like in
the toolchain, and it is why the 106 above is the custom space only.

## Not built

Nothing below has been built or measured. It is listed so the gap between the
plan and the machine is visible rather than implied.

**The coalescer.** One gather should become one request when the lanes agree.
The model, the ISA and the bench witness all define coalescing at **32-byte
line** granularity — but the shared L1's processor-side read port is 32 bits,
and that is a deliberate decision in a shared component: a line is walked as
eight 32-bit words on the array's second port, and eight cycles against a DRAM
round trip is free where a 256-bit processor-side read is not.

Against a 32-bit port a line-granular coalescer buys **nothing** — eight lanes
on five lines still need eight word reads, because the cache already coalesces
the *fills* by itself. Delivering the promised reduction requires a wide read
path contained to this PE, plus a per-lane word crossbar. **That crossbar is the
coalescer's headline number**, which is the reason to keep it out of the shared
L1: widening the shared cache would smear the cost of coalescing across classes
that do not coalesce, and understate it.

The request counter is reported **now**, before the optimisation exists, so the
improvement will be a measured change rather than a number that appears from
nothing. Today the ratio is the lane count by construction.

**Multiple outstanding misses.** The scheduler interleaves *issue*, but one
instruction is in flight at a time and a miss holds the whole front end.

**The workgroup barrier.** `bar` **encodes** and nothing reads it, so it retires
as a no-op. It is the one unbuilt thing in this ISA that does not fault, which
makes it the one that can produce a wrong answer with no witness. With one wave
per workgroup the no-op happens to be correct.

**Divide and remainder.** Deliberately not built: the encodings stay illegal and
fault. Divide-by-a-constant strength-reduces to `mulhu`.

**A graphics API path.** SPIR-V to this ISA is a designed path with no
implementation anywhere in `src/` or `compiler/`. Nothing here runs a shader
written in a shading language today.

## What is measured, and what is not

**Every published resource figure for this PE predates the current float tier**,
which was rebuilt from an E8M15 datapath with two operand formats into a
binary32-only one, and predates the removal of the separate float gate and the
per-thread multiplier count. [unit-counts](../unit-counts.md) is the price list
and names, per table, the tree each row was taken on; [ladder](ladder.md) is the
method and the gate-by-gate deltas.

**No frequency figure for this PE is a closed-timing result.** They are
out-of-context synthesis estimates, they are the optimistic end, and this
repository has measured a module lose 0.740 ns between synthesis and routing.

Three rules that make a figure from this PE quotable at all:

**Name the top.** A ladder whose top is one submodule **cannot see a path that
leaves it**. Synthesising the enclosing core for the first time found it at
**71.7 MHz** while the unit inside it closed at 324 — the cross-lane reduction
was written as a serial chain, 44 logic levels, and it lives in the core where
no submodule row ever looked. **No frequency claim for this PE may be taken from
a `kht_unit` row**, including the reassuring ones.

**Name the flatten.** `-flatten_hierarchy none` is **not the ship**: nothing
sets the setting on the ship's synthesis run, so it takes Vivado's default,
`rebuilt`. Measured on the assembled PE at the same target, `none` read
**636 LUT high**, because a preserved boundary cannot trim an unread output port
or fold a constant across a module edge. `none` is what makes a per-block row
attributable and it stays the diagnostic; a row quoted against a budget must be
`rebuilt`.

**Name the arithmetic tier.** An integer-only figure and a float-capable figure
are different machines, and so are a PE with and without the multiplier. A total
quoted without naming which lane array it stands on is not a result.

## How to reproduce

```
# the ISA, end to end
python tests/pe/tools/rv_simt_isa_test.py
python tests/pe/tools/rv_simt_check.py
python tests/pe/tools/rv_simt_emit.py --check

# the machine -- every shader, one verdict, gates included.  SERIAL: do not run
# two of these at once, and do not run one beside a bare rv_simt_run.py.
python tests/pe/tools/rv_simt_suite.py --gates

# a subset, by shader short name
python tests/pe/tools/rv_simt_suite.py --only smoke diverge

# or one shader at a time
python tests/pe/tools/rv_simt_run.py tests/pe/prog/simt_smoke.s  --arg 0x80000000 --dram zero
python tests/pe/tools/rv_simt_run.py tests/pe/prog/simt_gather.s --arg 0x80000000 --dram ramp
python tests/pe/tools/rv_simt_run.py tests/pe/prog/simt_fwalk.s  --arg 0x80000000 --dram zero --launch 16
python tests/pe/tools/rv_simt_run.py tests/pe/prog/simt_mul.s    --arg 0x80000000 --dram zero --launch 16

# the same shader at a narrower float width -- same golden memory, more cycles
python tests/pe/tools/rv_simt_run.py tests/pe/prog/simt_fwalk.s  --arg 0x80000000 --dram zero -d KHT_FLANES=2
```

Out-of-context synthesis is driven by `scripts/tcl/ooc_simt_pe.tcl`, one script
for every configuration because the configurations are generics on one module;
`scripts/py/ooc_sweep.py` and `scripts/py/khs_sweep.py` run a campaign of them.
**Every generic must be read back off the `synth_design` command line in the run
log**: a knob that is parsed but not applied produces a row that varies in its
tag and not in its netlist.
