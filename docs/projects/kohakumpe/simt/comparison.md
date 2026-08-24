---
title: SIMT PE — where this lands against shipped GPUs
summary: What the measured numbers are worth in industry terms, which mobile parts they correspond to, and what class of rendering workload that makes plausible. Arithmetic is not this machine's limit; fixed function and the software stack are.
tags:
  - architecture
  - pe
  - gpu
  - performance
  - comparison
---

# Where this lands against shipped GPUs

A FLOPS number is only useful if it tells you what to attempt. This page
establishes the comparison honestly, then spends most of its length on the part
that actually decides feasibility, which is not arithmetic.

Every figure for this machine is one accelerator on one part,
`xcvu13p-fhgb2104-2L-e`, out-of-context synthesis at the **2.857 ns ask
(350 MHz)** unless a row says otherwise.

## 1. The unit of comparison is FMA per clock

Peak floating-point rate is `lanes × 2 × clock`, because a fused multiply-add is
two operations. Comparing lane counts across vendors is meaningful; comparing
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

Sanity check on the older figure: G77 MP11 at ~850 MHz gives
`11 × 16 × 2 × 0.85 GHz ≈ 299 GFLOP/s`, which matches published MP11 numbers.

## 2. What this machine has

Both PE classes are now **measured at the configuration of record**, at the
2.857 ns ask, on `xcvu13p-fhgb2104-2L-e`:

| unit | LUT | FF | BRAM | DSP48 | Fmax | slack |
|---|---:|---:|---:|---:|---:|---:|
| **SIMT PE** — 8 int + 8 float lanes, RV32M | **21,586** | 17,268 | 30.5 | **48** | **365.6 MHz** | +0.122 |
| **SIMD PE** — SIMD 8 + 4 float lanes | **13,772** | 10,126 | 13 | **72** | 353.4 MHz | +0.027 |
| controller PE — `rv_pe`, `SIMD_EN = 0` | 2,477 | 4,140 | 5 | 0 | 377.9 MHz | — |

Nothing above is an estimate. The SIMT PE figure is the whole unit — SIMT core,
windows, banked LDS, L1, requestor, fabric port — with the float tier and the
integer multiplier both built. The float tier takes FP32 or FP16 operands per
instruction; that is not a build option and there is no configuration of this PE
that has the tier and refuses one of the two widths.

> Superseded figures that appear in older revisions, all at the **3.333 or
> 2.500 ns ask** and none of them this machine: `kht_core` at 9,653 LUT /
> 279.5 MHz; the integer-only assembled PE at 15,638 LUT / 392.8 MHz; the
> float-without-multiplier build at 19,215 LUT / 405.2 MHz; and the ~22k LUT
> *estimate* for a float-capable PE that this measurement replaced. Quote none
> of them without its ask.

Per-clock arithmetic, at the configuration the rendering target needs:

```
   SIMD PE, 4 float lanes                    4 FMA/clk
   SIMT PE, 8 float lanes                    8 FMA/clk

   mesh = 8 DSP x 4  +  4 GPU x 8   =  32 + 32  =  64 FMA/clk
   device = 4 meshes                             = 256 FMA/clk
```

**The target is met exactly on width.** One mesh is 64 FP FMA per clock, which
is one Mali-G610/G710 shader core, and it is reached by the two PE classes
contributing half each rather than by either one being widened past its own
sensible point.

### The mesh fits, with room

| | count | LUT each | LUT | DSP48 | BRAM | FMA/clk |
|---|---:|---:|---:|---:|---:|---:|
| SIMD PE, SIMD 8 + 4 float | 8 | 13,772 | 110,176 | 576 | 104 | 32 |
| SIMT PE, 8 int + 8 float | 4 | 21,586 | 86,344 | 192 | 122 | 32 |
| controller PE | 2 | 2,477 | 4,954 | 0 | 10 | — |
| **mesh total** | | | **201,474** | **768** | **236** | **64** |
| against the budget | | | ~350,000 | 3,072 | 672 | — |
| **used** | | | **58 %** | **25 %** | **35 %** | |

