---
title: Flits and links
summary: The one-cycle message, which of its fields belong to whom, the handshake that carries it, and the two kinds of flow control.
tags:
  - architecture
  - noc
  - protocol
---

# Flits and links

A **flit** is the unit of everything in this fabric: one fixed-width message
that occupies one link for exactly one cycle. It is not a packet that gets
fragmented and it is not a bus transaction with separate address and data
phases — it is one word, moved once.

This page says what is in a flit, who owns each part of it, and the wire
discipline that moves it. The **bit-exact encoding is normative and lives in
[spec/flit-format](../../spec/flit-format.md)**; what follows is the structure
and the reasoning, which is what `arch/` is for.

## The shape of a flit

A flit is a **header** followed by a **payload**. At the shipped width —
`FLIT_WIDTH = 288`, `POS_WIDTH = 4` — that is a 32-bit header and a 256-bit
payload, and the payload width is chosen so that one memory response entry is
exactly one flit.

| field | width | who owns it |
|---|---|---|
| `dst_x`, `dst_y` | `POS_WIDTH` each | **the fabric.** The only fields the router reads |
| `src_x`, `src_y` | `POS_WIDTH` each | **the fabric**, stamped by the sender's shell. This is how a reply finds its way home without anyone being configured with a coordinate |
| `type` | 4 bits | **the fabric.** The message class, below |
| `txn_id` | 8 bits | **the requester.** Echoed back on the response; the fabric never interprets it — except on a flit marked for another mesh, where the framework claims the field to carry the destination coordinate in the far mesh |
| `last` | 1 bit | **the requester.** Marks the end of a batch or a burst |
| `rsvd` | 3 bits | **the fabric.** One bit marks a flit for another mesh — read by the edge complex, never by the router — and the other two carry the destination mesh id on such a flit, or the word index within an entry on a memory read response |
| payload | the rest | **the message class**, and below that, you |

**The router reads `dst_x` and `dst_y` and nothing else.** Everything else is
opaque to it, which is what keeps the router small and lets the message set
change without touching the routing logic.

## Message classes

The classes exist so that an endpoint can demultiplex without a table. There are
ten codes in use:

| group | classes | contract with |
|---|---|---|
| memory | `MEM_RD_REQ`, `MEM_WR_REQ`, `MEM_RD_RESP`, `MEM_WR_ACK`, `MEM_WR_DATA` | [sysnode](../sysnode/), written out in [spec/memory-protocol](../../spec/memory-protocol.md) |
| compute unit | `CU_INST`, `CU_SIGNAL`, `CU_CTRL` | the compute unit. These three are the ones `noc_cu_base` handles for you — [spec/compute-unit-port](../../spec/compute-unit-port.md) and [spec/control-registers](../../spec/control-registers.md) |
| unit to unit | `CU_DATA` | the two units at either end |
| error | `ERROR` | the fabric |

One structural fact about the encoding is worth stating because it used to be
otherwise: **no single bit partitions memory traffic from compute-unit
traffic.** Five memory messages do not fit in four codes, and no cache exists
that would want to filter on such a bit, so the split is a comparison against a
threshold rather than a mask.

### What each payload carries, in shape

The envelope is fixed; what the classes agree to put inside it is the part
worth understanding.

- **A memory request** is a 40-bit address, a length in payload flits, and a
  small flags field — cacheable, invalidate, flush. The address itself is
  structured: one bit selects a command aperture instead of DRAM, two bits
  carry the mesh id, and the rest is a local offset
  ([address-map](../../address-map.md)).
- **A unit-to-unit payload** is *(which buffer, where in it, how much)* — a
  buffer index, an offset in 32-byte granules, a length, and flags. The network
  never learns what a buffer index means, which is why the same shape works for
  a unit with four operand buffers and one with two.
- **An instruction** is a length, a class, and a body the framework carries and
  never reads.
- **A signal** is a code and a 32-bit argument. Codes below a central threshold
  are allocated by the framework, so a controller can act on any unit's
  completion without knowing what the unit is; codes above it are the unit's,
  and the argument is always the unit's.

