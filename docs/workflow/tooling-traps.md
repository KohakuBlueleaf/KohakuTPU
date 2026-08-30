---
title: Tooling traps
summary: Tool and language behaviours that cost real time — Vivado, XPM, the simulators and Verilog itself — what each one does, why it does it, and how to avoid it.
tags:
  - workflow
  - vivado
  - xpm
  - simulation
---

# Tooling traps

This page is the tree's home for **durable facts about how the tools behave**:
things that are true of Vivado, of the vendor macro libraries, of the simulators
and of Verilog, that a competent engineer would not guess and that cost days to
discover. If a design decision elsewhere in these docs looks arbitrary, the
reason is often on this page.

None of them is exotic; every project that uses this framework will meet most of
them. They share a shape:

> **The tool does not fail. It does something else, quietly, and reports
> success.**

That is why they are worth a page. A crash is cheap — you read the message and
fix it. A silent substitution is expensive, because the wrong result is
indistinguishable from a right one until something much further downstream
contradicts it, and by then the cause is days behind you.

They are grouped by which tool produces them.

---

## Constraints

### XDC is parsed in a restricted mode, and control flow is silently skipped

Vivado does **not** evaluate an XDC file as ordinary Tcl. `proc`, `foreach` and
`if` are rejected with

    CRITICAL WARNING: [Vivado 12-1008] Command 'proc' is not supported in the
    xdc constraint file

A critical warning is **not an error**. The file keeps being read, the block is
skipped, and every constraint inside it never applies. Nothing downstream says
so.

The observed cost: a `set_clock_groups -asynchronous` wrapped in a `foreach`
over clock names never applied, so `route_design` spent hours trying to time
crossings that are asynchronous by construction, on a design that would
otherwise have routed.

**Write constraints flat.** Command substitution *is* allowed, which is enough
to express most of what a loop would have done:

```tcl
set_clock_groups -asynchronous \
    -group [get_clocks -of_objects [get_pins -hier -filter {NAME =~ *clk_wiz_ctrl*clk_out1}]] \
    -group [get_clocks -of_objects [get_pins -hier -filter {NAME =~ *clk_wiz_mesh*clk_out1}]] \
    -group [get_clocks -of_objects [get_pins -hier -filter {NAME =~ *ddr4_0*c0_ddr4_ui_clk}]]
```

If a constraint genuinely needs control flow, it is not a constraint file. Put it
in a Tcl hook that runs at a build step, where full Tcl is available, and have
that hook `puts` what it applied.

**A skipped `if` that was supplying a DEFAULT is worse than a skipped
constraint**, because the failure moves to the next line. `if {![info exists
::ooc_period]} { set ::ooc_period 2.500 }` at the top of an OOC constraint file
threw its critical warning on every run and never set anything; the
`create_clock -period $::ooc_period` below it then either used a value the
caller happened to set or failed on an unset variable. Every resource figure
that file produced was constrained at whatever the caller last used. Delete the
guard and require the caller to set it: an unset variable failing loudly on the
next line is the behaviour the guard was pretending to provide.

**Always check that a constraint applied**, rather than that the file was read.
`get_clocks`, `get_pins` and `get_cells` return empty lists for patterns that
match nothing, and every constraint command accepts an empty list without
complaint. See [measure.md](measure.md) for the same failure in the measurement
path and the abort that catches it.

### A pattern that matches nothing constrains nothing

The general form of the above. `set_false_path -from [get_ports {*rst*}]` on a
port named `resetn` matches nothing, applies nothing, and warns about nothing you
will notice. The false path is silently absent and the reset fanout becomes your
critical path.

Verify by consequence: after applying, ask whether the object you meant to
exclude still appears in the report.

---

## Command-line argument handling

### `-d NAME=VALUE` loses the value

Vivado ships its tools as `.bat` wrappers on Windows (`xvlog.bat`, `xelab.bat`,
`vivado.bat`). Those wrappers split arguments on `=`. A define passed as

    xvlog.bat -sv -d MX_MODEL=0 top.v

