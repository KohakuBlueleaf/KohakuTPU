---
title: Writing kernels
summary: The tensor level, the kernel level, and how much of the schedule you have to say.
tags:
  - kohakutpu
  - dsl
  - guide
---

# Writing kernels

There are two levels you write at, and one rule for choosing.

**Write at the tensor level unless you need a kernel.** A tensor-level program
says what to compute; a kernel says how the work is split. If you are not
choosing a split, you are writing a tensor program with extra ceremony.

```python
from kohakutpu import api as ktpu

x, w = ktpu.tensor(X), ktpu.tensor(W)
y = (x @ w).silu()          # @ contracts the LAST axis of both
print(y.numpy())            # the only line that crosses the link
```

`@` is not `numpy.matmul`. It contracts the last axis of **both** operands,
because `w` is stored `[N][K]` -- which is how `torch.nn.Linear` already keeps a
weight, so weights upload verbatim with no transpose.

Everything at this level is a kernel from `kohakutpu.kernels`, so there is one
lowering and the fluent form cannot disagree with the DSL.

| | |
|---|---|
| `a @ b` | `a @ b.T` in numpy terms |
| `a + b` | elementwise |
| `.silu() .gelu() .relu()` | activations, any rank |
| `.softmax()` | row-wise |
| `.rmsnorm(w)` `.layernorm(w, b)` | per row |
| `.numpy()` | bring it back |
| `.pin()` `.unpin()` | keep it resident when nothing references it, and stop |

Device control is the only thing below level 5 an application touches:
`ktpu.sync()`, `ktpu.empty_cache()`, `ktpu.stats()`.

---

## Writing a kernel

A kernel is a Python function whose parameters are **defaults, not annotations**.
`M` is a value, and a value in an annotation is an error in every type checker;
a default is exactly what a declaration is, and it gives the body the handle's
own type.

```python
from kohakuaccel.lang import dims, grid, sweep
from kohakutpu import lang as L
from kohakutpu.lang import kernel

M, K, N = dims("M, K, N")

@kernel
def matmul(a=L.In(M, K), b=L.In(N, K), c=L.Out(M, N), *, gm=2, gn=1, nk=2):
    for i, j in grid(a.tiles(gm), b.tiles(gn)):
        acc = L.tile(gm, gn, nk)
        for k in sweep(a.chunks(nk)):
            acc += a[i, k] @ b[j, k]
        c[i, j] <<= acc

y = matmul(x, w)      # a call, not a launch
```

Calling it solves `M, K, N` from the arguments' shapes, compiles for the machine
they live on, packs each operand into the byte order this compilation needs,
allocates `c`, dispatches, and returns it. There is no launcher to write; if you
find yourself writing one, something below failed to decide something.

`In` and `Out` are the whole interface. An `Out` is never passed -- the compiler
knows its shape and allocates it.

### Any rank, one kernel

A leading `...` absorbs axes the kernel does not tile.

```python
L.In(...)          # one contiguous block, whatever rank arrives
L.In(..., M, N)    # the leading axes are a BATCH; the body describes ONE element
L.In(N, K)         # no `...`: SHARED by every batch element -- a weight
```

That is how `silu` serves `(N,)`, `(M, N)` and `(B, H, L, D)` from one trace, and
how `batched_matmul` applies one weight to a stack.

### What the machine makes you say

Three facts leak into every kernel, because they are the hardware:

- **A region is counted in entries of `lanes x kblock`,** a tile in `4x4`
  sub-tiles. Granularity is the format's, not yours.
- **`gm`, `gn`, `nk` are literally instruction fields.** They are the tuning
  knobs and there are no others.
- **`exp2` is the transcendental**, not `exp`. Fold `log2 e` into whatever scale
  is already there; a separate multiply is a whole pass over the operand.

---

## How much of the schedule to say

The DSL is a band between two other levels, and every point in it is legal at
once. What a line does not say, the compiler decides.

**Above the band** is the tensor level. A contraction between whole buffers
states no split, so it says exactly what `ktpu.matmul(x, w)` says -- it is
refused, with the message pointing here.

**Below the band** is the IR. A line naming an address or a value id is not a
kernel.

| rung | you state | compiler decides |
|---|---|---|
| **tiling** | the grid and the sweep | L1 windows, fills, drain placement, barriers |
| **placement** | + which window holds what, and when it fills | barriers |
| **schedule** | + where the barriers fall | nothing |

