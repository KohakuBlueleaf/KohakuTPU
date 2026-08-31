---
title: Projects
summary: Accelerators built on KohakuAccel, documented at their own level — what each one computed, and how it used the framework.
tags:
  - projects
  - overview
---

# Projects

A project is an accelerator built on KohakuAccel: a compute unit somebody wrote,
plus the choices that turned it into a device image. This directory documents
projects at their own level.

A project doc answers **"what did this one compute, and how did it use the
framework"**. It does not answer "how does the framework work" — that is
[arch/](../arch/README.md), [spec/](../spec/README.md),
[integrate/](../integrate/README.md) and [workflow/](../workflow/README.md), and
none of those pages may depend on any project existing. The dependency runs one
way: a project doc cites the framework, never the reverse.

| | |
|---|---|
| [kohakutpu/](kohakutpu/README.md) | An MXFP7 tensor accelerator on `xcvu13p-fhgb2104-2L-e`. The reference instance. |
| [kohakumpe/](kohakumpe/README.md) | The same framework with **processors** on the port instead of a fixed datapath — a SIMD PE and a SIMT PE. It also supplies the SIMD unit that fills the framework's `SIMD_EN` slot. |
| [kohakuaxi/](kohakuaxi/README.md) | Two AXI systems. The **station bus**: a line of AXI stations replacing a large crossbar, at O(1) per port — the fabric the other two reach their meshes and control registers through. The **fused crossbar-cache**: M masters to N cached DRAM channels as one system with AXI only at its edges. |
| [kohakutransmit/](kohakutransmit/README.md) | **Kohaku Transmit Surface**: a credit-based transport with no `ready` on the wire — latency-insensitive across register stages, dies, clock domains and chips; carried over AXI4-Stream, AXI4 or a serial stream; AXI4 carried over it. A standalone project that imports nothing and is documented on its own. |

Two projects rather than one is the point of the numbers rule below: the same
framework measures differently under each, and neither set of figures is a
property of the framework.

## The rule about numbers

**A measurement describes one accelerator on one part.** Every Fmax, LUT count,
BRAM count, utilisation percentage and GFLOP/s figure in this directory was taken
from a specific design, on a specific device, under stated synthesis conditions.

Those numbers are **evidence that the framework reaches real silicon**. They are
not specifications of it. A second project on a different part will measure
different numbers from the same framework, and that is the expected outcome, not
a discrepancy.

Four obligations follow, and a project doc that skips one is wrong:

- **Name the device.** Never quote a frequency or a utilisation without it.
  For both accelerators here that is `xcvu13p-fhgb2104-2L-e`.
- **Name the tool and the mode.** Vivado 2024.2, and whether the run was
  **out-of-context or in-context**, **synthesis or routed**. Those four words
  change what a figure means, and none of them can be inferred from the number.
- **Say which direction the number bounds.** An out-of-context synthesis result
  that *met* its target is a **lower bound** on what the block can do; a run that
  *missed* is a **ceiling**. Out-of-context timing is an upper bound on placed
  timing in every case, because nothing is placed and the route is estimated.
- **Say where it came from.** Which script, which top module, which target
  frequency, which parameters. A figure whose conditions cannot be stated is
  reported as unconfirmed rather than as fact.

**No frequency anywhere in this directory is a closed-timing figure**, and no
page may present one as though it were. Every one is out-of-context synthesis
slack, which is optimistic — KohakuTPU's `m62_c1` mesh probe lost 0.740 ns
between synthesis and routing, enough to move a figure by most of a speed grade.
What *is* evidence that a design reaches silicon is a placed-and-routed result
with a written bitstream, and the pages that have one say so explicitly:
[kohakutpu/results.md](kohakutpu/results.md) §5.3 and
[kohakuaxi/station-bus.md](kohakuaxi/station-bus.md) §2.9.

Anything not measured is labelled **PROJECTED** or **ESTIMATE** where it is
used. Anything whose origin cannot be established is marked `[unverified]` and
is not repaired by repetition.

Each project keeps its measurements in one file — for KohakuTPU that is
[results.md](kohakutpu/results.md) — so there is a single place to check whether
a number is current, and design pages cite it rather than restating it.

## What a project consists of

The sections below are the template. They are derived from what KohakuTPU turned
out to need, and a second project is expected to copy the shape and diverge where
its own decisions differ.

| section | answers |
|---|---|
| **number format** | what the datapath computes on, and why that format rather than a standard one |
| **compute units** | the datapath itself: the circuit, what it maps onto in primitives, what bounds it |
| **instruction set** | how this project spent the framework's instruction payload bits |
| **software stack** | how a program becomes flits: IR, scheduling, frontends |
| **ship and device** | which part, which mesh populations, what got assembled |
| **results** | every measured number, with conditions |

The ordering is the order the decisions were actually forced. The format comes
first because it sets the operand width, which sets the packing, which sets the
cascade depth, which sets the block size — one chain of consequences, and the
project docs are written to make that chain visible rather than to catalogue
modules.

## Say which category each thing is

The tree distinguishes four kinds of thing, and **a project page has to label
them or it teaches the wrong lesson.** A reader who cannot tell which parts of a
worked example were forced will copy the accidents along with the decisions.

| category | meaning |
|---|---|
| **fixed protocol** | cannot be changed by a project — change it and the unit is not an endpoint |
| **customizable addon** | ships working and is meant to be swapped; the framework provides the slot, the project provides what goes in it |
| **convention** | how to design a thing. Some conventions are forced by the memory agent's design and some are free, and a project page should say which |
| **yours** | the project's own, top to bottom |

**Most of a project is the last row, and saying so plainly is what makes the
framework's claim credible.** A framework that had dictated the datapath would not
need a worked example to prove anything; the value of one is precisely that it
shows how much diverged.

KohakuTPU's own categorisation is
[projects/kohakutpu/README.md](kohakutpu/README.md) §3.1, and its sharpest
illustration is that its two compute units share nothing but the port — different
memory widths, different memory counts, different primitives, different read
latencies, and a macro-op against a program (§2.1 there).

Two things deliberately do **not** appear in a project doc:

- **Framework mechanism.** How a router forwards, how the memory agent turns a
  descriptor into AXI bursts, what the compute-unit port contract is. A project
  says *that* it uses these and *how much* of them it used.
- **Advice for other projects.** A worked example teaches by being specific. The
  generalisation belongs in [integrate/](../integrate/README.md), written by
  whoever generalises it — after there is a second data point.