arrives as `-d MX_MODEL` followed by a stray `0` that the tool tries to open as a
source file. The same splitting defeats `xelab -generic_top`.

There are three fixes, in descending order of preference:

**1. Put the option in a command file.** `.f` files are read by the tool itself,
not by the batch wrapper, so nothing splits anything:

```python
opts = ["-d " + d for d in defines]
(work / "xvlog.f").write_text("\n".join(opts + files) + "\n")
run(["xvlog.bat", "-sv", "-work", "w", "-f", "xvlog.f"])
```

This is the only fix that keeps the define global to the invocation, which is
what a define is supposed to be.

**2. Encode the value in the option name.** A value-less define — `-d MX_PUMP_A`
rather than `-d MX_PUMP=2` — carries no `=` and survives. The consumer selects
with `` `ifdef `` / `` `elsif `` inside the file that uses it.

**3. Select the variant with a source file listed first.** A one-line file
containing `` `define ACC_MW 14 ``, compiled ahead of the design. This is the
most fragile of the three; see the next trap for why.

A related splitting bug is on the *invoking* side: `powershell -File` splits a
string argument on `,`. A generic list therefore cannot use `,` **or** `=`.
Joining `NAME:VALUE` pairs with `+` and rebuilding them in Tcl avoids both.

### `` `define `` does not cross files under `-sv`

Under `-sv`, each source file is its own compilation unit. A `` `define `` in one
file is **not** visible in the next, however they are ordered on the command
line.

This breaks fix 3 above, and it breaks it in the worst possible way when the
consumer guards its default:

```verilog
`ifndef MX_MODEL
`define MX_MODEL 1
`endif
```

If the define did not cross, the bench compiles at 1 and reports nothing unusual.
You believe you ran the DSP-primitive variant; you ran the behavioural one twice.

Two disciplines make this safe:

- Prefer `-d` in a command file, which is genuinely global.
- **Have the bench print the value it compiled with**, in its banner, every run.
  A variant selection you cannot see in the log is a variant selection you cannot
  trust.

If a define really must be file-local, keep the `` `ifdef `` in the same file as
the code it selects, so there is no cross-file dependency to fail.

---

## XPM macros

### `USE_ADV_FEATURES` is a hex string, not a bit vector

The parameter is documented as a **string** of hex digits — `"0707"`,
`"1000"` — one bit per optional flag. A sized binary literal is a different type
entirely; it parses as garbage and elaboration fails, or worse, resolves to
something that is neither what you wrote nor the default.

Write the string. Check the macro's own documentation for which digit is which
flag, because the bit order is not the order the port list is in.

### Advanced features off means the flags you did not enable are tied off

`prog_full`, `prog_empty`, `overflow`, `underflow`, `wr_data_count` and
`rd_data_count` are **optional**. With `USE_ADV_FEATURES` at zero, XPM ties them
off — and it does so regardless of whether you also passed `PROG_FULL_THRESH`.

The consequence is a port called `prog_full` that is permanently low next to a
parameter called `PROG_FULL_THRESH` that is honoured by nothing. Any wrapper
exposing an "almost full" derived from it is exposing plain `full` under a name
that promises margin.

If a design needs real headroom, **count occupancy itself**. Do not rely on a
threshold flag you have not explicitly enabled, and do not name a signal
`almost_full` when it is not.

### `xpm_fifo_async` with advanced features off discards a write to a full FIFO

This is the one that loses data.

With overflow reporting disabled, writing to a full `xpm_fifo_async` does not
assert an error, does not stall, and does not corrupt the FIFO. It **drops the
write**. Silently. The beat is simply gone.

Any place that ties a ready signal high because "the FIFO is deep enough" is a
place where data disappears under load and nowhere else. It presents as a burst
that is short by a random amount, or a response that never arrives, and it is
load-dependent, so a directed test at low rate will never see it.

**Drive backpressure from the FIFO's own full flag:**

```verilog
// xpm_fifo_async with USE_ADV_FEATURES off does not flag an overflow, it
// DISCARDS the write: a tied-high ready loses data silently.
assign m_bready = !bq_full;
```

Either enable the overflow flag and check it in simulation, or never tie a ready
high in front of one.

### Reset busy is held for several cycles and must be folded into the flags

XPM holds `wr_rst_busy` and `rd_rst_busy` asserted for several cycles after
reset. A writer that ignores them loses the first beats — again presenting as a
burst that is short by a random amount.

Fold them into the flags the rest of the design sees, at the wrapper boundary, so
no consumer can forget:

```verilog
assign wr_full  = full  | wr_rst_busy;
assign rd_empty = empty | rd_rst_busy;
```

### Simulating XPM needs the library linked

`xelab -L xpm`. Without it, elaboration fails on an unresolved module — loudly,
which is the good case. Any bench that instantiates a FIFO or a named memory
needs it, which in practice is nearly all of them.

---

## Simulation

### `glbl` holds GSR asserted for the first 100 ns

Simulating against real Xilinx primitives (`-L unisims_ver`) requires `glbl`,
which drives a global set/reset for the first 100 ns of simulated time. Every
unisim register ignores everything before that, **regardless of the design's own
reset**.

A bench that starts driving at time 0 sees its first transactions vanish. The
first tile silently produces nothing.

Wait past 100 ns before the first stimulus in any bench that links `glbl`.

And do not add `glbl` everywhere as a precaution: adding it to a bench that does
not need it holds GSR over every XPM cell for 100 ns, which is a behaviour change
to benches that currently pass. Add it where the primitive library is linked, and
where an async FIFO drags in `xpm_cdc` (which instantiates `glbl` itself).

### RTL should carry no `` `timescale ``; the bench supplies one

