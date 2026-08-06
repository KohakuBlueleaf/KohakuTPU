# 1×5 system: a real matmul end to end

The smallest complete machine — one router, three nodes — running an actual
matmul with nothing stubbed but DRAM.

```
                        [ matmul CU (1,0) ]
                                 |  north
     west --  +----------------------------------+  -- east -- [ fake mem (2,1) ]
              |          router (1,1)            |
              +----------------------------------+
                                 |  local
                        [ orchestrator (1,1) ]
                                 |  AXI4
                              (host)
```

Two benches, run by `tests/run_system_sim.ps1`, each against both the
behavioural model and the real DSP48E2:

| Bench | Shape | Covers |
|---|---|---|
| `mx_system_tb` | `C[4,4] = A[4,256]·B[256,4]` | **depth** — one sub-tile accumulated across 8 blocks of K=32 |
| `mx_system32_tb` | `C[32,32] = A[32,32]·B[32,32]` | **breadth** — 64 sub-tiles, 128 instructions, 1,024 elements |
| `mx_mesh2x2_tb` | `C[32,64] = A[32,128]·B[128,64]` | **the machine** — 2×2 mesh, two 2-port clusters, 4 K blocks |

The first two drive `mx_matmul_cu`, the single-node baseline. The third is the
real architecture and has its own section below.

---

## 1. The path being exercised

```
   host stages a program over AXI
     -> orchestrator dispatches CU_INST across the mesh
     -> CU issues MEM_RD_REQ naming ITSELF as src
     -> memory replies MEM_RD_RESP straight to the CU
     -> cluster computes 4x32x4 per block, ACU accumulates
     -> CU issues MEM_WR_REQ with the FP16 result
     -> CU emits CU_SIGNAL, orchestrator mirrors it into NODE_STATUS
     -> host polls over AXI and reads the result back
```

The operand fetch is the part worth noticing. The CU names **itself** as `src`
in `MEM_RD_REQ`, so the response is delivered directly to it and never passes
through the orchestrator. That is what lets the orchestrator have no AXI master
at all ([`noc/spec.md`](noc/spec.md) §10.3) — it forwards instructions and never
touches operand data.

## 2. The problems

### 2.1 Depth — `mx_system_tb`

`C[4,4] = A[4,256] × B[256,4]`, as 8 blocks of K=32, with **per-block per-row
and per-column E8M0 scales**. So this exercises microscaling, not merely a
matmul: 8 different scale pairs are applied and accumulated into one resident
sub-tile.

Program: 8 `BLOCK` instructions and one `EMIT`.

```
   blocks=8  emits=1   memory reads=16  writes=1
   NODE_STATUS: code=01 (BATCH_COMPLETE)  signals=9
   35 checks, 0 errors
```

16 reads is exactly right: 8 blocks × (4 flits of A + 4 of B) delivered as two
burst requests each.

### 2.2 Breadth — `mx_system32_tb`

`C[32,32] = A[32,32] × B[32,32]`. K=32 is exactly one quantisation block, so
every one of the 64 output sub-tiles is a single `BLOCK` followed by an `EMIT` —
**128 instructions in one staged program**, producing 1,024 output elements.

This is the first bench to reuse the resident tile file: slots cycle 0..15 and
wrap four times, so every slot is loaded, emitted, and then loaded again. That
is the path where a stale accumulator bank would show up as a doubled result.

```
   blocks=64  emits=64   memory reads=128  writes=64
   NODE_STATUS: code=01 (BATCH_COMPLETE)  signals=128
   2,051 checks, 0 errors        (identical under MODEL=0 and MODEL=1)
```

The top-left corner of the result, hardware against FP64:

```
   hardware (FP16 read back out of the mesh)
        12.40    -73.38    174.50    -62.22    -28.02     28.09
       219.75   -200.25   -492.00   -159.25    226.50   -197.00
       104.38   -526.00  -1338.00    103.00     94.00   -106.69
      -110.00    246.00   -151.00   -180.88     67.19   -446.75

   FP64 ground truth
        12.40    -73.34    174.53    -62.23    -28.02     28.09
       219.78   -200.25   -491.88   -159.25    226.44   -197.03
       104.38   -525.75  -1338.00    103.00     94.00   -106.69
      -109.97    246.00   -151.00   -180.88     67.19   -446.84
```

