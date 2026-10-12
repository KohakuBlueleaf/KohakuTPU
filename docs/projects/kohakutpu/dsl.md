---
title: "The KohakuTPU DSL (kt)"
summary: Kernels written in Python and traced once into L2 text -- ports, constants, units, loops, vector and cluster ops, mover ops, the two ways a kernel spans several nodes, the examples and what they measure.
tags:
  - kohakutpu
  - dsl
  - compiler
  - software
---

# The KohakuTPU DSL (`kt`)

`kohakutpu.language.dsl` (imported as `kt`) writes a kernel as a Python
function that is **traced once** per (shapes, constants, machine) into L2 text
([ir/ktpu.md](ir/ktpu.md) §9). The L2 -> L1 compiler and the card do the rest:

```python
import kohakutpu.language.dsl as kt

@kt.kernel
def silu(x=kt.In("R", 128), y=kt.Out("R", 128), *, rows=kt.auto(16, 8)):
    for c in kt.par(kt.machine().vc, on=kt.vc):          # one instance a core
        for t in kt.range(x.shape[0] // 2 // rows):      # the core's stream loop
            r = c * (x.shape[0] // 2) + t * rows
            v = kt.load(x[r : r + rows, :])
            kt.store(y[r : r + rows, :], v * kt.recip(1.0 + kt.exp2(v * -kt.LOG2E)))

print(silu.trace({"x": ((512, 128), "f16")}).text)      # the L2 module
```

Code: `software/language/kohakutpu/language/dsl/` (`kernel.py` ports and
tracing, `ops.py` every statement, `values.py` buffers and register values,
`expr.py` symbolic indices, `trace.py` the recorder). Compiling and running:
`kohakutpu.compiler.kernel.specialise(kernel, given)` (L2 -> L1, cached by L2
text) and `kohakutpu.application.runtime.call.call(dev, kernel, arrays)`.

## 1. What runs when

Python runs once, at trace time. Everything it computes is a constant of the
kernel: shapes, tile choices, `if` on a Python value, a Python `for` (it
unrolls). Device-time control flow is written with `kt` and recorded once:

| written | recorded as |
|---|---|
| `for u in kt.par(n, on=kt.mg \| kt.vc \| kt.node, at=...)` | `par u in 0..n on mg[u]` -- `n` instances, each its own program; `at=kt.ANY` lets the place pass put them |
| `for t in kt.range(n[, hi], pipe=K)` | `for t in 0..n` -- inside a core instance the stream loop (a hardware loop), at node scope a loop whose next `K - 1` iterations may run ahead |
| `with kt.when(u == 0):` | a block kept for the instances where the index condition holds |
| `with kt.sticky():` | vector math run only when an `exp2d` overflowed (`if sticky`) |
| `kt.require(cond, why)` | a trace-time refusal that names the shape it does not cover |

A comparison of loop variables is an index condition, not a bool: `if u == 0:`
raises at the source line (`TracerControlFlowError`), and pyright flags it
before that (`Sym.__bool__` returns `NoReturn`).

## 2. Ports, constants, buffers

- `kt.In(*dims, dtype="f16", layout=..., space="dram")` / `kt.Out(...)`:
  parameters with extents named (`"R"`), fixed (`128`) or open (`None`); an
  output's names are bound by an input. `dtype` `mx7a` / `mx7b` is a cluster
  operand packed MXFP7, `layout` a dict or a function of the constants
  (`a_layout()` in the examples).
- Keyword-only parameters are **constants**; `kt.auto(a, b, ...)` takes the
  first candidate whose trace and compile succeed.
- `kt.buffer(shape, dtype, space=, **layout)` declares scratch (the caller
  places it zeroed); `tile=(gm, gn)` is the clusters' drained sub-tile layout.
- `kt.machine()` gives the card's `mg`, `vc` and `nodes` counts.

## 3. Statements

**Vector core** (inside `par ... on=kt.vc`):

| op | |
|---|---|
| `kt.load(view, gt4=)`, `kt.unpack(x)`, `kt.store(view, x)`, `kt.pack(x, dtype)` | memory <-> L1 <-> registers; `gt4=True` reads 4-row bands (4 x 32 a register); storing into an `mx7a` buffer quantises |
| `+ - * /`, `kt.exp2 log2 recip rsqrt absolute maximum minimum exp2d fma fnma` | one vector op a register; a number becomes a scalar register |
| `kt.rmax(x)`, `kt.rsum(x)` | row reductions into a statistic, broadcast back with arithmetic (`x - m`) |
| `kt.where(m, a, b)`, comparisons `<`, `>`, `==` on values | `cmp*` into a predicate, `sel` |
| `kt.xlane(x, k)` | lane l of every 16-lane chunk takes lane `l xor k` (the crossbar, exact) |
| `kt.recv(kt.mg[u], shape)`, `kt.send(p, kt.mg[u])` | a cluster's drained tile into L1; packed MXFP7 rows into a cluster's bank |

