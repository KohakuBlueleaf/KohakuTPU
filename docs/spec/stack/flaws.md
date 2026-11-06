---
title: Silicon flaws
summary: A silicon flaw is a defect of the RTL an image was built from.
tags:
  - spec
  - stack
---
# Silicon flaws

A silicon flaw is a defect of the RTL an image was built from. Each one below is
**a silicon flaw that needs an RTL fix**. Where generated code reaches a flaw, the
stack handles it at the level that emits the triggering input; a flaw no generated
path reaches, or an accuracy limitation no lowering can avoid, is recorded with
that reach. Raw words written outside the compiler (debug writes, hand-built
packages, raw PE shaders) are not covered by any handling. The fix named here
removes the flaw and any handling cost.

## How the stack knows

Every machine file carries a `flaws` section (`docs/spec/stack/machine.md`): one
entry per flaw id with `present` and the record below. The machine generator
(`scripts/py/machine/table.py` `FLAWS`) reads `present` from the image itself: the
RTL line that has the defect and the defines that expose it. The reading is
positive both ways (`read/image.py` `flaw_line`): the flawed line means present,
an explicitly supplied fixed line means absent, and a file with neither stops the
generator, since a changed line is not proof of a fix. An RTL round that fixes a
flaw records its fixed line, the next machine file reads `present: false`, and
every handling keyed on it turns off with no software change. `present` is in the
machine's digest, so a fixed image recompiles.

Compiler handlings use machine flaw facts. Firmware and OS handlings also include unconditional safeguards:

| Flaw | Handling | Reads |
|---|---|---|
| F-001 | One proof, ktpu-l1 `flaw.rs` (L1.MOV.019): every encoded copy, gather and scatter is free (one word), proven from a write-line base (every destination run an even number of words starting an even word from it, every burst even at any page offset), or unproven, and an unproven one is split into commands each free or proven; a register count in an unproven command is refused, and a register added to a write base steps two words. The L2 lowering reads the same proof first and keeps its cheaper handlings: `lower/mover/lone.rs` (L2.MOV.020) runs a copy with an unproven command on the vector cores; `passes/stage.rs` (L2.PASS.001) sends a gather or scatter stepping one-word runs on both sides, and a gather of even-word rows into an unproven walk, through a contiguous scratch. Each compile names the parameters a proven command's base lies in (`f001.line_ports`); the runtime refuses only those bound off a write line (`device/submission.py` `unaligned`). Not covered: mover fields a class template patches at bind (`templates/proof_mover.rs`) are not proved against F-001. | `Target::mover_lead`; ktpu-l1 `flaws.f001`; runtime `burst_line` |
| F-002 | ktpu-l1 `lower/image/desc.rs` `far_splits` (L1.VC.010): every far peer drain the compiler emits is split so no packet is one flit. | `CoreFacts::il_tail` |
| F-003 | ka-package `engine.rs` `lowered`: every RING stays a step; `replay.rs` refuses an engine RING entry (RUL.046). The firmware's `de_step` and the OS's `k_de_stream` run every RING through the room wait once the engine's queue has issued (firmware `ring`, OS `k_ring_word`), and the firmware's `de_stream` holds a raw ENGINE RING entry, and `pairs` a CTRL step's write to the interlink RING register, on the same wait and counts it (`ring_room`), so one count covers every ring the node sends. | `Facts::ring_unheld`; firmware and OS on every image |
| F-004 | `unproven` ka-package `engine.rs` `Stream::wait` raises a requested target to `max(want, sent - (2^(b-1) - 1))` when the flaw is present, where `b = node.engine.counter_bits` [12] and `sent` is the selected unit's dispatched-word count or the mover GO count. It emits the resulting target modulo `2^b` only when the raised target is positive. With the flaw absent it leaves the requested target unchanged before that positivity check and encoding. The OS graph emitter applies the same flaw-keyed clamp before positivity and deduplication for every generated WAIT, with both OSCard construction paths passing F-004.present. (code: software/kohakuaccel/compiler/crates/ka-package/src/engine.rs:142-152, :224-235, :248-256, :319-342, software/kohakuaccel/compiler/crates/ka-package/src/facts.rs:114-119, :158-164; evidence: software/kohakuaccel/compiler/crates/ka-package/src/engine.rs:570-624 contains host-only handling tests) | `Facts::wait_window = flaws.F-004.present` |
| F-005, F-006, F-007 | KohakuAccel OS (`software/kohakuaccel/os`), firmware (F-007) | not machine-keyed |
| F-008 | None: the load-window console is a best-effort debug channel. Queue stdout goes through the queue service's own sink and the OS keeps a DRAM log; neither uses this FIFO. | positive `flaw_line` |
| F-009 | Accuracy flaw: no software handling. Avoiding data-dependent subtraction/rounding inside the accumulator requires extra computation or changing the numerical algorithm; no cycle-free lowering preserves all inputs. | positive `flaw_line` and `MG` unit inventory |
| F-010 | No generated TPU path: L1 resolves vc/mg/sr only and software has no SIMT shader emitter. The PE assembler/reference tools can construct the instruction; they are not a protected hardware execution path. | positive `flaw_line` and `SIMT` unit inventory |
| F-011 | No generated TPU path: L1 resolves vc/mg/sr only and software has no SIMT shader emitter. Package and OS barriers are not SIMT BAR instructions. | positive `flaw_line` and `SIMT` unit inventory |
| F-012 | ktpu-l2 `lower/fills.rs` fill throttle (L2.VC.027): a `vsync fe on=fill` before every generated fill that would lose a completion entry by the queue rule `ktpu_machine::fill_lost` (ISA.VC.193); a descriptor set outside the image counts as a length equal only to its own register's. The verifier checks the same rule with the walked lengths (`ktpu-verify` `vc_fill_landed`). Both run on every machine: the rule is the ISA's. | `vc.fill_track_depth` |
| F-013 | The compiler owns each SRU's configuration: ktpu-l1 `lower/sru.rs` holds the words a kernel has written and admission validates the effective executed configuration, so no compiled job starts on an inadmissible image. Raw `SruInst::Config` words and runtime debug writes are outside it. | positive `flaw_line` and `SR` unit inventory |

