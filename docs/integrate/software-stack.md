---
title: The software stack
summary: What the host side must do for any project, what belongs to one project, and what it would take to make that split real.
tags:
  - integrate
  - driver
  - compiler
  - design
---

# The software stack

> **Kind: mixed.** The `Transport` interface, the dispatch protocol and the
> control-register surfaces it drives are **fixed protocol** — they are
> [spec/control-registers.md](../spec/control-registers.md) seen from the host.
> The layering below that — where a framework `Machine` stops and a project
> `Target` starts, what the four transport backends are, how the three
> simulation levels divide — is **convention**, and it is one worked answer. The
> encoder, the scheduler and the device model are **yours**.

Every accelerator built on this framework needs the same five things on the host
side. Only two of them are about your accelerator.

This page is a guide and a design document in roughly equal parts. The split it
argues for is the layout of `software/`: `kohakuaccel` is the framework,
`kohakutpu` and `toyaccel` are projects on top of it, and
`software/tests/test_imports.py` fails if the framework imports a project. §6
is the layout and names the couplings that are still uncut.

---

## 0. Where the host stops

Everything below assumes the host drives the machine. That is one of two shapes,
and which one you are in changes what "the software stack" means.

**The system node ships a control processor**, and which one is a build-time
parameter — `CPU_RV64` ([spec/parameters.md](../spec/parameters.md) §5).

| | `CPU_RV64 = 0` — the RV32 complex, the default | `CPU_RV64` set — the RV64 complex |
|---|---|---|
| what it is on the mesh | an ordinary compute unit: a `CU_CTRL` block, an instruction FIFO, completions | **nothing.** Its hub port is tied off both ways: it sends no flit, and a flit sent to its coordinate is accepted and discarded |
| how the host reaches it | dispatch, exactly like any unit | a dedicated window: load instruction memory, load scratchpad, ring a boot doorbell, poll status |
| what runs on it | short RV32 routines the host dispatches | **a program.** RV64IMA with Sv39 translation, an L1 onto DRAM, a scratchpad, and the memory mover as its memory unit |
| what the host does per step | everything: stage, kick, poll, decide | **boots it once** |

The second column is the one that changes this page. It is a runtime processor
inside the node — something to *target*, not just something to drive. The work
that §3 describes as the host's, and §5 as a simulation of the host's, has
somewhere on-card it could move to.

**Two cautions before you plan around that.** `CPU_RV64` defaults to 0, so the
first column is what ships; and the second column is **not finished** — the RV64
branch leaves the processor's mesh port tied off, so it can neither dispatch to a
compute unit nor receive one's traffic, and it has no doorbell and no external
interrupt ([spec/parameters.md](../spec/parameters.md) §5). Today it runs a
program against memory and the mover, and the *host* still dispatches every
compute unit through the orchestrator.

**So nothing on this page is retired.** The orchestrator's map is the same either
way, and a machine with the RV64 complex still needs transport, still needs a
loader, and still needs completion tracking for the compute units. What changes
is that a second target exists, with its own toolchain, its own memory image and
its own contract:
[spec/control-registers.md](../spec/control-registers.md) §6–§7 for the window
and the control region, and
[arch/cpu/rv64-sys/programming.md](../arch/cpu/rv64-sys/programming.md) for how a
program is built and run.

The rest of this page is the host side, which every machine has.

---

## 1. The five jobs

| job | project-independent? |
|---|---|
| **Transport** — reach the device's register window and its memory | yes, entirely |
| **Dispatch** — stage instruction words, kick, account for credits | yes, entirely |
| **Completion tracking** — know when work finished, and whether it failed | yes, entirely |
| **Debug plumbing** — enumerate units, read counters, decode status | mostly; the last decode step is per unit type |
| **Simulation** — run a program without hardware | mostly; the reference arithmetic is yours |
| **Encoding** — a shape becomes instruction words | **no.** Yours |
| **Scheduling** — what runs where, in what order, at what tile size | **no.** Yours |
| **Device model** — what a machine of yours contains | **partly.** The mesh is framework; the capacities are yours |

The first five are the driver framework. The last three are your driver.

The first five are also the ones you can build and test **before your accelerator
computes anything**. Transport, dispatch, credit accounting, completion polling
and enumeration are exercised entirely by the recording and in-memory transports
and by a mesh whose units the driver knows nothing about beyond what they publish
over the control plane. Build them first; a compiler that emits perfect
instructions into a broken dispatch path looks exactly like a broken compiler.

