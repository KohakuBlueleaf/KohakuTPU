---
title: Knowing you are correct
summary: What to test, at what level, and in what order — and why the framework's own benches are the baseline your unit is measured against.
tags:
  - integrate
  - testing
  - conformance
---

# Knowing you are correct

> **Kind: Convention throughout.** Nothing here is checked and nothing is
> required. What levels 2 and 3 *test for* is Fixed protocol — the obligations in
> [spec/compute-unit-port.md](../spec/compute-unit-port.md) — but the five levels,
> the order, the bench registry and the tiered runner are one worked practice,
> offered because it localises faults and the obvious alternatives do not.

"Does my compute unit work" is two questions wearing one coat, and they have
different answers, different fixes, and identical symptoms at the end of a long
simulation:

- **is my arithmetic right?**
- **is my unit a legal node?**

Almost everything below exists to keep those apart. The framework's own benches
pass on a clean tree with no project in sight; running them before you start
gives you a baseline, and running them after each change tells you which of the
two questions just changed its answer.

The second question is the one that fails silently. A unit that computes the
wrong number tells you so. A unit that withdraws a flit under backpressure, or
holds one it does not recognise, or retires an instruction twice, passes every
lightly-loaded test you write and then wedges a mesh three months later. That is
what levels 2 and 3 are for, and why they come before the interesting ones.

---

## 1. The five levels, in order

Run them in this order. Each one is cheap relative to the next, and each one
localises a class of fault the next cannot.

| level | what it proves | what it cannot |
|---|---|---|
| **0. Baseline** — the framework's own benches, unmodified | your tree, tools and environment are sound | nothing about your unit |
| **1. Datapath** — your arithmetic alone, no port | the numbers are right | nothing about protocol |
| **2. Port protocol** — your unit against a driven port, no mesh | you are a legal node | nothing about routing or contention |
| **3. Mesh** — several units, real routers, cross traffic | you behave under interleaving and backpressure | nothing about the host path |
| **4. End to end** — host, dispatch, memory, your unit, results back | the whole thing agrees with a software model | nothing about timing closure |
| **5. Out of context** — synthesis of your unit and of a mesh | it fits and it closes | nothing about function |

The two most commonly skipped are 0 and 2, and they are the two that pay best.
Level 0 because it is boring; level 2 because the mesh benches feel like they
subsume it, and they do not — a mesh bench never holds one unit's outbound link
busy long enough for completions to pile up.

---

## 2. Level 0 — establish the baseline

Before you write anything, run the framework's own benches on a clean tree: the
router mesh at a couple of sizes, the port-protocol bench against its reference
units, the orchestrator loopback, and the multi-unit bench. They pass, and the
output is your **reference** — a known-good result for tests you are about to
re-run against a tree whose only new variable is yours.

This is not ceremony. A failure here is a tool version, a path, a licence or a
simulator library problem, and finding that out now costs ten minutes instead of
being mistaken for a datapath bug later.

Keep the output.

---

## 3. Level 1 — your datapath, alone

A bench that instantiates your datapath with no port module and no flits.
Drive its command interface, feed its operand memory, check its results against a
software model of the same arithmetic.

Two disciplines the tree enforces and you should copy:

**Run behavioural and primitive-mapped builds separately.** Benches here run with
a model parameter selecting either behavioural arithmetic or the real hardware
primitives, so a failure is attributable: the same test passing one way and
failing the other localises the fault to the primitive mapping, not the
algorithm.

**Check against a model, not against yourself.** The software model is the golden
reference and the bench checks against it
([software-stack.md](software-stack.md) §5). And where somebody else has
implemented the same thing, check the *model* against theirs — a comparison where
you wrote both sides proves only that you were consistent.

The framework does not have a harness for this level, and cannot: it is your
arithmetic. What it does have is the bench registry — see §7.

---

## 4. Level 2 — port-protocol conformance

**This is the level this page exists for.** A bench with no mesh and no
orchestrator, driving the unit's outbound busy line directly, so the framework's
signal path is tested in isolation.

`tests/noc/cu_base_tb.v` is that bench today. What it does, in three phases:

1. **Hold the outbound link busy while several instructions retire.** Completions
   arrive far faster than a blocked link can drain. Check that *no* signal escapes
   while the link is busy, and that every one is delivered once it frees.