### What a flit deliberately does not carry

- **No burst type.** A memory request names an address and a length and nothing
  else. There is no way to express an AXI `WRAP` or `FIXED` burst, and the
  memory port at the far end drives `INCR` unconditionally. A master upstream
  that issues `WRAP` or `FIXED` has the burst type dropped at the AXI boundary
  and **executed as `INCR`** — a simulation assertion in the AXI shim reports
  it, and silicon does not. See [axi](../axi.md#what-the-boundary-drops).
- **No sequence number.** Ordering per source–destination pair comes from the
  routing being deterministic ([routing](routing.md)), so nothing has to be
  numbered and reassembly counts instead.
- **No length that the router understands.** A multi-flit message is a run of
  independent flits that happen to arrive in order. The router has no notion of
  a packet and never holds one flit waiting for another.
- **No priority or class field the router acts on.** There are no virtual
  channels. Everything that would need them is handled by end-to-end credit at
  the endpoints instead.
- **No error or retry field.** The link handshake retries at the wire level;
  above that, a failure is a message, not a flag.

## The link handshake, and why both halves are mandatory

```
sender:    assert valid, hold valid and data unchanged until a cycle
           in which busy is low. That cycle is the transfer.
receiver:  accept iff (valid && !busy). Once.
```

Neither half works alone, and the failure modes are asymmetric:

- A sender that **gives up** loses a flit. It commits against `busy` at T,
  presents at T+1, and the receiver raised `busy` at T+1. The flit is gone.
- A receiver that accepts **unconditionally** duplicates one, because every
  sender holds `valid` until it sees `!busy` — so a write on every cycle with
  room enqueues the same flit repeatedly.

In a memory system either error is silent and permanent. A duplicated write
beat overruns its slot's expected count; a dropped one leaves the slot short
forever, so the source's next descriptor opens a second slot and its data binds
to the older one.

This is also why the input FIFO's `wr_almost` output being no margin at all is
survivable. The shared FIFO wrapper passes `USE_ADV_FEATURES(0)`, so the vendor
primitive ties its programmable-full flag low and `wr_almost` reduces to plain
`full`. **What makes plain `full` safe is the retry, not a margin.** Anything
that needs a real margin counts for itself — [sysnode](../sysnode/) does.

The input port carries a simulation check for exactly this: it remembers a flit
that was offered while busy and reports if the same flit is not offered again
next cycle. Testing *that*, rather than "was a flit offered into a full
buffer", is what separates a sender retrying — the normal steady state under
backpressure — from one that gave up.

## Two kinds of flow control, for two different failures

**Hop-by-hop** is the link handshake above. It stops buffer overflow.

**End-to-end credit** stops *protocol* deadlock, which hop-by-hop cannot touch.
If a node's input fills with requests and it cannot inject the response that
would drain them, the fabric locks — and that is a dependency between message
classes, which routing does not address
([routing](routing.md#what-this-does-not-cover)). The rule is that a requester
may not issue a request whose response it cannot absorb, and a dispatcher may
not send an instruction the target's queue cannot hold.

Credits therefore live at the **endpoints**, not in the router. The router
contains no counter and no notion of message class. This is the single most
important thing to understand about the fabric's cost: making it deadlock-free
cost logic at the edges and nothing in the middle.

One counting rule generalises past this fabric and is worth carrying into any
shim you write against it: **reserve in units of what the queue actually
holds.** The AXI station shim reserves response space in *flits*, not in AXI
beats, because a wide manager's single beat becomes several flits — a queue
sized in beats looks generous and hangs the port
([axi](../axi.md#credit-is-reserved-in-flits-not-beats)).

## Conventions

**Hold credits per destination, and stall locally.** *(Forced.)* The protocol
requires that a requester never issue a request whose response it cannot
absorb. How many credits you hold is yours; that you hold them is not.
Deviating deadlocks the fabric under load, and the symptom appears at a node
that did nothing wrong.

**Take your flit type codes from `noc_pkt.vh`, never from a neighbouring
module.** *(Free, and the one most worth obeying.)* The type field is fixed
protocol, but nothing in the build enforces it, so every module that restates a
code is a chance to restate it wrongly. That has now happened twice in this
tree — see below.

**Let unit-to-unit payloads be *(which buffer, where in it, how much)*.**
*(Free.)* The envelope is fixed; the meaning of a buffer index is yours and the
network never interprets it. Publish what your indices mean as part of your
unit's contract.

**Signal codes below the central threshold are allocated; yours start above it,
and the argument is always yours.** *(Half forced.)* The allocation is protocol
so a controller can act on any unit's completion without knowing the unit. What
you attach to the event is free.

## Where today's source disagrees

**The flit layout is fixed protocol enforced only by convention.** This is the
sharpest illustration in this system of why the four categories are worth
keeping apart, so it is worth stating in full.

`src/kohakuaccel/noc/noc_pkt.vh` exists and is correct. It defines every header
field position, every message class and the descriptor payload layouts. It is
also **included by nothing** — `` `include `` appears zero times anywhere in
`src/`. Every module restates the same constants as local parameters or local
macros: `noc_cu_base.v`, `noc_cu_null.v`, `noc_l2_adapter.v`, `mag_mem_port.v`,
`mag.v`, `noc_orchestrator.v`, `mag_ilink.v` and `vec_cu.v`. The driver
restates them again in `driver/kohakuaccel/device/flit.py`. `mag_ilink.v` says
so at the point of restatement:

> `// Flit header positions, restated from noc_pkt.vh -- nothing includes it.`

So a layout that *is* protocol is held together by several modules and a Python
file agreeing by hand. The header documents its own failure, and the divergence
it exists to prevent has already happened once:

> `// INCLUDED BY NOTHING -- every module re-declares these, so a divergence is`
> `// silent, and one happened: CU_DATA was 0x4 here while mag_mem_port.v and`
> `// vec_cu.v used 0x4 for MEM_WR_DATA, so a CU_DATA flit reaching MAG would`
> `// have entered the write queue as data. Resolved in favour of the silicon.`

That is the concrete form of "routes plausibly and means something else": a
flit of one class silently consumed as another, in a queue that had no way to
know.

**Including it is not a no-op, which is why nobody has.** The header's
positions are literals — `287:284`, `255:222` — so it is correct only at
`FLIT_WIDTH = 288` and `POS_WIDTH = 4`, while every module computes its
positions from parameters. Including it as it stands would silently constrain
the mesh to one flit width. It has to be parameterised before it can become the
thing it claims to be. Its own first line also points at a specification path
this documentation tree replaced.

**And the convention has already failed a second time, inside the framework's
own module.** `noc_cu_null.v` declares `T_CU_DATA = 4'h4`, while `noc_pkt.vh`
says `CU_DATA` is `4'h8` and `4'h4` is `MEM_WR_DATA`. The module builds real
flits with that code, and the header's own memory-class test — *type code at or
below `0x4`* — classifies them as memory traffic. It is the *same* divergence
the header records as having happened once already: fixed in the shipping
units, left unfixed here.

It is harmless in practice. `noc_cu_null` is instantiated only by
`src/kohakuaccel/verif/noc_tile_1r.v` and
`src/kohakuaccel/verif/noc_cluster_2x2.v`, both measurement tops with no memory
agent present, so no flit it emits is ever classified by anything and no
measurement is invalidated. It is also one of the two reasons the module is
described in
[compute-unit-port](compute-unit-port.md#the-measurement-instrument) as an
instrument only. A new author who copied it as a skeleton would ship a mistyped
flit on day one, and would find out when their unit's first unit-to-unit
message arrived at a memory port's write queue as data.

**This is the argument for the four categories in one file.** The flit layout
is fixed protocol. It is enforced by convention. The convention is several
modules and a Python file agreeing by hand, and it has now failed twice — once
in a shipping path, once in the framework's own module. A category that says
"fixed" while the mechanism says "convention" is exactly the gap worth naming.
