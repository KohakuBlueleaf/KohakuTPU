---
title: The edge complex and the control agent
summary: Staging as the second addon slot, the hub that puts every client of the node on one set of attachments, the control agent, the host window and the mover.
tags:
  - architecture
  - mas
  - control
---

# The edge complex and the control agent

Everything at the mesh's edge that is not a memory port, and the layer that lets
them all share one set of attachments.

## Staging inside the memory agent

The second addon slot. **Staging** is an on-chip store with a reserved range in
the address map — reached by ordinary addresses, never by a new instruction, and
holding operand words verbatim. It is not a cache: no tags, no associativity, no
replacement, no coherence and no write policy. Fetched lines can be held there
so that several units asking for the same region do not each reach memory for
it.

What is fixed is the surrounding shape: requests arrive as flits, responses
leave as tagged flits, and the intake and emit paths are unchanged whether or
not anything is staged in between. Whether to stage at all, how much, with what
replacement behaviour and in which storage primitive, is the addon's business.
It is one of the two places the memory agent is designed to be extended rather
than merely parameterised — the other is
[the transform stage](transform-stage.md).

### Where the store sits decides who can use it

`STAGE_AT_PORT` is not a tuning knob. It chooses between two structurally
different machines, and only one of them is a shared store.

| | `STAGE_AT_PORT = 0` | `STAGE_AT_PORT = 1` |
|---|---|---|
| where | one store **inside every memory port**, upstream of where the requesters meet | one store on the **converged** path, in front of the DRAM port |
| who can be staged | that port's flit traffic only — **the mover and the interlink can never reach it** | every requester, including the mover, the processor and inbound remote writes |
| what it costs | `PORTS` × 64 URAM. At two ports, **4 MB of URAM to obtain 2 MB of reachable store**; at four, 256 URAM | **64 URAM**, 4 banks × 16,384 entries, ~2 MB |

**A store that half the machine cannot address is not a shared store, and
duplicating it per port does not make it one.** The per-port arrangement is also
the one where the resource that is plentiful hides the mistake: URAM is cheap
enough on this device that a doubled megabyte-scale array does not announce
itself in a LUT count. Read the memory columns of every synthesis report, not
only the logic ones.

The converged form is what the ship generator emits when staging is enabled, and
what the node is measured at. The module parameter still defaults to `0`.

Everything a runtime keeps in staging depends on this: the page tables, the
cross-node mailbox and the allocator bitmap are all reached by the processor
over the converged path, so the per-port arrangement is not merely twice the
URAM — it is the wrong store.

### Staging honours byte strobes

A write into staging changes **only the lanes its strobes name**. The store's
natural word is 32 bytes, and the bank memory is a byte-enabled primitive
(`kohaku_sdpram_be`); `mag_stage_port` passes the AXI write strobes straight
through to it.

**This is a correctness property, not an optimisation, and it is what makes
staging usable for structured data at all.** Without it a 64-bit store writes
its own eight bytes *and clears the other twenty-four* — so a program updating
one page-table entry destroys the three beside it, an allocator bitmap loses
every neighbour of the word it sets, and a mailbox slot takes its neighbours
with it. Every one of those failures is silent: the store succeeds, the memory
is simply wrong afterwards.

The rule generalises past this store. **A wide memory reached by narrow writes
needs byte enables or a read-modify-write, and inferring neither is the default
outcome** — the width mismatch is invisible in synthesis and in every test that
writes whole words.

### Where a remote write lands

A write whose mesh field names another mesh leaves over the interlink, and the
far side has to decide where in its own map to put it. The address's top bit
decides:

| the inbound address | lands in |
|---|---|
| **special — bit 39 set**, which is how every aperture is named | that mesh's staging, at the **full 40-bit address**; `mag_stage_port` claims it off the converged path |
| anything else | that mesh's DRAM, by its **low 32 bits** |

The truncation for DRAM is deliberate rather than a width accident: local DRAM
starts at zero and the mesh field sits high in the address, so carrying all 40
bits would place every remote write gigabytes out of range. An aperture address
has to survive intact for exactly the opposite reason — the aperture *is* named
by the high bits, and `mag_stage_port` claims by bit 39 and the mesh field, so a
truncated aperture address is no longer an aperture address and lands in DRAM at
the aperture's offset instead.

