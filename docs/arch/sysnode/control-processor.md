---
title: The control processor
summary: A RISC-V core inside the system node that dispatches the mesh, owns the memory mover as an execution unit, and reaches the mover's status and the transform slot's occupant registers through its own node range.
tags:
  - architecture
  - sysnode
  - cpu
---

# The control processor

A RISC-V core inside the system node that dispatches the mesh, owns the memory
mover as an execution unit, and carries cross-mesh control. It replaces
host-driven control register writes as the way work is started.

One per system node, so one per mesh. Mesh id is two bits (`NOC_MEM_MESH` is
`253:252`, and `mag_ilink`'s `dbell_n` has four entries), so **four on the
current device**.

This document is the design. Every claim about existing behaviour names the file
it came from.

---

## 1. Why

### It is what makes the mesh an SoC rather than an accelerator

The same silicon looks different from two directions. **From a CPU's side an SoC
is good because everything it needs is on the chip.** **From an accelerator's
side an SoC is good because there is a CPU next to it** — something that can run
a loop, take a branch, hold state between kicks and decide what happens next.

A mesh with a host on the far end of PCIe has neither. This core supplies the
missing half inside the mesh, and its two jobs are the two things a host is bad
at from that distance: **task dispatch** and **memory management**.

**Dispatch buys shapes a register poke cannot express.** *Graph execution* is
nodes with dependencies, where every edge is a poll and a decision. A *recorded
command program* is Vulkan's model: command buffers recorded once and submitted
many times, pipelines compiled ahead of dispatch, and synchronisation split into
fences, semaphores and barriers. Both are programs, and they belong on something
that runs programs. Neither says anything about what the compute units compute,
and this core does not know.

**Memory management is what makes the mover worth having**, which is why the
mover became an execution unit of this core rather than a peer with a doorbell —
§4. And several system nodes are far easier to orchestrate when each has a
processor: a cross-mesh edge becomes a program on each side plus a doorbell,
instead of one host serialising every edge across four meshes.

The rest of this section is the narrower version of the same argument, in the
terms the existing hardware states it.

### The front door, not the engine

The memory mover is commanded through `AUX_CFG`, a slice of MAG's control AXI
window. `mm_mover.v` has a `cfg_*` port and no instruction port; `mag.v:310`
shows `aux_cfg_en/addr/data` originating in `noc_orchestrator`, which is driven
from the control AXI slave `sc_*` (`mag.v:298-309`) and from nothing else.

Two independent problems follow, and only the first is about speed.

**Transport.** Every control write is a separate host transaction. Measured over
JTAG, one descriptor is five writes and costs about 22 ms wall, against a move of
a megabyte that completes inside the polling floor. Break-even is around 7 MB;
every real relayout is kilobytes. Dispatch is the bottleneck by orders of
magnitude.

**Structure.** A register file has no queue. There is nowhere to put descriptor
2 while descriptor 1 runs, because the descriptor *is* the register state. Even
with an infinitely fast front door the mover can only ever have one descriptor in
flight. This argument survives any transport improvement, and it is the stronger
of the two.

### What a compute unit cannot do

There is no address a compute unit can write that reaches the mover. The only
aperture that exists is L2 staging (`mag_stage_port.v`, aperture 0), and it does
not land on `cfg_en`. (Apertures 4–5 used to be a quantised upload path; that
path is retired — the transform slot belongs to the mover now, and a host
uploads raw bytes.)

The transport is not the missing part. `mag.v:393` already routes every
non-memory, non-remote flit arriving at a memory port into the agent, so
CU-to-MAG delivery works today. What is missing is that the agent drives
`aux_cfg` from its AXI write path (`noc_orchestrator.v:424-427`) and not from an
inbound flit.

---

## 2. Placement

The processor is **part of the system node**, beside MAG and behind the same
hub. It is not optional and there is no parameter that removes it: `sysnode.v`
instantiates it unconditionally, and MAG has no fabric port of its own either.
The two are a division of **design**, not of component — MAG is memory access
and cross-mesh communication, the processor is dispatch, small compute and the
memory mover, and neither ships alone.

Three reasons the processor belongs here rather than at a mesh node:

1. **The mover is its own.** The mover is the processor's SIMD memory unit, so
   `mv.go` is a store into a unit it contains: no flit, no NoC hop, no
   arbitration, no credit.
2. **Remote flits are encapsulated at the node.** The hub picks a flit for the
   interlink off its own port, so cross-mesh traffic never leaves the node twice.
   The processor's own remote flit still takes one router hop out and back —
   the encapsulator is fed from the INBOUND demux, and there is no short-circuit
   from the processor's outbound side to it. `ctrlpe_mesh2` proves the path
   works; it does not prove it is short.
3. **The doorbell is a node register.** `mag_ilink.v:706-710` raises `door_req`
   only from config offset `0x90`. Inside the node that is a wire, so software
   can ring cross-mesh doorbells, which otherwise only the host can do.

Inside the node; addressable from outside as a compute unit at `(0,0)` (§5.1).
`rv_pe` as it exists is a NoC-attached compute unit — the node-resident variant
is a different integration of the same core, and §3 is that difference.

---

## 3. Memory architecture

### 3.1 A new internal requester channel

`mag.v` converges `MP1` internal requesters onto one path. `g_req`
(`mag.v:852-902`) flattens each AXI-shaped requester into
`q_valid/q_ready/q_addr/q_len/q_write` plus `w_*`, `r_*`, `b_*`, and that feeds
`mag_stage_port` (L2) and then `mag_dram_port`.

The processor becomes requester channel `CP`. `MP1` goes from
`MEM_PORTS + 2 (+1 with ILINK)` to `MEM_PORTS + 3 (+1)`.

That is the whole integration, and it is the same thing the mover and the host
upload already do.

Two facts make it small:

- **Widths already match.** `rv_l1`'s line is 32 bytes and MAG's internal word is
  32 bytes: `fill_addr[30:0]` / `resp_data[255:0]` / `wb_addr[30:0]` /
  `wb_data[255:0]` map one-to-one onto `SW = DATA_W = 256` with no conversion.
  The replacement for `rv_noc_req` is a handshake adapter, not a protocol engine.
- **L2 staging is reached by address, not by a new mechanism.**
  `mag_stage_port.v:87` claims a request when

      a[39] && !a[38] && a[37:36] == MESH_ID && a[35:32] == AP_STAGE

  and it sits on the converged path. A processor request carrying those bits is
  served from URAM without a DRAM trip. **The dispatcher gets 2 MB of L2-latency
  scratch with no new RTL**, in the same address space as everything else.

Outstanding depth: `mag_stage_port` tracks `dwr[]` / `drd[]` per requester in
4-bit counters, so the path supports several outstanding transactions per
channel. `rv_l1` is a blocking-miss cache and stays one at a time for now; the
headroom is noted, not spent.

**Staging contention.** `mag_stage_port` serves one claimed burst at a time
across all requesters, round-robin on a single `id`. Processor staging traffic
therefore interleaves with mover staging traffic at burst granularity. Fine for a
dispatcher; a hot processor loop should not keep its working set in staging while
the mover is driving staging hard.

### 3.2 Address formation: four segment registers

The processor is RV32. MAG addresses are 40 bits with structure in the top eight:
`[39]` special, `[38]` reserved, `[37:36]` mesh, `[35:32]` aperture.

**The processor never has to address bulk data.** The mover takes full 40-bit
addresses straight out of its descriptor — the descriptor is data the processor
*writes*, not an address it *dereferences*. So the only things reached by
processor loads and stores are graph state, descriptor pools and metadata. 32
bits is not a real constraint; it simply cannot reach the top eight structural
bits.

**Four segment registers, selected by `addr[31:30]`**, supplying `[39:32]`:

    phys = { seg[addr[31:30]], addr[31:0] }

| `addr[31:30]` | typical contents |
|---|---|
| 0 | this mesh's DRAM |
| 1 | this mesh's L2 staging (special, mesh = self, aperture 0) |
| 2 | spare — upload aperture, or a second DRAM window |
| 3 | spare — the imem window if §6.4's direct store is built |

Four, not one. With a single register a DRAM-to-staging copy has to swap segments
per access; with four it is an ordinary loop. Cost is a 4:1 mux on 8 bits, around
50-80 LUT.

This generalises what `DRAM_BASE` already does — `rv_noc_req.v:149-150` ORs a
build-time constant onto a 31-bit software address, because the low bits are zero
by construction. The segment file is the same trick, made writable and plural.

### 3.3 No MMU

No TLB, no page tables, no walk logic, no fault handling.

A minimal RV32 MMU costs more LUT than the core itself, and it buys protection
and relocation for a machine that runs one program at a time, has no operating
system and no multi-tenancy, and whose addresses are emitted by a compiler.
Relocation happens at load time.

The problem the segment file solves is *reach*, not *protection*. They are not
the same problem and only one of them exists here.

### 3.4 Rule: processor load/store is mesh-local

`mag_ilink`'s address splitter sits **only** on the mover's write channel
(`mag.v:953`) and is a single-burst FSM. A foreign-mesh address on the
processor's channel would pass through `mag_stage_port` — its comment at line 7
says a foreign address passes through — and land in **local DRAM at a wrong
offset, silently**.

So:

> A processor `lw`/`sw` reaches this mesh's DRAM and this mesh's L2 staging.
> Anything crossing a mesh boundary goes through the mover (for data) or a flit
> (for control).

This costs nothing to enforce, matches the existing behaviour that remote reads
fault (`bad_remote_req` raises `F_RD_REMOTE`), and the mover's channel already
has the splitter for exactly this traffic.

### 3.5 IMEM, DMEM, L1

Unchanged from `rv_pe`: `rv_imem` at 2048 words, `rv_spad` at 2048 words
dual-ported so flits write port A while the core uses port B, `rv_l1` at 128
lines in one RAMB36.

There is no separate "mover instruction memory". The processor's DMEM is the
descriptor store, which is the same mechanism (§4.2).

---

## 4. The mover as an execution unit

### 4.1 Control merges, datapath does not

The mover keeps channel `MV` and its own AXI master. That is the point of the
merge: the **control** collapses into the processor pipeline, the **datapath**
stays a separate requester so a 32 B/cycle walk never contends with L1 fills in
the same channel.

This is not "the processor moves the data". A processor issuing per-word loads
and stores is bounded by outstanding transactions, not by instruction rate:
`rv_noc_req` allows one outstanding read and `WR_MAX = 1`, and its header
(`rv_noc_req.v:8-15`) calls that a contract rather than a simplification, because
MAG's agent matches write slots by source coordinate alone. So a software mover
moves 32 bytes per round trip where the walker moves 32 bytes per cycle. Even at
an implausibly good 10-cycle round trip that is an order of magnitude, and SIMD
memory instructions do not change it because they funnel through the same
one-tag, one-slot requestor.

**Keep the walker. Put the processor in front of it.**

### 4.2 ISA: one instruction, descriptor in DMEM

A descriptor is up to 26 64-bit values — six dimensions of count and stride for
each of two walkers, plus control and flags. Encoding that as 26 register-write
instructions is the wrong trade.

    mv.go   rs1     ; rs1 points at a descriptor in the scratchpad
    mv.wait         ; stall until the walk retires

`mv.go` hands the unit a pointer; the unit fetches its own descriptor from the
scratchpad. Consequences:

- one instruction per move
- the compiler emits descriptors as plain data, built with ordinary stores
- **the descriptor queue is the program.** Program order sequences moves; there
  is no ring buffer and no doorbell between processor and mover.
- `mv.wait` is a scoreboard hazard, reusing the mechanism `l1_stall` already
  provides. No status polling anywhere.

### 4.3 The mover becomes private

Nothing outside the node talks to the mover, and it is now literally inside the
processor: `rv_mag_pe.v` instantiates `mm_mover` and the transform slot, and
what leaves is the AXI master, channel `MV`. No compute unit needs to reach it,
because the processor owns orchestration and compute units are orchestrated. The
host does not, because it talks to the processor.

**One carve-out.** The mover's fault code must remain visible from outside, or a
faulted mover is indistinguishable from a hung processor. The processor reads it
as a CSR and republishes it in the status mirror of §6.3. The mover is still
private — nothing else addresses it — but it stays diagnosable.

### 4.4 What this deletes

- the `AUX_CFG` mover window and its `0x00`-`0x50` offsets
- the orchestrator path for mover control
- descriptor writes crossing the station bus at all, and with them every failure
  mode that comes from a 64-bit register write being repacked into a wider flit
  on the way in

---

## 5. Dispatch and graph execution

### 5.1 The processor is a compute unit, at (0,0)

From outside, it is a compute unit on the mesh: it is enumerated, addressed,
loaded, kicked and polled exactly like a vector core. That is the whole external
contract, and §6 is its detail.

**Its coordinate is `(0,0)`, and that is DERIVED — not a design choice.**

Routers occupy `(1..NX, 1..NY)`, with edge endpoints just outside them on the
four sides. A **corner touches no router**, so nothing can attach there and
`gen_mesh._check_corners` rejects a non-empty corner outright. `(0,0)` is
therefore free by construction in every mesh of every shape, forever, and no map
can collide with it. An earlier revision of this page called the coordinate "a
design choice, pick it from the generator"; that produced a `cpu` token which
spent a real attach point on a processor that needs none, and it is retired.

It is also not MAG's port-0 coordinate, where the agent answers (`ORC_X(MEM_X),
ORC_Y(MEM_Y)` — (0,1) at the defaults). Sharing would put two responders at one
address for CU_CTRL, which `noc_cu_base` answers automatically for indices 0-3.

So the hub's inbound demux keys on **destination coordinate**, not flit type,
which is what makes "looks exactly like a CU" true rather than approximately
true. X-then-Y routing delivers it with no special case: a flit for `(0,0)`
leaves its router westward onto that row's port, and the demux peels it off.

**It has no port of its own** — it is a client of `sn_hub`, as the agent and the
interlink are. Outbound joins the port-by-destination-row mux there. Priority:

    agent > processor > interlink > engine

Control flits are a handful against a stream of operand words, so the agent
wins; a stalled dispatch stalls the graph, so the processor is next; the
interlink sits below both because its burst is bounded by credit the far end
already granted.

### 5.2 What `rv_noc_req` needed, and has

This section once listed three missing pieces. **All three landed**; it is kept
because what they are for has not changed.

1. **A `CU_INST` emitter** — type `0x5`, with `last` and a program id.
   `rv_noc_req.v:326` builds it. Without this the unit could write a peer's
   memory but not start anything.
2. **A `CU_SIGNAL` consumer** — `rv_mag_pe.v` wires `rx_sig`/`sig_pop` into the
   core's signal queue. A dispatcher that cannot hear a CU retire can start work
   and never learn it finished; this is the completion edge of graph execution.
3. **A drivable `rsvd` field** — `disp_rsvd` is a port. Bit 258 is "this flit
   leaves the mesh", and without it no processor flit could ever be marked
   remote.

So the processor can dispatch to its own mesh's units and hear them retire. What
that does **not** yet have is a bench: `ctrlpe_mesh` proves the processor runs a
program and drives the mover, not that it orchestrates the compute units around
it. That is the gap worth closing next.

### 5.3 The orchestrator is not deleted

It consumes CU_SIGNAL for credit accounting; if the processor takes them, that
model starves. Resolve with a **mode bit**: host-dispatch (today's orchestrator
path) or processor-dispatch (completions route to the processor). Fanning each
flit to both consumers is possible but makes `mem_in_busy` conditional on two
takers, which is more mechanism than the choice is worth.

More importantly, the orchestrator is **the bootstrap**. Something has to load
the processor that then takes over dispatch (§6.1). It is also the debug path,
and specifically the path that still works when the processor itself is the
suspect — which is the case where a path that routes around it is worth most.

### 5.4 Active and passive units

Two categories, and the category is the definition rather than a rule to police:

- **Passive** — vector cores, matmul clusters. Consume CU_INST, emit CU_SIGNAL
  and memory traffic. A unit that instructs another unit is not passive.
- **Active** — the control processor, and any RV-based unit. Turing complete, so
  a reply is just another message it chooses to send.

Completion routing follows from `noc_cu_base.v:213-220`: a CU latches the
instruction's source coordinates and replies there. So whoever dispatches decides
who hears the retirement.

This implies a **centralized graph executor per mesh** — the dispatcher tracks the
whole graph, because completions always return to it. That is the intended shape.
A distributed variant would need a reply-to field that does not exist today.

### 5.5 Cross-mesh control

The transport exists and is type-agnostic. `mag_ilink.v:332-334` repurposes two
header fields:

| field | cross-mesh meaning |
|---|---|
| `RSVD[2]` (bit 258) | this flit leaves the mesh |
| `RSVD[1:0]` (257:256) | destination mesh |
| `TXN_ID` (267:260) | `fin` — final `{y, x}` in the destination mesh |

`mag.v:384-394` demuxes on `RSVD[2]` **before** type, and `mag_ilink.v:342-346`
reads the mesh, the remote bit, `fin`, the source and `last` off the flit and
does not test type either. On arrival `mag_ilink.v:640-647` copies the flit whole and
rewrites only `dst_x`/`dst_y` (from `fin`), `txn`, and the rsvd bits. **The type
field is never touched**, so CU_INST, CU_CTRL and CU_SIGNAL all cross.

It is one-way, for three independent reasons, all in the RTL:

1. **The tag field and the routing field are the same eight bits.** `TXN_ID`
   carries `fin` on the way over and is zeroed on injection. `noc_cu_base.v:268`
   echoes `HDR_ID` back as `ctrl_txn`, and line 217 latches it as `rep_id` for
   `SIG_BATCH_COMPLETE`. A cross-mesh CU_INST arrives with id 0.
2. **Source coordinates are not translated, deliberately.** `mag_ilink.v`'s
   header says so and names the cost: preserving `src` is what lets `vec_cu`'s
   `cd_alien` check separate two remote senders, and the price is that "answer the
   sender" answers a node in the wrong mesh. A remote CU's completion is delivered
   to whatever node sits at those coordinates locally.
3. **Remote reads fault.** `mag.v:397` raises `rq_rem`, which becomes
   `F_RD_REMOTE`.

**Therefore: partition the graph per mesh.** Each mesh's processor dispatches to
its own CUs and hears its own completions. Cross-mesh edges are **processor to
processor**, never processor to remote CU. Both ends are active, so each composes
its own reply and the broken return path never matters.

### 5.6 Cross-mesh data and ordering

A full producer/consumer handshake with no host in the loop:

1. producer's processor issues `mv.go` with a foreign destination address; the
   mover's write channel splits at `mag_ilink` and pushes
2. producer's processor rings the doorbell (`cfg` offset `0x90`, a wire from
   inside the node)
3. consumer's processor polls `dbell_n[src]`, readable at `stat_sel` 2-5, also a
   wire

The ordering guarantee is already correct: `mag_ilink.v:658` holds an inbound
doorbell until every write ahead of it has its `BRESP`, so a consumer released by
a doorbell is released by data that is in DRAM, not in a queue.

This needs **no new transport**.

---

## 6. The external interface

Load, fire, observe. All three reuse what `rv_pe` already implements, so the
host-side sequence is the one it already uses for any compute unit.

### 6.1 Load

| target | `buf_id` | granularity |
|---|---|---|
| instruction window | 1 | 32-byte granules |
| instruction window | 5 | one 32-bit word (patching) |
| scratchpad | 0 | 32-byte granules |
| scratchpad | 4 | one 32-bit word, byte enabled |

Bounds are checked and the check **rejects rather than wraps**
(`rv_pe.v:229-235`); an out-of-range burst is counted out, discarded and
reported.

The path from the host: `S_AXI_CTRL` → orchestrator staging → dispatch a CU_DATA
burst addressed to the processor's coordinate → the mesh delivers it → the demux
hands it to the processor. Local-to-local routing is deliberately reachable for
exactly this (`noc_router.v:196`).

### 6.2 Fire

A CU_INST carrying `{op, pc, arg}` (`rv_pe.v:325-327`).

**The kick cannot overtake the image.** `noc_cu_base` sorts CU_INST into the
instruction FIFO and CU_DATA into the receive FIFO, so ordering between one
sender and one unit is preserved on the wire but not across those two queues.
`K_IDLE` therefore waits on `rx_quiet`, which is cleared by the unit's own
progress and so cannot deadlock (`rv_pe.v:342`). This interlock already
exists and must not be removed.

### 6.3 Observe

Free, from `noc_cu_base` (`:270-276`):

| CU_CTRL index | contents |
|---|---|
| 0 | CAPS — type, version, buffers, instruction depth |
| 1 | STATUS — busy, instruction-FIFO space |
| 2 | `{instret, cycles}` |
| 3 | `dbg_ctr` |

Completion arrives as a CU_SIGNAL carrying `halt_word` as the result, with fault
set on cause 2 or 3 (`rv_pe.v:374-379`).

Two additions:

**A halt/reset bit.** Today only `boot_v` starts the core and only `core_halted`
ends it — there is no external stop, so a hung dispatcher needs a MAG reset. One
bit in a control register.

**Status mirrored into `AUX_STAT`.** MAG already has a `stat_sel` mux
(`mag.v:331`). Mirror run / halted / fault / pc, and the mover's fault code from
§4.3, into a slot. The host then reads liveness with **one 64-bit register read**
instead of a CU_CTRL flit round trip. This one is not optional: polling through a
flit round trip is precisely the cost this whole design exists to remove.

### 6.4 Large programs: the image loader

The instruction window is 8 KB, which is small for a full graph program. The
answer is an image loader: the host puts the real program in DRAM through
`S_AXI_MEM`, loads a small stub into imem, and the stub copies DRAM into imem.

The core executes from `rv_imem`, not from L1, so a DRAM-resident program cannot
run in place — it must be copied. And `imem_wr_en` today comes **only** from the
CU_DATA path (`rv_pe.v:261`), never from the core.

**This already works with no new RTL.** The peer-push path writes the instruction
window: `push_buf = pq_win ? BUF_IMEM_W : BUF_SPAD_W` (`rv_noc_req.v:185`), and
`push_dx`/`push_dy` may name the processor's own coordinate. So the stub is:

    loop:   lw   from DRAM (segment 0)
            push to self, win = 1     ; lands in imem

One word per push, two cycles each. An 8 KB image is a few thousand cycles at
mesh clock, and it runs once.

Worth building anyway: a **direct core-side imem store**, reached through a
segment window (§3.2, segment 3). That turns the loop into a plain `sw` and
removes the flit round trip from the one piece of code that runs before anything
else is known to work.

Constraint for the stub author: do not overwrite the region being executed.
Loader at the top of the window, program below.

### 6.5 What the host still does directly

Host talks to the processor for *work*. It still talks to MAG directly for:

- **`S_AXI_MEM`** — bulk upload and readback. This is how weights land, and it is
  the one path already proven end to end in simulation.
- **`S_AXI_CTRL`** — bring-up: clocks, resets, and interlink configuration at
  `cfg` offsets `0x80` / `0x88` / `0x90`.
- **unit-level access through the orchestrator** — mainly testing, but
  load-bearing for the case where the processor itself is the suspect. It costs
  nothing to keep, since the orchestrator stays for bootstrap regardless.

---

## 7. Cost — measured

Out-of-context on the node, at two ports throughout, which is the production
width. Every row but the last is **history** — `CTRL_PE` no longer exists and
the processor cannot be left out — but the differences are the only honest way
to price anything here.

| shape | LUT | FF | BRAM | DSP | WNS |
|---|---|---|---|---|---|
| **before the transform fold**, no processor | 36,733 | — | — | — | — |
| **before the transform fold**, with processor | **39,886** | — | — | — | — |
| after the fold, two modules, no processor | 28,016 | 48,104 | 36.5 | 35 | +0.096 |
| after the fold, two modules, with processor | 31,236 | 52,438 | 41.5 | 35 | +0.096 |
| **one component, hub** | **31,334** | 52,409 | 41.5 | 35 | +0.096 |

Rows 1–2 are 2026-08-24 morning, before the transform slot folded onto the
mover's read-return path; rows 3–4 the same evening, after it; row 5 on
2026-08-25 after the node became one component.

**The fold is where the area went**: 39,886 → 31,236 is **−8,650 LUT**, and it
is the largest single change this node has had. Against that genuinely
historical figure the node today is **−8,552 LUT, −21%**.

**The restructure itself costs +98 LUT (0.3%) and returns 29 FF**, at identical
BRAM, DSP and slack.

Where the 98 goes: the processor needs its own inbound arbiter, because a flit
for `(0,0)` may arrive at *any* port, so the hub carries a second 288-bit
port-select mux beside the agent's. Sharing one arbiter between them would
remove it — and would let a hung processor hold the port the host needs to
*recover* it, which is the one path that must never block. The mux is the price
of that, paid deliberately.

**Three things were tried to get it back, and only one worked.** Encoding the
outbound priority chain into a 2-bit select cost **+138**; narrowing the port
indices from 32 bits to `PSEL_W` cost **+18** — Vivado's own inference beat
both, and both were reverted. What did work: each port's mesh row is a
build-time constant, and passing it into the hub as a *wire* stopped it folding
against the flit compare on every port. As parameters it is **−155**.

> **There is no whole-mesh figure from before the fold.** Every mesh report in
> this tree is 2026-08-24 or later, so the mesh table below can only be read
> against the two-module shape, never against the 39,886-era node.

Hierarchically the hub is **631 LUT**: 320 of its own logic — the demux, three
arbiters and the outbound steer for four kinds of client — plus a 311-LUT
`sb_skid` on the encapsulator path that was inside `mag` before and folds away
entirely at `ILINK = 0`.

**The processor costs 3,220 LUT, 4,334 FF and 5 BRAM at two ports, and zero
DSP** (rows 3→4). At four meshes that is ~12,900 LUT. Hierarchically
`rv_mag_pe` was 2,818 LUT before it absorbed the mover; the difference is the
converged-path arbiters widening by one requester.

**The second memory port costs 6,557 LUT, 12,916 FF and no DSP** (21,459 at one
port → 28,016 at two), which is what makes a node with more than two ports
affordable.

**DSP is 35 at one port and at two.** That is the check that matters for the
fold: 32 for one transform bank plus 3 for the mover, and a figure that does not
move with the port count is the strongest form of "one bank per node".
`ooc_sysnode.tcl` errors above 48 precisely to catch a bank being generated per
port.

**WNS is +0.096 in every row.** The worst path is the same one with the
processor, without it, and behind the hub — so none of them sets it.

> **Synthesis, not implementation.** This tree has a recorded 0.740 ns synth →
> route loss on `m62_c1`, so +0.096 at synthesis is not a claim that 300 MHz
> closes on a placed design. It says the fold did not cost slack. The founded
> figure needs an implementation run, which this table does not carry.

### On the whole mesh, measured

The sysnode alone is not the vehicle that ships. On `ktpu_ship_2x2_6c2v_1m` — the
m62 shape, 6 matmul clusters, 2 vector cores, 2 memory ports — OOC at 3.333 ns:

| | LUT | FF | BRAM | URAM | DSP | WNS |
|---|---|---|---|---|---|---|
| m62, no processor | 178,792 | 254,077 | 447 | 158 | 1,961 | **+0.078** |
| m62 + processor, two modules | 182,636 | 258,971 | 456 | 158 | 1,961 | **−0.413** |
| **m62, one component** | **182,014** | 259,205 | 452 | 158 | 1,961 | **+0.078** |

The third row was taken before the port-row parameter fix above, so it carries
155 LUT the node no longer has.

**The two-module shape cost +3,844 LUT and 0.491 ns. The one-component shape
costs +3,222 LUT and NOTHING.** Slack is back to the no-processor figure exactly.

Why: the processor used to hang off a router local port, so its flits crossed
router → node → router, the same zig-zag `sn_hub`'s skid comment describes at
one level down. Behind the hub there is no extra hop, and the path that set the
−0.413 does not exist. Against the two-module shape the restructure is
**−622 LUT and +0.491 ns**; the node's own +253 is repaid several times over by
the router link it no longer needs.

**Area is cheap and slack is not** — which is why the second row is the one that
mattered, and why the third is the one that ships.

### The slack story, and why one measurement was not enough

Three measurements, and each corrected the one before it:

1. **OOC node, one port:** +2,844 LUT, **zero slack**. Read as "free".
2. **OOC sysnode, two ports:** +3,153 LUT, **+0.088 → −0.372**. So the one-port
   figure was not a per-port figure — *slack does not extrapolate*.
3. **OOC sysnode, two ports, after the transform left the memory port:** back to
   **zero slack**. `mag_mem_port.v` had recorded why — *the read FIFO's BRAM
   output reached the quantiser's DSP control through 9 LUT levels, 4.399 ns,
   and set the WNS on every SLR1 probe* — and removing it recovered 0.468 ns.
4. **Whole m62 mesh, same configuration:** **+0.078 → −0.413**.

So the node in isolation says the processor is free, and the mesh says it costs
0.49 ns. Both are real measurements of different things: the node's own critical
path is not the mesh's, and adding a requester to the converged arbiters
lengthens a scan that only matters once the clusters and vector cores are in the
same image competing for it.

**The honest statement is the mesh one**, because that is the thing that gets
built. What has not been measured is the placed-and-routed figure, and this tree
has retracted an Fmax claim before for exactly that reason — synthesis slack is
optimistic and a small negative should not be assumed away.

---

## 8. Software consequences

Scheduled work, not surprises:

- **`mover.py` is rewritten.** It stops being "write these registers" and becomes
  "build a descriptor, load a program, fire". The register-level mover API
  disappears with the `AUX_CFG` window.
- **The compiler gains a descriptor emitter.** Descriptors become data in the
  program image, built by ordinary stores, rather than a host-side register
  sequence.
- **The compiler gains a graph partitioner.** Per §5.5 the graph is partitioned
  per mesh, with cross-mesh edges lowered to processor-to-processor messages plus
  a doorbell.
- **Dispatch moves out of the driver.** The host-side dispatch loop becomes a
  bootstrap plus a completion poll on one register.

---

## 9. Order of work

1. **Measure `MP1 + 1` alone.** Add an idle requester channel to the m62 vehicle
   and run it. One parameter on an existing design, and if it costs slack
   everything downstream changes shape.
2. **Build the control-path bench.** No control-path RTL should be written before
   the control path can be simulated end to end — manager, station bus, MAG
   control window, orchestrator, mover pins — because that path is the one that
   has never been simulated as a whole.
3. **The three flit changes** (§5.2) on the NoC-attached `rv_pe`, where the
   existing PE benches already run.
4. **The MAG requester adapter and segment file** (§3.1, §3.2).
5. **`mv.go` / `mv.wait`** (§4.2).
6. **Halt bit, `AUX_STAT` mirror, image loader** (§6.3, §6.4).
7. **Cross-mesh** (§5.5, §5.6), on the two-mesh bench.

---

## 10. Open questions

- Whether one core suffices. As a *dispatcher* it does: descriptors are rare and
  a graph edge is a handful of instructions. Asserted, not measured.
- Whether `rv_l1` should gain multiple outstanding fills, given the MAG path
  supports them and the NoC path did not.
- Whether the direct core-side imem store (§6.4) lands in the first pass or after
  the self-push loader is proven.
