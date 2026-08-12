---
title: Bringing up
summary: Bitstream to first correct result — the debug surface, the order to check things in, and how to tell a build problem from an RTL problem from a driver problem.
tags:
  - workflow
  - bringup
  - debug
---

# Bringing up

Bring-up is the walk from "the bitstream programmed" to "the machine computed the
right answer". It is where three separately-verified things meet for the first
time — the build, the RTL and the driver — and where a fault in any of them
presents identically: nothing happens, or a plausible wrong number appears.

The whole discipline is therefore about **ordering checks so that each one can
only fail for one reason.**

## The governing principle

> Every check should have exactly one new thing in it.

A first run that exercises the host interface, the address map, the dispatch
mechanism, the compute unit, the memory path and the readback is not a test. It
is six tests wired in series, reported as one bit, and when it fails it tells you
nothing.

The ladder below adds one layer per rung. Whichever rung first fails names the
layer that is broken — which a single end-to-end run cannot do.

## The debug surface

Know what you have to poke with **before** you need it. In practice there are
four instruments and they are not interchangeable.

### A debug master

A host-independent path that can read and write the on-chip bus. Vendor
JTAG-to-AXI is the usual one, and it is worth its cost because **it works before
the host has enumerated the card** — so it separates "the fabric is wrong" from
"the host link is wrong", which is the very first question.

It is a control-plane instrument. Expect it to be several orders of magnitude
slower than the production data path: minutes for a transfer the real path does
in milliseconds. Any driver exposing one should carry a size guard that refuses a
transfer large enough to be a surprise, with a message saying how long it would
take and how to raise the limit deliberately.

**That guard is a guard, not a hardware limit.** A measurement that means to pay
the cost should raise it explicitly rather than trip over it.

### One register whose correct value is known in advance

This is the single most valuable thing in the design for bring-up.

A constant capability register — an interface width, a magic number, a grid
size — is **the only register whose right answer is known before the machine has
ever run**. Reading it separates:

- the card is not there / the address decode is wrong (reads zero, or all-ones)
- something is there but it is not what you think (reads a plausible-looking
  wrong value)
- the machine is present and answering (reads exactly the expected constant)

Every later failure hides that distinction. Read it first, always, and make it
the first thing a `--probe` mode does.

Encode identifying fields into it where you can — a grid dimension, a version.
Then the same read that proves presence also proves the **software's description
matches the bitstream**, which is the second most common bring-up fault.

### A raw message injector

A mailbox that can put an arbitrary message onto the on-chip network and read
whatever comes back. It exists for bring-up specifically, because it can inject
anything — including something malformed — which an address-mapped bridge could
never do.

This is what makes an unknown endpoint **enumerable rather than hardcoded**: ask
each coordinate for its identity register and see what answers. And it is the
only path to an endpoint that is not a dispatch, so it works before the dispatch
mechanism does.

### Status and counter registers

Per-unit status: is it busy, how much instruction queue is free, what was the
last signal it emitted. Per-unit counters: cycles busy, cycles stalled, requests
issued.

