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

**`ACC24` was intended as the matmul interface** -- the cluster accumulator's own
format, loaded so a vector core could finish a split-K reduction without a lossy
trip through FP16 ([vector-core.md](../compute/vector-core.md) §11).

> **Split-K is real, and it does not go through memory.** `mx_acu_fp.v` has
> peer transfer: `peer_in`/`peer_out` are `16*(ACC_MW+8)` bits -- one 4x4
> sub-tile at the accumulator's own width, FP22 at the default `ACC_MW=14` and
> FP24 at 16 -- and `OP_ADD_PEER` adds the incoming partial to the resident
> tile. **Clusters reduce split-K over the MESH, in the accumulator, without a
> trip through FP16.** Three widths are therefore in play and they are not the
> same list:
>
> | | |
> |---|---|
> | **memory** | FP32, FP16, MXFP7 (read-only). **Never 24-bit.** |
> | **mesh** | FP32, FP16, and the accumulator width for peer transfer |
> | **internal** | E8M15 in the lane, the resident tile in the accumulator |
>
> **What is unresolved is only this field.** A DRAIN writes FP16
> ([cluster.md](cluster.md) §5: one 256-bit word per 4x4 sub-tile is 16 elements
> at 16 bits, with no room for 24) and stage 6 converts on EMIT, so a partial in
> memory is FP16. `VLD.ACC24` therefore needs the vector core to receive a peer
> transfer directly -- not a memory format. **The mesh path now exists** (§5.1),
> but it lands raw 256-bit words in L1: it moves whatever the sender put in the
> flit, so it carries ACC24 only once a sender emits ACC24 and `VLD` learns to
> read it. Neither is true yet. `E8M15 (raw)` has no such route at all: it is the
> lane's own format and `vector-core.md` §1 says software sees FP32 or FP16 and
> nothing else.
>
> The driver used to allocate ACC24 *regions* and emit `dtype: acc24` on a
> DRAIN. That was fiction and is corrected; memory is FP16/FP32 throughout.
> Reducing split-K on a **vector** core still needs a decision: give it a peer
> port, or accept FP16 partials there and keep the wide reduction in the
> accumulator where it already works.

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
| `1E` | `VDRAIN` | start an L1 drain, to memory or to a peer CU (§5.1) | — |
| `1F` | `VHALT`  | signal done to the agent | — |

`VRED` kinds, in the `vc` field: `SUM MAX MIN SUMSQ DOT EXPSUM ANY ALL`.
`SUMSQ` is `a*a+c` in the tree nodes and `DOT` is `a*b+c` — both free from the
node being an FMA rather than an adder.

**`EXPSUM` (kind 5) keeps its elementwise result.** `vd = exp2(va)` written
back per element, *and* `Σ vd` reduced into `S[vd]`, in one pass — the vector
destination rides in the `vb` field, which a unary leaf leaves free. It exists
because softmax is the inner loop of attention and of every normalisation, and
fusing the two halves removes a pass and a `VSETMD`. Capability-gated as
`vec_reduce_writeback`; a core without it **faults** on kind 5 rather than
mis-executing.

Kind 5 previously read `ARGMAX` in this table, but ARGMAX was never implemented
— the decoder raised `F_OPCODE` on it — and it does not fit the shape anyway,
returning an index where every other kind returns a value. 5 was the only free
encoding in a 3-bit field.

The subtract in `exp2(a - m)` is **not** fused: that would be a two-stage leaf,
and sixteen ALUs cannot hold both a chain and a tree — see
`docs/compute/vector-core.md` §7.3a. Subtract the row max in a preceding FLAT
pass, as before.

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

### 5.1 Peer transfers: `CU_DATA` in both directions

L1 is reachable from the mesh, not only from memory. `CU_DATA` is
[`../noc/spec.md`](../noc/spec.md) §5.3 — **type `0x8`, not the `0x4` that table
still prints**; `0x4` is `MEM_WR_DATA` in `vec_cu.v` and `mag_mem_port.v`, and
`src/kohakunoc/noc_pkt.vh` records the resolution.

**Inbound.** A descriptor flit, then `len+1` pure data flits, the last with
`last` set. `offset` is in 32-byte granules, which is exactly an L1 word, so it
is the destination L1 address unchanged. The core has **one flat L1**: `buf_id`
must be 0, and `offset + len` must be inside `L1_DEPTH`. A burst failing either
is dropped whole and faults `F_CUDATA` at the next instruction boundary rather
than wrapping the address and quietly overwriting the scratchpad.

