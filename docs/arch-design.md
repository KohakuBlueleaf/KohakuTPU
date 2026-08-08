# Architecture

KohakuTPU: compute clusters on a NoC mesh, reaching DRAM through AXI4.
Target `xcvu13p-fhgb2104-2L-e` at **300 MHz**.

This is the entry point — the machine top to bottom, and how a matmul actually
flows through it. Every subsystem has its own document; this one exists so the
whole shape is in one place.

```
   {JTAG, PCIe}  <->  AXI4  <->  { DDR4, MAS, NoC mesh <-> clusters }
```

---

## 1. The machine

```
    host
      | AXI4
   +-------------------+
   | main orchestrator |  runs a control program: writes and polls, no branches
   +-------------------+  AXI master, NOT a NoC node
      | AXI4
   +-------------------+
   | MAG               |  memory engine + agent: staging RAM, dispatcher,
   +-------------------+  credits, NODE_STATUS mirror     <-> DRAM
      |
   ===== NoC mesh (288-bit flits, XY routing, credit-based) =====
      |                    |
   [cluster 0]        [cluster 1]   ...
    2 ports            2 ports
```

**The dispatch agent has no AXI master.** It never fetches an instruction from
DRAM: the host stages flits into its local RAM through the same AXI slave it
uses for the registers, then names a destination and kicks. And a cluster
fetches its own operands by issuing `MEM_RD_REQ` naming *itself* as source, so
the reply is delivered straight back and never passes through the agent. Those
two together are what keep dispatch a control plane rather than a data
bottleneck ([`noc/spec.md`](noc/spec.md) §10.3, [`isa/agent.md`](isa/agent.md)
§1).

**It has no NoC node either.** MAG's mesh attachments are **MAG ports**, one per
mesh row on the west edge, and the agent shares them — so a port is not a
*memory* port: it carries operand traffic and control traffic on the same wires.
Inbound each port demuxes by flit **type**, outbound a flit leaves from the port
on its destination's row, and the agent answers at port 0's coordinate. So
dispatch spreads across every attachment instead of leaving through one link,
and the mesh's north, south and east edges are free for what attaches next —
[`mas/spec.md`](mas/spec.md) §2.5.

**A port attaches to the NoC, not to a cluster.** Every cluster reaches every
port through the mesh, so which one its memory traffic leaves by is a routing
choice rather than a wiring one — see §2 for how clusters sit on the mesh.

The cost is that the staging window bounds how much work can be in flight, which
is what forces rounds — §4.1. The alternative, a fetching dispatcher, buys
unbounded programs and pays for them with a second master on the memory system,
competing with the clusters it is feeding.

## 2. A cluster, and why it takes two NoC ports

```
   NoC <-> cluster manager <-> tcu -> tcu -> tcu -> tcu -> acu <-> NoC
   port 0   L2 / L1              direct DSP cascade         port 1
            descriptors          (PCOUT/PCIN + W port)      resident tile
            program              256 DSP, 0 LUT             5 BRAM36
```

The four tensor CUs are **not** NoC nodes. They are wired to each other by the
DSP48E2 cascade, which is what makes them cost zero LUTs — the multiply *and*
the whole K=32 reduction happen inside the DSPs. Only the manager and the
accumulator face the network.

Two ports, not five, and the reason is arithmetic. The chain consumes
`A[4][32] + B[32][4]` every cycle — **eight 256-bit operand words** — and one
NoC port delivers **one**. Feeding the TCUs directly is an 8× deficit however
many ports you spend. Reuse closes it instead: a cluster computing a `Gm × Gn`
block of output sub-tiles from local operands needs

```
   words/cycle  =  4 (Gm + Gn) / (Gm · Gn)      =  0.375  at 16 x 32
```

so one port carries operands and one carries results. **64 NoC ports for 32
clusters, not 160.**

### 2.1 Where those two ports sit: managers outside, accumulators inside

A cluster is one **column** of a **band**, and a band is two mesh rows. Its
manager takes the local port of the band's **outer** row and its accumulator the
one **directly beneath**, so the two endpoints are on **adjacent** routers. With
two bands the second is **mirrored** against the bottom, so the accumulators meet
in the middle and every manager is on an outer row — two dataflow rings back to
back:

