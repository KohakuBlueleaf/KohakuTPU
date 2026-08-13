---
title: Four meshes
summary: This chip's mesh grid, what an address means across it, which splits the silicon can take, and which one it cannot.
tags:
  - kohakutpu
  - interlink
  - compiler
  - kernels
---

# Four meshes

KohakuTPU is four meshes on one device, one per SLR, each with its own DDR4
channel ([ship.md](ship.md)). This page is what that means when you write a
kernel: where a value has to live, what crossing costs, and the one split this
silicon will not do.

The framework's view of the same problem — why a mesh axis exists at all, and
what it constrains — is [integrate/multi-mesh.md](../../integrate/multi-mesh.md).
This page is specific on purpose.

---

## 1. What each mesh carries

The four are **not** alike, and a compiler that assumes they are will place a
softmax where no unit can run it:

| mesh | map | clusters | vector cores | SLR | DRAM |
|---|---|---|---|---|---|
| 0 | `mesh_2x2_6+2` | 6 | 2 | SLR0 | `ddr4_2` |
| 1 | `mesh_2x1_6+0` | 6 | **0** | SLR1 | `ddr4_3` |
| 2 | `mesh_2x2_6+2` | 6 | 2 | SLR3 | `ddr4_0` |
| 3 | `mesh_2x2_6+2` | 6 | 2 | SLR2 | `ddr4_1` |

**mesh_1 has no vector core.** SLR1 holds the host interface, so it is the most
crowded die and its mesh gave up the vector cores rather than the clusters. The
consequence is not "mesh_1 is a spare": it is six perfectly good matmul clusters
that can run any stage needing no epilogue, and nothing else. `MachineSpec`
carries a `MeshSpec` per mesh so a placement pass can read that off the unit
table and refuse precisely (`compiler/kohakuaccel/machinespec.py`).

Unit populations come from enumeration over the control plane, not from this
table — read them off the card rather than trusting a document.

---

## 2. The grid, and the two links

The meshes form a **2x2 grid**, with the mesh id read as `{y, x}`. `mag_switch.v`
derives each neighbour by flipping one bit — `peer0` flips x, `peer1` flips y:

```
        link0            mesh 0 --- mesh 1        link0: 0<->1 and 2<->3
      +--------+           |           |          link1: 0<->2 and 1<->3
   0 -+        +- 1        |           |
      |  grid  |         mesh 2 --- mesh 3        diameter 2:
   2 -+        +- 3                                 0<->3 and 1<->2 are two hops
      +--------+
```

Two facts about routing that a kernel author has to design against:

- **A MAG forwards traffic that is nothing to do with its own mesh.** Diagonal
  traffic is two hops, so `0 -> 3` passes through mesh_1 or mesh_2. A transfer
  can therefore be slowed by a mesh it is not addressing.
- **Routing is XY dimension-order on mesh coordinates**, and that is what makes
  the second routing layer deadlock-free. It is the same argument the NoC relies
  on, and it holds only while the mesh-of-meshes is a **grid**. Do not design a
  traffic pattern that assumes a ring's routing; a ring reintroduces the cycle
  the turn model exists to break ([interlink/topology.md](../../../kohaku_npu_docs/interlink/topology.md) §2).

A ring *collective* is still fine — the cycle `0 -> 1 -> 3 -> 2 -> 0` uses only
real links, one hop each. It is a traffic pattern, not a routing change.

**One link spans three SLRs.** A 4-cycle cannot embed in a 4-die stack without
one long edge, and here it is link1 between mesh_0 (SLR0) and mesh_2 (SLR3). That
is what `mag_link_pipe.v` exists for, and it is legal only because the link
protocol is credit-based with no ready travelling back. Expect that edge to be
the slow one.

---

## 3. An address is a mesh and an offset

`interlink.global_addr(mesh, byte_addr, mesh_count)` builds `{mesh[1:0],
local[31:0]}` — 34 bits, 4 GB per mesh, an exact split
(`src/ktpu/hw/interlink.py`).

