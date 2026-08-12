---
title: Parameters
summary: Every parameter of every framework module — type, default, what it controls, and legal range.
tags:
  - spec
  - normative
  - reference
  - parameters
---

# Parameters

> **Kind: reference.** Defaults are defaults, not requirements. Where a value is
> genuinely constrained the "legal range" column says so, and those constraints
> are Fixed.

Exhaustive lookup. Every parameter of every framework module, grouped by the role
the module plays rather than by which package currently holds it.

Compute-unit parameters are not here: a unit's parameters are the unit's, and
that includes every parameter describing its local memory — width, depth,
primitive, read latency. The framework has no opinion on those and this document
will not acquire one. The parameters of `noc_cu_base` *are* here, because that
module is the framework's.

**Derived parameters.** Several modules declare a parameter that is computed from
the others, because Verilog needs it before the port list. Those are marked
**derived** and **MUST NOT** be overridden. Overriding one elaborates cleanly and
builds something else.

## 1. Cross-cutting constants

These appear in many modules and **MUST** hold the same value in all of them. A
mismatch between two modules on any of these is a silent structural error: it
elaborates, and the flits are misparsed.

| Name | Type | Default | Controls | Legal range |
|---|---|---|---|---|
| `FLIT_WIDTH` / `DATA_WIDTH` (mesh) | integer | `288` | The width of a flit, and therefore of every mesh link, FIFO and register carrying one. | **Effectively fixed at 288.** The header is parameterised, but every payload field position in the framework is a literal part-select. Changing this changes the payload width and silently invalidates every descriptor decode. |
| `POS_WIDTH` | integer | `4` | Coordinate width, and therefore the maximum mesh extent (16×16 including edge endpoints). | **Effectively fixed at 4.** The driver packs `{y,x}` into one byte, and the orchestrator's status mirror is `1 << (2*POS_WIDTH)` words inside a 4 KB decode window, which overflows above 4. |
| `DATA_W` (AXI) | integer | `256` | AXI data width on the memory path. Equals the flit payload width by design, so nothing in the path has to gear between them. | Must equal the flit payload width, i.e. `FLIT_WIDTH - 4*POS_WIDTH - 16`. A wider DRAM interface is converted below the memory agent, not here. |
| `ADDR_W` | integer | `34` | Physical address width. Exactly the 16 GB map, deliberately not widened. | The flit's `addr` field is 34 bits. Values above 34 cannot be expressed on the mesh. Bits 33:32 additionally carry the mesh id on a memory request. |
| `ID_W` / `ID_WIDTH` | integer | `4` | AXI ID width. | Any. Must match across a master/slave pair; `axi_n1` widens it by its own index field. |
| `GRID_LO` | integer | `1` | Lowest router coordinate, both axes. Endpoints live outside the grid and are reached by the coordinate clamp. | `>= 1`. Coordinate 0 is the edge, and the four corners must be empty. |
| `GRID_HI` | integer | `14` (router, orchestrator), `2` (memory agent) | Highest router coordinate when the grid is square. | `>= GRID_LO`, and `< 2**POS_WIDTH - 1`. **The two defaults disagree; a mesh top MUST set both explicitly.** |

## 2. Mesh and routing

### `NoCRouter` — `src/kohakunoc/noc_router.v`

Five ports: north, east, south, west, local.

| Name | Type | Default | Controls | Legal range |
|---|---|---|---|---|
| `DATA_WIDTH` | integer | `288` | Flit width on all five ports. | See §1. |
| `FIFO_DEPTH` | integer | `32` | Per-input-port buffer depth. Only has to cover the backpressure round trip; depth does not prevent deadlock, XY routing does. | Power of two. |
| `MEMORY_TYPE` | string | `"distributed"` | Storage primitive for those buffers. | `"distributed"`, `"block"`, `"ultra"`. |
| `POS_WIDTH` | integer | `4` | Coordinate width. | See §1. |
| `POS_X` | integer | `1` | This router's X coordinate. | `GRID_LO`..`GRID_X_HI`. |
| `POS_Y` | integer | `1` | This router's Y coordinate. | `GRID_LO`..`GRID_Y_HI`. |
| `GRID_LO` | integer | `1` | Clamp lower bound, both axes. | See §1. |
| `GRID_HI` | integer | `14` | Clamp upper bound when square. | See §1. |
| `GRID_X_HI` | integer | `GRID_HI` | Clamp upper bound on X. Set separately for a rectangular mesh. | `>= GRID_LO`. |
| `GRID_Y_HI` | integer | `GRID_HI` | Clamp upper bound on Y. | `>= GRID_LO`. |

