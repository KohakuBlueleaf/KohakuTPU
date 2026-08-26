---
title: SIMD PE gates
summary: The benches that must pass before a SIMD PE number is quotable, what the list does not cover, and four ways a figure here can read as evidence without being any.
tags:
  - architecture
  - pe
  - simd
  - verification
---

# SIMD PE gates

> **Kind: Convention, and free.** Which benches must pass before a number is
> quotable is this project's own evidence standard, and nothing in the framework
> enforces it. It is worth copying rather than obeying; the general form is in
> [workflow/measure](../../../workflow/measure.md).

A number from this PE is quotable when the list below passes. This page is that
list, what it does **not** cover, and the four failure modes that produce a
figure which looks like evidence and is not — each of which has been paid for at
least once in this repository.

## The list

Run in this order; the cheap ones fail fastest.

```
python tests/pe/tools/rv_simd_isa_test.py     # every instruction, 0 disagreements
python tests/pe/tools/rv_simd_emit.py --check # the generated files agree
python tests/pe/tools/khs_seed_emit.py --check --report

python tests/pe/tools/rv_fpu_vec.py tests/pe/build/fpu
python scripts/py/xsim.py rv_fpu              # the multiply-add, on the bits

python tests/pe/tools/khs_sfu_vec.py tests/pe/build/fpu
python scripts/py/xsim.py khs_sfu             # the four seeds, on the bits

python tests/pe/tools/khs_facc_vec.py \
    tests/pe/build/khd/fp32/facc_ops.txt tests/pe/build/khd/fp32/facc_exp.txt
python scripts/py/xsim.py khs_facc
python scripts/py/xsim.py khs_ffold

python tests/pe/tools/khs_run.py --float --fcvt-units 8 --fsfu 2
python tests/pe/tools/khs_run.py                 # the integer half alone

python tests/pe/tools/rv_simd_gen.py --simd 8
python scripts/py/xsim.py rv_dsp              # the assembled PE
```

`rv_fpu.v` is the **framework's**, so anything that touches it must also run the
SIMT PE, whose float units instantiate the same file:

```
python tests/pe/tools/rv_simt_suite.py --gates
```

## What the list does not cover

- **`khs_run.py` with no arguments builds the integer half only.** The float
  row is a superset — its stream carries the integer cases too — so both rows
  are in the list and the float one is not optional.
- **A component bench does not default to the shipped writeback.**
  `rv_pe` defaults `SIMD_WB` to 1 and `khs_unit`'s own `WB_STAGE` default is 0,
  so a bare run of the assembled-PE bench is not the configuration the card
  runs. Pass the writeback explicitly, and publish both columns rather than one.
- **No SIMD feature has shipping-workload evidence, because there is no
  compiler path to this PE.** Nothing under `compiler/` references the SIMD PE
  or any of its instructions. Its only kernel library is
  `tests/pe/tools/rv_simd_kernels.py`, which exists to exercise the RTL.
- **The float tier has no kernel evidence at all.** That library contains zero
  float instructions, and neither does anything else in the repository. The
  integer features each have a paired kernel representing real work; the float
  tier has a component bench and a golden model and no workload. Any statement
  that the float tier is validated means *validated against a model*, and
  whether this PE has a float workload at all is a compiler question, not an
  RTL one.

## Four ways a number reads as evidence and is not

### A gate that is narrowed reports the trim as a pass

A flag that looks like a configuration choice can be a suppressed failure. A
`--no-<feature>` flag excluded the seed-capable configuration from this list
while the seed-capable configuration was the one that synthesised — so the gate
was passing by excluding the shipped build.

**Suspect the model before the hardware.** In that instance the defect was the
golden model's: its seed path ended in a clamp to the largest finite value, so
it could not produce an infinity for any input, and its fallthrough conflated a
negative operand with zero. The hardware was right and the reference was wrong.

**The lesson is the narrowing, not the clamp.** When a gate is trimmed until it
passes, the trim is what the green line is reporting.

### A check count can be a case count

A bench that makes a fixed number of unconditional checks per case has a check
count that is nearly constant by construction, and every other increment sits
inside a mismatch branch. Such a count **can go down when things get better**:
fixing a defect removes the failure reports that were themselves counted.

A passing run of that bench compares far more than it counts — the whole write
stream instruction by instruction, then the vector file, the accumulator and
every scratchpad word, per case — and none of that increments the number.

**Not every bench counts this way, and the difference is not visible from the
number.** A bench that counts unconditionally moves by exactly the number of
checks added. Same word, two meanings: **read how a bench counts before quoting
its total as progress.**

### A stale artifact turns a wrong path into a pass