That pair of rules is what lets one mesh's mover write into another mesh's
staging, which is the mechanism behind cross-node handoff: the producer copies
into the consumer's staging and rings its doorbell.

**Reads never cross.** The link carries remote writes, compute-unit flits and
doorbells. A read's source must be in the requester's own mesh.

> **The contention that comes with it must be stated.** The staging port serves
> **one claimed burst at a time across all requesters**, round-robin on a single
> id. So a processor's page-table walks and its mailbox polling interleave with
> whatever the mover is driving into staging, at burst granularity. That is fine
> for a dispatcher; a hot processor loop should not keep its working set in
> staging while the mover is driving staging hard.

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

**The mover is the control processor's, not the memory agent's** — its SIMD
memory unit, with the transform slot as that unit's extension. It is documented
with the processor in [control-processor](control-processor.md) and
[simd-model](simd-model.md); what appears at this level is one more requester on
the converged path, channel `MV`, which is all the agent ever knew about it.

Two descriptor walkers, source and destination, stepped in lockstep with the
destination defining the iteration space — which is what makes a source stride
of zero a broadcast with no extra mode. It has **no fabric endpoint**: it reads
memory and writes memory, and never talks to a compute unit.

The host's `AUX_CFG` window still reaches it, forwarded verbatim with the offset
preserved so a driver keeps its own register offsets. That path is a **slice of
the control window**, not a set of boundary ports, and the rule has a scar
behind it: loose sideband ports never get wired up in a block design, and a
shipped engine that nothing could command is worse than no engine. The
processor's own store wins when both pulse.

