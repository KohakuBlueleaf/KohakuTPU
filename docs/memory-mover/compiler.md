# Targeting the mover from the compiler

What changes in `src/ktpu`, and the two rules that decide when a move exists at
all.

---

## 1. The rule: materialisation is a property of the EDGE, not the op

This is the part most likely to be got wrong, and it is the same rule PyTorch
lives by. **A view is not free or not-free on its own.** A reshape that costs
nothing in isolation costs a copy the moment something downstream needs a layout
it cannot produce.

So the compiler must not ask *"is this reshape free?"*. It must ask
*"can the consumer read what the producer wrote, through the views between
them?"*

### 1.1 Views compose into a descriptor

`RESHAPE`, `PERMUTE`, `EXPAND`, `SLICE`, `PAD` and `CONCAT` are all
`OpKind` members of `VIEW`, and none of them computes. A chain of them over one
value composes into exactly one thing:

```
   base  +  a stride list  +  bounds
```

which is a mover descriptor ([`isa.md`](isa.md) §1) and, not coincidentally,
what `mx_tdesc` walks. **Do not lower views one at a time.** Fold the chain,
then decide once.

### 1.2 An edge is free iff the consumer's fetch can express the descriptor

| consumer | what its fetch can express today |
|---|---|
| cluster `FILL` | a base and a contiguous entry count |
| vector `VLD` | its AGU: base + 4 x (stride, bound) |
| general core | linear only |

So the free set is small and asymmetric, and that asymmetry is the whole
cost model:

| view | free for a `FILL`? | why |
|---|---|---|
| `SLICE` on **outer** axes | yes | a base offset |
| `SLICE` on **inner** axes | **no** | strided; this is `limits.md` §6.3's `(L, H*D)` |
| `RESHAPE` merging **contiguous** axes | yes | the address function is unchanged |
| `RESHAPE` merging non-contiguous axes | **no** | needs the elements gathered |
| `PERMUTE` of the two innermost | **no** | a transpose |
| `PERMUTE` of outer axes | yes | a reordering of offsets |
| `EXPAND` (stride 0) | for `VLD`, yes | for `FILL`, no |
| `PAD` | **no** today | free the day a fetch carries `mx_tdesc`'s bound axes |
| `CONCAT` | see §1.4 | |

**Where the answer is no, that is where a move goes.** `lower()` already does
exactly this for one case -- a matmul reading another matmul's output -- and
inserts a relayout band. Generalising that check to every edge, and emitting a
`MOVE` instead of a vector pass, is the change.

### 1.3 The layout each engine produces and requires

Materialisation is decided against these, not against shapes.

| | produces | requires |
|---|---|---|
| cluster | sub-tile order (a `DRAIN`) | entry order (a `FILL`) |
| vector core | linear | linear, strided by its AGU |
| general core | linear | linear |
| mover | whatever the destination descriptor says | anything |

The cluster row is why a matmul feeding a matmul needs a move at all: it
produces one order and requires another.

### 1.4 `CONCAT` is an allocation question first

A concat of two producers is **free** if both are told to write into the two
halves of one region -- no move at all, just an offset in each producer's
destination. It needs a move only when a producer's layout is already fixed by
something else.

Try allocation first. This matters for SDXL, where every up block concatenates a
skip connection, and paying a copy for each would be absurd.

### 1.5 What this means for `conv2d`

`ktpu.dsl.nn.conv2d` is built from `pad`, inner-axis `slice`, `reshape` and
`matmul`, and is numerically correct at level 1 today
(`tests/ktpu/unit/test_conv.py`). By §1.2 its per-tap operand is an **inner**
slice plus a merging reshape, so each tap materialises with today's `FILL`.

Two ways that improves, and they are worth keeping distinct:

- **with the mover**, materialisation costs a word-granular copy instead of a
  vector band
- **if a `FILL` ever carries an `mx_tdesc`**, the taps stop materialising at all
  and conv becomes what `tensor-isa.md` §3.2 says it is -- a matmul with a more
  interesting descriptor

