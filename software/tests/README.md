---
title: tests
summary: One suite a stage, each proving its stage on the level below and nothing further.
tags:
  - software
  - tests
---

# Tests

A compiler stage `C` from level k to level k+1 is correct when `⟦C(p)⟧ = ⟦p⟧` for
its corpus, read on the simulator of each level. Chained, a stage's proof at
every level gives L2 to hardware, so a test checks one stage's outcome and never
goes through a lower one. Three conditions hold the chain together: everything a
stage writes is in the next stage's corpus (`static/test_coverage.py`), the
simulators share no lowering with the compiler (`ktpu-sim`), and both ends are
pinned: tinygrad's CPU backend at the top, Verilator at the bottom.

```text
tests/
├── run.py      stage selection, jobs, budgets, reports in build/tests/
├── static/     imports, layout, the coverage contract
├── stages/     common.rs, and one directory a stage:
│               tinygrad (lowering vs tinygrad CPU), dsl (evaluator vs L2 simulator),
│               l2_passes, l2_l1, l1_schedule (L1 simulator), l1_l0 (L1 vs L0 simulator)
├── anchors/    the L0 simulator against Verilator, instruction by instruction
├── logic/      what the runtime decides itself, on plain Python doubles: schedules,
│               sequences, capture, recipes, replay budget, arenas, card knobs
├── runtime/    forms, memory, replay, tiny models on the device double
├── firmware/   host/ on native C, emu/ on ka-emu
├── machine/    softmmu, AMP/SMP, interrupts, interlink on core-only Verilator models
├── e2e/        whole models on the card: final verification, not debugging
├── perf/       speed, level by level: see below
└── fixtures/   tiny L2 per form, the kernels tiny models compile (l2/harvest), anchor images
```

Speed follows the same chain. Each level has a floor and a time, and a stage owns
the ratio it adds, so a loss is named without a long run:

| Level | Read from | Tool in `perf/` |
|---|---|---|
| compiler stages | each stage alone on the corpus | `compile_speed`, `speed.py` |
| math, then form work | a recorded trace's L2 and L1 counts | `record.py`, `waste.py` |
| one kernel, one step in time | the task graph of its L1 | `timed`, `waste.py` |
| the model itself | `anchors/` on Verilator, `anchors/memory/` and a whole step on the card | `anchors.py`, `compare.py` |
| every trace, every machine | the two tiers above, from disk | `matrix.py` |

A trace is made once on the offline device and read by everything else, with no
tinygrad and no card. Every number in `perf/params.json` names its anchor, and
`test_anchors.py` fails when breaking a parameter leaves the anchors in budget.
`test_waste.py` holds each detector to waste put in on purpose. A parameter's `value`
is what Verilator pins, its `card` what the card pins: DDR4 and a step's cold cache
exist only there, so `timed --card` reads a step and plain `timed` reads an anchor.
`compare.py three` reads recorded kernels on the model, Verilator and the card: the
first gap is the model's, the second is memory.

`KTPU_HARVEST=<dir> pytest -p harvest` (`tests/` on `PYTHONPATH`) writes every
kernel a double compiles as an L2 fixture: run over `runtime` (materialized
reads) and `stages/tinygrad` (composed reads), the corpus of L2 to L1 holds what
the planner really emits.

| Tier | Runs on | Budget |
|---|---|---|
| static, logic | Python | 30 s |
| stage | simulators, host-native C, ka-emu | 30 s a stage, 1 s a test |
| all of the above | | 2 min |
| anchors, machine | Verilator | 60 s each, 15 s a test, few dozen k cycles to few hundred k |
| e2e | the card | 15 min |

```text
python tests/run.py                 # what this tree changes
python tests/run.py --all           # every stage that needs no model
python tests/run.py machine anchors # the Verilator tier
python tests/run.py --all --mutants # each stage must tell every mutant it names
python tests/run.py stages/l2_l1 --mutants 3/4   # one shard of an audit
python tests/run.py --python-mutants             # the Python tiers, on a spec build
python tests/run.py --emu-mutants                # ka-emu against firmware/emu
python tests/e2e/fill.py mnist_mlp.py ...        # refill the library e2e reads
```

A failure at e2e means a stage test is missing: add it before fixing. A mutant
is a stage's proof that it can fail: a mutant a stage's outcomes cannot tell
stands in that stage's `known.txt`, with what it did: `Untouched` (no fixture's
program changed: a corpus gap) or `SameValues` (programs changed, values held).
`tests/known.txt` holds the Python tiers': `Cost` (values hold by design, only
cost moves) and `Untold` (a gap).
