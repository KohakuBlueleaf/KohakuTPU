# Rulings

Decisions on every disagreement the audit found between the code, the docs
and the hardware, ruled 2026-10-22 (the owner accepted each recommendation).
Each is a clause: the stack, firmware, driver and docs follow it. The
principles behind them: the program runs on the hardware as built, and every
hardware fact is configuration the stack reads from the machine schema.
Clause format and tags: `README.md`. Every ruling is `unproven` until the
2p.4 spec test that names it passes. Ruling R-NNN is clause RUL.NNN; the
`from:` field names the audit finding in the clause pages that the ruling
settles.

## Machine facts the stack assumes

- **RUL.001** `unproven` Vector core build, register count and read copies on v9t6: The machine schema (2p.3) carries vc.build, vc.regs and vc.copies generated from the board / bitstream config (v9t6: v21, 128, 2); the Rust target reads them, and `Card::of` refuses a board without them, and ir/ktpu.md states the @reg bound as vc.regs. (behaviour; from: isa/vector.md #7; l1/program.md #8; l2/program.md #4; ruling R-001)
- **RUL.002** `unproven` Vector IMEM depth: vc.imem_words becomes a schema parameter generated from the v2_core.v build (v9t6: 1024); ktpu-isa/src/unit/vector/ and the ktpu-l2/src/lower/vector/ image cap read it, and instruction-encoding.md is fixed to 1024 words with bit 242. (behaviour; from: isa/vector.md #3; ruling R-002)
- **RUL.003** `unproven` Vector DUALQ second queue: vc.dualq is a schema parameter from the build (v9t6: 0); the encoder refuses q = 1 when it is 0 and the verifier models two queues when it is 1. (behaviour; from: isa/vector.md #2; ruling R-003)
- **RUL.004** `unproven` Unit instruction depth and default credit: unit.inst_depth per unit is a schema parameter filled from the CU_CAPS depth the card enumerates (the dispatcher records the real depth); the builder, MachineSpec, target, engine.lower, ktpu-verify/src/sim/node.rs and the firmware fetch depth read it, every default is removed, and the docs cite the parameter. (behaviour; from: l0/package.md #2; l1/program.md #6; ruling R-004)
- **RUL.005** `unproven` Unit-count bounds: package, dispatch engine and boot table: One schema parameter per bound (de.units, fw.max_pkg_units, fw.boot_units) generated from the firmware headers; the writer refuses past the smallest bound that applies, the firmware checks a package's units against the boot table at the header pass (BAD_UNIT), and the docs cite the parameters. (behaviour; from: l0/package.md #1; l0/boot-block.md #3; ruling R-005)
- **RUL.100** `unproven` Buffer-count bound of a package: fw.max_buffers, generated from the firmware header, is a keyed fact; the builder makes no buffer past it (L0.BIND.005) and the writer refuses a package past it, so a package the firmware would fail with TOO_LARGE (L0.PKG.014) fails at compile. (behaviour; from: l0/package.md #1; ruling R-100)
- **RUL.006** `unproven` Queue line size against the transport's write granule: queue.line is a schema parameter generated from layout.h (64); the host layout reads it, opening a transport whose host_write_granule exceeds it is refused, and the layout.h comment is fixed. (behaviour; from: l0/node-queue.md #7; ruling R-006)
- **RUL.007** `unproven` Cluster MAC rate: mg.macs_per_cycle is derived in the schema from the tile shape and mg.kstep (v9t6: 1,024); performance tools and the docs read the derived value. (behaviour; from: isa/cluster.md #3 (ISA.MG.X03); ruling R-007)
- **RUL.008** `unproven` Clock behind tinygrad seconds: The tinygrad device converts cycles over the active profile's sysnode clock read from the schema / board profile, as runtime/card.py does, and CLOCK_HZ is removed. (behaviour; from: api/tinygrad.md #1; ruling R-008)
- **RUL.009** `frozen` DRAM size of the Verilated models: `card.dram.bytes` is the per-machine capacity, derived from the board's memory or the model build's DRAM depth; offline.py, card.py, bringup.py and the host map read it. The runtime exposes `Device.dram_bytes` before reservations; tinygrad.md states the parameter. (behaviour; from: api/tinygrad.md #3; ruling R-009)
- **RUL.010** `unproven` Target string in the DSL tracer: The tracer takes the target from the machine schema it is given; the constant is removed. (behaviour; from: dsl/graph.md #6; ruling R-010)
- **RUL.011** `unproven` Default machine when none is named: No default machine: card, offline and VerilatorCard require a named board (KOHAKUTPU_BOARD or an argument) resolved through the schema, and Kernel.trace requires a machine; each refuses otherwise. (behaviour; from: api/tinygrad.md #2; dsl/graph.md #1; ruling R-011)
- **RUL.012** `unproven` SRU FP16 form (F16_IEEE): sr.f16_ieee is a schema parameter generated from the built die (v9t6 and the generated die: 0); the SRU emitter reads it and refuses a program whose FP16 semantics differ from it. (behaviour; from: isa/sru.md #2 (ISA.SR.X02); ruling R-012)
- **RUL.013** `unproven` SRU profile features the emitter does not check: The schema carries the SRU profile's full knob set (F_LANES, H_TAPS, HAS_FN, TAB, HAS_MANT, FIN_RCP, HAS_MX7); ktpu-l1/src/lower/sru.rs refuses, by clause id, any op whose feature the machine's profile lacks. (behaviour; from: isa/sru.md #3 (ISA.SR.X03); ruling R-013)
- **RUL.014** `unproven` Two SRU profile defaults: Both constants are removed; the SRU profile comes only from the machine schema and the emitter reads it there. (behaviour; from: isa/sru.md #4 (ISA.SR.X04); ruling R-014)
- **RUL.015** `unproven` SIMT float tier in module defaults: simt.flanes and simt.fsfu_units are schema parameters from the build; the stack refuses FLT instructions when they are 0. (behaviour; from: isa/simt.md #3 (ISA.SIMT.X03); ruling R-015)

## Owner rules vs current code

- **RUL.016** `unproven` Core split capped at two cores: split() distributes trips over the node.vc instances the schema gives (any count) and the generator defaults are removed; a tier 0 test over vc = 1..4 checks every trip is covered exactly once. (behaviour; from: api/tensor.md #3; api/tinygrad.md #5; dsl/graph.md #2; ruling R-016)
- **RUL.017** `unproven` Descriptor count copied four times: All four read vc.descriptors from the machine schema, MAX_READS derived as vc.descriptors - 1. (behaviour; from: api/tinygrad.md #4; ruling R-017)
- **RUL.018** `unproven` Lane and word literals in the DSL tracer: The tracer reads vc.lanes and vc.chunk from the machine it is given (R-011) and every literal is replaced by them. (behaviour; from: dsl/graph.md #3; ruling R-018)
- **RUL.019** `unproven` Every tensor on the card is float: The tinygrad seam gives every bool / integer tinygrad buffer a float card form (bool 0.0 / 1.0, integers f32) and the device allocates no integer-typed buffer; in the DSL and layout, integer dtypes are allowed only as sysnode- or SIMT-private control data (mover index lists, loop counts) that no cluster or vector core reads, and number-format.md drops int8. (behaviour; from: api/tinygrad.md #8; dsl/graph.md #4; numerics/formats.md #3 (NUM.X03); isa/simt.md #2 (ISA.SIMT.X02); ruling R-019)
- **RUL.020** `unproven` Allocation tail versus exact sizes: The spec states exact sizes as the contract; 2p.8 makes kernel tails word-exact and drops TAIL, and until then the API clauses that depend on the tail are unproven. (behaviour; from: api/tensor.md #6; ruling R-020)
- **RUL.021** `unproven` Scratch contents on entry: Scratch contents are undefined on entry and a kernel fills what it reads; dsl.md:64 and L2.SPACE.008 are rewritten to say so, and a tier 1 test runs a kernel on scratch pre-filled with nonzero data. (behaviour; from: dsl/graph.md #8; l2/program.md #8; ruling R-021)
- **RUL.022** `unproven` Who binds addresses: driver or firmware relocation: Binding is the driver's only: the firmware refuses a package carrying relocation entries, its relocation code leaves the production image, and package-format.md describes host binding. (behaviour; from: l0/binding.md #1; ruling R-022)
- **RUL.023** `unproven` Physical addresses in packages versus handles: A handle is a buffer-table entry and the bind array holds the driver's translation (the current relocation form with handles in place of spans), so no payload carries a physical address; built in 2p.8. (behaviour; from: l0/binding.md #3; ruling R-023)
- **RUL.024** `unproven` Firmware memory regions and on-card heaps: The boot block carries a region table (base, size, use per region) filled from the driver's allocator; HEAP, ALLOC and FREE leave the queue protocol and os/heap leaves the image. (behaviour; from: l0/boot-block.md #2; l0/node-queue.md #8; ruling R-024)
- **RUL.025** `unproven` Refusals by name versus by clause id: Every Unsupported carries its clause id (API.REF.*, TG.PLAN.010) and the tests assert the clause id, never message text; the 'by name' doc sections are replaced by the clause list. (behaviour; from: api/tensor.md #2; api/tinygrad.md #7; ruling R-025)
- **RUL.026** `unproven` TinyJit replay on by default: The device refuses KTPUGraph replay on a card device by clause id TG.JIT.* until 2p.9 proves it, mnist_mlp.py defaults to no JIT, and the nn.py exactness statement is removed. (behaviour; from: api/tinygrad.md #11; ruling R-026)
- **RUL.027** `unproven` Format conversion on the host at upload and read-back: Conversion at the host boundary is the host's by clause, done by one converter that matches the card's F32 to F16 rule (RNE, saturate at 65504, R-057); api.md:56-58 and tinygrad.md:20-21 state that boundary. (behaviour; from: api/tensor.md #8; api/tinygrad.md #12; ruling R-027)
- **RUL.028** `unproven` N of a product: refuse or pad: kp pads N to whole cluster-column blocks (the block width from the schema) through the same planner path the device uses, and API.OP.006's refusal is dropped. (behaviour; from: api/tensor.md #4; ruling R-028)
- **RUL.029** `unproven` Padded-rows pitch rule: The padded-rows form's contract is np a multiple of vc.lanes, recorded in the form and read by every consumer; producers choose, and tier 0 tests cover consumers at ceil16 and ceil32 pitches. (behaviour; from: api/tensor.md #5; ruling R-029)
- **RUL.030** `unproven` The [R, 32] statistic pack in the DSL and L2: The graph contract keeps one statistic form, 16 a word; the f16[R, 32] pack is removed from ops.py and ktpu-l2/src/lower/vector/ and attention.py moves to the 16-a-word form. (behaviour; from: dsl/graph.md #5; l2/program.md #1; ruling R-030)

## L0, firmware and driver protocol

- **RUL.031** `unproven` TIMEOUT detail codes: ten in firmware, six on the host: The host table is generated from status.h (all ten, firmware names) with a tier 0 test comparing it to the header, and node-queue.md:166 lists the ten. (behaviour; from: debug/firmware.md C-1; l0/node-queue.md #1; ruling R-031)
- **RUL.032** `unproven` Engine-mode TIMEOUT detail: Engine-mode stalls report a dedicated ENGINE detail with the raw STAT in the value word, and the host decoder derives the head-blocked bit and the head entry's code from STAT (no firmware decode); the detail joins R-031's table. (behaviour; from: l0/dispatch-engine.md #2; ruling R-032)
- **RUL.033** `unproven` Wait timeout: raw cycles versus a profile-scaled bound: The host always writes the bound in cycles computed from seconds x the active profile's sysnode clock in the schema, the raw 2,000,000 default is removed, and a debug session passes its own seconds budget; a tier 0 test checks the boot block's bound for two profiles. (behaviour; from: debug/firmware.md C-3; l0/boot-block.md #1; ruling R-033)
- **RUL.034** `unproven` SR units without a firmware class: The generic class is the spec for SR completions, confirmed by a tier 2 test that an SR's completion codes retire as the generic class counts them, and SR fault words are decoded on the host per unit kind from the schema (R-078); no firmware SR class. (behaviour; from: l0/package.md #3; debug/firmware.md C-2; ruling R-034)
- **RUL.035** `unproven` Words dispatched by a lowered package (RUN value and quiesce): The firmware adds the words of each REL fetch entry to the unit's sent as it copies the entry, so RUN's value and the quiesce count are the words actually dispatched; a tier 1 test checks RUN's value against the lowering's per-unit word count. (behaviour; from: l0/dispatch-engine.md #8; l0/node-queue.md #4; ruling R-035)
- **RUL.036** `unproven` PROGRESS completion fields: PROGRESS carries the 32-bit SIGNAL value in the value word and run-relative cycles in word 2, as the spec says; a tier 1 test signals a value above 65535. (behaviour; from: l0/node-queue.md #5; ruling R-036)
- **RUL.037** `unproven` BAD_STEP and NO_REACH detail meanings: The detail is always the failing step's opcode and the specific index or offset goes in the value word; the spec lists the value's meaning per check. (behaviour; from: l0/node-queue.md #9; ruling R-037)
- **RUL.038** `unproven` Queue region in staging and host reach: The queue lives in staging at a schema-parameter offset, reachable through the window; node-queue.md:77, address-map.md:148-151 and memory-protocol.md:520-521 are fixed to that. (doc fix; from: l0/node-queue.md #2; l0/node-queue.md #3; ruling R-038)
- **RUL.039** `unproven` Relocation arithmetic width: The binder checks at bind that each bound address plus its addend lies inside its buffer and fits the field, refusing otherwise (with R-022 the host is the only binder). (behaviour; from: l0/binding.md #2; ruling R-039)
- **RUL.040** `unproven` Address 0 cannot be bound: 'Unbound' is an explicit all-ones sentinel defined once in the package format module and generated into the firmware header; address 0 binds like any other. (behaviour; from: l0/binding.md #4; ruling R-040)
- **RUL.041** `unproven` Machine signature never written: Program.build writes the signature of the schema it was built against and the node refuses a mismatch at the header; a tier 1 test submits a package with a wrong signature. (behaviour; from: l0/package.md #4; ruling R-041)
- **RUL.042** `unproven` 'No credit bound' sentinel: One named 'unbounded' value, the firmware header's, generated into the machine file as the keyed `fw.package.unbounded` and read by engine.lower and ktpu-verify/src/sim/node.rs. (behaviour; from: l0/steps.md #1; l1/program.md #7; ruling R-042)
- **RUL.043** `unproven` What AWAIT counts in engine mode versus the firmware path: AWAIT counts retirements only, as the engine RTL does, on both paths (the firmware path changes to match), acknowledgements are awaited by SEND ACK alone, and package-format.md is fixed; a tier 1 test runs one package in both modes. (behaviour; from: l0/steps.md #2; ruling R-043)
- **RUL.044** `unproven` Unit index checks in the firmware: The firmware checks every unit-naming step against the table in the header pass and fails BAD_UNIT; a tier 1 test submits a hand-built package naming an absent unit. (behaviour; from: l0/steps.md #3; ruling R-044)
- **RUL.045** `unproven` Signal slots for cross-node handshakes: Slots are allocated per program from il.slots in the schema and the lowering refuses a program that needs more. (behaviour; from: l0/steps.md #4; ruling R-045)
- **RUL.046** `unproven` Ring depth under the engine: An engine-lowered package carries at most il.ring_depth RING entries between waits; a package with more runs its rings on the firmware path; a tier 0 test lowers five back-to-back rings. (behaviour; from: l0/steps.md #5; ruling R-046; amended 2026-10-23 by the lead under the flaw-handling principle: the engine's RING has no interlink room check, so the compiler routes such rings through the firmware's room wait instead of refusing)

## Vector, mover and cluster ISA

- **RUL.047** `unproven` Shared B fills and their 64-entry limit: Shared fills are supported up to mg.shared_b_max (schema, 64 measured): the pass shares only within it and the lowering refuses an explicit share= past it; isa.md:162-169 and ir/ktpu.md are rewritten to that rule. (behaviour; from: isa/cluster.md #2 (ISA.MG.X02); l2/program.md #7; ruling R-047)
- **RUL.048** `unproven` FILL past L1 capacity: The in-bank wrap is the hardware rule stated in the spec (isa.md:171-174 fixed), and the encoder refuses eoff + n > mg.l1_bank_entries for every FILL, not only L2-lowered ones. (behaviour; from: isa/cluster.md #5 (ISA.MG.X05); ruling R-048)
- **RUL.049** `unproven` Mover modes 0-5 or 0-7: One mode table from the RTL (0..6, 6 SCATTER, 7 a copy in hardware) generated for compiler and driver; the stack refuses mode 7 and the 0..5 pages are fixed. (behaviour; from: isa/mover.md #1; ruling R-049)
- **RUL.050** `unproven` Which misaligned mover descriptors fault: The spec states the RTL's rule, and the stack encoder refuses every misaligned source base, gather pitch and index base by clause; a tier 0 test per field. (behaviour; from: isa/mover.md #3; ruling R-050)
- **RUL.051** `unproven` VLOOP with a zero count: The verifier models a count of 0 as one pass, as the RTL does, and the L2 lowering never emits a loop whose count can be 0 without a guard around it; a tier 2 test confirms the one pass. (behaviour; from: isa/vector.md #4; ruling R-051)
- **RUL.052** `unproven` What makes a VDRAIN a peer drain: The verifier decides by node [24] alone, as the core does; a tier 0 test feeds the node = 0, dst != 0 word. (behaviour; from: isa/vector.md #5; ruling R-052)
- **RUL.053** `unproven` Vector fault-code tables: One fault table per core build in the schema, generated from v2_core.v, read by ktpu-isa/src/unit/vector/core.rs and the host fault decoder (R-078); the V1 names leave ktpu-isa/src/unit/vector/core.rs. (behaviour; from: isa/vector.md #6; ruling R-053)
- **RUL.054** `unproven` SRU core contracts unchecked: The emitter refuses each of the four contracts by clause id (the software follows the hardware); a tier 0 test per contract. (behaviour; from: isa/sru.md #5 (ISA.SR.X05); ruling R-054)
- **RUL.055** `unproven` Float format of SIMT tensors: SIMT stack tensors are FP32 only, the stack refusing an FP16 SIMT port by clause and converting at the edges on the vector core; SIMT stays unproven. (behaviour; from: isa/simt.md #1 (ISA.SIMT.X01); ruling R-055)

## Numerics

- **RUL.056** `unproven` E8M15 encoding of constants: truncation versus RNE: One E8M15 converter (RNE, flush below 2^-126, overflow to infinity, matching vec_cvt.v) in one numerics module, used by the Rust vector and SRU encoders (`ktpu-isa/src/numerics.rs`); a tier 0 test pins 1 + 3*2^-17 to 0x3F8001 and 1e39 to infinity. (behaviour; from: isa/sru.md #1 (ISA.SR.X01); isa/vector.md #1; l1/program.md #9; numerics/formats.md #1 (NUM.X01); ruling R-056)
- **RUL.057** `unproven` FP16 overflow: saturate or infinity: Saturation at 65504 is the card's FP16 write contract and every host converter and reference follows it; the exponent-31 read is stated per consumer (vector unpack IEEE, SRU accumulator form binade) and fp16_values takes the consumer as an argument. (behaviour; from: numerics/formats.md #2 (NUM.X02); ruling R-057)
- **RUL.058** `unproven` Accumulator type named f32: The type becomes acc[M, N] whose format is the schema's mg.acc format (v9t6 S1E7M14), and accumulator.md drops ACC24. (behaviour; from: isa/cluster.md #4 (ISA.MG.X04); numerics/formats.md #8 (NUM.X08); ruling R-058)
- **RUL.059** `unproven` MXFP7 product tolerance: Products are checked against the quantised host reference at 1e-2 (hardware error) and, separately, the quantised reference against float64 at a stated quantisation-loss bound; tinygrad.md:125-126 states both. (behaviour; from: api/tinygrad.md #9; numerics/formats.md #4 (NUM.X04); ruling R-059)
- **RUL.060** `unproven` Truncation threshold for lane values: The threshold is derived from vc.sig_bits (2^15 on v9t6) with the rounding constant applied over the whole range; a tier 1 test truncates 16384.5. (behaviour; from: numerics/formats.md #5 (NUM.X05); ruling R-060)
- **RUL.061** `unproven` FP16 drain saturation: contract or defect: Saturation is the stated contract (NUM.ACC.007 frozen with a tier 1 test of a product past 65504), and accumulator.md:384-385 is rewritten to it. (behaviour; from: numerics/formats.md #6 (NUM.X06); ruling R-061)
- **RUL.062** `unproven` Lane rounding corner: The lane contract is RNE within one ulp, high only in that corner, as the RTL is built; e8m15.py states it and bit-exact tests exclude the corner. (behaviour; from: numerics/formats.md #7 (NUM.X07); ruling R-062)

## Scheduling model

- **RUL.063** `unproven` Two vector timing models price the same image differently: One timing module with schema parameters (unpack-to-math latency, occupancy from words moved, configuration cost, result latency from the last beat plus crossbar) read by both the L2 lowering and L1 image scheduler; the values are set from tier 2 measurements of the v21 core. (behaviour; from: l1/program.md #1; l1/program.md #2; l1/program.md #3; l1/program.md #4; l2/program.md #3; ruling R-063)
- **RUL.064** `unproven` Memory-path fill costs have no v9t6 figure: A tier 1 card session measures fill alone and shared on v9t6 (cycles from the debug trace) and sets vc.fill in the v9t6 schema; compiler fill costs come from the per-machine schema. (test to add; from: l2/program.md #5; ruling R-064)
- **RUL.065** `unproven` Dispatch piece size: l1-program.md:63-65 states the code's rule: pieces of half the credit, with the reason. (doc fix; from: l1/program.md #5; ruling R-065)

## Evidence to establish in 2p.4

- **RUL.066** `unproven` test_tensor.py has no run on the current tree: 2p.4 rewrites the test_tensor.py cases as tier 1 card value tests per API clause; until they pass, tt:: citations are not evidence and API clauses stay unproven. (test to add; from: api/tensor.md #7; l1/program.md #11; l2/program.md #9; ruling R-066)
- **RUL.067** `unproven` Recorded runs name the v9f model build: Every recorded result names its build or card image; the 2p.4 tier 1 runs replace the v9f records, and the example-only clauses are unproven until a tier 1 test covers each. (test to add; from: api/tinygrad.md #10; dsl/graph.md #7; api/tensor.md #7; ruling R-067)
- **RUL.068** `unproven` No witness of unpack lane=: A tier 1 test computes x * t.sum() with t a computed tensor, checks its L2 dump holds unpack .. lane= and checks the values against numpy. (test to add; from: dsl/graph.md #9; ruling R-068)
- **RUL.069** `unproven` Compare and predicated ops are not run by the compare test: 2p.4 adds tier 1 tests per compare op (vcmpgt, vcmplt, vcmpeq) and per predicate mode (unless=, P0..P3) with the select pass off, and the max form stays as its own test. (test to add; from: isa/vector.md #10; ruling R-069)
- **RUL.070** `unproven` V2 lane ALU and pack converter have no testbench: Tier 2 benches drive v2_alu and v2_e8_to_f16 with the V1 benches' vectors and expected results. (test to add; from: isa/vector.md #11; ruling R-070)
- **RUL.071** `unproven` Mailbox completion queue when full: hold or drop: A tier 2 Verilator test fills the completion queue past node.cq_depth with the firmware not draining and observes hold or drop; the spec and the firmware follow the RTL's behaviour. (test to add; from: l0/dispatch-engine.md #3; ruling R-071)
- **RUL.072** `unproven` Whether a completion orders a unit's DRAM writes: A tier 2 Verilator test per unit kind checks the completion flit against the last write acknowledgement; the spec states the observed rule as a unit-port clause. (test to add; from: l0/steps.md #6; ruling R-072)

## Debug firmware choices

- **RUL.073** `unproven` Debug image and its IMEM budget: A separate debug image (an IMAGES entry plus a DEFINES flag) built from the same sources, selected by the host per session; the production image is unchanged. (behaviour; from: debug/firmware.md C-4; ruling R-073)
- **RUL.074** `unproven` Trace counters: Each trace record carries the step's cycles and the firmware's own per-unit counters (sent, received, expected, in flight); no CU_CTRL reads. (behaviour; from: debug/firmware.md O-1; ruling R-074)
- **RUL.075** `unproven` Engine mode under trace and in the failure report: Value tests run every package in the production mode (engine on), so the debug session tests what LeNet runs; a package that fails is re-run once under NOENGINE with the trace on for exact step attribution. (behaviour; from: debug/firmware.md O-2; ruling R-075)
- **RUL.076** `unproven` Buffer dump mechanism: Host reads after completion, with 'after step k' a package the host cuts at k; no firmware code. (behaviour; from: debug/firmware.md O-3; ruling R-076)
- **RUL.077** `unproven` Last completions per unit: A ring of N words per unit in a debug region from the boot block's region table (R-024), N a debug-image define. (behaviour; from: debug/firmware.md O-4; ruling R-077)
- **RUL.078** `unproven` Where fault decodes live: Host decoders per unit kind, driven by the schema's fault tables (R-053); the firmware reports the raw signal word and its describe printers are removed. (behaviour; from: debug/firmware.md O-5; debug/firmware.md C-2; ruling R-078)
- **RUL.079** `unproven` Attribution in a batched session: Every trace and failure record carries its submission tag, so one batched session stays batched. (behaviour; from: debug/firmware.md O-6; ruling R-079)
- **RUL.080** `unproven` Where the host decoder lives: The decoder is in the Rust stack, called from Python. (behaviour; from: debug/firmware.md O-7; ruling R-080)
- **RUL.081** `unproven` Trace transport: Binary records in a debug region from the boot block's region table, read by the host after the completion and decoded by R-080's decoder. (behaviour; from: debug/firmware.md O-8; ruling R-081)
- **RUL.082** `unproven` Session continuation after a failure: The host goes on with the next package unless the failure did not drain (DBG.DUMP.002), and restarts the firmware then. (behaviour; from: debug/firmware.md O-9; ruling R-082)

## Stale docs

- **RUL.083** `unproven` Cluster opcode count: Four opcodes, POST an instruction, as the RTL decodes; the pages and code comments say four. (doc fix; from: isa/cluster.md #1 (ISA.MG.X01); ruling R-083)
- **RUL.084** `unproven` MEM_RD_REQ flags bit 5: flags[5] is INST; the reserved text is narrowed to flags[4] in every page. (doc fix; from: isa/mover.md #2; l0/steps.md #9; ruling R-084)
- **RUL.085** `unproven` Which dispatch-engine writes wait for a move: dispatch-engine.md states the RTL's rule. (doc fix; from: l0/dispatch-engine.md #1; ruling R-085)
- **RUL.086** `unproven` Mailbox STAT count width: [7:0] in every page, as the firmware reads it. (doc fix; from: l0/dispatch-engine.md #4; ruling R-086)
- **RUL.087** `unproven` Mover window in the control region: 0x100-0x17F in every page, as the firmware writes. (doc fix; from: l0/dispatch-engine.md #5; ruling R-087)
- **RUL.088** `unproven` Whether the node processor dispatches through its mailbox: Dispatch is through the mailbox; the second set of pages is fixed. (doc fix; from: l0/dispatch-engine.md #6; ruling R-088)
- **RUL.089** `unproven` Mailbox DST fields: The typed form in every page, as the firmware uses it. (doc fix; from: l0/dispatch-engine.md #7; ruling R-089)
- **RUL.090** `unproven` Processor stores across the interlink: Processor stores cross the interlink; memory-protocol.md and glossary.md are fixed and the clause cites test_remote_store. (doc fix; from: l0/steps.md #7; ruling R-090)
- **RUL.091** `unproven` Interlink ring register layout: The firmware's layout (slot [15:8], amount [31:16], fence [32]) in every page. (doc fix; from: l0/steps.md #8; ruling R-091)
- **RUL.092** `unproven` R_CONSOLE buffering: control-registers.md section 7.2 states the core port (no buffer) and points to one statement of the FIFO and its loss rule in node-queue.md section 5. (doc fix; from: debug/firmware.md C-5; l0/node-queue.md #6; ruling R-092)
- **RUL.093** `unproven` Firmware build optimisation flags: firmware.md states the build's flags as fw/build.py sets them. (doc fix; from: debug/firmware.md C-6; ruling R-093)
- **RUL.094** `unproven` Statistic layout in docstrings and comments: The docstrings and comments state 16 f16 values a word and the lane relayout. (doc fix; from: api/tensor.md #1; api/tinygrad.md #6; l2/program.md #1; ruling R-094)
- **RUL.095** `unproven` V1 vector-core pages read as current: The V1 sections are marked V1 (retired), and the compiler's reserved scalars S14 / S15 are stated as a stack clause. (doc fix; from: isa/vector.md #8; isa/vector.md #9; ruling R-095)
- **RUL.096** `unproven` IR levels: L2 is the top level: ir-text.md names both levels and the READMEs describe the tree as it is. (doc fix; from: l1/program.md #10; l2/program.md #2; ruling R-096)
- **RUL.097** `unproven` L2 view axis forms: l2-program.md lists the LO : HI form the reader accepts. (doc fix; from: l2/program.md #6; ruling R-097)

## All-zero MX blocks in K pairs (lead ruling, 2026-10-29)

- **RUL.099** `RTL` Every all-zero MXFP7 block feeding an `nk` >= 2 accumulator carries the E=0 scale field (NUM.MX7.008, NUM.ACC.008). Mover QUANT cannot give it, so: an L2 `quant` into an `nk` >= 2 destination without `backend=` lowers through `vector_mx7` (refused by the board's policy when it does not allow it); `backend=mover_quant` into an `nk` >= 2 destination is refused (L2.MOV.018); and a contraction plans `nk` = 1 where its quantiser is mover QUANT under pairing `unpaired`, or keeps `nk` = 2 with vector quantisation under pairing `vector` (`ktpu_machine::Pairing`, default `PAIRING`; the `matmul.plan` argument `pairing`, Python `KTPU_MX7_PAIRING`). L2.MOV.010.quant_and_gt4 runs under this ruling with `nk=1` destinations (`ktpu_l2::spec::SUPERSEDED`). (code: `ktpu-machine/src/pairing.rs`; `ktpu-l2/src/lower/mover/packing.rs` `quant_backend`; `ktpu-kernels/src/gen/matmul.rs` `plan`; evidence: L2.MOV.018 vectors; `test_zero_block_model.py`)

## SRU accumulation precision (lead ruling, 2026-10-24)

- **RUL.098** `RTL` R2 and every SRU row sum using `sru_red` with
  `KIND="sum"` use the following word-ordered accumulation, not an ideal
  single-rounding sum. The ISA describes the hardware that exists; compiler
  and numerical error budgets account for this precision without changing RTL.

For one row, a word contains four E8M15 elements `{s,e,m}`. Exponent zero
(including nonzero fraction) contributes zero. Exponent 255 contributes no
finite magnitude: any NaN, or both infinity signs anywhere in the row, produces
canonical NaN `0x7fc000`; otherwise infinity of the encountered sign wins.
All raw exponent fields participate in the maximum below, including 255;
the special result still takes precedence over the finite accumulator.

Start each sub-row with `E=0, A=0`. For each word in arrival order:

1. Set `Enew = max(E, e of all four elements in this word)`, and
   `dE = Enew-E`. Re-express the previous signed integer accumulator as
   `floor(A / 2^dE)` (arithmetic right shift, **toward negative infinity**,
   not toward zero). A shift beyond 47 yields 0 for nonnegative A, -1 otherwise.
2. Each normal finite element contributes
   `(-1)^s floor((32768+m)*256 / 2^(Enew-e))`.
   There are exactly **8 guard bits** beneath the 16-bit significand.
   Discard shifted-out bits without rounding; if `Enew-e > 23`, the magnitude
   is zero. Apply the sign **after** magnitude truncation, i.e. each element
   truncates toward zero. Exponent 0 or 255 contributes zero.
3. Add the four signed contributions to the rescaled A, in a signed 48-bit
   accumulator (two's-complement wrap if its range is exceeded); set E=Enew.
   The finite mathematical value represented is `A * 2^(E-150)`.

Finally `sru_sum2e8` converts this value once to E8M15: retain 16 significant
bits, increment on guard AND (sticky OR retained low bit), propagate carry into
exponent, overflow to signed infinity, flush rounded exponent <=0 to signed
zero; an exactly zero accumulator yields +0. Specials take precedence.
This is read from `src/kohakutpu/sru/stream/sru_red.v:104-136` (maximum),
`:147-151` (element truncation/sign), `:159-189` (signed accumulation and
arithmetic growth shift), and
`src/kohakutpu/sru/stream/sru_sum2e8.v:44-47,81-126` (magnitude, RNE and packing).
See [the numerical error budget](numerics/formats.md#sru-row-sum-error-budget).

### Hand-computed precision witnesses

A short example: word 1 is `[1,0,0,0]`; word 2 is
`[2^-16 + 2^-24 + 2^-31,0,0,0]` (E8 words `0x3f8000` and `0x378081`).
E stays 127, the grid quantum is `2^-23`. The second magnitude is
`floor(128 + 1/2 + 1/256)=128`; therefore the accumulated value is
`1+2^-16`, an even RNE tie giving `0x3f8000`. The exact sum is above that tie
and gives `0x3f8001`. This distinguishes 8 guard bits from ideal accumulation.

For the ROM flash job (seed 1), sub-row 0 row 24, the four elements in each
successive word are:

```
379838 3b2af5 3cd4f4 3bce48
3e9766 3f8956 38ce24 3dd6b0
3acb71 3a24fc 3aef8f 3964cb
3fcad2 39ad58 3bba3d 39ebb5
3abcc7 3a14a8 3b3b82 3ba13b
3ca51a 3a2a35 38c346 39747c
3b8869 3bb258 3a5b3e 3a054e
3af190 3afa26 3c8d7d 3a5fbd
```

After word 1: E=121, A=18746030. For word 2 E rises to 127;
`floor(18746030/64)=292906`; its four contributions are
2480512, 9000448, 824, 879360, giving A=12654050. Subsequent A values are
12689510, 26035763, 26117877, 26295123, 26386980, 26570492.
Thus the accumulated sum is `6642623/2097152`; the exact sum is
`850256029/268435456`, a difference of `285/268435456`.
The RNE midpoint is `103791/32768`. The accumulated value lies below it and
rounds to **0x404ab7**, whereas the exact sum rounds to 0x404ab8.
Adding initial R1=0x3f68bd gives **R1=0x408273** (ideal: 0x408274).
