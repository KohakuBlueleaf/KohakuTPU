---
title: Control registers
summary: The CU_CTRL block every compute unit answers over the mesh, and the orchestrator's AXI register map — dispatch, credits, completions, the status mirror and the mailbox.
tags:
  - spec
  - normative
  - registers
  - control-plane
---

# Control registers

> **Kind: Fixed** throughout. Every offset, width and bit position below is
> protocol. Two rows are labelled Convention where they are: what `cu_type`
> should look like, and what a unit ought to put in `CU_DBG`.

Two register surfaces, both framework-owned.

- **`CU_CTRL`** (§1) is reached over the mesh, one block per compute unit. It is
  how a controller enumerates a machine it was not told the shape of.
- **The orchestrator's register map** (§2–§5) is reached over AXI. It is how a
  host dispatches work and observes completion.

Neither is a debug convenience. Discovery and the status mirror are the only
things standing between a driver and a hardcoded map, and the credit registers
are the mechanism that keeps dispatch from deadlocking the mesh.

## 1. `CU_CTRL` — the per-unit block

Source of truth: `src/kohakunoc/noc_cu_base.v`.

### 1.1 How it is accessed

A `CU_CTRL` request is a single flit addressed at the unit's coordinate. The
reply is a single flit addressed back at the requester, with `txn` echoed.

`noc_cu_base` answers it **inside the endpoint**. The request never enters the
receive queue and never reaches the datapath, which is what makes discovery work
on a unit that is busy, stalled, or wedged on its own datapath.

Flit payload layouts are in [flit-format.md](flit-format.md) §4.8. In summary:

| | `[255:248]` | `[247:240]` | `[239:176]` |
|---|---|---|---|
| Request | `op`, MUST be 0 and is **not read** | `index` | — |
| Reply | `0x02`, read response | `index`, echoed | `value`, 64 bits |

Only one request is in flight per unit. A second arriving while a reply is
pending is **accepted and its index discarded**; the pending reply is unaffected.
A controller **MUST** wait for a reply before issuing the next request to the
same node.

There is no write path. Every index is read-only.

### 1.2 Index map

Four indices are mandatory and identical across every unit type, whatever it
computes. That is what makes the block worth having.

| Index | Name | Contents | Set by |
|---|---|---|---|
| `0` | `CU_CAPS` | What this endpoint is. | Parameters at elaboration. |
| `1` | `CU_STATUS` | What it is doing now. | The framework, live. |
| `2` | `CU_COUNTERS` | Retired instructions and busy cycles. | The framework, live. |
| `3` | `CU_DBG` | The datapath's own 64 bits. | **The unit**, via `dbg_ctr`. |
| `4`–`255` | — | Return zero. Reserved to the framework. | — |

An index above 3 returns `64'd0`. A unit **MUST NOT** assume an unallocated index
is free to use; the reply path is inside `noc_cu_base` and a unit cannot extend
it without forking the module.

### 1.3 Register layouts

#### Index 0 — `CU_CAPS`

| Bits | Field | Width | Source |
|---|---|---|---|
| `[63:48]` | `cu_type` | 16 | `CU_TYPE` parameter |
| `[47:40]` | `cu_version` | 8 | `CU_VERSION` parameter |
| `[39:36]` | `n_buffers` | 4 | `N_BUFFERS` parameter |
| `[35:20]` | `inst_depth` | 16 | `INST_DEPTH` parameter |
| `[19:0]` | zero | 20 | — |

- `cu_type` is any 16-bit value; the framework does not allocate type codes and
  does not check for collisions. *(Convention, free: two printable ASCII
  characters, so an unknown endpoint reports something readable rather than a
  number. KohakuTPU uses `'MG'` `0x4D47` for the matmul cluster, `'VC'` `0x5643`
  for the vector core, `'MX'` `0x4D58` for the earlier matmul unit.)*
- `cu_version` is a **mesh-wide build number, not the endpoint's own revision.**
  The question a driver asks is "is this bitstream the one my compiler targets",
  so every endpoint in an image **MUST** carry the same value and it **MUST** be
  bumped whenever any instruction set or datapath in that image changes. A stale
  value is exactly the case this field exists to catch: an old bitstream silently
  doing something else with a new program's bits.
