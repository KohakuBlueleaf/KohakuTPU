---
title: Routing and the deadlock argument
summary: Where an endpoint may sit, the routing function as built, and a complete proof that the fabric cannot deadlock on buffer dependencies — with what would break it.
tags:
  - architecture
  - noc
  - routing
---

# Routing and the deadlock argument

A **flit** is one fixed-width message that occupies one link for one cycle
([flits-and-links](flits-and-links.md)). This page says how a flit gets from
where it was injected to where it is consumed, and why that scheme cannot
deadlock.

The second half is the part worth checking rather than trusting. Deadlock
freedom here is a **property of the routing function**, not of how deep the
buffers are, and the argument is short enough to verify in full. It is set out
below in a form a reviewer can check against `src/kohakuaccel/noc/`.

## Coordinates, the router grid, and the edge ring

Positions are `(x, y)` in a `2^POS_WIDTH` square. **Routers** occupy an inner
rectangle: `GRID_LO` to `GRID_X_HI` in x, `GRID_LO` to `GRID_Y_HI` in y. The two
upper bounds are per-axis, so a mesh need not be square.

An **endpoint** — a compute unit, a memory port, the control agent — sits in one
of two places:

- on a router's **local** port, at that router's own coordinate; or
- just outside the router rectangle, on the **edge ring**, hanging off a
  router's otherwise unused directional port.

The edge ring is where gateways go, and that is why it exists. A memory port at
`(0, y)` draws traffic to router `(GRID_LO, y)` and to no other, so several
gateways on different rows genuinely split the load instead of splitting one
funnel.

An edge endpoint cannot literally finish X routing, because X would terminate
at a position that is not a router. `noc_inport.v` resolves this by routing
toward the **clamped** destination — the router adjacent to the target — and
taking the outward hop only on arrival:

```verilog
wire [POS_WIDTH-1:0] r_pos_x = (pos_x < LO) ? LO : (pos_x > XHI) ? XHI : pos_x;
wire [POS_WIDTH-1:0] r_pos_y = (pos_y < LO) ? LO : (pos_y > YHI) ? YHI : pos_y;
```

The alternative — routing Y-first for edge destinations — would mix XY and YX
in one network and put back exactly the cycles XY exists to prevent.

## The routing function, as built

Each input port computes the direction for the flit at its head, from the flit's
destination coordinates and the router's own position. In priority order:

| order | direction | condition |
|---|---|---|
| 1 | **west** | the clamped destination is west of this router, **or** this is the destination router and the *unclamped* x is west |
| 2 | **east** | the same, eastward |
| 3 | **north** | x already matches, and the clamped destination is north — **or** this is the destination router and the unclamped y is north |
| 4 | **south** | the same, southward |
| 5 | **local** | this router is the destination and neither coordinate is outside it |

Three properties of that table are load-bearing:

- **x is resolved before y is touched.** North and south require `x_done`. This
  is the whole of dimension-order routing.
- **The two "or" clauses are the outward hop to an edge endpoint.** They fire
  only when the flit has already arrived at the destination *router*
  (`at_router`), and they send it one hop out of the grid to the endpoint that
  consumes it.
- **It is a priority chain, not five parallel terms.** For a legal destination
  exactly one condition is true anyway; the chain exists because the four
  out-of-grid *corner* coordinates would otherwise assert two directions at
  once and break the one-hot assumption the arbiter depends on.

## Why the fabric cannot deadlock on buffers

### What is being claimed

Buffer deadlock is a set of flits each holding a buffer and each waiting for a
buffer another of them holds, with none able to advance. Formally: build the
**channel dependency graph**. A *channel* is one link plus the input-port buffer
at the far end of it. There is an edge `C1 → C2` when a flit that arrived on
`C1` may request `C2` next. A buffer deadlock requires a **cycle** in that
graph. So the claim to prove is: *this graph has no cycle.*

Nothing about buffer depth appears in that statement, which is the point.
Deeper buffers make a deadlock rarer and harder to reproduce; they do not
remove one.

### Step 1 — classify the channels

Call a channel an **X-channel** if it is an east or west link between two
routers, and a **Y-channel** if it is a north or south link between two routers.
Two other kinds of channel exist and are handled separately below: **injection**
channels (a router's local input) and **delivery** channels (a router's local
output, and any directional output that faces an edge endpoint rather than a
router).

### Step 2 — enumerate the turns the routing function permits

Read the table above with the arriving direction in mind:

| a flit that arrived on… | may next request |
|---|---|
| an **X-channel** | an X-channel **in the same direction**, a Y-channel, or a delivery channel |
| a **Y-channel** | a Y-channel **in the same direction**, or a delivery channel |
| an **injection** channel | anything |

Two entries carry the proof:

- **A flit on a Y-channel never requests an X-channel.** It could only do so
  through the west/east rows of the table, and those require the clamped
  destination x to differ from this router's x. But the flit is on a Y-channel,
  which means the router that sent it had already found `x_done` — and
  `x_done` compares the *clamped destination* against the router's x. Both
  routers on a Y link share the same x. So x still matches, and west and east
  are both false.
- **An X hop never reverses.** A flit travelling east left the previous router
  because the clamped destination x exceeded that router's x. This router's x
  is one greater. So the clamped destination x is either still greater —
  continue east — or equal — `x_done`. It can never be smaller. The same holds
  westward, and in y.

