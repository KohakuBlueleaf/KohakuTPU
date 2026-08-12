---
title: Building
summary: What the build flow actually is — generators, out-of-context runs, assembly, implementation — and which parts of it are framework machinery rather than project configuration.
tags:
  - workflow
  - build
  - vivado
---

# Building

The build turns a description of a machine into a bitstream and a driver that
knows what is in it. It is not one command, and it is not one tool: it is a
pipeline of generators, measurements, an assembly step and a very long
implementation run.

    map / manifest                  what machine to build
      |
      |  generators
      v
    generated tops  +  generated wrappers
      |
      |  out-of-context synthesis          seconds to minutes   measure.md
      v
    per-block Fmax and area
      |
      |  assembly: block design, IP, address map, floorplan
      v
    device top
      |
      |  synthesis                          tens of minutes
      |  implementation                     hours
      v
    bitstream  +  board description
      |
      v
    driver                                                     bringup.md

Two properties of that picture matter more than any individual step:

- **Everything downstream of a generator is generated.** A top, a wrapper, a
  floorplan constraint file and the driver's description of the machine all come
  from one description. Hand-editing any of them puts the machine and the
  software that drives it out of agreement, silently.
- **The expensive step is last.** Every measurement, every check and every gate
  exists to avoid discovering a problem after a multi-hour implementation run.

## Where the build lives

There are two directories and confusing them wastes time.

| directory | holds | in version control |
|---|---|---|
| the source repository | RTL, generators, scripts, docs, tests | yes |
| the tool project | the block design, IP, runs, checkpoints, reports | no |

The tool project **references RTL in the source repository by path**. It is not a
copy. Editing a source file changes what the next synthesis run reads — including
one that is already running, which is [a way to kill a
build](tooling-traps.md).

Keeping the project out of version control is deliberate: it is large, largely
binary, regenerable from scripts, and it changes on every run. What must be in
version control is **everything needed to rebuild it**: the block-design script,
the constraints, the generator inputs.

That implies a rule which is easy to state and easy to violate:

> **Any change made in the GUI that is not also in a script does not exist.**

The block design is the usual casualty. A connection made by hand survives until
someone regenerates, and then it is gone with no record that it was ever there.

## Two build modes, used for different questions

| mode | used for | why |
|---|---|---|
| **non-project / in-memory** | every measurement | no project state, no runs, no incremental confusion; a script creates a design in memory, synthesises, reports, exits |
| **project** | the device build | manages IP generation, out-of-context IP runs, incremental checkpoints, and the implementation run's many steps |

Measurement scripts should never touch the device project. They
`create_project -in_memory`, read sources, synthesise out of context and write to
their own results directory. That is what makes them safe to run while a build is
in flight, and cheap enough to run constantly.

## The generators

Generation is what keeps one description of a machine consistent across RTL,
constraints and software.

### The assembly top

A **map** describes the machine: a grid of routers, and what hangs off each port
of each router. The generator reads it and emits a synthesisable top that
instantiates the mesh, the memory agent, and the compute units at those
coordinates, with parameters threaded through.

The map format is a plain text grid of fixed-width tokens, one per position. Its
useful properties:

- **It is readable as a picture of the machine.** Reviewing a topology change
  means reading four lines of text, not a wiring diagram.
- **Unknown tokens are rejected by name**, with a message saying what to write
  instead. Retired node types stay in the rejection table rather than being
  deleted, so an old map fails with an explanation instead of a parse error.
- **Positions that cannot exist are required to be explicitly empty.** A grid
  corner touches no router, so a token there has nowhere to attach; requiring a
  placeholder means a mis-shaped map is caught rather than shifted.

Comments in a map are load-bearing. A map is where a topology decision is
recorded, so the reasoning for choosing this shape over the obvious alternative
belongs at the top of the file that encodes it.

### Interface wrappers

Vendor block designs infer an interface from a **port naming convention**. A
flattened bus does not match one, so it arrives in the design as loose wires:
nothing connects them, nothing complains, and the logic behind them is
unreachable. Synthesis prunes it, the design builds, meets timing and programs.

> **An unconnected output is harmless. An undriven input is the fault.**

An output left dangling costs at most the cone that feeds it, and the log says
so. An undriven input silently deletes everything behind it. One shipped design
had a whole engine commandable by nothing for exactly this reason. Nothing
failed. It simply never ran.

Two fixes:

