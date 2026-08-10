# The mover ISA

The normative interface. Two ways in -- registers and a command block -- with
identical semantics, so bring-up and production share one decoder.

Field widths follow `mx_tdesc.v`'s parameters exactly (`AW=40`, `CW=16`,
`SW=32` signed, `XW=16` signed). Do not narrow them here; a descriptor that the
walker can express and the ISA cannot is a trap for the compiler.

---

## 1. The execution model, in four sentences

**The destination descriptor defines the iteration space.** The source walker is
stepped in lockstep with it, which is what makes a source stride of 0 a
broadcast without a separate mode. **A source element whose `valid` is low
injects `MV_IMM`** (zero by default), which is how `pad` works. **A destination
element whose `valid` is low suppresses its write**, which is how a clipped or
bounds-limited write works.

A move ends when the destination walker reports `last`. `MV_STATUS.done`
increments after the final AXI `B` response, never before -- see
[`arch.md`](arch.md) §8.

## 2. Register map

64-bit writes on MAG's control window (`sc_*`), at `MV_BASE`. All registers are
write-only except `MV_STATUS`.

| offset | name | fields |
|---|---|---|
| `0x00` | `MV_CTRL` | `[2:0]` mode, `[4:3]` ewidth, `[15:8]` flags, `[16]` go, `[17]` abort |
| `0x08` | `MV_STATUS` | `[0]` busy, `[7:4]` fault, `[47:16]` moves completed |
| `0x10` | `MV_HDR` | `[0]` sel, `[43:4]` base, `[46:44]` ndim |
| `0x18` | `MV_DIM_A` | `[0]` sel, `[3:1]` dim, `[19:4]` count, `[51:20]` stride |
| `0x20` | `MV_DIM_B` | `[1:0]` axis, `[17:2]` astep -- **writing this commits the dim** |
| `0x28` | `MV_AXIS` | `[0]` sel, `[1]` which, `[17:2]` abase, `[33:18]` aext |
| `0x30` | `MV_IDX` | `[39:0]` index vector base, `[55:40]` index count |
| `0x38` | `MV_SEED` | `[63:0]` PRNG key -- see [`prng.md`](prng.md) |
| `0x40` | `MV_IMM` | `[31:0]` fill / padding immediate, in the element dtype |
| `0x48` | `MV_CMD` | `[39:0]` command-block address, `[40]` fetch-and-go |

`sel` is 0 for the source descriptor and 1 for the destination.

A dimension is programmed as `MV_DIM_A` then `MV_DIM_B`; the second write
commits both into `mx_tdesc`'s one-dimension-per-cycle loader. Dimension
`ndim-1` is innermost, matching `mx_tdesc.v`.

### 2.1 `mode`

| value | mode | source | needs |
|---|---|---|---|
| 0 | `COPY` | descriptor | both descriptors |
| 1 | `TRANSPOSE` | descriptor | both, routes via the granule buffer |
| 2 | `GATHER` | index buffer | dst descriptor, `MV_IDX`, row pitch in src dim 0 |
| 3 | `GENERATE` | PRNG | dst descriptor, `MV_SEED` |
| 4 | `FILL` | `MV_IMM` | dst descriptor |

### 2.2 `ewidth`

| value | width | note |
|---|---|---|
| 0 | 8 bit | INT8 masks and indices |
| 1 | 16 bit | FP16, the common case |
| 2 | 32 bit | FP32, and 32-bit indices |
| 3 | reserved | faults |

**There is no 24-bit width, and there never will be.** E8M15 and ACC24 are
internal formats; `MEMORY_DTYPES` is FP32, FP16 and MXFP7, and a test in the
compiler forbids naming an internal format as a memory dtype. A mover that could
write 24-bit words would silently reintroduce the bug that rule exists to stop.

### 2.3 `flags`

| bit | name | effect |
|---|---|---|
| 0 | `QUANT` | quantise FP16 -> MXFP7 on the write path |
| 1 | `NOTIFY` | raise the completion level for the agent |
| 2 | `NORMAL` | `GENERATE` produces N(0,1) rather than U[0,1) |

## 3. The command block

Twenty register writes per move is fine for bring-up and wrong for production.
`MV_CMD` points at a **128-byte block** -- four 256-bit beats, one burst -- that
the mover fetches itself and which carries the same fields:

```
  beat 0   [63:0]   MV_CTRL image      [127:64]  MV_IDX image
           [191:128] MV_SEED           [223:192] MV_IMM
  beat 1   src header, then src dims 0..2        (one 70-bit record each)
  beat 2   src dims 3..5, dst header
  beat 3   dst dims 0..5
```

A block is self-contained: fetching it is one read, and nothing about a move
depends on register state left behind by the previous one. That property is what
makes a queue of moves safe to build later.

**Command blocks are the compiler's target.** [`compiler.md`](compiler.md) §3
emits them into a region like any other constant.

## 4. Worked encodings

### 4.1 Tile order to entry order

The relayout band, deleted. Both layouts are built from 256-bit words, so this
is word granular and runs at bandwidth (`arch.md` §5).

```
  MV_CTRL   mode=COPY ewidth=16
  src       base=drained_tile   dims = the sub-tile walk
  dst       base=fill_region    dims = the entry walk
```

### 4.2 KV append, `(H,1,D)` into `(H,Lmax,D)`

```
  MV_CTRL   mode=COPY ewidth=16
  src       base=K_out    ndim=2   d0 (count=H, stride=D)      d1 (count=D, stride=1)
  dst       base=cache+t*D ndim=2  d0 (count=H, stride=Lmax*D) d1 (count=D, stride=1)
```

### 4.3 Embedding lookup, `table[idx]` for T tokens of C channels

```
  MV_CTRL   mode=GATHER ewidth=16
  MV_IDX    base=idx_region  count=T
  src       base=table   d0 stride = C     (the row pitch)
  dst       base=out     ndim=2  d0 (count=T, stride=C)  d1 (count=C, stride=1)
```

### 4.4 Zeroing a region, and `pad`

```
  MV_CTRL   mode=FILL ewidth=16     MV_IMM = 0
  dst       the region
```

`pad` proper does not need this: give the **source** a bound axis whose extent
is the unpadded shape, and `valid` low injects `MV_IMM` at exactly the padded
positions. `FILL` is for clearing a whole region.

## 5. What faults

`MV_STATUS.fault`, and the move stops rather than truncating.

| code | meaning |
|---|---|
| 1 | gather count exceeds the index buffer |
| 2 | a descriptor walked outside its declared region |
| 3 | AXI slave error |
| 4 | mode and descriptors disagree (§2.1's "needs" column) |
| 5 | `ewidth == 3` |

Code 2 is the important one: the compiler owns allocation, so a descriptor
leaving its region is a compiler bug, and this is the last place it can be
caught before it corrupts a tensor with no exception.

## 6. What is deliberately absent

- **No 24-bit element width** (§2.2).
- **No accumulate.** Scatter-add is read-modify-write; see `README.md` §4.
- **No compute.** No scale, no bias, no activation. The one transform on the
  path is `QUANT`, and only because the quantiser already exists in MAG.
- **No indirect destination.** Gather reads by index and writes linearly.
  Scatter is the mirror and is a separate mode when it lands, not a flag.
