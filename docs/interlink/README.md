# The interlink: four meshes, one machine

A single mesh spread across SLRs does not close timing. The measurement that
settles it is an implemented design, not a guess: a mesh spanning three SLRs had
a worst path of **4.6 ns that was 98.3% routing with ZERO logic levels**. Nothing
about logic depth caused that and nothing about logic depth fixes it.

So the machine is **four independent meshes, one per SLR, each with its own
DDR4**, joined by an explicit link between their memory agents.

```
        mesh0  <--->  mesh1
          ^             ^
          |             |
          v             v
        mesh2  <--->  mesh3
```

Each MAG gains **two inbound and two outbound link ports**, wired to its two
neighbours. Four links, a 2x2 grid of meshes.

## Why the link joins MAG to MAG

Not because MAG is a memory agent, but because **MAG is the only place that can
absorb a different world without changing the one behind it.**

The NoC's freedom from deadlock rests on XY dimension-order routing over a
single rectangular mesh with a strict turn order. Extending the fabric across
SLRs, or bolting two meshes together with a lateral link, creates cycles that
turn model was never proved against -- and the failure is a hang under load that
no bench reproduces.

Terminating at MAG means the NoC keeps its proof, its 288-bit flit, its turn
rules and its router, unchanged and unaware. The interlink is then free to have
whatever shape suits an SLR crossing rather than whatever suits a 2D mesh, which
is the entire point: **it is neither a memory bus nor a NoC.** It carries both,
in a packet format designed for one thing -- a wide, registered, point-to-point
connection over super-long lines.

## The three kinds of traffic

| | what moves | initiator | typical size |
|---|---|---|---|
| **MAG <-> MAG** | DRAM to DRAM, bulk | the memory mover | MB |
| **NoC <-> MAG** | a unit reads or writes remote DRAM | the compute unit | KB |
| **NoC <-> NoC** | a unit sends to a unit in another mesh | the sending unit | KB |

All three ride the same link and the same packet format. They differ only in
what the header says and who started it -- which is [transfers.md](transfers.md).

## Decisions taken

- **512-bit links.** At 300 MHz that is 19.2 GB/s, which is one DDR4 channel, so
  a cross-mesh copy runs at the same rate as a local one and the link stops being
  a separate performance class to reason about.
- **Registered on both sides, structurally in the RTL.** SLR crossings use
  Laguna sites, which *are* registers; one combinational gate in the crossing
  forfeits them and returns the routing-dominated path above.
- **No pblocks.** Four meshes whose only inter-connections are four links
  partition themselves: the placer minimises SLL usage, and a block with ~100,000
  internal nets against ~1,000 external ones is unambiguous. Constraining meshes
  to SLRs would fight XDMA, which must sit near its GT pins, and each MIG, which
  must sit near its DDR4 pins.
- **Each link is an AXI4-Stream interface**, so the block design shows one
  connection rather than a thousand loose wires.
- **A global 34-bit address space**, `addr[33:32]` naming the mesh. Locality
  becomes a property of the address rather than a separate field.

## Decisions NOT taken

- Link width may drop to 256 if Laguna placement in one column region turns out
  tighter than SLL count suggests. **Nothing is measured yet.**
- Whether a remote *read* should exist at all, or whether every cross-mesh
  transfer should be a push. See [transfers.md](transfers.md) s4.
- How the four agents coordinate a pipeline. Sketched in
  [transfers.md](transfers.md) s5, deliberately unfinished -- it is compiler
  work and the silicon only has to make it expressible.

## This does not bump CU_VERSION

The interlink is a **topology** feature: part of the current ISA version, simply
unimplemented in a machine that is one mesh and natively never needs it. A
compiler emitting `mesh_id = 0` and local addresses produces identical machine
code either way, and the three reserved header bits are reserved-and-zero in
`0x03` today.

Multi-mesh is described by the **board file**, which is already where topology
lives -- coordinates, ports, grid, capacities. Mesh count and this mesh's id join
that list.

## Read next

- [paths.md](paths.md) -- **the four cross-mesh paths and how each is achieved**,
  and the vocabulary the RTL, the driver and these documents all share
- [boundary.md](boundary.md) -- **what is new and what is absent in single-mesh
  silicon**, and how a driver tells which one it is holding
- [topology.md](topology.md) -- ports, the second routing layer, placement
- [protocol.md](protocol.md) -- packet format, flow control, deadlock
- [transfers.md](transfers.md) -- the three kinds, who initiates, the address map

The open questions listed above are now settled; the calls and their reasoning
are in `.plan/decisions.md`. The short version:

- `addr[33:32]` is the mesh id on the NoC and mover paths, and stays the quantise
  markers on the host window -- two spaces on two ports, never unified
- **`MEM_RD_REQ` does not cross in v1.** A remote read faults instead of hanging,
  which removes an entire deadlock class
- `DOORBELL` is a polled counter, not an interrupt and not something the agent
  can block on -- so a pipeline runs at host-poll latency until the control plane
  grows a wait primitive
- `TUSER` is 96 bits
- **`ILINK` defaults to 0 and generates none of this**, because the machine in
  the fab is a single mesh and has to stay bit-identical
