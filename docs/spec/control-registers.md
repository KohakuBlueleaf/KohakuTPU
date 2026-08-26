---
title: Control registers
summary: Every control register in the framework — the CU_CTRL block a compute unit answers over the mesh, the orchestrator's AXI map, the mover and interlink windows, and the RV64 control complex's host window and control region.
tags:
  - spec
  - normative
  - registers
  - control-plane
---

# Control registers

> **Kind: Fixed** throughout. Every offset, width and bit position below is
> protocol. Three rows are labelled Convention where they are: what `cu_type`
> should look like, what a unit ought to put in `CU_DBG`, and the dispatch order
> in §2.3.

Four register surfaces, all framework-owned. A **register surface** here means a
set of addresses one agent writes and another agent answers; the four differ in
who can reach them, not in kind.

| § | Surface | Reached over | By |
|---|---|---|---|
| §1 | **`CU_CTRL`**, one block per compute unit | the mesh, as a flit | any controller. This is how a machine is enumerated without a hardcoded map. |
| §2 | **The orchestrator's map** — dispatch, credit, the status mirror, the mailbox, the staging RAM | AXI, the system node's control window | the host |
| §3–§4 | **The mover's and the interlink's windows**, forwarded verbatim out of the orchestrator | the same AXI window | the host |
| §6–§7 | **The RV64 control complex's host window and control region** | a dedicated port, and the processor's own loads and stores | the host, and software running on the processor. Present only when `CPU_RV64` is set. |

§5 is a fifth thing and not a fourth surface: a host-side engine that drives §2
on the host's behalf.

None of these is a debug convenience. Discovery and the status mirror are the
only things standing between a driver and a hardcoded map, and the credit
registers are the mechanism that keeps dispatch from deadlocking the mesh.

## 1. `CU_CTRL` — the per-unit block

Source of truth: `src/kohakuaccel/noc/endpoint/noc_cu_base.v`.

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
| `[63:32]` | `instructions_retired` | 32 | Counts **every** `exec_done`, including one asserted with no instruction in flight — which produces no completion. See [compute-unit-port.md](compute-unit-port.md) §4. |
| `[31:0]` | `busy_cycles` | 32 | Counts cycles with `busy` high. |

Counted inside `noc_cu_base`, so **every unit type reports these identically**
whatever it computes. That is the point of them living there, and it is why a
unit MUST NOT reimplement them in `dbg_ctr`.

Both accumulate since `resetn` and **neither can be cleared**. There is no clear
register. A measurement is the difference between two reads, taken modulo 2³² —
and a reader **MUST** take it modulo 2³², because at any plausible fabric clock a
32-bit counter wraps in seconds, not hours. The RV64 control complex's own
counters are 64 bits for exactly this reason (§6.3), and they are *not*
free-running, so the two cannot share a decoder.

Wall-clock timing cannot substitute. A single access over the debug transport
costs orders of magnitude more time than the work being measured, so the host's
clock measures the transport.

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

Source of truth: `src/kohakuaccel/noc/ctrl/noc_orchestrator.v`.

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

**Three of those ranges scale and the table shows them at the reference build.**
`TX_FLIT` and `RX_FLIT` are `FLIT_WORDS` words each, and `STAGE` extends
`STAGE_FLITS * FLIT_WORDS * 8` bytes from `0x2000` — §2.6. `FLIT_WORDS` is
`ceil(FLIT_WIDTH / DATA_WIDTH)`, five here. Every other offset in the table is a
fixed decode.

### 2.3 Dispatch

The orchestrator holds instruction flits in a local staging RAM and forwards
them. **It has no AXI master and never fetches from DRAM**; it only forwards what
the host already placed in it. That is why a compute unit fetches its own
operands rather than being fed by the controller.

The sequence:

1. Write the program's flits into `STAGE`, `FLIT_WORDS` words per flit — five at
   the reference build — starting at slot `B`. §2.6.
2. `PROG_BASE = B`, then `PROG_LEN = n`, then `PROG_DST = {y, x}` — **in that
   order**.
3. `PROG_CRED = c`, seeding the credit counter.
4. Write `PROG_KICK`.

> **Kind: Convention — but binding on a bitstream built before the write window
> honoured byte strobes.** The order is not a hardware requirement: on current
> RTL any order works, because `PROG_KICK` genuinely kicks. On an earlier
> bitstream a 64-bit host write lands on four registers at once, `PROG_LEN` is
> what actually launches the dispatch, `PROG_KICK` launches nothing, and each
> position above is forced for a different reason. **§2.7 states which, and
> which registers each write destroys.** Read it before changing anything in this
> sequence.

**A driver that elides unchanged writes must not elide these.** Kick 2 of a round
normally carries a new `PROG_BASE` and the SAME `PROG_LEN`, so a write-shadow
skips the length — which the sequence has just cleared — and the kick launches
zero flits. Nothing reports an error: the bus is healthy, `STATUS` shows
`mesh_ready`, and the symptom is one node taking every completion while the other
signals nothing.

