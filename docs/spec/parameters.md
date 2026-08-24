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
| `ADDR_W` | integer | `40` | Physical address width, and the width of the flit's `addr` field. | The flit's `addr` field is **40 bits and is not parameterised** — `mag_mem_port.v` slices `[255 -: 40]` whatever `ADDR_W` is, because it is a flit contract rather than a width. `[39]` selects the aperture, `[38]` is reserved, `[37:36]` is the mesh id; see [address-map.md](../address-map.md). |
| `ID_W` / `ID_WIDTH` | integer | `4` | AXI ID width. | Any. Must match across a master/slave pair; `axi_n1` widens it by its own index field. |
| `GRID_LO` | integer | `1` | Lowest router coordinate, both axes. Endpoints live outside the grid and are reached by the coordinate clamp. | `>= 1`. Coordinate 0 is the edge, and the four corners must be empty. |
| `GRID_HI` | integer | `14` (router, orchestrator), `2` (memory agent) | Highest router coordinate when the grid is square. | `>= GRID_LO`, and `< 2**POS_WIDTH - 1`. **The two defaults disagree; a mesh top MUST set both explicitly.** |

## 2. Mesh and routing

### `NoCRouter` — `src/kohakuaccel/noc/router/noc_router.v`

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

### `InPortSwitch` — `src/kohakuaccel/noc/router/noc_inport.v`

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

### `OutPortSwitch` — `src/kohakuaccel/noc/router/noc_outport.v`

One per router output. Round-robin across the five input heads, and the register
driving the outbound link.

| Name | Type | Default | Controls | Legal range |
|---|---|---|---|---|
| `DATA_WIDTH` | integer | `288` | Flit width. | See §1. |

## 3. Compute-unit endpoint

### `noc_cu_base` — `src/kohakuaccel/noc/endpoint/noc_cu_base.v`

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

### `noc_cu_null` — `src/kohakuaccel/noc/endpoint/noc_cu_null.v`

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

### `noc_orchestrator` — `src/kohakuaccel/noc/ctrl/noc_orchestrator.v`

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

### `main_orch` — `src/kohakuaccel/verif/main_orch.v`

The host-side control-program engine. Register map:
[control-registers.md](control-registers.md) §5.

| Name | Type | Default | Controls | Legal range |
|---|---|---|---|---|
| `ADDR_W` | integer | `32` | Address width of both its slave and its master. | `>= 16`. |
| `ID_W` | integer | `4` | AXI ID width. | Any. |
| `NCMD` | integer | `128` | Command slots. | `NCMD * 32 <= 0x1000`, i.e. at most 128 at the current window size. |
| `POLL_IVL` | integer | `31` | Cycles between `POLL` retries. | Any. Larger reduces read traffic on a slow interconnect. |

### `axi_xbar2` — `src/kohakuaccel/axi/simple/axi_xbar2.v`

A 2-master, 2-slave crossbar standing in for a vendor interconnect in
simulation. One outstanding transaction per direction, whole transactions granted
at a time, no interleaving, no width conversion.

| Name | Type | Default | Controls | Legal range |
|---|---|---|---|---|
| `ADDR_W` | integer | `32` | Address width. | `> SEL_BIT`. |
| `DATA_W` | integer | `64` | Data width, both sides. | Any. |
| `ID_W` | integer | `4` | AXI ID width. | Any. |
| `SEL_BIT` | integer | `28` | The single address bit that selects slave 1 over slave 0. | `< ADDR_W`. |

### `InstReceiver` — `src/attic/legacy-axi/instruction_receiver.v`

An AXI4 slave that accepts instruction words into a FIFO. Predates the
orchestrator's staging path. **This module is in the attic and nothing builds
it**; the row is kept because the encoding it accepted is still readable in old
captures.