```
   NCL=8   4 cols x 4 rows        NCL=4   4 cols x 2 rows
   MAG(0,1) | mgr0 mgr1 mgr2 mgr3      MAG(0,1) | mgr0 mgr1 mgr2 mgr3
   MAG(0,2) | acu0 acu1 acu2 acu3      MAG(0,2) | acu0 acu1 acu2 acu3
   MAG(0,3) | acu4 acu5 acu6 acu7
   MAG(0,4) | mgr4 mgr5 mgr6 mgr7

   NCL=2   2 cols x 2 rows
   MAG(0,1) | mgr0 mgr1
   MAG(0,2) | acu0 acu1
```

**Adjacency is the property that matters.** A cluster's two attachments never
straddle another cluster's router, so nothing is interleaved and a fill response
travels one hop further than a result does.

**A band need not be full.** 3, 5, 6 and 7 clusters leave columns empty; those
counts are supported and merely not optimal, which is the driver's business to
report rather than the mesh generator's to forbid.

**MAG hangs off the west edge, one port per row**, and the north, south and east
edges are free for the vector unit and general core the plan still owes. Routing
is X-then-Y on clamped coordinates, so every cluster can reach every port:
columns nearer MAG exit by their manager's row, the farther half by their
accumulator's, which keeps both of a band's ports carrying. The links are full
duplex and a manager mostly *receives* while an accumulator mostly *sends*, so
the two rows load opposite directions rather than competing for one.
[`system.md`](system.md) §2.3 draws it against the bench that builds it.

**It costs about three points of peak at 8 CU** — `flops` 72.7% against 75.7%
on the old geometry — and the cost is routing, not memory service (`fetch` did
not move, `wslot_full` fell). What it buys is physical locality and simpler
program planning. **Layout tuning is deferred until the vector and general
cores exist**, because they attach to the edges this arrangement frees and will
change what an optimal placement is. [`perf.md`](perf.md) §0.1.

## 3. Numbers, in one place

```
   MXFP7 element     E5M3 scale shared by 32 + 7-bit signed significand
   accumulator       FP22  S1E7M14
   result            FP16
   cluster           4 x (4x8x4 TCU) + 1 accumulator = 512 MACs/cycle
   quantisation      K = 32, which is also the cluster's K span
   cluster Fmax      325.6 MHz, out-of-context -- an upper bound, not placed
```

| | per cluster | ×32 | ×45 |
|---|---|---|---|
| LUT | 17,521 | 560,672 | 788,445 (45.6%) |
| FF | 17,612 | 563,584 | 792,540 (22.9%) |
| BRAM36 | 5 | 160 | 225 (8.4%) |
| DSP | 272 | 8,704 | 12,240 (**99.6%**) |
| NoC ports | 2 | 64 | 90 |
| MACs/cycle | 512 | 16,384 | 23,040 |

At 300 MHz, 45 clusters is **~13.8 TFLOPS of AMP FP16-MXFP7**, DSP-bound. The
right-hand column is 45 rather than 48 because a cluster is 272 DSPs, not 256:
the cascade's 256 plus 16 in the accumulator, where the block scale is now
applied inside DSPs rather than in fabric
([`compute/accumulator.md`](compute/accumulator.md) §4.4).

**One cluster is what was synthesised.** The ×32 and ×45 columns are that one
measurement multiplied out — 12,288 / 272 = 45.2 — so they are budgets, and no
multi-cluster build has been placed or routed.
FLOPS rather than IOPS: MXFP7 is a floating-point format (a shared power-of-two
exponent and a significand), operands and results in memory are FP16, and the
integer datapath inside the DSP is how an MXFP7 multiply is implemented once the
shared exponent is factored out. See [`compute/matmul.md`](compute/matmul.md)
§3.0.

## 4. Workflow: how a matmul actually runs

```
   1  host writes a program into the orchestrator's staging buffer over AXI
             |
   2  orchestrator dispatches CU_INST flits to a cluster manager, credit-limited
             |
   3  FILL    manager walks a tensor descriptor, issues its own MEM_RD_REQ,
             |  assembles replies into 928-bit L1 entries
   4  GEMM    manager sweeps K OUTERMOST over the output sub-tiles, feeding the
             |  cascade one A entry and one B entry per cycle
             |  -> accumulator LOAD on the first K block, ADD after
   5  DRAIN   accumulator converts each sub-tile to FP16 and writes it back
             |  through its OWN port
   6  each instruction raises a completion signal; the orchestrator mirrors it
             |  into NODE_STATUS, the host polls over AXI
```