Neither failure raises `error`. `PROG_STAT` is the only witness — `run = 1` with
`flits_left > 0` and `credit = 0` is a starved stream; a node whose
`NODE_STATUS.count` never moves was never addressed.

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

- Write all `FLIT_WORDS` words of `TX_FLIT` — five at the reference build — low
  word first, then write `TX_KICK`. The window's extent scales with
  `FLIT_WORDS`, so the register ranges in §2.2 are the reference build's.
- The mailbox stamps **nothing**. Destination, source and every other field are
  exactly what was written. That is its purpose.
- `TX_KICK` is **ignored while `prog_run` is set** and while the transmit FIFO is
  full; the mailbox and the dispatcher share one FIFO. A host **MUST** check
  `PROG_STAT[0]` or wait for the program to finish rather than assuming the
  kick took.
- Receive: read `RX_STATUS[16]` for empty, read all `FLIT_WORDS` `RX_FLIT` words, then
  write `RX_POP`. `RX_STATUS[17]` is a sticky overflow flag.

**The staging RAM** holds instruction flits at `0x2000 + slot * FLIT_WORDS * 8`,
`FLIT_WORDS` words per flit, low word first. `FLIT_WORDS` is
`ceil(FLIT_WIDTH / DATA_WIDTH)`, which the module computes — **five 64-bit words,
so 40 bytes per slot, at the reference build's 288-bit flit and 64-bit control
window.** A driver MUST derive the stride rather than assume 40; both inputs are
parameters.

Its extent is `STAGE_FLITS * 5 * 8` bytes and the decode is derived from that, not
fixed at one page. A write inside the range lands in the RAM; a write outside it
falls through to the register decode.

**At the default `STAGE_FLITS = 128` the window is 5120 bytes and ends at
`0x3400`, past the end of the `0x2xxx` page.** The hardware handles that
correctly. An address map or driver that allocates a single 4 KB page for staging
does not: a full program's last 26 flits land in register space, the program
stops early, and the staging RAM still holds whatever was there before. Size the
mapping from `STAGE_FLITS`, never from a page.

**Staging MUST be written as contiguous blocks, ascending. §2.7.** Word by word
it loses three quarters of every flit on current silicon, with no symptom.

### 2.7 The 32-byte write window, and why the dispatch order exists

**A host write to this window does not arrive as one beat.** The window is
reached across the station bus, whose managers pack to a 32-byte flit: the flit
address is rounded **down**, and the mesh control port is 32 bits wide with an
upsizer in front of it. One 64-bit host write therefore arrives at the
orchestrator as **four 64-bit beats covering a whole 32-byte flit**, of which
only one carries byte strobes.

The current RTL handles that correctly. It obeys `s_axi_wstrb` in three ways,
and the three are the contract:

- **A beat with no strobes does nothing at all** — no register write, and no
  *arrival*, which is what `PROG_KICK` and `SIG_DONE` react to.
- **Register writes byte-merge.** The 16-bit `PROG_*` registers additionally
  require a strobe in bytes 0–1, since that is where they live.
- **`AUX_CFG` accumulates** into a one-entry shadow at the 8-aligned offset and
  pulses the merged value, because the client behind the window has no byte
  enables of its own and can only be handed a whole word.

**A bitstream built before that change writes four registers per host write**,
and the three the host did not write are **zeroed** rather than corrupted — the
packer clears at the flit's first beat and merges in only strobed lanes. Every
requirement in §2.3 is a consequence of that behaviour, and on such a bitstream
they are mandatory. The rest of this section states what a driver must do to
remain correct on one.

The window in 32-byte flits, in write-decode terms. "Exposure" describes a
pre-strobe bitstream; on a current one every row is safe:

| Flit | Registers | Exposure |
|---|---|---|
| `0x000` | `CTRL`, –, –, `IRQ_STAT` | Writing `IRQ_STAT` zeroes `CTRL`. Nothing reads `CTRL`. |
| `0x020` | `IRQ_EN`, –, –, – | Safe. |
| `0x040` | `PROG_DST`, `PROG_LEN`, **`PROG_KICK`**, – | **Any write here fires a dispatch.** |
| `0x060` | `PROG_CRED`, `PROG_BASE`, **`SIG_DONE`**, – | **Any write here zeroes the other two and clears `SIG_DONE`.** |
| `0x100` | `TX_FLIT[0..3]` | **Safe on every bitstream.** This is the one path that always honoured byte strobes. |
| `0x140` | `TX_KICK`, –, –, – | Safe: one decoded register in the flit. |
| `0x1C0` | `RX_POP`, –, –, – | Safe: one decoded register in the flit. |
| `0x800` | `AUX_CFG` +0x00/+0x08/+0x10/+0x18 | Mover `CTRL` shares a flit with its descriptor. §3. |
| `0x2000`+ | `STAGE` | Safe as a **contiguous block write** — every beat is then strobed. A lone `write64` zeroes its three neighbours. |

