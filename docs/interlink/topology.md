# Topology: ports, the second routing layer, and placement

## 1. What MAG gains

Two inbound and two outbound link ports, each an AXI4-Stream:

```
    module mag
        ...
        LINK0_IN   (slave  axis)     LINK0_OUT  (master axis)
        LINK1_IN   (slave  axis)     LINK1_OUT  (master axis)
```

Two, not four, because the topology is a grid and a 2x2 grid gives every node
exactly two neighbours. At eight meshes it becomes 2x4 and interior nodes want
three; the port count is therefore a parameter, not a constant, and the RTL
should say `NLINK` from the start even while it is 2.

Each direction is independent and separately registered. There is no shared bus
and no arbitration between the two links beyond the switch described below.

## 2. MAG contains a second router, and it needs its own deadlock proof

Diagonal traffic on a 2x2 grid is two hops: mesh0 reaching mesh3 passes through
mesh1 or mesh2. So each MAG must **forward** packets not addressed to it, which
makes it a three-port switch:

```
    link0  <-->  +--------+  <-->  link1
                 | switch |
                 +--------+
                      |
                    local   (this mesh's DRAM and its NoC)
```

**This is a second routing layer and it does not inherit the NoC's proof.** Give
it the same discipline for the same reason: **XY dimension-order on mesh
coordinates.** A packet routes in X until `mesh_x` matches, then in Y. On a
rectangular grid of meshes that is deadlock-free by the identical argument the
NoC already relies on, and it stays true when the grid grows to 2x4.

That is why the mesh-of-meshes must remain a **grid** as it scales, not become a
ring or a chain. A ring reintroduces the cycle the turn model exists to break.

Consequence worth stating: a MAG forwards traffic that has nothing to do with
its own mesh. Forwarding must not be able to block local traffic, or a busy
mesh0->mesh3 stream stalls mesh1's own DRAM access. That is a virtual-channel or
separate-buffer obligation, and it is in [protocol.md](protocol.md) s4.

## 3. Placement

### 3.1 Which mesh goes where, measured

The board decides this, not the design. Measured with `vivado -mode batch` on an
empty in-memory `xcvu13p-fhgb2104-2-e`, cross-checked against the XDC pinout and
a placed design's clock regions —
[`../general_cores/slr.md`](../general_cores/slr.md) §2.2 has the method.

**Exactly one DDR4 controller per SLR.** That is the fact the whole arrangement
rests on: **no mesh ever needs a cross-SLR AXI path to its own DRAM**, so the
only nets that cross are the four links.

| mesh id | `(x,y)` | SLR | size | DRAM | why this SLR |
|---|---|---|---|---|---|
| 0 | (0,0) | SLR0 | **6+4** | `ddr4_2` | empty today; only `ddr4_2` is on it |
| 1 | (1,0) | SLR1 | **4+4** | `ddr4_3` | XDMA is here, so it is the most crowded die |
| 2 | (0,1) | SLR3 | **4+4** | `ddr4_0` | the grid diagonal of SLR1 |
| 3 | (1,1) | SLR2 | **6+4** | `ddr4_1` | already holds a 6+4 at 93.6% CLB |

The small meshes go on SLR1 and its **grid diagonal**, which is where the crowded
die's neighbours are cheapest to reach. `mag_switch.v` takes the mesh id as
`{y,x}`: `peer0` flips x, `peer1` flips y. So link0 pairs (0,1) and (2,3); link1
pairs (0,2) and (1,3).

**Three of the four links are SLR-adjacent and one is not, and that is forced.**
A 4-cycle cannot embed in a 4-node path without one edge spanning:

| link | meshes | SLRs | |
|---|---|---|---|
| link0 | 0 ↔ 1 | SLR0 ↔ SLR1 | adjacent |
| link1 | 1 ↔ 3 | SLR1 ↔ SLR2 | adjacent |
| link0 | 2 ↔ 3 | SLR3 ↔ SLR2 | adjacent |
| link1 | 0 ↔ 2 | SLR0 ↔ SLR3 | **spans three** |

The long one is what `mag_link_pipe.v` exists for — a plain shift register, legal
precisely because the protocol is credit-based and has no handshake to preserve
([protocol.md](protocol.md) §3). **Add stages there and nowhere else**: a
pipeline stage anywhere with a real `TREADY` reintroduces the combinational
crossing that `mag_link.v` asserts against.