| Name | Type | Default | Controls | Legal range |
|---|---|---|---|---|
| `INSTRUCTION_DEPTH` | integer | `16` | FIFO depth. | Power of two. |
| `DATA_WIDTH` | integer | `64` | AXI data width. | Any. |
| `ADDR_WIDTH` | integer | `64` | AXI address width. | Any. |
| `STRB_WIDTH` | integer | `DATA_WIDTH/8` | **Derived.** Write-strobe width. | Do not override. |
| `WORD_WIDTH` | integer | `STRB_WIDTH` | **Derived.** Bytes per addressable word. | Do not override. |
| `WORD_SIZE` | integer | `DATA_WIDTH/WORD_WIDTH` | **Derived.** Bits per word. | Do not override. |
| `VALID_ADDR_OFFSET` | integer | `$clog2(STRB_WIDTH)` | **Derived.** Low address bits the slave ignores. | Do not override. |
| `ID_WIDTH` | integer | `4` | AXI ID width. | Any. |

## 5. System node

### `sysnode` — `src/kohakuaccel/sysnode/sysnode.v`

**THE node, and one component.** `sn_hub` owns every attachment; `mag` and the
control processor are its clients and neither has a fabric port. Neither is
separable and neither is optional — there is no `CTRL_PE`, and `PE_X`/`PE_Y` are
gone because the processor answers at `(0,0)`, a corner no mesh map can fill.

**Instantiate this. `mag` is not a module a top may use** — it has no NoC ports
and does not elaborate alone.

| Name | Type | Default | Controls | Legal range |
|---|---|---|---|---|
| `PORTS` | integer | `1` | The node's NoC attachment count, and **the only shape knob**. Every client inside shares these. | 1–4. Four coordinate pairs are declared. |
| `FLIT_WIDTH` | integer | `288` | Flit width. | See §1. |
| `POS_WIDTH` | integer | `4` | Coordinate width. | See §1. |
| `DATA_W` | integer | `256` | AXI memory width. Equals the flit payload by design. | See §1. |
| `ADDR_W` | integer | `40` | Physical address width. | See §1. |
| `ID_W` | integer | `4` | AXI ID width. | Any. |
| `MW` | integer | `DATA_W` | Memory beat width at the DRAM master. `mag_dram_port` packs `DATA_W` up to this. | `DATA_W` times a power of two. |
| `ILINK` | integer | `0` | Build the interlink. **Zero generates none of it.** | 0 or non-zero. |
| `MESH_ID` | integer | `0` | This mesh's id when the interlink is absent. | 0–3. |
| `LINK_W` | integer | `288` | Interlink AXIS payload width. | Both ends must agree. |
| `TUSER_W` | integer | `96` | Interlink AXIS sideband width. | Both ends must agree. |
| `MEM_X`, `MEM_Y` | integer | `0`, `1` | Mesh coordinates of port 0. **The control agent answers here too.** | Reachable by the clamp. |
| `MEM_X1`, `MEM_Y1` | integer | `0`, `3` | Coordinates of port 1. | As above, and on a different router. |
| `MEM_X2`, `MEM_Y2` | integer | `0`, `4` | Coordinates of port 2. | As above. |
| `MEM_X3`, `MEM_Y3` | integer | `0`, `5` | Coordinates of port 3. | As above. |
| `GRID_LO` | integer | `1` | Clamp lower bound. | See §1. |
| `GRID_HI` | integer | `2` | Clamp upper bound. | See §1. |
| `STAGE_FLITS` | integer | `128` | Orchestrator staging RAM depth, in flits. | Power of two. |
| `WR_SLOTS` | integer | `16` | Write-reassembly slots per memory engine. | `>= 1`. |
| `STAGE` | integer | `0` | Build the staging store. **Zero generates none of it.** | 0 or non-zero. |
| `STAGE_BANKS` | integer | `4` | Banks in the staging store. | `>= 1`. |
| `STAGE_ENTRIES` | integer | `16384` | Entries per bank. 4 × 16384 is 64 URAM, 2 MB. | Power of two. |
| `STAGE_PIPE` | integer | `1` | Extra register stage on the staging read. | 0 or 1. |
| `STAGE_AT_PORT` | integer | `0` | 0 puts one store on the converged path; 1 puts one inside each engine, unreachable by mover and interlink. | 0 or 1. |
| `PE_IMEM` | integer | `2048` | Words of instruction memory. | Power of two. |
| `PE_SPAD` | integer | `2048` | Words of scratchpad. | Power of two. |
| `PE_L1_LINES` | integer | `128` | Lines in the processor's own L1. | Power of two. |
| `PE_MEM_PRIM` | string | `"block"` | Storage primitive for the processor's imem and scratchpad. | `"block"`, `"distributed"`, `"ultra"`. |
| `XFORM_SLOTS` | integer | `1` | Transform occupants the slot selects between. | `>= 1`. |
| `XID_W` | integer | `4` | Width of the occupant id. | `>= clog2(XFORM_SLOTS)`. |
| `XMODE_W` | integer | `4` | Width of the mode field handed to an occupant. | Any. |
| `XFORM_IN_BITS` | integer | `2048` | **Declared by the occupant.** Bits consumed per entry. | Must match the bank. |
| `XFORM_OUT_WORDS` | integer | `4` | **Declared by the occupant.** Words produced per entry. | Must match the bank. |

