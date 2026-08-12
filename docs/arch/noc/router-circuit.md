---
title: What a router costs
summary: Input ports, output ports, and the four knobs that move the fabric's fabric cost.
tags:
  - architecture
  - noc
  - circuit
---

# What a router costs

A router is not a crossbar of storage. It is five input ports and five output
ports, and the flip-flop count is set by how many `FLIT_WIDTH`-wide registers
exist per router — which is the number the design has been repeatedly shaped
to reduce.

## Per input port

`noc_inport.v`: one FIFO, one `FLIT_WIDTH` holding register, a 5-bit one-hot
request register, and the routing comparison. The holding slot is **one per
input port, not one per output direction**. Two flits bound for the same output
were already serialised, so per-direction slots only helped when successive
flits diverged — and they cost twenty-five `FLIT_WIDTH` buses instead of five.
The trade is head-of-line blocking on a congested direction against two thirds
of the router's flip-flops, taken deliberately. Virtual channels are the
standard remedy if profiling ever shows it costs more than it saves.

The slot is kept rather than removed, and that is the other half of the trade.
Feeding the FIFO output straight to the arbiter saves more flops but puts FIFO
read, route computation, arbitration and a 5:1 `FLIT_WIDTH` mux into one
combinational path. Routing is computed on the FIFO output, one cycle before
the flit is offered, so it is off the arbitration path entirely.

## Per output port

`noc_outport.v`: a round-robin pointer, a 5:1 `FLIT_WIDTH` mux, and the output
register. Grants are withheld while the register holds a flit the receiver has
not taken, so nothing is popped on top of something that has not left. Both the
input load term and the output `room` term are true on the cycle the register is
being emptied, which is what sustains one flit per cycle rather than one every
two.

## The knobs that move fabric cost

In the order they matter:

| Knob | What it moves |
|---|---|
| `FLIT_WIDTH` | everything. Every register, mux and FIFO in the router is this wide |
| router count | linear. Cost per router is fixed |
| `MEMORY_TYPE` | which primitive the flit buffers land in — LUT versus block RAM |
| `FIFO_DEPTH` | almost nothing in LUTRAM up to a shift-register's depth; a step function in block RAM |

`MEMORY_TYPE` deserves the emphasis. A flit buffer is wide and shallow, which
is the shape distributed RAM is good at and the shape block RAM wastes: a block
RAM's widest port is far narrower than a flit, so the primitive count is set by
width and the depth then comes free. Whether that waste is worth taking depends
entirely on which resource the design is short of — and in a LUT-bound design
with block RAM sitting near empty, deliberately wasting block RAM to buy LUTs
back is the correct trade rather than an argument against it. The parameter
exists because the right answer differs per instance, not because there is a
default worth defending.

The same reasoning appears once more inside `noc_cu_base`. Its receive queue is
flit-wide and dominates the module's LUTs, while its completion queue is narrow
enough that block RAM loses outright — so the receive queue gets its own
`RECV_MEM` knob rather than sharing one parameter that would force the wrong
answer on one of them. Storage primitive is a per-instance decision, not a
global one, and the parameters are split wherever two instances genuinely want
different answers.

## Measuring it

The cost of *being connected* — a legal node with no arithmetic in it — is
isolated by `noc_cu_null.v`, described in
[compute-unit-port](compute-unit-port.md#the-measurement-instrument). Subtract
it from a real unit and the remainder is genuinely compute. That subtraction is
what decides between many small units and few large ones, which is in turn the
input to choosing a mesh shape in [ship](../ship/).