`TX_KICK` and `RX_POP` are safe for a structural reason rather than a strobe
one: each is the only **write-decoded** register in its flit, so no spurious kick
or pop is reachable however the beats land. That property does not depend on the
bitstream.

### 2.7.1 What a driver must still do

The rules below are what a driver owes a **pre-strobe** bitstream. On a current
one the ordering is no longer load-bearing, but nothing breaks by keeping it —
and a driver that drops it stops working on any board that has not been
reprogrammed. Retiring it is a deliberate act: name the bitstream that made it
safe, then delete the ordering and this section together.

**Staging is safe only as contiguous block writes, and only upwards.** Written
word by word, each write zeroes the three staging words sharing its flit; the
addresses ascend, so **only the last word of every four survives** — three
quarters of every instruction, silently. `Program.upload` refuses a transport
with no bulk write path for exactly this reason, and `Transport.bulk` defaults
to false, so a backend that forgets to set it gets the refusal rather than the
corruption.

Flit-aligned staging **cannot be guaranteed**: a slot is `5 * 8 = 40` bytes and
the window is 32, so a run of `n` slots closes a window only when `n % 4 == 0`,
and `STAGE` is write-only so there is no read-modify-write to pad with. The
trailing partial window zeroes up to three words *above* the run, which fall in
the next slot. That is harmless while slots ascend. **It is not harmless to
stage a lower slot range while a higher one is live** — which is precisely the
multi-program case `PROG_BASE` exists to serve. Stage upwards, or stage in
multiples of four slots.

**On a pre-strobe bitstream, `PROG_LEN` is what launches a dispatch and
`PROG_KICK` launches nothing.** `PROG_KICK` at `0x50` shares a flit with
`PROG_DST` and `PROG_LEN`, so any write in that flit *arrives* at the kick
decode; and `PROG_KICK`'s own write zeroes `PROG_DST`. What each write in the
mandated order `BASE, LEN, DST, CRED, KICK` does, and why each position is
forced:

| Write | Also does, on a pre-strobe bitstream | Why it is where it is |
|---|---|---|
| `PROG_BASE` | zeroes `PROG_CRED`, and clears `SIG_DONE` | the zeroed credit is what *holds* the dispatch that `PROG_LEN` fires |
| `PROG_LEN` | zeroes `PROG_DST`, and fires the dispatch | it must fire while the credit is still zero |
| `PROG_DST` | zeroes `PROG_LEN` | it repairs the destination before any flit moves; `PROG_LEN` is spent by now |
| `PROG_CRED` | zeroes `PROG_BASE` | it releases the held dispatcher, and the base has been consumed |
| `PROG_KICK` | zeroes `PROG_DST` | it is a no-op that must come last, since it destroys the destination |

Any other order and the dispatch leaves with `PROG_DST = 0` — node `{0,0}`, which
in the reply-addressing convention means "answer the sender" and which no
endpoint can occupy, so the flits are dropped. The symptom is "launches nothing,
silently": the flits go out and nothing retires.

**`SIG_DONE` is collateral, and worse than one lost baseline.** It sits at `0x70`,
in the same flit as `PROG_CRED` and `PROG_BASE`, so a kick clears it **twice**;
in a multi-kick round each kick destroys the completions the previous ones
counted. A host **MUST NOT** wait on an absolute `SIG_DONE` total on a
pre-strobe bitstream — it would poll for a number that keeps being reset, and
hang. Wait on `NODE_STATUS` at `0x1000+` instead: that mirror is written only by
arriving `CU_SIGNAL`s, never by a host write, and it is outside these flits
entirely. **That immunity is an accident of address, not a design choice.**

Note the dependency runs both ways. Code that *relies* on `PROG_BASE` clearing
the credit stops working on a current bitstream, where it does not.

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
| `0x10` | `[0]` which walker (0 source, 1 destination), `[4+:ADDR_W]` base address, `[46:44]` number of dimensions, and on the SOURCE walker `[50:47]` `XFORM_ID`, `[58:55]` `XFORM_MODE` — §3.2 |
| `0x18` | `[0]` walker, `[3:1]` dimension, `[19:4]` count, `[51:20]` signed stride |
| `0x20` | `[1:0]` axis, `[17:2]` signed axis step |
| `0x28` | `[0]` walker, `[1]` axis select, `[17:2]` signed axis base, `[33:18]` axis extent |
| `0x30` | `[ADDR_W-1:0]` index-buffer base address, `[55:40]` index count |
| `0x38` | `[63:0]` PRNG seed |
| `0x40` | `[31:0]` immediate, used as the fill value and as padding |
| `0x50` | `[31:0]` gather pitch, `[47:32]` gather words |

Modes: `0` copy, `1` transpose (**faults — not implemented**), `2` gather,
`3` generate, `4` fill, `5` transform. §3.2.

Fault codes: `0` none, `1` index length, `2` range, `3` AXI, `4` mode,
`5` element width, `6` alignment, `7` a bound axis in a transform move.

### 3.1 The RV32 control processor does not use this window

