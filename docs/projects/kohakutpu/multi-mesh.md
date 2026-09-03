---
title: Four meshes
summary: This chip's mesh chain, what an address means across it, which splits the silicon can take, and which one it cannot.
tags:
  - kohakutpu
  - interlink
  - compiler
  - kernels
---

# Four meshes

> **Kind: the mesh population and the split choices are Yours; the addressing is
> Fixed protocol.** How many meshes, what each holds and which splits a workload
> can take are this project's decisions. What an address means across meshes, and
> how a transfer crosses one, are Fixed protocol shared by every project
> ([address-map](../../address-map.md),
> [arch/ship/interlink](../../arch/ship/interlink.md)).

KohakuTPU is four meshes on one device, one per SLR, each with its own DDR4
channel ([ship.md](ship.md)). This page is what that means when you write a
kernel: where a value has to live, what crossing costs, and the one split this
silicon will not do.

The framework's view of the same problem — why a mesh axis exists at all, and
what it constrains — is [integrate/multi-mesh.md](../../integrate/multi-mesh.md).
This page is specific on purpose.

---

## 1. What each mesh carries

**Every index is the SLR.** Mesh `i` sits in SLR `i`, is station `i` on the
host's bus, is partition and home `i` of the Kohaku Xache, and its DRAM
controller is the block-design cell `ddr4_i` — the controller whose pins are in
SLR `i` (the board's own channel numbering, `c2 c3 c1 c0` for SLR 0..3, appears
in exactly one line of the build, `DDR_PORT_OF_SLR`). What differs between
cards is the population:

| card | mesh 0 | mesh 1 | mesh 2 | mesh 3 |
|---|---|---|---|---|
| `multimesh_v8t2` (the memory-path ship) | 2×2, 2 clusters + 2 vector cores | same | same | same |
| `multimesh_v7` | 2×2, 8 + 2 | 2×2, **6 + 2** | 2×2, 8 + 2 | 2×2, 8 + 2 |

SLR1 holds the host interface (XDMA, JTAG, the clock root), so it is the most
crowded die and carries the smallest mesh on the compute card. The four meshes
are otherwise the same module, differing only by `MESH_ID`. `MachineSpec`
carries a `MeshSpec` per mesh so a placement pass reads each mesh's unit table
and refuses precisely (`compiler/kohakuaccel/machinespec.py`).

Unit populations come from enumeration over the control plane, not from this
table — read them off the card rather than trusting a document.

---

## 2. The chain, and the three links

The meshes are a **line, not a grid**. An SLL joins only ADJACENT SLRs, so the
buildable fabric is the SLR stack in order, and the mesh ids ARE that order:

```
   SLR0     SLR1     SLR2     SLR3
   mesh0 ── mesh1 ── mesh2 ── mesh3      link0 is the neighbour one position DOWN
   pos 0    pos 1    pos 2    pos 3      link1 is the one UP
```

`mag_switch.v` writes that order exactly once, as `CH_SEQ`, and *derives* each
neighbour from it rather than taking a configured peer id — a separately
configured id would be a second place for the topology to be wrong, and the two
would disagree in silence. The compiler mirrors it as `CHAIN = (0, 1, 2, 3)` in
`compiler/kohakutpu/rt.py`, and `MachineSpec.mesh_hops` is a breadth-first search
over the link list built from that. The block design wires mesh `i`'s `LINK1`
to mesh `i+1`'s `LINK0` and its verify stage checks the order against `CH_SEQ`.

**mesh_0 to mesh_3 is three hops** — the diameter. They are the two ends of the
line, which is precisely the pair a 2x2 grid would have made adjacent.

Three facts about routing that a kernel author has to design against:

- **Routing is ONE COMPARISON** — move one position toward the destination. Position
  is monotone along a path, so a packet never reverses; the channel dependency
  graph is two disjoint chains, upward depending only on upward and likewise
  downward, hence acyclic and deadlock-free by shape. **A ring would close that
  cycle**, so do not design a traffic pattern that assumes one.
- **A MAG forwards traffic that is nothing to do with its own mesh.** `0 -> 3`
  transits mesh_1 and mesh_2, so a transfer can be slowed by a mesh it is not
  addressing. Transit and local egress merge ROUND-ROBIN rather than
  transit-first: strict priority would let a saturated through-stream pin the
  local queue's head, and round-robin bounds a forward's wait at one local packet.
- **The ends are ends.** mesh_0 has no lower neighbour and mesh_3 no higher one.
  The unused port is tied off, and a packet arriving there still needing a forward
  is a fault — the routing above cannot produce one.

A ring *collective* is a traffic pattern rather than a routing change, so it is
still allowed — but on this fabric its closing edge `3 -> 0` is not a link, and
costs three hops rather than one.

Every SLL crossing is a `kts_pipe_bd`: `STAGES` registers on the sending die
and `STAGES` on the landing die (`IL_STAGES`: 1 through v8t6, 3 from v8t7),
flits forward and credits back, each half on its own die's clock and copy of
the reset. The placer pulls the boundary pair into a Laguna site and the
other stages walk from there to the node's port, but retiming will not invent
a register that was never written, so those stages exist in RTL. A plain
register chain is correct there only because flow control is credit-based,
with no ready travelling back; each stage is a cycle of the credit loop.

---

## 3. An address is a mesh and an offset

`MachineSpec.global_addr(base, mesh)` builds `mesh << 36 | base` — a 40-bit
address with the mesh id in **`[37:36]`**, 64 GB per mesh
(`compiler/kohakuaccel/machinespec.py`). It raises for a mesh this machine does
not have and for a `base` that does not fit one mesh's 64 GB;
`MachineSpec.addr_mesh` reads the id back out. Above it, `[39]` selects a
command aperture instead of DRAM and `[38]` is reserved for a third mesh bit —
`stage_addr` is the one accessor that sets them. See
[address-map.md](../../address-map.md).

