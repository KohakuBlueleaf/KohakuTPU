# The DSL

Design. `src/ktpu/dsl/`.

**The DSL is about how to *use* the IR, not part of it.** It emits level-1 graph
IR and knows nothing below that — no tiles, no grid, no engines. That
independence is why it can be built alongside the lower levels rather than
after them.

---

## 1. Tracing, not parsing

A kernel is an ordinary Python function. It is called with **tracer values** —
objects that look like tensors and record what happens to them — and the graph
falls out of the operations they flow through.

```python
@ktpu.kernel
def rmsnorm(x: Tensor, g: Tensor, eps: float):
    ms = (x * x).mean(axis=-1, keepdim=True)
    return x * rsqrt(ms + eps) * g
```

Nothing here is parsed. `x * x` calls `Tracer.__mul__`, which appends a `mul` op
and returns a new `Tracer`. At the end, the returned tracers name the outputs and
the recorded ops are the graph.

**Why tracing rather than an AST pass.** Source parsing means reimplementing
Python's semantics — scoping, closures, comprehensions, decorators, imports —
and getting them subtly wrong. Tracing gets all of that for free by *being*
Python: a helper function is inlined because calling it is calling it, a
comprehension unrolls because it runs, a constant folds because it was never a
tracer to begin with. The cost is that only executed paths are captured, which
§2 turns from a limitation into the rule.

---

## 2. The rule: compile-time control flow is free, runtime control flow is an error

This is the whole design, in one sentence, and it is what makes tracing sound
rather than a trap.

```python
    if n_heads > 8:            # n_heads is a plain int  -> ordinary Python, fine
        ...
    for i in range(4):         # plain int               -> unrolls, fine
        ...
    if x[0] > 0:               # x is a Tracer           -> TracerControlFlowError
        ...
```

A tracer that reaches `__bool__`, `__len__`, `__iter__`, `__index__` or
`__float__` **raises**, with a message naming the source line and saying which
value was data-dependent. It does not silently take a branch, and it does not
quietly produce a graph that is correct for one input and wrong for the next —
which is exactly the failure mode that makes naive tracers untrustworthy.

Compile-time control flow needs no support at all: it is Python, it runs, and
what it leaves behind is the graph for those parameters. A kernel specialised on
`n_heads` is a different graph per `n_heads`, which is correct and is what the
cache key records (§5).

**Data-dependent selection is still available** — as a value, not a branch:

```python
    y = where(x > 0, x, 0.0)      # select, one op, both sides evaluated
    m = maximum(x, 0.0)           # better: relu is one op
```

That is the same trade every array language makes, and on a 16-lane SIMD engine
it is the right one: a branch would have to be predicated anyway
([`../isa/vector.md`](../isa/vector.md) §2).

---

## 3. The surface

Deliberately close to the graph op set ([`ir.md`](ir.md) §2.1), because a DSL
whose operations do not correspond to IR ops has to invent lowerings, and those
lowerings are where the semantics get lost.

```
   operators      + - * / **        max min
   comparison     < <= == != > >=   (produce mask tracers)
   elementwise    exp exp2 log log2 sqrt rsqrt recip abs relu where
   reduction      sum max min mean var  (axis=, keepdim=)
   contraction    dot(a, b)         -> the matmul macro-op
   views          reshape permute expand slice pad broadcast  (and [] indexing)
   casts          .to(FP16) .to(E8M15) ...
```

`mean` and `var` are **sugar that expands at trace time** into `sum` and `mul` —
they exist because kernels read better with them, and they leave no trace of
themselves in the graph. `exp`/`log` expand to `exp2`/`log2` with the constant
folded, per the base-2 argument in
[`../compute/vector-core.md`](../compute/vector-core.md) §4.1.

### 3.1 What is not in the surface

No `if`, no `while`, no `print`, no `.item()`, no numpy interop on tracers, no
in-place mutation. Each of those either needs a runtime value or breaks value
semantics, and both are rejections rather than omissions.

---

## 4. Shapes are known during tracing — and dynamic shape lives here

The IR has no symbolic extents ([`ir.md`](ir.md) §6.1): every shape in a graph
is a concrete integer, because a pass that cannot read an extent cannot pick a
tile, compute a grid, or prove the grid covers the output once.

**Handling dynamic shape is therefore this layer's job**, and it is a natural
one — tracing already runs at call time, when the real shape is in hand. Three
strategies, none needing IR support:

| strategy | when | what it does |
|---|---|---|
| **cache by shape** | a handful of distinct shapes | trace once per shape, key as §5 |
| **bucket** | sequence lengths | round to the next of a fixed set, mask the tail |
| **pad** | always, underneath | to a tile multiple — the machine needs it anyway, since a zero contributes nothing to a dot product |

So a caller may be fully dynamic; what reaches the IR never is. The same applies
to a tinygrad bridge, which specialises for the same reason.



Every tracer carries a concrete shape and dtype, so `x.shape` is a plain tuple
of ints and `x.shape[-1] * 2` is arithmetic, not a graph op. Shape errors are
raised **at trace time, at the source line that caused them**, which is the
single largest usability difference between this and an IR you build by hand.

Broadcasting follows numpy rules. Dtype does not promote — it **converges**
([`ir.md`](ir.md) §1.1): mixing float formats is legal and ordinary, because the
format an op computes in is the engine's rather than a function of its operands.
`fp32 + fp16` is something the vector core does natively. A float mixed with an
integer is an error.

---

## 5. Entry points and caching

```python
@ktpu.kernel                      # trace on first call, cache by signature
def f(x, w, *, alpha: float): ...

g = ktpu.trace(f, x_spec, w_spec, alpha=0.5)    # explicit, no call needed
```

The cache key is `(function identity, input shapes, input dtypes, every
non-tensor argument)` — because non-tensor arguments participate in
compile-time control flow and therefore change the graph. Getting that key
wrong means silently reusing a graph specialised on a different constant, so it
is worth a test that changes only a scalar and asserts the graph differs.

---

## 6. Build order

Each step is testable without the next.

1. `Tracer` with shapes, dtypes and the arithmetic dunders; the
   `TracerControlFlowError` guards. Testable: trace a function, assert the op
   list.
2. Reductions, views and `dot`.
3. `@kernel`, the cache, and the error messages with source locations.
4. Sugar (`mean`, `var`, `exp`, `log`) — pure expansion, no new IR.
5. A small kernel library written *in* the DSL — `rmsnorm`, `softmax`, `silu`,
   `gelu`, the split-K epilogue — which doubles as the integration test and as
   the evidence that the surface is sufficient.

Step 5 is the honest test of the whole design. If a kernel from
[`../compute/vector-bringup.md`](../compute/vector-bringup.md) §3.2 cannot be
written in the DSL without an escape hatch, the surface is wrong and this
document is what changes.

---

## 7. Open

- **Autodiff: no**, for now. Training would need it; inference does not, and
  adding it later to a value-semantics graph is tractable. Recorded so the
  answer is a decision rather than an oversight.
- **A `dot` that is not the matmul macro-op** — a small contraction that should
  run on the vector core's tree mode instead of a cluster. The DSL should
  probably not distinguish them; the engine-assignment pass should
  ([`ir.md`](ir.md) §3.3).
- **Multiple outputs and in-place stores.** The surface above returns values.
  Kernels that write several tensors need a convention, and "return a tuple" is
  probably it.
