# Tensor Core ISA

The instruction set a **cluster manager** executes, so that one cluster can be
handed a large matmul — or a convolution — as a small program rather than a
stream of per-tile commands.

Design intent for the datapath is [`matmul.md`](matmul.md); the accumulator is
[`accumulator.md`](accumulator.md). This document is about everything *around*
the datapath.

> **This is the agreed design, not what runs.** The descriptor walker is built
> and its conv2d im2col addressing is validated (`mx_tdesc.v`,
> `tests/matmul/mx_tdesc_tb.v`), but it is **not wired into the fill engine**.
> What a cluster executes today is three opcodes — `FILL` takes a base address
> and an entry count, `GEMM` takes a tile shape, `DRAIN` writes it out — and the
> addressing that a descriptor would have generated is computed by the driver
> instead. See [`../isa/cluster.md`](../isa/cluster.md) for the built ISA and
> [`../isa/kernel.md`](../isa/kernel.md) for where the addressing went.
>
> The trade is deliberate and worth naming: the driver can compute any address a
> descriptor could, so nothing is *unreachable* today — but it must unroll,
> which is what makes a program grow with the problem and forces rounds. A
> descriptor moves that loop into hardware. It is the right next step for
> convolution, where the address pattern is genuinely too irregular to unroll
> cheaply; it is not needed for GEMM, which is why it has not been.

> **The hard part is the memory instructions, not the control of the TCUs.**
> Sequencing a 4×32×4 chain is a pair of nested counters. Deciding which bytes
> of DRAM constitute "the A tile" is where the generality lives — and it is
> where convolution comes from for free.

---

## 1. Why a manager exists

A cluster consumes `A[4][32]` + `B[32][4]` per cycle. Delivered as 256-bit
operand words that is **8 words per cycle**; one NoC port delivers **1**. Five
NoC ports would still be 8× short, and would spend five router ports being
short.

The gap is closed by reuse, not by bandwidth. A cluster that computes a
`Gm × Gn` block of output sub-tiles from operands held locally needs

```
   words/cycle from the NoC  =  4 (Gm + Gn) / (Gm · Gn)
```

which is **1.0 at Gm=Gn=8** and **0.375 at 16×32**. So the cluster needs local
storage and something to manage it — and that something must also decide *what
to fetch*, because only it knows the tiling.

```
   NoC <--> cluster manager <--> tcu -> tcu -> tcu -> tcu --> acu <--> NoC
             program cache            direct DSP cascade       resident
             descriptors              (PCOUT/PCIN + W)         output tile
             L2 / L1
```

**Two NoC ports per cluster**, not five: operands in at the manager, results and
peer traffic out at the ACU. For 32 clusters that is 64 ports instead of 160.

## 2. L1 is not a cache

There is no tag array, no hit/miss, no eviction policy. The manager **owns** its
local storage and fills it explicitly, because it is the only thing that knows
the loop structure. `FILL` is an instruction, not a side effect of a miss.

This is the whole reason the memory ISA has to be expressive: a transparent
cache would paper over the access pattern, and the access pattern is exactly
what we want to describe precisely enough to cover convolution.

---

## 3. The memory model

### 3.1 Tensor descriptors

A descriptor is an **N-dimensional affine address generator with bounds**. Eight
of them live in a descriptor file.

```
   TD[d]:
     base          40-bit byte address in DRAM
     ndim          1..6
     for i in 0..ndim-1:
        count[i]   16-bit   iterations of this loop level
        stride[i]  32-bit   signed bytes advanced per iteration
        axis[i]    2-bit    which bound axis this dim contributes to (0 = none)
        astep[i]   8-bit    signed contribution to that axis per iteration
     for a in 1..2:
        abase[a]   16-bit signed    axis origin (negative = padding)
        aext[a]    16-bit           axis extent
```

Address generation walks the dims as nested loops, innermost last:

```
   addr    = base + SUM over i of  idx[i] * stride[i]

   axis[a] = abase[a] + SUM over i where axis[i]==a of  idx[i] * astep[i]
   valid   = for all a:  0 <= axis[a] < aext[a]

   !valid  ->  deliver ZEROS, issue no memory request
```

Two things earn their keep here. **Signed strides** let a descriptor walk
backwards, which is what a transposed or reversed operand needs. **Bound axes**
express padding as a property of the address generator, so a padded convolution
needs no border handling anywhere else in the machine — the out-of-range
elements simply never become memory requests, and zeros are injected in their
place.

### 3.2 Why this is exactly convolution

For `conv2d` with input `[N][H][W][C]`, filter `[KH][KW][C][F]`, stride `S`,
padding `P`, the im2col matmul is

```
   M = N·OH·OW      K = KH·KW·C      N_dim = F
```

and the activation element at row `(n, oy, ox)`, column `(ky, kx, c)` is

```
   input[n][oy·S + ky - P][ox·S + kx - P][c]
```

which is affine in six loop indices. So it is **one descriptor**:

```
   dim   i=0    i=1     i=2     i=3    i=4    i=5
         n      oy      ox      ky     kx     c
   count N      OH      OW      KH     KW     C
   stride sN    S·sH    S·sW    sH     sW     sC
   axis   -     1 (H)   2 (W)   1 (H)  2 (W)  -
   astep  -     S       S       1      1      -

   abase[1] = -P   aext[1] = H          abase[2] = -P   aext[2] = W
```

No im2col buffer is materialised, no data is duplicated, and padding falls out
of `valid`. **A convolution is a matmul with a more interesting descriptor** —
which is the property this ISA is built around.

The same mechanism covers strided/dilated convolution (change `astep`),
grouped convolution (add a dim), and transposed operands (negative `stride`).

### 3.3 Operand word format

`FILL` does not copy raw bytes; it assembles the 256-bit operand words the TCU
consumes, so the layout transform happens once, on the way in.

```
   A word   4 rows x 8 K-elements                    B word   8 K x 4 columns
   +---------------------------------+--------+     +---------------------------------+--------+
   |     32 x MXFP7  (224 bit)       | 4xE5M3 |     |     32 x MXFP7  (224 bit)       | 4xE5M3 |
   +---------------------------------+--------+     +---------------------------------+--------+
    255                            32 31     0       element (k,j)          scale per column j
```

`FILL.A` and `FILL.B` differ only in which loop index is the fast one, so both
are the same engine with a transposed emission order.

---

## 4. The memory hierarchy, and why each level is a different primitive

Three levels, three shapes, three primitives. Every one of them is **explicitly
instantiated** with the primitive named — see
[`../simulation.md`](../simulation.md) on why inference is not acceptable here.

```
   NoC  --0.375 word/cyc-->  L2 (URAM)  --96 bit/cyc-->  L1 (LUTRAM)  --2048 bit/cyc--> chain
                             deep, narrow                wide, shallow
```

| level | holds | shape | primitive | cost/cluster |
|---|---|---|---|---|
| **L2** | many K blocks of A and B | deep, ≤72b wide | **URAM288** | ~4 URAM |
| **L1** | one K block, double-buffered | 1024b wide, 96 deep | **LUTRAM** | ~2,048 LUT |
| **ACU tile** | 512 output sub-tiles | 352b wide, 512 deep | **BRAM36** | 5 BRAM36 |

The L1 width is what forces LUTRAM. Per cycle the chain wants four A words and
four B words; the four A words are the four K-slices of one row group, so they
are naturally **one 1024-bit L1 entry**:

```
   A entry (1024 bit) = kw0 | kw1 | kw2 | kw3   for row group g
   B entry (1024 bit) = kw0 | kw1 | kw2 | kw3   for col group h
```

With the sweep's `h` innermost, B is read every cycle and A is held in a
register for `Gn` cycles. One 1024-bit read per cycle at depth ~96 is a LUTRAM
shape; in BRAM a 1024-bit port costs 15 BRAM36 regardless of depth, and would be
2% utilised.

Per K block, L1 holds `Gm + Gn` entries — for 16×32 that is 48 kbit, 96 kbit
double-buffered. That is why the NoC refill rate is 192 words per 512 cycles:
**0.375 word/cycle, the number §1 predicted.**

---

## 5. Instruction set

One instruction is one 256-bit NoC payload.

```
 255    252 251                                                           0
+----------+---------------------------------------------------------------+
| opcode 4 |                          operands                              |
+----------+---------------------------------------------------------------+
```

| op | name | meaning |
|---|---|---|
| 0 | `NOP` | |
| 1 | `SETD d, field, value` | write one descriptor field |
| 2 | `FILL d, dst, kind` | run descriptor `d` into L2 or L1; `kind` = A / B / raw |
| 3 | `GEMM a, b, c, Gm, Gn, NK, flags` | sweep a block of output sub-tiles |
| 4 | `DRAIN d, c, n` | write `n` sub-tiles of the ACU tile out via descriptor `d` |
| 5 | `SYNC mask` | wait until the named fill engines are idle |
| 6 | `LOOP n` / `ENDL` | hardware loop, 4 deep |
| 7 | `DONE code` | raise `SIG_INST_COMPLETE` with a status code |

### 5.1 `GEMM` — the sweep, and why K is the outer loop

```
   GEMM a_base, b_base, c_base, Gm, Gn, NK, {FIRST, LAST}

   for kb in 0 .. NK-1:                      <-- K OUTER
     for g in 0 .. Gm-1:
       for h in 0 .. Gn-1:                   <-- sub-tile INNER
         chain <- A_entry[a_base + g][kb], B_entry[b_base + h][kb]
         acu   <- (kb == 0 && FIRST) ? LOAD : ADD   at tile address g*Gn + h
```

Putting K outside is not a scheduling preference, it is what makes the
accumulator cheap:

```
   K inner (old)   same tile address on back-to-back cycles
                   -> pipelined adder cannot close the loop
                   -> 3 banks, a fold on EMIT, a zero mask

   K outer (this)  the same address is revisited every Gm*Gn cycles
                   -> 512 cycles of slack at 16x32
                   -> ONE bank, no fold, no mask, and BRAM's
                      synchronous read is absorbed for free
```