---

## 2. Transport

The entire hardware dependency is two methods on a 64-bit window:

```python
class Transport(abc.ABC):
    bulk = False
    def write64(self, addr: int, data: int) -> None: ...
    def read64(self, addr: int) -> int: ...
```

`write_block` / `read_block` exist but are **not a second contract**: a block at
an address must be indistinguishable from the equivalent run of word accesses at
ascending addresses, little-endian, which is exactly what the base class does. A
backend overrides them only when it has a transfer that beats the loop. That
equivalence is what lets one test compare a bulk backend against recorded word
writes and demand the same bytes at the same addresses.

`bulk` says whether the override happened, because **the caller has work to do
either way**: coalescing scattered writes into contiguous runs is worth the
arithmetic over a DMA path, where a run is one descriptor and a loop is one
descriptor per word, and worth nothing over a transport that will only unpack it
again.

Four backends, and each earns its place:

| backend | what it is | for |
|---|---|---|
| recording | records writes instead of performing them | the fast test tier needs no simulator at all |
| memory | a dictionary standing in for the device | unit-testing the driver's own arithmetic |
| DMA | the production path | operands, at real bandwidth |
| debug link | a slow AXI window over the debug interface | bringup, and it works before the host has enumerated the card |

**The two hardware backends must be mapped identically and verified byte-exact**,
so a pointer means the same thing on both. That is what makes the slow one a
debugger for the fast one rather than a separate world.

Two design rules worth copying:

- **Distinguish "this backend is not available" from "this backend failed".**
  Absence is a configuration answer — wrong host, driver not installed, card not
  enumerated — and a caller that can fall back needs to tell them apart without
  parsing an OS error code.
- **A guard is not a limit, and its message must say so.** The debug link refuses
  transfers past a size ceiling, because a ceiling in bytes against a measured
  rate is the only honest way to say "this will not be quick". The refusal names
  the knob that raises it — and it has still cost a whole session of measurements
  that were recorded as impossible when they were merely slow. If you add a
  guard, make the escape hatch part of the error text, and treat a "cannot" from
  your own tooling with suspicion.

---

## 3. Dispatch and completion

This is the part most likely to be reinvented badly, so it is worth stating as a
protocol rather than as an API.

**Staging.** Instruction words go into the orchestrator's staging window over the
same transport as everything else. Walk slots in address order, so a whole
round's staging collapses into one contiguous block — one DMA descriptor rather
than one per word.

**Kick.** Write the destination node, the first staging slot, the flit count, and
then the kick. The write *is* the launch. One kick is one destination; work for
four units is four kicks, and giving each its own base slot is what stops them
serialising.

**Credits.** Seed the credit count before the round. The dispatcher will not push
more instructions than the target has room for, and each ordinary completion
refills one.

> **The credit accounting has a sharp edge.** Only an *ordinary* completion
> refills a credit. The final instruction of a batch — the one whose `last` bit
> is set — retires as a batch completion instead, and does not. Credits are
> therefore re-seeded per round rather than accumulated, and a driver that
> assumes conservation will slowly starve. This is real behaviour in the shipping
> RTL, not a bug to route around.

**Completion.** Two mechanisms, and use the right one:

- a **per-node status word**, updated from every signal, carrying the signal code,
  its argument and a *count* — so a host polling more slowly than events arrive
  can tell how many it missed rather than merely that something happened;
- a **single global completion counter** across all nodes, so "is everyone
  finished" costs one read instead of one read per node. It counts every signal
  regardless of code — a host that waits only for ordinary completions waits
  forever, because the last instruction of each batch reports a different one.

Signals are **absorbed**, not queued into the raw-flit receive path. Queuing them
was tried: unread signals fill the receive FIFO, raise the orchestrator's busy
line, and stop it accepting the very signals that return credits, so the machine
stalls silently after a fixed number of completions with nothing reporting an
error.

**The staging buffer is single-use while a dispatch runs.** The dispatcher streams
out of it, so refilling before it drains corrupts the program in flight. Waiting
for the *dispatch* to drain is not the same as waiting for the unit to finish
executing — the unit keeps working while the next program is staged, and that
overlap is where the concurrency comes from.

