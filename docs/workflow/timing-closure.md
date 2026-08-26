---
title: Closing timing
summary: The method — group the failing paths and fix the one root a group shares, read logic levels rather than slack, floorplan before pipelining, and spend flip-flops because they are the resource you have.
tags:
  - workflow
  - timing
  - floorplan
---

# Closing timing

A design closes timing when **every path meets its requirement after routing**.
Nothing earlier counts. A synthesis result is a prediction, and this page exists
partly because that prediction is systematically optimistic.

This page is method: how to close timing on a large design that fills most of a
multi-die FPGA, in the order the steps actually pay off. It assumes the
block-level question is already answered — whether one module's logic depth can
reach the target frequency at all is [out-of-context
measurement](measure.md)'s job, and doing it after the device build is the
expensive way round.

**Vocabulary used below.** A **ship** is one complete assembly floorplanned for a
specific device ([what is a ship](../arch/ship/what-is-a-ship.md)). A **mesh** is
the on-chip network and the compute units attached to it
([noc](../arch/noc/README.md)). A **system node** is the single component that
serves one mesh with memory access and dispatch
([sysnode](../arch/sysnode/README.md)). A **die** — the vendor calls it an SLR —
is one of the several fabric dice joined by an interposer inside a large package;
[floorplan](../arch/physical/floorplan.md) covers what crossing one costs.

## The order that works

1. **Group the failing paths.** Find the one root a group shares.
2. **Read logic levels**, not slack, to decide what kind of failure it is.
3. **Floorplan**, if the failure is about distance.
4. **Change the RTL**, if the failure is about logic — and pipeline freely,
   because flip-flops are what this fabric has spare.
5. **Implementation strategy last.** It is zero-sum.

Most projects run this backwards: weeks of pipelining a datapath that was never
the problem, then a floorplan with no schedule left. The rest of this page argues
for the order above.

---

## 1. Do not chase a single failing route

A large design does not have *a* critical path. It has a plateau of paths within
a few percent of each other, and the worst one is rarely the informative one.
Remove it and the next appears immediately, at almost the same slack, because
they are the same problem wearing different names.

> **N failing paths are usually one region with one cause. Find the region.**

So the first action on a failing build is not `report_timing`. It is: **list
every path with negative slack, group them by start-point region →
end-point region, and count.**

### The grouping

Two collapses turn a list of endpoints back into a list of problems:

- **Strip the bit index.** `ctrl/state_reg[3]/D` and `ctrl/state_reg[11]/D` are
  one register reported twice. `foo_reg[7]` → `foo_reg`.
- **Strip the tool's synthesised suffixes.** Synthesis names a LUT after the
  signal it drives and then disambiguates with `_i_12`, `__3`, `_N_4`. All of
  those belong to one source signal.

Then key each path on `start → end` after the collapse, and for each key keep
three things:

| kept per group | what it tells you |
|---|---|
| **count** | how wide the cone is — one register bit, or a whole datapath |
| **worst slack** | which group is setting the clock; sort on this |
| **maximum logic levels** | whether this group is logic-bound or distance-bound |

Sorted worst-first, the output is a short list of *distinct things wrong*, and
the top row is the one that decides the frequency. A design reporting hundreds of
failing endpoints routinely resolves to a handful of groups; fixing the top group
moves every path in it at once, which is exactly what fixing the single worst
path does not do.

### The worked example

`scripts/tcl/ooc_sysnode_rv64.tcl` carries the compact form. Its `@@@GROUP` block
is the whole technique in thirty lines:

```tcl
# EVERY failing path, with its logic level.
set bad [get_timing_paths -max_paths 2000 -nworst 1 -setup -slack_lesser_than 0]
puts "@@@FAILN [llength $bad]"

proc base {pin} {                      # a/b/c_reg[3]/CE -> a/b/c_reg
    set c [file dirname $pin]
    regsub {\[[0-9]+\]$} $c "" c
    return $c
}

array set grp {}
foreach p $bad {
    set k "[base [get_property STARTPOINT_PIN $p]] -> [base [get_property ENDPOINT_PIN $p]]"
    set s [get_property SLACK $p]
    set l [get_property LOGIC_LEVELS $p]
    if {[info exists grp($k)]} {
        lassign $grp($k) cnt worst lvl
        set grp($k) [list [expr {$cnt + 1}] [expr {$s < $worst ? $s : $worst}] \
                          [expr {$l > $lvl ? $l : $lvl}]]
    } else {
        set grp($k) [list 1 $s $l]
    }
}
# Worst group first, because that is the one that decides the clock.
foreach r [lsort -real -index 0 $rows] {
    lassign $r worst cnt lvl k
    puts [format "@@@GROUP %4d paths  worst %+7.3f  lvl %3s  %s" $cnt $worst $lvl $k]
}
```

Three details in it are deliberate and are the part worth copying:

- **`-nworst 1`** — one path per endpoint. Without it the query returns many
  paths to the same endpoint and the counts become a property of the query rather
  than of the design.
- **`-slack_lesser_than 0` with a large `-max_paths`** — *every* failing path,
  not the worst N. A worst-N list is a sample, and a sample of one region tells
  you the region is there, not how big it is.
- **`file dirname` on the pin** — the group key is the register, not the pin, so
  a path ending at `D` and one ending at `CE` on the same register group
  together.

`ooc_cones` in `scripts/tcl/ooc_class.tcl` is the fuller version of the same
idea. It adds three things worth having once the top group is identified: a
representative path per cone dumped cell by cell, the collapse applied to
mid-name indices as well as trailing ones, and a list of the highest-fanout nets
in the design — because on a large part route delay dominates these paths and
fanout is usually why. A net driving hundreds of loads cannot be placed near all
of them, and no amount of logic restructuring changes that.

