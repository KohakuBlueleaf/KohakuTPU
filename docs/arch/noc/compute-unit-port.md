---
title: The compute-unit port
summary: noc_cu_base — the shell every endpoint carries, the handshake a datapath is written against, and the properties that constrain how it may behave.
tags:
  - architecture
  - noc
  - compute-unit
---

# The compute-unit port

A **compute unit** is anything that attaches to one fabric port, accepts
instructions one at a time, names the memory it wants as an address and a
length, and signals retirement. Nothing in the fabric knows whether it
multiplies, sorts, or runs a program.

`src/kohakuaccel/noc/endpoint/noc_cu_base.v` is the **shell** that makes an
endpoint one. It sits between a router's local link and your datapath, and it
is where the framework earns its keep: everything else in this system is
infrastructure so that this module can offer a small, stable handshake, and so
that **you never have to work out how to connect to the fabric**. You still
write the whole unit. You do not write the connection.

```
    router local port
          |
    +-----v---------------------------------+
    |  noc_cu_base                          |
    |    inbound demux -> instruction FIFO  |
    |                 -> receive FIFO       |
    |                 -> CU_CTRL, answered  |
    |                    here               |
    |    outbound arbiter <- completions    |
    |                     <- ctrl replies   |
    |                     <- your traffic   |
    +-----+---------------------------------+
          |
    your datapath
```

The handshake it presents:

```
    inst_flit / inst_valid / inst_ready     one instruction to execute
    exec_done / exec_result / exec_fault    report it retired
    send_* / recv_*                         everything that is not an instruction
    dbg_ctr                                 two counters only you can know
    inst_space / busy                       visibility, for whoever dispatches
```

A unit author writes a datapath against those signals. Framing, routing,
completion reporting and capability discovery are already done. The
signal-level contract is normative in
[spec/compute-unit-port](../../spec/compute-unit-port.md); this page is what
the module does and why, which is what constrains how a datapath may behave.

## Six properties that constrain your datapath

**1 · The reply address comes from the instruction.** The shell latches the
source coordinate, transaction id and last-marker from each `CU_INST` flit it
issues, and answers there. A unit is never configured with its controller's
coordinate, so moving the controller is not a rebuild of every unit.

**2 · One instruction is in flight, and issue stops for three reasons.** The
shell offers an instruction only when its queue is non-empty, nothing is in
flight, **and the completion queue is not full**. That third condition is the
one worth knowing: an instruction that executed but cannot be reported is worse
than one that never issued, because the report is what returns the
dispatcher's credit.

**3 · Completions are queued, not held.** A datapath can retire faster than a
busy outbound link drains. One holding register would let each completion
overwrite the last, and each lost completion is a lost credit — so the
dispatcher stalls forever and nothing says why. The queue is **16 entries**,
fixed rather than parameterised.

What a completion carries is decided by the shell, not the datapath:

| condition | signal code | argument |
|---|---|---|
| the datapath raised `exec_fault` | fault | your `exec_result` |
| the instruction was marked last in its batch | batch complete | the batch's program id, taken from the instruction |
| otherwise | instruction complete | your `exec_result` |

**4 · A datapath must not raise `exec_done` in the same cycle it raises
`inst_ready`.** The retire arm would win, the in-flight flag would drop, and
the new instruction's own completion would find it low and never be queued —
after which the unit accepts nothing further. Leave a cycle between them. Every
shipping unit does, and the component bench counts accepted instructions
against completions to keep it that way.

**5 · Control-register reads are answered by the shell, and one at a time.**
`CU_CTRL` never reaches the datapath. That is what lets a controller enumerate
a unit it has never heard of:

| index | contents |
|---|---|
| 0 | capabilities: unit type, version, buffer count, instruction-queue depth |
| 1 | status: busy, and **live** instruction-queue space |
| 2 | busy cycles and retired instructions, counted **identically for every unit type** |
| 3 | the one 64-bit word your datapath supplies as `dbg_ctr` |

Any other index reads zero.

**A second control read arriving while a reply is still pending is dropped.**
The shell holds exactly one pending reply, and a `CU_CTRL` flit that arrives
while it is occupied is taken off the link and discarded — inbound backpressure
covers the two FIFOs, not this. So a controller must not have two control reads
outstanding to the same unit; there is no error and no reply, only silence.

Counting cycles in the framework rather than in each unit is deliberate: wall
clock cannot substitute when a single debug read costs milliseconds against
microseconds of compute.

**6 · Transmit arbitration has a fixed priority: completions, then control
replies, then your datapath.** Completions win because they return dispatch
credits, so starving them stalls the controller. Your `send_ready` is low while
either of the other two is pending — that is not a bug to work around, it is
the priority.

The free-to-send term covers the output register **being emptied this cycle**
rather than merely being idle. Deciding from "the link is not busy" alone pops
the completion queue against a link that may be busy by the time the flit is
presented, and a lost completion never returns its credit.

## Two more behaviours worth knowing

**Inbound backpressure is raised when *either* queue is full**, not when the
one this flit is headed for is full. The reason is that the busy signal has to
be meaningful in cycles when no flit is present at all, and the type field is
only trustworthy alongside a valid flit — so the shell cannot wait to see which
queue a flit wants before deciding whether it has room.

**`busy` means "not finished", not "executing".** It is true while an
instruction is in flight, while the instruction queue is non-empty, **or while
a completion has been generated but has not yet left the module.** A controller
polling `busy` therefore sees false only when the unit has genuinely nothing
outstanding on the wire.

## The measurement instrument

`src/kohakuaccel/noc/endpoint/noc_cu_null.v` attaches to the fabric and
computes nothing. Its job is to isolate what being *connected* costs before any
arithmetic exists — the number that decides between many small units and few
large ones. Subtract it from a real unit and the remainder is genuinely
compute.