**Cluster** (inside `par ... on=kt.mg`): `a = kt.fill(view, share=)`,
`acc = kt.zero(m, n)`, `acc = kt.gemm(a, b, acc)` (the name is rebound, so a
`kt.range` loop carries it), `kt.drain(acc, view | kt.vc[h] | kt.mg[j], add=)`.

**Mover** (node scope): `kt.copy(dst, src, perm=)` (word axes reordered),
`kt.copy(dst, src, walk=[(count, stride)...], at=, dwalk=, dat=)` (explicit
signed byte walks of whole buffers: flips, permutes, broadcasts, windows),
`kt.quant`, `kt.gt4(dst, src[, walk=, dwalk=])` (4 x 4 granule transpose),
`kt.fill_value(dst, v, ew=, walk=)`, `kt.gen`, `kt.gather`, `kt.scatter`.

## 4. Several nodes

A card of several sysnodes shares one memory through the Xache. A kernel uses
them in one of two ways, and both are written in `kt`:

- **Shared memory (the Xache).** A `kt.par` over more instances than one
  node holds (`kt.machine().vc` or `.mg` times `nodes`) becomes `par n on
  node[n]` around the per-node units, the variable `n * per + u`. The kernel
  has no cross-node code: every node reads and writes the shared buffers,
  and the L2 lowering orders the phases -- a node that wrote what another
  node's later phase reads rings that node (`ring mesh=...`), which waits
  (`wait slot ... consume`). `linear`, `mlp`, `swiglu`, `attention` run this
  way unchanged on 1 or 2 nodes.
- **A cross-node connection.** `kt.node[N].mg[J]` names another node's
  cluster: `kt.drain(acc, kt.node[0].mg[u], add=True)` adds an accumulator
  tile into node 0's at full precision across the interlink (L2 `drain ACC ->
  mg[J] add node=N`; the sender waits for the target's open tile, adds from
  one node go one at a time). `splitk` splits K over the nodes this way.

## 5. Examples

`software/language/kohakutpu/language/examples/dsl/` -- each with a numpy
reference (`EXAMPLES`, `REFERENCES`):

| example | what | 1 node, cycles (rel err) | 2 nodes |
|---|---|---|---|
| `vector/activation` | `act(x * s + t)`, act picked by name | 16,864 (3.0e-4) | |
| `vector/softmax` | row softmax with a temperature | 13,767 (2.3e-4) | |
| `vector/norm` | `layernorm`, `rmsnorm` | 21,132 / 9,275 | |
| `vector/glu` | `a * act(g)` | 7,128 (3.2e-4) | |
| `cluster/linear` | `act(x @ w.T + b)` | 38,157 (4.9e-4) | 32,016 |
| `cluster/mlp` | two products, the hidden layer packed MXFP7 on the cores | 67,683 (3.9e-3) | 63,562 |
| `cluster/swiglu` | `(act(x w1) * x w3) w2` | 84,711 (5.0e-3) | 76,284 |
| `cluster/attention` | dense attention, `v` given transposed | 55,504 (7.7e-3) | 58,942 |
| `cluster/attention`, `heads=2` (`heads`) | 2 heads in one call: q [2 x 128, 32], k [2 x 64, 32] | 20,684 (1.0e-2) | |
| `cluster/splitk` | K over the nodes, cross-node accumulator adds | 165,357 (3.6e-4) | 108,189 |

Measured on `build/v9f/vlt_card_v9_{1,2}n_v2` at the shapes in `EXAMPLES`
(e.g. `linear` x 128 x 256, w 512 x 256), rel err = max |got - want| / max
|want| against float64 on the MXFP7 values. At four times the rows every
example's L2 is the same length (the trace records loops, it does not
unroll them) and every one runs (`linear` 115,220 cycles, `mlp` 206,793,
`attention` 184,568).

The contraction examples share `cluster/sweep.py`: `sweep(a, b, h, tm, tn,
groups=1)` is `h = a @ b.T` over [tm, tn] tiles spread over every cluster;
with `groups` G, a and b are G stacked blocks of rows and block g of h is
`a_g @ b_g.T` -- the tile index picks its group's rows of b, so G
independent products are one loop. `attention(heads=H)` is built on it: H
heads (or every (image, head) pair of a batch) in one kernel, each head the
H-th block of rows of q, k, vt and o.

`software/application/examples/dsl/custom_kernels.py` writes a kernel of its
own (`gated_rmsnorm`), runs it inside a tinygrad program
([tinygrad.md](tinygrad.md)) and through the Tensor API ([api.md](api.md)),
and prints the two multi-node forms' L2.

## 6. Typing

Ports are typed as `Param`, `kt.auto` as its candidates' type, loop
variables as `Sym` (affine index arithmetic, comparisons give `Cond`),
values as `Value` (arithmetic gives `Value`); `software/pyproject.toml`
gives pyright / Pylance every package's import root. A kernel module checks
with zero errors.
