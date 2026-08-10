# Simulation

How to run simulations, what a bench must do to be usable, and the tool traps that
have cost real time here. Per-suite detail lives with each module:

- [`docs/axi/simulation.md`](axi/simulation.md) — AXI4 slave and master benches
- [`docs/noc/simulation.md`](noc/simulation.md) — mesh, orchestrator, full system

---

## 0. The inner loop is `check.py`, not a runner

```
   python scripts/py/check.py fast     ~5 s    pure Python, no simulator at all
   python scripts/py/check.py unit     ~70 s   + the two RTL benches that catch
                                               most of what breaks
   python scripts/py/check.py full     ~6 min  every bench and the e2e sweep
```

Run `fast` after every edit, `unit` before believing anything works, and `full`
only before calling something done.

The tiering is not politeness about CPU time — it is about whether the question
gets asked at all. Nearly every bug in this design so far was catchable by
`fast` or `unit`, and reaching for `full` each time turns a ten-second question
into a five-minute one, which teaches you to stop asking it. `fast` is pure
Python because most of the driver is a pure function: building a control program
records transactions against a `RecordingTransport`, so it can be checked
without a simulator existing.

`unit` adds `mag_wslot` specifically — cheap, and it covers the one structure
that has now broken twice under concurrent clusters, both times presenting at
system level only as a GEMM that never finished.

A single RTL bench, by name — `xsim.py --help` lists them:

```
   python scripts/py/xsim.py cluster_node    # or mag_system, mag_wslot, acu, ...
```

`mag_driver_tb` is the exception: it is not in that list, because it is not a
self-contained bench. The driver builds its operand image and its control
program, so it is driven from Python instead:

```
   python scripts/py/run_matmul.py --m 64 --n 64 --k 128    # batch, ~16 s to elaborate
   python scripts/py/repl.py                                # keeps one snapshot warm
```

That inversion is the point. The bench does not know what a matmul is; it loads
files and dumps a result. A wrong control program therefore produces a wrong
answer *there*, which makes the run a test of the driver and not only of the
RTL. See [`isa/kernel.md`](isa/kernel.md).

**The compiler's own end to end** is the same simulation with the instructions
coming from the DSL instead of the driver:

```
   python scripts/py/run_dsl.py --m 64 --k 64 --n 64
```

A kernel is traced to a level 1 graph, scheduled into level 2 bands, lowered to
a level 3 program and encoded; **those flits are what the cluster executes**.
Everything else -- operands, upload regions, control program, RTL -- is the
driver's, so running this next to `run_matmul.py` on one shape separates a
compiler fault from a hardware one: the only difference is who emitted the
instructions. `src/ktpu/hw/fromdsl.py` is the single place the two planners are
reconciled, and `tests/ktpu/integration/test_machine_code.py` compares them flit
for flit through that same module.

The PowerShell runners still exist and are what the suite docs describe:

```powershell
.\tests\run_axi_sim.ps1        # AXI4 slave and master
.\tests\run_noc_sim.ps1        # mesh, orchestrator, CU framework
.\tests\run_matmul_sim.ps1     # matmul datapath and accumulator
.\tests\run_system_sim.ps1     # end to end through the NoC
.\tests\run_mag_sim.ps1        # MAG, main_orch, the driver
.\tests\run_synth_check.ps1 -Only <top>    # out-of-context Fmax + utilisation
```

Exit code 0 = pass. Each prints a `PASS`/`FAIL` banner per bench plus a summary.

> **Never start a second simulation while one is running.** They take an
> exclusive lock on the work directory; a second run fails and can corrupt the
> first one's scratch.

---

## 1. What you need

| Tool | Needed for | Why |
|---|---|---|
| **Vivado `xsim`** | everything | the only option for the NoC — the router instantiates `xpm_fifo_sync`, and the XPM library must be linked with `-L xpm`, which iverilog cannot do |
| **iverilog** | AXI only, optional | second opinion on the AXI RTL, see §3 |

`run_axi_sim.ps1` auto-detects iverilog and falls back to `xsim`. Force one with
`-Sim xsim` / `-Sim iverilog`. The NoC runner is `xsim`-only.

Vivado is expected at `D:\Xilinx\Vivado\2024.2\bin`; override with `-VivadoBin`.
Scratch goes to `%TEMP%` and is removed afterwards unless you pass `-KeepWork`.

---

## 2. Adding a bench

Both runners take a list near the top of the script. Add an entry:

```powershell
$benches = @(
    @{ Name = 'my thing'; Src = @("tests\noc\my_tb.v"); Top = 'my_tb' }
)
```

The design sources are already listed (`$rtl` in the NoC runner, `$suite` in the
AXI one); `Src` is only the extra files your bench needs. Each bench compiles into
its own library, so two benches may define modules with the same name.

Three conventions, each of which exists because its absence has burned someone:

**Print a `PASS`/`FAIL` banner.** The runner greps for it and reports
`no verdict found` when it is missing, rather than passing silently.

**Include a watchdog.** A protocol break usually manifests as a *hang*, not a wrong
value — AXI has no timeout, and a deadlocked network produces no answer rather than
a bad one. So a timeout is a first-class check, not a safety net. Both runners
special-case `WATCHDOG TIMEOUT` and print likely causes.

**Count checks and report the number.** `PASS` on its own cannot distinguish a
working bench from one that skipped everything.

---

## 3. Tool traps

**Vivado's `.bat` wrappers split arguments on `=`.** This silently breaks
`xelab -generic_top N=3` *and* `xvlog -d MESH_N=3` — the tool sees `3` as a separate
token and reports `cannot find design unit work.3`. `run_noc_sim.ps1` writes the
define into a generated source file instead. Do the same if you need a parameter
override.

**`powershell -File` converts every argument to a string**, so an array parameter
does not survive. `-Size 4,5,6` binds the string `"4,5,6"`, which PowerShell then
coerces to the integer **456** — and the runner cheerfully starts elaborating a
456×456 mesh. There is no error; it simply never finishes. Pass one size per
invocation from a non-PowerShell shell:

```bash
for n in 4 5; do powershell -NoProfile -File tests/run_noc_sim.ps1 -Size $n; done
```

From PowerShell itself, `.\tests\run_noc_sim.ps1 -Size 4,5,6` works as written.

**RTL has no `` `timescale ``** — correct for synthesis — so the elaborator needs
`-timescale 1ns/1ps`. Without it, xelab fails as soon as any file *does* have one.

**The NoC needs `-L xpm`**, because `xpm_fifo_sync` lives in the XPM library.

**iverilog is more permissive than Vivado, in ways that matter.** It accepts
use-before-declaration that `xvlog` rejects outright, and sizes implicit nets
differently: an undeclared identifier in an instance port list becomes a **1-bit**
net in Vivado (IEEE 1364-2005 §3.5). That is exactly how a 288-bit link in
`noc_router.v` silently became a 1-bit net with no driver. **Passing iverilog is
not evidence that Vivado will compile the file**, let alone synthesise it the same
way. Run both where you can.

**`sed` on Windows paths eats backslashes** — `\a` becomes a BEL character, `\h`
vanishes. Editing PowerShell paths with `sed` produced `srckohakuaxi<BEL>xi4_ram.v`
here. Python's `re.sub` has the same hazard *in the replacement string*. Use a real
editor, or build the backslash with `chr(92)` and a lambda replacement.

**A loop that only waits for a fall reads "already finished" as "not yet
started".** `mag_system_tb` waited for the mover with `while (mv_busy) ...` one
edge after issuing the command. While the command was a single wire, `busy` had
already risen and the loop worked. Moving the command behind AXI added a few
cycles, the loop found `busy` still low, **exited immediately, read the
destination mid-transfer, and its "mover never went idle" check passed
vacuously** — the failure surfaced as 32 words of `x`, several modules from the
cause. Wait for the rise (bounded) and *then* for the fall. Any bench that polls
a busy flag straight after issuing work has this latent, and it fails in the
direction of a false pass.

---

## 4. Reading a failure

Suite-specific symptoms are in the per-module docs. Generally:

| Symptom | Usually means |
|---|---|
| `WATCHDOG TIMEOUT` | a handshake is stuck — see the suite doc for which |
| `no verdict found` | the bench never printed `PASS`/`FAIL`, usually an early `$finish` |
| compile error only under `xsim` | permissive iverilog let something through, see §3 |
| passes standalone, fails in the runner | the bench depends on a module another bench also defines; check `Src` |

---

## 5. A verdict that cannot fail on the answer is not a verdict

**2026-08-10.** For a period, every green run in this project — simulator and
hardware alike — meant only *"it finished and produced finite numbers"*.

It surfaced on silicon. A `128x64x128` matmul on the shipped bitstream returned
**15,440 of 16,384 elements past 10% relative error** and reported `PASS`. The
underlying fault was a capacity mismatch (see [`driver/`](driver/README.md) and
`ktpu.hw.board`), but the fault is not the point — the point is what the verdict
did with it:

```
gates=True  pass=True   orchestrator finished    ORCH-OK
gates=True  pass=True   produced an answer       16384 elements
gates=False pass=False  vs mxfp7 model p50       9.65e-01     (limit 1e-3)
gates=False pass=False  vs mxfp7 model worst     3.20e+04
gates=False pass=False  elements over 10%        15440 of 16384
gates=False pass=False  vs fp64 p50              9.64e-01
                                                 verdict.pass = True
