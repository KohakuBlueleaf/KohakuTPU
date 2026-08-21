---
title: PE integration
summary: Instantiating a controller PE — the attach, the parameters, the constraint to ask of it, extension seams, and the test suite.
tags:
  - architecture
  - pe
  - rv32
  - integration
---

# PE integration

What the hardware integrator needs: how a PE joins a mesh, what the
parameters decide, what clock to ask of it, and how to prove a configuration
before trusting it. The costs behind every choice are in
[performance](performance.md).

## The attach

One local port, one clock. `rv_pe` instantiates `noc_cu_base` and presents
the standard compute-unit interface — the same flit port, instruction FIFO,
receive FIFO and `CU_CTRL` block as every unit on the fabric
([compute-unit-port](../noc/compute-unit-port.md)). There is no other
connection: no AXI, no sideband, no second clock domain. A mesh gains a PE
the way it gains any unit — a router coordinate and a window in the address
map.

The unit lives entirely on `noc_clk`. Its requestor speaks to the memory
agent over the fabric, so PE count per mesh is bounded by the agent's
capacity, not by the core: four PEs per NoC/MAG pair is the measured ceiling
([performance](performance.md#multi-core-scaling)).

```
src/kohakuaccel/pe/rv32/
    rv_pe.v          the assembled PE: instantiation and wiring only
    core/            the pipeline: IF, ID, EX, MEM, WB, regfile, predictor
    mem/             the two L1s and the byte-enable RAM wrapper
    noc/             the requestor
```

## Parameters

Every knob is a parameter of one design — no forked files. **The defaults
below are the shipped configuration**, and they are what
[performance](performance.md) characterizes; a changed setting is verified
as itself by the suite below, never assumed from the default build.

| Parameter | Default | What it decides |
|---|---|---|
| `BTB_ENTRIES` | 32 | predictor size; 0 removes the predictor entirely (a generate) |
| `FWD_X` | 1 | the distance-1 bypass. 0 is measured worse on every axis and remains only as the proof — [microarchitecture](microarchitecture.md#hazards) |
| `L1_LINES` | 128 | internal L1 lines. 128 fills the BRAM's natural depth; halving saves almost nothing |
| `REGFILE_PRIM` | `"distributed"` | LUTRAM vs block-RAM register file. Interchangeable timing; a resource trade — [performance](performance.md#resources) |
| `IMEM_WORDS` / `SPAD_WORDS` | 2048 | the two windows, sized to fill their BRAM tiles exactly |
| `WR_MAX` | 1 | un-acknowledged writes. 1 is what the communication model assumes; raising it buys nothing a blocking cache can use |

## The constraint to ask

**Constrain the PE at 3.333 ns.** The design's ceiling is set by a block
RAM's clock-to-out and it reaches that ceiling at the 3.333 ns request;
asking for more cannot raise the frequency and makes synthesis spend LUT
answering an impossible question — 13–15 % of the unit at 2.2–2.0 ns asks,
for zero megahertz. The numbers are in
[performance](performance.md#frequency).

At mesh ship clocks (`noc_clk` at 300 MHz), the PE carries better than 30 %
timing margin.

## Verifying a configuration

Four levels, one command each, every run bounded (per-case cycle ceilings, a
watchdog, spin caps — a hanging core fails rather than hanging the bench):

```
python tests/pe/tools/rv_run.py --gate 1     # the core against a golden model, instruction by instruction
python tests/pe/tools/rv_run.py --gate 2     # the memory frontend against the protocol
python tests/pe/tools/rv_run.py --gate 3     # real software on the real memory substrate
python tests/pe/tools/rv_run.py --gate 4     # one, two and four PEs sharing a NoC and a memory agent
```

Level 1 co-simulates against a Python RV32I model, comparing PC, destination
and value for every committed instruction — and it runs the *configured*
shape: a variant with the predictor removed is verified as itself, never
assumed from the full build. Level 3 boots through the real window-write
path and checks halt word, completion code and a DRAM checksum. Level 4 is
where the communication protocol is proven between running cores rather than
against a bench.

Configuration knobs flow to the benches and to synthesis from the same
definition, so what levels 1–4 verified is what the synthesis measured — a
knob that reached only one of the two would silently decouple them.

## Where extensions attach

Nothing enters this baseline because it is normally found in a CPU. Each of
`RV32M`, `Zbb`, a larger predictor, a larger L1, multiple outstanding
misses, atomics, DSP or SIMD lanes is a separate experiment: add one
feature, measure resources and frequency, measure workload benefit.

The seams a KohakuMPE extension would use, named but not designed:

| Seam | Where |
|---|---|
| new functional units | the EX stage's ALU select, widened; the decoder already routes `funct3`/`funct7` |
| wide operands | a second register file beside `rv_regfile`, on the same two-boundary read timing |
| wide memory | the internal L1's line is already 256 bits at the fill boundary; only the CPU-side port is 32 |
| bulk peer transfer | a write-combining buffer in front of the requestor's push path |
| more miss concurrency | the requestor's single tag becomes a small MSHR table; `rv_l1`'s blocking FSM is what changes |