- `n_buffers` is how many `CU_DATA` buffer indices the unit accepts, counting
  from 0. See [flit-format.md](flit-format.md) §4.7.1.
- `inst_depth` is the instruction FIFO's total depth, so a dispatcher can seed
  credit without a hardcoded constant.

`CU_CAPS` is deliberately one word. Richer self-description belongs behind a
descriptor block, not in a wider `CU_CAPS`, so the mandatory region stays fixed
forever.

#### Index 1 — `CU_STATUS`

| Bits | Field | Width | Meaning |
|---|---|---|---|
| `[63]` | `busy` | 1 | An instruction is in flight, or queued, or a completion is unsent. |
| `[62]` | `error` | 1 | **Tied to 0.** Allocated, unimplemented. |
| `[61:48]` | zero | 14 | — |
| `[47:32]` | `inst_space` | 16 | **Free** entries in the instruction FIFO. |
| `[31:0]` | zero | 32 | — |

`inst_space` at zero means the dispatcher is being held off, which is a different
problem from a slow datapath and looks identical in wall-clock time.

`busy` covers completions that have been *generated* but not yet *left*. A unit
is not idle until its signals are on the wire.

#### Index 2 — `CU_COUNTERS`

| Bits | Field | Width | Meaning |
|---|---|---|---|
| `[63:32]` | `instructions_retired` | 32 | Counts `exec_done`. |
| `[31:0]` | `busy_cycles` | 32 | Counts cycles with `busy` high. |

Counted inside `noc_cu_base`, so **every unit type reports these identically**
whatever it computes. That is the point of them living there, and it is why a
unit MUST NOT reimplement them in `dbg_ctr`.

Both accumulate since `resetn` and **neither can be cleared**. There is no clear
register. A measurement is the difference between two reads, taken modulo 2³².
At a few hundred MHz that is a wrap roughly every ten seconds.

Wall-clock timing cannot substitute: one JTAG access is milliseconds against
microseconds of compute.

#### Index 3 — `CU_DBG`

| Bits | Field | Width | Meaning |
|---|---|---|---|
| `[63:0]` | `dbg_ctr` | 64 | Whatever the unit drives. Published verbatim. |

**This is the one index whose contents are unit-defined**, and the only part of
the block a unit implements. See §1.4.

### 1.4 What a unit owes

Almost nothing, and that is deliberate. A unit that instantiates `noc_cu_base`
satisfies §1.1–§1.3 by construction. Its obligations are:

- **MUST** drive `dbg_ctr`. A unit with nothing to report **MUST** tie it to
  zero rather than leave it floating.
- **MUST** publish what `dbg_ctr` means, and whether it is cumulative or
  per-run. The driver decodes index 3 per `CU_TYPE`, so an undocumented value is
  unreadable.
- **MUST NOT** duplicate index 2. Retired instructions and busy cycles are
  already counted identically for every unit.

*Convention, free: report something a stalled machine can be diagnosed from. The
useful shape is time spent waiting against time spent computing, because it turns
"slower than expected" into a cause rather than a size.*

KohakuTPU's two units illustrate the range and the hazard:

| Unit | `dbg_ctr` | Scope |
|---|---|---|
| `mx_cluster_cu` | `{compute_cycles, memory_cycles}` — the array running against the sequencer waiting on operands. Both free-running and **independent**, so they overlap and their sum is not a total. | Cumulative; difference two reads. |
| `vec_cu` | `{32'd0, kernel_cycles}` | **Per run.** The core clears it at every start, so it describes the last kernel and MUST NOT be differenced. |

Two units, two scopes, one index. That is legal, and it is exactly why the
obligation to publish the meaning is a MUST.

## 2. The orchestrator register map

Source of truth: `src/kohakunoc/noc_orchestrator.v`.

### 2.1 Access

A 64-bit AXI4 slave. Every register is one 64-bit word at an 8-byte-aligned
offset; there are no byte-enable semantics on registers other than the mailbox
staging words.

The orchestrator is instantiated inside the memory agent as its control plane and
its window is the agent's control AXI slave. The agent adds no offset: the
addresses below are offsets within that window.

