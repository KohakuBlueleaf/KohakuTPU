---
title: SDXL as a requirements inventory
summary: Every layer SDXL issues, the op it becomes on this machine, whether that op exists, and the kernels the gaps need — now with the gaps closed, and with the conversion cost measured in CYCLES rather than in host round trips.
tags:
  - kohakutpu
  - compiler
  - kernels
  - memory
---

# SDXL as a requirements inventory

> **Kind: Yours throughout.** Which layers a workload issues, which op each
> becomes and which kernels close the gaps are this project's inventory against
> its own instruction set. A second accelerator would produce a different table
> from the same framework.

**This page is not a plan to run SDXL.** SDXL is used here as the probe: it is a
modern network with convolution, two kinds of attention, three kinds of
normalisation, gating, resampling and a decoder, so enumerating what it issues
enumerates what this machine is missing. The output is the inventory and the
kernel designs, not a demo.

Every number is labelled:

| tag | meaning |
|---|---|
| **MEASURED** | run on the unit models in this repository, on the date of this page. No figure here came off the card. |
| **SOURCE** | read out of a file, either ours or a reference implementation under `.ref/` |
| **ARITHMETIC** | derived from MEASURED or SOURCE figures; the derivation is shown |

Nothing on this page has been run on silicon. Where a claim depends on the card,
it says so and says what would settle it.

---

## 1. What SDXL is, module by module

SOURCE for the UNet: `.ref/sd-scripts/library/sdxl_original_unet.py`.
SOURCE for the VAE: `.ref/ComfyUI/comfy/ldm/modules/diffusionmodules/model.py`
with the config at `.ref/ComfyUI/comfy/sd.py:652`.
SOURCE for the text encoders: `.ref/ComfyUI/comfy/sdxl_clip.py` and
`clip_config_bigg.json`.

### 1.1 The five stages of one image

| # | stage | runs | what it is |
|---|---|---|---|
| 1 | tokenise | 2 (cond, uncond) | host. Two tokenisers, 77 tokens each |
| 2 | text encode | 2 x 2 encoders | CLIP-L (768) and OpenCLIP-bigG (1280), penultimate hidden states concatenated to **2048**; bigG's projected pooled output is 1280 |
| 3 | conditioning vector | 2 | `concat(pooled[1280], sinusoid(orig_hw)[512], sinusoid(crop_tl)[512], sinusoid(target_hw)[512])` = **2816** |
| 4 | UNet | steps x 2 | the diffusion loop |
| 5 | VAE decode | 1 | 4-channel latent to RGB, 8x upsample |

`2816 = 1280 + 3 x 512`, SOURCE `sdxl_minimal_inference.py:195-199`.

### 1.2 The UNet, at 1024x1024

Latent is `128x128x4`. `model_channels = 320`, `channel_mult = [1,2,4]`,
`num_res_blocks = 2`, `transformer_depth = [_, 2, 10]`, `context_dim = 2048`,
`num_head_channels = 64`, `use_linear_projection = True`.

| level | spatial | channels | heads | transformer depth |
|---|---|---|---|---|
| 0 | 128x128 | 320 | — | none |
| 1 | 64x64 | 640 | 10 | 2 |
| 2 | 32x32 | 1280 | 20 | 10 |
| mid | 32x32 | 1280 | 20 | 10 |

**Head dim is 64 at every level** — 320/5, 640/10, 1280/20 — and 64 is two
32-element K-blocks, so a head never splits one.

Instance counts, ARITHMETIC from the module tree:

| module | count | note |
|---|---|---|
| `ResnetBlock2D` | 17 | 6 down, 2 mid, 9 up |
| `Transformer2DModel` | 11 | 2 + 2 down, 1 mid, 3 + 3 up |
| `BasicTransformerBlock` | **70** | 4 + 20 + 10 + 30 + 6 |
| `Downsample2D` (3x3 stride 2) | 2 | |
| `Upsample2D` (nearest 2x + 3x3) | 2 | |
| 3x3 convolutions | **40** | 34 in resnets, 2 down, 2 up, conv_in, conv_out |
| 1x1 convolutions (resnet skip) | 11 | wherever `in != out` |
| `nn.Linear` | **743** | 700 in transformer blocks, 22 proj_in/out, 17 resnet emb, 4 in the embeddings |
| `LayerNorm` | **210** | 3 per transformer block |
| `GroupNorm(32, ...)` | **46** | 34 resnet (eps 1e-5), 11 transformer (eps 1e-6), 1 final |
| `SiLU` | 54 | |
| `GELU` (inside GEGLU) | 70 | |
| softmax | **140** | 70 self, 70 cross |
| residual add | 255 | |
| channel concat (skip) | 9 | `torch.cat([h, hs.pop()], dim=1)` |

### 1.3 What each layer actually issues

**`ResnetBlock2D(Cin, Cout)`** — `sdxl_original_unet.py:298`

```
   GroupNorm(32, Cin) -> SiLU -> Conv3x3(Cin, Cout)
   + Linear(1280, Cout)(SiLU(emb))[:, :, None, None]      broadcast over H,W
   GroupNorm(32, Cout) -> SiLU -> Conv3x3(Cout, Cout)
   + (Conv1x1(Cin, Cout) if Cin != Cout else identity)(x)
```

Note the order: **norm, then activation, then conv.** A `conv -> activation`
fused kernel is fitting the wrong shape; `group_norm -> silu` is the pair worth
fusing, and `kernels.group_norm_silu` already is that kernel.

**`Transformer2DModel`** — `:678`

```
   residual = x                                          [N, C, H, W]
   GroupNorm(32, C, eps=1e-6)
   permute(0,2,3,1).reshape(N, H*W, C)                   NCHW -> tokens
   Linear(C, C)                                          proj_in, SQUARE
   depth x BasicTransformerBlock
   Linear(C, C)                                          proj_out, SQUARE
   reshape(N,H,W,C).permute(0,3,1,2)                     tokens -> NCHW
   + residual
```

`inner_dim = heads * dim_head == in_channels` at every SDXL level, so both
projections are square. `use_linear_projection=True`, so they are `Linear`, not
`Conv1x1`.

**`BasicTransformerBlock`** — `:603`

```
   x = attn1(LayerNorm(x))              + x     self,  ctx = x
   x = attn2(LayerNorm(x), context)     + x     cross, ctx = 2048
   x = ff(LayerNorm(x))                 + x
```

**`CrossAttention`** — `:391`

```
   q = Linear(dim,  heads*64, bias=False)(x)
   k = Linear(ctx,  heads*64, bias=False)(source)
   v = Linear(ctx,  heads*64, bias=False)(source)
   reshape(B, L, heads, 64).permute(0,2,1,3).reshape(B*heads, L, 64)   x3
   softmax(q @ k.T * 64**-0.5) @ v
   reshape(B, heads, L, 64).permute(0,2,1,3).reshape(B, L, heads*64)
   Linear(heads*64, dim)                                    to_out.0, HAS bias
```

**`FeedForward`** — `:581`. `net.0` is GEGLU, `net.1` Identity, `net.2` the
projection out.

```
   h, gate = Linear(dim, 2*4*dim)(x).chunk(2, dim=-1)
   h * gelu(gate)                              gate is the SECOND half
   Linear(4*dim, dim)
```

**Timestep embedding** — `:238`. Sinusoidal, `flip_sin_to_cos=True` so it is
`concat(cos, sin)`, then `Linear(320,1280) -> SiLU -> Linear(1280,1280)`.
`label_emb` is the same shape from 2816.

### 1.4 Shapes and dtypes, per layer class, at 1024x1024

| layer | shape | dtype | native layout wanted |
|---|---|---|---|
| activation, conv path | `[H][W][C]`, 128x128x320 / 64x64x640 / 32x32x1280 | fp16 | `ConvEntry` = `[C/32][plane][32]` |
| conv weight | `[N][9C]` = 320x2880 / 640x5760 / 1280x11520 | fp16 or pre-quantised MXFP7 | `Entry(gn, 1)` |
| activation, token path | `[B, H*W, C]` = 4096x640 / 1024x1280 | fp16 | `Entry(gm, nk)` for a fill, `Flat` for a reduction, `Tile` after a drain |
| `Linear` weight | `[out][in]`, torch order | fp16 / MXFP7 | `Entry(gn, nk)` |
| attention q, k | `[B*heads, L, 64]` | fp16 | `Entry` |
| attention v | **`[B*heads, 64, L]`** — transposed | fp16 | `Entry` |
| attention scores | `[L, 64]` per key block | fp16 | `Tile` from the GEMM, `Flat` for the softmax |
| context | `[B, 77, 2048]` | fp16 | `Entry`, padded to 128 keys |
| GroupNorm operand | `[N*32, C/32 * H * W]` | fp16 | `Flat` |
| timestep embedding | `[B, 320]` then `[B, 1280]` | fp16 | `Flat` / `Entry` |

The transposed `v` is not a preference: `kernels.flash_attention` declares
`v = L.In(..., Dv, Lkv)`, SOURCE
`compiler/kohakutpu/kernels/attention.py:139`.

### 1.5 The VAE decoder

`ch=128`, `ch_mult=[1,2,4,4]`, `num_res_blocks=2`, `attn_resolutions=[]`,
`z_channels=4`, scale factor `0.13025`.

```
   z * (1/0.13025) -> Conv1x1(4,4) post_quant -> Conv3x3(4, 512)   at 128x128
   mid:  Resnet(512) -> AttnBlock(512) -> Resnet(512)              at 128x128
   up[3]: 3 x Resnet(512->512), Upsample(nearest2x + Conv3x3)      128 -> 256
   up[2]: 3 x Resnet(512->512), Upsample                           256 -> 512
   up[1]: 3 x Resnet(512->256), Upsample                           512 -> 1024
   up[0]: 3 x Resnet(256->128)                                     at 1024x1024
   GroupNorm(32,128) -> SiLU -> Conv3x3(128, 3)
```

The VAE's `ResnetBlock` is the same `norm -> swish -> conv` shape as the UNet's,
with `temb_channels=0` so no embedding add.

**`AttnBlock` is the heaviest single operator in the whole pipeline.** It is
spatial self-attention over `H*W = 16,384` tokens at `d = 512`, **one head**, with
q/k/v/proj as 1x1 convolutions. The score matrix is `16,384 x 16,384`.

### 1.6 What stays on the host

Tokenisation, the sinusoidal embedding table, the sampler (Euler/DPM++: a few
elementwise ops on a `128x128x4` latent per step), CFG combination, and the
final `[-1,1] -> uint8` conversion. None of it is worth a kernel; all of it is
a few hundred KB per step.

---

## 2. The arithmetic

### 2.1 Multiply-accumulate, per image at 1024x1024

