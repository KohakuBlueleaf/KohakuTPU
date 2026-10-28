---
title: tinygrad on KohakuTPU (ktpugrad)
summary: The KTPU tinygrad device -- where it hooks in, which tinygrad optimisations it switches off and why, how each scheduled kernel becomes a generated DSL kernel on the card (moves, transposes, padded boxes, narrow rows, statistics), what it covers, what it refuses by clause, and the examples that run models on the simulator.
tags:
  - kohakutpu
  - tinygrad
  - software
---

# tinygrad on KohakuTPU (`ktpugrad`)

`kohakutpu.application.ktpugrad` makes `Tensor(x, device="KTPU")` a tinygrad
device whose kernels run on the KohakuTPU card -- the Verilated card model
by default (`build/v9g/vlt_card_v9_<n>n_v2`: the fast models with V2 vector
cores and `card.dram.bytes` of DRAM from the named machine file;
`card_model.py --board` takes the model's DRAM depth from its board's
`model.dram_words`;
`KOHAKUTPU_SIM_BUILD`, `KOHAKUTPU_SIM_NODES`). Every kernel
tinygrad schedules is planned, lowered to a generated DSL kernel
([dsl.md](dsl.md)), compiled L2 -> L1 ([ir/ktpu.md](ir/ktpu.md) §9) and run;
a kernel no lowering covers raises `KTPUUnsupported` naming why. Nothing is
computed on the host.

```python
import kohakutpu.application.ktpugrad          # registers KTPU, installs the seam
from tinygrad import Tensor
(Tensor(x, device="KTPU") @ Tensor(w, device="KTPU").T).relu().numpy()
```

`python -m kohakutpu.application.ktpugrad --autoload` writes a `.pth` hook
into the environment; then `DEV=KTPU python script.py` runs an unmodified
tinygrad script on the card (`--remove` takes it out).

Code: `software/application/kohakutpu/application/ktpugrad/` (`seam.py`,
`device.py`, `lower.py` the plan, `run.py` the runner, `bridge.py`,
`fold.py` index-only constants, `uops.py`), the generated kernels in
`application/kernels/` (`vector.py` rows kernels, `matmul.py`, `convert.py`
moves and conversions, `transpose.py`), the buffer forms in
`application/runtime/forms.py`.

## 1. The seam, and what is switched off

tinygrad calls `engine.realize.to_program(ast, renderer)` for every kernel;
for KTPU the seam returns a PROGRAM whose binary is the ast's key, its plan
filed under it, and `KTPUProgram` runs the plan. tinygrad's kernel-shaping
optimisations make forms this machine does not run, so the seam turns them
off while installed and the runner shapes kernels itself:

| | | why |
|---|---|---|
| `NOOPT` | 1 | every OptOps is a loop transform of generated code; none is generated |
| `BEAM` | 0 | beam search times candidates by running them (a sim run each) |
| `PARALLEL` | 0 | the compile pool lowers in subprocesses that never see the seam |
| `NO_MEMORY_PLANNER` | 1 | the planner suballocates buffers as views of one arena |
| `DEFAULT_FLOAT` | half | the machine's memory is f16 |
| `SPLIT_REDUCEOP` | 0 | a large reduction split as reshape + permute partials; the runner reduces any size |
| `WINO` / `PCONTIG` | 0 | constant-matrix conv transforms; local staging buffers |
| buffers a kernel | 8 | a vector core walks 8 memory views (output + 7 reads) |
| producer inlining | `elementwise` | a computed producer inlined into a consumer's reduction (`relu(h)` into a product) hides the contraction from the planner, so it stays its own kernel; reloads, plain (which `assign` relies on) or padded (a convolution's input, read through its window), and elementwise consumers follow tinygrad. `KTPU_TINYGRAD_FUSE=0` keeps every computed producer, `1` is tinygrad's own rule |
| buffer lifetime | last read | a schedule holds every buffer it names until it ends; the seam's `run_linear` frees each KTPU intermediate (no live Tensor names it) after the last kernel that reads it -- the card's DRAM capacity is the named machine's `card.dram.bytes` |

tinygrad still writes float32 for some intermediates (sum accumulators,
`exp`); on the card every float buffer is held as f16 (its f16 form), so a
hidden state is f16 or, as a product's operand, MXFP7.

### Queued execution and host visibility

`KOHAKUTPU_QUEUE=1` (the default) submits generated kernels without waiting
for their completions. `KOHAKUTPU_QUEUE=0`, or `Device(queued=False)` on the
attached runtime, keeps synchronous kernel calls. This is independent of
TinyJit: eager calls and recorded replay use the same queues and limits.

