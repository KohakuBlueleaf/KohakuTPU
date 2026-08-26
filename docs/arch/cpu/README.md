---
title: Processors
summary: The machine has two RISC-V processors — a batch compute unit and a runtime host. Why there are two, what they share, and how to choose between them.
tags:
  - architecture
  - cpu
  - overview
---

# Processors

A KohakuAccel machine carries **two RISC-V processors**, and they are not two
sizes of the same thing. They exist because the machine has two jobs with
incompatible lifecycles, and a lifecycle decides a processor's shell before it
decides anything about its pipeline.

| | [**RV32 PE**](rv32-pe/) | [**RV64 system core**](rv64-sys/) |
|---|---|---|
| what it is | a small programmable **compute unit** on a mesh | a **runtime host** that boots once and stays up |
| its life | kicked, runs to completion, reports a 32-bit word | boots, runs forever, never reports a completion |
| written for | sequencing kernels, walking descriptors, reacting to completions | running a runtime: allocation, scheduling, servicing the units |

Everything else about them follows from those three rows.

## Why the machine cannot use one core for both

The framework hands every compute unit the same shell, `noc_cu_base`, and that
shell implements one shape of life: *someone kicks me, I run to completion, I
report a word.* Three things make that shape wrong for a runtime host, in order
of how hard they are to work around.

**1 · There is no completion to report.** A program meant never to end has
nothing to put in the result word and never reaches the state the shell exists
to announce. Building a *run → done* wrapper around it is machinery that can
only ever be half-used.

**2 · The unit that arbitrates the fabric must not be flow-controlled by the
fabric.** This is the argument that actually forces the split, and the cycle is
specific. The host is the unit that *services* the network: it dispatches
instructions to compute units and consumes their completions. Behind the shell,
its inbound path is gated by the shell's own finite instruction and receive
queues, and its outbound dispatch shares one port with the shell's signal and
control traffic. So: the host blocks trying to send a dispatch → it stops
draining its receive queue → the queue fills → its inbound path backpressures →
the completions it needs in order to make progress cannot land.

A compute unit can afford to block, because something else is scheduling it.
The scheduler cannot.

**3 · A loader is a second memory-write protocol.** Behind the shell, a program
image arrives as flits, which needs a loader state machine, a buffer-id map, a
bounds check and a receive-quiet interlock. The host already sits on a memory
path the outside world can write directly, so loading its memory is an ordinary
write plus a doorbell, and none of that machinery has to exist.

**The shell is not expensive.** `noc_cu_base` measures **756 LUT**
out-of-context on its own. Against a core in the six-thousands that is small,
and the argument above is lifecycle and deadlock — **not** area. Quoting the
shell's cost as the reason for the split gets the reasoning backwards.

What the host takes on by dropping the shell is real and not free: the shell
guaranteed *every write is visible when the completion arrives*. A processor
without it has to publish its own ordering guarantee to whoever waits on it.

## What the two share

They are more alike than the table above suggests, and deliberately so.

- **The same authorship.** Both are cores written for this project, in this
  repository, under the same resource priority: **LUT is the objective**, block
  RAM and URAM are worth spending LUT to reach, and flip-flops are effectively
  free — so both prefer a pipeline stage to a bypass, and neither treats a
  flip-flop count as an argument for or against anything.
- **The same build and measurement flow.** Both are synthesised out-of-context
  on `xcvu13p-fhgb2104-2L-e` under Vivado 2024.2 by a script in
  `scripts/tcl/`, and both are verified by a golden-model co-simulation that
  compares PC, destination and value for every committed instruction before
  anything reaches synthesis.
- **The same memory conventions.** Both name their memory primitives rather
  than letting the tool infer them, because read latency here is pipeline
  structure. Both reach memory through a frontend that is part of the core
  rather than a generic CPU bus with an adapter bolted on. Both treat *program
  order is arrival order, per destination* as the property their communication
  idioms rest on.
- **The same physical-address pipeline.** Address translation is a property of
  the *system*, not of the pipeline: the RV64 core proper is a
  physical-address machine, and the page-table walk and TLB live in the wrapper
  around it. That is what lets the same core appear with and without an MMU.

