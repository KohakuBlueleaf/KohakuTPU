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

A program already in a core's IMEM is not sent again: the lowering places
each image in a slot and RUNs at it, so several stay resident.

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
| H3 | nothing reports a unit's remote flits landed: a fenced ring covers the mover and the processor, not a cluster's flits still in the meshes, so a consumer settles |
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
| `matmul/` | `matmul`, `longk` (compile to the hand L1's bytes), `splitk` (`drain ... add`) |
| `vector/` | `silu`, `add`, `softmax` (16-row tiles), `layernorm`, `leaky` (`cmpgt` / `sel`), `lazysoftmax` (the row max only when `if sticky`) |
| `fused/` | `ffn`, `swiglu` (production size: 1024 tokens, 1024 -> 2048 -> 1024 on four clusters and two cores, row blocks of 256 tokens, h / a2 holding two blocks so the pipeline pass overlaps the layers), `mmsilu` (a cluster tile, then silu on its drained GT4 sub-tiles), `mlp` (matmul, silu quantised to MXFP7 on the cores, matmul), `tilesilu` (tiles drained straight into the cores, `pipe=2`), `mlpdirect` (cluster -> core -> cluster, nothing in memory between the layers), `mmsoftmax` (row softmax of score tiles straight from the clusters: GT4 row reductions), `anysilu` (`tilesilu` written through an `@any` scratch) |
| `mover/` | `transpose` (`perm=`), `gather`, `scatter`, `fill`, `generate`, `convert` (`quant` A and B packing, `gt4`) |
| `multinode/` | `matmul2` (`share=cross`), `share2` (a small witness of shared fills), `silu2`, `matmul4` (four nodes) |

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

Verilated card models `build/l1cov/vlt_card_v9_{1,2,4}n_v2` (V2 cores), each
case on a card of its own, the second of two identical runs, checked against
a float64 numpy reference on MXFP7 operands (`tools.measure`, reports
`build/measure/{1n,2n,4n}/report.json`). MFU counts multiply-adds over 1,024
a cluster-cycle on every cluster the card has (4 a node); lane use, lane
beats over 16 a core-cycle on every core; B/cycle, the bytes a kernel moves.

| kernel | nodes | hand L1 | compiled L2 | efficiency (compiled) | rel err |
|---|---|---|---|---|---|
| matmul 1024³ | 1 | 312,797 | 312,797 | 83.8% MFU | 4.7e-4 |
| longk 1024×2048×1024 | 1 | 577,786 | 577,786 | 90.7% MFU | 4.4e-4 |
| splitk 256×256×1024 (2 clusters) | 1 | 74,732 | 74,732 | 21.9% MFU | 4.5e-4 |
| mmsilu | 1 | | 51,296 | 16.0% MFU | 4.8e-4 |
| mlp 256³ ×2 | 1 | | 66,987 | 12.2% MFU | 4.9e-3 |
| tilesilu 256³ (direct, `pipe=2`) | 1 | | 68,996 | 5.9% MFU | 6.7e-4 |
| mlpdirect | 1 | | 12,099 | 2.1% MFU | 1.6e-3 |
| mmsoftmax | 1 | | 27,697 | 3.7% MFU | 2.3e-3 |
| convert (quant A, B, gt4) | 1 | | 10,755 | 7.6 B/cycle | bit-exact |
| silu 512×128 | 1 | 13,505 | 13,667 | 74.9% lane | 2.6e-4 |
| add 512×128 | 1 | 12,759 | 12,866 | 30.6 B/cycle | 3.2e-4 |
| softmax 1024×128 | 1 | 33,433 | 34,281 | 62.5% lane | 3.9e-4 |
| lazysoftmax 1024×128 | 1 | | 32,169 | 52.5% lane | 3.9e-4 |
| leaky 512×128 | 1 | | 15,470 | 66.2% lane | 2.4e-5 |
| layernorm 1024×128 | 1 | 38,945 | 38,309 | 62.0% lane | 2.9e-4 |
| matmul2 1024×2048×2048 | 2 | 660,880 | 668,895 | 78.4% MFU | 4.4e-4 |
| share2 | 2 | | 73,145 | 44.8% MFU | 4.1e-4 |
| silu2 1024×128 | 2 | 17,060 | 17,164 | 44.7% lane | 3.8e-4 |
| matmul4 1024×4096×1024 | 4 | | 425,966 | 61.5% MFU | 4.2e-4 |
| flash 10 heads | 1 | 1,553,908 | | 21.1% MFU | 3.1e-2 |
| flash 2 heads a node | 1 | 348,406 | | 18.8% MFU | 2.3e-2 |
| flash 2 heads a node | 2 | 449,792 | | 14.6% MFU | 2.6e-2 |
| flash 2 heads a node | 4 | 578,587 | | 11.3% MFU | 2.6e-2 |

Flash rows: `build/measure/flash/report.json` (split by heads: every node runs
the same program on its own heads; MFU counts both products).

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
  rendezvous costs on one node); the four together 61.5% (68.0% the first
  run); with the weights in DRAM, 44.9%.
- **Vector.** Compiled within 0-3% of the hand images. `lazysoftmax` skips
  the max pass while no exponent reaches 2^8 (6% faster than `softmax` on
  N(0, 1) rows; a tile that overflows redoes it: 49,787 cycles at 8 N(0, 1)).