The node executes its submission queue in order. A host read (`numpy()`,
`item()`, allocator `_copyout`) collects through the last pending writer of
the storage's forms. A host upload (`_copyin`) collects through the last
reader or writer before replacing bytes or invalidating forms. Transfers to
unrelated storage do not drain the device. `Device["KTPU"].synchronize()`
collects everything; an explicit program timing request (`wait=True`) waits
for its runs and returns their cycles divided by the active sysnode clock.
An ordinary queued call returns no completed timing value.

The runtime retains every bound allocation, including temporary forms, until
completion is collected. Freeing a tinygrad intermediate releases its owner,
not memory still referenced by a submitted run. Admission collects oldest
runs at the actual node queue's SQ/CQ limit or at the pinned-byte budget
(`KOHAKUTPU_QUEUE_BYTES`, bytes; default: combined arena capacity). A changed
node set, or a multi-node run, establishes a completion barrier because
independent node queues alone cannot order cross-node memory dependencies.

A completion fault raises `NodeError` naming the tinygrad plan, generated
run and original completion tag/status/step. Later results are discarded;
close the runtime before using another device. Pending pins remain protected
until shutdown. See [runtime queue ownership](runtime/queue.md) for the
submission, capture and collection interfaces.

## 2. A kernel's plan

`lower.plan(ast)` reads the loop nest (`SINK -> END -> STORE`), every read
as `{range: stride}` + offset, the value as a `kernels.expr` expression:
tinygrad's ALU ops as the vector core's (`1/sqrt` one `rsqrt`), an
index-only subexpression folded into an f16 table. A range read
as `r // d` / `r % d` (a reshape that divides) is split into its mixed-radix
digits so every index is affine; a padded read `INDEX(buf, WHERE(gate, idx,
Invalid))` keeps its gate as a table. The digit split takes nested
divisions (`(r // a) % b`, a head split of a merged batch-head range) and
folds every division over the digits itself: the terms whose stride the
divisor divides form the quotient, the rest (bounded inside the divisor)
the remainder.