> **Applies to `CPU_RV64 = 0`, the default.** The RV64 complex reaches the mover
> through its own control region and has no `MVGO` descriptor path at all; §7.

**The mover is an executor of the control processor, not a peer.** To *issue a
move* the processor does not touch these offsets one at a time. It builds a
descriptor in its scratchpad —

```
word 0        : n, the number of register writes
then n times  : {24'b0, offset[7:0]}, value[31:0], value[63:32]
```

— and stores the pointer to `MVGO` (`0xF000_0000`). `mv_exec.v` fetches it and
drives the same `cfg` port this section describes, offset for offset. Program
order is the queue; there is no ring buffer and no doorbell.

So **`AUX_CFG` is host-facing only, and it disappears** rather than being
mirrored into the processor's address space: issuing a move register-by-register
is the transport cost the executor design exists to delete.

**That is about the window, not about registers.** The processor reaches the
mover's **status**, and each occupant's registers, through its own **node range**
by ordinary load and store — a range already carved out ahead of the L1
(`l1_req = l1_req_core && !is_node`), which is what such a window needs: uncached
and not reorderable against `MVGO`. The mover's *control* registers are not
there: the descriptor already is a stream of register writes, so a move costs one
store rather than one per field.

| address | | |
|---|---|---|
| `0xF000_0000` | W | `MVGO` — the descriptor pointer, and the go |
| `0xF000_0000` | R | `[0]` busy, `[7:4]` mover fault, `[11:8]` occupant fault |
| `0xF001_0000 \| (id << 8) \| reg` | RW | occupant `id`'s register `reg`, 4 bytes wide |

Bit 16 splits the range. Nothing here is special; it is what a load and a store
already are. The host keeps the `AUX_STAT` mirror and gets nothing new.

**A node read is answered in WB**, one cycle after the request, exactly as an L1
hit is — the value is registered rather than returned combinationally, because
the core samples `l1_rdata` with `l1_req` already low.

### 3.2 The converting move

**Mode 5, an ordinary descriptor.** The transform slot is on the mover's own
read-return path, so `mem/L2 → occupant → mem/L2` is one pass of one engine.
There is no second command set and no second engine.

The occupant is named on the **source walker's header**, register `0x10` with
`sel = 0`, in bits the header already left free:

| bits | field |
|---|---|
| `[50:47]` | `XFORM_ID` — `0` bypass, `1` slot 1, `n` slot n |
| `[58:55]` | `XFORM_MODE` — **opaque**, carried to the occupant and never interpreted |

KohakuTPU's occupant reads `mode[0]` as its A/B packing select.

The two walkers count different things, and this is the one place a transform
descriptor differs from a copy:

| walker | counts | typical stride |
|---|---|---|
| source (`sel = 0`) | **source words**, `IN_BITS / DATA_W` per entry | 32, or whatever the layout is |
| destination (`sel = 1`) | **entries** | `OUT_WORDS × 32` |

The source defines the iteration space. A strided source needs no gather pass:
the walker issues the entry's reads wherever they live and the in-order returns
stream into the occupant.

An entry is `IN_BITS` of source and `OUT_WORDS` of destination, both declared by
the occupant, because the mover sizes both walks before the transform has run.
KohakuTPU's declares 2048 and 4 — eight source beats in, four words out.
`OUT_WORDS` is at most 4, because the bank presents four word outputs.

**A bound axis faults (code 7).** A padded element issues no read and the
occupant is fed a fixed beat count off the read return, so a bound axis would
leave an entry a beat short forever.

Progress and faults are reported through `AUX_STAT` as for any other mode.

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

**The write offsets have two writers.** The host reaches them here, and the RV64
control processor reaches the same three registers through its own control
region at `0xC0` (§7.4). When both pulse in one cycle **the host wins**.

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

### 4.1 Where a write that crosses the link lands

Normative, because a driver or a runtime composing a cross-mesh descriptor has
to know it. A write whose mesh field names another mesh leaves over the link;
the receiving node places it by the **top bit of the address**:

| Inbound address | Lands in |
|---|---|
| **bit 39 set** — a special address, which is how every aperture including staging is named | that mesh's **staging**, at the full 40-bit address. `mag_stage_port` claims it off the converged path |
| anything else | that mesh's **DRAM**, by its **low 32 bits** |

DRAM is truncated because local DRAM starts at zero and the mesh field sits high
in the address, so all 40 bits would land the write far out of range. An
aperture address must survive whole for the opposite reason: the aperture *is*
named by the high bits, so a truncated one is no longer an aperture address and
lands in DRAM at the aperture's offset.

**Reads never cross the link.** It carries remote writes, compute-unit flits and
doorbells only; a read's source **MUST** be in the requester's own mesh.

**A write into staging honours byte strobes** — the bank memory is byte-enabled
and the staging path passes the AXI strobes through — so a store narrower than
the 32-byte word leaves the other lanes alone. Page tables, allocator bitmaps
and mailbox words in staging are safe to update in place.

## 5. The host control-program engine

