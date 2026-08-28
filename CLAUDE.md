# KohakuAccel — project philosophy & rules

## 1. What this is

**KohakuAccel** (`src/kohakuaccel/`) is a framework for building FPGA
accelerators: a NoC mesh of compute units reaching DRAM through AXI4, plus the
plumbing around it — routers, orchestrator, system node (MAG, mover, control
processor, interlink), the CPU PE, the AXI station bus and links, and named
memory/FIFO primitives.

Everything else is a **project built on the framework**:

- **KohakuTPU** (`src/kohakutpu/`) — the reference accelerator: matmul, vector,
  transform occupants, generated tops.
- **KohakuMPE** (`src/kohakumpe/`) — the SIMT PE, and `simd/`, the SIMD unit that
  fills the framework's `SIMD_EN` slot.
- `src/examples/`, `src/templates/` — worked examples and the framework's
  template occupants/adapters.

Target part `xcvu13p-fhgb2104-2L-e` at **300 MHz**.

## 2. Philosophy

**Frameworkize FPGA/RTL/HDL development.** The goal is not to ship a pile of
useful IP — it is to ship a useful *platform*: a repeatable way to build, wire,
simulate, and measure accelerators, where a new unit drops into named slots and
the framework carries the rest.

**Choose the simplest *general* solution, never the simplest *special* one.** A
knob that covers the whole design space beats a hard-coded value that happens to
fit today. When two approaches differ only in behaviour, both ship as a
configurable option; when they differ only in cost, both get built and measured.
The framework's job is to make the general case cheap, not to special-case the
common one.

## 3. Where things go, and the rules for each

```
src/kohakuaccel/     THE FRAMEWORK (noc, sysnode, pe, axi, common, verif)
src/kohakutpu/       reference accelerator          src/kohakumpe/  SIMT + SIMD
src/templates/       framework worked examples      src/examples/   example projects
compiler/            the toolchain                  driver/         host/runtime driver
tests/               benches (one source list per bench lives in scripts/py/xsim.py)
scripts/             tcl (ooc_*, synth), py (check, xsim, vlint, vstyle, deps, ...)
docs/                public design tree              docs-web/       public web docs
.plan/               internal working notes, progress, checklists (never public)
```

**Folder shape: highly nested, never flat.** Categorise into a proper hierarchy;
a directory with 40 sibling files is a smell. Nesting is the default, flattening
is the exception you justify.

**Verilog style** is enforced by `scripts/py/vstyle.py` over every `.v` file, and
the checks in `scripts/py/deps.py` — the framework never instantiates, includes,
or documents a project module (the only exception is a **slot**: a
parameter-guarded name, 0 by default, `xform_bank` / `khs_unit` /
`khs_scalar_decode`). Memory primitives are **named, never inferred**: BRAM/URAM
through `src/kohakuaccel/common/kohaku_sdpram.v`, FIFOs through
`common/sync_fifo.v` / `async_fifo.v` — inference makes both the resource cost
and the read latency depend on a tool heuristic, and read latency is a design
decision.

**Python style** — ruff + black over every directory; **no imports inside
functions** (all imports at module top); follow `CONTRIBUTING.md`.

**File I/O uses the builtin tools** (Read/Write/Edit/Grep/Glob), never shell
`cat`/`sed`/`head`/heredocs/redirection — enforced by a hook.

**Do not commit** unless explicitly asked.

## 4. RTL development rules

**1. RTL is not a software project.** Do not bring SWE reflexes to it —
iterate-in-production, ship-a-patch, hot-fix-one-line are all wrong here.

**2. "Fast draft" does not work, even before the FPGA.** A full place-and-route
past ~50% utilisation on the xcvu13p takes **30+ hours**. There is no cheap
round trip to lean on, so the discipline has to come from the design, not from
fast retries.

**3. One goal, one full loop:** implement the goal *in full, all at once* → then
review and audit *everything at once* → then simulation, behaviour verification,
and test-benches *at once* → and only after everything is fully settled, run the
OOC synth to read Fmax and resources. No stage begins before the previous one is
complete.

**4. Review and audit covers behaviour AND cost.** You are not done reviewing
when it is functionally right — you review for LUT usage and for Fmax the same
pass, before simulation.

