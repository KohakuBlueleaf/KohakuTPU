---
title: Fast 3x3 convolution
summary: The SDXL UNet convolutions as an ordinary matmul over an im2col the memory mover builds on the card — the walk, the address formulas, stride and upsampling, and what each costs.
tags:
  - kohakutpu
  - compiler
  - kernels
  - sysnode
---

# Fast 3x3 conv on this machine

> **Kind: Yours throughout.** Lowering a convolution to a matmul over an
> on-card im2col is this project's decision about its own operand format. The
> framework supplies the pieces it uses — the mover as a package step, the
> tracked-format conversion — and neither offers nor forbids the lowering.

Target: the SDXL UNet convolutions, on the instructions that already exist — no
new opcode, no RTL change.

```
    1x128x128x320   320 -> 320, 3x3
    1x 64x 64x640   640 -> 640, 3x3
    1x 32x 32x1280  1280 -> 1280, 3x3
```

## 1. The three shapes are one shape

Every one of them is **15.1 GMAC**:

| shape | M = H*W | N | K = 9*C | MAC |
|---|---|---|---|---|
| 128x128x320 | 16384 | 320 | 2880 | 1.51e10 |
| 64x64x640 | 4096 | 640 | 5760 | 1.51e10 |
| 32x32x1280 | 1024 | 1280 | 11520 | 1.51e10 |

Resolution quarters as channels double, so the UNet holds compute constant down
the stack. Every shape satisfies the hardware's `M = 4a, N = 4b, K = 32c`.

**ARITHMETIC**, at the v7 population of 30 matmul clusters and the measured
512 MAC/cycle per cluster ([results.md](results.md) §8): 15,360 MAC/cycle, so
one of these layers is 983,000 cycles — 9.8 ms at a 100 MHz matmul clock,
3.3 ms at 300 MHz, on the arithmetic alone.

## 2. Conv is a matmul; the memory pattern is the mover's

A `C_in -> C_out`, `kh x kw` convolution is

```
    [patches] x [C_in*kh*kw] x [C_out]
```

one row per output pixel, K tap-major then channel. The cluster runs it as an
ORDINARY matmul (`ops.matmul`, `nk = 1`): no conv-specific FILL, no lane
offset, no conv order in the cluster path. Everything conv-specific is the
operand's memory pattern, and that is the memory mover's job — its walkers are
six-dimensional affine, and its transform slot quantises on the way:

1. **The activation** lands as `PadHWC` — a zero-padded channels-last image
   `[hp][wp][cp]`, FP16, with `C` rounded up to a whole 32-channel block. The
   host packs it, or the mover copies a produced `[H][W][C]` into it (a FILL of
   zeros, then one COPY).
2. **The im2col** is the mover's converting move: its source walk gathers the
   windows FP16 entry by FP16 entry, the quantiser converts each, and the
   destination walk writes the dense MXFP7 operand `MxEntry(gt, 1, 0)` — the
   exact order the matmul FILLs. The patch matrix never exists in FP16.
3. **The weights** are `[C_out][kh*kw*cp]` (`weights_for_k`, zero-padded
   channels), FP16 on the host; the tracked-format rule converts them once and
   keeps the copy for every later call.

Padding is the zeros around the image: nothing masks anything.

## 3. The walk

`gt` lane groups per tile, `ow4` the output width rounded to whole lanes (a lane
group never crosses a row), `TX = ow4/(4*gt)` tiles per row, `nb = cp/32`
channel blocks, `nch = kh*kw*nb` K-chunks, `px = cp*2` bytes a pixel. Output row
`r = oy*ow4 + ox`. Every level of the walk is affine on both sides:

| level | count | source stride (bytes) | destination stride (bytes) |
|---|---|---|---|
| output row `oy` | `oh` | `s*wp*px` | `TX*nch*gt*128` |
| tile `tx` | `TX` | `4*gt*s*px` | `nch*gt*128` |
| tap row `dy` | `kh` | `wp*px` | `kw*nb*gt*128` |
| tap column `dx` | `kw` | `px` | `nb*gt*128` |
| channel block `cb` | `nb` | `64` | `gt*128` |
| lane group `g` | `gt` | `4*s*px` | `128` |
| lane (source only) | 4 | `s*px` | — |
| word (source only) | 2 | 32 | — |