The clamp bounds are also what the router derives its **turn masks** from: which
neighbours are routers rather than edge endpoints, and therefore which turns XY
routing can never ask for. A wrong bound presents as a **hang**, not a wrong
answer — the request is never granted and the input port's holding slot never
clears. There is a simulation check that names it at the router.

### `InPortSwitch` — `src/kohakunoc/noc_inport.v`

One per router input. Buffers arriving flits, computes the output direction for
the head, offers it through a single holding slot.

| Name | Type | Default | Controls | Legal range |
|---|---|---|---|---|
| `DATA_WIDTH` | integer | `288` | Flit width. | See §1. |
| `FIFO_DEPTH` | integer | `32` | Buffer depth. | Power of two. |
| `MEMORY_TYPE` | string | `"distributed"` | Storage primitive. | `"distributed"`, `"block"`, `"ultra"`. |
| `POS_WIDTH` | integer | `4` | Coordinate width. | See §1. |
| `POS_X` | integer | `1` | Owning router's X. | As `NoCRouter`. |
| `POS_Y` | integer | `1` | Owning router's Y. | As `NoCRouter`. |
| `GRID_LO` | integer | `1` | Clamp lower bound. | See §1. |
| `GRID_HI` | integer | `14` | Clamp upper bound when square. | See §1. |
| `GRID_X_HI` | integer | `GRID_HI` | X clamp. | `>= GRID_LO`. |
| `GRID_Y_HI` | integer | `GRID_HI` | Y clamp. | `>= GRID_LO`. |

### `OutPortSwitch` — `src/kohakunoc/noc_outport.v`

One per router output. Round-robin across the five input heads, and the register
driving the outbound link.

| Name | Type | Default | Controls | Legal range |
|---|---|---|---|---|
| `DATA_WIDTH` | integer | `288` | Flit width. | See §1. |

## 3. Compute-unit endpoint

### `noc_cu_base` — `src/kohakunoc/noc_cu_base.v`

The framework side of every compute unit. Contract:
[compute-unit-port.md](compute-unit-port.md).

| Name | Type | Default | Controls | Legal range |
|---|---|---|---|---|
| `FLIT_WIDTH` | integer | `288` | Flit width. | See §1. |
| `POS_WIDTH` | integer | `4` | Coordinate width. | See §1. |
| `POS_X` | integer | `2` | This endpoint's X coordinate. Stamped into every flit the base sends. | Anywhere the mesh's clamp can reach, including outside the router grid. |
| `POS_Y` | integer | `2` | This endpoint's Y coordinate. | As above. |
| `CU_TYPE` | 16-bit | `16'h0000` | Published as `CU_CAPS[63:48]`. Identifies the unit type to the driver. | Any. SHOULD be two printable ASCII characters. Not centrally allocated. |
| `CU_VERSION` | 8-bit | `8'h01` | Published as `CU_CAPS[47:40]`. A **mesh-wide build number**, not this endpoint's revision. | Any. MUST be identical across every endpoint in one image, and MUST be bumped when any instruction set or datapath changes. |
| `N_BUFFERS` | integer | `4` | Published as `CU_CAPS[39:36]`. How many `CU_DATA` buffer indices the unit accepts, counting from 0. | 0–15. MUST match what the unit actually accepts. |
| `INST_DEPTH` | integer | `32` | Instruction FIFO depth, and the value published as `CU_CAPS[35:20]`. Bounds how much dispatch credit a host may seed. | Power of two. |
| `RECV_DEPTH` | integer | `16` | Receive FIFO depth, in flits. **This is what bounds how far a requester may run ahead**, and therefore how much memory latency it can hide. | Power of two. |
| `MEM_TYPE` | string | `"distributed"` | Storage primitive for the instruction FIFO. | `"distributed"`, `"block"`, `"ultra"`. |
| `RECV_MEM` | string | `"distributed"` | Storage primitive for the receive FIFO. Separate knob because the receive queue is the widest structure in the module and the right answer differs from the instruction FIFO's. | `"distributed"`, `"block"`, `"ultra"`. |

`RECV_MEM` cannot weaken backpressure: the full flag is derived from the pointers
whichever memory backs the data. What it moves is `recv_flit` onto a block-RAM
output register, in front of whatever reads it combinationally.

### `noc_cu_null` — `src/kohakunoc/noc_cu_null.v`

The minimum conforming compute unit: all the mesh obligations, none of the
compute. A measurement instrument and a template.

| Name | Type | Default | Controls | Legal range |
|---|---|---|---|---|
| `FLIT_WIDTH` | integer | `288` | Flit width. | See §1. |
| `POS_WIDTH` | integer | `4` | Coordinate width. | See §1. |
| `POS_X` | integer | `1` | Endpoint X. | As `noc_cu_base`. |
| `POS_Y` | integer | `1` | Endpoint Y. | As `noc_cu_base`. |
| `CU_TYPE` | 16-bit | `16'h0000` | Passed through to `CU_CAPS`. | Any. |
| `INST_DEPTH` | integer | `32` | Instruction FIFO depth. | Power of two. |
| `MEM_TYPE` | string | `"distributed"` | Instruction FIFO primitive. | `"distributed"`, `"block"`, `"ultra"`. |