Two properties of step 4 are load-bearing rather than incidental:

**K is the outer loop.** Sweeping sub-tiles inside K means a given accumulator
address recurs only every `Gm·Gn` cycles instead of every cycle. That removes
the read-after-write recurrence entirely, which is what lets the resident tile
be a plain block RAM with a registered read — and deleted the three rotating
banks, the `EMIT` fold and the zero mask that the old K-inner order required.

**L1 is explicitly managed, never a cache.** No tags, no misses, no eviction.
The manager owns it and fills it by instruction, because only it knows the loop
structure. That is also what makes the memory instructions expressive enough to
cover convolution.

### 4.1 Anything larger than one tile is a loop in the driver

The machine holds exactly one tile of the problem: `gm*gn` output sub-tiles
resident in the accumulator, and `gm*nk` / `gn*nk` L1 entries for the two
operands. At the bench's capacities (`TILES = 512`, `GA = 128`, `GB = 256`, from
`tests/mas/mag_driver_tb.v`) that is a **64×128×256 pass**: 512 resident
sub-tiles is `Gm = 16` by `Gn = 32`, and `128/16 = 256/32 = 8` K blocks.
`C[256,256]` is then 8 output tiles, so 8 passes, each sweeping the whole of a
K = 256 problem at once.

The loop lives in `src/ktpu/hw/kernel.py`, not in hardware, because the
control ISA has no branches ([`isa/orchestrator.md`](isa/orchestrator.md) §2).
That is a deliberate trade and it is cheap: **the program is control, and the
instruction flits are data**. Flits go straight into the staging RAM as host
writes, so a program grows with its control flow rather than with the problem —
on a two-cluster GEMM, 55 commands became 15.

Three things bound how much of that loop can be resident at once, and the
tightest wins:

| bound | value | what it is |
|---|---|---|
| staging window | `STAGE_FLITS = 128` flits | the agent's staging RAM |
| command RAM | `NCMD = 128` commands | `main_orch`'s program store |
| dispatch credit | `INST_DEPTH - 1 = 31` passes | see below |

The credit bound is the one that is not obvious. Credit exists to keep
instructions in flight below one CU's instruction FIFO, because a full FIFO
raises `noc_in_busy`, which backpressures the mesh link the CU's own **memory
read responses** arrive on — so it can never drain the FIFO that is blocking it.
The counter is therefore seeded once per round rather than per kick, and a
program's last flit retires as `SIG_BATCH_COMPLETE`, which does not refund. The
full reasoning is in [`isa/agent.md`](isa/agent.md) and in `seed_credits` in
`src/ktpu/hw/device.py`; it is not worth re-deriving.

When the passes do not fit, they are cut into **rounds** — each a self-contained
upload / load / `GO` — and the clusters are round-robined into the list before
cutting, so no round is one cluster's work while the others sit idle. See
[`isa/kernel.md`](isa/kernel.md).

### 4.2 What it actually achieves, and why

Measured on `tests/mas/mag_driver_tb.v`: two clusters, real MAG, real AXI RAM.

| shape | run cycles | fill | gemm | drain | idle | MAC/cyc | GFLOP/s |
|---|---|---|---|---|---|---|---|
| 64×64×128 | 8,213 | 56.7% | 17.3% | 12.1% | 13.8% | 63.8 | 38.3 |
| 128×128×128 | 32,361 | 61.3% | 17.1% | — | — | 64.8 | 38.9 |
| 256×256×256 | 239,786 | 71.9% | 13.4% | 6.7% | 7.9% | 70.0 | 42.0 |

Peak for two clusters is 1,024 MAC/cycle — 4 TCUs each, 128 MACs per TCU —
which is **614 GFLOP/s at 300 MHz**. So this is 6–7% of peak, and the shares
above say exactly where the rest went: the machine is **fill-bound**. It spends
most of its time moving operands, and only about a sixth of it multiplying.

