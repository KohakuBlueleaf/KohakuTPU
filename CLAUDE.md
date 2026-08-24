# KohakuTPU — working conventions

An FPGA TPU: compute units on a NoC mesh, reaching DRAM through AXI4. Target
part `xcvu13p-fhgb2104-2L-e` at **300 MHz**.

---

## Where writing goes

`docs/` is the durable, external-facing tree: design intent, measured results,
and why. It gets the *conclusion* once it is settled, and the RTL is the source
of truth it is checked against. A number that only exists in a chat transcript
is lost.

---

## Hard rules

**Memory primitives are named, never inferred.** BRAM and URAM are explicitly
instantiated through `src/kohakuaccel/common/kohaku_sdpram.v`
(`xpm_memory_sdpram` with `MEMORY_PRIMITIVE` set), the same way
`src/kohakuaccel/common/sync_fifo.v` names `FIFO_MEMORY_TYPE`. Never write a
`reg` array and rely on synthesis to map it.
Inference makes both the resource cost *and the read latency* depend on a reset
clause or a tool heuristic — and read latency sets pipeline depth, which is a
design decision, not a synthesis outcome.

**File I/O uses the builtin tools.** Read / Write / Edit / Grep / Glob — never
`cat`, `sed`, `head`, heredocs or shell redirection. Enforced by a PreToolUse
hook.

**A round trip cannot witness a layout.** `unpack(pack(x)) == x` passes just as
well when pack and unpack are wrong *together* — and every layout here is a
permutation, so composing one with its own inverse hides an error in either
half. Rewriting one side for speed is exactly the change that breaks them
together. Witness a byte order with a **SHA of the packed bytes**, taking the
digest from the implementation being replaced, and diff new against old across
shapes *and* truncations before replacing anything. Non-divisible shapes,
`(1, 1)` and half-written buffers are where they part. Applies to every
`pack`/`unpack` pair — `Entry`, `Tile`, `ConvEntry` and whatever comes next.
A layout is the one artefact in the stack that cannot report its own mistakes:
an operand packed wrong is the right bytes in the wrong places, and every unit
downstream accepts it.

**A check that cannot fail when the thing is broken is not a check.** Two of
these ran on the same afternoon and both reported success while the property
was false. `python $f >/dev/null 2>&1 && echo ran` was asserting on an EXIT
CODE — and a script that opens a card exits 0 exactly like one that opens the
simulator, so it could not see the failure it was written for. Grepping stdout
for `SimDevice` was no better: two examples never print their device, so they
read as failures while being correct. **Verify the property, not a proxy for
it.** For "this never touches hardware", booby-trap the card path so an open
RAISES, then confirm the trap itself fires — a check you have not seen fail is
an assumption. A test that would pass on a deleted feature is testing nothing.

**A failure count says nothing about the size of the cause.** Removing `FP16`
from what `rt.py` re-exports took out **96 tests, a third of the suite**, and
the fix was restoring one name to one import. A module other modules import
from has an API whether or not anyone wrote it down, so deleting a name there
is an interface change, not a tidy-up. **Read the first traceback before
reacting to the count** — a cascade reads as catastrophe and is usually one
line, and the instinct to revert a whole change costs more than the diagnosis.
Announce renames of exported names; that one reached a file its author did
not own.

**The framework never names a project.** `src/kohakuaccel/` is the framework;
`src/kohakutpu/` and `src/kohakumpe/` are projects built on it. Nothing under
`kohakuaccel/` may instantiate, include or document a module that only exists
under a project tree — a framework that does is not reusable, it is one
accelerator with its parts in two directories. The same rule governs the docs:
framework-level pages describe the mechanism, never a project's workload.

**It holds for the RTL, and `scripts/py/deps.py` measures it** — in the standard
check suite, reading instantiations rather than build lists, so adding a file to
a list cannot hide an edge. The last violation was the SIMD unit reaching into
`src/kohakutpu/` for its float arithmetic. It is `src/kohakumpe/simd/` now: the
whole unit moved to the project, because project→project is fine and the float
tier was not separable from the int tier without inventing a seam.

What survives is the **slot** — a module the framework names behind a parameter
that is 0 by default, so a framework-only build never elaborates one and the
name need not resolve. There are three: `xform_bank` at MAG's transform stage,
and `khs_unit`/`khs_scalar_decode` at the CPU PE's `SIMD_EN`. A slot is the only
shape in which `kohakuaccel/` may mention what it does not own. Do not add one
without saying so; `deps.py` will refuse it otherwise, which is the point.

**Simulate with Vivado `xsim`**, through `scripts/py/xsim.py`. The iverilog
wrapper in the sibling `JTAG-DMA-test` repo is not for this project.

