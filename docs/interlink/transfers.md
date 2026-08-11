# Transfers: the three kinds, and who starts them

## 1. The question that matters

Not "can data move between meshes" -- the link makes that trivially true -- but
**who decides that it should, and what does it have to know.** A design where the
consumer pulls needs the consumer to know the data is ready. A design where the
producer pushes needs the producer to know where it goes. Both need someone to
know when it landed.

The answer here is that **the agent inside MAG is always the initiator**, because
it is already the thing that issues instructions, and cross-mesh then becomes a
field on instructions that already exist rather than a second control plane.

## 2. The address map does the routing

`addr[33:32]` names the mesh. Each mesh knows its own id; a request whose
`addr[33:32]` equals it is local, anything else is remote.

```
    0x0_0000_0000 .. 0x0_FFFF_FFFF    mesh 0's DRAM
    0x1_0000_0000 .. 0x1_FFFF_FFFF    mesh 1's DRAM
    0x2_...                            mesh 2
    0x3_...                            mesh 3
```

**One global 16 GB space, and the same address means the same byte from
anywhere.** Locality becomes a property of the address rather than a separate
field, which means:

- a `FILL` or a drain needs **no new instruction bits** to reach another mesh
- the compiler places a tensor and its locality follows
- a wrong address is a wrong *place*, not a corrupt access

### The collision, stated because it will otherwise be found the hard way

`mag.v` already uses bit 33 as `HW_QUANT` and bit 32 as `HW_BLAY` -- the
markers that tell an upload to quantise on the way in. Those live on the **host
window** (`S_AXI_MEM`), which is ranged 16G precisely so a host can present them.

These do not collide *today* because they are different ports: the host window
and the NoC memory requests reach MAG by different paths, and only the NoC path
would use `addr[33:32]` as a mesh id. But the two interpretations of the same
two bits are one refactor away from meeting, and a mistake there is silent -- an
upload quantised when it should not be, or a request sent to mesh 3.

**Decide before building**: either the host window keeps 33:32 as markers and
NoC requests keep them as mesh id, documented as two address spaces that must
never be confused, or the markers move to the request's `addr_spare[221:216]`
field, which exists and is reserved for exactly this kind of thing.

The second is cleaner and costs a field nobody uses.

## 3. MAG <-> MAG: bulk, pushed by the mover

The producer knows when its data is ready; the consumer does not. So this is a
**push**, and `mm_mover` is the module -- it is already a strided DMA engine with
two descriptor walkers, its own AXI master, and no NoC endpoint.

The only change is that its destination address may name another mesh. With the
map above, that is **not a new field at all** -- a mover descriptor whose
destination base has `addr[33:32] != my_mesh` produces remote writes, and MAG
routes them.

```
    mover reads local DRAM  ->  MAG packetises MEM_WR  ->  link
                             ->  far MAG writes its DRAM  ->  DOORBELL back
```

The `DOORBELL` matters. Without it the producer knows the data was *sent*, which
is not the same as landed, and a consumer starting on that is the same class of
bug as a `signal_on_complete` that is decoded and ignored.

### As built

The mover is unchanged. `mag_ilink` sits on its write channel and splits by
address: local writes pass through to the mover's own AXI master, remote ones
become `MEM_WR` packets **and are answered locally, at once**. A posted write is
the point -- waiting for a far DRAM would put an SLR round trip inside the
mover's per-word loop, and the mover already runs one transaction at a time.

The `DOORBELL` is what makes that safe. It is rung by the driver after `mv_done`,
so it enters the link behind every write it stands for; the link delivers in
order; and the far side holds it until every write ahead of it has its `BRESP`.
`interlink_2mesh_tb` stalls the far DRAM deliberately and checks the doorbell
does not count until the data has landed.

**Bulk runs at about 4.8 GB/s, not 19.2** -- one 32-byte write is one packet
carrying 256 bits of a 512-bit beat, and header and data handshake in separate
cycles. It does not matter yet: the mover delivers perhaps 1.6 GB/s, and the
link was idle 1,095 cycles out of the two-mesh run. Coalescing contiguous writes
would recover the factor of four and needs a flush timeout, which is a way to
lose the tail of a transfer. `.plan/measurements/interlink.md` s5.

## 4. NoC <-> MAG: a unit reaching remote DRAM

A cluster issues `MEM_RD_REQ` for an address in another mesh. Its local MAG sees
a remote address, forwards, the far MAG performs the DRAM read, and the response
comes back.

**This works and should be discouraged.** The latency is a crossing, plus a DRAM
access, plus a crossing back -- and a cluster's fill path stalls on it. The local
machine already measured what happens when a fill is latency-bound rather than
streamed: the vector core's `VFILL` sits at **0.20 GB/s**, needing 160 cycles of
fill to feed 8 cycles of compute, purely because each read is a full round trip
with no overlap. A remote read is that, with an SLR crossing added twice.

It also carries the deadlock obligation in [protocol.md](protocol.md) s4(c),
which is the reason to cap outstanding remote reads.

So the design position is: **remote reads exist for correctness, not for
performance.** The compiler should push data to the consumer's DRAM before it is
needed.

