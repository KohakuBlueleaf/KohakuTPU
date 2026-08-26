---
title: SIMT PE — where this lands against shipped GPUs
summary: What the arithmetic width is worth in industry terms, which mobile parts it corresponds to, what class of rendering that makes plausible, and why arithmetic is not this machine's limit.
tags:
  - architecture
  - pe
  - gpu
  - performance
  - comparison
---

# Where this lands against shipped GPUs

> **Kind: none — this page classifies no design surface.** It positions this
> project's arithmetic width against shipped parts. Nothing in it is forced or
> chosen by the framework, and its conclusion — that arithmetic is not this
> machine's limit — is a measurement claim rather than a contract.

A floating-point rate is only useful if it tells you what to attempt. This page
establishes the comparison honestly, then spends most of its length on the part
that actually decides feasibility, which is not arithmetic.

**Read the provenance rules first, because every number here depends on them.**

- The **width** figures — how many fused multiply-adds a configuration issues
  per clock — are arithmetic over a named configuration. They are **PROJECTED**
  for a mesh and a device: no mesh of these PEs has been placed.
- The **clock** is an assumption. No frequency figure in this project is a
  closed-timing result; they are out-of-context synthesis estimates, they are
  the optimistic end, and a module in this repository has lost 0.740 ns between
  synthesis and routing. Every rate below is therefore computed at an
  **assumed 350 MHz** and labelled PROJECTED. It is not a measured frame rate
  and it is not a closed clock.
- The **area** figures that would say whether such a mesh fits are not current:
  every published LUT total for either PE predates the float tier's rebuild in
  binary32. [unit-counts](../unit-counts.md) says what each table does and does
  not cover. **This page therefore makes no claim about whether the mesh below
  fits**, only about what its arithmetic would be.

## 1. The unit of comparison is FMA per clock

Peak floating-point rate is `units × 2 × clock`, because a fused multiply-add is
two operations. Comparing unit counts across vendors is meaningful; comparing
GFLOPS across vendors and clock domains hides where the difference comes from,
which for an FPGA is always the clock.

ARM documents Mali per shader core, so the comparison is exact. Qualcomm does
not disclose an FP32 ALU count for Adreno, so that one is compared on quoted
throughput only.

| shipped part | FMA/clk per shader core |
|---|---:|
| Mali-G77, G57, G78 — Valhall gen 1–2 | **16** — one 16-wide execution engine |
| Mali-G310 — Valhall gen 3, entry | 16 minimum, scaling to 64 |
| Mali-G510, G610, G710 — Valhall gen 3 | **64** — two engines, dual datapath |

Sanity check on the older figure: a G77 MP11 at ~850 MHz gives
`11 × 16 × 2 × 0.85 GHz ≈ 299 GFLOP/s`, which matches published MP11 numbers.

## 2. What this configuration has

Both PE classes compute in IEEE binary32 with the same units, so their
multiply-adds are directly commensurable.

```
   SIMD PE, 4 float units                    4 FMA/clk
   SIMT PE, 8 float units                    8 FMA/clk

   mesh = 8 SIMD x 4  +  4 SIMT x 8  =  32 + 32  =  64 FMA/clk    PROJECTED
   device = 4 meshes                             = 256 FMA/clk    PROJECTED
```

**One mesh is 64 FMA per clock, which is one Mali-G610 or G710 shader core**,
and it is reached by the two PE classes contributing half each rather than by
either one being widened past its own sensible point. At four float units on the
SIMT side the same mesh is 48 and short.

That is the target the SIMT PE's float unit count was chosen against, and it is
the one figure on this page that is a design decision rather than a measurement
or an assumption.

## 3. Where that lands

All rows for this machine at an **assumed 350 MHz**, PROJECTED.

| | FMA/clk | clock | GFLOP/s |
|---|---:|---:|---:|
| **one of these meshes** | 64 | 350 MHz assumed | **44.8** |
| Mali-G57 / G77 core | 16 | ~950 MHz | ~30 |
| Mali-G310, minimum configuration | 16 | ~650 MHz | ~21 |
| Mali-G610 / G710 core | 64 | ~850 MHz | ~109 |
| **a four-mesh device** | 256 | 350 MHz assumed | **179.2** |
| Mali-G57 MC3 — Dimensity 700 class | 48 | ~950 MHz | ~91 |
| Adreno 540 — Snapdragon 835, 2017 | not disclosed | ~710 MHz | ~567 |
| Mali-G610 MC6 | 384 | ~850 MHz | ~653 |

Three readings.

**One mesh is four G57 cores wide and about 1.5× one in throughput.** Once the
width ratio reaches 4:1 the clock deficit stops deciding the outcome.

