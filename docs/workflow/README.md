---
title: Workflow
summary: The loop from an RTL edit to a running machine — what each stage costs, what it tells you, and when skipping one is legitimate.
tags:
  - workflow
  - overview
---

# Workflow

**What this section is.** The other trees describe the machine. This one
describes the *practice* of getting a design onto it: the loop from an RTL edit
to a running device, what each stage costs, what it tells you, and how each one
lies if you set it up wrong. It is written as method — none of it depends on
building this particular accelerator.

Hardware has no `pip install`. There is no step where you take a dependency and
move on; every part of the machine has to be simulated, synthesised, measured,
placed, routed and brought up on real silicon, and each of those has its own
failure modes and its own hours.

This is where a project's schedule actually goes. It is also where a framework
saves the most time — but only if the practice is written down, because most of
what is expensive here is not difficult. It is just easy to get wrong in a way
that produces a plausible answer.

**Vocabulary.** A **ship** is one complete assembly floorplanned for a specific
device ([what is a ship](../arch/ship/what-is-a-ship.md)); a **mesh** is the
on-chip network and the compute units on it ([noc](../arch/noc/README.md)); a
**system node** is the single component serving one mesh with memory access and
dispatch ([sysnode](../arch/sysnode/README.md)); a **compute unit** is the block
you write ([compute-unit](../integrate/compute-unit.md)).

## The loop

    edit RTL
      |
      v
    lint                          seconds                 simulate.md
      |
      v
    simulate                      seconds to minutes      simulate.md
      |
      v
    measure out of context        seconds to minutes      measure.md
      |
      v
    assemble                      minutes                 build.md
      |
      v
    implement                     HOURS                   build.md, timing-closure.md
      |
      v
    bitstream
      |
      v
    bring up                      minutes, then days      bringup.md

Every stage before implementation exists to avoid discovering something during
implementation. That is the whole shape of the flow, and it follows from one
number: **placement and routing a large design takes most of a day, and a crash
or a mistake costs the whole run.**

## What each stage costs and what it tells you

| stage | cost | answers | does not answer |
|---|---|---|---|
| **lint** | seconds | does it elaborate — modules, ports, parameters | anything about behaviour |
| **software tests** | seconds | is the compiler / driver logic right | anything about hardware |
| **unit simulation** | seconds | is the arithmetic bit-exact | anything about handshakes |
| **module simulation** | seconds–minutes | does the block obey its protocol under stress | anything crossing a boundary |
| **system simulation** | minutes | does the assembly work end to end | timing, area, placement |
| **out-of-context synthesis** | seconds–minutes | is the logic depth compatible with the target frequency; what does it cost in area | routing, congestion, the real device |
| **out-of-context synthesis of the assembly** | tens of minutes | do the parts fit together at all | placement |
| **device synthesis** | tens of minutes | does the whole design elaborate and fit | timing |
| **implementation** | **hours** | will it actually close, and route | whether it computes the right answer |
| **bring-up** | minutes per probe, days per unknown | does the machine compute the right answer | whether it does so quickly |

Two asymmetries drive every decision in this documentation set.

**The cost curve is not smooth — it is a cliff at implementation.** Everything
before it is minutes; implementation is hours. So the value of a check is not its
own accuracy, it is how much implementation time it saves. A measurement that is
optimistic but takes ninety seconds is worth far more than an exact one that
takes six hours, provided you know which direction it is wrong in.

**The information curve runs the other way.** Cheap stages answer narrow
questions. A block that passes out-of-context synthesis may still fail on the
device; a device that closes timing may still compute the wrong answer. Nothing
earlier substitutes for anything later. The stages are filters, not proofs.

## Reading a result honestly

Most of the expensive mistakes in this flow are not wrong measurements. They are
**correct measurements read as answering a question they do not answer.**

Three that recur:

- **An out-of-context result that meets its target is a lower bound**, not a
  measurement. The optimiser stopped when the constraint was satisfied. Only a
  failing run tells you a ceiling. See [measure.md](measure.md).
