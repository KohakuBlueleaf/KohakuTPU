---
title: L1 program
summary: The L1 level as numbered clauses -- the .ktpu text, the module, node statements, unit words, vector images, mover ops, sync, the image scheduler, the lowering to L0 and the static verifier -- each with its evidence.
tags:
  - spec
  - stack
  - ir
---

# L1 program

L1 is one instruction stream per unit plus the node's sync points, written as
`.ktpu` text. A body is read against bound buffer addresses into one
`Program` per node; `Program.build` lowers it to an L0 package; the static
verifier orders and checks every package before a card runs it. Clause format,
ids and evidence tags: [../README.md](../README.md).

**Path prefixes.** `T/` = `software/language/kohakutpu/language/text/`,
Rust crate paths are relative to `software/rust/crates/`. `E/` =
`software/language/kohakutpu/language/examples/`. Doc pages: `ir-text` =
`docs/spec/ir-text.md`, `l1p` = `docs/spec/l1-program.md`, `ktpu` =
`docs/projects/kohakutpu/ir/ktpu.md`, `verify` =
`docs/projects/kohakutpu/verify.md`.

**Evidence.** `silicon` cites the statement tally of the 455 card kernels
(as `uses / kernels`) or their L1
text; a refusal or error path is never `silicon`, nor is code changed since
`af7c1bb` (the relocatable build: `Program.spans`, `Package.bind`). `RTL`
names a test of `software/application/tests/test_l1_examples.py`
(`tl1::NAME`), which runs the example on a Verilated card (`KTPU_CARD1` /
`KTPU_CARD2` name the model; unset, it is skipped), or a test using the
`dev` fixture (`software/application/tests/conftest.py`, the runtime's
Verilated card): `software/application/tests/ktpugrad/test_tinygrad.py`
(76 of 80 passing on the current tree; the 4 misses are MXFP7 precision) and
`software/application/tests/api/test_tensor.py` (`tt::NAME`; its recorded
run, `docs/projects/kohakutpu/api.md:78-81`, is on `build/v9f` and predates
the current tree). A test of `tl1::` names its example; a refusal is
witnessed only by a test that expects it. Tests under
`software/compiler/tests/` and `software/language/tests/` run on the
reference package interpreter or on no machine: they leave a clause
`unproven`.

## Parameters

Machine-schema parameters this page reads, with the v9t6 value.

| parameter | v9t6 | read at |
|---|---|---|
| `unit.inst_depth` | 512 words a unit's queue | ktpu-l1/src/lower/target.rs, ktpu-l0/src/program.rs |
| `fw.package.regs` | 16 (r15 the patch scratch) | ktpu-l0/src/program.rs |
| `il.slots` | 16 | ktpu-l0/src/build/mod.rs |
| `card.nodes` | 4 | boards/multimesh_v9t6.json:5 |
| `mg.count`, `vc.count` | 2, 2 a node | boards/multimesh_v9t6.json:74-95 |
| `vc.regs` | 128 (VEC_CORE v21); the silicon kernels name v0..v31 only | scripts/tcl/v9t3/00_config.tcl:11, T/module.py:21 |
| `vc.rf_copies` | 2 | ktpu-l1/src/lower/target.rs |
| `vc.lanes`, `vc.chunks`, `vc.vlmax` | 16, 8, 128 | ktpu-isa/src/unit/vector/mod.rs |
| `vc.sregs`, `vc.kregs`, `vc.preds`, `vc.marks`, `vc.descriptors` | 16, 4, 4, 8, 8 | ktpu-l1/src/lower/image/mod.rs |
| `vc.imem_words`, `vc.run_reach` | 1024, 512 | ktpu-l1/src/lower/imem.rs |
| `vc.queue_depth`, `vc.dma_queue_depth` | 8 (U, M, P), 4 (F, D) | ktpu-l1/src/sched/schedule.rs, ktpu-l1/src/sched/timing.rs |
| `vc.ainc_bits` | 18 signed | ktpu-l1/src/lower/image/mod.rs |
| `unit.burst_words` | 256 | ktpu-l1/src/lower/node/mod.rs |
| `mg.l1_bank_entries`, `mg.max_fill`, `mg.max_peers`, `mg.kstep` | 256, 255, 3, 2 | ktpu-isa/src/unit/cluster.rs |
| GEMM `anchor` (the stack's default) | 40 | ktpu-isa/src/unit/cluster.rs |
| `die.word_bytes` | 32 | ktpu-isa/src/unit/cluster.rs |
| `mover.levels`, `mover.max_index` | 6, 2048 | ktpu-l0/src/mover.rs |
| the stack's move bound | 16384 words | ktpu-l0/src/mover.rs |

## Vector timing facts (RUL.063)

The shared Rust parameters are `ktpu_l1::sched::timing::Params`; L2's
`lower/vector/modulo/ops.rs::link` and L1's `sched/timing.rs::Core` read them.
A start is an engine's instruction acceptance, not front-end dispatch. If an
instruction occupies B beats, its exclusive end is `start + B`: the numbered
cycle after its last beat. Thus an end-to-start latency L means a dependent
one-beat instruction has issue-to-issue distance `1 + L`, not L. Queue waits,
configuration words, bank conflicts and synchronization are separate costs.

The measurement records contain the source programs, K, VL, elapsed, busy and
per-engine counters, affine fits and every residual:

- [v21, math-queue skid](../../../../machines/measured/vc_timing.v21.mq1.json):
  v9t5, v9t6, v9t7, v9t8 and their machine variants.
- [v21, no math-queue skid](../../../../machines/measured/vc_timing.v21.mq0.json): v9t4.
- [v2](../../../../machines/measured/vc_timing.v2.mq0.json): v9.

`vc.build` alone does not identify timing: image overrides such as
`V2_MQ_SKID` also belong to the build. The generator requires a record matching
both the named build and all `V2_*`/`VEC_CORE_*` defines, with no fallback.
The probes use the image's core defines, not the replay bench's default RF.
They are compiled by `_rs.image` and `_rs.vc_flits`, without scheduling.
The 256-word upload is constant, padded after HALT, so instruction delivery and
scoreboard initialization affect the intercept rather than the K slope.

- **L1.TIMING.001** `RTL` `vc.timing.math_math` is the extra end-to-start
  delay for a math producer and a dependent ordinary math operand, including
  the register's last-beat completion. It is 20 cycles in all three records:
  `math_math` has dependent slope B+20 and independent slope B at B=1,4,8;
  all elapsed residuals are zero. It is not the initiation interval of
  independent math instructions. (records above, bench `math_math`)
- **L1.TIMING.002** `RTL` `vc.timing.xbar` is the additional RAW delay when
  a math-produced value is a crossbar operand of another math instruction.
  It is added to `math_math`, not to unpack-produced operands. `bcast` and
  `rot` chains both measure zero additional cycles at B=1,4,8; configuration
  costs are intercepts, not dependency latency. (records above, benches
  `xbar`, `xbar_rot`, `math_math`)
- **L1.TIMING.003** `RTL` `vc.timing.unpack_math` is the extra delay after
  an unpack's exclusive last-word end until a dependent math instruction may
  start. Both readers also use it for an unpack-to-pack RAW edge. It is 9
  cycles: repeated same-register unpack has slope B+9; the unpack/compare
  read-release ring has slope 2B+10. The ring includes a one-cycle registered
  read-release return edge. Both calibrations agree for B=1,4,8, with zero
  residuals. Independent controls retain resource contention and their
  nonzero residuals are recorded, not treated as latency measurements.
  (records above, benches `unpack_waw`, `unpack_ring`)
- **L1.TIMING.004** `RTL` `vc.timing.math_pack` is the extra delay after a
  math producer's exclusive last-beat end until a dependent pack can start
  reading. It is 20 cycles. The math/pack read-release ring has slope 2B+21;
  subtract its two B-beat operations and the one-cycle registered return
  edge. The math-chain calibration independently gives the same value:
  both consumers observe the same last math-write completion. This is not
  the pack's output-retirement delay. (records above, benches `pack_ring`,
  `math_math`)
- **L1.TIMING.005** `RTL` `vc.timing.cond` is the ready one-word front-end
  condition instruction's dispatch occupancy, 2 cycles. Satisfied
  `vsync fe on=math` and register `setvl s0` independently have slope 2.
  SETI has a second immediate word and measures slope 3, not 2; changing
  its destination does not remove that cost. The shared timing reader charges
  SETI `seti` and explicit SETVL `cond`, including when L2 prices an image.
  (records above, benches `cond_sync`, `cond_setvl`, `cond_seti`)
- **L1.TIMING.006** `RTL` `vc.timing.mx7_pack_words` is the MX7 pack's
  active RF-read beat count, the active component of pack occupancy. It is
  16 for both `mx7a` and `mx7b` (`V2PACK.act` slope 16). It is neither
  output L1 words nor words per cycle. Their elapsed slope is 17, exposing
  a one-cycle inter-instruction gap beyond those active beats; the scalar
  must not be changed to 17 while retaining its active-word meaning.
  (records above, benches `mx7a`, `mx7b`)

- **L1.TIMING.007** `RTL` `vc.timing.seti` is the complete front-end
  dispatch cost of SETI, including its immediate word and fetch bubble: 3
  cycles. Repeated SETI to one scalar and rotating destinations both have
  slope 3 with zero residuals. The shared L1/L2 image timing reader uses this
  value rather than the one-word `cond` cost. (records above, `cond_seti`)
- **L1.TIMING.008** `RTL` `vc.timing.mx7_issue_gap` is the non-active
  inter-instruction gap of repeated MX7 packs: elapsed slope minus the
  `V2PACK.act` slope, 17 - 16 = 1 cycle. MX7A and MX7B independently agree
  with zero residuals. L1 timing and L2 modulo reserve
  `mx7_pack_words + mx7_issue_gap` cycles per MX7 pack; neither changes the
  active RF-read count to include the gap. (records above, `mx7a`, `mx7b`)

The probes' return-edge equations refer to the registered source-read release
at `m_end`/`p_endw` in `v2_core.v`, not VSYNC's later output retirement.
Every clause has a frozen tier-2 vector in `software/tests/spec/vectors/l1/timing`;
the runner reruns its calibrations and rejects a machine value offset by one.
RUL.063's status follows the convention of the rulings page.

## 1. Text

