---
title: RV64 system core performance
summary: What it costs and what it achieves — LUT and frequency out of context and inside the node, Dhrystone and IPC, what each structure buys, and the fabric-latency sweep.
tags:
  - architecture
  - cpu
  - rv64
  - performance
---

# RV64 system core performance

What the processor costs and what it achieves. The short form — and **each row
names the run it came from, because rows from different runs may not be
compared, subtracted or averaged**, which is the discipline the rest of the page
is about:

| | | from |
|---|---|---|
| the processor where it ships | **7,244 LUT**, 5,776 FF, 12 RAMB36 + 2 RAMB18, 1 URAM, 4 DSP | `sysnode` whole, `rebuilt` |
| the node it sits in | **32,859 LUT**, **WNS +0.039 ns** at a 3.333 ns request — **300 MHz is met, with 0 failing endpoints, in out-of-context synthesis** | the same run |
| where the processor's area is | **7,334 LUT** total: 1,304 of CSR file, 1,366 of multiply–divide, 2,201 of forwarding, hazards and the trap path, 151 of MMU, 211 of register file | `rv64_syscore` as its own top, `none` |
| what it executes | **1.331 DMIPS/MHz** at **IPC 0.805** on Dhrystone, unchanged across the fabric port | Verilator |
| what none of it is | **routed.** Every frequency here is out-of-context synthesis and there is no measurement from silicon | — |

The node's own figure and the node's own budget belong to
[sysnode](../../sysnode/README.md); it is quoted here only because the
processor's timing is measured inside it.

## How to read every number on this page