ARITHMETIC. Convolution `MAC = H_out * W_out * Cin * Cout * 9`; linear
`MAC = rows * in * out`; attention scores `MAC = B*heads * Lq * Lkv * D` and the
same again for the weighted sum.

| block | MAC per UNet forward |
|---|---|
| 3x3 and 1x1 convolutions | 8.11e11 |
| level 1 transformer (4 blocks, 4096 tokens, d=640) | 2.16e11 |
| level 2 transformer (20 blocks, 1024 tokens, d=1280) | 6.77e11 |
| mid transformer (10 blocks) | 3.38e11 |
| output level 2 transformer (30 blocks) | 1.01e12 |
| output level 1 transformer (6 blocks) | 3.24e11 |
| **UNet total** | **3.38e12** |

| stage | MAC, whole image |
|---|---|
| UNet, 30 steps x CFG 2 | 2.03e14 |
| text encoders, 2 prompts | 1.1e11 |
| VAE decoder | 5.24e12 |
| **total** | **2.08e14 = 208 TMAC** |

The VAE decoder is **1.55x one UNet forward** and it is spent almost entirely in
the last two levels, where the plane is 512x512 and 1024x1024.

### 2.2 Against this machine

Peak, **ARITHMETIC** from [results.md](results.md) §8's measured 512 MAC/cycle
per cluster and the v7 population of 8+2 / 6+2 / 8+2 / 8+2, which is 30 matmul
clusters and 8 vector cores:

```
   30 clusters x 512 MAC/cycle = 15,360 MAC/cycle
   at a matmul clock of 400 MHz  6.14e12 MAC/s = 12.3 TFLOP/s
   at a matmul clock of 300 MHz  4.61e12 MAC/s =  9.2 TFLOP/s
```

> **400 MHz is the ceiling this silicon has been measured to, not the one the
> board profile asks for.** The `ship` profile requests 600 MHz on the matmul
> domain; laddered on the card, that domain is bit-identical to 400, degrades at
> 450 and dies at 700, and at 600 the error is 157% ([results.md](results.md)
> §9.5). **A peak quoted at 600 MHz describes a machine that does not exist**,
> so the rows below stop at 400. The card currently runs its meshes at 100–150
> MHz ([multi-mesh.md](multi-mesh.md) §8.1), which is another factor again.

| | at 400 MHz | at 300 MHz |
|---|---|---|
| 208 TMAC at 100% of peak — an unreachable bound, stated as one | 33.9 s | 45.1 s |
| at 75.5%, the best MEASURED large-GEMM efficiency ([results.md](results.md) §8.2) | 44.8 s | 59.7 s |
| VAE decode alone, at 100% | 0.85 s | 1.14 s |

Nothing in §2 is the problem. §4 is the problem — and it is the problem by a
margin that no clock choice in this table would close.

### 2.3 Parameters and footprint

ARITHMETIC, summing the module tree. MXFP7 in memory is 7 bits per element plus
one 8-bit E5M3 scale per 32, so `(32*7 + 8)/32 = 7.25` bits = 0.906 B/element.

| | parameters | fp16 | MXFP7 |
|---|---|---|---|
| UNet | 2.56 e9 | 5.13 GB | 2.32 GB |
| CLIP-L | 1.23 e8 | 0.25 GB | 0.11 GB |
| OpenCLIP bigG | 6.94 e8 | 1.39 GB | 0.63 GB |
| VAE decoder | 4.95 e7 | 0.10 GB | 0.04 GB |
| **total** | **3.43 e9** | **6.87 GB** | **3.10 GB** |

**One mesh has 4 GB of DDR4** (`docs/address-map.md` §Capacity). So:

- **fp16 UNet weights do not fit one mesh** — 5.13 GB against 4 GB, before a
  single activation.
- **MXFP7 UNet weights do** — 2.32 GB, leaving 1.7 GB for activations, which is
  ample (§2.4).
- The whole pipeline in MXFP7 is 3.10 GB and fits one mesh; in fp16 it needs two.

That makes **weights held in MXFP7** a capacity requirement here, not only the
throughput win `results.md` §8.2 measured. "Pre-quantised" is no longer a
distinction: a fetch is never transformed, so whatever format a weight is in
memory is the format the cluster reads. The choice is which format to WRITE it
in — the host converts, or a mover pass through the transform slot does — and at
these sizes there is no choice at all.

### 2.4 The activation working set

| tensor | elements | fp16 |
|---|---|---|
| UNet level 0 activation 128x128x320 | 5.24e6 | 10.5 MB |
| the 9 skip tensors, summed | 2.56e7 | 51 MB |
| VAE 1024x1024x128 activation | 1.34e8 | **268 MB** |
| VAE 512x512x256 | 6.71e7 | 134 MB |
| level 1 self-attention scores, 10 heads x 4096x4096 | 1.68e8 | **335 MB** |
| VAE mid attention scores, 16384x16384 | 2.68e8 | **537 MB** |

Two of those must never be materialised, and `flash_attention` is the reason
they need not be. The VAE's 268 MB activation is a DRAM tensor and nothing else;
no staging store on this machine is within two orders of magnitude of it.

---

## 3. The operator inventory against what is built

Status is against `compiler/kohakutpu/{ops,kernels}` and the v7 bitstream.
"partial" means it exists and refuses at SDXL's shapes.

| # | requirement | status | what exists | what is missing |
|---|---|---|---|---|
| 1 | GEMM `x @ w.T`, w in torch `[out][in]` order | **supported** | `ops.matmul`, `M=4a N=4b K=32c` — every SDXL shape satisfies it | — |
| 2 | GEMM + per-channel bias | **partial** | `kernels.linear_bias`, rides the resident tile via `layout.ChannelBias` | fused-only. A grid wider than the vector cores is refused, not staged (`hardware-wants.md` §7) |
| 3 | GEMM + full-shape residual | **supported** | `kernels.linear_add` | — |
| 4 | GEMM + SiLU / GELU / ReLU | **supported** | `kernels.linear_silu`, `_gelu`, `_relu`; MEASURED 0 relayouts | — |
| 5 | 3x3 conv, stride 1, pad 1 | **supported** | `ops.conv2d`, branch C. MEASURED at 16x16x32 -> 32: 1 dispatch, 0 relayouts, peak-rel max 1.7e-2, identical to a matmul | `nk` forced to 1, so a pass is `3*(9C/32)+1` flits |
| 6 | 3x3 conv + bias | **partial** | `kernels.conv2d_bias` | bias must arrive at FULL shape; the per-channel form is written in the file as a comment and not built |
| 7 | **3x3 conv, stride 2** | **supported** | `ops.conv2d_stride2` + `layout.ConvEntry(step=)`, the residue packer `conv2d.md` §4.1 derives. MEASURED at three shapes: 1 dispatch, 0 relayouts, error identical to a stride-1 conv, and **4.00x / 4.00x / 3.67x fewer cycles** than dense-and-discard | the packer's field cannot be called `stride`: `lang/backend.py:_held` reads that name off a layout as a batch BYTE stride and sizes the whole activation at ONE element. It is `step` |
| 8 | 1x1 conv | **supported** | identity-equivalent to a GEMM in the `ConvEntry` layout (`conv2d.md` §4) | — |
| 9 | LayerNorm, D <= 128 | **supported** | `kernels.layernorm` (fused), 1 stage | — |
| 10 | **LayerNorm, D = 640 or 1280** | **supported** | `kernels.layernorm_wide` through the SPLIT fold (§5.4). MEASURED at m=16: D=640 p50 3.73e-5 / p99 3.80e-4 / max 8.99e-4; D=1280 p50 3.37e-5 / p99 3.45e-4 / max 7.28e-4. Nothing past 1%, 0 relayouts, 0 saturated | — |
| 11 | softmax, N a power of two x 128 | **supported** | MEASURED OK at 1024 and 4096 | — |
| 12 | softmax, N = 1280 / 77 / 16384 | **supported** | 1280 and 16,384 through `softmax_wide` + `part_for`; 77 through `ops.softmax_keys` and `api.softmax(keys=)`, with the padded columns exactly 0 and rows summing to 1 within 1e-4 | the 77 mask is a table at the row's FULL shape, because `Buffer.repeated` serves one grid instance — §5.4 |
| 13 | GroupNorm, group <= 128 | **supported** | `kernels.group_norm` | irrelevant — no SDXL group is that small |
| 14 | **GroupNorm at 40,960 / 81,920 / 163,840** | **supported** | `kernels.group_norm_wide` and `group_norm_silu_wide`, EVERY GROUP AT ONCE, plus `api.group_norm` / `api.group_norm_silu`. MEASURED max 1.17e-3 / 8.68e-4 / 8.25e-4, nothing past 1%, 0 saturated, 0 relayouts; at a mean 64 sigma off zero max 2.35e-3 | `w` and `b` arrive at FULL shape: a per-channel gain over a plane would be a spread taking ONE element of every `H*W`, and a spread's sub-row is whole 16-element words |
| 15 | SiLU, GELU (both forms), sigmoid | **supported** | `ops.silu`, `ops.gelu`, `kernels.gelu_tanh`. MEASURED in the demo: silu 3.4e-4, gelu 2.4e-4 | — |
| 16 | GEGLU | **supported** | `kernels.geglu` (two GEMMs sharing the `x` fill, MEASURED 0 relayouts) and `demos/kohakutpu/sdxl/kernels.py:geglu_fused` | the chunked form still needs a host slice; the two-GEMM form does not, and is bias-free |
| 17 | flash attention, self | **partial** | `kernels.flash_attention` — correct, MEASURED demo grade 1.3e-2, and its conversions now RUN ON CARD | `6*blocks - 3` conversions, **59-80% of the call in cycles**, of which `3*blocks - 3` are DEAD. §5.2a locates every one and names its level |
| 18 | flash attention, cross (Lkv=77) | **partial** | the `keys=` argument pads to a whole block | the same conversion bill as row 17 |
| 19 | residual add, elementwise | **supported** | `ops.residual` and every `linear_add` | — |
| 20 | **head split / join permute** | **DELETED, not implemented** | `kernels.heads_of` is a RESHAPE of the checkpoint weight; `project_heads` puts the batch on the weight; `attn_out` sweeps the heads as K-chunks of ONE accumulator. MEASURED: 4 host permutes per attention → **0**, at x1.00 cycles and x1.00 flits — §5.2 | — |
| 21 | **NCHW <-> tokens permute** | **missing on device** | — | 22 per forward, one pair per `Transformer2DModel` |
| 22 | **transpose for attention `v`** | **DELETED, not implemented** | `ops.matmul(heads_of(wv), source)` — the SHIPPED kernel with the weight as the batch and the operands swapped, giving `[heads][D][L]` COMPUTED rather than moved. No new kernel at all | — |
| 23 | reshape (no element move) | **supported** | `Buffer.as_rows`, `Reshaped` | only within a layout; a reshape that changes the fold across layouts is a relayout |
| 24 | broadcast / per-channel read | **supported** | `Buffer.repeated`, `per_group`, `Spread` — stride-0 in `vec_agu`, no pass at all | the only address-dependent operand the DSL has |
| 25 | **slice / chunk** | **free on the channel axis** | MEASURED byte-identical at all five real SDXL shapes: a slice of the low channel blocks IS the leading prefix of a `ConvEntry` buffer | a slice of the last axis of a `Tile`-ordered result is still strided. GEGLU needs none — the two-GEMM `kernels.geglu` never chunks |
| 26 | **channel concat** | **free, needs an allocator** | MEASURED byte-identical at all five real skip shapes: in `[C/32][plane][32]` the concatenation IS the two operands in order, provided the first is written without its tail | the allocator has to be able to say ADJACENT — one span, two halves. `kohakuaccel/lifetime.py`, §5.8 |
| 27 | **nearest-2x upsample** | **supported** | `ops.conv2d_upsample2` + `weights_for_upsample2`: each output residue class is a 2x2 conv on the ORIGINAL plane, weights folded once at load. MEASURED 0 relayouts, error identical to a plain conv, **2.15x / 2.00x fewer cycles**, and the 4x activation never written | 1.08x only at 8x8, where the pad ring is the layer |
| 28 | sinusoidal timestep embedding | **partial** | `L.exp2`, `L.table` exist | no sin/cos in the ALU. §5.9 |
| 29 | cross-mesh split of a layer | **partial** | `kohakutpu.meshes`, MEASURED 2026-08-13 at 3.98x compute on 4 meshes, wall time worse either way — all transport | — |
| 30 | **MAG L2 staging (2 MB/mesh)** | **built, unreachable** | `mag_stage.v` in the v7 top: 4 banks x 16,384 entries | no compiler or driver file names it. `machinespec.SPECIAL_BIT` is declared and **never used**, and `global_addr` raises for any base with bit 36 or above set — so the compiler cannot form an L2 address at all |
| 31 | **NoC L2 adapter (256 KB x 10/mesh)** | **built, unreachable and narrower than it looks** | `noc_l2_adapter.v` on all 8 clusters and 2 vector cores | `l2_en` is **0 at reset** and nothing writes the `CU_CTRL` window. And it serves only the endpoint behind it and refuses multicast — see §4.4 |
| 32 | **memory mover TRANSPOSE** | **not needed** | `COPY` with the two walkers in different orders | mode 1 is allocated and **faults if requested** (`docs/spec/control-registers.md` §3), and it is a canned convenience rather than a capability: every index permutation is affine, so six loop levels a side express it. What the engine genuinely cannot do is anything finer than a 32-byte word. See H8 for who may command it |