2. **Stutter the link during a longer program.** Bursts of busy and not-busy while
   instructions of varying latency retire. Check the signal count exactly.
3. **Hold the link busy permanently.** With no way to signal, the unit must apply
   backpressure rather than execute into the void — and whatever it accepted, it
   must eventually signal for all of it once the link frees.

What is at stake is stated in its own header, and it is worth repeating:
**a completion returns a dispatch credit**. A completion that is generated but
never transmitted is a credit that never comes back, and the orchestrator stalls
later with nothing to point at. "Exactly one signal per instruction" therefore has
to hold under *any* backpressure pattern, not just an idle link.

### Two measurement traps in the bench itself

Both were bugs in this bench, not in the RTL, and both will bite anyone writing a
new one:

**Count transfers, not valid cycles.** The framework holds a flit asserted until a
cycle the receiver is not busy, so counting `noc_out_valid` alone counts one
delivered signal once per cycle it waited — which reads as hundreds of duplicated
signals after a long stall. Count `noc_out_valid && !noc_out_busy`.

**Do not drive the busy line from a process woken by the edge your counter samples
on.** The count then depends on process ordering within one timestep, and the
result changes between simulators.

### The probes

The bench also reaches into the unit hierarchically and counts three things the
port cannot show: instructions accepted, completions raised by the datapath, and
completions actually queued by the framework. All three must equal the number
issued. A discrepancy names which side lost the signal, which is the difference
between a five-minute fix and an afternoon.

One probe is a rule rather than a count: it fails if the datapath ever raises
`exec_done` and `inst_ready` in the same cycle, because the framework drops its
in-flight flag on that arm and the new instruction's completion is never queued.
No unit in the tree does it, and the probe exists to keep it that way.

