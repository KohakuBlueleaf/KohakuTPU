---
title: SIMD PE gates
summary: The benches that must pass before a SIMD PE number is quotable, what the list does not cover, the two ways a number here can look like evidence without being any — a check count that cannot fall and a stale artifact that turns a wrong path into a pass — and why the float specials have to be pinned against the spec rather than against a workload.
tags:
  - architecture
  - pe
  - simd
  - verification
---

# SIMD PE gates

## The list

Run in this order; the cheap ones fail fastest.

```
python tests/pe/tools/rv_simd_isa_test.py     # 106 instructions, 0 disagreements
python tests/pe/tools/rv_simd_emit.py --check # 2 of 2 generated files agree

python tests/pe/tools/khs_float_vec.py
python scripts/py/xsim.py khs_float_lane      # 4000 vectors, 0 mismatches

python tests/pe/tools/rv_simd_fsfu_test.py     # 101 checks: the seed model
                                               # against vec_alu_tb section 9

python tests/pe/tools/khs_gen.py --simd 8 --float --flanes 4 --no-facc
python scripts/py/xsim.py khs_unit -d KHS_FLOAT=1 -d KHS_FLANES=4 \
                                   -d KHS_FACC=0                 # 60 checks

python tests/pe/tools/khs_gen.py --simd 8
python scripts/py/xsim.py khs_unit                                # 36 checks

python tests/pe/tools/rv_simd_gen.py --simd 8
python scripts/py/xsim.py rv_dsp              # assembled PE, 48 checks
```

`vec_alu` and `vec_lanes` are shared arithmetic, so **anything that touches
`vec_alu.v` or `khs_float_lane.v` must also run them at DEFAULT parameters** —
those two protect the vector core and the SIMT PE, which instantiate the same
files:

```
python scripts/py/xsim.py vec_alu             # 26,900 checks, 0 errors
python scripts/py/xsim.py vec_alu --model 0   # the same, on the real DSP48E2
python scripts/py/xsim.py vec_lanes           # 1,158 checks, 0 errors
```

`--model 0` swaps `vec_dsp`'s behavioural multiplier for DSP48E2 and pulls in
`glbl` and `-L unisims_ver`. **Run it for any change to `vec_alu`'s arithmetic**;
both models currently agree bit for bit, every group's worst case identical, so a
divergence would mean the DSP configuration rather than the maths.

## What the list does not cover

- Nothing here is narrowed any more, and that is recent. **`--no-fsfu` used to
  be in this list**, because the FSFU-on configuration failed 4 of 62 on the FP32
  seed corners — and `SIMD_FSFU = 1` is what synthesises, so the gate was
  excluding the shipped build. It was a golden-model fault and not the lane:
  `rv_simd_model.py`'s seed path ended in `max(min(y, 3.4e38), -3.4e38)`, so it
  could not produce an infinity for **any** input, and its `else` branches
  conflated negative with zero (`log2(-1)` and `rsqrt(-1)` returned a large
  finite where IEEE and the hardware both say NaN). `vec_alu_tb` section 9 had
  been checking those same specials directly against the RTL and passing the
  whole time. Fixed in the model; the FSFU configuration is now **60 checks, 0
  errors** and `rv_simd_fsfu_test.py` pins the model to that table so the two
  cannot drift apart again.

  **The lesson is the narrowing, not the clamp.** A gate that is trimmed until
  it passes reports the trim as success; the flag `--no-fsfu` looked like a
  configuration choice and was actually a suppressed failure.