## How each is tested

Each existing RTL witness bench asserts its flaw as recorded, so it fails on a
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

F-008 through F-013 use source-reasoned witnesses, not executed benches.
F-012's handling: `software/tests/stages/l2_l1/fill_throttle.rs` (a fill through
another outside descriptor past the queue's depth waits for a landing; through
one register none is added). F-004's OS clamp:
`software/tests/logic/test_os_graph.py`
`test_os_waits_remain_reachable_after_a_half_counter_window`.

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

F-008, F-009, F-010, F-011 and F-013 cost no device work. F-012's throttle adds
one front-end sync before a fill only where more fills than the queue's depth
would be unlanded through descriptors of possibly different lengths; images
within the depth are unchanged. F-004's OS clamp is O(1) host arithmetic per
wait; ordinary 512-credit and fetch windows keep their entries, and a credit
window wider than half the counter range gets the extra WAITs that keep each
target inside it. Machine fact generation reads one RTL signature and, for
unit-specific flaws, the unit inventory; this is off the execution path.

## The flaws

| Id | Unit | What goes wrong | Witness | Fix |
|---|---|---|---|---|
| F-001 | mover, `mm_mover.v` | A one-beat copy burst chained off the previous burst's last beat pops the data FIFO before XPM fwft shows its word: the burst writes the word before, the pop is ignored and the word stays queued, so every later copy lands one word late until the mesh is reset. Present with `KOHAKU_IL_MVSKID=1` (v9t5 on), whose W skid takes a beat the edge after the chain starts. | `tests/sysnode/mm_mover_stream_tb.v`, `tests/sysnode/mm_mover_wskid_tb.v` (xsim on the real cell, Verilator on the fwft shim); v9t6 card, gathers into one-word rows | A presentation register between `u_dfifo` and the W port, loaded only on `!f_empty`: about 257 FF and a few LUT. |
| F-002 | interlink, `mag_ilink.v` | A one-flit outbound packet is read from `u_enc` two edges after its write, before XPM fwft shows it: the previous flit goes twice and the real one stays queued as the next packet's first flit. A far vector peer drain of a multiple of `il.packet_beats` words ends in such a packet. Both initial load and refill sample/pop without FIFO readiness; this is the same stale-head mechanism, not a second flaw. | `tests/sysnode/mag_ilink_tail_tb.v` (xsim on the real cell, Verilator on the fwft shim: flits 1, 2, 2, 3 where 1, 2, 3, 4 were sent); v9t8 card, far peer drain of 32 words | An elastic output stage: sample and pop only a valid FIFO head, hold data/last while stalled, and advance completion only on valid accepted beats. |
| F-003 | dispatch engine, `dispatch_engine.v` | An engine RING writes the interlink with no room check, so a ring past the interlink's queue (`node.ring_depth`) is dropped and its receiver waits forever. | `tests/sysnode/dispatch_engine_tb.v`: six RINGs issue against an interlink queue of four | Hold a RING entry while the interlink reports `node.ring_depth` rings queued; let the firmware read the interlink's queued count. |
| F-004 | dispatch engine, `dispatch_engine.v` | `RTL` A WAIT releases only when `(count - target) mod 2^b < 2^(b-1)`, where `b = node.engine.counter_bits` [12]. A completed target whose modular distance behind the count is at least half the counter range therefore blocks the WAIT and subsequent entries; it remains blocked while the count is unchanged. (code: src/kohakuaccel/sysnode/dispatch/dispatch_engine.v:163-168, :180-182; evidence: tests/sysnode/dispatch_engine_tb.v:359-367) | `tests/sysnode/dispatch_engine_tb.v:359-367` enqueues a target exactly half the counter range behind, disables incoming signals and checks after 200 cycles that the WAIT and following write remain queued. | Track issued work and completion epochs so that an already completed target releases independently of the modular subtraction's sign. |
| F-005 | RV64 MMU, `rv64_mmu.v` | Under Sv39 a data access presented the cycle after a multi-cycle access completes takes that access's physical address. | `software/tests/machine/programs/xlat/xlat.c` on `rv64_node_pair` and the kos core model | Clear `resolved_q` on the data access's completion. |
| F-006 | interlink, `mag_ilink.v` | The signal word names a newly selected slot a cycle before its count follows: the first matching read carries the previous slot's count. | `software/tests/machine/programs/sigslot/sigslot.c` on `rv64_node_pair` | Register `sig_sel` beside `sig_q` and report the registered copy. |
| F-007 | interlink, `mag.v` | A node's signal slots, sent-ring count and faults reset only with the system, so a reloaded node starts with the previous run's counts. | v9t6 card (a second load hung in its first barrier); `run_core.py --boots 2` | Reset the node-facing interlink state with the processor reset. |
| F-008 | RV64 load window (rv64_load_win.v) | The 256-byte console uses equal 8-bit pointers for empty and has no full state: 256 unconsumed writes alias empty; later writes overwrite unread bytes without reporting loss. | Source witness: from reset, push 256 bytes without a host pop; R_CONS valid clears. Push one more and only that byte is visible. Not an executed bench. | Track occupancy/full separately from empty and expose loss or backpressure; define simultaneous push/pop and overflow behavior. |
| F-009 | matmul FP22 accumulator (mx_fpacc.v) | Opposite-sign nonzero operands with discarded alignment residue round away from zero when the truncated difference is exactly a retained-format midpoint. Before saturation the result differs by one FP22 ULP from nearest-even, with exact error at most 16639/32768 ULP. A rounding carry into exponent 127 invokes saturation and can amplify the difference to 32767 ULP of the correct exponent-126 result. These are single-add bounds, not accumulation bounds. | Source arithmetic witness: MW=14, (sign, exponent, fraction) operands (0,63,1) and (1,48,1) produce (0,63,1) instead of (0,63,0); swapping operands or negating both preserves the magnitude error. Not an executed RTL bench. | Carry the sign of discarded alignment residue through subtraction, normalization and nearest-even rounding in both split and reference add paths. |
| F-010 | SIMT scalar load decode (kht_predec.v) | Custom SMOV funct7=2 (SLD) is admitted by the family decode but selects neither a scalar register write nor a memory request; the instruction retires without loading its destination. | Source witness: assemble sld s2,s1 followed by a use of s2 with distinct old and memory values. tests/pe/tools/rv_simt_isa.py defines SMOV funct7=2. No RTL witness was executed. | Implement scalar load request, response, destination and fault handling, or report the unsupported encoding through the existing illegal-instruction fault. |
| F-011 | SIMT barrier decode (kht_predec.v, kht_core.v) | BAR is accepted and sets C_BAR, but the core has no consumer of that control: the instruction retires without workgroup arrival, release or memory ordering. | Source witness: two waves with one producer delayed past the other's BAR can pass the purported barrier independently. No RTL witness was executed. | Implement workgroup arrival/release with memory ordering, or fault BAR as unsupported through the existing illegal-instruction path. |
| F-012 | vector fill completion tracker (v2_core.v, v2_fifo.v) | VFILL admission does not reserve room in the 16-record completion FIFO. With delayed ordered responses, a seventeenth unretired fill overwrites a length record; completion accounting no longer matches issued fills. | Source witness: accept requests for a 256-word fill and sixteen one-word fills while withholding responses, then release responses in order. The length tracker has only sixteen slots. Not an executed bench. | Reserve a completion record before f_take and release it on retirement, accounting for simultaneous retirement/admission and the fill pending ft_push; never drop a full push. |
| F-013 | SRU retained job admission (sru_core.v, sru_cu.v) | CFG retains omitted words and RUN starts without checking the effective image. No-pool arithmetic finalise can stall, disabled reciprocal acts as MOV, and disabled MX7 output disagrees with shell word counts. Unsupported profile/geometry combinations are not faulted at job acceptance. | Source witness: with FIN_RCP=0, store an unused reciprocal finalise entry, then update only FINLEN to execute it with K0=2: the retained reciprocal moves 2 instead of producing 0.5. Raw OUT=2 on HAS_MX7=0 gives mismatched core/shell output counts. Not an executed bench. | Validate the complete retained configuration before starting core or walkers; return one existing execution fault completion with no transfers or row writes, preserve configuration, and admit the next valid job. |