**A missing file is a loud failure. An old file is a silent one.** A generator
that writes to one directory and a bench that reads from another produce a loud
`NORETIRE` when nothing is there — and a silent pass when a superseded tree from
before a rename still is. A guard that checks the *contents* of a file it found
cannot catch a file it should never have found.

Three rules follow, and all three are cheap:

- a `$readmemh` path and the generator that fills it are one fact in two files.
  When either is renamed, grep the other; **do not go from memory**;
- **delete the superseded output tree**, so a wrong path fails loudly instead of
  finding something;
- **check the path, not just the payload.**

When a stale path is found, do not discard the results and do not keep them:
**open the stale artifact, compare it to the fresh one, and say which results
the difference can and cannot have touched.**

### Decode without datapath

**A feature can be decoded, wired, parameterised and priced in LUT, and still
not exist.** The instruction is named in the table, a register is written, the
fault checks are wired, and the parameter appears in a cost report — and none of
that touches the datapath. Two instances of it have been fixed on this PE: a
converter group whose registered decode signals had no branch in the result mux,
so its instructions wrote the *integer* lane's output; and an accumulator whose
float units were instantiated without connecting their operation port, so the
tool tied it to opcode zero, a pass-through, and the tier neither multiplied nor
accumulated.

**Follow the signal to the RESULT, not from the instruction.** Three checks find
the whole class:

- **grep the decode register and count its reads.** Declared, assigned, and
  *nothing* is the signature. A dead decode register is not automatically a
  bug — one whose function is fully covered by another signal is a flop the tool
  removes — so the question is whether the *instruction* has a datapath, not
  whether the *signal* has a reader.
- **grep the synthesis log for `[Synth 8-7071]` and keep only the INPUTS.** An
  unconnected *output* is ordinary; nobody read a status flag. An unconnected
  *input* is tied to zero and silently becomes a legal-looking value. The
  unconnected operation port above was named in every synthesis log for as long
  as the feature existed.
- **read the area column.** Those accumulator units synthesised at roughly a
  fifth of what the elementwise ones cost. A full multiply-add unit cannot be
  that small, and the discrepancy is the missing datapath appearing as a number.

**No bench built from the decode can catch it.** The generator, the golden model
and the RTL are all written from one instruction table, so a feature missing
from the *datapath* is missing from all three consistently and they agree with
each other about nothing being wrong. A generator that excludes an instruction
because "the unit faults on it" makes that worse: the exclusion is correct at a
width of 0 and conceals the defect at every other width.

## Why the specials have to be pinned against the spec

**A special case is exactly the input a workload never supplies.** Coverage from
kernels is coverage of the middle of the range; the edges are only pinned by a
table written against the specification.

A seed returning `+inf` where IEEE requires `−inf` on a negative zero survived
for as long as the seed existed, because no kernel in the library issues that
seed at all — so no amount of workload evidence could have found it, and no
tolerance would have either: the magnitude was infinite and correct, and only
the sign was wrong. It took reading the specification and then checking the RTL
against it. The tell was internal: two seeds in the same file contradicted each
other on one input class, one deriving the sign from its operand and the other
hard-coding it, which is not a decision anyone made.

Two cautions follow, and they are why the specials are checked **exactly** and
per case rather than under a blanket tolerance:

- **derive the expectation before reading the RTL.** A corner transcribed from
  the hardware makes the bench defend whatever the hardware does.
- **a corner can be checked carefully along the wrong axis and still be wrong.**
  An assertion that reads "= +inf, *not NaN*" is checked on the infinity-versus-NaN
  axis, and the sign is never questioned.

The finite paths are held to a measured tolerance because the seeds are a table
plus a range reduction rather than a correctly-rounded function; the bounds are
the component bench's own measured worst case, and a second test holds the same
path to a relative error so a specials fix cannot pass while the arithmetic
rots.

## One observation is not yet a rule

Every claim overturned quickly in this project has had the same shape: a sound
reading of one case, promoted to a general rule before a second case was looked
at.

| the observation, correct | the rule, false | the second case |
|---|---|---|
| one bench's check count is a case count | "a check count is a case count" | another bench counts unconditionally, and its deltas *are* readable |
| `-flatten_hierarchy rebuilt` beat `none` by 647 LUT | "the flatten gap is 647 LUT" | at a different setting of one knob it is 243 — the 647 was all DSP inference for one signal |
| a synthesis run repeated bit-identically | "out-of-context runs repeat exactly" | another module's row came back four LUT apart across two suites |

**The second case is usually already in your data**; it does not get found by
thinking harder about the first. When a finding is about to become a rule, name
the second case that would falsify it and go and look. If there is none
available, say "true of X" rather than the general form.
