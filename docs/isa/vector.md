# Vector ISA

**Design, not built.** The ALU underneath it is built and measured
([vector-core.md](../compute/vector-core.md) §13); the core, the register file,
the L1 and this instruction set are not.

The sixth instruction set, and the first one that can *branch*. The other five
([README.md](README.md)) are all straight-line by construction — the control
program cannot loop, the CU cannot address DRAM, the ACU has no addressing at
all — because each of them describes a fixed dataflow. A vector core does not
have a fixed dataflow: it is the part of the machine that runs *kernels*, and
kernels are written, not generated.

| level | what it expresses |
|---|---|
| agent → vector core | a kernel: instruction stream, hardware loops, predication |

---

## 1. Architectural state

```
  V0 .. V15    vector registers   VL x E8M15 (24-bit), striped over 16 lanes
  S0 .. S15    scalar registers   24-bit, broadcast to every lane
  P0 .. P3     predicate registers  VLMAX bits, one per element
  K0 .. K3     constant registers   24-bit, read-only operands  (0.0, 1.0, -1.0, +1 free)
  A0 .. A7     address descriptors  base + 4 x (stride, bound)
  VL           active vector length, 1 .. VLMAX
  VMODE        lane topology: FLAT / D2 / D4 / TREE
```

**VLMAX = 128**, so a vector register is 8 elements deep per lane. `VL` is a
register rather than a constant so a kernel written once runs on a tail without
a second code path — the same reason RISC-V V has one.

**Striping is by lane, not by block**: lane *i* holds elements *i*, *i+16*,
*i+32*, … That is what makes a lane-local register file work at all (§3), and it
is why `VSHUF` exists — anything that crosses lanes has to say so.

---

## 2. Encoding

One 32-bit word. Every arithmetic instruction has the same shape, because every
one of them is the same FMA underneath
([vector-core.md](../compute/vector-core.md) §3.4).

```
  31    27 26 25 24 23 22 21 20  17 16  13 12   9  8   5  4  3  2  1 0
 ┌────────┬─────┬─────┬─────┬──────┬──────┬──────┬──────┬─────┬───┬───┐
 │ opcode │ sa  │ sb  │ sc  │  vd  │  va  │  vb  │  vc  │ pr  │pm │ - │
 └────────┴─────┴─────┴─────┴──────┴──────┴──────┴──────┴─────┴───┴───┘
      5      2     2     2     4      4      4      4      2    2   1

  sa/sb/sc   operand source, one per port
             00 V  vector register named by va/vb/vc
             01 S  scalar register, broadcast
             10 C  CHAIN -- the previous stage's result (D2/D4/TREE only)
             11 K  constant register named by va/vb/vc[1:0]

  pm         00 unpredicated   01 where P[pr] set   10 where P[pr] clear
```

Three independent source selectors is the whole flexibility budget, and it is
what makes one opcode cover many operations: `VFMA` with `sb=K(1.0), sc=K(0.0)`
is a move; with `sb=S` it is a scale; with `sc=C` it is a chain step.

Loads and stores need an address rather than three operands, so they overlay the
same word:

```
  31    27 26   24 23  21 20  17 16  14 13                        0
 ┌────────┬───────┬──────┬──────┬──────┬──────────────────────────┐
 │ opcode │ dtype │  ad  │  vd  │  pm  │       offset (signed)    │
 └────────┴───────┴──────┴──────┴──────┴──────────────────────────┘

  dtype  000 E8M15 (raw)   001 FP16   010 FP32   011 ACC24 (S1E7M16)
         100 INT8          101 INT16   -- for masks and indices, not arithmetic
```

**`ACC24` is the matmul interface** and the reason the dtype field is not just
"FP16 or FP32": it is the cluster accumulator's own format, and loading it is
what lets the vector core finish a split-K reduction without a lossy trip
through FP16. See [vector-core.md](../compute/vector-core.md) §11.

---

## 3. Opcodes

Thirty-two, five bits, no room and no need for more.