## Reach and limits of software handling

`present` describes the flawed RTL in an image, not proof that a particular
program reaches it. F-009 through F-013 also require an instantiated MG, SIMT,
VC or SR unit respectively; the generator reads that inventory as well as the
positive flawed line. A missing unit makes the fact false, not a fixed RTL line.
Unrecognized RTL still stops generation. Source witnesses below are reasoning
about the inspected circuit, not reports of executed benches.

### Console (F-008)

The producer has neither FIFO occupancy nor backpressure. Firmware's unsunk
`ka_putc` and `ka_debug` write the register directly
(`software/kohakuaccel/firmware/lib/stdio/stdio.c:21-36`). Queue service installs a
separate stdout sink (`software/kohakuaccel/firmware/queue/service.c:297`), so its
ordinary stdout does not traverse this FIFO. The OS mirrors every byte to the
hardware console and separately keeps a DRAM log
(`software/kohakuaccel/os/kernel/console.c:63-76`). The log is not evidence that
the load-window copy is lossless. Host `console()` reads valid then pops; a full
FIFO aliases empty and terminates that loop
(`software/kohakuaccel/driver/kohakuaccel/driver/device/rv64load.py:203-210`).
Compiler unit instructions and package templates do not supply console flow
control; firmware or an OS application can print independently of them.