```python
# tiling -- the minimal form, and where examples live
for i, j in grid(a.tiles(gm), b.tiles(gn)):
    acc = L.tile(gm, gn, nk)
    for k in sweep(a.chunks(nk)):
        acc += a[i, k] @ b[j, k]
    c[i, j] <<= acc

# placement -- worth it when one window is refilled from several tensors
for i, j in grid(a.tiles(gm), b.tiles(gn)):
    ra, rb = L.region(gm, nk), L.region(gn, nk)
    acc = L.tile(gm, gn)
    for k in sweep(a.chunks(nk)):
        ra <<= a[i, k]
        rb <<= b[j, k]
        acc += ra @ rb
    c[i, j] <<= acc

# schedule -- `stage` is `grid` plus a barrier
with L.stage(a.tiles(gm), b.tiles(gn)) as (i, j):
    ...
```

All three emit the **same instructions**. `compiler/tests/test_spectrum.py`
asserts that, so saying less can never cost anything.

A rung is not a mode. Two lines of one kernel may sit on different rungs.

---

## Stages, and the one rule about them

A stage is a set of independent instances on one kind of unit. Stages run in
order with a barrier between, and a barrier is the only synchronisation this
machine has.

You rarely write one. `grid` does not demand a barrier; neighbouring grids that
want the same split are banded onto one stage, and a boundary appears where the
work changes shape -- which for a matmul-then-activation kernel is exactly once.

**A unit does not wait between the statements of one stage.** A statement that
reads what an earlier statement in the same stage wrote reads stale bytes -- 79%
error, measured, not a hang. The compiler refuses it and names both statements.
Put them in separate stages:

```python
with L.stage(x.parts(part)) as e:
    corr[e] <<= top[e]                       # keep the old value
with L.stage(x.parts(part)) as e:
    top[e] <<= L.maximum(top[e], block[e])   # before overwriting it
```

Work that crosses unit types does **not** have to cross memory. A cluster's
`DRAIN` addresses either the memory port or a NoC node, and a node-addressed
drain lands in the receiving vector core's L1
(the cluster ISA, §10).
Write the elementwise work on the accumulator and the tile never reaches DRAM:

```python
with L.stage(x.tiles(gm), w.tiles(gn)) as (i, j):
    acc = L.tile(gm, gn, nk)
    for k in L.sweep(x.chunks(nk)):
        acc += x[i, k] @ w[j, k]
    y[i, j] <<= acc * sigmoid(acc)     # elementwise ON the accumulator
```

`L.temp(...)` is the other road, and still the right one when the receiving core
needs the value in row order, when the tiling does not fit an L1, or when you
simply want the two halves written separately. The compiler falls back to it on
its own where the fused form does not fit, which is what `Kernel.relax` does.

---

## A tiling is a VIEW, not a memory checkpoint

This is the rule the kernel library is written against, and it is easy to get
backwards:

> **`with units(...)` declares a TILING. It does not declare a memory boundary.**

A tiling inside a tiling is a **sub-view of the current tile**. That tile may live
in an accumulator, in a unit's L1, or in registers — so a nested tiling **must not
force another memory tile**. Where a value lives is a lowering decision the
compiler makes from residency and capacity; it is not a consequence of the author
having written a tiling. The sentence to test any change against:

> **A tile or block inside a memory tile should not be another memory tile.**

Four shapes, and **only the last one writes memory**:

1. **One top-level `with units(...)`, multi-dimensional.** Every operand tiled by
   its own property in one space. No checkpoint; the only thing that leaves is
   the result. A single tiling does not mean a single block size — this is what
   lets one tiling cover operands that tile differently, which is why the answer
   is one `with units(...)` rather than one per operand.
2. **A tiling inside a tile** — a sub-view, never another memory tile. The nested
   tiling re-describes the tile the enclosing scope already holds, so it changes
   how the data is addressed, not where it is. This is how vector work is managed
   *inside* a matmul tile.
3. **A loop inside a tile** — iteration, not tiling. Loop-carried state stays in
   whatever storage class holds it. No checkpoint per iteration.
4. **Two or more top-level `with units(...)`** — an explicit checkpoint, and it is
   legal. This is the only shape that writes memory, and it is a deliberate
   instruction from the author rather than an accident of syntax. Its cost is
   visible precisely because a second top-level tiling was written.