## 4. Control plane

### `noc_orchestrator` — `src/kohakunoc/noc_orchestrator.v`

AXI4 slave to mesh local port: flit mailbox, instruction dispatch, status mirror.
Register map: [control-registers.md](control-registers.md) §2.

| Name | Type | Default | Controls | Legal range |
|---|---|---|---|---|
| `DATA_WIDTH` | integer | `64` | AXI data width. A 288-bit flit is five beats at 64. | Must divide the flit into a whole number of beats with padding; the module computes `FLIT_WORDS` by rounding up. |
| `ADDR_WIDTH` | integer | `32` | AXI address width. Only the low 16 bits are decoded. | `>= 16`. |
| `ID_WIDTH` | integer | `4` | AXI ID width. | Any. |
| `FLIT_WIDTH` | integer | `288` | Flit width. | See §1. |
| `POS_WIDTH` | integer | `4` | Coordinate width. Also sizes the status mirror, at `1 << (2*POS_WIDTH)` words. | See §1. **Above 4 the mirror overflows its 4 KB decode window.** |
| `GRID_LO` | integer | `1` | Published in `CAPS`, so software can size itself. | See §1. |
| `GRID_HI` | integer | `14` | Published in `CAPS`. | See §1. |
| `ORC_X` | integer | `1` | This orchestrator's own X coordinate, stamped into every dispatched flit as the source so targets can reply without configuration. | Must be the coordinate at which the orchestrator is actually reachable. |
| `ORC_Y` | integer | `1` | Its Y coordinate. | As above. |
| `TX_DEPTH` | integer | `16` | Transmit FIFO depth, shared by the dispatcher and the mailbox. | Power of two. |
| `RX_DEPTH` | integer | `16` | Receive FIFO depth. `CU_SIGNAL` bypasses it, so this sizes only `CU_CTRL` replies and other unhandled traffic. | Power of two. |
| `STAGE_FLITS` | integer | `128` | Instruction staging RAM depth, in flits. Sets how many flits can be staged across all pending programs. | Any. The staging window is `STAGE_FLITS * FLIT_WORDS * 8` bytes and at 128 it already exceeds one 4 KB page. |

### `main_orch` — `src/kohakuaxi/main_orch.v`

The host-side control-program engine. Register map:
[control-registers.md](control-registers.md) §5.

| Name | Type | Default | Controls | Legal range |
|---|---|---|---|---|
| `ADDR_W` | integer | `32` | Address width of both its slave and its master. | `>= 16`. |
| `ID_W` | integer | `4` | AXI ID width. | Any. |
| `NCMD` | integer | `128` | Command slots. | `NCMD * 32 <= 0x1000`, i.e. at most 128 at the current window size. |
| `POLL_IVL` | integer | `31` | Cycles between `POLL` retries. | Any. Larger reduces read traffic on a slow interconnect. |

### `axi_xbar2` — `src/kohakuaxi/axi_xbar2.v`

A 2-master, 2-slave crossbar standing in for a vendor interconnect in
simulation. One outstanding transaction per direction, whole transactions granted
at a time, no interleaving, no width conversion.

| Name | Type | Default | Controls | Legal range |
|---|---|---|---|---|
| `ADDR_W` | integer | `32` | Address width. | `> SEL_BIT`. |
| `DATA_W` | integer | `64` | Data width, both sides. | Any. |
| `ID_W` | integer | `4` | AXI ID width. | Any. |
| `SEL_BIT` | integer | `28` | The single address bit that selects slave 1 over slave 0. | `< ADDR_W`. |

### `InstReceiver` — `src/kohakuaxi/instruction_receiver.v`

An AXI4 slave that accepts instruction words into a FIFO. Predates the
orchestrator's staging path.

| Name | Type | Default | Controls | Legal range |
|---|---|---|---|---|
| `INSTRUCTION_DEPTH` | integer | `16` | FIFO depth. | Power of two. |
| `DATA_WIDTH` | integer | `64` | AXI data width. | Any. |
| `ADDR_WIDTH` | integer | `64` | AXI address width. | Any. |
| `STRB_WIDTH` | integer | `DATA_WIDTH/8` | **Derived.** Write-strobe width. | Do not override. |
| `ID_WIDTH` | integer | `4` | AXI ID width. | Any. |

## 5. Memory agent

### `mag` — `src/kohakumas/mag.v`