**Two of `mag`'s parameters are still not forwarded** and take `mag`'s defaults
whatever a top asks for: `IL_RX_BEATS` and `IL_MAX_BEATS`. A ship cannot change
the interlink's credit depth without editing `sysnode.v`. The transform geometry
**is** forwarded now, so a project whose occupant is not 2048-in / 4-out can
express that.

**Never call this a "node".** A NoC endpoint is a node; this is the system node.

### `sn_hub` — `src/kohakuaccel/sysnode/core/sn_hub.v`

The node's attachments, and the demux and arbitration that let four kinds of
client share them. Nothing else in the node has a NoC port.

| Name | Type | Default | Controls | Legal range |
|---|---|---|---|---|
| `FLIT_WIDTH` | integer | `288` | Flit width. | See §1. |
| `POS_WIDTH` | integer | `4` | Coordinate width. | See §1. |
| `PORTS` | integer | `1` | Attachments presented. | 1–4, matching `sysnode`. |
| `ILINK` | integer | `0` | Whether the interlink client exists. At 0 its arm folds to a constant false. | 0 or non-zero. |
| `MEM_Y` | integer | `1` | Mesh row of port 0, for the outbound steer. | Must match `sysnode`'s. |
| `MEM_Y1` | integer | `3` | Mesh row of port 1. | As above. |
| `MEM_Y2` | integer | `4` | Mesh row of port 2. | As above. |
| `MEM_Y3` | integer | `5` | Mesh row of port 3. | As above. |

**The rows are parameters, not a port**, and that is a measured decision: a
port's row is a build-time constant compared against a flit field on every port
every cycle. Carried in as a wire it cannot fold across the module boundary,
and that cost **155 LUT**.

The control processor's coordinate is a **localparam of `(0,0)`, not a
parameter**. A corner touches no router, so it is free in every mesh by
construction and there is nothing to choose.

### `mag` — `src/kohakuaccel/sysnode/core/mag.v`

The single point where a partition touches everything outside it. Protocol:
[memory-protocol.md](memory-protocol.md).

