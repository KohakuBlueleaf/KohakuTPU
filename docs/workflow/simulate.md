---
title: Simulating
summary: The four simulation levels, when each is the right one, and the discipline that keeps a bug findable by a small test instead of a long one.
tags:
  - workflow
  - simulation
  - testing
---

# Simulating

Simulation is where correctness is established. Synthesis says whether a design
is buildable and [measurement](measure.md) says whether it is fast enough;
neither says whether it computes the right answer.

The framework's simulation flow uses Vivado's `xsim`, because the memory
primitives are XPM macros and the primitive-accurate arithmetic models need the
Xilinx libraries. A permissive simulator cannot elaborate either, and passing
under one is not evidence about the other — see
[tooling-traps.md](tooling-traps.md).

## Four levels

Each level is a **different shape of test**, not a bigger one. A level exists
because there is a class of bug that only it can see, and a class it can no
longer localise.

| level | what it holds | what only it can catch | what it can no longer localise |
|---|---|---|---|
| **unit** | one arithmetic or storage block | bit-exactness, rounding, edge cases | anything involving a handshake |
| **module** | one complete block behind its real ports | protocol violations, backpressure, deadlock | anything crossing a module boundary |
| **mesh** | several blocks in their real topology | routing, arbitration, credit accounting, ordering | anything involving the host or memory |
| **end-to-end** | the whole machine, memory model included | integration, address maps, the dispatch chain | almost nothing — everything is in scope |

Run them in that order when diagnosing. A fault in the mesh that the module
bench already passes is a mesh fault, and a fault at end-to-end with both passing
is an integration fault. **Running them out of order buys nothing and costs the
bisection.**

### Unit

Exact arithmetic checked bit-for-bit against a model computed in the bench
itself. There is no tolerance to hide behind: a floating-point block either
produces the model's bits or it does not.

These are fast — seconds — and they are the level a datapath bug should be caught
at. If a numeric bug is being chased at a higher level, the unit bench for that
block is missing or too weak.

### Module

One block, driven through its real ports by a **hostile** bench: randomised
backpressure on every channel, stalls at every legal point, bursts of every legal
length, and an assertion monitor watching for protocol violations.