Source of truth: `src/kohakuaccel/verif/main_orch.v`. A separate AXI slave — not a mesh
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

## 6. The RV64 host window

> **Present only when `sysnode`'s `CPU_RV64` is non-zero.** With the default RV32
> complex the window's ports exist on `sysnode` and are tied off: `hs_rdata`
> reads `64'd0` and `hs_console_we` is low.

Source of truth: `src/kohakuaccel/pe/rv64-sys/rv64_syscore.v`.

The RV64 control complex has **no NoC compute-unit shell**. It answers no
`CU_CTRL` block and it is not dispatched to. It *does* originate mesh traffic,
through a dispatch mailbox in its own control region (§7.5) rather than through
a shell. The host reaches it over a dedicated port on `sysnode` — not AXI, and
not part of the orchestrator's map in §2.

> **A flit addressed to its coordinate is accepted and discarded unless it is a
> `CU_SIGNAL`, and a reader has to know that.** The mailbox holds `rx_busy` low,
> which the hub reads as *not busy*, so an arriving flit is always taken: a
> `CU_SIGNAL` is queued and everything else is dropped. A `CU_CTRL` read to that
> coordinate therefore never replies, and the node reads as absent —
> indistinguishable from an empty coordinate.
>
> With the default RV32 complex the same coordinate *is* a conforming compute
> unit and does answer §1. **Which register surface exists at `(0, 0)` is decided
> by `CPU_RV64`**, and there is no runtime way to discover which.

### 6.1 The port

| Signal | Direction | Width | Meaning |
|---|---|---|---|
| `hs_addr` | in | 32 | Byte address within the window. |
| `hs_wr` | in | 1 | This cycle is a write. |
| `hs_wdata` | in | 64 | Write data. |
| `hs_wstrb` | in | 8 | Byte enables. **Honoured on the scratchpad and nowhere else** — see §6.2. |
| `hs_rd` | in | 1 | This cycle is a read. **Not read by the RTL**; the read path is unconditional. |
| `hs_rdata` | out | 64 | Registered. Valid **one cycle after** `hs_addr`. |
| `hs_ready` | out | 1 | Tied to `1'b1`. There is no backpressure and no stall. |

A write takes effect in the cycle `hs_wr` is high. A read is a pure function of
`hs_addr`, registered once; a requester **MUST** hold the address for one cycle
and sample on the next. Because the read path ignores `hs_rd`, reading has no
side effect and cannot be sequenced against a write by the port.

### 6.2 The three regions

`hs_addr[31:28]` selects the region. Every other bit of `hs_addr[31:8]` is
ignored.

| `hs_addr[31:28]` | Region | Access | Addressed by |
|---|---|---|---|
| `0x0` | Instruction memory | **write only** | `hs_addr[IAW+1:2]`, one 32-bit word per address, from `hs_wdata[31:0]` |
| `0x1` | Scratchpad | **write only** | `hs_addr[SAW+2:3]`, one 64-bit word per address, byte-enabled by `hs_wstrb` |
| `0x2` | Control | read and write | `hs_addr[7:0]`, §6.3 |
| other | — | none | writes are ignored |

`IAW` is `$clog2(IMEM_WORDS)` and `SAW` is `$clog2(SPAD_WORDS)`; both are set by
`sysnode`'s `PE_IMEM` and `PE_SPAD` ([parameters.md](parameters.md) §5.1).

Four consequences, all of them things a loader has to know:

- **The instruction memory takes the low 32 bits of `hs_wdata` and ignores
  `hs_wstrb`.** Two instruction words per 64-bit host write is not available;
  each write places one word.
- **Neither memory can be read back.** There is no verify path over this window.
- **The host and the core share the scratchpad's one write port, and the host
  wins.** A host write asserted in the same cycle as a core store replaces both
  the address and the data, so the core's store is lost silently. A host
  **MUST NOT** write the scratchpad while the core is running — that is, between
  `HR_BOOT = 1` and `HR_STATUS` reporting halted or exited.
- **The read decode ignores `hs_addr[31:28]`.** `hs_rdata` is selected from
  `hs_addr[7:0]` alone, so a read at offset `0x18` of *any* region returns
  `HR_STATUS`. A reader **MUST** use region `0x2` regardless; the aliasing is a
  property of the decode, not a second address for the same register.

### 6.3 The control region's registers

At `hs_addr[31:28] == 0x2`, offset `hs_addr[7:0]`. Every register is 64 bits.