A sliding window (`Tensor._pool`: a convolution, a pool) is a repeat of its
input reshaped into windows, and its backward reads
`(r * i + y) % (i + d) // s` under gates. `domain.py` makes those indices
affine where they are read, deciding everything by evaluating the
index-only expressions in numpy and checking each form point for point:
a sum whose gates let through at most one point of some reduced ranges per
point of the others loses those ranges, each now an affine function of the
others (a pool's backward: one repeat holds each input element); a read's
index becomes the affine index equal to it wherever its gate lets
it through (a convolution's input gradient: `y - ky`); a sum of a sum is
one sum over both range sets. Either may split a range into mixed-radix
digits first (`y = 2 y1 + y0`). Plans: `const`, `copy`, `matmul` (a sum
over one range of a product of two reads, any layout, any epilogue) and
`vector`.

## 3. How the runner runs a plan

`run.execute` picks the first path that takes the plan:

| path | takes | on the card |
|---|---|---|
| window | one sum of the product of two reads (under constants, zero-else gates and any epilogue) where a read is a window (two ranges at one stride, signed: `x[oy + ky]`, `g[y - ky]`) or a gate bounds it: a convolution, its two gradients (`window.py`) | each side laid out dense `[batch.., outer.., K.., lane]`: its source gathered once (`forms.relayout`) into its coordinates with a lane innermost -- a K range each side reads alone (a channel, the batch) padded to whole 16-element words, or a lane of one -- zero outside the gate's box (the padding), then the window walked by the mover in whole words (im2col); the clusters' product into a temporary; the epilogue reading it. M is the side with more outer elements; M's lane of one holds 16 copies of each value (`forms.relayout`: the other side's padded lanes are zero) |
| move | one read stored as it is, walked in whole 32-byte words: slices, row flips, permutes keeping the innermost axis, row broadcasts, windows | one mover copy with signed walks |
| transpose | one read whose innermost axis moved (`x.T`, NHWC <-> NCHW), the new innermost side whole 16-element words; the other side too, or read 16-padded where the extra lanes stay inside its buffer (a narrow channel count of a product held in padded rows) | mover GT4, a 4 x 4 transpose in each 16-lane chunk (`xlane` + lane masks), GT4, a mover copy (the padded rows dropped) -- exact |
| flip | one read walking its innermost axis backwards, rows of whole words | mover walks words last to first, `xlane(v, 15)` reverses lanes |
| gather | any other single read into an f16 output (a transpose or flip of rows not of whole words, odd permutes, strided slices, any window) | one kernel of `kernels/relayout.py`'s forms (exact; `forms.relayout`): both walks whole words, one mover walk; the output's innermost axis contiguous in the source too, runs -- each cut into segments of at most one vector-core row of words, a segment's source words one gathered row (bursts), rotated by its lane rotation and masked in a core, its whole lines scattered back (segments sharing a line in turn); a broadcast, each value a row of copies first (a statistic broadcast), then runs; strided with another axis contiguous, a transpose (runs into whole-word sides, `kernels/transpose.py`'s 16 x 16 exchange, runs out). Any other pair, the lane relayout (`kernels/lanes.py`): taps of one source word at one lane rotation, a class's words gathered and scattered per tap. Into a window or at an offset, what the output's words hold between its elements is kept |
| boxes | a `where` on index tables (pad, cat, stack): the kept ranges cut where any table changes | each box with the tables constant: a fill, a move, or a rows kernel into a window -- no read leaves its buffer |
| materialise / narrow | several reduction range sets; more than 7 reads | an inner part first into a temporary; a sub-expression first |
| flat | elementwise math with every read at the output's index or one value (a statistic is its f16 values) | one pass of 128-value rows; a one-value read is its word, the value broadcast to every lane (`unpack lane=`) |
| rows | everything else: elementwise math, reductions along the rows or down them (`sum(0)`, batch statistics) | one view (`run._view`): along (rows the kept ranges, columns the reduced), down (rows the reduced: a halving tree of rows kernels, `runtime/tree.py`), or elementwise at a split of the output's ranges -- the fewest reads laid out, then whole 32-column rows. A single long range (a flat update beside scalars) is folded into rows first. W runs as W' = ceil32(W), a reduction's padding lanes its identity (a column mask). A read the kernel takes as it is binds as it is (rows of whole words at least W' apart, a row from a word, a value a row from a word (16 a word), one value); any other is laid out once over every row and every column range it reads, rows W' apart (a table on the host; a buffer by move / transpose / flip / gather, spread to W' by the mover or `forms.relayout`). A statistic (a reduction along the rows) is R f16 values, 16 a word: a kernel reading or writing one moves 16 rows a trip, its band registers merged lane by lane into a word (the crossbar's merge) and broadcast back (`bcast`). A reduction needing a layout whose ranges interleave with the kept ones runs as two, the inner first (a channel's statistic over batch and image); reductions down the rows run one after another. The result goes into the output where its rows are whole words and W' = W, else its padded-rows form (a whole float buffer), else a temporary moved in (any window, any offset; a non-f16 output through its f16 form). A reduction across more than one column block: per-block partials (`[blocks, R]`), folded by halving the blocks |
| matmul | a product in any layout; a product under an epilogue the planner did not take as one (its output permuted: the projection a head split reads) | the product alone into a `[batch, M, N]` temporary first (its range order searched, adjacent runs coalesced), the epilogue after; an output not dense as `[batch.., M, N]` (a batch range between M and N, a window) computed into such a temporary and moved in; operands not `[.., rows, K]` laid out first as `[batch.., rows, K]` over every batch range (a B no batch reads shared) (move / transpose / gather); `N` padded to whole 32-column blocks; an operand whose rows are not whole words gathered to a 16-element pitch with zero columns; an epilogue operand of rows narrower than the padded `N` laid out to it (the mover, else `forms.relayout`, a table on the host), one held as padded rows at least that wide read as those rows; an MXFP7 packing of padded rows made from them, never compacted first; an epilogue reading a statistic pads each batch's rows to 16; MXFP7 packings made once; the clusters' product with the epilogue on the cores |

**Forms.** A buffer is its row-major bytes plus forms made on the card
(`runtime/forms.py`): the f16 form of a float32 buffer, MXFP7 packings, and
**padded rows** (`n` values `ceil32(n)` apart) for rows that are not whole
words -- read back compacted on the way out, read on the card through
remapped strides. A product's output of `N` not whole words stays in that
form; read row-major, it is compacted on the card (the mover for whole
words, else `forms.relayout`, exact). A product's epilogue reading those rows
at its padded width, or an MXFP7 packing of them, reads them where they are.

## 4. Coverage

