---
title: Node queue
summary: How a host hands work to a node's dispatcher and gets results back — the boot block, the queue region in card memory, the submission and completion rings, the stdio rings, the package heap and the node's region heaps, all polled.
tags:
  - spec
  - normative
  - software
  - dispatcher
---

# Node queue

> **Kind: Fixed** throughout. The ring depths, the region's address and the
> heap's size are parameters the host chooses (§3); the layout around them is
> not.

A node's RV64 processor runs the **dispatcher firmware**: it takes
[packages](package-format.md) from a submission ring, runs them against the
node's compute units, and posts a completion for each. The host only writes
submissions and reads completions. **Both sides poll** — the host the completion
tail, the firmware the submission tail; nothing in the protocol needs an
interrupt.

The firmware side is `software/firmware/kohakuaccel/queue/service.c` with the
layout in `software/firmware/kohakuaccel/include/ka/queue/layout.h`; the host
side is `software/driver/kohakuaccel/driver/node/queue/` (`NodeQueue`), with
the layout in `layout.py`.

## 1. Addresses

Every address in this protocol is **unit-global** — mesh in `[37:36]`, as a
compute unit's instruction carries it ([memory-protocol.md](memory-protocol.md))
— so the host, the firmware and every package name a byte the same way. The
firmware reaches the region through its node port, **uncached**, by setting
bit 38 of the address (the processor's uncached alias,
[control-registers.md](control-registers.md) §7), so it never reads a stale
copy of what the host wrote.

**The region belongs in the node's staging store.** A staging address is
`1 << 39 | mesh << 36 | offset` (aperture 0); the host reaches it through that
mesh's memory window with the address whole — `window | address`, e.g.
`(mesh + 1) << 40 | 1 << 39 | mesh << 36 | offset` on the v8 boards — and the
processor at the same unit-global address. Staging is on-chip, honours byte
strobes from both sides, and is never cached. A region in DRAM works the same
way; the address is the boot block's, so where it lives is the host's choice.

## 2. The boot block