It is written to defeat synthesis pruning, which is the whole reason it can be
trusted as an instrument: every bit of both flits folds into an output that
reaches a port, so no part of a flit path is dead, and traffic originates from
external inputs so the mesh cannot be proven idle and constant-folded.

**It is an instrument, not a template. Do not copy it as a starting skeleton.**
Its own header still describes it as one; the header is wrong, for two
independent reasons:

- **it carries a mistyped flit code** — it builds unit-to-unit flits with the
  code the protocol assigns to memory write data
  ([flits-and-links](flits-and-links.md#where-todays-source-disagrees));
- **it violates property 4 above.** It raises `inst_ready` and `exec_done` in
  the same cycle, which is exactly the case the shell cannot absorb: the
  completion is never queued and the in-flight flag never clears, so the unit
  accepts one instruction and then nothing.

Neither defect affects any measurement. `noc_cu_null` is instantiated only by
`src/kohakuaccel/verif/noc_tile_1r.v` and
`src/kohakuaccel/verif/noc_cluster_2x2.v`, which are synthesis-only tops — the
logic is elaborated and counted, never run, and there is no memory agent
present to misclassify a flit. It is a live trap for anyone who copies the
file, and no trap at all for the number it exists to produce.

For a worked starting point, see
[integrate/compute-unit](../../integrate/compute-unit.md).

## What sits between the shell and the router

The local link between a router and an endpoint is a place two optional modules
can be inserted, and both present the same six signals on each face, so
removing them is a straight wire:

| module | what it does |
|---|---|
| `noc_l2_adapter` | **customizable addon.** Explicit staging in the local link — no tags, an address is in the programmed window or it is not, and a run that leaves the window is forwarded whole. Its control plane is `CU_CTRL`, the framework's own class, so it speaks no unit's vocabulary and the shell behind it never sees the control flits it terminates |
| `noc_local_cdc` | one direction of a local link across two clocks, so a unit may run on its own clock while router-to-router stays one domain — [README](README.md#one-clock-per-mesh-and-one-exception) |

## What the port does not constrain

The port says how you receive and send. It says nothing about what is behind
it.

Your unit's memories — how many, how wide, how deep, what read latency, which
storage primitive, how banked — are entirely your design. Two units in the
reference project have operand memories of 928 and 256 bits, different memory
counts and different read latencies, and both are ordinary conforming nodes.
Nothing in the shell knows or cares.

If a page anywhere in this tree reads as though the framework supplies your L1,
it is wrong.

## What the port deliberately does not do

- **It does not give you more than one instruction at a time.** Depth is in the
  FIFO, not in your datapath. Overlap inside your unit if you want overlap.
- **It does not reassemble multi-flit messages.** A run of flits arrives in
  order on your receive port; making a descriptor and its data into one object
  is yours.
- **It does not interpret your instruction or your payload.** It reads the
  header and nothing else.
- **It does not hold credits for you.** The shell publishes live queue space so
  a dispatcher can count; counting is the dispatcher's job
  ([flits-and-links](flits-and-links.md#two-kinds-of-flow-control-for-two-different-failures)).
- **It does not time out, retry, or report an error class.** A unit that stops
  answering stops answering; the framework has no watchdog.
- **It does not cross clock domains.** That is `noc_local_cdc`, outside it.

## Conventions

**Start from a shipping unit's port wiring, not from the instrument and not
from the spec.** *(Free.)* The shell already implements the retire-cycle rule
and the hold-until-not-busy rule; a unit that instantiates it and follows the
worked example in [integrate/compute-unit](../../integrate/compute-unit.md)
gets both right. Starting from the spec means rediscovering them, and both fail
silently. Starting from `noc_cu_null.v` inherits two known defects.

**Keep one instruction in one flit if you can.** *(Free.)* The header takes its
fixed slice and the rest of the payload is yours. Continuation flits are
supported and nothing in the reference project has needed them. A single-flit
instruction makes dispatch accounting exactly one credit per instruction, which
is what every tool in the tree assumes when reading counters.

**Put your opcode where the framework's demultiplex does not look.** *(Free,
but narrow.)* The type field routes your flit to the instruction queue; your
payload is then untouched. Reusing header bits for your own meaning works right
up until a framework version starts reading them.

**Report `dbg_ctr`, even if the count is arbitrary.** *(Free.)* Busy cycles and
retired instructions are counted for you, identically for every unit type. The
one 64-bit word you supply is the only unit-defined observability the control
plane has, and tying it to zero is a decision to be blind during bring-up.

The conventions that govern what you put *in* a flit — credits, type codes,
unit-to-unit payload shape, signal codes — are in
[flits-and-links](flits-and-links.md#conventions).

## What a compute-unit author must know

1. **You get one instruction at a time.** Depth is in the FIFO, not in your
   datapath.
2. **Do not raise `exec_done` and `inst_ready` in the same cycle.**
3. **Hold `valid` and data until `busy` is low, on every port you drive.** If
   you write anything that touches a link directly, this is the rule that
   matters most.
4. **Your instruction encoding is yours.** The framework carries it and never
   reads it. What it does read is the header: destination, source, type,
   transaction id, last.
5. **Report retirement even when there is nothing to say.** The signal is what
   returns the dispatcher's credit.
6. **Tie `dbg_ctr` to zero if you have nothing to report.** It is read the same
   way for every unit type, so leaving it unconnected loses you the only
   unit-defined observability the control plane has.

The step-by-step version of this list, with the code, is
[integrate/compute-unit](../../integrate/compute-unit.md); the checkable form
is [integrate/conformance](../../integrate/conformance.md).