`software/application/tests/ktpugrad/test_tinygrad.py` (program cases,
an assign read back in the same realize, a bridged kernel, a kernel of 12
reads, convolution and pool gradients, refusals; `test_train.py` the
optimizer and a module step; all on
`build/v9f/vlt_card_v9_1n_v2`; data movement bit-exact; MATMUL products
within 1e-2 of the quantised host reference, whose quantisation loss against
float64 numpy is < `MXFP7_BOUND = 20*u + 34*u^2 = 0.18631495504963153`,
`u = (1 + 2^-3)/(2*(2^6 - 1)) + 2^-12`; other non-exact cases < 2e-2).
The metric is max absolute error / max absolute reference (floor 1e-9).
The quantised reference replaces both product operands with MXFP7 values,
32-element blocks along K, and keeps float64 products and epilogues; bias,
dense epilogue operands and relu are unchanged. The 1e-2 check includes the
card's f16 product drain and vector-core epilogue rounding. Every tensor is
float: a loss reads its labels as one-hot float values.

The quantisation bound is conservative and specific to these seeded inputs,
not an elementwise relative-error guarantee. With `P_A`, `P_B` the K-block
peaks repeated at each element, `|dA| <= u*P_A`: six magnitude bits give 63,
the E5M3 scale rounds up by at most `1 + 2^-3`, magnitude rounding is
half-up, and the 12-bit reciprocal table's relative error is at most `2^-12`
(its 13 entry is 2520, under the nearest 2521). A product's absolute error is
then at most `u*E1 + u^2*E2`, where `E1 = P_A @ abs(B) + abs(A) @ P_B` and
`E2 = P_A @ P_B` for `A @ B`. Bias adds no error, relu is 1-Lipschitz, two
products' envelopes add, and multiplication by an unchanged dense operand
scales them by its absolute value. Divided by the largest float64 output,
these inputs give `max(E1) < 20` and `max(E2) < 34`; measured quantisation
loss is 0.013-0.024 (k10 0.0239, n10_out 0.0215, n84_epilogue 0.0221: the
whole of those cases' distance from float64).

| family | covered |
|---|---|
| elementwise | every float ALU op, `where`, comparisons and bool masks, `floor` / `ceil`, `sin` / `exp` / `log` / `tanh` / `gelu` / `sigmoid`, f32 inputs, `arange`, broadcasts |
| reductions | along rows, down columns, several axes (`sum((0, 2))`, `max((1, 3))`), narrow rows (16 wide), rows of 32 x an odd number (1,184, 2,400), rows 6,144 / 20,480 / 40,960 wide (a capped trip, halves, quarters), down rows of 120, a channel's sum over batch and image (batch 8 and 64), batch-norm statistics, `softmax` / `log_softmax` / `layernorm` / `std`, cross entropy |
| movement | slices (any stride), `flip` of any axis, `.T` and any `permute` of any shape (rows not of whole words gathered), `pad` (any value), `cat` / `stack`, `expand`, reshapes that divide (nested: a head split of a merged range) |
| matmul | `x @ w.T` (native), `x @ w` with w [K, N], `x.T @ y`, batched, K and N of any size (10, 84), several column blocks, a product written permuted, fused bias / activation epilogues, an epilogue reading a product or a dense operand of 84 columns |
| windows | `conv2d` (padding 0 / 1 / 2, one or several input channels), both its gradients, `avg_pool2d` and its gradient, through a SiLU (`test_window.py` plans a conv -> GroupNorm -> SiLU -> pool -> LayerNorm step without a card) |
| training | `assign` read back in the same realize; backward of linear / relu / cross entropy; `Adam16` steps |
| wide kernels | more than 8 buffers (tinygrad cuts at 8; folded tables split by the runner) |

Off the card, `software/application/tests/ktpugrad/test_compile.py` runs
every function the models use over shapes and axes (unary and binary ops
over nine shapes, broadcasts, eight reductions over seven shapes and their
axes, norms, movement, matmuls, eight convolution configurations with
stride, dilation and groups, a transposed convolution, pools, attention,
gradients) on an offline device (`runtime.device` kind `offline`):
`test_compiles` takes each case's kernels through planning, generation,
L2 -> L1 -> L0, scheduling and the static verifier; `test_plans_compute`
computes every plan by its numpy meaning (`ktpugrad.reference`, no kernel)
against tinygrad on the CPU, within 3% of the result's largest magnitude.
Compiled-package value witnesses run on the card or Verilated card through
`software/tests/spec/runners/target/num.py` and the application card tests.
No tensor-computing behaviour model serves as an oracle.

**Refusal clause:** TG.PLAN.010 identifies an unsupported plan, including
`rand` (THREEFRY / BITCAST: lanes hold E8M15 values, not bits).
`KTPUUnsupported.clause` carries the id, also included in its diagnostic;
tests assert the id rather than message wording. A module is initialised
on the host (`with Context(DEV="CPU")`) and placed on the card.

## 5. Models on the simulator

`software/application/examples/tinygrad/`, every model a tinygrad module.
The defaults are short simulator runs; full-size runs are flags.

| script | what | measured on `build/v9f/vlt_card_v9_1n_v2` |
|---|---|---|
| `quickstart.py` (`DEV=KTPU`, no KohakuTPU import) | plain tinygrad | gelu 3.2e-4, softmax 4.2e-4, relu(x @ w.T + b) 1.0e-2, x.T @ x 5.5e-3, cat + flip exact, layernorm(x).sum(0) 7.3e-4; 26 kernel runs, 179,512 card cycles |
| `mnist_mlp.py` (784 -> 128 -> 10) | TRAINED on the card: forward, backward and `Adam16` (§6) | 4 steps of 64: loss 2.12 -> 1.95, 431 kernel runs, 5,819,153 cycles (~1.45 M a step); test accuracy 50.8% on 256 images after those 4 steps |
| `vit_mnist.py` (2 blocks, 64 wide, 2 heads, 64 patches of 4 x 4) | a pretrained f32 checkpoint (tinygrad on the host) transferred to the card; attention = the DSL `attention` kernel (§7) | checkpoint: 600 f32 Adam steps on the CPU; one encoder block on the card against f32 on the CPU rel 8.4e-3 (31 kernels, 335,086 cycles, 2 images); the attention of 16 images (32 heads) one DSL call, rel 7.1e-3, 112,831 cycles; the whole model on 16 test images: accuracy 1.0000 (CPU f32 1.0000), agreement 1.0000, max logit difference 0.259, 202 kernel runs, 2,946,627 card cycles, 276 s wall |

Products are MXFP7, so a model's logits differ from f32 by the format's
rounding.

## 6. Training on the card

`ktpugrad.nn` (`software/application/kohakutpu/application/ktpugrad/nn.py`):

- `place(module, device="KTPU", dtype=half)` moves every tensor of a
  tinygrad module (`get_state_dict`) to the card, floats cast to f16;
- `Adam16(params, lr, b1=0.9, b2=0.95, eps=1e-8, weight_decay, loss_scale,
  compensate)` is AdamW whose every state is f16 on the card, the update
  computed at register precision (E8M15 lanes) and narrowed on writeback:
  `w -= lr * (m * m_scale / (rms * r_scale + eps) + wd * w)`.

| piece | why |
|---|---|
| `rms` = sqrt(v) stored, not v | the gradient's own range: v of a 1e-4 gradient is below f16's smallest subnormal |
| `m_scale`, `r_scale` from the host each step | `1 - b2^t` is within an f16 ulp of 1 after a few thousand steps |
| `lr` the optimizer's tensor | a schedule assigns it; no kernel is rebuilt |
| `scale(loss)` before `backward` (`loss_scale` 1024) | small f16 gradients stay above the subnormals; `m / rms` does not see the scale (eps is scaled with it) |
| `compensate` (Kahan, 2 bytes a weight) | an update below half an f16 ulp of its weight is kept in an error buffer and lands later; stochastic rounding needs `rand`, which the card refuses |

```python
with Context(DEV="CPU"):
    model = MLP()                      # tinygrad modules draw weights here
place(model)
opt = Adam16(nn.state.get_parameters(model), lr=2e-3)
with Context(TRAINING=1):
    loss = -(model(x).log_softmax() * y).sum(-1).mean()   # y: one-hot f16
    opt.zero_grad()
    opt.scale(loss).backward()
    Tensor.realize(loss, *opt.schedule_step())   # the loss read before the step writes
```

`software/application/tests/ktpugrad/test_train.py` checks the compensated
weights against the same updates accumulated exactly (within 3 ulp, while
the uncompensated ones lose most of the movement) and one backward step of
an `nn.Linear` module on the card.

## 7. Your own kernel in a tinygrad program

`ktpugrad.bridge(kernel)` is any `@kt.kernel` as a function of tinygrad
Tensors: inputs are realised on KTPU and handed over in the form each port
reads, outputs are new KTPU Tensors written row-major; calling it returns
the output of a one-output kernel, `.outputs(...)` every output. The ViT
example runs its attention this way: the DSL `attention` kernel with
`heads=B*H` takes every (image, head) pair of a layer in one call
(`software/application/examples/tinygrad/vit_mnist.py`,
`software/application/examples/dsl/custom_kernels.py`).
