---
title: The KohakuTPU software stack spec
summary: What the stack, firmware and driver guarantee, layer by layer, as numbered clauses.
tags:
  - spec
  - stack
---
# The KohakuTPU software stack spec

These pages describe the software stack, firmware and driver as numbered
clauses. Each clause carries an evidence tag. The rulings in `rulings.md` state
how specific disagreements between the code, documentation and RTL are resolved.

## Clauses

Each clause is one list item. This fenced example illustrates the syntax; it does not allocate another id or replace the clause on the package page:

```
- **L0.PKG.003** `unproven` The package writer starts its tables after fw.package.header_bytes [96] and writes that value into the header size field. (code: software/kohakuaccel/compiler/crates/ka-package/src/facts.rs:111; software/kohakuaccel/compiler/crates/ka-package/src/format/wire.rs:55-61, :373-403; machines/multimesh_v9t6.json:602; evidence: host implementation inspection only)
```

- **Id.** `<layer>.<area>.<nnn>`, never reused. The layers are L0, ISA, L1, L2,
  DSL, API, TG (tinygrad device), NUM (numerics) and DBG (debug firmware).
- **Evidence tag.**
  - `silicon`: the v9t6 card ran it. The evidence is the 455 kernels and the
    packages built from them for the 2026-10-21 card runs; each page states
    the statement and instruction counts it relies on inline. A recorded
    card result also counts if it names the v9t6 image. Host code counts only
    where it is unchanged since `af7c1bb`, the tree the card ran; refusal and
    error paths count only where a card log shows them.
  - `RTL`: a test passes on a Verilated model of the RTL, or an RTL testbench
    covers it. The test is named. Behaviour-model and offline tests are not
    evidence. Evidence tags are documentation metadata. The software test runner
    selects explicitly registered suites; it does not discover tests from these
    clauses or rewrite their tags. Its `anchors` and `machine` suites are
    non-default suites. (code: software/tests/run.py:85-202)
  - `unproven`: neither silicon nor named RTL-test evidence establishes the
    clause. This tag records evidence status; it does not itself disable the
    behaviour. The compiler refuses inputs at explicit validation checks and
    reports the clause identifier selected by the check. (code:
    software/kohakutpu/compiler/crates/ktpu-isa/src/unit/cluster.rs:391-423,
    software/kohakutpu/compiler/crates/ktpu-l1/src/lower/sru.rs:97-145)
- **Hardware facts are parameters.** A clause names a machine-schema
  parameter (`vc.lanes`, `vc.l1_words`, ...) and gives the v9t6 value in
  brackets.
- **Frozen** means `silicon` or `RTL`.

## Pages

| layer | pages |
|---|---|
| L0 | `l0/package.md`, `l0/binding.md`, `l0/steps.md`, `l0/dispatch-engine.md`, `l0/node-queue.md`, `l0/boot-block.md` |
| ISA | `isa/vector.md`, `isa/mover.md`, `isa/cluster.md`, `isa/sru.md`, `isa/simt.md` |
| L1 | `l1/program.md` |
| L2 | `l2/program.md` |
| DSL | `dsl/graph.md` |
| API, TG | `api/tensor.md`, `api/tinygrad.md` |
| NUM | `numerics/formats.md` |
| DBG | `debug/firmware.md` |
| RUL | `rulings.md` |
| machine | `machine.md` (the machine files every hardware fact comes from) |
| flaws | `flaws.md` (the silicon flaws, how the stack handles each and what it costs) |

Each page ends with **Rulings**: the `rulings.md` clauses that settle what
the code, docs and RTL disagreed on for that page.

## Inputs

- An inventory of every hardware fact in the stack, firmware and driver with
  its file and line, the spec pages, and the contradictions between them.
- A tally of the statements and instructions the card ran.
- The older pages in `docs/spec/` and `docs/projects/kohakutpu/` are sources
  for these pages.