### 2.3 The machine — `mx_mesh2x2_tb`

Orchestrator, a 2×2 mesh, **two 2-port clusters**, and memory. This is the first
bench of the architecture rather than the single-node baseline.

```
          x=0    x=1    x=2    x=3
   y=0     .      .      .      .
   y=1    ORC -- [R] -- [R] --  .       [R] = router, grid 1..2
                  |      |
                 mgr0   acu0
   y=2    MEM -- [R] -- [R] --  .
                  |      |
                 mgr1   acu1
   y=3     .      .      .      .
```

**Each cluster spans two routers** — manager on one local port, accumulator on
another router's local port. Sharing one router would put operand fetch and
result write-back through the same buffers and arbiter, which is exactly the
contention the two-port split exists to remove.

The orchestrator and memory are *off-grid* coordinates on west edge ports. That
works because the router routes to the **clamped** destination first and only
then uses the unclamped coordinate to pick an edge port, so a packet for (0,2)
travels to router (1,2) and exits west.

`C[32,64] = A[32,128] × B[128,64]`, **split by output**: cluster 0 takes columns
0–31, cluster 1 takes 32–63, and each sweeps the whole of K itself. No peer
reduction is needed — K inside a cluster is free, whereas splitting K across
clusters costs a reduction that dominates the compute
([`compute/tensor-isa.md`](compute/tensor-isa.md)).

Each cluster runs a **four-instruction program**: `FILL A`, `FILL B`, `GEMM`,
`DRAIN`. The manager issues its own `MEM_RD_REQ`s to fill L1; the accumulator
writes results back through its own port.

K=128 is four quantisation blocks, which is the point: with a single block every
accumulator command is a `LOAD` and the cross-block accumulate path is never
exercised. Four blocks means `LOAD` then three `ADD`s into each of 64 resident
sub-tiles, each with a different E8M0 scale pair.

```
   cu0  fills=2 gemms=1 drains=1        cu1  fills=2 gemms=1 drains=1
   memory: reads=128  writes=128
   4,096 checks, 0 errors               identical under MODEL=0 and MODEL=1
```

## 3. Precision

Three quantities, and the distinction between them is the point:

| | what it is |
|---|---|
| **EXACT MXFP7** | the matmul as a CPU would compute it, integer arithmetic, block scales applied by shifting |
| **FP64** | the same sum in `real` |
| **HARDWARE** | the FP16 written back to memory |

```
   4x256x4           worst  3.97e-4                0.41 FP16 ULP

   32x32x32          worst  4.86e-4 at C[0][18]    0.50 FP16 ULP
                     mean   1.41e-4 over 1,024     0.14 FP16 ULP

   32x128x64, 2 CU   worst  2.29e-3                2.3  FP16 ULP
                     mean   1.71e-4 over 2,048     0.18 FP16 ULP
```

**Half an FP16 ULP at worst for a single K block**, across 1,024 elements and
the whole machine. One FP16 ULP is 9.77e-4, so the accumulator is not the
limiting factor there — the output format is.

The four-block case is worth reading carefully: the **mean is unchanged** at
0.18 ULP, but the worst case rises to 3 ULP. That is not accumulator drift —
it is **cancellation**. Four blocks with independent scale pairs are summed, and
where the final value is small relative to the intermediate terms, the relative
error of the result is amplified by the ratio between them. It is a property of
the problem, not of the hardware, and it is why the mean matters more than the
maximum for judging the accumulator.

Both benches assert that EXACT and FP64 agree with each other *before* comparing
either against hardware. Without that, a drifting model would be
indistinguishable from a hardware error.

What this error does **not** include: quantisation. The operands are already
MXFP7 in memory, which is where MAS would have quantised them. The numbers above
are purely what the FP22 accumulator and the FP16 emission cost.

## 4. What the benches do not yet cover

```
   MAS quantiser        operands are preloaded already-quantised
   peer accumulation    one cluster, so ACC_SEND / ACC_ADD_PEER are unused
   multiple CUs         one compute node on the router
   backpressure         no congestion; the mesh is never contended
   MEM_WR_ACK           the CU retires on send rather than waiting for the ack
```

