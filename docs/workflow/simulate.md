---
title: Simulating
summary: The tiered simulation flow — Verilator as the inner loop, xsim as the gate, Vivado for anything about resources — the four levels of test, and the discipline that keeps a bug findable by a small test instead of a long one.
tags:
  - workflow
  - simulation
  - testing
---

# Simulating

Simulation is where correctness is established. Synthesis says whether a design
is buildable, [measurement](measure.md) says whether it is fast enough, and
neither says whether it computes the right answer.

This page covers three things that are often run together and should not be
confused: **which simulator to use for which question**, **what shape of test
catches what**, and **the discipline that keeps a failure attributable**.

**Vocabulary used below.** A **mesh** is the on-chip network and the compute
units attached to it. A **flit** is the fixed-size unit that crosses a link on
that network ([flits and links](../arch/noc/flits-and-links.md)). A **system
node** is the single component serving one mesh with memory access and dispatch
([sysnode](../arch/sysnode/README.md)). A **station bus** is the AXI-side
transport between the host-facing fabric and a node
([station bus](../projects/kohakuaxi/station-bus.md)). **XPM** is the vendor's
parameterised macro library, which is where this tree's FIFOs and memories come
from.

---

## Three tools, three jobs

| | Verilator | xsim | Vivado synthesis |
|---|---|---|---|
| **role** | the inner loop | the gate of record | resources and timing |
| **reaches an elaboration error in** | seconds | tens of seconds | minutes |
| **simulation run** | **two orders of magnitude faster** | seconds to minutes | — |
| **build step** | 5–50 s of C++, per RTL change | none | — |
| **X propagation** | no, by design | yes | — |
| **`$random` stream** | its own | a different one | — |
| **block RAM / ultra RAM inference** | no | no | **yes — and only it** |
| **LUT, DSP, Fmax** | no | no | yes |

Two consequences run through everything below.