No polling frequency guarantees that an asynchronous producer cannot write 256
bytes between drains, so the load-window console is a best-effort debug channel:
bytes past 256 undrained are lost. Output that must arrive goes through the queue
service's stdout sink or the OS DRAM log.

### FP22 subtraction accuracy (F-009)

Let the magnitude-ordered operands have nonzero exponents, significand integers
`B = 2^14 + mb`, `S = 2^14 + ms`, and exponent gap `d`. Define
`q = floor(S * 256 / 2^d)`, discarded fraction
`r = S * 256 / 2^d - q`, integer difference `T = B * 256 - q`,
`l = 23 - floor(log2(T))`, and normalized integer `N = T * 2^l`.
These are exactly the split align, subtract and normalize operations in
`src/kohakutpu/matmul/mx_fpacc.v:333-347`, :387-395.

The sticky-under-borrow error requires **all** of:

- Opposite signs and both exponents nonzero (zero operands take the pass path).
- `r > 0`, hence `d >= 9` with the eight appended alignment zeros.
- `N mod 512 = 256`: the truncated difference is exactly a retained FP22
  midpoint, with guard set and every lower explicit bit zero.

For these combinations the exact magnitude is *below* that midpoint, while
:435-446 treats `lost` as a positive sticky and rounds *above* it. Swapping
operands changes nothing; negating both changes the direction of the signed
error, not its magnitude. Same-sign additions, exact alignments and other
rounding positions do not exhibit this particular error. There is no large
cancellation with lost residue: `d >= 9` implies `l` is 1 or 2, so the discarded
fraction cannot cross another explicit rounding boundary.

