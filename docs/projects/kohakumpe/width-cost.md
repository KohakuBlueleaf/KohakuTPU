---
title: Configurable width area cost
summary: Measured LUT, FF, DSP and BRAM for every configurable width on the SIMD and SIMT PEs, the marginal cost of each knob, and the interpolated and extrapolated cost at widths that were not synthesised.
tags:
  - architecture
  - pe
  - simd
  - simt
  - area
---

# Configurable width area cost

> **Kind: none — this page reports measurements of parts labelled elsewhere.**
> The widths priced here are this project's own free parameters, never
> framework-mandated sizes — a figure at one width is evidence about cost, not a
> statement that the width is required. Provenance rules are in
> [projects/README](../README.md).

GENERATED — do not edit by hand. Rebuild with `python scripts/py/khs_cost.py --fit --report <this file>` after a `python scripts/py/khs_sweep.py --all` campaign.

Every figure is out-of-context **synthesis** of one PE on `xcvu13p-fhgb2104-2L-e`, Vivado 2024.2, at a 3.333 ns target, `-flatten_hierarchy rebuilt`, `-directive default`. Nothing here is placed or routed: synthesis estimates are the optimistic end and are **not a closed frequency**.

> **STALE — this checked-in copy predates the current RTL and must be regenerated
> before any figure below is quoted.** It was produced from a campaign whose
> configuration strings carry `f16`, `f32`, `simd_fslots` and `dsp_en`. The first
> three configured an E8M15 float tier with two operand formats, which has been
> replaced by a binary32-only tier, and none of those parameters exists in
> `khs_unit` or in `scripts/py/khs_sweep.py` any more. The rows that price them
> therefore price a feature that is gone, and every absolute total below is a
> total for a PE that can no longer be built. The delta columns for knobs that
> *do* survive — `ilanes`, `permu`, `shiftu`, `red`, `fsfu`, `flanes`, `nacc`,
> `vregs`, `wb`, `vprim`, `rmem`, and the SIMT set — are the right shape but have
> not been re-taken.
>
> The prose price list with its provenance stated per table is
> [unit-counts](unit-counts.md); it carries the same caveat.
>
> The generator does not currently emit the tool version or a campaign date.
> Both belong in this header and should be added to `khs_cost.py`.

## Anchors

The ONE absolute figure per core. Everything after this section is a delta; to budget a configuration, start here and add the rows.

### SIMD anchor

| LUT | FF | DSP | BRAM | Fmax (MHz) |
|---:|---:|---:|---:|---:|
| 20,572 | 13,886 | 53 | 14.5 | 280.0 |

Measured at the configuration below. **This is NOT every feature at maximum** — read it. Every delta in the next section is this build with one knob moved.

```
dsp_en=1 f16=1 f32=1 facc=0 falu=1 fcvt=0 flanes=8 float=1 fsfu=1 ilanes=8
nacc=2 npart=16 permu=8 red=1 rmem=distributed shiftu=8 simd=8
simd_fslots=default simd_shround=default vprim=distributed vregs=8 wb=0
```

### SIMT anchor

| LUT | FF | DSP | BRAM | Fmax (MHz) |
|---:|---:|---:|---:|---:|
| 20,388 | 17,623 | 49 | 32.0 | 331.9 |

Measured at the configuration below. **This is NOT every feature at maximum** — read it. Every delta in the next section is this build with one knob moved.

```
f16=1 f32=1 flanes=8 fmodel=0 fpu=1 fsfu=1 imemwords=2048 instdepth=16
ipdom=1 ipdomd=8 l1lines=128 lanes=8 ldsb=-1 ldsbank=1 mask=1 memprim=block
recvdepth=512 shfl=1 shflu=-1 simd_fslots=default simd_shround=default
spadwords=2048 top=kht_pe vregprim=block waves=16
```

## What each feature costs

**Every number is a DELTA against that feature switched off.** The `vs` column names the reference, which is width 0 — not built — wherever the feature can be removed at all. Nothing in this table is an absolute total: to budget a configuration, take the anchor above and add the rows you want.

