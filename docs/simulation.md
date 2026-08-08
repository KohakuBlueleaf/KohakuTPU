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

---

## 4. Reading a failure

Suite-specific symptoms are in the per-module docs. Generally:

| Symptom | Usually means |
|---|---|
| `WATCHDOG TIMEOUT` | a handshake is stuck — see the suite doc for which |
| `no verdict found` | the bench never printed `PASS`/`FAIL`, usually an early `$finish` |
| compile error only under `xsim` | permissive iverilog let something through, see §3 |
| passes standalone, fails in the runner | the bench depends on a module another bench also defines; check `Src` |