Counters are **the only honest cycle measurement available during bring-up**.
Wall clock cannot substitute when one debug-master access costs orders of
magnitude more than the work being measured — see [The measurement that is not
one](#the-measurement-that-is-not-one).

### Not on this list: waveform capture

An embedded logic analyser is available and is usually the wrong tool at this
stage. It costs a rebuild to change what it watches, its buffer is tiny compared
to the timescales involved, and it tells you about signals rather than about
state.

A register block and a message injector answer "what does the machine think is
happening", which is nearly always the question. Reach for waveform capture when
you have a *specific* signal-level hypothesis and no register that can confirm
it.

## The ladder

### Stage 0 — before the card is touched

These are build checks and they belong to [build.md](build.md), but they are
listed here because every one of them presents at bring-up as a hardware fault:

1. **Every top-level input belongs to an inferable interface, or is a clock or a
   reset.** An undriven input deletes everything behind it, silently, and the
   design still builds and programs.
2. **Every generated wrapper measures identical in area to what it wraps.** A
   mis-wire shows up as *smaller*.
3. **Wide addresses were formatted as wide addresses.** Otherwise the whole map
   is piled at the bottom of the address space.
4. **The software's description of the machine was generated, not transcribed**,
   from the same inputs the build consumed.

### Stage 1 — is there a link at all

Bring the debug transport up on its own.

- Can the driver reach the debug server / the device node? A missing tool server
  must be a distinct, named error — "no transport available" — and never a
  generic I/O error, because the two lead to completely different next steps.
- **Is the master the width you think?** A narrower debug master than the design
  expects makes every beat half a word, and the driver then writes a
  coherent-looking control program into the wrong half of every register. Check
  the width and refuse to proceed if it is wrong.

### Stage 2 — is the write path honest

This is the check most projects do not have, and it is the one that has caused
the most confusing failures.

**Baseline memory before trusting anything.** Write a known pattern to scratch
memory, read it back, and verify it byte for byte — as a preflight, at session
start, every session.

The specific failure it catches: a write-address queue and a write-data queue
that have gone out of step, so **every write lands at an address other than the
one requested, and the transport reports success**. Operands are shifted by a
fixed number of beats. Everything downstream reads as a compute fault.

A driver that can measure the skew should **refuse to run by default** when it is
non-zero, with a message saying what the shift is and that recovery is
reprogramming the fabric — not a soft reset, which restores status bits and not
queues. Offer compensation as an explicit opt-in that taints every result
produced under it.

### Stage 3 — identity

Read the known-constant register. Then check, in this order:

1. **The constant matches.** Something is there and it is the thing you think.
2. **The shape fields match the software's description.** A grid width that
   disagrees means the software is describing a different bitstream. Stop here —
   everything after this point will be wrong in ways that look like RTL faults.
3. **Every unit the description declares answers**, with its own type and version.
4. **The version gate passes.** If a unit reports a different interface version
   than the description was written for, **refuse**. This is the gate working, not
   a fault: the card is running a different bitstream and its capacities and
   instruction encoding may have moved. Reprogram, or regenerate the description
   from that build. Do not override it.

Then run the check the other way round: **sweep every coordinate, including ones
the description does not declare.** Enumeration can only find missing units; a
sweep finds *extra* ones — a unit that exists in the bitstream and not in the
software's description. A generated description looks authoritative enough that
nobody would think to check.

Two things make a full sweep cheap and safe: a message to a coordinate with no
endpoint is dropped rather than hanging, so an absent unit costs one timeout; and
the whole sweep is a few hundred register accesses.

**Watch for the self-echo.** Addressing the routing agent's own coordinate looks
exactly like an endpoint answering: the request comes back rather than being
answered. Check the reply's opcode and tag, not just that a reply arrived.

### Stage 4 — the datapath, one layer at a time

A ladder of increasingly complete operations, each adding exactly one thing:

| rung | what it adds | a failure means |
|---|---|---|
| **halt** | dispatch reaches the unit and it retires an instruction | the dispatch path or the unit's front end |
| **copy** | memory in, memory out, no arithmetic | the memory path or address generation |
| **compute** | the arithmetic, checked against a model | the datapath |
| **compute, all units** | every instance, not just the first | per-unit wiring or coordinates |
| **one real operation** | the whole chain, end to end | integration |

Whichever rung first fails names the broken layer. Running only the last one is
the mistake this ladder exists to prevent.

### Stage 5 — one real operation

The smallest complete operation the machine exists to do, at the smallest size
that is still meaningful, scored against a reference implementation.

Two rules for scoring it:

**Judge on the tail, never the median.** A median can look perfect while a
quarter of the elements are wrong. One observed case scored a near-perfect median
next to a maximum error of order one. A spot-check of the median would have
passed it. Report a distribution and a count of bad elements, not a single
number.

**Score against a model of the machine's own arithmetic**, not only against
double precision. Comparing to double precision folds the format's inherent cost
together with the machine's error, and the two need to stay separate — otherwise
a correct machine in a low-precision format is indistinguishable from a broken
one.

## Telling the three apart

The reason bring-up is hard is that a build fault, an RTL fault and a driver
fault present the same way. These are the discriminators that have actually
worked.

| symptom | class | why |
|---|---|---|
| Known-constant register reads zero or all-ones | **build** — address decode, or nothing is there | the value is known in advance; nothing else can explain it |
| Constant reads correctly, shape fields disagree | **description vs bitstream** | the machine is fine; the software is describing another one |
| Every address window overlaps at the bottom of the map | **build** — wide addresses truncated | validates and builds; surfaces as overlap |
| A whole subsystem is present but responds to nothing | **build** — an undriven input pruned the logic | nothing failed; it simply never ran |
| The bus hangs forever, no error anywhere | **RTL** — a response was never emitted | the bus protocol has no timeout |
| Version gate refuses | **wrong bitstream, or stale description** | the refusal is correct; do not override |
| Everything green, most output elements wrong | **description** — capacity drift | the units silently overran their real capacity |
| Results consistently displaced | **driver / transport** — write path skew | stage 2 catches it; nothing else will |
| Readback raises a bus error rather than returning data | **the machine wrote nothing there** | see [ECC](#ecc-turns-never-written-into-an-error) |
| A run never retires, per-unit counters name which unit | **RTL / dispatch** | the counters localise it |
| Wrong answer only above a certain size | **capacity or an encoding field overflowing** | walk the parameter across the boundary |
| Wrong answer on a fraction of runs, deterministic per run | **not timing** — same input, same output means logic | hash the output across repeated runs to establish it |

Three further diagnostics that repeatedly earn their keep:

**Determinism separates logic from timing.** Run the same input three times and
hash the output. Bit-identical means the fault is not marginal timing, and that
excludes an entire class of cause in one cheap measurement.

**Narrowing the parallelism separates distribution from computation.** If the
same fault appears with one unit active, it is not a multi-unit distribution
problem.

**Reading the emitted program beats inferring from the output.** When a driver
constructs a control program, disassemble and read it. A host-side construction
bug says so outright in the listing, and no amount of staring at wrong numbers
will. Print the failing case's listing next to a passing case's.

## Things that will cost you a session

### The first run after programming may fail

On some boards the first compute after programming is unreliable. **A failed
first compute is not evidence of a fault — re-run before concluding anything.**
This has already caused one session to declare a working card dead.

### There may be no soft reset

If reset is a signal the host cannot drive and there is no reset bit, then a
genuinely wedged machine needs the bitstream reloading. Know this before you need
it, and make the driver's timeout message say it, so nobody spends an hour
looking for the reset that does not exist.

The same applies to a hung bus: the debug master's own reset restores its status
bits and does not clear a stalled slave. Only reprogramming does.

### ECC turns "never written" into an error

Memory with ECC returns an **uncorrectable error** for a line that has never been
written, not zeros. A readback of a region the machine failed to write therefore
raises a bus error rather than returning wrong data.

That is a gift, not a nuisance: it is the difference between diagnosing a hang
and guessing at one. Make the driver catch it and say so explicitly. And offer a
prefill — mark the whole region with a recognisable pattern before a run — so
that "the machine wrote nothing" and "the machine wrote the wrong thing" are
distinguishable.

### Never reprogram while the host driver holds the device open

Reprogramming the fabric under a host driver that has the device mapped can take
the host down, and afterwards the device's registers read as all-ones. Close the
host side first.

### The measurement that is not one

When one debug-transport access costs orders of magnitude more than the work
being measured, **wall clock minus transport overhead is not a measurement**. It
is the difference of two large numbers whose noise is itself far larger than the
answer, and it can come out negative.

Report what the hardware's own counters say, or report nothing. A performance
figure that requires subtracting the instrument from the reading is not a figure.

### Two functions that compute the same thing

If a planner and a driver each decide something independently — a tile shape, an
address, a capacity — they will diverge, and the divergence will present as a
hardware fault. Reconcile them in one place and test that the two agree, flit for
flit, rather than testing each against its own expectations.

### The same encoding meaning different things at different units

If two unit types decode the same instruction bits differently, then a message
delivered to the wrong unit type **is not rejected** — it decodes as whatever
that unit's table says and executes. Make the type explicit in the encoding, or
make the tables disjoint.

## Exit codes are the first triage

A bring-up entry point should distinguish, by exit code, at minimum:

| code | meaning |
|---|---|
| 0 | passed |
| 1 | ran and the answer was wrong |
| 2 | refused before running — the request is not valid for this machine |
| 3 | no machine to run on — transport unavailable |
| 4 | the machine did not answer as expected |

Codes 3 and 4 are the ones that matter. Collapsing "there is no card" into "the
answer was wrong" sends people to debug arithmetic that never executed.

## A useful pattern: two emitters, one machine

If a project has two paths that produce instructions for the same hardware — a
planner and a compiler, say — then running both on the same problem and
comparing **separates a compiler fault from a hardware fault, because the only
difference is who emitted the instructions.**

That is worth arranging deliberately. It converts an unattributable wrong answer
into an attributable one.

## When it works

Record the result as a baseline: the shapes that ran, the error figures, the
counter values, and the exact commands. The next bitstream is diffed against
that sheet, and "is this better or worse than last time" is otherwise an argument
rather than a measurement.

## Open questions

- Clock retuning is arithmetic in the driver with no path to the device: there is
  no probe that checks the register offsets against the IP, and a wrong offset
  writes a divider into a status register and the clock simply never changes.
- The status register field layout is documented differently in the driver
  comment and the RTL. Only one field is ever polled, so nothing depends on it —
  but someone diagnosing a stall from that register will read the wrong field.