**AXI and the mesh share one clock.** There is no clock crossing inside this
module; the interconnect in front of it does the crossing.

Reads and writes obey the reference AXI discipline used throughout the
framework: `VALID` is never a function of `READY`, a burst's length comes from a
counter rather than from `WLAST`, and `BID`/`RID` echo `AWID`/`ARID`. A burst
walks the address, so a burst write hits consecutive registers in order.

### 2.2 The map

| Offset | Name | Access | Contents |
|---|---|---|---|
| `0x0000` | `CTRL` | RW | Stored and read back. **No other logic reads it.** |
| `0x0008` | `STATUS` | RO | `[0]` busy (`!tx_empty \| prog_run`), `[1]` error (tied 0), `[2]` mesh_ready (tied 1) |
| `0x0010` | `CAPS` | RO | `[15:0]` `FLIT_WIDTH`, `[23:16]` `POS_WIDTH`, `[31:24]` `GRID_LO`, `[39:32]` `GRID_HI` |
| `0x0018` | `IRQ_STAT` | W1C | Write-1-to-clear. **Nothing ever sets it.** |
| `0x0020` | `IRQ_EN` | RW | Stored and read back. **No interrupt output exists.** |
| `0x0040` | `PROG_DST` | RW | `[2*POS_WIDTH-1:0]` = `{y, x}` of the dispatch target |
| `0x0048` | `PROG_LEN` | RW | `[15:0]` flits to send |
| `0x0050` | `PROG_KICK` | W | Any write starts dispatch |
| `0x0058` | `PROG_STAT` | RO | `[0]` run, `[16:1]` flits left, `[32:17]` credit |
| `0x0060` | `PROG_CRED` | RW | `[15:0]` credit. Write seeds; read returns the live value |
| `0x0068` | `PROG_BASE` | RW | `[15:0]` first staging slot of this program |
| `0x0070` | `SIG_DONE` | R / W-clear | Read: total completions from every node. Write: clear |
| `0x0078` | `AUX_STAT` | RO | One 64-bit word from the attached client. §3 |
| `0x0080`–`0x00FF` | `AUX_STATW[0..15]` | RO | Sixteen 64-bit words from the attached client. §4 |
| `0x0100`–`0x0127` | `TX_FLIT[0..4]` | RW | The mailbox flit, low word first, byte-enabled |
| `0x0140` | `TX_KICK` | W | Push `TX_FLIT` into the transmit FIFO |
| `0x0148` | `TX_STATUS` | RO | `[16]` tx_full |
| `0x0180`–`0x01A7` | `RX_FLIT[0..4]` | RO | The head of the receive FIFO, low word first |
| `0x01C0` | `RX_POP` | W | Pop the receive FIFO |
| `0x01C8` | `RX_STATUS` | RO | `[16]` rx_empty, `[17]` rx_overflow (sticky) |
| `0x0800`–`0x08FF` | `AUX_CFG` | W | Forwarded verbatim to the attached client. §3, §4 |
| `0x1000`–`0x1FFF` | `NODE_STATUS[{y,x}]` | RO | The status mirror, one word per coordinate. §2.5 |
| `0x2000`+ | `STAGE` | W | Instruction staging RAM. §2.6 |

Unlisted offsets read zero and ignore writes.

### 2.3 Dispatch

The orchestrator holds instruction flits in a local staging RAM and forwards
them. **It has no AXI master and never fetches from DRAM**; it only forwards what
the host already placed in it. That is why a compute unit fetches its own
operands rather than being fed by the controller.

The sequence:

1. Write the program's flits into `STAGE`, five 64-bit words per flit, starting
   at slot `B`. §2.6.
2. `PROG_BASE = B`, `PROG_LEN = n`, `PROG_DST = {y, x}`.
3. `PROG_CRED = c`, seeding the credit counter.
4. Write `PROG_KICK`.

`PROG_BASE` exists so a second target's flits can be staged while the first
program is still being consumed. Without it every kick restarts at slot 0, which
serialises nodes that have no data dependency.

