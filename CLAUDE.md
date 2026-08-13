# KohakuTPU — working conventions

An FPGA TPU: compute units on a NoC mesh, reaching DRAM through AXI4. Target
part `xcvu13p-fhgb2104-2L-e` at **300 MHz**.

---

## Two places for writing, and they are not interchangeable

```
   docs/    durable, external-facing. Design intent, measured results, why.
   .plan/   internal working state. Git-ignored. Survives across sessions.
```

**`.plan/` is fully internal — track intermediate results there**, with a nested
structure rather than one flat pile:

```
   .plan/
     README.md          what this directory is
     macro-plan.md      the roadmap and where we are in it
     decisions.md       settled calls + reasoning, so they are not relitigated
     session-log.md     what changed each session, newest first
     measurements/      raw sweeps and intermediate numbers, one file per topic
```

Anything measured goes to `.plan/measurements/` as it is produced — Fmax sweeps,
utilisation runs, parameter studies, dead ends. `docs/` gets the *conclusion*
once it is settled. A number that only exists in a chat transcript is lost.

---

## Hard rules

**Memory primitives are named, never inferred.** BRAM and URAM are explicitly
instantiated through `src/common/kohaku_sdpram.v` (`xpm_memory_sdpram` with
`MEMORY_PRIMITIVE` set), the same way `src/common/sync_fifo.v` names
`FIFO_MEMORY_TYPE`. Never write a `reg` array and rely on synthesis to map it.
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

**Simulate with Vivado `xsim`**, through the runners in `tests/`. The iverilog
wrapper in the sibling `JTAG-DMA-test` repo is not for this project.

**Do not commit** unless explicitly asked.

---

## Running things

```
   .\tests\run_matmul_sim.ps1                  matmul datapath + accumulator
   .\tests\run_noc_sim.ps1                     mesh, orchestrator, CU framework
   .\tests\run_system_sim.ps1                  end-to-end through the NoC
   .\tests\run_axi_sim.ps1                     AXI slave/master
   .\tests\run_synth_check.ps1 -Only <top>     out-of-context Fmax + utilisation
```

Synth generics are `+`-separated `NAME:VALUE`, e.g.
`-Generics "ACC_MW:14+DEPTH:512"`. Benches run against both `MODEL=1`
(behavioural) and `MODEL=0` (real `DSP48E2`) so a failure is attributable.

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
  mask-then-reduce. See `docs/compute/accumulator.md` §4.1.
- **Variable part-select writes** build a barrel mux across the whole register.
  `buf[(i*32 + {ctr,3'd0} + k)*7 +: 7] <=` cost 32,292 LUTs until the loop was
  unrolled over the counter. See `docs/compute/matmul-impl.md` §3.1.
- **Paired parameters that must agree** and nothing checks them — `DEPTH` and
  `TAW` in `mx_acu_fp` silently gave a 16-entry tile at `DEPTH=512`. Derive one
  from the other.
- **`glbl` holds GSR for the first 100 ns**, so unisim registers ignore
  everything before that regardless of the design's own reset.

---

## Orientation

```
   src/kohakunoc/    mesh, routers, orchestrator, CU framework
   src/kohakutpu/    compute: matmul datapath, accumulator, cluster
   src/kohakuaxi/    AXI4 slave/master
   src/common/       sync_fifo, kohaku_sdpram
   src/synth_top/    synthesis-only wrappers for measurement/goal
```

Start at `docs/README.md`. The active compute design is
`docs/compute/tensor-isa.md` and `docs/compute/matmul.md`.