- **L1.TXT.001** `silicon` A statement is `[TARGET =] OP ARGS [: ANNOTATION]` on one line, optionally followed by a block of statements indented under it. (code: T/ktpu.lark:7-17; doc: ir-text:22-29; evidence: every one of the 455 kernels is read through this grammar)
- **L1.TXT.002** `silicon` A block is indented deeper than its parent; the printer writes 4 columns a level (F-021). (code: T/syntax.py:226-239; evidence: 455 kernels)
- **L1.TXT.003** `silicon` A newline inside `( )`, `[ ]` or `{ }` does not end a statement. (code: T/syntax.py:228-229; doc: ir-text:27-28; evidence: 455 kernels)
- **L1.TXT.004** `silicon` `#` starts a comment to the end of the line; a comment-only line is no statement. (code: T/ktpu.lark:101-104; evidence: 455 kernels)
- **L1.TXT.005** `silicon` Arguments are separated by spaces or by commas; a statement records which (`Stmt.commas`) and prints back with it. (code: T/ktpu.lark:20-21, T/syntax.py:362-364, 580-591; evidence: 455 kernels)
- **L1.TXT.006** `silicon` An argument `NAME=TERM` is a keyword argument (`Stmt.kwargs`); every other argument is positional (`Stmt.positional`), including `TERM = TERM` whose left side is not a name. (code: T/syntax.py:210-222; evidence: 455 kernels)
- **L1.TXT.007** `silicon` Terms: a name (letters, digits, `_`, a dot followed by a letter), a decimal integer with `_` separators, a hexadecimal `0x...`, a size `16K` / `1M` / `1G` (shift 10 / 20 / 30), a float (`1.5`, `1e-05`, `inf`, `nan`), dims `64x64x2`, a string without quote or newline. (code: T/ktpu.lark:86-99, T/syntax.py:247-253; doc: ir-text:35-53; evidence: 455 kernels)
- **L1.TXT.008** `silicon` A name touching `(` is a call, touching `[` a view, touching `{` a computed name; a name and a bracket apart are two terms (`MG (1, 0)`). (code: T/ktpu.lark:82-89; doc: ir-text:55; evidence: 455 kernels)
- **L1.TXT.009** `silicon` Binary operators bind tighter than argument separation: `a -b` is one subtraction; `+` parses to `Offset`, `-` `*` `/` `%` to `BinOp`; unary `-`, `+`, `!`; `x @ y` is `Placed`. (code: T/ktpu.lark:33-47; doc: ir-text:56-57; evidence: 455 kernels)
- **L1.TXT.010** `silicon` `->` and `<-` are arrow terms and outrank the comparisons: `a <- b` is an arrow, `a < -b` a comparison. (code: T/ktpu.lark:78-80; evidence: 455 kernels)
- **L1.TXT.011** `unproven` A span is `LO..HI`, a membership `VAR in TERM`, an add-assign `TERM += TERM`, a tuple `()`, `(x,)` or `(x, y, ...)`; a view's slice is `TERM`, `LO : HI`, `LO :`, `: HI`, `:` or `*`. (code: T/ktpu.lark:24-31, 60-76; evidence: offline `software/language/tests/test_syntax.py`)
- **L1.TXT.012** `unproven` A binding target may be computed: `q{i*4 + u} = mark vc0`. (code: T/ktpu.lark:14, T/syntax.py:390-393; evidence: the 455 kernels use plain targets only; the computed form is offline-tested)
- **L1.TXT.013** `unproven` Canonical text: hexadecimal in groups of four digits (`0x8010_0000`), a size printed in K/M/G when whole, `inf` / `-inf` / `nan`, keyword arguments after positional ones, one-binding statements as `OP NAME = VALUE`, annotations of a block aligned two columns past its longest statement and at most column 48, a blank line around each top-level statement with a block. Reading canonical text and printing it gives the same text. (code: T/syntax.py:457-630; doc: ir-text:60-65; evidence: offline `test_syntax.py`)
- **L1.TXT.014** `unproven` Every parse or meaning error is a `TextError` naming the file, line and column, followed by the source line and a caret; unexpected text is "unexpected character 'c'" or "unexpected TOKEN"; a text ending early names the tokens expected. (code: T/syntax.py:21-39, 428-453, T/reader.py:20-21; doc: ir-text:67-68; evidence: the reader of the 455 kernels; the messages are offline-tested)
- **L1.TXT.015** `unproven` A dedent to a column no enclosing block opened is a `TextError` "a dedent to no enclosing block's column". (code: T/syntax.py:433-436; evidence: refusal, offline only)

## 2. Module

