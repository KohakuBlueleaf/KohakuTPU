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

**That order is NOT a hardware requirement. It is a workaround for the striding
defect in §2.7**, and on a bitstream carrying that defect it is mandatory. Read
§2.7 before changing anything in this sequence: on such a bitstream `PROG_LEN` is
what actually launches the dispatch, `PROG_KICK` launches nothing, and each
position of the order is forced for a different reason.

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

**Staging MUST be written as contiguous blocks, ascending. §2.7.** Word by word
it loses three quarters of every flit on current silicon, with no symptom.

### 2.7 The 32-byte write window — a defect, and the workaround built on it

**On a bitstream built before 2026-08-23, one 64-bit host write to this window
writes FOUR registers.** Everything §2.3 calls a hardware requirement is a
workaround for this, discovered empirically and mis-attributed.

**How.** `noc_orchestrator.v:366` decodes `wsel` from `waddr`, the same register
`:448` advances by 8 on every write beat; the aux path at `:424` did the same and
neither read `s_axi_wstrb`. Every station-bus manager flit-aligns
(`sb_nmu.v:134` `PACK=1` whenever `MW <= FW`, `:355` `hdr_size = FSZ`, `:381`
address rounded **down** to the 32-byte flit), the mesh control port is 32-bit
(`gen_station_wrap.py:664`), and `scripts/tcl/v6/40_bus.tcl:144` upsizes 32→64 in
front of it. So a `write64` arrives as four 64-bit beats covering the whole flit,
and the three the host did not write carry **zeros** — `sb_nmu.v:578` clears the
packer at each flit's first beat and merges in only strobed lanes. That is why
the symptom is a neighbour *zeroed* rather than corrupted.

The window in 32-byte flits, in write-decode terms:

| Flit | Registers | Exposure |
|---|---|---|
| `0x000` | `CTRL`, –, –, `IRQ_STAT` | Writing `IRQ_STAT` zeroes `CTRL`. Nothing reads `CTRL`. |
| `0x020` | `IRQ_EN`, –, –, – | Safe. |
| `0x040` | `PROG_DST`, `PROG_LEN`, **`PROG_KICK`**, – | **Any write here fires a dispatch.** |
| `0x060` | `PROG_CRED`, `PROG_BASE`, **`SIG_DONE`**, – | **Any write here zeroes the other two and clears `SIG_DONE`.** |
| `0x100` | `TX_FLIT[0..3]` | **Safe even before the fix** — `:416` is the one path that honoured byte strobes. |
| `0x140` | `TX_KICK`, –, –, – | Safe: one decoded register in the flit. |
| `0x1C0` | `RX_POP`, –, –, – | Safe: one decoded register in the flit. |
| `0x800` | `AUX_CFG` +0x00/+0x08/+0x10/+0x18 | Mover `CTRL` shares a flit with its descriptor. §3. |
| `0x2000`+ | `STAGE` | Safe as a **contiguous block write** — every beat is then strobed. A lone `write64` zeroes its three neighbours. |

**The fix is the pattern this file already contained.** `TX_FLIT` was never
exposed because `noc_orchestrator.v:416-420` loops over `s_axi_wstrb` per byte —
one path in the whole FSM got it right and the rest wrote `s_axi_wdata`
wholesale. The change generalises that loop; it invents nothing.
`TX_KICK` and `RX_POP` are safe for a different and structural reason: each is
the only **write-decoded** register in its flit, so no spurious kick or pop is
reachable however the beats land.

### 2.7.1 Fixed in RTL 2026-08-24 — but §2.3's order stays, because no bitstream carries it

The 2026-08-23 change was reverted the same day by a blanket RTL revert, which is
why the defect above outlived its own fix. It was re-applied on 2026-08-24 in
three parts: a beat with no strobes does nothing at all (no register write and no
*arrival*, which is what `PROG_KICK` and `SIG_DONE` react to); register writes
byte-merge, with the 16-bit `PROG_*` ones gated on `|s_axi_wstrb[1:0]`; and
`AUX_CFG` accumulates at the 8-aligned offset into a one-entry shadow, because
the client has no byte enables.

**Measured, `tests/sysnode/mover_cfg32_tb.v`:**

| shape | before | after |
|---|---|---|
| one 64-bit beat, strobes `FF` | 7 pulses, exact | 7 pulses, exact |
| two halves, `0F` then `F0` | 64 of 64 words wrong | **0 wrong** |
| 2-beat burst | 63 wrong | 63 — correct: it addresses two registers by construction, and no strobe rule can make it mean one |
| **flit-aligned, the ship's shape** | **28 pulses, 64 of 64 wrong** | **7 pulses, 0 wrong** |

`PASS mover_cfg32: 503 checks`, and again at the ship's clocking.