**Before exponent saturation**, the difference from correctly rounded FP22 is
exactly one ULP of the lower adjacent FP22 value. Error relative to the exact
sum is `(1/2 + r * 2^l / 512)` ULP. For `l=1` a midpoint needs `d <= 15`;
for `l=2` it needs `d <= 16`. Thus the maximum exact error is
`16639/32768` ULP (less than `65/128`), attained with `l=2`, `d=16`,
`B=16384`, `S=16639`. A simple non-boundary witness is
`(sign, exponent, fraction) = (0,63,1) + (1,48,1)`:
`1 + 2^-15 - 2^-29` rounds to `(0,63,0)`, but the RTL emits `(0,63,1)`.
Its exact error is `16385/32768` ULP. These rational calculations do not
constitute an RTL simulation.

**The saturation branch matters.** At :451-452, an increment into exponent 127
returns `(sign,127,16383)`, not `(sign,127,0)`. The above trigger with larger
operand `(sign,127,0)` and smaller opposite-sign operand `(other,111,f)`,
`1 <= f <= 255`, therefore yields the saturated value instead of the correct
`(sign,126,16383)`. The discrepancy is **32767 ULP** of that correct result;
the largest exact error is `1073692927/32768` such ULP, at `f=255`.
Already-saturated results are not covered by a blanket one-ULP assertion.
Flush-to-zero does not add a case here: lost residue requires an exponent gap
of at least nine between nonzero operands. No single-add bound is a bound on
an entire reduction, relative error near cancellation, or a final FP16 result.

Both the split arithmetic and the unsplit reference repeat the unsigned-sticky
rule (:511-545). The production pump instantiates the split modules
(`src/kohakutpu/matmul/mx_acu_fp_pump.v:787`, :845, :893). Matmul operand values
and scales, including values supplied through raw packages, determine the
trigger inside the running accumulator. Neither a compiler scheduling rule,
firmware instruction admission nor a host/template check can change that
internal rounding while preserving arbitrary data and all cycles. This is an
accuracy flaw with **no software handling**, not a reason to disable matmul or
refuse mixed-sign operands.

### SIMT (F-010, F-011)

`tests/pe/tools/rv_simt_isa.py:284-290` assigns SLD SMOV funct7=2;
`src/kohakumpe/simt/kht_predec.v:35`, :97-105 admits the SMOV family, but
:118-119 and :164-170 provide no scalar load write or memory path.
BAR sets `C_BAR` at :116; its only other occurrence under the SIMT sources is
the declaration at `src/kohakumpe/simt/kht_ctrl.vh:27`.

The TPU L1 target resolver admits only vc/mg/sr
(`software/kohakutpu/compiler/crates/ktpu-l1/src/lower/target.rs:142-146`).
The software tree's SIMT entries describe topology and bindings, not a shader
compiler; L2/DSL/kernel templates lower through the supported TPU units.
ka-package transports words, firmware dispatches them and the host can upload
raw memory; none supplies a SIMT shader instruction emitter or a workgroup
barrier implementation. A raw PE shader authored with the PE assembler can
still contain these instructions. That debug/bench exposure needs the RTL fix,
not an unused allowlist in the TPU compiler. Package/OS barriers are not BAR.
No generated-code software handling is installed.

### Vector tracker (F-012)

`src/kohakutpu/vector2/v2_core.v:1220` admits a fill independently of tracker
room; :1230-1241 pushes its length and retires lengths only as responses land.
The FIFO reports full but does not inhibit pushes
(`src/kohakutpu/vector2/v2_fifo.v:55-72`). One 256-word fill plus sixteen
one-word fills with requests accepted and responses delayed exceeds the
sixteen-record capacity. Losing the first long record can make a short
response count appear sufficient to retire it.

L1 emits a direct fill at
`software/kohakutpu/compiler/crates/ktpu-l1/src/lower/image/memory.rs:117-126`;
L2, DSL and kernel templates eventually use that image encoder. Raw vector
words can bypass it. ka-package/firmware/OS dispatch credits count instruction
retirements, not this internal length FIFO's occupancy, and the core accepts up
to 34 fills before any response lands, so no upstream limit keeps it within 16.
Generated code reaches it: a modulo prefetch issues 21 fills through three
descriptors of 16, 8 and 8 words before its first unpack. The fill throttle
(L2.VC.027, `software/kohakutpu/compiler/crates/ktpu-l2/src/lower/fills.rs:66-89, :160-207`)
walks dispatch order and inserts a front-end fill sync before a fill that
`fill_lost` says would lose an entry: past the depth when the shadowed entry's
length may differ, at twice the depth otherwise. Lengths come from the image's
`desc` statements; a descriptor set outside the image has a stand-in length equal
only to its own register's. Raw vector words bypass the throttle.