`measured` is a synthesis run. `interpolated` is a straight line between the two measured points either side. `extrapolated` continues the slope of the two nearest measured points off the end of the data — a prediction, not a result, and the weakest number here.

### SIMD

| feature | width | vs | ΔLUT | ΔFF | ΔDSP | ΔBRAM | basis |
|---|---:|---:|---:|---:|---:|---:|---|
| `dsp_en` | 0 | 0 | +0 | +0 | +0 | +0.0 | measured |
| `dsp_en` | 1 | 0 | +17,728 | +9,490 | +49 | +9.5 | measured |
| `f16` | 0 | 0 | +0 | +0 | +0 | +0.0 | measured |
| `f16` | 1 | 0 | +4,460 | +360 | +0 | +0.0 | measured |
| `f32` | 0 | 0 | +0 | +0 | +0 | +0.0 | measured |
| `f32` | 1 | 0 | +3,547 | +1,256 | +0 | +0.0 | measured |
| `facc` | 0 | 0 | +0 | +0 | +0 | +0.0 | measured |
| `facc` | 1 | 0 | +6,664 | +7,388 | +16 | +0.0 | measured |
| `falu` | 0 | 0 | +0 | +0 | +0 | +0.0 | assumed |
| `falu` | 1 | 0 | +0 | +0 | +0 | +0.0 | measured |
| `fcvt` | 0 | 0 | +0 | +0 | +0 | +0.0 | measured |
| `fcvt` | 1 | 0 | -128 | -16 | +0 | +0.0 | measured |
| `fcvt` | 2 | 0 | +820 | -5 | +0 | +0.0 | interpolated |
| `fcvt` | 4 | 0 | +2,509 | +2 | +0 | +0.0 | interpolated |
| `fcvt` | 8 | 0 | +5,454 | +6 | +0 | +0.0 | interpolated |
| `flanes` | 0 | 0 | +0 | +0 | +0 | +0.0 | extrapolated |
| `flanes` | 1 | 0 | +1,414 | +926 | +2 | +0.0 | extrapolated |
| `flanes` | 2 | 0 | +2,828 | +1,853 | +4 | +0.0 | measured |
| `flanes` | 4 | 0 | +5,656 | +3,706 | +8 | +0.0 | measured |
| `flanes` | 8 | 0 | +9,721 | +7,361 | +16 | +0.0 | measured |
| `fsfu` | 0 | 0 | +0 | +0 | +0 | +0.0 | measured |
| `fsfu` | 1 | 0 | +953 | +412 | +1 | +1.5 | measured |
| `fsfu` | 2 | 0 | +1,491 | +775 | +2 | +3.0 | measured |
| `fsfu` | 4 | 0 | +1,690 | +1,484 | +4 | +6.0 | interpolated |
| `fsfu` | 8 | 0 | +2,089 | +2,903 | +8 | +12.0 | measured |
| `ilanes` | 0 | 0 | +0 | +0 | +0 | +0.0 | measured |
| `ilanes` | 1 | 0 | +116 | +264 | +4 | +0.0 | interpolated |
| `ilanes` | 2 | 0 | +233 | +528 | +8 | +0.0 | measured |
| `ilanes` | 4 | 0 | +592 | +278 | +16 | -1.3 | interpolated |
| `ilanes` | 8 | 0 | +1,309 | -222 | +32 | -4.0 | measured |
| `nacc` | 1 | 1 | +0 | +0 | +0 | +0.0 | assumed |
| `nacc` | 2 | 1 | +0 | +0 | +0 | +0.0 | measured |
| `nacc` | 4 | 1 | +0 | +0 | +0 | +0.0 | assumed |
| `npart` | 4 | 4 | +0 | +0 | +0 | +0.0 | assumed |
| `npart` | 8 | 4 | +0 | +0 | +0 | +0.0 | assumed |
| `npart` | 16 | 4 | +0 | +0 | +0 | +0.0 | measured |
| `permu` | 0 | 0 | +0 | +0 | +0 | +0.0 | measured |
| `permu` | 1 | 0 | +815 | +269 | +0 | +0.0 | measured |
| `permu` | 2 | 0 | +1,286 | +284 | +0 | +0.0 | measured |
| `permu` | 4 | 0 | +1,851 | +288 | +0 | +0.0 | measured |
| `permu` | 8 | 0 | +1,760 | +30 | +0 | +0.0 | measured |
| `red` | 0 | 0 | +0 | +0 | +0 | +0.0 | measured |
| `red` | 1 | 0 | +521 | +136 | +0 | +0.0 | measured |
| `rmem` | block | block | +0 | +0 | +0 | +0.0 | measured |
| `rmem` | distributed | block | +58 | +556 | +0 | -4.0 | measured |
| `shiftu` | 0 | 0 | +0 | +0 | +0 | +0.0 | measured |
| `shiftu` | 1 | 0 | +62 | +157 | +0 | +0.0 | interpolated |
| `shiftu` | 2 | 0 | +125 | +314 | +0 | +0.0 | measured |
| `shiftu` | 4 | 0 | +501 | +239 | +0 | +0.0 | interpolated |
| `shiftu` | 8 | 0 | +1,254 | +88 | +0 | +0.0 | measured |
| `simd_fslots` | 8 | 8 | +0 | +0 | +0 | +0.0 | measured |
| `simd_fslots` | 16 | 8 | +31 | -8 | +0 | +0.0 | measured |
| `simd_fslots` | default | 8 | +31 | -8 | +0 | +0.0 | measured |
| `simd_shround` | 0 | 0 | +0 | +0 | +0 | +0.0 | measured |
| `simd_shround` | default | 0 | +120 | +44 | +0 | +0.0 | measured |
| `vprim` | block | block | +0 | +0 | +0 | +0.0 | measured |
| `vprim` | distributed | block | +256 | +763 | +0 | -12.0 | measured |
| `vregs` | 8 | 8 | +0 | +0 | +0 | +0.0 | measured |
| `vregs` | 16 | 8 | -127 | -6 | +0 | +0.0 | measured |
| `vregs` | 32 | 8 | -381 | -18 | +0 | +0.0 | extrapolated |
| `wb` | 0 | 0 | +0 | +0 | +0 | +0.0 | measured |
| `wb` | 1 | 0 | +1 | +252 | +0 | +0.0 | measured |

