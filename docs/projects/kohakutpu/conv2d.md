---
title: Fast 3x3 convolution
summary: The SDXL UNet convolutions as an implicit GEMM with the taps inside the K sweep — the layout fork, the branch that runs on today's bitstream, and what materialising the operand instead would cost.
tags:
  - kohakutpu
  - compiler
  - kernels
  - sysnode
---

# Fast 3x3 conv on this machine

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
the stack. **One kernel tuned once serves all three**, and a regression at one
resolution is a regression at all of them. Every shape already satisfies the
hardware's `M = 4a, N = 4b, K = 32c`.

At 24 clusters x 512 MAC/cycle = 12,288 MAC/cycle: 12.3 ms for the whole chip at
100 MHz, 4.10 ms at 300 MHz.

## 2. Conv here is compute-bound, and not marginally

| shape | activation | weights (MXFP7) | MAC per byte |
|---|---|---|---|
| 128x128x320 | 10.5 MB | 835 KB | 719 |
| 32x32x1280 | 2.6 MB | 13.4 MB | 812 |

Three orders of magnitude clear of the port. **So the design goal is to keep the
clusters fed with the fewest instructions, not to save bandwidth** — which is
what makes the layout question below worth paying memory for.

## 3. The formulation: implicit GEMM, taps inside the K sweep

A 3x3 conv is a GEMM with a contraction of `9*C`, where the K axis runs over
(tap, channel) and each tap is the same activation at a different spatial offset:

```
    C[p, n] = sum_t sum_c  A[p + delta(t), c] * W[n, t, c]
```

`delta(t) = dy*Wp + dx`. Nothing here needs im2col's 9x memory: a tap is a
different **base address**, and the accumulator already persists across the GEMMs
of one sweep, so all nine land in one tile with one drain.

Weights are `[N][9C]`, K-index `(dy+1)*3C + (dx+1)*C + c` — a host-side permute of
PyTorch's `(out, in, kh, kw)`, done once, free.

**The whole difficulty is the operand layout, and it is one question: can a fetch
stride?** A cluster's operand entry is 4 lanes x 32 K, and for conv the lanes are
4 adjacent output columns. Lane `l` computes output `x0+l`, so for tap `dx` it
needs input `x0+l+dx` — "the same entry, one position over". In a plain NHWC
buffer that entry is 4 runs of 32 channels strided by `C`, so it needs a strided
fetch. **The fill engine cannot stride:** `FILL` takes a base and a count, and
entries are contiguous by construction, deliberately.

## 4. Branch C — conv on the current bitstream

**Built and verified**, and what ships: `compiler/kohakutpu/kernels/conv2d.py`.
What blocked it was never the hardware — the FILL `addr` field is a full 40-bit
byte address and MAG reads at byte granularity — but the compiler, whose fill address
was an integer times the fill's own span. `Slice` now carries an offset in lanes,
and `LO.ConvEntry` describes the layout, which `LO.Entry` cannot: nine taps are
nine OVERLAPPING windows, not a tiling.

**Store the activation `[C/32][H][W][32]` and run with `nk = 1`.** One L1 entry
is 4 lanes x 32 K. In this layout a pixel's 32-channel block is 64 B contiguous,
so 4 consecutive pixels are **256 B contiguous = exactly one entry**, and the
fill is a single run. The lane packing never breaks, because the channel block —
not the pixel — is the outer axis. A 3x3 tap is then a **constant base offset of
`(dy*Wp + dx) * 64` bytes**, straight into the FILL `addr` field. Nine taps, nine
bases, one accumulator. Nothing in the hardware changes.

It is also **identity-equivalent for 1x1, linear and attention**, so the same
layout serves the whole SDXL block with no conversion anywhere. Compare the
alternatives at `gm=8`: NHWC gives 64 B runs (4 AXI transactions per entry), NCHW
gives 2 B runs (16x amplification, which the mover cannot even express), this
gives 256 B = one entry, one AR.

Four consequences, each derived and each load-bearing:

- **`nk = 1` is FORCED, not chosen.** Two channel blocks of one pixel are
  `plane*64` bytes apart, so a fill of `nk > 1` entries is not a run. Any layout
  that makes them adjacent makes the four lanes non-adjacent, which is what the
  `dx` tap shifts. There is no trade to make. Bandwidth is `4(gm+gn)/(gm*gn)`,
  independent of `nk`, so it costs instruction count only.
- **A pass is `3*(9C/32) + 1` flits** = 271 / 541 / 1081 at the three shapes.
  The 128-flit staging window is not a constraint: `dispatch.plan` cuts an
  instance into windows and kicks them in order on one node, and nothing between
  them touches the accumulator, so a 1081-flit sweep across nine rounds chains.
- **The TILE is the lever.** Flits per pass do not move with `gm`/`gn`, so passes
  do: a layer is **22–26k flits** near `gm*gn <= TILES` and **45–47 million** at
  `gm=2, gn=1`. That is 2000x, and it is the only number in branch C worth
  tuning.
- **The M axis must be the PADDED raster**, `q = y*Wp + x`. Four adjacent outputs
  are four adjacent inputs only WITHIN a row, so the sweep runs the whole plane
  and discards `x >= W`: +3.1% / +6.3% / +12.9% of M. It is also what makes zero
  padding free — the halo is already in the plane, so there is no masking.

Two caveats, neither handled today: a 64 B-offset base puts **1 entry burst in 16
across a 4 KB boundary** and `mag_mem_port.v` has no split logic — it is 6 of the
9 taps, no packing removes it, and it is parked
([hardware-wants.md](hardware-wants.md) §5). And the allocation needs one entry of
tail padding, which `ConvEntry` derives from the tiling rather than assuming.

### 4.1 Stride and dilation — both free, one needs a packer

Analysis only, checked numerically but not built. The shipped kernel assumes
stride 1, and SDXL's downsample path is 3x3 **stride 2**.

**Dilation is already free.** At dilation `d` with `pad = d`, a tap reads
`(oy + dy*d, ox + dx*d)`, still a constant `(dy*d*Wp + dx*d) * 64` bytes, and four
adjacent outputs are still four adjacent inputs. Only the constant changes.

**Stride breaks contiguity**, because four adjacent outputs then read inputs `s`
apart. Three ways out:

| | cost | what changes |
|---|---|---|
| compute dense, discard | **exactly `s^2`** MAC — 4.0x measured at all three shapes | `positions()` only |
| split by `x mod s` | 1.5–3.8x in per-row tiling waste | packer, 2-D grid, richer `Tap` |
| **split by `(y mod s, x mod s)`** | **none** | **packer only** |

The middle row is the trap: it restores contiguity along x, but the row index
becomes `s*oy + dy`, which is not affine in a flat raster — so the grid must be
per-row, and a `4*gm`-wide tile against a `ceil(Wp/s)`-wide row wastes 1.97x at
128x128 and 3.76x at 32x32, no better than computing dense.

The third works because splitting BOTH axes by residue makes the tap constant
again. With `dy = qy*s + ry` and `dx = qx*s + rx`,

```
    A[s*oy + dy, s*ox + dx]  ==  sub[ry, rx][oy + qy, ox + qx]
```

so the offset is `(ry*s + rx)*plane + qy*Wsub + qx` — a constant, exactly as at
stride 1. Verified exhaustively at 8x8 and 16x16 stride 2, 12x10 stride 2, 9x9
stride 3. **So stride costs a packer variant and nothing else** — no compiler
mechanism beyond the lane offset that already exists, no ISA change, no RTL.

## 5. Branch A — the designed answer, built but not wired

The architecture already decided how conv works, and the mechanism exists:

> **Convolution is a memory request.** The compute instruction for a convolution
> is *byte-identical* to the one for a matmul. Only the descriptor changes.

`src/kohakuaccel/sysnode/mover/mx_tdesc.v` is a 6-dimensional affine walker,
**built and conv2d im2col validated** — but **not wired into the fill engine**.
The
descriptor for a 3x3 conv is six lines:

```
   dim     n     oy     ox     ky    kx    c
   stride  sN    S*sH   S*sW   sH    sW    sC
   axis    -     H      W      H     W     -
```

Out-of-range addresses **inject zeros and issue no memory request**, so padding
needs no halo buffer, no zero rows, and no handling anywhere else in the machine.
**Cost: nothing at all.** Conv becomes a matmul whose operand descriptor happens
to be six-dimensional.

What is missing is the wiring, not the walker — roughly 2–3 weeks, all in
`mag_mem_port.v`. In-order return is free, since every read uses `m_arid = 0`
and AXI requires same-ID responses in order, so a lane's beats arrive where the
entry assembler expects them however the bursts were split. Zero injection is at
the input, not the emit path: for an invalid lane, do not issue the AR and drive
`beat = 0`, `beat_valid = 1` — two wires the read engine already drives.

Untouched: the whole cluster CU and manager, the emit buffer, response tagging,
peer multicast, credit accounting, L1 write addressing. Two open risks, both
ordinary engineering: widening the 8-bit count, and whether MAG's read engine
can hold per-cluster descriptor state without serialising the eight clusters
that share it. **The second is not determined.**

> **THE ARGUMENT THIS SECTION USED TO MAKE IS GONE, AND SO IS THE PROBLEM IT
> WORKED AROUND.** It reasoned about feeding the quantiser: that `mx_quant` has
> no `last` port so four 2-beat bursts feed it byte-identically to one 8-beat
> burst, and that pre-quantised *activations* were what conv gave up, because a
> converted entry's word interleaves all four lanes at 7-bit granularity and conv
> needs each lane from a different address.
>
> **A fetch is never transformed now.** What is at an operand's address is
> already in its final format, so there is no quantiser in this path to feed and
> no per-operand choice to give up. The interleaving observation survives as a
> LAYOUT fact and it still decides the same thing: a converted entry cannot be
> assembled from four independently-addressed lanes, so a conv activation is
> either held in FP16 or converted by a mover pass that walks the conv order.
> The trade moved from the instruction to the schedule.

## 6. Branch B — materialising the operand, and what it would cost

Fold the x-tap into K. Build `A'[y][x][3][C]` with `A'[y][x][t][c] =
A[y][x+t-1][c]`, zeros outside: the x tap is then inside the contraction so lanes
never shift for it, and the y tap is a whole-row offset. The kernel is three
accumulating sweeps of `K = 3C` at row offsets `-W`, `0`, `+W`, into one
accumulator.

Building `A'` is one mover descriptor, and the mover's ISA suits conv better than
expected — `COPY` with both descriptors is an arbitrary N-D affine strided copy,
and a source element whose `valid` is low injects an immediate, "which is how
`pad` works", so bounded axes give zero padding natively.

**And on the engine this was measured against, it must not.** ~98 MB/s, one
32-byte word per packet, ~33 cycles each: building a 31.5 MB `A'` at that rate
is **~320 ms against 12.3 ms of convolution**, 26x more expensive than the work
it exists to enable. The same arithmetic kills full im2col and kills a host-side
build over any transport this machine has.

**THAT RATE IS SUPERSEDED and the conclusion is therefore open.** It predates
the mover rebuild — `multi-mesh.md` §8 has the measurement and says so — and one
word per packet is exactly what the rebuild coalesces. Nobody has re-measured
the mover, so branch B is **unpriced**, not refuted. What is unchanged is the
ratio it has to beat: a build pass that costs more than the convolution it feeds
is not worth wiring in, whatever the rate.

Branch C is what runs meanwhile, and the enabling change for branch B is the
same one it always was — the descriptor in the fill path, so `A'` is never
materialised at all.

> **"Fill engine" is a misnomer.** There is none in `mx_cluster_mgr.v` — the
> manager only exposes a backdoor L1 write port. The FILL address walk lives in
> **MAG**, one NoC hop away, and the CU issues *one* flit naming the whole run.
