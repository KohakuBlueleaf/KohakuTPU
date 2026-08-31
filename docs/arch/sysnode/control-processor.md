---
title: The control processor
summary: A control processor is part of the system node rather than an option on it, and which processor it is, is a parameter. The two complexes, what each one connects, the address space the RV64 one sees, and what both measure.
tags:
  - architecture
  - sysnode
  - cpu
---

# The control processor

**A control processor is part of the system node, not an option on it.**
`sysnode.v` instantiates one unconditionally: there is no parameter that removes
it, and no empty slot where one might go. That is the structural claim, and it
is the one worth making — the node cannot be built as memory service alone,
because MAG on its own cannot start work without a host round trip.

**Which processor it is, is a parameter.** `CPU_RV64` selects one of two control
complexes. Both hold the same three things — a processor, the memory mover, and
the transform slot — and only the processor differs.

| | `CPU_RV64 = 0` — **the default** | `CPU_RV64 = 1` |
|---|---|---|
| module | `rv_mag_pe` | `rv64_mag_pe` |
| the processor | RV32, no control registers, a blocking L1, faults reported as halts | RV64IMA, supervisor privilege, Sv39 translation, a write-back L1 |
| how the host loads it | `CU_DATA` flits through the mesh, kicked with a `CU_INST` | an AXI-side register window, `hs_*`, and a boot doorbell |
| on the fabric | a compute unit at `(0,0)` — enumerated, kicked, reports a completion | a hub client at `(0,0)` with **no compute-unit shell**: it dispatches from a mailbox and answers no `CU_CTRL` read |
| status | mirrored into the node's one status register | in its own host window, and busy/fault in the node's status register |

