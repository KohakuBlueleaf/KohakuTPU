---
title: The router as built
summary: What a five-port router is made of, what each stage costs, the knobs that move the number, and which figures in this tree are actually reproducible.
tags:
  - architecture
  - noc
  - circuit
---

# The router as built

A **router** is the switching element of the mesh: five input ports, five
output ports, and a one-cycle path from any input to any output. Four of the
five faces are links to neighbouring routers; the fifth, **local**, faces an
endpoint. A flit ([flits-and-links](flits-and-links.md)) enters on one port,
has its direction computed once ([routing](routing.md)), and leaves on another.

It is **not** a crossbar of storage. Nothing in it is per-(input, output) pair,
and that is the single decision the whole cost model follows from: the
flip-flop count is set by how many `FLIT_WIDTH`-wide registers exist per router,
and the design has been repeatedly shaped to reduce that number.

## Reading the numbers on this page

| | |
|---|---|
| **part** | `xcvu13p-fhgb2104-2L-e` |
| **tool** | Vivado 2024.2 |
| **mode** | out-of-context, **synthesis, not routed**, unless a figure says otherwise |

**No Fmax in this repository is a closed-timing figure.** Synthesis slack is
optimistic — elsewhere in this project a module lost 0.740 ns going from
synthesis to routing — so treat every frequency below as an upper bound.
[measurement](../physical/measurement.md) defines this for the whole tree.

