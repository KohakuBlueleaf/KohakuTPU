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
| producer inlining | `elementwise` | a computed producer inlined into a consumer's reduction (`relu(h)` into a product) hides the contraction from the planner, so it stays its own kernel; plain reloads (which `assign` relies on) and elementwise consumers follow tinygrad. `KTPU_TINYGRAD_FUSE=0` keeps every computed producer, `1` is tinygrad's own rule |
| buffer lifetime | last read | a schedule holds every buffer it names until it ends; the seam's `run_linear` frees each KTPU intermediate (no live Tensor names it) after the last kernel that reads it -- the card's memory is its 16 MB of DRAM |

tinygrad still writes float32 for some intermediates (sum accumulators,
`exp`); on the card every float buffer is held as f16 (its f16 form), so a
hidden state is f16 or, as a product's operand, MXFP7.

## 2. A kernel's plan

`lower.plan(ast)` reads the loop nest (`SINK -> END -> STORE`), every read
as `{range: stride}` + offset, the value as a `kernels.expr` expression:
tinygrad's ALU ops as the vector core's (`1/sqrt` one `rsqrt`, integer
division and remainder corrected to exact, shifts and power-of-two masks as
arithmetic), an index-only subexpression folded into a table. A range read
as `r // d` / `r % d` (a reshape that divides) is split into its mixed-radix
digits so every index is affine; a padded read `INDEX(buf, WHERE(gate, idx,
Invalid))` keeps its gate as a table. The digit split takes nested
divisions (`(r // a) % b`, a head split of a merged batch-head range) and
folds every division over the digits itself: the terms whose stride the
divisor divides form the quotient, the rest (bounded inside the divisor)
the remainder. Plans: `const`, `copy`, `matmul` (a
sum over one range of a product of two reads, any layout, any epilogue) and
`vector`.

## 3. How the runner runs a plan

`run.execute` picks the first path that takes the plan:

| path | takes | on the card |
|---|---|---|
| move | one read stored as it is, walked in whole 32-byte words: slices, row flips, permutes keeping the innermost axis, row broadcasts, windows | one mover copy with signed walks |
| transpose | one read whose innermost axis moved (`x.T`, NHWC <-> NCHW), both sides whole 16-element words | mover GT4, a 4 x 4 transpose in each 16-lane chunk (`xlane` + lane masks), GT4, a mover copy -- exact |
| flip | one read walking its innermost axis backwards, rows of whole words | mover walks words last to first, `xlane(v, 15)` reverses lanes |
| gather | any other single read (a transpose or flip of rows not of whole words, odd permutes, strided slices) | every source value expanded to a 64-byte row of its own (the `[n, 32]` statistic form), one mover walk into the output's order (any element stride is a word stride there), the on-card transpose back to row-major -- exact; 128 bytes of scratch a value |
| boxes | a `where` on index tables (pad, cat, stack): the kept ranges cut where any table changes | each box with the tables constant: a fill, a move, or a rows kernel into a window -- no read leaves its buffer |
| rows | elementwise math and row reductions over rows of whole 32-column blocks | the rows kernel (GT4 bands, every core); a row wider than one band's registers is one phase a column block (the band narrowed until every operand fits L1), a single long range (a flat update beside scalars) folded into rows first; a read no row view takes (a transposed operand) laid out first in the output's order over the ranges it reads, or over every output range when it is broadcast across a row range (a `[T, D]` table added across the batch) |
| narrow rows | rows of W not whole 32-column blocks | the rows kernel 32-wide over padded reads (a reduction's padding lanes masked to its identity), into the padded-rows form |
| column | reductions down the rows (`sum(0)`, batch statistics) | a halving tree of rows kernels |
| materialise / segments / narrow | several reduction range sets; a value broadcast over some kept ranges; more than 7 reads | an inner part first into a temporary; one launch an index of the outer ranges (up to 64); a sub-expression first |
| matmul | a product in any layout; a product under an epilogue the planner did not take as one (its output permuted: the projection a head split reads) | the product alone into a `[batch, M, N]` temporary first (its range order searched, adjacent runs coalesced), the epilogue after; operands not `[.., rows, K]` laid out first (move / transpose / gather); `N` padded to whole 32-column blocks; an operand whose rows are not whole words gathered to a 16-element pitch with zero columns; MXFP7 packings made once; the clusters' product with the epilogue on the cores |

**Forms.** A buffer is its row-major bytes plus forms made on the card
(`runtime/forms.py`): the f16 form of a float32 / int buffer, the `[n, 32]`
row-statistic form a reduction leaves (made row-major by an on-card
transpose when a kernel reads it as rows), MXFP7 packings, and **padded
rows** (`n` values `ceil32(n)` apart) for rows that are not whole words --
read back compacted on the way out, read on the card through remapped
strides. A product's output of `N` not whole words stays in that form.

## 4. Coverage

`software/application/tests/ktpugrad/test_tinygrad.py` (66 program cases,
an assign read back in the same realize, a bridged kernel, a kernel of 12
reads, refusals; `test_train.py` the optimizer and a module step; all on
`build/v9f/vlt_card_v9_1n_v2`; data movement and integer cases bit-exact,
the rest rel err < 2e-2 against float64 numpy):

| family | covered |
|---|---|
| elementwise | every ALU op, `where`, comparisons and bools, `floor` / `ceil`, `sin` / `exp` / `log` / `tanh` / `gelu` / `sigmoid`, f32 and int inputs, `arange`, `one_hot`, broadcasts |
| reductions | along rows, down columns, several axes (`sum((0, 2))`, `max((1, 3))`), narrow rows (16 wide), batch-norm statistics, `softmax` / `log_softmax` / `layernorm` / `std`, cross entropy, `argmax` |
| integer | `+ - *`, `//` and `%` with negatives (exact while values fit f16: |v| <= 2048) |
| movement | slices (any stride), `flip` of any axis, `.T` and any `permute` of any shape (rows not of whole words gathered), `pad` (any value), `cat` / `stack`, `expand`, reshapes that divide (nested: a head split of a merged range) |
| matmul | `x @ w.T` (native), `x @ w` with w [K, N], `x.T @ y`, batched, K and N of any size (10), several column blocks, a product written permuted, fused bias / activation epilogues |
| training | `assign` read back in the same realize; backward of linear / relu / cross entropy; `Adam16` steps |
| wide kernels | more than 8 buffers (tinygrad cuts at 8; folded tables split by the runner) |

**Refused by name:** a convolution's or a cumulative sum's window read (its
bound moves with a reduced range), `rand` (THREEFRY / BITCAST: lanes hold
E8M15 values, not bits). A module is therefore initialised on the host
(`with Context(DEV="CPU")`) and placed on the card.

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
    loss = model(x).sparse_categorical_crossentropy(y)
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
