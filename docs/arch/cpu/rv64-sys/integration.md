---
title: RV64 system core integration
summary: The two wrappers — the mesh compute unit with its loader and kick, and the node processor that replaces the compute-unit shell — why the shell is gone, and the node complex the processor sits in.
tags:
  - architecture
  - cpu
  - rv64
  - sysnode
---

# Integrating the RV64 system core

`rv64_core` has no fabric interface, no memory map and no way to be started. All
three come from a wrapper, and there are two — plus a third module that is not a
third configuration but the node complex the second one sits inside.

| top | what it is | attaches to |
|---|---|---|
| `rv64_sys_pe` | the core as a **compute unit** the framework recognises | one fabric port |
| `rv64_syscore` | the core as the **system node's processor** | an AXI slave window from the host, one AXI master onto MAG, and one flit port on the node's hub |
| `rv64_mag_pe` | `rv64_syscore` **plus the node's mover and transform slot** | the same, plus the mover's own master |

Terms, defined once. A **flit** is the 288-bit unit the fabric carries; a
**compute unit** is anything that attaches to one fabric port, takes
instructions one at a time and signals retirement; a **kick** is the instruction
that starts one; a **completion** is the flit it sends back. **MAG** is the
system node's memory access half. The **mover** is the node's
descriptor-walking memory engine, and the **transform slot** is the addon
position on the mover's read-return path. A **doorbell** is a word one agent
writes to make another notice. **Staging** is on-chip memory inside MAG shared
by the mover and the inter-mesh link.

## Why the node processor has no compute-unit shell

`noc_cu_base` is the framework's compute-unit shell: it owns the fabric port,
the instruction queue, the `CU_CTRL` registers and the kick-and-complete
handshake. Every compute unit on the fabric has one. `rv64_syscore` does not,
and the reasons are ordered by weight — **area is the least of them**, since the
shell measures 756 LUT against a whole-node budget of 35,000.

**Lifecycle.** The shell implements *someone kicks me, I run to completion, I
report a 32-bit word*. That is a batch compute unit. A runtime boots once and
runs forever: there is no completion to report, and `exec_result` has nothing to
carry. Building `K_RUN → K_DONE` around a program meant never to end is the
wrong shape, and every diagnostic that lives inside it inherits the wrong shape
too.

**Deadlock, and the cycle is specific.** The node processor is the unit that
*services* the fabric: it dispatches to compute units and consumes their
completions. Behind the shell its inbound path is gated by `noc_in_busy` from
finite instruction and receive queues, and its dispatch shares one output port
with the shell's own signal and control traffic. So:

```
   processor blocked sending a dispatch
        └─▶ stops draining its receive queue
              └─▶ the queue fills
                    └─▶ noc_in_busy
                          └─▶ the completions it needs to make progress
                              cannot land
                                └─▶ it stays blocked
```

**The unit that arbitrates the fabric must not be flow-controlled by the
fabric.** A compute unit can afford to block; the scheduler cannot.

**The loader is a second memory-write protocol.** MAG already provides a memory
path and the host already reaches the card over AXI into MAG. Loading the
instruction window by AXI write plus a doorbell needs no loader state machine,
no buffer-id map, no bounds check and no receive-quiet interlock — all of which
exist only because the image arrives as flits.

### What replaces it, and what is owed