**Most figures on this page are `[unverified]`, and they are marked.** There is
no OOC synthesis script for a router in `scripts/tcl/`. The numbers that exist
were recorded in the RTL and in the mesh generator at the time the choice was
made, and this tree carries no report that reproduces them. They are kept
because a marked figure a reader can go and re-measure is worth more than a
deleted one; they are marked because a figure with no script behind it is not
evidence. See [what is not measured](#what-is-not-measured).

## Per input port

`noc_inport.v` — one FIFO, one `FLIT_WIDTH` holding register, a 5-bit one-hot
request register, and the routing comparison.

**The holding slot is one per input port, not one per output direction.** Two
flits bound for the same output were already serialised, so per-direction slots
only helped when successive flits diverged — and they cost twenty-five
`FLIT_WIDTH` buses per router instead of five. The trade is head-of-line
blocking on a congested direction against two thirds of the router's
flip-flops, taken deliberately. Virtual channels are the standard remedy if
profiling ever shows it costs more than it saves.

**The slot is kept rather than removed**, and that is the other half of the
trade. Feeding the FIFO output straight to the arbiter saves more flops but
puts FIFO read, route computation, arbitration and a 5:1 `FLIT_WIDTH` mux into
one combinational path. Routing is instead computed on the FIFO output, one
cycle before the flit is offered, so it is off the arbitration path entirely.

Two details that are structure rather than detail:

- **The FIFO is first-word-fall-through, so the read enable *is* the load.**
  Nothing is popped speculatively and no spill register is needed.
- **The slot loads on the cycle it is being emptied**, not the cycle after.
  That is what sustains one flit per cycle rather than one every two.

## Per output port

`noc_outport.v` — a rotating-priority pointer, a 5:1 `FLIT_WIDTH` mux, and the
output register that drives the link.

**The priority pointer free-runs.** It advances by one every cycle, wrapping at
five, regardless of whether anything was granted. Arbitration is then: rotate
the request vector so the pointer's port is bit 0, take the lowest set bit,
rotate back. That is fair over time without a grant-driven update, and it is
three small rotates instead of a priority chain.

The chain it replaced was five variable-index muxes in series, each writing
through a decoder, sitting directly under the grant outputs: **888 paths at
11–14 logic levels** `[unverified]` — recorded in `noc_outport.v`, no report in
this tree.

**Grants are withheld while the register holds a flit the receiver has not
taken**, so nothing is popped on top of something that has not left. Both the
input port's load term and the output port's room term are true on the cycle
the register is being emptied, which is what sustains one flit per cycle rather
than one every two.

## What that comes to, per router

Structurally, and independent of any measurement:

| | count | width |
|---|---|---|
| input FIFOs | 5 | `FLIT_WIDTH` × `FIFO_DEPTH` |
| input holding registers | 5 | `FLIT_WIDTH` |
| request registers | 5 | 5 bits |
| output registers | 5 | `FLIT_WIDTH` |
| output muxes | 5 | 5:1 at `FLIT_WIDTH` |

So **ten `FLIT_WIDTH` registers plus five FIFOs**, and the per-direction slot
design the current one replaced would have been thirty registers.

## The knobs that move fabric cost

In the order they matter:

| Knob | What it moves |
|---|---|
| `FLIT_WIDTH` | everything. Every register, mux and FIFO in the router is this wide |
| router count | linear. Cost per router is fixed |
| `MEMORY_TYPE` | which primitive the flit buffers land in — LUT versus block RAM |
| `FIFO_DEPTH` | almost nothing in LUTRAM up to a shift register's depth; a step function in block RAM |

`MEMORY_TYPE` deserves the emphasis. A flit buffer is wide and shallow, which is
the shape distributed RAM is good at and the shape block RAM wastes: **a block
RAM's widest port is far narrower than a flit**, so the primitive count is set
by width and the depth then comes free. Whether that waste is worth taking
depends entirely on which resource the design is short of — and in a LUT-bound
design with block RAM sitting near empty, deliberately wasting block RAM to buy
LUTs back is the correct trade rather than an argument against it. The
parameter exists because the right answer differs per instance, not because
there is a default worth defending.

Two recorded measurements of that trade, both `[unverified]` — the source is
named, but no report in this tree reproduces either:

| change | effect | recorded in |
|---|---|---|
| `FIFO_DEPTH` 4096 in URAM → 32 in distributed RAM | **15 URAM → 0, for +400 LUT per router** | `noc_router.v` |
| `FIFO_DEPTH` 32 → 512, `MEMORY_TYPE` `"block"` | **20 BRAM per router either way, −1.8 MHz** | `scripts/py/gen_mesh.py` |

The second row is the one that explains the shipped setting: a 288-bit port is
four `RAMB36` tiles at *any* depth up to 512, so once the tile is spent the
depth is already paid for.

## The shipped configuration is not the default

Worth stating plainly, because a reader who takes the module defaults as the
design will get the resource picture wrong.

| | `noc_router.v` default | what `gen_mesh.py` emits |
|---|---|---|
| `FIFO_DEPTH` | 32 | **512** |
| `MEMORY_TYPE` | `"distributed"` | **`"block"`** |

The generated mesh raises the depth **because the storage was already paid
for**, not because a deeper buffer is safer. Depth does not prevent deadlock;
the routing function does ([routing](routing.md)). A buffer only has to cover
the backpressure round trip.

The same reasoning is applied at the endpoint. `noc_cu_base`'s instruction and
receive queues default to 32 and 16 and are generated at **512 each**, recorded
as **8 BRAM either way, +34 LUT, 574 → 567 MHz** `[unverified]`
(`scripts/py/gen_mesh.py`).

And the same reasoning is why the parameters are split rather than shared.
Inside `noc_cu_base` the receive queue is flit-wide and dominates the module's
LUTs, while the completion queue is narrow enough that block RAM loses
outright — so the receive queue gets its own `RECV_MEM` knob rather than
sharing one parameter that would force the wrong answer on one of them.
**Storage primitive is a per-instance decision, not a global one.**

## What the endpoint attach costs

The router is only half of what a node costs. The other half is
`noc_cu_base` — the shell every compute unit carries
([compute-unit-port](compute-unit-port.md)).

| figure | condition |
|---|---|
| **498 LUT, 840 FF, 4 RAMB36** | the shell *as instantiated inside* the RV32 PE, hierarchical report, `-flatten_hierarchy none`, 2.500 ns request, `xcvu13p-fhgb2104-2L-e`, Vivado 2024.2, **synthesis not routed** — [rv32-pe/performance](../cpu/rv32-pe/performance.md#resources) |
| **756 LUT** | the same module synthesised **standalone**, out-of-context, same part and tool. Reproducible: `scripts/tcl/ooc_rv_pe.tcl` takes the top module as its first argument. No stored report in this tree |

**Those are different measurements, not a contradiction, and must not be
subtracted from one another.** A module synthesised alone and the same module
inside a parent are optimised in different contexts, and the queue parameters
move storage between LUTs and block RAM — which is why one row has four block
RAM tiles and the other is a bare LUT count. A shell figure is only meaningful
with its configuration and its timing request attached.

The attach does not grow with the datapath behind it. On the RV32 PE it is
18.6 % of the whole unit; on the widest compute unit measured so far it is
3.0 %.

## Two circuit-level traps this system paid for

Both are properties of the tools, not of this design, and both are the kind of
thing that costs a week.

**A reset can be *re-extracted* from constants you did not ask for.** Writing
reset-free RTL is not enough. In `noc_cu_base`'s transmit path, Vivado inferred
a synchronous reset from the zero fields of the outgoing flit and drove it onto
all 288 output flops, and **those reset pins routed at −3.726 ns against a
3.334 ns target** `[unverified]` — a *routed* figure, unusually, recorded in
`noc_cu_base.v` with no report in this tree. Two things fixed it: an explicit
`EXTRACT_RESET = "no"` attribute, and building the outgoing flit as **one
enable and one mux** rather than an if/else chain, which is what gave synthesis
the constants to fold into a reset in the first place.

**Flow control that crosses a module boundary combinationally lands on the
critical path.** With the system node's clock-domain crossing disabled, its
inbound busy signal reaches the router's flow control combinationally: **12
logic levels, −0.561 ns, and it was the mesh's worst path in every
configuration measured** `[unverified]` (`scripts/py/gen_mesh.py`). The
generated mesh therefore registers the queue flags at that boundary **even when
both sides are on the same clock**.

## Measuring it

The cost of *being connected* — a legal node with no arithmetic in it — is
isolated by `noc_cu_null.v`, described in
[compute-unit-port](compute-unit-port.md#the-measurement-instrument). Subtract
it from a real unit and the remainder is genuinely compute. That subtraction is
what decides between many small units and few large ones, which is in turn the
input to choosing a mesh shape in [ship](../ship/).

Splitting *router* cost from *endpoint* cost needs a second equation, and the
tree carries the two tops that provide it:

- `src/kohakuaccel/verif/noc_tile_1r.v` — one router carrying all five
  endpoints it can hold. Ratio 1 : 5.
- `src/kohakuaccel/verif/noc_cluster_2x2.v` — four routers, twelve endpoints.
  Ratio 4 : 12.

Two tops, two ratios, two equations, so router cost and endpoint cost fall out
of measurement rather than being estimated from a standalone synthesis run —
where the optimisation context is different and the numbers do not transfer.

## What is not measured

Stated so that absence is not read as a result:

- **There is no OOC synthesis script for a router, or for either measurement
  top, in `scripts/tcl/`.** The method above is built and the scripts that ran
  it are not in the tree. Every per-router figure on this page is therefore
  marked `[unverified]`.
- **No routed figure for a router or a mesh exists here**, with the single
  exception of the reset-pin slack noted above, which is itself unreproducible
  from this tree.
- **No in-context router figure.** What a router costs inside an assembled
  device image, with real placement, is not recorded.
- **No power figure of any kind.**
- **No measured head-of-line blocking cost.** The single-slot trade is argued
  structurally above and has never been profiled against a per-direction
  design, which is why virtual channels remain a "if profiling ever shows it"
  rather than a rejected option.