- **Flash.** S (20 MB a node per 10 heads) makes a memory round trip:
  cluster drain to DRAM, core read back. That route carries 24.7 B/cycle at
  best (S traffic alone, no softmax: 1,699,091 cycles for 10 heads = 19.3%
  attention-equivalent), so the kernel sits at its memory route. Removing
  the cores' packs or math moves the total by under 4%. Split by heads, the
  nodes share that memory: per node 1.29x the one-node time on two nodes,
  1.66x on four. A direct cluster -> core hand-off per block needs a
  unit-level landed-burst signal (H3), which the node can only stand in for.
- **Small fused tiles** (`tilesilu`, `mlpdirect`, `mmsoftmax`) are bound by
  the node's steps: a wait, a settle and the sends of an iteration cost
  several hundred cycles each against tiles of a few thousand.

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
| | `x = recv vc[H] : mx7a[M, K] @A` | nothing: the entries core H sent into this cluster's bank (`send`); a gemm reads them there (`aoff` / `boff`) |
| | `c = zero : f32[M, N] @acc`; `c = gemm a, b[, c] : f32[M, N] @acc` | the next sweep opens the tile; a GEMM |
| | `drain c -> VIEW` | an emitting last sweep plus a fused DRAIN placed before the unit's next emitting sweep, else a DRAIN |
| | `drain c -> vc[H]` | a DRAIN of the sub-tiles into core H's L1: a ring over its top 256 words (a `recv` there) |
| | `drain c -> mg[J] add` | J's sweeps waited, a peer DRAIN into J's accumulator, a settle; J's own drain plain |
| `par v in LO..HI on vc[E]` | `x = load VIEW : f16[R, C] @l1[(gt4)]` | a descriptor (`ainc` along the stream loop), filled one iteration ahead |
| | `h = recv mg[U] : f16[R, C] @l1(gt4)` (before the loop); `r = unpack h[ROWS, :]` (in it) | nothing: the tile cluster U drained here; each trip's row band unpacked through the unpack pointer (`uptr`, `uinc 8`) |
| | `r = unpack x : f32[...] @reg[(gt4)]`; `p = pack r : f16[...] @l1[(gt4)]`; `store VIEW <- p` | VUNPK / VPACK of each register; a VDRAIN after the iteration's packs |
| | `p = pack r : mx7a[4k, C] @l1`; `store A_VIEW <- p` | each GT4 block's chunks moved to the quantiser's order (8 VMOVs), a VPACK MX7 an entry; the drain walks the A packing (a chunk's `nk` entries, chunks `gm * nk` entries apart, bands `nk` apart) |
| | `send p -> mg[I] side=A\|B` (`p` packed MXFP7) | a peer VDRAIN of each trip's entries into cluster I's bank, the trips one after another (a granule is a quarter entry) |
| | `add sub mul max min exp2d fma fnma exp2 inv rsqrt log2 neg abs mov` | one instruction a register; a constant in an S or K register; `m[:, *]` a statistic broadcast to its row |
| | `m = cmplt\|cmpgt\|cmpeq a, b`; `o = sel m, a, b` | a compare into a predicate, then 1.0 / 0.0 by a move and a predicated move; VSEL (a where m is not 0, else b) |
| | `rmax`, `rsum` (rows to `f32[R] @reg`) | the merge tree over 8 rows; `rsum (mul a, b)` folds the product. Of `@reg(gt4)` rows (drained tiles): a 4-row band's registers folded, chunk 4 + i onto chunk i, an xor butterfly over the 16 lanes, chunks 0-3 copied to 4-7 -- one register a band whose every chunk holds its row's value, an elementwise operand to the band (`m[:, *]`) |
| | `if sticky` (a block of math) | the front end waits for the math, GETSTK, VSKIPZ over the block; the block's results overwrite their names' registers, so the skipped path keeps the values |
| node scope | `copy DST <- SRC [perm=]`, `fill`, `gen`, `gather`, `scatter` | one move of walks |
| | `quant DST <- SRC`; `gt4 DST <- SRC` (row-major f16 to an `mx7a` / `mx7b` buffer, or to `tile=` sub-tiles) | a transform move: the source walked in the destination's entry order, cut into moves of at most 16,384 source words |

**Order.** Each `par` of units and each run of node-scope moves is a stage.
A stage records what it touches: byte ranges of buffers, a core's L1 words,
a cluster's bank entries and its receive port (FILL responses and incoming
`CU_DATA` share one entry register). A later stage waits only for the units
of earlier stages it conflicts with (one writes what the other reads or
writes), up to a mark after that stage; a unit touching its own memory twice
is ordered by its own run. After a unit-to-unit transfer's write comes
`settle 256`: no unit reports such flits landed (H2, H3). Moves are posted;
the last stage of a program is a plain `move`, or a `barrier` follows it.

`for` inside a vector instance is the stream loop: its body runs twice a
VLOOP pass (two L1 buffers a stream), rows in groups of 4 (8 with a
reduction), the shape -- how many groups run together, whether a value is
re-unpacked after a reduction, whether the next group's unpacks go ahead --
chosen by a model of the V2 issue (in-order dispatch into 8-deep engine
queues, unpack words and math beats sharing the write port, a result 20
cycles after issue). `when` on an instance variable keeps a block per
instance.

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
(a core's packed output into a cluster bank) are written out or refused.