---

## 4. The layout problem, which is the actual blocker

### 4.1 What a relayout is here

A buffer on this card exists in exactly one byte order, and the orders are not
interchangeable:

| layout | granule | who wants it |
|---|---|---|
| `Entry(groups, blocks)` | 4 lanes x 32 K elements = 256 B | a cluster **FILL** |
| `Tile(grid, gm, gn)` | one 4x4 sub-tile = one 32 B word | a cluster **DRAIN** |
| `Flat` | row-major fp16 | any vector-core row reduction |
| `ConvEntry` | `[C/32][plane][32]` | a convolution fill |
| `ChannelBias(gn)` | one word per column group | a fused per-channel epilogue |

MEASURED: `Tile((2,8),8,8)` and `Entry(8,2)` over a 64x256 array are 32,768 bytes
each and **16,192 of 16,384 elements land in different places**. They are not
the same order under any reading.

`lang/backend.py:_conversions` computes every order change a kernel implies and
records it on `Compiled.conversions`. **Nothing executes that list.** The only
mechanism that performs a relayout is `rt.Tensor.address`, which reads the
tensor back to the host, repacks it in numpy, and uploads it again — counted in
`Device.counters["relayouts"]`.

### 4.2 The measured bill

MEASURED on the unit models:

| kernel | shape | dispatches | relayouts |
|---|---|---|---|
| `linear_silu` | 64x128x64 | 2 | **0** |
| `geglu` | 64x128x64 | 4 | **0** |
| `conv2d` | 16x16x32 -> 32 | 1 | **0** |
| `mlp` | 64x128x256x64 | 3 | **1** |
| `flash_attention` | 4 heads, L=64, D=64 | 6 | **3** |
| `flash_attention` | 4 heads, L=128 | 10 | **9** |
| `flash_attention` | 4 heads, L=256 | 18 | **21** |

`3, 9, 21` against `1, 2, 4` key blocks is `6 * blocks - 3`: **six host round
trips per key block.**

The three conversions per key block, SOURCE `flash_attention.plan(...).conversions`
at L=128:

```
   t4  scores    tile:4x2:8x8  ->  flat            the softmax band reads rows
   t5  weights   flat          ->  entry:8x2       the p@v GEMM fills it
   t6  part_o    tile:4x2:8x8  ->  flat            the accumulate band reads rows
```

and the same three in reverse on the next block. **Every cluster-to-vector-core
handoff through memory is a layout change**, and there is exactly one path that
avoids it — the fused epilogue, which drains the tile straight into a vector
core's L1 over the NoC and never lands in memory (`memory.md` §1). That path is
limited to a grid no wider than the vector cores and to an epilogue reading the
resident tile plus at most one per-channel operand.

**One whole `BasicTransformerBlock`, MEASURED** at `DIM=256, HEADS=4,
TOKENS=64, CTX=256` — 2.5 MB of parameters:

```
   dispatches   79
   rounds      377
   flits    38,412
   sent     12.5 MB      to the card
   fetched   9.05 MB     back from it
   relayouts    24       compiler-level, each a host round trip
   host permutes 9       head split x6, head join x2, geglu chunk x1
```

**33 host round trips, and 21.6 MB of link traffic for 2.5 MB of weights.**

### 4.3 The same bill at SDXL's shape

ARITHMETIC, applying `6 * blocks - 3` and the temp sizes the kernel declares
(`span x block` for `scores`, `weights` and `part_o`, with `span = Lq` when
`qblock` is unset):

Each conversion moves the buffer down and back, so it costs `2 x` the temp;
there are three temps per key block.

| attention | B*heads | Lq = Lkv | key blocks | one temp, all heads | host bytes per call |
|---|---|---|---|---|---|
| level 1 self | 10 | 4096 | 64 | 5.24 MB | **2.01 GB** |
| level 2 self | 20 | 1024 | 16 | 2.62 MB | 252 MB |
| level 1 cross | 10 | 4096 / 128 | 2 | 5.24 MB | 62.9 MB |
| level 2 cross | 20 | 1024 / 128 | 2 | 2.62 MB | 31.5 MB |

Per UNet forward: `10 x 2.01 + 60 x 0.252 + 10 x 0.063 + 60 x 0.032` **= 37.7 GB**.
Per image at 30 steps and CFG: **2.3 TB through the host link.**

This is not a performance figure to improve. It is the statement that
`flash_attention` as written cannot run SDXL at any clock, on any transport,
and that **the layout conversion — not arithmetic, not bandwidth, not the ISA —
is the thing standing between this machine and a modern network.**

### 4.4 Why the L2 that is on the card does not currently help

Both stores are in the v7 ship top, SOURCE
`src/kohakutpu/top/generated/ktpu_ship_2x2_8c2v_1m.v`:

| | where | size | reached by | state |
|---|---|---|---|---|
| MAG staging | `mag_stage.v` on the converged path inside the memory agent | 4 banks x 16,384 entries x 1,024 b = **2 MB per mesh** | **address**: `addr[39]=1`, `addr[35:32]=0`, mesh in `[37:36]` | built, no software |
| NoC L2 adapter | `noc_l2_adapter.v` spliced into each local link | 8,192 lines x 256 b = **256 KB**, x8 clusters + x2 vector cores = 2.5 MB per mesh | **address** in a window programmed over `CU_CTRL` | built, **disabled at reset**, no software |

Five facts from the RTL that decide what each is good for:

1. **The MAG store is mesh-wide and shared.** It sits behind the DRAM port's
   arbiter (`src/kohakuaccel/sysnode/core/mag_dram_port.v`), the converged
   path where every requester meets — compute units, the mover, the
   interlink, and the host through `S_AXI_MEM`; a foreign mesh's address is
   not claimed and passes through, which is what lets mesh 0 reach mesh 3's
   L2. **This is the one store on the card that two different units can hand
   a tensor through.**
2. **Only its narrow port is wired.** `src/kohakuaccel/sysnode/core/mag.v`
   ties the store's entry-granular 1,024-bit port A off — the whole subject
   of `notes/cache/mag-staging.md` §2 — and all traffic goes through the
   256-bit port B from the DRAM port, one claimed burst at a time. So the
   store's advantage is **latency and DRAM-traffic relief, not width**; it is
   the same 256 bits per MAG clock the DRAM path is.
3. **The NoC adapter can only serve the unit behind it.** It snoops its own
   endpoint's outbound flits (`eu_*`), so it is a private scratchpad. It cannot
   move data between clusters, and `notes/cache/noc-staging.md` already records
   this as the limit form 2 turned out to have.
4. **The NoC adapter refuses a multicast fill.**
   `wire i_mine = i_fits && (i_nd == 2'd0)` — a fill asking for extra
   destinations is forwarded to MAG whatever the address, because this adapter
   can only answer the node behind it. It no longer looks at `flags[4]`: a fetch
   is never transformed, so that bit is reserved and ignored and refusing on it
   would have given a different answer from MAG for the same request.
5. **Neither is addressable from the compiler.** `machinespec.SPECIAL_BIT`
   (`1 << 39`) is declared and referenced nowhere, and `global_addr` raises for
   any base that does not fit one mesh's 64 GB — so no arena allocation can ever
   carry bit 39. The adapters' `l2_en` is 0 at reset and nothing in `driver/`
   writes their `CU_CTRL` window.

**Conclusion for the transpose question the brief asks about:** a transpose that
must not round-trip through DRAM has to stage in the **MAG store**, because it
is the only one both the producing and the consuming unit can address. The
per-unit adapters are the wrong shape for it and always will be.

---

## 5. Kernel designs for the gaps

### 5.1 The enabling change: perform a relayout on the card

**Level: compiler.** No RTL, no ISA.

Everything needed already exists and is unwired:

| piece | where | state |
|---|---|---|
| the conversion list | `lang/backend.py:_conversions` | computed, recorded, **never executed** |
| a 4-dimension strided walk on VFILL and VDRAIN | `vec_agu`, `hw/vector.py:dim(stride, bound)` | shipped; signed 18-bit stride, 16-bit bound |
| the Flat -> Entry dims, and the refusal when they do not fit | `isa/vecemit.py:entry_walk` | written and unit-tested, **called by nothing but its test** |
| a kernel that takes a strided drain | `isa/vecemit.py:ElementwiseKernel(..., drain=dims)` | implemented, **used only by `compiler/tests/test_layout.py`** |

So the design is: emit a **relayout stage** for each entry of
`Compiled.conversions`, as a vector-core program that fills contiguously and
drains strided (or the reverse).

```python
# what the compiler should emit for (at, name, Tile(...), Entry(g, b)):
#
#   VSETVL / VSETMODE FLAT
#   VFILL  ad_in   <- contiguous run of `words` 32-byte words at src
#   VBAR
#   for each chunk: VLD v0, ad_ld ; VST v0, ad_st          identity chain
#   VDRAIN ad_out  -> STRIDED walk, dims from entry_walk(...)
#   VHALT
```

Bounds, SOURCE from the RTL constants:

| bound | value | consequence |
|---|---|---|
| `vec_agu` dimensions | 4 | a permutation needing 5 must be split into two passes |
| `vec_core` F_LEN | 256 entries per VFILL/VDRAIN | one pass moves at most **8 KB** |
| `L1_SAFE` | 320 words, or exactly 512 | the pass's footprint is `2 x words`; 256-word in + 256-word out is 512, which is the legal upper point |
| `entry_walk` returns None | over 4 dims or over 256 words | the compiler must tile, not truncate — `test_layout.py` pins this |

**A 64x64 fp16 tile is exactly 8 KB = 256 words = one pass.** That is not a
coincidence worth relying on twice, but it means the attention temps
(`span x block` at `block = 64`) tile into whole passes with no remainder.

**Cost.** A relayout pass is one vector-core dispatch moving 8 KB. At the
`flash_attention` bill in §4.3, level-1 self-attention's 192 conversions become
192 x (5.12 MB / 8 KB) = 123,000 passes — which is worse than useless. So §5.1
alone is not enough; it must be combined with §5.2 and §5.3, which remove most
of the conversions rather than making them cheaper.

**What to build first, and how to prove it:** wire one conversion — `mlp`'s
single `t1: Tile -> Entry`, MEASURED at 1 relayout today — and check that the
counter goes to 0 with the result unchanged at the MEASURED peak-relative
p50 3.7e-3 / max 2.2e-2. That is a one-kernel, one-counter, one-number test.

### 5.2 The head split is an N-partition, and costs nothing

**Level: kernel. BUILT and MEASURED.** `kernels.heads_of`, `kernels.project_heads`
and `kernels.attn_out`; `compiler/tests/test_head_split.py`.

`_heads` in the demo permutes `[B, L, heads*64] -> [B*heads, L, 64]` on the host,
MEASURED at 8 permutes per transformer block. It does not need to exist.

`to_q` is `x @ Wq.T` with `Wq` of shape `[heads*64][ctx]`. Output column `j`
belongs to head `j // 64`. **The head axis is the N axis of the projection**, and
N is already the grid axis the compiler tiles on — so the split is a RESHAPE of
the checkpoint array, and the batch axis rides the WEIGHT:

```python
# `heads_of` is a reshape, not a slice: the head axis is already outermost.
#     wq = heads_of(Wq, heads)               # [heads][64][ctx], same bytes
#     q  = project_heads(x, wq)              # [heads][L][64], the attention shape
```

MEASURED: `np.array_equal` with the wide projection followed by a host permute.

**It costs NOTHING**, and the reason is a property of the head width rather than
of the split. A lane group is FOUR elements, so `gn = 8` covers 32 columns and a
64-wide head is TWO whole N tiles — the tiling never changes and nothing is
recomputed. A head width that was *not* a whole number of N tiles would force
`gn = 4` and the penalty in the third row below. MEASURED at
`256x1280 -> 20 heads x 64`, and the same at `1024x640 -> 10x64` and
`256x2048 -> 20x64`:

| | intensity `2·gm·gn/(gm+gn)` | cycles | flits |
|---|---|---|---|
| one wide projection, `gm=8 gn=8` | 8.00 | 418,176 | 19,520 |
| per-head projection, `gm=8 gn=8` | 8.00 | **418,176** | **19,520** |
| per-head projection, `gm=8 gn=4` | 5.33 | 551,264 (x1.32) | 39,040 (x2.00) |
| per-head projection, `gm=8 gn=2` | 3.20 | 825,184 (x1.97) | 78,080 (x4.00) |

There is no trade to make. `gn = 4` is a 1.32x cycle and **2.00x flit** penalty
that nothing about a 64-wide head asks for, and with the clock coming down the
flits column is the one that matters.

The **join** disappears the same way: `to_out` contracts over `heads*64` and that
axis is head-major in the checkpoint too, so the weight's chunk index IS the
sweep step and neither operand is sliced. `kernels.attn_out` sweeps `(head,
K-chunk)` on ONE loop counter — a GEMM chains on the innermost counter and 0
clears the tile, so two loops would keep only the last head — with the head half
of the step rebound the way `Tap` rebinds a convolution's.

MEASURED at 1, 2, 3 and 8 K-chunks per head, and priced against the two passes it
replaces:

| | cycles |
|---|---|
| `128x256 h=4`: join matmul 9,600 + residual 3,584 | 13,184 |
| `128x256 h=4`: `attn_out` | **13,184** (x1.000) |
| `256x1280 h=20`: join matmul 418,176 + residual 35,840 | 454,016 |
| `256x1280 h=20`: `attn_out` | **454,016** (x1.000) |

The residual is not optional in `attn_out` and is not a compromise: `Lq` is `o`'s
rows over `heads`, which the extent solver cannot divide, so the result's own
length has to be read off an operand — and SDXL adds one here every time.

**Requirement 20 and 22 both go away.** `v` transposed to `[D][L]` needs no new
kernel at all: `to_v` produces `[L][heads*64]` and the kernel wants `[64][L]` per
head, which is `Wv_h @ source.T` — the SHIPPED `ops.matmul` with the weight as
the batch and the operands swapped. Nothing is transposed at runtime.

**What the head split does NOT do.** MEASURED: it deletes 4 host permutes per
attention and **zero cycles of conversion**. `flash_attention`'s conversion cost
is identical before and after it, to the cycle — 116,610 at `4x128`, 541,114 at
`4x256`, 2,693,306 at `20x256` — because every one of those conversions is INSIDE
the kernel and the projections carry none in either shape (§5.2a). The permutes
this section deletes are host round trips, which are not cycles on this machine
at all; what they cost is link traffic, and §4.3 prices that.

### 5.2a Where attention's cycles actually are

**MEASURED with `cost.relayouts`, which prices one stage per conversion.** A
conversion is 59-80% of a `flash_attention` call, and it is `6*blocks - 3` of
them — three cluster-to-vector-core handoffs per key block, and three back.

| heads | Lq | Lkv | blocks | listed | RUN | conversion cycles | `cost.time` | share |
|---|---|---|---|---|---|---|---|---|
| 4 | 128 | 128 | 2 | 9 | 6 | 77,740 | 157,612 | 49.3% |
| 4 | 128 | 256 | 4 | 21 | 12 | 155,480 | 298,328 | 52.1% |
| 4 | 256 | 256 | 4 | 21 | 12 | 309,208 | 507,352 | 60.9% |
| 20 | 256 | 256 | 4 | 21 | 12 | 1,539,032 | 2,526,168 | 60.9% |
| 10 | 512 | 512 | 8 | 45 | 24 | 3,078,064 | 4,547,504 | 67.7% |

**`compiled.conversions` LISTS `6*blocks - 3` and the runtime RUNS `3*blocks`.**
The other `3*blocks - 3` rewrote a buffer the very next stage overwrote wholesale
and never read — the temps are reused across key blocks, so after block `j` read
`scores` as `flat`, block `j+1`'s DRAIN wrote the whole buffer as `tile` and the
old order was already dead. They are no longer executed or charged. **The list is
therefore no longer the authority: anything that zips `compiled.conversions`
against `cost.relayouts` misaligns after the first drop**, and the resulting
mislabelling is silent — a conversion's cost gets attributed to the wrong pair
of layouts, which reads as a plausible number rather than as an error.

**What is left is two granule transposes a key block, and they are 99.6% of it.**
Priced from each conversion's own layouts at `4x256`:

| temp | conversion | each | live | share of the conversion cost |
|---|---|---|---|---|
| `scores` | `tile <-> flat` | 38,511 | 4 | 154,044 — **49.8%** |
| `part_o` | `tile <-> flat` | 38,511 | 4 | 154,044 — **49.8%** |
| `weights` | `flat <-> entry` | 280 | 4 | 1,120 — 0.4% |

**The expense is not "a conversion", it is "a conversion that touches Tile."**
MEASURED over `(1024, 64)`, which is what these temps are:

| pair | destination words whole in the source | kind | cycles | `route_for` |
|---|---|---|---|---|
| `flat <-> entry` | **4096 of 4096** | word | **280** | None |
| `tile <-> flat` | **0 of 4096** | granule | **38,511** | None |
| `tile <-> entry` | **0 of 4096** | granule | **38,511** | None |

**137x**, and `route_for` returns None for both Tile pairs — no staging route
beats doing it in place, because the cost is ALU work and not movement.

**"Attention is vector-bound" and "attention is relayout-bound" are NOT the same
fact, and they converge at real sizes.** The standing figure — `Lq=64 Lkv=256`,
one head, 45,056-47,104 cycles at MG 3,584 against VC 43,520, a 92% vector share
— was recorded as a property of the workload. It no longer reproduces: the same
shape now prices at 85,224, because **the cost model of the day charged NOTHING
for a conversion.** `cost.relayouts`' own docstring says so: *"a conversion is
NOT a statement, so `time_of` cannot see it"*. So that baseline cannot have been
evidence about the transpose in either direction.

Splitting `cost.time` by unit, with conversion stages separated by `by_kind`:

| shape | total | MG | VC | of VC: granule transpose | of VC: the rest |
|---|---|---|---|---|---|
| h=1 Lq=64 Lkv=256 | 85,224 | 4.2% | **95.8%** | 20,968 — 25.7% | 60,672 — 74.3% |
| h=1 Lq=256 Lkv=256 | 210,712 | 5.1% | **94.9%** | 78,616 — 39.3% | 121,344 — 60.7% |
| h=4 Lq=256 Lkv=256 | 507,352 | 7.8% | **92.2%** | 309,208 — **66.1%** | 158,720 — 33.9% |
| h=20 Lq=256 Lkv=256 | 2,526,168 | 7.7% | **92.3%** | 1,539,032 — **66.0%** | 793,600 — 34.0% |

