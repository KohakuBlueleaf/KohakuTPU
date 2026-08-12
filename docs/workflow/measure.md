---
title: Measuring out of context
summary: How to find out whether a block makes its frequency without building the whole device, and the ten ways the measurement lies if you set it up wrong.
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

It costs seconds to minutes. A full implementation costs hours. That ratio is
the reason OOC measurement is the framework's central practice: **you find out
that a datapath cannot make its target before you have built anything around
it**, and you find out again after every change to it.

It answers nothing about placement, routing, congestion, SLR crossings or
interaction with the rest of the device. A module that passes OOC may still miss
by a wide margin in a full ship. The relationship is one-directional:

| OOC result | what it means for the real device |
|---|---|
| misses the target | the real device **will** miss it. Fix the RTL. |
| makes the target | the real device **may** miss it. Nothing is proved. |

Use it to disqualify, not to sign off.

## The two scripts

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
| `OOC_TAG` | suffix on the output directory | none |

`scripts/tcl/ooc_pump.tcl` — a **ratio-locked** pair, `clk1x` and `clk2x`, as one
MMCM produces them. `OOC_P1` is the 1x period in ns; the 2x period is derived.
`OOC_IMPL=1` additionally places and routes.

Both write to `.plan/ooc/<top><tag>/`:

    ooc.xdc              the constraints that were actually applied
    <top>.dcp            checkpoint, so a re-read costs no re-synthesis
    util.rpt             utilisation
    timing_summary.rpt   the summary
    timing_all.rpt       200 worst paths, full clock expanded

and print `@@@`-prefixed lines to stdout: the twelve worst paths with slack,
logic levels, and start and end pin. **Read the paths, not just the number.**

`scripts/tcl/ooc_class.tcl` provides `ooc_classify`, which reports an Fmax for
**every** clock in the design, queried one clock at a time.
`scripts/tcl/ooc_reclass.tcl` re-runs that classification over checkpoints
already on disk (`OOC_DCPS`), so revisiting a result costs a checkpoint open
rather than a re-synthesis.

Script paths are where these live today, not a stable interface; see
[Framework machinery versus project configuration](build.md#framework-machinery-versus-project-configuration).

### The synth-check flow

`scripts/synth_check.tcl`, invoked through `tests/run_synth_check.ps1`, is an
older argument-driven variant of the same idea, and it is the one wired into the
test suite:

    .\tests\run_synth_check.ps1                    # every registered target
    .\tests\run_synth_check.ps1 -Only <top>        # one of them
    .\tests\run_synth_check.ps1 -Freq 400          # a different target frequency
    .\tests\run_synth_check.ps1 -Generics "DEPTH:512+ACC_MW:14"

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

### 2. A bare `create_clock` leaves port paths unreported

The opposite failure. A clock created without any `set_input_delay` /
`set_output_delay` leaves every path that begins or ends at a port
**unconstrained and therefore unreported**. The tool reports only the
register-to-register paths, which are the fast ones, and the block measures far
faster than it can actually be driven.

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
`+0.180 ns` at 300 MHz does **not** mean the block runs at 316 MHz. It means the
optimiser stopped as soon as it had 300, and the true ceiling is somewhere at or
above that — unknown, and usually well above.

- **A failing run gives you a real ceiling.** The tool tried as hard as it could
  and still missed; the achieved period is what the logic actually costs.
- **A met run gives you a lower bound.** Nothing more.

To measure a ceiling, tighten the period until the run fails, and quote the
failing run. To check a target, run at the target and read the verdict.

**Always say which one you have.** "324 MHz (failing run at 3.0 ns)" and
"at least 300 MHz (met, not pushed)" are different claims. Writing the second as
if it were the first is how a design gets budgeted at a frequency nobody
measured.

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

### 7. A parameter override that names nothing is not an error

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
of an insensitive parameter.

### 8. Each override needs its own flag

```tcl
lappend cmd -generic $generics       # WRONG
foreach g $generics { lappend cmd -generic $g }   # right
```

Appending a list as one argument flattens to `-generic A=1 B=2`. Vivado takes
`A` and silently drops `B`. With a single override it happens to work, which is
why this survives until the first two-parameter sweep.

### 9. A per-clock sweep reports only the clocks it happened to see

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

### 10. Zero timing paths is a failure, not a pass

If the clock reached no sequential element — wrong port, purely combinational
top, everything optimised away — `report_timing` returns nothing and a naive
script exits 0. "no paths" lands in the column the eye reads as a result.

Treat an empty path list as a hard failure and print which clock was created.

## Composition is not additive

A submodule synthesised alone optimises differently from the same submodule
inside a parent: constant propagation, boundary optimisation and retiming all
cross the boundary in the parent and cannot in the child.

So "what does one router cost inside the mesh" cannot be answered by subtracting
standalone runs. It has to be read out of a hierarchical utilisation report of
the parent (`report_utilization -hierarchical`), which both scripts write.

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

These tops belong in a synthesis-only directory. They are not part of any
shipped design and they never appear in a bitstream.

## Placement changes the answer

`OOC_IMPL=1` in `ooc_pump.tcl` runs `opt_design`, `place_design`,
`phys_opt_design`, `route_design` on the isolated block. It is much slower than
synthesis, and it is the only way to see routing pressure.

This matters most for a claim that rests on routing rather than logic — a
double-pumped datapath, for example, trades area for a second clock domain, and
synthesis cannot see whether the 2x domain routes. Synthesis-only numbers are
evidence about logic depth; only a routed run is evidence about routing.

## Device choice is part of the measurement

Every OOC number is against one speed grade. A `-2L` low-voltage part is slower
than a `-2`, and the difference is not a rounding error. **Measure against the
part you will ship on**, and record the part beside every number. A sweep taken
on a faster grade than the board carries is optimistic by an amount nobody can
reconstruct later.

## Where results go

Raw sweeps, intermediate numbers and dead ends belong in the project's working
directory as they are produced. Framework docs carry the practice; the numbers
belong to the project that measured them ([docs/README.md](../README.md),
"Numbers").

A number that only exists in a terminal scrollback is lost. Write it down when it
appears, with the part, the period, the tool version, and whether the run met or
failed.

## Open questions

- The measurement scripts hardcode the part and the repository root. Both are
  project configuration; see the note on a project manifest in
  [build.md](build.md).
- `ooc_check.tcl` constrains I/O delay against the *first* clock only. For a
  module whose ports genuinely belong to a second domain, that is wrong, and
  nothing currently detects it.
