# Silicon flaws

A silicon flaw is a defect of the RTL an image was built from. Each one below is
**a silicon flaw that needs an RTL fix**: the stack works around it today, at a
cost this page prices, and the fix named here removes both the flaw and its
handling.

## How the stack knows

Every machine file carries a `flaws` section (`docs/spec/stack/machine.md`): one
entry per flaw id with `present` and the record below. The machine generator
(`scripts/py/machine/table.py` `FLAWS`) reads `present` from the image itself: the
RTL line that has the defect and the defines that expose it. An RTL round that
fixes a flaw changes that line, the next machine file reads `present: false`, and
every handling keyed on it turns off with no software change. `present` is in the
machine's digest, so a fixed image recompiles.

Each handling reads its flaw's `present` and nothing else:

| Flaw | Handling | Reads |
|---|---|---|
| F-001 | ktpu-l2 `passes/stage.rs` (L2.PASS.001) | `Target::mover_lead` |
| F-002 | ktpu-l1 `lower/image/desc.rs` `far_splits` (L1.VC.010) | `CoreFacts::il_tail` |
| F-003 | ka-package `engine.rs` `rings_overflow`, `replay.rs` (RUL.046) | `Facts::ring_unheld` |
| F-004 | ka-package `engine.rs` `Stream::wait` | `Facts::wait_window` |
| F-005, F-006, F-007 | KohakuAccel OS (`software/os`), firmware (F-007) | not machine-keyed |

## What each costs

`software/tests/perf/flaws.py TRACE --machine M` times a recorded trace's kernels
on machine `M` and on `M` with one flaw marked absent, for each flaw present. The
difference over the warm step is what that flaw's fix buys: modelled cycles, the
kernels the handling changes and the step launches they make. Only a handling
in the kernel's L2 or L1 changes what `timed` reads (F-001); the others run in
the L1 image encoder (F-002), the runtime's package lowering (F-003, F-004),
the firmware or KohakuAccel OS (F-005 to F-007), and are listed with where they
run as not modelled. Measured costs are in `measurements/REPORT.md`.

## The flaws

| Id | Unit | What goes wrong | Witness | Fix |
|---|---|---|---|---|
| F-001 | mover, `mm_mover.v` | A one-beat copy burst chained off the previous burst's last beat pops the data FIFO before XPM fwft shows its word: the burst writes the word before, the pop is ignored and the word stays queued, so every later copy lands one word late until the mesh is reset. Present with `KOHAKU_IL_MVSKID=1` (v9t5 on), whose W skid takes a beat the edge after the chain starts. | `tests/sysnode/mm_mover_stream_tb.v`, `tests/sysnode/mm_mover_wskid_tb.v` (xsim on the real cell, Verilator on the fwft shim); v9t6 card, gathers into one-word rows | A presentation register between `u_dfifo` and the W port, loaded only on `!f_empty`: about 257 FF and a few LUT. |
| F-002 | interlink, `mag_ilink.v` | A one-flit outbound packet is read from `u_enc` two edges after its write, before XPM fwft shows it: the previous flit goes twice and the real one stays queued as the next packet's first flit. A far vector peer drain of a multiple of `il.packet_beats` words ends in such a packet. | v9t8 card, far peer drain of 32 words; the Verilated 2-node model with the fwft shim | `OB_HDR` and `OB_DAT` load `ltx_dat` and pop `u_enc` only when `!ef_empty`. |
| F-003 | dispatch engine, `dispatch_engine.v` | An engine RING writes the interlink with no room check, so a ring past the interlink's queue (`node.ring_depth`) is dropped and its receiver waits forever. | `dispatch_engine.v` `write_ok` | Hold a RING entry while the interlink reports `node.ring_depth` rings queued; let the firmware read the interlink's queued count. |
| F-004 | dispatch engine, `dispatch_engine.v` | A WAIT compares its count modulo the counter: a target more than half the counter's range behind reads as ahead and holds forever. | v9t8 card (a wait held after 3,000 words); ka-package engine test `a_wait_far_behind_what_was_sent_still_releases` | Release a WAIT against the issued count, not the counter's sign. |
| F-005 | RV64 MMU, `rv64_mmu.v` | Under Sv39 a data access presented the cycle after a multi-cycle access completes takes that access's physical address. | `software/os/tests/xlat/xlat.c` on `rv64_node_pair` and the kos core model | Clear `resolved_q` on the data access's completion. |
| F-006 | interlink, `mag_ilink.v` | The signal word names a newly selected slot a cycle before its count follows: the first matching read carries the previous slot's count. | `software/os/tests/sigslot/sigslot.c` on `rv64_node_pair` | Register `sig_sel` beside `sig_q` and report the registered copy. |
| F-007 | interlink, `mag.v` | A node's signal slots, sent-ring count and faults reset only with the system, so a reloaded node starts with the previous run's counts. | v9t6 card (a second load hung in its first barrier); `run_core.py --boots 2` | Reset the node-facing interlink state with the processor reset. |