- **Name the interface** with the vendor's interface attributes, and associate
  the clock with it, so the block design connects the whole bus in one action.
  This is what the wrapper generator emits — one wrapper per port count, because
  a Verilog port list cannot come from a `generate` block.
- **Put the input behind an interface that already exists** — decode it out of a
  control window you already have.

Then check the wrapper is only wiring: **synthesise the wrapper and the module it
wraps at the same parameters, and require the areas to be identical.** A mis-wire
lets synthesis prune, so a broken wrapper comes out *smaller* — which is why an
identical number is evidence here, where usually it would be a coincidence.

Register every generated wrapper as a measurement target. **A target no script
names has never been read by a tool, however green the test suite is.**

### The machine description for software

The driver needs to know what is on the device: coordinates, capacities, the
address map, the interface version. Generate it, from three sources that fail
differently:

| source | supplies | how it fails |
|---|---|---|
| the map | coordinates and node types | detectably — synthesis consumed the same file |
| the synthesis log | capacities and interface version | invisibly — it describes one build and rots |
| explicit arguments | the address map | never defaulted — it comes from an assembly nothing else can read |

Hand transcription is the known fault. One board description was written by
reading a synthesis log by eye, missed that the bitstream had smaller capacities
than the RTL's defaults, and produced a run in which the overwhelming majority of
output elements were wrong **while every gate passed**.

So the generator compares what it read against what the software plans for, and
warns — or refuses, under a strict flag — when the build has *less* capacity than
the planner assumes. And every generated description ends by saying it has not
been verified against hardware, with the command that would verify it.

### Generated files that no longer generate

A generated artefact whose generator can no longer produce it is **not a source
file**, and leaving it in the tree invites someone to build it. The failure is
the one above: a machine whose capacities silently disagree with the software.

Either regenerate it or delete it. Do not leave it looking like a build target.
If deleting is somebody else's call, say so in a file next to it, naming what has
drifted.

## Assembly

Assembly wires the generated tops to the outside world: host interface, memory
controllers, clock generation, reset, the control fabric and the address map.

Do it in a script. The script should be **idempotent** — re-runnable after a
crash, a partial edit, or a change of mind — because it will be re-run constantly.
That means:

- guard deletions, which error on empty lists
- test connectivity by counting endpoints, not by asking whether a net exists —
  a net outlives the cell at its far end
- treat "already connected" as success, not as a conflict

See [tooling-traps.md](tooling-traps.md) for these in detail.

### The address map

Two rules, both learned expensively:

**Format wide addresses as wide addresses.** Tcl's `%X` is 32-bit. An address
above 4 GB comes back truncated, and on a wide map that silently piles every
window onto the bottom of the address space. Use `%llX`. It fails silently,
produces a design that validates and builds, and surfaces much later as
overlapping segments.

**Assign control windows first, high; memory windows follow.** A multi-gigabyte
window placed at zero swallows anything already under it.

And after any structural change, **check reachability explicitly**. Block-design
validation checks the address map, not whether a path exists to the thing the map
names. It has passed a design with a large memory window for a slave with no
route to it.

### Clocking

Clock constraints are their own file, written flat. [XDC is parsed in a
restricted mode](tooling-traps.md) and control flow inside it is silently
skipped, which has cost hours of routing.

What belongs there is the relationship between domains — chiefly which of them
are mutually asynchronous, so the tool does not try to time crossings that are
asynchronous by construction. Clock *creation* usually comes from the IP itself
and should not be duplicated by hand.

If a clock generator is runtime-reconfigurable, note it in the constraint file:
the tool constrains the generated clock from its build-time settings, so that
frequency — and not whatever the design is later tuned to — is the verified
ceiling. See [timing-closure.md](timing-closure.md).

### The floorplan

Generate the region constraints from the same description that generates the
assembly, and mark the file as generated. See
[timing-closure.md](timing-closure.md) for what to put in it and why.

Two mechanical points:

- **`get_cells -quiet` everywhere**, so the file survives being read against a
  design that lacks the cell.
- Do not emit constraint files by string-building Tcl with braces in it unless
  you have checked the result. Braces inside a generated string are a
  well-established way to swallow an entire block into an unterminated string
  with no error. If a constraint file is static, keep it static.

### Making the wrapper the top

After assembly, the design's top is the generated wrapper. Two things follow, and
both have been missed:

- Refer to the generated wrapper **by object, not by literal path**. The
  generated directory is named after the project, so a hardcoded path is wrong in
  any other project.