> **This table is the baseline, and it was acted on.** The 256-cube now runs in
> **18,701 cycles at 538.3 GFLOP/s — 87.6% of the same peak** — and eight
> clusters reach **1,856 GFLOP/s** on `512x1024x256`. The `fill` share fell from
> 71.9% to the low twenties. Current figures for every shape and cluster count
> are [`perf.md`](perf.md) §0; what each change was worth is
> [`optimization.md`](optimization.md) §I. The paragraphs below are the
> diagnosis that produced them, kept because the reasoning is what generalises —
> but note that the conclusion "only a larger resident tile moves the ratio" was
> **half right**: the tile did move (`TILES` 64 → 512, using BRAM depth already
> paid for), and the rest came from overlap, residency and a MAG port per
> mesh row. The rates are cycle counts converted at 300 MHz, and 300 MHz now
> closes: the cluster measures 325.6 MHz and `mag_mem_port` 330.0 MHz
> out-of-context, which is an upper bound rather than a placed result
> ([`perf.md`](perf.md) §0).
>
> **Both the table and the figures in this note predate the mesh layout change**
> (§2.1). They were measured when a cluster was a (row, left-column) pair with
> another cluster's manager between its two endpoints, not a column of a band.
> On the current layout 2 CU and 8 CU both pass end to end, and 8 CU measures
> `flops` **72.7%** where the old geometry measured 75.7% — about three points
> of peak, paid in routing rather than in memory service. Nothing here is a
> figure for the layout the RTL builds now; [`perf.md`](perf.md) §0.1 is.

**That is structural, not a scheduling failure.** A pass moves `(gm+gn)*nk` L1
entries in order to perform `gm*gn*nk` tile-ops, so its arithmetic intensity is

```
   tile-ops per entry  =  gm*gn / (gm + gn)      =  4  at gm = gn = 8
```

and `gm`, `gn`, `nk` are pinned by `TILES` and `L1_ENTRIES`. Reordering the
loops, cutting rounds differently, or adding clusters does not move that ratio —
only a larger resident tile does, which is §8.2 of
[`compute/matmul.md`](compute/matmul.md) arriving from the other direction:
the accumulator's capacity is the knob that sets operand bandwidth, and it is
currently set small. The measured trend agrees: going from a 64-wide to a
256-wide problem raises MAC/cycle from 63.8 to 70.0 and no further, because the
tile did not change.

> **The read-bandwidth figure is demand, not achieved.** 2.62 GB/s on the 256
> case is measured against a bench RAM that answers immediately. Real DRAM
> latency does not reduce the operand traffic; it lengthens the time spent
> waiting for it, so the fill share on hardware will be **higher** than the
> table shows, not lower. Read these numbers as an upper bound on what the
> current tile shape can do.

## 5. Convolution is a memory request

The compute instruction for a convolution is **byte-identical** to the one for a
matmul. Only the descriptor changes.

A tensor descriptor is an N-dimensional affine address generator with bound
axes. For `conv2d`, the activation at im2col row `(n, oy, ox)` and column
`(ky, kx, c)` is `input[n][oy·S+ky-P][ox·S+kx-P][c]` — affine in six loop
indices, with two bounded axes for padding:

```
   dim     n     oy     ox     ky    kx    c
   stride  sN    S·sH   S·sW   sH    sW    sC
   axis    -     H      W      H     W     -
```

Out-of-range addresses inject zeros and issue no memory request, so padding
needs no handling anywhere else in the machine. No im2col buffer is
materialised. See [`compute/tensor-isa.md`](compute/tensor-isa.md) §3.2.

## 6. The partition is the unit of design

A **partition** is a complete, self-contained machine: one MAG, one DRAM
channel, eight clusters, and a mesh that never leaves the die.

Design it once, verify it once, replicate it four times. **Scaling to the whole
device is then a parallelism problem, not a hardware one** — the same problem as
running across four cards, with the same answers available (data parallel,
tensor parallel, pipeline parallel) and the same tools for reasoning about it.

That framing is what makes the 4-SLR constraint a gift rather than a tax.

### 6.0 MAG — Memory Access Gateway

The combination of the memory engine and the dispatch agent needs a name,
because it is one module with three interfaces and it is the only thing in a
partition that talks to the outside world.

