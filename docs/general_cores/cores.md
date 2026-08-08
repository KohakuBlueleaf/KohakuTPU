# The general cores

Four small scalar cores: **two attached to MAG, two as nodes on the NoC.** The
split is not for capacity. They do different jobs, and the job decides where the
core has to sit.

---

## 1. What a scalar core is for here

`limits.md` §1 again: a scalar core buys the **scalar-dynamic** class and none of
the element-dynamic one. That sounds like a small prize until you count what is
in it.

- **KV-cache append.** Writing at step `t` goes to a base the host knows before
  the kick -- a descriptor field, not a gather (`limits.md` §6.5). It works
  today. It costs **one host round trip per decode step**, which is the single
  strongest latency argument in that document.
- **Sequence length, layer index, which expert bank this call uses.** Same
  shape: a scalar the host computes between kicks.
- **Loop trip counts** that depend on a value rather than on the shape.
- **Index arithmetic**, once `floor` exists (`limits.md` §4.2) -- `pos % period`,
  a quantisation bucket, an `arange` generated rather than uploaded.

None of that is throughput. All of it is latency and round trips, and an
autoregressive decode step is where round trips are the whole cost.

## 2. Why two of them are on MAG and not on the mesh

Because a descriptor computed next to the memory it addresses never has to enter
the NoC.

The MAG-side pair exists to feed [`memory-mover.md`](memory-mover.md): compute a
base, a stride, an index vector, hand it to the mover, and the whole operation
happens inside the memory subsystem. A KV-cache append becomes a local
transaction rather than a host round trip plus a dispatch round.

Putting these on the NoC instead would mean every descriptor update crosses the
mesh twice -- out to compute it, back to use it -- on the exact links the
clusters and vector cores are contending for.

## 3. Why the other two are on the NoC

Because some control has to observe compute finishing.

The mesh-side pair is for decisions interleaved with the kernel: an epilogue
that depends on a reduction, a loop bound that depends on a partial result, a
dispatch that should not be issued until a band retires. Those need to see NoC
traffic, so they have to be on it.

This is also the pair that makes scheduling more flexible in the sense that
motivated four rather than two: a control decision can be taken next to the
engine it concerns instead of being hoisted to the host and pushed back down.

## 4. They compute. Nearly a full CPU

Not address generators with delusions -- **real cores, with integer arithmetic
and some floating point**, for work that is important and small.

The distinction is volume, not kind. `isa/vector.md` §3.1 gives the budget in one
line: this class of work is "thousands of operations per token, not billions".
That is far too little to justify a lane on the vector core, and far too much to
keep paying a host round trip for. It is precisely the gap a small CPU fills.

What lands there:

- **Routing.** Top-k over an expert gate, `argmax`, `argsort` -- see §5, this is
  the big one.
- **Sampling.** Temperature, top-p, beam bookkeeping. Data dependent, branchy,
  tiny.
- **Index and address arithmetic.** `pos % period`, quantisation buckets, an
  `arange` computed rather than uploaded -- all of which need `floor` and
  integer divide, which `limits.md` §4.2 calls the cheapest gap on the page.
- **Control flow.** Loop bounds from a value, early exit, convergence tests.
  `VLOOP`'s counter is the only branch a vector core has.
- **Random numbers.** A counter-based PRNG (Philox/Threefry) is a few hundred
  LUTs once integer ops exist, and `limits.md` §4.3 makes it the prerequisite for
  training: dropout currently needs a host upload the size of the activations,
  every step.

What does not: anything with real arithmetic volume. A GEMM, an elementwise
sweep, a reduction over a full tensor. If a general core is doing that, the work
was scheduled wrong -- not because the core cannot, but because two engines
built for it are sitting idle.

**They are not a gather engine.** They compute indices; the mover uses them. A
wide memory port here would duplicate [`memory-mover.md`](memory-mover.md)
badly, and worse, would put a slow engine on the critical memory path.

## 5. What core plus mover buys that neither buys alone

`limits.md` §1 says a scalar core buys the scalar-dynamic class and "**nothing**"
of the element-dynamic one, which needs a vector of addresses. That is true of a
scalar core *alone*. Put the two together and the sentence stops holding:

```
   general core            memory mover            clusters
   decides WHICH      ->   moves the bytes    ->   compute on the result
   (top-k, argsort)        (gather, permute)       (grouped GEMMs)
```

The worked example is MoE. `moe_dense` deliberately evaluates **every** expert,
and its docstring says why: "choosing experts per token is a gather with
computed indices, which this machine does not do -- route and permute on the
host, then call this on the grouped tokens." Both halves of that are exactly
what this pair does. A general core runs the top-k; the mover performs the
gather and permutation; the clusters run `moe_grouped`, which already exists and
already expects host-grouped tokens.

The same shape covers embedding lookup (§3.2, currently a `one_hot @ table`
matmul costing `T x V x C` MACs to move `T x C` values) and the KV-cache scatter.

`limits.md` §3.4 should be revisited when this lands: it concludes sort and
top-k "stay on the host" as "the right permanent answer". That conclusion was
reached against a machine with no general core in it.

## 6. Open

- **The ISA has its own document** -- [`isa.md`](isa.md) argues the existing
  soft core against a bespoke one and recommends RV32IM plus a custom extension,
  with the reasoning and the conditions under which it should be revisited.
- Whether two on MAG is right, or whether it should be one per MAG port.
- Whether the NoC-side pair needs a memory port of its own, given it must cross
  the mesh to reach a tensor while the MAG-side pair does not.
