---
title: "The KohakuTPU Tensor API (kp)"
summary: numpy-like tensors on the card -- a lazy graph whose elementwise chains, row statistics and matmul epilogues fuse into one generated DSL kernel each -- the physical forms a buffer is held in, your own DSL kernel on tensors, and what it refuses.
tags:
  - kohakutpu
  - api
  - software
---

# The KohakuTPU Tensor API (`kp`)

`kohakutpu.application.api` (imported as `kp`) is the machine's own tensor
library. It sits on the same pieces as the tinygrad backend
([tinygrad.md](tinygrad.md)) -- generated DSL kernels ([dsl.md](dsl.md)),
the L2 -> L1 compiler, the runtime's buffer forms -- but plans the graph
itself, so it fuses what this machine fuses best.

```python
from kohakutpu.application import api as kp
x = kp.tensor(np.random.randn(256, 128))
w = kp.tensor(np.random.randn(512, 128) / 11)
y = kp.linear(x, w).gelu().layernorm()     # 2 kernels: mm + gelu, layernorm
y.numpy(); kp.runs()                       # the result; each run's cycles
z = kp.function(my_kernel)(y, eps=1e-6)    # any @kt.kernel on tensors
```

Code: `software/application/kohakutpu/application/api/` (`tensor.py` the
graph and its planner, `custom.py` kernels on tensors), the runtime below it
in `application/runtime/` and `application/kernels/` (the generated kernels).

## 1. The graph and what fuses

An op makes a node; nothing runs until `numpy()` / `realize()` or a consumer
that cannot fuse. A realised tensor is one generated kernel:

- **elementwise chains** of any length, broadcasts of a row (a bias) or of a
  statistic (`x - x.max(-1, keepdim=True)`) included;
- **row statistics** along the last axis (`sum max min prod mean var std`,
  `softmax`, `log_softmax`, `layernorm`, `rmsnorm`) with the chain around
  them, one kernel for rows up to 256 wide (wider rows: per-block partials,
  then a fold);
- **a product** `x @ w.T` / `kp.linear(x, w, b)` with its epilogue (bias,
  activation, residual) on the vector cores beside the clusters.

`kp.set_fusion(False)` runs one kernel an op (to compare); the API test
counts kernels (`software/application/tests/api/test_tensor.py`: a 4-op
chain is 1 kernel, 4 unfused).

## 2. Forms

A tensor's storage is its row-major bytes plus forms made on the card when a
kernel needs them, kept while the buffer is unwritten: the f16 form of a
float32 buffer, the `[n, 32]` row-statistic
form (each value in 32 lanes: how a reduction leaves a statistic and how a
row reads it), MXFP7 A / B packings of a product's operands (quantised once,
reused), and padded rows (rows of `n` values `ceil32(n)` apart: what the
tinygrad runner's kernels over rows not of whole 32-column blocks write). A
form is made on the card or refused; bytes cross the link only on upload and
read back.

## 3. Your own kernel

`kp.function(kernel)(*tensors, **consts)` runs a `@kt.kernel` with the
tensors bound to its input ports in order, each in the form the port reads
(f16, f32, or an MXFP7 packing made on the card); calling it returns the
output of a one-output kernel, `.outputs(...)` every output in port order,
new tensors that keep fusing with what follows. `kp.call(kernel, *tensors)`
is the tuple form.

## 4. Refused, by name

`Unsupported` names the construct: a product `x @ w` with `w` stored [K, N]
(keep weights [N, K] and write `x @ w.T`, or use tinygrad, which lays it out
on the card), a reduction over an axis other than the last, a row statistic
broadcast as a row of values (keep the axis: `keepdim=True`).

## 5. Measured

`software/application/tests/api/test_tensor.py` (19 cases on
`build/v9f/vlt_card_v9_1n_v2`, rel err against float64 numpy under 2e-2;
softmax 2.0e-4 in 1 kernel, layernorm 2.8e-4 in 1 kernel, linear + gelu
3.9e-4 in quant, quant, mm).