The address map falls out of `ADDR_W = 34` and
`ktpu.hw.interlink.global_addr()`: `{mesh_id[1:0], local[31:0]}`, and each DRAM
is exactly 4 GB, so the split is exact.

| segment | offset | size |
|---|---|---|
| mesh 0..3 memory | `0x0/1/2/3_0000_0000` | 4 GB each |
| `ddr4_0..3` control | `0x4_0000_0000` + n·1 MB | 1 MB |
| gpio | `0x4_0040_0000` | 64 KB |
| mesh 0..3 control | `0x4_0080_0000` + n·64 KB | 64 KB |

**Every mesh master (`M_AXI_MEM*`, `UPLOAD`, `MOVER`, `ILINK`) sees only its own
DDR's 4 GB, at offset 0.** The mesh id rides the interlink header, not the local
AXI address — which is why a mesh's masters need no address decode change to
become one of four.

`validate_bd_design` passes on this with **no critical warnings**. Two block
design traps that cost real time getting there — Tcl's 32-bit `format %X` and
`design_1.bd`'s wrong DDR refclk — are in
[`../axi/bringup.md`](../axi/bringup.md).

### What is required

**Registers on both sides of every crossing, in the RTL.** UltraScale+ SLR
crossings go through Laguna sites, and a Laguna site is a flip-flop. The tool can
only use one when the path is `flop -> SLL -> flop` with nothing in between. A
single combinational gate -- an AND with a valid, a mux on a ready -- forfeits
Laguna and the crossing becomes ordinary interconnect, which is how a path
becomes 98.3% routing.

This is structural. It must not be left to retiming, because retiming will not
introduce a register that was not written.

**Credit-based flow control, not backpressure across the boundary.** A `TREADY`
that travels back across the SLR combinationally is exactly the combinational
crossing above. See [protocol.md](protocol.md) s3.

### What is deliberately NOT constrained

**No pblocks, and no SLR assignment.** Two reasons:

1. **It would fight fixed logic.** XDMA must be near its GTs, each MIG near its
   DDR4 pins. A pblock pinning a mesh to SLR0 when XDMA also needs SLR0 makes
   placement worse.
2. **It is unnecessary if the design is honest.** The placer minimises SLL usage
   as a first-class cost. Four meshes with ~100,000 internal nets each and ~1,000
   external ones are unambiguous clusters. The narrow interlink is not merely
   friendly to routing -- it is *what makes the automatic partitioning correct*.

### What must be verified rather than assumed

"The placer usually does the right thing" and "it did this time" are different
claims, and the difference is the 4.6 ns path. After implementation, check:

- which SLR each mesh landed in, and that no mesh is split
- SLL count per boundary against the ~1,000 per link expected
- that the interlink paths used Laguna registers rather than general routing

If a mesh did split, the fix is not a pblock on the mesh -- it is finding what
else crossed. A stray control or debug net between meshes will drag logic across
a boundary and cost more than the link saves. The `obs` output that used to exist
on the generated tops is exactly that shape of hazard: a wire nobody reads,
connecting things that should not be connected.

## 4. Width, and why 512

| width | nets per direction | GB/s at 300 MHz | |
|---|---|---|---|
| 256 | ~270 | 9.6 | half a DDR4 |
| **512** | **~530** | **19.2** | **one DDR4 channel** |
| 1024 | ~1,050 | 38.4 | ahead of DRAM, no consumer |

512 is chosen so **a cross-mesh copy runs at the same rate as a local DRAM
copy**. That is the property that matters for the compiler: the interlink stops
being a distinct performance class and a stage boundary costs what a memory copy
costs.

Four links at ~1,060 nets each (both directions) is ~4,200 SLLs if all four
cross boundaries, against roughly 17,000 available per boundary on this device.
SLL count is not the constraint.

**Laguna register availability in one column region probably is**, and nobody has
measured it. If 512 proves tight, the answer is 256 rather than a wider link with
serialisation logic -- and the fallback is to add a second link between the same
pair, not to widen one. Two moderate structures place better than one large one,
give the switch two paths, and keep each crossing at its natural width.

## 5. Block design integration

Each link is declared as `xilinx.com:interface:axis:1.0` via `X_INTERFACE_INFO`:

```
    TDATA[511:0]   TVALID   TREADY   TLAST   TUSER[n:0]
```

