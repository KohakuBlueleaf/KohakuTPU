---
title: Choosing a mesh
summary: How many units, where they attach, what the interlink is for, and why SLR boundaries decide the answer.
tags:
  - integrate
  - mesh
  - floorplan
---

# Choosing a mesh

A mesh is a grid of routers, the endpoints hanging off them, and one memory
agent. You choose its shape by writing a picture of it. This page is about
choosing well: what the grid can hold, what an extra endpoint really costs, and
which of these decisions is actually a floorplanning decision wearing a topology
costume.

The router's own behaviour — ports, routing rule, flow control — is
[arch/noc/README.md](../arch/noc/README.md). The physical constraints behind §6
are [arch/physical/README.md](../arch/physical/README.md).

---

## 1. What a mesh can hold

Routers sit on a rectangular grid at coordinates `(1..NX, 1..NY)`. Each router
has five ports: four to its neighbours, and **one local port**. Endpoints attach
in two places:

```
     xxx  A    B   xxx        interior: one endpoint per router local
     mag  R    R    C
     mag  R    R    D         edge: endpoints live just outside the grid,
     xxx  E    F   xxx              at (x,0), (x,NY+1), (0,y), (NX+1,y)
```

An **edge** endpoint attaches to the outward-facing link of the router on that
side — the link that would otherwise be tied off. It costs a link, not a router,
and the router's coordinate clamp is what makes those out-of-grid coordinates
routable. This is the lever that matters most in practice: it lets a grid carry
considerably more endpoints than it has routers.

So the capacity of an `NX × NY` grid is:

```
    NX * NY          router locals
  + 2 * (NX + NY)    edge slots
```

Three constraints on filling it:

- **The four corners must be empty.** A corner touches no router, so an endpoint
  there would have nowhere to attach. This has a protocol consequence worth
  knowing: because `(0,0)` can never hold an endpoint, zero is a safe sentinel in
  any field that names a node — several message classes use it to mean "reply to
  the sender" rather than "reply to node zero".
- **The memory agent occupies at least one slot and at most four.** It declares
  a fixed number of NoC port coordinate pairs; a map with none has nothing to
  serve memory. **Port count is a bandwidth decision, and it is the main one you
  make here**: each port has its own read path and its own intake, so units
  bound to one port contend for it and units on different ports do not.

  **Ports do not buy transform throughput, and it is worth knowing why the
  question is closed.** The transform slot is one instance per agent, on the
  memory mover's read return, and there is one converged DRAM master behind
  every port — so N transforms could consume one beat per cycle between them,
  and N−1 would be idle by construction. That is structural, not a property of
  any workload. Buy ports for fetch bandwidth and intake, never for conversion.
- **The control plane occupies none.** The dispatch orchestrator has no node of
  its own — it lives behind the memory agent's ports, and each port's inbound
  flits are demultiplexed by type, memory traffic to the memory engine and
  everything else to the orchestrator. Do not budget an endpoint for control.

Grids need not be square. The generated top sets each axis's clamp separately,
which is why the router clamps X and Y independently.

---

## 2. Writing the map

A mesh is described as a picture, one three-character token per position, and a
generator turns it into a top module:

```
    xxx mat vec xxx
    mag mat mat xxx
    mag mat mat xxx
    xxx mat vec xxx
```

The interior is the router grid; the first and last rows are the north and south
edges, the first and last columns the west and east edges. `xxx` is nothing,
`nul` is a port that exists but is tied off — which is how you leave a side empty
without changing the grid's shape.

**A `mag` token places one of the system node's attachments, not "MAG".** The
node is one component and every client inside it — the memory engines, the
control agent, the interlink, the control processor — shares those attachments
through `sn_hub`. There is no token for the processor: it answers at **(0,0)**, a
corner, which touches no router and which the generator forbids any map from
filling, so the coordinate is free by construction and costs no attach point. A
map that still carries the retired `cpu` token is rejected by name.

