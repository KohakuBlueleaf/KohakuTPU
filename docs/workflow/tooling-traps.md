---
title: Tooling traps
summary: Vivado and XPM behaviours that cost real time, what each one does, why it does it, and how to avoid it.
tags:
  - workflow
  - vivado
  - xpm
---

# Tooling traps

Every trap here was paid for once. None of them is exotic; every project that
uses this framework will meet most of them. They share a shape:

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

### A permissive simulator passing is not evidence

`iverilog` accepts things Vivado rejects — most notably use-before-declaration,
where a forward reference to an undeclared identifier quietly becomes an implicit
one-bit net in one tool and a hard error in the other.

Passing under a permissive simulator is not evidence that the stricter one will
even compile the file, let alone synthesise it the same way. Where both are
available, run both; where only one is, make it the one the build uses.

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