The LUT budget is the ~350,000 a mesh has once the fabric and the memory agent
are paid for ([dsp/performance](../simd/performance.md#what-a-mesh-holds));
the DSP48 and BRAM columns are **per SLR** on this part
([ship](../../../projects/kohakutpu/ship.md)), which is the right denominator
because a mesh is placed in one.

**The arithmetic target is not what constrains the mesh.** At 58 % of the LUT
and a quarter of the DSP48, the width could be bought again — which is the
finding, because for eighteen months the assumption was that reaching a shader
core's width would exhaust the die.

### The clock

Both PE classes close **above** the 350 MHz ask in synthesis — 365.6 and 353.4 —
so the mesh clock is set by the slower of the two rather than by either missing.
Every rate below is computed at **350 MHz**, the ask, not at either Fmax:
out-of-context synthesis is not routing, and this project has measured a module
lose 0.740 ns between the two.

## 3. Where that lands

All rows at 350 MHz for this machine.

| | FMA/clk | clock | GFLOP/s |
|---|---:|---:|---:|
| **one of our meshes** | 64 | 350 MHz | **44.8** |
| Mali-G57 / G77 core | 16 | ~950 MHz | ~30 |
| Mali-G310, minimum config | 16 | ~650 MHz | ~21 |
| Mali-G610 / G710 core | 64 | ~850 MHz | ~109 |
| **our whole device** | 256 | 350 MHz | **179.2** |
| Mali-G57 MC3 — Dimensity 700 class | 48 | ~950 MHz | ~91 |
| Adreno 540 — Snapdragon 835, 2017 | not disclosed | ~710 MHz | ~567 |
| Mali-G610 MC6 | 384 | ~850 MHz | ~653 |

Three readings.

**One mesh is four G57 cores wide and about 1.5× one in throughput.** Once the
width ratio reaches 4:1 the clock deficit stops deciding the outcome.

**One mesh matches one G610/G710 core in width exactly**, and reaches **41 %** of
it in throughput. That is the standing target, and the remaining gap is entirely
clock — 350 MHz against ~850 — not lanes.

**The device sits between a 2020 mid-range mobile GPU and a 2017 flagship** —
almost exactly **2× a Mali-G57 MC3**, and **32 % of an Adreno 540**. A full
G610 MC6 is not reachable: it is 1.5× the width *and* 2.4× the clock.

## 4. What that makes plausible

The useful question is not GFLOPS, it is whether the machine can issue enough
instructions per pixel. Issue capacity is one instruction per cycle per PE across
eight threads:

```
   4 meshes x 4 SIMT PE x 8 threads x 350 MHz  =  44.8 G thread-instructions/s
```

Against that, two frame budgets at 2× overdraw:

| target | fragments/s | instructions each | issue used |
|---|---:|---:|---:|
| 720p, 30 fps | 55.3 M | 50 | **6.2 %** |
| 1080p, 60 fps | 248.8 M | 200 | **111 %** |

Read as an instruction budget rather than as a percentage, the same two rows say:

```
   720p30, 2x overdraw    810 thread-instructions per fragment
   1080p60, 2x overdraw   180 thread-instructions per fragment
```

**1080p60 with 200-instruction shaders is now just PAST the ceiling rather than
just under it** — 111 % of issue, where the 380 MHz working assumption this page
previously carried put it at 102 %. The measured clock moved the crossing point,
and the honest form of the claim is the instruction budget: 180 per fragment at
1080p60, 810 at 720p30.

The supporting traffic is not the constraint either, and it does not move with
the clock. A framebuffer at 720p30 is ~221 MB/s written and read; one RGBA
bilinear tap per fragment is 12 FMAs and a 32-byte entry read, so ~1.8 GB/s at
720p30 — against four DRAM channels.

**So a small real-time renderer at 720p30 is comfortably inside the arithmetic,
and 1080p60 with rich shaders is at or slightly over the ceiling.** That is a
real result and it is the reason this comparison is worth writing down.

## 5. What these numbers do not say

Everything above prices arithmetic. Rendering is not arithmetic-bound on this
machine, and the gap is fixed function. **Every item below was re-checked
against the tree for this revision and every one is still true.**

- **There is no rasteriser.** Triangle setup, edge functions and coverage are
  software on the same PEs that shade. Their cost is unmeasured and it is the
  single largest unknown in this page. A Mali core does this in fixed function
  and spends none of its 16 or 64 lanes on it.
- **There is no texture sampler.** Address math is the integer path and costs
  nothing new; filtering is lane code at 12 FMAs per RGBA bilinear tap. Both are
  priced, but a shipped mobile GPU gets them free and we pay issue slots.
- **There is no depth or blend fixed function**, and **no atomics** — the A
  extension's major is not in `kht_predec`'s legal set, so an `amo*` opcode
  raises an illegal-instruction fault rather than being quietly ignored.
- **The API stack is planned, not built.** SPIR-V → NIR → this ISA is a designed
  path with no implementation anywhere in `src/` or `compiler/`. Nothing here
  runs a shader written in GLSL today.

Precision is *not* on this list. E8M15 carries FP32's full 8-bit exponent, so
range is FP32-equivalent and only the significand is short — 1.5e-5 relative
error, which is 32× better than the fp16 that mobile fragment shaders actually
run at, and more range than the FP24 that shipped in DX9-era hardware. For
colour, filter weights and interpolation this is above the bar rather than below
it. The only mobile advantage is a *rate* one: those parts run fp16 at double
their FP32 rate, bought by dropping to E5M10.

**Integer multiply is no longer on this list either.** RV32M `mul`/`mulh`/
`mulhsu`/`mulhu` are built, one 33×33 signed product per lane, so `y * width + x`
is one instruction rather than a shift-add chain. Divide and remainder still
fault, deliberately — divide-by-a-constant strength-reduces to `mulhu`.

## 6. What to measure next, in order

Item 1 of the previous revision — *"the eight-lane float build, which settles the
Fmax derate and turns ~22k LUT from ESTIMATE into a number"* — **is done**, and
§2 is that number. What is left:

1. **Software rasterisation cost**, in instructions per triangle and per
   fragment. Until this exists, §4's frame budgets describe shading only, and
   the honest headline is "the shading is affordable", not "the frame is". This
   is now the largest unknown on the page by a wide margin.
2. **A real shader corpus through the issue model**, so 50 and 200 instructions
   per fragment stop being placeholders. §4's instruction budgets — 810 and 180
   per fragment — are what such a corpus would be judged against.
3. **Place and route one mesh.** Every figure here is out-of-context synthesis
   at 58 % LUT occupancy; the interconnect and the memory agent are budgeted for
   but not co-placed with twelve PEs, and the clock is what would move.

Until item 1 lands, treat §4 as an upper bound on what the arithmetic permits,
not a frame rate.