### SIMT

| feature | width | vs | ΔLUT | ΔFF | ΔDSP | ΔBRAM | basis |
|---|---:|---:|---:|---:|---:|---:|---|
| `f16` | 0 | 0 | +0 | +0 | +0 | +0.0 | assumed |
| `f16` | 1 | 0 | +0 | +0 | +0 | +0.0 | measured |
| `f32` | 0 | 0 | +0 | +0 | +0 | +0.0 | assumed |
| `f32` | 1 | 0 | +0 | +0 | +0 | +0.0 | measured |
| `flanes` | 0 | 0 | +0 | +0 | +0 | +0.0 | extrapolated |
| `flanes` | 1 | 0 | +963 | +830 | +2 | +0.0 | extrapolated |
| `flanes` | 2 | 0 | +1,926 | +1,660 | +4 | +0.0 | measured |
| `flanes` | 4 | 0 | +3,852 | +3,320 | +8 | +0.0 | measured |
| `flanes` | 8 | 0 | +7,458 | +6,657 | +16 | +0.0 | measured |
| `fsfu` | 0 | 0 | +0 | +0 | +0 | +0.0 | measured |
| `fsfu` | 1 | 0 | +432 | +343 | +1 | +1.5 | measured |
| `fsfu` | 2 | 0 | +888 | +675 | +2 | +3.0 | measured |
| `fsfu` | 4 | 0 | +1,425 | +1,381 | +4 | +6.0 | interpolated |
| `fsfu` | 8 | 0 | +2,498 | +2,793 | +8 | +12.0 | measured |
| `ipdom` | 0 | 0 | +0 | +0 | +0 | +0.0 | assumed |
| `ipdom` | 1 | 0 | +0 | +0 | +0 | +0.0 | measured |
| `ldsb` | -1 | -1 | +0 | +0 | +0 | +0.0 | interpolated |
| `ldsb` | 0 | -1 | -1,709 | -320 | +0 | -6.0 | measured |
| `ldsb` | 1 | -1 | -1,596 | -11 | +0 | -6.0 | measured |
| `ldsb` | 2 | -1 | -1,096 | +0 | +0 | -6.0 | measured |
| `ldsb` | 4 | -1 | -881 | -7 | +0 | -4.0 | measured |
| `ldsb` | 8 | -1 | +0 | +0 | +0 | +0.0 | measured |
| `mask` | 0 | 0 | +0 | +0 | +0 | +0.0 | assumed |
| `mask` | 1 | 0 | +0 | +0 | +0 | +0.0 | measured |
| `shfl` | 0 | 0 | +0 | +0 | +0 | +0.0 | assumed |
| `shfl` | 1 | 0 | +0 | +0 | +0 | +0.0 | measured |
| `shflu` | -1 | -1 | +0 | +0 | +0 | +0.0 | interpolated |
| `shflu` | 0 | -1 | -850 | -14 | +0 | +0.0 | measured |
| `shflu` | 1 | -1 | -273 | -12 | +0 | +0.0 | measured |
| `shflu` | 2 | -1 | -272 | +2 | +0 | +0.0 | measured |
| `shflu` | 4 | -1 | -69 | +9 | +0 | +0.0 | measured |
| `shflu` | 8 | -1 | +0 | +0 | +0 | +0.0 | measured |
| `waves` | 4 | 4 | +0 | +0 | +0 | +0.0 | extrapolated |
| `waves` | 8 | 4 | +478 | +254 | +0 | +0.0 | measured |
| `waves` | 16 | 4 | +1,432 | +762 | +0 | +0.0 | measured |

