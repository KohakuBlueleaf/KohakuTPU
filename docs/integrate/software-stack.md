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

Every accelerator built on this framework needs the same five things on the host
side. Only two of them are about your accelerator.

This page is a guide and a design document in roughly equal parts, because the
software layer is the part of the framework that is **not yet frameworkised**.
`src/ktpu/` today is KohakuTPU's driver and compiler, not a framework for writing
drivers, and it contains both halves entangled. §6 says exactly where, and what
separating them would take. That section is the useful one if you are here to
build the framework rather than to use it.

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
Today the decoder for it lives in the framework half and switches on the unit
type, so a framework module carries a table of KohakuTPU's unit types. The right
shape is a registry: a project registers a decoder against its type code, and the
framework asks the registry. §6.

**Fault reporting.** A fault arrives as a signal code with the unit's own 32-bit
argument. The driver should surface it as the unit's fault code, not as a
generic failure — the unit went to the trouble of encoding one.

**Disassembly.** Turning a staged flit back into readable fields is a project's
job today and is worth having from the start: the most common bring-up question
is "is the machine wrong, or did I stage what I think I staged".

---

## 5. Simulation

Three levels, and they answer different questions. Keeping them separate is the
point.

**A software model of the arithmetic.** Whatever your read-path transform and
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

## 6. Frameworkisation: where the seam is not

Everything above describes what a driver framework should provide. What exists is
one Python package, `src/ktpu/`, which is KohakuTPU's compiler and runtime and
contains the framework half inside it. The split is not hard to see, which is the
good news; it has simply not been made.

### What is already framework-shaped

| module | what it is |
|---|---|
| `src/ktpu/hw/device.py` | the register maps, the flit codec, the `Transport` ABC, the recording and memory backends, the control-program builder, the completion helpers. Almost entirely framework |
| `src/ktpu/hw/jtag.py`, `xdma.py` | the two hardware backends |
| `src/ktpu/hw/sim.py` | the simulator session |
| `src/ktpu/hw/clock.py`, `chain.py`, `fpga.py` | runtime frequency, device chain, bitstream handling |

### Where the two halves are entangled

These are the specific couplings, each of which has to be cut:

1. **`device.py` knows KohakuTPU's unit types.** It defines constants for the
   matmul and vector unit type codes, and `decode_dbg(word, cu_type)` switches on
   them to name the counters. A framework module carrying a project's type table.
   *Fix: a registry a project populates.*

2. **`board.py` mixes machine geometry with project capacities.** It carries
   genuinely framework facts — mesh geometry, node coordinates, memory port
   coordinates, mesh id and count, transport selection, address rebasing, an
   address-space `verify()` — and also returns project types (`caps()`,
   `target()`), counts clusters specifically, and encodes MXFP7 quantisation
   flags in `upload_addr()`. *Fix: a framework `Machine`, and a project subclass
   or side-car for capacities.*

3. **`target.py` is entirely project-specific except for two fields.** Cluster
   counts, tile budgets, L1 entry counts, lane and block sizes, vector geometry —
   all KohakuTPU. `stage_flits` and `ncmd` are framework dispatch limits and
   belong with the machine. The `FEATURES` mechanism — an explicit set of
   optional hardware features, with a typo-raising `has()`, gated so that a
   program built for a machine lacking a feature still lowers and still runs — is
   a *framework* pattern with project-specific member names. *Fix: keep the
   mechanism, move the names.*

4. **The bench source list is a hand-maintained file list** naming every RTL file
   the driver-to-simulator path elaborates, framework and project mixed. Adding a
   client to the memory agent means editing it. *Fix: the framework owns its own
   list; a project appends.*

5. **The mesh generator's vocabulary is hardcoded** to three KohakuTPU tokens
   ([mesh-topology.md](mesh-topology.md) §2). *Fix: a project-supplied token
   table.*

6. **One package name for both halves.** `ktpu` is the compiler, the runtime, the
   device model and the framework. There is no import you can make that gets you
   the framework without the project.

7. **The read-path transform's software model lives in the project half but its
   RTL lives in a framework package** — the mirror image of the same problem
   ([README.md](README.md) §2).

### What separation would look like

```
    src/
      kohakudrv/            the driver framework
        transport.py        the ABC, recording and memory backends
        backends/           DMA and debug-link backends
        registers.py        orchestrator and control-register maps
        flit.py             the codec: headers, message classes, payloads
        dispatch.py         staging, kick, credits, the control program
        completion.py       status polling, global count, fault surfacing
        enumerate.py        discovery, and the per-type decoder registry
        machine.py          mesh geometry, node table, dispatch limits
        sim.py              the simulator session and its lock

    projects/<name>/sw/
        isa.py              encoding: your fields, your opcodes
        capacities.py       your Target, your features
        schedule.py         your policy
        model.py            your arithmetic reference
        decode.py           registers your dbg_ctr decoder
```

The test that the split is real: **a project's software imports the framework and
the framework imports nothing of the project.** Today that is false in both
directions.

There is one more test, and it is the better one: **with no project package
installed at all, can the framework's driver open a transport, enumerate a mesh,
stage and dispatch a program, and account for its completions — knowing nothing
about the units beyond what they publish over the control plane?** Everything in
§1's first five rows should be able to. If it can, the split is real; if it
cannot, the entanglement is still load-bearing.

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
