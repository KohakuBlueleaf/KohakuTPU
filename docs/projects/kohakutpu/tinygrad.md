---
title: tinygrad on KohakuTPU (ktpugrad)
summary: The KTPU tinygrad device -- where it hooks in, which tinygrad optimisations it switches off and why, how each scheduled kernel becomes a generated DSL kernel on the card (moves, transposes, padded boxes, narrow rows, statistics), what it covers, what it refuses by name, and the examples that run models on the simulator.
tags:
  - kohakutpu
  - tinygrad
  - software
---

# tinygrad on KohakuTPU (`ktpugrad`)

`kohakutpu.application.ktpugrad` makes `Tensor(x, device="KTPU")` a tinygrad
device whose kernels run on the KohakuTPU card -- the Verilated card model
by default (`build/v9g/vlt_card_v9_<n>n_v2`: the fast models with V2 vector
cores and 256 MB of DRAM, `scripts/py/card_model.py card_v9_<n>n_v2
--build-root build/v9g -d VEC_CORE_V2 -G DRAM_WORDS=1048576`;
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
| buffer lifetime | last read | a schedule holds every buffer it names until it ends; the seam's `run_linear` frees each KTPU intermediate (no live Tensor names it) after the last kernel that reads it -- the card's memory is its 16 MB of DRAM |

tinygrad still writes float32 for some intermediates (sum accumulators,
`exp`); on the card every float buffer is held as f16 (its f16 form), so a
hidden state is f16 or, as a product's operand, MXFP7.

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
| window | one sum of the product of two reads (under constants, zero-else gates and any epilogue) where a read is a window (two ranges at one stride, signed: `x[oy + ky]`, `g[y - ky]`) or a gate bounds it: a convolution, its two gradients (`window.py`) | each side laid out dense `[batch.., outer.., K.., lane]`: its source gathered once (statistic form) into its coordinates with a lane innermost -- a K range each side reads alone (a channel, the batch) padded to whole 16-element words, or a lane of one -- zero outside the gate's box (the padding), then the window walked by the mover in whole words (im2col); the clusters' product into a temporary; the epilogue reading it. M is the side with more outer elements; M's lane of one holds 16 copies of each value (its statistic form's first word, one mover walk: the other side's padded lanes are zero) |
| move | one read stored as it is, walked in whole 32-byte words: slices, row flips, permutes keeping the innermost axis, row broadcasts, windows | one mover copy with signed walks |
| transpose | one read whose innermost axis moved (`x.T`, NHWC <-> NCHW), the new innermost side whole 16-element words; the other side too, or read 16-padded where the extra lanes stay inside its buffer (a narrow channel count of a product held in padded rows) | mover GT4, a 4 x 4 transpose in each 16-lane chunk (`xlane` + lane masks), GT4, a mover copy (the padded rows dropped) -- exact |
| flip | one read walking its innermost axis backwards, rows of whole words | mover walks words last to first, `xlane(v, 15)` reverses lanes |
| gather | any other single read into an f16 output (a transpose or flip of rows not of whole words, odd permutes, strided slices, any window) | every source value expanded to a 64-byte row of its own (the `[n, 32]` statistic form), one mover walk into the output's elements (any element stride is a word stride there), the on-card transpose back to row-major -- exact; 128 bytes of scratch a value. Into a window or at an offset, the output's words are expanded first, so what they hold between its elements is kept |
| boxes | a `where` on index tables (pad, cat, stack): the kept ranges cut where any table changes | each box with the tables constant: a fill, a move, or a rows kernel into a window -- no read leaves its buffer |
| materialise / narrow | several reduction range sets; more than 7 reads | an inner part first into a temporary; a sub-expression first |
| stat space / flat | elementwise math over statistics at the output's index; every read at the output's index | over the `[n, 32]` forms; one pass of 128-value rows |
| rows | everything else: elementwise math, reductions along the rows or down them (`sum(0)`, batch statistics) | one view (`run._view`): along (rows the kept ranges, columns the reduced), down (rows the reduced: a halving tree of rows kernels, `runtime/tree.py`), or elementwise at a split of the output's ranges -- the fewest reads laid out, then whole 32-column rows. A single long range (a flat update beside scalars) is folded into rows first. W runs as W' = ceil32(W), a reduction's padding lanes its identity (a column mask). A read the kernel takes as it is binds as it is (rows of whole words at least W' apart, a row from a word, a value a row or one value from the statistic form, a statistic read as rows flattened first); any other is laid out once over every row and every column range it reads, rows W' apart (a table on the host; a buffer by move / transpose / flip / gather, spread to W' by the mover or the statistic form). A reduction needing a layout whose ranges interleave with the kept ones runs as two, the inner first (a channel's statistic over batch and image); reductions down the rows run one after another. The result goes into the output where its rows are whole words and W' = W, else its padded-rows form (a whole float buffer), else a temporary moved in (any window, any offset; a non-f16 output through its f16 form). A reduction across more than one column block: per-block partials, folded by halving the blocks |
| matmul | a product in any layout; a product under an epilogue the planner did not take as one (its output permuted: the projection a head split reads) | the product alone into a `[batch, M, N]` temporary first (its range order searched, adjacent runs coalesced), the epilogue after; an output not dense as `[batch.., M, N]` (a batch range between M and N, a window) computed into such a temporary and moved in; operands not `[.., rows, K]` laid out first as `[batch.., rows, K]` over every batch range (a B no batch reads shared) (move / transpose / gather); `N` padded to whole 32-column blocks; an operand whose rows are not whole words gathered to a 16-element pitch with zero columns; an epilogue operand of rows narrower than the padded `N` laid out to it (the mover, else the statistic form, a table on the host); MXFP7 packings made once; the clusters' product with the epilogue on the cores |

**Forms.** A buffer is its row-major bytes plus forms made on the card
(`runtime/forms.py`): the f16 form of a float32 buffer, the `[n, 32]`
row-statistic form a reduction leaves (made row-major by an on-card
transpose when a kernel reads it as rows), MXFP7 packings, and **padded
rows** (`n` values `ceil32(n)` apart) for rows that are not whole words --
read back compacted on the way out, read on the card through remapped
strides. A product's output of `N` not whole words stays in that form; read
row-major, it is compacted on the card (the mover for whole words, else
through the statistic form, exact).

## 4. Coverage

`software/application/tests/ktpugrad/test_tinygrad.py` (program cases,
an assign read back in the same realize, a bridged kernel, a kernel of 12
reads, convolution and pool gradients, refusals; `test_train.py` the
optimizer and a module step; all on
`build/v9f/vlt_card_v9_1n_v2`; data movement bit-exact, the rest rel err
< 2e-2 against float64 numpy). Every tensor is float: a loss reads its
labels as one-hot float values.

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
`software/application/tests/behave/test_values.py` runs one case of each
lowering path through the compiled packages themselves on the behaviour
model (`kohakutpu.simulation.behave`: the node's steps, the mover, vector
cores and clusters for values, no cycles) against the CPU.

**Refused by name:** `rand` (THREEFRY / BITCAST: lanes hold E8M15 values, not
bits). A module is therefore initialised on the host (`with Context(DEV="CPU")`)
and placed on the card.

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