> **On a single-mesh bitstream a remote address does not fault — it ALIASES.**
> Bits 33:32 are undecoded there, so a transfer meant for another mesh silently
> reads and writes local DRAM at the same offset. `global_addr` refuses it in
> software because the hardware cannot. Never build one by hand with a shift.

Every mesh master — `M_AXI_MEM*`, `UPLOAD`, `MOVER`, `ILINK` — sees only its own
4 GB, at offset 0. The mesh id rides the interlink header, not the local AXI
address, which is why a mesh needs no address-decode change to become one of
four.

> **This is not hypothetical, and it is happening now.** The compiler emits bare
> 32-bit arena addresses, and the interlink reads `[33:32]` as a mesh id — so
> **every ordinary matmul on mesh_1, mesh_2 or mesh_3 raises `IL_F_RD_REMOTE`**
> ("a NoC memory request carried an address outside this mesh"). Verified by
> clearing the fault, running one clean matmul, and finding it back.
>
> It is harmless *today* only because the access aliases to local DRAM and the
> answer comes out right. Two consequences meanwhile: **`IL_FAULT` is not a
> health signal on any mesh but mesh_0**, and the day a remote fill or store
> lands, every one of those requests becomes a real cross-mesh read of mesh_0.
> Build every address — including local ones — with `global_addr`.

The same trap caught a mover run: a bare `0x2400_0000` written from mesh_1
landed in **mesh_0's** DRAM, confirmed by reading the bytes back there.

**4 GB per mesh is a hard per-shard budget.** A value too large for one mesh must
span several whether or not splitting it buys any speed.

---

## 4. What a drain can already do

The remote path exists in the shipped ISA. `DRAIN` carries two fields
([isa/cluster.md](../../../kohaku_npu_docs/isa/cluster.md) §10.3):

| field | meaning |
|---|---|
| `dfin` | `{fin_y, fin_x}` in the destination mesh; **nonzero is what makes the drain remote** |
| `dmesh` | destination mesh, read only when `dfin` is nonzero |

`(0,0)` is a mesh corner and can hold no endpoint, which is what lets zero mean
"local" — so every instruction encoded before the interlink existed still means
what it meant.

So **a result can be written into another mesh's memory today**, with no new
instruction. That is the smallest useful cross-mesh primitive and the one to
build on first.

---

## 5. Which splits the silicon takes

`SEND` and `ADD_PEER` move an accumulator tile at **full FP22 precision**, so a
contraction split costs no FP16 round trip.

There are two peer paths and they are easy to confuse:

- **The direct ACU-to-ACU wire does not exist.** `mx_cluster_cu.v:387` leaves
  `.peer_out()` open, so there is no cluster-to-cluster accumulator chain, at any
  distance. `FWD` has no command source either.
- **The drain path does, and it reaches another mesh.** A `DRAIN` with `dbuf=2`
  leaves through the drain queue carrying the accumulator's own 352-bit float,
  and `dmesh`/`dfin` aim it at a cluster in another mesh.

**That has been run.** mesh_0 cluster (1,1) into mesh_2 cluster (2,2) over link1:
2 packets / 18 beats out and in, `IL_FAULT` clean, and the far tile read back
**121 of 128 elements bit-identical** to the same matmul drained locally, median
difference zero. 18 beats is 8 sub-tiles x 2 granules plus 2 descriptors — full
accumulator precision, which is exactly the property a K-split needs.

| split | across meshes | within a mesh |
|---|---|---|
| output columns (N) | yes | yes |
| attention heads | yes | yes |
| the contraction (K) | **yes, through the drain path** | yes |

> **`dbuf=2` is `OP_ADD_PEER`: it ADDS into the receiver's resident tile, and
> tile memory has no reset.** The receiving cluster must have run a GEMM that
> opened those sub-tiles first. Draining into a cluster that never did adds the
> burst to whatever was left there — the observed result was 65504 saturation
> everywhere. Running `0 @ b` on the far cluster to open an exactly-zero tile
> fixes it. **Any reduce-scatter built on this must open the destination tile
> before the burst arrives**; that is a scheduling constraint, not a detail
> ([isa/cluster.md](../../../kohaku_npu_docs/isa/cluster.md) §9.4).