**One burst at a time.** There is one descriptor and one write pointer, so two
senders' bursts interleaved on the wire would merge into each other — their data
flits are indistinguishable by content. This is the same shape as the existing
"one `VFILL` outstanding" rule, and per-sender state is real hardware for a case
nothing generates yet. It is **detected** rather than prevented: flits of one
burst cannot arrive out of order (same source and destination, so the same
dimension-ordered path), which makes a data flit whose source differs from the
open descriptor's conclusive. It faults `F_CUDATA` and drops the rest, and the
burst is not acknowledged.

A peer write is **not** a `VFILL` retirement. `VBAR` and `VHALT` wait on
outstanding *fills*, and nothing here issued a request, so a burst that arrives
mid-kernel does not satisfy a barrier and does not disturb one. Ordering between
a peer's write and the kernel that reads it is the sender's problem, and
`signal_on_complete` is how it is solved: the core answers `SIG_DATA_RECEIVED`
(`0x03`, `arg = buf_id`) once the last word lands, so the agent can stage the
`RUN` behind it.

**Where that answer goes is `[215:208]` of the descriptor, `{ack_y, ack_x}`.**
Zero means the descriptor's source, which is what a host-originated burst uses —
the dispatcher stamps itself as the source, so the ack lands in
`NODE_STATUS[receiver]` where the host can see it. Zero is unambiguous because
`(0,0)` is a mesh **corner**: it touches no router and can never hold an
endpoint.

A CU-to-CU burst must set it. Left at zero the ack goes to the *sending CU*,
where nothing consumes it — `vec_cu` drops any flit that is neither a read
response nor `CU_DATA` — and the host has no way to sequence a reader behind the
writer. Pointing it at the orchestrator keeps the payload on the mesh and puts
only the completion where the sequencer can see it.

**Outbound.** `VDRAIN` gains a sink. The bits it did not read are the overlay,
and every encoding written before this one leaves them zero — which reads as
`to_node = 0`, memory, so nothing needs re-encoding:

```
  31    27 26 25 24 23  21 20  17 16  13 12   9  8                  0
 ┌────────┬──┬──┬──┬──────┬──────┬──────┬──────┬────────────────────┐
 │  1E    │- │sg│nd│  ad  │ dst_x│ dst_y│buf_id│  L1 start word     │
 └────────┴──┴──┴──┴──────┴──────┴──────┴──────┴────────────────────┘

  nd  0 sink is memory, A[ad] walks byte addresses -- unchanged
      1 sink is the CU at (dst_x, dst_y), buffer buf_id
  sg  ask that sink for SIG_DATA_RECEIVED when the last word lands
```

With `nd = 1` the descriptor is read differently, because a `CU_DATA` descriptor
covers **one contiguous run**: `A[ad]`'s *base* is the destination offset in
32-byte granules and its *bounds* are the count. A strided `A[ad]` is not a peer
drain — the sink would read the words as consecutive. The L1 side of the walk is
unchanged: `L1 start word` and upward, one word per flit, capped at 256 by
`F_LEN` as before.

The base carries the fields the instruction word has no room for — it has a
single bit left and the base has eighteen spare:

```
  A[ad].base   [33:26] fin   [25:24] mesh   [23:16] {ack_y, ack_x}   [15:0] peer L1 word
```

So a peer drain that the host must observe is
`desc(ad, ack_y << 20 | ack_x << 16 | peer_word, dim(1, n))`.

**`fin` and `mesh` make the drain remote.** `dst_x`/`dst_y` in the instruction
word then address **this** mesh's MAG port, so the local routers see an ordinary
local flit and the NoC never learns another mesh exists; `fin` is `{fin_y,
fin_x}` in the destination mesh and rides the flit's `txn` field, `mesh` rides
`NOC_RSVD` as `{1'b1, mesh}`, and **both are on every flit of the burst** because
MAG's encapsulator is stateless. `fin` nonzero is the only thing that makes it
remote — `(0,0)` is a mesh corner and can hold no endpoint, exactly as for
`{ack_y, ack_x}` — so every encoding written before the interlink leaves both
zero and reads as a local drain.
[`../interlink/paths.md`](../interlink/paths.md) §4 has the far end;
[`../interlink/boundary.md`](../interlink/boundary.md) §4 has what a driver must
not do with these bits on single-mesh silicon, where they alias rather than
fault. The cluster carries the same two values at `CU_INST` `[78:77]`/`[76:69]`
— shared semantics, different bit positions ([cluster.md](cluster.md) §10.3).

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