### SRU retained configuration (F-013)

`src/kohakutpu/sru/sru_core.v:103-109` updates only the addressed word.
Unsupported output packing is coerced at :115-116, while the CU counts output
using the raw output bit (`src/kohakutpu/sru/sru_cu.v:557-559`). No-pool arithmetic
has no result valid (`sru_core.v:314-321`); disabled reciprocal becomes MOV
(:495-517). CFG and RUN are admitted at `sru_cu.v:515-571`, with `exec_fault`
tied low at :114. Validation must precede both core start and shell walkers,
not merely detect an eventual timeout.

Semantic `words()` validates supplied/reconstructible Job contents
(`software/kohakutpu/compiler/crates/ktpu-isa/src/unit/sru/config.rs:518-577`).
Raw `SruInst::Config` emits its address/value pairs directly, and RUN does not
reconstruct an effective image (`unit/sru/mod.rs:244-294` in that crate).
L1 maintains `held` for write deduplication, but validates `want` before merging
it (`software/kohakutpu/compiler/crates/ktpu-l1/src/lower/sru.rs:440-483`): this
is not a proof of arbitrary omitted retained words across submissions.
`ka-package` `send_raw` accepts words and dispatches them via `b.send`
(`software/kohakuaccel/compiler/crates/ka-package/src/program.rs:306-319`,
:809-817). Generic package DISPATCH/FETCH, firmware and OS execution likewise
do not decode the SRU's complete retained image. Runtime raw transport/debug
writes can modify hardware outside a semantic Job's lifetime.

The compiler is the configuration's owner: a compiled kernel's SRU words come
from L1's `held`/`want` tracking and pass admission on the effective executed
configuration, so a compiled job never starts on an inadmissible image. Raw
`SruInst::Config`, `send_raw` packages and runtime debug writes are debug paths
outside that owner. The semantic capability checks describe unavailable
hardware and stay on for every image.

### Existing F-002 and F-004 coverage boundaries

F-002's initial load and refill both sample/pop without FIFO readiness
(`src/kohakuaccel/sysnode/interlink/mag_ilink.v:619-625`, :685-710).
A one-flit tail is a concrete trigger of that same mechanism, not another id.
The general RTL fix must gate **both** sampling and popping, hold valid/data/last
under backpressure, and count only accepted valid data beats.
`far_splits` is flaw-keyed and rewrites recognized structured far-peer drains
(`software/kohakutpu/compiler/crates/ktpu-l1/src/lower/image/desc.rs:169-249`),
then `image/memory.rs:170-177` emits two drains. It is not a readiness guarantee
for arbitrary raw flits, descriptor patches or refill gaps. Raw SEND transports
words without that descriptor analysis (`ka-package/src/program.rs:809-817`
under `software/kohakuaccel/compiler/crates`): raw flits are a debug path.

F-004 includes the distance **equal to** half the range
(`src/kohakuaccel/sysnode/dispatch/dispatch_engine.v:163-168`, :182).
The main package lowerer's generated waits share `Stream::wait`
(`software/kohakuaccel/compiler/crates/ka-package/src/engine.rs:142-152`).
The raw `stream` route (:508-514) appends `Put` without interpreting issuance,
so calling that wait helper does not prove raw-stream safety. Firmware
`de_wait` clamps known frontiers (`software/kohakuaccel/firmware/package/interp.c:719-735`),
but raw ENGINE waits are rebased and pushed (:895-902), not clamped against a
reconstructed issuance frontier. Mailbox credit waits call the clamp only once
credit is exhausted (:755-758). The OS host emitter applies the same machine-keyed clamp before target
positivity filtering and deduplication. Both OSCard construction paths pass
`F-004.present`; credit waits (including mailbox, dispatch-via-fetch and explicit
FETCH), AWAIT/MWAIT, split/end drains and barriers share the owner. An ordinary
512-credit window already demands a newer target and is unchanged; a wider
credit window gets the extra WAITs that keep each target within half the range.
The logic-stage `test_os_waits_remain_reachable_after_a_half_counter_window`
witness emits 2049 words then requests target 1 in one epoch, checks release
with the flawed modular comparator, and checks unchanged targets when absent.
Raw ENGINE Put issuance is not tracked by the semantic counters: raw ENGINE
streams are a debug path and carry their own targets.