The single point where a partition touches everything outside it. Protocol:
[memory-protocol.md](memory-protocol.md).

| Name | Type | Default | Controls | Legal range |
|---|---|---|---|---|
| `FLIT_WIDTH` | integer | `288` | Flit width. | See §1. |
| `POS_WIDTH` | integer | `4` | Coordinate width. | See §1. |
| `DATA_W` | integer | `256` | AXI memory width. Equals the flit payload by design. | See §1. |
| `ADDR_W` | integer | `34` | Physical address width. | See §1. |
| `ID_W` | integer | `4` | AXI ID width. | Any. |
| `MEM_PORTS` | integer | `1` | How many mesh memory endpoints this agent presents. **This is the unit the machine grows by**, not a tuning knob: a port owns its intake, read engine, transform and AXI channel. | 1–4. Four coordinate pairs are declared. |
| `ILINK` | integer | `0` | Build the interlink. **Zero generates none of it** — no switch, no links, no extra AXI master, and the remote decode folds to a constant false. | 0 or non-zero. |
| `MESH_ID` | integer | `0` | This mesh's id when the interlink is absent. With the interlink present the id is a runtime register instead. | 0–3. |
| `LINK_W` | integer | `288` | Interlink beat width. One beat is one flit. | Must match at both ends and in every pipe stage. |
| `TUSER_W` | integer | `96` | Interlink packet-header width. | Must match at both ends. |
| `IL_RX_BEATS` | integer | `64` | Interlink receive buffer per class, in beats, and therefore the initial credit. | `>= IL_MAX_BEATS`. Both ends must agree. |
| `IL_MAX_BEATS` | integer | `32` | Longest interlink packet this end may emit. | `<= IL_RX_BEATS`. Above it, a packet exists that can never be granted credit — a dead link. |
| `MP1` | integer | `MEM_PORTS + 2 + (ILINK ? 1 : 0)` | **Derived.** AXI master channel count: one per memory port, one for the host upload, one for the mover, and one for inbound remote writes when the interlink is present. | Do not override. |
| `MEM_X` | integer | `0` | Mesh X coordinate of memory port 0. | Reachable by the clamp. |
| `MEM_Y` | integer | `1` | Mesh Y coordinate of port 0. **The control agent answers at this coordinate too.** | As above. |
| `MEM_X1` | integer | `0` | Port 1 X. | As above. |
| `MEM_Y1` | integer | `3` | Port 1 Y. | As above. |
| `MEM_X2` | integer | `0` | Port 2 X. | As above. |
| `MEM_Y2` | integer | `4` | Port 2 Y. | As above. |
| `MEM_X3` | integer | `0` | Port 3 X. | As above. |
| `MEM_Y3` | integer | `5` | Port 3 Y. | As above. |
| `GRID_LO` | integer | `1` | Passed to the control agent, which publishes it in `CAPS`. | See §1. |
| `GRID_HI` | integer | `2` | Passed to the control agent. **Note this default differs from the router's 14.** | See §1. |
| `STAGE_FLITS` | integer | `128` | Passed to the control agent's staging RAM. | See §4. |
| `WR_SLOTS` | integer | `16` | Write reassembly slots per memory port. | **At least two per node that can have a write in flight.** Under-sizing deadlocks; it does not corrupt. |

Port coordinates are named per port rather than packed into one vector: a packed
field is one shift away from pointing a whole port at the wrong node, and it
would elaborate cleanly.

Ports **MUST** be placed at different mesh nodes. Routing is XY on clamped
coordinates, so two ports on one router split the server without splitting the
funnel.

### `mag_mem_port` — `src/kohakumas/mag_mem_port.v`

One memory endpoint and the AXI master behind it.

| Name | Type | Default | Controls | Legal range |
|---|---|---|---|---|
| `FLIT_WIDTH` | integer | `288` | Flit width. | See §1. |
| `POS_WIDTH` | integer | `4` | Coordinate width. | See §1. |
| `DATA_W` | integer | `256` | AXI data width. Also sets the AXI burst lengths the engine computes from entry sizes. | See §1. |
| `ADDR_W` | integer | `34` | Address width. | See §1. |
| `ID_W` | integer | `4` | AXI ID width. | Any. |
| `MEM_X` | integer | `0` | This port's mesh X coordinate, stamped as the source of every response. | Reachable by the clamp. |
| `MEM_Y` | integer | `1` | Its Y coordinate. | As above. |
| `WR_SLOTS` | integer | `16` | Write reassembly slots. Each holds a whole burst. | `>= 2` per writing node. |
| `Q_DEPTH` | integer | `64` | Depth of each of the two intake queues. | Power of two, `> Q_MARGIN`. |
| `Q_MARGIN` | integer | `4` | Entries of headroom at which the port raises backpressure. **This is a real margin, counted by the port itself** — the FIFO's own `almost` flag is not one. | `< Q_DEPTH`. |
| `MEM_TYPE` | string | `"distributed"` | Storage primitive for the intake queues. | `"distributed"`, `"block"`, `"ultra"`. |

