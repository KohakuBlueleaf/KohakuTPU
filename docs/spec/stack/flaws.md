---
title: Silicon flaws
summary: A silicon flaw is a defect of the RTL an image was built from.
tags:
  - spec
  - stack
---
# Silicon flaws

A silicon flaw is a defect of the RTL an image was built from. Each one below is
**a silicon flaw that needs an RTL fix**: the stack works around it today, at a
cost this page prices, and the fix named here removes both the flaw and its
handling.

## How the stack knows

Every machine file carries a `flaws` section (`docs/spec/stack/machine.md`): one
entry per flaw id with `present` and the record below. The machine generator
(`scripts/py/machine/table.py` `FLAWS`) reads `present` from the image itself: the
RTL line that has the defect and the defines that expose it. The reading is
positive both ways (`read/image.py` `flaw_line`): the flawed line means present,
the fixed line recorded beside it means absent, and a file with neither stops the
generator, since a changed line is not proof of a fix. An RTL round that fixes a
flaw records its fixed line, the next machine file reads `present: false`, and
every handling keyed on it turns off with no software change. `present` is in the
machine's digest, so a fixed image recompiles.

Each handling reads its flaw's `present` and nothing else:

| Flaw | Handling | Reads |
|---|---|---|
| F-001 | One proof, ktpu-l1 `flaw.rs` (L1.MOV.019): every encoded copy, gather and scatter is free (one word), proven from a write-line base (every destination run an even number of words starting an even word from it, every burst even at any page offset), or unproven, and an unproven one is split into commands each free or proven; a register count in an unproven command is refused, and a register added to a write base steps two words. The L2 lowering reads the same proof first and keeps its cheaper handlings: `lower/mover/lone.rs` (L2.MOV.020) runs a copy with an unproven command on the vector cores; `passes/stage.rs` (L2.PASS.001) sends a gather or scatter stepping one-word runs on both sides, and a gather of even-word rows into an unproven walk, through a contiguous scratch. Each compile names the parameters a proven command's base lies in (`f001.line_ports`); the runtime refuses only those bound off a write line (`device/submission.py` `unaligned`). Not covered: mover fields a class template patches at bind (`templates/proof_mover.rs`) are not proved against F-001. | `Target::mover_lead`; ktpu-l1 `flaws.f001`; runtime `burst_line` |
| F-002 | ktpu-l1 `lower/image/desc.rs` `far_splits` (L1.VC.010) | `CoreFacts::il_tail` |
| F-003 | ka-package `engine.rs` `lowered`: every RING stays a step; `replay.rs` refuses an engine RING entry (RUL.046). The firmware's `de_step` and the OS's `k_de_stream` run every RING through the room wait once the engine's queue has issued (firmware `ring`, OS `k_ring_word`), and the firmware's `de_stream` holds a raw ENGINE RING entry, and `pairs` a CTRL step's write to the interlink RING register, on the same wait and counts it (`ring_room`), so one count covers every ring the node sends. | `Facts::ring_unheld`; firmware and OS on every image |
| F-004 | ka-package `engine.rs` `Stream::wait` | `Facts::wait_window` |
| F-005, F-006, F-007 | KohakuAccel OS (`software/os`), firmware (F-007) | not machine-keyed |

## How each is tested

Each flaw's witness bench asserts the flaw as recorded, so it fails on a
simulation model that does not show it (the old Verilator FIFO stand-in) and on
an RTL that fixes it. Each compile-path handling has a test that it fires on a
machine with its flaw and does not without it: `software/tests/logic/test_flaws.py`
(F-001: `lone`, `pagetail`, `oddrows`, the split backstop, the class loop and
the per-port bind rule), ktpu-l1 `flaw.rs` tests (the proof and the split,
word for word), `software/tests/firmware/host/test_package_chain.py` (F-003's raw
ENGINE RINGs), `software/tests/logic/test_far_split.py` (F-002), ka-package `engine.rs`
`rings_leave_the_engine_only_for_f003`,
`an_engine_ring_is_refused_only_for_f003` and
`without_f004_a_far_behind_wait_keeps_its_target`. On the card, F-003's handling
runs `examples/l1/multinode/ringstress.ktpu` (eight back-to-back rings against a
queue of four): v9t8 at mid, nodes 0 and 1, 3 of 3 runs counted all eight.
F-001's handlings on v9t6 at mid, node 0, 5 runs each, every word checked:
`examples/l2/mover/pagetail.ktpu` all 832 words, `oddrows.ktpu` all 12 rows
with their tails untouched, `scatter.ktpu` (proven, on the mover) all 64 rows.