The generator does four things you would otherwise do by hand, and the fourth is
the one that catches fire when done manually: it instantiates the routers with
the right per-axis clamps, wires every link with exactly one driver per
direction, instantiates each endpoint with its coordinates, and **ties off every
link nothing claimed**. An unclaimed link has an undriven direction — data,
valid, and the busy coming back at it all float — and which direction that is
depends on which side of the router it sits on.

It also assigns each endpoint its memory port: a unit is elaborated with
`MEM_X`/`MEM_Y` naming the nearest port by clamped Manhattan distance. A unit is
therefore **bound to one memory port at elaboration**, not at runtime.

> **Today the generator's token vocabulary is hardcoded to KohakuTPU's units.**
> `scripts/py/gen_mesh.py` knows `mag`, `vec` and `mat`, and emits those module
> names with those parameters. A new project adds a token by editing the
> generator, which is the wrong seam — the vocabulary should come from the
> project. It is listed in the restructuring notes rather than hidden here.

---

## 3. One port or two

A unit gets one local port unless you deliberately give it two, and the default
is right far more often than it looks.

KohakuTPU's matmul cluster was originally two endpoints on adjacent routers — a
manager and an accumulator — and was merged into one. The reasoning generalises:

- **A second port buys no bandwidth if both endpoints load the same direction of
  the link.** The link is full duplex. The manager's fetches and the
  accumulator's drains were opposite directions of one link, so one port carried
  both at no contention.
- **A second port costs a router local**, and locals are the scarce resource. At
  two locals per unit, a given unit count forces a larger grid; at one, it fits a
  smaller one. Routers are the dominant cost of an endpoint (§4), so that
  difference is several routers, not a rounding error.

Take a second port when the two directions genuinely conflict — a unit that both
streams operands in *and* streams results out continuously, at rates that
together exceed one link. Otherwise merge.

---

## 4. What an endpoint costs

Two costs scale differently, and conflating them gives the wrong answer:

**The endpoint framework is cheap.** `noc_cu_base` is three flit queues and a
little logic. Attaching one more unit to an existing router is nearly free.

**Routers are not, and router count follows endpoint count.** A grid large
enough for N endpoints has a router count that grows with N, and each router is
substantially more expensive than the framework attached to it. The real cost of
"one more endpoint" is mostly the fraction of a router it forces.

Two conclusions:

- **Prefer coarse units.** Three small units behind one port, one framework
  instance and one local memory cost far less than three endpoints. This is the
  same conclusion §3 reaches from the other direction, and the same one
  [README.md](README.md) §4 reaches from workload shape: if two units are tightly
  coupled, write one larger unit.
- **Granularity and specialisation are different axes.** An endpoint can be
  all-one-kind or mixed at any size. Choosing "one unit does one thing" does not
  oblige you to choose "many small units".

For the measured figures behind this — per-router and per-endpoint utilisation on
a specific part — see [projects/kohakutpu/results.md](../projects/kohakutpu/results.md)
and [workflow/measure.md](../workflow/measure.md). They are properties of one
accelerator on one device, not of the framework, which is why they are not
quoted here.

---

## 5. Routing, and what it means for you

Flits route **dimension-order: X first, then Y**. Two properties follow that you
should design against rather than around:

**Deadlock freedom comes from the routing rule, not from buffer depth.** Making a
router's queues deeper does not make a cyclic dependency safe, and no finite
simulation can prove the absence of one. Do not invent a path that bypasses the
turn model.

**Flits from one sender to one receiver arrive in order; nothing else does.**
Same source, same destination, same path — so a burst cannot self-reorder. But
the routers interleave traffic from different senders freely, so another unit's
flit can land in the middle of your burst. Every receiver must demultiplex by
flit type and, when more than one sender can address it, by source
([compute-unit.md](compute-unit.md) §4 and §6).

---

## 6. SLRs, and why they decide the topology

On a multi-die FPGA the topology question is not really "what shape mesh" — it is
"what fits on one die". Three constraints are hardware rules rather than
preferences:

**A compute unit cannot span an SLR boundary.** Carry chains, DSP cascades and
BRAM/URAM cascades do not propagate across one; the only connection between dies
is the dedicated crossing wires. If your datapath uses a cascade — and a
systolic or accumulator-style datapath almost certainly does — the whole unit is
die-resident by construction.