**Fixed constants, not parameters.** `WBURST` is 8: a write slot holds eight
beats, and a `MEM_WR_REQ` with `len > 7` has undefined behaviour. The transform's
entry sizes (2048 bits in, 1024 out) are localparams of the current transform.
See [memory-protocol.md](memory-protocol.md) §4.2 and §10.

### `mm_mover` — `src/kohakumas/mm_mover.v`

Layout, gather and fill engine with its own AXI master and no mesh endpoint.
Command registers: [control-registers.md](control-registers.md) §3.

| Name | Type | Default | Controls | Legal range |
|---|---|---|---|---|
| `DATA_W` | integer | `256` | AXI data width. Every transfer is one beat, so this is also the transfer granule. | Strides must be multiples of `DATA_W/8`. |
| `ADDR_W` | integer | `34` | Address width. | See §1. |
| `ID_W` | integer | `4` | AXI ID width. | Any. |
| `IDX_WORDS` | integer | `256` | Depth of the gather index buffer, in `DATA_W`-bit words — 8 indices per word. | Sets the maximum gather index count at `IDX_WORDS * 8`. Must be large enough that the port address width matches the module's index registers. |

### `mm_prng` — `src/kohakumas/mm_prng.v`

Counter-based PRNG behind the mover's `GENERATE` mode. Stateless in the sense
that matters: the value is a pure function of `(key, counter)`, so noise is
independent of how a region was tiled and is restartable after a fault.

| Name | Type | Default | Controls | Legal range |
|---|---|---|---|---|
| `ROUNDS` | integer | `10` | Rounds per 128-bit draw, one round per four cycles. | Changing it changes the generated values. Any value below the algorithm's specified count weakens it. |

## 6. AXI transport and memory models

### `axi_n1` — `src/kohakuaxi/axi_n1.v`

N AXI4 masters onto one slave, across two clock domains. Arbitration, response
routing and the clock crossing — no address decode, no width conversion, no
protocol conversion.

| Name | Type | Default | Controls | Legal range |
|---|---|---|---|---|
| `N` | integer | `4` | Number of master-side interfaces. | `>= 1`. |
| `ADDR_W` | integer | `34` | Address width. | Any. |
| `DATA_W` | integer | `256` | Data width, both sides. | Any. |
| `ID_W` | integer | `4` | Master-side ID width. | Any. |
| `AW_DEPTH` | integer | `16` | Write-address crossing queue depth. Needs only to cover the crossing latency. | Power of two. |
| `W_DEPTH` | integer | `64` | Write-data crossing queue depth. Sized for burst throughput. | Power of two. |
| `B_DEPTH` | integer | `16` | Write-response queue depth. | Power of two. |
| `AR_DEPTH` | integer | `16` | Read-address queue depth. | Power of two. |
| `R_DEPTH` | integer | `64` | Read-data queue depth. Sized for burst throughput. | Power of two. |
| `WR_MEM` | string | `"block"` | Storage primitive for the W and R queues, which are the two wide ones. | `"distributed"`, `"block"`. |
| `IDX_W` | integer | `(N <= 1) ? 1 : $clog2(N)` | **Derived.** Master index width. | Do not override. |
| `SID_W` | integer | `ID_W + IDX_W` | **Derived.** Slave-side ID width. **The attached slave's ID width MUST be `SID_W`**, and it MUST echo the full ID — response routing is the ID, not a table. | Do not override. |

Optional AXI signals (`LOCK`, `CACHE`, `PROT`, `QOS`, `REGION`) are not carried.
No master in the framework drives them.

### `mag_dram_port` — `src/kohakumas/mag_dram_port.v`

N requesters onto one AXI4 master, packing a narrow internal beat up to a wider
memory beat across a clock crossing. **Not instantiated by `mag.v`**; tests use a
copy.

| Name | Type | Default | Controls | Legal range |
|---|---|---|---|---|
| `N` | integer | `5` | Number of requesters. | `>= 1`. |
| `ADDR_W` | integer | `34` | Address width. | Any. |
| `SW` | integer | `256` | Internal beat width. | Any. |
| `MW` | integer | `512` | Memory beat width. | `SW` times a power of two. |
| `ID_W` | integer | `4` | AXI ID width. | Any. |
| `AWQ` | integer | `16` | Write-address queue depth. | Power of two. |
| `WQ` | integer | `64` | Write-data queue depth. | Power of two. |
| `BQ` | integer | `16` | Write-response queue depth. | Power of two. |
| `ARQ` | integer | `16` | Read-address queue depth. | Power of two. |
| `RQ` | integer | `64` | Read-data queue depth. | Power of two. |
| `WR_MEM` | string | `"block"` | Storage primitive for the wide queues. | `"distributed"`, `"block"`. |