> **MAG = Memory Access Gateway.** It contains **MAS** (the memory engine:
> arbiter, and later TLB and cache) and the **agent** (dispatch, credits,
> `NODE_STATUS`). "MAS" keeps its meaning as the memory half; MAG is the
> package. Working name — easy to change, hard to change later.

```
   AXI MASTERS      JTAG        XDMA        main orchestrator
                      │           │                │
                      └───────────┴────────┬───────┘
                                           │  AXI4 512b
                                  ┌────────┴────────┐
                                  │  SmartConnect   │
                                  └────────┬────────┘
                       ┌───────────────────┼──────────────┬─────────┐
   AXI SLAVES          │                   │              │         │
                 main orchestrator       MAG0           MAG1  ...  MAG3
                 (its own regs:            │
                  programs, IRQ,           │
                  global status)           │
   ════════════════════════════════════════╪═══════════════════════════════
    PARTITION  (one SLR)                   │
                                           │
   ┌──────────────────────────────────┴────────────────────────────────────┐
   │  MAG                          AXI slave IO                            │
   │                                    │                                  │
   │                             address decode                            │
   │                        ┌───────────┴───────────┐                      │
   │             memory range│                      │control range         │
   │                         ▼                      ▼                      │
   │              ┌──────────────────┐   ┌─────────────────────┐           │
   │              │ MAS              │   │ agent               │           │
   │              │  memory ports    │◄─►│  staging RAM        │           │
   │              │  (TLB later)     │   │  dispatcher, credits│           │
   │              │  (cache later)   │   │  NODE_STATUS mirror │           │
   │              └────────┬─────────┘   └──────────┬──────────┘           │
   │                       │  share layer (s2.5):   │                      │
   │                       │  in : by FLIT TYPE     │                      │
   │                       │  out: by DEST ROW      │                      │
   │                  AXI master IO           NoC ports x MEM_PORTS        │
   └───────────────────────┬────────────────────────┬──────────────────────┘
                           │ AXI4 512b              │
                  ┌────────┴────────┐               │
                  │   DDR4 / URAM   │      ═════════╪═════════ mesh ══════
                  │   19.2 GB/s     │               │
                  └─────────────────┘        8 clusters (2 ports each),
                                             MAG on the WEST edge only
                                             + vector / general cores later,
                                               on the N/S/E edges left free
```

The agent's arrow and the memory ports' arrow are the **same wires**. MAG has
`MEM_PORTS` mesh attachments and no others: inbound each port demuxes by flit
type, outbound an agent flit leaves from the port on its destination's row, and
the agent answers at port 0's coordinate. See [`mas/spec.md`](mas/spec.md) §2.5.

**The DRAM belongs to the MAG**, not to the interconnect. That is the whole
trick: the SmartConnect's slave list holds MAGs where it used to hold DDR
controllers, so nothing was added to it, and each MAG arbitrates host traffic
against NoC traffic using the arbiter it needs anyway.

**The main orchestrator is both master and slave.** A slave so the host can load
programs and read global status; a master so it can push dispatch into MAG
control ranges. It is no longer a NoC node at all — its reach into the mesh is
an AXI write that a MAG turns into a flit.

```
   who          role     what it does
   ─────────────────────────────────────────────────────────────────────
   JTAG         master   bring-up, debug, poking anything
   XDMA         master   bulk tensor upload -> MAG memory range -> DRAM
   main orch    master   dispatch -> MAG control range -> NoC packet
   main orch    slave    host loads programs, polls global status
   MAG x4       slave    memory range and control range
```

Every arrow into the machine is an AXI write whose *address* decides whether it
is data, control, or a NoC packet. There is no second mechanism.

```
   per partition      8 clusters x 512 MACs   =  4,096 MACs/cycle
                      at 300 MHz              =  2.46 TFLOPS
                      2,176 DSP of 3,072         71% of the SLR
                      140 kLUT of 432 k          32%
                      1 DDR4 channel             19.2 GB/s
                      20 NoC attachments         a 4x4 router grid: 16 cluster
                                                 ports (8 clusters x 2, one
                                                 column of a band each) plus 4
                                                 MAG ports off the west edge.
                                                 N/S/E free, room to grow
   x4                                         =  9.83 TFLOPS
```

Everything below is one partition unless it says otherwise.