A `` `timescale `` in synthesisable RTL is meaningless to synthesis and
constrains every consumer of that file. Supply it at elaboration instead:
`xelab -timescale 1ns/1ps`.

### A function in a continuous assign is sensitised by its ARGUMENTS only

`assign y = f(a, b);` re-evaluates when `a` or `b` changes. If `f` also reads a
signal from module scope, **that read does not create sensitivity** — the value
sampled is whatever it was the last time an argument moved, and the assign is
stale for as long as nothing else changes.

It presents as a datapath that is right on the first transaction of a burst and
wrong on the rest, or as a register that takes the previous beat's value. The
fix is to pass everything the function reads: a byte-merge helper that read
`wdata` and `wstrb` from module scope pulsed the previous beat's data into a
control register, and moving both into the argument list changed nothing else
about it.

A function that reads module scope is not wrong in an `always` block, where the
sensitivity list is inferred from every read. It is wrong in a continuous
assign, and the two look identical on the page.

### Delays are 64-bit; a watchdog that did not fire is a logic bug

`#N` in `xsim` is a 64-bit quantity, so a watchdog at `#500_000_000` is 500 ms
and not a wrapped-to-zero no-op. **A watchdog that never fired means the
condition it was waiting on was already true**, or the bench exited another way
— never that the delay overflowed 32 bits.

Worth stating because the wrap explanation is plausible, cheap to believe, and
sends the next hour into the wrong file.

### A permissive simulator passing is not evidence

`iverilog` accepts things Vivado rejects — most notably use-before-declaration,
where a forward reference to an undeclared identifier quietly becomes an implicit
one-bit net in one tool and a hard error in the other.

Passing under a permissive simulator is not evidence that the stricter one will
even compile the file, let alone synthesise it the same way. Where both are
available, run both; where only one is, make it the one the build uses.

### The Verilator XPM FIFO shim is faster than the library

`sim/verilator/shims/` models `xpm_fifo_sync` behaviourally. In first-word
fall-through mode the shim presents a written word two cycles earlier than the
XPM library does in xsim: a register → boundary → register → FIFO hop measures
3 cycles accept-to-deliver under Verilator and 5 under xsim with the real
macro (`kx_hop_tb`, `TB_LEAN=0`). A named primitive through `kohaku_sdpram`
measures the same in both (4). Take any XPM-FIFO latency from xsim, never from
Verilator; Verilator's number is a lower bound.

### One vendor macro blocks the whole library under Verilator