### `mag_dram_rr` — same file

Lowest set bit at or after a base, wrapping. Shared by both of that module's
arbiters.

| Name | Type | Default | Controls | Legal range |
|---|---|---|---|---|
| `N` | integer | `5` | Request vector width. | `>= 1`. |
| `IDX_W` | integer | `3` | Index width. | `>= $clog2(N)`. |

### `axi_ram` — `src/kohakumas/axi_ram.v`

AXI4 slave RAM standing in for DRAM so the machine can be simulated end to end.
One outstanding transaction per port, INCR bursts only, no narrow transfers, no
interleaving.

| Name | Type | Default | Controls | Legal range |
|---|---|---|---|---|
| `DATA_W` | integer | `256` | Data width. Matches the flit payload so nothing in the simulation path gears between widths. | Any. |
| `ADDR_W` | integer | `34` | Address width. | Any. |
| `ID_W` | integer | `4` | AXI ID width. | Any. |
| `WORDS` | integer | `4096` | Storage depth in `DATA_W`-bit words. | Any. |
| `PORTS` | integer | `1` | Independent AW/W/B and AR/R channel sets over one array — a model of a multi-channel controller in front of one address space. | `>= 1`. At 1 every port width is exactly what a single-port instantiation expects. |

Two ports writing the same word in the same cycle is last-writer-wins here and
unordered on real hardware. The framework does not prevent it.

### `axi4_ram` — `src/kohakuaxi/axi4_ram.v`

AXI4-Full slave RAM, the reference implementation for AXI bring-up. INCR, FIXED
and WRAP bursts to 256 beats, `WSTRB` byte enables, ID reflection. No exclusive
access, no `AxCACHE`/`AxPROT` semantics, no narrow-transfer read lane
replication.

| Name | Type | Default | Controls | Legal range |
|---|---|---|---|---|
| `DATA_WIDTH` | integer | `64` | Data width. | Any. |
| `ADDR_WIDTH` | integer | `64` | Address width. | Any. |
| `ID_WIDTH` | integer | `4` | AXI ID width. | Any. |
| `DEPTH` | integer | `4096` | Storage depth in `DATA_WIDTH`-wide words. | Any. |

### `axi4_master` — `src/kohakuaxi/axi4_master.v`

AXI4-Full master reference implementation. Takes one command and turns it into as
many legal bursts as required: never crossing a 4 KB boundary, never exceeding
256 beats, `WLAST` on the last beat of every burst. **One burst outstanding at a
time**, deliberately, for a reference.

| Name | Type | Default | Controls | Legal range |
|---|---|---|---|---|
| `DATA_WIDTH` | integer | `64` | Data width. | Any. |
| `ADDR_WIDTH` | integer | `64` | Address width. | Any. |
| `ID_WIDTH` | integer | `4` | AXI ID width. | Any. |
| `AXI_ID` | integer | `0` | The constant ID this master issues. | `< 2**ID_WIDTH`. |

## 7. Inter-mesh link

Generated only when the memory agent's `ILINK` is non-zero. Architecture:
[arch/mas/](../arch/mas/). Registers:
[control-registers.md](control-registers.md) §4.

### `mag_ilink` — `src/kohakumas/mag_ilink.v`

Everything the memory agent needs to speak to another mesh: the mover's remote
writes, inbound remote writes, flit encapsulation and injection, and doorbells.

| Name | Type | Default | Controls | Legal range |
|---|---|---|---|---|
| `FLIT_WIDTH` | integer | `288` | Flit width. | See §1. |
| `POS_WIDTH` | integer | `4` | Coordinate width. | See §1. |
| `DATA_W` | integer | `256` | AXI data width. | See §1. |
| `ADDR_W` | integer | `34` | Address width. Bits 33:32 select the mesh. | See §1. |
| `LINK_W` | integer | `288` | Beat width. A beat is one flit, so a flit crosses verbatim with nothing packed, padded or reconstructed. | Must match at both ends. |
| `TUSER_W` | integer | `96` | Packet header width. | Must match at both ends. |
| `MESH_ID` | integer | `0` | Reset value of the mesh id register. The live value is a runtime register, so one bitstream is usable at any position in the grid. | 0–3. |
| `MAX_BEATS` | integer | `32` | Longest packet emitted. | `<= RX_BEATS` at the far end. |
| `MEM_X` | integer | `0` | The local memory port's X coordinate, used when injecting an inbound flit. | Must match `mag`'s `MEM_X`. |
| `MEM_Y` | integer | `1` | Its Y coordinate. | Must match `mag`'s `MEM_Y`. |