One gap before treating a cross-mesh K-split as proven: the measured run added
into a **zeroed** tile, so it demonstrated delivery-by-addition rather than the
sum of two genuinely non-zero partials. The add path is certainly live — the
failure above proves it — but the two-partial case has not been checked. Do that
before building reduce-scatter on it.

Note that [interlink/topology.md](../../../kohaku_npu_docs/interlink/topology.md)
§6.2 says a matmul-only mesh "takes N-splits, not K-splits". That is true of the
direct wire and too strong for the drain path.

---

## 6. Splitting a transformer onto this machine

The pairing that avoids collectives is standard, and it survives §5 as long as
the row-parallel stage stays within a mesh:

| stage | split | crosses a link? |
|---|---|---|
| qkv projection | output columns | no |
| attention | heads | no — a head never needs another head |
| out projection | the contraction | one reduction |
| MLP up | output columns | no |
| MLP down | the contraction | one reduction |

Two reductions per block, and at **98 MB/s** each one is worth pricing before it
is designed in: a reduction moves accumulator tiles at 352 bits per sub-tile,
which is 2.75x what the same tile costs in FP16. Keeping the reduction inside one
mesh remains the cheaper arrangement wherever the layer allows it.

Two placements to weigh against each other, and they have not been measured:

- **Four independent layers, one per mesh** — pipeline parallel, no collective at
  all, and the activation crosses once per layer boundary.
- **One layer across four meshes** — tensor parallel on the column-parallel
  stages, with each mesh's row-parallel stage reducing internally.

The second is what a multi-card stack would do. On this chip the links are on
silicon rather than over PCIe, so it may well win — but "on chip, therefore fast"
is exactly the kind of claim this project has been wrong about before
([results.md](results.md) §8), and it should be measured, not assumed.

---

## 7. The rungs, concretely

| rung | on this chip | state |
|---|---|---|
| 1 | treat all four meshes as one device; the compiler places and inserts crossings | mesh axis exists in the IR; collectives do not |
| 2 | place weights so traffic avoids the three-SLR link and the two-hop diagonals | manual only |
| 3 | tensor-parallel splits; column, head, and K through the drain path (§5) | manual only |
| 4 | give mesh_1 the stages needing no vector core | `MachineSpec` can express it; nothing chooses it |

Ship rung 1 and the manual form of 2, 3 and 4; write real kernels at 2, 3 and 4;
measure them against rung 1. Those kernels are the specification for the
automatic version — not the other way round.

---

## 8. What a crossing actually costs

**Measured 2026-08-13, mesh_1 -> mesh_3 over link1**, `mm_mover` P1 push, mesh
clock measured at 100.09 MHz off `mag_link`'s free-running idle counter:

| | |
|---|---|
| moved | 131,514,368 B |
| wall | 1.337–1.350 s |
| **rate** | **98 MB/s (0.098 GB/s)** |
| link1 tx | 4,109,824 packets / 4,109,824 beats |
| mesh_3 rx | identical, `IL_FAULT` empty, stall count 0 |

**That is the MOVER's figure, not the link's. Label it as such wherever it
appears.** The same fabric, driven by the drain path instead, runs **12.8x
faster**:

| path | rate | beats per packet |
|---|---|---|
| `mm_mover` P1 push | 0.098 GB/s | **1.00** |
| remote `DRAIN`, one cluster | **1.253 GB/s** sustained | **9.00** |
| link ceiling at 100.09 MHz | 3.20 GB/s | — |

One cluster on mesh_0 streaming remote drains into mesh_2, 128 sub-tiles each:
9,216 B of link payload in **736.1 cycles**, tx and rx both 32 packets / 288
beats, nothing dropped. Timed in hardware cycles off `CU_COUNTERS` as a slope
between two trip counts, reproducible to 0.05% -- a wall clock cannot do this,
since a drain moves 288 bytes and JTAG would time itself.

