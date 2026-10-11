# `.ktpu`: KohakuTPU kernels as one text at every level

A `.ktpu` file is a module ([docs/spec/ir-text.md](../../../spec/ir-text.md)
§6): one kernel under one name, with a hand-written body at L3, L2 and L1, plus
the vector-core `image`s and `macro`s its L1 body uses. The hand bodies are the
references the compilers are gated against, and the hand L1 body is the
measurement of what the hardware does on that kernel. No Python builds a
kernel: a runner loads the file, binds buffers to addresses and runs.

Code: `compiler/kohakutpu/ktpu/` — `l1/node.py` (an `fn NAME.l1` body to an L1
`Program`), `l1/vector.py` (an `image` to V2 IMEM words), `l2/` (an
`fn NAME.l2` body read to nodes and verified), `l3/reference.py` (the L3 body
as the numeric reference and the work count), `buffers.py` (a parameter's
memory layout from its type). Kernels: `compiler/kohakutpu/ktpu/kernels/`.
Tests: `compiler/tests/ktpu/`.

## 1. Kernels

| file | L3 | L2 | L1 | images |
|---|---|---|---|---|
| `vector/silu.ktpu` | yes | yes | 2 cores, 65,536 values | `silu_stream` |
| `vector/add.ktpu` | yes | yes | 2 cores, 65,536 values | `add_stream` |
| `vector/softmax.ktpu` | yes | yes | 2 cores, 1024 x 128 rows | `softmax_pipe` (`softmax_rows`: 16-row lockstep form) |
| `vector/layernorm.ktpu` | yes | yes | 2 cores, 1024 x 128 rows | `layernorm_rows` |
| `fused/mlp.ktpu` | yes | yes | 4 clusters + 2 cores, 1024^3 twice | `silu_quant` |
| `fused/swiglu.ktpu` | yes | yes | 4 clusters + 2 cores, 1024^3 three times | `swiglu_quant` |
| `fused/flash.ktpu` | yes | yes | 4 clusters + 2 cores, 10 heads of 1024 x 1024, d = 64, online softmax | `flash_p`, `flash_y`, `flash_ones` |
| `fused/attention.ktpu` | yes | yes | 4 clusters + 2 cores, 1024 x 1024, d = 64, exact two-pass softmax | `attn_p`, `attn_out` |

The L3 bodies are read and verified by the L3 reader and run by its reference
interpreter. The L2 bodies are read and verified by the L2 reader (§4).

## 2. L1 node bodies (`fn NAME.l1`)

Parameters are buffers. The caller binds each to an address and the body is
expanded with those addresses as constants; every `for` unrolls.

| statement | meaning |
|---|---|
| `send vc[i]` (block of `desc`, `run`) | words to vector core `i` |
| `desc aN = ADDR walk=(STRIDE, BOUND)` / `walk=((S, B), ...)` | a descriptor's base and up to four dims; all four are written, an unused one as (0, 0) |
| `run IMAGE(args)` | the image's descriptors, its IMEM words, then RUN at 0; an image is encoded once per argument tuple |
| `send mg[i]` (block of `fill`, `gemm`, `drain`) | words to cluster `i` |
| `fill A0\|A1\|B0\|B1 <- ADDR n=ENTRIES [at=ENTRY]` | MXFP7 entries into an L1 side and bank |
| `gemm GMxGNxNK a=A? b=B? [acc \| acc=EXPR] [aoff=] [boff=] [emit=ADDR]` | one sweep; `emit=` drains the tile fused into the sweep |
| `drain ADDR n=SUBTILES [fused]` / `drain n=N to=vc[i] l1=WORD [flags=] [ack=(x, y)]` | accumulator out to memory, or to a core's L1 |
| `move` / `T = move posted` (block of `copy`, `quant`, `gt4`) | mover ops; `copy SRC -> DST bytes=N`, `quant SRC -> DST entries=N`, `gt4 SRC -> DST groups=N` |
| `T = mark UNIT` / `wait UNIT [upto=T]` | a completion token on a unit's stream, and a wait up to it (or for all) |
| `wait moves T` / `barrier` | a posted move's completion; every unit idle |
| `fetch UNIT from ADDR n=N` | words in memory streamed into a unit by its fetch port |