**Vector-bound holds at every shape, 92-96%.** The transpose's share of the
vector side is a quarter at the standing shape and **two thirds at any real one**,
because the granule cost is per-WORD ALU work while the arithmetic is per-RUN, so
more words tilt it. At SDXL's sizes **~61% of the whole call is the transpose**.

The consequence for the baseline: "the number any attention rewrite must beat" is
mostly a layout conversion, and a rewrite of the softmax chain can only address
the other 34%. Compare per unit AND with the conversion stages split out, or the
comparison measures the wrong term.

### 5.2b Can a flash kernel be laid out with NO transpose? No, and here is why

**PROVED NOT POSSIBLE, and the obstruction is 2-D.** The lead was that softmax
reduces over KEYS while VRED folds CONTIGUOUS lanes, so a `(B, L, H, D)`
assignment where the reduction axis is the contiguous one would delete the
conversion. **There is no such assignment, for any tiling.**

A `Tile` word IS a 4x4 sub-tile. MEASURED, word 0 of a packed 32x32 index image:

    element indices [0,1,2,3, 32,33,34,35, 64,65,66,67, 96,97,98,99]
    which is rows [0,1,2,3] and columns [0,1,2,3]

So in `Tile` order the longest CONTIGUOUS run is **4 elements along a row and 1
along a column — for all sixteen tilings**, `gm` and `gn` each in `{1,2,4,8}`.
`Flat` and `Entry` run the full row. VRED folds a multiple of 16 at most 128, of
ONE row. Four is never sixteen.

That is why choosing among B, L, H and D cannot reach it: those decide only which
axis is M and which is N, while the sub-tile breaks **both** axes at 4. A 1-D
choice cannot answer a 2-D obstruction. Head dim being 64 at every SDXL level
buys nothing here.

**And the compiler already exploits the only freedom that does exist.**
Elementwise work is order-agnostic — the same permutation of every operand and of
the result gives the same permutation of the answer — and the compiler knows:

| a GEMM followed by | conversions | cycles | the drained temp ends in |
|---|---|---|---|
| one ELEMENTWISE pass | **0** | 4,928 | `tile:8x2:8x8`, and so do the other operands |
| one REDUCTION | 1 | 14,199 | `flat`, at 9,783 cycles |

**Conversions are caused by reductions, and only by reductions.** So the floor
follows directly:

- `scores -> flat` is forced by the softmax reduction. **Irreducible.**
- `weights -> entry` is 0.4%. Not worth an argument.
- `part_o -> flat` is **NOT forced by a reduction.** It is dragged there because
  it is read in the same elementwise pass as `corr`, and `corr` is an output of
  the `flat` softmax band.

So the floor looks like ONE crossing a key block and we pay TWO — and closing it
means `acc_o`'s chain never meeting a `flat` operand, which means no per-block
`corr`. **That cannot be rearranged away.** Writing the accumulation out,
`acc_o_final = sum_j part_o_j * prod_{k>j} corr_k`, and pre-scaling `weights_j`
by `1/s_j` with `s_j = prod_{k<=j} corr_k` removes the per-block correction — but
`s_j = exp2(top_0 - top_j)` TELESCOPES, so that pre-scale is algebraically
identical to never subtracting a running max at all. The rescale IS the numerical
safety; it can be given up, not reorganised.

**Therefore: two granule transposes per key block is the floor for online-softmax
flash attention on this machine, and `flash_attention` is already at it.** Below
two means a fixed max, which is the thing flash exists to avoid.

What would move the floor, by level:

| change | level | effect |
|---|---|---|
| a drain that writes row-major, or an L1 read granule matching the 4x4 drain | **RTL** | the root: MG's output granule is the whole obstruction |
| the C6a full-shape epilogue operand | compiler + ISA | does NOT help here — it would let `part_o` fuse, but `corr` still crosses at the same size |
| a fixed-max softmax | kernel | one crossing a block instead of two, for the numerical safety flash exists for |

The MAG mover cannot help with any of it: it is word-granular (`lt_ma` faults a
non-32-byte-aligned destination) and the two orders disagree BELOW the word. It
is the right engine for `flat <-> entry`, which is already 0.4% of the bill.

The three per block are real handoffs, and each is blocked at a different level.
Named here so nobody re-derives them:

| handoff | why it lands in memory | level |
|---|---|---|
| `scores` `tile -> flat` | the softmax band needs `row_max`/`row_sum` over the drained tile, and a resident tile is 4x4 sub-tile order in L1, so a row of `gn*4` columns is spread across `gn` words while VRED folds contiguous lanes | **ISA / drain order** |
| `weights` `flat -> entry` | a vector core writes it and a cluster fills it, and VC -> cluster L1 is shut because a vector core cannot emit MXFP7 | **RTL** (H9) |
| `part_o` `tile -> flat` | a fused epilogue's side operand is spelled `b[j]` — ONE index on a 1-D buffer — and lowered as a per-CHANNEL walk, `gn` words at stride 0 down `gm` rows. `acc_o` is full shape. **There is no spelling for a full-shape operand in a fused epilogue at all**: `acc_o[a, b]` is a `Slice`, the cluster's own two-index view, and a `Slice` has no arithmetic | **compiler / a missing form** |

`block == Dv` is not a preference and cannot be raised to cut the block count:
the accumulate reads `corr` at `span x block` and `part_o` at `span x Dv`, and an
elementwise pass walks one length — MEASURED, `block=128` refuses with *"the pass
writing 't7' reads operands of different lengths"*. It would not have helped
either: the conversion BYTES are `3 * Lq * Lkv` however the key axis is cut.

**The `part_o` one is costed, and PROVED NOT POSSIBLE from `kernels/`.** Four
rewrites were built and run, not argued:

| rewrite | result |
|---|---|
| **as shipped** — `corr` materialised, `part_o` staged | 9 conversions, 196,482 cycles, 116,610 of them conversion |
| **1.** the correction rides the softmax band, so `corr` never exists | **COMPILES** — the band does take the extra operand. **9 conversions still, and 200,578 cycles: WORSE.** `corr` was never a converted buffer; it is written and read by vector passes only, both `flat`. Removing it saves a temp and buys nothing |
| **2.** rewrite 1, then a fused epilogue reading the tile plus `acc_o` alone | **NOT EXPRESSIBLE.** `acc_o[a, b]` is a `Slice` and a `Slice` has no arithmetic — `TypeError: float() argument must be ... not 'Slice'` |
| **3.** a fused epilogue reading the tile plus `acc_o` AND `corr` | **NOT EXPRESSIBLE**, same reason — `unsupported operand type(s) for *: 'Slice' and 'Slice'` |

Two more were ruled out without building: making the accumulate `tile`-ordered
throughout moves the same conversion onto `acc_o`, since `corr` is the only
`flat` operand in that chain and it is the same size; and one accumulator across
every key block hits `_chainable`'s one-loop rule, while the two-pass fixed-max
softmax it would need materialises the whole score matrix — the 335 MB §2.4 says
must never exist.

So the change is a **full-shape side operand in a fused epilogue**, which needs a
spelling as well as a slot. The budget is there: at `gm = gn = 8` a tile plus TWO
full-shape operands is 7 of 8 descriptors (`AD_IN`, `AD_OUT`, `AD_DRAIN`, two
`BFILL`, two `BREAD`) and 256 of the 320 safe L1 words, against 5 and 136 today.
It deletes one of the three handoffs, worth 38,511 cycles per key block —
**20.8% of a `4x256` call** — in `isa/vecemit.py:ResidentEpilogueKernel`,
`lang/cluster.py`'s `Slice`, and `_epilogue`'s one-operand check.

### 5.3 Cross-unit and cross-mesh staging through the MAG L2

**Level: compiler + driver.** This is the answer to "how to use L2 to cache
temporary state so a transpose does not round-trip through DRAM".

The MAG store is the only one both a cluster and a vector core can address
(§4.4). The design has three parts and no RTL:

**(a) An L2 address space in the allocator.** `machinespec.global_addr` gains a
`tier` argument, and the arena gains a second region:

```python
def global_addr(self, base, mesh=None, tier="dram"):
    where = self.mesh(mesh).index
    if tier == "l2":
        # aperture 0. 2 MB per mesh, MEASURED from the shipped
        # STAGE_BANKS=4 / STAGE_ENTRIES=16384 parameters.
        if base >= L2_BYTES:                       # 2 << 20
            raise ValueError(...)
        return SPECIAL_BIT | (where << MESH_SHIFT) | base
    ...
```

`SPECIAL_BIT` is already defined at `machinespec.py:24` and used nowhere. The
capacity constant is the one thing to read from the bitstream rather than assume,
because `00_config.tcl` sets it per build.

**(b) A `tier=` on `L.temp`.** A kernel author says where a temp lives; the
compiler refuses one that does not fit:

```python
scores  = L.temp(span, block, tier="l2")     # 8 KB per head-block. Fits.
weights = L.temp(span, block, tier="l2")
part_o  = L.temp(span, Dv,    tier="l2")
```

**(c) The relayout pass of §5.1 reads and writes L2 addresses.** Then a
conversion is: vector core VFILLs 8 KB from L2, VDRAINs it strided back into L2.
Two URAM round trips at 2-cycle latency, instead of two DRAM round trips or one
host round trip.

**What fits, ARITHMETIC against 2 MB:**

| candidate | size | fits |
|---|---|---|
| one attention key block's `k` and `v`, D=64, block=64 | 16 KB | yes, trivially |
| one head's whole `K` and `V` at Lkv=4096, D=64 | 1.0 MB | **yes** — and every query block re-reads all of it |
| one head's whole `K` and `V` at Lkv=1024 | 256 KB | yes, 8 heads at once |
| a cross-attention `K` and `V`, all heads, Lkv=128, d=640 | 328 KB | **yes** — re-read by all 64 query blocks |
| the three attention temps at `span = Lq = 4096`, 10 heads | 15.4 MB | **no** — must be tiled by head, or `qblock` set |
| a 3x3 conv weight at 1280 -> 1280, MXFP7 | 13.4 MB | no; a K-chunk of it does |
| any VAE decoder activation past 128x128 | 67-268 MB | no, by two orders of magnitude |

**The design rule that falls out: L2 holds the operand that is re-read, not the
operand that is streamed.** `K` and `V` are re-read by every query block; `Q` and
the output are streamed. Put `K` and `V` in L2, per head, and the DRAM traffic of
a self-attention drops by the query-block count.