| the shell provided | the node processor's answer |
|---|---|
| the fabric port and the instruction queue | **a mailbox in the control region** — the processor is a client of the node's hub with no shell around it, and dispatch is a store rather than a lifecycle ([below](#the-dispatch-mailbox)) |
| the image loader | the host writes the memories through an AXI slave window |
| the kick | a boot register in that window |
| the completion | a status register the host polls, plus the exit word |
| **"every write is visible when the completion arrives"** | **not discharged for the cached range** — [memory-system](memory-system.md#what-the-core-publishes-about-ordering) |

That last row is a real obligation taken on, not a free removal.

The first row is the one that changed most. Dropping the shell dropped the only
path onto the fabric with it, and what replaced it is deliberately *not* a
smaller shell: a mailbox has no lifecycle, so the processor can command units it
did not start and consume completions for work it did not dispatch.

## `rv64_sys_pe` — the mesh compute unit

The core wrapped so the framework recognises it: the image arrives over
`CU_DATA` flits, the kick over `CU_INST`, the result leaves on `CU_SIGNAL`.

```
   noc_cu_base ── CU_INST ──▶ kick FSM ──▶ core reset release
               ── CU_DATA ──▶ loader ────▶ instruction window / scratchpad
               ◀─ CU_SIGNAL ── completion, carrying the exit word
```

| | |
|---|---|
| `CU_TYPE` | `0x5236` |
| buffers | 4 |
| instruction window | `IMEM_WORDS × 32 bit` — 4096 words, 16 KB by default |
| scratchpad | `SPAD_WORDS × 64 bit` — 2048 words, 16 KB, byte-writable |
| instruction queue / receive queue | 16 / 32 entries |

The unit does not emit flits of its own: the shell's send path is tied off, so
there is no `CU_CTRL` reply and no peer message. What it publishes is the
`CU_CAPS` set `noc_cu_base` provides by default, plus its cycle and retire
counters on the shell's debug pair.

### The loader

`CU_DATA` carries a buffer id, an offset and a length; the id chooses what the
payload means:

| `buf_id` | payload | one flit writes |
|---|---|---|
| 0 | scratchpad **granule** | 4 × 64-bit words |
| 1 | instruction window **granule** | 8 × 32-bit words |
| 4 | scratchpad **word** | one 64-bit word |
| 5 | instruction window **word** | one 32-bit word |
| 3 | reserved | rejected |
| anything else | — | rejected |

A **granule** is 256 bits, spooled one word per cycle rather than written as a
wide port, which is what keeps both memories at their natural width and off any
wide write path.

A rejected transfer is **consumed and dropped**, not written somewhere. The
bounds check is expressed in granule units for both forms, so a
word-granularity write reaches only the first `IMEM_WORDS/8` words of the
instruction window or the first `SPAD_WORDS/4` words of the scratchpad; the
granule forms reach all of both.

### The interlock that must not be removed

`CU_INST` and `CU_DATA` arrive on **two queues**, so a kick can overtake the
image it is the doorbell for. The kick machine waits on **receive-quiet** — no
pending receive, no granule in flight, loader idle — before it accepts one. The
core is additionally **held in reset until the boot pulse**, so no instruction
is fetched before the image is complete. The hold clears by the unit's own
progress and cannot deadlock.

### Kick and completion

| step | |
|---|---|
| `K_IDLE` | accept a `CU_INST` once receive-quiet |
| `K_START` | opcode 1 pulses boot and enters `K_RUN`; **any other opcode retires immediately without running anything** |
| `K_RUN` | until the core halts, or the host asserts `halt_req` |
| `K_DONE` | send `CU_SIGNAL` with the exit word; set the fault flag if the halt cause was 2 or 3 — `EBREAK` or a fault |

**The kick's start PC and argument word are latched and not used.** The core's
reset PC is a module parameter fixed at 0, so a program always starts at address
0 and receives no argument. A caller that needs to pass one writes it into the
scratchpad before the kick.

The cycle and retire counters clear on the **boot pulse**, not on the core's
reset. The core is put back into reset at `K_DONE`, and counters cleared there
would read zero to whoever asked for them.

### The memory map and the control region

Memory here is **Harvard and local**: fetch reaches the instruction window,
every load and store reaches the scratchpad or the control region, and there is
no path off the unit. A program built for this wrapper needs `link_pe.ld`, not
the flat standalone map — [programming](programming.md#the-link-maps).

| offset from `CTRL_BASE` | |
|---|---|
| `0x00` | **exit** — a store ends the run and its low 32 bits become the completion argument |
| `0x08` | console byte, observation only |
| `0x10` | doorbell — writable, and bit 0 reaches the core's software interrupt line |

**The control region has one readable value.** Every read of it returns the
doorbell bit, whatever the offset; the read is registered, and the select with
it, because as a combinational mux it sat inside the core's load path and cost
the unit 52 MHz against the core alone. The scratchpad is a one-cycle read
anyway, so making the control region match it costs nothing.

### What this configuration does not have

- **No fabric memory requestor.** No fill, no writeback, no push, no dispatch. A
  load outside the scratchpad **goes nowhere** — it aliases onto the scratchpad
  ([architecture](architecture.md#what-is-deliberately-absent)).
- **No MMU and no L1.** Neither is instantiated; the configuration pays for
  neither.
- **No `CU_CTRL` emitter and no inbound control class.** A dropped `CU_CTRL`
  reply is the current behaviour and it is silent.
- **Nothing remote can ring the doorbell.** The register exists and reaches the
  interrupt line; no flit reaches the register.

## `rv64_syscore` — the node's processor

```
   host AXI ──▶ slave window ──┬──▶ instruction window   (write only)
                               ├──▶ scratchpad          (write only)
                               └──▶ control registers   (read and write)

   fetch ──▶ page register ──▶ instruction window
                  ▲
                  └── refill ──┐
                               │   one MMU, and the data port wins it
   core ──▶ MMU ──▶ decode ──┬─┴──▶ scratchpad / control region
                             ├──▶ L1        ──┐
                             └──▶ uncached  ──┴──▶ node port ──▶ MAG
                                                  ▲
                             page-table walker ───┘

   control region ──▶ dispatch mailbox ──▶ the node's hub ──▶ the mesh
                  ──▶ mover config window
                  ──▶ interlink doorbell window
```

### The host window

`hs_addr[31:28]` selects: **0** the instruction window as 32-bit words, **1**
the scratchpad as 64-bit words with byte strobes, **2** the control registers.
The window is always ready.

**It is write-only for the memories.** The read-data register is a case on the
low address byte regardless of the selector, so reading the instruction-window
or scratchpad selector returns control-register values. Read-back of an image is
not available.

| offset | register | |
|---|---|---|
| `0x00` | `HR_BOOT` | write 1: pulse boot and enable running |
| `0x08` | `HR_PC` | **latched and unused** — the core's reset PC is a parameter fixed at 0 |
| `0x10` | `HR_DBELL` | doorbell; bit 0 reaches the core's software interrupt line |
| `0x18` | `HR_STATUS` | `{exited, halted, cause[1:0]}` |
| `0x20` | `HR_EXIT` | the program's exit word |
| `0x28` | `HR_HALTPC` | where it stopped |
| `0x30` | `HR_CYCLES` | 64-bit cycle counter, cleared on boot |
| `0x38` | `HR_RETIRED` | 64-bit retire counter, cleared on boot |

Both counters are 64-bit, unlike the shell's 32, because a runtime runs long
enough to wrap 32.

**The halt state is latched, and it has to be.** Running is enabled by
`HR_BOOT` and dropped the moment the core halts, and dropping it takes the core
back into reset — which clears the core's own `halted` output. A status register
reading that output directly reports nothing at all. `halt_l`, `cause_l` and
`haltpc_l` hold the answer until the next boot. The general form is worth
keeping: **a diagnostic that lives inside the thing being reset is not a
diagnostic.**

### The node port

`cp_*` — one AXI master, 40-bit address, 256-bit data, one outstanding access,
no bursts. It is deliberately the same shape the RV32 control processor
presents, so the RV64 complex drops into the same socket inside the node.
A 64-bit access is placed into its lane of the beat by `addr[4:3]`
([memory-system](memory-system.md#the-node-port-arbiter)).

### The control region

256 bytes, and it is where everything that is not memory lives. Reads are early
and writes are registered, for the timing reason in
[memory-system](memory-system.md#registering-every-address-consumer).

| offset | write | read |
|---|---|---|
| `0x00` | **exit** — latch the 64-bit word, halt the core, set `exited` | 0 |
| `0x08` | console byte, observation only | 0 |
| `0x10` | — **the core cannot ring its own doorbell**; the host writes it through `HR_DBELL` | the doorbell bit |
| `0x18` | — | `satp`, a **read-only mirror** of CSR `0x180` |
| `0x20` | — | mover status: `[32]` busy, `[31:28]` fault code, `[27:0]` descriptors completed |
| `0x28` | — | the **inbound doorbell counts**, four 16-bit lanes, mesh 0 in `[15:0]` |
| `0x40`–`0x7F` | the **dispatch mailbox**, one register per 8-byte slot | the mailbox |
| `0x80`–`0xBF` | one **mover config** register per 8-byte slot | 0 |
| `0xC0`–`0xFF` | the **interlink config** window, one register per 8-byte slot | 0 |

**`mv.go` is a store, not an opcode.** Decoding the mover's command window out
of an address range keeps the ISA unchanged and matches the framework rule that
control is a range rather than an instruction. The register index is the low six
bits of the offset, so the byte offset inside the window *is* the config
address; the descriptor a program builds in its own memory becomes seven stores,
in program order, and program order is the queue.

**The `satp` mirror is read-only on purpose.** `satp` is a supervisor CSR and
supervisor software owns the value; the mirror exists so a host can read the
translation root without stopping the core. Two writable copies of it would
disagree the moment either was written alone.

### The dispatch mailbox

`rv64_noc_mbox`, at control offset `0x40`. It is the processor's whole
connection to the mesh, and it is a **client of the node's hub** rather than a
port of its own — the complex answers at coordinate **(0,0)**, a corner, so it
costs no attachment point.

```
   store DST, ARG0, ARG1, then GO ──▶ hardware builds a CU_INST flit ──▶ hub
                                                                          │
   read STAT / HEAD, write POP ◀── 16-deep queue ◀── CU_SIGNAL ◀──────────┘
                     │
                     └──▶ a non-empty queue raises the external interrupt
```

| index | offset | register | direction |
|---|---|---|---|
| 0 | `0x40` | `DST` | write — x in bits 3:0, y in bits 11:8 |
| 1 | `0x48` | `ARG0` | write — payload `[63:0]` |
| 2 | `0x50` | `ARG1` | write — payload `[127:64]` |
| 3 | `0x58` | `ARG2` | write — payload `[191:128]` |
| 4 | `0x60` | `ARG3` | write — payload `[255:192]` (opcode at `[255:252]`) |
| 5 | `0x68` | `GO` | write — build the whole 256-bit payload and send |
| 6 | `0x70` | `STAT` | read — `[4:0]` queued, `[15]` a flit still offered, `[31]` sticky overflow |
| 7 | `0x78` | `HEAD` | read — the oldest completion; write — drop the head |

Four properties are contract, and each is a consequence of the fabric rather
than a choice about registers:

- **Hardware composes the flit, not software.** A flit is 288 bits against a
  64-bit store port. Software sets `DST` and the four payload words at leisure,
  and `GO` builds the routing header and commits — the whole 256-bit payload,
  any opcode to any node, with no tearing window.
- **An inbound flit is always accepted, even when the queue is full.** Held
  instead, it would sit at the head of the hub's queue and stall the link for
  everything behind it — including the traffic that would drain the queue.
  Overflow is therefore a **sticky bit** rather than backpressure, and reading
  it is how software tells a dropped completion from a unit that never finished.
- **An offered flit is held until the fabric takes it.** Withdrawing one
  destroys it, and the loss is silent at every point downstream.
- **Popping is a write.** The control region answers reads from a register a
  cycle later, so a read-triggered pop would have to guess which cycle the read
  really happened on.

The register map as software sees it, with an example, is
[programming](programming.md#dispatching-work-to-a-compute-unit).

### The interlink doorbell

The window at control offset `0xC0` is the processor's reach into the
interlink: it rings a doorbell in another mesh, and it reads and clears the
rings that arrive here.

| index | offset | fields |
|---|---|---|
| 0 | `0xC0` | `[0]` enable — set at reset; `[1]` clear the inbound counts; `[2]` clear faults |
| 1 | `0xC8` | `[1:0]` this node's mesh id, defaulting to its `MESH_ID` |
| 2 | `0xD0` | `[1:0]` destination mesh, `[15:8]` transaction tag — **the write is the ring** |

**The window's offsets are shifted into the interlink's own address space.**
The interlink claims a config write only at `0x80` and above, so the wrapper
sends `0xC0 + n` on as `0x80 + n`: offset `+0x00` reaches the interlink's
`0x80`, `+0x08` its `0x88`, `+0x10` its `0x90`. That translation is the whole of
the wrapper's involvement — and with the wrong high bits every doorbell write
was accepted by the control region and then dropped by the interlink, which is
the shape of bug a register window makes easy to build and hard to see.

**Inbound, a ring is a counted level.** Each arriving doorbell increments the
count for its source mesh; the counts are read at control `0x28` and cleared
through `0xC0` bit 1. **While any count is non-zero the external interrupt line
is held up**, so a ring that arrives while another is being serviced is not
lost — and the handler must clear the counts, or it re-enters forever.

**A ring is not a release fence on its own.** The sending arbiter rotates
between writes, flits and doorbells, so **a ring issued while a burst is still
leaving can overtake it** and arrive ahead of the data it is announcing.

The handoff is correct only when both of these hold, and software supplies the
first:

1. **the sender waits for the mover to report idle** — `MV_STAT[32]` clear —
   which is the point at which every write packet has been accepted onto the
   link, and the link delivers in order;
2. **the receiving interlink holds an inbound doorbell** until every write that
   arrived ahead of it has been acknowledged by its memory.

Fact 2 is the hardware's and needs nothing from software. Fact 1 is not: skip
the wait and the ring races the burst, with nothing to report the loss. The
sequence is therefore **write, wait for idle, ring** — never write-and-ring.

**The host and the processor share this window, and the host wins a same-cycle
collision.** It is a debug path; the processor retries.
[arch/sysnode/abilities](../../sysnode/abilities.md#7-the-interlink-and-doorbells)
is the node-level reference.

### Boot

```
   1. write the image      selector 0, word by word          (instruction window)
   2. write the data       selector 1, with byte strobes     (scratchpad)
   3. write HR_BOOT = 1    releases the core's reset one cycle later
   4. poll HR_STATUS       exited, halted, cause
   5. read HR_EXIT, HR_HALTPC, HR_CYCLES, HR_RETIRED
```

There is no receive-quiet interlock here and none is needed: the host writes the
memories and then writes the boot register through the same ordered AXI slave.

## The node complex — `rv64_mag_pe`

`rv64_syscore` is the processor. `rv64_mag_pe` is the processor **plus the
node's memory mover and the transform slot on its read-return path**, and the
distinction matters for every area argument made about it: the RV32 and RV64
complexes hold the *same* three things and differ only in the processor. The
mover and the slot belong to the node, not to whichever CPU sits in it.

```
   rv64_mag_pe
     ├── rv64_syscore     the processor, with its own AXI master onto MAG
     ├── mm_mover         the node's descriptor engine, its own AXI master
     └── mag_xform        the transform slot, on the mover's read return
```

**The processor wins the config port.** The mover's configuration is reachable
from two places — the processor's control-region window and the host's `aux`
window — and when both pulse in one cycle the processor's address and data are
taken. The `aux` window splits at offset `0x80`: below it belongs to the mover,
at or above it to the interlink. Without the interlink the gate is a constant
and the mover sees every write, as it always has.

**The transform slot's own config port is tied off here.** `mag_xform` is
instantiated with `cfg_en` low, so its configuration path is unreachable from
this complex — and synthesis strips it, which is why an area figure for this top
is smaller than the same instance measured inside the node
([performance](performance.md#as-a-sub-hierarchy-inside-a-larger-synthesis)).

## What is wired at the node

`rv64_mag_pe` is instantiated inside `sysnode` behind a parameter, and **the
parameter is off by default**: `CPU_RV64` is 0, so a node built without asking
for it ships the RV32 control complex and the RV64 path is a parameter swap
rather than the shipped configuration.

Every port of the complex is now connected in that branch:

| at `sysnode` | RV64 branch |
|---|---|
| the host window `hs_*` | **connected** |
| the processor's memory path `cp_*` onto MAG | **connected** |
| the mover's master `mv_*`, and its status | **connected** |
| the host's `aux_cfg_*` config path and the interlink gate | **connected** |
| console debug, and the node's busy line | **connected** |
| the hub's compute-unit port — `pe_tx_*`, `pe_rx_*` | **connected**, to the dispatch mailbox. The complex sits at `(0,0)` |
| the interlink doorbell — `db_en`, `db_addr`, `db_data`, `db_status` | **connected.** `mag` takes a second config writer beside the host's, and `mag_ilink` exports the four inbound doorbell counts |
| `irq_summary`, the external interrupt line | **connected**, carrying a mover fault, a host halt request, and a registered *any inbound doorbell count is non-zero* |
| `pe_status` | **connected**, reporting busy and fault |

So in the RV64 configuration as it stands the processor boots, runs, reaches
DRAM and staging through MAG, commands the mover, dispatches to compute units
and takes their completions as interrupts, rings and reads interlink doorbells,
and reports to the host.

Three details of that wiring are worth having, because each is a rule rather
than a connection:

- **The host wins a same-cycle collision on the interlink's config window.** It
  is a debug path, and the processor retries; the alternative is arbitration on
  a path neither party contends for in practice.
- **`db_status` is zero without the interlink.** With `ILINK = 0` there are no
  doorbells to count, so the register reads zero rather than being absent.
- **The external interrupt is an OR of three node conditions and one of the
  processor's own**: a mover descriptor that faulted, a host halt request, **an
  inbound interlink doorbell** — registered, and held while any count is
  non-zero — and a non-empty completion queue in the mailbox. All four are
  things a scheduler must react to rather than poll for, and because the line is
  shared a handler has to work out which of them raised it.

[arch/sysnode](../../sysnode/README.md) owns the node-level picture; this page
stops at the processor's own boundary.

## Parameters

Defaults are what is measured. Nothing here changes behaviour software can see
except the two window sizes and the two base addresses.

### `rv64_core`

| | default | |
|---|---|---|
| `RESET_PC` | `0` | both wrappers leave it at 0 |
| `MEM_PRIM` | `distributed` | the register file's primitive — a measured trade, [microarchitecture](microarchitecture.md#why-the-register-file-is-lutram-and-not-block-ram) |
| `HAS_ATOMIC` | `1` | 0 constant-propagates the whole AMO sequencer away. **Both wrappers leave it on** |
| `PADDR_W` | `40` | the physical address width, which sizes `satp.PPN`: bits above it are WARL zero. The card is a 40-bit machine and this is why a TLB entry fits a block-RAM port ([memory-system](memory-system.md#an-entry-is-57-bits-because-the-card-is-40-bit)) |

**Dropping atomics is worth 776 LUT — 13.3 % of the core — at essentially no
change in frequency.** A mesh compute unit may take that trade: it has no second
writer to race. The node processor may not, and the reason is not preference:
staging is multi-writer but single-reader, which makes it a mailbox rather than
shared memory. It gives join and release and never mutual exclusion or a shared
counter, so without the A group the machine cannot express a multi-writer
location outside DRAM at all.

### `rv64_sys_pe`

| | default | |
|---|---|---|
| `FLIT_WIDTH`, `POS_WIDTH`, `POS_X`, `POS_Y` | 288, 4, 2, 2 | the fabric's, not this unit's |
| `IMEM_WORDS`, `SPAD_WORDS` | 4096, 2048 | **must match the link script** — changing one silently truncates the image at the loader's bounds check |
| `INST_DEPTH`, `RECV_DEPTH` | 16, 32 | the shell's queues |
| `SPAD_BASE`, `CTRL_BASE` | `0x0001_0000`, `0x0002_0000` | honoured by the decode |
| `MEM_PRIM` | `block` | the instruction window |
| `SPAD_STYLE` | `ultra` | measured: `ultra` 289.9 MHz / 6,962 LUT / 1 URAM against `block` 280.9 / 7,007 / 10 BRAM. UltraRAM wins on the byte-write-enable path |
| `RF_PRIM` | `distributed` | passed to the core's `MEM_PRIM` |

### `rv64_syscore` and `rv64_mag_pe`

| | default | |
|---|---|---|
| `ADDR_W`, `DATA_W` | 40, 256 | the card's physical address width and the node's beat |
| `FLIT_WIDTH`, `POS_WIDTH` | 288, 4 | the fabric's, not this processor's; they size the dispatch mailbox's flit and its coordinate fields |
| `IMEM_WORDS`, `SPAD_WORDS` | 8192, 4096 | 32 KB each; must match `link_sys.ld` |
| `L1_LINES` | 64 | 32-byte lines, so 2 KB |
| `TLB_ENTRIES` | 32 | direct-mapped |
| `SPAD_BASE`, `CTRL_BASE` | `0x0001_0000`, `0x0002_0000` | honoured by the decode |
| **`NODE_BASE`, `CACHE_LO`** | `2^28`, `2^31` | **not honoured.** The decode is `\|pa[39:28]` and `pa[31]` with the bit positions written literally, so changing either parameter changes nothing. Treat both as documentation and edit the tests |
| `XFORM_SLOTS`, `XID_W`, `XMODE_W`, `XFORM_IN_BITS`, `XFORM_OUT_WORDS` | 1, 4, 4, 2048, 4 | the transform slot's, on `rv64_mag_pe` only |

`sysnode` selects between the two control complexes with **`CPU_RV64`, which
defaults to 0** — the RV32 path stays byte-identical unless a build asks for the
swap. `PE_IMEM`, `PE_SPAD` and `PE_L1_LINES` pass through to whichever is
generated.

## What integration deliberately does not provide

- **No dispatch out of the mesh compute-unit configuration.** `rv64_sys_pe`'s
  send path is tied off: it receives its image and its kick and answers with one
  completion, and that is the whole of its fabric traffic. The mailbox is on
  `rv64_syscore` only.
- **No queue of outbound dispatches.** The mailbox holds **one** flit at a time.
  A scheduler that wants a backlog keeps it in memory and feeds `GO`.
- **No flow control on completions.** The queue is 16 deep, an overflow is
  reported and not prevented, and the fabric is never held to make room —
  [above](#the-dispatch-mailbox).
- **Nothing remote can ring the mesh unit's doorbell.** `rv64_sys_pe`'s register
  exists and reaches the software interrupt line; no flit reaches the register.
- **The processor cannot raise its own *software* interrupt from the control
  region.** `rv64_syscore`'s doorbell at `0x10` is written by the host through
  `HR_DBELL` and only read by the core. Ringing *another mesh* is a different
  window and a different line — [the interlink
  doorbell](#the-interlink-doorbell) — and it works.
- **No reset PC and no kick argument.** Both wrappers latch one and use neither;
  programs start at 0.
- **No image read-back.** The host window's memory selectors are write-only.
- **No second processor per wrapper.** One core each, and the reservation
  machinery assumes it.
- **No clock-domain crossing.** Every port on all three tops is synchronous to
  one clock; crossing to the host or to DRAM is the AXI surface's job
  ([arch/axi](../../axi.md)).