Two things may still force a checkpoint the author did not write, and both are
lowering decisions the compiler must **report** rather than perform silently:
**capacity** (the tile does not fit the accumulator or the unit's L1), and **a
reduction that genuinely crosses units**. The second is usually a tiling choice
rather than a law, so the compiler should say which it is.

### Which ops can be an epilogue, and why a row is not a row

> **An op that commutes with a permutation of the elements can be an epilogue.
> An op that does not needs true row order.**

A drained accumulator is 4x4 sub-tiles, one 32-byte word each, row-major *inside*
the sub-tile. So L1 word 0 holds logical elements `[0,1,2,3, 64,65,66,67,
128..131, 192..195]` — **four logical rows interleaved inside every word** — and
one logical row takes 4 elements from each of `gn` words. Every data path on this
machine moves whole 32-byte words: `VLD`'s quantum is whole L1 words, and so is
`VFILL`/`VDRAIN`, a cluster `FILL`, and `CU_DATA`'s granule.

Putting a row on one unit does not make it **addressable as a row**, and no
tiling fixes that, because the 4x4 sub-tile is fixed hardware. `exp2(s - m)`, a
mask, and a correction factor all commute and can fuse now; the row reductions
and `p @ v` do not, and they are exactly the ops that need a de-interleave.

### The accumulator chain is guarded, and the obvious guard was wrong

Writing a key-block loop *outside* the sweep silently computed the wrong answer:
the `gemm` acc flag is the innermost counter, which restarts at 0 every outer
iteration, and `acc=0` means "clear the accumulator". At `outer=3` the result was
1.001x a single pass rather than 3x, compiled clean, with no warning.

`Tile.__init__` now records its birth depth and `__iadd__` refuses when the nest
has deepened by more than one since. **Refusing on trace loop-depth alone would
have been wrong** — flash attention's `with units(...)` sits inside a loop, so its
depth at the sweep is 2, but that loop opened *before* the tile existed and each
pass opens a new stage with a fresh resident tile, where the flag restarting is
correct. Only loops opened AFTER the tile can carry its chain.

---

## Compile-time control flow

Block parameters are ordinary Python values while tracing, so ordinary Python
control flow specialises the kernel. An option that is off is not in the graph.

```python
@kernel
def attention(..., *, causal=False, block=64):
    tri = L.table(np.tril(np.ones((block, block), np.float16))) if causal else None
    ...
    if causal:
        for j in sweep(i):        # only the blocks below the diagonal
            key_block(i, j, None)
        key_block(i, i, tri)      # and the one straddling it
    else:
        for j in sweep(kblocks):
            key_block(i, j, None)
```

`L.table(array)` is a buffer whose contents are known while tracing: the kernel
builds it and the compiler ships it, so no caller passes a mask in. The same
mechanism carries any mask or bias, not just a causal one.

A loop bound may depend on the loop around it (`sweep(i)` above), because both
are unrolled when the numbers are known.

---

## Numerics that have bitten

fp16 has 11 bits of mantissa and stops at 65504. Three traps, all measured:

- **Reduce a mean, not a sum.** `sum(x*x)` over 64 lanes at RMS 30 is 57600 --
  one step from `inf`, after which `rsqrt` returns zero and the layer silently
  outputs zeros. Divide before squaring: `(x / N) * x`.
- **Never `maximum` against a sentinel.** `maximum` is `(|a-b| + a + b) / 2`; at
  `a = -60000` the ULP is 32 and it returns 26.6 for a true max of 21.2. Assign
  the first value instead of maxing against a fake one.
- **A chain reads at most three operands.** Eight descriptors, two per operand
  plus two. Fold constants together (`1.702 * log2 e` is one number) or split the
  expression across statements.

---

## Inspecting what you wrote

```python
print(matmul.trace().pretty())        # the structure, before any shape
print(matmul.plan(x, w).pretty())     # stages, units, relayouts, buffers
print(ktpu.stats())                   # dispatches, rounds, flits, bytes moved
```

`plan` compiles without running. The relayouts it lists are the honest cost: a
cluster drains 4x4 sub-tiles and a row reduction runs along a row, so a kernel
that scores then softmaxes pays for reordering in between.

## Where to look next

`examples/kohakutpu/` writes every kernel out rather than importing one; start at
`01_tensors.py` and `02_kernel.py`. `demos/kohakutpu/` shows whole networks.

[fused-epilogue.md](fused-epilogue.md) is the lowering behind the accumulator
form above, and the band where it fits. [memory.md](memory.md) is why the drained
byte order is the fast one. [hardware-wants.md](hardware-wants.md) collects what
the kernels ran into that the silicon or the compiler owes them.