| Name | Type | Default | Controls | Legal range |
|---|---|---|---|---|
| `FLIT_WIDTH` | integer | `288` | Flit width. | See §1. |
| `POS_WIDTH` | integer | `4` | Coordinate width. | See §1. |
| `DATA_W` | integer | `256` | AXI memory width. Equals the flit payload by design. | See §1. |
| `ADDR_W` | integer | `40` | Physical address width. | See §1. |
| `ID_W` | integer | `4` | AXI ID width. | Any. |
| `PORTS` | integer | `1` | How many memory engines, one per hub attachment. **This is the unit the machine grows by**, not a tuning knob: an engine owns its intake, read logic, write slots and AXI channel. **Measured: a second port is 6,557 LUT and no DSP.** | 1–4. Four coordinate pairs are declared. |
| `ILINK` | integer | `0` | Build the interlink. **Zero generates none of it** — no switch, no links, no extra AXI master, and the remote decode folds to a constant false. | 0 or non-zero. |
| `MESH_ID` | integer | `0` | This mesh's id when the interlink is absent. With the interlink present the id is a runtime register instead. | 0–3. |
| `LINK_W` | integer | `288` | Interlink beat width. One beat is one flit. | Must match at both ends and in every pipe stage. |
| `TUSER_W` | integer | `96` | Interlink packet-header width. | Must match at both ends. |
| `IL_RX_BEATS` | integer | `64` | Interlink receive buffer per class, in beats, and therefore the initial credit. | `>= IL_MAX_BEATS`. Both ends must agree. |
| `IL_MAX_BEATS` | integer | `32` | Longest interlink packet this end may emit. | `<= IL_RX_BEATS`. Above it, a packet exists that can never be granted credit — a dead link. |
| `MP1` | integer | `PORTS + 3 + (ILINK ? 1 : 0)` | **Derived.** Internal requester count, and therefore the width of the converged path into `mag_dram_port`: one per memory engine, one for the host upload, one for the processor's mover, one for the processor's L1, and one for inbound remote writes when the interlink is present. The processor is not optional, so neither is its pair. | Do not override. |
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
| `MW` | integer | `DATA_W` | Memory beat width at `M_AXI_DRAM`. `mag_dram_port` packs `DATA_W` up to this, so at 512 an 8-beat 256-bit burst becomes 4 beats. | `DATA_W` times a power of two. |
| `STAGE` | integer | `0` | Build the staging store. **Zero generates none of it.** | 0 or non-zero. |
| `STAGE_BANKS` | integer | `4` | Banks in the staging store. | `>= 1`. |
| `STAGE_ENTRIES` | integer | `16384` | Entries per bank. At the defaults that is 4 banks × 16,384 × 256 bits = 16 Mib of URAM. | `>= 1`. |
| `STAGE_PIPE` | integer | `1` | Extra register stages on the staging read path, for timing. | `>= 0`. |
| `STAGE_AT_PORT` | integer | `0` | Put the staging store behind a memory port instead of on the converged path. The two placements are an A/B — `mm_mesh_stage -d MM_L2_PORT` runs the same checks either way. | 0 or non-zero. |

Port coordinates are named per port rather than packed into one vector: a packed
field is one shift away from pointing a whole port at the wrong node, and it
would elaborate cleanly.

Ports **MUST** be placed at different mesh nodes. Routing is XY on clamped
coordinates, so two ports on one router split the server without splitting the
funnel.

### `mag_mem_port` — `src/kohakuaccel/sysnode/core/mag_mem_port.v`

One memory endpoint and the AXI master behind it.

| Name | Type | Default | Controls | Legal range |
|---|---|---|---|---|
| `FLIT_WIDTH` | integer | `288` | Flit width. | See §1. |
| `POS_WIDTH` | integer | `4` | Coordinate width. | See §1. |
| `DATA_W` | integer | `256` | AXI data width. Also sets the AXI burst lengths the engine computes from entry sizes. | See §1. |
| `ADDR_W` | integer | `40` | Address width. | See §1. |
| `ID_W` | integer | `4` | AXI ID width. | Any. |
| `MEM_X` | integer | `0` | This port's mesh X coordinate, stamped as the source of every response. | Reachable by the clamp. |
| `MEM_Y` | integer | `1` | Its Y coordinate. | As above. |
| `WR_SLOTS` | integer | `16` | Write reassembly slots. Each holds a whole burst. | `>= 2` per writing node. |
| `Q_DEPTH` | integer | `64` | Depth of each of the two intake queues. | Power of two, `> Q_MARGIN`. |
| `Q_MARGIN` | integer | `4` | Entries of headroom at which the port raises backpressure. **This is a real margin, counted by the port itself** — the FIFO's own `almost` flag is not one. | `< Q_DEPTH`. |
| `MEM_TYPE` | string | `"distributed"` | Storage primitive for the intake queues. | `"distributed"`, `"block"`, `"ultra"`. |
| `MESH_ID` | 2-bit | `2'd0` | Which mesh this port belongs to, for the absolute address test. A request whose `addr[37:36]` names another mesh is not this port's. | 0–3. Must agree with the interlink's runtime id. |
| `AP_DECODE` | integer | `0` | Decode the aperture bit (`addr[39]`) at the port rather than downstream. Off, the port treats every address as DRAM. | 0 or non-zero. Requires `STAGE`. |
| `STAGE` | integer | `0` | Build the staging store behind this port. | 0 or non-zero. |
| `STAGE_BANKS` | integer | `4` | Banks in that store. | `>= 1`. |
| `STAGE_ENTRIES` | integer | `16384` | Entries per bank. | `>= 1`. |
| `STAGE_PIPE` | integer | `1` | Extra register stages on the staging read path. | `>= 0`. |