### Step 3 — no cycle

Any cycle in the dependency graph is one of three shapes, and each is
impossible:

1. **Entirely within X-channels.** Every X-to-X edge continues in one direction
   and strictly reduces the distance to the destination column (step 2). A walk
   that strictly decreases a non-negative integer cannot return to where it
   started.
2. **Entirely within Y-channels.** The identical argument in y.
3. **Mixed.** A cycle that uses both kinds must contain at least one Y-channel
   → X-channel edge, and step 2 shows there is none.

Injection channels have no incoming edges — nothing routes *into* a local input
— so they cannot lie on a cycle. Delivery channels have no outgoing edges: the
endpoint consumes the flit rather than forwarding it, so they cannot either.

That is the whole argument. **Deadlock freedom follows from the turn set, and
from nothing else.**

### Step 4 — what the RTL actually enforces

`noc_router.v` turns the argument into logic rather than leaving it as an
assumption. Each input port's request vector is masked by a `*_KILL` constant
that removes the turns XY can never ask for:

| input | turns removed |
|---|---|
| north | back north (a U-turn), and east and west — the Y→X turns |
| south | back south, and east and west |
| west | back west |
| east | back east |
| local | **nothing** — injection may request any direction |

The masks are computed from `GRID_LO` / `GRID_X_HI` / `GRID_Y_HI` rather than
passed in as a second parameter, **because a separately supplied parameter is a
second place for the topology to be wrong.**

Outside synthesis the router also reports a masked request that was actually
made, once and only once. A wrong mask presents as a hang several modules away
from its cause — the request is never granted, the input port's holding slot
never clears — so it is named where it happens. The report is guarded because
the request is held forever, and an unguarded one would be an unbounded flood
on top of the hang.

### The edge-ring exception, and why it is not a hole

Each mask term is conditioned on the neighbour in that direction actually being
a **router**: the north input's west-kill is `N_RTR && W_RTR`, and so on. So at
the boundary column, where west of this router is an edge endpoint rather than
another router, a north-in → west-out turn is **not** killed.

That is correct and does not weaken step 3. West of a boundary-column router is
a *delivery* channel: the flit is being handed to the endpoint that consumes it,
which is the outward hop from the routing table, not a Y→X routing turn. A
delivery channel has no outgoing edges, so it cannot be part of a cycle.

For the same reason, U-turns on an edge-facing side and local-to-local are left
reachable rather than killed. The way *in* to an edge endpoint is a flit
addressing a coordinate outside the grid, and local-to-local is an endpoint
addressing its own coordinate. Killing those as well was **measured at ~380 LUT
across the mesh** (`noc_router.v`, no reproducing script in this tree —
`[unverified]`), which does not buy a driver-side invariant every future author
would then have to know.

### What this does not cover

**Message-class deadlock.** If a node's input fills with requests and it cannot
inject the response that would drain them, the fabric locks — and that is a
dependency between message *classes*, not between channels. Routing does not
address it at all. It is handled by end-to-end credit at the endpoints; see
[flits-and-links](flits-and-links.md#two-kinds-of-flow-control-for-two-different-failures).

**Anything outside one mesh.** The argument covers one router grid. A flit for
another mesh is recognised at the edge complex and handed to the interlink; the
router never knows another mesh exists. See [ship](../ship/).

### What would break it

Stated explicitly, because these are the changes that look harmless:

| change | why it breaks the proof |
|---|---|
| adding an adaptive or minimal-oblivious route alongside XY | reintroduces Y→X edges; step 2 fails |
| routing Y-first for some destinations | mixes XY and YX in one graph, which is the classic cycle |
| letting the router grid span a mesh boundary so a path can re-enter x after y | step 2's "both routers on a Y link share the same x" stops holding |
| deepening the FIFOs "to be safe" | changes nothing in the proof; a cycle with deep buffers is still a cycle |
| a `*_KILL` mask that does not match the topology | the request is never granted and the fabric hangs — which is why the masks are derived, not supplied |
| a compute unit that holds a flit indefinitely without consuming it | turns a delivery channel into one with an outgoing edge in practice; the port contract forbids it ([compute-unit-port](compute-unit-port.md)) |

## Two consequences the rest of the machine depends on

- **Delivery is in order per source–destination pair.** The path is
  deterministic, so two flits from one source to one destination cannot
  reorder. This is what lets the memory system reassemble by counting instead
  of by sequence number, what lets a multi-flit message be a descriptor
  followed by its data, and what makes *push the payload, then push the
  doorbell* a working protocol with no barriers.
- **Buffers only have to cover the backpressure round trip.** They are sized
  for throughput, not against deadlock. `noc_router.v`'s own default is
  `FIFO_DEPTH = 32`; the generated ship meshes raise it because the storage was
  already paid for, not because a deeper buffer is safer —
  [router-circuit](router-circuit.md#the-shipped-configuration-is-not-the-default).

## Where this constrains you

Which coordinate an endpoint actually occupies is decided when a mesh is
assembled, not here. See [ship](../ship/).

The one rule a mesh author must respect is the one the proof rests on: **every
router in a grid must be given the same `GRID_LO` / `GRID_X_HI` / `GRID_Y_HI`
as its neighbours.** The masks and the clamp are both derived from them, and a
router that disagrees with the router next to it will route a flit into a turn
its neighbour has killed.