- **The assembled-PE cycle bench does not default to the shipped writeback.**
  `rv_simd_tb.v` defines `RV_SIMD_WB` to 0 while `rv_pe.v` defaults it to 1, and
  the bench never passes `SIMD_DOTDSP`, so that one takes the shipped 1. A bare
  `xsim.py rv_dsp` is therefore `DOTDSP = 1, WB = 0`. **Pass `-d RV_SIMD_WB=1`
  for the configuration the card runs** — both columns are published in
  [performance](performance.md#what-each-feature-costs-against-the-kernel-that-uses-it),
  and the shipped one costs 1.9 % to 33.3 % of a vector kernel's cycles.

  `-d` is safe for this define. It is **not** safe for `MX_MODEL`: `xsim.py`
  appends its own `MX_MODEL={args.model}` *after* the user's defines, so
  `-d MX_MODEL=0` is silently overridden and the run reports `MODEL=1` while
  looking like it obeyed. Use `--model 0`, which also pulls in the DSP48
  primitive, `glbl` and `-L unisims_ver`.
- **`SIMD_FLOAT_LANES` used to be un-checked and now is not.** `FLANES` must
  divide `2 × SIMD`; 3 elaborated, synthesised, reported a plausible 330.4 MHz
  and failed the component bench 10 of 66, because `PASSES` truncated to 5 and
  the walk covered 15 of 16 elements. `khs_unit` now instantiates a module that
  does not exist in the illegal branch, so the build stops at elaboration:

  ```
  ERROR: [VRFC 10-2063] Module <khs_unit_requires_FLOAT_LANES_to_divide_2x_SIMD>
  not found while processing module instance <g_bad_fl.u_bad>
  ```

  A second guard covers `PASSES` dividing `NPART` when the accumulator is built.
  Neither fires at 2, 4, 8 or 16 lanes — all four still elaborate and pass.

## Numbers that look like evidence and are not

Two of these in one day, in the same benches. They are different mechanisms with
the same failure mode: **a figure that reads as proof of work done, when what it
actually measures is something else.**

### A check count is a CASE count, not a coverage count

`khs_unit_tb.v` makes **exactly six unconditional checks per case** — write-stream
compare, write count, scalar count, vector-file dump length, scratchpad dump
length, case completed. Ten cases is therefore a **floor of 60**, and every other
`checks = checks + 1` in that file sits inside a mismatch branch.

- **A passing run compares far more than it counts.** It walks the whole write
  stream instruction by instruction, then 64 vector-file words, 16 accumulator
  words and every scratchpad word, per case. None of that increments the number,
  so the count is nearly constant by construction.
- **The count can go DOWN when things get better.** The FSFU fix took it from
  `62 checks, 4 errors` to `60 checks, 0 errors`. The two "extra" checks were the
  failure reports themselves (+1 check and +1 error each); the other two errors
  came from the write-stream compare, which raises `errors` **without** raising
  `checks`. Nothing was lost — but that has to be derived from the accounting,
  not assumed because the line went green.

**Not every bench counts this way, and the difference is not visible from the
number.** `vec_alu_tb` counts unconditionally: adding three specials moved it
26,897 → 26,900 and its specials group 21 → 24, which is exactly what three new
checks should look like. So a delta there IS readable and a delta in
`khs_unit_tb` is not. Same word, two meanings — read how the bench counts before
quoting its total as progress.

### A stale artifact turns a wrong path into a pass

**A missing file is a loud failure. An OLD file is a silent one.** Three benches
in this repository hit that on the same day. Two were this PE's.

**1. `rv_dsp` was failing outright and had been for as long as the rename.**
`rv_simd_gen.py` wrote its cases to `tests/pe/build/simd/dsp%02d/` while the
renamed `rv_simd_tb.v` opened `simd%02d/`. Nothing was there to find, so the
bench read an empty instruction memory and reported `NORETIRE` — loud, and
therefore harmless once anyone ran it.

**2. Two paths in the same bench were silent.** `rv_simd_tb.v` also read
`<PE_DIR>/dsp/ix00/prog.hex` and `<PE_DIR>/dsp/ndsp.hex` — the second being the
**case count** — from a `tests/pe/build/dsp/` tree left behind by the pre-rename
generator. A guard on the count existed and was correct; it never fired, because
the stale file was readable.

**The damage was bounded from the artifacts, not from confidence.** The stale
`ndsp.hex` held `0000000f` and so does the freshly generated one — fifteen cases
either way — so the fifteen workload cases were read from the correct
`simd/simdNN/` path and their cycle counts are real. Only `ix00`, the
bench-driven vector-scratchpad case, read a stale image. After fixing both paths,
deleting `tests/pe/build/dsp/`, and re-running: **48 checks, 0 errors, and every
cycle count byte-identical.** Nothing had to be retracted.

That is the procedure worth repeating. When a stale path is found, do not
discard the results and do not keep them — **open the stale artifact, compare it
to the fresh one, and say which results the difference can and cannot have
touched.**

### The rules these leave behind

- A `$readmemh` path and the generator that fills it are one fact in two files.
  When either is renamed, grep the other; **do not go from memory.**
- Delete the superseded output tree, so a wrong path fails loudly instead of
  finding something.
- A guard that checks the *contents* of a file it found cannot catch a file it
  should never have found. Check the **path**, not just the payload.
- **Read how a bench counts before quoting its count.** A number that cannot
  fall is not the same kind of evidence as one that can.
- **A file's contents *now* are not evidence about a run from *before*.** Two
  people made this exact error in opposite files within an hour: one claimed a
  CRITICAL WARNING could not have come from an `.xdc` that had been fixed since
  the run, while the other's own comment in that same file still described a
  guard the first had already removed. Neither instance is remarkable; the pair
  is, because it shows the error is structural rather than careless. The run log
  named the file and the line, and that was the better evidence all along.
- **A true observation is not yet a rule, and generalising it takes a second
  measurement.** Every claim overturned in a day had this shape — a sound
  reading of one case, promoted before a second case had been looked at:

  | the observation, correct | the rule, false | the second case |
  |---|---|---|
  | `khs_unit_tb`'s check count is a case count | "a check count is a case count" | `vec_alu_tb` counts unconditionally; its deltas *are* readable |
  | `rebuilt` beats `none` by 647 LUT | "the flatten gap is 647 LUT" | at the shipped `DOTDSP = 1` it is **243** — the 647 was all DSP inference for `sum_r` |
  | duty writes timed out, so a clock could not be retuned over JTAG | "that build is unusable" | a different revision's acceptance run had already retuned all four wizards by that method |

  **The second case is usually already in your data**; it does not get found by
  thinking harder about the first. The flatten gap needed one more synthesis at
  the other setting. The check count needed reading a second testbench's
  accounting. The clock one needed noticing that the log rolls across builds, so
  the timeouts belonged to an older revision than the conclusion drawn from
  them — and the evidence that would have refuted it was sitting in a passing
  run nobody re-read. When a finding is about to become a rule, name the second
  case that would falsify it and go and look; if there is none available, say
  "true of X" rather than the general form.

## DECODE WITHOUT DATAPATH — a named defect class, four instances

**A feature can be decoded, wired, parameterised, priced in LUT, and still not
exist.** Four confirmed instances in this repository, three of them found on one
day in three unrelated subsystems, so this is a pattern and not an accident.

| where | what decodes | what is missing | what the hardware does |
|---|---|---|---|
| **`SIMD_FCVT`** | six `vfcvt` forms; `m_is_fcvt` and `m_fcvt_op` are registered | **no branch in the `vres` result mux** — both registers are assigned and never read | writes the **integer lane output** |
| **`SIMD_FACC`** | the whole accumulator group | **`.op` left unconnected** on `khs_unit:1177`'s four `khs_float_lane`s | tied to 0 = `OP_MOV`, so it passes `a` through instead of multiply-accumulating |
| station bus | `s_awburst` / `s_arburst`, sampled on `sb_nmu` | the flit has **no burst-type field**; `sb_nsu` hardwires `m_awburst = 2'b01` | WRAP and FIXED execute as **INCR**, silently |
| SIMT `HAS_MASK` / `HAS_IPDOM` | `tmc`, `split`, `join` | `unbuilt` faults on `HAS_SHFL`/`HAS_FLT`/`HAS_FSFU` but **omits these two** | a generate branch with no datapath returns a plausible non-answer |

### The detection rule

**Follow the signal to the RESULT, not from the instruction.** A decode is easy
to read and easy to believe: the opcode is named, the register is written, the
fault checks are wired, the parameter appears in a price list. None of that
touches the datapath.

- For a register that decodes an instruction, **grep it and count the reads.**
  Declared + assigned + *nothing* is the signature. `m_is_fcvt` and `m_fcvt_op`
  both have exactly two occurrences.
- **A dead decode register is not automatically a bug** — `m_is_falu` and
  `m_is_fsfu` are also assigned and never read, but their function is fully
  covered by `m_is_fel` and `m_fop`, so they are dead flops the tool removes.
  The question is whether the *instruction* has a datapath, not whether the
  *signal* has a reader.
- **The synthesis log is evidence, and one filter finds this whole class.**
  `port 'op' of module 'khs_float_lane' is unconnected` names the FACC bug
  outright, and it had been in every log for as long as the feature existed.
  **Grep `[Synth 8-7071]` and keep only the INPUTS**: an unconnected *output* is
  ordinary — nobody read a status flag — while an unconnected *input* is tied to
  0 and silently becomes a legal-looking value, here opcode 0, `OP_MOV`. Worth
  running on any tier before pricing it. The same filter over the SIMT build
  returns only outputs, so that tier is clean.
- **So is the area column.** Those FACC lanes synthesise at ~256 LUT against
  ~1,150 for the elementwise ones. A full FMA lane cannot be 256 LUT, and the
  discrepancy is the missing datapath showing up as a number.

### Why no bench catches these — and one of them is a harness defect in its own right

**A bench built from the decode inherits the decode's blind spot.** The
generator, the golden model and the RTL were all written from one instruction
table, so a feature missing from the *datapath* is missing from all three
consistently and they agree with each other about nothing being wrong.

**`khs_gen.py`'s `NOT_BUILT` exclusion hides `SIMD_FCVT` by construction.** It
excludes all six `vfcvt` forms with the stated reason *"the unit faults on
these"* — which is **true at `SIMD_FCVT = 0` and false at `SIMD_FCVT = 1`, the
exact configuration the exclusion then conceals.** At 0 the exclusion is correct
and unnecessary; at 1 the instructions become legal, return the integer lane
output, and are still never emitted. **That is a test-harness defect and it
belongs beside the RTL one**, because it is the reason the RTL one survived: a
generator that skips an instruction can never disagree with a datapath that
lacks it.

The same shape as [the narrowed gate](#what-the-list-does-not-cover) and
[pinning the specials](#why-the-specials-have-to-be-pinned-exactly): **the cases
a workload never supplies are the cases nothing checks.** Kernel coverage cannot
find an unimplemented instruction, because no kernel issues it — which is
generally *why* it was never implemented.

That is the same shape as [the narrowed gate](#what-the-list-does-not-cover) and
as [pinning the specials](#why-the-specials-have-to-be-pinned-exactly): **the
cases a workload never supplies are the cases nothing checks.** Kernel coverage
cannot find an unimplemented instruction, because no kernel issues it — that is
*why* it was never implemented.

## Why the specials have to be pinned exactly

`rsqrt(-0)` returned **+inf** where IEEE requires **-inf**, for as long as the
seed existed. `vec_alu_tb` section 9 tested `inv(+inf)` but not `inv(-inf)`,
`exp2(-0)` or `rsqrt(-0)`, so nothing pinned it — and **no kernel in the SIMD
library issues `vfrsqrt` at all**, so no amount of workload evidence could ever
have caught it. It took reading `vec_alu.v` against the specification, and the
tell was internal: `OP_INV` derived the same sign from `raD_s` and got
`inv(-0) = -inf` right while `OP_RSQRT` hardcoded `1'b0` twenty lines later. Two
seeds contradicting each other on one input class is not a decision anyone made.

The general point: **a special case is exactly the input a workload never
supplies.** Coverage from kernels is coverage of the middle of the range. The
edges only get pinned by a table written against the spec, which is what section
9 is for — and every gap in that table is a case where the RTL is matched by
somebody *reading* it, which is how a golden model comes to encode a bug as
correct. All three gaps above are now closed.
