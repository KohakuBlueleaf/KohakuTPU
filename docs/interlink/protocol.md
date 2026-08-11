# Protocol: one packet format for two worlds

The interlink carries bulk DRAM traffic and encapsulated NoC flits. Those have
opposite shapes -- one is a long stream where header overhead must vanish, the
other is a single 288-bit item where a wasted beat doubles the cost -- and the
format has to serve both without becoming two protocols.

## 1. Header in TUSER, payload in TDATA

```
    TDATA[511:0]    payload only, every beat
    TUSER[...]      the header, valid on the first beat of a packet
    TLAST           last beat of this packet
```

Putting the header in the stream's sideband rather than in-band is what makes
both shapes efficient: a 4 MB mover burst pays **zero** header beats, and a lone
NoC flit does not spend a 512-bit beat carrying 64 bits of header.

Header fields:

| field | bits | meaning |
|---|---|---|
| `kind` | 4 | `MEM_WR`, `MEM_RD_REQ`, `MEM_RD_RESP`, `NOC_FLIT`, `DOORBELL`, `CREDIT` |
| `dst_mesh` | 3 | final destination; the switch routes on this |
| `src_mesh` | 3 | for responses and for provenance |
| `txn` | 8 | echoed on a response |
| `len` | 16 | payload beats minus one |
| `addr` | 34 | memory kinds: the global address, see transfers.md s2 |
| `dst_x`,`dst_y` | 4+4 | `NOC_FLIT`: the coordinate to inject at, in the far mesh |

`src_mesh` is not decoration. A response has to find its way home, and a
`DOORBELL` is meaningless without knowing who rang it.

## 2. Why not reuse the 288-bit flit

Because the flit is shaped for a 2D mesh with per-hop arbitration and 288-bit
links, and the interlink is a point-to-point wire that wants to move 512 bits per
cycle. Encapsulating a flit costs 288 of 512 bits on the one packet kind where
that matters least (`NOC_FLIT` is rare and small), and the alternative -- making
the interlink 288 bits so flits fit exactly -- would halve bulk bandwidth to
serve the minority case.

The flit crosses **as payload**, unmodified. The far MAG rewrites only the
header it needs to inject with, which is why the NoC never learns another mesh
exists.

## 3. Flow control is credits, and that is a placement decision

A normal AXI-Stream `TREADY` travels backwards. Across an SLR that means a
combinational path from the receiver's buffer state to the sender's enable --
which forfeits Laguna and produces the routing-dominated crossing this whole
design exists to avoid.

So: **the sender holds credits, the receiver returns them as `CREDIT` packets**.
Every signal crossing the boundary is then a registered forward path in its own
direction, and `TREADY` is used only *locally*, between the link port and the
logic behind it.

Credit accounting is per `kind`-class, not global, for the reason in s4.

## 4. Deadlock: three obligations, and they are separate

**(a) The mesh-of-meshes.** XY dimension-order on mesh coordinates, as in
[topology.md](topology.md) s2. Free, provided the topology stays a grid.

**(b) Forwarding must not block local traffic.** A MAG relaying mesh0->mesh3
must not be able to stall its own mesh's DRAM access. Separate buffers and
separate credits for forwarded traffic; a full forward queue backpressures the
link it came from, never the local port.

**(c) Requests and responses must not share a channel.** A `MEM_RD_REQ` occupying
the last buffer that a `MEM_RD_RESP` needs to drain is the classic
request/response deadlock, and it is reachable here because reads cross in one
direction and their data crosses back. **Responses get their own credit class**,
sized so an outstanding-read limit guarantees space.

That last one is the argument for capping outstanding remote reads, and it is a
second reason -- beyond latency -- to prefer pushes. See
[transfers.md](transfers.md) s4.

## 5. What the link does NOT do

- **No reordering.** A point-to-point link delivers in order, which is what lets
  the far side treat a `MEM_WR` burst as contiguous without a per-beat address.
- **No retry, no CRC.** SLLs are on-die. If that assumption is ever wrong the
  failure is silent corruption, so it is written here rather than assumed.
- **No cache coherence.** A mesh's DRAM is written by exactly one producer at a
  time; the compiler is responsible for that, and nothing in hardware checks it.
  This is the same contract the local machine already has.

## 6. As built

`mag_link.v`, `mag_switch.v`, `mag_ilink.v`. Where this page and the RTL differ,
the RTL is right and this section says how.

**`TUSER` is 96 bits**, laid out little-endian by field:

```
    [3:0]    kind      1 MEM_WR, 2 NOC_FLIT, 3 DOORBELL, 4 CREDIT
    [5:4]    dst_mesh  [7:6] src_mesh
    [15:8]   txn       CREDIT: the number of beats being returned
    [31:16]  len       payload beats minus one
    [65:32]  addr      MEM_WR: the global byte address
                       NOC_FLIT: [39:32] = {fin_y, fin_x}
                       CREDIT:   [32]    = which class
    [95:66]  reserved, zero
```

**`MEM_RD_REQ` and `MEM_RD_RESP` do not cross.** The kind encoding leaves room
for them and nothing implements them, which removes obligation s4(c) entirely --
with no response class there is no request/response channel cycle to break. A
NoC memory request naming another mesh raises `IL_F_RD_REMOTE` and the access
aliases to local DRAM, exactly as it does on a machine with no interlink.

**The two credit classes are "terminates at the peer" and "the peer forwards
it"**, not request and response. That is s4(b) rather than s4(c), and it is the
obligation that survived. The sender classifies from `dst_mesh` against the
peer's mesh id; the receiver classifies the same packet the same way against its
own, and they are the same number.

**`TREADY` is a constant.** The receiver ties `s_axis_tready` high and the sender
never reads `m_axis_tready` -- its output register is simply overwritten each
cycle, because credit reserved the space before the beat was sent. A simulation
assertion fires if TREADY is ever low, which is what a real slave on the far end
would look like. This is the only shape that keeps the crossing `flop -> SLL ->
flop`.

**Credit depth is 64 beats per class**, and `mag_link_tb` sweeps the crossing
latency from 1 to 16 cycles to show that correctness does not depend on it. The
`2mesh` and `4mesh` benches never exhausted credit at all -- a receiver draining
at one beat per cycle returns credit as fast as a sender consumes it -- so the
exhaustion path is asserted to occur in `mag_link_tb` rather than hoped for.

**A packet may not exceed `MAX_BEATS` = 32.** Above that there exists a packet
that can never be granted credit, which presents as a dead link, so it is a
reported fault rather than a wait.