The fake memory (`tests/noc/noc_fake_mem.v`) implements the wire protocol of
[`noc/spec.md`](noc/spec.md) §5.1/§5.2 and nothing else — no cache, no reordering, no quantiser. That is
deliberate: a system test should fail because the system is wrong, not because
the stub grew its own bugs.

## 5. Bugs these tests found

**Two continuous drivers on a mesh link.** The 2×2 bench tied off "all unused
north ports" in a generate loop, which also drove `n_in[0][1]` — the (0,0)→(0,1)
vertical link — alongside a cluster's accumulator port. The link resolved to X.
Cluster 1 never noticed, because its route to memory is a single westward hop;
cluster 0 routes **west then south straight through it**, so its memory requests
vanished and it hung with no error anywhere. Edge ports are now tied off one by
one. **No single-cluster test could have found this** — it needs two nodes whose
routes differ.

**`gemm_busy` reported done when the last tile was *issued*.** The cascade is
~19 cycles deep, so results were still in flight; the CU signalled completion,
`DRAIN` seized the accumulator control mux, and the tail sub-tiles came back as
zeros — 11 of 64 per cluster. Now gated on the ACU command FIFO being empty,
which is exact and needs no latency constant.

**A synchronous L1 read needs two cycles of control delay, not one.** Counters
assign the address at T, the RAM sees it at T+1, data is valid at T+2. Consuming
it at T+1 shifted every result by one sub-tile — structured and silent, and it
looked like an addressing bug rather than a timing one. This is exactly the
latency that memory *inference* hides.

**The accumulator's reuse contract caught a real violation the moment it
existed.** Making the tile single-bank turned "commands to the same address must
be ≥5 cycles apart" from a structural guarantee into a requirement on the
caller, so it is checked in simulation. It immediately flagged `mx_matmul_cu`,
which emits a tile 2–3 cycles after the last accumulate into it. That CU now
waits out the distance; the two-port cluster never violates it, because sweeping
K outermost puts `Gm·Gn` cycles between reuses by construction.

> Worth noting what *kind* of bug that is. Nothing had failed — the banked
> accumulator served the pattern correctly. The check exists so that removing
> the banks could not silently break a caller, and it earned its keep on the
> first run.

**The staging window was one 4 KB page, not `STAGE_FLITS`.** The orchestrator
decoded a stage write as `waddr[15:12] == 4'h2`, i.e. 0x2000–0x2FFF = 512
words = **102.4 flits**, while `STAGE_FLITS` defaults to 128 and the RAM is
sized for it. Everything past flit 102 was decoded as a register write instead
and silently discarded.

The failure looked nothing like an address bug: the program simply stopped, at
exactly 51 of 64 sub-tiles, with `run=1 left=10 credit=0` — which reads like a
credit deadlock. Re-running with 4 credits instead of 16 stopped at 51 again,
which is what ruled flow control out: a rate problem moves, a decode boundary
does not. 51 pairs is 102 instructions, and instruction 102 is the first whose
five staged words straddle 0x3000.

The window is now derived from `STAGE_WORDS`. **Nothing shorter than 103 flits
could have found this**, and the previous longest bench was 9.

**Unsized literals in the instruction encoding.** `(WA_BASE + b*4) * 32` written
straight into a concatenation contributes 32 bits, not the 34 the address field
is, so the payload came out 4 bits short and every field below it shifted. The
CU then read nonsense and executed nothing. Symptom: `blocks=0`, no memory
traffic, no error anywhere. Same trap as [`simulation.md`](simulation.md) §3. The 32×32×32
bench hits it again with the 4-bit tile field, which is why `tile4` is an
explicitly sized reg.

**A fixed-cycle wait for `emit_valid`.** The CU counted 10 cycles after issuing
`EMIT` rather than waiting on the flag. Deepening the accumulator pipeline made
10 too few, and the CU wrote a zero result while every unit test still passed.
See [`compute/accumulator.md`](compute/accumulator.md) §5.

**AXI write handshake.** Waiting for `awready && wready` together deadlocks —
the orchestrator's write FSM takes AW first, then W, so they are never both
high.