The VU13P is a **4-SLR** device, and that is not a placement detail to be
handled later — it is why the partition boundary sits where it does, because
two things in this design physically cannot cross a die boundary.

```
   JTAG / XDMA
        │  AXI4
   ┌────┴──────────┐
   │ SmartConnect  │
   └──┬─────────┬──┘
      │         └──────────────── bulk weight upload ──────────► DDR
      │  AXI-Lite, long and latency-insensitive
   ┌──┴───────────────────────────────────────────┐
   │  main orchestrator: address map, global       │   NOT a NoC node
   │  status aggregation, IRQ                      │
   └──┬────────┬────────┬────────┬─────────────────┘
      │        │        │        │   AXI writes into each MAG's control range
   ┌──┴──┐  ┌──┴──┐  ┌──┴──┐  ┌──┴──┐
   │MAG0 │  │MAG1 │  │MAG2 │  │MAG3 │   MAS + agent, ONE module each
   └┬─┬──┘  └┬─┬──┘  └┬─┬──┘  └┬─┬──┘
    │ └DDR0  │ └DDR1  │ └DDR2  │ └DDR3   one AXI master per memory port
   ═╪════════╪════════╪════════╪═══ NoC mesh ═══
    │        │        │        │   MEM_PORTS attachments each, one per mesh
    │        │        │        │   row. The AGENT SHARES THEM -- there is no
    │        │        │        │   separate agent node, and no east-edge link.

   SLR 0      SLR 1     SLR 2     SLR 3
   8 clusters each
```

### 6.1 What cannot cross an SLR

**The DSP cascade.** `mx_cluster_core` chains 8 DSP48E2 per column through
`PCOUT → PCIN`, a dedicated route between vertically adjacent DSPs in the same
column. There is no such route across a die boundary. **Each 8-DSP chain must be
contiguous within one SLR**, which in practice means each cluster is
SLR-resident and needs a `pblock`. Left to the placer this is not a warning — it
is an unroutable design or a silently relocated cascade.

That single constraint is why the floorplan is 8 clusters per SLR rather than
whatever the placer prefers.

**Backpressure, without help.** The NoC uses busy/valid, not ready/valid, so a
link's backpressure must arrive in time. Registering `data`/`valid` forward
across an SLL is easy; registering `busy` backward lengthens the backpressure
loop, and the sender must then hold everything already in flight. Cross-SLR mesh
links therefore need **skid buffering sized to the round-trip**, which is a
design decision, not a constraint file.

### 6.2 Should the mesh cross SLRs at all? Yes — but rarely used

It must be *possible*: address slicing sends a request to whichever MAS slice
owns the line, and nothing guarantees that is the local one.

It should be *rare*: the compiler places tensors so a cluster's traffic lands on
its own SLR's slice, making cross-SLR hops the exception. The mesh provides the
capability; the schedule avoids needing it.

The alternative — four disconnected per-SLR meshes — is simpler and worse. It
forces every tensor to be partitioned or replicated per die, and replicating
weights across four channels wastes both DRAM capacity and the bandwidth used
to write them.

### 6.3 The orchestrator splits in two — done

**This rework has happened.** `noc_orchestrator.v` used to be one module that
was both an AXI4 slave and a NoC node. In this topology the main orchestrator
has no NoC port at all, so it divided:

| | goes to | why |
|---|---|---|
| staging RAM, dispatcher, credits | **agent**, inside MAS | must be next to the mesh ports it feeds |
| TX/RX raw flit mailbox | **agent** | same |
| `NODE_STATUS` mirror | **agent**, for its own quadrant | signals arrive on those ports |

> "The mesh port it feeds" was singular, and is now the **memory ports, plural**.
> The agent has no attachment of its own: it rides all of MAG's, told apart from
> the memory engine by flit type — [`mas/spec.md`](mas/spec.md) §2.5.
| AXI address map, CAPS, IRQ | **main** | one host-facing interface |
| global status aggregation | **main** | the only thing with a whole-machine view |

The agent was ~90% of the old orchestrator, so the rework was largely
re-parameterising which nodes it owns and reaching it over AXI rather than it
being an AXI slave in its own right. `noc_orchestrator.v` still holds the agent
logic; `src/kohakumas/mag.v` instantiates it and wires the control window to it,
and `src/kohakuaxi/main_orch.v` is the new host-facing half.