That single reordering deletes the whole banking mechanism described in
[`accumulator.md`](accumulator.md) §4.2–4.3, and is what lets the resident tile
be BRAM instead of LUTRAM.

The tail case is a sweep *shorter* than the pipeline (a 1–2 sub-tile remainder
block). Those must either stall or keep a 2-bank fallback; see §8.

### 5.2 Multiple operands in flight

Three engines run concurrently and are ordered only by `SYNC`:

```
   fill A   descriptor walk -> L2 -> L1 buffer ^b
   fill B   descriptor walk -> L2 -> L1 buffer ^b
   sweep    GEMM over L1 buffer b, while ^b is being refilled
```

Double-buffering L1 is what keeps the chain fed: `FILL` for block `kb+1` runs
during the 512 cycles that `GEMM` spends on block `kb`. `SYNC` before switching
buffers is the only ordering the program must state.

---

## 6. Worked examples

### 6.1 `C[32,32] = A[32,32] · B[32,32]`

Gm=8, Gn=8, NK=1 — 64 sub-tiles, one K block.

```
   SETD  0, {A: base, 2 dims, count=(8,4), stride=(4*32, 32)}
   SETD  1, {B: base, 2 dims, count=(8,4), stride=(4*32, 32)}
   FILL  0, L1.A, kind=A
   FILL  1, L1.B, kind=B
   SYNC  A|B
   GEMM  0, 0, 0, Gm=8, Gn=8, NK=1, FIRST|LAST
   DRAIN 2, 0, 64
   DONE  0
```

8 instructions replace the 128 that `mx_system32_tb` stages today.

### 6.2 `C[64,128] = A[64,K] · B[K,128]`, the balanced shape

Gm=16, Gn=32 → 512 sub-tiles → `DEPTH=512`, one BRAM-backed ACU tile.

```
   LOOP  NK                       ; K blocks
     FILL  0, L1.A^b, kind=A      ; overlaps the GEMM below
     FILL  1, L1.B^b, kind=B
     SYNC  A|B
     GEMM  ..., Gm=16, Gn=32, NK=1, (first iteration ? FIRST : 0)
   ENDL
   DRAIN 2, 0, 512
```

512 compute cycles per K block against 192 operand words — the port is 37.5%
occupied, which is the headroom the vector and general units need.

### 6.3 `conv2d` — the same `GEMM`, one descriptor

```
   SETD  0, {6 dims as in s3.2, axis bounds = H and W, abase = -P}
   SETD  1, {filter: 2 dims, count=(F/4, KH*KW*C/8), stride=(...)}
   FILL  0, L1.A, kind=A          ; im2col happens in the address generator
   FILL  1, L1.B, kind=B
   GEMM  ..., Gm=OH*OW/4, Gn=F/4, NK=KH*KW*C/32, FIRST|LAST
```

The compute instruction is **byte-identical to the matmul case**. Only the
descriptor changed.

---

## 7. Where quantisation happens

The machine is AMP FP16-MXFP7 ([`matmul.md`](matmul.md) §3.0): FP16 in memory,
MXFP7 in the multiplier. Something must convert, and with the manager owning its
own fills there is now a natural home for it.

```
   DRAM (FP16) --> [ FILL engine: max-tree over 32 -> E5M3, shift+round -> MXFP7 ] --> L1
```

Putting it in the fill path means L1 and L2 hold MXFP7, so both are 2.3× denser
than holding FP16, and the conversion cost is amortised over the reuse factor
(each element is quantised once and used `Gn` or `Gm` times).

The cost is a 32-element max-tree plus a shift/round per element on the fill
path. At 0.375 word/cycle the fill path is nowhere near critical, so this is the
cheap place to put it.

> Not yet built. `FILL` currently assumes memory already holds MXFP7, which is
> what both system benches preload. See [`../system.md`](../system.md) §4.

---

## 8. Open questions

```
   tail sweeps        a block smaller than the pipeline depth still needs
                      either a stall or a 2-bank fallback in the ACU
   descriptor depth   6 dims covers conv2d; conv3d and grouped+dilated
                      together may want 8
   L2 fill policy     FILL is explicit, but who decides the *program* --
                      host, or a loop over descriptors in the manager?
   peer reduction     only needed when M*N < 262,144 and there are not
                      enough output tiles to fill 32 clusters
   scale layout       E5M3 scales are replicated per K-slice today; a
                      descriptor could gather them separately instead
```

## 9. Status

Nothing here is implemented yet. This document is the design agreed before
building, in this order:

```
   1  this ISA                                      <- you are here
   2  mx_cluster_mgr + bench against {chain, acu}   L1 preloaded, program cached
   3  NoC <-> mgr <-> chain <-> acu <-> NoC
   4  full system test through the orchestrator
```

The existing single-node `mx_matmul_cu` (12,973 LUT, 306.4 MHz, 256 DSP, from a
300 MHz-target run and not re-measured since) stays as the measured baseline to
compare the 2-port cluster against. The 2-port cluster it is compared with now
measures 17,521 LUT, 325.6 MHz and 272 DSP — [`timing.md`](timing.md) §1.