**In simulation the kick order stops mattering.** Phases `F`, `I` and `J` of that
bench show zero spurious kicks and both the documented *and* the natural `PROG_*`
order reaching node `0x21`. The five writes still work — now because `PROG_KICK`
genuinely kicks, which it never did before.

**On silicon it still matters, and that is the operative fact.** The fix is in
RTL and verified in simulation only; v7 and v7.1 are the bitstreams in hand and
neither carries it. `Program.kick` keeps the order deliberately. See the
retirement condition at the end of this section — it has not been met.

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

**The trace.** `BASE, LEN, DST, CRED, KICK`, measured in `tests/sysnode/mover_cfg32_tb.v`
phase E:

| after | run | len | dst | cred | flits out |
|---|---|---|---|---|---|
| `BASE` | 0 | 4 | 21 | **0** | 0 |
| `LEN` | **1** | 4 | **00** | 0 | 0 |
| `DST` | 1 | **0** | 21 | 0 | 0 |
| `CRED` | 0 | 0 | 21 | 12 | **4** |
| `KICK` | 0 | 0 | 00 | 12 | 4 |

**`PROG_LEN` is the kick** — `0x50` is in its flit. **`PROG_KICK` has never
launched anything on this silicon.** Each position is forced: `BASE`'s
credit-zeroing is what *holds* the dispatch `LEN` fires; `DST` repairs the
destination `LEN` destroyed before any flit moves; `CRED` releases the held
dispatcher. Any other order and the dispatch leaves with `PROG_DST = 0` — node
`{0,0}`, "answer the sender", dropped. Measured: the documented order puts the
first flit at `0x21`, the natural order at `0x00`. **That** is why the recorded
symptom is "launches nothing, silently": the flits go out and nothing retires.

**The attribution in §2.3 is wrong three ways.** `PROG_BASE` zeroes the **credit
only**; **`PROG_LEN`** zeroes `PROG_DST`; **`PROG_DST`** zeroes `PROG_LEN`. And a
fourth effect was never recorded: **`PROG_BASE` also clears `SIG_DONE`**, so a
completion baseline read before kicking is destroyed. `Program.await_node_at`
survives only because it polls `NODE_STATUS`, which is per node and not in this
window.

**The ordering may be retired ONLY once a post-fix bitstream ships, and not
before.** `Program.kick` keeps it deliberately: v7 is on the card and v7.1 is
being implemented without the fix, so dropping it would break every board that
can be run today. Note what breaks in the other direction too — code that
*relies* on `PROG_BASE` clearing the credit stops working on a post-fix
bitstream, so the dependency runs both ways and the switch is a deliberate act,
not a cleanup. When a post-fix bitstream ships, retire it on purpose: delete the
ordering, delete this paragraph, and say which bitstream made it safe.

**`SIG_DONE` is collateral, and it is worse than one lost baseline.** Every
`PROG_CRED` and every `PROG_BASE` write clears it (0x70 shares their flit), so a
kick clears it **twice**, and in a multi-kick round each kick destroys the
completions the previous ones already counted. Any wait on an absolute
`SIG_DONE` total — `Program.await_all` — would therefore poll for a number that
keeps being reset, and hang. **It is latent, not live: `await_all` and
`clear_done` have no callers.** Every path that actually waits uses
`await_node_at` / `await_signal`, which poll `NODE_STATUS` at `0x1000+` — a
per-node mirror written only by arriving `CU_SIGNAL`s, never by a host write, and
outside these flits entirely. That immunity is an accident of address, not a
design choice, so **do not start using `await_all` on a pre-fix bitstream.**

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

### 3.1 The control processor does not use this window

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

## 6. Known divergences

| Divergence | Detail |
|---|---|
| `CU_CTRL` map versus the snapshot | An earlier pre-reframing snapshot lists byte offsets `0x00/0x04/0x08/0x0C` and registers `CU_CONTROL` (RW) and `CU_ERROR`. The RTL uses word **indices** 0–3, has no writable register at all, and indices 2 and 3 are counters. §1.2 is the silicon. |
| `CU_STATUS.error` | Allocated, tied to zero. A unit's faults are reported through `SIG_FAULT`, not here. |
| `CTRL`, `IRQ_EN`, `IRQ_STAT` | Storage with no consumer. No interrupt output exists on the orchestrator. |
| Staging window versus 4 KB | The decode is derived from `STAGE_WORDS` and at the default `STAGE_FLITS = 128` extends past `0x2FFF`. Correct in RTL; a hazard for a host that assumes one page. §2.6. |
| `CU_VERSION` default | The parameter defaults to `8'h01`; every instantiation in the tree overrides it to the current build number. A unit that forgets to override it reports a version it does not have. |
| Orchestrator location | `noc_orchestrator.v` lives under `src/kohakuaccel/noc/` but is the memory agent's control plane and is instantiated only by `mag.v`. |
