---
title: The measurement discipline
summary: What a number in this tree means — the five things every figure names, why no Fmax here is a closed-timing result, and which artefact to read.
tags:
  - architecture
  - physical
  - measurement
---

# The measurement discipline

Placement decisions need numbers, and numbers need instruments. This page says
what a figure anywhere in `arch/` means, so that the other pages can point here
instead of repeating it.

## The claim this whole tree rests on

> **No Fmax figure in this repository is a closed-timing result.** They are
> out-of-context synthesis numbers. Synthesis slack is optimistic — one module
> lost **0.740 ns** of worst slack going from synthesis to routing, same design
> and same constraints. An out-of-context synthesis Fmax is a screening number,
> not a guarantee that a design closes.

*(That 0.740 ns is KohakuTPU's `m62_c1` mesh probe on `xcvu13p-fhgb2104-2L-e`
under Vivado 2024.2 — see
[projects/README](../../projects/README.md).)*

The consequence is a rule about wording, not a caveat to be waved at. "312 MHz"
is not a statement this project can make. "312 MHz, out-of-context synthesis,
`xcvu13p-fhgb2104-2L-e`, Vivado 2024.2, top `foo`, failing run at 3.2 ns" is.
A page that prints the first form is claiming something nobody checked.

## The five things every figure names