**Do not commit** unless explicitly asked.

---

## Running things

```
   python scripts/py/check.py [fast|unit|blocks|e2e|full] [-j N]
   python scripts/py/check.py full --counts build/counts.json
   python scripts/py/xsim.py <bench> [--model 0|1] [-d DEFINE]
   python scripts/py/vlint.py <bench> [--tb]
   python scripts/py/vstyle.py [--lines] [--show] [paths...]
   python scripts/py/docpaths.py            # doc citations against the tree
   python scripts/py/specparams.py          # spec/parameters.md against the RTL
```

`check.py full` is the gate: every bench in `xsim.py`, plus ruff and black over
every directory, the two pytest suites, the Verilog style rules over all 391
`.v` files, and the two doc checks. `--counts` records the numbers each bench
printed and
`--counts-baseline` fails the run when any of them moved, which is what a
refactor or a reformat has to clear — a green suite does not say a count held.

**A doc is checkable where it names something.** `docpaths.py` compares every
repo path a doc cites against the tree; `specparams.py` compares
`docs/spec/parameters.md` against the `parameter` declarations of the modules it
claims to describe. They found 103 and 42 defects respectively on first run,
including `ADDR_W` documented as 34 across five tables when the RTL is 40.

Benches run against both `MODEL=1` (behavioural) and `MODEL=0` (real
`DSP48E2`) so a failure is attributable. Out-of-context synthesis is
`scripts/tcl/ooc_*.tcl`; synth generics are `+`-separated `NAME:VALUE`, e.g.
`-Generics "ACC_MW:14+DEPTH:512"`.

---

## Traps that have cost real time

- **Unsized literals in concatenations** contribute 32 bits, not the field
  width. `(BASE + i*4) * 32` into a 34-bit field silently shifts every field
  below it. Use an explicitly sized `reg`.
- **Vivado's `.bat` wrappers split arguments on `=`**, and `powershell -File`
  splits string arguments on `,`. Hence `NAME:VALUE` joined by `+`.
- **Serial loops synthesise serially.** `if (!found && x[i]) found = 1` over 25
  bits is a 25-level LUT chain inside one pipeline stage, and no amount of
  pipelining around it helps. Searches use smear-isolate-encode; sticky bits use
  mask-then-reduce. See `docs/projects/kohakumpe/simd/accumulator.md` §4.1.
- **Variable part-select writes** build a barrel mux across the whole register.
  `buf[(i*32 + {ctr,3'd0} + k)*7 +: 7] <=` cost 32,292 LUTs until the loop was
  unrolled over the counter. See `docs/projects/kohakutpu/matmul.md` §3.1.
- **Paired parameters that must agree** and nothing checks them — `DEPTH` and
  `TAW` in `mx_acu_fp` silently gave a 16-entry tile at `DEPTH=512`. Derive one
  from the other.
- **`glbl` holds GSR for the first 100 ns**, so unisim registers ignore
  everything before that regardless of the design's own reset.

---

## Orientation

```
   src/kohakuaccel/          THE FRAMEWORK
     noc/                    mesh, routers, orchestrator, CU base
     sysnode/                system node: MAG, mover, control processor,
                             interlink. NEVER call it "node" -- a NoC
                             endpoint is a node too
     pe/rv32/                the CPU PE. `SIMD_EN` names an extension it
                             does not own -- a slot, filled by kohakumpe
     axi/                    station bus, links, AXI plumbing
     common/                 sync_fifo, kohaku_sdpram, sb_skid
     verif/                  axi_ram and other bench-only models
   src/templates/            the framework's worked examples: CU, transform
                             occupant, endpoint adapter, each with a bench
   src/examples/saxpy/       the example project, RTL half
   src/kohakutpu/            the reference accelerator: matmul, vector,
                             transform occupants, generated tops
   src/kohakumpe/            the SIMT PE, and simd/ -- the SIMD unit that
                             fills the framework's SIMD_EN slot
   src/reference/            reference and proof-of-concept copies. Only
                             intcluster/ and legacy-cu/ are still compiled by
                             a bench; nothing ships from any of it
   src/attic/                dead
```

Start at `docs/README.md`. Benches run through `scripts/py/xsim.py` — one
source list per bench, in one place. The PowerShell runners in `tests/attic/`
are dead, and so are the ones in `tests/axi/build-jtagdbg/`: that directory is
a frozen copy of the station-bus RTL with three benches of its own, and only
`sb_conv12_tb.v` is reached from `xsim.py` (against the LIVE sources, not the
copies). House style is `CONTRIBUTING.md`.
