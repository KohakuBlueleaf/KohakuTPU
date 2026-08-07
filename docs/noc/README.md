# NoC

`src/kohakunoc/` — the mesh every compute unit, gateway and accumulator talks
over. Built and passing.

| Doc | Covers |
|---|---|
| [spec.md](spec.md) | The contract: flit format, routing, message types, the mandatory CU interface, AXI interworking |
| [cu-framework.md](cu-framework.md) | What every CU must implement, and what `noc_cu_base` gives it free |
| [resource-budget.md](resource-budget.md) | Measured NoC cost, and what is left for compute |
| [simulation.md](simulation.md) | Mesh, orchestrator and multi-CU benches, and the bugs they found |

The instruction traffic that rides on it is described from the other side, in
[`../isa/`](../isa/README.md): [`memory.md`](../isa/memory.md) for
`MEM_RD_REQ`/`MEM_WR_REQ` and [`cluster.md`](../isa/cluster.md) for `CU_INST`.

## Why not a bus

AXI4-Full is too heavy and AXI4-Lite is too inefficient, and neither problem is
fixable by configuration.

An AXI4 interconnect wide enough to feed 32 or more compute units is a crossbar
whose cost grows with the product of masters and slaves, and it carries
machinery this design never uses — out-of-order completion by ID, burst
reordering, exclusive access, four independent channel handshakes per
transaction. AXI4-Lite drops all of that and drops the bandwidth with it.

What this machine actually needs is narrower than either: every unit runs on the
same clock or a power-of-two division of it, every message is one flit or a
short run of them, and the traffic pattern is mostly nearest-neighbour with a
gateway at the edge. That is a mesh, and a mesh built for exactly this is
smaller than an interconnect configured down to it.

The cost of choosing a mesh is that nothing off the shelf understands it, so
every bridge to the outside world has to be written here — which is why
[spec.md](spec.md) §10 is as long as it is.

## What it is

| Router | 2D mesh |
| ---------------------------------------------- | ---------------------------------------------- |
| ![router](../image/NoC/1735483831920.png) | ![mesh](../image/NoC/1735483805869.png) |

```
   flit          288 bits, one flit per cycle per link
                 32-bit header + 256-bit payload
   router        5 ports -- north, east, south, west, local; identical logic on
                 each, so a PE can hang off any of them
   routing       XY dimension-order on clamped coordinates: acyclic by
                 construction, so deadlock-freedom is a property, not a test
                 result
   flow control  busy/valid WITH RETRY on the link, credits end to end
   buffering     one xpm_fifo_sync per input port
   space         2^POS_WIDTH square; at POS_WIDTH = 4 that is a 16x16 coordinate
                 space, 14x14 routers and 56 border PEs
```

**The link protocol is busy/valid with retry, not AXI's valid/ready.** A sender
asserts `valid` and **holds** both `valid` and `data` until a cycle in which
`busy` is low; a receiver accepts **iff** `valid && !busy`. Both halves are
required and neither is separately choosable — see [spec.md](spec.md) §2.1 for
what each half costs when it is missing. Mixing this up with AXI is the easiest
way to write a broken bench: [simulation.md](simulation.md) §7.

**Credits exist to stop a protocol deadlock, not to smooth throughput.**
Backpressuring an instruction into the mesh blocks the flit behind it, which may
be the completion signal that would have freed the resource the instruction is
waiting for. So the dispatcher stalls on credit and never on the network. The
same reasoning is why `CU_SIGNAL` is summarised into a status register rather
than queued into a FIFO nobody drains — [`../isa/agent.md`](../isa/agent.md) §5.

Measured cost of a full router at 288-bit flits and depth-32 buffers, and what
that leaves for compute, is in [resource-budget.md](resource-budget.md).