**Neither simulator sees resource risk.** A design that simulates perfectly can
fall out of block RAM. A block-RAM port is 72 bits at its widest, so a 74-bit
array does not fit one — it becomes thousands of LUTs and zero block RAM instead,
and **no warning is issued**, because from the tool's point of view nothing went
wrong. Only synthesis catches it, which is why the gates in any implementation
plan sit on the Vivado side. See [tooling-traps.md](tooling-traps.md) and
[Explicit memory primitives](tooling-traps.md#memory-primitives-are-named-never-inferred).

**Verilator is not simply "the fast one".** On a small directed check the two
simulators are comparable once Verilator's C++ build is counted: a 5–50 s build
against an xsim run of tens of seconds is not a win worth restructuring for. The
win is entirely in long runs, and the first two steps below exist to earn it.

### The order of work

**0 · Lint, before spending any simulator run.**

```
python scripts/py/vlt.py <bench> --lint-only
```

Seconds, against tens of seconds for xsim to reach the same missing module, port
mismatch or parameter error. It needs no C++ build and it is the cheapest useful
thing in the flow. A lint pass is a lint pass — none of the divergences below
apply to it.

**1 · The small directed check, under xsim.** The corner cases, the ones written
while the RTL is still moving. xsim has no build step, so at this size it is as
fast in practice, and it is the gate of record anyway.

**2 · The same case under Verilator — for agreement, not for speed.** Build the
model, run *the same case*, compare. This step's product is not a result; it is
**the agreement**, and the agreement is what licenses step 3. A long run on a
model that has never been cross-checked against a small case is a confident
answer with nothing behind it.

**3 · Something serious, under Verilator.** A linked binary, a boot, a real
workload: millions to hundreds of millions of cycles. This is the step xsim
cannot do at all, and it is the entire reason the first two are worth their time.

### How much faster, and measured how

Across this tree's bench matrix — Verilator 5.020 under WSL against Vivado
2024.2's xsim, same benches, same build root, same day — the **simulation run**
is 100–300× faster while the C++ build is not. The anchor that matters for long
runs: one twelve-clock design in this tree reached **200 ms of simulated time in
495 s of wall clock**, of order 10⁸ clock periods. At that rate a 10⁷-cycle
program is a coffee break and a 10⁸-cycle one is an overnight run. Under xsim
both are out of reach.

Build time stops mattering once a model is built once and driven for hours
rather than rebuilt per run, which is the argument for the C++ harness path
below.

---

## Four levels of test

Each level is a **different shape of test**, not a bigger one. A level exists
because there is a class of bug only it can see, and a class it can no longer
localise.

| level | what it holds | what only it can catch | what it can no longer localise |
|---|---|---|---|
| **unit** | one arithmetic or storage block | bit-exactness, rounding, edge cases | anything involving a handshake |
| **module** | one complete block behind its real ports | protocol violations, backpressure, deadlock | anything crossing a module boundary |
| **mesh** | several blocks in their real topology | routing, arbitration, credit accounting, ordering | anything involving the host or memory |
| **end-to-end** | the whole machine, memory model included | integration, address maps, the dispatch chain | almost nothing — everything is in scope |

Run them in that order when diagnosing. A fault in the mesh that the module bench
already passes is a mesh fault; a fault at end-to-end with both passing is an
integration fault. **Running them out of order buys nothing and costs the
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
the bench needs a watchdog and the run needs a verdict.

This is the level at which a compute unit written against the framework's port
should be verified. The bench acts as the network and as memory; no mesh is
involved.

### Mesh

Several real blocks in a real topology, with the bench standing in for whatever
is outside the picture — typically the host agent and a memory model.

What a pass at this level means is narrower than it looks. Over the traffic
actually exercised: nothing was lost, everything landed where it was addressed,
per-pair order held, and the run finished. **Deadlock freedom is not
established** — that comes from the routing function being acyclic by
construction, and no finite test can establish it. A pass is corroboration, not
proof.

### End-to-end

The whole machine with nothing stubbed but DRAM: the host stages a program, the
dispatch mechanism issues it, units request operands, memory answers, results are
written back, completion is signalled and the host polls it.

This level is expensive and it is the **worst** place to find a bug. Its purpose
is to answer one question — *is the system runnable, end to end?* — not to
localise faults.

### Above RTL: the software stack

The compiler, scheduler and driver have their own test tiers in Python, run under
pytest. They are not RTL simulation and they are much faster; a bug in address
planning or instruction encoding should be caught there and never reach a
waveform.

The bridge between the two is a reference implementation: the same operation
computed in Python and in RTL, compared bit-for-bit.

---

## Running a bench

**One source list per bench, in one place.** `scripts/py/xsim.py` names every
bench in a single `BENCHES` table mapping it to a top module and a source list.
`scripts/py/vlt.py` **imports that table**, so a bench is defined once: adding a
source file reaches both simulators with no second edit, and the xsim runner is
never modified to serve Verilator.

That is not a tidiness preference. A per-runner copy of a source list is a copy
that will drift — a module gains a dependency, the shared table learns about it,
a private copy does not, and that runner alone fails elaboration on an unresolved
module while everything else keeps passing. Any runner that hand-maintains a
duplicate source list is a runner that will report a tool problem as a design
problem.

```
python scripts/py/xsim.py <bench>
python scripts/py/xsim.py <bench> --model 0 --keep
python scripts/py/xsim.py <bench> --max-time 200us

python scripts/py/vlt.py  <bench> --lint-only
python scripts/py/vlt.py  <bench> --warn
python scripts/py/vlt.py  <bench> --cc sim/verilator/harness/<name>_main.cpp
```

| xsim flag | |
|---|---|
| `--model 0` / `1` | the vendor-primitive arithmetic model, or the behavioural one |
| `-d NAME=VAL` | a define, passed through a command file rather than the command line |
| `--vcd <scope>` | dump a scope's signals; counts the objects and fails if the scope matched nothing |
| `--max-time <t>` | **stop at a simulated-time budget** instead of running to completion |
| `--wall <s>` | kill the process after this many seconds of wall clock |
| `--build-root` | build somewhere other than `build/` |

| Verilator flag | |
|---|---|
| `--lint-only` | elaborate and check, no C++ build |
| `--warn` | do not silence the bulk warning classes |
| `--timebox <t>` | the simulated-time budget, as `--max-time` above |
| `--cc <harness.cpp>` | build a C++ model plus a harness that owns `main()` |
| `--trace` | VCD, at 10–100× the cost |
| `--native` | a `verilator` on `PATH` rather than the WSL one |

xsim compiles with `xvlog -sv`, elaborates with `xelab -L xpm` (plus
`-L unisims_ver` and `glbl` when the primitive models are in play), and runs the
simulation. Exit code 0 means the bench printed its pass verdict.

Two mechanical points that cost a debugging round each when missed:

- **Work lands in `build/xsim_<bench>/` and is wiped at the start of each run**,
  so two invocations of the *same* bench collide. Override the root
  (`--build-root`, or `KOHAKU_XSIM_BUILD`) when running a comparison in parallel.
- **A relative build root is resolved against the repository, not the shell's
  directory**, because `xvlog` runs with its working directory inside the build
  directory. A relative path that is not resolved reaches the tool as a missing
  generated header, which reads as a broken build rather than as a bad option.

### Bound the run and print the state

> **Prefer a simulated-time budget to running to completion.**

A bench that runs to completion has two outcomes: it finishes, or it does not.
A hung device under test makes the bench spin out its whole internal spin limit
before anything is printed, so a debug loop learns nothing for a long time. Stop
at roughly 1.5× the point where the answer was due, and have the bench print
where it got to.

Both runners take a budget for this — `--max-time` for xsim, `--timebox` for
Verilator — and both print a line saying the budget was reached, which is a
distinct outcome from a pass and from a fail.

The same reasoning applies to the state a bench prints. A stalled run is
diagnosable by *how far it got*, so a bench should emit progress, not only a
verdict. And **stream the output rather than capturing it whole**: captured and
then killed on a timeout, a bench prints nothing at all, which is
indistinguishable from one that failed to elaborate.

---

## Watchdogs and verdicts

Three rules, each of which exists because the alternative reports the wrong
thing.

**Every bench has a watchdog.** A deadlock without one is an infinite run — in CI
a timeout with no output, in a terminal a person waiting. With one it is a
`WATCHDOG TIMEOUT` line naming the last thing that happened.

A watchdog that never fired means the condition it was waiting on was already
true, or the bench exited another way. It does **not** mean the delay overflowed:
`#N` in xsim is a 64-bit quantity, so a watchdog at `#500_000_000` is 500 ms and
not a wrapped-to-zero no-op. The wrap explanation is plausible, cheap to believe,
and sends the next hour into the wrong file.

**Every bench prints an explicit verdict**, and the runner treats *no verdict* as
a failure distinct from `FAIL`. A bench that neither passed nor failed did not
run; that is a different bug from one that ran and got the wrong answer, and
collapsing the two loses the distinction exactly when it matters.

**The verdict line carries a count, and the count is what a refactor has to
hold.** `scripts/py/check.py --counts LEDGER` records every integer a check
printed on its `PASS`/`FAIL` lines, and `--counts-baseline LEDGER` fails a later
run when one moves.

> **A green suite is not evidence that a refactor changed nothing.** 503 checks
> becoming 501 is still a PASS.

Take the ledger *before* the change. Afterwards there is nothing left to compare
against.

**Do not filter the output by shape.** Benches print their results indented, so
it is tempting to keep only indented lines. Assertion monitors do not match that
shape:

```verilog
$display("%0t ERROR mag_link: receive FIFO overflow on class %0d -- credit accounting is wrong, not the buffer size.", $time, in_cls);
```

starts with a timestamp — a digit. Filtering on the indent alone discards every
assertion monitor at once: lost-flit checks, reuse-window checks, queue overflow
checks. All of them exist to make a failure loud, and all of them are thrown away
before anyone can read one. Keep `ERROR` explicitly, whatever the line looks
like, and keep `PASS`/`FAIL` explicitly too — some benches print a verdict
unindented and a shape filter fails them all while hiding the line that says so.

### The inner loop must stay fast

If the cheap check is slow, people stop running it, and then it does not exist.
That is the reason to tier a suite, not politeness about CPU time.

`scripts/py/check.py` is that structure, and its own header states the tiers and
their measured cost at `-j4`: `fast` (11 s) is the pure-Python compiler and
schedule checks against a functional model; `unit` (40 s) is the RTL benches that
have caught the most; `blocks` (63 s) runs every block's own bench; `e2e` is the
tier that would run compiler-emitted instructions through the real RTL; `full` is
all of them. Every check is bounded, and one that produces no result inside its
budget is killed and reported as **STALLED** — a different event from a FAIL, and
printed as one.

Two things about that structure are worth copying and one is worth knowing:

- **A tier is a set, not a level.** `unit` does not include `fast` in this tree,
  so "the unit tier passed" is not "the linters passed". Whether tiers nest is a
  choice; leaving it implicit is how a gate gets skipped.
- **The `e2e` tier is currently empty.** The list it draws from lost its contents
  when the package feeding it was retired, so the tier runs whatever the tiers it
  composes with run and nothing of its own. It passes. A tier whose membership can
  become empty without the tier disappearing is a gate that reports success for
  running nothing — worth an explicit check that each tier is non-empty.

The corollary: **a check that takes much longer than usual is a stall, not
slowness.** Investigate it rather than waiting it out.

---

## Assertion monitors belong in the RTL

The most useful checks are not in the bench. They are in the module, guarded by
reset, describing what *cannot* happen:

- a flit that was offered and not accepted while the sender did not hold it
- a receive buffer that accepted a beat while full
- a length field that disagrees with the `last` beat that arrived
- a ready signal asserted by something that must tie it high
- two ports writing the same location on the same cycle, where the design says
  only one may

Written this way, a check fires in **every** bench that instantiates the module —
including ones written years later by someone who never read it — and it names
the cause rather than the symptom. The examples above end with a sentence saying
which side is wrong, because the person reading it at 2am is not the person who
wrote it.

---

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
`glbl` holds a global set/reset asserted for the first 100 ns of simulated time
([tooling-traps.md](tooling-traps.md)). Benches must wait past it. Do not link
`glbl` as a precaution either: adding it to a bench that does not need it holds
that global reset over every XPM cell for 100 ns, which is a behaviour change to
benches that currently pass.

---

## Multi-clock simulation

Anything with two clock domains needs its bench to exercise **both ratios**, not
one. A clock-crossing FIFO that works at 1:1 and hangs at 3:7 is an ordinary
outcome, and the failure mode is a hang rather than a wrong answer, so no
correctness check will catch it.

- **Drive both clocks from independent generators** with periods that are not
  integer multiples of each other, and run several ratios in one bench.
  Coincidental edge alignment hides the bug that a real MMCM will not.
- **Randomise backpressure on every channel independently.** A crossing that is
  never backpressured on one side is a crossing whose full path was never
  exercised.
- **Reset the two domains at different times**, in both orders. Reset release
  order is a real hazard, and a bench that always releases them together will
  never see it.
- **An asynchronous FIFO drags in `glbl`**, because the vendor's clock-domain
  crossing macro instantiates it — so such a bench needs it even when no
  primitive arithmetic model is in play.
- **Cross-domain checks belong on the *slow* side.** A monitor sampling a fast
  domain from a slow clock will miss pulses and report a phantom loss.

For timing rather than simulation of clock relationships — false paths, clock
groups, ratio-locked pairs, and the opposite errors of grouping them wrongly —
see [measure.md](measure.md) and [timing-closure.md](timing-closure.md).

---

## Cross-checking the two simulators

**When a bench is added, and after any RTL change that touches it: run both, same
build root, same day, and compare check counts.**

| outcome | meaning |
|---|---|
| identical counts, identical result | the model is trusted for that bench |
| identical counts, different result | a real divergence — classify it |
| different counts | usually `$random`; confirm before dismissing |
| one simulator fails to build | usually a testbench idiom — classify it |

Recording the pair is the point. A bench that has never been cross-checked is
xsim-only regardless of how often it has passed.

### Classifying a divergence

In the order they actually occur.

**`$random`.** The two simulators have different streams, so *check counts*
differ legitimately while both pass. Not a bug. Confirm by checking that both
report zero errors.

**A testbench idiom one simulator does not accept.** `disable` on a named fork
branch is the common one; Verilator refuses to build. This is bench code, not
RTL. Either rewrite the bench or leave it xsim-only — and note that a C++ harness
has no Verilog testbench, so it can never affect the harness path.

**A non-blocking assignment inside `initial`, driving stimulus against a clock
edge.** A genuine clock-edge race that the two simulators resolve differently.
**This is a testbench bug**, and no shim or RTL change will fix it. Worth fixing
on its own merits.

**X propagation.** xsim propagates X; Verilator is two-state and assigns a value.
Reset and initialisation checks that depend on X are meaningful only under xsim
and should stay there. When X is *suspected*, test it rather than assuming: run
Verilator under `--x-assign 0`, `1` and `unique`. **If all three agree with each
other and disagree with xsim, X is not the cause** and something else is. That
test is cheap and it regularly comes back negative — a deterministic,
byte-identical divergence across all three X policies has nothing to do with
two-state modelling.

**Everything else.** One of the two is wrong about the RTL, and which one is not
automatic. Narrow it to a single expression before deciding. The method that
works is a purpose-built cross-check bench that **reports an internal quantity
from both** rather than pass/fail — peak occupancy, a pointer value, a credit
count. A bench that only reports pass/fail cannot tell you which of two passing
models is right.

One attribution rule outranks all of the above: **get the xsim baseline before
blaming the new tool.** A bench that fails under both simulators is a broken
bench or broken RTL, not a divergence, and it will absorb a day if it is
diagnosed as one.

---

## Shims

Verilator cannot compile the vendor's XPM sources directly. The library's
assertions can be cleared with a define, but a handful of Verilog-1995
`deassign` statements inside one memory module cannot — and that module is
instantiated by every FIFO and every RAM, so it blocks all of them. The answer is
`sim/verilator/shims/`: independent models of the four XPM cells this tree
actually uses, each named by exactly one wrapper module. Vendor sources are never
copied and never patched.

Two rules make shims safe:

**Fail on an unimplemented mode rather than approximating one.** Each wrapper
here pins every option, so the surface to model is small; each shim halts on a
mode it does not implement. A wrong waveform that looks plausible is worse than a
stopped run.

**A shim change requires a cross-check bench that measures the property that
changed** — not a bench that passes.

That second rule has a sharp illustration. A first-word-fall-through FIFO carries
words in **output stages beyond the array**, so a shim sized to the declared
depth is genuinely shallower than the real cell. A cross-check that measured
*ordering* passed with hundreds of words and zero errors while the shim was still
wrong, because ordering was not the property that had broken. Anything sizing
credit against the real depth then deadlocks with no error message anywhere. And
the two cells are **not symmetric** — the synchronous one carries two extra
words, the asynchronous one carries one — so assuming symmetry, the obvious move,
leaves one of them wrong by one.

> **A cross-check must measure every property the design depends on, not just the
> one that is easy to check.** Ordering, capacity and reset-during-traffic are
> three separate measurements.

**Uninitialised memory is left uninitialised.** No shim zeroes its array. Reading
an address never written stays a real X under a four-state simulator and is
randomised per run under Verilator's `--x-initial unique`. Zeroing it would hide
exactly the read-before-write bugs the benches exist to catch.

---

## Promotion

A bench moves through three states:

1. **xsim-only** — the default. Not cross-checked, or a known divergence with no
   fix.
2. **cross-checked** — both simulators agree. Verilator is the development loop;
   xsim still gates.
3. **Verilator-gated** — Verilator is the gate of record for this bench, and xsim
   runs occasionally as an audit.

Promotion to state 3 requires agreement across *several* RTL changes rather than
once, no reliance on X, no `$random` in the comparison, and no testbench idiom
Verilator cannot build.

**Long-running benches are the ones worth promoting**; short ones stay at state 2
because the gain does not repay the tracking. Which makes the natural first
candidate a **bare processor core**, and the reasoning generalises to any block
of that shape:

- **It touches almost none of the divergences.** No station bus, no mesh, no
  interlink, essentially no XPM in its path. The unresolved cross-simulator items
  in a tree like this one are all in machinery a bare core does not use.
- **Its validation is millions of cycles.** An architectural test suite is not a
  handful of directed cases. At xsim speed that is a run you do not repeat
  casually; at Verilator speed it is part of the loop.
- **Its strongest check needs a C++ harness**, below.

What stays on xsim regardless: anything crossing into the node, the station bus
or the mesh — which is where a tree's cross-simulator divergences tend to live.
What stays on Vivado regardless: every LUT figure, every Fmax figure, and the
check that memories actually became block RAM or ultra RAM.

---

## The C++ harness, and running real programs

A standalone simulation binary runs a Verilog testbench with no way in from
outside. Verilator's `--cc` mode instead emits a **C++ class**, and the harness
owns `main()`, the clock and `eval()`:

```cpp
void step() {                    // one full clock period
    top->clk = 0; top->eval(); ctx->timeInc(HALF);
    top->clk = 1; top->eval(); ctx->timeInc(HALF);
}
```

Everything else is protocol against ports the design already has. This is what
`vlt.py --cc <harness.cpp>` builds, and the harnesses live in
`sim/verilator/harness/`.

Two capabilities depend on it and cannot be had otherwise.

**Differential testing against a golden model.** A testbench compares a result at
the end. A co-simulation compares **architectural state at every retirement**,
which is how processor cores are actually verified, and it needs a C++ harness
because both models have to be stepped in lockstep from the same loop:

```
step the RTL one retire  ->  read PC, the register that changed, its value
step the golden model    ->  the same three things
compare; on mismatch, stop and print both
```

**Running a real program.** Five things a harness needs before that is possible,
none of them hard and all of them easy to leave out:

1. **An ELF loader**, not a hex file per test. A serious workload is a linked
   binary with sections at addresses; the harness parses it into the memory map
   before releasing reset.
2. **A sparse C++ memory map, not a Verilog array.** Address space per mesh is
   gigabytes. An array of that cannot be elaborated; a hash map of pages costs
   only what the program touches. This is the concrete reason a harness should
   not simply reuse the AXI RAM model from the bench tree.
3. **A console.** A store to a known address that the harness turns into stdout.
   Without it, a program that runs for ten million cycles is a black box — the
   run either ends or it does not, with nothing in between.
4. **A halt-and-result convention**, so the program can say "finished, here is
   the answer" rather than being stopped by a cycle budget. An explicit
   control-region store carrying a result word is the right shape, and the
   harness watches for it directly.
5. **Tracing off by default.** Waveform tracing costs 10–100× and turns a
   feasible run back into an infeasible one. Put it behind a flag, ideally with a
   cycle window so a long run dumps only the interesting part.

For a processor, **adopt the architecture's existing host-communication
convention** for items 3 and 4 rather than inventing one. The standard RISC-V
test suites and reference ISA simulators already use a pair of magic memory
locations to emit a character and to signal completion; supporting that costs a
compare on a store address, and it means the standard suites run with no
adaptation and every tool built around them works unchanged.

What this unlocks is the thing worth the effort: compiling a runtime and
**booting it against the real RTL before any bitstream exists**, with the whole
architectural state visible and reproducible, and a driver bug distinguishable
from an RTL bug because both sides are in view.

---

## A bug should be catchable by a minimal directed test

This is the discipline that matters most and the one most easily skipped.

When a bug is found at end-to-end, the work is not finished when the end-to-end
run passes. It is finished when:

1. The bug is reproduced by the **smallest** bench that can express it — usually
   the module bench for the block at fault, occasionally a new directed test of a
   dozen lines.
2. That small test is fixed: it fails before the fix and passes after.
3. The small test joins the suite permanently.

> **Chasing a bug through a long full-system run is a symptom of a missing small
> test.** Every time it happens, the missing test is the actual deliverable — the
> fix is incidental, and the next bug in that block costs the same days again
> without it.

A system-level reproduction **locates** a bug. It does not fix one. Push the
reproduction down to the level that owns the behaviour, fix it there, and wire
the result back up.

The economics are stark. A unit bench runs in seconds and points at one module. A
full-system bench runs in minutes and points at the whole machine. Bisecting with
the second costs a hundred times what bisecting with the first does, and the
answer is less precise.

### Directed beats random, for the bug you already have

Randomised stress is for finding unknown bugs. Once a bug is known, a directed
test that reproduces it in ten cycles is worth more than a random one that
reproduces it one run in five: it is faster, it is deterministic, and it
documents the failure for whoever reads the suite later.

Keep both. Random stress finds; directed tests pin.

---

## A bench that is not maintained is worse than no bench

A bench that has drifted a generation behind the interfaces it drives keeps
reporting results, and they are wrong in a way that reads as a design fault.

The shape to expect: a bench packs an instruction layout that predates a field
widening, so every field lands a byte off, *and* its memory stub answers reads
with a constant where a response index belongs, so no result is ever committed.
It reports **wrong answers** for what is actually a hang. Two independent
staleness bugs conspiring to produce a plausible failure is not unusual; a stale
bench has had time to accumulate several.

The choices when a bench falls behind are: repair it, or delete it. There is no
third option where it stays in the tree reporting nothing trustworthy. If its
coverage exists elsewhere against the real block, deleting is correct — and
saying so, in the same table that lists the live benches, keeps the next person
from re-adding it.

The same applies to **generated files whose generator can no longer produce
them**. They look like build targets and they are not; synthesising one produces
a machine whose capacities silently disagree with what the compiler assumes. See
[build.md](build.md).

---

## What a passing suite does and does not mean

- **Does**: over the traffic exercised, the properties checked held.
- **Does not**: anything about traffic not exercised, properties not checked, or
  the frequency any of it runs at.

Coverage of a hardware design by simulation is always partial. State that
plainly, keep the properties explicit, and let structural arguments — an acyclic
routing function, a credit scheme that cannot oversubscribe — carry the claims
that no finite test can.

And **stale build artefacts fake a pass**. A run against a build directory that
was not wiped can pass on an object file from a previous version of the design.
Delete the tree and read the counts, rather than trusting a green line.

## Open questions

- Selecting a compile-time variant by listing a define file first depends on
  behaviour that `-sv` does not guarantee, and consumers guard their defaults
  with `` `ifndef ``, so a failure of the mechanism is silent. **Benches should
  print the variant they compiled with**, in their banner, every run. See
  [tooling-traps.md](tooling-traps.md).
- Verilator's bulk-silenced warning classes are not noise. `--warn` shows them
  and some are real findings that xsim has never reported — inferred latches, and
  width truncations that discard the high bits of a shift. A warning sweep is
  worth doing as its own occasional pass, separately from any bench work.
- The reset-busy duration of the FIFO shims is unvalidated: the real cells hold
  their reset-busy flags for many cycles while clearing the array and the shims
  release after one. No cross-check covers it.
- The two memory shims have no cross-check bench at all. They are exercised
  indirectly by benches that pass, which is evidence and not proof.