**Cross-mesh.** `src/kohakuaccel/sysnode/core/mag_stage.v:70` tests the mesh id
absolutely, and the header
says a foreign address passes through — so mesh 0 writing
`SPECIAL_BIT | (3 << 36) | off` lands in mesh 3's L2. Combined with
`cross-mesh is write-only` (push, never pull) and the doorbell, an all-to-all
reshard is a **scatter into peers' L2**, which is exactly the formulation
`notes/data-movement-problem.md` §2.3 says push-only forces and does not forbid.
**Untraced, and the one thing to check before designing on it:** whether the
`S_AXI_MEM` / interlink path actually forwards a remote *staging* address, or
only a remote DRAM one. `docs/address-map.md` flags the analogous question for
host traffic and says do not plan around it until someone follows the path.

### 5.4 LayerNorm and softmax at SDXL's widths

**Level: compiler. BUILT and MEASURED.** `kernels/wide.py`, `ops/norm.py`,
`api.py`; `compiler/tests/test_sdxl_norms.py`. The impossibility proof below is
worth more than the fix, because it rules out the obvious approach for good.

A fold that halves what is left at every level demands a power-of-two sub-row
count. The group-norm fold solves the same problem without that restriction — it
lifts an odd row into a buffer of its own length and folds it in at the end:

```python
    tails, n = [], rows
    while n > 1:
        if n % 2:
            tail = L.temp(1, width)
            with units(1) as e:
                tail[e] <<= held.rows_from(n - 1)[e] * 1.0
            tails.append(tail)
            n -= 1
        ...
```

**There are two fold implementations in this compiler and only one of them can
count.** MEASURED: `group_stats.plan` accepts rows = 320, 640 and 1280 —
every SDXL group size — while `api.layernorm` refuses D = 640 and D = 1280.

**A PERIODIC TAIL CANNOT WORK, AND THAT IS PROVED.** The obvious fix — lift the
odd sub-row into one row per group and read it back with `per_group` at stride 0
— was tried, and it is impossible rather than merely awkward. A spread reaches a
whole number of groups, `m * rows`. The fold's final buffer is forced to exactly
`(m-1) * rows + 1`: one sub-row shorter and the last group's start falls off the
end, one longer and some level read past its own operand. A multiple of `rows`
and `(m-1)*rows + 1` never agree, so `lang/backend.py:_agree` refuses the pass —
correctly, and that refusal is what you get.

**What works is a SPLIT.** An even count halves, as this fold always did; an odd
count splits at the power of two below it and joins the two partial buffers, both
sized to exactly the final length. `count == 1` is a VIEW and no pass at all, and
the invariant `rows - off - count + 1 == out` is what makes its reach exactly the
result's, so the join above it agrees without any spread:

```python
def _fold_span(src, off, count, out, rows, width, part, join):
    if count == 1:
        return src.rows_from(off) if off else src
    if not count % 2:
        ...                                    # the halving, unchanged
    top = 1 << (count.bit_length() - 1)
    big = _fold_span(src, off, top, out, rows, width, part, join)
    rest = _fold_span(src, off + top, count - top, out, rows, width, part, join)
    joined = L.temp(out, width)                # both partials are `out` rows
    ...
```

It costs PASSES, never reach: the fold still shrinks by exactly `rows - 1`
however `rows` factorises, and at a power of two it emits the program it always
emitted, statement for statement. SHIPPED in `kernels/wide.py`; every SDXL width
and group size runs at 0 relayouts and 0 saturations, MEASURED in
`compiler/tests/test_sdxl_norms.py`.

`softmax` at N=77 is a different refusal — `VRED` needs a multiple of 16 — and
the answer is the one `flash_attention` uses: pad the key axis to a width it does
fold and pass `keys=77`. SHIPPED as `ops.softmax_keys` and `api.softmax(keys=)`.
**PAD WITH ZEROS**: the mask lands after the exponential, which is exact, but the
MAXIMUM is still taken over the padded row, and a pad above the real maximum
drives every real term toward underflow.

The mask is a table at the row's FULL shape, and that is not a choice.
`Buffer.repeated` is this DSL's only bounded broadcast and it is addressed at
`part * instance`, so it serves ONE grid instance: MEASURED, a 512-element table
against a 2,048-element span at one instance leaves **1,152 of 2,048 elements
unwritten and reports success**, and past one instance the walk refuses. A
bounded edge mask — and a staged per-channel operand with it — waits on
`lang/backend.py:_span` exempting a broadcast from the instance offset, which
`_agree` already does.

`softmax` at N=16,384 (the VAE mid attention) was refused only because `part=8192`
is not whole groups. `kernels.part_for` is the dispatch that fixes it and
`api._fold` applies it, so a caller names neither. MEASURED at `2 x 16,384`:
p50 9.77e-6, p99 8.10e-5, max 6.16e-4, nothing past 1%.

### 5.5 GroupNorm at SDXL's group sizes

**Level: kernel. BUILT and MEASURED** as `kernels.group_norm_wide` and
`group_norm_silu_wide`, sharing `layernorm_wide`'s arithmetic rather than
copying it — a GroupNorm is a LayerNorm whose `rows` span the group.
`group_stats` works (MEASURED) and stops at the statistics; what SDXL needs is
the whole normalisation, over all 32 groups, in one kernel.

```python
@kernel
def group_norm_wide(x=L.In(..., R, W), w=L.In(..., R, W), b=L.In(..., R, W),
                    y=L.Out(..., R, W), *, eps=1e-5, rows=1280, width=VLMAX,
                    part=PART):
    """`(x-mu)*rstd*w + b` over groups of `rows*width`, EVERY GROUP AT ONCE.

    `rows` need not be a power of two once `fold_flat` carries an odd tail
    (5.4); SDXL's three group sizes are 1280, 640 and 320 sub-rows.
    """
    v, wv, bv, out = [t.as_rows(width) for t in (x, w, b, y)]
    mu = group_mean(v, rows, width, part)          # two scalings, never one
    dev = L.temp(v.rows, width)
    dev <<= v - mu.per_group(rows)
    msq = group_msq(dev, rows, width, part)        # divide BEFORE squaring
    inv = L.temp(msq.rows, width)
    inv <<= L.rsqrt(msq + eps)
    scaled = L.temp(v.rows, width)
    scaled <<= dev * inv.per_group(rows)
    out <<= scaled * wv + bv
```

This is `layernorm_wide` with `rows` set to the group rather than to the channel
axis — the same relationship `group_norm_fused` has to `layernorm_fused`. Three
properties are forced and each was paid for once already:

- **Two passes over the group, not `E[x^2] - E[x]^2`.** At a group of 8,192 with
  a mean 64 sigma off zero the identity returns the variance **21.5% low** on
  this lane's 16-bit significand (SOURCE `kernels/groupnorm.py` header).
- **Scale before folding and again after the row sum.** A group of 163,840 sums
  far past fp16's 65,504, and so does `rows * width` itself.
- **`w` and `b` arrive at full shape**, because an elementwise pass reads
  operands of one length. For a per-channel gain over `C/32 * H * W` this is a
  `Spread` with period `H*W` — one AGU dimension, no pass — which
  `demos/.../nn.py:GroupNorm` currently does by materialising the broadcast.

MEASURED accuracy to beat, from `group_stats`' own docstring at 163,840 elements:
mean 2.4e-04, variance 7.7e-04, against 7.5e-02 for the form that skips the
spread.

**Fuse the SiLU.** `kernels.group_norm_silu` already exists for the VLMAX case
and is the exact pair every UNet and VAE resnet uses. The wide form should carry
the same `y <<= h * sigmoid(h)` tail; it is two passes rather than one because
`h` is read twice and a vector chain carries one running result.

### 5.6 Convolution: what runs, and the one thing that does not

Stride-1 3x3 is **built and MEASURED correct** at 1 dispatch and 0 relayouts.
`conv2d.md` §4 has the derivation; nothing here changes it.

**Stride 2 is a packer, not a kernel — BUILT and MEASURED** as
`ops.conv2d_stride2` over `layout.ConvEntry(step=)`: 1 dispatch, 0 relayouts,
error identical to stride 1, and **4.00x / 4.00x / 3.67x fewer cycles** than
dense-and-discard at 16², 32², 64². The field is `step`, not `stride`, because
`lang/backend.py:_held` reads `.stride` off a layout as a batch BYTE stride and
a 2 there sizes the whole activation at one element. `conv2d.md` §4.1 derives
it and the derivation is verified numerically but not built. Splitting **both**
axes by residue makes the tap offset constant again:

```
    dy = qy*s + ry ,  dx = qx*s + rx
    A[s*oy + dy, s*ox + dx]  ==  sub[ry, rx][oy + qy, ox + qx]
    offset = (ry*s + rx)*plane + qy*Wsub + qx          a constant, as at stride 1
```

So `ConvEntry` gains a `stride` parameter that packs `s*s` sub-planes instead of
one, `Tap.rebind` gains the `(ry, rx)` term, and **nothing else changes** — no
compiler mechanism beyond the lane offset that already exists, no ISA, no RTL.
Cost is `s*s` sub-planes of the same total size. The alternative, computing dense
and discarding, costs exactly `s^2` = **4.0x MAC, measured at all three shapes**.

Two caveats stand, neither this page's to fix: a tapped fill straddles a 4 KB
boundary on **6 of 9 taps** and `mag_mem_port.v` has no split logic
(`hardware-wants.md` §5, untested on silicon); and `nk` is forced to 1, so a pass
is `3*(9C/32) + 1` flits and the **tile is the only lever** — 22-26k flits near
`gm*gn <= TILES` against 45-47 million at `gm=2, gn=1`.

### 5.7 Cross-attention: the shape that suits this machine best

Cross-attention is the cheapest attention in SDXL and the one L2 helps most.

| | level 1 | level 2 |
|---|---|---|
| Lq | 4096 | 1024 |
| Lkv | 77, padded to 128 | 77, padded to 128 |
| K and V, all heads, fp16 | 164 KB each | 164 KB each |
| query blocks re-reading them | 64 | 16 |

`K` and `V` together are 328 KB — **12% of one mesh's MAG L2** — and they are
re-read by every query block. Staging them (§5.3) turns 64 DRAM re-reads into 64
URAM re-reads and removes them from the DRAM budget entirely.

The kernel is `flash_attention` with `keys=77` and no other change; the `edge`
mask it builds from `L.table` is exactly the padding this needs. What must change
is only where `k` and `v` are allocated.

### 5.8 Upsample, downsample and concat

Concat and slice are **allocation, not arithmetic**, and the bytes are now
PROVED identical — what is left is a way to say ADJACENT. The upsample turned
out not to be allocation at all: it folds into the convolution's weights, and
that is BUILT.

**Channel concat** (`torch.cat([h, hs.pop()], dim=1)`, 9 per forward). In the
`ConvEntry` layout the activation is `[C/32][plane][32]`, so the channel axis is
the **outermost**. Concatenating along it is placing two buffers adjacently:

```
    [Ca/32][plane][32]  followed by  [Cb/32][plane][32]
    ==  [(Ca+Cb)/32][plane][32]
```

**The concat is free if the two producers wrote into one allocation.** That is a
lifetime-planner requirement — "allocate `h` and the skip tensor as two halves of
one span" — and `kohakuaccel/lifetime.py` already packs temps whose lives miss.
It needs a way to say *adjacent*, which `lifetime.pack(groups=)` half provides.
Every SDXL channel count is a multiple of 32, so no concat straddles a block.

**MEASURED, and it is exact.** `pack(a)` without its tail entries, followed by
`pack(b)`, is BYTE-IDENTICAL to `pack(concat(a, b))` at all five real skip
shapes — `32x32 1280+1280`, `64x64 640+640`, `128x128 320+320`, `64x64 640+1280`
and `8x8 32+64`. The one condition is the one the sentence above states: the
first buffer must be written without its tail, which is what "one span, two
halves" means. **A channel SLICE of the low blocks is the same fact backwards**
— the leading prefix, byte-identical at the same five shapes — so requirement 25
is closed on the channel axis for free. `compiler/tests/test_conv2d_stride2.py`.

**Nearest-2x upsample** (2 in the UNet, 3 in the VAE). Output pixel `(2y+i,
2x+j)` reads input `(y, x)` for all four `(i,j)`. In `ConvEntry` order a pixel's
32-channel block is 64 contiguous bytes, so the upsample is a **stride-0 read on
two axes** — precisely what `vec_agu` spells and what `Buffer.repeated` is. It is
a 4-dimension affine walk:

```
    dst (c, y, i, x, j)   strides (plane_out, 2*Wout, Wout, 2, 1)
    src (c, y, _, x, _)   strides (plane_in,  Win,    0,    1, 0)
```

Five dimensions, which is one more than `vec_agu` has and one fewer than the
mover's six. So: **either two vector passes (duplicate along x, then along y), or
one mover `COPY`.** The mover is host-commanded and its rate is unmeasured since
the rebuild (§6), so the two-pass vector form is what to build.

**Fusing it into the following conv is better still, and it is BUILT** —
`ops.conv2d_upsample2` and `weights_for_upsample2`. `Upsample2D` is always
`nearest2x -> Conv3x3`, so output `(2y+iy, 2x+ix)` reads
`a[y + (iy+dy-1)//2][x + (ix+dx-1)//2]` — and over `dy` that takes only TWO
values, whichever `iy` is. So each of the four output residue classes is a
**2x2** convolution on the ORIGINAL plane, at ordinary stride-1 tap offsets
shifted by `(iy, ix)`, with the taps that land on one input pixel ADDED
together once at checkpoint load.

The tap index is `(iy+dy-1)//2 + 1 - iy`: the input offset runs `-1..1` and the
operand's runs `0..1`, and the difference is exactly the class shift the kernel
adds back. Off by one there reads the neighbouring pixel and still looks like a
convolution, which is why the test checks that each class's folded weights sum
to the whole 3x3.

MEASURED: 4 dispatches, 0 relayouts, error identical to a plain conv, and
**2.15x / 2.00x fewer cycles** at 16x16 and 32x32 than materialising the 2x
activation and running a 3x3 over it — 1.08x at 8x8, where the pad ring is the
layer. The 4x activation is never written. The four results ARE the residue
split of the `[2H][2W][N]` output, which is `ConvEntry(step=2)` order, so a
consumer that wants the interleaved plane still pays for the interleave; the
next thing in a resnet is a GroupNorm, which does not care about pixel order.

**Stride-2 downsample** is §5.6.

### 5.9 Timestep and label embedding

`get_timestep_embedding` is `cos/sin(t * exp(-log(10000) * arange(160)/160))`.
The vector ALU has `exp2, log2, inv, rsqrt` and **no sin or cos**
(`hw/vector.py:OPS`). Three options, and the third is right:

| | cost |
|---|---|
| polynomial sin/cos on the lane | a chain per element, plus range reduction the ALU does not help with |
| a 5th transcendental seed | RTL, and it would be used 320 times per forward |
| **build the 320-wide embedding on the host** | 320 floats per step, once, uploaded with the step |

The embedding is `B x 320` per step — 640 bytes. `time_embed` and `label_emb`
themselves are ordinary `Linear -> SiLU -> Linear`, which is `kernels.mlp` with
`act="silu"`, and `label_emb`'s 2816 -> 1280 is the widest K in the model at a
trivial M.

**Do not put sin/cos in this ALU for SDXL.** The budget for host-side scalar work
is "thousands of operations per token, not billions" (`vector-core.md` §9), and
320 sinusoids per step is four orders of magnitude inside it.

### 5.10 The VAE mid attention

`AttnBlock(512)` at 128x128 latent is `Lq = Lkv = 16,384`, `D = 512`, **one
head**. It is the one operator in the pipeline that `flash_attention` does not
fit, for two reasons:

1. **`block == Dv` is required**
   (`compiler/kohakutpu/kernels/attention.py:152`), and `Dv = 512` here. A
   512-wide key block against a 16,384-long key axis is 32 blocks, which is fine,
   but the temps become `span x 512`, and with `qblock` unset `span = 16,384` —
   16 MB per temp, against 2 MB of L2 and three temps.
2. The `q/k/v/proj_out` projections are 1x1 **convolutions** over `[C][H][W]`,
   which in `ConvEntry` order is a GEMM (`conv2d.md` §4) — that part is free.

So the VAE mid attention needs `qblock` set, which the kernel already supports:
`qblock=512` gives temps of `512 x 512` = 512 KB, three of them = 1.5 MB, which
fits MAG L2 with room. Cost is one extra pass per key block, which the kernel's
own docstring prices.

**And it needs §5.1**, because at 32 key blocks and 32 query blocks the
un-fixed relayout bill is `1024 x 6` host round trips for one operator.

---

## 6. What is missing or untested, by level

Naming the level is the point; an unlocated blocker is missing research, not a
blocker.

### 6.1 Compiler

| # | gap | evidence |
|---|---|---|
| C1 | ~~computed and never executed~~ **CLOSED for the intra-kernel list.** A conversion runs on a vector core. A tensor handed BETWEEN kernels in two orders still goes through `rt.Tensor.address` | MEASURED: the head-split path's `q`, `k` and `v` — exactly 3, and it does not grow with the key-block count |
| C2 | `isa/vecemit.entry_walk` and `ElementwiseKernel(drain=)` implement an on-card strided relayout and are called by **nothing but `compiler/tests/test_layout.py`** | SOURCE, repo-wide grep |
| C3 | ~~two folds, one can count~~ **CLOSED.** `fold_flat` splits an odd count instead of halving it; every SDXL width and group size runs | MEASURED, §5.4 and `compiler/tests/test_sdxl_norms.py` |
| C3a | ~~converts buffers the next stage overwrites~~ **CLOSED.** `3*blocks - 3` of `flash_attention`'s conversions are no longer run or charged | MEASURED: `4x256` went from 21 run to 12, and `cost.time` from 739,258 to 507,352. `compiled.conversions` still LISTS 21, so it is no longer the authority — do not zip it against `cost.relayouts` |
| C3b | **`Buffer.repeated` serves ONE grid instance.** `_agree` exempts a broadcast from the length rule; `_span` and `_at` still charge it `part * instance` | MEASURED: 1,152 of 2,048 elements unwritten and success reported, §5.4 |
| C4 | No L2 address can be formed. `machinespec.SPECIAL_BIT` is dead; `global_addr` raises above 1<<36 | SOURCE |
| C5 | No transpose, permute or slice view in the DSL. `Spread` — a buffer read periodically, `take` elements of every `period` — is the only address-dependent operand it has | SOURCE `compiler/kohakutpu/lang/buffers.py:269` |
| C6 | A per-channel epilogue operand cannot be **staged** — `linear_bias` is fused-only | `hardware-wants.md` §7 |
| C6a | **A fused epilogue has no FORM for a full-shape operand.** Its one side operand is spelled `b[j]` and lowered as a per-CHANNEL walk; `acc_o[a, b]` is a `Slice`, which has no arithmetic. The budget is there — 7 of 8 descriptors, 256 of 320 L1 words at `gm=gn=8` — the spelling is not | MEASURED: four rewrites, §5.2a. Worth 20.8% of a `4x256` flash call |
| C7 | ~~the cost model does not price a MAG round trip~~ **`cost.relayouts` now prices a conversion**, which is how §5.2a was measured at all | — |
| C8 | Nothing bounds a **model**, only a call; weights are not distinguished from activations; no test at 16 GiB | `memory.md` §4 |
| C9 | The L1 bank bits are written as zero, so half of a 512-entry L1 is unreachable | `hardware-wants.md` §4 |

### 6.2 Hardware, built but untargeted

| # | thing | state |
|---|---|---|
| H1 | **MAG L2 staging**, 2 MB/mesh, mesh-wide, read/write, addressable by every requester | in the v7 top; no software; **port A tied off**, so only the 256-bit path is live |
| H2 | **NoC L2 adapter**, 256 KB x 10 per mesh | in the v7 top; `l2_en = 0` at reset; serves only its own endpoint; **refuses a quantised fill and multicast** |
| H3 | the 6-D tensor descriptor walker `mx_tdesc.v`, conv-im2col validated | **not wired into the fill engine**. `conv2d.md` §5: this is the enabling change for conv at full speed, ~2-3 weeks in `mag_mem_port.v` |
| H4 | shared fetch (one DRAM read multicast to up to 4 units) | decoded by the hardware, **the driver does not set it**; a follower cannot yet tell which fill an entry belongs to |
| H5 | split-K epilogue on a vector core | designed, not built |
| H6 | `FWD`, chain bypass, second accumulator | not built |

### 6.3 Hardware, missing

| # | thing | consequence for SDXL |
|---|---|---|
| H7 | **mover `TRANSPOSE` faults if requested** | the one engine with 6 affine dimensions cannot permute |
| H8 | a compute unit **cannot command the mover** — only the control processor can, and a unit has no address that reaches it | any rearrangement is orchestrated by the node's processor or by the host between programs. The processor is now part of every node, so the `_pe` top variant is gone: every mesh carries one |
| H9 | a vector core cannot emit MXFP7, so VC -> cluster L1 is shut | costs flash attention half its stages, and blocks `linear -> act -> linear` with the activation never reaching DRAM |
| H10 | no sin/cos | §5.9 — do not fix this for SDXL |
| H11 | no element-dynamic behaviour (gather/scatter with computed indices) | SDXL needs none. Recorded because it is the machine's largest single gap and the next model may |

### 6.4 Untested — things this page could not settle