What the scripts print, and how to make them print it, is in
[measure.md](measure.md#grouped-failing-path-reporting). The rest of this page is
what to do with the answer.

---

## 2. Logic levels are the diagnostic, not slack

Slack says how far you missed. It does not say why, and two paths with the same
slack can need opposite fixes. **Logic levels** — the count of LUT and carry
stages a signal passes through between registers — is the number that
partitions the work.

| levels | reading |
|---|---|
| **≤ 10** | comfortable. If this is failing, the problem is not logic depth. |
| **7 – 9** | the upper limit a mature design should sit at. Sustainable, not spare. |
| **≥ 11** | **this is the thing to fix.** Pipeline it or restructure it. |

**It is a screen, not a verdict.** A level is not a fixed delay: a carry chain
stage is cheap and a wide multiplexer stage is not, and a four-level path across
a die is slower than a twelve-level path inside one slice column. Use the level
count to decide *which question to ask*, then confirm against the report's split
of logic delay versus route delay:

- **Many levels, delay dominated by logic** — compute-bound. Pipeline it, or
  restructure the logic. Floorplanning will not help.
- **Few levels, delay dominated by route** — distance-bound. Pipelining the
  datapath will not help either; the signal is not passing through logic, it is
  travelling. This is a placement problem and placement fixes it.

A path that is almost entirely route delay at **zero** logic levels is a
placement failure stated as plainly as a tool can state it. A pipeline stage
there adds latency and moves nothing.

### An address is already most of the budget

A 64-bit adder is about **8 logic levels** on its own, before it has done
anything with the result. So the moment an effective address is computed and then
*used* by combinational logic — compared against a range, decoded to a port,
matched against a tag, folded into a select — the path is over budget and the
address arithmetic was not the part that broke it.

This produces the single most reliably useful rule on this page:

> **Register every consumer of an effective address — except a memory read
> address.**

The read address is the one consumer that must stay combinational, because
registering it adds a cycle to every access to the structure it reads, and that
structure is usually the one the design is built around. Everything else takes
the registered copy: hit and miss logic, range and permission checks, tag
comparison, port and bank decode, the write-back address, anything that turns the
address into control. Each of those is a separate consumer, each costs the same
8 levels of prefix, and each of them can afford a cycle.

Two consequences worth stating before someone rediscovers them:

- Registering the consumers is a **latency change on the control path only**. The
  data still arrives when it did.
- It is why an address adder that measures fine in isolation shows up as the
  start point of half the failing groups in the assembly. It is not slow. It is
  early.

---

## 3. Registered control lands a cycle late

A control signal that is itself a register, driving a read-first memory array,
does not take effect on the cycle it changes. The array was addressed with the
old value; the new value reaches the array's address port a cycle later, and the
data comes back a cycle after that.

> **Re-probing a read-first array under registered control takes two cycles, not
> one.**

Design the state machine for two settle cycles rather than discovering it. This
matters because of *when* it goes wrong: a control signal is often registered as
part of a timing fix, and the functional bug that introduces is a
one-cycle-early sample that only shows on the second probe of a sequence. It
appears in the run after a change that was supposed to change no behaviour, which
is precisely when nobody is looking for a functional bug.

If a design cannot afford the second cycle, the fix is to compute the *next*
control value one cycle early and register that, rather than to un-register the
control and give the address path its 8 levels back.

---

## 4. Pipelining to reach frequency is nearly free — with one bound

The resource ranking on this fabric is not close:

> **LUT ≫ BRAM/URAM ≫ FF.**

LUTs are what a design runs out of. Block RAM and ultra RAM are the next thing to
run out of and they are hard blocks — a memory is either claimed or it is not, so
those counts are exact and do not compress. Flip-flops are the most abundant
resource on the part, and a LUT-bound design leaves most of them idle: the fabric
offers roughly two flip-flops per LUT, and a design using one has half the
flip-flops in *already-occupied* slices sitting empty. A pipeline register placed
there costs no additional slice at all.

So:

> **Do not contort a design to save a few flip-flops.** Registering a stage to
> shorten a path costs nothing worth counting, and it buys frequency, which is
> the thing that is actually scarce.

**"Free" has a bound, and the bound is memory, not registers.** The argument
above is about flip-flops in fabric slices. It does not license duplicating a
megabyte-scale array to shorten a path, splitting a store into two copies so two
readers each get a short route, or adding a shadow copy of a table for one
consumer. Those are BRAM/URAM, they are second on the scarcity list, and they do
not pack into space you have already paid for.

Which leads to the habit that catches it:

> **Read the memory columns on every run, not only LUT.**

Block RAM, ultra RAM and DSP counts belong in the same report line as the LUT
count, on every measurement, even when memory is nowhere near the budget.
Plentiful is exactly why nobody checks them — a change that quietly doubles a
store shows up as *nothing at all* in the column everyone reads, and surfaces
when the assembly no longer fits. `scripts/tcl/ooc_syscore.tcl` prints the memory
columns beside LUT for this reason, and says so in its header.

The reverse case matters too: a memory that was supposed to be a hard block and
came back as LUTs is a large, silent area regression. Simulation cannot see it —
only synthesis can. See [tooling-traps.md](tooling-traps.md) for the shapes that
cause it.

---

## 5. Control-bound versus compute-bound

The most common misdiagnosis is treating an interconnect failure as a datapath
failure. Grouping (§1) is what makes the distinction cheap, because the group key
names the module.

**Compute-bound** looks like: start and end both inside your datapath, high logic
levels, delay dominated by logic, and one structure owning most of the groups.
The fix is RTL — a pipeline stage, a restructured search, a wide operation split
in two.

**Control-bound** looks like: paths inside vendor interconnect, address decoders,
arbiters, data-width converters, reset trees, or a state machine whose next-state
logic touches everything. The fix is almost never in the datapath and quite often
not in RTL at all.

The distinction decides schedule, not just diagnosis. It is entirely ordinary for
a full-chip build to have **no** failing path in compute and every failing path
inside a vendor AXI interconnect's width converter. Weeks of datapath
optimisation would change nothing there. The fix is structural, and it
generalises:

> **You cannot spread one interconnect instance across a device. You place
> several small ones.**

A monolithic crossbar is a single placeable object. It must sit somewhere, so
every master and slave reaches it from wherever they are, and its internal paths
are whatever the tool makes of them. A hierarchy of small crossbars can be placed
*with* the things they serve — one per region, each short — and the only long
path left is the one between levels, which is a single well-defined crossing you
can pipeline deliberately. It is usually cheaper in area as well, because
port-pair count falls quadratically.

### Reset and enable fanout

A signal that reaches every register in a block is a distance problem wearing a
control signal's clothes. If a group ends at reset pins, enable pins or clock
enables, the fix is fanout reduction — replicate the driver, pipeline it, or make
the reset synchronous and locally buffered — not datapath work.

In out-of-context measurement this is usually an artefact of a false path that
matched nothing ([measure.md](measure.md)). In a routed device it is genuine.

---

## 6. Floorplanning is the first lever

On a multi-die device the largest single lever on timing is **which die each
block lands on**. Not the RTL, and not the strategy setting. The vendor's own
guidance orders it this way — keep tightly coupled modules together first, and
pipeline only what cannot be kept together — and that ordering is routinely
inverted in practice.

### Why it dominates

A crossing between dice is slow, limited in number, and hold-sensitive. A design
placed without a floorplan scatters blocks by whatever the placer's cost function
prefers, and the placer does not know that your compute block and its memory
controller must be neighbours. The result is the highest-bandwidth path in the
design — the one carrying the most bits at the highest rate — spread across the
interposer on a thousand-odd wires.

Constraining each block to sit with the resources it uses removes those wires
entirely, which is categorically better than pipelining them: pipelining adds
latency to every access on the path and the wires are still there.

> **Do not pipeline the highest-bandwidth path to fix a crossing. Remove the
> crossing.**

The general result, stated without the numbers, which belong to [the project that
measured them](../projects/kohakutpu/results.md): on this class of design, adding
per-die region constraints and a hierarchical interconnect — **with no RTL change
at all** — is the difference between failing placement by a wide margin and
passing it. A design without a floorplan on a multi-die part is not yet a design,
and its timing report is not evidence about the RTL.

### How to floorplan

Soft region constraints, placement only:

```tcl
create_pblock pb_region0
resize_pblock [get_pblocks pb_region0] -add {CLOCKREGION_X0Y0:CLOCKREGION_X7Y3}
add_cells_to_pblock [get_pblocks pb_region0] [get_cells -quiet {top_i/block_0}]
add_cells_to_pblock [get_pblocks pb_region0] [get_cells -quiet {top_i/leaf_ic_0}]
set_property CONTAIN_ROUTING false [get_pblocks pb_region0]
```

Four things there are deliberate:

- **The region is a whole die.** The goal is "this block and its memory
  controller are on the same die", not "this block is in this corner". A tight
  pblock over-constrains the placer and usually makes things worse.
- **`CONTAIN_ROUTING false`.** Contain placement, not routing. Fencing the router
  inside the region gives it fewer options for the very paths you are helping.
- **The block's own interconnect leaf goes in with it.** Placing the compute and
  leaving its crossbar outside re-creates the crossing you removed.
- **`-quiet` on every `get_cells`.** The file then survives being read against a
  design that lacks the cell — during an out-of-context run, or after a rename.
  Without it one stale path aborts constraint processing and takes every
  constraint after it down with it.

**Generate the pblock file from the same description that generates the design,**
and mark it as generated at the top. A hand-written region constraint drifts from
the hierarchy it names, and a pblock naming a cell that no longer exists
constrains nothing and says nothing.

### What a floorplan cannot fix

Hard blocks do not move. A PCIe block, a memory controller's I/O banks, a
transceiver — each is pinned to a site, and everything that must be adjacent to
it inherits that pinning. The consequences are structural:

- The die holding the host interface is permanently the most congested. Put the
  smallest partition there, by plan rather than by discovery.
- With one memory controller per die, a block that needs memory has already had
  its die chosen for it.
- If the topology contains a cycle, at least one edge must span a long distance —
  it is not possible to embed every edge locally. **Identify which edge that is,
  pipeline that one, and pipeline no others.** A stage added everywhere "for
  safety" costs latency everywhere.

Work these out before floorplanning, not during. They determine the answer.

### Crossings want registers on both sides

Devices with dedicated crossing flip-flops use them only if there is a register
available to pull into them. A path registered on one side and combinational on
the other has nothing to place there, and the tool routes it as ordinary fabric —
so a design can use **none** of its several thousand crossing sites while
crossing dice constantly, purely because the interface was registered outbound
and bare inbound.

Register both directions at any interface that may end up spanning dice. It costs
one cycle and it is the difference between using the dedicated resource and not.

Vendor register-slice IP usually has a setting meaning "this crossing spans dice"
and sizes the pipeline itself. Prefer it to a hand-built one, and **do not
region-constrain the slice** — pinning it defeats the point, which is that the
tool places the pipeline along the crossing.

### Expect hold violations, not setup, on crossings

Variability between dice is larger than within one, so the timing engine budgets
crossings pessimistically for hold. A crossing that fails hold is normal and is
fixed by delay insertion, which the tool does automatically given somewhere to
put it. A crossing that fails **setup** usually means it should not have been a
crossing.

---

## 7. Utilisation: what is actually scarce

This is the most consistently misread part of a utilisation report, and getting
it wrong changes architectural decisions.

> **CLB percentage is routing and packing pressure. It is not a capacity ceiling.
> Never quote it as one.**

A CLB (or slice) holds several LUTs and roughly twice as many flip-flops. The
percentage reported as "CLB" is the fraction of CLBs with *anything* in them, not
the fraction of their contents used. A design can sit near 90% CLB while using
under 60% of the LUTs — the occupied CLBs are averaging around five LUTs of
eight, and **added logic largely packs into CLBs already in use**.

Consequences:

- "We are at 90% CLB, there is no room for this feature" is usually **false**.
  Check the LUT percentage before believing it.
- A high CLB figure beside a low LUT figure is a *packing* signal: more control
  sets than the fabric packs well, not a full device.
- **Control sets are the scarce resource**, not LUTs. A control set is a distinct
  combination of clock, set/reset and clock enable; eight flip-flops share one
  such triple per slice, so a stray reset splits the packing. Two regions with
  similar LUT counts can differ substantially in achievable density on control-set
  count alone.

So the first lever on utilisation is **control-set reduction**, not logic
reduction: fewer distinct resets, fewer distinct clock enables, enables expressed
as datapath multiplexers where they are cheap. Better packing is free capacity,
and it buys frequency as well as area, because a less fragmented placement routes
shorter.

Judge a reset-removal experiment on **LUT count and control-set count**, not on
the number of registers that carry a reset. Reading the RTL does not settle it
either: a reset-free source still routes as one if synthesis folds one in.
`ooc_resets` and `ooc_ctrlsets` in `scripts/tcl/ooc_class.tcl` report both from a
netlist.

One counter-intuitive consequence: removing resets **densifies** packing, so
congestion can rise even as area falls. Pair a reset-removal change with a
placement directive that spreads, or the win shows up as a routing failure.

### Composed area is not the sum of the parts

Adding up per-module utilisation over-predicts the assembled design, sometimes
substantially: constant propagation, shared logic and boundary optimisation all
cross module boundaries in the assembly and cannot in isolated runs.

Per-component sums are an **upper bound**. Only a composed synthesis is a number.

The exception is hard blocks. A DSP or a memory block is either claimed or not,
so those counts sum exactly — which makes them the right currency for capacity
planning before a composed build exists.

### Composition costs frequency too

An assembled design closes lower than its own slowest component measured alone.
Budget for the drop: a component that exactly meets the target in isolation will
miss it in the machine.

---

## 8. Synthesis slack is optimistic

> **An out-of-context synthesis result is not a closed-timing figure and must
> never be presented as one.**

Synthesis estimates routing. Placement and routing then supply the real numbers,
and they are worse — not by a rounding error. One module in the reference
instance lost **0.740 ns** of worst slack between synthesis and routing, same
design and same constraints, on `xcvu13p-fhgb2104-2L-e` under Vivado 2024.2.

Two rules follow, and they are the same rule at two altitudes:

- **A block's synthesis Fmax is a screen.** It disqualifies. It does not sign
  off. See [measure.md](measure.md).
- **A frequency claim needs a routed run behind it**, in context, on the part
  that ships. Anything else is labelled with what it actually is: an
  out-of-context synthesis figure at a stated period.

[arch/physical/measurement.md](../arch/physical/measurement.md) is the page that
states this once for the whole tree; every number anywhere in these docs is
subject to it.

---

## 9. Implementation knobs are zero-sum

Implementation strategy directives — explore variants, extra optimisation passes,
retiming, alternative placers — are real and they are small. More usefully:

> **They redistribute slack. They do not create it. The ceiling is RTL work.**

A directive that recovers slack on the group you are watching has usually spent
it somewhere else, and the next run's worst path is a group you had not been
looking at. They also multiply build time, which is already the binding
constraint on iteration.

Reach for them when a design is close and structurally settled — the last few
tens of picoseconds on a design whose failing groups are all in your own
datapath. Reaching for them while the failing groups are still in an
unfloorplanned interconnect spends hours per attempt to recover a fraction of
what a pblock file gives for free.

Keep the strategy settings in one file that the build flow actually sources, and
check that it is the file in effect. A strategy file no script reads is a
strategy nobody is using, and it will be quoted in a design review as if it were.

---

## A runtime-tunable clock changes the economics

If the clock feeding the compute region comes from a reconfigurable generator
with a control interface, then "the frequency missed" costs a register write
instead of a rebuild. The rebuild cycle for a large design is measured in hours;
sweeping frequency on real hardware turns a multi-day question into a
multi-minute one. Arrange it early.

Two things to know:

- **The control plane must not stand on the clock it changes.** Use a separate,
  fixed generator for the control path. A control interface clocked by the clock
  it is retuning cannot recover from a bad setting.
- **Timing analysis does not know the clock is tunable.** The tool constrains the
  generated clock from its build-time settings, so *that* frequency is the
  verified ceiling. At or below it, the design is analysed. Above it you are in a
  deliberately unmeasured sweep — a legitimate thing to do, and one that must be
  labelled as such wherever the resulting numbers are quoted.

---

## Checklist

When a build misses:

1. **List every failing path and group them** by start → end after stripping bit
   indices and synthesis suffixes. Sort worst-first. Do not read a single path.
2. Take the **top group**. Note its count, its worst slack and its maximum logic
   levels.
3. **Levels ≥ 11?** Logic depth. Pipeline or restructure — and check whether an
   effective address is feeding combinational logic, because that is 8 of them.
4. **Levels low, route delay high?** Placement. Check the high-fanout net list
   before anything else.
5. Is the group in **your** logic, or in interconnect, reset or address decode?
   If not yours, stop optimising the datapath.
6. Do the paths **cross between dice**? If so, is the block on the same die as
   the memory and the host interface it uses? Fix that before anything else.
7. Is a **single monolithic interconnect** involved? Split it.
8. Check the **LUT** percentage before believing a CLB percentage, and the
   control-set count before believing a packing problem is a capacity problem.
9. Read the **memory columns** — a fix that moved an array out of BRAM is a
   regression whatever it did to slack.
10. Only then, strategy directives.

## What this page does not cover

- **Whether the design computes the right answer.** Timing closure and
  correctness are independent; see [simulate.md](simulate.md).
- **Whether one block can reach a frequency at all.** That is
  [measure.md](measure.md), and it belongs before this page, not after.
- **Hold closure inside one die.** The tool fixes it automatically and it is
  almost never the interesting failure. Crossings are the exception, above.

## Open questions

- Region constraints are generated from the assembly description; the assembly
  description itself is currently project-specific. See [build.md](build.md).
- Crossing-register discipline is a convention, not something any script checks.
  A rule that "any interface which may span dice is registered in both
  directions" belongs in [the compute-unit port
  spec](../spec/compute-unit-port.md) rather than in a workflow page.
- Nothing enforces the logic-level budget. A build that closes at 12 levels
  closes; the budget is advice about where the next failure will come from, not a
  gate.
