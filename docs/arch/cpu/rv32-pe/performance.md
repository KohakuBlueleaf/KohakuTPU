---
title: RV32 PE performance
summary: What the RV32 PE costs and achieves — frequency, resources, instruction and memory timing, communication latency, multi-core scaling, and the condition on every figure.
tags:
  - architecture
  - cpu
  - rv32
  - performance
---

# RV32 PE performance

The measured characteristics of the [RV32 PE](README.md) in its one shipped
configuration: 128-line internal L1, 2048-word windows, 32-entry predictor,
`FWD_X` 1, LUTRAM register file, `WR_MAX` 1.

## How to read every number on this page

Read this before quoting anything below.

| | |
|---|---|
| **part** | `xcvu13p-fhgb2104-2L-e` |
| **tool** | Vivado 2024.2, Build 5239630 |
| **mode** | **out-of-context, synthesis — not placed and not routed** |
| **produced by** | `scripts/tcl/ooc_rv_pe.tcl`, top `rv_pe`, `-flatten_hierarchy none` |
| **configuration** | `REGFILE_PRIM=distributed`, `L1_LINES=128` (the shipped value, **not** the script's own default of 64), `FWD_X=1`, `BTB_ENTRIES=32`, `IMEM_WORDS=2048`, `SPAD_WORDS=2048`, directive `default` |
| **request** | **3.333 ns**, which is what this page's headline figures are taken at and what [integration](integration.md#the-constraint-to-ask) tells you to use. A second run at 2.500 ns is quoted below as a controlled comparison |
| **cycle figures** | the PE's own `CTL_CYCLE` / `CTL_INSTRET` counters, on the full-system simulation — real routers, the real memory agent, RAM behind it — driven by `tests/pe/tools/rv_run.py` |
| **LUT figures** | CLB LUT **sites**, as Vivado reports them |

**No Fmax in this repository is a closed-timing figure.** Synthesis slack is
optimistic — elsewhere in this project a module lost 0.740 ns going from
synthesis to routing — so every megahertz below is an upper bound on what a
routed design would show. [measurement](../../physical/measurement.md) is the
page that defines this for the whole tree.

| | LUT | FF | BRAM | DSP48 | control sets | achieved | request | slack |
|---|---|---|---|---|---|---|---|---|
| the whole PE | **2,586** | 3,844 | **9** | **4** | 88 | **363.5 MHz** | 3.333 ns | **+0.582 ns** |

Of the 2,586 LUTs, 2,350 are logic, 236 are LUTRAM and none are shift
registers. There are no URAMs.

**The four DSP48s are the multiplier.** `rv_ex.v` builds one 33 × 33 signed
product serving `mul`, `mulh`, `mulhsu` and `mulhu`
([microarchitecture](microarchitecture.md#the-multiplier)). An earlier
measurement of this unit reported **0 DSP48** and is superseded: it predated
the multiplier, and every LUT, FF, BRAM and Fmax figure it carried described a
core without the multiply half of `RV32M`.

## Frequency

**The ceiling is 363.5 MHz, and asking for more does not raise it.** The two
runs below differ in one variable — the requested period. Same script, same
top, same shipped configuration, same flow, same RTL vintage:

| request | LUT | achieved | slack |
|---|---|---|---|
| **3.333 ns** — 300 MHz | **2,586** | **363.5 MHz** | **+0.582 ns** |
| 2.500 ns — 400 MHz | 2,672 | **363.5 MHz** | −0.251 ns |

**The tighter request bought zero megahertz and cost 86 LUT.** Synthesis
answered an impossible ask by spending logic on a path whose length it cannot
change: the ceiling is a block RAM's clock-to-out, and no constraint moves it.

Two details make the comparison worth trusting. **FF, BRAM, DSP and control
sets are identical across the two runs** — 3,844, 9, 4 and 88 — so nothing
structural changed and the 86 LUT is timing-driven duplication and nothing
else. And the 400 MHz run **misses its request**, at −0.251 ns, so it is not
merely a worse trade; it is a request the design does not meet.

**Constrain the PE at 3.333 ns.** That is the whole argument, measured on the
shipped RTL, and [integration](integration.md#the-constraint-to-ask) is where
it is stated as guidance.

At a 300 MHz fabric clock, 363.5 MHz is 21 % of margin **against a synthesis
figure**. That is a screen, not closure.

### The primitive mix

LUT6 909 · LUT3 601 · LUT2 523 · LUT4 496 · LUT5 340 · LUT1 41 · FDRE 3,800 ·
FDSE 44 · CARRY8 59 · RAMB36E2 9 · RAMD32 326 · RAMS32 50 · RAMD64E 48.

The six LUT rows sum to **2,910**, which is *more* than the 2,586 in the table
above rather than less. That is the site-versus-primitive split described under
[Resources](#resources), and it runs in both directions: **a CLB LUT site can
host two smaller LUT primitives at once**, so a primitive count runs ahead of a
site count wherever synthesis has paired them — while the `RAMD`/`RAMS` cells
are LUTRAM, which occupies sites counted in the 2,586 but is not a LUT
primitive and so appears in rows of its own here.

Neither number is the other one plus an error term. Quote whichever answers
your question, and say which it is.

### The critical path

Measured on the same 3.333 ns run as the figures above, extracted with
`get_timing_paths -max_paths 12 -nworst 1 -setup` on the synthesised design.
**That extraction came from a one-off script: no tool in `scripts/tcl/` reports
the endpoint pair**, which is why the previous figure here went stale without
anyone noticing.

**The binding path starts at the memory stage's address register and ends in
the ID stage:**

```
+0.582 ns   9 levels
  u_core/u_ex/m_addr_reg[29]/C  ->  u_core/u_id/d_imm_reg[12]/S   (and [13]..[19])

+0.631 ns   8 levels
  u_core/u_ex/m_addr_reg[29]/C  ->  u_imem/u_mem/u_ram/.../ADDRBWRADDR[4]
```

**This is a control path, not a datapath one.** `rv_mem` is the one address
decoder the PE has, and it decodes on the registered memory-stage address; the
region it computes feeds the stall term, and the stall term reaches back into
the ID stage's pipeline registers. The endpoints are *set* pins rather than
data inputs, which is why one path arrives at eight consecutive bits of the
immediate register at once.

**Eight of the top ten paths are that one startpoint into consecutive bits of
one register.** They are one path with eight endpoints, not eight problems —
which is exactly why counting failing endpoints overstates the number of things
wrong, and why [timing-closure](../../../workflow/timing-closure.md) groups
paths before reading them.

**Nine logic levels, and the second group is eight.** Both are comfortably
under the level count that signals trouble, so the design is not level-bound at
this request — consistent with 363.5 MHz being a genuine ceiling rather than an
artefact of a long combinational chain.

#### What this replaces, and what it does not

The path recorded before the multiplier was the **load-data return** —
`u_spad/.../CLKBWRCLK -> u_core/u_id/x_op1_reg[16]/D`, at 6 levels: a data
array's clock-to-out, the cross-port bypass, the sub-word extract, the
forwarding network, the ID operand register. **That endpoint pair no longer
appears in the top twelve.** It is superseded, not merely unconfirmed.

It is worth being precise about what that does and does not settle. The
multiplier's result joins the pipeline through the mux feeding the distance-1
forwarding path, and
[microarchitecture](microarchitecture.md#where-the-result-joins-and-why-that-was-the-risk)
argues that this makes the mux an expensive place to add a case. **That
reasoning stands; the mux is not where the binding path is.** Adding a level to
a mux that feeds the register a critical path terminates at is still the wrong
place to spend one — but on the shipped RTL, at this request, the path that
binds runs from the address decode into ID's register control, and the
forwarding network is not on it.

## Resources

Per unit, from the **hierarchical** utilisation report — `-flatten_hierarchy
none`, the shipped configuration, with the multiplier built.

**This breakdown is from the 2.500 ns run, not the 3.333 ns headline**, which
is why its total is 2,672 rather than 2,586. It is the split that is the
information here, and the split is what the two runs share: the 86 LUT between
them is timing-driven duplication spread across the design, and FF, BRAM and
DSP are identical in both. Do not mix a row from this table with the 3.333 ns
total.

| instance | module | Total LUT | Logic | LUTRAM | FF | RAMB36 | DSP |
|---|---|---|---|---|---|---|---|
| **`rv_pe`** | **the whole unit** | **2,672** | 2,436 | 236 | **3,844** | **9** | **4** |
| `(rv_pe)` | its own logic: window writer, kick FSM | 191 | 191 | 0 | 477 | 0 | 0 |
| `u_base` | `noc_cu_base` — the framework attach | **498** | 410 | 88 | 840 | 4 | 0 |
| `u_core` | `rv_core` — the RV32 pipeline | 1,298 | 1,226 | 72 | 1,085 | 0 | 4 |
| `u_core/u_ex` | `rv_ex` — ALU, multiplier, branch, address | **489** | 489 | 0 | 214 | 0 | **4** |
| `u_core/u_id` | `rv_id` — decode, operands, forwarding | 248 | 248 | 0 | 351 | 0 | 0 |
| `u_core/u_mem` | `rv_mem` — the one address decoder | 206 | 206 | 0 | 117 | 0 | 0 |
| `u_core/u_if` | `rv_if` — fetch, with the whole predictor | 135 | 103 | 32 | 163 | 0 | 0 |
| `u_core/u_rf` | `rv_regfile` | 129 | 89 | 40 | 108 | 0 | 0 |
| `u_core/u_wb` | `rv_wb` | 20 | 20 | 0 | 0 | 0 | 0 |
| `u_l1` | `rv_l1` — internal L1, 128 lines | 365 | 317 | 48 | 413 | 1 | 0 |
| `u_req` | `rv_noc_req` — the NoC requestor | 274 | 246 | 28 | 992 | 0 | 0 |
| `u_spad` | `rv_spad` — scratchpad | 46 | 46 | 0 | 37 | 2 | 0 |
| `u_imem` | `rv_imem` — instruction window | 0 | 0 | 0 | 0 | 2 | 0 |

**This is the site accounting, and it is not the only one the script emits.**
The same run also produces a per-instance count taken by filtering cells on
`REF_NAME =~ LUT?`, and its numbers differ — `u_core/u_ex` reads 589 there
against 489 here, and `u_core` 1,379 against 1,298.

The two are counted on different rules, and the difference is not a discrepancy
to be reconciled:

- **CLB LUT sites**, from `report_utilization` — how much of the device the
  design occupies. Logic, LUTRAM and shift-register uses are all sites, so they
  are all in this number. The table above is this one.
- **Raw LUT primitives**, from the cell filter — how many logic cells synthesis
  inferred. LUTRAM and SRL cells are counted separately and are *not* in the
  LUT column.

Whole-unit, the same run reads **2,586 CLB LUT sites and 2,910 raw LUT
primitives**. Take a whole table from one report or the other, name which one,
and **never subtract a figure in one accounting from a figure in the other** —
the difference measures the report, not the design. A hierarchical report also
re-parents leaf cells, which is a second reason the two per-instance tables do
not line up.

Four of these are design outcomes rather than accounting:

- **The EX stage is 489 LUT and all four DSPs.** It is the largest block in the
  pipeline, and it is where the multiply half of `RV32M` lives. The DSPs are
  the product; the LUTs beside them are the operand extension, the
  half-selection and the merge.
- **The predictor is inside the 135 LUT of fetch**, because its entries live in
  LUTRAM depth rather than in logic.
- **The scratchpad is 46 LUT** rather than the ~7 of a plain array, because
  most of that is the cross-port bypass that makes the doorbell correct — a
  priced correctness cost, sitting on the critical path.
- **The framework attach is 498 LUT and 840 FF**, or **18.6 % of the 2,672 in
  this table** — port logic that every compute unit on this fabric carries,
  processor or not. So the marginal cost of *this unit being a processor* is
  about **2,174 LUT**. Both figures are within this run; there is no per-unit
  breakdown at 3.333 ns to restate them against.

### A shell figure means nothing without its configuration and its request

`noc_cu_base` now has three recorded values in this tree, and they are three
different measurements rather than three attempts at one number:

| figure | what it is |
|---|---|
| **498 LUT, 840 FF, 4 RAMB36** | inside `rv_pe`, hierarchical report, 2.500 ns request, the run at the top of this page |
| **756 LUT** | synthesised standalone, out-of-context, same part and tool. Reproducible: `ooc_rv_pe.tcl` takes the top module as its first argument |
| 657 LUT, 1,381 FF, 0 RAMB36 | inside `rv_pe` at the same 2.500 ns request, from the **pre-multiplier** run. Superseded, and kept here only as the evidence for the rule |

The rule the three of them make: **quote a shell figure with the configuration
and the timing request attached, or it is not a figure.** A module synthesised
alone and the same module inside a parent are optimised in different contexts,
the queue parameters move storage between LUTs and block RAM, and the request
moves LUT count on its own. None of these three may be subtracted from another.

The register file is LUTRAM. A block-RAM form exists behind `REGFILE_PRIM` with
identical timing, but a 32 × 32 register file leaves a 1K × 36 `RAMB36E2`
3.1 % depth-utilised — the worst ratio anything in this design could post — so
the LUTRAM form ships.

### BRAM depth

Every array that earns a tile fills the tile's natural depth at its aspect
(1K × 36 for a 32-bit port). The script reports these rather than asserting
them:

| Array | Words × width | Tiles | Depth used |
|---|---|---|---|
| instruction window | 2048 × 32 | 2 | **100 %** |
| scratchpad | 2048 × 32 | 2 | **100 %** |
| internal L1 data | 1024 × 32 | 1 | **100 %** |

Width is 88.9 % everywhere — a 36-bit face carrying 32 data bits — which is the
primitive, not a choice. The tag array is far too shallow to earn a tile and
stays LUTRAM, carrying `valid`/`dirty` beside the tag; that is what makes the
128-line capacity nearly free on the same single tile.

Those three arrays account for five of the unit's nine tiles. **The other four
belong to the framework attach**, whose instruction and receive queues are
built in block RAM at this configuration — which is why `u_base` shows 4
RAMB36 in the table above and why the shell's flip-flop count is lower than an
all-LUTRAM build of the same module would show.

## Where this PE sits among the classes

The wide classes built on this core — their LUT, their lane counts, the format
they compute in, and what a mesh of them populates to — are
[KohakuMPE's numbers](../../../projects/kohakumpe/README.md). They are measured
on the same part and reported there with their own conditions, and this page
does not restate them: a framework page carrying one project's utilisation
table reads as a specification of the framework, and is not one.

One comparison across the classes **is** framework, and it is why they are
mentioned here at all. **Every compute unit on this fabric pays the same
attach.** On the RV32 PE `u_base` is close to a fifth of the unit; on the SIMT
PE, the widest class measured so far, it is 3.0 %. The attach does not grow
with the datapath behind it, which is the property being claimed.

Three things to carry into any cross-class comparison you make:

- **The ask is not decoration.** On this core, measured on current RTL,
  tightening the request from 3.333 ns to 2.500 ns bought **zero megahertz and
  cost 86 LUT** ([above](#frequency)) — so subtracting two figures taken at
  different requests measures the constraint as much as it measures the design.
- **Two flows are in circulation.** On one wide design at a 2.500 ns ask,
  `-flatten_hierarchy none` and `rebuilt` differ by **+720 LUT and −4.1 MHz**
  for `none` — small, but only once it is named. **Compare within a flow where
  you can**, and note that this page's figures are `none` while the RV64
  processor's are `rebuilt` ([cpu](../README.md#the-condition-on-those-figures)).
- **Two accountings are also in circulation**, from the same run — CLB LUT
  **sites** and raw LUT **primitives**, which for this unit read 2,586 and
  2,910 ([above](#resources)). Neither is wrong; they answer different
  questions, and a figure quoted without saying which is not comparable with
  one that does.

## Instruction timing

| Event | Cost |
|---|---|
| most instructions | 1 cycle |
| `mul`, `mulh`, `mulhsu`, `mulhu` | 3 stall cycles |
| `div`, `rem` | **fault** — not built on any class |
| load-use, back to back | 2 stall cycles |
| load-use at a spacing of one | 1 stall cycle |
| taken branch, predicted | 0 |
| mispredict or unpredicted taken branch | 3 cycles |
| peer-window push | 1 cycle + hold until the requestor accepts |
| dispatch: the argument and PC stores | 1 cycle each — they write requestor registers and always accept |
| dispatch: the opcode store | 1 cycle + hold until the requestor accepts; it is the doorbell |
| `ECALL` / `EBREAK` | halts; the completion carries the word |

## Memory timing

A scratchpad or control access is one cycle, always. A DRAM access hits in one
cycle; a miss is a line-fill round trip through the fabric and the memory agent
— hundreds of cycles, dominated by the agent and DRAM, not the PE. An eviction
adds a one-descriptor writeback ahead of the fill; a steady-state
read-modify-write stream runs at **~30 cycles per evict-and-refill pair**
against the real agent.

A flush-all runs at **~12 cycles per dirty line** when acknowledgements return
promptly — 197 cycles for 16 dirty lines. Against a slow agent each line pays
the acknowledgement latency instead: **677 cycles for the same 16**.

The single outstanding write (`WR_MAX` 1) costs nothing anywhere else: a
blocking one-miss cache has a whole fill round trip between dirty evictions,
and the previous acknowledgement always arrives inside it. It is what the
[ordering rules](architecture.md#ordering) assume; an extension adding miss
concurrency must re-measure it.

An invalidate-all is a sweep: one cycle per line, pipeline held, nothing on the
wire.

## Communication

**A push-and-doorbell round trip between two running cores is 49 cycles** — two
window pushes, two hops each way, and two poll loops. The number is quantised
by the poll: a four-instruction poll loop is ~9 cycles, and a push is
observable only when the loop next comes round, so one extra iteration costs a
whole loop. That quantisation is what the scratchpad's cross-port bypass buys:
a poll that sampled the array mid-push would read undefined data and go round
once more.

**Two concurrent pairs cost exactly what one costs** — identical to the cycle,
on link-disjoint routes — so pairwise communication scales until routes share a
link.

All-to-one aggregation (workers push value-then-flag, the leader polls flags
only): three workers cost the leader **380 cycles and 10 instructions** over
one worker. The leader reads a value beside a flag it has seen with no
handshake back — the per-destination ordering rule doing the work.

## Multi-core scaling

One memory agent serves up to four PEs. The cost of sharing it, on a fixed
compute-bound program — cycles, identical instruction stream at every count, so
the whole difference is memory:

| | 1 PE | 2 PEs | 4 PEs |
|---|---|---|---|
| the same program, kick to halt | 7,418 | 7,778 (+4.9 %) | 8,431 (+13.7 %) |

The +13.7 % is measured while the three neighbours run the heaviest memory work
in the suite, including a deliberate worst case: a copy whose source and
destination sit exactly one cache-size apart, so every access conflict-misses —
26.6 cycles per instruction, the hardest load one PE can put on the agent with
no wasted instructions. A stride that maps source and destination to the same
sets is what a 4 KB direct-mapped cache punishes; lay buffers out accordingly.

Two DRAM hand-offs (flush → doorbell → invalidate → re-read) running
concurrently through one agent cost the second pair **+159 cycles on the write
side and +175 on the read side** over the first — two blocking flush-alls
sharing the agent's write slots.

## What is not measured here

Stated so absence is not read as a result:

- **no routed figure for this unit** — every number above is out-of-context
  synthesis;
- **no checked-in tool reports the critical path.** The endpoint pair above is
  current, but it was extracted by hand; `ooc_rv_pe.tcl` reports utilisation,
  BRAM depth, control sets and an Fmax per clock, and no path endpoints. That
  gap is why the previous figure survived a change of RTL without being
  noticed, and it will do so again;
- **no per-unit breakdown at 3.333 ns.** The hierarchical table is from the
  2.500 ns run;
- **no measurement of the multiplier in isolation.** The `rv_ex.v` comment
  recording 365.6 MHz for the multiplier's form has no run behind it in this
  tree; the 363.5 MHz here is the whole assembled unit;
- **no in-context figure** — the PE has never been measured as a sub-hierarchy
  of an assembled system, the way the RV64 configurations have
  ([cpu](../README.md#what-each-one-costs)), so its contribution to a full
  design is unknown;
- **no power figure of any kind.**