| | example | why it changes the answer |
|---|---|---|
| **part** | `xcvu13p-fhgb2104-2L-e` unless the figure says otherwise | speed grade is not a rounding error; a `-2L` low-voltage part is slower than a `-2` |
| **tool and version** | Vivado 2024.2 | inference, packing and directives move between releases |
| **context** | **out-of-context** or **in-context** | a block synthesised alone optimises differently from the same block inside a parent |
| **stage** | **synthesis**, **placed**, or **routed** — three values, not two | slack moves at every one of them. See below |
| **what produced it** | `scripts/tcl/ooc_rv_pe.tcl`, top `rv_pe`, period 2.500 ns, `-flatten_hierarchy none` | the target period is part of the measurement, not context for it — the tool spends area to meet a constraint, so a LUT count without its period is under-specified. The flattening mode belongs here too, for the reason in [the re-parenting trap](#two-correct-reports-that-must-never-be-subtracted) |

Anything not measured is marked **PROJECTED** or **ESTIMATE**, in the same
sentence as the figure. A figure whose origin cannot be established is deleted
or marked `[unverified]`. It is never given a provenance that sounds plausible.

### Stage is three values, and this is the tree's vocabulary

A design passes through `opt_design` → `place_design` → `phys_opt_design` →
`route_design`, and **worst slack changes at every step**. Collapsing that into
"placed or not" throws away the distinction that matters most:

| Stage | What it knows | What a figure from it is worth |
|---|---|---|
| **synthesis** | logic structure. Routing is *estimated*, and estimated optimistically | a screen. It answers "is this deep enough to fail?", not "will it close" |
| **placed** | where the cells are, so real distance — but not the wires taken | the first point at which a distance-bound path is visible at all |
| **routed** | the actual wires | the only stage at which a timing result is a result |

**Synthesis and routed are different claims about different things**, and the
gap between them is not a rounding error. Say which one you have. A figure that
does not name its stage is assumed to be synthesis, which is the weakest reading
and usually the correct one.

### "Bound" points in two directions, and both are true

This is the single most misread word in these pages, because the same figure is
a bound in opposite directions depending on what it is a bound *on*. Both
statements below are true of one out-of-context run at once:

| Frame | The claim | Why |
|---|---|---|
| **on the block's own ceiling** — a run that MET its target | a **lower** bound | the optimiser stopped as soon as the constraint was satisfied. The true ceiling is at or above the target, unknown, and usually well above |
| **on the assembled, routed device** | an **upper** bound | composition costs frequency and routing costs more. The machine will not exceed what the block managed standing alone |

They are not in tension. A number can be a floor on what one block can do and a
ceiling on what the machine built from it will do, because those are two
different machines. **What produces a contradiction is writing the word down
without saying which frame you are in.**

This page uses the **device** frame throughout: when a page in `arch/` calls an
out-of-context figure an upper bound, it means the assembled routed machine will
not beat it. The block frame, and how to turn a met run into an actual ceiling
by tightening until it fails, is
[workflow/measure](../../workflow/measure.md#two-directions-of-bound-and-they-are-not-the-same-claim).

### One figure, written out in full

What a complete provenance line looks like, as a specimen rather than as a claim
about the machine:

> `rv_pe`, out-of-context **synthesis** — not placed, not routed — on
> `xcvu13p-fhgb2104-2L-e` under Vivado 2024.2, produced by
> `scripts/tcl/ooc_rv_pe.tcl` at a **2.500 ns request**:
> **2,672 LUT, 3,844 FF, 9 BRAM, 4 DSP, 363.5 MHz, worst slack −0.251 ns.**

Three things about that line are worth reading twice.

**The request and the achieved figure are different numbers, and both are
needed.** 2.500 ns is 400 MHz; the run achieved 363.5. Quoting "363.5 MHz"
alone loses the fact that it was asked for 400 and missed.

**The slack is negative, so this is a failing run — which makes the frequency
figure more informative, not less.** The tool tried as hard as it could and
still missed, so 363.5 MHz is close to what the logic actually costs. A *met*
run at a loose target would have told you less.

**The resource figures are not independent of the request.** The tool spends
area to chase a constraint it cannot meet, so the LUT count belongs to the
2.500 ns ask and not to the module. `ooc_rv_pe.tcl` takes the period as an
argument for exactly this reason and says so in its header.

Where that figure *lives* is the RV32 PE's own page, not this one —
[arch/cpu/rv32-pe/performance](../cpu/rv32-pe/performance.md). It appears here
only to show the shape.

## Read the report file, not the checkpoint

A routed **timing summary report file** is the artefact to parse. Opening a
design checkpoint to inspect it is slower and occupies a tool session — one that
a build or a hardware session may want — for a number that the run already wrote
to disk. Reach for the checkpoint when you need a query the reports do not
answer, not to read a figure back.

Utilisation and timing reports are written beside every run under
`build/ooc/<top><tag>/`:

    ooc.xdc              the constraints that were actually applied
    <top>.dcp            the checkpoint
    util.rpt             utilisation
    timing_summary.rpt   the summary
    timing_all.rpt       the worst paths, full clock expanded

and, for a run that was placed, `util_placed.rpt` and `timing_placed.rpt`
alongside them. The suffix on the directory is the run's tag, which is why a tag
that does not describe the configuration makes a result unattributable later.

Two report-reading habits do most of the work:

**Read the paths, not the number.** The worst path's start pin, end pin and
logic-level count say what kind of failure it is; the slack only says by how
much. The budget — **≤ 10 comfortable, 7–9 the upper limit a mature design sits
at, ≥ 11 the thing to fix** — is a screen for which question to ask, never a
verdict, and a 64-bit address adder is about 8 levels before it has done
anything with the result. The method is
[workflow/timing-closure](../../workflow/timing-closure.md#2-logic-levels-are-the-diagnostic-not-slack).

**Report where a block's cells actually landed.** Per-SLR spread for each
top-level block turns "the floorplan is what I asked for" from an assumption
into a report line — see [floorplan](floorplan.md).

## Two correct reports that must never be subtracted

One run can produce two per-unit accountings that disagree, with neither of them
wrong. A **hierarchical** utilisation report over a netlist synthesised with
boundaries rebuilt **re-parents leaf cells**: a cell that optimisation moved
across a module boundary is counted against wherever it ended up, not against
where it was written. A **flattened** run keeps the boundaries and counts it
where it came from.

The two therefore answer different questions and are measured on different
rules. From a single out-of-context synthesis of `rv_pe`
(`xcvu13p-fhgb2104-2L-e`, Vivado 2024.2, `scripts/tcl/ooc_rv_pe.tcl`, 2.500 ns):

| | hierarchical | flattened |
|---|---|---|
| `u_core/u_ex` | 489 LUT | 589 LUT |
| `u_core` | 1,298 LUT | 1,379 LUT |

Same design, same run, same instant. **Subtracting one from the other, or
mixing them in one table, produces a number that describes neither.** It is a
silent failure: both inputs are correct, the arithmetic is valid, and the result
is meaningless.

The rule that follows:

> **Attribute with one mode, report with the other, and never let a figure
> cross.** Use the flattened run to answer *where did the area go*; use the
> as-shipped run to answer *what does it cost*. Name which one a figure came
> from, every time.

The as-shipped flattening mode is the one the design actually synthesises at, so
it is the one an area claim must use — boundary optimisation is a real saving
and the flattened run forbids it. Which flag to pass, and when, is
[workflow/measure](../../workflow/measure.md#hierarchy-none-attributes-rebuilt-ships).

The same prohibition applies one level up: **the cost of a component inside an
assembly cannot be got by subtracting its standalone run from the assembly's
total.** Synthesise both assemblies and diff them instead.

## A search cannot prove a value is absent

The third member of the same family, and the cheapest one to fall into: **a
search that finds nothing has established that your pattern did not match, and
nothing else.** It is not evidence that the value is absent from the design.

The reason is that a parameter's value at elaboration need not appear anywhere
as a literal. It can be a ternary chosen per generate index, a macro, a
`localparam` computed from other parameters, or an expression evaluated at
elaboration time. Searching for the number then fails while the design carries
it. From this tree — `src/kohakuaccel/axi/topo/sb_line4.v:342`:

```verilog
.REQ_DEPTH((i == 2) ? 16 : 256),
```

Three of the four line stations elaborate with a request FIFO of 256. A search
for `REQ_DEPTH(256)` returns nothing, because the digits `256` never sit next to
the parameter name. **The value is computed, so it is invisible to a string
search and present in the silicon.**

> **To establish that a value is absent, read the instantiation. A grep can
> confirm presence; it can never confirm absence.**

This matters here rather than only in a style guide, because a wrong absence
claim propagates the same way a wrong number does — it gets repeated, and the
second repetition carries no trace of how weak the first one's evidence was. The
same shape recurs across this flow and is worth recognising as one thing:

| The query | What it silently returns | What it does not mean |
|---|---|---|
| a text search for a parameter value | no match | the value is not in the design |
| a property filter naming a property that does not exist | zero objects | nothing matched the filter |
| a hierarchical utilisation report on a rebuilt netlist | a per-instance number | that instance's cells |
| a parameter override naming a parameter the top does not declare | a clean run | the override took effect |

Every row produces a plausible result rather than an error. The tool-side
mechanics of the last three are
[workflow/tooling-traps](../../workflow/tooling-traps.md) and
[workflow/measure](../../workflow/measure.md#the-traps); the discipline they
share belongs here:

> **Check that the thing you asked for happened, not that the command
> returned.**

## A timing target is a budget, not zero

The useful target for an assembled device is a **worst negative slack budget**
rather than zero, because the number that matters downstream is the period the
machine actually achieves, not whether one arbitrary constraint was met. A WNS
figure only means something once it is converted back into an achieved period at
the clock it was reported against — and across several SLRs, that conversion has
to be done per clock, since a design that misses on one domain and passes on
three has a single achieved frequency set by the domain that missed.

Two corollaries, both of which change what an experiment is worth:

- **Implementation directives are zero-sum.** A directive sweep redistributes
  slack between paths; it does not create any. The ceiling is RTL work, and a
  sweep that moves WNS without changing the design has found a different
  placement of the same problem.
- **A met run has not measured the block's ceiling**, only shown it clears the
  bar — the block frame in
  [Bound points in two directions](#bound-points-in-two-directions-and-both-are-true).
  To measure a ceiling, tighten until the run fails and quote the failing run.

## Convention

**Name the instrument on every number.** *(Free.)* Part, tool version, context,
stage — **synthesis, placed or routed** — and what produced it, including the
target period and the flattening mode. An out-of-context frequency is an upper
bound on the assembled machine; quoting one as though a placed and routed design
achieved it is a claim nobody checked.

**Say which frame a "bound" is in.** *(Free.)* Lower bound on the block,
upper bound on the machine. Bare, the two read as a contradiction.

**Never subtract two accountings taken on different rules.** *(Free.)*
Hierarchical and flattened reports are both correct and are not comparable;
neither is a standalone run against an assembly's total.

**Parse the routed report file rather than opening the checkpoint.** *(Free.)*
It is faster and it does not consume a tool session.

The corresponding rule for *where* numbers live: framework pages carry none, and
measured figures belong with the project that produced them. See
[arch/README](../README.md#no-numbers-here). Device facts — how many SLRs a part
has, how wide a block-RAM port is — are properties of the silicon rather than
measurements of an accelerator, and those do appear here, on
[device-facts](device-facts.md).

The mechanism behind all of this — the scripts, their arguments, the twelve ways
an out-of-context measurement produces a plausible wrong number — is
[workflow/measure](../../workflow/measure.md).