The main orchestrator is now an **AXI master** that writes into control
address ranges (§6.4) — it is no longer a NoC node, and it no longer needs a
private control fabric to the agents, because the SmartConnect it already sits
on reaches them. The register maps of both halves are in
[`isa/orchestrator.md`](isa/orchestrator.md) and [`isa/agent.md`](isa/agent.md).

### 6.4 MAS is an AXI adapter, bound to its memory

The clean formulation. MAS is **not a new master on the interconnect** — it is a
**slave**, and it takes ownership of the memory behind it:

```
                       ┌── MAS0 ──┐                    each MAS:
   JTAG / XDMA ──┐     ├── MAS1 ──┤  master IO ──►      AXI slave  IO  (host side)
                 ├─SMC─┤          │   ──► RAM/DDR       AXI master IO per port
   main orch ────┘     ├── MAS2 ──┤   + agent           MEM_PORTS NoC ports,
                       └── MAS3 ──┘                       SHARED with the agent
                                                        address decode
```

**Nothing is added to the main SmartConnect.** Its slave list previously held
four DDR controllers; now it holds four MAS instances, each of which owns a DDR
controller. Same port count, same crossbar size — the memory simply moved one
level down, behind an adapter.

That single change resolves several things at once.

**Control and data share one path, distinguished by address.** MAS decodes its
slave port into ranges:

```
   memory range    forwarded to the AXI master  ->  DDR / URAM
   control range   MAS registers, and NoC packet injection
```

A write into the control range **becomes a NoC packet**. So the host can inject
control traffic, and so can the main orchestrator, and neither needs a private
path to do it — the address map already distinguishes them.

**The orchestrator agent lives inside MAS.** It was going to be a separate NoC
node next door with its own port; instead it is part of the adapter, reachable
by the same AXI writes. The main orchestrator becomes an **AXI master** issuing
writes into control ranges, rather than a NoC node — which was the rework §6.3
identified, now with a much simpler answer.

**And it has no port inside the adapter either.** Moving the agent in without
merging its attachment left MAS hanging off two opposite edges of the mesh, with
every dispatch in the machine leaving through the one east link. It now shares
the memory ports — demuxed inbound by flit type, steered outbound by destination
row — so dispatch spreads across every attachment and the north, south and east
edges are free for the vector unit and general core this design still owes.
[`mas/spec.md`](mas/spec.md) §2.5.

**The host gets NoC access for free.** JTAG/XDMA can inject control packets by
writing the same ranges, which makes bring-up and debug a matter of `jaxi::write`
rather than a dedicated mechanism. That is the same argument that put the AXI↔NoC
bridge before MAS in the original plan, arrived at from the other direction.

**Two masters per channel collapse to one.** The earlier design needed a 2×1
SmartConnect per channel to arbitrate the host against MAS. Now the host's
traffic *arrives through* MAS, so MAS arbitrates it against NoC-side traffic
using the arbiter it already has. One less IP per channel.

### 6.4a Scope: build one partition

Everything above describes one partition, and that is deliberately where the
work stops for now. **Four partitions is a parallelism problem, not a hardware
one** — the same problem as four cards in a box.

Two ways to connect them later, both deferred:

```
   one large mesh, 4 NUMA partitions    cross-partition access is just a NoC
                                        packet. Needs cross-SLR links with skid
                                        buffering for busy/valid backpressure.

   MAG <-> MAG memory copy              an explicit DMA-style command. Movement
                                        happens on AXI, where latency is
                                        tolerable, and nothing crosses the mesh.
```

The second looks more likely to win, because it puts inter-partition traffic on
the interconnect built for long distances instead of on the mesh built for short
ones — and because an explicit copy matches how the rest of this design already
works: L1 and L2 are explicitly filled, so an explicitly copied partition
boundary is the same idea one level up.

Neither is needed to run a real workload on one partition, which is the point.

### 6.5 What this does to the partitioning question

It largely dissolves it, because **the host reaches every channel through the
SMC**. Placement is a host-side decision with no NoC involvement: upload a
tensor to whichever channel should hold it, including four copies of a shared
weight matrix if that is what the schedule wants.