### The control program

There is one more layer, and it is what makes a run a single host transaction: a
small engine executes a list of **write**, **poll** and **done** commands loaded
into a command window. Three opcodes are enough because the machine's entire
control surface is memory-mapped, and branches or arithmetic there would only
duplicate the host.

The value is latency. A run becomes one host transaction instead of a poll loop
across the link, and the same program works over the fast and slow transports
alike. Your driver's output, in the end, is a control program plus a staged
instruction image.

---

## 4. Debug plumbing

Everything here works against an empty machine, which is why it is worth building
first.

**Enumeration.** A control read to a node returns its type, version, buffer count
and instruction depth; a second returns busy, error and instruction free space.
This is how a driver sizes itself without a hardcoded map — and how it discovers
that the bitstream is not the one the compiler targets.

**Counters.** Two registers, and they are different in kind. One is
framework-owned and identical for every unit type: retired instructions and busy
cycles. The other is unit-defined — the 64 bits the datapath drives — and only
its owner knows what it means.

**That second one is the exact place where a driver framework meets a project.**
It is a registry (`kohakuaccel.driver.unit`): a project registers a decoder
against its type code, the framework asks the registry, and a type nobody
registered reads as `UNKNOWN_DBG` rather than crashing.

**Fault reporting.** A fault arrives as a signal code with the unit's own 32-bit
argument. The driver should surface it as the unit's fault code, not as a
generic failure — the unit went to the trouble of encoding one.

**On a node carrying the RV64 complex there is a second set of counters and they
are not reached this way.** Cycles and retirements come from the processor's host
window rather than from a `CU_CTRL` read, they are 64 bits rather than 32 —
because a runtime runs long enough to wrap 32 — and they are zeroed at each boot
rather than being free-running since reset. A driver **MUST NOT** difference two
reads across a boot, and **MUST NOT** feed them to a decoder written for
`CU_COUNTERS`. [spec/control-registers.md](../spec/control-registers.md) §6.3.

**Disassembly.** Turning a staged flit back into readable fields is a project's
job today and is worth having from the start: the most common bring-up question
is "is the machine wrong, or did I stage what I think I staged".

---

## 5. Simulation

Three levels, and they answer different questions. Keeping them separate is the
point.

**A software model of the arithmetic.** Whatever your transform occupant and
datapath compute, in Python, exactly — including precision, not idealised. This
is the golden reference the hardware bench checks against, and it is what lets a
kernel be checked before any RTL exists. If a third-party implementation of your
number format exists, check *your model* against it: a format comparison where
you wrote both sides proves nothing.

**A functional interpreter.** Execute a schedule against arrays, with the engines
applying their real arithmetic. Answers "does this program compute the right
numbers" in seconds instead of a full RTL run.

**A live simulator session.** The real RTL, driven by the real driver through a
transport that talks to the simulator instead of a card. This is the authority; a
functional disagreement with the interpreter is a compiler bug, and only a
*timing* disagreement needs the RTL.

The design property that makes this work is that **all three are driven through
the same `Transport` interface**. The recording backend gives you a byte-exact
trace with no simulator at all; the memory backend gives you a device that
answers; the session backend gives you the RTL. One driver, four devices, and the
test that compares two of them byte for byte is what keeps them honest.

The framework should own the session machinery — building the simulator snapshot,
holding it under a lock, running a program, timing out and reporting a hang *as a
hang*. That last one matters more than it sounds: a wedged bench does not fail, it
runs to its own watchdog and then grades whatever the untouched memory held, so a
stall arrives as a wrong answer after a long wait and points at the datapath
rather than at the thing that stopped.

---

## 6. The framework and its projects

Everything above describes what a driver framework should provide. It is
`software/`, one uv workspace with one member per component
([software/README.md](../../software/README.md)). A module is named
`<side>.<component>.*`: the side is `kohakuaccel` (the framework) or a project
(`kohakutpu`, `toyaccel`), and each side is a namespace package spread across
the members.

### The layout