**Fixed constants, not parameters.** `WBURST` is 8: a write slot holds eight
beats, and a `MEM_WR_REQ` with `len > 7` has undefined behaviour. The transform's
entry sizes (2048 bits in, 1024 out) are localparams of the current transform.
See [memory-protocol.md](memory-protocol.md) §4.2 and §10.

### `mm_mover` — `src/kohakuaccel/sysnode/mover/mm_mover.v`

Layout, gather and fill engine with its own AXI master and no mesh endpoint.
Command registers: [control-registers.md](control-registers.md) §3.

| Name | Type | Default | Controls | Legal range |
|---|---|---|---|---|
| `DATA_W` | integer | `256` | AXI data width. Every transfer is one beat, so this is also the transfer granule. | Strides must be multiples of `DATA_W/8`. |
| `ADDR_W` | integer | `40` | Address width. The map is **absolute** — `[39]` aperture, `[37:36]` mesh — so a narrower build is a different map, and the module refuses anything but 40. | `40` only. |
| `ID_W` | integer | `4` | AXI ID width. | Any. |
| `IDX_WORDS` | integer | `256` | Depth of the gather index buffer, in `DATA_W`-bit words — 8 indices per word. | Sets the maximum gather index count at `IDX_WORDS * 8`. Must be large enough that the port address width matches the module's index registers. |
| `XID_W`, `XMODE_W` | integer | `4`, `4` | Widths of the transform id and mode it carries to the slot. | Must match `mag_xform`. |
| `XF_IN_BITS`, `XF_OUT_WORDS` | integer | `2048`, `4` | The occupant's geometry, used to size both walks of a mode-5 move. | Must match the bank; `XF_OUT_WORDS <= 4`. |
| `BURST_MAX` | integer | `128` | Longest AXI burst the mover issues, in beats. | `<= 256`, and short enough that the 4 KB boundary rule still holds. |
| `MAX_OUT` | integer | `16` | Reads in flight. The read side reserves FIFO space before every AR, so this bounds what must already be reserved. | `>= 1`, and `FIFO_D` must cover `MAX_OUT * BURST_MAX` beats. |
| `MAX_WOUT` | integer | `32` | Writes in flight. | `>= 1`. |
| `FIFO_D` | integer | `512` | Depth of the staging FIFO between the read return and the write side, in beats. | Must cover every reserved read; under-sizing deadlocks rather than corrupts. |
| `CMD_D` | integer | `128` | Depth of the internal command queue between the walkers and the AXI side. | `>= 1`. |

### `mm_prng` — `src/kohakuaccel/sysnode/mover/mm_prng.v`

Counter-based PRNG behind the mover's `GENERATE` mode. Stateless in the sense
that matters: the value is a pure function of `(key, counter)`, so noise is
independent of how a region was tiled and is restartable after a fault.

| Name | Type | Default | Controls | Legal range |
|---|---|---|---|---|
| `ROUNDS` | integer | `10` | Rounds per 128-bit draw, one round per four cycles. | Changing it changes the generated values. Any value below the algorithm's specified count weakens it. |

