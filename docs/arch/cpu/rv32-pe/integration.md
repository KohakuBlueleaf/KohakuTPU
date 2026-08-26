---
title: RV32 PE integration
summary: Instantiating an RV32 PE — the attach, the parameters, the constraint to ask of it, extension seams, and the test suite.
tags:
  - architecture
  - cpu
  - rv32
  - integration
---

# RV32 PE integration

The [RV32 PE](README.md) attaches to a mesh exactly like any other compute
unit, so integrating one is mostly a matter of choosing parameters and a clock
constraint. This page is what a hardware integrator needs: how it joins a mesh,
what the parameters decide, what clock to ask of it, and how to prove a
configuration before trusting it. The costs behind every choice are in
[performance](performance.md).

## The attach

One local port, one clock. `rv_pe` instantiates `noc_cu_base` — the framework's
compute-unit shell, which owns the flit port, the instruction FIFO, the receive
FIFO and the `CU_CTRL` block that every unit on the fabric presents
([compute-unit-port](../../noc/compute-unit-port.md)). There is no other
connection: no AXI, no sideband, no second clock domain. A mesh gains a PE the
way it gains any unit — a router coordinate and a window in the address map.

**The whole unit is on the fabric clock and there is no clock-domain crossing
anywhere in it.** The requestor has to speak the port contract in that domain,
so the core joins that domain rather than being bridged into it.