> **Using this bench for your unit means editing it.** It instantiates one
> specific unit by name and its probes use that unit's internal hierarchy —
> including the name of the `noc_cu_base` instance, which the tree spells two
> different ways. Making it generic is an open item ([§8](#10-open-questions)).
> Until then: copy it, retarget it, keep the three phases and both probes.

---

## 5. Level 3 — the mesh

Two benches, and they ask different questions.

**The mesh itself, at several sizes.** Traffic across a whole grid, checked for
loss, misdelivery, and per-pair ordering, with a watchdog. A pass means: over the
traffic actually exercised, nothing was lost, everything landed where it was
addressed, and per-pair order held. Note what it does *not* mean —
deadlock-freedom comes from the routing rule being acyclic by construction, and
no finite test can establish it. A pass is corroboration, not proof.

**Several units of different designs, with unit-to-unit traffic.** This is where
a unit that is subtly wrong about interleaving fails: another sender's flit
landing in the middle of your burst, two senders addressing you at once, an
acknowledgement you did not expect. The tree runs two deliberately different
reference units here for exactly that reason — a framework accidentally fitted to
one style would fail on the other, and so would a unit.

Drop your unit into the multi-unit bench. If your receive path frames a stream by
position rather than by type and source, this is the level that finds it.

---

## 6. Level 4 — end to end

Host writes, dispatch, memory agent, your unit, results back, compared against a
software model of the whole computation.

The tree runs this two ways deliberately — through the direct planner and through
the compiler — against the same RTL through the same simulator session. The only
difference is who emitted the instructions, so running both **separates a
compiler fault from a hardware one**. That is worth copying the moment you have
two ways to produce instructions.

Two disciplines:

**Every check is bounded, and a hang is reported as a hang.** A wedged bench does
not fail — it runs to its own watchdog and then grades whatever the untouched
memory held, so a stall arrives as a wrong answer after a long wait and points at
the datapath rather than at the thing that actually stopped. Give every bench a
watchdog that prints, and give every runner a timeout that kills the whole
process tree, not just its direct child.

**Simulation reset is not your reset.** The vendor's global set/reset holds for
the first stretch of simulated time regardless of your own reset, so primitive-
backed registers ignore everything before then. Start stimulus after it.

---

## 7. How the benches are registered and run

Two mechanisms, and knowing which is which saves confusion:

**A bench registry** maps a short name to a top module and its source list, so
`xsim.py <name>` builds and runs any bench without a bespoke script. Adding a
bench is adding an entry. The registry is also read by the tiered runner rather
than duplicated in it — listing benches in two places lets them drift, and the
failure mode of that drift is silent: a new block gets a bench, nobody adds it to
a tier, and the full run stays green while covering less than it did.

**A tiered runner** groups checks by the question they answer and runs them in
parallel:

| tier | question |
|---|---|
| fast | does the compiler mean the right thing? Pure software, no simulator |
| unit | plus the handful of RTL benches that have historically caught the most |
| blocks | every block's own bench — the level at which a failure names a module rather than naming "the machine" |
| e2e | the whole path, on shapes small enough to run every time |
| full | all of it |

Run the fast tier after every edit and the full one before calling something
done.

Parallelism here is a correctness question, not a tuning one: checks are grouped
into **lanes** by the resource two of them would fight over. A simulator session
is exclusive — a second one does not race, it fails outright. Distinct benches
are independent, but the same bench twice is not, and two runs sharing one build
directory destroy each other in a way that reads as a dozen benches failing with
tool errors rather than as an RTL problem.

> **The port-protocol and mesh benches are not in the tiered runner.** They live
> in a separate PowerShell script and are not in the bench registry, so the
> default check loop does not run them. For framework RTL that changes rarely
> this has been tolerable; for a project whose unit is under active development
> it is not. Wire your level-2 bench into the registry so it runs in the `blocks`
> tier, and do it before you need it.

---

## 8. Level 5 — out of context

Synthesise your unit on its own, and the mesh containing it, out of context.
Utilisation is reliable there and timing is optimistic, which is the right trade
for a decision about whether something fits.

Two things to measure, and the second is the one people skip:

- **your unit** — does it close, and what does it cost;
- **a mesh of the same shape populated with the zero-compute endpoint** — because
  your unit's real cost is the difference between them, and without that
  subtraction you are attributing the network's cost to your arithmetic.

That second measurement is what `src/kohakuaccel/noc/endpoint/noc_cu_null.v` exists for, and why
it is built to defeat synthesis pruning: every bit of both inbound flits reaches
an output, and its traffic originates externally so the mesh cannot be proven
idle and constant-folded. Without those properties the subtraction would be
against a number the optimiser invented. It is a measurement instrument and
nothing else — see [compute-unit.md](compute-unit.md) §1.2 before copying it.

The mechanics, the generic-passing syntax, and the ways an out-of-context
measurement can lie are [workflow/measure.md](../workflow/measure.md).

---

## 9. A procedure for one change

1. Run the fast tier. It is seconds.
2. Run your datapath bench, both arithmetic modes.
3. Run your port-protocol bench. If it fails, stop — nothing at a higher level
   will be interpretable.
4. Run the multi-unit mesh bench.
5. Run end to end, both instruction sources if you have two.
6. Before calling it done: the full tier, and an out-of-context run against the
   baseline you kept from level 0.

If something above level 2 fails and level 2 passes, suspect your logic. If level
2 fails, suspect the seam — and re-read
[spec/compute-unit-port.md](../spec/compute-unit-port.md) §8.3, which is the list
of things a unit may never do, in the order they are most often done.

---

## 10. Open questions

- **There is no generic port-protocol harness.** The bench names one unit and
  probes its internals by hierarchical path. Making it point at an arbitrary unit
  needs, at minimum, an instance-name convention for the base module — which the
  tree currently spells two different ways — and probably a small wrapper the
  unit provides. This is the single most valuable missing piece of framework
  tooling for a new project.
- **The framework's own RTL benches are outside the tiered check loop** (§7). They
  should be in the registry.
- **Nothing checks a unit's published capabilities against its behaviour.**
  `CU_CAPS` reports an instruction depth and a buffer count; nothing verifies that
  the unit actually has them, and a wrong value is a driver that over-commits.
- **There is no conformance test for the driver side.** A unit has a contract and
  a bench; a driver has neither, and the credit-accounting edge in
  [software-stack.md](software-stack.md) §3 is exactly the kind of thing one
  would catch.
- **Nothing verifies that a mesh map and the software's machine description
  agree.** Two descriptions of one machine, checked by neither.
