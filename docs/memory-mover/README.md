# The memory mover

A layout, gather and fill engine inside MAG. It moves bytes without computing
on them, which is most of what [`../limits.md`](../limits.md) says this machine
cannot do.

| | |
|---|---|
| [`arch.md`](arch.md) | where it sits, the datapath, the buffers, and how it is sized |
| [`isa.md`](isa.md) | the command and descriptor encoding, bit for bit |
| [`prng.md`](prng.md) | the noise generator that rides beside it |
| [`compiler.md`](compiler.md) | what the compiler emits, and the verification ladder |

**This folder is meant to be implementable without re-deciding anything.**
Where a number is still open it is in §6 with the measurement that settles it,
and nothing in §6 blocks starting.

## 0. Status: built

`src/kohakumas/mm_mover.v` and `mm_prng.v` exist, are wired into `mag.v` as AXI
master `MEM_PORTS + 1`, and are verified:

| | |
|---|---|
| `xsim.py mm_prng` | Philox-4x32-10 against the published Random123 vectors |
| `xsim.py mm_mover` | COPY/relayout, padding, FILL, GENERATE, GATHER, faults |
| `xsim.py mag_system` | a GEMM **and** a mover transpose, through MAG |
| `xsim.py mm_mesh` | the minimal machine end to end: MAG + cluster + vector core |
| `run_dsl.py` | a DSL kernel compiled to level 3 and **executed as machine code in xsim** |
| `run_synth_check.ps1` | mover **331.8 MHz**, whole machine **325.6** -- §10 of `arch.md` |

What is **not** built: `TRANSPOSE` (needs the granule buffer, §5 of `arch.md`),
`GENERATE.NORMAL` (needs the tables in `prng.md` §5), bursting, the command
block, and anything compiler-side. `arch.md` §10 records the timing work, which
found its bugs in the **vector core**, not here.

---

## 1. Scope: two targets, and a pass/fail test

Not "general NN". Two concrete workloads, each checkable op by op.

**Target 1 -- a decoder-only transformer decode step with no software
fallback.**

| step | today | with the mover |
|---|---|---|
| embedding lookup | `one_hot @ table`, `T x V x C` MACs to move `T x C` | **gather** |
| `(L, H*D)` -> `(H, L, D)` | host transpose | **permute** |
| KV append | contorted cache layout, or the host | **one descriptor**, `arch.md` §7 |
| relayout between engines | a vector band that multiplies by 1.0 | **a descriptor** |
| norm, attention, MLP, LM head | already work | unchanged |

Two things stay outside, and neither is a data round trip -- they are scalars
passed with a kick the host is already making: the **attention extent**
(`ceil(t/BLOCK)` K tiles, which grows per step) and **sampling**.

**Target 2 -- an SDXL UNet.** Self-attention, cross-attention with
`Lq != Lkv`, SiLU and the sampler arithmetic already work. Skip-concat,
nearest-2x upsample and layout are mover jobs. `conv2d` is **built** -- it is a
DSL composition of `pad`/`slice`/`reshape`/`matmul` that needs no mover at all
(`ktpu.dsl.nn.conv2d`, `tests/ktpu/unit/test_conv.py`). GroupNorm's reductions
exist; only the op is missing. That leaves **noise**, which is
[`prng.md`](prng.md).

**Neither target needs a programmable general core.** That is the conclusion
`../general_cores/` reaches from the other side, and it is why this is the
piece to build first.

## 2. Why it is ranked ahead of the general cores

It is the only one of the two that unloads the bottleneck.

Attention is **vector bound** -- 8 to 16 vector cores is about 2x throughput
measured, with vector busy falling only 97% to 88%. Relayout, tile-order
conversion and `limits.md` §6.1's permutation-matmul workaround are all paid on
those cores today.

And the bandwidth to absorb it is there. `mag.v` records why the port count went
up: a single read engine *"stopped the machine scaling -- and it stopped while
nothing was saturated, so the constraint was the server, not the bandwidth."*
This spends a resource measurement says is spare to relieve one measurement says
is not.

## 3. What it absorbs

- **Order conversion between engines.** A cluster DRAINs in sub-tile order, a
  FILL reads L1-entry order, and only a vector store converts. A matmul reading
  another matmul's output was silently wrong at every size -- 1.26e+00 relative
  error, no exception, right bytes and wrong order. `lower()` currently repairs
  it with a relayout band.
- **Views the IR refuses.** `concat` is two writes into one region; `pad` is a
  bound and an offset; `permute` is a permutation of the stride list.
- **The gather.** `table[idx]`, `index_select`, and the MoE routing
  `moe_dense` deliberately does not do.

## 4. What it does NOT do

- **Compare.** No sort, no top-k, no argmax. That is `../general_cores/`.
- **Arithmetic.** A move that multiplies by 1.0 is the relayout band being
  deleted.
- **Scatter-add.** Read-modify-write with duplicate indices needs atomicity or
  a pre-sort. Plain scatter is the same engine with the descriptors exchanged
  and is in scope; accumulation is not.
- **Boolean mask selection.** The output length depends on the data, which a
  static-shape IR cannot hold. It can execute a compaction whose length
  something else already computed.

## 5. Work order

Each step is independently useful and independently testable.

1. **`arch.md` §3 core: COPY mode**, one descriptor pair, word granular. Kills
   the relayout band, which is the largest single win and the easiest to check
   -- the existing back-to-back GEMM tests already fail loudly when layout is
   wrong.
2. **GATHER**, with the index buffer. Unlocks embedding lookup and MoE.
3. **GENERATE** ([`prng.md`](prng.md)). Unlocks diffusion and training dropout.
4. **The granule buffer** for true transposes. Deferrable: until it exists,
   element-granular transposes work and are slow, and `compiler.md` §5 prices
   them so nothing falls into that regime silently.

## 6. Open, with what settles each

| question | settled by |
|---|---|
| granule buffer size | which transposes must be fast; one BRAM36 buys 32x32 at FP16 |
| index buffer size | the largest single gather a kernel issues; 1024 rows is one BRAM36 |
| shares the port quantiser? | whether a gathered weight bank should come back MXFP7 in one pass |
| one mover or several | measure it bandwidth-bound before adding a second |

None of these change the interface in [`isa.md`](isa.md), which is why they do
not block starting.