## 3. V2 vector-core images (`image NAME(params)`)

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
| `vbar [q=1]` | unpack waits for fills |
| `ainc aN += BYTES` (signed 18 bits) / `uptr N` `uinc N` `pptr N` `pinc N` | descriptor and pointer increments |
| `seti sN = VALUE` / `seti k3 = VALUE` / `setvl sN` / `getstk sN` | scalars: an integer is 24 raw bits, a float is E8M15 |
| `loop sN` (block) / `skipz sN` (block) | VLOOP over the block (loops do not nest) / VSKIPZ over it |
| `halt` / `vcfg SEL PAYLOAD` / `word W` | VHALT, a raw config word, a raw word |

**Configuration is the encoder's.** CHKVL, XB, USTR and PSTR are never
written. The encoder tracks the core's VL, chunk selects, crossbar fields and
walk strides, and emits a config word only where an op needs a value the core
does not hold; a field an op does not read keeps the value in force.
Configuration persists across RUNs, so an image enters in an unknown state
unless its caller states one. A `loop` body is encoded twice — entered in the
state found, and entered in the state its own end leaves with the set-up hoisted
before VLOOP — and the shorter body wins. After `skipz` only the fields both
paths agree on are known.

## 4. L2 bodies (`fn NAME.l2`)

An L2 body places tile work on units and orders it; it names no instruction.
Parameters are memory (`@dram`). Index expressions are integer arithmetic over
the enclosing loop variables (`+ - * / %`, `/` integer division).