The dispatcher **rewrites the routing header** of each flit as it pushes it:
destination from `PROG_DST`, source from the orchestrator's own coordinates. Type,
`txn`, `last` and the payload pass through untouched. This is what lets one
staged program be dispatched to several nodes, and it is why staged `dst`/`src`
fields are don't-care.

### 2.4 Credit

**This is the deadlock prevention mechanism, not an optimisation.**

Backpressuring a `CU_INST` into the mesh is the protocol deadlock the framework
exists to avoid: a node whose input fills with instructions it cannot drain
stalls the link, and the link carries the completions that would drain it.

The rule: **a sender MUST NOT dispatch more instructions to a node than that
node's instruction FIFO can hold.**

| Event | Effect on `PROG_CRED` |
|---|---|
| Host writes `PROG_CRED` | Set to the written value. A host write always wins, so re-seeding between programs is predictable. |
| A `CU_INST` flit is pushed | Decrement, unless a completion arrives the same cycle. |
| `SIG_INST_COMPLETE` arrives from any node | Increment, unless a flit is pushed the same cycle. |
| Credit reaches zero | The dispatcher **stalls locally**, which is safe, instead of stalling the network, which is not. |

A host **MUST** seed `PROG_CRED` with at most the target's `inst_depth` from
`CU_CAPS` index 0. Seeding higher is the one way to reintroduce the deadlock.

Note the asymmetry that a driver has to get right: credit is refilled by
`SIG_INST_COMPLETE` only. The **final** instruction of a program reports
`SIG_BATCH_COMPLETE` instead, so a program of `n` instructions returns `n-1`
credits. Use `SIG_DONE`, not credit, to decide a program has finished.

### 2.5 Completions and the status mirror

`CU_SIGNAL` is **summarised, not queued.** It is written into `NODE_STATUS` and
the flit is dropped.

The reason is the same deadlock in a different place: queued, unread signals fill
the receive FIFO, raise the orchestrator's `busy`, and stop it accepting
anything — including the signals that return dispatch credits. A host that never
drains the mailbox would wedge the control plane after `RX_DEPTH` completions.
`NODE_STATUS` is the mechanism for completions; the mailbox is for traffic with
no other home, such as `CU_CTRL` replies.

`NODE_STATUS[{y, x}]` at `0x1000 + ({y,x} * 8)`:

| Bits | Field | Width | Meaning |
|---|---|---|---|
| `[63:56]` | `code` | 8 | The `CU_SIGNAL` code most recently received from this node. |
| `[55:24]` | `arg` | 32 | Its argument. |
| `[23:8]` | `signal_count` | 16 | How many signals this node has sent. |
| `[7:1]` | zero | 7 | — |
| `[0]` | `valid` | 1 | Set once this node has signalled at all. |

`signal_count` is a **count, not a sticky flag**, so a host polling slower than
events arrive can tell how many it missed. There is a slot for every coordinate,
edge endpoints included.

`SIG_DONE` at `0x0070` is the same information collapsed: completions from every
node in one register, so "is everyone finished" costs one read rather than one
per node. It counts **every** signal, matching `NODE_STATUS` — including
`SIG_BATCH_COMPLETE` and `SIG_DATA_RECEIVED`. A host counting only
`SIG_INST_COMPLETE` would see N-1 of every N and wait forever. Writing the
register clears it.

### 2.6 The mailbox and the staging RAM

**The mailbox** is the raw-flit path, and it is the only way to send a flit the
framework would not otherwise construct — a `CU_CTRL` read, or a deliberately
malformed header. An address-mapped bridge could only ever emit `MEM_RD_REQ` and
`MEM_WR_REQ`.

- Write the five words of `TX_FLIT`, low word first, then write `TX_KICK`.
- The mailbox stamps **nothing**. Destination, source and every other field are
  exactly what was written. That is its purpose.
- `TX_KICK` is **ignored while `prog_run` is set** and while the transmit FIFO is
  full; the mailbox and the dispatcher share one FIFO. A host **MUST** check
  `PROG_STAT[0]` or wait for the program to finish rather than assuming the
  kick took.
- Receive: read `RX_STATUS[16]` for empty, read the five `RX_FLIT` words, then
  write `RX_POP`. `RX_STATUS[17]` is a sticky overflow flag.

