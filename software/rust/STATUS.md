# Rust stack: workstreams and open items

Each agent owns the crates named here. Before new work, run your crates'
tests (with and without `--features mutants`) and clippy, and report what
you find: the tree may hold half-finished edits.

## Done (owner keeps them green)

- ktpu-machine, ktpu-isa (rs-isa): 784/784 goldens, 161 mutants.
- ktpu-l0, ktpu-cache (rs-l0): 112 clause, 97/97 goldens, 109 mutants.
- ktpu-l1 incl. sched/ (rs-l1; sched/ is rs-l0's): 310/310 goldens, 245/245
  scheduler goldens, 117 vectors, 131 mutants.
- ktpu-l2 (rs-l2): 177/177 goldens, 109 vectors, 120 mutants.
- ktpu-graph (rs-graph): 90 + 60 goldens.
- `KTPU_RS` switch (rs-l0): `kernel.compile` is the one entry from L2 (a
  trace, L2 text or Module) to what a card runs; with the flag the card
  runs the Rust stack's own packages (`ktpu::CardStack`, fetch port from
  the die), bound and lowered by `_rs.bind`, its checker verdict gating the
  run (`build.Prebuilt`, `build.built`). Every runner that compiles L2
  goes through it (`test_switch.py`: 0 Python compiles with the flag, and
  an entry ignoring the flag fails that count). Tier 0: flag unset 3102
  passed; flag `1`: 3 Python-encoder mutants (isa.cluster/fields on
  ISA.MG.030/.082.paired, isa.vector/dim on ISA.VC.123.add) cannot reach
  the Rust compile (same-named Rust mutants exist; the pyd has no mutant
  bridge).
- `KTPU_RS` reaches every card build (rs-l0): hand-written L1 bodies too
  (`kernel.compile(level="l1")`, `_rs.compile(l1=True, binds=)`: built
  at their bindings; a relocatable build whose words hold an address
  outside its spans is refused, STACK.RELOC); cuts are Rust's
  (`Package::cut`, `_rs.bind(after=)`, mutant dbg/no_cut); spec-test
  mutants through `_rs.compile(mutant=)` (`scripts/py/build_rs.py spec`).
  `test_switch.py` 27/27. Tier 1, dbg/l0/l1/isa on the sim
  (`build/spec/l1path-{rs1,py}`): every clause the same in both modes
  (DBG.DUMP.001 and its mutant pass under Rust); Rust leaves 93 isa / 2 l1
  mutants unbroken (ktpu-isa layout mutants that do not reach the L1
  build; Python mutants with no Rust counterpart), sent to rs-isa / rs-l1.
- Tier 1 on the Verilated card (rs-l0; v9 die), dbg/l0/tg rerun after the
  fixes, `build/spec/fix-{rs1,py}`: both modes 22/25 dbg, 54/63 l0 clauses
  (12 l0.card.boot vectors skipped: v9t6 die on a v9 target), 23/24 tg
  (TG.RUN.012: one 2.6 MB relayout package, not done in 5M sim cycles);
  mutants all killed; 136 compiles through `_rs`, 0 through Python.
- ktpu-kernels (rs-gen): 781/781 generator calls.
- ktpu-plan (rs-graph): 165 plan goldens (5 synthetic: _ungated,
  _enumerated, _lane_copies, rows stat-first x2), every ast, 165/165 runs,
  175/175 kp entries, launches by name and graph through
  ktpu_kernels::call (a truncating launch against its RUL.060 graph),
  every case's paths taken by both planners; 30 mutants killed by the
  goldens. `ktpu_plan::spec` (tg.plan, tg.run): 31/33 tier-0 vectors, the
  2 `grep` / `params` ones need the pipeline (`spec::realized` gives a host
  the launches); 31 vector mutants killed.
- ktpu-spec (rs-golden): 487/487 tier-0 vectors over isa, l0, l1, l2,
  verify, kernels, plan (tg.run grep / params hosted); 572 mutants killed.
- ktpu e2e (rs-golden): 193 cases, every one with a Python reference, 0
  disagree over the 4 comparison shards (plain, card and arena builds); one
  ruled refusal (examples-dsl-attention, RUL.030, golden/ruled.json).

## Open

- rs-isa: RUL.033 no_raw_default in the Python driver (BootArgs has no
  default timeout; the four frozen vectors L0.BOOT.007/.008, L0.STEP.008,
  DBG.TMO.004 get an explicit 2,000,000 timeout in their input, expected
  bytes kept; `RUL.033.no_raw_default` frozen with mutant
  `l0.boot/raw_default`). Then the exact command and file
  list for the lead's card run of the 9 DBG vectors. Then DBG.FAIL.005 and
  DBG.DUMP.*.
- rs-l2: small Verilator config vlt_v9t6s_1n (board, machine, gen_card top,
  xsim bench done). Blocker: WSL Verilator 5.020 rejects mx_quant.v:272
  (BLKLOOPINIT); try the native micromamba Verilator; no RTL edits.
  Build in the background, under
  2 min a step; smoke check with card_run.py.
- rs-gen (ktpu-kernels): the `l2.lanes` spec runner (plan + host apply,
  3 mutants) in ktpu_kernels::spec; DSL.VEC.015 follows RUL.030; report the
  full suite counts with and without mutants; die.target source cites the
  image build scripts, not compiler/target.py.
- rs-l1: measured cost (Rust vs Python compile time on golden/e2e, per
  stage, release and dev, cold and warm cache, compile_many at 1/4/N
  threads with identical output; bench `crates/ktpu/examples/compile_cost.rs`).
- rs-verify (ktpu-verify): finish the v9t8-6c2v example dumps in chunks
  under 100 s, rerun goldens, final report.
