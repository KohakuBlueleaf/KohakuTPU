# The KohakuTPU software stack spec

What the stack, firmware and driver guarantee, layer by layer, as numbered
clauses. A frozen clause is a contract: a failure against it is a bug.
Frozen 2026-10-22: every `silicon` and `RTL` clause below, and the rulings
in `rulings.md`, which settle every disagreement the audit found.

## Clauses

Each clause is one list item:

```
- **L0.PKG.003** `silicon` The header is 96 bytes ... (code: path:line; doc: page:line; evidence: ...)
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
    evidence. The 2p.4 spec tests re-run each named test; a clause whose test
    fails there becomes `unproven`.
  - `unproven`: neither. Unproven clauses are listed so that the stack refuses
    them by clause id until a test proves them.
- **Hardware facts are parameters.** A clause names a machine-schema
  parameter (`vc.lanes`, `vc.l1_words`, ...) and gives the v9t6 value in
  brackets. The stack reads the parameter, never the number (2p.3).
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

Each page ends with **Rulings**: the `rulings.md` clauses that settle what
the code, docs and RTL disagreed on for that page.

## Inputs

- An inventory of every hardware fact in the stack, firmware and driver with
  its file and line, the spec pages, and the contradictions between them.
- A tally of the statements and instructions the card ran.
- The older pages in `docs/spec/` and `docs/projects/kohakutpu/` are sources
  for these pages.