**Resource and frequency figures.** Part `xcvu13p-fhgb2104-2L-e`, speed grade
`-2L`, **Vivado 2024.2**, **out-of-context synthesis**, at a
**3.333 ns (300 MHz) request** throughout — the default in `ooc_syscore.tcl`,
passed explicitly. **What differs between tables is the measurement context, and
[that distinction is the first thing to read](#a-module-does-not-have-a-lut-count).**
Two scripts produce them:

- `scripts/tcl/ooc_syscore.tcl <top> <period> <mem_prim> [HIER]` — one module or
  one whole configuration as the top, standalone. Without `HIER` it synthesises
  at `-flatten_hierarchy rebuilt`, which is the ship flow; with `HIER` it
  switches to `none`, which is the flow whose hierarchical rows are an
  attribution;
- `scripts/tcl/ooc_sysnode_rv64.tcl <ports>` — `sysnode` whole with
  `CPU_RV64 = 1`, which writes `build/node_sn64_p2_*.rpt`.

> **No Fmax on this page is a closed-timing figure.** Every one is a
> **synthesis** result: pre-placement, pre-route. Synthesis slack is optimistic
> — one module in this tree lost **0.740 ns** going from synthesis to routing —
> so treat every frequency here as an upper bound and nothing else. There is no
> routed result for any of these tops, and no measurement from silicon. See
> [the measurement discipline](../../physical/measurement.md).

Resource figures are CLB LUT **sites**, as Vivado reports them after synthesis
and before `opt_design`, which typically lowers the final count.

**Cycle figures** come from **Verilator 5.020** driving a C++ harness — no
Verilog testbench — via `python scripts/py/vlt.py <top> --cc <harness>`.
Harnesses are in `sim/verilator/harness/`; programs are in `tests/rv64/`, built
with `riscv64-unknown-elf-gcc` 13.2.0 at `-O2`. Never quote a LUT or an Fmax
from a simulator, and never quote a cycle count from synthesis.

## A module does not have a LUT count

It has one **per measurement context**, and this page carries several. Read this
section before using any figure below, because it is the easiest way to draw a
wrong conclusion from two correct numbers.

Five things vary independently, and changing any one of them changes the answer:

| what varies | the two values in circulation here |
|---|---|
| **what was the top** | the module synthesised **standalone as its own top**, or measured as a **sub-hierarchy inside a larger synthesis** |
| **the flatten mode** | `-flatten_hierarchy rebuilt`, which lets synthesis re-parent leaves across boundaries, or `none`, which keeps them |
| **how it was counted** | a **hierarchical** report row, or a flattened count of the whole top |
| **the timing request** | this page is 3.333 ns throughout, but a tighter request buys LUT and not always megahertz |
| **the RTL vintage** | the same configuration at two points in the design's history — the *only* one of the five where the two figures are legitimately comparable |

Three worked instances of this on this page, all real and all from the current
RTL:

- **`rv64_syscore` is 7,334 LUT as its own top at `none`, and 7,244 LUT as a
  sub-hierarchy of `sysnode` at `rebuilt`.** Two contexts *and* two flows. That
  they land within 1 % of each other is a coincidence and not a confirmation:
  neither number was derived from the other and there is no reason they should
  agree.
- **`rv64_regfile` is 211 LUT at `none` and 1,555 LUT in the node's `rebuilt`
  hierarchical row.** Not a growth — re-parenting, worked through
  [below](#why-u_rf-is-the-largest-row-and-why-it-is-not-the-register-file).
- **`rv64_csr` is 1,304 LUT at `none` and 1,616 in the node's `rebuilt` row.**
  Same design, same run family, 24 % apart, for the same reason.

The rule that follows, and it is the whole of this section:

> **A figure from a different measurement context is not a newer or an older
> number. It is a different number.** Cite the context with the figure; where two
> exist, give both and say what each one measures. **Never present one as
> superseding the other, and never subtract across them.**

The exception is the last row of the table — **two runs of the same
configuration in the same context at different RTL vintages *are* comparable**,
and there the earlier figure genuinely is superseded. Telling that case apart
from the others is what the
[memory-profile fingerprint](#how-the-two-runs-were-identified-as-the-same-configuration)
is for.

This also settles the cross-core case. `ooc_syscore.tcl` uses
`-flatten_hierarchy rebuilt`; the RV32 PE's `ooc_rv_pe.tcl` uses `none`. On
`rv64_core` the same design measured **6,012 LUT at `none` against 5,824 at
`rebuilt`** — a 3.2 % spread that is the flow and not the design. **Do not
subtract an RV32 figure from an RV64 one**, and do not build a two-core table
out of rows taken under different flows without saying so in the table.

## Standalone, as its own top

### Where the processor's area is — the attribution run

`scripts/tcl/ooc_syscore.tcl rv64_syscore 3.333 block HIER`,
`xcvu13p-fhgb2104-2L-e`, Vivado 2024.2, **`-flatten_hierarchy none`**,
**synthesis, not routed**, at a 3.333 ns request. `MEM_PRIM = block` sets the
instruction window, the L1 and the TLB; the core's register file stays at its
`RF_PRIM` default of `distributed` and the scratchpad at `SPAD_STYLE = ultra`.

**`none` is the flow whose rows mean something and `rebuilt` is the flow that
ships.** Read this table for *where the area is* and never as the design's area.

| instance | LUT | FF | what it is |
|---|---:|---:|---|
| **`rv64_syscore`** | **7,334** | **5,856** | the whole processor |
| `(rv64_syscore)` | 464 | 889 | the wrapper: host window, handshake, address decode, control region, return mux |
| `u_core` | 5,944 | 3,218 | the pipeline |
| ` (u_core)` | 2,201 | 1,245 | forwarding, hazards, load align, **the trap path** |
| ` u_alu` | 539 | 0 | one adder, one shifter |
| ` u_bp` | 205 | 223 | the predictor's logic; its tables are block RAM |
| ` u_csr` | 1,304 | 987 | the CSR file, privilege and trap state |
| ` u_dec` | 118 | 0 | the decoder |
| ` u_md` | 1,366 | 633 | multiply and divide, plus 4 DSP |
| ` u_rf` | 211 | 130 | the register file **as it actually is** |
| `u_l1` | 349 | 499 | the cache |
| `u_mbox` | 138 | 308 | the dispatch mailbox |
| `u_mmu` | 151 | 217 | TLB and page-table walker |
| `u_np` | 288 | 725 | the node-port arbiter |

Both levels sum exactly — the five module rows plus the wrapper make 7,334, and
`u_core`'s seven children make 5,944 — which is the audit to apply to any
breakdown before trusting it.

Three readings worth taking from it:

- **The CSR file and the multiplier are the two big blocks**, at roughly 1,300
  LUT each, and the trap-and-forwarding glue in the parent is larger than
  either. Architecturally visible state and the hazard network are where a core
  of this shape spends.
- **`u_rf` is 211 LUT.** That is what 4 Kbit of mirrored LUTRAM costs. Every
  four-figure number attached to this instance elsewhere on the page is
  re-parenting, not the array.
- **`u_mmu` is 151 LUT and `u_mbox` 138.** Translation and the whole way onto
  the fabric together are under 4 % of the processor.

**Worst path in that run: −0.465 ns at 14 logic levels**, from the register
file's read, through the forward mux, the address adder, the misalignment test,
the trap decision and the vector select, into the program counter. That is
3.333 + 0.465 = **3.798 ns achieved, a 263.3 MHz upper bound in synthesis** —
and it is the path the design already names: there is no address-generation
stage between E and M, so the adder and the trap decision share a cycle
([microarchitecture](microarchitecture.md#why-there-is-no-address-generation-stage)).
**At the node, in the ship flow, nothing fails and this path is not the binding
one.**

### The `rebuilt` standalone runs, and what they are

> **These two rows were not re-run for the current RTL.** They are the last
> `rebuilt` standalone measurement of each configuration, taken before
> privilege, Sv39, fetch translation and the dispatch mailbox were built, and
> the CSR file alone has grown since. **Do not quote them as current area or
> current frequency.** The current figures this page can support are the `none`
> table above and the node run [below](#in-context-inside-the-system-node).

`scripts/tcl/ooc_syscore.tcl <top> 3.333 block`, same part, same tool,
`-flatten_hierarchy rebuilt`, synthesis, not routed.

| top | LUT | logic | LUTRAM | FF | BRAM | URAM | DSP | ctrl sets | Fmax | slack |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `rv64_sys_pe` | 6,360 | 6,024 | 336 | 4,394 | 6 | 1 | **4** | 87 | 316.3 | +0.171 |
| `rv64_syscore` | 6,349 | 6,193 | 156 | 5,095 | 12 | 1 | **4** | 87 | 307.8 | +0.084 |

Both configurations carry **4 DSP48**, because both instantiate the same
`rv64_muldiv` — the four-step 32 × 32 reuse. Any table that shows a DSP count
for one and a blank for the other is wrong, and that has stayed true across
every vintage.

At that vintage the two landed within 11 LUT of each other and spent it
differently: the mesh compute unit's in LUTRAM (the shell's receive queue at
`RECV_MEM = "distributed"`), the node processor's in logic and in twice the
block RAM (a 32 KB instruction window against 16 KB, plus the L1 and the TLB).

### Where `rv64_sys_pe`'s 6,360 LUT went, at that vintage

The hierarchical breakdown of the standalone run, and it **sums to its own
total** — which is the check to apply before trusting any breakdown at all.

| instance | LUT | FF | what it is |
|---|---:|---:|---|
| top glue | 16 | 458 | the `CU_DATA` loader, the kick machine, the counters |
| `u_base` | 690 | 1,061 | `noc_cu_base` — the framework attach every compute unit carries |
| `u_core` | **5,585** | 2,875 | the pipeline |
| `u_imem` | 69 | 0 | the instruction window's address glue; the array itself is block RAM |
| **total** | **6,360** | **4,394** | |

Inside `u_core`:

| instance | LUT | what it is |
|---|---:|---|
| core glue | 1,031 | forwarding muxes, hazard logic, load align and extend, the trap path |
| `u_bp` | 257 | the predictor |
| `u_csr` | 775 | the CSR file and trap state |
| `u_md` | 1,352 | multiply and divide, plus 4 DSP |
| `u_rf` | **2,170** | the register file — **see below** |
| **total** | **5,585** | |

> **If a breakdown misses its own total by a wide margin, rows from two
> different runs have been mixed. Do not publish it.** This is the cheapest
> possible audit of a figure table and it catches the most common way one goes
> wrong. The tolerance is not quite zero —
> [a `rebuilt` breakdown can overshoot its parent by a few
> cells](#the-cores-internals-and-why-this-table-is-a-shape-and-not-an-attribution).

**Block RAM reconciles in tiles, not instances.** This unit is 5 RAMB36 plus
2 RAMB18, which is **6 tiles** — 5 whole plus a half-pair. Summing the RAMB36
column alone reads 5 and looks like an error.

#### Why `u_rf` is the largest row, and why it is not the register file

2,170 LUT is the biggest single entry in that table — larger than the multiplier
and divider together. **It is not what the register file costs.**

The array is 31 × 64 bits held as two mirrored banks: **4 Kbit of storage**,
which at `distributed` is on the order of a hundred LUTs of LUTRAM and is
reported as such — the node's own run shows 80 LUTRAM on this instance. Nothing
about 4 Kbit of storage costs two thousand LUTs.

What has happened is `-flatten_hierarchy rebuilt` **re-parenting**: synthesis
dissolves module boundaries to optimise across them, and the leaves it produces
are attributed to whichever instance boundary survives nearest. The direct
evidence is in the same reports — **`u_alu` and `u_dec` have no row at all**.
Their logic did not vanish; it landed on neighbouring instances, and `u_rf` sits
directly between the register file read and the E stage that consumes it, which
is exactly where the forwarding muxes and the operand select live.

So read that row as *the register file plus the operand path around it*, and
take the array's own cost from a run at `none` instead — **211 LUT** inside
`rv64_syscore`, or **147 LUT at `distributed` and 67 LUT with 2 RAMB18 at
`block`** with `rv64_regfile` as its own top. The same caution applies to every
row in a `rebuilt` breakdown; it is the reason `ooc_syscore.tcl` offers a `HIER`
argument that switches the run to `-flatten_hierarchy none` when the question is
attribution rather than area.

### Two `rebuilt` vintages of `rv64_syscore`

A figure of **6,335 LUT / 4,968 FF / 331.1 MHz, slack +0.313** is in circulation
for `rv64_syscore` alongside the **6,349 / 5,095 / 307.8 MHz** in the table
above. They are the **same configuration in the same context** at two states of
the RTL, so unlike the pairs elsewhere on this page these two *are* comparable —
and **both are now superseded**, because neither has been re-run since privilege
and Sv39 were built.

**What changed between those two runs is not established.** Neither the RTL
delta nor whether the earlier run used identical generics has been confirmed, so
the pair is recorded and not explained. It is not evidence of a regression and
should not be described as one; an unexplained 23 MHz delta, flagged, is worth
more than a confident story about it.

### How the two runs were identified as the same configuration

Worth stating as a method, because it is reusable by anyone auditing these
numbers:

| | recorded earlier | the later run |
|---|---:|---:|
| LUT | 6,335 | 6,349 |
| FF | 4,968 | 5,095 |
| **BRAM · URAM · DSP** | **12 · 1 · 4** | **12 · 1 · 4** |
| Fmax | 331.1 | 307.8 |

**Block RAM, UltraRAM and DSP track the *configuration* and barely move with
RTL vintage; LUT and FF track the logic and move constantly.** So the memory and
DSP columns fingerprint *which build* a figure came from where LUT and Fmax
cannot. Identical memory profile with LUT and FF drifting a couple of hundred is
the signature of one configuration at two vintages. A *different* memory profile
means a different configuration or a different context, and the two figures are
then not comparable at all.

### The component modules

Measured the same way, each as its own top.

| top | LUT | FF | BRAM | DSP | Fmax |
|---|---:|---:|---:|---:|---:|
| `rv64_core` | 5,264 | 2,811 | 2 | 4 | 312.6 |
| `rv64_l1` | 332 | 455 | 2 | 0 | 502.8 |
| `rv64_mmu` | 96 | 186 | 1 | 0 | 436.9 |

**The MMU is double figures because the card is 40-bit physical**, which is a
result rather than an accident: the arithmetic that keeps a TLB entry at 57 bits
keeps it inside a block-RAM port, and the architectural 44-bit PPN would have
made it 73 bits and silently spent hundreds of LUT instead
([memory-system](memory-system.md#an-entry-is-57-bits-because-the-card-is-40-bit)).
The L1's 332 LUT is the same shape of result: per-line `valid` and `dirty` ride
in the tag word rather than in flop arrays.

These three rows were taken across the core's development rather than in one
run, and **the MMU row predates fetch translation, the shared port and the
poisoned-page fault** — the same module measured inside `rv64_syscore` at `none`
is 151 LUT today. So **do not subtract them from any configuration above**: the
per-module rule at the top of this page applies within the tree as well as
across it.

## As a sub-hierarchy inside a larger synthesis

The same modules, measured where they actually ship. **These are not newer or
older than the standalone rows; they answer a different question** — what the
instance costs inside the design that contains it, with cross-boundary
optimisation and re-parenting both in play.

The two rows below are **the same earlier vintage** as the `rebuilt` standalone
table and were not re-run; the current in-context measurement is
[the node run](#in-context-inside-the-system-node). They are kept because the
*comparison* they support is a property of the flow rather than of the RTL.

| instance | module | context | LUT | FF | BRAM | URAM | DSP | Fmax |
|---|---|---|---:|---:|---:|---:|---:|---:|
| `rv64_sys_pe` | `rv64_sys_pe` | in-system hierarchical | 6,772 | 4,271 | 6 | 1 | **4** | 315.7 |
| `rv64_mag_pe` | `rv64_mag_pe` | as its own top, transform config tied off | 14,851 | 15,839 | 20 | 1 | 39 | 281.5 |

Two readings that a careless comparison gets wrong:

- **`rv64_sys_pe` at 6,772 against 6,360 standalone is not staleness, and is not
  a change.** These are the *same RTL* measured two ways: the in-system figure
  includes what the surrounding design does to the instance's boundary, the
  standalone figure does not. Note the memory profile is identical — 6 BRAM,
  1 URAM, 4 DSP — which is the fingerprint that says so.

  **`[unverified]`**: [arch/cpu](../README.md#what-each-one-costs) records the
  same 6,772 · 315.7 MHz as a *standalone* run of an older vintage, and the
  slack and Fmax quoted with it are the tell of a run whose top it was. The two
  pages disagree, the disagreement has not been resolved by re-running it, and
  it is left visible rather than reconciled by picking one.
- **`rv64_mag_pe`'s row was taken with the transform slot's config port tied
  off**, which is how the RTL instantiates it, so synthesis strips the
  configuration path. The same instance measured inside the node is larger —
  [in context](#in-context-inside-the-system-node), and
  [integration](integration.md#the-node-complex--rv64_mag_pe).

## In context, inside the system node

**This is the current measurement of the processor as it ships.** From
`build/node_sn64_p2_hier.rpt`, `build/node_sn64_p2_util.rpt` and
`build/node_sn64_p2_time.rpt` — one run of `scripts/tcl/ooc_sysnode_rv64.tcl 2`
on 2026-08-26, `sysnode` as the top with `CPU_RV64=1 PORTS=2 STAGE=1 ILINK=1
STAGE_AT_PORT=1 PE_IMEM=8192 PE_SPAD=4096 PE_L1_LINES=64`,
`-flatten_hierarchy rebuilt`, design state **Synthesized**, and
`report_utilization -hierarchical -hierarchical_depth 4`.

**This is the last run of that day, and it is the one that includes the
doorbell-window, staging-strobe and cross-mesh-landing fixes.** An earlier run
the same day reports the node a few tens of LUT smaller; the three reports here
carry one timestamp and reconcile with each other, which is the test to apply
when two figures for "the node" are in circulation.

| instance | module | LUT | FF | RAMB36 | RAMB18 | URAM | DSP |
|---|---|---:|---:|---:|---:|---:|---:|
| `sysnode` | the whole node | **32,859** | 46,436 | 54 | 7 | 65 | 47 |
| `g_rv64.u_pe` | `rv64_mag_pe` | **16,010** | 16,458 | 20 | 2 | 1 | 47 |
| ` u_cpu` | `rv64_syscore` | **7,244** | 5,776 | 12 | 2 | 1 | 4 |
| `  (u_cpu)` | wrapper glue | 252 | 830 | 0 | 0 | 1 | 0 |
| `  u_core` | `rv64_core` | **6,169** | 3,205 | 1 | 2 | 0 | 4 |
| `  u_imem` | the instruction window | 1 | 0 | 8 | 0 | 0 | 0 |
| `  u_l1` | `rv64_l1` | 501 | 499 | 2 | 0 | 0 | 0 |
| `  u_mbox` | `rv64_noc_mbox` | 76 | 300 | 0 | 0 | 0 | 0 |
| `  u_mmu` | `rv64_mmu` | **103** | 217 | **1** | 0 | 0 | 0 |
| `  u_np` | `rv64_nport` | 142 | 725 | 0 | 0 | 0 | 0 |

`u_cpu`'s seven rows sum to 7,244 exactly, and the node's three top-level
children — `rv64_mag_pe` 16,010, `sn_hub` 514, `mag` 16,335 — sum to 32,859
exactly.

The node's own figure and the node's own budget belong to
[sysnode](../../sysnode/README.md). What this page owns is the 7,244 of it that
is the processor: the mover (4,226) and the transform slot (4,540) make up the
rest of `rv64_mag_pe` and belong to the node whatever processor sits in it.

**`u_mmu` at 103 LUT with one block RAM is translation as built.** The TLB array
is present in the netlist, which it was not while `priv` was tied to machine
mode — with `enabled` a constant, every output the array fed was dead and
synthesis removed all of it, leaving the power-on sweep counter. Reaching the
same module from software is what put the array back, and it costs 103 LUT and
one RAMB36 to serve both fetch and data.

**`(u_cpu)` carrying 251 LUT is the useful shape here.** The wrapper — host
window, access handshake, address decode, control region, return-path mux, and
now the fetch page register — is lean. Any area argument about this complex that
starts with the wrapper is looking in the wrong place; it is a core problem.

### Timing: the node meets 300 MHz in synthesis

From `build/node_sn64_p2_time.rpt`, same run:

| | |
|---|---|
| request | 3.333 ns (300 MHz) |
| WNS | **+0.039 ns** |
| failing endpoints | **0**, of 124,100 — *"all user specified timing constraints are met"* |
| achieved period | 3.333 − 0.039 = **3.294 ns**, a **303.6 MHz** upper bound |
| worst path, and it **passes** | `rv64_core`'s `wb_val_reg[1]` → `halt_cause_reg[1]`, **12 logic levels** |

**Say this one as carefully as the failing version was said.** The claim the
evidence supports is:

> **300 MHz is met out of context, in synthesis, on this part at this speed
> grade.** It is **not** closed timing. There is no placed-and-routed result for
> this node and no measurement from silicon.

The distinction is not pedantry: **synthesis slack is optimistic, and a module
in this tree lost 0.740 ns going from synthesis to routing** — six times the
margin this run has. A design that passes synthesis by 0.039 ns should be
expected to fail routing until a routed run says otherwise. Treat +0.039 ns as
*the last synthesis-level obstacle is gone*, not as *the frequency is achieved*.

Two things about the result are worth more than the number:

- **The mover cone that failed for the whole of this work is closed.** It was
  `mm_mover`'s `mode` register → a FIFO-room add and compare → `stall` → the
  command FIFO's write enable, 12 logic levels, and it accounted for every one
  of the 123 endpoints that used to fail. Registering the FIFO-room limit
  against a **config-time constant** — a value that cannot change while a
  descriptor is running, so a register costs nothing — took the add and the
  compare out of the path. That was the last failing cone in the node.
- **The binding path is back inside the processor**, at
  `wb_val_reg → halt_cause_reg`: the writeback value reaching the halt-cause
  register through the forward mux. It passes, and it is the same family as
  every path this core has been optimised out of — *the forward mux reaching
  through something late* ([below](#where-the-frequency-goes)). Closing the
  largest cone does not remove the critical region; it hands it to whoever is
  next.

### The core's internals, and why this table is a shape and not an attribution

| instance | module | LUT | FF | RAMB36 | RAMB18 | DSP |
|---|---|---:|---:|---:|---:|---:|
| `(u_core)` | everything else | 1,747 | 1,251 | 0 | 0 | 0 |
| `u_bp` | `rv64_bpred` | 259 | 215 | 1 | 2 | 0 |
| `u_csr` | `rv64_csr` | 1,616 | 976 | 0 | 0 | 0 |
| `u_md` | `rv64_muldiv` | 1,002 | 633 | 0 | 0 | 4 |
| `u_rf` | `rv64_regfile` | 1,555 | 130 | 0 | 0 | 0 |

**These five rows sum to 6,179 against `u_core`'s own 6,169, and the ten-LUT
overshoot is itself the point.** Under `rebuilt`, a LUT that serves two
hierarchies is counted in both children and once in the parent, so a breakdown
can exceed its own total by a handful of cells. Refine the audit accordingly:

> **A breakdown that misses its total by hundreds has rows from two runs in it
> and must not be published. One that overshoots by single digits is cell
> sharing inside one run.** The check is still worth making — it is the cheapest
> audit of a figure table there is — but the tolerance is not zero.

Passing it does **not** make the rows an attribution. `-flatten_hierarchy
rebuilt` **re-parents leaves**, and this table shows what that does: `u_alu` and
`u_dec` do not appear at all — they were absorbed into the parent — and `u_rf`
reports 1,475 logic LUTs beside its 80 LUTRAMs, for an array that holds 4 Kbit
and measures **211 LUT at `none`**. Those logic LUTs are core glue that landed
on the nearest surviving instance boundary
([worked through above](#why-u_rf-is-the-largest-row-and-why-it-is-not-the-register-file)).

Use the table for its shape: the CSR file, the multiplier and divider, the
register-file row's re-parented glue and the parent's own hazard-and-trap logic
are each between one and two thousand LUT, and the predictor and the
register-file array are not where the area is. For an attribution, read the
`none` table [above](#where-the-processors-area-is--the-attribution-run).

## What each structure costs

Each row is a measured difference between two synthesis runs of the same design
with one thing changed. They were taken at different absolute baselines, so the
**differences** are the result and the absolutes are not comparable across rows.

| change | area | frequency |
|---|---|---|
| the branch predictor, added | **+362 LUT, +2 BRAM** | no change |
| atomics — `HAS_ATOMIC` 1 against 0 | **+776 LUT**, 13.3 % of the core (5,824 against 5,048) | none: 330.3 against 329.8 |
| the CSR file with traps and interrupts, added | **+1,120 LUT, +724 FF** | no change — it is not on the ALU path |
| the register file at `block` instead of `distributed` | −5 LUT, +2 BRAM | **−59.6 MHz** (264.1 against 323.7) |
| one shared shifter instead of three barrel shifters | **−499 LUT** in `rv64_alu` (1,038 → 539) | — |
| registering the DSP instead of driving it combinationally | LUT **fell** | **+47 MHz** |
| store replication instead of a barrel shift | −235 LUT on `rv64_sys_pe`, −107 on `rv64_syscore` | **+25.8** and **+15.9 MHz** |
| registering every consumer of the effective address | **−227 LUT** | **+13.8 MHz**, 17 failing paths to 0 |
| the compute-unit shell `noc_cu_base` | 756 LUT | 761.6 MHz standalone — nowhere near critical |
| **WARL masks on the sparse CSRs** — storing only the implemented bits | **−209 LUT, −373 FF** in `rv64_csr` (1,476 → 1,267 LUT and 1,222 → 849 FF, both at `none`) | — |
| **a *vector installed* flag** instead of a 64-bit `mtvec != 0` compare inside the trap decision, with `sfence` unqualified by the trap | +223 LUT at the node | **node WNS −1.371 → −0.519 ns** |
| **the trap's state writes registered one cycle behind the redirect**, with a one-cycle fetch hold | **LUT-neutral**: the whole node moved by the mover's write-slot change in the same step | node WNS −0.160 → −0.147 ns, and the processor left the failing set |
| **the mover's FIFO-room limit registered** against a config-time constant (the node's, not this processor's) | — | node WNS **−0.081 → +0.039 ns, 123 failing endpoints to 0** — the last failing cone in the node |

Three of those rows are the ones to carry away.

**Nineteen 64-bit CSRs would be 1,216 flip-flops before any logic**, and
`mcycle`, `mtime` and `minstret` are three 64-bit increments on top. The file
measures 987 — because **a bit that is not implemented is not stored.** WARL
narrowing is architecturally permitted and it is what keeps a nineteen-register
file smaller than its register count implies; the masks are published with the
[CSR table](architecture.md#the-csrs-that-exist) for that reason.
Architecturally visible state is the expensive part of a core, which is why the
file implements the set the runtime uses rather than the set the specification
permits.

**A frequency fix can cost area and still be the right change.** The
*vector installed* flag row above added LUT and bought 0.852 ns of worst slack:
it replaced a 64-bit compare in the trap decision with a flag set at the write.
Area and frequency moving the *same* way is the signature of removing logic;
moving in opposite directions is the signature of moving it, and the second is a
perfectly good trade when the path it left was the binding one.

**Area and frequency moving the same way is the signature of removing logic**
rather than trading it. The last three rows all do it, and all three are the
same shape of change: something late in the datapath had been let into a path it
did not belong in.

## Cycles

### The core alone

`rv64_core` with a flat memory harness (`link.ld`), so every access answers in
one cycle and the numbers are the pipeline's rather than the memory system's.

| program | cycles | retired | **IPC** | stalled |
|---|---:|---:|---:|---:|
| `dhry` | 855,429 | 688,648 | **0.805** | 19.5 % |
| `hello_im` | 645,778 | 517,814 | 0.802 | 19.8 % |
| `csr` | 577 | 397 | 0.688 | 31.2 % |
| `atomics` | 7,507 | 3,266 | 0.435 | 56.5 % |

`atomics` at 0.435 is the AMO sequencer and not a defect: each atomic holds
execute for three or four cycles and the program is deliberately atomic-dense.

The retire pulse is gated on `!stall`, so it counts **instructions and not
occupancy** — `e_valid` stays high for all 66 cycles of a divide. `minstret`
counts the same pulse, so software sees the same number the harness does.

### Dhrystone

```
   855,429 cycles / 2000 runs  =  427.7 cycles per Dhrystone
   DMIPS/MHz = 1e6 / (427.7 x 1757)  =  1.331
```

**1.331 DMIPS/MHz at IPC 0.805.** `tests/rv64/dhry.c`, Dhrystone 2.1, built
`-march=rv64ima -O2 -DRUNS=2000`, every reference result correct including the
three locals.

DMIPS/MHz is the denominator a LUT count needs: without one, an area figure says
nothing. It is a per-megahertz figure and therefore carries **no frequency
claim** — pairing it with an Fmax from
[the standalone table](#standalone-as-its-own-top) would be pairing a measured
number with an upper bound. Comparing it against another core needs that core's
own conditions, which this page does not have.

Dhrystone contains no divide and almost no multiply, so its 19.5 % of stalled
cycles is branch kills and the load-use interlock. **That split has not been
measured** — see [what is not measured](#what-is-not-measured).

### What the predictor buys

Same three programs, same core, with and without `rv64_bpred`:

| program | no predictor | with predictor | |
|---|---:|---:|---:|
| `atomics` | 8,415 | **7,507** | −10.8 % |
| `hello_im` | 666,352 | **645,778** | −3.1 % |
| `dhry` | 956,572 | **855,429** | **−10.6 %** |

For +362 LUT and 2 BRAM. **This is what the BTB and the direction table deliver
together**; the return-address stack's contribution is not separated out, and
[microarchitecture](microarchitecture.md#the-return-address-stack-and-what-it-is-currently-connected-to)
explains why the stack's answer is not reaching the instruction it describes.

### What the load-use interlock costs

Registering the writeback and interlocking a load's consumer for one cycle moved
Dhrystone from 914,440 cycles to 956,572 — **+4.6 %**, or 1.245 DMIPS/MHz down
to 1.190. It bought the removal of a 121-path region at 20 logic levels, where
byte alignment, sign extension and the forward mux all sat in front of the ALU.
The predictor then more than repaid it: 956,572 down to 855,429, and 1.190 up to
**1.331**.

### As a mesh compute unit

`rv64_sys_pe`, driven through the fabric port only — the bench writes `CU_DATA`
and `CU_INST` flits and reads `CU_SIGNAL`, and reaches nothing inside the unit.

| | load | core cycles | retired | IPC |
|---|---:|---:|---:|---:|
| `pe_hello` | 61 | 853 | 617 | 0.723 |
| `pe_dhry` | 3,207 | 855,456 | 688,674 | **0.805** |

**The wrap costs 27 cycles out of 855,429.** Standalone Dhrystone is 855,429
core cycles; the same program through the endpoint is 855,456, at identical IPC,
so **1.331 DMIPS/MHz holds on the node**.

Two units at different coordinates, images interleaved granule by granule and
kicked back to back, each running a different program:

```
   load    2,306 cycles (both, interleaved)
   wall  855,439 cycles for both to complete
   A         853 cycles,     617 retired    "pe ok"
   B     855,265 cycles, 688,524 retired    "dhrystone ok"
```

Wall time equals the slower unit, so they ran concurrently rather than in
sequence. Interleaving the two images is what makes it a test: loading one fully
and then the other would pass even if the two units shared a memory.

### As the node's processor

`sys_hello.c` on `link_sys.ld` writes and reads back two distant node regions,
checks they do not alias, does byte and word accesses across the fabric, runs an
`amoadd.d` to the node range, and drives 8 KB through the 2 KB L1 so every line
is evicted and refilled.

```
   31,653 core cycles,  10,219 retired      IPC 0.323
   node   571 reads, 295 writes             exit 0
```

Two units side by side on one fabric memory run in 31,736 wall cycles for both —
each unit's own runtime, so concurrently — and the harness intersects the exact
set of words each wrote and reports them **disjoint**. That last check is what
makes it a test rather than a demonstration: both units run the same program, so
without it two units writing identical values to identical addresses would pass
while proving nothing. A minimum-to-maximum span is the wrong check and reports
a false overlap, because each unit writes three separate regions and the spans
interleave even when every word is disjoint.

IPC 0.323 against 0.805 for local-only code is the memory system, and it is
correct rather than wrong: one outstanding access, the core held in execute for
all of it, and a 2 KB direct-mapped cache being deliberately thrashed.

### The fabric-latency sweep

**These four rows were taken on a configuration with no L1**, and on the earlier
form of `sys_hello.c` before its cached section existed. Every access in them is
a full fabric round trip. They are not current figures for the shipped
configuration; they are the measurement of what fabric latency costs when
nothing overlaps it, and that shape is what the rest of this section rests on.
The harness models the fabric's answer delay and takes it as `--latency`.

| fabric latency | core cycles | IPC |
|---:|---:|---:|
| 2 | 1,774 | 0.546 |
| 6 | 2,145 | 0.452 |
| 20 | 3,458 | 0.280 |
| 40 | 5,333 | 0.182 |

94 node accesses, and 3,559 cycles of spread across 38 units of latency — so
**one core cycle per access per unit of fabric latency**, which is arithmetically
exactly what *one outstanding, nothing overlapped* means. Every cycle the fabric
takes is a cycle the core spends held in execute.

That is why a non-blocking L1 is the largest single lever on this configuration,
and why the L1 that exists — blocking, one outstanding miss — changes the
constant and not the slope.

## Where the frequency goes

The binding path has moved five times, and each move is a general lesson rather
than a fix:

| the path | the shape of the problem |
|---|---|
| `cnt → operand mux → DSP → accumulator add`, 23 levels and 11 CARRY8 | a **pipelined primitive used combinationally**. A DSP48E2 carries A/B, M and P registers; not using them costs frequency for no area gain |
| the register file array's own clock-to-out | **block-RAM clock-to-out is slow and no logic restructuring moves it.** It stayed the top path through two rounds of optimisation before the primitive changed |
| `wb_rd_reg → m_val_reg`, 19 levels | a **comparator in the same cycle as the mux it selects and the ALU behind it**. Moving the comparison to decode left a mux |
| `wb_val_reg → …`, 14 to 17 levels, repeatedly | **the forward mux reaching through the 64-bit address adder into something late** — a range decode, a byte-write enable, a CSR write enable, the predictor's stack pointer |
| the trap decision into the CSR file's clock enables, 18 levels | **a deep combinational decision used as the write enable of a whole register file.** The trap decision carries the address adder through the misalignment test, and it gated roughly two hundred flip-flops. Registering the *data* and leaving only the PC redirect combinational is what removed it ([architecture](architecture.md#when-a-traps-effects-land)) |

The last row happened four times with four different endpoints, and that is the
lesson worth carrying: **a design has a critical *region*, and one endpoint
rarely names it.** Both scripts on this page therefore report **every**
negative-slack path, collapsed to `startpoint → endpoint` and counted per group,
worst group first. At one point that report read

```
   @@@FAILN 68
   @@@GROUP  64 paths  worst -0.076  lvl 11  wb_val_reg -> u_bp/ras_tos_reg
   @@@GROUP   4 paths  worst -0.076  lvl 11  wb_val_reg -> u_bp/ras_sp_reg
```

— 68 failures and **one** region, ending in the predictor. The root was six
gates upstream in `misalign`, the last address-derived term in `stall`, and
removing that one term killed all 68 at once and bought **+37.8 MHz**. Fixing
the endpoint the tool named would have moved it somewhere else, which is what
had happened four times already.

**Logic levels are the screen, not slack.** Anything above 11 levels is bad
whatever the slack says: a path at −0.017 ns and 14 levels is not "nearly
passing", it is fragile. The 64-bit address adder alone is about eight of the
eleven.

### Where it is now

**In the node, nothing fails, and the binding path is back in this processor.**
The mover cone that failed throughout this work is closed, and the whole node's
worst path is now `rv64_core`'s `wb_val_reg → halt_cause_reg` at 12 logic levels
with **+0.039 ns of slack**
([above](#timing-the-node-meets-300-mhz-in-synthesis)).

That path is the fifth instance of one shape — **the forward mux reaching
through something late** — and the endpoint is new each time: a range decode, a
byte-write enable, a CSR write enable, the predictor's stack pointer, and now
the halt-cause register. It is passing, so it is not a defect; it is where the
next megahertz would have to come from.

**Standalone, this processor's own worst path is the one it has always named:**
the register file's read, through the forward mux, the 64-bit address adder, the
misalignment test, the trap decision and the vector select, into the program
counter — 14 levels, −0.465 ns at `none`
([above](#where-the-processors-area-is--the-attribution-run)). There is no
address-generation stage between E and M, so the adder and the trap decision
share a cycle, and every local fix on that path has been a registration rather
than a restructuring. The structural fix is the stage
([microarchitecture](microarchitecture.md#why-there-is-no-address-generation-stage)),
and it is not built.

That the standalone run's worst path and the node's are different paths in the
same core is not a contradiction: they are different syntheses of different tops
under different flows, and **a path's rank is a property of the run**. Neither
is "the" critical path of the design.

## Verification behind these numbers

Cycle figures are only worth their programs. What produced them:

| bench | what it covers |
|---|---|
| `rv64_alu` | **4,409,094 exhaustive comparisons**, 0 errors, against an independently written C++ model |
| `rv64_decode` | **791,131 comparisons**, 0 errors, same method — every funct3/funct7 of every opcode, the base I datapath fields checked in full and the M / A / Zicsr / privileged encodings checked for legality, which is where an illegal-detection hole hides |
| `rv64_muldiv` | 54,548 checks including divide-by-zero, `−2⁶³ ÷ −1`, and both `W` extension rules |
| `rv64_regfile` | 99 checks, including that a same-cycle write returns the **old** value — the property the forwarding network is built against |
| `rv64_l1` | 8 KB through a 2 KB cache against a reference memory, checking **every writeback beat as it leaves**: 2,048 hits, 512 misses, 512 fills, 256 writebacks, 0 read failures, 0 writeback mismatches |
| `rv64_mmu` | cold walks, warm hits, permission failures, superpages, machine-mode passthrough — **and the shared port**: a data access pre-empting a fetch walk, a cold request not riding the previous request's hit, and a fetch fault staying with the fetch |
| `rv64_core` | four programs — `hello_im`, `atomics`, `dhry`, `csr` — against an independently written C++ model |
| `priv` on `rv64_core` | machine → supervisor → user and back, delegation, an illegal `SRET` in user mode, misaligned causes 4 and 6 |
| `sv39` on `rv64_syscore` | three-level tables walked by hardware; a translated store read back at its physical address; a delegated load page fault (13, with `stval` the address); and **a jump into an unmapped page** — instruction page fault 12, `sepc` and `stval` both the bad address, resumed by the handler |
| `osloop` on `rv64_syscore` | user code under Sv39 on pages of its own, preempted by the timer six times and resumed, with one delegated `ECALL` |
| `dispatch` on `rv64_syscore` | the mailbox: a dispatch built in hardware from three stores, the completion queued, an interrupt raised, the head read and popped |
| `rv64_sys_pe`, `rv64_pe_pair` | the fabric port only, including all four loader rejection cases sent **before** the real image |
| `rv64_syscore`, `rv64_syscore_pair` | the node port and the host window, with the disjointness check above |

The models are independently written rather than mirrors of the Verilog. A
mirror agrees with the design about a shared misreading, which is the failure
mode a differential bench exists to catch. The same workload built `-march=rv64i`
and `-march=rv64im` produces **byte-identical output**, so the hardware multiply
and divide agree exactly with the software routines over 638 k cycles.

The core is lint-clean under `--lint-only --warn` with `LATCH` and `PINMISSING`
enabled. Both are silenced by default in `scripts/py/vlt.py` because older RTL in
this tree trips them wholesale, and both catch real faults here: an undriven
output that floats to X, and a missing default assignment in a combinational
`always` block that infers a latch and holds a control bit into the following
instruction.

## What is not measured

Stated plainly, because a missing measurement is more useful named than guessed
at:

- **No routed result for anything on this page.** Every frequency is synthesis.
  There is no placed-and-routed figure for `rv64_core`, `rv64_syscore`,
  `rv64_sys_pe` or `rv64_mag_pe`, and no measurement from real silicon.
- **No current standalone `rebuilt` figure for either configuration.** The
  `rebuilt` standalone rows on this page predate privilege, Sv39, fetch
  translation and the mailbox and **have not been re-run**. The current numbers
  are the `none` attribution run and the node run, and neither substitutes for
  what a `rebuilt` standalone would report.
- **No current figure of any kind for `rv64_sys_pe`.** It shares `rv64_core`, so
  its area has certainly moved with the CSR file; nothing has measured by how
  much.
- **The 19.5 % stall budget on Dhrystone is not broken down** between branch
  kills and the load-use interlock. Both levers are known; their relative size
  is not.
- **The predictor's sizes have never been swept.** `BTB_ENTRIES`,
  `PHT_ENTRIES`, `HIST_W` and `RAS_DEPTH` are all parameters and the only
  measurement is the whole predictor at −10.6 %. The split between the BTB, the
  direction table and the stack is unknown, and the stack's selector timing
  ([microarchitecture](microarchitecture.md#the-return-address-stack-and-what-it-is-currently-connected-to))
  is a reason to expect the stack's share to be near zero as built.
- **No cycle figure for the dispatch path.** The mailbox is wired at the node
  and a completion round trip is verified functionally, but nothing measures
  what a dispatch and its completion cost in cycles.
- **No hit-rate measurement for the L1** on a real workload. The bench drives a
  deliberate thrash; nothing measures a representative one.
- **No cycle measurement for Sv39.** It can be switched on now, and the
  functional benches switch it on — but **no cycle count has been taken** with
  translation enabled, so the cost of a TLB hit, a miss and a walk in a real
  workload is unknown. The area figures are measurements; the cycle cost is not.
- **No measurement of what fetch translation costs.** One page register refills
  on a page crossing and holds fetch while it does; how often that happens in
  real code, and what it costs, has not been counted.
- **No standalone figure for `rv64_mag_pe` at the current RTL.** It appears here
  only as a sub-hierarchy of the node.
- **No power figure of any kind.**