Before the processor is released from reset the host writes this block to the
first bytes of its scratchpad (`0x1_0000` in the processor's map; the load
window's region 1, offset 0). The firmware image reserves the space as its
first section and refuses to run when the magic is wrong.

| Word | Field |
|---|---|
| 0 | magic `0x31544F4F42414B` ("KABOOT1") |
| 1 | version, `2` |
| 2 | the queue region's unit-global address |
| 3 | this node's mesh index |
| 4 | `scan` — with `n_units` zero, the firmware enumerates coordinates `0..scan` in x and y |
| 5 | `timeout` — cycles any one wait may take before the package fails |
| 6 | `cq_depth` — a node-wide bound on outstanding completions, `0` for none ([package-format.md](package-format.md) §4.1) |
| 7 | flags: `[0]` `SERIAL`, send each dispatch step whole rather than round-robin ([package-format.md](package-format.md) §4); `[1]` `TIMING`, print each package run's phase cycles to stdout after it ends; `[2]` `NOFETCH`, ignore the packages' fetch ports and send every payload through the mailbox ([package-format.md](package-format.md) §4.7); `[3]` `STEPS`, print each step's end cycle; `[4]` `NOENGINE`, run every package on the firmware path even where a dispatch engine exists ([package-format.md](package-format.md) §4.8) |
| 8 | `n_units`, at most 16; `0` asks the firmware to find its own |
| 9–24 | the units, as unit words ([package-format.md](package-format.md) §1.2) |

The unit table is what the firmware computes the machine signature over. Given
here, it is the host's; with `n_units` zero the firmware sends a `CU_CTRL` read
of `CU_CAPS` to every coordinate in `0..scan` (its own, `(0, 0)`, excepted)
through its mailbox and keeps each coordinate that answers **from that
coordinate** — the router clamps an out-of-grid address onto a real unit, which
answers with its own source. Either way the table is published in the queue
region (§3) before the firmware reports ready.

## 3. The queue region

The host allocates it, page-aligned, in the node's DRAM, writes the header
(§3.1) and zeroes every index **before** booting the firmware. The firmware
resumes the indices it finds, so a restarted firmware continues a queue the host
still holds.

**One writer per 32-byte line.** The host writes card memory in 32-byte
granules and zero-fills whatever a write does not carry; two writers in one line
would erase each other. Every field below therefore sits alone at the start of
its own line, and the host always writes whole lines. The processor's 8-byte
uncached stores honour byte strobes, so the firmware may write single words.

| Offset | Writer | Words |
|---|---|---|
| `0x000` | host | magic `0x314555455551414B` ("KAQUEUE1"), version `2`, `sq_n \| cq_n << 16`, `so_bytes \| si_bytes << 32` |
| `0x020` | firmware | state (0 none, 1 ready, 2 stopped, 3 fatal), ABI version, entries completed, fatal code |
| `0x040` | host | SQ tail — entries submitted, ever |
| `0x060` | firmware | SQ head — entries consumed |
| `0x080` | firmware | CQ tail — completions posted |
| `0x0A0` | host | CQ head — completions read |
| `0x0C0` | firmware | stdout written (bytes, ever), stdout bytes lost |
| `0x0E0` | host | stdout read |
| `0x100` | host | stdin written |
| `0x120` | firmware | stdin read |
| `0x140` | firmware | the number of units served; the unit words follow from `0x160`, one per 8 bytes, room for 16 |
| `0x1E0` + 32·r | firmware | node heap `r` (r = 0..3, §7): free bytes, largest free block, `live \| table entries << 32`, failed requests; all 0 while unconfigured |
| `0x280` | host | the SQ: `sq_n` entries of 64 bytes |
| then | firmware | the CQ: `cq_n` entries of 32 bytes |
| then | firmware | stdout ring, `so_bytes` (a multiple of 32) |
| then | host | stdin ring, `si_bytes` (a multiple of 32) |

Indices count entries or bytes **ever**, never wrapped; a slot is the index
modulo the ring's size. A ring is empty when its two indices are equal and full
when they differ by its size.

### 3.1 Submission entry — 64 bytes, written whole

Word 0 is `[7:0]` the opcode and, for `RUN`, `[31:16]` the number of bindings;
word 1 is the tag, returned in the completion. Words 2–7 depend on the opcode:

| Opcode | Word 2 | Word 3 | Word 4 | Word 5 | Word 6 | Completion value |
|---|---|---|---|---|---|---|
| 0 `NOP` | | | | | | 0 |
| 1 `RUN` | the package's unit-global address | its length in bytes | the bindings' address (one 64-bit address per buffer), or 0 | per-wait timeout in cycles, 0 for the boot block's | flags: `[0]` verify the checksum, `[1]` raise the host interrupt once the completion is posted (control-registers.md §7.2 `R_IRQ`; a host that cannot take one leaves it clear) | §3.2 |
| 2 `STOP` | the exit value | | | | | 0 |
| 3 `HEAP` | region `r` | base | bytes, 0 retires the region | granule, 0 for 64 | | 0 |
| 4 `ALLOC` | region `r` | bytes | alignment, 0 for the granule | tag | | the block's address |
| 5 `FREE` | region `r` | the block's address | | | | 0 |

Unused words are 0. A heap entry's completion carries the region in its detail
and the cycles the firmware spent on it.

The host writes the entry, then the SQ tail. The firmware reads the entry, runs
it, posts its completion and only then advances the SQ head, so a slot is free
for reuse once the head has passed it.

### 3.2 Completion entry — 32 bytes

| Word | Field |
|---|---|
| 0 | tag |
| 1 | `[15:0]` status, `[31:16]` detail, `[63:32]` step index |
| 2 | node cycles from the package's first read to this completion (`RUN`) |
| 3 | `RUN` that succeeded: instructions dispatched. Failed: the failure's value. `SIGNAL`: the step's 64-bit value |

The firmware writes the entry, then the CQ tail; each uncached store completes
before the next is issued, so a host that reads the tail first and the entry
second never sees a torn entry.

## 4. Status codes

| Status | Name | Detail / value |
|---|---|---|
| `0x00` | `OK` | |
| `0x01` | `PROGRESS` | a `SIGNAL` step; the package is still running. Detail: its 32-bit value |
| `0x10` | `BAD_MAGIC` | |
| `0x11` | `BAD_VERSION` | |
| `0x12` | `BAD_SIGNATURE` | value: the node's own signature |
| `0x13` | `BAD_CHECKSUM` | value: the checksum computed |
| `0x14` | `BAD_STEP` | detail: the opcode |
| `0x15` | `TOO_LARGE` | more units or buffers than the firmware holds (32, 64) |
| `0x16` | `BAD_RELOC` | detail: the entry |
| `0x17` | `BAD_UNIT` | a step names a unit the table lacks |
| `0x18` | `BAD_LAYOUT` | a section or a payload index past the package |
| `0x20` | `UNIT_FAULT` | detail: the unit's index; value: its completion word |
| `0x21` | `MBOX_OVERFLOW` | reserved: the mailbox holds rather than drops, and the firmware raises it nowhere |
| `0x22` | `MOVER_FAULT` | detail: the mover's fault code |
| `0x23` | `NO_REACH` | a mover register the processor cannot write; detail: the register |
| `0x30` | `TIMEOUT` | detail: what was awaited — 1 credit, 2 `AWAIT`, 3 barrier, 4 mover, 5 doorbell, 6 the mailbox's outbound slot |
| `0x40` | `BAD_OP` | an unknown submission opcode |
| `0x50` | `NO_MEMORY` | no free block of the region holds the request |
| `0x51` | `NO_SLOTS` | the region's block table (64 entries) is full |
| `0x52` | `BAD_FREE` | the address is not the start of a used block |
| `0x53` | `BAD_ARG` | a region past 3 or unconfigured, a zero size, an alignment or granule that is not a power of two, a base off the granule |
| `0x54` | `HEAP_BUSY` | `HEAP` on a region that still has live blocks |

## 5. stdio

Everything the firmware prints -- `ka_printf` and every library path to it --
goes to the stdout ring once the queue service has started; nothing goes to the
console tap. The firmware stores whole 8-byte words and then advances its
written count; the host reads the bytes between its read count and that, and
advances its read count. The ring is flow-controlled: a full ring holds the
firmware for one `timeout`, after which the bytes are dropped and counted in the
line's second word.

**The console tap is a debug tap.** `R_CONSOLE` (control region `0x08`) feeds a
256-byte FIFO in the load window (`rv64_load_win.v:88-107`) that never stalls
the processor and never reports a loss: a write past 256 unread bytes
overwrites, and once the write index wraps onto the read index the FIFO reads
EMPTY, so all 256 are gone. The firmware writes it only before the ring exists
(boot, or an image with no queue) and through `ka_debug` for short debug
lines, such as the one-line fatal-trap notice.

stdin is the mirror image: the host writes whole lines of the ring, then its
written count; the firmware reads a word, takes its byte, and advances its read
count.

## 6. The package heap

The rest of the region, past the rings, is the host's: it holds a binding array
per SQ slot (64 addresses each) and then the packages. The firmware never
allocates in it — a submission names the package's address and length. A host
keeps a package resident and submits it again, with new bindings, for as long as
it likes; `NodeQueue` finds a package it has uploaded before by its digest and
empties the heap only when nothing submitted is still outstanding.

## 7. Node heaps

The firmware holds four **region heaps** (`software/firmware/kohakuaccel/os/heap/`,
`ka/os/mem.h`). Each covers one span of unit-global memory the host names with
`HEAP` — by convention region 0 is the node's DRAM and region 1 its staging —
and hands out blocks from it to the host (`ALLOC`/`FREE`) and to the
firmware's own code (`ka_mem_alloc`/`ka_mem_free`), from one pool.

- **Granules.** Every block is whole granules (a power of two, at least 8; 64
  by default, 32 matches the host's write granule) at an address aligned to
  the larger of the granule and the request's alignment, so no two blocks
  share a granule.
- **Placement** is first fit by address; a pad left in front of an aligned
  block stays free; a free merges with both free neighbours. The policy is
  deterministic: `kohakuaccel.driver.node.heap` is the same policy in Python
  and predicts every address the firmware returns.
- **Bookkeeping is off the managed memory.** The block table (64 entries per
  region, so at least 31 live blocks) is in the scratchpad: a heap never
  reads or writes the memory it manages.
- **Reconfiguring** a region is refused while it has live blocks.

## 8. What a host owes

1. Write the region's header and zero its indices before the firmware boots;
   write the boot block after the image and before `HR_BOOT`.
2. Wait for the firmware state to read *ready* before the first submission.
3. Write whole 32-byte lines, and the SQ entry before the SQ tail.
4. Read the CQ tail before the entries it publishes, and advance the CQ head
   after — the firmware will not post into a full CQ.
5. Read results from memory only after the package's completion: a unit's
   completion is sent only once its memory writes are acknowledged, so by then
   every write the package made has landed
   ([package-format.md](package-format.md) §4.4).

## 9. Through the card daemon

The card daemon (`python -m kohakutpu.driver.daemon`) can be the host: it holds
each node's queue (`NodeQueue`) and polls it itself, so a remote client
(`kohakuaccel.driver.daemon.client.RemoteNodeQueue`, the same method surface) pays one
round trip per operation:

| Op | Does | Returns |
|---|---|---|
| `node_attach` | a queue at `base` for node `n` (geometry as §3), header written | `base`, heap offset, size |
| `node_load` | the ELF (sent whole) and a boot block onto the node, started | load sizes and seconds |
| `node_ready` | polls until the state reads *ready* | the firmware's line |
| `node_submit` / `node_wait` | one entry; its final completion | tag; completion (+ stdout on failure) |
| `node_run`, `node_nop`, `node_stop` | submit and wait in one | completion |
| `node_heap`, `node_alloc`, `node_free` | §7, submit and wait in one | completion (`ALLOC`: the address) |
| `node_heap_stats`, `node_stdout`, `node_state`, `node_units`, `node_counters` | reads | |

A waiting op polls in steps on the daemon's hardware thread and gives the
thread to other clients between steps; on the Verilator backend
(`--backend verilator --build <dir>`) each step also advances the model
(`--poll-cycles`, 600).

The daemon library knows no board, so three pieces are injected, all optional:
`node_mem`, a transport addressed by unit-global address (the board's rebase
over the card transport, with staging addresses reached through their mesh's
window); `node_loader(node, elf_path, BootArgs, mode)`, which puts firmware on
a node and starts it; and `poll_idle()`, run on the hardware thread between two
polls, where a simulated card advances its clock. Without `poll_idle` the loop
sleeps `poll_seconds` (2 ms) between polls. The CLI builds all three from the
board's address map; a board whose map names no per-node DRAM, staging and
control runs the daemon without node ops.
