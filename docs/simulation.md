# Simulation

How to run simulations, what a bench must do to be usable, and the tool traps that
have cost real time here. Per-suite detail lives with each module:

- [`docs/axi/simulation.md`](axi/simulation.md) — AXI4 slave and master benches
- [`docs/noc/simulation.md`](noc/simulation.md) — mesh, orchestrator, full system

```powershell
.\tests\run_axi_sim.ps1     # AXI4
.\tests\run_noc_sim.ps1     # NoC
```

Exit code 0 = pass. Each prints a `PASS`/`FAIL` banner per bench plus a summary.

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