At runtime, a cluster talks to its **local** MAS and reads memory bound to that
MAS. No cross-SLR NoC traffic, no slice-granularity question, no NUMA hotspot —
those all came from assuming a cluster might need a remote slice.

The cost is that the working set of an SLR's 8 clusters must *be* in that SLR's
memory, which for matmul means replicating the shared operand. Weights are small
against 16 GB — a 1024×4096 FP16 layer is 8 MB, so four copies is 32 MB — and
the price is paid once at upload.

> **If remote runtime access is ever needed**, the escape hatch is small: put
> the DDR controllers on the SMC as slaves alongside MAS, so a MAS master port
> can reach any channel. Cross-SLR movement then happens on **AXI**, where
> latency is tolerable and SmartConnect handles the pipelining — rather than on
> the NoC, where busy/valid backpressure across an SLL needs skid buffers. That
> is the right place for it, and it is additive.

## 7. Where to read next

| you want | read |
|---|---|
| **what the machine actually executes, at every level** | [`isa/README.md`](isa/README.md) |
| how a GEMM of any size becomes passes and rounds | [`isa/kernel.md`](isa/kernel.md) |
| the descriptor ISA as designed (not what runs) | [`compute/tensor-isa.md`](compute/tensor-isa.md) |
| the matmul datapath and formats | [`compute/matmul.md`](compute/matmul.md) |
| DSP48E2 packing and the cascade | [`compute/matmul-circuit.md`](compute/matmul-circuit.md) |
| what is built, measured resources | [`compute/matmul-impl.md`](compute/matmul-impl.md) |
| accumulator precision and the road to 300 MHz | [`compute/accumulator.md`](compute/accumulator.md) |
| the machine running end to end | [`system.md`](system.md) |
| packet format, routing, CU interface | [`noc/spec.md`](noc/spec.md) |
| memory access, TLB, the two caches | [`mas/README.md`](mas/README.md) |
| how to run and write benches | [`simulation.md`](simulation.md) |

## 8. Status

```
   matmul datapath        built, exact against both DSP models
   FP22 accumulator       327.7 MHz, 5 BRAM36
   2-port cluster         325.6 MHz, 17,521 LUT, 17,612 FF, 272 DSP, 5 BRAM36,
                          0 URAM -- clears 300 by 8.5%, out-of-context
   quantiser              BUILT -- src/kohakumas/mx_quant.v, FP16 -> int7+E5M3
                          on the way out of MAG, checked bit-for-bit against
                          the model in src/ktpu/hw/mxfp7.py
   MAG                    BUILT -- src/kohakumas/mag.v: MEM_PORTS memory ports,
                          an AXI master each, write slots, and the dispatch
                          agent SHARING those ports rather than holding a mesh
                          attachment of its own -- mas/spec.md s2.5
   orchestrator           SPLIT -- src/kohakuaxi/main_orch.v is the AXI-facing
                          command-RAM machine; the agent (staging, dispatch,
                          credits, NODE_STATUS) lives inside MAG and is no
                          longer a NoC node at all
   arbitrary-size GEMM    BUILT, in the DRIVER -- src/ktpu/hw/kernel.py
                          tiles any shape onto one hardware tile and streams
                          the passes as rounds
   tensor descriptors     built and conv2d im2col validated, but NOT wired into
                          the fill engine -- FILL still takes a base and a count
   SLR pblocks            not written -- and the DSP cascade makes them a
                          correctness requirement, not an optimisation
   TLB, cache             not started, and v1 needs neither
```

Everything above is exercised by `tests/mas/mag_driver_tb.v`, a two-cluster
partition with a real MAG and a real AXI RAM, driven by the real driver.

> The vector and general-purpose units in the original sketch are not built.
> The FP8→FP12→FP16 tensor core they were designed around has been superseded by
> the MXFP7 design; [`compute/arithmetic.md`](compute/arithmetic.md) and
> [`compute/costs.md`](compute/costs.md) retain its measured numbers, which are
> still the only baseline for the FP16 ALU path.

> The tensor-descriptor ISA in [`compute/tensor-isa.md`](compute/tensor-isa.md)
> is the *agreed* design and the walker is built, but the instruction set the
> machine actually runs is the three-opcode one in
> [`isa/cluster.md`](isa/cluster.md). Where they disagree, `isa/` is what runs.