| Offset | Name | Access | Contents |
|---|---|---|---|
| `0x00` | `HR_BOOT` | W | `[0]` — writing 1 requests a boot and enables the run; writing 0 stops the core. A boot request is a **one-cycle pulse**, and it also clears the latched halt state and the exit flag and zeroes the cycle and retire counters. |
| `0x08` | `HR_PC` | W | A 64-bit boot PC is **stored and never read.** The core always starts at its `RESET_PC`, which `rv64_syscore` fixes at 0. |
| `0x10` | `HR_DBELL` | W | `[0]` — the software-interrupt doorbell. Drives the core's `irq_soft` directly and stays at the written level; it is not a pulse and the core does not clear it. Software clears it by reading it back through the control region and having the host write 0. |
| `0x18` | `HR_STATUS` | R | `[3]` exited, `[2]` halted, `[1:0]` halt cause. `[63:4]` zero. |
| `0x20` | `HR_EXIT` | R | The 64-bit word the program last stored to the control region's `R_EXIT`. §7. |
| `0x28` | `HR_HALTPC` | R | The PC latched when the core halted. |
| `0x30` | `HR_CYCLES` | R | Free-running cycle count while the core is out of reset. **64 bits**, unlike the compute-unit shell's 32. |
| `0x38` | `HR_RETIRED` | R | Instructions retired. 64 bits. |
| other | — | R | `64'd0`. |

**`halted` and `cause` are latched, and the latch is what makes them readable.**
The core's own `halted` output is cleared as soon as its reset is re-asserted,
which the complex does the moment it halts, so an unlatched status register would
report nothing forever.

**A boot clears the latch, and clears `exited`.** So the sequence a host runs is:
write the memories, write `HR_BOOT = 1`, poll `HR_STATUS`, and read `HR_EXIT`
once `[3]` or `[2]` is set.

**Undefined:** the halt cause encoding is not specified here. It is two bits
produced by `rv64_core`; see
[arch/cpu/rv64-sys/architecture.md](../arch/cpu/rv64-sys/architecture.md).

## 7. The RV64 control region

> **Present only when `sysnode`'s `CPU_RV64` is non-zero.**

This is the processor's *own* view — a range in its physical address space that
software reaches by ordinary load and store. It is not the host window, and the
two do not share offsets.

**A store into this range is a command, never a line.** The range is decoded
ahead of the L1 and is uncached by construction, so a control write is never
buffered and never reordered against a later one.

### 7.1 Where it is

A **256-byte** range at `CTRL_BASE`, default `0x0000_0000_0002_0000`. The decode
is `pa[ADDR_W-1:8] == CTRL_BASE[ADDR_W-1:8]` — a bit test, not a magnitude
compare, so `CTRL_BASE` **MUST** be 256-byte aligned. The offset is `pa[7:0]`.

Reads are answered from the early address and writes from the registered one,
which means a read is answered in the cycle after the access starts, exactly as
an L1 hit is.

### 7.2 The map

| Offset | Name | Access | Contents |
|---|---|---|---|
| `0x00` | `R_EXIT` | W | Any store sets the `exited` flag and latches the stored word, which the host reads at `HR_EXIT`. This is how a program terminates: it is a store, not an instruction. |
| `0x08` | `R_CONSOLE` | W | `[7:0]` is emitted on the complex's console byte port for one cycle. There is no buffering and no flow control; a byte written while the consumer is not looking is lost. |
| `0x10` | `R_DBELL` | **R** | `{63'd0, dbell}` — the doorbell the host set at `HR_DBELL`. **Read-only from the processor**: a store here is decoded by no case and does nothing. Software cannot acknowledge its own doorbell. |
| `0x18` | `R_SATP` | **R** | A **read-only mirror** of the `satp` CSR. `satp` is architectural state owned by supervisor software and written with `CSRRW`; this offset exists so a host can read the translation root without a path into the register file. A store here is decoded by no case and does nothing. |
| `0x20` | mover status | R | `[32]` mover busy, `[31:28]` mover fault, `[27:0]` moves completed. `[63:33]` zero. |
| `0x28` | doorbell status | R | The 64-bit word on the complex's `db_status` input: `mag_ilink`'s four inbound doorbell counts, mesh 0 in `[15:0]` up to mesh 3 in `[63:48]`, or zero when no interlink is built. |
| `0x40`–`0x7F` | dispatch mailbox | RW | A store writes mailbox register `pa[5:3]`; a load reads it. §7.5. |
| `0x80`–`0xBF` | mover config | W | A store writes mover register `pa[5:0]` with the stored 64-bit value. §7.3. |
| `0xC0`–`0xFF` | interlink config | W | A store drives the complex's `db_*` port with address `{2'b10, pa[5:0]}` and the stored value, so it writes interlink client register `0x80 + pa[5:0]`. §7.4. |
| everything else | — | R | `64'd0`. Writes are ignored. |