| statement | meaning |
|---|---|
| `NAME = buffer : TYPE @dram` | a scratch buffer, at the top of the body |
| `par VAR in LO..HI on UNIT[EXPR]` (block) | instances `VAR`, instance `VAR` on `vc[EXPR]` or `mg[EXPR]` |
| `for VAR in LO..HI [pipe=K]` (block) | ordered iterations on the enclosing unit, `K` in flight |
| `NAME = load VIEW : TYPE @l1` | a memory view into a vector core |
| `NAME = OP ARGS [ATTR=C] : TYPE` | a vector op (`add` `sub` `mul` `max` `min` `exp2` `log2` `inv` `rsqrt` `neg` `abs` `copy` `fma` `sel` `reduce.max` `reduce.sum` `reduce.min` `quantise` `transpose`) on the instance's own values |
| `NAME = gemm A_VIEW, B_VIEW k=K : f32[M, N] @acc` | a cluster sweep, `A` an `mx7a [M, K]` view, `B` an `mx7b [N, K]` view |
| `NAME = drain ACC : TYPE @dram` / `store VIEW <- drain ACC` | the instance's accumulator out to memory |
| `store VIEW <- VALUE` / `store VIEW <- OP ARGS` | a value (or one op's result; a scalar broadcasts) into memory |

A view is `NAME[axis, ...]` with each axis `:` (whole), `LO : +SIZE`, `I` (one
element, the axis dropped) or `*` (a new broadcast axis).

**The verifier** (`l2/verify.py`) refuses, at the statement:

- a name not in scope, or a register value shadowing a memory name;
- a view outside its buffer for any value of its loop variables (every point up
  to 4,096, else the corners), or naming a variable that is not a loop's;
- a `par` index outside the machine's units;
- a vector op outside a `vc` instance, or on another instance's value, or on
  memory without a `load`; a load not into `@l1`;
- a `gemm` or `drain` outside an `mg` instance, a `gemm` whose operands are not
  `mx7a` / `mx7b` with matching K, a `drain` of an accumulator not its own;
- a result whose shape is not its annotation, or a stored value whose shape is
  not the view's;
- a value stored into memory of the other kind (MXFP7 or not).

A name a `for` body rebinds from outside the loop is **loop-carried**: it keeps
its type, and the next iteration and the code after the loop see the new value
(`m = copy mn` carries flash attention's running max).

## 5. Buffer types (`buffers.py`)

| type | layout |
|---|---|
| `f16[M, N] @dram` | row-major fp16 |
| `mx7a[M, K] @dram(gm=G, nk=K2)` | MXFP7 entries in the clusters' A packing, tile-major (`matmul.pack_a`) |
| `mx7b[N, K] @dram(gn=G, nk=K2)` | B packing (`matmul.pack_b`) |
| `f16[M, N] @dram(tile=GMxGN)` | drained output: tiles of GM x GN sub-tiles (4 x 4 each), tiles row-major, sub-tiles row-major in a tile |

An MXFP7 buffer is one byte a value. The host writes an input in its type's
layout and reads an output back through it.

## 6. Runners

`scripts/py/ktpu/run.py --build MODEL KERNEL [--init NAME=normal:S] [--out DIR]`
runs a kernel's L1 body on a card model. The L1 parameters are the L3 inputs,
then the result, then scratch (zeroed). Inputs are seeded random, packed by
type. The program runs twice; the second run, image and descriptors resident,
is the one reported. The error is the largest absolute difference from a
reference over the reference's largest magnitude, against two references: the
L3 body's interpreter as written (fp16 and MXFP7 rounding where its types say,
`err`) and the same math with no rounding at all (`err_exact`).

| figure | definition |
|---|---|
| lane use | lane beats / (16 x cores used x cycles) |
| work use | the L3 body's lane operations / (16 x cores used x cycles) |
| MFU | the L3 body's multiply-adds / (1024 x clusters x cycles) |

`scripts/py/ktpu/core.py KERNEL IMAGE NAME=INT|in:N|out:N` runs one image on
the V2 core alone (`vec_replay_tb`, 128 KB memory with no round-trip latency):
busy cycles, lane use and every engine's busy and stall counters, in seconds.

## 7. Compilers (`compile.py`)

Every stage's output is `.ktpu` text, read back and verified by the next
stage's reader:

    L3 --plan--> L2 --L2 passes--> L2 --lower--> L1 --L1 passes--> L1

`compile_l1(module, name, level, l1=("schedule",), l2=("retile=auto",),
options)` compiles from `l2` or `l3`; `compile_l3` gives the planned L2 text.
`run.py --level l2|l3 [--l2-passes ...] [--l1-passes ...] [--lower KEY=VALUE]`
runs the result and writes the texts next to the report.

**L3 -> L2** (`l3/plan/`). The problem size is the hand L2 signature's; a
parameter read through `transpose` is laid out transposed.

| body | form |
|---|---|
| no gemm, all `[N]` | elementwise: `N / 2` a core, tiles of the largest of 2048 / 1024 / 512 values whose double buffers fit L1 |
| no gemm, `[R, C]` and `[C]` | rows: `R / 2` a core, 16-row tiles, `[C]` loaded once |
| gemms with elementwise epilogues (`quantise` -> `mmt` chains) | 256 x 256 tiles, column tile u on mg[u % 4], gemms reading one A into one epilogue share a phase, the epilogue on vc[u % 2] into an `mx7a` scratch, the result gemm drained to the output |
| the attention dataflow | `exact`: scores tiles drained, P and l on a core, P V, the division (`--lower softmax=exact`) |
| a `map` over heads of the attention dataflow | the online form of `flash.l2` for R / 1024 heads |

**L2 -> L2** (`l2/passes.py`). `retile=N` splits each vector loop's S-row
tiles into N-row tiles (contiguous rows, the loop S / N times as long);
`retile=auto` takes 8 where a reduction follows the first op reading a row
statistic (a second reduction chain only overlaps another tile's work), else
leaves the loop.

**L2 -> L1** (`l2/lower.py`):

- `stream.py`: 1-D elementwise. Groups of four registers, a pass of two tiles,
  group i + 1 unpacking while group i computes; results reuse dying operands'
  registers.
- `rows.py`: R x 128 tiles. R = 16: the two 8-row halves side by side, two
  input and two output buffers. R = 8: stage A (to the first row op reading a
  statistic) of tile parity j in one math run with stage B of parity 1 - j.
  `sub` then `exp2` of its result is one `vexp2d`; a squared row summed is a
  `vmul` + `vfma` per half; reductions alternate two partial registers.
- `fused/`: cluster tiles and vector tasks unrolled with constant addresses
  and the regions they read and write (`tasks.py`, layouts in `layout.py`).
  A tile over several K chunks emits from its last sweep and is drained,
  fused, by the cluster's next command; a one-chunk tile drains plainly. Fill
  banks alternate across a cluster's chunks. Vector tasks: generated
  elementwise epilogues on drained 256 x 256 tiles into MXFP7 entries
  (`epilogue.py`), or library forms matched to hand images (`library.py`:
  `attn_p`, `attn_out`). Node order: `rounds` (each cluster's next tile in
  turn, then the vector tasks drained `lag` rounds back) when the clusters
  bound the kernel, `consumer` (by dependence depth, each core's next task's
  tiles first) when the cores do; waits are on the latest mark a unit needs.
  The flash form (`fused/flash.py`) is matched whole and lowered to its node
  program over flash.ktpu's images and node macros.

**L1 -> L1** (`l1/schedule.py`): each run of consecutive math statements in an
expanded image is list-scheduled over a model of the in-order math queue
(`ceil(vl / 16)` beats, results `LATENCY` = 20 cycles after the last beat, a
cycle for a config word on a VL / select / crossbar change); dependences per
register chunk and predicate. Images holding `expand` / `for` / `when` are left
as written. `schedule=N` sets the latency.

## 8. Measured

`card_v9_1n` with the V2 vector cores (`build/v9v2c/vlt_card_v9_1n_v2`), warm
runs, reports in `build/ktpu/v9v2c/levels/NAME.LEVEL.json` (the compiled
texts beside them). Inputs: MLP / SwiGLU weights normal(0, 0.03); flash q
normal(0, 0.5), k normal(0, 0.6), v normal(0, 1); the rest normal(0, 1).
Default passes (`retile=auto`, `schedule`). Error is against the L3 body run
with fp16 / MXFP7 rounding.

| kernel | hand L1 | from L2 | from L3 | cold (hand) | lane use (hand) | MFU (hand / compiled) | error |
|---|---|---|---|---|---|---|---|
| silu, 65,536 | 13,111 | 13,111 | 13,111 | 14,227 | 78.1% | | 5.0e-4 |
| add, 65,536 | 12,933 | 12,933 | 12,933 | 18,966 | 15.8% | | 3.3e-4 |
| softmax, 1024 x 128 | 33,003 | 32,832 | 32,832 | 34,762 | 65.4% | | 3.1e-4 |
| layernorm, 1024 x 128 | 38,538 | 37,337 | 37,337 | 41,204 | 61.6% | | 8.0e-4 |
| MLP, 1024^3 x 2 | 734,691 | 726,910 | 726,910 | 788,968 | 22.3% | 71.4% / 72.1% | 5.3e-3 |
| SwiGLU, 1024^3 x 3 | 1,154,878 | 1,138,901 | 1,138,901 | 1,177,399 | 17.0% | 68.1% / 69.1% | 5.1e-3 |
| attention (exact), 1024 x 64 x 1024 | 351,871 | 356,780 | 356,780 | 357,015 | 30.2% | 9.3% / 9.2% | 7.4e-3 |
| flash, 10 x 1024 x 64 x 1024 | 1,524,489 | 1,524,489 | 1,524,489 | 1,553,971 | 33.6% | 21.5% | 1.3e-2 |

Flash compiles to the hand program word for word. Its lazy offset is
data-driven: with q and k normal(0, 1) the same program takes 4,302,003
cycles (7.6% MFU, `levels/flash_n1/`): the median P RUN is 12.9k cycles
against ~4.5k, the sticky flag firing on almost every half. The L1 scheduler's
latency moves softmax: 33,915 cycles at `schedule=16`, 32,832 at 20.

add moves 393,216 bytes in 12,933 cycles: 30.4 B a cycle, a memory figure, not
a lane one.

The fused kernels read more than the die's one 256-bit read return bus (32 B a
cycle, shared by every cluster and core) carries in their compute time. A
256 x 256 output tile at K = 1024 fills 512 KB of MXFP7 operands for 65,536
cycles of one cluster's multiply-adds; four clusters at full rate need the
whole bus before the vector cores read anything:

| kernel | bytes read | read floor (cycles) | compute floor | MFU ceiling | bus use measured |
|---|---|---|---|---|---|
| MLP | 16 MB operands + 2 MB `h` | 589,824 | 524,288 | 88.9% | 80.3% |
| SwiGLU | 24 MB operands + 4 MB `g`, `up` | 917,504 | 786,432 | 85.7% | 79.4% |