The second is the real prize and it is a change to the fetch path, not to the
mover.

## 2. Region lifetime: what can be freed

Raised separately and it belongs here, because it is the same analysis.

`codegen/cu.py` allocates a region per intermediate value and never reuses one.
For a deep graph that is most of the image: a decoder layer's intermediates are
dead the moment the next layer's input is written, and today they occupy memory
until the program ends.

The fix is standard and the information is already present:

1. compute **liveness** over the level-2 band order -- a value is live from the
   band that writes it to the last band that reads it
2. colour the interference graph, or, since bands are a linear order, run a
   simple **linear-scan** allocator over region sizes
3. reuse a freed region only when the round barrier that separates writer from
   reader guarantees the old reader has retired

Step 3 is the one with teeth, and it is the same ordering rule as
[`arch.md`](arch.md) §8: a region may be reused in round *r+1* if every reader
of its old contents was dispatched in round *r* or earlier. Get that wrong and
the failure is a plausible wrong answer, not a crash.

**This is independent of the mover and can land before it.** It is listed here
because the liveness it needs is the same walk §1 needs, and doing them together
is cheaper than doing them twice.

## 3. What the compiler emits

| | |
|---|---|
| `ir/sched.py` | an `Engine.MOVE` |
| level 2 | a `MOVE` band, with a folded descriptor pair per §1.1 |
| level 3 | a `MOVE` instruction naming a **command block** ([`isa.md`](isa.md) §3) |
| `codegen/cu.py` | emit command blocks into a region like any other constant |
| `passes/lower.py` | replace the relayout band; run the §1.2 check on every edge |
| `interp/mesh.py` | execute a `MOVE` as a numpy strided copy |
| `codegen/cost.py` | price it by regime, §5 |

Command blocks are constants. They are built at compile time, live in a region,
and the mover fetches them -- so the agent issues one pointer, not twenty
register writes.

## 4. The simulator is the golden model

`interp/mesh.py` executes a `MOVE` by building the same descriptor as numpy
strides and copying. That makes the Python side the reference the RTL is checked
against, which is the practice this repo already uses for `vec_tables.py`
(generator and bit-exact model) and for the vector core (`interp/mesh.py`
executes the vector ISA, and `vec_cu_tb.v` was checked against it).

The property to assert is stronger than "the bytes match": **a move followed by
its consumer must produce the same tensor as the unmoved graph.** A test that
only compares the moved region cannot tell a correct move from a
correctly-wrong descriptor pair, which is precisely how the 1.26e+00 relayout
bug survived.

## 5. Cost model

Price by regime, not by byte count ([`arch.md`](arch.md) §5):

| regime | cost |
|---|---|
| word granular | `bytes / port bandwidth` |
| granule buffered | the same, plus a fixed per-granule overhead |
| element granular | `elements * word_bytes / bandwidth` -- the honest 16x |

**The third row must be visible in the model.** A kernel that falls into
element granularity should look expensive in the timing report, because it is.
This project has been burned by plausible wrong answers; a plausible wrong cost
is the same failure moved into the scheduler, where it is harder to see.

Report it as a bracket, the way the timing model already reports throughput and
cycles.

## 6. Verification ladder

In order. Each step is worth landing on its own.

1. **Descriptor folding**, in Python: a chain of views against numpy's own
   strides for the same chain. Pure unit test, no hardware.
2. **`MOVE` in the simulator**: the §4 property, on relayout, permute, pad,
   concat and gather.
3. **`lower()` emits a move exactly where §1.2 says it must.** Assert on the
   band list, not on numbers -- a test that only checks values passes when a
   redundant move is inserted.
4. **RTL against the simulator**, one bench per mode, driven the way
   `vec_cu_tb.v` drives the vector core: a local bench that plays memory and
   issues command blocks, with no mesh.
5. **The back-to-back GEMM tests**, unchanged, with the relayout band replaced
   by a move. They already fail loudly on wrong layout, which makes them the
   best regression this change has.

Step 5 is the acceptance test for the first milestone. If those pass with the
vector band gone, the mover works.