## 6. AXI transport and memory models

### `axi_n1` — `src/kohakuaccel/axi/simple/axi_n1.v`

N AXI4 masters onto one slave, across two clock domains. Arbitration, response
routing and the clock crossing — no address decode, no width conversion, no
protocol conversion.

| Name | Type | Default | Controls | Legal range |
|---|---|---|---|---|
| `N` | integer | `4` | Number of master-side interfaces. | `>= 1`. |
| `ADDR_W` | integer | `40` | Address width. | Any. |
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

### `mag_dram_port` — `src/kohakuaccel/sysnode/core/mag_dram_port.v`

N requesters onto one AXI4 master, packing a narrow internal beat up to a wider
memory beat across a clock crossing. This is where every internal requester
converges and where AXI exists exactly once; `mag.v:934` instantiates it with
`N = MP1`.

| Name | Type | Default | Controls | Legal range |
|---|---|---|---|---|
| `N` | integer | `5` | Number of requesters. | `>= 1`. |
| `ADDR_W` | integer | `40` | Address width. | Any. |
| `SW` | integer | `256` | Internal beat width. | Any. |
| `MW` | integer | `512` | Memory beat width. | `SW` times a power of two. |
| `ID_W` | integer | `4` | AXI ID width. | Any. |
| `AWQ` | integer | `16` | Write-address queue depth. | Power of two. |
| `WQ` | integer | `64` | Write-data queue depth. | Power of two. |
| `BQ` | integer | `16` | Write-response queue depth. | Power of two. |
| `ARQ` | integer | `16` | Read-address queue depth. | Power of two. |
| `RQ` | integer | `64` | Read-data queue depth. | Power of two. |
| `RD_OUT` | integer | `1` | Reads one requester may have in flight. **DEFAULTS OFF BECAUSE IT IS BROKEN**: at 4 it measured 2,744 → 8,917 MB/s on 20-word bursts and **corrupted memory in `mover_chain1`**. The bandwidth is real and so is the corruption. | `1`, until the corruption is found. |
| `WR_MEM` | string | `"block"` | Storage primitive for the wide queues. | `"distributed"`, `"block"`. |

### `mag_dram_rr` — `src/kohakuaccel/sysnode/core/mag_dram_port.v`

Lowest set bit at or after a base, wrapping. Shared by both of that module's
arbiters.

| Name | Type | Default | Controls | Legal range |
|---|---|---|---|---|
| `N` | integer | `5` | Request vector width. | `>= 1`. |
| `IDX_W` | integer | `3` | Index width. | `>= $clog2(N)`. |

### `axi_ram` — `src/kohakuaccel/verif/axi_ram.v`

AXI4 slave RAM standing in for DRAM so the machine can be simulated end to end.
One outstanding transaction per port, INCR bursts only, no narrow transfers, no
interleaving.

| Name | Type | Default | Controls | Legal range |
|---|---|---|---|---|
| `DATA_W` | integer | `256` | Data width. Matches the flit payload so nothing in the simulation path gears between widths. | Any. |
| `ADDR_W` | integer | `40` | Address width. | Any. |
| `ID_W` | integer | `4` | AXI ID width. | Any. |
| `WORDS` | integer | `4096` | Storage depth in `DATA_W`-bit words. | Any. |
| `PORTS` | integer | `1` | Independent AW/W/B and AR/R channel sets over one array — a model of a multi-channel controller in front of one address space. | `>= 1`. At 1 every port width is exactly what a single-port instantiation expects. |

Two ports writing the same word in the same cycle is last-writer-wins here and
unordered on real hardware. The framework does not prevent it.

### `axi4_ram` — `src/kohakuaccel/verif/axi4_ram.v`

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

### `axi4_master` — `src/attic/legacy-axi/axi4_master.v`

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
[arch/sysnode/](../arch/sysnode/). Registers:
[control-registers.md](control-registers.md) §4.