The source base is the image plus `(sy*wp + sx)*px` (the window origin; zero
except for an upsample class), the destination base the operand. The
destination steps once per entry and writes its four words; the source walk
feeds eight words per entry, lane-major, which is an FP16 entry.

Eight levels against a walker's six: the source spends two on lane and word,
so **the four widest entry levels go to the hardware and every other
combination is one move** (`ops.conv2d.im2col_moves`). At 128x128x320, `gt =
32` makes `TX = 1` and the layer is **three moves**; `gt = 16` makes it six.

`compiler/tests/test_conv2d.py` checks the operand the moves write BYTE FOR
BYTE against the patch matrix built in numpy and packed by `MxEntry.pack`, and
the convolutions against float64.

## 4. Stride and upsampling

**Stride `s`** multiplies two source strides — lane and output row — and
changes nothing else: only outputs that exist are gathered, so a stride-2
layer is a quarter of the rows of a dense one and no MAC is discarded
(`conv2d_stride2`).

**Nearest-2x then 3x3** (`Upsample2D`) needs no 2x activation: output `(2y+iy,
2x+ix)` reads `a[y + (iy+dy-1)//2][x + (ix+dx-1)//2]`, which over three taps
takes two values. Each residue class `(iy, ix)` is a 2x2 convolution over the
ORIGINAL image with its window origin shifted by `(iy, ix)` and weights folded
on the host (`weights_for_upsample2`, exact — a fold is an addition).
16 MAC per input pixel against 36.

## 5. What it costs

The im2col is a write of the operand, `kh*kw` times the activation in MXFP7
(half its FP16 bytes): 9 x 10.5 MB / 2 = 47 MB at 128x128x320. It reads each
pixel `kh*kw` times.

Measured on the card model (`scripts/py/sw_conv.py`, node cycles, the im2col in
a package of its own; an empty package is 896):

| conv | im2col | entries | cycles | over empty | per entry | matmul package |
|---|---|---|---|---|---|---|
| 8x8x32 -> 32, s1 | 1 move | 144 | 14,660 | 13,764 | 96 | 220,119 |
| 8x8x20 -> 32, s1 (C padded to 32) | 1 move | 144 | 14,660 | 13,764 | 96 | 220,119 |
| 12x12x32 -> 32, s2 | 1 move | 108 | 18,837 | 17,941 | 166 | 169,288 |

Each im2col operand read back BYTE-IDENTICAL to the numpy patch matrix packed
by `MxEntry.pack`; results 1.91e-2, 1.37e-2 and 1.46e-2 from float64, 0.5-1.0
scale-ulp from the unit models. The stride-2 walk costs more per entry: its
lane step is two pixels, so a lane's two words are never adjacent to the next
lane's.

## 6. What is not done

| # | thing | why it stands |
|---|---|---|
| 1 | A batch axis | `conv2d` takes one `[H][W][C]` image; a batch is one call per image. |
| 2 | Conv into conv | The result is `[oh*ow4][C_out]` with lane padding in each row; the next conv's `PadHWC` copy from it needs a walk that skips the padding columns. Not built. |
| 3 | The im2col is materialised | `kh*kw` x the activation. The alternative is the walk in the FILL path itself (a descriptor-driven fill), which would never write it; that is an RTL change in `mag_mem_port.v`. |
| 4 | `C` not a multiple of 16 from a produced activation | The pad copy moves whole 32-byte words, so a produced activation's channels must be a multiple of 16; a host upload has no such limit. |
| 5 | A per-channel bias | `kernels.conv2d_bias` adds the bias in a second pass (`ops.residual`) at the result's full `[rows][N]` shape, broadcast once on the host: an elementwise pass takes operands of one length, and a per-channel read is address-dependent. The shape it should have is a drain epilogue taking the per-channel row; the emitter has no fill for a second epilogue operand. |