The failure mode at this level is usually a hang rather than a wrong answer, so
the bench needs a watchdog and the run needs a verdict. See
[Watchdogs and verdicts](#watchdogs-and-verdicts).

This is the level at which a compute unit written against the framework's port
should be verified. The bench acts as the network and as memory; no mesh is
involved.

### Mesh

Several real blocks in a real topology, with the bench standing in for whatever
is outside the picture — typically the host agent, and a memory model.

What a pass at this level means is narrower than it looks. Over the traffic
actually exercised: nothing was lost, everything landed where it was addressed,
per-pair order held, and the run finished. **Deadlock freedom is not
established** — that comes from the routing function being acyclic by
construction, and no finite test can establish it. A pass is corroboration, not
proof.

### End-to-end

The whole machine with nothing stubbed but DRAM: host stages a program, the
dispatch mechanism issues it, units request operands, memory answers, results are
written back, completion is signalled and the host polls it.

This level is expensive, and it is the **worst** place to find a bug. Its purpose
is to answer one question — *is the system runnable, end to end?* — not to
localise faults.

### Above RTL: the software stack

The compiler, scheduler and driver have their own test tiers in Python, run under
pytest. They are not RTL simulation and they are much faster; a bug in address
planning or instruction encoding should be caught there and never reach a
waveform.

The bridge between the two is a reference implementation: the same operation
computed in Python and in RTL, compared bit-for-bit.

## Running a bench

The canonical runner is a Python script that names benches in **one table** and
maps each to its top module and source list:

    python scripts/py/xsim.py <bench>
    python scripts/py/xsim.py <bench> --model 0 --keep
    python scripts/py/xsim.py <bench> -d SOME_FLAG

It compiles with `xvlog -sv`, elaborates with `xelab -L xpm` (plus
`-L unisims_ver` and `glbl` when the primitive models are in play), and runs
`xsim -runall`. Exit code 0 means the bench printed its pass verdict.

Work lands in `build/xsim_<bench>/`, wiped at the start of each run. Two
invocations of the *same* bench collide; override the root
(`--build-root`, or `KOHAKU_XSIM_BUILD`) when running a comparison in parallel.

There is also a set of PowerShell runners under `tests/` — `run_matmul_sim.ps1`,
`run_noc_sim.ps1`, `run_mag_sim.ps1`, `run_interlink_sim.ps1`,
`run_axi_sim.ps1`, `run_system_sim.ps1` — which predate the Python runner and do
the same thing per subsystem. They each keep their **own copy** of the source
list, and that duplication is exactly how they break: a module gains a
dependency, the shared table learns about it, one runner's private copy does not,
and that runner fails elaboration on an unresolved module while everything else
keeps working.

**One source list per bench, in one place.** A runner that hand-maintains a
duplicate is a runner that will drift. Where the two disagree today, the shared
table is the one that is right.

## Watchdogs and verdicts

Three rules, and all three exist because of a run that reported the wrong thing.

**Every bench has a watchdog.** A deadlock without one is an infinite run, which
in CI is a timeout with no output and in a terminal is a person waiting. With
one, it is a `WATCHDOG TIMEOUT` line naming the last thing that happened.

**Every bench prints an explicit verdict**, and the runner treats *no verdict* as
a failure distinct from `FAIL`. A bench that neither passed nor failed did not
run; that is a different bug from one that ran and got the wrong answer, and
collapsing the two loses the distinction exactly when it matters.

**Do not filter the output by shape.** Benches print results indented, so it is
tempting to keep only indented lines. Assertion monitors do not match that shape:

```verilog
$display("%0t ERROR mag_link: receive FIFO overflow on class %0d -- credit accounting is wrong, not the buffer size.", $time, in_cls);
```

starts with a timestamp — a digit. Filtering on the indent alone discarded every
assertion monitor in the project at once: lost-flit checks, accumulator reuse
windows, queue overflow checks. All of them exist to make a failure loud, and all
of them were being thrown away before anyone could read one.

Keep `ERROR` explicitly, whatever the line looks like.

**Stream the output rather than capturing it whole.** A bench that stalls is
diagnosable by how far it got. Captured whole and killed on a timeout, it prints
nothing at all — indistinguishable from a bench that failed to elaborate.

## Assertion monitors belong in the RTL

The most useful checks are not in the bench. They are in the module, guarded by
`resetn`, describing what *cannot* happen:

- a flit that was offered and not accepted while the sender did not hold it
- a receive buffer that accepted a beat while full
- a length field that disagrees with the `last` beat that arrived
- a ready signal asserted by something that must tie it high

Written this way, a check fires in **every** bench that instantiates the module —
including ones written years later by someone who never read the module — and it
names the cause rather than the symptom. The examples above all end with a
sentence saying which side is wrong, because the person reading it at 2am is not
the person who wrote it.

## Two arithmetic models

Any block built on a hard primitive — a DSP, a hard multiplier — should be
simulable two ways:

| model | what it is | a failure means |
|---|---|---|
| behavioural | the arithmetic, no primitive library | a maths or wiring bug |
| primitive | the real cell, via the vendor library | a primitive **configuration** bug |

Running both is the point, because it makes a failure attributable. A DSP
register-stage misconfiguration — one operand path taking two register stages and
the other taking one, so the operands arrive a cycle apart — is invisible in the
behavioural model and invisible under stable operands. It shows up only under
streaming, only against the real cell.

The primitive model needs the vendor library linked and `glbl` compiled in, and
`glbl` holds global reset for the first 100 ns
([tooling-traps.md](tooling-traps.md)). Benches must wait past it.

## Multi-clock simulation

Anything with two clock domains needs its bench to exercise **both ratios**, not
one. A clock-crossing FIFO that works at 1:1 and hangs at 3:7 is a common
outcome, and the failure mode is a hang rather than a wrong answer, so it will
not be caught by a correctness check.

Practical rules:

- **Drive both clocks from independent generators** with periods that are not
  integer multiples of each other, and run several ratios in one bench.
  Coincidental edge alignment hides the bug that a real MMCM will not.
- **Randomise backpressure on every channel independently.** A crossing that is
  never backpressured on one side is a crossing whose full path was never
  exercised.
- **Reset the two domains at different times**, in both orders. Reset release
  order is a real hazard, and a bench that always releases them together will
  never see it.
- **`xpm_cdc` drags in `glbl`**, so a bench containing an async FIFO needs it even
  when no primitive arithmetic model is in play.
- Cross-domain checks belong on the *slow* side. A monitor sampling a fast domain
  from a slow clock will miss pulses and report a phantom loss.

For timing rather than simulation of clock relationships — false paths, clock
groups, ratio-locked pairs — see [measure.md](measure.md) and
[timing-closure.md](timing-closure.md).

## A bug should be catchable by a minimal directed test

This is the discipline that matters most, and it is the one most easily skipped.

When a bug is found at end-to-end, the work is not finished when the end-to-end
run passes. It is finished when:

1. The bug is reproduced by the **smallest** bench that can express it — usually
   the module bench for the block at fault, occasionally a new directed test of a
   dozen lines.
2. That small test is fixed, and fails before the fix and passes after.
3. The small test is added to the suite permanently.

**Chasing a bug through a long full-system run is a symptom of a missing small
test.** Every time it happens, the missing test is the actual deliverable — the
fix is incidental, and the next bug in that block will cost the same days again
without it.

The economics are stark. A unit bench runs in seconds and points at one module. A
full-system bench runs in minutes and points at the whole machine. Bisecting a
fault with the second one costs a hundred times what bisecting it with the first
does, and the answer is less precise.

### Directed beats random, for the bug you already have

Randomised stress is for finding unknown bugs. Once a bug is known, a directed
test that reproduces it in ten cycles is worth more than a random test that
reproduces it one run in five — it is faster, it is deterministic, and it
documents the failure for whoever reads the suite later.

Keep both. Random stress finds; directed tests pin.

## A bench that is not maintained is worse than no bench

A bench that has drifted a generation behind the interfaces it drives will keep
reporting results. They will be wrong in a way that reads as a design fault.

One deleted bench packed a pre-widening instruction layout, so every field landed
a byte off, *and* its memory stub answered reads with a constant where a response
index belonged, so no result was ever committed. It reported **wrong answers**
for what was actually a hang.

The choices when a bench falls behind are: repair it, or delete it. There is no
third option where it stays in the tree reporting nothing trustworthy. If its
coverage exists elsewhere against the real block, deleting is correct — and
saying so, in the same table that lists the live benches, keeps the next person
from re-adding it.

The same applies to **generated files whose generator can no longer produce
them.** They look like build targets and they are not; synthesising one produces
a machine whose capacities silently disagree with what the compiler assumes.

## What a passing suite does and does not mean

- **Does**: over the traffic exercised, the properties checked held.
- **Does not**: anything about traffic not exercised, properties not checked, or
  the frequency any of it runs at.

Coverage of a hardware design by simulation is always partial. State that
plainly, keep the properties explicit, and let structural arguments — an acyclic
routing function, a credit scheme that cannot oversubscribe — carry the claims
that no finite test can.

## Open questions

- The PowerShell runners and the Python runner disagree about which sources a
  bench needs. Consolidating on one table is a prerequisite for a second project
  reusing this flow; see [build.md](build.md).
- Selecting a compile-time variant by listing a define file first depends on
  behaviour that `-sv` does not guarantee, and the consumers guard their defaults
  with `` `ifndef ``, so a failure of the mechanism is silent. Benches should
  print the variant they compiled with.
