---
title: What a ship is
summary: The boundary shape and why it is exactly clocks, resets and AXI; the two forms of memory boundary; what a ship costs.
tags:
  - architecture
  - ship
---

# What a ship is

A **ship** is one complete, self-contained accelerator, elaborated for a
specific mesh shape: a grid of routers, the endpoints attached to them, one
system node, the memory boundary behind it, and the AXI surface in front. It has
a name, a fixed shape, and a boundary consisting of clocks, resets and AXI
interfaces — and nothing else.

```
              clocks, resets
                    |
   S_AXI_MEM   ---->|        the memory window
   S_AXI_CTRL  ---->|        control, staging, pass-through
                    |
           +--------v-----------------------------+
           |   system node + memory boundary      |
           +--------+-----------------------------+
                    |
           +--------v-----------------------------+
           |   routers, and the endpoints on them |
           +--------------------------------------+
                    |
   M_AXI_...   <----+        memory masters, or one after concentration
   M_AXIS_LINKn <-->|        interlink, when enabled
```

Everything inside is fixed at elaboration. Everything outside is AXI.

## Why the boundary is exactly that

That shape is not an accident of convenience — it is what makes a ship
droppable into a vendor block design without hand-wiring.

**Every interface belongs to a named clock, and the module says which.** Each
port on the boundary carries interface-inference attributes naming its bus, its
clock and its reset, so the tool ties them up on its own. A ship does *not* take
one clock: it takes up to six, of which two carry interfaces and the rest are
internal rates. The full list, and which of them are distinct domains, is
[physical/clocking](../physical/clocking.md#what-a-ships-clock-boundary-actually-is).

The two that carry interfaces are worth naming here, because they are the ones a
block design has to connect:

| Clock and reset | Interfaces on it |
|---|---|
| `axi_aclk` / `axi_aresetn` | `S_AXI_MEM`, `S_AXI_CTRL`, and the interlink's `M_AXIS_LINKn` / `S_AXIS_LINKn` — everything that terminates in the system node |
| `dram_aclk` / `dram_aresetn` | `M_AXI_DRAM`, on the concentrated form only |

**Neither carries a direction prefix.** One pair serves masters and slaves alike
within its domain, so the port is `axi_aclk` rather than `s_axi_aclk`, and a
ship's two AXI slaves and its AXI-Stream masters all take the same one.

## Two forms of memory boundary

Two variants exist and the difference is worth naming. The **plain** form
exposes one AXI master per internal requester and expects the device image to
merge them. The **concentrated** form merges them inside the ship and exposes
one wider master, having also crossed into the memory's clock domain — which is
why that form, and only that form, has a `dram_aclk` on its boundary. See
[axi](../axi.md). Which one to use is a device-image decision, not a mesh
decision.

## What a ship costs

**The cost of a ship is the sum of its parts and the wiring between them, and
the wiring is not free.** A mesh's routers are its fixed overhead; the endpoints
are what you actually wanted. The ratio between those two is the topology
decision, and it is the reason [noc](../noc/router-circuit.md) spends so much
effort on what a router costs per port.

Two elaboration choices change that cost without changing what the ship does,
and both are timing decisions rather than throughput ones: putting each
endpoint type on its own clock, and putting the system node on its own clock.
Each costs one crossing FIFO per direction on the ports involved. Both are in
[physical/clocking](../physical/clocking.md#three-consequences).

Measured figures for one instance belong with that project and not here — see
[measurement](../physical/measurement.md) for what such a figure means.

## Where today's source disagrees

**`src/kohakuaccel/sysnode/sysnode.v` is a reusable composition in a directory of
device tops.** It is the concentrated memory boundary described above, and it is
assembly, not a top.
