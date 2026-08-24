---
title: PE performance
summary: What the controller PE costs and achieves — frequency, resources, instruction and memory timing, communication latency, and multi-core scaling.
tags:
  - architecture
  - pe
  - rv32
  - performance
---

# PE performance

The measured characteristics of the PE, in its one shipped configuration:
128-line L1, 2048-word windows, 32-entry BTB, `FWD_X` 1, LUTRAM register
file, `WR_MAX` 1.

| | LUT | FF | BRAM | DSP48 | Fmax | ask | flow |
|---|---|---|---|---|---|---|---|
| the PE, flattened | **2,491** | ~4,140 | **5** | 0 | **410.8 MHz** | 3.333 ns | flattened |
| the PE, hierarchy preserved | 2,477 | 4,140 | 5 | 0 | 377.9 MHz | 3.333 ns | `-flatten_hierarchy none` |

Resource and frequency figures are out-of-context synthesis on
`xcvu13p-fhgb2104-2L-e` (Vivado 2024.2, synth only — pre-placement, so a
routed result will be somewhat worse). Resource figures are CLB LUT
**sites**. Cycle figures are read from the PE's own `CTL_CYCLE` /
`CTL_INSTRET` counters, on the full system — real routers, the real memory
agent, RAM behind it. Every number on this page is this configuration.
**Both rows above are the same RTL** — the spread is the synthesis flow, and
the hierarchy-preserved row is the one to hold beside the SIMD PE, which is
measured the same way ([below](#where-this-pe-sits-among-the-classes)).

## Frequency

**The ceiling is 410.8 MHz — a 2.434 ns achieved period — and the design
reaches it at a 3.333 ns request** with 0.899 ns to spare. Constrain it
there. Below 3.333 ns the constraint buys nothing: the ceiling is a block
RAM's clock-to-out, and synthesis answers an impossible request by spending
LUT on a path whose length it cannot change — ~90 extra sites at a 2.5 ns
ask, 350–400 (13–15 % of the unit) at 2.2–2.0 ns, for zero megahertz.

At the mesh ship clock (`noc_clk` at 300 MHz) the PE carries better than
30 % timing margin.

### The critical path

```
u_spad/.../mem_reg_0/CLKBWRCLK  ->  u_core/u_id/x_op1_reg[16]/D      6 levels
```

The **load-data return**: a data array's clock-to-out, the cross-port
bypass, the sub-word extract, the forwarding network, the ID operand
register. It is the distance-3 forward of a load result — the one hazard
distance resolved by forwarding because a load's data exists no earlier —
and the array at its head is the scratchpad, whose
[cross-port bypass](microarchitecture.md#the-write-both-ports-can-make-at-once)
put a mux between the array and the extract. A scalar core with honest
synchronous memories ends at the speed a memory hands a word back, and this
one does.

## Resources

Per unit (hierarchical site accounting, taken at a tighter 2.5 ns request
where the whole PE is 2,583 — the split is the information; it moves only a
few sites across requests):

| Unit | Total LUT | Logic LUT | LUTRAM | FF | BRAM |
|---|---|---|---|---|---|
| **whole PE** | **2,583** | 2,215 | 368 | **4,140** | **5** |
| top glue: window writer, kick FSM | 183 | 183 | 0 | 413 | 0 |
| `u_base` — the framework attach | 657 | 409 | 248 | 1,381 | 0 |
| `u_core` — the RV32I pipeline | 1,187 | 1,115 | 72 | 1,013 | 0 |
| `u_l1` — internal L1, 128 lines | 364 | 316 | 48 | 413 | 1 |
| `u_req` — NoC requestor | 147 | 147 | 0 | 883 | 0 |
| `u_imem` — instruction window | 0 | 0 | 0 | 0 | 2 |
| `u_spad` — scratchpad | 45 | 45 | 0 | 37 | 2 |

Inside `u_core`: EX 418, ID 234, MEM 181, IF (with the whole predictor)
135, register file 129, WB 20, hazard/run/counters 70. The predictor fits
in the IF number because its entries live in LUTRAM depth rather than
logic.

Two more of these numbers are design outcomes rather than accounting. The
scratchpad is 45 LUT rather than the ~7 of a plain array because 38 of them
are the cross-port bypass that makes the doorbell correct — a priced
correctness cost, sitting on the critical path. And **a quarter of the unit
is the framework attach**: `u_base` is 657 LUT and 1,381 FF of port logic
that every compute unit on this fabric carries, processor or not — the
marginal cost of *this unit being a processor* is nearer 1,900 LUT.

The register file is LUTRAM. A block-RAM form exists behind `REGFILE_PRIM`
with identical timing, but a 32 × 32 register file leaves a 1K × 36
`RAMB36E2` 3.1 % depth-utilized — the worst ratio anything in this design
could post — so the LUTRAM form ships.

### BRAM depth

Every array that earns a tile fills the tile's natural depth at its aspect
(1K × 36 for a 32-bit port):

| Array | Words × width | Tiles | Depth used |
|---|---|---|---|
| instruction window | 2048 × 32 | 2 | **100 %** |
| scratchpad | 2048 × 32 | 2 | **100 %** |
| internal L1 data | 1024 × 32 | 1 | **100 %** |

Width is 88.9 % everywhere — a 36-bit face carrying 32 data bits — which is
the primitive, not a choice. The tag array is far too shallow to earn a tile
and stays LUTRAM, carrying `valid`/`dirty` beside the tag; that is what
makes the 128-line capacity nearly free on the same single tile.

## Where this PE sits among the classes

Every compute class on this fabric is measured out-of-context on the same part,
`xcvu13p-fhgb2104-2L-e`, Vivado 2024.2, synth only. Collected here so the
controller PE has something to be a fraction of.

**Every row is an assembled PE**, and the first column is the arithmetic it
ships with, because that is the thing that makes two LUT figures comparable or
not:

| class | the arithmetic it ships with | LUT | FF | BRAM | DSP48 | Fmax | **ask** |
|---|---|---:|---:|---:|---:|---:|---|
| **controller PE** `rv_pe` | RV32I only — no multiply, no divide, no float | **2,477** | 4,140 | 5 | 0 | 377.9 MHz | 3.333 ns |
| **SIMD PE**, 8 int + 4 float | SWAR packed int8/16/32, `vmul`, `vdot`; a float tier of **4 lanes** | **13,772** | 10,126 | 13 | 72 | 353.4 MHz | **2.857 ns** |
| **SIMT PE** `kht_pe`, 8 int + 8 float, 16 waves | per-thread RV32I **+ `RV32M`**; a float tier of **8 lanes** | **21,586** | 17,268 | 30.5 | 48 | 365.6 MHz | **2.857 ns** |

The float column of that table is a **width**, and only a width. There is one
dtype configuration in this machine — FP32 or FP16 operands in, **E8M15**
compute, FP32 or FP16 out — operand width is a per-instruction property rather
than a build option, and the internal format is never anything else. So the
three rows differ in whether a float tier is present and how many lanes it has;
they do not differ in format
([the PE classes](README.md#there-is-one-dtype-configuration-and-it-is-the-whole-design)).

Four more things about that table are load-bearing:

- **The ask column is not decoration.** The controller row is at the older
  3.333 ns request; the two wide rows at 2.857 ns. A tighter ask on this core
  buys LUT and no megahertz at all — 90 sites at 2.5 ns, 350–400 at 2.0 ns
  ([above](#frequency)) — so a controller figure lifted from a 2.857 ns run
  would be larger and no faster, and subtracting rows taken at different asks
  measures the constraint as much as the design.
- **The DSP48 counts are not comparable to the LUT counts.** The SIMD PE's 72
  includes `SIMD_DOTDSP = 1`, which keeps the `vdot` sum inside the DSP48 column
  and *buys LUT back* — 32 more DSP for a measured 256 LUT and 32 CARRY8 per
  eight lanes. DSP is the cheap resource on this device and LUT is the binding
  one, so the two classes are spending on the same axis in opposite directions
  and the totals still compare.
- **`div` and `rem` fault on every class**, per-thread included. `mul` faults on
  the controller and the SIMD PE and is built, per thread, on the SIMT PE.
- **Two flows are in that table.** The controller and DSP rows are
  `-flatten_hierarchy none`; the GPU row is `rebuilt`. Run both ways on the same
  GPU design at a 2.500 ns ask, the two flows differ by **+720 LUT and
  −4.1 MHz** for `none` — real, but small enough that the rows above stay
  comparable if the difference is named. **Compare within a flow where you
  can.** The controller
  PE also has a second, larger figure from a fully flattened run: **2,491 LUT at
  410.8 MHz**, 33 MHz of which is the flow rather than the design.

The one comparison the table supports without any qualification is the
**framework attach**, because every row carries the same one: `u_base` is 657
LUT and 1,381 FF of port logic that any compute unit on this fabric pays,
processor or not. On the controller PE that is a quarter of the unit; on the
SIMT PE it is 3.0 %.

### What a mesh of these costs

The mesh population is **8 SIMD PEs, 4 SIMT PEs and 2 controller PEs**, and its
float width is chosen rather than fallen into:

```
   8 x 4 float lanes  +  4 x 8 float lanes  =  64 FP FMA / clock
```

— one Mali-G610 shader core's width, exactly. On the figures above:

| | LUT | DSP48 | BRAM tiles | FF |
|---|---:|---:|---:|---:|
| 8 x SIMD PE | 110,176 | 576 | 104 | 81,008 |
| 4 x SIMT PE | 86,344 | 192 | 122 | 69,072 |
| 2 x controller PE | 4,954 | 0 | 10 | 8,280 |
| **total** | **201,474** | **768** | **236** | **158,360** |
| available | ~350,000 | 3,072 | — | — |

LUT is the binding resource and DSP is not — 768 of 3,072 — which is what makes
`SIMD_DOTDSP` and the DSP48-backed float lanes the right trade rather than a
convenience.

## Instruction timing

| Event | Cost |
|---|---|
| most instructions | 1 cycle |
| load-use, back to back | 2 stall cycles |
| load-use at a spacing of one | 1 stall cycle |
| taken branch, predicted | 0 |
| mispredict or unpredicted taken branch | 3 cycles |
| peer-window push | 1 cycle + hold until the requestor accepts |
| `ECALL` / `EBREAK` | halts; the completion carries the word |

## Memory timing

A scratchpad or control access is one cycle, always. A DRAM access hits in
one cycle; a miss is a line fill round trip through the fabric and the
memory agent — hundreds of cycles, dominated by the agent and DRAM, not the
PE. An eviction adds a one-descriptor writeback ahead of the fill; a
steady-state read-modify-write stream runs at ~30 cycles per evict-and-
refill pair against the real agent.

A flush-all runs at ~12 cycles per dirty line when acknowledgements return
promptly — 197 cycles for 16 dirty lines; against a slow agent each line
pays the acknowledgement latency instead, 677 cycles for the same 16. The
single outstanding write (`WR_MAX` 1) costs nothing anywhere else: a
blocking one-miss cache has a whole fill round trip between dirty
evictions, and the previous acknowledgement always arrives inside it. It is
what the [ordering rules](architecture.md#ordering) assume; an extension
adding miss concurrency must re-measure it.

An invalidate-all is a sweep: one cycle per line, pipeline held, nothing on
the wire.

## Communication

**A push-and-doorbell round trip between two running cores is 49 cycles** —
two window pushes, two hops each way, and two poll loops. The number is
quantised by the poll: a four-instruction poll loop is ~9 cycles, and a push
is observable only when the loop next comes round, so one extra iteration
costs a whole loop. (That quantisation is what the scratchpad's cross-port
bypass buys: a poll that sampled the array mid-push would read undefined
data and go round once more.)

**Two concurrent pairs cost exactly what one costs** — identical to the
cycle, on link-disjoint routes — so pairwise communication scales until
routes share a link.

All-to-one aggregation (workers push value-then-flag, the leader polls flags
only): three workers cost the leader **380 cycles and 10 instructions** over
one worker. The leader reads a value beside a flag it has seen with no
handshake back — the per-destination ordering rule doing the work.

## Multi-core scaling

One memory agent serves up to four PEs. The cost of sharing it, on a fixed
compute-bound program (cycles, identical instruction stream at every count —
the whole difference is memory):

| | 1 PE | 2 PEs | 4 PEs |
|---|---|---|---|
| the same program, kick to halt | 7,418 | 7,778 (+4.9 %) | 8,431 (+13.7 %) |

The +13.7 % is measured while the three neighbours run the heaviest memory
work in the suite, including a deliberate worst case: a copy whose source
and destination sit exactly one cache-size apart, so every access conflict
-misses — 26.6 cycles per instruction, the hardest load one PE can put on
the agent with no wasted instructions. A stride that maps source and
destination to the same sets is what a 4 KB direct-mapped cache punishes;
lay buffers out accordingly.

Two DRAM hand-offs (flush → doorbell → invalidate → re-read) running
concurrently through one agent cost the second pair **+159 cycles on the
write side and +175 on the read side** over the first — two blocking
flush-alls sharing the agent's write slots.