Its requestor reaches memory over the fabric, so the number of PEs per mesh is
bounded by the memory agent's capacity, not by the core: four PEs per
fabric/agent pair is the measured ceiling
([performance](performance.md#multi-core-scaling)).

```
src/kohakuaccel/pe/rv32/
    rv_pe.v          the assembled PE: instantiation and wiring only
    core/            the pipeline: IF, ID, EX, MEM, WB, regfile, predictor
    mem/             the two L1s and the byte-enable RAM wrapper
    noc/             the requestor
```

## Parameters

Every knob is a parameter of one design — no forked files. **The defaults below
are the shipped configuration**, and they are what
[performance](performance.md) characterises; a changed setting is verified as
itself by the suite below, never assumed from the default build.

### Placement and addressing

| Parameter | Default | What it decides |
|---|---|---|
| `POS_X` / `POS_Y` | 2 / 2 | this unit's mesh coordinate |
| `MEM_X` / `MEM_Y` | 0 / 1 | the coordinate of the memory port that serves it |
| `POS_WIDTH` | 4 | coordinate field width; the mesh is a `2^POS_WIDTH` square |
| `FLIT_WIDTH` | 288 | the fabric's flit width; fixed by [spec](../../../spec/flit-format.md) |
| `DRAM_BASE` | 0 | the 40-bit physical base the PE's 2 GB software DRAM window maps onto. **OR-ed in, never added** — the low 31 bits are zero by construction, so the translation costs no logic |

### Size and structure

| Parameter | Default | What it decides |
|---|---|---|
| `IMEM_WORDS` / `SPAD_WORDS` | 2048 | the two windows, sized to fill their block-RAM tiles exactly |
| `L1_LINES` | 128 | internal L1 lines. 128 lines × 32 B = 4 KB = 1024 words of 32 bits, which is exactly one `RAMB36E2` at its 1K × 36 aspect. 64 lines is the same number of tiles for half the cache |
| `BTB_ENTRIES` | 32 | predictor size; 0 removes the predictor entirely (a generate) |
| `BTB_TAG_W` | 8 | predictor tag width; the table may alias, so a short tag is a size choice and not a correctness one |
| `FWD_X` | 1 | the distance-1 bypass. 0 is measured worse on every axis and remains only as the proof — [microarchitecture](microarchitecture.md#hazards) |
| `REGFILE_PRIM` | `"distributed"` | LUTRAM vs block-RAM register file. Interchangeable timing; a resource trade — [performance](performance.md#resources) |
| `MEM_PRIM` | `"block"` | the primitive the windows and the L1 data array name. **Named, never inferred**: read latency here is pipeline structure |
| `WR_MAX` | 1 | un-acknowledged writes. 1 is what the communication model assumes; raising it buys nothing a blocking cache can use |

### The shell's queues

| Parameter | Default | What it decides |
|---|---|---|
| `INST_DEPTH` | 16 | inbound instruction FIFO depth. **16 is a floor, not a preference** — the vendor synchronous FIFO refuses a shallower depth, and its behavioural model ends the simulation immediately, which presents as a bench producing no output rather than as an error |
| `RECV_DEPTH` | 32 | inbound data FIFO depth |
| `RECV_MEM` | `"block"` | the primitive behind the receive FIFO |

### The extension slot

`SIMD_EN` is 0 in the shipped configuration, and at 0 none of the extension is
elaborated: the unit is bit-identical to the plain RV32 PE. The remaining
`SIMD_*` parameters — lane counts, vector register count, accumulator count,
vector scratchpad size, the float unit counts — configure an occupant that is
[KohakuMPE's](../../../projects/kohakumpe/README.md), and their costs are that
project's numbers.

One convention worth knowing before reading them: in this parameter set **0
means not built**, not "one of". A float lane count of 0 removes the float tier
rather than building a single lane.

## The constraint to ask

**Constrain the PE at 3.333 ns.** The design's ceiling is set by a block RAM's
clock-to-out; asking for more cannot raise the frequency, and makes synthesis
spend LUT answering an impossible question.

That is measured on the shipped RTL, as a controlled comparison — same script,
same top, same configuration, same flow, **only the requested period differs**
(OOC synthesis, not routed, `xcvu13p-fhgb2104-2L-e`, Vivado 2024.2):

| request | LUT | achieved | slack |
|---|---|---|---|
| **3.333 ns** | **2,586** | **363.5 MHz** | **+0.582 ns** |
| 2.500 ns | 2,672 | 363.5 MHz | −0.251 ns |

**Zero megahertz gained, 86 LUT spent, and the tighter request is not even
met.** Flip-flops, block RAM, DSP and control sets are identical between the
two, so nothing structural changed — the 86 LUT is timing-driven duplication.

Against the measured 363.5 MHz, a 300 MHz fabric clock leaves 21 % of margin.
**That margin is a synthesis result and not a routed one**; treat it as a
screen, not as closure. [performance](performance.md#frequency) carries the
full conditions.

## Verifying a configuration

Four levels, one command each, every run bounded — per-case cycle ceilings, a
watchdog and spin caps, so a hanging core fails rather than hanging the bench:

```
python tests/pe/tools/rv_run.py --gate 1     # the core against a golden model, instruction by instruction
python tests/pe/tools/rv_run.py --gate 2     # the memory frontend against the protocol
python tests/pe/tools/rv_run.py --gate 3     # real software on the real memory substrate
python tests/pe/tools/rv_run.py --gate 4     # one, two and four PEs sharing a fabric and a memory agent
```

Level 1 co-simulates against a Python RV32I model, comparing PC, destination
and value for every committed instruction — and it runs the *configured* shape:
a variant with the predictor removed is verified as itself, never assumed from
the full build. Level 3 boots through the real window-write path and checks
halt word, completion code and a DRAM checksum. Level 4 is where the
communication protocol is proven between running cores rather than against a
bench.

Configuration knobs flow to the benches and to synthesis from the same
definition, so what levels 1–4 verified is what the synthesis measured — a knob
that reached only one of the two would silently decouple them.

## Where extensions attach

Nothing enters this baseline because it is normally found in a CPU. Each
addition is a separate experiment: add one feature, measure resources and
frequency, measure workload benefit. The multiply half of `RV32M` is the one
that has been through that process and shipped
([microarchitecture](microarchitecture.md#the-multiplier)); `Zbb`, a larger
predictor, a larger L1, multiple outstanding misses and atomics have not.

The seams an extension would use, named but not designed:

| Seam | Where |
|---|---|
| new functional units | the EX stage's ALU select, widened; the decoder already routes `funct3`/`funct7` |
| wide operands | a second register file beside the integer one, on the same two-boundary read timing |
| wide memory | the internal L1's line is already 256 bits at the fill boundary; only the CPU-side port is 32 |
| bulk peer transfer | a write-combining buffer in front of the requestor's push path |
| more miss concurrency | the requestor's single tag becomes a small MSHR table; the L1's blocking state machine is what changes |

## What integration deliberately does not offer

- **No second clock domain.** If your surrounding logic runs elsewhere, the
  crossing is outside the unit, not inside it.
- **No AXI port and no sideband.** Everything in and out is flits through the
  one local port. A PE that needed a private path to memory would not be a
  compute unit any more.
- **No parameter that removes the shell.** A unit without `noc_cu_base` is not
  a mesh compute unit; that configuration exists, and it is the
  [RV64 system core](../rv64-sys/README.md).
- **No runtime reconfiguration.** Every knob above is elaboration-time.