### `mag_ilink` — `src/kohakuaccel/sysnode/interlink/mag_ilink.v`

Everything the memory agent needs to speak to another mesh: the mover's remote
writes, inbound remote writes, flit encapsulation and injection, and doorbells.

| Name | Type | Default | Controls | Legal range |
|---|---|---|---|---|
| `FLIT_WIDTH` | integer | `288` | Flit width. | See §1. |
| `POS_WIDTH` | integer | `4` | Coordinate width. | See §1. |
| `DATA_W` | integer | `256` | AXI data width. | See §1. |
| `ADDR_W` | integer | `40` | Address width. `[37:36]` selects the mesh, which is what `mag_ilink` compares against `MESH_ID` to decide local or remote. | See §1. |
| `LINK_W` | integer | `288` | Beat width. A beat is one flit, so a flit crosses verbatim with nothing packed, padded or reconstructed. | Must match at both ends. |
| `TUSER_W` | integer | `96` | Packet header width. | Must match at both ends. |
| `MESH_ID` | integer | `0` | Reset value of the mesh id register. The live value is a runtime register, so one bitstream is usable at any position in the grid. | 0–3. |
| `MAX_BEATS` | integer | `32` | Longest packet emitted. | `<= RX_BEATS` at the far end. |
| `MEM_X` | integer | `0` | The local memory port's X coordinate, used when injecting an inbound flit. | Must match `mag`'s `MEM_X`. |
| `MEM_Y` | integer | `1` | Its Y coordinate. | Must match `mag`'s `MEM_Y`. |

### `mag_switch` — `src/kohakuaccel/sysnode/interlink/mag_switch.v`

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

### `mag_link` — `src/kohakuaccel/sysnode/interlink/mag_link.v`

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

### `mag_link_pipe` — `src/kohakuaccel/sysnode/interlink/mag_link_pipe.v`

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

### `il_pkt_mux2`, `il_pkt_demux` — `src/kohakuaccel/sysnode/interlink/il_pkt_arb.v`

Packet-stream plumbing: one 2:1 merge and one 1:N split, both locking for the
duration of a packet. A mux that re-arbitrates per beat interleaves two packets
on one stream, and a receiver that frames by `TLAST` cannot tell.

`il_pkt_demux` takes `N_OUT` real outputs and treats `sel_in == N_OUT` as a
sink that accepts and discards. `mag_switch` uses `N_OUT=4` for local egress
({link, class}) and `N_OUT=2` for each transit stream's class split.

| Name | Type | Default | Controls | Legal range |
|---|---|---|---|---|
| `LINK_W` | integer | `288` | Beat width. | Must match the link. |
| `TUSER_W` | integer | `96` | Header width. | Must match the link. |

## 8. Shared primitives

### `sync_fifo` — `src/kohakuaccel/common/sync_fifo.v`

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

### `async_fifo` — `src/kohakuaccel/common/async_fifo.v`

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

### `kohaku_sdpram` — `src/kohakuaccel/common/kohaku_sdpram.v`

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

### `MultiBitLut` — `src/attic/common/lut.v`

Direct instantiation of LUT primitives for a small hard-coded table.

| Name | Type | Default | Controls | Legal range |
|---|---|---|---|---|
| `input_bits` | integer | `6` | Table index width. | `5` or `6`. Other values instantiate nothing. |
| `output_bits` | integer | `10` | Table output width. | Must be a multiple of `7 - input_bits`. |
| `INIT` | bit vector | `0` | The table contents, `64 * output_bits / (7 - input_bits)` bits. | Sized by the expression above. |

### `xorshift64`, `xorshift128_single`, `xorshift256` — `src/attic/common/xorshift.v`

No parameters.

### `float_display` — `src/attic/common/fp.v`

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

### `mag_xform` — `src/kohakuaccel/sysnode/core/mag_xform.v`

The transform slot's framework side: arbitration, the requester mux, the
registered stage, and the geometry parameters. It instantiates `xform_bank` and
nothing else.