### `mag_switch` — `src/kohakumas/mag_switch.v`

Three-port switch: link 0, link 1, local. A **second routing layer** that does not
inherit the mesh's deadlock proof and gets its own, by the same argument: XY
dimension-order on mesh coordinates over a rectangular grid of meshes.

| Name | Type | Default | Controls | Legal range |
|---|---|---|---|---|
| `LINK_W` | integer | `288` | Beat width. | Must match everywhere on the link. |
| `TUSER_W` | integer | `96` | Header width. | As above. |
| `RX_BEATS` | integer | `64` | Receive buffer per class, in beats, and therefore the initial credit. | `>= MAX_BEATS`. |
| `CRED_BATCH` | integer | `8` | Credits returned per credit packet. | `<= RX_BEATS`. |
| `MAX_BEATS` | integer | `32` | Longest packet emitted. | `<= RX_BEATS`. |

Two links, not `N`: the mesh id is two bits, so a fifth mesh is an instruction-set
change rather than a parameter change, and a port count that cannot vary is not
spelled as though it can.

### `mag_link` — `src/kohakumas/mag_link.v`

One full-duplex end of a mesh-to-mesh crossing. Two of these back to back, with
nothing between them but registers, is a complete crossing.

| Name | Type | Default | Controls | Legal range |
|---|---|---|---|---|
| `LINK_W` | integer | `288` | Beat width. 288 is what one mesh port produces; the link itself is width-agnostic. | **Both ends and every pipe stage MUST agree.** |
| `TUSER_W` | integer | `96` | Packet header width. | Both ends must agree. |
| `RX_BEATS` | integer | `64` | Receive buffer per class, in beats, and therefore the initial credit. | **Both ends MUST agree.** A receiver smaller than the sender's credit overflows, and no backpressure is left to catch it. |
| `CRED_BATCH` | integer | `8` | Credits returned per credit packet. | `<= RX_BEATS`. |
| `MAX_BEATS` | integer | `32` | Longest packet this end may emit. | **`<= RX_BEATS`.** Above it there exists a packet that can never be granted credit, which presents as a dead link. |

Credit is **per class** — "does this packet stop at the peer, or does the peer
forward it" — as two counters. One shared pool would let a stalled forward path
stop traffic that was going to terminate anyway.

### `mag_link_pipe` — `src/kohakumas/mag_link_pipe.v`

Extra register stages in one direction. Those stages have to exist in the RTL:
the placer will pull a register into a crossing site, but retiming will not
invent one that was never written.

| Name | Type | Default | Controls | Legal range |
|---|---|---|---|---|
| `LINK_W` | integer | `288` | Beat width. | Must match the link. |
| `TUSER_W` | integer | `96` | Header width. | Must match the link. |
| `DEPTH` | integer | `2` | Number of register stages. | `>= 1`. The `tap` input selects the live depth at run time; tie it to `DEPTH` for synthesis and it folds away. |

A plain shift register is correct here **only because flow control is
credit-based**. There is no handshake to preserve and no skid buffer, which is
what makes inserting latency free rather than a redesign.

### `il_pkt_mux2`, `il_pkt_demux4` — `src/kohakumas/il_pkt_arb.v`

Packet-stream plumbing: one 2:1 merge and one 1:4 split, both locking for the
duration of a packet. A mux that re-arbitrates per beat interleaves two packets
on one stream, and a receiver that frames by `TLAST` cannot tell.

| Name | Type | Default | Controls | Legal range |
|---|---|---|---|---|
| `LINK_W` | integer | `288` | Beat width. | Must match the link. |
| `TUSER_W` | integer | `96` | Header width. | Must match the link. |

## 8. Shared primitives

### `sync_fifo` — `src/common/sync_fifo.v`

Synchronous FIFO over `xpm_fifo_sync`, first-word-fall-through, read latency 0.

| Name | Type | Default | Controls | Legal range |
|---|---|---|---|---|
| `DATA_WIDTH` | integer | `288` | Entry width. | Any. |
| `FIFO_DEPTH` | integer | `32` | Depth. | **Power of two.** |
| `MEMORY_TYPE` | string | `"distributed"` | Storage primitive. | `"distributed"`, `"block"`, `"ultra"`. |
| `PROG_FULL_THRESH` | integer | `FIFO_DEPTH - 5` | Passed to XPM. **Has no effect** — see below. | — |
| `PROG_EMPTY_THRESH` | integer | `5` | Passed to XPM. Has no effect. | — |