**One mesh matches one G610/G710 core in width exactly**, and would reach about
41% of it in throughput at the assumed clock. The remaining gap is entirely
clock — 350 MHz against ~850 — not units.

**The device would sit between a 2020 mid-range mobile GPU and a 2017
flagship** — roughly 2× a Mali-G57 MC3, and about a third of an Adreno 540. A
full G610 MC6 is not reachable: it is 1.5× the width *and* 2.4× the clock.

## 4. What that makes plausible

The useful question is not GFLOPS, it is whether the machine can issue enough
instructions per pixel. Issue capacity is one instruction per cycle per PE
across eight threads:

```
   4 meshes x 4 SIMT PE x 8 threads x 350 MHz  =  44.8 G thread-instructions/s
                                                  PROJECTED
```

Against that, two frame budgets at 2× overdraw:

| target | fragments/s | instructions each | issue used |
|---|---:|---:|---:|
| 720p, 30 fps | 55.3 M | 50 | **6.2%** |
| 1080p, 60 fps | 248.8 M | 200 | **111%** |

Read as an instruction budget rather than as a percentage, the same two rows
say:

```
   720p30, 2x overdraw    810 thread-instructions per fragment
   1080p60, 2x overdraw   180 thread-instructions per fragment
```

**The instruction budget is the honest form of the claim**, because 50 and 200
instructions per fragment are placeholders rather than a measured shader corpus,
and the percentage moves with the assumed clock while the budget does not.
1080p60 with 200-instruction shaders sits just past the ceiling rather than just
under it, and a 10% change in the assumed clock moves it across.

The supporting traffic is not the constraint, and it does not move with the
clock. A framebuffer at 720p30 is ~221 MB/s written and read; one RGBA bilinear
tap per fragment is 12 multiply-adds and a 32-byte entry read, so ~1.8 GB/s at
720p30 — against four DRAM channels.

**So a small real-time renderer at 720p30 is comfortably inside the arithmetic,
and 1080p60 with rich shaders is at or slightly over the ceiling** — as an
arithmetic bound, not as a frame rate.

## 5. What these numbers do not say

Everything above prices arithmetic. Rendering is not arithmetic-bound on this
machine, and the gap is fixed function.

- **There is no rasteriser.** Triangle setup, edge functions and coverage are
  software on the same PEs that shade. Their cost is unmeasured and it is the
  single largest unknown on this page. A Mali core does this in fixed function
  and spends none of its 16 or 64 units on it.
- **There is no texture sampler.** Address arithmetic is the integer path and
  costs nothing new; filtering is lane code at 12 multiply-adds per RGBA
  bilinear tap. Both are priced, but a shipped mobile GPU gets them free and
  this machine pays issue slots.
- **There is no depth or blend fixed function**, and **no atomics** — the A
  extension's opcode major is not in the legal set, so an atomic raises an
  illegal-instruction fault rather than being quietly ignored.
- **There is no shading-language path.** SPIR-V to this ISA is a designed route
  with no implementation anywhere in `src/` or `compiler/`. Nothing here runs a
  shader written in a shading language today.
- **No mesh has been placed.** Every figure this project has for either PE is
  out-of-context synthesis of one PE. The interconnect and the memory agent are
  budgeted for but not co-placed with a dozen PEs, and the clock is what would
  move.

**Precision is not on this list.** Both PE classes compute in IEEE binary32
throughout, which is the format desktop shading uses and a wider one than the
fp16 mobile fragment shaders actually run at. The only mobile advantage here is
a *rate* one: those parts run fp16 at double their FP32 rate, which this machine
does not.

**Integer multiply is not on this list either.** RV32M is built on both PE
classes, one 33×33 signed product per lane, so a pixel index or a Morton address
is one instruction rather than a shift-add chain. Divide and remainder fault,
deliberately — divide-by-a-constant strength-reduces to `mulhu`.

## 6. What to measure next, in order

1. **Software rasterisation cost**, in instructions per triangle and per
   fragment. Until this exists, §4 describes shading only, and the honest
   headline is "the shading is affordable", not "the frame is". This is the
   largest unknown on the page by a wide margin.
2. **A real shader corpus through the issue model**, so 50 and 200 instructions
   per fragment stop being placeholders. §4's instruction budgets — 810 and 180
   per fragment — are what such a corpus would be judged against.
3. **Re-measure both PEs against the current float tier.** No area figure this
   project has describes a PE the RTL can build today, so nothing on this page
   can yet say whether the mesh in §2 fits.
4. **Place and route one mesh.** Every figure here is out-of-context synthesis
   of one PE at a time; the clock is what would move, and every rate in §3 and
   §4 is computed on an assumption until it does.

Until item 1 lands, treat §4 as an upper bound on what the arithmetic permits,
not as a frame rate.
