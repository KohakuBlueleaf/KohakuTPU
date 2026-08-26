---
title: RV64 system core memory system
summary: Sv39 and its hardware walk, the one MMU that serves both fetch and data and the rules that keep them apart, why a TLB entry is 57 bits, the write-back L1, the four-client node-port arbiter, what is cached and what is not, and the ordering the core publishes.
tags:
  - architecture
  - cpu
  - rv64
  - memory
---

# RV64 system core memory system

Everything between the core's memory ports and the system node. The core's
**datapath is a physical-address machine** and stays one: it issues an address
and waits, and every structure on this page — TLB, walker, cache, arbiter —
lives in the wrapper around it. What the core does own is the architectural
state translation is *controlled* by, `satp`, `priv`, `SUM` and `MXR`, which it
exports to the wrapper ([architecture](architecture.md#the-privilege-model)).
That split is why the mesh compute-unit configuration carries none of this
machinery and pays for none of it.

Terms used below, defined once. **MAG** is the system node's memory access half
— the block that turns descriptors into DRAM bursts and carries cross-mesh
traffic. **Staging** is on-chip memory inside MAG that the mover and the
inter-mesh link share; it is reached through the **aperture**, the top bit of
the card's 40-bit address ([address-map](../../../address-map.md)). The
**mover** is the node's descriptor-walking memory engine. A **doorbell** is a
single word one agent writes to make another notice.

## The path an access takes

```
                             rv64_syscore
   ┌──────────┐
   │          │ imem_addr ──▶ fetch page register ──▶ instruction window
   │          │◀── imem_stall ──────┴── refill ──┐
   │          │                                  │
   │ rv64_core│                            ┌───────────┐
   │          │                            │ rv64_mmu  │ TLB + walker
   │          │ dmem_req (decode only) ────┤  one, and │────────────────┐
   │          │ dmem_addr ────────────────▶│  data wins│──▶ pa          │
   │          │                            └───────────┘     │          │
   │          │◀── dmem_stall ───────────────────────────────┴──────────┘
   │          │                                              │
   │          │      ┌── scratchpad      32 KB, byte-writable, 1 cycle
   │          │      │
   │  dmem_   │      ├── control region  256 B of registers
   │  rdata   │──────┤
   │          │      ├── rv64_l1 ──────┐ 64 lines x 32 B, write-back
   │          │      │                 │
   └──────────┘      └── uncached ─────┤
                                       │
                     rv64_mmu walker ──┤
                                       │
                                       ▼
                                  rv64_nport ─── one AXI master ──▶ MAG
                                  4 clients, 40-bit address, 256-bit data
```

Four consumers converge on one AXI master: the page-table walker, the L1's fill,
the L1's writeback, and an uncached 64-bit access.
[The node-port arbiter](#the-node-port-arbiter) is why that can be a priority
mux rather than a queue.

**One MMU serves two requesters** — instruction fetch and the data port — and
[the rules that keep them from waiting on each
other](#one-mmu-two-requesters) are the part of this page a reader building
their own would need first.

## One handshake for every access

Every access — scratchpad, control register, cached, uncached — goes through the
same two-phase handshake, and the shape of it is a timing decision that software
can feel.

The stall the wrapper returns to the core is an OR of exactly three terms: the
MMU is busy, **this is the access's first cycle**, or the access has started and
is not done. Nothing else.

**The first cycle is decided from the core's memory-request bit alone**, which
the core exports as decode-only — no address, no adder
([microarchitecture](microarchitecture.md#nothing-address-derived-may-reach-stall)).
On that cycle the wrapper latches the translated address, the byte enables, the
write data and the store flag, and latches the **range decode** as four select
bits. Cycles two onward are steered entirely from those registers.

So `dmem_stall` is one OR of a constant-ish term, a decode-only term and a
register. Nothing in it runs through the 64-bit address adder, which matters
because the core's `stall` gates every pipeline register's enable, including the
branch predictor's stack pointer.

**The price is one extra cycle on every access, the local scratchpad included.**

| access | cycles held in execute |
|---|---|
| scratchpad or control register | 2 |
| L1 hit | 3 |
| L1 miss | 3 + the fill round trip (+ an eviction, if the victim is dirty) |
| uncached | 2 + the node round trip |
| any of the above behind a page-table walk | + the walk |

The alternative to paying it is an address-generation stage between execute and
memory, which this core does not have —
[microarchitecture](microarchitecture.md#why-there-is-no-address-generation-stage).

### Registering every address consumer

The rule the wrapper is built to, stated once because it generalises:

> **Every consumer of the effective address is registered, except a memory
> *read* address.**

A read has to be issued in the first cycle to be answered in the second.
Nothing else does — write data, write enables, byte-write enables, range
decodes, control-register decodes and the stall itself all have a spare cycle
and take it. The 64-bit adder is roughly eight logic levels on its own and the
whole budget for a path is about eleven, so anything the adder feeds
combinationally starts two thirds spent.

Applied one consumer at a time, each fix exposing the next, this measured
**−227 LUT and +13.8 MHz together**, taking seventeen failing paths to none.
Area and frequency moving the same way is what removing logic looks like, as
opposed to trading it.

## Sv39

`rv64_mmu` — a direct-mapped TLB and a hardware page-table walker, sitting
between the core's two memory ports and everything else.

### When translation is on

Translation is on when **both** hold: `satp`'s `MODE` field says Sv39 (the value
8), **and the current privilege level is not machine**. That second condition is
the architecture's, not this design's — machine mode never translates on any
RISC-V implementation.

Both terms are now reachable from software. `priv` is a register in the core's
CSR file and `satp` is a supervisor CSR, so a supervisor or user program runs
translated and a machine-mode runtime does not
([architecture](architecture.md#the-privilege-model)).

**Translation off costs nothing.** With `MODE` zero, or in machine mode, the
address goes straight through combinationally: `busy` never asserts, `pa` is
`va[39:0]`, and the walker stays idle. A machine-mode runtime therefore runs at
exactly the IPC it did before this module existed. With Sv39 on, the TLB array
answers a cycle after the address goes in, so **even a hit holds the core for
one cycle**; pipelining the lookup into execute would need the address a stage
earlier than the core produces it.

`SUM` and `MXR` are implemented in `mstatus` and reach the MMU, so a supervisor
may read and write a user page when `SUM` is set, and may read an
execute-only page when `MXR` is set. **Neither relaxes instruction fetch** —
see [the fetch rule](#supervisor-may-not-fetch-from-a-user-page).

### `satp` is a CSR, and the control region mirrors it

`satp` is CSR `0x180`, owned by supervisor software. The wrapper's control
region keeps a **read-only mirror** of it at `CTRL_BASE + 0x18` so a host can
see the translation root without stopping the core.

**Read-only is the whole point.** Two writable copies of a translation root is
one too many: the CSR and the memory-mapped word would disagree the moment
either was written alone, and nothing would say which one the hardware was
using.

`SFENCE.VMA` sweeps the whole TLB — every entry, `rs1` and `rs2` ignored,
because there are no ASIDs to select by. It also retires the fetch page
register, one cycle after the fence itself retires, and fetch is held for that
cycle so no instruction is fetched through a stale translation.

### An entry is 57 bits because the card is 40-bit

Sv39's PPN field is **44 bits**. No address on this card exceeds **40**, so the
stored PPN is 28 bits and an entry is

```
{ valid, tag[21:0], ppn[27:0], perms[5:0] }  =  57 bits
```

which fits inside a block-RAM port. **Storing the architectural 44 would make
the entry 73 bits, a block-RAM port is 72 at its widest, and the array would
silently become LUTs** — no error, no warning, and a module that costs hundreds
of LUT instead of dozens. It is the same arithmetic that decides the branch
predictor's 39-bit target
([microarchitecture](microarchitecture.md#the-target-is-39-bits-and-that-is-a-block-ram-decision)),
and it is worth stating as a general rule for anyone building on this part:
**check that the synthesis report says block RAM where you expected block RAM.**

`perms` is `{D, A, U, X, W, R}`, taken from the leaf PTE.

### Direct-mapped, not a CAM

32 entries by default, indexed by the low bits of the VPN, tag the rest. A
32-entry fully associative array is thirty-two 22-bit comparators and a priority
encoder, and it buys hit rate this machine does not need: its addresses are
computable ahead of time and its working sets are descriptor-shaped rather than
pointer-shaped. That assumption is the framework's, not this core's
([docs/README](../../../README.md#does-your-workload-fit)).

The array answers a cycle after the address goes in, so the tag is compared
against a **registered** copy of the request. Even a hit therefore holds the
core for one cycle; pipelining the lookup into execute is a later optimisation
and would need the address a stage earlier than the core produces it.

### The walk

Three levels, one 64-bit read each, through the node port:

```
   W_IDLE   a miss with translation on               ──▶ W_REQ,  level 2
   W_REQ    issue the read for this level            ──▶ W_WAIT
   W_WAIT   the PTE arrives:
              !V, or W without R                     ──▶ W_FAULT
              a leaf whose superpage PPN is not
              aligned to its own size                ──▶ W_FAULT
              a leaf, R or X                         ──▶ W_DONE, the TLB is written
              not a leaf, and level > 0              ──▶ W_REQ,  level - 1
              not a leaf, and level == 0             ──▶ W_FAULT
   W_DONE   ─▶ W_DONE2 ─▶ W_IDLE   two cycles, so the TLB write is visible
   W_FAULT  raise fault and cause                    ──▶ W_IDLE
```

**The walker uses the uncached port, and that is a deadlock argument rather than
a convenience.** Page tables live in staging; staging is outside the cached
range; so a walk never enters the L1 and cannot wait on the miss that triggered
it. A blocking L1 whose miss could require a walk through the same blocking L1
has a cycle in it, and this is how the cycle is removed rather than tolerated.

**The completion is two cycles, not one.** The TLB write is non-blocking and
lands at the edge *ending* the first of them, and the array is read-first — so a
re-probe issued in that cycle still reads the old entry, misses, and walks the
whole table again. The symptom is six PTE reads per translation instead of
three, with a correct result and nothing failing.

**The walk owns its own copy of the request.** The registered request the TLB
compares against follows whatever is on the port, and the port switches to a
data access the cycle one arrives — so a fetch walk indexed by the shared copy
would finish with the *data* address's VPN slices and install that hybrid as a
translation. The walker latches the VPN, the store flag and the fetch flag on
entry and uses those.

Permission checking is the specification's: readable is `R || (MXR && X)`,
`priv_ok` is `U` in user mode and `!U || SUM` otherwise, and the access needs
`A` plus `X` for a fetch, `W && D` for a store, readable for a load. Failures
name cause 12 for a fetch, 15 for a store or AMO, 13 for a load.

### Superpages are filled as the 4 KB slice that was asked for

A leaf found at level 2 or level 1 — a gigapage or a megapage — maps a range
larger than one TLB entry can describe, and the array carries no level field to
say so. Rather than store one, the walker merges the VPN bits below that level
into the PPN and installs **the single 4 KB page the access asked for**:

```
   level 2, 1 GB leaf   ppn = { pte_ppn[hi:18], vpn[17:0] }
   level 1, 2 MB leaf   ppn = { pte_ppn[hi:9],  vpn[8:0]  }
   level 0, 4 KB leaf   ppn =   pte_ppn
```

Each 4 KB piece of a superpage therefore earns its own entry on demand, and no
entry has to record how big the mapping it came from was. **Taking the leaf's
PPN unmodified instead translates the whole superpage to its first page**,
silently and with no fault — which is what a level field is usually there to
prevent.

A superpage whose PPN is **not aligned to its own size** is a malformed table,
and the specification requires a fault rather than a truncation: the walker
raises one.

### One MMU, two requesters

Instruction fetch and the data port share one `rv64_mmu`. Sharing is what keeps
translation at 103 LUT and one block RAM in the node
([performance](performance.md#in-context-inside-the-system-node)) rather than
paying for a second TLB and a second walker — but two requesters on one blocking
structure is exactly the shape that deadlocks, so the rules that prevent it are
the contract:

1. **The data port wins.** A data access is already in execute and cannot be
   deferred; a fetch can, and is. A fetch that is stalled issues no data access,
   so the two can never be waiting on each other.
2. **A "resolved" answer is qualified by same-request.** The MMU's answer
   describes the address presented a cycle ago. Carried forward unqualified, it
   released a *data* access on the *fetch*'s hit in the cycle the port switched
   between them. A newcomer therefore never rides the previous requester's hit.
3. **A fault is tagged with its owner and holds only that requester.** A fetch
   fault does not stall a data access, and a data fault does not stall fetch.

### Fetch is translated through one page register

There is no instruction TLB. Fetch translation is **one registered mapping** —
the current page's VPN and its PPN — held in the wrapper:

```
   imem_addr[38:12] == the held VPN ?
        yes ──▶ instruction window addressed by { held PPN, addr[11:0] }
        no  ──▶ hold fetch, ask the MMU, capture the answer, resume
```

**A full instruction TLB would be paid every cycle for a lookup that almost
never changes.** Consecutive fetches share a page, so one registered translation
covers roughly a thousand instructions and refills only on the crossing. The
refill is deferred behind any data access by rule 1 above, and the fetch is
stalled anyway while it waits.

Three more cycles are held that the diagram does not predict, and none of them
is the refill:

- **one cycle after the translation lands.** The instruction array has a read
  latency of one, so on the cycle the mapping is captured it is still answering
  the *untranslated* address; the first word that belongs to the new page
  arrives the cycle after.
- **one cycle after an `SFENCE.VMA` retires.** The invalidation of the page
  register is registered — unregistered it sat in every one of these registers'
  enables at 13 logic levels — so it lands a cycle late, and fetch waits for it
  rather than reading through a mapping that is about to be dropped.
- **one cycle after a trap or a return**, because the PC has moved but `priv`
  has not — it lands a cycle later
  ([architecture](architecture.md#when-a-traps-effects-land)) — so *is this
  address translated* is momentarily stale and no fetch may use it.

#### A faulting fetch installs a poisoned page

The obvious construction — re-walk until it succeeds — never terminates, and the
core never learns anything is wrong. So a fetch whose translation faults
**installs the page register anyway, marked bad**:

1. the fetch proceeds and the instruction word is delivered;
2. the word is **decoded as a `NOP` regardless of what the page returned**,
   carrying a *faulted* flag beside it — undecoded, whatever the poisoned page
   held could issue a load or redirect fetch before the trap landed;
3. the core traps in execute with **cause 12** and `tval` set to the faulting
   PC.

The flag travels with the word through the fetch register and the decode hold,
because the array answers a cycle after the address and a held stage keeps being
handed new data ([microarchitecture](microarchitecture.md#the-instruction-word-has-to-be-captured-when-fetch-stops)).

#### Supervisor may not fetch from a user page

`SUM` relaxes **loads and stores** and never instruction fetch — that is the
architecture's rule, and it has a direct consequence for how a program is
linked: **kernel text and user text cannot share a page.** Aligning the user
function alone does not help, because the linker packs the next kernel function
in behind it.

`tests/rv64/link_sys.ld` therefore carries a page-aligned `.utext` section, and
a user-mode routine is placed in it
([programming](programming.md#user-mode-code-needs-its-own-text-pages)).

## The L1

`rv64_l1` — direct-mapped, **32-byte lines**, write-back, write-allocate, one
outstanding miss. Sixty-four lines by default, so 2 KB. It sits **behind** the
TLB and is physically tagged, so nothing in it ever sees a virtual address and
an address-space change costs it nothing.

**It is never written from outside, and that single property removes coherence
from the design.** Everything a peer can write lands in the node windows, which
are the *home* of their addresses and are uncached here. There is no
externally-written-versus-dirty-line case to construct.

**A line is one beat.** Thirty-two bytes is one 256-bit AXI beat and one flit
payload, so a fill is one request and one response and a writeback is one
descriptor and one beat. That is the second job of the cache and it does not
depend on hit rate: ordinary byte, halfword, word and doubleword accesses are
presented to software while the upstream protocol stays line-oriented.

### The arrays

| array | shape | primitive | holds |
|---|---|---|---|
| tag | 64 × 31 | LUTRAM | `{valid, dirty, tag[28:0]}` |
| data | 256 × 64 | block RAM, true dual port with byte enables | four 64-bit words per line |

**Per-line state lives in the tag word.** `valid` and `dirty` beside the tag
cost no flops and no indexed read mux; as flop arrays they cost LUT twice, once
as flops and once as the mux in front of the tag compare, which is what makes
the line count nearly free.

Port A of the data array walks a line for a fill, an eviction or a sweep; port B
is the CPU's. `linebuf` is a **rotate**, not an indexed register: a fill walks
words out in order and an eviction walks them in, so always taking the bottom
word removes a 4:1 64-bit mux from port A. The trick pays only where the
register was already written word-at-a-time — applied to a register loaded
whole, the same construction *adds* logic.

The two ports can address one word at once, which is undefined in silicon and
not merely in the model, so the array wrapper asserts on the collision unless
the instantiation declares it safe. Here it is declared safe with a reason: a
fill collides with the stalled access's own word once per fill, and **that read
is discarded** — the value is taken after the re-probe, when port A is idle —
rather than bypassed.

### Two guards that are not optional

**The tag is stale on an access's first cycle.** `probe_addr` is presented in
the same cycle as `addr` here, not a stage ahead as on the RV32 PE, so the array
still holds the *previous* index's entry. For sequential addresses the
neighbouring line carries the same tag, so without a guard the cache reports a
hit, skips the fill, and returns the previous address's data.

The guard is a *freshness* bit: the address presented now must match the address
presented last cycle, and no write can have landed in between. A hit requires
freshness, a valid line and a tag match; a **miss** requires freshness too, and
that is the point.

`stall` and `miss` are deliberately different widths: **hold on anything that is
not a confirmed hit**, including the stale first cycle, but **start a fill only
once the tag is known**. Acting on `!hit` alone begins a fill in the stale
cycle, capturing the wrong victim tag and the wrong dirty bit.

**A read immediately after a write to the same word needs an extra cycle.** The
array is `no_change`, so port B's read output *holds* while that port is
writing, and sampling it the next cycle returns the pre-write value. `wrote_q`
adds the cycle.

### The state machine

```
   L_IDLE      a miss, victim clean                ──▶ L_F_REQ
               a miss, victim dirty                ──▶ L_EV_RD
               a flush or invalidate is pending    ──▶ L_S_SCAN or L_I_SCAN

   L_EV_RD     four words out of the array, exiting on words COLLECTED
   L_EV_SEND   wb_valid and wb_ready               ──▶ L_F_REQ

   L_F_REQ     fill_valid and fill_ready           ──▶ L_F_WAIT
   L_F_WAIT    one 256-bit response                ──▶ L_F_WR
   L_F_WR      four words into the array, then write the tag
   L_REPROBE   two cycles, not one -- see below
   L_REPROBE2                                      ──▶ L_IDLE

   L_S_SCAN    read the tag at the sweep index     ──▶ L_S_TEST
   L_S_TEST    the line is dirty                   ──▶ L_S_RD
               past the last line                  ──▶ L_S_DRAIN
               otherwise, index + 1                ──▶ L_S_SCAN
   L_S_RD      four words out of the array
   L_S_SEND    wb_ready; mark the line clean and still valid, index + 1
   L_S_DRAIN   wait for wr_idle                    ──▶ L_IDLE, or L_I_SCAN

   L_I_SCAN    one line per cycle, tag cleared     ──▶ L_IDLE at the last line
```

Four details are contract-shaped rather than incidental:

- **A fill handshake tests both halves.** `fill_valid && fill_ready`, not
  `fill_ready` alone: the port's ready is an idle indicator and is already high
  on entry, so testing it alone clears the request in the same cycle it is
  raised and the port never sees it.
- **The eviction exits on words *collected*, not on the address counter.** The
  address leads the read data by a cycle, so exiting on the address sends the
  line one word short and rotated wrong.
- **The re-probe is two cycles, not one.** `tag_we` is itself a register, so the
  tag write lands a cycle after the state sets it, and the array is read-first —
  with one cycle the machine still sees the old tag, misses again, and evicts the
  line it has just filled.
- **A flush finishes on acknowledgements, not on the last beat sent.** Only the
  write response says the data is in memory, which is what `wr_idle` from the
  node port reports.

Reset does not put the machine in `L_IDLE`. It puts it in the sweep with an
invalidate pending, because the tag array has no reset and every line has to be
written clean before a lookup can mean anything.

### The dirty bit is written through the read-first window

A store hit dirties its line by writing the tag array, whose read port is
read-first, so a probe of that index in the same cycle reads the old bit.
`d_eff` ORs in a one-entry shadow of the write that is in flight, which is what
stops a just-dirtied line being evicted as clean.

## The node-port arbiter

`rv64_nport` — the one AXI master, 40-bit address, 256-bit data, shared by four
clients in fixed priority:

| priority | client | transaction |
|---|---|---|
| 1 | the page-table walker | a 64-bit read |
| 2 | the L1's fill | a 32-byte read |
| 3 | the L1's writeback | a 32-byte write |
| 4 | an uncached access | a 64-bit read or write |

**At most one is ever active, and that is what lets this be a priority mux
instead of a queue.** The invariant has three legs, and it is worth stating
plainly because it is the first thing that breaks if the L1 ever becomes
non-blocking:

- a walk runs to completion before the access that triggered it is retried,
  because the MMU holds the core while it walks;
- the L1 sequences its own eviction ahead of its own fill;
- the core stalls on any node access, so it cannot issue a second one.

No bursts anywhere: `awlen` and `arlen` are zero, `wlast` is tied high, and both
response channels are always ready. A 64-bit access is placed into its lane of
the 256-bit beat by `addr[4:3]`, and its byte strobes are shifted into that lane.

`wr_idle` — no write outstanding — is what the L1's flush sweep waits on.

## What is cached and what is not

The decode is **bit tests, not magnitude compares**, because it sits in the
stall path. Written as `>=`/`<` on 40 bits it cost 200 failing paths.

It runs on the address the MMU produced, so with Sv39 on it is the **translated**
address that decides which region an access lands in.

| region | the test on the 40-bit physical address | what that is |
|---|---|---|
| scratchpad | `pa[39:15]` equals 2 | one equality — 32 KB at `0x0001_0000` |
| control region | `pa[39:8]` equals `0x200` | one equality — 256 B at `0x0002_0000` |
| the node range | **any bit of `pa[39:28]` set** | one OR reduction — base 2²⁸ |
| cached | in the node range **and `pa[31]` set** | one bit — base 2³¹ |
| uncached | in the node range and `pa[31]` clear | the complement |

Every range is power-of-two aligned and power-of-two sized on purpose, which is
what lets each test be a single equality or a single bit rather than a pair of
40-bit comparisons.

| region | cached | why |
|---|---|---|
| the scratchpad and the control region | n/a | local arrays, one cycle |
| the node range with `pa[31]` set | **yes** | DRAM: large, re-read, private to this core |
| the node range with `pa[31]` clear | **no** | staging, the node's own registers, cross-mesh writes — the *home* of their addresses, and multi-writer |
| the aperture, `pa[39]` set | **no** | it has bit 31 clear, so the same test excludes it |

**`pa[31]` is a bit test and reads literally.** An address at 4 GB with bit 31
clear is uncached, and so is anything in the aperture. This is a convention of
the wrapper rather than a property of the machine, and a design that wants a
different split changes the test.

### Staging honours byte strobes

Staging — the 2 MB on-chip store inside MAG, reached through the aperture — is
built out of 32-byte words, and an uncached store from this processor is
**8 bytes**. Those strobes are carried the whole way:

> **A 64-bit store into staging writes its own lane of the 32-byte word and
> leaves the other three alone.**

That is what makes staging usable for the structures this core keeps there.
Without it a single store writes the same value into all four lanes, so **three
of every four page-table entries would be wrong** — and a page table in staging
is exactly what the walker reads
([above](#the-walk)). Mailbox words and completion flags have the same shape and
the same exposure.

It is worth stating as a property to check rather than assume, because it fails
quietly: the store returns, the address is right, and only the neighbours are
wrong.

### Where a cross-mesh write lands

The processor's own port is **local**: a load or store reaches this mesh's
staging, this mesh's DRAM, and no other mesh's anything. Reads never cross a
link. Moving data to another mesh is the mover's job, and the destination
address decides where it arrives:

| a remote write whose address is | lands in |
|---|---|
| **staging** — the special flag, address bit 39 | that mesh's staging, at the **full 40-bit address** |
| **DRAM** | that mesh's DRAM, by its **low 32 bits** |

The two rules are different on purpose and the difference is easy to trip over:
a staging address carries meaning above bit 32 — the mesh field selects whose
store — so truncating it would drop the very bits that named the destination.
A DRAM address does not, so its low 32 bits are the whole of it.

**Nothing about a cross-mesh write is ordered against a doorbell by itself.**
The rule that makes a write-then-announce handoff sound — wait for the mover to
report idle *before* ringing, because the ring can otherwise overtake the burst
— is [the interlink doorbell's](integration.md#the-interlink-doorbell), and it
is the one part of this a program has to get right.

The node owns this behaviour rather than the processor;
[arch/sysnode/abilities](../../sysnode/abilities.md#6-the-mover-from-the-processor)
is the reference, and the descriptor format is
[arch/sysnode](../../sysnode/README.md)'s.

Nothing that another agent writes is cached, which is the property that removes
coherence. Nothing that this core wants another agent to read should be cached
either — [what the core publishes about ordering](#what-the-core-publishes-about-ordering).

## What the core publishes about ordering

The compute-unit shell that `rv64_syscore` does not have carried a guarantee
with it: *every write this unit made is visible when its completion arrives.*
Dropping the shell means the core has to publish its own. Here is what the
memory path actually supports today.

**Rule 1 — one access at a time, in program order.** The core stalls in execute
for the whole of every access, so two accesses never overlap and they reach the
node port in the order the program issued them. `FENCE` is a NOP because there
is nothing weaker to strengthen.

**Rule 2 — an uncached store is in memory before the next instruction retires.**
The uncached path completes on the AXI write response, not on the address being
accepted, and the core is held until then. This is the strong guarantee, and it
is the one to build on.

**Rule 3 — an uncached load returns data no older than every store before it**,
by rules 1 and 2.

**Rule 4 — a cached store is *not* in memory, and there is no way to put it
there.** It lands in the L1 and reaches DRAM only when its line is evicted. The
L1's `flush` and `inval` inputs exist and **the wrapper ties both to zero**, so
software has no cache maintenance at all: no flush, no invalidate, no way to
make a dirty line visible or to drop a stale one.

The practical rule that follows, and it is the one to write software against:

> **Anything another agent will read must be written through the uncached
> range.** Descriptors a mover will fetch, doorbells, mailbox words, completion
> flags. The cached range is for this core's own working set and nothing else.

That is also why staging is uncached rather than merely happening to be: it is
where the cross-agent structures live.

**The obligation is not discharged.** For the cached range the core cannot yet
publish anything equivalent to what the shell provided, and that is a gap rather
than a decision. What is missing is a software-reachable flush and the ordering
statement that would be built on it.

## What this memory system deliberately does not do

- **No coherence, and no mechanism that would need it.** Cached memory is never
  written from outside; externally written memory is never cached.
- **No hit-under-miss, no MSHRs, no load/store queue.** One outstanding miss,
  and the core is stalled for it. A non-blocking L1 is the single largest lever
  on the fabric-latency numbers in [performance](performance.md#the-fabric-latency-sweep),
  and its miss-status file is not free — it also ends
  [the arbiter's invariant](#the-node-port-arbiter) and would need real
  arbitration and per-client response routing.
- **No write buffering and no write combining.** An uncached store waits for its
  response.
- **No cache maintenance instructions.** See
  [what the core publishes about ordering](#what-the-core-publishes-about-ordering).
- **No instruction cache.** Fetch reaches the local instruction window directly,
  through [the fetch page
  register](#fetch-is-translated-through-one-page-register) when translation is
  on. There is nothing to cache: the window is on-chip and answers in a cycle.
- **No instruction TLB.** One page register, refilled on a page crossing, and it
  is the data port that owns the MMU when the two want it at once.
- **No superpage TLB entries.** A superpage is walked and mapped correctly, but
  as one 4 KB entry per slice, so a 2 MB mapping costs 512 entries rather than
  one and a scan across it refills the TLB continuously.
- **No unmapped-address fault.** The wrapper answers every address; a local
  access outside the scratchpad and the control region aliases onto the
  scratchpad ([architecture](architecture.md#what-is-deliberately-absent)).
- **No second port for a peer to write.** Unlike the RV32 PE's scratchpad, which
  is a true dual port with a cross-port bypass so a doorbell can land in the word
  a poll loop is reading, this core's scratchpad has one write port shared
  between the host window and the core. A doorbell reaches it through the
  control region's interrupt line, not through memory.