> **On a single-mesh bitstream a remote address does not fault — it ALIASES.**
> Bits `[37:36]` are undecoded there, so a transfer meant for another mesh
> silently reads and writes local DRAM at the same offset. `global_addr`
> refuses it in software because the hardware cannot. Never build one by hand
> with a shift.

Every mesh master — `M_AXI_MEM*`, `UPLOAD`, `MOVER`, `ILINK` — sees only its own
range, at offset 0. The mesh id rides the interlink header, not the local AXI
address, which is why a mesh needs no address-decode change to become one of
four.

> **This is not hypothetical, and it is happening now.** The compiler emits bare
> arena addresses with `[37:36]` at zero, and the interlink reads that field as
> a mesh id — so
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
(the cluster ISA, §10.3):

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

- **The direct ACU-to-ACU wire does not exist.**
  `src/kohakutpu/matmul/mx_cluster_cu.v:435` and `:455` leave `.peer_out()` open,
  so there is no cluster-to-cluster accumulator chain, at any distance. `FWD` has
  no command source either.
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
> (the cluster ISA, §9.4).

One gap before treating a cross-mesh K-split as proven: the measured run added
into a **zeroed** tile, so it demonstrated delivery-by-addition rather than the
sum of two genuinely non-zero partials. The add path is certainly live — the
failure above proves it — but the two-partial case has not been checked. Do that
before building reduce-scatter on it.

An earlier interlink note said a matmul-only mesh "takes N-splits, not
K-splits". That is true of the direct wire and too strong for the drain path.

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

Two reductions per block, and each is worth pricing before it is designed in
whatever the mover's current rate turns out to be: a reduction moves accumulator
tiles at 352 bits per sub-tile,
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
| 2 | place weights so traffic stays near in the chain, above all off the three-hop `0 <-> 3` | manual only |
| 3 | tensor-parallel splits; column, head, and K through the drain path (§5) | manual only |
| 4 | give mesh_1 the stages needing no vector core | `MachineSpec` can express it; nothing chooses it |

Ship rung 1 and the manual form of 2, 3 and 4; write real kernels at 2, 3 and 4;
measure them against rung 1. Those kernels are the specification for the
automatic version — not the other way round.

---

## 8. What a crossing actually costs

The measurement below is the **remote drain path**. It is the only interlink
figure this project currently stands behind.

> **There is no mover rate to quote.** The one interlink rate ever measured
> through `mm_mover` predates the mover rebuild — it was taken on an engine that
> sent one 32-byte word per packet, and the current engine coalesces — so it
> describes hardware that no longer exists and has been withdrawn rather than
> carried forward. Nobody has re-measured the mover on today's RTL. A design
> that needs a mover rate has to measure one.

**Measured 2026-08-13 on the card**, one cluster on mesh_0 streaming
remote drains into mesh_2, 128 sub-tiles each. Mesh clock measured at
100.09 MHz off `mag_link`'s free-running idle counter; timed in hardware cycles
off `CU_COUNTERS` as a slope between two trip counts, reproducible to 0.05%. A
wall clock cannot do this — a drain moves 288 bytes and JTAG would be timing
itself.

| | |
|---|---|
| link payload | 9,216 B in **736.1 cycles** |
| tx and rx | 32 packets / 288 beats each, nothing dropped, `IL_FAULT` clean |
| **rate, sustained** | **1.253 GB/s** |
| beats per packet | **9.00** |
| link ceiling at 100.09 MHz | 3.20 GB/s — arithmetic, not measured |

At 18 drains there is no credit stall at all and the rate reads 1.378 GB/s;
**quote the sustained 1.253**, since `stalled` is 8.7% per drain and occupancy
40.1% at the rate above.

A packet's beat count is what separates the two paths: a drain pays one header
handshake per 288 bytes, where a one-word-per-packet engine pays one per 32.
That ratio is a property of the packetisation and survives the withdrawn
measurement; the rate that went with it does not.

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
445.5 cycles, so the remote path adds **290.6 cycles for 288 beats — about one
cycle per beat.** The interlink adds wire time and essentially nothing else.

So the interlink's floor is **measured at 1.25 GB/s and its true ceiling is above
what any single source can reach.**

One structural fact worth designing against, independent of any rate: a remote
write is **posted** — answered the moment the packet is queued — while a local
write waits for a real DDR4 `BRESP`. A cross-mesh write is therefore not
automatically slower than a local one.

Still not measured:

- **The mover, on today's RTL.** See the note above.
- **The three-hop path (0 <-> 3).** Expected slower from the two forwards it
  needs. The measurement above crosses SLR-adjacent dies; this is the easy one.
- **Per-mesh floorplans are not equivalent.** mesh_1 is a different map on the
  most crowded die.

> **`interlink.clear()` does not clear the traffic counters**, despite its
> docstring. `IL_CTRL[1]` only drives `dbell_clr`; `n_tx_pkt`, `n_tx_beat`,
> `n_idle` and `n_stall` live in `mag_link.v` and reset only on `resetn`. Take
> deltas. And a JTAG poll costs ~13 ms, so a transfer has to run for seconds
> before a wall-clock figure means anything.

> **The four meshes are not numerically different from each other.** Given
> identical host-generated operands all four produce identical results to the
> digit. A benchmark that appears to single one out is far more likely to be
> saturating the FP16 drain at 65,504 — which no normalised network reaches —
> than to have found a defective die ([results.md](results.md) §6.6).