**`wr_almost` is not a margin**, despite the name and despite the threshold being
passed. `USE_ADV_FEATURES` is zero, so XPM ties `prog_full` low and `wr_almost`
reduces to `wr_busy`. It never asserts early. What makes plain *full* safe on a
mesh link is the retry discipline, not a margin
([compute-unit-port.md](compute-unit-port.md) §2). Anything wanting a real margin
**MUST** count for itself, as `mag_mem_port` does with `Q_MARGIN`. Nothing should
depend on this bit until `USE_ADV_FEATURES` is changed.

Reset-busy is folded into the flags, so a writer that honours `wr_busy` cannot
lose the first beats after reset.

### `async_fifo` — `src/common/async_fifo.v`

Asynchronous FIFO over `xpm_fifo_async` — the clock crossing and nothing else.
Deliberately a separate module rather than a mode of `sync_fifo`: the same name
for both would let a single-clock instantiation compile against a crossing, and
that corruption is invisible in simulation because both clocks are ideal there.

| Name | Type | Default | Controls | Legal range |
|---|---|---|---|---|
| `DATA_WIDTH` | integer | `64` | Entry width. | Any. |
| `FIFO_DEPTH` | integer | `16` | Depth. | **Power of two.** |
| `MEMORY_TYPE` | string | `"distributed"` | Storage primitive. | `"distributed"`, `"block"`. |

`CDC_SYNC_STAGES` is fixed at 2 and is not a parameter: the pointer synchronisers
are the whole reason the module exists.

### `kohaku_sdpram` — `src/common/kohaku_sdpram.v`

Simple dual-port RAM, one write port, one read port, one clock. **The storage
primitive is named by the caller and passed straight through; it is never left to
synthesis to infer from the shape of a register array.**

| Name | Type | Default | Controls | Legal range |
|---|---|---|---|---|
| `WIDTH` | integer | `256` | Word width. | Any. |
| `DEPTH` | integer | `512` | Word count. | Any; the address width is `$clog2(DEPTH)`. |
| `MEM_PRIM` | string | `"block"` | The primitive. `"distributed"` is LUT RAM, wide and shallow. `"block"` is 512×72 at its widest. `"ultra"` is 4096×72, fixed, deep and narrow. | `"distributed"`, `"block"`, `"ultra"`. |
| `READ_LAT` | integer | `1` | Read latency in cycles. | `0` is **legal only for `"distributed"`**. |

Why this module exists rather than an inferred array: left to inference, whether
an array becomes LUT RAM, block RAM or ultra RAM depends on a reset clause, a
read latency, or a heuristic that can change between tool versions — so both the
resource cost **and the read latency** can move without the RTL changing. Read
latency is not a detail; it sets how far an address must lead its data, and
callers build pipeline structure on that number.

### `MultiBitLut` — `src/common/lut.v`

Direct instantiation of LUT primitives for a small hard-coded table.

| Name | Type | Default | Controls | Legal range |
|---|---|---|---|---|
| `input_bits` | integer | `6` | Table index width. | `5` or `6`. Other values instantiate nothing. |
| `output_bits` | integer | `10` | Table output width. | Must be a multiple of `7 - input_bits`. |
| `INIT` | bit vector | `0` | The table contents, `64 * output_bits / (7 - input_bits)` bits. | Sized by the expression above. |

### `xorshift64`, `xorshift128_single`, `xorshift256` — `src/common/xorshift.v`

No parameters.

### `float_display` — `src/common/fp.v`

A simulation-only decoder that turns a floating-point word into a printable real.

| Name | Type | Default | Controls | Legal range |
|---|---|---|---|---|
| `prefix` | string | `"Number"` | Label used in output. | Any. |
| `EXP_BITS` | integer | `8` | Exponent field width. | `>= 1`. |
| `MANT_BITS` | integer | `23` | Mantissa field width. | `>= 1`. Total input width is `EXP_BITS + MANT_BITS + 1`. |

## 9. Instance-specific modules currently in framework packages

Listed for completeness, and flagged because a second accelerator does **not**
inherit them. See [memory-protocol.md](memory-protocol.md) §10 for which part of
the read-path transform is framework-owned and which is not.

### `mx_quant` — `src/kohakumas/mx_quant.v`

KohakuTPU's instance of the read-path preprocess: FP16 to a 7-bit block format
with a shared E5M3 scale.

| Name | Type | Default | Controls | Legal range |
|---|---|---|---|---|
| `SBIAS` | integer | `20` | Scale exponent bias. The scale spans 2⁻²⁰ to 2¹⁰ with an FP16 peak, so 20 centres it on the 5-bit field. | Changing it changes the numeric result and MUST be matched by the consuming datapath and by the driver's software model. |

Its entry sizes — 2048 bits in, 1024 bits out — are localparams, not parameters,
and they are what set the memory agent's default `entry_words` of 4.