Everything below that names one configuration says which. **Neither is described
here as shipping**: the default is the RV32 complex, and the RV64 one is a
measured configuration the ship generator cannot yet select. Its node-level
connections are made — [what the RV64 configuration
connects](#what-the-rv64-configuration-connects) states each one, and what still
has no owner.

One processor per system node, so one per mesh. The mesh field in an address is
two bits (`[37:36]`, and `mag_ilink`'s doorbell array has four entries), so
**four on the current device**.

> **Looking for what a node can actually do, rather than why it is built this
> way?** [abilities](abilities.md) is the reference: every ability of the node
> as a standalone system, the register map a program needs for each, and the
> test behind it. This page explains the mechanisms underneath.

## Where it sits

The processor is a client of `sn_hub` like everything else inside the node, and
has no fabric attachment of its own — [edge-and-control](edge-and-control.md#the-hub).
Beside it sit MAG, which serves memory and carries cross-mesh traffic, and the
**memory mover**, the node's descriptor-driven copy engine. MAG and the
processor are a division of **design**, not of component: MAG alone cannot start
work without a host round trip, and the processor alone cannot reach memory or
another mesh.

```
   host AXI ──► MAG control window ─────────────────┐
   host hs_* ─────────────────────────────────────┐ │
                                                  ▼ ▼
   sn_hub ◄───── flits ────────────────►   the control complex
                                            rv64_syscore  ── cp_* ─┐
                                            mm_mover      ── mv_* ─┤
                                            mag_xform (the slot)   │
                                                                   ▼
                                            MAG's converged path ──► DRAM
```

### The complex holds three things, and only one of them is the processor

Both complexes assemble the processor, the **memory mover**, and the
**transform slot** — the format-conversion socket on the mover's read return.
`rv_mag_pe` and `rv64_mag_pe` differ in the first of the three and in nothing
else.

**The mover and the slot belong to the node, not to whichever processor sits in
it.** Removing the processor does not remove the mover; it moves back to MAG.
Any cost argument that subtracts the complex to price a processor has subtracted
the mover with it, and the mover does not disappear.

The design view of that three-layer arrangement — scalar processor, mover as its
SIMD memory unit, slot as that unit's extension — is
[simd-model](simd-model.md).

## The interface the RV64 complex presents

Seven boundaries, and each is one direction of traffic.

| boundary | signals | what crosses it |
|---|---|---|
| **host window** | `hs_addr` (32), `hs_wr`, `hs_wdata` (64), `hs_wstrb`, `hs_rd`, `hs_rdata` | the program image, the boot doorbell, and status readback |
| **node port** | `cp_*` — one AXI master, 40-bit address, 256-bit data | every access the processor makes outside its own memories |
| **mover master** | `mv_*` — a second AXI master, channel `MV` | the mover's own datapath, kept separate so a 32 B/cycle walk never contends with an L1 fill |
| **host config window** | `aux_cfg_en/addr/data` | the host's path to the mover's registers, arbitrated against the processor's |
| **hub client** | `noc_in_*`, `noc_out_*` at `(0,0)` | dispatch flits out of the mailbox, completion flits in |
| **interlink doorbell** | `db_en/addr/data` out, `db_status` in | the processor rings a doorbell in another mesh, and reads the four inbound counts back |
| **node status** | `irq_summary` in; `busy` out, and the node's `pe_status` word built from it | mover fault, host halt request and a pending inbound doorbell in; busy and fault out |

The RV32 complex presents the first four the same way, except that its image and
its kick arrive as flits through the hub rather than through an `hs_*` window.
It presents the last three differently: it wears a compute-unit shell, so its
fabric client is that shell rather than a mailbox, and it takes the halt request
as an input and composes `pe_status` itself.

The **datapath does not merge, only the control does.** That is the point of
folding the mover into the complex: `mv.go` is a store the processor decodes,
while the bytes the mover moves stay on their own requester channel.

Both AXI masters land on MAG's converged internal path as requesters `CP` and
`MV`. MAG's requester count is `PORTS + 3 (+1 with the interlink)` — the memory
engines, the host upload window, `CP` and `MV`, and with the interlink the
channel inbound remote writes land through. **There is no configuration in which
that count is smaller**, because the processor is not optional.

### Load, boot, observe

`hs_addr[31:28]` selects which of three things a host write reaches:

| selector | target |
|---|---|
| `0` | the instruction memory, one 32-bit word per write |
| `1` | the scratchpad, one 64-bit word per write, byte-enabled |
| `2` | the host control registers below |

| offset | R/W | meaning |
|---|---|---|
| `0x00` | W | boot: writing 1 pulses the boot request and enables the run |
| `0x08` | W | the boot PC |
| `0x10` | W | the doorbell bit, which raises the core's software interrupt |
| `0x18` | R | `{exited, halted, cause}` |
| `0x20` | R | the exit word the program stored |
| `0x28` | R | the PC at which it halted |
| `0x30` / `0x38` | R | cycles and retired instructions, 64-bit |

> **Halt state is latched, and it has to be.** `run_en` drops the core's reset
> the moment the core halts, and that clears `halted` inside the core — so a
> status register reading the live signal reports nothing, forever. `halt_l`,
> `cause_l` and `haltpc_l` hold it across the reset. **Diagnostics that live
> inside the thing being reset are not diagnostics.**

The counters are 64-bit, unlike the 32-bit pair a compute unit publishes: a
runtime runs long enough to wrap 32 bits. They clear on the **boot pulse**, not
on the core's reset — the core goes back into reset when it halts, and a counter
cleared there reads zero to whoever asked.

## The address space the processor sees

Four regions, and the boundary between them is decided by **bit tests, not
magnitude compares**. Every range is power-of-two aligned and power-of-two
sized, so each test is one equality or one bit.

| region | base | reached by | latency |
|---|---|---|---|
| scratchpad | `0x0001_0000`, `SPAD_WORDS × 8` bytes | the array directly | 1 cycle |
| control region | `0x0002_0000`, 256 bytes | a register mux | 1 cycle |
| node fabric, **cached** | in the node range with bits 39 and 38 clear — DRAM | the L1, then `cp_*` on a miss | L1 hit, or a fabric round trip |
| node fabric, **uncached** | in the node range with bit 39 set (staging, apertures) or bit 38 set (the uncached alias of DRAM) | `cp_*` directly, bit 38 cleared on the way out | a fabric round trip |

The node range is everything at or above `2^28`. Inside it, two bits decide
whether an access is cached: bit 39 (the fabric's aperture bit) and bit 38,
which the fabric keeps at zero and the processor uses as the **uncached alias**
of DRAM — `pa | 1 << 38` is the same bytes with no L1 in the way, stripped in
`rv64_nport` before the port. The bit map is in
[address-map.md](../../address-map.md).

> **Why a bit test rather than a compare.** This decode is in the stall path —
> forward mux, address adder, decode, stall — and stall gates every pipeline
> register including the predictor's return-address stack. Written as `>=`/`<`
> on 40 bits it measured 200 failing paths at −0.552 ns. That is a general rule
> for this machine, not a local fix: **size and align a range so its test is one
> bit.**

### The L1 caches DRAM and nothing else

**Staging is uncached on purpose.** The staging L2 — a ~2 MB URAM store in the
node's address map, reached by ordinary load and store — holds the page tables,
the cross-node mailbox and the allocator bitmap. Caching it would put a
page-table walk behind the L1 miss that triggered the walk, which is a deadlock
against a blocking L1 rather than a slowdown.

The L1 itself is direct-mapped, 32-byte lines, write-back, **one outstanding
miss**. A line is exactly one 256-bit AXI beat, so a fill is a single-beat read
and a writeback a single-beat write: there are no bursts on the node port at
all.

### One handshake for every access, and it costs a cycle

The core issues a memory access in E and consumes it in M, so a tag lookup
started in E answers in M — too late to hold E on a miss. Holding E for the
lookup instead makes hit and miss the same shape, at one cycle. **Every access
pays it, the local scratchpad included.**

That is a deliberate price. The first cycle is decided from a decode-only
signal, the range decode is **registered** on that cycle, and cycles 2 onward
are steered from registers — which is what keeps the 64-bit effective-address
adder out of `stall`.

> **The rule that generalises: register every consumer of the effective address,
> except a memory read address.** A read has to be issued in the first cycle to
> be answered in the second; nothing else does. Writes, write enables, control
> decodes and stalls all have a spare cycle and must use it. Each consumer that
> did not — the L1 array's byte-write enable, the scratchpad's, the control
> region's write path — was a separate 13-to-15-level chain rooted in the same
> adder.

## The control region

Reached by ordinary load and store, uncached, and **not reorderable against a
mover command**, which is the property such a window needs.

| offset | R | W |
|---|---|---|
| `0x00` | — | **program exit** — stores the result word and raises the core's external halt |
| `0x08` | — | one byte to the debug console |
| `0x10` | the doorbell the host set | — |
| `0x18` | `satp`, a **read-only mirror** of the CSR | — |
| `0x20` | the mover's status: `[32]` busy, `[31:28]` fault, `[27:0]` moves retired | — |
| `0x28` | the interlink doorbell status: the four inbound counts, mesh 0 in `[15:0]` up to mesh 3 in `[63:48]` | — |
| `0x40`–`0x7F` | the **dispatch mailbox's** registers | the same registers; the index is address bits `[5:3]` |
| `0x80`–`0xBF` | — | the **mover's** registers; the mover's own offset is the low six bits |
| `0xC0`–`0xFF` | — | the **interlink's** registers; the low six bits are its offset, plus `0x80` |

**`satp` is the CSR, and this window only mirrors it.** Translation is
supervisor software's to configure, so the writable copy is the architectural
one; the control region keeps a read-only view at the offset it always had, so a
host can see the translation root without a debug port into the register file.
Two writable copies of a translation root is one too many.

**`mv.go` is a store, not an opcode.** Decoding a mover command from an address
keeps the ISA unmodified — a stock RV64 toolchain compiles it — and matches the
framework rule that control is a range rather than a side channel. All three
sub-ranges — mailbox, mover, doorbell — take their register index from the
address rather than from a decode, so adding a register costs nothing.

**Program exit is a control-region store, not `ECALL`.** `ECALL` has to remain a
call — that is the point of having supervisor mode — and `EBREAK` keeps its
debug meaning and its fault cause, so making `EBREAK` the terminator would
report every clean finish as a failure. The store-driven exit reports cause 0.

### The processor wins the mover's config port

The host's window and the processor's stores both reach the mover's registers.
When both pulse in one cycle, **the processor's store wins**. The host window is
split at offset `0x80`: below it is the mover's, at or above it the interlink's.
Without the interlink that gate folds to a constant and the mover sees every
write, exactly as it did before the interlink existed.

### The dispatch mailbox

`rv64_noc_mbox`, at control-region offsets `0x40`–`0x7F`. It is how the RV64
complex reaches the mesh at all: dropping the compute-unit shell dropped the
complex's only path onto the fabric with it, and this is the replacement.

**Software writes a dispatch, not a flit.** A flit is 288 bits against a 64-bit
store port, so a program composing one would take five stores with a tearing
window in the middle of them. Instead it names a destination and two payload
words, and hardware assembles the `CU_INST` — routing header, source coordinate,
and a transaction tag it increments itself.

Seven registers, indexed by address bits `[5:3]`:

| offset | name | access | contents |
|---|---|---|---|
| `0x40` | `DST` | RW | `[3:0]` destination x, `[11:8]` destination y |
| `0x48` | `ARG0` | RW | payload word 0 — the low 64 bits of the flit's payload |
| `0x50` | `ARG1` | RW | payload word 1 |
| `0x58` | `GO` | W | any store builds the flit from `DST`/`ARG0`/`ARG1` and offers it to the hub |
| `0x60` | `STAT` | R | `[7:0]` completions queued, `[15]` a dispatch is offered and not yet taken, `[31]` sticky queue overflow |
| `0x68` | `HEAD` | R | the oldest queued completion, or zero when the queue is empty |
| `0x70` | `POP` | W | any store discards the head |

A queued completion is one word: `[55:52]` source y, `[51:48]` source x,
`[47:40]` the `CU_SIGNAL` code, `[39:8]` its 32-bit argument.

Four properties a dispatcher has to build against.

**`GO` is ignored while the previous flit is still offered.** An offered flit is
held until the hub takes it — withdrawing one destroys it, and the loss is
silent at every point downstream — so a second `GO` arriving in that window does
nothing and reports nothing. **Poll `STAT[15]` before every `GO` but the first.**

**A completion the queue cannot hold is accepted and dropped, not
backpressured.** The mailbox never raises busy on the hub. Held instead, an
unwanted completion would sit at the head of the hub's queue and stall the link
for everything behind it — including the traffic that would drain the queue.
`STAT[31]` is sticky because a dropped completion and a unit that never finished
look identical from software, and only the flag separates them.

**Popping is a write, not a side effect of reading `HEAD`.** The control region
answers a read from a register one cycle later, so a read-triggered pop would
have to guess which cycle the read really happened on.

**A non-empty queue raises the core's external interrupt**, alongside the node's
own `irq_summary`. Waiting for a completion is exactly the condition a scheduler
must not have to poll for.

Only `CU_SIGNAL` flits are queued. Anything else addressed at `(0,0)` is
accepted and discarded, and the complex answers no `CU_CTRL` read — it is a
client of the hub, not a conforming compute unit, and a controller enumerating
the mesh sees the coordinate as empty.

The normative map, including the flit the mailbox builds and the credit rule a
program has to keep for itself, is
[spec/control-registers §7.5](../../spec/control-registers.md#75-the-dispatch-mailbox).

### The interlink window and the doorbell

`0xC0`–`0xFF` reaches the **interlink's** own registers: the processor's offset
plus `0x80` is the interlink's, which is the same map the host drives through
its config window. Three registers exist.

| offset | register | fields |
|---|---|---|
| `0xC0` | control | `[0]` enable — **set at reset**; `[1]` clear the inbound doorbell counts; `[2]` clear the sticky fault register |
| `0xC8` | mesh id | `[1:0]`, defaulting to the node's `MESH_ID`. A **runtime** value, so one bitstream is usable at any position in the grid |
| `0xD0` | ring | `[1:0]` destination mesh, `[15:8]` a tag. **The write itself rings the doorbell** |

**A doorbell is a count, not a flag** — one 16-bit count per source mesh, all
four read together at `0x28`, mesh 0 in `[15:0]` up to mesh 3 in `[63:48]`. A
reader polling slower than events arrive can tell how many it missed, which a
flag cannot.

**An inbound doorbell raises the core's external interrupt as a level.** The
line is asserted while any count is non-zero, so a ring arriving while another
is being serviced is not lost; the handler reads the counts and clears them with
`0xC0` bit 1, and the level drops with them. A clear racing an arriving doorbell
loses to the doorbell — losing one count is better than a clear that silently
does not clear.

> **The doorbell does not order itself against data, and software must.** The
> interlink's outbound arbiter picks between a remote write, a flit and a
> doorbell by **rotating priority**, so a ring requested while a remote write is
> still queued can leave first. The sequence that works is the one the two-node
> bench runs: write the data with the mover, **poll the mover's status at `0x20`
> until it is no longer busy**, and only then ring. Do not treat the ring as a
> release fence the hardware supplies.

Both halves of this window — configuring the link and ringing — are the
processor's only reach into another mesh. It cannot load or store there: the
node port is local, and reads never cross the link. Data moves by the mover.

## Sv39, and why it is in the wrapper

Translation is a property of the **system**, not of the pipeline. The core owns
the architectural state — `satp`, the privilege level, `mstatus.SUM` and `MXR` —
and issues the address; the TLB and the walker sit in the wrapper, between the
core's memory port and the node fabric. The consequence that matters is that the
*other* configuration of the same core — the one that attaches to a mesh as an
ordinary compute unit — **carries no MMU and pays nothing for one.**

Translation is on when `satp.MODE` is 8 and the hart runs below machine mode.
**One MMU serves both the data port and instruction fetch.** The data port wins;
a stalled fetch issues no data access, so the two cannot wait on each other.
Fetch is translated through a single page register in the wrapper rather than a
second TLB, because consecutive fetches share a page: one registered translation
covers about a thousand instructions and refills on the crossing. The walker,
the permission rules and how a faulting fetch is delivered to the core are in
[arch/cpu/rv64-sys](../cpu/rv64-sys/README.md).

> **The TLB entry is 57 bits because the card is 40-bit physical.** Sv39's PPN
> field is architecturally 44 bits; no address on this card exceeds 40, so the
> stored PPN is 28 and an entry is `{valid, tag[21:0], ppn[27:0], perms[5:0]}`.
> At the architectural 44 the entry is 73 bits — and **a block-RAM port is 72
> bits at its widest**, so the array becomes LUTs and the tool issues no
> warning. The same trap costs the branch predictor its target width: entries
> store a 39-bit target, not 64.

**Sv39 governs the processor. It does not translate one byte the mover moves**,
and it is not asked to. Bulk movement is the mover's traffic — 32 bytes per
cycle against a core's 32 bytes per round trip — and the card's own memory
management stays descriptor-built, a lookup performed once when a descriptor is
built rather than once per access.

That layering is also where isolation comes from, and it costs nothing to build:
a program running under Sv39 holds virtual addresses and **cannot name a
physical card address**, so it cannot construct a mover descriptor. It asks the
runtime, which holds the mapping. That is the same shape as a driver's
pin-and-get-device-address call.

**Page tables are per node and are never shared.** That invariant deletes TLB
shootdown entirely: no `SFENCE.VMA` ever has to cross a node.

## Why a second complex exists at all

The RV32 complex is 8 KB windows, a blocking L1, no control registers, faults
reported as halts, a four-bit address decode, and a **NoC compute-unit shell**
that makes the node's processor look from outside exactly like any other compute
unit — enumerated at `(0,0)`, loaded with `CU_DATA` flits, kicked with a
`CU_INST`, reporting a 32-bit word on a `CU_SIGNAL`.

Every one of those is right for a unit that runs a kernel and retires. None of
them is right for a processor that hosts a runtime, owns memory for a whole
mesh, and outlives the work it dispatches. So the RV64 complex is a second
design rather than a widened first one, and four things drove it — strongest
first.

**1. Lifecycle.** The shell implements *someone kicks me, I run to completion, I
report a word*. That is a batch compute unit. A runtime boots once and runs;
there is no completion to report and no result word to carry.

**2. Deadlock, and the cycle is specific.** The node's processor is the unit
that *services* the fabric: it dispatches to compute units and consumes their
completions. Behind the shell its inbound path is gated by a busy signal from
finite instruction and receive queues, and its dispatch shares one outbound port
with the shell's own traffic. A processor blocked sending a dispatch stops
draining its receive queue; the queue fills; busy rises; and the completions it
needs in order to make progress cannot land. **The unit that arbitrates the
fabric must not be flow-controlled by the fabric.** A compute unit can afford to
block. The scheduler cannot.

The [dispatch mailbox](#the-dispatch-mailbox) is that rule built into a
mechanism: it never raises busy on the hub, so a completion it has no room for
is accepted and dropped behind a sticky flag rather than held. The cost is that
losing one is possible; the gain is that the processor's inbound path can never
be the thing that stops the link.

**3. The loader is a second memory-write protocol.** The host already reaches
the card over AXI. Loading an instruction memory by AXI write plus a doorbell
needs no loader state machine, no `buf_id` map, no bounds check and no
receive-quiet interlock — all of which exist only because the image arrives as
flits.

**4. Reach.** A 32-bit processor cannot form the top eight structural bits of a
40-bit address. The RV32 complex works around it with a segment file; a 64-bit
core simply holds the address.

Dropping the shell is not free, and the obligation is worth stating: **the shell
guarantees that every write is visible when the completion arrives.** That is
the host's and a dispatcher's only sequencing point, and a complex without the
shell has to publish its own ordering guarantee to whoever waits on it. The RV64
complex has not done so yet.

### The other configuration of the same core is kept

The same RV64 core is also built as an ordinary **mesh compute unit** — core,
compute-unit shell, loader and kick/complete, no MMU, atomics optional. That is
a different product and it is not this page's subject. Both configurations are
covered in [arch/cpu/rv64-sys](../cpu/rv64-sys/README.md).

## Cost — measured

**Out-of-context synthesis, `xcvu13p-fhgb2104-2L-e`, Vivado 2024.2, one clock
constraint at 3.333 ns, design state Synthesized, `PORTS=2` — which is the
production width.** Produced by `scripts/tcl/ooc_sysnode.tcl 2` (RV64,
reports `build/node_sn64_p2_{util,hier,time}.rpt`, run of 2026-08-26 23:46) and
`scripts/tcl/ooc_sysnode.tcl` (RV32), each synthesising `sysnode` whole with
`-flatten_hierarchy rebuilt`, which is the ship flow. **Nothing here is routed.**

| whole node | LUT | FF | BRAM tiles | URAM | DSP | WNS |
|---|---|---|---|---|---|---|
| **RV64 complex** | **32,859** | 46,436 | 57.5 | 65 | 47 | **+0.039** |
| RV32 complex — read the caveat below | 31,220 | 52,481 | 41.5 | 128 | 39 | +0.096 |

> **The node meets 300 MHz in out-of-context synthesis.** WNS is **+0.039 ns**
> against a 3.333 ns request — an achieved synthesis period of 3.294 ns — with
> **no failing endpoint** among 124,100. The last cone to close was in the
> mover, and it had nothing to do with the processor: `mm_mover`'s `mode` →
> `fifo_room` (an add and a compare) → `stall` → `proc` → the command FIFO's
> write enable, 12 logic levels. **Registering the `fifo_room` limit** took the
> add and the compare out of that path and closed it, at 19 LUT less than
> before.
>
> **That is synthesis, not routing, and the distinction is the whole caveat.**
> Synthesis slack is optimistic in this tree — one module lost 0.740 ns going
> from synthesis to routing, which is twenty times the margin here — and no
> routed result exists for this top. The founded claim is **"meets 300 MHz in
> out-of-context synthesis"**. It is not a claim that the design has closed
> timing, and no Fmax above 300 MHz follows from +0.039 ns.

> **The RV32 row is the last measurement of that configuration, not its current
> cost.** It is a run of 2026-08-26 09:04, and two modules that both
> configurations share changed after it: `mag_mem_port`'s write-slot data array
> moved from distributed RAM to block RAM, and `mm_prng`'s constant multiplies
> moved onto DSPs. The RV32 configuration has **not been re-synthesised since**.
> The direction is known — fewer LUTs, more block RAM, more DSP — the values are
> not, and this page does not estimate them.

**The two runs are not otherwise identical, and the difference is not only the
processor.** The RV64 run also sets `STAGE_AT_PORT=1` — one staging store on the
converged path rather than one inside every memory port — and gives the
processor a larger instruction memory, scratchpad and L1. That is why URAM falls
from 128 to 65.

The complex is the closest thing to a processor-swap figure, from the
hierarchical report of those same two runs:

| instance | LUT | FF | RAMB36 / RAMB18 | URAM | DSP |
|---|---|---|---|---|---|
| `rv_mag_pe` — the RV32 complex | 11,665 | 15,163 | 13 / 0 | 0 | 39 |
| **`rv64_mag_pe` — the RV64 complex** | **16,010** | 16,458 | 20 / 2 | 1 | 47 |

The 4,345 LUT between them buys a 64-bit datapath, hardware divide, the full `A`
extension, machine/supervisor/user privilege with delegation, control registers
with traps and interrupts, a 256-entry branch target buffer with gshare and a
return-address stack, Sv39 with a hardware walker shared by fetch and data, a
write-back L1, and the dispatch mailbox.

> **That difference is not a founded figure either**, for the same reason as the
> whole-node one: the mover sits inside both complexes and the two rows were
> synthesised from different mover RTL. The PRNG alone accounts for 817 LUT of
> it — 1,026 in the RV32 row against 209 in the RV64 one — in the RV32 row's
> favour, so the true swap cost is the larger. Treat 4,345 as a lower bound
> until the RV32 node is re-run.

Inside the RV64 complex, hierarchically:

| instance | LUT | FF | RAMB36 / RAMB18 | URAM | DSP | belongs to |
|---|---|---|---|---|---|---|
| `rv64_syscore` — the processor | 7,244 | 5,776 | 12 / 2 | 1 | 4 | the processor |
| `mm_mover` — the mover | 4,226 | 5,770 | 8 / 0 | 0 | 11 | **the node** |
| `mag_xform` — the slot and its bank | 4,540 | 4,912 | 0 / 0 | 0 | 32 | **the node** |

Those three sum to the complex's 16,010 exactly: `rv64_mag_pe` holds no logic of
its own beyond the three instances and the config-port arbiter that folds into
them.

Inside the processor, the core is 6,169 LUT of the 7,244 — **85%**. The
remaining 1,075 is everything else the wrapper holds: the L1 at 501, the node
port's four-client mux at 142, the MMU at 103 (its TLB is one block RAM), the
dispatch mailbox at 76, the instruction window's one LUT of glue in front of 8
block RAMs, and 252 of host window, address decode and control region. **Any
area argument that starts with the integration is looking in the wrong place.**

> **These sub-rows come from a hierarchical report on a `rebuilt` netlist**, so a
> leaf may be charged to the instance it was re-parented into. The top-line
> totals are exact; treat the breakdown as attribution rather than arithmetic.

**DSP is 47 in the RV64 node**: 32 for one transform bank, 4 for the core's
multiplier, and 11 for the mover — 3 in the mover proper and **8 in the PRNG**.
Those eight are new, and they are deliberate. Philox needs four multiplies by a
32-bit constant per round, and a constant multiply is not a multiply to
synthesis: left alone the four became shift-and-add trees costing 1,026 LUT and
no DSP. Carrying `use_dsp` on them puts them in the DSP48s the design planned
on, at 209 LUT — **−817 LUT for +8 DSP**, on a part where LUTs are the scarce
resource and 47 of 12,288 DSPs is not.

> **The per-port DSP check is a narrower guard than it was.** `ooc_sysnode.tcl`
> errors above 48 DSP to catch a transform bank being generated per memory port.
> A duplicated bank adds 32 and still trips it, but the margin above the expected
> value is now 1 rather than 9. `ooc_sysnode_rv64.tcl` carries a 35,000 LUT
> budget check and no DSP check at all; it should carry both.

The whole-node budget for this work is **35,000 LUT with a hard ceiling of
38,000**. At 32,859 the RV64 node is 2,141 under the target, and it meets the
timing request as well — see the note above for what "meets" is and is not
claiming.

## What the RV64 configuration connects

Every node-level port on the complex is driven or read in `sysnode.v`'s
`CPU_RV64 != 0` branch:

| port | what drives or reads it | what it means |
|---|---|---|
| `pe_tx_*`, `pe_rx_*` | the dispatch mailbox, as a client of `sn_hub` at `(0,0)` | the processor sends `CU_INST` flits and queues the `CU_SIGNAL` flits that come back |
| `db_status` | `mag_ilink`'s four inbound doorbell counts | a doorbell status read returns real counts. It reads zero when the interlink is not built, which is also what "no doorbells" looks like |
| `db_en`, `db_addr`, `db_data` | the control region's `0xC0`–`0xFF` window, into a second config writer on `mag` — the host wins a same-cycle collision | the processor enables the link, sets its mesh id and rings a peer's doorbell without a host round trip |
| `irq_summary` | mover fault, the host's halt request, or a **pending inbound doorbell** | the node conditions a runtime must react to rather than poll. A non-empty completion queue raises the same core input |
| `pe_status` | `{62'd0, mover fault, busy}` | the node's status mirror reports whether this node is running and whether its mover faulted |

That is what the 32,859 LUT figure measures.

Three things a dispatcher still has to supply for itself, and none of them is a
missing wire.

**Credit is software's.** The mailbox has no credit register and no credit
counter. The control agent's dispatch path has both, and stalls locally when
credit runs out precisely because backpressuring a `CU_INST` into the mesh is
the protocol deadlock the framework exists to avoid —
[spec/control-registers](../../spec/control-registers.md#24-credit). A program
dispatching from the mailbox **must not** send a unit more instructions than its
instruction FIFO holds, and nothing in hardware will stop it. The depth is
readable from that unit's `CU_CAPS`.

**Ordering is unpublished.** The compute-unit shell guarantees that every write
a unit made is visible when its completion arrives. That is the only sequencing
point a dispatcher has, and a complex without the shell owes whoever waits on it
an equivalent guarantee. This one has not stated one yet.

**The node is not enumerable.** `(0,0)` answers no `CU_CTRL` read, so a
controller walking the mesh sees the coordinate as empty — indistinguishable
from an unoccupied one. The RV32 configuration does answer, because it wears the
shell. There is no runtime way to tell which configuration a bitstream carries.

## What neither configuration does

**It does not translate mover traffic**, and Sv39 is not asked to.

**It does not isolate requesters on the card.** Nothing checks that a descriptor
names memory its author was entitled to. The mover's fault codes are length,
range, AXI, mode, width, alignment and padding — none of them is a permission
check, and Sv39 does not change that.

**It does not fault and resume.** Neither a compute unit's instruction nor a
mover walk can be suspended mid-access; the mover carries 768 bits of walker
state with no checkpoint path, on an interface where a read return can never be
refused.

**It does not manage another node.** A system node has **no master port onto the
station bus**, so this processor can drive its own mesh and push into a peer's
memory over the interlink, but it cannot write a peer's control registers,
retune a peer's clocks or reset a peer. Those remain host operations. An owning
node is a **server, never a manager**, and that bounds what a capability held in
one node can ever mean.

**It does not boot itself.** Nothing writes the instruction memory at reset and
the node owns no non-volatile storage. Standalone operation needs the program in
the bitstream, or storage the device top supplies. That is a missing peripheral,
not a software gap.

## What the host still does directly

The host talks to the processor for *work*. It still talks to MAG for:

- **the memory window** — bulk upload and readback, which is how weights land;
- **the control window** — bring-up: clocks, resets, and interlink
  configuration;
- **the mover's registers**, through the config window, arbitrated against the
  processor's stores as above;
- **unit-level access through the control agent** — mainly testing, and
  load-bearing for the case where the processor itself is the suspect. A path
  that routes around the processor is worth most exactly then.

## Where today's source disagrees

Four places where the source and its own stated intent do not line up.

- **The ship generator cannot select the RV64 complex.** `sysnode.v` takes
  `CPU_RV64`, but `gen_mesh.py` emits no value for it, so every generated ship
  top elaborates the default RV32 branch and there is **no way to build a ship
  with the RV64 complex without editing the generator**. The RV64 figures on
  this page come from `ooc_sysnode_rv64.tcl`, which sets the parameter directly
  on a standalone `sysnode` synthesis.
- **Two of the mover's nine registers are unreachable from the control region.**
  The mover decodes registers at config offsets `0x00, 0x10, 0x18, 0x20, 0x28,
  0x30, 0x38, 0x40, 0x50`. The control region maps its own `0x80`–`0xBF` onto
  mover offsets `0x00`–`0x3F`, so `0x40` — the fill immediate — and `0x50` — the
  gather pitch and word count — fall outside it, into the interlink sub-range.
  **A `FILL` or `GATHER` move therefore cannot be fully programmed from the RV64
  processor**; both registers remain reachable from the host's config window,
  which passes every offset below `0x80` through. Nothing about the address map
  makes this deliberate — the two windows were sized independently.
- **The transform bank's register port is tied off in the RV64 complex.**
  `rv64_mag_pe` wires the bank's `cfg_en` to zero and leaves `cfg_rdata` and the
  bank's fault output unread, so **occupant registers and the bank's own fault
  code are not reachable there**. The RV32 complex reaches both through its node
  range, so this is a connection the swap dropped rather than a design position.
  Pages describing occupant registers as processor-reachable —
  [simd-model](simd-model.md) and [transform-stage](transform-stage.md) — say
  which configuration they mean.
- **`CACHE_LO` names a threshold and the RTL tests a bit.** The parameter's
  comment reads "this and above is cached"; the decode is `address bit 31`,
  which agrees with the comment only below 4 GB. The bit test is what makes the
  decode cheap enough to sit in the stall path, so the name is the thing that is
  wrong.

## Where this is verified

| bench | what it holds |
|---|---|
| `rv64_core` | the pipeline alone, running compiled C — a call chain with a real stack, recursion, a self-checking sort, and byte through doubleword traffic |
| `rv64_l1` | 8 KB driven through a 2 KB cache against a reference memory, every writeback beat checked as it leaves |
| `rv64_mmu` | the TLB and the walker, and the shared port: a data access pre-empting a fetch walk, a cold request not riding the previous hit, a fetch fault staying with the fetch |
| `rv64_nport` | the four-client node-port mux |
| `rv64_syscore` | the whole processor, driven by compiled programs — node regions written and read back and checked not to alias, an atomic to the node range, privilege transitions and delegation, Sv39 tables walked by hardware, and `tests/rv64/dispatch.c` for the mailbox: a dispatch built in hardware, a completion queued and popped |
| `rv64_mag_pe` | the complex — the processor with the mover and the transform bank instantiated |
| `rv64_syscore_pair` | two complexes on one fabric memory, each running the same program, with the **written-word sets intersected** to prove the footprints are disjoint |
| `rv64_node_pair` | **two whole `sysnode`s on one interlink**, each with its own DRAM model and its own program: a strobed store into staging, a mover copy into the far mesh's staging, the doorbell taken as an interrupt on the far side, and a reply rung back |

The pair tests are what make a pair test a test at all: `rv64_syscore_pair`
intersects the exact words touched, without which two units writing identical
values to identical addresses would pass while proving nothing; `rv64_node_pair`
is the only bench in which a processor drives the interlink itself.

**The node-level benches in `tests/sysnode/` exercise the RV32 complex.** For
`CPU_RV64 = 1` the whole-node evidence is `rv64_node_pair` rather than anything
under that directory. The full list, with the program behind each ability, is in
[abilities](abilities.md#11-verification-behind-this-page).