## What each costs

`software/tests/perf/flaws.py TRACE --machine M` times a recorded trace's kernels
on machine `M` and on `M` with one flaw marked absent, for each flaw present. The
difference over the warm step is what that flaw's fix buys: modelled cycles, the
kernels the handling changes and the step launches they make. Only a handling
in the kernel's L2 or L1 changes what `timed` reads (F-001); the others run in
the L1 image encoder (F-002), the runtime's package lowering (F-003, F-004),
the firmware or KohakuAccel OS (F-005 to F-007), and are listed with where they
run as not modelled. Measured costs are in `measurements/REPORT.md`.

F-001's vector copy, on the card: `examples/l2/mover/lone.ktpu` (256 words into
every other word) takes 5,970 cycles on v9t8 at low, against 5,227 for the
mover copy the flaw corrupts (`measurements/ISSUES.md` row 48); on v9t6 at mid
the vector copy of N one-word runs costs about 15.8 cycles a word against 13.6
on the mover, 7% to 15% more from 32 to 1,536 words (row 48). Over a model's warm
step it is +1.2% on MLP and +0.6% to +0.8% on wide, and −0.7% to −0.8% on Mixer and
−1.7% to −1.8% on LeNet, where the vector copy is the cheaper one (REPORT, round e1).

## The flaws

| Id | Unit | What goes wrong | Witness | Fix |
|---|---|---|---|---|
| F-001 | mover, `mm_mover.v` | A one-beat copy burst chained off the previous burst's last beat pops the data FIFO before XPM fwft shows its word: the burst writes the word before, the pop is ignored and the word stays queued, so every later copy lands one word late until the mesh is reset. Present with `KOHAKU_IL_MVSKID=1` (v9t5 on), whose W skid takes a beat the edge after the chain starts. | `tests/sysnode/mm_mover_stream_tb.v`, `tests/sysnode/mm_mover_wskid_tb.v` (xsim on the real cell, Verilator on the fwft shim); v9t6 card, gathers into one-word rows | A presentation register between `u_dfifo` and the W port, loaded only on `!f_empty`: about 257 FF and a few LUT. |
| F-002 | interlink, `mag_ilink.v` | A one-flit outbound packet is read from `u_enc` two edges after its write, before XPM fwft shows it: the previous flit goes twice and the real one stays queued as the next packet's first flit. A far vector peer drain of a multiple of `il.packet_beats` words ends in such a packet. | `tests/sysnode/mag_ilink_tail_tb.v` (xsim on the real cell, Verilator on the fwft shim: flits 1, 2, 2, 3 where 1, 2, 3, 4 were sent); v9t8 card, far peer drain of 32 words | `OB_HDR` and `OB_DAT` load `ltx_dat` and pop `u_enc` only when `!ef_empty`. |
| F-003 | dispatch engine, `dispatch_engine.v` | An engine RING writes the interlink with no room check, so a ring past the interlink's queue (`node.ring_depth`) is dropped and its receiver waits forever. | `tests/sysnode/dispatch_engine_tb.v`: six RINGs issue against an interlink queue of four | Hold a RING entry while the interlink reports `node.ring_depth` rings queued; let the firmware read the interlink's queued count. |
| F-004 | dispatch engine, `dispatch_engine.v` | A WAIT compares its count modulo the counter: a target more than half the counter's range behind reads as ahead and holds forever. | `tests/sysnode/dispatch_engine_tb.v`: a WAIT half the counter's range behind holds; v9t8 card (a wait held after 3,000 words) | Release a WAIT against the issued count, not the counter's sign. |
| F-005 | RV64 MMU, `rv64_mmu.v` | Under Sv39 a data access presented the cycle after a multi-cycle access completes takes that access's physical address. | `software/tests/machine/programs/xlat/xlat.c` on `rv64_node_pair` and the kos core model | Clear `resolved_q` on the data access's completion. |
| F-006 | interlink, `mag_ilink.v` | The signal word names a newly selected slot a cycle before its count follows: the first matching read carries the previous slot's count. | `software/tests/machine/programs/sigslot/sigslot.c` on `rv64_node_pair` | Register `sig_sel` beside `sig_q` and report the registered copy. |
| F-007 | interlink, `mag.v` | A node's signal slots, sent-ring count and faults reset only with the system, so a reloaded node starts with the previous run's counts. | v9t6 card (a second load hung in its first barrier); `run_core.py --boots 2` | Reset the node-facing interlink state with the processor reset. |
