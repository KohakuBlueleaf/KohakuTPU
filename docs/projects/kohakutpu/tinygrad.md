---
title: The tinygrad frontend
summary: tinygrad as an optional tensor frontend that dispatches into the same kernel library — where the seam is, what is switched off, and the ops where this path is worse than calling the library.
tags:
  - kohakutpu
  - compiler
  - software
---

# Tinygrad as the tensor frontend

`ktpugrad` makes `Tensor(x, device="KTPU")` work by matching tinygrad's lowered
loop nest against this project's kernel library and dispatching the library
kernel. It is **an optional frontend, not a replacement for L5** — the reason is
§3.

## 1. The seam

On tinygrad 0.13 the ast is already a lowered loop nest by the time a backend
sees it: `Ops.PARAM` / `INDEX` / `RANGE` / `REDUCE` / `RECIPROCAL`, with no
ShapeTracker and strides as affine coefficients. There is no `Tensor.schedule`
and no `realize.get_runner`; **the ast exists only where `to_program` is called**,
which is therefore the seam. `ktpugrad.capture()` records asts through the
installed seam rather than rebuilding one, and `_PATTERNS` is captured at import:
rebuilding the pattern matcher from an already-rebound matcher re-snapshots a
stale dict and the seam silently stops firing.

`ktpugrad` touches nothing outside its own directory. The device takes its
machine from `kohakutpu.api.device()`, so the whole path is gradeable without a
card.

## 2. What is switched off, and why

On the `Renderer`:

| flag | set to | why |
|---|---|---|
| `has_local` | `False` | there is no thread and no `program_id`; a unit is programmed, not commanded |
| `has_shared` | `False` | no inter-unit barrier inside a kernel, so no shared scratch to put behind one |
| `shared_max` | `0` | follows |
| `local_max` | `(1,1,1)` | one program per unit |
| `tensor_cores` | `[]` | WMMA is a warp of threads cooperating on a fragment; our GEMM is a whole-tile instruction with `gm/gn/nk` as fields, not a warp op |
| `supports_float4` | `False` | vector width is a codegen concern; our fills are entry-granular, 4 lanes x 32 K |

By environment:

- **`NOOPT=1`** — skips `apply_tensor_cores` and `hand_coded_optimizations`, i.e.
  every `OptOps`: `UPCAST`, `UNROLL`, `LOCAL`, `GROUP`, `GROUPTOP`, `NOLOCALS`,
  `PADTO`, `SWAP`. All are loop transforms over generated code. **We generate no
  loops** — the equivalent decision here is `gm`/`gn`/`nk`, which `Compiled.compile`
  already chooses per shape.
- **`BEAM=0`** — beam search times candidate kernels by RUNNING them. Over JTAG at
  100 MHz that is not a search, it is a hang.

## 3. Where the mapping fails

1. **Our multi-stage fused kernels lose.** `softmax` is FOUR tinygrad kernels and
   ONE of ours; `rmsnorm`, `layernorm`, `group_norm_silu` and `flash_attention`
   are the same shape of loss. The scheduler has already cut them apart before we
   see them, so matching them back means re-fusing across kernel boundaries —
   which is a scheduler of our own, i.e. the thing adopting tinygrad was meant to
   avoid. **Adopting it wholesale would make attention and the norms slower than
   the library we already ship.**
2. **Reduces map only along a row.** `REDUCE_AXIS` over the LAST axis is
   `row_sum`/`row_max`. Any other axis has no mapping; the vector core reduces
   ALONG a row and nothing transposes.
3. **dtype.** tinygrad defaults to float32; this machine is fp16 in, MXFP7
   operands, FP22 accumulator. The frontend is pinned to `dtypes.float16`, and
   the accumulator's extra range is invisible to tinygrad's cost model. The
   product itself must be fp16 to match: an fp32 matmul over fp16 buffers has its
   casts exactly where the accumulator's are, so a matcher that only unwraps
   casts will match it and dispatch the wrong kernel.
4. **`Tensor.rand` costs 7 threefry kernels** of pure elementwise integer work.
   None of it maps; use `Tensor(numpy_array)`.

A fused elementwise chain was on this list and came off it: `x*2+1` is one
tinygrad kernel and two library entries, but `lang/vector.py` already turns an
expression tree into a vector chain, so the job was UOp-tree → L4 chain at the
level built for it — a translation rather than new machinery.

## 4. What generates

Matmul, epilogues, elementwise chains, and — via the chain generator — softmax
and the norms. Two readings carry more weight than their size suggests:

- **`rsqrt`, not `sqrt`, is what unlocked the norms.** tinygrad spells
  `rsqrt(z)` as `RECIP(SQRT(z))`, read as the PAIR onto `OpKind.RSQRT`, which
  lowers to a correct `VRSQRT`. Nothing maps `OpKind.SQRT`, so a bare `x.sqrt()`
  still refuses.
- **min and max arrive as selects.** `WHERE(CMPLT(a, b), b, a)` is `max(a, b)`
  and `WHERE(CMPLT(a, b), a, b)` is `min(a, b)`; `x.clip(lo, hi)` is two of them
  nested. Those are the only two select shapes read as arithmetic, because every
  other one is a real predicate.

## 5. The ceiling, and it is the one that matters for a DiT

**A row must be a multiple of 16 and at most VLMAX = 128.** So a DiT's attention
softmax at L=1024 REFUSES, and so does a layernorm over a 1024-wide model
dimension. This is pre-existing — `RowReduceKernel` carries the identical check —
but it means "softmax and the norms generate" is true at 64-wide rows and **false
at DiT rows**.

**And the escape hatch does not lift it.** Measured at 256 and 1024:
`ktpugrad.softmax`, `K.softmax`, `K.layernorm`, `K.rmsnorm`, `K.group_norm` and
`K.row_sum` all raise the same refusal, so the message names that rather than
sending the reader to a door that is also shut. The hierarchical form exists only
as `kernels.groupnorm.group_stats` — verified past 128, unexported, and wired
into no row-wise kernel.

`scaled_dot_product_attention` still needs the hatch: its score is a batched
contraction the matcher declines, and a rank-3 reduce is refused rather than
guessed at — reading `[2][32][64]` as rows=2 would be the wrong row count, so the
refusal is at rank and does not lean on a downstream span check.

Also refused, each by name: a fold down a column, a fold over two axes, a `MUL`
reduce, and a row where `VRED` cannot fold.

## 6. The shape of the answer

> tinygrad as an OPTIONAL frontend that dispatches into the same kernel library,
> with our own multi-stage kernels kept and reachable, and a documented list of
> the ops where the tinygrad path is slower than calling the library directly.

The refusals are asserted as tightly as the matches. A matcher that quietly
widened would dispatch a wrong kernel and report success — which is why every
refusal above is a test, and why the escape hatch is held to the same line: a
refused ast still raises rather than falling through to something that computes
a different thing.