What its command set can express is in
[instruction-space](instruction-space.md#what-the-memory-instruction-set-covers).

## The hub

`sn_hub.v`. **The system node has attachments; nothing inside it does.** The
memory engines, the control agent, the interlink and the control processor are
all clients of one hub, and the node presents exactly `PORTS` of them.

```
  in:   flit arrives at port N, first arm to claim it owns it
          remote marker?      -> the interlink encapsulator
          dst == (0,0)?       -> the control processor
          memory type?        -> that port's engine
          otherwise           -> the control agent

  out:  a flit for row y      -> leaves by the port on row y
        engine response       -> its own port
        priority: agent, then ctrl PE, then interlink, then engine
```

**Order matters inbound because one flit can satisfy two tests.** A memory flit
may also be marked remote, and the engine is not the consumer of one that is
leaving this mesh — so remote is asked first.

The agent wins outbound because its traffic is a handful of control flits
against a stream of operand words, and engine priority would starve dispatch
exactly when the machine is busiest. The processor sits next: a stalled dispatch
stalls the whole graph. The interlink is below both because its burst is already
bounded by credit the far end granted.

Inbound, the ports round-robin into each single-input client, and a pointer
moves only on an accepted flit — moving it every cycle would let a port lose its
turn to one that had nothing to send. The three arbiters are **separate**:
sharing one would let a stalled interlink hold up dispatch, or a busy processor
hold up the agent.

### Why (0,0), and why it is not a choice

Routers occupy `(1..NX, 1..NY)` with edge endpoints just outside them on the
four sides. A **corner touches no router**, and `gen_mesh` rejects a non-empty
corner outright — so `(0,0)` is free by construction in every mesh of every
shape, and no map can ever collide with it.

X-then-Y routing delivers it with no special case: a flit addressed `(0,0)`
leaves its router westward and lands on the port on that row, whose demux peels
it off. An on-mesh compute unit cannot reach the processor this way, because a
unit names memory by descriptor and never by node; the host can, and so can
another mesh's processor over the interlink. That is exactly the intended set —
**and it is what the default RV32 configuration does**, where the processor
wears a compute-unit shell and is loaded, kicked and polled through it.

**The cost is one router hop out and back.** A flit the host dispatches to
`(0,0)` leaves the node's port, reaches the router, and comes straight back to
the port it left from. The same is true of the processor's own remote flits: the
encapsulator is fed from the inbound demux, so an outbound remote flit takes the
round trip before it is claimed. Nothing short-circuits either, and nothing
needs to — both are control paths measured in single flits, and a short-circuit
would be a fifth outbound source on every port's mux.

> **The classification is the hub's; the client behind it is the processor's.**
> Both control complexes are live clients at `(0,0)`, and the difference is what
> sits behind the port. The RV32 complex's client is a compute-unit shell with
> finite instruction and receive queues, so its busy signal is a real function of
> occupancy. The RV64 complex's is a dispatch mailbox that **never raises
> busy** — a completion it has no room for is accepted and dropped behind a
> sticky flag rather than held — so the backpressure term on that client folds to
> a constant inside the hub. Measured, that shows in the hub's own logic:
> **201 LUT** in the RV64 node against **320** in the RV32 one, with the skid on
> the encapsulator path at 313 and 311 and whole-hub totals of **514** and
> **631**. Out-of-context synthesis on `xcvu13p-fhgb2104-2L-e`, Vivado 2024.2,
> at 3.333 ns, `PORTS=2`, from `ooc_sysnode_rv64.tcl 2` and `ooc_sysnode.tcl`.
> Synthesis, not routed.

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

**Measured, out-of-context synthesis on `xcvu13p-fhgb2104-2L-e`, Vivado 2024.2,
at 3.333 ns, design state Synthesized, `sysnode` whole at `PORTS=2`** —
hierarchical LUT rows from `ooc_sysnode_rv64.tcl 2` and `ooc_sysnode.tcl`.
Nothing here is routed:

| instance | RV64 node | RV32 node | what it is |
|---|---|---|---|
| `sn_hub` | 514 | 631 | the hub, including the encapsulator skid |
| `noc_orchestrator` | 2,240 | 2,201 | the control agent |
| `mag_ilink` + `mag_switch` + link skids | 3,907 | 3,865 | the interlink, enabled |
| `mag_dram_port` | 1,993 | 1,964 | the one AXI master and its clock crossing |
| `mag_stage_port` | 4,093 | — | the converged staging path, with its 64 URAM and its byte-enabled banks |

The staging row exists only in the RV64 run because that is the run with
`STAGE_AT_PORT=1`; in the RV32 run the same store is inside the memory ports and
is charged there instead. **These are attribution on a `rebuilt` netlist**, so a
leaf may be charged to the instance it was re-parented into — the whole-node
totals are the exact figures.

**The two columns are not the same vintage.** The RV64 column is the run of
2026-08-26 23:46; the RV32 column is one of the same morning, and two modules
both configurations share changed between them — `mag_mem_port`'s write-slot
data array and `mm_prng`'s multiplies. Neither is on this table, so these rows
are comparable, but the whole-node totals the two runs report are not.
[control-processor](control-processor.md#cost--measured) states that in full.

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

**`noc_orchestrator.v` — the control agent — lives in `src/kohakuaccel/noc/`.** It is
instantiated by exactly one module, `mag.v`, and belongs with the control plane,
not with the router.

**`sn_hub.v` sits under `sysnode/core/`, beside `mag.v`.** It is neither the
agent's nor the engines' — it is the node's — so `core/` is where it landed
rather than a directory of its own.

**The interlink is packaged inside the memory agent.** `mag_link.v`,
`mag_link_pipe.v`, `mag_switch.v`, `mag_ilink.v` and `il_pkt_arb.v` implement a
second routing layer with its own topology, its own deadlock argument and its
own credit protocol. They live here because MAG hosts the endpoint. Their
description is in [ship](../ship/), and that is where the package boundary
should be too.

**The node presents exactly one AXI master.** Requesters speak an internal
protocol — `q_valid/q_ready/q_addr/q_len/q_write` plus `w_*` and `r_*` streams —
and `mag_dram_port.v`, instantiated inside `mag.v`, is the single converter that
arbitrates them, packs slave width to master width, and carries byte strobes.
There is no second AXI master anywhere in the agent: AXI is heavy, so it appears
once, at the boundary. `mag_stage_port.v` claims staged traffic off that same
converged path before it reaches DRAM. See [axi](../axi.md) for the overlap
between `mag_dram_port.v` and `axi_n1.v`.