```

Every precision check ran. Every one of them failed. None of them was allowed to
decide anything, because `sim._verdict` computed `pass` over the `gates: True`
checks only, and precision carried `gates: False` across the board.

**The original rule was right about one check and over-applied to all four.** Not
gating on the *worst element* is correct: an output that cancels against its own
partial sums has a large relative error and a correct circuit, and a good
`256x64x256` run really does show `worst 2.03` while being right. But `p50` is a
median over the whole matrix, which cancellation cannot move — a correct run sits
at `1.7e-04` and a broken one at `9.6e-01`, five thousand times apart with
nothing in between. And `elements over 10%` was introduced for exactly this job;
its own `why` string reads *"the COUNT carries the signal: a lost sub-tile
corrupts 16 at once, which the worst element cannot distinguish from
cancellation."* The check written to carry the signal was the one not allowed to
act on it.

### The first fix was still a threshold, and thresholds are the wrong instrument

Gating `p50` and `elements over 10%` closed the hole, and it was the wrong
shape of answer. **An absolute error limit is a property of the OPERANDS, not
of the circuit.** Cancelling inputs raise every format's relative error
together, so any fixed limit can be beaten by choosing values — measured here:
zero-mean operands take the native format's `p50` from `3.2e-03` to `1.5e-02`,
which fails a *perfect* circuit against a 1e-3 limit.

**What replaced it: the format ladder.** `formats.ladder()` runs the same
matmul in every candidate format against FP64 and puts the machine's answer in
as one more column, so correctness is a *position* rather than a number. On
silicon:

```
    int7 + E5M3             3.332e-03  ...      dist to answer 3.049e-04
    mxfp7 model             3.252e-03  ...      dist to answer 1.699e-04  <- own
    THE ANSWER              3.280e-03  ...
```

Two gating measurements come off it, and neither is a fitted constant — both
are ratios or differences of quantities measured on the *same* operands, so
extreme inputs move numerator and denominator together:

| | meaning | correct | noise | lost sub-tile |
|---|---|---|---|---|
| `detach` | distance to its own model, over what that model costs vs FP64 | 0.052 | 411 | 0.000 |
| `excess` | elements past 10% that its own model does not have | +0 | +3884 | **+16** |

Both are needed and neither subsumes the other: a median cannot see 16 corrupt
elements in 4,096, and `excess` is what catches them. A perfect circuit scores
`0.000 / +0` on normal operands, on operands scaled by 1e6 until the FP16
output saturates, and on cancelling operands — the three cases a fixed limit
gets wrong. `tests/ktpu/hw/test_ladder.py` pins all of it.

The old figures are still printed, now with no limit attached at all. They are
worth reading; they were never worth gating on.

### The method, which is the transferable part

The tempting move was to explain the hardware result. The useful move was to ask
whether the hole was in the hardware path at all — and `sim.payload` is a pure
function of `(problem, answer, log)`, so the question can be put to it directly
with no machine anywhere:

```python
r = sim.payload(prob, rng.standard_normal((m, n)) * 100, ["ORCH-OK"], ...)
#  err_hw.rel.p50 1.3194e+00   over10 3959/4096   verdict.pass True
```

Pure noise passed. That one line separated *"a gap in the new hardware path"*
from *"this has always been true of every run"* — which are the same symptom and
completely different problems. **When a check lets something through, reproduce
it against the checker rather than against the thing that failed.**

### What it says about `payload` being pure

That `payload` could be interrogated this way is not luck; it is the property
§0 relies on. The same purity that lets `fast` score a run without a simulator
lets a bad verdict be reproduced without one. A scorer entangled with the
simulator would have had to be debugged through a fifteen-second elaboration,
and the "is the simulator affected too?" question could not have been asked at
all.
