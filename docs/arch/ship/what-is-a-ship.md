---
title: What a ship is
summary: The boundary shape and why it is exactly clock, reset and AXI; the two forms of memory boundary; what a ship costs.
tags:
  - architecture
  - ship
---

# What a ship is

One complete, self-contained accelerator, elaborated for a specific mesh shape.

```
                 clock, reset
                      |
   S_AXI_MEM   ------>|        the memory window
   S_AXI_CTRL  ------>|        control, staging, pass-through
                      |
             +--------v-----------------------------+
             |   edge complex + memory boundary     |
             +--------+-----------------------------+
                      |
             +--------v-----------------------------+
             |   routers, and the endpoints on them |
             +--------------------------------------+
                      |
   M_AXI_...   <------+        memory masters, or one after concentration
   M_AXIS_LINKn <---->|        interlink, when enabled
```

Everything inside is fixed at elaboration. Everything outside is AXI.

That boundary shape is not an accident of convenience — it is what makes a ship
droppable into a vendor block design without hand-wiring. One clock and one
reset serve every interface, master and slave alike, so neither carries a
direction prefix, and interface-inference attributes on the port list name them
all so the tool ties them up on its own.

## Two forms of memory boundary

Two variants exist and the difference is worth naming. The **plain** form
exposes one AXI master per internal requester and expects the device image to
merge them. The **concentrated** form merges them inside the ship and exposes
one wider master, having also crossed into the memory's clock domain — see
[axi](../axi.md). Which one to use is a device-image decision, not a mesh
decision.

## What a ship costs

**The cost of a ship is the sum of its parts and the wiring between them, and
the wiring is not free.** A mesh's routers are its fixed overhead; the endpoints
are what you actually wanted. The ratio between those two is the topology
decision, and it is the reason [noc](../noc/router-circuit.md) spends so much
effort on what a router costs per port.

## Where today's source disagrees

**`src/kohakuaccel/sysnode/sysnode.v` is a reusable composition in a directory of device
tops.** It is the concentrated memory boundary described above, and it is
assembly, not a top.
