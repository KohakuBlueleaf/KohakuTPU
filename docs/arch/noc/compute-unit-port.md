---
title: The compute-unit port
summary: noc_cu_base, the handshake a datapath is written against, and the five properties that constrain how it may behave.
tags:
  - architecture
  - noc
  - compute-unit
---

# The compute-unit port

This is where the framework earns its keep. Everything else in this system is
infrastructure that exists so `src/kohakuaccel/noc/endpoint/noc_cu_base.v` can offer a small,
stable handshake — and so that **you never have to work out how to connect to
the fabric**. You still write the whole unit; you do not write the connection.

The handshake:

```
    inst_flit / inst_valid / inst_ready     one instruction to execute
    exec_done / exec_result / exec_fault    report it retired
    send_* / recv_*                         everything that is not an instruction
    dbg_ctr                                 two counters only you can know
```

A unit author writes a datapath against those signals. Framing, routing,
completion reporting and capability discovery are already done. Four properties
of the module are worth understanding because they constrain how a datapath may
behave.

**The reply address comes from the instruction.** `noc_cu_base` latches
`src_x` / `src_y` / `txn_id` / `last` from the `CU_INST` flit it issues, and
answers there. A unit is never configured with its controller's coordinate, so
moving the controller is not a rebuild of every unit.

**Completions are queued, not held.** A datapath can retire faster than a busy
outbound link drains. One holding register would let each completion overwrite
the last, and each lost completion is a lost credit — so the dispatcher stalls
forever and nothing says why. Instruction issue therefore also stops when the
signal queue is full: an instruction that executed but cannot be reported is
worse than one that never issued.

**A datapath must not raise `exec_done` in the same cycle it raises
`inst_ready`.** The retire arm would win, `in_flight` would drop, and the new
instruction's own completion would find it low and never be queued. Leave a
cycle between them. Every unit in the reference project does, and the
component bench counts accepted instructions against completions to keep it
that way.

**Control-register reads are answered here, not in the datapath.** That is what
lets a controller enumerate a unit it has never heard of: capabilities, status
with live instruction-queue space, a busy-cycle and retired-instruction pair
counted identically for every unit type, and one 64-bit word the datapath
supplies. Counting cycles in the framework rather than in each unit is
deliberate — wall clock cannot substitute when a single debug read costs
milliseconds against microseconds of compute.

**Transmit arbitration has a fixed priority: signals, then control replies,
then the datapath.** Signals win because they return dispatch credits, so
starving them stalls the controller. The `tx_free` term covers the output
register being emptied *this* cycle rather than merely idle, because deciding
from `!noc_out_busy` alone pops a signal against a link that may be busy by the
time the flit is presented — and a lost signal never returns its credit.

## The measurement instrument

`src/kohakuaccel/noc/endpoint/noc_cu_null.v` attaches to the fabric and computes nothing. Its
job is to isolate what being *connected* costs before any arithmetic exists —
the number that decides between many small units and few large ones. Subtract it
from a real unit and the remainder is genuinely compute.

It is written to defeat synthesis pruning, which is the whole reason it can be
trusted as an instrument: every bit of both flits folds into an output, and
traffic originates from external inputs so the mesh cannot be proven idle and
constant-folded.

**It is an instrument, not a template.** Do not copy it as a starting skeleton —
it carries a mistyped flit code, described in
[flits-and-links](flits-and-links.md#where-todays-source-disagrees). For a
worked starting point, see
[integrate/compute-unit](../../integrate/compute-unit.md).

## What the port does not constrain

The port says how you receive and send. It says nothing about what is behind it.

Your unit's memories — how many, how wide, how deep, what read latency, which
storage primitive, how banked — are entirely your design. Two units in the
reference project have operand memories of 928 and 256 bits, different memory
counts, and different read latencies, and both are ordinary conforming nodes.
Nothing in `noc_cu_base` knows or cares.

If a page anywhere in this tree reads as though the framework supplies your L1,
it is wrong.

## Conventions

**Start from a shipping unit's port wiring, not from the instrument and not from
the spec.** *(Free.)* `noc_cu_base` already implements the retire-cycle rule and
the hold-until-not-busy rule; a unit that instantiates it and follows the worked
example in [integrate/compute-unit](../../integrate/compute-unit.md) gets both
right. Starting from the spec means rediscovering them, and both fail silently.
Starting from `noc_cu_null.v` inherits a known defect — see
[flits-and-links](flits-and-links.md#where-todays-source-disagrees).

**Keep one instruction in one flit if you can.** *(Free.)* The header takes its
fixed slice and the rest of the payload is yours. Continuation flits are
supported and nothing in the reference project has needed them. A single-flit
instruction makes dispatch accounting exactly one credit per instruction, which
is the case every tool in the tree assumes when reading counters.

**Put your opcode where the framework's demultiplex does not look.** *(Free, but
narrow.)* The type field routes your flit to the instruction queue; your payload
is then untouched. Reusing header bits for your own meaning works right up until
a framework version starts reading them.

**Report `dbg_ctr`, even if the count is arbitrary.** *(Free.)* Busy cycles and
retired instructions are counted for you, identically for every unit type. The
one 64-bit word you supply is the only unit-defined observability the control
plane has, and tying it to zero is a decision to be blind during bring-up.

The conventions that govern what you put *in* a flit — credits, type codes,
unit-to-unit payload shape, signal codes — are in
[flits-and-links](flits-and-links.md#conventions).

## What a compute-unit author must know

1. **You get one instruction at a time.** Depth is in the FIFO, not in your
   datapath. If you want to overlap, overlap inside your unit.
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