1. ~~**Whether a remote *staging* address forwards over the interlink.**~~
   **SETTLED, and the answer is no.** `mag_ilink`'s AXI slave side is wired to
   the mover's write channel alone
   (`src/kohakuaccel/sysnode/core/mag.v:667-671`); a compute unit's request
   and a host access both reach `M_AXI_DRAM` with the full 40-bit address and
   land in local DRAM above 64 GB, where nothing answers. Only the mover
   crosses. §5.3's cross-mesh half therefore needs a mover pass on the
   **sending** side, not a remote address on the receiving one.
2. **The memory mover's rate.** There is none to quote: the figure branch B in
   [conv2d.md](conv2d.md) §6 was once decided against predates the mover rebuild
   and has been withdrawn. Nothing here uses a mover rate; §5.8 chooses the
   vector-core form partly for that reason.
3. **Whether the unit models are byte-faithful across an unexecuted
   conversion.** MEASURED: `mlp` records a `Tile -> Entry` conversion, the
   two orders differ in 16,192 of 16,384 elements, and the kernel nonetheless
   returns peak-relative p50 3.7e-3 / max 2.2e-2 — bit-identical to the
   two-call form. The `relayouts` counter says why: the runtime performed a
   host round trip. That is consistent, but it has **never been run on
   silicon**, and it is the first thing to check when `mlp` next is.
4. **Everything on this page is simulation.** No conv, no attention and no norm
   figure here came off the card.
5. **`gn = 4` for a 64-wide head projection** (§5.2) has not been measured
   against `gn = 8`. The intensity arithmetic says 5.3 against 8.0; whether that
   shows at 700-900 MAC/byte is an experiment, not a derivation.

---

## 7. What each gap costs

Effort is in the units this project has used before — a compiler change that
touches one pass, or an RTL change with a bench.

**BUILT**, with what each one is and what it unlocked:

| item | level | what it is | what it unlocked |
|---|---|---|---|
| §5.4 split `fold_flat` | compiler | one pass in `kernels/wide.py`. A **split** at the power of two below an odd count, not a periodic tail — §5.4 proves the tail impossible | **all 210 LayerNorms and all 46 GroupNorms** |
| §5.5 `group_norm_wide` | kernel | one kernel sharing `layernorm_wide`'s arithmetic, plus the SiLU pair | the UNet and VAE resnet normalisation |
| §5.2 head split as an N-partition | kernel | a RESHAPE and two kernels, at **x1.00 cycles and x1.00 flits** | requirements 20 and 22, and 4 host permutes per attention |
| §5.1 execute `conversions` on the card | compiler | the relayout path in [relayout.md](relayout.md) | the host, out of every INTRA-kernel relayout |
| §5.6 stride-2 packer | compiler | `ConvEntry(step=)` — the name is `step`, not `stride`, which collides with the batch byte stride the backend reads | the 2 `Downsample2D`, against a MEASURED **4.00x** |
| §5.8 upsample fused into conv | kernel | four 2x2 convolutions on the original plane, weights folded at load | the 2 `Upsample2D` and the VAE's 3, at **2.0-2.15x** and a quarter of the activation |

**LEFT**, in the order the evidence now argues for:

| item | level | effort | what it unlocks |
|---|---|---|---|
| a drain that writes ROW-MAJOR, or an L1 read granule matching the 4x4 drain | **RTL** | the matmul array's output granule | **the whole conversion bill.** It is 49-68% of a flash call and 99.6% of it is the two Tile crossings, which exist only because MG's output granule is a 4x4 sub-tile while everything else reads rows. §5.2b |
| C6a a full-shape side operand in a fused epilogue | compiler + ISA | a SPELLING as well as a slot; the budget is already there | lets `part_o` fuse — but NOT a cycle of the transpose, since `corr` then crosses at the same size. Worth it for the DRAM round trip, not for the ALU. §5.2b |
| C3b `Buffer.repeated` past one instance | compiler | exempt a broadcast from the instance offset in `_span`, as `_agree` already does | a BOUNDED edge mask, and a staged per-channel operand with it |
| §5.8 adjacent allocation for concat | compiler | `lifetime.pack(groups=)` | the 9 skip concats, free — the bytes are already proved identical |
| §5.3 L2 tier in the allocator | compiler + driver | `SPECIAL_BIT`, a tier on `L.temp`, an arena region | **2 MB per mesh** that is on the card and unreachable |
| §5.7 K/V resident in L2 | kernel | allocation only, once §5.3 lands | 64x fewer DRAM re-reads on a cross-attention |
| H3 wire `mx_tdesc` into the fill engine | **RTL** | ~2-3 weeks in `mag_mem_port.v`, per `conv2d.md` §5 | conv at full speed; a 6-D operand descriptor is a **general** answer to much of §5.1 |
| H9 quantiser on the vector-core drain path | **RTL** | a bench and a format | the `weights` handoff, one of the three in §5.2a; opens `linear -> act -> linear` |
| H7 mover `TRANSPOSE` | **RTL** | mode 1 is allocated | an alternative to §5.1 for bulk, host-scheduled permutes |

## 8. The order the evidence argues for

Ordered by measured cycles, not by how much work an item appears to delete.
Those two orderings disagree here: deleting host permutes is real and removes
**zero cycles of conversion** (§5.2), while the item that changes the floor is
the one nothing at the kernel or compiler level can reach.

1. **The drain order, in RTL.** §5.2b: conversions are 49-68% of a flash call,
   99.6% of that is the two `tile <-> flat` crossings, and they exist because
   MG's output granule is a 4x4 sub-tile while every reduction and every fill
   reads rows. Nothing at the kernel or compiler level reaches it — the compiler
   already keeps `tile` through elementwise work, and the reduction genuinely
   needs rows. This is the only item that changes the floor.
2. **§5.3**, because until an L2 address can be formed, 2 MB per mesh of shipped
   silicon is unreachable and every staging design is untestable.
3. **§5.8's allocator half and C3b**, both small and both blocking something
   already proved correct.
4. **C6a**, worth it for the DRAM round trip it saves, not for the ALU — §5.2b
   shows it moves the crossing rather than deleting it.
5. **H3**, which makes conv fast rather than merely correct.

Nothing on this list is now what keeps an SDXL layer from running at its real
width — §§5.2, 5.4, 5.5, 5.6 and 5.8's kernel half are built and measured. What
is left is what keeps it from running FAST, and four of the six items are the
same thing: a conversion that should not happen, or should not land in memory.

---

## 9. Do the pieces COMPOSE?

Every gap above was closed and measured on its own. This section is the other
question: assembled into the blocks SDXL actually repeats, do they still run?
Each fragment is built from `ops`/`kernels`/`api` only, graded against a float64
reference, and priced with `cost.time`. **Host rearrangements are counted**, on
the rule that a fragment which only runs by going through numpy has not run.

### 9.1 What runs

| fragment | shape | p50 | p99 | max | >1% | >10% | cycles | conversion | host moves |
|---|---|---|---|---|---|---|---|---|---|
| `ResnetBlock2D` | 8x8, 320 -> 320 | 3.64e-3 | 1.52e-2 | 2.68e-2 | 7.7% | **0%** | 301,568 | 0 | 10 |
| `ResnetBlock2D` | 8x8, 320 -> 640 | 3.46e-3 | 1.44e-2 | 2.65e-2 | 6.4% | **0%** | 724,096 | 0 | 10 |
| down-block: 2 resnets + `Downsample2D` | 8x8, 320 | 5.17e-3 | 2.32e-2 | 3.48e-2 | 21.8% | **0%** | 637,824 | 0 | 21 |

The group is 640 elements — `(320/32) * 8 * 8`, five sub-rows — which is a real
SDXL group size and one of the shapes that was refused outright before §5.4.
**Nothing in the convolution path refuses, and nothing past 10% comes out.** The
error grows with depth exactly as composing fp16 stages should: one block 2.7e-2
at the max, three blocks 3.5e-2.

### 9.2 What it costs to compose: the host moves

The 21 moves of the down-block, by cause. Eight are LOAD-TIME — the affine put
into group order depends only on the weights and the plane — so **13 are per
call**:

| moves | cause | per call? |
|---|---|---|
| 4 | gather the groups out of `[H][W][C]` | yes |
| 4 | scatter the groups back | yes |
| 4 | the gain into group order | no, once at load |
| 4 | the shift into group order | no, once at load |
| 5 | pick the real raster rows out of `[plane][N]` | yes |

**The gather is the finding, and it is a channel-count fact.** A `GroupNorm(32,
C)` group is `C/32` channels by the whole plane; a `ConvEntry` channel block is
32 channels by the whole plane and is CONTIGUOUS. The two line up only when the
group IS a block:

| C | channels a group | against the 32-channel block |
|---|---|---|
| **320** | **10** | no |
| **640** | **20** | no |
| **1280** | **40** | no |
| 512 (VAE) | 16 | divides 32 |
| 256 (VAE) | 8 | divides 32 |
| 128 (VAE) | 4 | divides 32 |
| 1024 | 32 | the group IS a block |

**None of the three UNet channel counts aligns.** So every `norm -> conv` pair —
and a resnet is two of them — needs the channels gathered into group order and
scattered back.

And the obvious escape does not work either. The reductions are a sum and a sum
of squares, which are order-free, so where a group IS a block the norm could read
the convolution's own bytes. **MEASURED at `8x8x1024`, and it is wrong**: p50
2.84e-2, max 2.35e-1, **2.7% of elements past 10%**. `ConvEntry`'s plane is
PADDED — 100 positions for a 8x8 image — so the group carries 36 halo zeros per
channel, and while a sum over them is a sum over zeros, the MEAN is not: it
divides by the padded count. Fixing that needs the pad excluded from the
reduction and the divisor told the real count, which is a masked reduction with a
`valid=` count — a kernel change, and one that would only ever help the VAE,
since no UNet count reaches 32 channels a group.

### 9.3 What refuses

| fragment | state |
|---|---|
| `ResnetBlock2D` | runs |
| down-block (2 resnets + downsample) | runs |
| `BasicTransformerBlock` | not assembled here. Its parts each run; the composed fragment has not been graded, and §9.2's host-move accounting is what it would have to carry |

Two traps the assembly turned up, both in the caller and worth writing down:

- **`kernels.geglu(x, wg, wu)` gates on its FIRST weight.** SDXL's
  `h, gate = proj(x).chunk(2, dim=-1)` gates on the SECOND half, so the halves go
  in swapped. Getting it backwards is a plausible-looking 20% wrong — p50 1.97e-1
  with 73% of elements past 10%, which reads like a broken kernel and is not one.
- **`geglu` lowers the SIGMOID gelu**, `x * sigmoid(1.702x)`, while SDXL's
  `nn.functional.gelu` is the erf one. Grading against `tanh` reports the
  approximation rather than the kernel; `kernels.gelu_tanh` is the other one.