**5. The OOC synth reports EVERYTHING, in one run.** No partial report. Put
everything on disk: every path's slack, every module's LUT usage hierarchically
down to the finest-grained submodule, and the full log (so a bad Vivado
behaviour is visible and avoidable). The same config is never synthesised twice —
one run catches it all.

**6. Bad numbers mean a new loop, not a patch.** If resources or Fmax say the
design needs work, *use the full report* to find every weak point, review and
audit them, then do a full re-design and re-plan, and start the loop at rule 3
again. Never "I think X is the cause, let me change one line and burn a 10-minute
sim and a 30-minute synth."

**7. Track the goal in `.plan/<current-goal>/`** — a checklist/todo you keep
current. Items in one goal have **no priority ordering that lets you skip**:
multiple items in the same goal are the same priority, done in a strict order, to
the same quality. Everything gets finished.

**8. Never ask "path A or B" under a fixed spec.** If the only difference besides
Fmax/LUT is how long it takes to build — build both and compare. If the
behaviour differs — build both and make it a knob. The answer to "should I choose
A or B" is always "both, configurable" (SASD vs SAMD, fabric bit-width, cache
width vs depth, …). Pick the optimal one, or ship both as options; do not ask.

**Traps that have cost real time:**
- **Unsized literals in concatenations** contribute 32 bits, not the field width.
- **A round trip cannot witness a layout** — `unpack(pack(x))==x` passes when
  both halves are wrong together. Witness byte order with a SHA of the packed
  bytes, diff new vs old across shapes and truncations.
- **A check that cannot fail on a broken design is not a check** — verify the
  property, not an exit code or a stdout grep.
- **Serial loops synthesise serially** — `if(!found&&x[i])found=1` over N bits is
  an N-level LUT chain; use smear-isolate-encode / mask-then-reduce.
- **Variable part-select writes** build a barrel mux across the whole register.
- **Paired parameters that must agree** with nothing checking them — derive one
  from the other.
- **`glbl` holds GSR for the first 100 ns** — unisim registers ignore everything
  before that.
- **`.bat`/`powershell -File` split args on `=`/`,`** — synth generics are
  `NAME:VALUE` joined by `+`.

Simulate with Vivado `xsim` through `scripts/py/xsim.py` (or `vlint.py` for a
lint); `check.py full` is the gate. OOC synth is `scripts/tcl/ooc_*.tcl`.

## 5. Documentation rules

`docs/` and `docs-web/` are **public**. Write for a reader who, after reading,
can become a core contributor or a high-level developer building on the platform.

- **The only thing you may assume** is that the reader knows what RTL, an FPGA,
  and Verilog are. Everything else is explained.
- **Proper structure** — highly nested, properly categorised, a real hierarchy.
- **Describe what exists, directly.** No history, no narrative of debugging: no
  "the bug we found and fixed", no "the issue we resolved", no "it used to cost X
  LUT and now costs Y". State what the design is and what it costs, now.

## 6. The Kohaku principles

Two hooks enforce these, each in two modes: **PreToolUse** (mid-round — checks
the last completed text block; the in-progress message is not on disk yet, so it
lags by one block) and **Stop** (the ending block). A SHA-1 guard makes each fire
at most once per drifting block.

**Practical workflow:** mid-round, prefer **batched tool calls with NO
descriptive text** — a pure tool-call message has no text block to check, so the
first-principle hook stays quiet. Write prose only to summarise, and end that
prose with the line. This avoids tripping the hook on every step.

**First principle.** End EVERY text block with the exact line
`following kohaku first principle`, placed **last, before any tool call**. A
block without it means you have drifted: re-read this file, reconcile, continue.

**Second principle — the goal.** While a goal is active, carry a
**Kohaku Second Principle — Goal** block just above the first-principle line:

```
## Kohaku Second Principle — Goal
- [x] <goal item one>
- [ ] <goal item two>
```

Mid-round (PreToolUse) the block needs only its header and at least the item you
are working on; the ending block (Stop) must carry the FULL list, each item
`[ ]` or `[x]`. Nothing is dropped, reordered, or rescoped; every item is the
same priority. The goal hook holds the canonical list (set when a goal ships) and
an `ACTIVE` arg; when every item is `[x]` it fires once telling you to CLOSE it by
setting `ACTIVE = False` (an arg, not removal from settings).
