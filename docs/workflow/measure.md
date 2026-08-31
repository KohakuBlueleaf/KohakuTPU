---
title: Measuring out of context
summary: How to find out whether a block makes its frequency without building the whole device, what a figure produced this way is and is not, and the ways the measurement lies if it is set up wrong.
tags:
  - workflow
  - timing
  - measurement
---

# Measuring out of context

Out-of-context (OOC) synthesis takes one module, synthesises it alone against a
part, constrains it with a clock you invent, and reports the worst path. It
answers one question:

> Is this block's logic depth compatible with the frequency I want?

It costs seconds to minutes; a full implementation costs hours. That ratio is
why OOC measurement is the framework's central practice: **you find out that a
datapath cannot make its target before you have built anything around it**, and
you find out again after every change.

It answers nothing about placement, routing, congestion, die crossings or
interaction with the rest of the device. Use it to disqualify, not to sign off.

| OOC result | what it means for the real device |
|---|---|
| misses the target | the real device **will** miss it. Fix the RTL. |
| makes the target | the real device **may** miss it. Nothing is proved. |

## Every figure carries its provenance

This is the rule the rest of the tree points at. **A number without a named
instrument is not a number.** Wherever a figure is written down — a report, a
commit message, a page in this tree, a message to a colleague — it names five
things:

| | example | why it changes the answer |
|---|---|---|
| **part** | `xcvu13p-fhgb2104-2L-e` | speed grade is not a rounding error; see below |
| **tool and version** | Vivado 2024.2 | inference, packing and directives move between releases |
| **context** | out-of-context, or in-context | a block alone optimises differently from a block in a parent |
| **stage** | **synthesis**, placed, or **routed** | synthesis estimates routing, and estimates it optimistically |
| **what produced it** | `scripts/tcl/ooc_syscore.tcl`, top `rv64_core`, period 3.333 ns | the target period is part of the measurement, not context for it |

Two of those are routinely dropped and both matter more than they look.

**Stage.** Synthesis slack is optimistic. One module in the reference instance
lost **0.740 ns** of worst slack between synthesis and routing, same design and
same constraints. **No Fmax produced by out-of-context synthesis is a
closed-timing figure**, and presenting one as though a placed and routed design
achieved it is a claim nobody checked. Say
"312 MHz, OOC synthesis, `xcvu13p-fhgb2104-2L-e`, Vivado 2024.2, top `foo`,
failing run at 3.2 ns" or say nothing.

**Target period.** A resource figure is not independent of the frequency it was
asked for — the tool spends area to meet a constraint. A LUT count quoted without
the period it was synthesised at is under-specified.
`scripts/tcl/ooc_rv_pe.tcl` takes the period as an argument for exactly this
reason, and its header says so.

Anything not measured is marked **PROJECTED** or **ESTIMATE**, explicitly, in the
same sentence as the figure.

[arch/physical/measurement.md](../arch/physical/measurement.md) states this
convention as architecture; this page is the mechanism behind it.

### Two directions of "bound", and they are not the same claim

The word "bound" appears in both directions in these docs and the frames are
different. Both statements are true:

- **Against the block's own ceiling, a run that MET its target is a *lower*
  bound.** The optimiser stopped when the constraint was satisfied; the true
  ceiling is at or above the target and is unknown. See [trap 4](#4-a-met-run-is-a-lower-bound-not-a-measurement).
- **Against the assembled, routed device, an out-of-context figure is an *upper*
  bound.** Composition costs frequency, routing costs more, and the machine will
  not exceed what the block managed alone. This is the sense
  [arch/physical/measurement.md](../arch/physical/measurement.md) uses.

State which one you mean. A figure that is a lower bound on the block and an
upper bound on the machine is not a contradiction, but writing it down without
saying which produces one.

## Device choice is part of the measurement

Every OOC number is against one speed grade. A `-2L` low-voltage part is slower
than a `-2`, and the difference is not small. **Measure against the part you will
ship on**, and record the part beside every number. A sweep taken on a faster
grade than the board carries is optimistic by an amount nobody can reconstruct
later — and the part number appears in enough separate places in a build flow
that the two drifting apart is an ordinary event rather than a careless one. Grep
for it and make them agree.

## The scripts

Both are Tcl driven by environment variables, run under `vivado -mode batch`.

`scripts/tcl/ooc_check.tcl` — one clock domain, or several unrelated ones.

| variable | meaning | default |
|---|---|---|
| `OOC_TOP` | top module name | required |
| `OOC_SRCS` | space-separated source paths, repo-relative | required |
| `OOC_CLK` | clock **port** names, space separated | `clk` |
| `OOC_PERIOD` | target period in ns | `3.125` |
| `OOC_IO` | input/output delay in ns | 30% of the period |
| `OOC_GEN` | `NAME=V` parameter overrides, space separated | none |
| `OOC_ASYNC` | put every clock in its own asynchronous group | off |
| `OOC_PLACE` | place before reporting | off |
| `OOC_TAG` | suffix on the output directory | none |

`scripts/tcl/ooc_pump.tcl` — a **ratio-locked** pair, `clk1x` and `clk2x`, as one
MMCM produces them. `OOC_P1` is the 1x period in ns; the 2x period is derived.
`OOC_IMPL=1` additionally places and routes.

Both write to `build/ooc/<top><tag>/`:

    ooc.xdc              the constraints that were actually applied
    <top>.dcp            checkpoint, so a re-read costs no re-synthesis
    util.rpt             utilisation
    timing_summary.rpt   the summary
    timing_all.rpt       200 worst paths, full clock expanded

and print `@@@`-prefixed lines to stdout: the worst paths with slack, logic
levels, and start and end pin. **Read the paths, not just the number.**

`scripts/tcl/ooc_class.tcl` is the shared reporting library the per-target
scripts source. It provides `ooc_classify` (an Fmax for **every** clock, queried
one clock at a time), `ooc_record` (one synthesis, every number, as machine
-readable `@@@REC` / `@@@FMAX` / `@@@HIER` lines), `ooc_cones` (grouped failing
paths — below), `ooc_resets` and `ooc_ctrlsets` (what a reset actually costs),
and `ooc_lut_census` (which *signal* the LUTs of a flat module belong to).
`scripts/tcl/ooc_reclass.tcl` re-runs the classification over checkpoints already
on disk (`OOC_DCPS`), so revisiting a result costs a checkpoint open rather than
a re-synthesis.

The per-target scripts — `ooc_syscore.tcl`, `ooc_sysnode.tcl`,
`ooc_sysnode_rv64.tcl`, `ooc_rv_pe.tcl`, `ooc_simt_pe.tcl`, `ooc_station.tcl` and
the rest — each state in their header what that run measures, on what part, and
what the figure is comparable to. **Quote the header when citing the output.**
Several of them also state what the run is *not*: `ooc_station.tcl` says
"SYNTH ONLY — no opt, no place, no route" in its first line, which is the
provenance rule enforced at the source.

Script paths are where these live today, not a stable interface; see
[Framework machinery versus project configuration](build.md#framework-machinery-versus-project-configuration).

### The synth-check flow

`scripts/synth_check.tcl`, invoked through `scripts/tcl/ooc_check.tcl`, is an
older argument-driven variant of the same idea:

    vivado -mode batch -source scripts/tcl/ooc_check.tcl -tclargs <top> ...

> **Nothing runs synthesis automatically.** `check.py` is simulation, linting
> and the doc gates; no tier of it synthesises anything, because the cheapest
> synthesis is ninety seconds and the suite's contract is seconds.
> Out-of-context measurement is something you run deliberately before an
> implementation run, which is exactly what
> [README.md](README.md#when-to-skip-a-stage-and-when-not-to) says not to skip.

It takes `<top> <period_ns> <part> <generics> <file>...` and prints a `RESULT`
line, a `VERDICT` line, the ten worst paths and a utilisation extract per target,
keeping each run's full Vivado log beside the results — a synthesis failure is
usually explained halfway up the log rather than at the end.

Parameter overrides are `NAME:VALUE` joined by `+`. Not `=`, because the tool's
batch wrappers split on it, and not `,`, because PowerShell splits string
arguments on it. Both are rebuilt in Tcl, where nothing is splitting anything.
See [tooling-traps.md](tooling-traps.md).

The script carries a table of modules and their source lists. That table is
project configuration rather than framework machinery — see
[build.md](build.md).

## Reading the result

The number that matters is not the reported Fmax. It is the **worst path's start
and end pin**, plus its logic-level count.

    @@@  -0.412 ns  lvl 14   u_alu/stage2_reg[3]/C -> u_alu/acc_reg[17]/D

- **Start and end both inside your datapath** — a real result. Pipeline it, or
  restructure the logic between them.
- **Start at a port** — an artefact of the measurement boundary. In OOC mode a
  port-to-register path is timed against `set_input_delay` plus the whole
  period; that is a constraint you invented, not a circuit property.
- **End at a reset pin, an enable, or a fanout of one control signal** — you are
  measuring control distribution, not compute. See
  [timing-closure.md](timing-closure.md).
- **High logic levels, low delay per level** — logic-bound; add a pipeline stage.
- **Low logic levels, high delay** — routing or fanout bound; pipelining will not
  help much and floorplanning might.

Logic levels are the diagnostic rather than the slack; the budget — ≤10
comfortable, 7–9 a mature upper limit, ≥11 the thing to fix — is in
[timing-closure.md](timing-closure.md#2-logic-levels-are-the-diagnostic-not-slack).

### Report the memory columns, always

Print block RAM, ultra RAM and DSP counts beside the LUT count on **every** run,
even when memory is nowhere near the budget. LUT is usually the objective and the
memory columns are usually slack, which is exactly why nobody looks at them — and
a module that simulates perfectly can still fall out of block RAM and come back
as thousands of LUTs, with no warning from any tool but synthesis.
`scripts/tcl/ooc_syscore.tcl` prints them for this reason and says so in its
header. `ooc_record` in `ooc_class.tcl` emits every column as one `@@@REC` line so
a sweep is machine-readable rather than eyeballed.

### Grouped failing-path reporting

A run that fails does not have *a* worst path; it has a plateau. Reporting the
single worst one repeatedly points at whichever endpoint happens to hold it, and
that is often not the structure holding most of the failures.

So the measurement scripts do not report a worst path. They report **every path
with negative slack, grouped**: start and end pin collapsed to their base names —
bit indices and the tool's `_i_N` / `__N` suffixes stripped — then counted, and
sorted worst-group-first. What comes out is a short list of distinct problems
instead of a long list of one problem's aliases.

    @@@FAILN 214
    @@@GROUP  128 paths  worst  -0.412  lvl  14  u_ctrl/addr_reg -> u_l1/tag_reg
    @@@GROUP   61 paths  worst  -0.208  lvl   9  u_ctrl/addr_reg -> u_ctrl/hit_reg
    @@@GROUP   25 paths  worst  -0.061  lvl  12  u_alu/stage2_reg -> u_alu/acc_reg

`scripts/tcl/ooc_sysnode.tcl` carries the compact implementation and
`ooc_cones` in `scripts/tcl/ooc_class.tcl` the fuller one, which additionally
dumps a representative path per group cell by cell and lists the design's
highest-fanout nets.

Three query details decide whether the counts mean anything:

- **`-nworst 1`** so the query returns one path per endpoint. Without it the
  counts describe the query, not the design.
- **`-slack_lesser_than 0` with a large `-max_paths`** so it is every failing
  path rather than a sample of the worst region.
- **Group on the register, not the pin** (`file dirname` the pin), so paths
  ending at `D` and at `CE` of the same register group together.

**What to do with the grouping is [timing-closure.md](timing-closure.md#1-do-not-chase-a-single-failing-route).**
That page owns the method: one group, one root, and fixing the top group rather
than the top path. This section owns only the mechanism and the output format.

### Hierarchy: `none` attributes, `rebuilt` ships

`report_utilization -hierarchical` on a netlist synthesised with the default
`-flatten_hierarchy rebuilt` re-parents leaves, so it will confidently attribute
LUTs to the wrong instance. To ask *where the area went*, re-synthesise with
`-flatten_hierarchy none`, which keeps boundaries intact.

But `none` is **not** the number to quote as the design's area — the shipped
design synthesises at `rebuilt`, and boundary optimisation is a real saving that
`none` forbids. Two runs, two purposes: `none` to attribute, `rebuilt` to
report. `scripts/tcl/ooc_syscore.tcl` takes a `HIER` argument that switches
between them and says this in a comment at the call site.

For a flat module whose LUTs are one undifferentiated lump, `ooc_lut_census`
buckets them by the signal name synthesis derived each LUT from, which turns the
lump back into a ranked list of signals to aim at.

## The traps

Every one of these produces a **plausible number**, not a crash. That is what
makes them expensive: nothing tells you the measurement is wrong, and the wrong
number is indistinguishable from a right one until something downstream
contradicts it.

### 1. A clock that matches no port still reports a worst path

`create_clock ... [get_ports aclk]` on a module whose port is called `clk`
creates nothing. Synthesis proceeds. `report_timing` returns a path. A number
comes out. The design is entirely unconstrained, and the number is meaningless.

The tell is in the timing summary — WNS reads `NA` with thousands of
unconstrained endpoints — but nobody reads the summary when a headline number
already printed.

**The script must abort.** `ooc_check.tcl` counts clocks after synthesis and
errors if fewer were created than requested:

```tcl
set made [get_clocks -quiet]
if {[llength $made] < [llength $clks]} {
    puts "@@@ FAIL only [llength $made] clock(s) created from '$clks'"
    puts "@@@ FAIL ports are: [get_property NAME [get_ports -quiet *]]"
    error "clock constraint did not apply -- set OOC_CLK to the real port names"
}
```

It prints the port list, because the fix is always "you named the wrong port".
Never make this check a warning. A warning scrolls past.

The same guard belongs on a script that reads a checkpoint rather than
synthesising one: **a bare synthesis checkpoint carries no clocks**, so every
timing query against it returns empty and a naive script reports zero failing
paths on a design it never analysed.

### 2. A bare `create_clock` leaves port paths unreported

The opposite failure. A clock created without any `set_input_delay` /
`set_output_delay` leaves every path that begins or ends at a port
**unconstrained and therefore unreported**. The tool reports only the
register-to-register paths, which are the fast ones, and the block measures far
faster than it can actually be driven — a memory port measured this way came out
well over 100 MHz above what the same block managed inside a mesh.

Always constrain the boundary. Both scripts set input and output delay to 30% of
the period against the primary clock. The exact fraction is a convention; having
one is not.

### 3. A false path that misses its target

    set_false_path -from [get_ports {*rst* *aresetn*}]

does not match a port named `resetn`. Neither `*rst*` nor `*aresetn*` contains
it. The reset then fans out to every register in the block and is timed as
combinational logic, and it wins — reset fanout is the widest net in most
designs, so it becomes the reported critical path and the block appears to fail
by a large margin for a reason that does not exist.

Use `{*rst* *reset*}`, which covers `rst`, `rst_n`, `reset`, `resetn`,
`aresetn`, `s_aresetn`. Better: after applying it, check that the worst path is
not a reset path anyway. The pattern is a guess; the report is evidence.

### 4. A MET run is a lower bound, not a measurement

This is the most misread result in the flow.

Vivado stops optimising once the constraint is satisfied. A run that reports
`+0.180 ns` at a 300 MHz target does **not** mean the block runs at 316 MHz. It
means the optimiser stopped as soon as it had 300, and the true ceiling is
somewhere at or above that — unknown, and usually well above.

- **A failing run gives you a real ceiling.** The tool tried as hard as it could
  and still missed; the achieved period is what the logic actually costs.
- **A met run gives you a lower bound.** Nothing more.

To measure a ceiling, tighten the period until the run fails, and quote the
failing run. To check a target, run at the target and read the verdict.

**Always say which one you have.** "324 MHz (failing run at 3.0 ns)" and
"at least 300 MHz (met, not pushed)" are different claims, and neither is a
closed-timing figure.

### 5. Ratio-locked clocks need a multicycle path

`clk1x` and `clk2x` from one MMCM are phase aligned and harmonic. Vivado will
time crossings between them — which is correct, and is the entire reason a
double-pumped block is safe. But the default analysis picks the tightest
launch/capture edge pair, and for phase-aligned harmonic clocks that pair is the
**same edge**: the requirement is 0.000 ns.

Every 1x → 2x path then fails by its whole delay, and the block looks
catastrophically broken.

```tcl
set_multicycle_path -setup 2 -from [get_clocks clk1x] -to [get_clocks clk2x]
set_multicycle_path -hold  1 -from [get_clocks clk1x] -to [get_clocks clk2x]
```

Setup 2 gives the path the full 1x period it actually has; hold 1 moves the hold
check back with it. Omit the hold line and you swap a bogus setup failure for a
bogus hold failure.

### 6. Clock periods must be exactly harmonic in picoseconds

Vivado stores periods at picosecond resolution. `OOC_P1=3.333` rounds to
3.333 ns and its half to 1.667 ns — and 1.667 × 2 ≠ 3.333. The two clocks are no
longer harmonic, so the tool synthesises a beat pattern between them and finds a
tight edge relationship that does not exist in silicon. One observed result was a
1.168 ns requirement on a 1.667 ns clock.

`ooc_pump.tcl` refuses the input rather than rounding it:

```tcl
set ps1 [expr {round($p1 * 1000)}]
if {$ps1 % 2} { error "OOC_P1 must be an even number of ps, got $p1 ns" }
```

The same applies to any constrained ratio, not just 2:1. Work in integer
picoseconds and check divisibility.

### 7. Genuinely asynchronous clocks must be grouped, or a correct crossing fails

The inverse of trap 5. Two clocks that are asynchronous by construction — the
two sides of a clock-domain-crossing FIFO, for instance — are timed
*synchronously* by default, because the tool has no way to know they are
unrelated. The FIFO's gray-coded pointers then fail setup **and** hold, and a
correct crossing is reported as broken by a wide margin.

`OOC_ASYNC=1` puts every clock in its own group:

```tcl
set_clock_groups -asynchronous -group [get_clocks clkA] -group [get_clocks clkB]
```

Traps 5 and 7 are opposite errors with the same symptom, so decide which one
applies **before** reading the number: are these two clocks ratio-locked from one
generator, or genuinely independent? Grouping a ratio-locked pair hides a real
failure; not grouping an asynchronous pair invents one.

### 8. A parameter override that names nothing is not an error

`-generic FOO=8` where the top declares no `FOO` is silently ignored. Vivado
synthesises the default and reports a number that looks exactly like a
measurement.

This has three shapes, all seen:

- A misspelt name. A five-point sweep returns five identical results.
- A name declared only in a **submodule**. `-generic` binds to the top only, so
  it is ignored as silently as a typo. The parameter must be threaded up to the
  top before it can be swept.
- A source snapshot that predates the parameter. An A/B whose two arms agree to
  the digit.

`scripts/synth_check.tcl` checks the top's own parameter list **textually, before
`synth_design`**, and refuses to run:

```
SYNTH FAILED: -generic ACC_MW=14 names a parameter top module mx_acu_fp does not declare.
```

Textual rather than elaborated, because by the time a netlist exists the wrong
number has already been produced.

The general rule: **a sweep whose points do not differ has not measured
anything.** Two arms that agree to the digit are evidence of a broken sweep, not
of an insensitive parameter. The stronger form of the same rule: a knob can be
accepted, printed in the run's tag and its report line, and still never reach
`synth_design` — so check that the arm you are comparing actually changed the
netlist, not just the label.

### 9. Each override needs its own flag

```tcl
lappend cmd -generic $generics       # WRONG
foreach g $generics { lappend cmd -generic $g }   # right
```

Appending a list as one argument flattens to `-generic A=1 B=2`. Vivado takes
`A` and silently drops `B`. With a single override it happens to work, which is
why this survives until the first two-parameter sweep.

### 10. A per-clock sweep reports only the clocks it happened to see

`get_timing_paths -max_paths N` returns the N worst paths overall. If one domain
is much tighter than another, every returned path belongs to the tight domain and
the other **silently vanishes from the report** — not as zero, as absent.

`ooc_classify` queries per clock:

```tcl
foreach c [get_clocks] {
    set ps [get_timing_paths -to $c -max_paths $npaths -nworst $npaths -setup]
    ...
}
```

and prints `no paths` explicitly when a clock reached nothing, because a clock
that reached nothing is a constraint bug, not a fast domain.

### 11. Zero timing paths is a failure, not a pass

If the clock reached no sequential element — wrong port, purely combinational
top, everything optimised away — `report_timing` returns nothing and a naive
script exits 0. "no paths" lands in the column the eye reads as a result.

Treat an empty path list as a hard failure and print which clock was created.

### 12. An unplaced clock net is estimated as ordinary fabric routing

Synthesis has not placed anything, so it estimates a clock net's delay as if it
were signal routing. For a clock arriving on a port this is harmless — the net
has no estimate at all and the analysis starts at the port. For a clock that
passes through a **global buffer instantiated inside the design**, it is not: the
buffer's output is a net with enormous fanout, and the estimate for it is
enormous too, far beyond anything the real clock tree costs.

The consequence is a measurement that says a clock-gated arm is nanoseconds
worse than an ungated one, for a buffer whose cell delay is tens of picoseconds.
The number is a placement artefact and there is nothing wrong with the design.

**Any design that instantiates a global buffer must be placed before its timing
numbers mean anything.** `OOC_PLACE=1` in `ooc_check.tcl` runs `opt_design` and
`place_design` and reports again; `OOC_IMPL=1` in `ooc_pump.tcl` goes on to route.
Both are much slower than synthesis and both are the only way to see routing
pressure — which matters most for a claim that rests on routing rather than logic.
A double-pumped datapath, for example, trades area for a second clock domain, and
synthesis cannot see whether the 2x domain routes at all.

## Composition is not additive

A submodule synthesised alone optimises differently from the same submodule
inside a parent: constant propagation, boundary optimisation and retiming all
cross the boundary in the parent and cannot in the child.

So "what does one router cost inside the mesh" cannot be answered by subtracting
standalone runs. It has to be read out of a hierarchical utilisation report of
the parent, with the `none`/`rebuilt` caveat above.

The same trap has a subtler form when the thing being subtracted is not a leaf.
Subtracting a processor's standalone figure from a node's total removes
everything the processor's source list dragged in with it — and the parts that
are properties of the *node* rather than of the processor do not disappear when
the processor is swapped. **When the question is "what does swapping this
component cost", synthesise both assemblies and diff them.** Do not subtract.
`scripts/tcl/ooc_sysnode.tcl` and `ooc_sysnode_rv64.tcl` exist as a matched pair
for this reason, and both say so in their headers.

The corollary for frequency: **measuring every leaf module tells you nothing
about the assembly.** Build a synthesis-only top that instantiates the real
composition — two routers wired together rather than one router; a compute unit
attached to its network port rather than bare — and measure that. A one-module
measurement cannot see the link between modules, and the link between modules is
frequently where the critical path lives.

## Measuring a pair or a tile

Three shapes are worth having as measurement tops, and they are complementary:

- **The unit alone** — is the datapath's logic depth sane?
- **The unit at its framework port** — what does attaching to the network cost?
  Measure a null unit at the same port to separate the two.
- **A tile at two different ratios** — one router with five endpoints, and four
  routers with twelve. The router cost and the endpoint cost are then
  *solvable* from two equations rather than assumed from one.

The third shape generalises into a rule about per-unit costs: **price a marginal
unit from two adjacent measurements, never by dividing a total.** A tier's total
includes fixed overhead that belongs to the tier and not to the units in it, so
dividing charges each unit for machinery it did not add. Two configurations
differing by one unit give the marginal cost directly.

These tops belong in a synthesis-only directory. They are not part of any
shipped design and they never appear in a bitstream.

## Where results go

Raw sweeps, intermediate numbers and dead ends belong in the project's working
directory as they are produced. Framework docs carry the practice; the numbers
belong to the project that measured them ([docs/README.md](../README.md),
"Numbers").

A number that only exists in a terminal scrollback is lost. Write it down when it
appears, with everything in [Every figure carries its
provenance](#every-figure-carries-its-provenance). This is cheaper to do at the
moment the run finishes than at any later time, and the alternative is not
remembering it approximately — it is re-measuring it.

## Open questions

- The measurement scripts hardcode the part and the repository root. Both are
  project configuration; see the note on a project manifest in
  [build.md](build.md).
- `ooc_check.tcl` constrains I/O delay against the *first* clock only. For a
  module whose ports genuinely belong to a second domain, that is wrong, and
  nothing currently detects it.
- Nothing checks that a script's stated provenance matches what it did. A header
  saying "SYNTH ONLY" and a body that places are not currently reconciled by any
  gate.