| | op | effect | passes |
|---|---|---|---|
| `00` | `VMOV`   | `vd = a` | 1 |
| `01` | `VNEG`   | `vd = -a` | 1 |
| `02` | `VABS`   | `vd = \|a\|` | 1 |
| `03` | `VADD`   | `vd = a + c` | 1 |
| `04` | `VSUB`   | `vd = a - c` | 1 |
| `05` | `VMUL`   | `vd = a * b` | 1 |
| `06` | `VFMA`   | `vd = a*b + c` | 1 |
| `07` | `VFNMA`  | `vd = -(a*b) + c` | 1 |
| `08` | `VMAX`   | `vd = max(a,b)` | 1 |
| `09` | `VMIN`   | `vd = min(a,b)` | 1 |
| `0A` | `VSEL`   | `vd = P[pr] ? a : b` | 1 |
| `0B` | `VCMPLT` | `P[pr] = a < b` | 1 |
| `0C` | `VCMPGT` | `P[pr] = a > b` | 1 |
| `0D` | `VCMPEQ` | `P[pr] = a == b` | 1 |
| `0E` | `VEXP2`  | `vd = 2^a` | **1** |
| `0F` | `VLOG2`  | `vd = log2(a)` | **1** |
| `10` | `VINV`   | `vd = 1/a` | **1** |
| `11` | `VRSQRT` | `vd = 1/sqrt(a)` | **1** |
| `12` | `VCVT`   | convert between dtypes | 1 |
| `13` | `VRED`   | tree reduce `va` into `S[vd]`, kind in `vc` | VL/16 + tail |
| `14` | `VLD`    | `vd = mem[A[ad] + offset]`, converting from `dtype` | — |
| `15` | `VST`    | `mem[A[ad] + offset] = vd`, converting to `dtype` | — |
| `16` | `VBCAST` | `vd = S[va]`, or `S[vd] = va[0]` | 1 |
| `17` | `VSHUF`  | lane rotate / gather within the register | 1 |
| `18` | `VSETVL` | `VL = min(S[va], VLMAX)`, and return it | — |
| `19` | `VSETMODE` | `VMODE = va` | — |
| `1A` | `VSETI`  | `S[vd] = imm24` (second word follows) | — |
| `1B` | `VLOOP`  | hardware loop: `count = S[va]`, body length `vb` | — |
| `1C` | `VBAR`   | wait until the named `VFILL`/`VDRAIN` has retired | — |
| `1D` | `VFILL`  | start an L1 fill from descriptor `A[ad]` | — |
| `1E` | `VDRAIN` | start an L1 drain to descriptor `A[ad]` | — |
| `1F` | `VHALT`  | signal done to the agent | — |

`VRED` kinds, in the `vc` field: `SUM MAX MIN SUMSQ DOT ARGMAX ANY ALL`.
`SUMSQ` is `a*a+c` in the tree nodes and `DOT` is `a*b+c` — both free from the
node being an FMA rather than an adder.

**The four transcendentals are one pass.** That is the single most consequential
line in this table: on a GPU they are quarter-rate SFU ops, and softmax,
normalisation and every activation are transcendental-bound rather than
FMA-bound.

### 3.1 What is deliberately absent

No integer arithmetic beyond dtype conversion, no bitwise ops, no scatter/gather
with computed indices, no data-dependent branch other than `VLOOP`'s counter. A
wide SIMD array is worst at exactly those, and the budget for them is thousands
of operations per token, not billions
([vector-core.md](../compute/vector-core.md) §12).

---

## 4. Chaining: `VMODE` and the `C` source

`VMODE` picks how the 16 ALUs are wired, and the `C` operand source is what
reads across the wiring.

```
  FLAT   16 x 1     16 results/cycle    every ALU independent
  D2      8 x 2      8 results/cycle    instruction pairs fuse
  D4      4 x 4      4 results/cycle    instruction quads fuse
  TREE   8+4+2+1     16 in -> 1 out     plus a 16-deep accumulator (s5)
```

In `D4`, instructions are issued in groups of four and an operand with source
`C` reads the previous instruction's result **without going through the register
file**. A whole `sigmoid` is one group:

```
  VSETMODE D4
  VMUL   v1, v0, K[-log2e]      ; x * -log2(e)
  VEXP2  v1, C                  ; 2^that
  VADD   v1, C,  K[1.0]         ; + 1
  VINV   v1, C                  ; reciprocal
```

Four ALUs, one pass, no intermediate ever written. In `FLAT` the same sequence
is four passes and four round trips through the register file.

**Chaining is not an optimisation here, it is what makes the core
compute-bound at all** — §6 has the arithmetic.

---

## 5. Addressing

An address descriptor is `base` plus four `(stride, bound)` pairs:

```
  addr = base + sum_i ( idx_i * stride_i ),   idx_i < bound_i
```

Reshape, permute, expand, pad, slice and broadcast are **views, not
arithmetic**. With strides they are all free — permute is a permutation of the
stride list, broadcast is stride 0, pad and slice are bounds and an offset.
Without strides every one of them becomes a physical copy, and something has to
perform those copies.

This is the difference between a kernel language and a fixed function, and it is
worth more than any arithmetic feature in this document.

---

## 6. Why the core is 16 lanes and why chaining is mandatory

A NoC flit is 288 bits — a 32-bit header and a **256-bit payload**
(`src/kohakunoc/noc_pkt.vh`) — and a vector core is a two-port endpoint like a
cluster, so **512 payload bit/cycle**. At FP16 that is 32 elements/cycle.

A flat elementwise op reads two vectors and writes one: **3 elements of traffic
per result.**

```
  bandwidth ceiling   32 / 3  =  10.7 results/cycle
  compute ceiling     16 ALUs =  16   results/cycle
```

**Flat mode is memory-bound**, and adding lanes would not help. Now chain:

| mode | ops per result | ops/cycle at the bandwidth ceiling | bound by |
|---|---|---|---|
| FLAT | 1 | 10.7 | **memory** |
| D2 | 2 | 21.4 | **compute** (16 ALUs) |
| D4 | 4 | 42.7 | **compute** |

**D2 already saturates the ALUs**, and every mode past it is pure margin. So 16
lanes is not a number chosen to look round: it is the point where two ports of
NoC bandwidth and a depth-2 chain meet. Sixteen also makes `8+4+2+1` a tree that
consumes 16 elements per cycle and leaves exactly one ALU as the accumulator.

That is also why the register file and L1 exist at all. Data that stays resident
does not pay the 3-elements-per-result tax, and a kernel that keeps its working
set in `V0..V15` is compute-bound in flat mode too.

---

## 7. A worked kernel: the split-K epilogue

The reason `ACC24` is in the dtype field. `S` clusters have each computed a
partial sum over their slice of K; the vector core adds the partials and stores
once. See [vector-core.md](../compute/vector-core.md) §11 for why this must not
happen in FP16.

```
        VSETI   S0, 512                 ; elements in one output tile
        VSETVL  S0                      ; VL = min(512, 128) = 128
        VSETMODE FLAT
        VLOOP   S1, 4                   ; S1 = tile-chunks, body = 4 words
          VLD.ACC24  v0, [A0 + 0]       ; partial from cluster 0
          VLD.ACC24  v1, [A1 + 0]       ; partial from cluster 1
          VADD       v0, v0, v1
          VST.FP16   v0, [A2 + 0]
```

Every load converts `S1E7M16 -> E8M15` on the way in, which is
**range-lossless** — E8 contains E7 — and costs one rounding of the bottom
mantissa bit. The sum happens with FP32's exponent range, so the ceiling that
FP16 imposes at 65504 is simply not there. Only `VST.FP16` can saturate, and by
then the value is the final one.

For `S > 2` the loads and adds unroll, or `D4` chains four adds into one pass.

---

## 8. Getting instructions in

The vector core is an ordinary NoC endpoint built on `noc_cu_base`, with the
same two ports and the same flit format as a cluster
([cluster.md](cluster.md)). The agent stages a kernel and kicks it exactly as it
stages a `GEMM`; `VHALT` is what a `DRAIN` is.

So nothing above the vector core changes: the control program, the dispatch
registers and the agent are the same five instruction sets they already were.
The vector core is a sixth *consumer*, not a sixth layer.
