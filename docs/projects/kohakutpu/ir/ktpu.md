---
title: ".ktpu: KohakuTPU kernels at L2 and L1"
summary: KohakuTPU's L1 text -- each unit's instruction stream and the node's steps, reaching every hardware feature -- and its L2 text (batches of work on named units) with the compiler between them; the hand-written examples, how to run them, and what they measure.
tags:
  - kohakutpu
  - ir
  - compiler
  - kernels
---

# `.ktpu`: KohakuTPU kernels at L2 and L1

A `.ktpu` file is a module ([docs/spec/ir-text.md](../../../spec/ir-text.md)
§2): `fn NAME.l2` and `fn NAME.l1` bodies, the vector-core `image`s they run
and the `macro`s they expand. An L1 body is one instruction stream per unit
plus the node's steps; it lowers one statement to one or a few package steps
([package-format.md](../../../spec/package-format.md)) through the framework's
L1 program ([l1-program.md](../../../spec/l1-program.md)). An L2 body is
batches of work on named units ([l2-program.md](../../../spec/l2-program.md),
§9 here); the L2 -> L1 compiler writes L1 text. No Python builds a kernel: a
runner loads the file, binds buffers to addresses and runs.

Code: `software/language/kohakutpu/language/` (`text/` the syntax and
modules, `l1/ops.py` the vector core's math facts, `numerics/`, `examples/`);
`software/compiler/kohakutpu/compiler/` (`emit/node.py` an `fn NAME.l1` body
to programs, `emit/image.py` an `image` to V2 IMEM words, `encode/` the unit
and mover ops, `layout/buffer.py` a parameter's memory layout, `build.py`).
Tests: `software/compiler/tests/` (encoders and L1 statements, against the
reference package interpreter), `software/application/tests/test_l1_examples.py`
(every example on the Verilated card).

## 1. A body

Parameters are buffers; the caller binds each to an address, and the body is
read with those addresses as constants. Every `for` unrolls; `when` keeps or
drops a block; `expand MACRO(args)` inlines a macro. A body is either one
node's program, or every statement is a `node N` block, one program each.

## 2. Node statements

| statement | step |
|---|---|
| `send vc[i]` (block of `desc`, `run`) / `send mg[i]` (block of `fill`, `gemm`, `drain`) | the unit's words (`DISPATCH`, or its fetch port) |
| `move` / `T = move posted` (block of mover ops, §4) | `MOVER`, waited for / not |
| `T = mark UNIT` / `wait UNIT [upto=T]` / `wait moves T` / `barrier` | `AWAIT`, `MWAIT`, `BARRIER` |
| `fetch UNIT from ADDR n=N` | `FETCH`: words a unit wrote, streamed into a unit |
| `ring mesh=M [slot=S] [amount=N] [plain]` | `RING`, fenced unless `plain` |
| `wait slot S n=N [consume]` / `wait bell M [n=N]` | `WAIT_SLOT` / `WAIT_BELL` |
| `store ADDR <- V \| rN` | `STORE`: a 64-bit store; another mesh's address is a posted remote write |
| `ctrl OFFSET <- V \| rN` | `CTRL`; offsets by name: `irq`, `il_ctl`, `il_mesh`, `il_ring`, `il_consume`, `il_select`, `dcache`, `xf_sel`, `xf_data` |
| `rN = load ADDR [+ rM]` / `rN = ctrl OFFSET` | `LOAD` from memory / a control register (`mvstat`, `dbcnt`, `sig`, `xf_data`, ...) |
| `rN = set B` / `rN = add\|sub\|mul\|and\|or\|xor\|shl\|shr\|min\|max\|divu\|remu rA, B` | `ALU`; `B` a constant or a register |
| `label NAME` / `goto NAME [if rA CMP B] [signed]` | a jump target / `JUMP` (`==` `!=` `<` `<=` `>` `>=`, unsigned unless `signed`) |
| `signal V32 [V64]` / `settle N` | `SIGNAL` / `SETTLE` |
| `push UNIT buf=B at=OFF <- ADDR n=N [signal] [ack=UNIT] [wait]` | `SEND` of a `CU_DATA` burst read from memory; `wait` waits for its `DATA_RECEIVED` |
| `recv n=N -> ADDR [from=UNIT]` | `RECV`: flits sent to the node, payloads into memory |
| `query UNIT index=I -> ADDR` | a `CU_CTRL` read (`SEND` + `RECV`): the reply's payload at `ADDR`, its value at bits `[239:176]` |

Registers are r0..r14 (r15 is the patch scratch). **Any field may be a
register:** `fill A0 <- x + r2 n=r3`, `desc a1 = b + r0`, `copy w(x + r3, ...)`,
`fetch vc[0] from x + r1 n=8` -- the word is encoded with the constant and a
`PATCH` sets the field from the register when the node reaches it.

## 3. Cluster words (`send mg[i]`)

| statement | |
|---|---|
| `fill A0\|A1\|B0\|B1 <- ADDR n=ENTRIES [at=ENTRY] [peers=(UNIT, ...)]` | MXFP7 entries into an L1 side and bank; with `peers` the lowest {y, x} of the set reads once and every member receives: a follower must be in this FILL when the entries come, which the node orders (follower's own fills, `mark`, then the leader's words after a `wait` on that mark) |
| `fill A0\|A1\|B0\|B1 recv n=ENTRIES` | no fetch: wait until another unit's COUNT bursts have landed `n` entries on that side, and take them ([isa.md](../isa.md) §5.4) |
| `gemm GMxGNxNK a=A? b=B? [acc \| acc=EXPR] [aoff=] [boff=] [emit=ADDR] [anchor=]` | one sweep; `emit=` streams the tile out of its last sweep |
| `gemm ... emit to=UNIT l1=GRANULE [buf=] [count] [credit] [signal]` | the same, streamed into a unit's buffer; the fused drain that collects it names the same sink and flags |
| `drain ADDR n=SUBTILES [fused] [mesh=M]` | the accumulator to memory, fused into an emitting sweep or not; `mesh=` to another mesh's memory |
| `drain n=N to=UNIT [buf=0\|1\|2] l1=GRANULE [signal] [count] [credit] [ack=UNIT]` | to a unit's buffer: a vector core's L1 (buffer 0), a cluster's L1 A or B, or its accumulator (2, an add at full precision); `count` flags the bursts for the sink's count, `credit` sends each burst only with credits for it |
| `post n=CREDITS to=UNIT` | once no sweep reads L1, return `n` credits to the unit that fills it |
| `drain n=N to=meshM.mg[i] buf=2 l1=0` | the same into another mesh: the flits leave by this mesh's memory port, the far coordinate rides in `dfin`, the ack defaults to the far agent |
| `drain n=N to=node l1=0` | to the node's RX queue, a descriptor and up to 8 sub-tiles a burst |

A fused drain pairs with the emitting gemm before it on that cluster; the
reader refuses one without its emit, and an emit no drain collects.

## 4. Mover ops (`move` blocks)

A walk is `w(BASE, LEVEL, ..., win1=(B, E), win2=(B, E))`, levels outermost
first, each `(COUNT, STRIDE)` or `(COUNT, STRIDE, AXIS, ASTEP)` (bytes,
multiples of 32; a stride of 0 repeats). `win1` / `win2` bound axes 1 and 2:
an element outside reads the immediate (a source) or is not written (a
destination). The destination walk is the iteration order, except a scatter's
and a transform's, which follow the source.

| op | |
|---|---|
| `copy SRC -> DST bytes=N` | contiguous |
| `copy WALK -> WALK [pad=V] [ew=8\|16\|32] [burst=N] [nocoal]` | any word permutation; `pad` the immediate a padded element reads |
| `fill WALK value=V [ew=]` | the 32-bit immediate, or its low 8 / 16 bits replicated |
| `gen WALK seed=S [ew=]` | pseudo-random words keyed by the seed and each word's address |
| `gather SRC -> WALK index=ADDR n=N pitch=P words=W [fp]` | row r of the walk := the W words at SRC + index[r] * P; `fp`: indices are fp32 |
| `scatter WALK -> DST index=ADDR n=N pitch=P words=W [fp]` | row r of the walk -> DST + index[r] * P |
| `quant SRC -> DST entries=N [bpack]` / `quant WALK -> WALK [bpack]` | FP16 entries to MXFP7, the A packing or the B |
| `gt4 SRC -> DST groups=N` / `gt4 WALK -> WALK` | 4 x 4 granule transpose |
| `xform WALK -> WALK id=I [mode=M]` | any transform-bank occupant |

`burst=N` caps a write burst at 2^(N-1) beats; `nocoal` turns write coalescing
off. Indices, counts, the immediate, the seed, pitch and row words take
registers.

## 5. V2 vector-core images (`image NAME(params)`)

An image's parameters are constants. Leading `desc` statements are sent before
its RUN and are not IMEM words; a `desc` after the first instruction is refused.

| form | instruction |
|---|---|
| `vadd d, a, c` `vsub d, a, c` `vmul d, a, b` `vmax` `vmin` `vexp2d d, a, b` | binary math; the sources an opcode reads, in field order a, b, c |
| `vfma d, a, b, c` `vfnma` `vsel` | three sources |
| `vmov d, a` `vneg` `vabs` `vexp2` `vlog2` `vinv` `vrsqrt` | unary |
| `vcmplt p1, a, b` `vcmpgt` `vcmpeq` | compare into a predicate |
| source `vN`, `sN`, `kN`; `v3[2:]` (from chunk 2, stride 1), `v3[0]` (chunk 0 every beat) | V, S or K selector and chunk select |
| `xor(v, K)` `rot(v, K)` `merge(v, K)` `bcast(v, lane=L, sh=S)` on b, or on c (b's input) | the crossbar |
| `vl=N` (default 128), `vl=keep`, `if=pN`, `unless=pN` | length and predication |
| `vunpk vN[cb:] <- l1[OFF] f16\|gt4\|f32 [n=] [walk=(s0, s1)] [rel] [q=]` | L1 to registers |
| `vpack vN[cb:] -> l1[OFF] f16\|gt4\|f32\|mx7a\|mx7b [n=] [walk=] [rel] [q=]` | registers to L1; MXFP7 packs a whole register |
| `vfill aN -> l1[OFF] [rel]` / `vdrain aN <- l1[OFF] [rel] [to=(x, y) buf=B signal]` | DMA through a descriptor; a peer drain |
| `vsync WAITER on=KIND [slack=N] [q=]` / `vsync WAITER mark=mN [q=]` / `vmark mN on=KIND [slack=N]` | WAITER `fe unpack math pack fill drain`; KIND `fill unpack math pack drain unpack1 pack1` |
| `vwait WAITER rx\|cr n=N [q=]` | WAITER waits for `n` more granules landed by COUNT bursts (`rx`) or `n` more credits (`cr`) ([vector-core-v2.md](../vector-core-v2.md) §4.1) |
| `vpost UNIT n=N` | a CREDIT of `n` to UNIT, from the drain queue |
| `vbar [q=1]` | unpack waits for fills |
| `ainc aN += BYTES` (signed 18 bits) / `uptr N` `uinc N` `pptr N` `pinc N` | descriptor and pointer increments |
| `seti sN = VALUE` / `seti k3 = VALUE` / `setvl sN` / `getstk sN` | scalars: an integer is 24 raw bits, a float is E8M15 |
| `loop sN` (block) / `skipz sN` (block) | VLOOP over the block (loops do not nest) / VSKIPZ over it |
| `chk a=(B, S) ...` / `chkvl vl=N a=...` / `xb mode=M k=K sh=S [xc] [if=pN]` / `ustr S0 S1` / `pstr S0 S1` | a config word written out |
| `halt` / `vcfg SEL PAYLOAD` / `word W` | VHALT, a raw config word, a raw word |

**Configuration is the encoder's unless written.** The encoder tracks the
core's VL, chunk selects, crossbar fields and walk strides, and emits a config
word only where an op needs a value the core does not hold; a config line
written out sets that state, and an op then adds none. Configuration persists
across RUNs, so an image enters in an unknown state unless its caller states
one. A `loop` body is encoded twice -- entered in the state found, and in the
state its own end leaves with the set-up hoisted before VLOOP -- and the
shorter body wins. After `skipz` only the fields both paths agree on are known.

**Descriptors** (`desc aN = BASE walk=(STRIDE, BOUND)` or `walk=((S, B), ...)`):
all four dims are written, an unused one as (0, 0). A peer drain's base is
`peer(OFF, ack=(x, y), mesh=M, fin=(x, y), count=1)`: the destination
granule, the ack's coordinate, for a sink in another mesh its id and
coordinate there -- the drain's `to=` then names this mesh's memory port --
and `count` flags the bursts for the sink's count.

A program already in a core's IMEM is not sent again: the node emitter skips
an image's words when the core last ran that same encoded image (along
straight-line node code; a `label` or `goto` forgets it). The L2 lowering
writes one image a distinct body -- every memory descriptor's base is a
parameter (`desc aK = p_aK`) the `run` binds (`run IMG(x+0, y+4096)`) -- so
the instances of one shape share an image: `linear` at 512 rows runs 16
instances of one image, 559 L1 lines.

## 6. Several nodes and other meshes

`node N` blocks give one program per node, each on its own mesh; the nodes
share memory, and order is a `ring` and a `wait slot`. `mg[i]` / `vc[i]` name
the node's own units; `meshM.mg[i]` names another mesh's, as a drain's
destination only (a node sends only into its own mesh: the mailbox marks no
flit remote, H1). What L1 cannot express, and why:

| | |
|---|---|
| H1 | the node's mailbox sends no flit to another mesh |
| H2 | the processor's coordinate (0, 0) is the "local / the sender" value of `dfin` and of an ack, so no unit can aim a remote burst or a `DATA_RECEIVED` at a node |
| H3 | a unit's words retire once its flits left, and a fenced ring covers the mover and the processor, not a cluster's flits still in the meshes: a burst's landing is seen only by its receiver -- COUNT for a vector core's L1 and a cluster's A / B; an accumulator add by a COUNTed entry its sender drains right behind it (one source's flits land in order) |
| H4 | a FILL's `peers` follower has no rendezvous of its own: the node's `mark` / `wait` is it (§3), at a node step a K chunk |
| H5 | a vector core's memory drain never crosses a mesh (no remote marker on `MEM_WR`); with shared memory it need not |
| H6 | the mailbox sets `last` on every flit it sends |
| H7 | the dispatch engine waits on no slot and no RX flit, so those steps drain it first |

## 7. Examples (`software/language/kohakutpu/language/examples/`)

Hand-written L1 (`l1/`), each a standalone file:

| area | examples |
|---|---|
| `mover/` | `transpose` (by walk order), `pad` (a bound axis and the immediate), `mask` (a destination window), `fill` (8/16/32-bit), `generate` (address-keyed, split-independent), `gather` (u32 and fp32 indices), `scatter` |
| `node/` | `loop` (a trip count loaded from memory, patched mover addresses), `memops` (store, load, ALU, transform-bank readback, `CU_CTRL` query, mover status), `push` (node -> core L1, core -> node peer drain, `recv`) |
| `matmul/` | `matmul` (1024^3 on four clusters), `longk` (1024 x 2048 x 1024), `tiles` (a data-dependent tile loop, patched cluster addresses), `splitk` (K over two clusters through the accumulator), `tonode` (a drain into the node) |
| `vector/` | `silu`, `add`, `softmax`, `layernorm` (two cores) |
| `fused/` | `flash` (online-softmax attention, 10 heads x 1024 x 1024, d = 64: scores on two clusters through a DRAM ring, P quantised to MXFP7 on the cores, P V on the other two), `flashd` (the same with S on the direct path: load-and-emit sub-rows into the cores' L1 rings under credits, P drained block by block into the P V clusters, the two key halves combined on the cores) |
| `multinode/` | `silu2`, `matmul2` (two nodes, tensor parallel, shared A fills), `ring` (slot and doorbell), `remote` (a processor store into another node's staging), `xsplitk` (K over two meshes through the accumulator), `xpeer` (a core's L1 into another mesh's core) |

Hand-written L2 (`l2/`), compiled to L1 by §9's pipeline:

| area | examples |
|---|---|
| `matmul/` | `matmul`, `longk` (compile to the hand L1's bytes), `splitk` (`drain ... add`); x an FP16 activation, w pre-quantised: `matmulq` (the cores quantise each 256-row block into a two-block DRAM ring while the clusters sweep the one before), `matmulqm` (the node mover's `quant` instead), `matmulqs` (the cores quantise all of x into staging, then the both-MXFP7 sweep) |
| `vector/` | `silu`, `add`, `softmax` (16-row tiles), `layernorm`, `leaky` (`cmpgt` / `sel`), `lazysoftmax` (the row max only when `if sticky`) |
| `fused/` | `ffn`, `swiglu` (production size: 1024 tokens, 1024 -> 2048 -> 1024 on four clusters and two cores, row blocks of 256 tokens, h / a2 holding two blocks so the pipeline pass overlaps the layers), `mmsilu` (four clusters' 64-row strips, then silu on the drained GT4 sub-tiles), `mlp` (matmul on four clusters, silu quantised to MXFP7 on the cores, matmul), `tilesilu` (tiles drained straight into the cores, `pipe=2`), `mlpdirect` (cluster -> core -> cluster, nothing in memory between the layers), `mmsoftmax` (row softmax of score tiles straight from the clusters: GT4 row reductions), `anysilu` (`tilesilu` written through an `@any` scratch) |
| `mover/` | `transpose` (`perm=`), `gather`, `scatter`, `fill`, `generate`, `convert` (`quant` A and B packing, `gt4`) |
| `multinode/` | `matmul2` (`share=cross`), `share2` (a small witness of shared fills), `silu2`, `matmul4` (four nodes), `xsplitk` (node 0 adds its K half into node 1's accumulators: `drain ... add node=1`) |

`python -m kohakutpu.application.tools.l1 --build MODEL [--nodes 0,1] NAME
[--out DIR]` runs an example (or any `.ktpu` file) on a Verilated card with
seeded inputs and reports cycles and lane beats;
`python -m kohakutpu.application.tools.measure --build MODEL --nodes ... --out
DIR` runs every example of that node count, hand L1 and compiled L2, checks
each against numpy and writes `report.json` / `report.md`. `KTPU_CARD1=MODEL
KTPU_CARD2=MODEL2 pytest software/application/tests/test_l1_examples.py` is
the card test (each example under a minute). `python -m
kohakutpu.application.tools.core EXAMPLE IMAGE ...` runs one image on the V2
core alone.

## 8. Measured

Verilated card models `build/v9f/vlt_card_v9_{1,2,4}n_v2` (V2 cores), each
case on a card of its own, the second of two identical runs, checked against
a float64 numpy reference on MXFP7 operands (`tools.measure`, reports
`build/measure/{1n,2n,4n}/report.json`). MFU counts multiply-adds over 1,024
a cluster-cycle on every cluster the card has (4 a node); lane use, lane
beats over 16 a core-cycle on every core; B/cycle, the bytes a kernel moves.

| kernel | nodes | hand L1 | compiled L2 | efficiency (compiled) | rel err |
|---|---|---|---|---|---|
| matmul 1024³ | 1 | 307,578 | 307,578 | 85.2% MFU | 4.7e-4 |
| longk 1024×2048×1024 | 1 | 571,771 | 571,771 | 91.7% MFU | 4.4e-4 |
| splitk 256×256×1024 (2 clusters) | 1 | 73,456 | 73,456 | 22.3% MFU | 4.5e-4 |
| mmsilu | 1 | | 51,664 | 15.9% MFU | 4.8e-4 |
| mlp 256³ ×2 | 1 | | 60,689 | 13.5% MFU | 4.9e-3 |
| ffn 1024×1024→2048→1024 | 1 | | 1,517,985 | 69.1% MFU | 4.9e-3 |
| swiglu 1024×1024→2048→1024 | 1 | | 2,439,751 | 64.5% MFU | 4.2e-3 |
| tilesilu 256³ (direct, `pipe=2`) | 1 | | 44,754 | 9.2% MFU | 6.7e-4 |
| mlpdirect | 1 | | 8,586 | 3.0% MFU | 1.6e-3 |
| mmsoftmax | 1 | | 23,148 | 4.4% MFU | 2.3e-3 |
| convert (quant A, B, gt4) | 1 | | 10,753 | 7.6 B/cycle | bit-exact |
| silu 512×128 | 1 | 13,516 | 13,728 | 74.6% lane | 2.6e-4 |
| add 512×128 | 1 | 13,343 | 13,078 | 30.1 B/cycle | 3.2e-4 |
| softmax 1024×128 | 1 | 33,441 | 34,495 | 62.2% lane | 3.9e-4 |
| lazysoftmax 1024×128 | 1 | | 32,177 | 52.5% lane | 3.9e-4 |
| leaky 512×128 | 1 | | 15,462 | 66.2% lane | 2.4e-5 |
| layernorm 1024×128 | 1 | 39,110 | 38,523 | 61.6% lane | 2.9e-4 |
| flash 10 heads (S via DRAM) | 1 | 1,571,091 | | 20.9% MFU | 1.5e-2 |
| flashd 10 heads (S direct) | 1 | 1,131,543 | | 28.9% MFU | 1.5e-2 |
| matmul2 1024×2048×2048 | 2 | 668,655 | 667,511 | 78.5% MFU | 4.4e-4 |
| share2 | 2 | | 75,081 | 43.6% MFU | 4.1e-4 |
| silu2 1024×128 | 2 | 17,169 | 17,277 | 44.5% lane | 3.8e-4 |
| matmul4 1024×4096×1024 | 4 | | 413,567 | 63.4% MFU | 4.2e-4 |
| flash 2 heads a node | 1 | 348,406 | | 18.8% MFU | 2.3e-2 |
| flash 2 heads a node | 2 | 449,792 | | 14.6% MFU | 2.6e-2 |
| flash 2 heads a node | 4 | 578,587 | | 11.3% MFU | 2.6e-2 |

Flash rows: `build/measure/flash/report.json` (split by heads: every node runs
the same program on its own heads; MFU counts both products). The flashd row:
`build/measure/c1/report.md`, which also has it on the V2.1 core (two
register-file read copies, card_v9t3): 1,146,024 cycles, 1.3% over the
four-copy core, from the combine's three-register FMA.

The V2.1 card (`build/simfix/vlt_card_v9t3_1n`: V2.1 cores, 128 registers,
two read copies; compiled with `--core v21`, the fill costs of `target.MEM`),
one node, the same method:

| kernel | cycles | efficiency | rel err |
|---|---|---|---|
| matmul 1024³ (hand L1 = compiled) | 308,917 | 84.9% MFU | 4.7e-4 |
| longk 1024×2048×1024 | 571,431 | 91.8% MFU | 4.4e-4 |
| splitk | 73,457 | 22.3% MFU | 4.5e-4 |
| matmulq (x FP16, cores quantise beside the sweep) | 424,818 | 61.7% MFU | 4.7e-4 |
| matmulqs (x FP16, cores quantise into staging first) | 397,659 | 65.9% MFU | 4.7e-4 |
| matmulqm (x FP16, the mover's quant) | 647,813 | 40.5% MFU | 4.7e-4 |
| ffn | 1,491,551 | 70.3% MFU | 4.9e-3 |
| swiglu | 2,418,921 | 65.0% MFU | 4.2e-3 |
| mmsilu (four clusters) | 41,599 | 19.7% MFU | 4.8e-4 |
| mlp (four clusters) | 49,412 | 16.6% MFU | 4.9e-3 |
| tilesilu / mlpdirect / mmsoftmax | 44,733 / 8,586 / 23,189 | 9.2 / 3.0 / 4.4% MFU | ≤2.3e-3 |
| silu / add / softmax / layernorm (L2) | 13,236 / 12,602 / 27,499 / 31,319 | 77.4% lane / 31.2 B/cycle / 79.7% / 75.8% | ≤3.9e-4 |
| lazysoftmax / leaky | 32,173 / 10,288 | 52.5% / 39.8% lane | ≤3.9e-4 |
| flashd | 1,146,024 | 28.6% MFU | 1.5e-2 |

`flash` is refused by the static checker ([verify.md](../verify.md) §11). An FP16 activation against
pre-quantised weights is bound by the node's 32 B/cycle read bus: the clusters
read 7.2 MB (A and B each four times over 256 x 256 tiles), x's FP16 adds 2
MB, a floor of ~288k cycles beside the sweep's 263k. Quantising first into
staging, then sweeping, leaves the cores' fills 41k cycles of waiting
(alone); quantising beside the sweep gets a core ~5 cycles a word. One read
of A for all four clusters (`share=all`) is slower on one node (374,182
against 311,698 at K chunks of 128): the rendezvous costs more than the
reads it saves.

Row normalisations by width and register budget (DSL kernels, card
`build/v9t3/vlt_card_v9t3_1n`: V2.1 cores of 128 registers, two read copies;
256 rows, cycles / lane use; `build/measure/b5/report.md`). Every budget gives
the same output bytes; a refused cell is "the stream loop needs more than N
vector registers".

| kernel | W | 32 registers | 64 registers | 128 registers |
|---|---|---|---|---|
| rmsnorm | 256 | 13,136 / 64.3% | 12,864 / 65.7% | 12,864 / 65.7% |
| rmsnorm | 512 | refused | 20,499 / 76.2% | 20,499 / 76.2% |
| rmsnorm | 1024 | refused | refused | 54,529 / 54.9% |
| layernorm | 256 | 32,163 / 47.8% | 19,599 / 78.4% | 19,597 / 78.4% |
| layernorm | 512 | refused | 44,349 / 64.7% | 35,604 / 84.4% |
| layernorm | 1024 | refused | refused | 86,329 / 64.1% |

What sets the numbers:

- **One node, matmul.** Sweeps alone run at 99.8%, fills with sweeps at
  97.1% (the node's 32 B/cycle read return bus), sweeps with drains at
  85.3%: four clusters emitting a tile at once hold their emitting sweeps
  ~11k cycles. A longer K covers it (90.7% at K = 2048, 91.7% at 4096).
- **Two nodes.** Both read the activations from shared memory (~52 B/cycle
  served to two nodes); one read of each A chunk a cross pair of clusters
  (`share=cross`, K chunks of 128) gives 79.3%.
- **Four nodes** (`build/measure/4n/probes.json`). A node's share alone runs
  at 84.0% with its weights in staging (76.2% with cross pairs, whose
  rendezvous costs on one node); the four together 63.4% (70.8% the first
  run); with the weights in DRAM, 44.9%.
- **Vector.** Compiled within 0-3% of the hand images. `lazysoftmax` skips
  the max pass while no exponent reaches 2^8 (6% faster than `softmax` on
  N(0, 1) rows; a tile that overflows redoes it: 49,787 cycles at 8 N(0, 1)).
- **Flash, S via DRAM** (`flash`). S (20 MB a node per 10 heads) makes a
  memory round trip: cluster drain to DRAM, core read back. That route
  carries 24.7 B/cycle at best (S traffic alone, no softmax: 1,699,091
  cycles for 10 heads = 19.3% attention-equivalent). Split by heads, the
  nodes share that memory: per node 1.29x the one-node time on two nodes,
  1.66x on four.
- **Flash, S direct** (`flashd`). The score clusters drain S into the cores'
  L1 under credits; the core counts landed granules (`vwait drain rx`).
  Core state profile (`build/measure/v9s_1n_attn/flashd.l1.model.log`):
  a 32-row tile costs a core ~3,690 cycles: MX7 pack 2,206 (128 registers
  at ~16.4), math 1,457, unpack waiting on the shared register-file write
  port ~600. Pack beside the write port bounds a tile at ~2,050 cycles
  (~50% MFU); the clusters wait on the cores.
- **Small fused tiles** (`tilesilu`, `mlpdirect`, `mmsoftmax`) are bound by
  the node's steps: a wait and the sends of an iteration cost several hundred
  cycles each against tiles of a few thousand.

## 9. L2 (`fn NAME.l2`)

The structure is [l2-program.md](../../../spec/l2-program.md)'s; these are
KohakuTPU's spaces and ops. Code: `software/compiler/kohakutpu/compiler/l2/`.

**Spaces**

| space | holds | bound |
|---|---|---|
| `@dram(LAYOUT)` / `@stage(LAYOUT[, node=N])` | a buffer; `node=` a node's own staging store | DRAM / 1 MB a node past its queue |
| `@A` / `@B` | a cluster L1 side: `mx7a[M, K]` / `mx7b[N, K]` | M/4 x K/32 entries <= 256 (a bank) |
| `@acc` | a cluster's accumulator tile, `f32[M, N]` | M/4 x N/4 sub-tiles <= 4096 |
| `@l1` / `@l1(gt4)` | a vector core's L1: rows of f16, or 4 x 4 sub-tiles | 512 words |
| `@reg` / `@reg(gt4)` | registers: one 128-value row each, or one 4 x 32 block each | 32 registers |
| `@mem` / `mg[any]` / `vc[any]` | left open for the `space` / `place` pass | |

**Statements**

| unit | statement | lowers to |
|---|---|---|
| `par v in LO..HI on node[E]` | | a `node N` block |
| `for v in LO..HI [pipe=K]` (node scope) | | its stages in order; with `pipe`, the next K - 1 iterations' words may be sent ahead of this one's waits |
| `par v in LO..HI on mg[E]` | `x = fill VIEW [share=cross\|port\|all] : mx7a[M, K] @A` | FILLs of the view's runs, a bank the latest sweep does not read; with `share`, one read for a group of clusters, the node the rendezvous |
| | `x = recv vc[H] : mx7a[M, K] @A` | `fill A recv n=` for the counted entries core H sent into this cluster's bank (`send`); a gemm reads them there (`aoff` / `boff`). Inside a loop, a **stream**: `fill A recv n=` waits for the counted entries in a two-slot ring from entry 0; the stage's first recv posts the ring as credits after the stage's own fills, each later one returns its slot after its last sweep but the last two |
| | `c = zero : f32[M, N] @acc`; `c = gemm a, b[, c] : f32[M, N] @acc`; `gemm a[LO : +R, :], b` | the next sweep opens the tile; a GEMM; a row slice of an operand is the same bank `LO / 4 * nk` entries in |
| | `drain c -> VIEW` | an emitting last sweep plus a fused DRAIN placed before the unit's next emitting sweep, else a DRAIN |
| | `drain c -> vc[H]` | a COUNTed DRAIN of the sub-tiles into core H's L1: a ring over its top 256 words (a `recv` there). To a core that receives inside its stream loop, a **stream**: the last sweep emits `to=vc[H] count credit` into a two-slot ring at the top of the L1, its fused drain before the next emitting sweep |
| | `drain c -> mg[J] add` | J's sweeps waited, a peer DRAIN into J's accumulator and a COUNTed entry into J's A0 entry 0 behind it, which J takes (`fill A0 recv`) before the rest of its stream; J's own drain plain |
| | `drain c -> mg[J] add node=N` | the same into node N's cluster J across the interlink (`to=meshN.mg[J]`): J opens its tile, rings this node (`ring mesh=.. slot=ADD+n`), which waits that slot before sending; one node's far adds go one at a time |
| `par v in LO..HI on vc[E]` | `x = load VIEW : f16[R, C] @l1[(gt4)]` | a descriptor (`ainc` along the stream loop), filled one iteration ahead |
| | `h = recv mg[U] : f16[R, C] @l1(gt4)` (before the loop); `r = unpack h[ROWS, :]` (in it) | `vwait unpack rx` for the tile's granules cluster U drained here; each trip's row band unpacked through the unpack pointer (`uptr`, `uinc 8`) |
| | `s = recv mg[U] : f16[R, C] @l1(gt4)` (in the loop) | a stream: each trip `vwait unpack rx` for the tile's granules in the ring slot of its parity, and once unpacked `vpost mg[U]` returns the slot; the core's open image posts the whole ring at the program's start, the cluster spends the last lap's credits at its end |
| | `r = unpack x : f32[...] @reg[(gt4)]`; `p = pack r : f16[...] @l1[(gt4)]`; `store VIEW <- p` | VUNPK / VPACK of each register; a VDRAIN after the iteration's packs. A store whose two buffers do not fit the L1 beside the loop's loads packs over the trip's load of the same words and layout (`vsync pack on=unpack` before the packs, `vsync fill on=drain` before the refill): a band of 4 rows x 1024 columns (256 words) double-buffers in the whole L1 |
| | `p = pack r : mx7a[4k, C] @l1`; `store A_VIEW <- p` | the op that made `r` (read by nothing else) writes it in the quantiser's chunk order, one chunk an op (GT4 chunk 4q + i to chunk 2i + q, every register operand chunk-selected alike), else 8 VMOVs; a VPACK MX7 an entry; the drain walks the A packing (a chunk's `nk` entries, chunks `gm * nk` entries apart, bands `nk` apart) |
| | `send p -> mg[I] side=A\|B` (`p` packed MXFP7) | a COUNT-flagged peer VDRAIN of each trip's entries into cluster I's bank, the trips one after another (a granule is a quarter entry); the cluster's `recv` takes them by `fill recv`. To a cluster that receives inside a loop, a stream: one descriptor a ring slot by parity, each trip after `vwait drain cr` for its granules |
| | `m = rmax x[:, C0:C1]` | the reduction over the 32-column blocks C0..C1 of each band |
| | `add sub mul max min exp2d fma fnma exp2 inv rsqrt log2 neg abs mov` | one instruction a register; a constant in an S or K register; `m[:, *]` a statistic broadcast to its row |
| | `m = cmplt\|cmpgt\|cmpeq a, b`; `o = sel m, a, b` | a compare into a predicate, then 1.0 / 0.0 by a move and a predicated move; VSEL (a where m is not 0, else b) |
| | `y = xlane x, K` | lane l of every 16-lane chunk takes lane `l xor K`: `vmul d, k1, xor(x, K)` a register (exact); with `sel` over a lane mask, any swap inside a chunk (a 4 x 4 transpose is two) |
| | `x = load VIEW : f16[1, C] @l1(gt4)` read by a band of R rows; `s = load VIEW : f16[R] @l1` | one row broadcast to every row of the band; a 1-D view loaded as a row statistic |
| | `rmax`, `rsum` (rows to `f32[R] @reg`) | the merge tree over 8 rows; `rsum (mul a, b)` folds the product. Of `@reg(gt4)` rows (drained tiles): a 4-row band's registers folded, chunk 4 + i onto chunk i, an xor butterfly over the 16 lanes, chunks 0-3 copied to 4-7 -- one register a band whose every chunk holds its row's value, an elementwise operand to the band (`m[:, *]`); `rsum (mul a, b)` there folds the product too (a multiply, then a fused multiply-add of the next register pair: the product is never held) |
| | `if sticky` (a block of math) | the front end waits for the math, GETSTK, VSKIPZ over the block; the block's results overwrite their names' registers, so the skipped path keeps the values |
| node scope | `copy DST <- SRC [perm=]`, `fill`, `gen`, `gather`, `scatter` | one move of walks |
| | `copy BUF <- BUF walk=((N, S), ...) [at=B] [dwalk=(...)] [dat=B]`; `fill BUF value=V walk=(...) [at=B]` | explicit signed byte walks of whole buffers (the last level whole words; a stride of 0 repeats): flips, permutes, broadcasts, windows |
| | `quant DST <- SRC`; `gt4 DST <- SRC` (row-major f16 to an `mx7a` / `mx7b` buffer, or to `tile=` sub-tiles) | a transform move: the source walked in the destination's entry order, cut into moves of at most 16,384 source words |
| | `gt4 BUF <- BUF walk=(..., (4, S)) dwalk=(...)` | any GT4 move: each group of four source words (the walk's last level) to four consecutive words, the destination stepping once a group |

**Order.** Each `par` of units and each run of node-scope moves is a stage.
A stage records what it touches: byte ranges of buffers, a core's L1 words,
a cluster's bank entries and its receive port (FILL responses and incoming
`CU_DATA` share one entry register). A later stage waits only for the units
of earlier stages it conflicts with (one writes what the other reads or
writes), up to a mark after that stage; a unit touching its own memory twice
is ordered by its own run. A unit-to-unit transfer's reader waits for its
landing itself (a COUNT it takes; H3), so the wait
on its sender only orders the reuse of the landing place. A stream's ring
orders itself (credits and counted arrivals): its accesses make no wait, and
consecutive stages joined by one are sent as one, the cores' RUNs first, the
clusters' segments interleaved by progress (a unit's queue holds a bounded
number of words). Moves are posted; the last stage of a program is a plain
`move`, or a `barrier` follows it. The node's dispatch order within a run of
DISPATCH steps is the dispatch scheduler's ([verify.md](../verify.md) §6),
and every package is checked by the static verifier.

**Several nodes.** A `par v on node[n]` body runs one program a node; the
nodes share memory. A stage carries its phase (one run of a `par` over the
nodes); when a stage of a later phase touches memory a stage of an earlier
phase on another node wrote (or reads what it read), the earlier node waits
its units and rings the later one (`ring mesh=B slot=ORDER+A`), which waits
that slot (`wait slot ORDER+A n=1 consume`) before it starts. A fill shared
by a group of clusters (`share=`) carries at most 64 B entries (a shared B
fill past 64 entries gives a follower wrong data, measured on the Verilated
card); the `share` pass leaves larger ones plain. A vector store of loaded
L1 bytes (no unpack / pack) orders the next fill behind its drain and the
drain behind its fill (`vsync drain on=fill`).

`for` inside a vector instance is the stream loop: its body runs twice a
VLOOP pass (two L1 buffers a stream), rows in groups of 4 (8 with a
reduction), the shape -- how many groups run together, whether a value is
re-unpacked after a reduction, whether the next group's unpacks go ahead --
chosen by a model of the V2 issue (in-order dispatch into 8-deep engine
queues, unpack words and math beats sharing the write port, a result 20
cycles after issue). `when` on an instance variable keeps a block per
instance.

The other lowering of a stream loop is a **modulo schedule** (`l2/modulo.py`):
one trip's code becomes an iteration started every II cycles, several in
flight; its registers rotate over K copies, coloured on a circle of K x II
(an arc starts at its time modulo the circle), each load and store a ring of
K L1 slots, each VSYNC's slack the exact count it may leave outstanding.
Before an image is kept its register dataflow is replayed in program order:
a read that would find another iteration's value fails the compile. The
lowering takes whichever of the two the image timing model (`l2/timing.py`)
times faster; a sticky block, a cluster stream, a banded unpack or a store
packed over its load keeps the band lowering.

Both price a fill by the machine's memory path (`target.MEM`, `target.mem`):
a latency before its first word and cycles a 32-byte word, measured on
card_v9t3_1n by a core's serial fills of 2 KB and 256 B. Alone (the node's
memory path to the cores only) 78 cycles and 2 a word (142.6 / 86.3 cycles a
fill; two cores split the node's 32 B/cycle read bus); beside a four-cluster
matmul 330 and 5 (664 / 371). A body whose vector stage shares a node loop
with a cluster stage (the pipeline pass overlaps them) takes the shared
figures; the modulo schedule issues an iteration's fills that latency plus
its words' cycles ahead of its first unpack.

**Passes** (`l2/passes.py`, `l2/pipeline.py compile_l1`): `place`, `space`,
`share` (in a body over several nodes, a fill whose view does not name the
cluster becomes `share=cross`), `split` (`gemm ... split`: the K loop),
`pipeline` (a node-scope `for` whose iterations run pars of two unit kinds
gets `pipe=2`; when the body's first `par` runs on units a later `par` of it
also runs on, the loop is rotated -- that `par` of iteration i + 1 ahead of
the rest of iteration i -- where no memory view of it meets one of the `par`s
it passes), `retile` (a vector stream loop's rows a trip: of 1/4 to 4
times the written tile, the one the issue model times fastest over all its
trips, kept only 2% better; a `par` inside node-scope loops costed at their
first iteration, its transfers landing at the top of the L1), the lowering,
then the L1 image
scheduler (`opt/schedule.py`: each run of math, unpack and pack
list-scheduled under the same issue model with a cost for each configuration
word, kept only when the model times it 1% faster).

`space` also decides `@any`: a scratch tile the clusters of one `par` drain
and only the cores of the next read back, a band of whole tile rows a trip,
becomes `drain c -> vc[..]` and `recv` when every core reads exactly one
cluster's tile and it fits the 256-word landing ring (`anysilu`, written
through memory, compiles to `tilesilu`'s L1 word for word); otherwise it is
memory like `@mem`.

**Not in L2.** A `for` inside a vector stream loop, a vector value carried
from one stage to the next (a running statistic across key blocks), any
run-time condition other than `sticky`, and the `send` direction of `@any`
(a core's packed output into a cluster bank) are written out or refused. A
stream's receiver takes an even number of transfers a stage (two slots).