so the block design shows **one connection per link**. Four meshes is four
connections, mis-wiring is visible, and Vivado treats each as a bus for its own
grouping and reporting rather than as a thousand anonymous wires.

This is not a new technique here: `gen_mesh.py` already emits MAG's five AXI
masters as named interfaces for exactly this reason, because a packed bus arrives
in the block design as loose wires needing Slice IP on every field.

## 6. Generating one, and the four instances

`scripts/py/gen_mesh.py` takes `--ilink` and `--mesh-id`. Four facts about what
they do, each of which is a place to get it wrong:

* **`master_names()` appends `M_AXI_ILINK`.** MAG's `MP1` is one master wider at
  `ILINK=1`, because the interlink's landing channel is `LK = MEM_PORTS+2`. The
  count *and the order* must match `mag.v` or every master shifts one slot — and
  a shifted master is a block design that builds and addresses the wrong memory.
* **Two AXI-Stream interfaces per link**, `M_AXIS_LINKn` out and `S_AXIS_LINKn`
  in, named so Vivado infers them with no `.xci` (§5).
* **`MESH_ID` is a module parameter, not a literal.** One RTL file therefore
  serves all four instances — and it is only the *reset* value, since `IL_MESH`
  is writable ([boundary.md](boundary.md) §3), so a single bitstream can occupy
  any position in the grid.
* **`ILINK=0` output is byte-for-byte identical to the shipped
  `ktpu_ship_3x2.v`.** Verified by sha256 and difflib rather than by eye, because
  the machine in the fab is a single mesh and has to stay bit-identical.

### 6.1 A cluster may now sit on a router EDGE port

`mat` on a non-interior tile used to be rejected — "a cluster needs a router
local". It does not: **an edge cluster costs a link, not a router**, and that is
what lets six clusters fit a 2x2 router grid instead of forcing 3x2. The one
thing that changes is which side drives: on a router's local port the endpoint
drives `fwd` because the router named the link and took `rev`; on an edge the
side decides, so the generator derives it (`edge_link`) rather than assuming.

Vector cores and MAG ports were already edge endpoints — this makes clusters the
same kind of thing rather than a special case, and the tie-off sweep for unused
edges now covers all three uniformly.

`(0,0)` remains an unusable **corner**, which is what lets it be the "not remote"
and "answer the sender" sentinel throughout
([`../isa/cluster.md`](../isa/cluster.md) §9.2, §10.3).

### 6.2 The maps that exist

`src/synth_top/maps/`, one file per topology, named `<grid>_<clusters>+<vectors>`.
Five are new this session and every one of them needs §6.1:

| map | shape | LUT of an SLR | note |
|---|---|---|---|
| `mesh_2x1_6+0.txt` | 6+0 on **two** routers | -- | both routers fully packed: local, north, south and one of west/east are all endpoints. Six clusters for two routers instead of four |
| `mesh_2x2_6+0.txt` | 6+0 | -- | `6+2` with the vector cores replaced by `nul`, so router shape and MAG placement are identical and only the endpoints move |
| `mesh_2x2_6+2.txt` | 6+2 | ~60.3% **derived** | four clusters on locals, two on column 1's north/south edges, vectors on column 2's |
| `mesh_2x2_6+4.txt` | 6+4 | -- | the east column's clusters hang off the routers' *east* ports rather than adding a router row |
| `mesh_2x2_4+4.txt` | 4+4 | -- | the smallest 4+4 |
| `mesh_3x2_6+3.txt` | 6+3, row-local | 68.5% | every row is `mag mat mat vec`, so nothing crosses a column |
| `mesh_3x2_6+4.txt` | 6+4 | 76.7% | past the ~75% routability ceiling; this is what SLR2 holds today at 93.6% CLB |

**A router is ~20 BRAM, and BRAM is the tight resource once four meshes and four
MIGs are on one device** — which is why trading a router for edge links is worth
doing at all, and why 2x1 exists. The 6+2 figure is derived from per-unit costs
rather than synthesised; treat it as an estimate until it is run.

> **The matmul-only meshes take N-splits, not K-splits.** `peer_out` is
> unconnected in `mx_cluster_cu`, so partial sums arriving from another mesh have
> no accumulation path. A matmul-only mesh is therefore weights-resident with one
> shipment in and one out — splitting the *output* columns, never the contraction.
