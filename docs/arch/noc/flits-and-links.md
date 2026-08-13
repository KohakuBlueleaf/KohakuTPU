---
title: Flits and links
summary: The one-cycle message, its message classes, the handshake that carries it, and the two kinds of flow control.
tags:
  - architecture
  - noc
  - protocol
---

# Flits and links

The unit of everything in the fabric, and the wire discipline that moves it.

## Message classes

The router reads `dst_x` and `dst_y` and nothing else. Everything else in a
flit is opaque to it, which is what keeps the router small and lets the message
set change without touching the routing logic.

The classes exist so that endpoints can demultiplex without a table: memory
request, memory response, write data, instruction, signal, control-register
access, unit-to-unit data. The exact codes and field layouts are normative and
live in [spec/flit-format](../../spec/flit-format.md). What matters
architecturally is the split:

- **Memory classes** are the contract with [mas](../mas/), written out in
  [spec/memory-protocol](../../spec/memory-protocol.md).
- **`CU_INST` / `CU_SIGNAL` / `CU_CTRL`** are the contract with the compute
  unit, and they are the three that `noc_cu_base` handles for you. See
  [spec/compute-unit-port](../../spec/compute-unit-port.md) and
  [spec/control-registers](../../spec/control-registers.md).
- **Unit-to-unit data** is defined so that a payload can be addressed as
  *(which buffer, where in it, how much)* without the network knowing what a
  buffer index means. That abstraction holds for a unit with four operand
  buffers and for one with two.

Signal codes are centrally allocated below a threshold and unit-defined above
it, so a controller can act on any unit's completion without knowing what the
unit is, while the argument stays whatever the unit wants to say.

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

This is why `sync_fifo`'s `wr_almost` output being no margin at all is
survivable: `USE_ADV_FEATURES` is zero, so it reduces to plain `full`. What
makes plain full safe is the retry, not a margin. Anything that needs a real
margin counts for itself — [mas](../mas/) does, with `Q_MARGIN`.

## Two kinds of flow control, for two different failures

**Hop-by-hop** is the link handshake above. It stops buffer overflow.

**End-to-end credit** stops *protocol* deadlock, which hop-by-hop cannot touch.
If a node's input fills with requests and it cannot inject the response that
would drain them, the fabric locks — and that is a dependency between message
classes, which routing does not address. The rule is that a requester may not
issue a request whose response it cannot absorb, and a dispatcher may not send
an instruction the target's queue cannot hold.

Credits therefore live at the **endpoints**, not in the router. The router
contains no counter and no notion of message class. This is the single most
important thing to understand about the fabric's cost: making it deadlock-free
cost logic at the edges and nothing in the middle.

## Conventions

**Hold credits per destination, and stall locally.** *(Forced.)* The protocol
requires that a requester never issue a request whose response it cannot absorb.
How many credits you hold is yours; that you hold them is not. Deviating
deadlocks the fabric under load, and the symptom appears at a node that did
nothing wrong.

**Take your flit type codes from `noc_pkt.vh`, never from a neighbouring
module.** *(Free, and the one most worth obeying.)* The type field is fixed
protocol, but nothing in the build enforces it, so every module that restates a
code is a chance to restate it wrongly. That has now happened twice in this
tree.

**Let unit-to-unit payloads be *(which buffer, where in it, how much)*.**
*(Free.)* The envelope is fixed; the meaning of a buffer index is yours and the
network never interprets it. This shape holds for a unit with four operand
buffers and for one with two, which is why it is the one worth copying. Publish
what your indices mean as part of your unit's contract.

**Signal codes below the central threshold are allocated; yours start above it,
and the argument is always yours.** *(Half forced.)* The allocation is protocol
so a controller can act on any unit's completion without knowing the unit. What
you attach to the event is free.

## Where today's source disagrees

**The flit layout is fixed protocol enforced only by convention.** This is the
sharpest illustration in this system of why the four categories are worth
keeping apart, so it is worth stating in full.

`src/kohakunoc/noc_pkt.vh` exists and is correct. It defines every header field
position, every message class and the descriptor payload layouts. It is also
**included by nothing** — `` `include `` appears zero times anywhere in `src/`.
Every module restates the same constants as local parameters or local macros:
`noc_cu_base.v`, `mag_mem_port.v`, `mag.v`, `noc_orchestrator.v`, `mag_ilink.v`,
`vec_cu.v` and `synth_top/poc/l2_adapter.v`. The driver restates them again in
`driver/kohakuaccel/device/flit.py`. `mag_ilink.v` says so at the point of
restatement:

> `// Flit header positions, restated from noc_pkt.vh -- nothing includes it.`

So a layout that *is* protocol is held together by seven modules and a Python
file agreeing by hand. The header documents its own failure, and the divergence
it exists to prevent has already happened once:

> `// INCLUDED BY NOTHING -- every module re-declares these, so a divergence is`
> `// silent, and one happened: CU_DATA was 0x4 here while mag_mem_port.v and`
> `// vec_cu.v used 0x4 for MEM_WR_DATA, so a CU_DATA flit reaching MAG would`
> `// have entered the write queue as data. Resolved in favour of the silicon.`

That is the concrete form of "routes plausibly and means something else": a flit
of one class silently consumed as another, in a queue that had no way to know.

**Including it is not a no-op, which is why nobody has.** The header's positions
are literals — `287:284`, `255:222` — so it is correct only at `FLIT_WIDTH=288`
and `POS_WIDTH=4`, while every module computes its positions from parameters.
Including it as it stands would silently constrain the mesh to one flit width.
It has to be parameterised before it can become the thing it claims to be.

Two stale claims travel with it. `src/kohakunoc/README.md` describes it as the
"single source of truth", which nothing reads; and its own first line points at
a specification path this rewrite replaced.

**And the convention has already failed a second time, inside the framework's
own module.** `noc_cu_null.v:49` declares `T_CU_DATA = 4'h4`, while
`noc_pkt.vh:42` says `CU_DATA` is `4'h8` and `4'h4` is `MEM_WR_DATA`. Line 121
builds real flits with that code, and `NOC_T_IS_MEM(t)` — defined as
`(t) <= 4'h4` — classifies them as memory traffic. It is the *same* divergence
the header records at lines 27-30 as having happened once already: fixed in the
shipping units, left unfixed here.

It is harmless in practice. `noc_cu_null` is instantiated only by
`synth_top/noc_tile_1r.v` and `synth_top/noc_cluster_2x2.v`, both measurement
tops with no memory agent present, so no flit it emits is ever classified by
anything and no measurement is invalidated. It is also the reason the module is
described in [compute-unit-port](compute-unit-port.md#the-measurement-instrument)
as an instrument only. A new author who copied it as a skeleton would ship a
mistyped flit on day one, and would find out when their unit's first
unit-to-unit message arrived at a memory port's write queue as data.

**This is the argument for the four categories in one file.** The flit layout is
fixed protocol. It is enforced by convention. The convention is seven modules
and a Python file agreeing by hand, and it has now failed twice — once in a
shipping path, once in the framework's own module. A category that says "fixed"
while the mechanism says "convention" is exactly the gap worth naming.
