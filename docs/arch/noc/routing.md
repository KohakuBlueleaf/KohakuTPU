---
title: Routing and the coordinate space
summary: Where endpoints sit, how the clamp reaches the ones outside the router grid, and why XY makes deadlock-freedom a proof.
tags:
  - architecture
  - noc
  - routing
---

# Routing and the coordinate space

Two things that look like implementation detail and are not: where an endpoint
is allowed to be, and why the fabric cannot deadlock.

## Coordinates and the clamp

Positions are `(x, y)` in a `2^POS_WIDTH` square. Routers occupy an inner
rectangle: `GRID_LO` to `GRID_X_HI` in x, `GRID_LO` to `GRID_Y_HI` in y. The
two upper bounds are per-axis, so a mesh need not be square.

Endpoints sit in one of two places. On a router's **local** port, or just
outside the router rectangle on the **edge ring**, hanging off a router's
otherwise unused directional port. The edge ring is where gateways go, which is
why it exists: a memory port at `(0, y)` draws traffic to router `(GRID_LO, y)`
and not to any other, so several gateways on different rows genuinely split the
load instead of splitting one funnel.

An edge endpoint cannot literally finish X routing, because X terminates at a
position that is not a router. `noc_inport.v` resolves this by routing toward
the **clamped** destination — the router adjacent to the target — and taking
the outward hop only on arrival:

```verilog
wire [POS_WIDTH-1:0] r_pos_x = (pos_x < LO) ? LO : (pos_x > XHI) ? XHI : pos_x;
wire [POS_WIDTH-1:0] r_pos_y = (pos_y < LO) ? LO : (pos_y > YHI) ? YHI : pos_y;
```

The alternative — routing Y-first for edge destinations — would mix XY and YX
in one network and put back the cycles XY exists to prevent.

## Routing is XY, and that is a proof rather than a test

Route X until the column matches, then Y, then at most one outward hop. The
Y-to-X turn never happens, so the channel dependency graph is acyclic, so the
fabric cannot deadlock on buffer dependencies. That property does not come from
buffer depth. Deeper FIFOs make a deadlock rarer and harder to find; they do not
remove one.

Two consequences follow and both are load-bearing elsewhere:

- **Delivery is in order per source-destination pair.** The path is
  deterministic. This is what lets the memory system reassemble by counting
  instead of by sequence number, and what lets a multi-flit message be a
  descriptor followed by data.
- **Buffers only have to cover the backpressure round trip.** They are not
  sized against deadlock, because nothing about deadlock depends on them.

`noc_router.v` turns the acyclic argument into logic. Each input port's request
vector is masked by a `*_KILL` constant derived from the clamp bounds, killing
the turns XY can never ask for. The masks are computed from `GRID_LO` /
`GRID_X_HI` / `GRID_Y_HI` rather than passed in as a second parameter, because a
separately supplied parameter is a second place for the topology to be wrong.
Outside synthesis the router also reports a masked request that was actually
made — a wrong mask presents as a hang several modules away, so it is named
where it happens.

## Where this constrains you

Deadlock-freedom here covers only buffer dependencies. The other kind —
dependencies between message *classes* — is not addressed by routing and is
handled by end-to-end credit at the endpoints; see
[flits-and-links](flits-and-links.md#two-kinds-of-flow-control-for-two-different-failures).

Which coordinate an endpoint actually occupies is decided when a mesh is
assembled, not here. See [ship](../ship/).