### 8.1 The mesh clock ceiling is 150 MHz, and it buys 1.28x not 1.5x

Swept 2026-08-13 with fixed operands on all four meshes, graded on p50/p99:

| clock | all four meshes | cluster cycles | compute |
|---|---|---|---|
| 100 MHz | clean | ~144,000 | 1,443 us |
| **150 MHz** | **clean, p50 bit-identical to 100** | ~168,700 | 1,125 us |
| 165 MHz | mesh_0 and mesh_3 clean, **mesh_1 and mesh_2 HANG** | 176,900 | 1,072 us |

A hang is a cluster that stops retiring mid-program (323 of 346 completions).
mesh_1 failed on both attempts. **175 MHz was never reached and should not be.**

Three things this corrects:

- **The realised gain from 100 to 150 is 1.28x, not 1.5x.** Cycles RISE 17%,
  because DDR4 is on its own clock and does not scale — a fixed memory latency
  costs more mesh cycles at a faster mesh clock. Any "N% more clock gives N% more
  throughput" estimate is ~85% true at best.
- **It is not thermal.** The die went 62.9 -> 64.9 C across the whole sweep,
  nowhere near the 70 C seen previously. There is no SYSMON driver in the repo;
  read it through the Vivado Tcl socket with
  `get_property TEMPERATURE [get_hw_sysmons]`.
- **The failure mode is a HANG, not wrong numbers**, unlike the report at
  200 MHz. A sharp boundary between bit-identical-clean at 150 and a stalled unit
  at 165 does not match a gradual power wall; timing and droop cannot be told
  apart from the host, so this is an observation, not a conclusion.

**We run at 100 MHz and 150 is clean — 1.28x is available and untaken.** A retune
resets every mesh, so re-open the `Card` after one and restore 100 MHz in a
`finally`.

**The crossing itself is nearly free.** The same program draining locally costs
445.5 cycles, so the remote path adds **290.6 cycles for 288 beats -- about one
cycle per beat.** The interlink adds wire time and essentially nothing else.

This is also the first time the link has ever been pressed: `stalled` is 8.7%
per drain and occupancy 40.1%, where the mover showed exactly zero credit stall
across 3.7 s. At 18 drains there is no stall and the rate is 1.378 GB/s; quote
the **sustained** 1.253.

So the interlink's floor is **measured at 1.25 GB/s and its true ceiling is above
what any single source can reach.** The mover is behind by ~13x because at one
beat per packet it pays a header handshake every 32 bytes, where a drain pays one
per 288.

One consequence for anything designed against these numbers:

- **Quote 0.098 GB/s for the mover, 1.25 GB/s for the drain path, and 3.2 GB/s
  for the fabric.** A design that assumes one where it meant another will be
  wrong by up to 33x.
- A local control run — same mover, same bytes, mesh_1 to itself — managed
  **61 MB/s**, so a remote write is *faster* than a local one. A remote write is
  posted, answered the moment the packet is queued, while a local one waits for a
  real DDR4 `BRESP`.

Still not measured:

- **The three-SLR link (0 <-> 2).** Expected slower from the pipe stages it
  needs. link1 between 1 and 3 is SLR-adjacent; this number is from the easy one.
- **Per-mesh floorplans are not equivalent.** mesh_1 is a different map on the
  most crowded die.

> Repeating this: **`interlink.clear()` does not clear the traffic counters**,
> despite its docstring. `IL_CTRL[1]` only drives `dbell_clr`; `n_tx_pkt`,
> `n_tx_beat`, `n_idle` and `n_stall` live in `mag_link.v` and reset only on
> `resetn`. Take deltas. And a JTAG poll costs ~13 ms, so a transfer has to run
> for seconds before the wall time means anything.

> An earlier round of debugging concluded that mesh_0 was faulty. **It is not.**
> Given identical host-generated operands all four meshes produce identical
> results to the digit; the failures were a synthetic benchmark saturating the
> FP16 drain at 65504, which no normalised network reaches. Do not plan around a
> mesh_0 defect.