- **Reset the runs.** A run keeps the top it was launched with. Change the top
  without resetting and synthesis keeps building the old one — successfully.

## Synthesis and implementation

The implementation flow is a sequence of steps, each of which can be re-run
independently:

    init_design -> opt_design -> place_design -> phys_opt_design
        -> route_design -> post_route_phys_opt -> write_bitstream

Order-of-magnitude costs for a large design filling most of a big device:

| step | order of magnitude |
|---|---|
| out-of-context measurement of one block | seconds to minutes |
| synthesis of the whole device | tens of minutes |
| `opt_design` | ~an hour |
| `place_design` | **several hours** — the dominant cost |
| `phys_opt_design` | minutes |
| `route_design` | hours |

Two consequences shape everything else in this documentation set:

- **Placement is where the schedule goes.** Anything that lets you find a problem
  before placement is worth doing, which is the entire argument for
  [out-of-context measurement](measure.md).
- **A failed build is a day.** A crash mid-route costs the whole run. Do not edit
  constraints, sources or the block design while one is in flight.

Practical notes:

- **Set the thread count.** The default is far below the cap and this is the
  cheapest build-time win available. See [tooling-traps.md](tooling-traps.md).
- **Run implementation in the background**, always, and never poll it by hand.
- **Read the whole log.** Result lines come after hundreds of warnings; grepping
  the head of a log truncates the answer without saying so.
- **Keep strategy settings in one file that the flow actually sources.** A
  strategy file no script reads is a strategy nobody is using — and it will be
  quoted in a review as if it were.

### Gates before the expensive step

The point of the earlier stages is to gate the later one. In increasing cost:

1. **Lint and software tests** — seconds.
2. **Unit and module simulation** — seconds to a minute
   ([simulate.md](simulate.md)).
3. **Out-of-context synthesis of each changed block** — minutes
   ([measure.md](measure.md)).
4. **Out-of-context synthesis of the assembled top** — tens of minutes. This is
   the only cheap thing that answers "do the parts fit together", and it is worth
   running deliberately rather than as part of a sweep.
5. **Whole-device synthesis, then implementation.**

Skipping a stage is legitimate when a change cannot affect it — a comment, a
docstring, a test. Skipping stage 3 or 4 because "it is a small change" is how a
multi-hour run gets spent discovering a two-minute fact.

## Framework machinery versus project configuration

This section is the one that matters for reusing any of the above.

The build flow described here divides cleanly into two kinds of thing, and today
the two are mixed together inside the same files:

**Framework machinery** — the same for every project:

- the out-of-context measurement flow, its abort conditions and its report format
- the per-clock classification of results
- the generic-existence check before synthesis
- hierarchical utilisation and per-die spread reporting from a checkpoint
- the thread-count hooks
- the wrapper-equivalence check
- the block-design idempotency helpers
- the shape of the gate ladder

**Project configuration** — different for every project:

- which device, which speed grade
- which modules exist, and which sources each needs
- which clock ports each module has, and what frequency it targets
- which tops are ships and which are measurement-only
- the address map, the region-to-die assignment, the IP set
- where the tools are installed, and where the project directory is

Every script in the current tree embeds the second kind. A measurement runner
carries a table of dozens of modules with their source file lists. Measurement
Tcl hardcodes a repository root and a part number. Simulation runners each keep
their own copy of a source list. None of that is framework machinery; it is one
project's configuration wearing framework clothing, and a second project cannot
use any of it without editing it.

**A measurement script that hardcodes a source list is project configuration.**

The fix is a **project manifest**: one declarative file describing device, tool
paths, targets and their sources, clock ports and periods, benches and their
sources, ship tops, and generator inputs. The scripts read it. They then contain
no project-specific fact at all, and a second project supplies its own manifest
and runs the identical flow.

The same manifest removes the duplication that has already caused failures: a
bench's source list appears once, so a module gaining a dependency cannot leave
one runner broken while the others keep working.

## Open questions

- There is no manifest today. Sources, tops, device and clock targets are spread
  across a Tcl script, a PowerShell runner and a Python runner, in three
  incompatible formats.
- Two source-list tables disagree about which files a bench needs.
- The device part appears in at least three places, and at least one of them
  historically named a faster speed grade than the board carries — which makes
  every measurement taken through it optimistic by an unrecorded amount.
- Where the thread-count hook is registered as a build-step hook is not recorded
  anywhere; the setting reaches the generated run script, but the registration is
  not in any file under version control.