| Name | Type | Default | Controls | Legal range |
|---|---|---|---|---|
| `DATA_W` | integer | `256` | Beat width. | See §1. |
| `NREQ` | integer | `2` | Requesters contending for the bank. | `>= 1`. |
| `SLOTS`, `ID_W`, `MODE_W` | integer | `1`, `1`, `1` | Passed through to the bank. | See `mag`. |
| `IN_BITS`, `OUT_WORDS` | integer | `2048`, `4` | The occupant's declared geometry. | Must match the bank; `OUT_WORDS <= 4`, because the bank presents `word0..word3`. |

It also carries the occupant register port (`cfg_*`, `fault`) through to the
bank, interpreting none of it — the same contract `mode` has.

**Grant is held for a whole run**, and a requester must not issue its read until
it holds one — that is what makes it impossible for a beat to arrive with nowhere
to go. Per-entry grant is unsafe: a requester issues the next entry's read while
the current entry is still in the occupant.

### The converting move — `mm_mover` mode 5

`mem/L2 → slot → mem/L2` is a **mode of the mover**, not a separate engine: the
slot sits on the mover's read-return path, between R and its staging FIFO. The
four parameters that carry the occupant's geometry into the mover — `XID_W`,
`XMODE_W`, `XF_IN_BITS`, `XF_OUT_WORDS` — are in [the `mm_mover`
table](#mm_mover--srckohakuaccelsysnodemovermm_moverv) and are not repeated
here, because a value stated twice is a value that will disagree with itself.

Commanded as an ordinary descriptor: the id and mode ride the source walker's
header at `0x10` bits `[50:47]` and `[58:55]`, the source walker counts source
words, and the destination walker counts entries.

> It was a separate engine, `mm_xfer.v`, muxed onto the mover's AXI channel,
> because the mover's flow control was one 32-byte word in per word out and a 2:1
> transform breaks that. Folded, the reservation counts `OUT_WORDS` per entry
> instead of one per element — still static, still taken before the AR.

### `xform_bank` — two files, one module name

**The one module the framework names.** It holds a project's occupants and
demuxes the id internally; id 0 is bypass. A build compiles exactly one of:

| file | |
|---|---|
| `src/kohakutpu/transform/xform_bank.v` | id 0 bypass, id 1 the quantiser |
| `src/templates/transform/xform_bank.v` | the identity bank: every id bypass, `IN_BITS = 4 × DATA_W`, `OUT_WORDS = 4` |

Sharing the module name is deliberate: it is what lets a framework-only build
elaborate with no project source at all.

Every bank presents `cfg_en / cfg_id / cfg_addr / cfg_data / cfg_rdata / fault`.
Register `0x00` reads the sticky fault and any write clears it; `0x04` reads
`{8'd0, OUT_WORDS, IN_BITS}` for `cfg_id`, or zero if that id names no occupant.
`fault[0]` means an entry was started with an id naming no occupant — the one
fault a bank can detect by itself, and it matters because the demux would
otherwise answer with bypass and report success.

### `mx_quant` — `src/kohakutpu/transform/mx_quant.v`

KohakuTPU's occupant of the transform slot, at id 1: FP16 to a 7-bit block
format with a shared E5M3 scale. It is reached through
`src/kohakutpu/transform/xform_bank.v`, which is the one module the framework
names — see [transform-slot.md](transform-slot.md).

| Name | Type | Default | Controls | Legal range |
|---|---|---|---|---|
| `SBIAS` | integer | `20` | Scale exponent bias. The scale spans 2⁻²⁰ to 2¹⁰ with an FP16 peak, so 20 centres it on the 5-bit field. | Changing it changes the numeric result and MUST be matched by the consuming datapath and by the driver's software model. |

Its entry sizes — 2048 bits in, 1024 bits out — are what the bank declares to
the agent as `IN_BITS` and `OUT_WORDS`, because the mover has to size both walks
before the transform has run. They are also what set the default `entry_words`
of 4 for an ordinary fetch of already-converted operands.