Two things stop Verilator compiling the vendor's XPM sources directly, and only
one is fatal.

**Assertions — solvable.** The XPM sources carry SystemVerilog assertions that
Verilator rejects (cycle-delay ranges, boolean-abbrev repetition). One define
clears every one of them across the FIFO, CDC and memory libraries at once; the
narrower define that names assertions only clears some of them, which reads as
progress and is not.

**`deassign` — fatal.** After that define, the only remaining errors are a
handful of Verilog-1995 `deassign` statements, all inside the **base memory
module**. Verilator rejects `deassign` outright, and the FIFO macros instantiate
that module, so a single module blocks every FIFO and every RAM in a design. The
clock-divider primitive has the same problem.

The consequence for planning: "Verilator cannot simulate this design" is almost
never true at the design level. It is usually one vendor module, and the answer
is a shim for that module rather than abandoning the tool. See
[simulate.md](simulate.md#shims).

### Verilator makes warnings fatal, so real findings get silenced in bulk

Verilator's default is to treat warnings as errors, and a codebase written
against a more permissive tool trips them by the hundred on first contact. The
practical response is to silence the noisy classes and pass `-Wno-fatal`.

That is the right move and it has a cost worth naming: **the silenced list is not
noise.** Inferred latches and width truncations that discard the high bits of a
shift are in there, they are real, and no other tool in this flow reports them.
Keep an explicit "show everything" flag and run it as an occasional pass of its
own, separate from the simulation loop.

### A comment whose first word is `Verilator` fails the build

Verilator parses `// verilator ...` as a metacomment — a directive — and an
unrecognised one is a hard error, not a warning. So an ordinary English comment
that happens to begin with the tool's name stops the build with
"Unknown verilator comment".

Start the line differently.

### A lint-only bench prints no verdict, so it failed by construction

`xsim.py`'s `*_lint` entries elaborate an RTL module as the top. A module has
no `initial` block, so `xsim -runall` returns at once having printed nothing,
the harness finds no `PASS` line, and every such row was red in `check.py`
from the day it was added — including the `kaxi_lint` precedent the newer
ones copied. Elaboration *is* the check for these; the harness now prints the
verdict itself after `xelab` and never runs the simulator.

### One wedged simulator starves every other xsim on the machine

A legacy bench that never finishes (`kaxi_xbar4 -d KAXI4` and its two
variants) left an `xsimk` spinning at one core. While it lived, a 32-shape
gate did three shapes in 40 minutes and `check.py full` sat on its
fifteenth row; killing it, both finished at their usual pace. A stuck
simulator is not a local loss — find it (`xsimk`, hours of CPU, started when
the hung row was) before reading anything else as slow, and keep a bench
that is known to hang out of the tier, listed as a gap.

### Verilator 5.020 overflows its stack at a `fork` inside a task

`kx_pxache_tb` runs under xsim and dies under Verilator — SIGILL or SIGSEGV
after ~13 s with nothing printed (`--binary`, `--timing`). AddressSanitizer
names it: a **stack-overflow** in `VlCoroutine`'s constructor, entered from
the fork inside the bench's `stream_rd` task. `kx_xache_tb`, the same bench
without that fork, runs in 0.2 s under the same tool. Verilator 5.020
(the WSL package) predates the fork-in-task and stack-sizing fixes of the
5.02x line; the bench is not changed for it, and xsim is the gate of record.
A model that dies prints nothing because its stdout buffer dies with it —
`stdbuf -oL` first, then read the last lines.

### A register in front of a RAM's write port is a duplicate

A block RAM's and a distributed RAM's write port register WE, ADDR and DIN
at the clock edge. A "landing register" placed in front of one — the reflex
for a wire arriving from another die — is a second register stage that
costs a cycle and a flop per bit and adds nothing the RAM does not already
do. `kx_hop` measured 4 cycles accept-to-deliver with it and 3 without,
590 FF per hop either way (`kx_hop_tb`). What only a placed run can settle is
whether the crossing's far end wants a fabric flop for its own timing; keep
that as a parameter, not the default.

### A verdict at column 0 is dropped by the harness

`xsim.py` keeps only indented lines, `ERROR` lines and `PASS`/`FAIL`
verdicts, so a bench's `@@@ PERF …` measurement lines at column 0 never
reached the caller — the Xache's bandwidth figures were being read from a
kept work directory. Lines starting `@@@` are now kept; a bench that prints
a number it wants read should still indent it.

---

## Reports and object queries

The Tcl object model returns an empty list for a query that matches nothing, and
every consumer accepts an empty list. So an entire class of trap is "the number
is zero because the query was wrong", and zero is a legitimate-looking answer.

### A bare synthesis checkpoint carries no clocks

Open a post-synthesis checkpoint that was written before any constraint applied,
and every timing query against it returns nothing. `report_timing` prints no
paths, `get_timing_paths` returns an empty list, and a script that counts failing
paths reports **zero failing paths** on a design it never analysed.

Check `get_clocks` first, and error if it is empty. "No failing paths" and "no
analysis" must not be the same output.

### `[` opens a character class in a Vivado glob

`get_cells -hier -filter {NAME =~ g_stn[1]/*}` does not match `g_stn[1]`. The
brackets are a glob character class, so the pattern matches the single character
`1` in that position — and every bracketed instance in the design counts **zero**,
in silence.

Escape them before building the pattern:

```tcl
set pfx [string map [list "\[" "\\\[" "\]" "\\\]"] $prefix]
```

The same shape appears with `-filter {NAME == ...}` against a hierarchical
generate name: use the literal path in braces, and check the result is non-empty
rather than trusting the pattern.

### A property name that does not exist filters to zero

`get_cells -hier -filter {PRIMITIVE_GROUP == CLB_LUT}` returns nothing. The
property exists; that value does not. Nothing warns, and the count lands in the
column that reads as "that instance is empty".

Count by `REF_NAME` against the primitive families you actually mean
(`LUT?`, `FD*`, `RAMB*`, `CARRY*`), and sanity-check the total against
`report_utilization` before believing any of the sub-counts.

### `-of_objects` wants nets or pins, not a parent cell

`get_cells -hier -of_objects <cell>` to enumerate a cell's contents returns
nothing, silently. `-of_objects` traverses connectivity, not hierarchy. Scope by
`NAME` instead.

Relatedly, `report_timing -of_objects <path>` **refuses `-input_pins` and drops
it** without complaint, and the header it emits reads `Delay type` in lower case
— so a parser matching `Delay Type` produces empty dumps that look exactly like
clean paths.

### Parsing a utilisation report is three traps in one line

- The total row is **`CLB LUTs*`**, with a trailing asterisk. An exact string
  match on `CLB LUTs` drops the row that matters.
- Sub-rows are **indented** inside the same table. A match anchored to the left
  edge drops all of them.
- Values are **not all integers**. A single 18 Kb block-RAM primitive makes
  `Block RAM Tile` read `26.5`, and an integer test silently discards the row —
  reporting zero block RAM for a design that uses it.

Match on substring, test with a floating-point predicate, and print what was
parsed.

### `-hierarchical` re-parents leaves on a flattened netlist

`report_utilization -hierarchical` on a netlist synthesised with the default
`-flatten_hierarchy rebuilt` will confidently attribute LUTs to the wrong
instance: the boundaries it reports were reconstructed after flattening, not
preserved through it.

To attribute area, re-synthesise with `-flatten_hierarchy none`. But that run is
**not** the number to quote as the design's area — the shipped design synthesises
at `rebuilt`, and boundary optimisation is a real saving `none` forbids. Two
runs, two purposes. See [measure.md](measure.md#hierarchy-none-attributes-rebuilt-ships).

---

## Synthesis

### Ports that are arrays are rejected out of context

Out-of-context synthesis will not accept an array port. Flatten it: carry
`N` interfaces as one `[N*W-1:0]` vector plus `[N-1:0]` control bits, and slice
inside the module.

This is worth doing at every module boundary that a measurement top might cut,
not only the ones that are cut today — a boundary you cannot synthesise
standalone is a boundary you cannot measure.

### `auto_detect_xpm` is project-mode only

Calling it in a non-project flow errors with "No open project". It is not needed:
non-project `synth_design` resolves XPM macros on its own.

### `general.maxThreads` defaults to 2

The default is **2**. The cap is **32**. Synthesis, placement and routing are all
substantially parallel, and leaving this at the default is the single cheapest
build-time mistake available.

Two places need it, because they are different processes:

- **`Vivado_init.tcl`** — sourced by every Vivado invocation, including the GUI.
  Setting it here means a GUI-launched implementation picks it up too, which the
  next item does not cover.
- **A `TCL.PRE` hook on each implementation step** — re-running a single step
  starts a fresh process, so a value set during an earlier step is gone:

```tcl
# TCL.PRE hook for impl steps: Vivado defaults to 2 threads, cap is 32. Set on
# every step because re-running one step starts a fresh process.
set_param general.maxThreads 32
puts "@@@ general.maxThreads = [get_param general.maxThreads]"
```

Confirm what your installation actually accepts rather than assuming — the cap
has moved between versions, and a rejected value leaves the old one in place.
`scripts/tcl/check_threads.tcl` probes it:

```tcl
puts "@@@ default maxThreads = [get_param general.maxThreads]"
foreach n {8 16 32 64} {
    if {[catch {set_param general.maxThreads $n} e]} {
        puts "@@@ set $n REJECTED"
    } else {
        puts "@@@ set $n -> [get_param general.maxThreads]"
    }
}
```

### Editing a source file while a background synthesis is reading it kills the run

Synthesis reads sources over a period, not atomically at launch. Saving a file
mid-run gives you a truncated read, a parse error deep in the log, or — worse — a
netlist built from a mixture of two versions.

A long run is not a background task you can work around. Either wait, or work on
a copy. If a build must run while editing continues, snapshot the sources into
the build's own working directory first and synthesise the snapshot.

The same applies to a build that reads a generated file: regenerate before the
run starts, never during.

---

## Block designs

### `validate_bd_design` does not check reachability

It passed a design containing a 4 GB address window for a slave with **no path to
it**. The address map is validated; the connectivity implied by it is not.

After any structural change, check connectivity explicitly — walk the ports you
care about and report what each is connected to:

```tcl
foreach m {M00 M01 M02 M03 M04 M05 M06 M07} {
    set n [get_bd_intf_nets -quiet -of_objects [get_bd_intf_pins root_smc/${m}_AXI]]
    set ends {}
    foreach e [get_bd_intf_pins -quiet -of_objects $n] { lappend ends [get_property PATH $e] }
    puts "@@@ $m -> [expr {[llength $ends] >= 2 ? $ends : {UNCONNECTED}}]"
}
```

### A module-reference parameter is typed from its default

IPI decides whether a Verilog parameter is an integer or a bit-string from the
**default value**, not from the declared width. `parameter [NSWAP*8-1:0] SWAP_A
= 0` — a width in terms of another parameter and an unsized zero — is an
integer, and `set_property CONFIG.SWAP_A 0b0001...` is refused with
`IP_Flow 19-3452 Invalid long/float value`. A fixed width with a sized zero is
a bit-string:

```verilog
parameter [255:0] SWAP_A = 256'h0;   // bit-string: CONFIG.SWAP_A 0b... is accepted
```

Slice it down to the consumer's width at the instantiation
(`.SWAP_A(SWAP_A[NSWAP*8-1:0])`).

### Dropping module-reference caches forces a repackage on open

`<proj>.gen/sources_1/bd/mref/<module>/component.xml` is packaged once and the
RTL is never re-parsed, so a source change reaches the block design only after
that directory is deleted. Delete it **only before a rebuild**: deleted before a
plain `open_project`, every module is repackaged on open, which is where a batch
session can die without a message.

### Runs outlive the session that launched them

`launch_runs -jobs N` hands the runs to a scheduler (`vrs.exe`) and per-run
`runme.bat` chains. Killing the Vivado session that launched them stops
nothing: the scheduler keeps dispatching queued runs, each finished run starts
the next, and the project directory stays locked. To stop a build, enumerate
processes whose **command line names the project directory** — excluding the
shell doing the enumerating, whose command line contains it too — kill them,
and confirm none remain before touching the project.

### A net outlives the cell at its far end

Deleting a block design cell leaves its nets behind as one-ended stubs. Testing
"is this pin connected" by asking whether a net exists therefore returns true for
a pin connected to nothing.

**Count endpoints, not nets.** A live connection has two or more interface pins
on its net; anything less is a stub to delete before reconnecting.

### `delete_bd_objs` errors on an empty list

Which aborts a whole script on its second run. Guard it, so that re-running a
structural edit is safe:

```tcl
set l1 [get_bd_cells -quiet leaf_smc_1]
if {[llength $l1]} { delete_bd_objs $l1 } else { puts "@@@ leaf_smc_1 already gone" }
```

Every block-design edit script should be idempotent. They are re-run constantly —
after a crash, after a partial edit, after a merge — and one that only works from
a specific starting state is a script that works once.

---

## Verilog

These are language traps rather than tool traps, but they cost the same and they
show up as area or timing rather than as errors.

### Unsized literals in a concatenation contribute 32 bits

`{..., (BASE + i*4) * 32, ...}` places a 32-bit value, not one of the field's
width, and silently shifts every field below it. Use an explicitly sized `reg`
or a sized literal for anything entering a concatenation.

### Serial loops synthesise serially

```verilog
for (i = 0; i < 25; i = i + 1)
    if (!found && x[i]) begin found = 1; idx = i; end
```

is a 25-level LUT chain inside one pipeline stage. No amount of pipelining
*around* the stage helps, because the depth is inside it.

Searches want smear–isolate–encode; sticky bits want mask-then-reduce. The
rewrite is mechanical and the difference is an order of magnitude in logic
levels.

### Variable part-select writes build a barrel mux across the whole register

`buf[(i*32 + {ctr,3'd0} + k)*7 +: 7] <= ...` costs a mux tree spanning every bit
of `buf`. Unrolling over the varying counter turns it into a static select and
removes the tree entirely. One observed case cost tens of thousands of LUTs
until unrolled.

### Paired parameters that must agree, and nothing checks them

A depth and its address width, a lane count and its index width. Declaring both
independently means a caller can set one and not the other, and the result is a
structure silently smaller than requested — a 16-entry buffer where 512 was
asked for.

**Derive one from the other** (`localparam AW = $clog2(DEPTH)`), or add an
elaboration-time assertion. Never document the constraint and rely on it being
read.

### A parameter sliced to its own index width is zero

`W[$clog2(W)-1:0]` is not `W`. It is `W` truncated to the number of bits an
INDEX into `W` needs, and for any power of two that is exactly zero: 256 in
8 bits is 0, 512 in 9 bits is 0.

It elaborates clean, because both sides are legal, and it presents as a
structure of size zero or a counter that never advances. The shape appears
wherever a width and an index width sit next to each other in the same
expression, which is the same neighbourhood as the paired-parameter trap above —
and the same fix applies: give the index width its own name and use it only for
indexing.

### Memory primitives are named, never inferred

Write a `reg` array and the mapping to distributed RAM, block RAM or ultra RAM is
a tool heuristic — and so is the **read latency**, which sets pipeline depth.
Pipeline depth is a design decision, not a synthesis outcome.

Instantiate the primitive explicitly through a named wrapper with the memory type
as a parameter. The cost of a shape is then measured rather than argued, and it
does not change when a reset clause is edited.

---

## A general discipline

Most of the above reduces to one habit:

> **Check that the thing you asked for happened, not that the command returned.**

Vivado's Tcl surface accepts empty object lists everywhere, warns at severities
that scroll past, and prefers a default to a failure. Every script in this
framework that measures or constrains something ends by asserting the
constraint's presence — clocks created, nets connected, parameters recognised —
and **errors out**, loudly, when it is absent. That habit is worth more than any
individual item on this page.
