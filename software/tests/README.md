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

The timed model retains both aggregate `work` and `node_work`, indexed by
physical sysnode ID. Each resource floor is the maximum of that node's work
divided by its local MG/VC capacity or read/write bandwidth. Read work includes
mover reads. Grant size is not a divisor: an imbalanced schedule and a single
active owner retain their own bounds. Aggregate work remains the basis for
whole-card MFU. The perf stage runs the Rust `floors` target, comparing balanced,
imbalanced and single-owner schedules with their individual-node references.
The model implementation lives in the production `ktpu-l2` cost module; the
perf crate re-exports it. The `ownership` target compares automatic joint matmul
selection against row replication using emitted read traffic and full resource
completion, and `resource_state` checks descriptor restoration, consumed slot
generations and native counted-receive completion. Production schedule pricing
uses the bundled, anchor-labelled `sysnode-card-v1` profile; external perf
parameter files continue to select independent calibrations of the same model.
The L2-to-L1 dependency witness schedules repeated matrix effects within a bounded
admission window and checks the exact release frontier and pipe ordering. The
DSL cache witness distinguishes forced-only prices from a complete automatic
comparison; a forced call cannot hide a cheaper alternative on the next call.

The L2-to-L1 quant-choice witnesses compare direct mapped-word unpacks and
joint source tiles with exact packed output bytes and late
nonfinite rejection. The movement witness copies raw arbitrary bytes through a
fragmented source walk, checks one write per output byte and a lower complete
price than the safe mover. Its companion cost witness requires production
mover prices to agree with independently requested resource reports. Mapped-choice witnesses require automatic selection no dearer than a strictly cheaper candidate; ordinary policies may retain the original on ties. The DSL
batched-product witness retains separate manifest ownership while grouping
whole heads with common local rectangles into owner-local matrix waves. Joint source-tile packing checks
exact packed bytes and late nonfinite status against independent-entry packing,
including targets with four descriptors. The DSL direct-relayout witness checks
independent source addressing, single writes, hostile unused table contents and
minimum price against p1 under a permuted grant. The ordinary entry-major quant
witness requires complete-value unpacks instead of sliced row unpacks, exact
packed bytes and late nonfinite rejection, and minimum complete policy price.
The counted-copy witness exercises repeated DMA pairs and an odd final piece,
including raw output bytes, single writes, engine-local fill/drain dependencies
and automatic price selection against both barrier and queued schedules. DSL
owner-tile witnesses compare unequal local rectangles and column-grain origins
against ordered products and require the automatic complete minimum price. The
row-grain witness also covers two short independent-weight batches on a permuted
four-owner grant, verifying the emitted padded extent and geometry cost bound. Resident-B
witnesses compare resource read traffic for ordinary/ring products at the same
geometry and exact logical and emitted outputs with independent weights, odd row-tile tails and a staging-limited ring, certified with that same machine. Identity-layout delivery is compared at a common rectangle as well as owner-wave geometry. A separate L2-to-L1
witness keeps three original K chunks live across two complete products,
checking bank-entry placement against L2 execution.

The logic sequence-choice witness compares equivalent transfers whose permitted
ownership differs, requiring the decision to change with the physical grant and
family forcing and to recover for a pinned home. The runtime LeNet test compares
all actual WAITs with the package writer. Barrier/count-AWAIT semantics have an
explicit program in `logic/test_os_graph.py`, independent of a model's selected
engine schedule. The convolution comparison uses a private seeded input stream
so test ordering cannot change its sample; its numerical tolerance is unchanged.

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