Note the two status words differ from the RV32 complex's single `node_word`
([§3.1](#31-the-rv32-control-processor-does-not-use-this-window)): the fields are
in different places and the occupant fault is absent. A driver **MUST NOT** share
a decoder between them.

### 7.3 The mover window reaches only half the mover

A store at `0x80 + k` writes mover register `k`, for `k` in `0x00`–`0x3F`. The
register index is `pa[5:0]` zero-extended.

**`0x40` and above of the mover's map are therefore unreachable from the
processor.** That is the mover's immediate register (`0x40`, the fill value and
the padding value) and its gather pitch and word count (`0x50`) —
[§3](#3-the-memory-movers-command-registers). A program running on the RV64
complex can command `COPY`, `GENERATE` and `XFORM` moves, and cannot fully
configure `FILL` or `GATHER`. The host's `AUX_CFG` window (§3) still reaches all
of them, and the processor's writes win over the host's when both pulse in one
cycle.

**There is no `MVGO` descriptor path.** The RV32 complex issues a move by storing
a pointer to a register-write list; the RV64 complex has no such register, so a
move costs one store per field of the descriptor rather than one store total.

### 7.4 The interlink window

A store at `0xC0 + k` drives `db_en`, `db_data` and `db_addr = {2'b10, k}`, so
it writes the interlink's client register `0x80 + k` — the same map the host
reaches through `AUX_CFG` ([§4](#4-the-interlink-registers)). `0x28` reads
`db_status`.

`sysnode` connects all four in the `CPU_RV64` branch. `db_en` / `db_addr` /
`db_data` reach `mag` as a **second writer** on the interlink's config port,
where **the host wins a same-cycle collision** — the host path is a debug path
and the processor can retry.

The three registers that exist, at their control-region offsets:

| Offset | Interlink register | Fields |
|---|---|---|
| `0xC0` | `0x80` control | `[0]` enable — **reset to 1**; `[1]` clear the inbound doorbell counts; `[2]` clear the sticky fault register |
| `0xC8` | `0x88` mesh id | `[1:0]`, reset to the node's `MESH_ID` parameter |
| `0xD0` | `0x90` ring | `[1:0]` destination mesh, `[15:8]` transaction tag. **The write itself rings the doorbell** |

Offsets `0xD8`–`0xFF` decode to interlink registers that do not exist and are
ignored.

**Receiving a doorbell.** Each inbound ring increments the 16-bit count for its
source mesh, read at `0x28`, and **raises the core's external interrupt as a
level** while any count is non-zero. A ring arriving while another is being
serviced is therefore not lost. A handler **MUST** clear the counts (`0xC0` bit
1) to drop the line; a clear racing an arriving doorbell loses to the doorbell,
so a count may survive a clear rather than a ring being lost.

> **The doorbell is not ordered against data by hardware.** The interlink's
> outbound arbiter selects between a remote write, a compute-unit flit and a
> doorbell by **rotating priority**, so a ring requested while a remote write is
> still queued may leave first. A producer **MUST** establish the ordering
> itself: issue the writes, poll the mover's status at `0x20` until it is no
> longer busy, and only then ring. Do not treat a doorbell as a release fence.

The mover window (§7.3) reaches the mover's registers `0x00`–`0x3F` for the
matching reason: the mover's own map starts at `0x00`, so it needs no offset.

### 7.5 The dispatch mailbox

Source of truth: `src/kohakuaccel/pe/rv64-sys/rv64_noc_mbox.v`.

This is how the RV64 complex reaches the mesh. Dropping the compute-unit shell
dropped the complex's only path onto the fabric with it, and the mailbox is the
replacement: **software writes a dispatch, not a flit.** A flit is 288 bits
against a 64-bit store port, so composing one in software would be five stores
with a tearing window in the middle. Instead a program names a destination and
two payload words, and hardware assembles the `CU_INST`.

Registers at `0x40 + index * 8`, the index being `pa[5:3]`:

| Offset | Index | Name | Access | Contents |
|---|---|---|---|---|
| `0x40` | 0 | `M_DST` | RW | `[POS_WIDTH-1:0]` destination x, `[8+:POS_WIDTH]` destination y. Reads back in the same packing |
| `0x48` | 1 | `M_ARG0` | RW | Payload word 0 — the low 64 bits of the flit's payload |
| `0x50` | 2 | `M_ARG1` | RW | Payload word 1 — the next 64 bits |
| `0x58` | 3 | `M_GO` | W | Any store builds the flit and offers it to the hub. **Ignored while a previous flit is still offered** |
| `0x60` | 4 | `M_STAT` | RO | `[7:0]` completions queued, `[15]` a dispatch is offered and not yet taken, `[31]` sticky queue overflow. All other bits zero |
| `0x68` | 5 | `M_HEAD` | RO | The oldest queued completion, or `64'd0` when the queue is empty |
| `0x70` | 6 | `M_POP` | W | Any store discards the head. A store when the queue is empty does nothing |
| `0x78` | 7 | — | RO | `64'd0` |

The flit `M_GO` builds is a `CU_INST` (type `0x5`): destination from `M_DST`,
source from the complex's own `(my_x, my_y)`, `last` set, an 8-bit `txn` the
mailbox increments per dispatch, and `{M_ARG1, M_ARG0}` zero-extended as the
payload. Nothing else in the flit is reachable from software.

A queued completion is one 64-bit word:

| Bits | Field | Width | Meaning |
|---|---|---|---|
| `[63:56]` | zero | 8 | — |
| `[55:52]` | `src_y` | `POS_WIDTH` | The signalling node's y |
| `[51:48]` | `src_x` | `POS_WIDTH` | Its x |
| `[47:40]` | `code` | 8 | The `CU_SIGNAL` code |
| `[39:8]` | `arg` | 32 | Its argument |
| `[7:0]` | zero | 8 | — |

The field positions above are shown at the reference build's `POS_WIDTH = 4`;
the packing is `{8'd0, src_y, src_x, code, arg}` left-justified in the word.

Four rules bind a dispatcher.

- **`M_GO` is ignored while `M_STAT[15]` is set.** An offered flit is held until
  the hub takes it, because withdrawing one destroys it and the loss is silent
  downstream. A second `M_GO` inside that window does nothing and reports
  nothing. A dispatcher **MUST** check `M_STAT[15]` before every `M_GO` after the
  first.
- **A completion the queue cannot hold is accepted and dropped.** The queue is
  `CQ_DEPTH` deep, 16 at the reference build. The mailbox never raises busy on
  the hub — held, an unwanted completion would sit at the head of the hub's queue
  and stall the link for everything behind it, including the traffic that would
  drain the queue. `M_STAT[31]` is **sticky** and is the only witness, because a
  dropped completion and a unit that never finished are otherwise identical from
  software.
- **Popping is a write.** Reading `M_HEAD` has no side effect. The control region
  answers a read from a register one cycle later, so a read-triggered pop would
  have to guess which cycle the read happened on.
- **Credit is the program's.** There is no credit register here and no credit
  counter. The rule of [§2.4](#24-credit) still holds — a sender **MUST NOT**
  dispatch more instructions to a node than that node's instruction FIFO can
  hold — and in this configuration nothing in hardware enforces it. The depth is
  `inst_depth` from that node's `CU_CAPS` (§1.3).

A non-empty queue raises the core's external interrupt, alongside the node's
`irq_summary`. Waiting for a completion is exactly the condition a scheduler
must not have to poll for.

## 8. Known divergences

| Divergence | Detail |
|---|---|
| `CU_CTRL` map versus the snapshot | An earlier pre-reframing snapshot lists byte offsets `0x00/0x04/0x08/0x0C` and registers `CU_CONTROL` (RW) and `CU_ERROR`. The RTL uses word **indices** 0–3, has no writable register at all, and indices 2 and 3 are counters. §1.2 is the silicon. |
| `CU_STATUS.error` | Allocated, tied to zero. A unit's faults are reported through `SIG_FAULT`, not here. |
| `CTRL`, `IRQ_EN`, `IRQ_STAT` | Storage with no consumer. No interrupt output exists on the orchestrator. |
| Staging window versus 4 KB | The decode is derived from `STAGE_WORDS` and at the default `STAGE_FLITS = 128` extends past `0x2FFF`. Correct in RTL; a hazard for a host that assumes one page. §2.6. |
| `CU_VERSION` default | The parameter defaults to `8'h01`; every instantiation in the tree overrides it to the current build number. A unit that forgets to override it reports a version it does not have. |
| Orchestrator location | `noc_orchestrator.v` lives under `src/kohakuaccel/noc/` but is the memory agent's control plane and is instantiated only by `mag.v`. |
| `HR_PC` has no consumer | The RV64 host window accepts a 64-bit boot PC at `0x08` and stores it. `rv64_core` takes its start address from the `RESET_PC` parameter, which `rv64_syscore` fixes at 0, and has no PC input. The register is a reservation. §6.3. |
| RV64 host-window read decode | `hs_rdata` is selected from `hs_addr[7:0]` with no test of `hs_addr[31:28]`, so every region aliases the control region for reads. §6.2. |
| RV64 mover window is half-width | The control region carries mover register index `pa[5:0]`, so offsets `0x40` and above of the mover's map cannot be written by the processor. §7.3. |
| Doorbells are not ordered against data | The interlink's outbound arbiter picks between a remote write, a flit and a doorbell by rotating priority, so a ring can leave ahead of a queued write. A producer must order it in software — writes, then the mover idle, then the ring. §7.4. |
| RV64 coordinate is not enumerable | The complex is a live hub client at `(0, 0)` and dispatches, but it wears no compute-unit shell, so it answers no `CU_CTRL` read. A controller walking the mesh sees the coordinate as empty, and there is no runtime way to tell which configuration a bitstream carries. §6. |
| RV64 dispatch has no credit mechanism | The orchestrator's dispatch path holds a credit counter and stalls locally at zero (§2.4). The mailbox has neither, so on this path the rule against over-dispatching a node's instruction FIFO is enforced only by the program. §7.5. |
| RV64 ordering guarantee unpublished | A compute unit's completion means every write it made is visible; that is a dispatcher's only sequencing point. The RV64 complex has no shell and has not published an equivalent guarantee for traffic it originates. §7.5. |
| RV64 status words differ from RV32's | The RV32 complex reports `{busy, mover fault, occupant fault}` in one 32-bit `node_word`; the RV64 control region reports mover busy, mover fault and moves-completed at `0x20` in different positions and carries no occupant fault, because its transform register port is tied off. §7.2, [transform-slot.md](transform-slot.md). |