- **L1.MOD.001** `silicon` `target`, `fn` and `image` are top-level statements of a module. (code: T/module.py:89-132; doc: ir-text:70-88; evidence: 455 kernels)
- **L1.MOD.002** `silicon` `target NAME` appears once. (code: T/module.py:96-105; evidence: `target ktpu.v9` in 455 / 455)
- **L1.MOD.003** `unproven` `vregs` is one of 32, 64, 128 and `vcopies` one of 2, 3, 4, else "vregs is one of (32, 64, 128)" / "vcopies is one of (2, 3, 4)"; left out they are 32 and 4, and `target_line()` prints only those that differ. (code: T/module.py:19-22, 56-66, 106-111; evidence: the silicon kernels name neither)
- **L1.MOD.004** `silicon` `fn NAME.LEVEL(p: TYPE, ...)` defines one body; LEVEL is `l2` or `l1`. (code: T/module.py:18, 112-117, 136-155; evidence: `<fn>` 455 / 455 (`l1`); every card kernel's L2 `fn NAME.l2` was read by the same reader before compiling)
- **L1.MOD.005** `silicon` `image NAME(params)` defines an image; a parameter is a name. (code: T/module.py:118-128; evidence: `<image>` 381 in 290 kernels)
- **L1.MOD.006** `RTL` `macro NAME(params)` is a top-level statement too. (code: T/module.py:89-132; evidence: E/l1/vector/softmax.ktpu's macros (L1.EXP.004): tl1::test_softmax; no card kernel holds one)
- **L1.MOD.010** `unproven` Any other top-level statement is refused: "no top-level statement 'X'; target, fn, image, macro". (code: T/module.py:89-132; evidence: refusal, offline only)
- **L1.MOD.007** `unproven` A second `target`, a non-name, or a keyword other than `vregs` / `vcopies` is refused "wanted one `target NAME [vregs=N] [vcopies=N]`". (code: T/module.py:96-105; evidence: refusal, offline only)
- **L1.MOD.008** `unproven` `fn ... -> TYPE` declares a result; `NAME.LEVEL` defined twice, a parameter not `name: type`, a missing level or an empty body is refused. (code: T/module.py:112-117, 136-155; evidence: no card kernel declares a result; refusals)
- **L1.MOD.009** `unproven` `image` and `macro` share one name table: a name defined twice in either is refused "'NAME' defined twice"; a definition with no statement is refused. (code: T/module.py:118-128, T/meta.py:278-285; evidence: refusals, offline only)

## 3. Read-time expansion

- **L1.EXP.001** `silicon` Before the L1 reader sees a body, the expander rewrites it against an environment of constants; the result holds no `for`, `when` or `expand`. (code: T/meta.py:1-8, 56-96; doc: ir-text:93-108; evidence: 455 kernels)
- **L1.EXP.002** `RTL` An L1 body unrolls every `for VAR in LO..HI` of constant bounds; the block is repeated for VAR = LO .. HI-1. (code: T/meta.py:98-119, ktpu-l1/src/lower/node/mod.rs; evidence: the compiled silicon kernels contain no `for`; hand L1 with `for` (E/l1/matmul/matmul.ktpu:19-20): tl1::test_matmul_four_clusters)
- **L1.EXP.003** `RTL` `when COND` keeps its block when COND folds to a nonzero constant and drops it when zero. (code: T/meta.py:65-75; evidence: E/l1/vector/softmax.ktpu:47, :73-77 (blocks kept and dropped): tl1::test_softmax)
- **L1.EXP.004** `RTL` `expand MACRO(args)` inlines the macro with its parameters bound, nested. (code: T/meta.py:121-136; evidence: E/l1/vector/softmax.ktpu:23, :36-37: tl1::test_softmax)
- **L1.EXP.008** `unproven` An unrolled `for` of non-constant bounds is refused "an unrolled `for` needs constant bounds". (code: T/meta.py:98-119; evidence: refusal, offline only)
- **L1.EXP.009** `unproven` In an L1 body a non-constant `when` condition is refused "`when` wants a constant condition". (code: T/meta.py:65-75; evidence: refusal, offline only)
- **L1.EXP.010** `unproven` An `expand` with a wrong argument count is refused; nesting deeper than 32 is refused "macro NAME expands itself". (code: T/meta.py:121-136; evidence: refusals, offline only)
- **L1.EXP.005** `unproven` Arithmetic on two constants folds; `/` on integers is floor division; `/` or `%` by zero is refused "division by zero"; a free name plus constants stays one `Offset(name, constant)`; comparisons of constants fold to 1 / 0. (code: T/meta.py:157-165, 212-223, 263-275; evidence: offline)
- **L1.EXP.006** `unproven` A computed name `v{expr}` folds to `vN` (or a view of it); in an L1 body a non-constant index is refused. (code: T/meta.py:166-181; evidence: offline)
- **L1.EXP.007** `unproven` A name the environment binds to a number is that number; a macro argument that is a name stays a name and, used as a statement's op, becomes the op; the left side of `=` or `+=` that is a name is never substituted. (code: T/meta.py:81-86, 141-142, 201-207; doc: ir-text:105-108; evidence: offline)

## 4. Body and nodes

- **L1.BODY.001** `silicon` An L1 body's parameters are buffers: the caller binds each to a byte address and the body is read with those addresses as constants. (code: ktpu-l1/src/lower/node/mod.rs; doc: ktpu:34-37; evidence: 455 kernels)
- **L1.BODY.002** `silicon` A parameter's type is `DTYPE[dims] @dram[(LAYOUT)]`. (code: ktpu-l1/src/layout/buffer.rs; evidence: every parameter of the 455 kernels is `@dram`)
- **L1.BODY.003** `RTL` A body is either one node's program or every top-level statement is a `node N` block, one program each on mesh N's view of the machine. (code: ktpu-l1/src/lower/node/mod.rs; doc: ktpu:36-37, 157-161; evidence: tl1::test_silu_on_two_nodes, test_ring_slot_and_bell (E/l1/multinode/ring.ktpu:16, :21))
- **L1.BODY.005** `unproven` A parameter left unbound is refused "NAME.l1: unbound parameters [...]". (code: ktpu-l1/src/lower/node/mod.rs; evidence: refusal, offline only)
- **L1.BODY.006** `RTL` A parameter's byte size (`Buffer.of(t).nbytes`) makes a span `{address: size}` of the program (`Program.spans`) the package relocates against (L1.LOW.013). (code: ktpu-l1/src/lower/node/mod.rs, ktpu-l0/src/program.rs; evidence: added since af7c1bb; every test_tinygrad.py case runs through it (`runtime/card.py:383` `build.packages`))
- **L1.BODY.007** `unproven` A parameter may be `@stage`; with `node` blocks a statement outside a block is refused "with `node` blocks, every statement is in one"; `node N` twice is refused. (code: ktpu-l1/src/layout/buffer.rs, ktpu-l1/src/lower/node/mod.rs; evidence: no card kernel or card test has a `@stage` parameter; refusals offline)
- **L1.BODY.004** `RTL` Nodes share memory and meet only through `ring` and `wait slot` / `wait bell`. (code: ktpu-l1/src/lower/node/mod.rs; doc: l1p:97-99; evidence: tl1::test_ring_slot_and_bell, test_remote_store)

## 5. Node statements

- **L1.NODE.001** `silicon` `mark` and `move` bind a name (`T = mark UNIT`, `T = move posted`); `send` and `move` take a block. (code: ktpu-l1/src/lower/node/mod.rs; evidence: `mark` 7752 / 165, `move posted` 119, `send` 8423 / 290, `move` 411 / 292)
- **L1.NODE.002** `silicon` `send`, `move`, `mark`, `wait` and `barrier` are node statements. (code: ktpu-l1/src/lower/node/mod.rs; doc: ktpu:39-57; evidence: `send`, `move`, `mark`, `wait`, `barrier` in the silicon tally)
- **L1.NODE.003** `RTL` Program registers are r0..r14. (code: ktpu-l1/src/lower/node/mod.rs, ktpu-l0/src/program.rs; doc: ktpu:59; evidence: E/l1/node/loop.ktpu (r1..r3): tl1::test_loop_runs_a_count_from_memory)
- **L1.NODE.004** `RTL` A field that takes a run-time value may be written `C + rN`. (code: ktpu-l1/src/lower/node/mod.rs; doc: ktpu:59-62; evidence: E/l1/matmul/tiles.ktpu:14 `x + c*16K + r3`, :18-19: tl1::test_tiles_from_a_count_in_memory)
- **L1.NODE.005** `RTL` `rN = set V` and `rN = OP rA, B` with OP `add` or `mul` and B a constant are one ALU step. (code: ktpu-l1/src/lower/node/mod.rs; doc: ktpu:52; evidence: E/l1/node/loop.ktpu:8, :10, :13, memops.ktpu:9: tl1::test_memops, test_loop_runs_a_count_from_memory)
- **L1.NODE.006** `RTL` `rN = load ADDR` loads 64 bits from memory; `rN = ctrl NAME` reads a control register (`xf_data`, `mvstat`). (code: ktpu-l1/src/lower/node/mod.rs; doc: ktpu:51; evidence: E/l1/node/memops.ktpu:8, :12, :15: tl1::test_memops)
- **L1.NODE.007** `RTL` `store ADDR <- V|rN` is a 64-bit store (an address in another mesh is a posted remote write); `ctrl NAME <- V` a control-region write; a constant value is taken mod 2^64. (code: ktpu-l1/src/lower/node/mod.rs; doc: ktpu:49-50; evidence: memops.ktpu:7, :10, :11: tl1::test_memops; remote.ktpu:9: test_remote_store)
- **L1.NODE.008** `RTL` `label NAME` marks the next step; `goto NAME if rA < rB` jumps when the unsigned comparison holds. (code: ktpu-l1/src/lower/node/mod.rs; doc: ktpu:53; evidence: E/l1/node/loop.ktpu:9, :14: tl1::test_loop_runs_a_count_from_memory)
- **L1.NODE.009** `unproven` A label defined twice is refused "label NAME twice" (a `ValueError`); a `goto` to no label is refused at build "goto NAME: no such label". (code: ktpu-l0/src/program.rs; evidence: offline Rust program tests (`ktpu-l0/tests/ported.rs`))
- **L1.NODE.010** `RTL` `ring mesh=M slot=S amount=N` rings mesh M's signal slot S by N, fenced; `amount` defaults to 1. (code: ktpu-l1/src/lower/node/mod.rs; doc: ktpu:47; evidence: E/l1/multinode/ring.ktpu:20: tl1::test_ring_slot_and_bell; remote.ktpu:10 (default amount): test_remote_store)
- **L1.NODE.011** `RTL` `wait slot S n=N consume` holds the node until slot S holds N, taking it; `wait bell M n=N` until N more rings from mesh M arrived. (code: ktpu-l1/src/lower/node/mod.rs; doc: ktpu:48; evidence: E/l1/multinode/ring.ktpu:22-23: tl1::test_ring_slot_and_bell)
- **L1.NODE.012** `RTL` `signal V32 V64` is a progress completion to the host; `settle N` a fixed wait of N cycles. (code: ktpu-l1/src/lower/node/mod.rs; doc: ktpu:54; evidence: E/l1/node/memops.ktpu:17-18: tl1::test_memops)
- **L1.NODE.013** `RTL` `push UNIT buf=B at=OFF <- ADDR n=N wait` sends one CU_DATA burst of N words read from ADDR, N in 1..`unit.burst_words` [256]; the descriptor carries buf [255:248], offset [247:232], N-1 [231:224], the signal flag [216] (set by `signal` or `wait`), the ack {y, x} [215:208]; `wait` waits for its DATA_RECEIVED. (code: ktpu-l1/src/lower/node/mod.rs; doc: ktpu:55; evidence: E/l1/node/push.ktpu:14: tl1::test_push_and_recv)
- **L1.NODE.014** `RTL` `recv n=N -> ADDR from=UNIT` takes N flits sent to the node from that unit into memory at ADDR + 32 i. (code: ktpu-l1/src/lower/node/mod.rs, ktpu-l0/src/program.rs; doc: ktpu:56; evidence: push.ktpu:17: tl1::test_push_and_recv; tonode.ktpu:12: test_a_drain_into_the_node)
- **L1.NODE.015** `RTL` `query UNIT index=I -> ADDR` sends a CU_CTRL with I at bit 240 and receives the one reply flit into ADDR. (code: ktpu-l1/src/lower/node/mod.rs; doc: ktpu:57; evidence: tl1::test_memops)
- **L1.NODE.016** `unproven` `fetch UNIT from ADDR n=N` makes the unit's fetch port read N words from ADDR when the node gets there; ADDR may be `rN` / `C + rN` (patched into step bits [127:64]); N is a constant. They count as sent to the unit; no lowering hook sees them. (code: ktpu-l1/src/lower/node/mod.rs, ktpu-l0/src/program.rs; doc: l1p:38; evidence: no example uses it)
- **L1.NODE.017** `silicon` `T = mark UNIT` names a point in UNIT's stream; `wait UNIT upto=T` holds the node until UNIT completed every word up to mark T, whatever was sent after. (code: ktpu-l1/src/lower/node/mod.rs, ktpu-l0/src/program.rs; doc: ktpu:45; evidence: `mark` 7752 / 165, `wait ... upto` 7752 / 165)
- **L1.NODE.018** `silicon` `barrier` takes nothing and holds the node until every unit completed everything sent to it. (code: ktpu-l1/src/lower/node/mod.rs; evidence: `barrier` 163 / 163)
- **L1.NODE.019** `silicon` `move` (a block of mover ops) is one mover step the node waits for, with every unit; `T = move posted` is one the node does not wait for. (code: ktpu-l1/src/lower/node/mover.rs; doc: ktpu:44; evidence: `move` 411 / 292, 119 `posted`)
- **L1.NODE.020** `silicon` `wait moves T` holds the node until the moves of post T and every move before them are done. (code: ktpu-l1/src/lower/node/mover.rs, ktpu-l0/src/program.rs; evidence: `wait moves` 119 / 119)
- **L1.NODE.021** `unproven` Only `mark`, `move`, the ALU ops, `load` and `ctrl` bind a name; any other statement with a target is refused "`OP` binds no name". Only `send` and `move` take a block; others are refused "`OP` takes no block". (code: ktpu-l1/src/lower/node/mod.rs; evidence: refusals, offline only)
- **L1.NODE.022** `unproven` `fetch`, `ring`, `store`, `ctrl`, `load`, the ALU ops, `signal`, `settle`, `label`, `goto`, `push`, `recv`, `query` are node statements too (each tagged by its clause, L1.NODE.003-.016); any other is refused "no L1 node statement 'X'". (code: ktpu-l1/src/lower/node/mod.rs; doc: ktpu:39-57; evidence: none is in a card kernel; refusal offline)
- **L1.NODE.023** `unproven` r15 is the patch scratch; naming it is refused "wanted a register r0..r14". (code: ktpu-l1/src/lower/node/mod.rs; evidence: refusal, offline only)
- **L1.NODE.024** `unproven` A run-time field may be a bare `rN`; anything but `C`, `rN` or `C + rN` is refused "wanted a constant, rN or C + rN (compute others with ALU ops)". (code: ktpu-l1/src/lower/node/mod.rs; evidence: no tl1 example writes a bare `rN` field; refusal)
- **L1.NODE.025** `unproven` `rN = set rM`; the ALU ops `sub and or xor shl shr min max divu remu`; a register B; a wrong operand count or a non-constant non-register B is refused. (code: ktpu-l1/src/lower/node/mod.rs; evidence: no tl1 example uses them; refusals)
- **L1.NODE.026** `unproven` `rN = load ADDR + rM`; `rN = ctrl OFFSET` with a numeric OFFSET or one of `dbell dbcnt irq il_ctl il_mesh il_ring il_consume sig il_select dcache xf_sel`. (code: ktpu-l1/src/lower/node/mod.rs; evidence: no tl1 example uses them)
- **L1.NODE.027** `unproven` `ctrl OFFSET <- rN` writes a register's value to the control region. (code: ktpu-l1/src/lower/node/mod.rs; evidence: memops.ktpu writes constants only)
- **L1.NODE.028** `unproven` `goto NAME` jumps always; `goto NAME if rA CMP B [signed]` with CMP `== != <= > >=`, a constant B, or `signed`; with a register B, `>` and `<=` swap the operands; with a constant k, `>` is `>= k+1` and `<=` is `< k+1`. (code: ktpu-l1/src/lower/node/mod.rs; evidence: loop.ktpu uses `<` of two registers only)
- **L1.NODE.029** `unproven` `ring`'s slot defaults to 0; `plain` drops the fence; any other flag or keyword is refused. (code: ktpu-l1/src/lower/node/mod.rs; evidence: no tl1 example; refusal)
- **L1.NODE.030** `unproven` `wait slot S n=N` without `consume` leaves the count; `wait bell M`'s n defaults to 1. (code: ktpu-l1/src/lower/node/mod.rs; evidence: ring.ktpu writes both `consume` and `n=`)
- **L1.NODE.031** `unproven` `signal V32` alone is a completion of one value; another value count is refused. (code: ktpu-l1/src/lower/node/mod.rs; evidence: memops.ktpu signals two values; refusal)
- **L1.NODE.032** `unproven` `push ... signal` sets the signal flag without waiting; `ack=UNIT` names the ack; N outside 1..256 is refused "a CU_DATA burst is 1..256 words". (code: ktpu-l1/src/lower/node/mod.rs; evidence: push.ktpu uses `wait` only; refusal)
- **L1.NODE.033** `unproven` `recv n=N -> ADDR` without `from=` takes flits from any unit. (code: ktpu-l1/src/lower/node/mod.rs; evidence: both tl1 examples name `from=`)
- **L1.NODE.034** `unproven` `wait UNIT` (no `upto=`) holds the node until UNIT completed every word sent to it; a `mark` without a target is refused "t = mark UNIT"; `upto=` naming no mark is refused, and a mark of another unit is refused "token N marks X, not Y". (code: ktpu-l1/src/lower/node/mod.rs, ktpu-l0/src/program.rs; evidence: no plain `wait UNIT` in the card kernels (every `wait` is `upto=` or `moves`); refusals offline)
- **L1.NODE.035** `unproven` `posted` without a target, or a target without `posted`, is refused; an empty `move` block is refused. (code: ktpu-l1/src/lower/node/mover.rs; evidence: refusals, offline only)
- **L1.NODE.036** `unproven` `wait moves T` with T not from `T = move posted` is refused. (code: ktpu-l1/src/lower/node/mover.rs; evidence: refusal, offline only)

## 6. Unit names

- **L1.UNIT.001** `silicon` A unit is `vc[i]` or `mg[i]`: the i-th coordinate, in sorted (x, y) order, of that kind on the program's mesh. (code: ktpu-l1/src/lower/node/mod.rs; evidence: `send vc[..]` / `send mg[..]` 8423 / 290)
- **L1.UNIT.002** `RTL` `meshM.mg[i]` names another mesh's unit, as a drain's destination. (code: ktpu-l1/src/lower/node/mod.rs; doc: ktpu:160-172; evidence: E/l1/multinode/xsplitk.ktpu:34-35 `drain ... to=mesh1.mg[0]`: tl1::test_split_k_across_meshes)
- **L1.UNIT.003** `unproven` `sr[i]` names a row unit likewise; an index past the count is refused "KIND[i]: this machine has N in mesh M". (code: ktpu-l1/src/lower/node/mod.rs; evidence: v9t6 has no row units; refusal offline)
- **L1.UNIT.004** `unproven` A statement other than a drain that sends to a unit in another mesh is refused "... is in mesh M: a node sends only into its own (H1)". (code: ktpu-l1/src/lower/node/mod.rs; evidence: refusal, offline only)

## 7. Cluster words (`send mg[i]`)

- **L1.MG.001** `silicon` A cluster's send block holds `fill`, `gemm`, `drain`; none binds a name or takes a block. (code: ktpu-l1/src/lower/node/cluster.rs; evidence: `fill` / `gemm` / `drain` in 15 matmul kernels)
- **L1.MG.002** `silicon` `fill SIDE <- ADDR n=N` streams N MXFP7 entries from ADDR into L1 side and bank SIDE (`A0 A1 B0 B1`). (code: ktpu-l1/src/lower/node/cluster.rs; doc: ktpu:68; evidence: `fill A0 <- a+0 n=64` form 8942 in 15 kernels)
- **L1.MG.014** `unproven` A cluster's send block holds `post` too (L1.MG.012); any other statement is refused "a cluster's send holds fill, gemm, drain, post; not 'X'". (code: ktpu-l1/src/lower/node/cluster.rs; evidence: no `post` in the card kernels; refusal offline)
- **L1.MG.015** `unproven` `fill ... at=E` starts at entry E; N may be a register; a bank other than `A0 A1 B0 B1` is refused. (code: ktpu-l1/src/lower/node/cluster.rs; evidence: no `at=` or register count in the card kernels or a tl1 example; refusal)
- **L1.MG.016** `RTL` A fill's ADDR may be `C + rN` (patched, L1.LOW.008). (code: ktpu-l1/src/lower/node/cluster.rs; evidence: E/l1/matmul/tiles.ktpu:14 `x + c*16K + r3`: tl1::test_tiles_from_a_count_in_memory)
- **L1.MG.003** `unproven` A FILL of more than `mg.max_fill` [255] entries is refused by the encoder "FILL of N entries exceeds the 255-entry streaming count; split it". (code: ktpu-isa/src/unit/cluster.rs; evidence: offline)
- **L1.MG.004** `RTL` `peers=` names up to `mg.max_peers` [3] sharers; the lowest {y, x} of the set reads once and every member receives; a follower must already be in this FILL when the entries come, which the program orders (follower's fills, `mark`, then the leader's words after a `wait` on that mark). (code: ktpu-isa/src/unit/cluster.rs, ktpu-isa/src/unit/cluster.rs; doc: ktpu:68; evidence: tl1::test_l2_shared_fills_on_two_nodes)
- **L1.MG.005** `RTL` `fill A0 recv n=N` fetches nothing: it waits until N entries landed on that side by COUNT bursts and takes them. (code: ktpu-l1/src/lower/node/cluster.rs; doc: ktpu:69; evidence: E/l1/matmul/splitk.ktpu:25: tl1::test_split_k_through_the_peer_accumulator)
- **L1.MG.006** `silicon` `gemm GMxGNxNK a=A? b=B? acc=0|1 [emit=ADDR]` is one sweep; anchor defaults to 40 (the stack's anchor, `2 * SBIAS`). (code: ktpu-l1/src/lower/node/cluster.rs; doc: ktpu:70; evidence: `gemm 64x8x1 a=A0 b=B0 acc=0` form 4471 / 15, no `anchor=` written)
- **L1.MG.007** `unproven` A GEMM's NK is 1 or a multiple of `mg.kstep` [2]; any other is refused "a GEMM of nk N: the pumped sweep takes K-blocks in pairs, and an odd nk >= 3 corrupts sub-tile (0, 0); use 1 or an even nk". (code: ktpu-isa/src/unit/cluster.rs, ktpu-isa/src/unit/cluster.rs; evidence: the measured corruption is on card_v9_1n; the refusal is offline-tested)
- **L1.MG.008** `unproven` `gemm ... emit to=UNIT l1=GRANULE [buf=] [count] [credit] [signal]` streams the tile's sub-tiles into a unit's buffer from its last sweep; a bare `emit` without `to=` is refused "gemm ... emit to=UNIT l1=GRANULE: emit names its sink". (code: ktpu-l1/src/lower/node/cluster.rs; doc: ktpu:71; evidence: the L2 lowering emits `emit to=` only for a stream (ktpu-l2/src/lower/cluster.rs), and `tilesilu`'s core `recv` is before its loop, so tl1::test_l2_tiles_drain_into_the_cores_and_overlap drains plainly (ktpu-l2/src/lower/cluster.rs); the hand uses (E/l1/fused/flashd.ktpu:253, :276) have no card test)
- **L1.MG.009** `silicon` `drain ADDR fused n=SUBTILES` writes the accumulator's sub-tiles to memory. (code: ktpu-l1/src/lower/node/cluster.rs; doc: ktpu:72; evidence: `drain h1+0 fused n=512` form 314 / 15, every card drain fused)
- **L1.MG.010** `RTL` `drain n=N to=UNIT l1=GRANULE [buf=0|2] [count]` writes into a unit's buffer at byte 32 x GRANULE (buf 0 a core's L1 or a cluster's L1 A, 2 the accumulator); `to=node` is the node's RX queue at (0, 0). (code: ktpu-l1/src/lower/node/cluster.rs; doc: ktpu:73-76; evidence: E/l1/matmul/splitk.ktpu:22-23: tl1::test_split_k_through_the_peer_accumulator; tonode.ktpu:11: test_a_drain_into_the_node)
- **L1.MG.011** `RTL` A drain to a unit in another mesh routes to this mesh's memory port with the far unit in `dfin`; its ack defaults to the far mesh's agent. (code: ktpu-l1/src/lower/node/cluster.rs; doc: ktpu:75; evidence: E/l1/multinode/xsplitk.ktpu:34: tl1::test_split_k_across_meshes)
- **L1.MG.012** `unproven` `post n=CREDITS to=UNIT` returns credits once no sweep reads L1; the unit is in this mesh ("a POST is a CU_SIGNAL inside this mesh"). (code: ktpu-l1/src/lower/node/cluster.rs; doc: ktpu:74; evidence: the L2 lowering emits `post` only for a stream receive (ktpu-l2/src/lower/cluster.rs) and `mlpdirect`'s `recv` is in no loop, so tl1::test_l2_mlp_with_nothing_in_memory_between_layers has none; the hand use (E/l1/sru/srflash.ktpu:57, :95) has no card test)
- **L1.MG.013** `silicon` A fused drain collects the oldest open emitting gemm of its cluster: its address, sub-tile count (GM x GN) and sink fields equal the emit's. (code: ktpu-l1/src/lower/node/cluster.rs; doc: ktpu:78-79; evidence: `emit=` / `fused` 628 uses in 15 kernels)
- **L1.MG.017** `unproven` `fill B? recv n=N`; any keyword but `n=` on a `fill .. recv` is refused. (code: ktpu-l1/src/lower/node/cluster.rs; evidence: splitk.ktpu receives on A0 only; refusal offline)
- **L1.MG.018** `unproven` A `gemm`'s `a` defaults to A0 and `b` to B0; `aoff=`, `boff=`, `anchor=`, `acc=EXPR` and the flags `count credit signal`; `a=` not naming an A bank or `b=` not a B bank is refused ("a= names an A bank, b= a B bank"); a flag outside `acc emit count credit signal` is refused. (code: ktpu-l1/src/lower/node/cluster.rs; evidence: every card gemm writes `a=`, `b=` and `acc=0|1` only; refusals offline)
- **L1.MG.019** `unproven` A drain to memory without `fused`; `mesh=M` aims it at mesh M through that mesh's first memory port, refused when that mesh names none. (code: ktpu-l1/src/lower/node/cluster.rs; evidence: every card drain is fused, none names `mesh=`; refusal)
- **L1.MG.020** `unproven` A unit drain's `buf=1` (L1 B), `signal`, `credit` and `ack=UNIT`; `count` and `credit` without `to=` are refused "count and credit are a drain to a unit's (to=UNIT)"; a flag outside `fused signal count credit` is refused. (code: ktpu-l1/src/lower/node/cluster.rs; evidence: no tl1 example uses them; refusals)
- **L1.MG.021** `unproven` A fused drain whose address, sub-tile count or sink differs from the open emit's is refused ("the open emit streams N sub-tiles to ADDR"); a fused drain with no open emit is refused; an emitting gemm no fused drain collects is refused at the end of the body. (code: ktpu-l1/src/lower/node/cluster.rs; evidence: refusals, offline only)

## 8. Vector sends (`send vc[i]`)

- **L1.VC.001** `silicon` A core's send block holds `desc` and `run`. (code: ktpu-l1/src/lower/node/mod.rs; doc: ktpu:43; evidence: `send` / `run` 8109 / 290)
- **L1.VC.002** `silicon` `run IMAGE(args)` sends the image's leading descriptors, the image's IMEM words unless the core last ran the same encoded image along straight-line node code, then a RUN at pc 0. (code: ktpu-l1/src/lower/node/mod.rs; doc: ktpu:149-151; evidence: 8109 runs)
- **L1.VC.003** `silicon` `desc aN=BASE walk=(STRIDE, BOUND)` or `walk=((S, B), ...)` writes all four dims of descriptor aN (an unused dim as (0, 0); a bound of 0 reads as 1); N in 0..`vc.descriptors`-1 [7]. (code: ktpu-l1/src/lower/image/mod.rs; doc: ktpu:142-147; evidence: `desc` 1275 / 290, `walk=((32, 8), (20480, 4))` form 231 / 80)
- **L1.VC.004** `unproven` A descriptor base written `rN` or `C + rN` is patched into the DESC word's `value` and `value_hi` fields when the node reaches it. (code: ktpu-l1/src/lower/node/mod.rs; doc: ktpu:60; evidence: E/l1/matmul/tiles.ktpu has no `send vc` or `desc` (its patches are cluster fills, L1.MG.016); no card witness of vector descriptor patches)
- **L1.VC.005** `RTL` A peer drain's base is `peer(OFF, count=1, fin=meshM.vc[i])`: granule OFF [15:0], the far mesh 0..3 [25:24] (from `fin=`), the far {y, x} [33:26], COUNT [34]. (code: ktpu-l1/src/lower/image/mod.rs; doc: ktpu:143-147; evidence: E/l1/multinode/xpeer.ktpu:10: tl1::test_vector_peer_drain_across_meshes)
- **L1.VC.006** `unproven` Any statement but `desc` and `run` in a core's send block is refused "a vector core's send holds desc and run, not 'X'". (code: ktpu-l1/src/lower/node/mod.rs; evidence: refusal, offline only)
- **L1.VC.007** `unproven` A `label` or `goto` forgets what each core holds. (code: ktpu-l1/src/lower/node/mod.rs; evidence: no card kernel has a label; no tl1 example runs an image after one)
- **L1.VC.008** `unproven` A `desc` of more than four dims, a non-constant dim, or a non-constant base is refused. (code: ktpu-l1/src/lower/image/mod.rs; evidence: refusals, offline only)
- **L1.VC.009** `unproven` A peer base's `ack=UNIT` ({y, x} [23:16]) and an explicit `mesh=M`; a `mesh=` other than `fin=`'s mesh is refused. (code: ktpu-l1/src/lower/image/mod.rs; evidence: xpeer.ktpu names neither; refusal)

- **L1.VC.010** `unproven` `send vc[i] repeats=N [increments=((word, bit, width, delta), ...)]` emits one L0 REPEAT template. N is 1..65535; the completion total is N times the encoded template length. Increments use the encoded send's word indices, require nonnegative deltas, and cannot wrap their fields. Register-patched sends and non-vector repeat sends are refused. Explicit repeat templates require external resident rewriting to be disabled; the L1 reader forgets the core's resident image after a repeated send. (code: Rust `ktpu-l1/src/lower/node/send.rs`, `ktpu-l0/src/program.rs`; evidence: host tests `ktpu-l1/tests/repeat.rs`, `ktpu-l0/tests/ported.rs`; no device evidence)

## 9. Vector images (`image NAME(params)`)

- **L1.IMG.001** `silicon` An image's parameters are constants bound by its `run`. (code: ktpu-l1/src/lower/image/mod.rs; doc: ktpu:108; evidence: 381 images, e.g. `run flat_af58b1a754_n0_vc0_0(in0+0, in1+0, out+0)`)
- **L1.IMG.002** `silicon` Leading `desc` statements are sent before the RUN and are no IMEM words. (code: ktpu-l1/src/lower/image/mod.rs; doc: ktpu:108-109; evidence: 381 images, `desc` 1275 / 290)
- **L1.IMG.003** `silicon` An image is encoded once per value of the parameters its instructions read: a parameter only the leading descriptors read changes the descriptors sent, never the IMEM words, so the instances of one shape share one encoding and one IMEM load. (code: ktpu-l1/src/lower/image/mod.rs; doc: ktpu:151-155; evidence: 8109 runs of 381 images)
- **L1.IMG.004** `silicon` No image statement binds a name; `loop` takes a block. (code: ktpu-l1/src/lower/image/mod.rs; evidence: 381 images, `loop` 160 / 137)
- **L1.IMG.005** `silicon` Math ops and their sources in field order: `vmov vexp2 vlog2 vinv vrsqrt` read a; `vmul vmax` read a, b; `vadd` reads a, c; `vsel` reads a, b, c. (code: ktpu-l1/src/lower/image/mod.rs, software/language/kohakutpu/language/l1/ops.py:5-27; doc: ktpu:113-116; evidence: vmul 7225, vadd 4441, vmov 1799, vsel 1272, vinv 664, vexp2 378, vmax 343, vrsqrt 131, vlog2 8)
- **L1.IMG.006** `RTL` `vsub` (reads a, c), `vfma`, `vfnma` (a, b, c), `vexp2d` and the compares `vcmplt`, `vcmpgt` (a, b) are encoded by the same rule; none is in the silicon kernels. (code: ktpu-l1/src/lower/image/mod.rs; evidence: `vsub` / `vfma` / `vfnma`: tl1::test_l2_layernorm; `vexp2d`: tl1::test_l2_sticky_softmax_on_both_paths; `vcmplt` / `vcmpgt`: test_tinygrad.py `floor_ceil`, `where` (compares the select pass keeps, kernels/expr.py:249-256); `vcmpgt` also `tests/api/test_tensor.py::test_op_on_the_card[where]`)
- **L1.IMG.007** `unproven` `vmin`, `vneg` and `vabs` are encoded by the same rule. (code: ktpu-l1/src/lower/image/mod.rs; evidence: Rust image-encoding witnesses only (`ktpu-l1/tests/ported/image.rs`))
- **L1.IMG.008** `silicon` A source is `vN` (N below `vc.regs`), `sN` (below 16), `kN` (below 4); a chunk select `vN[c:]` starts at chunk c stepping 1, `vN[c]` reads chunk c every beat, c in 0..`vc.chunks`-1 [7]. (code: ktpu-l1/src/lower/image/mod.rs; doc: ktpu:117; evidence: chunk selects `[0 :]`, `[4 :]` 6907 in 49 kernels (reduction trees), `bcast(v0[0], ...)` 1648 / 35, `k0` / `k1` sources 4144 / 133)
- **L1.IMG.009** `silicon` The crossbar `xor(v, K)` or `bcast(v, lane=L, sh=S)` applies to b, or to c reading b's input (named twice). (code: ktpu-l1/src/lower/image/mod.rs; doc: ktpu:118; evidence: `xor(` 1636 in 52 kernels (on `vmul` b, `vadd` c), `bcast(` 1648 / 35)
- **L1.IMG.010** `silicon` `vl=N` sets VL in 1..`vc.vlmax` [128], default 128. (code: ktpu-l1/src/lower/image/mod.rs; doc: ktpu:119; evidence: `vl=64` on vadd, vmax, vmov, vmul in the tally; every other math op takes the default)
- **L1.IMG.011** `RTL` `if=pN` predicates a math op on p0..p3 (the lanes where the predicate is set). (code: ktpu-l1/src/lower/image/mod.rs; doc: ktpu:119; evidence: `vmov ..., k1 if=p1` of the L2 compare lowering (ktpu-l2/src/lower/vector/mod.rs): test_tinygrad.py `where`, `floor_ceil`; `tests/api/test_tensor.py::test_op_on_the_card[where]`)
- **L1.IMG.012** `RTL` A compare writes predicate pN (p0..p3), unpredicated. (code: ktpu-l1/src/lower/image/mod.rs; doc: ktpu:116; evidence: as L1.IMG.011)
- **L1.IMG.013** `silicon` `vunpk vN <- l1[OFF] f16|gt4 [n=] [walk=(s0, s1)]` and `vpack vN -> l1[OFF] f16 [walk=]` move L1 words and registers; n defaults to the chunks from the first. (code: ktpu-l1/src/lower/image/mod.rs; doc: ktpu:120-121; evidence: vunpk 8581 (f16 8131, gt4 450, `n=1` 412, `walk=` 5928), vpack 5868 (f16 only, `walk=` 4731))
- **L1.IMG.014** `RTL` `vpack vN -> l1[OFF] mx7a` packs one whole register as one MXFP7 entry. (code: ktpu-l1/src/lower/image/mod.rs; doc: ktpu:121; evidence: E/l2/fused/mlp.ktpu:25 `pack o : mx7a`: tl1::test_l2_mlp_quantises_its_activation_on_the_core)
- **L1.IMG.015** `silicon` `vfill aN -> l1[OFF] [rel]` and `vdrain aN <- l1[OFF] rel` move memory through descriptor aN. (code: ktpu-l1/src/lower/image/mod.rs; doc: ktpu:122; evidence: vfill 3076 / 290 (rel 2872), vdrain 1891 / 290 (all rel))
- **L1.IMG.016** `silicon` `vsync WAITER on=KIND [slack=N]`: WAITER `fe unpack pack fill drain`, KIND `fill unpack pack drain`. (code: ktpu-l1/src/lower/image/mod.rs; doc: ktpu:123; evidence: vsync 7327 / 290 with waiters fe, unpack, pack, fill, drain, on= fill, unpack, pack, drain and slack 0..16, 63)
- **L1.IMG.017** `RTL` `vwait unpack rx n=N` waits for N more granules landed by COUNT bursts. (code: ktpu-l1/src/lower/image/mod.rs; doc: ktpu:124-125; evidence: `tilesilu`'s prologue `vwait unpack rx n=128` (ktpu-l2/src/lower/vector/mod.rs): tl1::test_l2_tiles_drain_into_the_cores_and_overlap)
- **L1.IMG.018** `silicon` `vbar` holds the unpack queue until the fills before it have landed. (code: ktpu-l1/src/lower/image/mod.rs; doc: ktpu:126; evidence: vbar 395 / 192)
- **L1.IMG.019** `unproven` `vmark mN on=KIND [slack=N]` (m0..m7) records a mark a `vsync WAITER mark=mN` waits on. (code: ktpu-l1/src/lower/image/mod.rs; doc: ktpu:123; evidence: Rust image-encoding witnesses (`ktpu-l1/tests/ported/image.rs`))
- **L1.IMG.020** `silicon` `ainc aN += BYTES` sets a descriptor's increment, BYTES in the signed `vc.ainc_bits` [18] range. (code: ktpu-l1/src/lower/image/mod.rs; doc: ktpu:127; evidence: ainc 612 / 192)
- **L1.IMG.021** `silicon` The encoder writes `ainc aN 0` first for every descriptor a `rel` transfer steps before the image's own `ainc` sets it. (code: ktpu-l1/src/lower/image/mod.rs; doc: verify:384-386; evidence: images with `rel` and no `ainc` in the silicon set)
- **L1.IMG.022** `RTL` `uptr N`, `uinc N` set the unpack pointer and increment. (code: ktpu-l1/src/lower/image/mod.rs; doc: ktpu:127; evidence: `tilesilu`'s banded unpack of its received tile (ktpu-l2/src/lower/vector/mod.rs): tl1::test_l2_tiles_drain_into_the_cores_and_overlap)
- **L1.IMG.023** `silicon` `seti sN = V` is two words: an integer is its low 24 bits, a float its E8M15 encoding. (code: ktpu-l1/src/lower/image/mod.rs; doc: ktpu:128; evidence: seti 320 / 185, floats (`seti s0 = 0.95`) 138 / 70, integers (`seti s15 = 469`) 160 / 137)
- **L1.IMG.024** `RTL` `getstk sN` reads (and clears) the sticky flag into sN. (code: ktpu-l1/src/lower/image/mod.rs, ktpu-l2/src/lower/vector/mod.rs; doc: ktpu:128; evidence: tl1::test_l2_sticky_softmax_on_both_paths)
- **L1.IMG.025** `unproven` `setvl sN` sets VL from a scalar; the encoder then knows no VL. (code: ktpu-l1/src/lower/image/mod.rs; doc: ktpu:128; evidence: offline)
- **L1.IMG.026** `silicon` `loop sN` (a block) is VLOOP over the block. (code: ktpu-l1/src/lower/image/mod.rs; doc: ktpu:129; evidence: loop 160 / 137)
- **L1.IMG.027** `RTL` `skipz sN` (a block) is VSKIPZ over the block; after it the encoder knows only the configuration fields both paths agree on. A block whose words pass VSKIPZ's skip field is encoded as consecutive VSKIPZ on sN, each over whole statements, when no statement in it writes sN (`seti`, `getstk`, `loop` on sN, or a raw `word`): every part then runs or none does. A single statement past the field, or a block that writes sN, is refused by the encoder. (code: ktpu-l1/src/lower/image/mod.rs, ktpu-l1/src/lower/image/flow.rs; doc: ktpu:140; evidence: tl1::test_l2_sticky_softmax_on_both_paths, skipz::a_long_skip_runs_as_consecutive_skips (255 words one VSKIPZ, 256 two), test_skipz_model (split block equals the hand-split block on the card model, register zero and non-zero))
- **L1.IMG.028** `silicon` Configuration is the encoder's unless written: it tracks VL, chunk selects, crossbar fields and walk strides, and emits a CHKVL / CHK / XB / USTR / PSTR word only where an op needs a value the core does not hold. An image enters with every field unknown (configuration persists across RUNs). (code: ktpu-l1/src/lower/image/mod.rs; doc: ktpu:133-138; evidence: every silicon image carries only encoder-chosen configuration words)
- **L1.IMG.029** `unproven` A written `chk a=(B, S) ...`, `chkvl vl=N ...`, `xb mode=M k=K sh=S [xc] [if=pN]`, `ustr S0 S1` or `pstr S0 S1` emits that word and sets the encoder's state, so an op needing the same adds none; `vcfg SEL PAYLOAD` and `word W` (32 bits) leave the state unknown. (code: ktpu-l1/src/lower/image/mod.rs; doc: ktpu:130-131; evidence: Rust image-encoding witnesses (`ktpu-l1/tests/ported/image.rs`))
- **L1.IMG.030** `silicon` A `loop` body is encoded twice -- entered in the state found, and in the state its own end leaves with the set-up hoisted before VLOOP -- and the shorter body wins, then the shorter set-up. (code: ktpu-l1/src/lower/image/mod.rs; doc: ktpu:138-140; evidence: 160 loops)
- **L1.IMG.031** `silicon` `halt` (no arguments) ends the image. (code: ktpu-l1/src/lower/image/mod.rs; evidence: halt 381 / 290)
- **L1.IMG.032** `unproven` `run` with the wrong argument count is refused "image NAME takes N arguments, got M"; a missing image is refused naming the images the module has. (code: ktpu-l1/src/lower/image/mod.rs; evidence: refusals, offline only)
- **L1.IMG.033** `unproven` `when` blocks holding only `desc`s lead an image like `desc`s; a `desc` after the first instruction is refused "an image's descriptors lead it: they are set before its RUN". (code: ktpu-l1/src/lower/image/mod.rs; evidence: no card image holds a `when`; refusal offline)
- **L1.IMG.034** `unproven` `skipz` takes a block too (L1.IMG.027); an unknown op is refused "no V2 instruction 'X'"; a math op of the wrong operand count is refused "OP d, a, ...: N operands". (code: ktpu-l1/src/lower/image/mod.rs; evidence: refusals, offline only)
- **L1.IMG.035** `unproven` A chunk select on an S or K register is refused. (code: ktpu-l1/src/lower/image/mod.rs; evidence: refusal, offline only)
- **L1.IMG.036** `RTL` The crossbar `rot(v, K)` and `merge(v, K)` apply the same way. (code: ktpu-l1/src/lower/image/mod.rs; evidence: `rot`: the L2 `xrot` of the lane relayout (L2.OP.010), test_tinygrad.py `narrow_transpose`, `odd_permute`; `merge`: the plain-row merge tree (L2.OP.011), tl1::test_l2_softmax, and the 16-a-word statistic pack (L2.STAT.003), test_tinygrad.py `sum_rows`; neither is in the tally)
- **L1.IMG.037** `unproven` A crossbar on a is refused "the crossbar is on b (and c with b's input)"; c through the crossbar while b is not is refused. (code: ktpu-l1/src/lower/image/mod.rs; evidence: refusals, offline only)
- **L1.IMG.038** `unproven` `vl=` outside 1..128 is refused "vl=N is not 1..128"; `vl=keep` keeps the core's VL. (code: ktpu-l1/src/lower/image/mod.rs; evidence: no `vl=keep` in the card kernels; refusal)
- **L1.IMG.039** `unproven` `unless=pN` predicates on the lanes where pN is clear; `if=` and `unless=` on one op are refused "one of if= / unless="; a compare with `if=` / `unless=` is refused "a compare writes its predicate unpredicated". (code: ktpu-l1/src/lower/image/mod.rs; evidence: the L2 lowering writes `if=` only; refusals offline)
- **L1.IMG.040** `unproven` `vunpk` / `vpack` take a chunk select `vN[cb:]` (n defaulting to the chunks from cb), the f32 mode, `rel` and `q=`; a pack in gt4 mode; a chunk select not stepping 1 is refused ("a walk starts at a chunk: vN[c:]"); a flag but `rel` is refused. (code: ktpu-l1/src/lower/image/mod.rs; evidence: no card kernel unpacks or packs so; refusals offline)
- **L1.IMG.041** `unproven` `vpack vN -> l1[OFF] mx7b`; a chunk select, `n=` or an unpack in MX7 mode is refused "an MX7 pack takes a whole register". (code: ktpu-l1/src/lower/image/mod.rs; evidence: `mlp` packs mx7a only; refusals offline)
- **L1.IMG.042** `RTL` `vdrain aN <- l1[OFF] to=(x, y)` is a peer drain. (code: ktpu-l1/src/lower/image/mod.rs; evidence: E/l1/multinode/xpeer.ktpu:13 `vdrain a1 <- l1[0] to=(0, 1)`: tl1::test_vector_peer_drain_across_meshes)
- **L1.IMG.043** `unproven` A peer drain's `to=UNIT`, `buf=B` and `signal`; `buf=` / `signal` without `to=` are refused "buf= and signal are a peer drain's (to=(x, y))"; a peer drain's `to=` naming another mesh is refused. (code: ktpu-l1/src/lower/image/mod.rs; evidence: refusals, offline only)
- **L1.IMG.044** `unproven` `vsync WAITER mark=mN` and `q=`; WAITER `math`, KIND `math`, `unpack1`, `pack1`; exactly one of `on=` / `mark=` (else refused); a mark's wait takes no slack. (code: ktpu-l1/src/lower/image/mod.rs; evidence: none is in the card kernels; refusals offline)
- **L1.IMG.045** `unproven` `vwait WAITER cr n=N` waits for N more credits; `q=`; a `mk` waiter is refused "a count is waited on, not marked"; `vpost UNIT n=N` sends a CREDIT of N from the drain queue, UNIT in this mesh. (code: ktpu-l1/src/lower/image/mod.rs; evidence: `cr` and `vpost` come only from a stream (ktpu-l2/src/lower/vector/mod.rs, ktpu-l2/src/lower/mod.rs) and no tl1 example streams; refusal)
- **L1.IMG.046** `unproven` `vbar q=1`; a positional argument is refused. (code: ktpu-l1/src/lower/image/mod.rs; evidence: every card `vbar` is bare; refusal)
- **L1.IMG.047** `unproven` `ainc` BYTES outside the signed 18-bit range is refused "ainc N: signed 18 bits". (code: ktpu-l1/src/lower/image/mod.rs; evidence: refusal, offline only)
- **L1.IMG.048** `unproven` `pptr N`, `pinc N` set the pack pointer and increment. (code: ktpu-l1/src/lower/image/mod.rs; evidence: the L2 lowering never emits them; only E/l1/fused/flash*.ktpu, which no card test runs)
- **L1.IMG.049** `unproven` `seti k3 = V`; a non-constant `seti` value is refused. (code: ktpu-l1/src/lower/image/mod.rs; evidence: no `seti k3` in the card kernels; refusal)
- **L1.IMG.050** `unproven` A loop inside a loop is refused "a loop inside a loop: VLOOP does not nest"; an empty loop is refused; a body past VLOOP's length field is refused by the encoder. (code: ktpu-l1/src/lower/image/mod.rs; evidence: refusals, offline only)
- **L1.IMG.051** `unproven` `vcmpeq` is encoded by the same rule. (code: ktpu-l1/src/lower/image/mod.rs; evidence: no card kernel or card test emits it)

## 10. Mover ops (`move` blocks)

- **L1.MOV.001** `silicon` A walk is `w(BASE, LEVEL, ...)`, levels outermost first, each `(COUNT, STRIDE)`, strides in bytes (whole `die.word_bytes` [32] words; negative steps back). (code: ktpu-l1/src/lower/node/mover.rs; doc: ktpu:83-88; evidence: `copy w(src+2432, (3, 10368), (14, 576), (14, 32), (5, -576), (5, -32)) -> ...` and every mover op of the 455 kernels)
- **L1.MOV.002** `silicon` `copy`, `quant`, `gt4` and `fill` are move ops. (code: ktpu-l1/src/lower/node/mover.rs; doc: ktpu:90-100; evidence: copy 1432, gt4 442, quant 172, fill 503)
- **L1.MOV.003** `silicon` `copy WALK -> WALK` copies any word permutation, the destination walk the iteration order. (code: ktpu-l1/src/lower/node/mover.rs; doc: ktpu:86-93; evidence: copy 1432 / 258)
- **L1.MOV.004** `RTL` `win1=` bounds walk axis 1: a source element outside reads the immediate `pad=V`, a destination element outside is not written. (code: ktpu-l1/src/lower/node/mover.rs; doc: ktpu:85-88; evidence: E/l1/mover/pad.ktpu:8 `win1=(-1, 16)` `pad=`: tl1::test_pad; mask.ktpu:7 destination `win1=(-4, 8)`: test_mask)
- **L1.MOV.005** `silicon` `fill WALK value=V [ew=16]` writes the 32-bit immediate, or its low 16 bits replicated. (code: ktpu-l1/src/lower/node/mover.rs; doc: ktpu:94; evidence: fill 503 uses, `ew=16` 194 / 30)
- **L1.MOV.006** `RTL` `gen WALK seed=S` writes pseudo-random words keyed by the seed and each word's address, independent of how the walk is split. (code: ktpu-l1/src/lower/node/mover.rs; doc: ktpu:95, 181; evidence: E/l1/mover/generate.ktpu:8, :11-12: tl1::test_generate)
- **L1.MOV.007** `RTL` `gather SRC -> WALK index=ADDR n=N pitch=P words=W [fp]` makes row r of the walk the W words at SRC + index[r] x P; `scatter WALK -> DST index=... n= pitch= words=` the reverse; `fp` reads fp32 indices. (code: ktpu-l1/src/lower/node/mover.rs; doc: ktpu:96-97; evidence: E/l1/mover/gather.ktpu:7-8 (with and without `fp`): tl1::test_gather; scatter.ktpu:7: test_scatter)
- **L1.MOV.008** `silicon` `quant WALK -> WALK [bpack]` converts FP16 entries to MXFP7 (A packing, B packing with `bpack`); `gt4 WALK -> WALK` transposes 4 x 4 granules; a transform move's element width is 16. (code: ktpu-l1/src/lower/node/mover.rs; doc: ktpu:98-100; evidence: quant 172 / 25 (`bpack` 19 / 13), gt4 442 / 38)
- **L1.MOV.009** `unproven` The forms `copy SRC -> DST bytes=N`, `quant SRC -> DST entries=N [bpack]` and `gt4 SRC -> DST groups=N` take plain addresses; any other keyword with them is refused. (code: ktpu-l1/src/lower/node/mover.rs; doc: ktpu:92; evidence: offline)
- **L1.MOV.010** `RTL` A register in a walk base is a PATCH of that field when the node reaches the step. (code: ktpu-l1/src/lower/node/mover.rs; doc: ktpu:102-104; evidence: E/l1/node/loop.ktpu:12 `copy w(x + r3, ...) -> w(y + r3, ...)`: tl1::test_loop_runs_a_count_from_memory)
- **L1.MOV.011** `unproven` A walk level may be `(COUNT, STRIDE, AXIS, ASTEP)`; a stride 0 repeats; `win2=` bounds axis 2; a walk without a level is refused. (code: ktpu-l1/src/lower/node/mover.rs; evidence: none in the card kernels or a tl1 example; refusal)
- **L1.MOV.012** `unproven` `xform`, `gen`, `gather` and `scatter` are move ops too (L1.MOV.006-.007 tag the last three); `xform WALK -> WALK id=I [mode=M]` runs any transform-bank occupant; any other op is refused "a move holds copy quant gt4 xform fill gen gather scatter; not 'X'". (code: ktpu-l1/src/lower/node/mover.rs; evidence: no `xform` in the card kernels or a card test; refusal)
- **L1.MOV.013** `unproven` `copy ... ew=` other than 8, 16, 32 is refused "ew= 8, 16 or 32". (code: ktpu-l1/src/lower/node/mover.rs; evidence: refusal, offline only)
- **L1.MOV.014** `unproven` `burst=N` caps a write burst at 2^(N-1) beats and `nocoal` turns write coalescing off. (code: ktpu-l1/src/lower/node/mover.rs; doc: ktpu:102-103; evidence: offline)
- **L1.MOV.015** `silicon` `fill ... ew=8`, `ew=16` and `ew=32` replicate the low 8 bits, replicate the low 16 bits, or write all 32. (code: ktpu-l1/src/lower/node/mover.rs; evidence: E/l1/mover/fill.ktpu `y8`, `y16`, `y32`: tl1::test_fill; vector `l1/mov/L1.MOV.015.card-fill` on the v9t6 card, 2026-10-22)
- **L1.MOV.016** `unproven` A `fill` value, a `gen` seed or a mover `imm`, `index`, `count`, `pitch`, `words` or level count may be a register (patched); `gen ... ew=`; a register for a field the move does not have is refused "no mover field NAME to set from a register". (code: ktpu-l1/src/lower/node/mover.rs; evidence: no tl1 example patches them; refusal)
- **L1.MOV.017** `unproven` `scatter ... fp`; a gather or scatter missing a keyword is refused. (code: ktpu-l1/src/lower/node/mover.rs; evidence: scatter.ktpu has no `fp`; refusal)

## 11. Row units (`send sr[i]`)

- **L1.SR.001** `unproven` A row unit's send block holds `job`, `pass`, `load`, `run`, `store`, `post`; its statements and fields are [../isa/sru.md](../isa/sru.md)'s; a `run` writes only the configuration words that differ from what the program last wrote to that unit, and a `label` / `goto` forgets them. (code: ktpu-l1/src/lower/sru.rs, ktpu-l1/src/lower/node/mod.rs; evidence: v9t6 has no row units)

## 12. Sync

- **L1.SYNC.001** `silicon` Within one unit, order is program order. (code: ktpu-l0/src/program.rs; doc: l1p:54; evidence: 455 kernels)
- **L1.SYNC.002** `silicon` Between units, sends inside one sync interval are unordered: what one unit must see from another is ordered by a `wait` / `barrier` between them, never by send order. (code: ktpu-l0/src/program.rs; doc: l1p:55-57; evidence: 455 kernels, ordered by the dispatch scheduler L1.VER.002)
- **L1.SYNC.003** `silicon` Every word sent to a unit returns one completion; a wait counts completions. (code: ktpu-l0/src/program.rs; doc: l1p:58-59; evidence: 455 kernels)
- **L1.SYNC.004** `RTL` A unit's words retire once their flits left: a burst's landing is seen only by its receiver (COUNT into a core's L1 or a cluster's A / B), and an accumulator add is seen by a COUNTed entry its sender drains right behind it. (code: ktpu-l2/src/lower/sync.rs; doc: ktpu:169; evidence: accumulator: E/l1/matmul/splitk.ktpu:22-25: tl1::test_split_k_through_the_peer_accumulator; COUNT into a core's L1: xpeer.ktpu:10-13: test_vector_peer_drain_across_meshes)

## 13. Lowering to L0 (`Program.build`)

- **L1.LOW.001** `silicon` Each unit's sends in one sync interval become DISPATCH steps; past half the unit's credit (`unit.inst_depth` / 2 [256] words) a stream is cut into pieces of that size and the units' pieces interleave (A's first, B's first, A's next, ...). (code: ktpu-l0/src/program.rs; doc: l1p:63-72; evidence: 455 kernels)
- **L1.LOW.002** `silicon` A unit type with a lowering hook has its interval's words rewritten against the caller's `resident` state; `resident=None` runs no hook; a dispatch carrying patches runs no hook. (code: ktpu-l0/src/program.rs; doc: l1p:73-75; evidence: 455 kernels)
- **L1.LOW.003** `silicon` The vector core's hook (`imem.rewrite`): a program the core holds is sent as its DESC words and a RUN at its slot; a program placed over held words is sent as the words that differ; a DESC word writing the value its field holds is dropped; programs take slots starting below `vc.run_reach` [512] in `vc.imem_words` [1024], the least recently used evicted. (code: ktpu-l1/src/lower/imem.rs, ktpu-l1/src/build.rs; evidence: 455 kernels built against the cores' residents)
- **L1.LOW.004** `silicon` `wait` and `barrier` become AWAIT steps counting exactly the words dispatched to that unit since its last await, then (for `barrier`) BARRIER; an AWAIT is cumulative. (code: ktpu-l0/src/program.rs; doc: l1p:76-85; evidence: 455 kernels)
- **L1.LOW.005** `silicon` A `mark` dispatches every queued word at once and records how many the unit has been sent; `wait UNIT upto=T` awaits up to that count; the words after the mark stay queued in the unit. (code: ktpu-l0/src/program.rs; doc: l1p:82-85; evidence: 7752 marks)
- **L1.LOW.006** `silicon` A `move` is a MOVER step and a BARRIER; a posted move is a MOVER step flagged POSTED; `wait moves T` is an MWAIT counting the package's moves (writes of GO) up to that post's last. (code: ktpu-l0/src/program.rs; doc: l1p:78-80; evidence: 411 moves, 119 posted)
- **L1.LOW.007** `RTL` The node steps `store`, `load`, `alu`, `label` and `jump` dispatch every unit's pending words first, so a register a patch reads holds the value it had where the op was written. (code: ktpu-l0/src/program.rs; doc: l1p:86-88; evidence: E/l1/node/loop.ktpu:7-15 (load, ALU, label, jump, store around a patched move): tl1::test_loop_runs_a_count_from_memory)
- **L1.LOW.008** `RTL` A patch is an ALU into r15 (when its constant is nonzero) and a PATCH of each segment, right before the dispatch that sends its word; past a label a patched unit is awaited before its words are patched again. (code: ktpu-l0/src/program.rs; doc: l1p:88-93; evidence: tl1::test_tiles_from_a_count_in_memory)
- **L1.LOW.009** `unproven` A `wait moves` past a label is refused "`wait moves` past a label: its count is the package's moves so far, which a step run again changes; use a blocking move inside a loop". (code: ktpu-l0/src/program.rs; doc: l1p:95; evidence: offline)
- **L1.LOW.010** `unproven` A FETCH step follows the unit's queued words (the unit and every unit queued before it are flushed first) and counts its words as sent. (code: ktpu-l0/src/program.rs; doc: l1p:80-81; evidence: no example)
- **L1.LOW.011** `silicon` The program ends with every unit awaited to everything sent and a BARRIER; jumps are resolved to label step indices at the end. (code: ktpu-l0/src/program.rs; doc: l1p:80-81, 94; evidence: 455 kernels)
- **L1.LOW.012** `silicon` `build.package(prog, fetch, resident)` builds one node's package for the dispatch engine with no buffer defaults; `build.packages(progs, machine, fetch, residents)` is what a card is sent: every node's package in the dispatch scheduler's order. (code: ktpu-l1/src/build.rs; evidence: the 455 kernels' packages, built by `runtime/card.py:383` (unchanged since af7c1bb))
- **L1.LOW.013** `RTL` With `addresses` and `spans`, every address field inside one of the first `RELOC_BUFFERS` [256] bound buffers is a relocation, and an address in a buffer past those stays absolute: the package bytes name no absolute address inside the relocated buffers and run at whatever addresses they are bound to. (code: ktpu-l0/src/program.rs, ktpu-l0/src/build/mod.rs, ktpu-verify/src/check/mod.rs; evidence: added since af7c1bb (`Program.spans`, `verify._built`); every test_tinygrad.py case runs relocated builds through `runtime/card.py:383`)
- **L1.LOW.014** `unproven` IMEM words with no RUN after them are refused. (code: ktpu-l1/src/lower/imem.rs; evidence: refusal, offline only)
- **L1.LOW.015** `unproven` The node steps `ring`, `signal`, `settle`, `push`, `recv` and `send_raw` dispatch every unit's pending words first too. (code: ktpu-l0/src/program.rs; evidence: no tl1 example puts one before a patched word)
- **L1.LOW.016** `RTL` Each package `build.packages` returns is bound: its relocations applied (`Package.bind`), its bindings empty. (code: ktpu-l1/src/build.rs, ktpu-l0/src/format/bind.rs; evidence: added since af7c1bb; every test_tinygrad.py case runs bound packages (`runtime/card.py:383-402`))

## 14. Scheduling

- **L1.SCHED.001** `silicon` The image scheduler (`ktpu-l1/src/sched/schedule.rs`) list-schedules each run of consecutive movable statements of an image -- math ops, `vunpk`, `vpack` -- and leaves every other statement (`vfill vdrain vsync vwait vpost vmark vbar vsetvl vcfg vseti`, scalar ops, loops) in place as a bound of the runs around it; a statement with a target or a block is not movable. (code: ktpu-l1/src/sched/schedule.rs; doc: ktpu:472-474; evidence: the silicon images are its output)
- **L1.SCHED.002** `silicon` Dependences: vector registers (a chunk-selected destination also reads its register), scalar and predicate registers (a compare reads its destination; `if=` / `unless=` read the predicate), the L1 words an unpack reads or a pack writes (8 words unless `n=`), and any two unpack / pack statements whose L1 words are unknown. (code: ktpu-l1/src/sched/schedule.rs; evidence: silicon images)
- **L1.SCHED.003** `silicon` The issue model: dispatch one statement a cycle in order into per-engine queues of `vc.queue_depth` [8]; a configuration change (VL and chunk selects, or the crossbar) costs CFG_COST = 4 cycles of dispatch each; unpack and math share the register file's write port; an unpack or pack occupies its engine WALK = 8 cycles; a math op occupies max(1, VL / 16) beats; a math result is ready MATH_LATENCY = 20 cycles after the op starts; an unpacked register is ready at the unpack's end. (code: ktpu-l1/src/sched/schedule.rs; evidence: silicon images; the model's disagreement with `ktpu-l1/src/sched/timing.rs` is a ruling below)
- **L1.SCHED.004** `silicon` A run of fewer than 3 statements is kept; otherwise the scheduler repeatedly takes the ready statement that starts earliest under the model, ties to the written order, and keeps the new order only when the model times it at least MIN_GAIN = 1% faster than the written run. (code: ktpu-l1/src/sched/schedule.rs; evidence: silicon images)
- **L1.SCHED.005** `silicon` A whole image's reordering replaces the written image only when `l2.timing` times the whole image strictly faster; images are expanded first (macros, `for`, `when`), and the module is printed and read back. (code: ktpu-l1/src/sched/schedule.rs; evidence: silicon images)
- **L1.SCHED.006** `silicon` Scheduling is deterministic: the same module gives the same text (no randomness; ties keep the written order). (code: ktpu-l1/src/sched/schedule.rs; evidence: the compile cache keys on text, kohakutpu.stack.kernel)

## 15. Static verifier (`ktpu-verify`)

- **L1.VER.001** `silicon` `verify.schedule(programs, machine, fetch, residents)` builds each node's package against the residents (updated as the card will be) and runs them on the abstract machine with each node free: a segment (consecutive DISPATCH / REPEAT steps) runs in package order, and only when no process can act does a node send the earliest item heading its unit's queue that can move; the packages are rebuilt in the logged order. (code: ktpu-verify/src/check/mod.rs, ktpu-verify/src/sim/node.rs; doc: verify:234-269; evidence: every package of the 455 kernels was ordered by it; the diff since af7c1bb changes only what is built and keyed (relocated packages run bound, L1.LOW.013, L1.VER.003), not the order rule in ktpu-verify/src/sim/node.rs)
- **L1.VER.002** `silicon` A segment the program already orders well is left as written. (code: ktpu-verify/src/check/mod.rs; doc: verify:262-265; evidence: 455 kernels completed on the card)
- **L1.VER.016** `unproven` When no order completes, `schedule` raises `ScheduleError` with the deadlock findings ("no dispatch order completes"), a program error. (code: ktpu-verify/src/check/mod.rs; evidence: error path, offline only)
- **L1.VER.003** `unproven` An order found is kept by what decides it (the machine, the fetch port, the relocated package bytes, the buffers' overlaps, the held descriptors relative to the buffers) in memory and on disk (`build/schedule`, or `KTPU_SCHEDULE_CACHE`, empty for none), keyed also by the checker's source; a build is kept by program identity and the cores' whole state. (code: ktpu-verify/src/check/mod.rs; evidence: offline)
- **L1.VER.004** `unproven` `verify.check(programs, machine, fetch, runs=2)` schedules the program's first run and its rerun (the cores' programs then resident), runs them back to back on a fresh machine with the totals of a first run, and returns every finding; empty means none of the finding kinds can happen. (code: ktpu-verify/src/check/mod.rs; doc: verify:37-40; evidence: offline Rust verifier tests (`ktpu/tests/verify_ported.rs`))
- **L1.VER.005** `unproven` `verify.gate(programs, machine, fetch, mode, cache)`: mode `refuse` raises `Refused` when a finding is of a REFUSE kind (`deadlock rest port race bounds route line cluster`), `warn` returns every finding, `off` checks nothing; another mode is refused; with `cache` the findings are kept as `<cache>/<key>.json`, the key digesting the checker's source, the machine, the fetch port and each node's package built against empty cores, relocated. (code: ktpu-verify/src/check/gate.rs; doc: verify:44-50; evidence: offline)
- **L1.VER.006** `unproven` A `deadlock` finding: some process can never reach its end under every schedule. (code: ktpu-verify/src/model/mod.rs; doc: verify:24-26, 114-146; evidence: offline)
- **L1.VER.007** `unproven` A `rest` finding: after the rerun a counter is left above what its waiters took -- granules landed and never taken, credits received and never spent, a doorbell or slot count never taken, flits no RECV took, or a DATA_RECEIVED that may arrive after its package ends: the next run starts from another state. (code: ktpu-verify/src/sim/report.rs, ktpu-verify/src/sim/units/cluster.rs, ktpu-verify/src/sim/units/row.rs, ktpu-verify/src/sim/node.rs; doc: verify:27, 362-392; evidence: offline)
- **L1.VER.008** `unproven` A `port` finding: two bursts from different senders into one unit whose earlier landing does not happen before the later sending (P1); a burst into a shared fill's follower that may queue behind the lead's responses (P2); an L1 burst into a cluster that may land while a memory FILL's responses arrive (P3). (code: ktpu-verify/src/sim/report.rs; doc: verify:342-360; evidence: offline)
- **L1.VER.009** `unproven` A `race` finding: two accesses to one L1 location, one a write, neither happening before the other; at most 8 are kept a unit. (code: ktpu-verify/src/sim/memory.rs; doc: verify:300-340; evidence: offline)
- **L1.VER.010** `unproven` A `bounds` finding: a burst its receiver rejects (past a core's `vc.l1_words` [512], a buffer a core or row unit does not have, past a cluster buffer or an odd accumulator offset), a peer VDRAIN past `vc.walk_max` [256] words (F_LEN), or a node SEND of more words than its descriptor. (code: ktpu-verify/src/sim/card.rs, ktpu-verify/src/sim/units/vector.rs, ktpu-verify/src/sim/units/cluster.rs, ktpu-verify/src/sim/node.rs; doc: verify:30; evidence: offline)
- **L1.VER.011** `unproven` A `route` finding: a credit or a burst aimed where no unit is, or a package naming a coordinate where no unit is. (code: ktpu-verify/src/sim/card.rs, ktpu-verify/src/sim/node.rs; doc: verify:31; evidence: offline)
- **L1.VER.012** `unproven` A `loop` finding (reported, not refused): a JUMP on a loaded value, checked for 0..3 trips (`Card.trips`); a jump on registers computed by ALU on immediates runs exactly. (code: ktpu-verify/src/sim/card.rs, ktpu-verify/src/sim/node.rs; doc: verify:227-232; evidence: offline)
- **L1.VER.013** `unproven` A `model` finding (reported, not refused): what the machine does not decide -- a FETCH step, an unsupported non-vector instruction, or more than 2x10^8 events. VC instruction and control-flow inconsistencies are refusing `vector` findings (L1.VER.019). (code: ktpu-verify/src/sim/units/vector.rs, ktpu-verify/src/sim/node.rs, ktpu-verify/src/model/mod.rs; doc: verify:33, 207-225; evidence: offline)
- **L1.VER.014** `unproven` `verify.lint(module, machine)` reports `bank` findings: a math beat that holds or starves for want of register-file read copies at `vc.rf_copies` [2]; they are cycles, never wrong values, and `check` does not report them. (code: ktpu-verify/src/lint.rs; doc: verify:405-416; evidence: offline `test_banks.py`)
- **L1.VER.015** `unproven` A finding of a REFUSE kind means the run can hang, change its own next run, interleave flits at a receive port, read unordered L1 data, be rejected by a receiver, or address no unit; a clean `check` is a proof over every timing the machine admits, under the assumptions A1-A6. (code: ktpu-verify/src/lib.rs; doc: verify:114-191, 394-403; evidence: a soundness claim: the card runtime gated the 455 kernels' packages (`runtime/card.py:378`, `:423-435`, `check="refuse"`) and they ran to completion, which is consistent with it but witnesses no finding kind)

### External write-line ownership

- **L1.VER.017** `unproven` Within one bound package, two different mover or transform GO entries may not both write a strict subset of the same `card.dram.line_bytes` line. Coverage is the union of an entry's destination words, including transform expansion, not its bounding span. A destination walk is evaluated as contiguous runs: a dimension no window filters whose stride is plus or minus the run so far folds into the run, so the static budget (2,000,000 node steps and runs per package) counts runs, not words. Waits do not waive ownership. Constant node control flow and payload patches are evaluated. For data-dependent control flow, a finite known-bit interpretation joins every reachable branch and loop backedge; it accepts when every GO covers complete write lines, with any variable destination base bits above the line offset. Mover configuration starts unknown, and writes establish the required dimensions, windows and transform footprint. Other unknown write addresses, unsupported transforms and sub-line scatter rows without index-data ownership proof are refused conservatively as `line` findings. The verifier cache includes binding line phases and rounded-line overlaps. RECV writes incoming words to destination DRAM with the node CPU's byte-strobed stores; it does not replace package payloads, alter ENGINE opcodes or configure the mover. The mover projection therefore leaves its state unchanged at RECV. A later LOAD from that memory is still unknown: using received data to PATCH mover control must independently satisfy the known-bit proof. This rule assumes the same immutable package storage as other memory operations; it is not an allocation-containment or CPU/mover concurrency theorem. (code: `ktpu-verify/src/lines.rs`; witnesses: `ktpu-verify/tests/lines.rs`, mutants `verify.lines/off`, `verify.lines/no-dynamic-full-lines`, `verify.lines/recv-is-payload` and `verify.lines/word-runs`; RECV admission/execution also witnessed by the tier-1 tonode/push vectors)

### MG instruction consistency

- **L1.VER.018** `unproven` The package checker decodes MG words with the reference ISA, independent of the encoder. A `cluster` finding refuses an unknown opcode, nonzero reserved bits, nondefault unused fields, an illegal shape, count, bank, peer or destination, unknown drain flags, an unmatched fused DRAIN, an FP16 emission into a native-precision peer accumulator (that transfer requires a plain DRAIN), or a GEMM/ADD_PEER arithmetic read before its operand/accumulator entry is initialized. Plain DRAINs may also send padding or credit markers, so their count is bounded by accumulator capacity rather than the last GEMM's extent. Limits come from the ISA formats and machine facts. The abstract sequencer checks COUNT arrivals, shared-FILL rendezvous, credits and POST ordering; missing arrivals refuse as `deadlock`, surplus arrivals and uncollected emissions as `rest`. Existing counter, route and race diagnostics take precedence over consequent initialization diagnostics. Node-loop checks retain L1.VER.012's data-loop bound. (code: `ktpu-verify/src/cluster.rs`, `ktpu-verify/src/sim/units/cluster.rs`; witnesses: `ktpu-verify/tests/cluster.rs`, mutant `verify.cluster/off`; offline only)

### VC image consistency

- **L1.VER.019** `unproven` The package checker decodes VC CU words and images with the reference ISA, independently of encoder and decoder mutants. A refusing `vector` finding reports reserved CU/core/configuration bits, unknown configuration selectors, illegal source/sync fields, missing loaded instructions or VSETI immediates, control flow into immediates, nested or malformed loop bodies, unproven loop counts or VSETVL values, illegal unpack/pack geometry, and oversized or unaligned DMA descriptors. Both skip arms and scalar loop backedges are checked; scalar control state persists across RUNs and scheduled packages. Instruction-shaped immediate data is not executed. Legal chunk and L1 address wrapping, zero-trip loops executing once, and ignored single-queue selectors retain ISA semantics. Limits come from machine facts and ISA formats/constants. The abstract engines and package counters refuse unsatisfiable sync/completion waits as `deadlock`, surplus arrivals as `rest`, and invalid peer landings/destinations as `bounds`/`route`. VWAIT, VPOST, peer VDRAIN and early VHALT under a data-dependent skip are refused conservatively because their counter totals or termination paths are unproven; this includes bodies controlled by a conditionally skipped loop or skip header. Node loops retain L1.VER.012's bound. (code: `ktpu-verify/src/vector.rs`, `ktpu-verify/src/sim/units/vector.rs`; witnesses: `ktpu-verify/tests/vector.rs`, mutant `verify.vector/off`; offline only)

## Class-address updates

- **L1.MOV.018** `unproven` `move advance=1` is blocking. A register in a walk base adds its signed delta to the existing 40-bit payload base; the initial base remains an ordinary host relocation. Only whole base fields admit this mode. A subsequent visit to the statement advances the previously patched base. The node loop must supply the difference from its preceding visit, not an absolute offset. (code: software/rust/crates/ktpu-l1/src/lower/node/mover.rs; witness: software/application/tests/kernels/test_class_moves.py, `l0.program/advance-replace`)
- **L1.MG.022** `unproven` `send mg[I] advance=1` advances split address patches in their low field while preserving the separately bound high field. The host refuses a binding whose buffer crosses that low field's region. Other register patches, including `gemm ... acc=rN`, replace their fields. The send waits for its dispatched words; an array-idle POST is required before reusing a GEMM's operand banks (ISA.MG.048). (code: software/rust/crates/ktpu-l0/src/program.rs; witnesses: software/rust/crates/ktpu-l1/tests/repeat.rs, software/application/tests/kernels/test_class_mm.py, `l2.cluster/no-idle-post`)

## Rulings

The disagreements found on this page are ruled in `../rulings.md`: RUL.001, RUL.004, RUL.042, RUL.056, RUL.063, RUL.065, RUL.066, RUL.096.