```
    software/
      language/     kohakutpu.language     the .ktpu text, L3 / L2 readers,
                                           the L3 -> L1 compilers
      compiler/     kohakuaccel.compiler   L1 programs, packages, the machine
                                           description, the ISA field tables
                    kohakutpu.compiler     the ISA, encoders, layouts, the L1
                                           body and image emitters
      driver/       kohakuaccel.driver     transport/, device/, unit/ (the
                                           registry), node/, daemon/, runtime/
                    kohakutpu.driver       clock/, host/, units/, daemon/
      simulation/   kohakuaccel.simulation the package interpreter and SimMachine
                    kohakutpu.simulation   verilator/: the Verilator card
      firmware/     (C)                    the node firmware and its host tools
      application/  kohakutpu.application  a skeleton, and the kernel tools
      template/     toyaccel.*             the smallest project, every component
```

### The import rules

`software/tests/test_imports.py` reads every module's imports and fails on a
breach:

| component | may import |
|---|---|
| language | language |
| compiler | compiler, language |
| driver | driver |
| simulation | simulation, driver, compiler, language |
| application | every component |

On top of that the framework imports no project, a project imports no other
project, and every import sits at module level. The driver imports no compiler:
it moves bytes, words and packages, and what they mean is the compiler's.

### The couplings, and which are cut

1. **The unit types.** A framework module carrying a project's type table is
   cut: `kohakuaccel.driver.unit` is the registry a project populates
   (`kohakutpu.driver.units`, `toyaccel.driver.unit`).

2. **Machine geometry against project capacities.** `kohakuaccel.compiler.machine`
   (`MachineSpec`, a `MeshSpec` per mesh) carries the geometry; a project's
   target (`kohakutpu.language.target`, `kohakutpu.compiler.target`) its
   capacities.

3. **The bench source list is a hand-maintained file list** naming every RTL file
   the driver-to-simulator path elaborates, framework and project mixed. Adding a
   client to the memory agent means editing it. **Still open.** *Fix: the
   framework owns its own list; a project appends.*

4. **The mesh generator's vocabulary is hardcoded** to three KohakuTPU tokens
   ([mesh-topology.md](mesh-topology.md) §2). **Still open.** *Fix: a
   project-supplied token table.*

5. **The transform occupant.** The occupant is
   `src/kohakutpu/transform/xform_bank.v`, the framework fixes only the module
   NAME `xform_bank`, and `src/templates/transform/` supplies an identity bank
   so a framework-only build elaborates. `tests/sysnode/xform_identity_tb.v`
   builds exactly that and would fail to compile if the rule were broken.

6. **The SIMD unit.** It is `src/kohakumpe/simd/` — project→project, the
   allowed direction — and `SIMD_EN` is a slot like `xform_bank`.
   `scripts/py/deps.py` holds it: it reads every instantiation under
   `src/kohakuaccel/` and fails the run on one whose module is defined only under
   a project, so the framework tree parses with no project source on the path.

### The second project

The test that the split is real: **with no KohakuTPU package in play, can the
framework's driver open a transport, enumerate a mesh, stage and dispatch a
program, and account for its completions — knowing nothing about the units
beyond what they publish over the control plane?** That is `toyaccel`
([software/template/README.md](../../software/template/README.md)):
`python -m toyaccel.application.run` discovers two saxpy units on a
`SimMachine`, dispatches to both, and grades the vector it reads back.

---

## 7. Open questions

- **Where the boundary between a framework `Machine` and a project `Target`
  falls** is a judgement call, not a derivation. Dispatch limits are clearly
  framework and tile budgets clearly project; node counts are arguable, because
  the compiler wants "how many of my engine are there" and the framework knows
  "how many endpoints of each type".
- **Whether the machine description should be derived from the mesh map** rather
  than written twice. Today a map generates RTL and a board file describes the
  same machine to software, and nothing checks that they agree. The failure mode
  is a driver addressing a node that is not there.
- **There is no machine-readable instruction encoding.** Each project writes an
  encoder in Python and a decode in Verilog and nothing proves they agree except
  a test comparing bytes against a second implementation. A field description
  with both sides generated from it is the obvious fix and does not exist.
- **The framework has no opinion about the compiler.** Levels of IR, scheduling,
  tiling and fusion are entirely a project's. That is probably right — the
  framework serves people who want to design a machine, and a machine's compiler
  is part of the machine — but it means every project starts its middle end from
  nothing, and the parts that are genuinely reusable (a graph, a scheduling
  representation, a cost model interface) have not been separated out.
- **Nothing enforces the driver's own conformance.** A unit has a port contract
  and a bench for it; a driver has neither.