**The staging RAM** holds instruction flits at `0x2000 + slot * 40`, five 64-bit
words per flit, low word first.

Its extent is `STAGE_FLITS * 5 * 8` bytes and the decode is derived from that, not
fixed at one page. A write inside the range lands in the RAM; a write outside it
falls through to the register decode.

**At the default `STAGE_FLITS = 128` the window is 5120 bytes and ends at
`0x3400`, past the end of the `0x2xxx` page.** The hardware handles that
correctly. An address map or driver that allocates a single 4 KB page for staging
does not: a full program's last 26 flits land in register space, the program
stops early, and the staging RAM still holds whatever was there before. Size the
mapping from `STAGE_FLITS`, never from a page.

## 3. The memory mover's command registers

The `AUX_CFG` window at `0x0800`–`0x08FF` is forwarded out of the orchestrator
verbatim, with the **offset within the window** as the client's own register
address. It is 256 bytes rather than 256 indexed slots precisely so a client keeps
its own offsets; an index here would alias a client's `0x38` onto its `0x00`.

The window is split at `0x80`:

| Sub-range | Client |
|---|---|
| `0x0800`–`0x087F` (client offsets `0x00`–`0x7F`) | the memory mover |
| `0x0880`–`0x08FF` (client offsets `0x80`–`0xFF`) | the interlink, when built |

With no interlink the split is a constant and the mover sees every write, as it
always has.

Mover registers, at client offsets. Writes only; status is read back through
`AUX_STAT`.

| Offset | Fields |
|---|---|
| `0x00` | `[2:0]` mode, `[4:3]` element width, `[15:8]` flags, `[16]` **GO** |
| `0x10` | `[0]` which walker (0 source, 1 destination), `[4+:ADDR_W]` base address, `[46:44]` number of dimensions |
| `0x18` | `[0]` walker, `[3:1]` dimension, `[19:4]` count, `[51:20]` signed stride |
| `0x20` | `[1:0]` axis, `[17:2]` signed axis step |
| `0x28` | `[0]` walker, `[1]` axis select, `[17:2]` signed axis base, `[33:18]` axis extent |
| `0x30` | `[ADDR_W-1:0]` index-buffer base address, `[55:40]` index count |
| `0x38` | `[63:0]` PRNG seed |
| `0x40` | `[31:0]` immediate, used as the fill value and as padding |
| `0x50` | `[31:0]` gather pitch, `[47:32]` gather words |

Modes: `0` copy, `1` transpose (**faults — not implemented**), `2` gather,
`3` generate, `4` fill.

`AUX_STAT` at `0x0078` reports:

| Bits | Field |
|---|---|
| `[63:40]` | moves completed |
| `[39:24]` | memory-agent read count, summed across ports |
| `[23:8]` | memory-agent write count, summed across ports |
| `[7:4]` | fault code: 0 none, 1 index length, 2 range, 3 AXI, 4 mode, 5 element width, 6 alignment |
| `[3:1]` | zero |
| `[0]` | busy |

The two traffic counters are 16 bits and free-running. **Read deltas, not
totals.**

## 4. The interlink registers

Present only when the interlink is built. Writes go to `AUX_CFG` client offsets
`0x80`+; reads come back through the `AUX_STATW` window at `0x0080`–`0x00FF`,
whose index is `(offset - 0x80) / 8`.

Writes:

| Offset | Fields |
|---|---|
| `0x80` | `[0]` enable, `[1]` clear doorbell counters, `[2]` clear the fault register |
| `0x88` | `[1:0]` this mesh's id — a **runtime** value, not a parameter, so one bitstream is usable at any position in the grid |
| `0x90` | `[1:0]` doorbell destination mesh, `[15:8]` doorbell tag. The write itself rings it |

Reads, by `AUX_STATW` index:

| Index | Contents |
|---|---|
| `0` | Capability word, and **zero while disabled** — reading zero here is how a driver learns the interlink is absent or off. `[15:0]` = `0x494C` (`'IL'`); `[19:16]` = 2; `[21:20]` = the live mesh id; `[23:22]` = 0; `[27:24]` = 4; `[31:28]` = 1; `[63:32]` = 0. The three constant nibbles are unnamed in the source and are not interpreted here. |
| `1` | `[7:0]` sticky fault register |
| `2`–`5` | Per-destination-mesh doorbell counters: `[15:0]` received, `[31:16]` sent |
| `6`, `7` | Link 0 transmit and receive beat counters |
| `8`, `9` | Link 1 transmit and receive beat counters |
| `10`, `11` | Link 0 and link 1 stall counters |
| `12` | Forwarded-packet counter |
| `13` | `[31:0]` link 0 credit state, `[63:32]` link 1 |
| `14` | Doorbells sent |
| `15` | Local-egress block counter |

Fault register bits:

| Bit | Name | Raised when |
|---|---|---|
| `0` | `RD_REMOTE` | A memory request named a mesh other than this one. The access aliased to local memory. |
| `1` | `ACK0` | A remote `CU_DATA` burst arrived with no explicit ack destination, so its completion cannot be routed. |
| `2` | `SWITCH` | The inter-mesh switch reported a fault — a packet asking for a turn the routing model forbids. |
| `3` | `AXI` | An AXI error on the mover's write path or the inbound write path. |
| `4` | `INJ` | An inbound flit could not be injected into the local mesh and was dropped. |

All five are **sticky** and cleared only by writing `0x80` bit 2.

## 5. The host control-program engine

Source of truth: `src/kohakuaxi/main_orch.v`. A separate AXI slave — not a mesh
node — whose reach into the machine is an AXI write into a memory agent's control
window. Dispatch, configuration and debug injection therefore share one
mechanism.

Its value is that a run becomes **one host transaction**: the host is not in the
loop per poll, and the same program works over JTAG and over PCIe.

| Offset | Name | Access | Contents |
|---|---|---|---|
| `0x0000` | `CTRL` | W: `[0]` GO. R: `[0]` busy, `[1]` done, `[2]` err | There is no abort. |
| `0x0008` | `PC` | RO | Current command index. |
| `0x0010` | `CODE` | RO | The `DONE` code. |
| `0x0018` | `POLLS` | RO | Polls executed, for debugging a program that will not finish. |
| `0x1000`+ | `CMD[n]` | W | Command `n`, field `f`, at `0x1000 + n*32 + f*8`. |

Command fields:

| `f` | Contents |
|---|---|
| 0 | `[3:0]` opcode |
| 1 | `[ADDR_W-1:0]` address |
| 2 | `WR`: data. `POLL`: the wanted value |
| 3 | `POLL`: mask |

Opcodes:

| Code | Name | Meaning |
|---|---|---|
| 1 | `WR` | Issue an AXI write of `data` to `addr`. |
| 2 | `POLL` | Read `addr` until `(data & mask) == want`. Retries every `POLL_IVL` cycles. |
| 3 | `DONE` | Stop, latch `code`, raise the done flag. |

Three opcodes are enough because the machine's whole control surface is
memory-mapped. Branches or arithmetic here would duplicate the host.

## 6. Known divergences

| Divergence | Detail |
|---|---|
| `CU_CTRL` map versus the snapshot | An earlier pre-reframing snapshot lists byte offsets `0x00/0x04/0x08/0x0C` and registers `CU_CONTROL` (RW) and `CU_ERROR`. The RTL uses word **indices** 0–3, has no writable register at all, and indices 2 and 3 are counters. §1.2 is the silicon. |
| `CU_STATUS.error` | Allocated, tied to zero. A unit's faults are reported through `SIG_FAULT`, not here. |
| `CTRL`, `IRQ_EN`, `IRQ_STAT` | Storage with no consumer. No interrupt output exists on the orchestrator. |
| Staging window versus 4 KB | The decode is derived from `STAGE_WORDS` and at the default `STAGE_FLITS = 128` extends past `0x2FFF`. Correct in RTL; a hazard for a host that assumes one page. §2.6. |
| `CU_VERSION` default | The parameter defaults to `8'h01`; every instantiation in the tree overrides it to the current build number. A unit that forgets to override it reports a version it does not have. |
| Orchestrator location | `noc_orchestrator.v` lives under `src/kohakunoc/` but is the memory agent's control plane and is instantiated only by `mag.v`. |