**Settled: they do not cross in v1.** Omitting them removes deadlock obligation
protocol.md s4(c) entirely, and nothing in a pipeline-parallel schedule needs
them. A NoC memory request naming another mesh raises `IL_F_RD_REMOTE` and its
access aliases to local DRAM -- the same thing that happens on a machine with no
interlink, which is what makes the driver's check the same check either way. The
compiler must not emit one, and `ktpu.hw.interlink.global_addr` refuses to build
the address.

## 5. NoC <-> NoC: unit to unit, and this is what pipelines want

A cluster in mesh 0 sends `CU_DATA` to a vector core in mesh 1. The flit is
addressed to the **local MAG port coordinate** with the mesh id in the three
reserved header bits; local routers see an ordinary local-destination flit and
route it normally; MAG encapsulates it as `NOC_FLIT`; the far MAG rewrites the
header to the local destination and injects.

**The NoC never learns another mesh exists.** No new turn cases, no re-proof, no
router change.

This is the path a pipeline stage boundary should use, because it skips DRAM
entirely: stage *n*'s output goes straight into stage *n+1*'s L1. Everything it
needs already exists -- `CU_DATA` carries an explicit destination, a `buf_id`, an
offset in granules, and an **ack destination**, which was added precisely because
`signal_on_complete` answers the descriptor's source and a sender otherwise has
no way to be told the data landed.

Across meshes the ack has further to travel, and it becomes more important rather
than less.

### As built, and one reversal

The sender addresses the flit to its **local MAG port**, so the routers see an
ordinary local-destination flit. The final node rides in `NOC_TXN_ID` -- which
`CU_DATA` does not use -- and the destination mesh in `NOC_RSVD` as
`{1'b1, mesh_id}`. Both are on **every flit of the burst**, header and data
alike, because MAG's encapsulator is stateless and the routers interleave bursts
from different senders at its port. A stateful one would need a CAM keyed on
source.

Flits are **packed**: one packet accumulates a burst's flits while they share a
destination mesh, final node and source, and closes on `NOC_LAST`, on a key
change, or at 32 beats. One flit per beat, so a cross-mesh burst moves at the
NoC's own rate rather than half of it.

**The far MAG preserves the source coordinate.** The design above proposed
rewriting it to MAG's own; that is wrong, and the reason is `vec_cu`'s
`cd_alien` check -- the mechanism that stops two senders' bursts being merged
into one L1 region. Rewriting the source makes two remote bursts arriving at one
node indistinguishable. The cost of preserving it is that `ack == 0` ("answer the
sender") would answer a node in the *wrong* mesh, so **a remote burst must name
its ack destination explicitly**; `IL_F_ACK0` reports one that does not, and the
Python encoder refuses to build one.

One obligation this leaves with the compiler: a node must not be the destination
of two concurrent bursts whose sources share a coordinate. Within a mesh that is
free, because coordinates are unique. Across meshes it is not.

## 6. Who initiates: the agent, uniformly

The agent inside MAG already stages instructions and dispatches them. Making it
the initiator for cross-mesh work means there is **one control plane, not two**:

| to move | the agent dispatches |
|---|---|
| DRAM to remote DRAM | a mover descriptor whose destination address is remote |
| a unit's output to a remote unit | a `DRAIN` with `dnode` set and the mesh id in the flit |
| remote DRAM into a unit | a `FILL` whose address is remote (discouraged, s4) |

No new instruction. No new dispatcher. The cross-mesh case is a value in a field
that already exists, which is the same shape as `to_node` on `VDRAIN`, `buf` on
the cluster drain, and the entry-size field on a memory request -- all of which
are legacy-by-omission and cost nothing when unused.

### What the agent cannot do alone

Four meshes have four agents. Stage *n* finishing does not tell stage *n+1* to
start, and nothing in the list above crosses that gap.

Two candidates, and this is **open**:

- **Host coordinates.** Simplest, already how everything works, and puts the host
  in a per-microbatch loop -- at ~32 ms per JTAG access that is fatal, at PCIe
  latencies it may be fine.
- **`DOORBELL` lands in a register the remote agent polls.** The agent's own
  program then waits on it, exactly as a round already waits on `NODE_STATUS`.
  Keeps the host out of the steady state, and needs the agent to be able to
  block on something that is not a local signal.

The second is the one that makes a pipeline run at hardware speed, and it is the
piece of the control plane that does not exist yet.

## 7. What this means for the compiler

Not designed here, and deliberately. The silicon's obligation is to make these
expressible; choosing between them is compiler work. But two properties are worth
fixing now because they constrain the hardware:

- **A tensor's locality is its address.** Nothing else has to be tracked.
- **A stage boundary is either a DRAM push or a unit-to-unit send**, and both
  report completion. Anything that cannot be expressed as one of those is a gap
  in this design, not in the compiler.

Pipeline parallelism needs microbatching, which is not obviously easier than
tensor parallelism. The relevant hardware fact is that **tensor parallelism is
what the interlink cannot serve**: a cross-mesh reduction every layer is exactly
the traffic 19.2 GB/s cannot carry when the mesh behind it computes at
1.5 TFLOP/s. So the hardware is choosing the family of schedules, and it should
say so rather than pretend to be neutral.