## How wrong this model is

The terms are marginals from a single base, so the estimate is additive by construction and cannot see two features that share control logic. Re-estimating a SINGLE-knob row proves nothing — that row IS the point its own term came from. The rows below moved several knobs at once and were **never used to fit anything**, so they are the only honest test here.

| row | knobs moved | measured LUT | estimate | error |
|---|---:|---:|---:|---:|
| `base_simd_1` | 3 | 9,069 | 9,898 | +829 (+9.1%) |
| `empty_0` | 7 | 3,507 | 5,054 | +1,547 (+44.1%) |
| `flanes_0` | 2 | 9,069 | 9,898 | +829 (+9.1%) |
| `nacc_0` | 2 | 27,452 | 27,236 | -216 (-0.8%) |
| `nacc_2` | 2 | 27,487 | 27,236 | -251 (-0.9%) |
| `npart_0` | 2 | 27,463 | 27,236 | -227 (-0.8%) |
| `shape_simd_0` | 4 | 17,581 | 17,640 | +59 (+0.3%) |
| `simd_width_0` | 6 | 7,747 | 11,000 | +3,253 (+42.0%) |
| `simd_width_1` | 6 | 11,961 | 15,128 | +3,167 (+26.5%) |
| `base_simt_0` | 2 | 11,988 | 12,498 | +510 (+4.3%) |
| `shape_simt_0` | 2 | 19,249 | 19,020 | -229 (-1.2%) |

**The error is one-directional and that matters for budgeting.** Removing features together saves MORE than the sum of removing them one at a time, because shared control and mux logic goes away once when its last consumer does. A stripped configuration therefore comes in CHEAPER than this model predicts, never dearer — so an estimate used as a ceiling is safe, and one used as a floor is not.

These rows moved several generics at once and are therefore NOT used to derive any per-knob term:

- `base_simd_1` — flanes, float, fsfu
- `empty_0` — flanes, float, fsfu, ilanes, permu, red, shiftu
- `flanes_0` — flanes, fsfu
- `nacc_0` — facc, nacc
- `nacc_2` — facc, nacc
- `npart_0` — facc, npart
- `shape_simd_0` — fcvt, ilanes, permu, shiftu
- `simd_width_0` — flanes, ilanes, npart, permu, shiftu, simd
- `simd_width_1` — flanes, ilanes, npart, permu, shiftu, simd
- `base_simt_0` — flanes, fsfu
- `shape_simt_0` — ldsb, shflu