- **A per-module measurement is optimistic about the assembly**, sometimes
  substantially. Composition costs frequency, and a design filling a device does
  not hold the slack that one block alone does.
- **A synthesis result is optimistic about the routed result.** Synthesis
  estimates routing, and the estimate is generous. A synthesis Fmax is not a
  closed-timing figure, whatever it says.
- **A simulation pass covers the traffic it exercised and nothing else.** It is
  corroboration for structural properties, never proof of them. See
  [simulate.md](simulate.md).

The habit that prevents all four: **write down which claim you have, with the
instrument that produced it.** "Meets its 3.33 ns target, not pushed" and "fails
at 3.0 ns, so the ceiling is near there" are different statements, and one of
them is not a frequency. Every figure in this tree names its part, its tool
version, whether it was in or out of context, whether it was synthesised or
routed, and which script produced it —
[measure.md](measure.md#every-figure-carries-its-provenance) is the rule and
[arch/physical/measurement.md](../arch/physical/measurement.md) is where it sits
as architecture.

## When to skip a stage, and when not to

Skipping is legitimate when a change **cannot** affect a stage:

- Documentation, comments, test-only changes — skip synthesis entirely.
- A change inside one module with an unchanged interface — simulate that module
  and measure that module; the system simulation adds nothing the module bench
  did not already cover.
- A parameter change that only affects capacity — re-measure area, not
  frequency, unless the parameter feeds a critical structure.

Skipping is **not** legitimate in three specific cases, all of which have cost
real time:

**Do not skip out-of-context measurement before an implementation run.** This is
the highest-value check in the flow by a wide margin: minutes against hours. A
change that pushes a block past its target is discoverable in ninety seconds and
otherwise costs a day.

**Do not skip the assembly-level check because the modules all passed.** One
module at a time cannot say whether the parts are compatible. It cannot see the
link *between* modules, and the critical path frequently lives there.

**Do not skip re-generating what is generated.** A generated top, wrapper,
constraint file or machine description that is stale does not fail. It produces a
machine whose capacities silently disagree with the software driving it, and
every gate passes.

### The inner loop must stay fast

If the cheap check is slow, people stop running it, and then it does not exist.
That is the actual reason to tier the test suite — not politeness about CPU time.

Structure the suite so there is something to run after **every** edit that
finishes in seconds, something to run before believing anything works that
finishes in about a minute, and something to run before calling a thing done. If
the fast tier drifts up to a minute, fix it; a ten-second question that becomes a
five-minute question teaches people to stop asking it.

`scripts/py/check.py` is that structure, and its header states each tier's
measured cost at `-j4`: `fast` is the pure-Python compiler and schedule checks
against a functional model; `unit` is the RTL benches that have caught the most;
`blocks` runs every block's own bench; `e2e` is the tier for compiler-emitted
instructions through the real RTL; `full` is all of them. Every check is
**bounded**, and one that produces no result inside its budget is killed and
reported as `STALLED` — a different event from a `FAIL`, and printed as one.

Two properties of a tiered suite decide whether it can be trusted, and both are
easy to leave implicit: **whether tiers nest** — "the unit tier passed" is not
"the linters passed" unless the unit tier contains them — and **whether a tier
can become empty**. A tier whose membership drains away when the thing it tested
is retired does not disappear; it passes, silently, for running nothing. Assert
that each tier is non-empty. See [simulate.md](simulate.md).

The corollary: **a check that takes much longer than usual is a stall, not
slowness.** Investigate it rather than waiting it out.

## The discipline underneath all of it

Six habits, each of which shows up repeatedly in the pages below.

**Check that the thing you asked for happened.** Tool APIs accept empty object
lists everywhere, warn at severities that scroll past, and prefer a default to a
failure. A constraint that matched nothing applied nothing, and said so only in a
line nobody reads. Assert the constraint's presence and fail loudly.

**A plausible number is the failure mode, not a crash.** Every trap in
[tooling-traps.md](tooling-traps.md) produces something that looks like a result.
Design checks around that: a sweep whose points do not differ has not measured
anything; a rate improvement with no matching component counter is unexplained; a
verdict that cannot fail on the answer is not a verdict.

**A bug should be catchable by a minimal directed test.** Chasing one through a
long full-system run is a symptom of a missing small test, and the missing test is
the real deliverable. See [simulate.md](simulate.md).

**Generate what can be generated, from one description.** RTL top, wrappers,
floorplan constraints and the software's model of the machine all describe the
same object. Hand-maintaining more than one copy guarantees they diverge, and the
divergence presents as a hardware fault.

**Record a number when it appears**, with the part, the target period, the tool
version, whether it was in or out of context, whether it was synthesised or
routed, and whether the run met or failed. A number that exists only in a
terminal scrollback is lost, and it will be re-measured — or worse, remembered
approximately.

That applies to simulation too, and there it is mechanical:
`check.py full --counts LEDGER` writes what every check printed on its
`PASS`/`FAIL` lines, and `--counts-baseline LEDGER` fails a later run when any
of them moved. **A green suite is not evidence that a refactor changed nothing**
— 503 checks becoming 501 is still a PASS. Take the ledger before the change;
afterwards there is nothing left to compare against.

**Numbers belong to the project that measured them.** Framework pages describe
practice. Any frequency, resource count or utilisation figure describes one
accelerator on one part, and lives with that project — for the reference
instance, [projects/kohakutpu/results.md](../projects/kohakutpu/results.md).

## The pages

**[build.md](build.md)** — the build flow: generators, the two build modes,
assembly, address maps, implementation steps and their costs. Ends with the split
between framework machinery and project configuration, which is what a second
project needs.

**[measure.md](measure.md)** — out-of-context measurement, the core practice for
answering "is my unit fast enough" without building a device. What a figure
produced this way is and is not, what provenance every number carries, how to
report failing paths so they name a problem rather than an endpoint, and the
dozen ways the measurement lies if set up wrong.

**[timing-closure.md](timing-closure.md)** — the method: group the failing paths
and fix the one root a group shares, read logic levels rather than slack,
floorplan before pipelining, spend flip-flops because they are the resource you
have, and treat implementation directives as zero-sum.

**[simulate.md](simulate.md)** — which simulator answers which question, the four
levels of test and what only each can catch, cross-checking two simulators
against each other, multi-clock simulation, running real programs against RTL,
and the minimal-test discipline.

**[bringup.md](bringup.md)** — bitstream to first correct result: the debug
surface, the ladder, and how to tell a build problem from an RTL problem from a
driver problem.

**[tooling-traps.md](tooling-traps.md)** — durable facts about how the tools
behave: Vivado, the vendor macro libraries, the simulators and Verilog itself.
Every future project meets most of them.

## What this section does not cover

Stating the boundary explicitly, because a page that lists only capabilities
reads as marketing:

- **How to design a compute unit.** That is [integrate/](../integrate/README.md).
  This tree assumes you have RTL and want to know whether it works.
- **What the machine is.** [arch/](../arch/README.md) describes the parts;
  nothing here explains the mesh, the node or the flit protocol beyond naming
  them.
- **Any specific accelerator's numbers.** Frequency, resource and utilisation
  figures describe one accelerator on one part and live with
  [that project](../projects/README.md). These pages carry practice, and the
  worked examples they cite are cited for their shape, not their values.
- **Hardware operation of a particular board.** Power, cooling, thermal limits
  and clock policy on a specific card are the project's, not the framework's.

## Where this fits

[integrate/](../integrate/README.md) describes the surface you build against and
what a conforming compute unit must do. [spec/](../spec/README.md) is the
normative contract. This tree is how you find out whether what you built actually
works, how fast, and on what.

Start here if you have RTL and no bitstream. Start at
[integrate/compute-unit.md](../integrate/compute-unit.md) if you have neither.
