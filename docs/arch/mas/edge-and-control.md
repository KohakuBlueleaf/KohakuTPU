---
title: The edge complex and the control agent
summary: Staging as the second addon slot, the share layer that puts three consumers on one set of attachments, the control agent, the host window and the mover.
tags:
  - architecture
  - mas
  - control
---

# The edge complex and the control agent

Everything at the mesh's edge that is not a memory port, and the layer that lets
them all share one set of attachments.

## Staging inside the memory agent

The second addon slot. Fetched lines can be held on the memory-agent side so
that several units asking for the same region do not each reach memory for it.

What is fixed is the surrounding shape: requests arrive as flits, responses
leave as tagged flits, and the intake and emit paths are unchanged whether or
not anything is staged in between. Whether to stage at all, how much, with what
replacement behaviour and in which storage primitive, is the addon's business.
It is one of the two places the memory agent is designed to be extended rather
than merely parameterised — the other is
[the transform stage](transform-stage.md).

## The host memory window

An AXI slave with its own master channel behind it. It is on a separate channel
from the memory ports on purpose: an upload is bursty and rare, the steady state
is neither, and sharing a state machine stops a long upload and a unit's write
from overlapping.

Two details of the shape are worth carrying into any reimplementation. The
source and destination burst lengths are unrelated — the agent issues its own
bursts. And a write response must be latched rather than passed through, because
a pipelined host that raises `BREADY` after `BVALID` is legal AXI and would
otherwise never see it.

## The mover

The engine behind the mover command set: two descriptor walkers, source and
destination, stepped in lockstep, with the destination defining the iteration
space. That is what makes a source stride of zero a broadcast with no extra
mode. It has its own AXI master and **no fabric endpoint** — it reads memory and
writes memory, and never talks to a compute unit.

Its command path is a **slice of the control window**, not a set of boundary
ports. That is a design rule with a scar behind it: loose sideband ports never
get wired up in a block design, and a shipped engine that nothing could command
is worse than no engine. The window forwards writes verbatim with the offset
preserved, so the client keeps its own register offsets.

What its command set can express is in
[instruction-space](instruction-space.md#what-the-memory-instruction-set-covers).

## The edge complex

```
  in:   flit arrives at port N
          memory type?      -> that port's engine
          remote marker?    -> the interlink encapsulator
          otherwise         -> the control agent

  out:  agent flit for row y  -> leaves by the port on row y
        interlink injection   -> same rule
        engine response       -> its own port
        priority: agent, then interlink, then engine
```

The agent wins outbound arbitration because its traffic is a handful of control
flits against a stream of operand words. Engine priority would let a busy port
starve dispatch exactly when the machine is busiest.

Inbound, the ports round-robin into the agent's single input, and the pointer
moves only on an accepted flit — moving it every cycle would let a port lose its
turn to one that had nothing to send.

One rule here is a deliberate loss of data, and it is the most important line in
the module. **The control agent must never block memory.** It raises busy when
its receive FIFO is full, and a host that does not drain that FIFO leaves it full
indefinitely. Holding the port busy for that would stop the memory flits behind
it on the same link, permanently, because nothing clears the condition. So a
control flit that *cannot* be delivered is accepted, dropped, and reported.
Waiting one's turn is different, still holds the port, and is bounded by the port
count.

## The control agent

The host's reach into the mesh. An AXI slave on one side and a fabric endpoint
on the other, offering three things:

**A raw flit mailbox.** Inject and receive any flit, malformed ones included. An
address-mapped bridge could only ever emit memory requests — never an
instruction, never a deliberately bad header — so bring-up and fault injection
would have no mechanism.

**Instruction dispatch.** The host stages instruction flits in a local RAM
through the same AXI slave, names a destination, and kicks. The agent reads the
staging RAM, rewrites the routing header — destination from the register, source
stamped with its own coordinates so the target can reply without configuration —
and pushes. It needs no AXI master, because it never fetches from memory; it
only forwards what the host already placed there. Dispatch stalls on credit and
never on the network.

**A status mirror.** Completion signals are summarised into a per-node status
word and a global count, and the flit itself is dropped rather than queued.
Queued, unread signals fill a FIFO, raise busy, and stop the agent accepting
anything — including the very signals that return dispatch credits. A host that
never reads would wedge the control plane after a FIFO's worth of completions.
The mirror stores a **count** rather than a sticky flag, so a host polling slower
than events arrive can tell how many it missed. The global count exists because
"is everyone finished" against a per-node mirror would otherwise cost one poll
per node and grow the host program with the machine.

## What the rest costs

Per-port cost is in [memory-port](memory-port.md#what-a-port-costs). What is
left is per machine.

**In the control agent.** Two RAMs, and both are LUTRAM for reasons that are
structural rather than preference:

- The staging RAM's read destination is a variable part-select, and block RAM
  read data has to land in a plain register. A `ram_style` attribute asking for
  block is rejected as infeasible and silently downgraded — and an ignored
  attribute reads exactly like a guarantee, so none is written.
- The status mirror does a read-modify-write of one address in one cycle, which
  block RAM cannot do.

**The interlink, when it is enabled.** Disabled, every one of its nets is tied
to a constant, every use folds, and the generated top does not expose the ports
at all — so a build without it is identical to one made before it existed. That
is maintained deliberately: every addition sits inside a generate or is gated by
the parameter, because "costs nothing when off" is only true if someone keeps
checking.

## Where today's source disagrees

**`noc_orchestrator.v` — the control agent — lives in `src/kohakunoc/`.** It is
instantiated by exactly one module, `mag.v`, and belongs with the control plane,
not with the router.

**The interlink is packaged inside the memory agent.** `mag_link.v`,
`mag_link_pipe.v`, `mag_switch.v`, `mag_ilink.v` and `il_pkt_arb.v` implement a
second routing layer with its own topology, its own deadlock argument and its
own credit protocol. They live here because MAG hosts the endpoint. Their
description is in [ship](../ship/), and that is where the package boundary
should be too.

**`mag_dram_port.v` is not instantiated by `mag.v`.** The composition that gives
a MAG one AXI master instead of several — arbitration, width packing and the
clock crossing — is `src/synth_top/mag_1m.v`, a reusable assembly sitting in a
directory of device tops. See [axi](../axi.md) for the overlap between
`mag_dram_port.v` and `axi_n1.v`.