## Choosing between them

| If you are… | Use |
|---|---|
| putting a programmable sequencer on a mesh beside your datapath | [**RV32 PE**](rv32-pe/) |
| writing kernels that push, doorbell and hand off through DRAM | [**RV32 PE**](rv32-pe/) |
| running many small controllers and counting LUT | [**RV32 PE**](rv32-pe/) — it is around a third of the size, though see the caveats below before treating that as a ratio |
| running a program that does not end | [**RV64 system core**](rv64-sys/) |
| needing 64-bit addresses, privilege modes, or virtual memory | [**RV64 system core**](rv64-sys/) |
| needing atomics between two writers outside DRAM | [**RV64 system core**](rv64-sys/) |
| replacing the host's role in scheduling work on the card | [**RV64 system core**](rv64-sys/) |

The RV32 PE can already dispatch instructions to peers and collect their
completions ([programming](rv32-pe/programming.md#dispatch-and-completions)),
so a mesh can run a dependency graph without either processor's help from the
host. What it cannot do is *stay up between graphs*.

## The two, side by side

The RV64 core exists in two configurations, and the shell is what separates
them — so this is really three deployments of two cores:

| | RV32 PE | RV64 as a mesh compute unit | RV64 as the system core |
|---|---|---|---|
| module | `rv_pe` | `rv64_sys_pe` | `rv64_syscore` |
| ISA | RV32I + the multiply half of `M` | RV64I + `M` | RV64I + `M` |
| divide | **faults** | built | built |
| atomics (`A`) | none | **optional** — a compute unit has no second writer to race | **required** |
| privilege | none | machine, supervisor and user — the same CSR file, but with no MMU behind it they buy no isolation | **machine, supervisor and user**, with delegation |
| CSRs | **none at all** | `Zicsr` | `Zicsr` |
| MMU | none | none | **Sv39**, on fetch and data |
| interrupts | none — it halts | timer, and software from its own doorbell register; the external line is tied off | timer, software and external, at either level |
| shell | `noc_cu_base` | `noc_cu_base` | **none** — fused directly to the memory agent |
| where it sits | a mesh router's local port | a mesh router's local port | inside the system node |
| address space | its own windows + a 2 GB DRAM aperture | its own instruction memory + scratchpad | the node fabric |
| how it starts | a kick (`CU_INST`) | a kick (`CU_INST`) | it boots |
| how it ends | halt → `CU_SIGNAL` | halt → `CU_SIGNAL` | a store to a control region, not `ECALL` |
| **DSP48** | **4** | **4** | **4** |

### What each one costs

**A module does not have a LUT count.** It has one per measurement context, and
the rows below are not interchangeable readings of one number.

Everything here is `xcvu13p-fhgb2104-2L-e` under Vivado 2024.2, **synthesis —
not placed and not routed**, and every figure is that module **synthesised
out-of-context as its own top**.

| | RV32 PE | RV64 mesh CU | RV64 system core |
|---|---|---|---|
| **current, `-flatten_hierarchy none`** — CLB LUT sites at a 3.333 ns request | **2,586 · 363.5 MHz** · +0.582 ns | **not measured** | **7,334** · −0.465 ns, so **263.3 MHz** as an upper bound |
| **an earlier vintage, `rebuilt`** — not re-run | — | 6,360 · 316.3 MHz · +0.171 ns | 6,349 · 307.8 MHz · +0.084 ns |
| **an earlier vintage still** — accounting not recorded | — | 6,772 · 315.7 MHz · +0.165 ns | 6,335 · 331.1 MHz · +0.313 ns |
| FF | 3,844 | 4,394 (earlier vintage) | 5,856 (current, `none`) |
| BRAM · URAM | 9 · 0 | 6 · 1 (earlier vintage) | 12 · 1 (earlier vintage) |
| script | `ooc_rv_pe.tcl` | `ooc_syscore.tcl` | `ooc_syscore.tcl … HIER` |
| flow | `-flatten_hierarchy none` | `rebuilt` | `none` on the current row, `rebuilt` below it |

**One cell in that table is recorded two ways and is `[unverified]`.**
`rv64_sys_pe`'s 6,772 · 315.7 MHz appears here as a standalone run of an older
vintage and on
[rv64-sys/performance](rv64-sys/performance.md#as-a-sub-hierarchy-inside-a-larger-synthesis)
as an in-system sub-hierarchy reading. Its context has not been re-established,
and the two pages are left disagreeing rather than quietly reconciled — a figure
whose context is unknown is not a figure.

**Neither RV64 column has a current `rebuilt` standalone figure.** Those runs
predate the privilege, Sv39, fetch-translation and dispatch-mailbox work, and
the CSR file alone has grown since; they are kept as the last readings that
exist and are labelled as such. The RV64 system core's current row is the
`none` attribution run, which is the **same flow as the RV32 column** and so is
the one place on this page where the two processors are measured comparably —
though still at different RTL vintages, which is its own axis.

Where it actually ships, inside a system node at `rebuilt`, `rv64_syscore`
measures **7,244 LUT, 5,776 FF, 12 RAMB36 + 2 RAMB18, 1 URAM, 4 DSP**, and the
node it sits in **meets its 300 MHz request in out-of-context synthesis** —
WNS +0.039 ns, no failing endpoints. That is a synthesis result and not closed
timing: nothing here has been placed and routed. That is a different measurement context again and must not be
subtracted from the row above it —
[rv64-sys/performance](rv64-sys/performance.md#in-context-inside-the-system-node).

**The three LUT rows differ by vintage, by flow, and possibly also by
accounting.** The current row is CLB LUT **sites**, read from
`report_utilization`. The oldest figures were recorded without their accounting
named, and the tooling emits two incompatible ones from the same run, so which
they used cannot be recovered. They are kept because they are the only readings
of that vintage, and labelled for what is actually known about them: standalone
runs of that top, earlier RTL, accounting unrecorded.

The system core's figure moved from 331.1 MHz to 307.8 MHz between the two
`rebuilt` vintages, and what changed is not established here.
[rv64-sys](rv64-sys/) owns the question. **Neither is current**, and the current
row is not a fourth point on that line — it is a different flow answering a
different question.

### The four axes a LUT count varies along

A figure needs all four named, and one missing any of them cannot be compared
with one that has them:

| axis | the two answers in this tree |
|---|---|
| **context** | synthesised standalone as its own top, or counted as a sub-hierarchy inside a larger synthesis |
| **flow** | `-flatten_hierarchy none`, or `rebuilt` |
| **timing request** | whatever period was asked for — a tighter one buys LUT and not always megahertz |
| **accounting** | CLB LUT **sites** from `report_utilization`, or raw LUT **primitives** from a `REF_NAME`-filtered cell count |

That last axis is the easiest to miss because both numbers come out of the same
run and neither is labelled in passing. They are not close: the RV32 PE is
**2,586 CLB LUT sites and 2,910 raw LUT primitives**, and the gap is larger on
both RV64 configurations. A site count and a primitive count answer different
questions — how much of the device is occupied, versus how many logic cells
were inferred — and subtracting one from the other is meaningless.

**These are different kinds of difference, and only one of them is about the
design.** Two readings at different *vintages* are the same measurement of a
design that changed. Two readings in different *contexts, flows or accountings*
are different measurements of a design that did not. Neither kind may be
subtracted, averaged, or read as one being better than the other — **a figure
without its axes is not a smaller or larger number, it is a different
number** — but only the vintage kind says anything about the RTL having moved.

### Reading a figure's context off its memory profile

Establishing one figure's context does not establish its neighbour's. Two
numbers quoted side by side in the same source need not share one.

The check that settles it is the **memory profile, not the LUT count.** BRAM,
URAM and DSP counts move with configuration and barely with RTL vintage or
accounting, so they identify which run a figure came from where LUT and Fmax
cannot — two readings 24 MHz apart that agree on 12 BRAM, 1 URAM and 4 DSP are
the same configuration measured twice, not two different configurations.

A second tell separates a standalone run from a sub-hierarchy: **a slack and a
failing-path count belong to a synthesis run, not to a branch of one.** A
figure quoted with its own Fmax, its own slack and its own failing-path count
came from a run whose top it was.

The same rule governs the compute-unit shell inside the RV32 PE, where
`noc_cu_base` has three different LUT counts depending on how it was measured
([rv32-pe/performance](rv32-pe/performance.md#a-shell-figure-means-nothing-without-its-configuration-and-its-request)).
It has now caught three figures in this tree along three different axes, which
is why it is stated here as a rule rather than as a footnote on each of them.

### The condition on those figures

**No Fmax anywhere in this repository is a closed-timing figure.** Synthesis
slack is optimistic, and elsewhere in this project a module lost 0.740 ns going
from synthesis to routing. [measurement](../physical/measurement.md) defines
this for the whole tree.

Two further qualifications, because the *columns* are not directly comparable
either — the rows above vary by vintage and accounting, these vary by flow:

- **Different flows, and not uniformly by column.** The RV32 column comes from
  `scripts/tcl/ooc_rv_pe.tcl`, which synthesises with
  `-flatten_hierarchy none`. The RV64 columns come from
  `scripts/tcl/ooc_syscore.tcl`, which synthesises with
  `-flatten_hierarchy rebuilt` unless it is passed `HIER` — so the system core's
  *current* row is `none` and the two rows beneath it are `rebuilt`. The two
  flows differ by hundreds of LUT and several megahertz on the same RTL: on
  `rv64_core` the same design measured **6,012 LUT at `none` against 5,824 at
  `rebuilt`**. **So do not subtract across this table** either: compare a
  processor against itself, on one axis at a time.
- **Nothing here is an in-*device* number.** Every figure is an out-of-context
  synthesis; nothing has been placed and routed inside an assembled device
  image, so none of these frequencies says what a ship will close at.

### Why the two RV64 configurations differ where they do

Each difference in that middle-versus-right column is a decision with a reason,
not a build variant:

- **Atomics are optional on the mesh unit and required on the system core.** A
  compute unit has no second writer to race. The system core does: on-chip
  staging is multi-writer, and without atomics the machine cannot express a
  multi-writer location outside DRAM at all. Where atomics are dropped,
  synthesis constant-propagates the whole atomic state machine away, so the
  saving is real and needs no restructuring.
- **The MMU is on the system core only.** Translation wraps the core rather
  than living in the pipeline, so the mesh unit carries no MMU and pays nothing
  for one, and the core's memory port stays a plain synchronous interface in
  both.
- **The exit protocol differs because `ECALL` has to stay a call.** On a core
  with supervisor mode, making `ECALL` the terminator would remove the point of
  having supervisor mode, and making `EBREAK` the terminator would report every
  clean finish as a fault. So the terminator moves: the system core exits by
  storing to a control region, which reports a clean cause rather than a fault.

The details of both RV64 configurations — the pipeline, the memory system, the
integration into a system node, and their measured behaviour — are in
[rv64-sys/](rv64-sys/).

## What neither processor is

- **Neither is the machine's arithmetic.** The wide datapaths do the work;
  these are controllers and hosts. Neither has scalar floating point.
- **Neither is a general-purpose application processor.** There is no
  user-mode/kernel-mode software ecosystem here, no device tree, no interrupt
  controller you can attach arbitrary peripherals to.
- **Neither is required in order to use the framework.** A compute unit you
  write does not have to be a processor, and nothing in the fabric knows the
  difference.
- **Neither has a second clock domain.** Each lives entirely in the domain it
  attaches to, so there is no clock-domain crossing inside either.

## Fixed protocol, customizable addon, convention, or yours

| Thing | Category |
|---|---|
| the compute-unit port and the flit both processors' mesh configurations attach through | **fixed protocol** — [spec](../../spec/) |
| the kick/completion lifecycle a shelled unit presents | **fixed protocol** |
| each processor's own address regions, control words and halt model | **fixed protocol** of that processor |
| which processor a design uses, and how many | **yours** |
| what a program on either one computes | **yours** |
