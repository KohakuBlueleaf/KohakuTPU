# The four cross-mesh paths, and how each one is achieved

Two things can hold data in a mesh -- its **DRAM** and its **NoC** (a CU's L1) --
so there are four ways to move data between two meshes. This page names all
four, says exactly how each is built, and fixes the vocabulary the RTL, the
driver and the rest of these documents all use for it.

**Status is stated per path and is not aspirational.** Two are built and
simulated; two are not, and for those this page says what is missing rather than
describing them as though they work.

| | path | driver name | built? |
|---|---|---|---|
| **P1** | DRAM -> DRAM | `push` | **yes** |
| **P2** | NoC -> DRAM | `remote store` | **no** -- s6 |
| **P3** | DRAM -> NoC | `remote fill` | **no** -- s6 |
| **P4** | NoC -> NoC | `remote drain` | **yes** |

---

## 0. The vocabulary, used identically everywhere

Every term below means the same thing in `mag_*.v`, in `ktpu/hw/interlink.py`,
and on this page. Where the RTL and the driver name the same object they use the
same word; where a word was ambiguous it is not used at all.

| term | means | where it is |
|---|---|---|
| **mesh** | one NoC, its MAG and its DRAM. Ids 0..3 | `my_mesh`, `Board.mesh_id` |
| **interlink** | the whole MAG-to-MAG facility | `ILINK`, `ktpu.hw.interlink` |
| **link** | one full-duplex connection to one neighbour | `mag_link`, `link0`/`link1` |
| **switch** | the three-port router inside MAG | `mag_switch` |
| **adapter** | the thing that turns local traffic into packets | `mag_ilink` |
| **packet** | a header plus 1..`MAX_BEATS` beats | -- |
| **beat** | one 288-bit transfer on a link | `LINK_W` |
| **slot** | one item in a beat. One per beat at 288 | `SLOT_W` |
| **kind** | `MEM_WR`, `NOC_FLIT`, `DOORBELL`, `CREDIT` | `K_*`, `interlink` kinds |
| **class** | credit class: 0 stops at the peer, 1 the peer forwards | `tx0`/`tx1`, `cred0`/`cred1` |
| **global address** | `{mesh_id[1:0], local[31:0]}`, 34 bits | `global_addr()` |
| **doorbell** | the landed-completion counter, per source mesh | `IL_DBELL*`, `ring()` |

Words deliberately NOT used, because they were each ambiguous between two of the
paths above: *transfer*, *copy*, *DMA*, *message*.

**A 288-bit beat is one flit.** That is why the link is 288 and not wider: one
NoC port produces 288 bits per cycle, so at this width the link is matched to
its source rather than waiting on it, and a flit crosses verbatim -- nothing is
packed, padded, split or reconstructed. 256 payload bits per beat at 300 MHz is
**9.6 GB/s**.

---

## 1. P1, DRAM -> DRAM: `push`

The memory mover reads this mesh's DRAM and writes another mesh's.

```
  mm_mover ──AXI write──► mag_ilink ──MEM_WR──► link ──► far mag_ilink ──AXI──► DRAM
                              │                                              │
                          BRESP now                                     BRESP there
```

**How.** `mm_mover` is unchanged. `mag_ilink` sits on its write channel and
splits by `awaddr[33:32]`: local writes pass through to the mover's own AXI
master, remote ones become `MEM_WR` packets carrying the global address.

**The write is posted** -- answered locally the moment the packet is queued.
Waiting for the far DRAM would put an SLR round trip inside the mover's per-word
loop, and the mover already runs one transaction at a time.

**Completion is the doorbell, and it means landed.** The driver runs the mover,
waits for `mv_done`, then writes `IL_DOOR`. The link delivers in order, and the
far adapter holds a doorbell until every write ahead of it has its `BRESP` --
so a consumer released by the doorbell reads DRAM, not a queue.
`interlink_2mesh_tb` stalls the far DRAM on purpose and checks the doorbell
waits.

**Rate.** One packet per 32-byte word, so this path is bounded by the mover
(~0.4-1.6 GB/s), not by the link. Coalescing contiguous writes is the fix and it
needs a flush timeout -- `.plan/measurements/interlink.md` s6.

---

## 4. P4, NoC -> NoC: `remote drain`

A CU in one mesh drains its results straight into a CU's L1 in another. **This is
the pipeline stage boundary and it never touches DRAM.**

```
  CU ──CU_DATA burst──► local router ──► MAG port ──► adapter ──NOC_FLIT──► link
                                                                             │
  far CU ◄── router ◄── inject ◄── far adapter ◄─────────────────────────────┘
```

**How, in four moves:**

1. The CU addresses the burst at **its own mesh's MAG port**, so the local
   routers see an ordinary local-destination flit. No new turn cases, no
   re-proof, no router change -- the NoC never learns another mesh exists.
2. The real destination rides in `NOC_TXN_ID` (which `CU_DATA` does not use) and
   the destination mesh in `NOC_RSVD` as `{1'b1, mesh_id}`. **On every flit of
   the burst**, because the adapter is stateless and the routers interleave
   bursts from different senders at its port.
3. The adapter accumulates one burst into one packet -- keyed on destination
   mesh, final node and source -- closing on `NOC_LAST` or `MAX_BEATS`. One flit
   per beat, so the burst crosses at the NoC's own rate.
4. The far adapter injects each flit with `dst` rewritten to the final node and
   `txn`/`rsvd` cleared. **`src` is preserved**, because `vec_cu`'s `cd_alien`
   check is what stops two senders' bursts merging into one L1 region, and
   rewriting the source would make two remote bursts indistinguishable.

**Consequence of preserving `src`:** `ack == 0` means "answer the sender", and
the sender's coordinate also exists in the destination mesh -- so a remote drain
**must name its ack destination**. `IL_F_ACK0` reports one that does not, and
`matmul.remote_drain` refuses to encode one.

**ISA.** The drain descriptor's base carries `[25:24]` destination mesh and
`[33:26]` final node; nonzero final node is what makes it remote. Both are zero
in every encoding written before the interlink, which is what lets one compiler
serve a single-mesh and a multi-mesh machine.

**Rate.** One flit per beat, 256 payload bits per cycle = **9.6 GB/s**, exactly
what one NoC port can source. The link is not the limit on this path; the port
is, and a second concurrent burst needs a second port.

---

## 6. P2 and P3 are NOT built, and this is what each needs

Today a NoC memory request naming another mesh raises `IL_F_RD_REMOTE` and its
access **aliases to local DRAM** -- the top two address bits are not decoded on
the memory-port path. That is the same thing a single-mesh bitstream does, which
is deliberate: the driver's guard is then the same guard in both cases. But it
means a wrong address returns wrong bytes rather than an error, so
`interlink.global_addr()` refuses to build one.

### P2, NoC -> DRAM (`remote store`) -- the cheap one

A CU issues `MEM_WR_REQ` + `MEM_WR_DATA` at a remote address; it becomes the
same `MEM_WR` packet P1 already uses.

It is a **push**: no response, no new credit class, no new deadlock obligation.
What it needs is the request/data pair reunited at MAG -- the two flits arrive
separately and the demux round-robins across ports, so the adapter needs to key
the pending address on the source coordinate rather than assuming adjacency.
That is the whole job.

### P3, DRAM -> NoC (`remote fill`) -- the one with a real obligation

A CU issues `MEM_RD_REQ` at a remote address and expects `MEM_RD_RESP` back.

This is the request/response pair, and it reintroduces deadlock obligation
`protocol.md` s4(c): a request occupying the last buffer its own response needs
to drain. Making it safe needs a **third credit class** for responses plus a cap
on outstanding remote reads sized to the response buffer. The class field is one
bit today and would become two, with the FIFOs to match.

It is also the path `transfers.md` s4 argues against on latency: a crossing,
plus a DRAM access, plus a crossing back, with the cluster's fill path stalled
on all of it. **P1 or P2 followed by a doorbell is strictly better** wherever the
producer knows the consumer.

---

## Where each path is proved

| path | bench | checks |
|---|---|---|
| P1 | `interlink_2mesh_tb` | byte-exact arrival, doorbell ordered after the data |
| P4 | `interlink_2mesh_tb` | burst crosses, re-addressed, `src`/ack/`NOC_LAST` intact |
| the link under both | `mag_link_tb` | every kind, crossing latency 1..16, credit exhaust and recover |
| routing for both | `mag_switch_tb`, `interlink_4mesh_tb` | all 12 routes, diagonals, adversarial load |