**A DRAM channel cannot span one either.** All the I/O banks an interface uses,
and its clocking, must be on one die. The channel is anchored by its pinout.

**A crossing is flop-to-flop with real delay, and needs pipeline stages.** At
production clock rates the crossing itself consumes a substantial fraction of a
period before any fabric routing at either end.

The conclusion the framework has already drawn from those, so you do not have to:
**one mesh per die, each with its own DRAM channel, joined by an explicit
registered link between memory agents.** A single mesh spanning dies was built
and measured, and rejected — its worst path was almost entirely routing delay
with no logic levels in it. The wire budget for a crossing was never the binding
constraint; latency and routing were.

So: **size a mesh to one die**, and reach for more dies by adding meshes, not by
growing one.

The device-level facts behind this — which die holds which DRAM channel, where
the host interface lands, and what that forces about mesh placement — are
properties of one board and one part, so they live with the project that built
on them: [projects/kohakutpu/ship.md](../projects/kohakutpu/ship.md). Read it
before choosing a topology for a real device; the arrangement is less free than
it looks, because each mesh is pinned to the die that carries its memory.

---

## 7. The interlink

The interlink joins meshes memory-agent to memory-agent, over registered
streaming links. What you need to know as a unit author:

- **The mesh does not learn that other meshes exist.** A remote transfer is
  addressed to the *local* memory agent's port, and the real destination rides in
  header fields the message class does not otherwise use. The routers see an
  ordinary local flit.
- **The remote destination goes on every flit of a burst, not just the first.**
  The encapsulator at the far end is stateless, and the routers interleave bursts
  from different senders at its port; a first-flit-only scheme would need a
  lookup keyed on source to reunite them.
- **Zero means local.** A remote-node field of zero reads as a local transfer,
  because `(0,0)` is a mesh corner and can hold no endpoint (§1). Every
  instruction encoded before the interlink existed therefore still means what it
  meant.
- **Flow control across the boundary is credit-based, with no ready signal
  travelling back.** A backwards-travelling ready is exactly the combinational
  crossing the registered link exists to avoid. Do not add one, and add pipeline
  stages only in the module built for them.

Detection is a capability register, not the unit version field: a compiler
emitting purely local addresses produces identical machine code either way, so
the version that answers "is this the bitstream my compiler targets" has the same
answer on both. See [spec/control-registers.md](../spec/control-registers.md).

---

## 8. A procedure

1. **Count endpoints.** Your units, plus one to four memory ports. Not the
   control plane.
2. **Merge until coupling is loose.** Anything two endpoints must do together
   within a few cycles should be one endpoint (§3, §4).
3. **Pick the smallest grid whose capacity — `NX*NY + 2*(NX+NY)`, corners
   excluded — holds them**, filling edge slots before growing the grid.
4. **Place memory ports near the traffic**, remembering each unit is bound to its
   nearest one at elaboration (§2).
5. **Check it fits one die**, with room for the host infrastructure — DMA, AXI
   interconnect and a memory controller are not free, and one of your dies also
   pays for the host interface.
6. **If it does not fit, add a mesh, not rows** (§6).
7. **Generate, elaborate, and measure out of context** before believing any of
   it ([workflow/measure.md](../workflow/measure.md)).

---

## 9. Open questions

- **The map vocabulary is not extensible without editing the generator** (§2).
  What a project-supplied token table should contain — module name, parameter
  mapping, port-name mapping — is not designed.
- **Nothing checks a map against the device it is meant for.** Endpoint count,
  grid size and die capacity are related by arithmetic nobody performs until
  synthesis fails.
- **The memory port assignment is nearest-by-hops and nothing else.** It does not
  balance load across ports, and a map whose traffic is skewed toward one port
  gets no warning.
- **Endpoint placement within a die is unmodelled.** A within-die layout change
  has been observed to cost real performance through routing alone, and no tool
  in the flow predicts that from a map.
