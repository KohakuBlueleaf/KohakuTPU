---
title: Sysnode queue
summary: How a host hands work to a sysnode's dispatcher and gets results back — the boot block, the queue region in card memory, the submission and completion rings, the stdio rings, host-owned package storage and allocator-backed boot regions, all polled.
tags:
  - spec
  - normative
  - software
  - dispatcher
---

# Sysnode queue

> **Kind: Fixed** throughout. The ring depths, the region's address and the
> heap's size are parameters the host chooses (§3); the layout around them is
> not.

A sysnode's RV64 processor runs the **dispatcher firmware**: it takes
[packages](package-format.md) from a submission ring, runs them against the
sysnode's compute units, and posts a completion for each. The host only writes
submissions and reads completions. **Both sides poll** — the host the completion
tail, the firmware the submission tail; nothing in the protocol needs an
interrupt.

The firmware side is `software/kohakuaccel/firmware/queue/service.c` with the
layout in `software/kohakuaccel/firmware/include/ka/queue/layout.h`; the host
side is `software/kohakuaccel/driver/kohakuaccel/driver/node/queue/` (`NodeQueue`), with
the layout in `layout.py`.

## 1. Addresses

Every address in this protocol is **unit-global** — mesh in `[37:36]`, as a
compute unit's instruction carries it ([memory-protocol.md](memory-protocol.md))
— so the host, the firmware and every package name a byte the same way. The
firmware reaches the region through its sysnode port, **uncached**, by setting
bit 38 of the address (the processor's uncached alias,
[control-registers.md](control-registers.md) §7), so it never reads a stale
copy of what the host wrote.

**The region belongs in the sysnode's staging store.** A staging address is
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
| 1 | version, `fw.boot.version` [3] |
| 2 | the queue region's unit-global address |
| 3 | this sysnode's mesh index |
| 4 | `scan` — with `n_units` zero, the firmware enumerates coordinates `0..scan` in x and y |
| 5 | `timeout` — cycles any one wait may take before the package fails |
| 6 | `cq_depth` — a sysnode-wide bound on outstanding completions, `0` for none ([package-format.md](package-format.md) §4.1) |
| 7 | flags: `[0]` `SERIAL`, send each dispatch step whole rather than round-robin ([package-format.md](package-format.md) §4); `[1]` `TIMING`, print each package run's phase cycles to stdout after it ends; `[2]` `NOFETCH`, ignore the packages' fetch ports and send every payload through the mailbox ([package-format.md](package-format.md) §4.7); `[3]` `STEPS`, print each step's end cycle; `[4]` `NOENGINE`, run every package on the firmware path even where a dispatch engine exists ([package-format.md](package-format.md) §4.8) |
| 8 | `n_units`, at most 16; `0` asks the firmware to find its own |
| 9–24 | the units, as unit words ([package-format.md](package-format.md) §1.2) |
| 25–36 | `fw.boot.regions` [4] entries of base, byte extent and use (0 empty, 1 DEBUG, 2 QUEUE) |

Boot flag bit 5, SERIALQ, disables next-entry read-ahead. (code: software/kohakuaccel/firmware/include/ka/boot/args.h:8-16, :27-56)

The unit table is what the firmware computes the machine signature over. Given
here, it is the host's; with `n_units` zero the firmware sends a `CU_CTRL` read
of `CU_CAPS` to every coordinate in `0..scan` (its own, `(0, 0)`, excepted)
through its mailbox and keeps each coordinate that answers **from that
coordinate** — the router clamps an out-of-grid address onto a real unit, which
answers with its own source. Either way the table is published in the queue
region (§3) before the firmware reports ready.

## 3. The queue region

The host allocates it from the sysnode's staging store (§1, §7), writes the header
(§3.1) and zeroes every index **before** booting the firmware. The firmware
resumes the indices it finds, so firmware can resume indices when the host leaves them intact. The runtime restart path instead reinitializes queues (code: software/kohakutpu/application/kohakutpu/application/runtime/card/__init__.py:439-442).

**One writer per 64-byte line.** A host writes card memory in whole granules
and fills whatever a write does not carry: 32 bytes over JTAG (the rest zeroed),
64 bytes over XDMA (a narrower write is a read-modify-write of the line). Two
writers in one line would erase each other. Each fixed publication line has one writer; multiple fields owned by that writer may share the line. The host writes complete `fw.queue.line` [64]-byte lines. (code: software/kohakuaccel/firmware/include/ka/queue/layout.h:14-49) The processor's 8-byte uncached stores honour byte strobes,
so the firmware may write single words.

| Offset | Writer | Words |
|---|---|---|
| `0x000` | host | magic `0x314555455551414B` ("KAQUEUE1"), version `4`, `sq_n \| cq_n << 16`, `so_bytes \| si_bytes << 32`; at `0x020` (`KA_Q_EPOCH`, word 4) the host's queue generation epoch |
| `0x040` | firmware | state (0 none, 1 ready, 2 stopped, 3 fatal), ABI version, entries completed, fatal code; at `0x060` (`KA_Q_LIST_ABI`, word 4) the execution-list ABI version (`KA_Q_LIST_VERSION`, `1`) |
| `0x080` | host | SQ tail — entries submitted, ever |
| `0x0C0` | firmware | SQ head — entries consumed |
| `0x100` | firmware | CQ tail — completions posted |
| `0x140` | host | CQ head — completions read |
| `0x180` | firmware | stdout written (bytes, ever), stdout bytes lost |
| `0x1C0` | host | stdout read |
| `0x200` | host | stdin written |
| `0x240` | firmware | stdin read |
| `0x280` | firmware | the number of units served; the unit words follow from `0x2C0`, one per 8 bytes, room for 16 |
| `0x340`..`0x43f` | reserved | no heap statistics |
| `0x440` | host | the SQ: `sq_n` entries of 64 bytes |
| then | firmware | the CQ: `cq_n` entries of 32 bytes |
| next 64-byte line | firmware | stdout ring, `so_bytes` (a multiple of 64) |
| then | host | stdin ring, `si_bytes` (a multiple of 64) |
| then | firmware | run record ring (version 4): `cq_n` records of 128 bytes (`KA_Q_REC_BYTES`) |

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
| 3..5 reserved | | | | | | `BAD_OP` |
| 6 `LIST` | the descriptor array's unit-global address (`count` 64-byte `RUN` entries) | item count | queue epoch (`KA_Q_EPOCH`) | list ABI version (`KA_Q_LIST_VERSION`, `1`) | flags: `[0]` chain the items on the dispatch engine (L0.NQ.057) | total instructions dispatched across completed items, or the failing item's value |

For `LIST`, header validation checks the queue epoch, list ABI version and flags
before reading descriptor items: an epoch mismatch (`word 4 != KA_Q_EPOCH`)
posts `BAD_ARG` with `detail = 1` and `value = word 4`, while a version mismatch
(`word 5 != KA_Q_LIST_VERSION`) or a flag past bit 0 (`word 6 >> 1 != 0`) posts `BAD_ARG`
with `detail = 2` and `value = word 4`. Each descriptor item is a 64-byte `RUN`
entry (`KA_SQ_RUN_*`). If item `i`'s opcode (`word 0 & 0xff`) is not `RUN`, the
list stops at item `i`, prints `[ka] package <item_tag> failed: status 40 detail <op> step 0 value 0`
to stdout, and posts `BAD_OP` with `detail = op`, `value = 0`, and run record
`list_index = i`, `item_tag = item[1]`, `epoch = KA_Q_EPOCH`.

Unused words are 0. Binding belongs to the host; firmware refuses packages
carrying relocation entries. RUN's binding fields are reserved compatibility
fields, not a firmware relocation service.

The host stages entries and flushes contiguous batches before publishing the SQ tail (code: software/kohakuaccel/driver/kohakuaccel/driver/node/queue/client.py:868-910). The firmware reads the entry, runs
it, posts its completion and only then advances the SQ head, so a slot is free
for reuse once the head has passed it. A head one behind the tail can therefore
mean an executing package, not just an unread submission.

`NodeQueue.pointers()` reports host-local `sq_tail` and `cq_head` alongside
`sq_tail_card` and `cq_head_card`, which read back the published words. These
reads are sequential observations, not an atomic snapshot of both agents.

### 3.2 Completion entry — 32 bytes

| Word | Field |
|---|---|
| 0 | tag |
| 1 | `[15:0]` status, `[31:16]` detail, `[63:32]` step index |
| 2 | sysnode cycles from run entry before header reading to its result snapshot, including failure quiesce but excluding subsequent reporting and CQ publication (`RUN`) |
| 3 | `RUN` that succeeded: instructions dispatched. Failed: the failure's value. `SIGNAL`: the step's 64-bit value |

The firmware writes the entry, then the CQ tail; each uncached store completes
before the next is issued, so a host that reads the tail first and the entry
second never sees a torn entry.

In a version 4 region, the firmware writes the 128-byte run record (`KA_Q_REC_BYTES`,
version `KA_Q_REC_VERSION` = `1`) at the same slot index modulo `cq_n` before
posting the CQ entry. Words are 8 bytes (`KA_Q_REC_WORD`): `tag` (`0`), `so_wr`
(`8`), the `RUN` timing words `head` (`16`), `copy` (`24`), `bind` (`32`),
`steps` (`40`), `barrier` (`48`), `local` (`56`), `total` (`64`), `first` (`72`),
`last` (`80`), `sent` (`88`), `idle` (`96`), then `list_index` (`104`, items
completed or the failing item's index for `LIST`, `0` for `RUN`), `item_tag`
(`112`, the last or failing item's tag for `LIST`, `0` for `RUN`), and `epoch`
(`120`, the queue generation epoch captured at SQ-entry dispatch and copied to its completions).

LIST terminal timing fields are componentwise sums of invoked RUN results, not zero. (code: software/kohakuaccel/firmware/queue/service.c:270-286, :323-324)

The application runtime's default per-wait firmware budget is one third of its
host completion timeout. A failed wait can consume one budget, and the firmware
can spend another budget draining outstanding work before posting its error
completion. The remaining third allows setup, reporting and host observation.
An explicit `NodeCard(wait_seconds=...)` overrides that default. This is a
budget allocation, not a bound on a package with arbitrarily many progressing
waits or on an unresponsive bus transaction. A posted completion can report an
error; its tag alone does not establish success.

## 4. Status codes

| Status | Name | Detail / value |
|---|---|---|
| `0x00` | `OK` | |
| `0x01` | `PROGRESS` | a `SIGNAL` step; the package is still running. Detail: the low 16 bits of its 32-bit argument |
| `0x10` | `BAD_MAGIC` | |
| `0x11` | `BAD_VERSION` | |
| `0x12` | `BAD_SIGNATURE` | value: the sysnode's own signature |
| `0x13` | `BAD_CHECKSUM` | value: the checksum computed |
| `0x14` | `BAD_STEP` | reason-dependent detail; not always an opcode |
| `0x15` | `TOO_LARGE` | more units or buffers than the firmware holds (32, 64) |
| `0x16` | `BAD_RELOC` | the package carries relocations; detail 0, value: the relocation count |
| `0x17` | `BAD_UNIT` | a step names a unit the table lacks |
| `0x18` | `BAD_LAYOUT` | a section or a payload index past the package |
| `0x20` | `UNIT_FAULT` | detail: the unit's index; value: its completion word |
| `0x21` | `MBOX_OVERFLOW` | reserved: the mailbox holds rather than drops, and the firmware raises it nowhere |
| `0x22` | `MOVER_FAULT` | detail: the mover's fault code |
| `0x23` | `NO_REACH` | a mover register the processor cannot write; detail: the register |
| `0x24` | `CONTRACT` | ASSERT_ZERO found a nonzero register; detail is step count, value is register contents |
| `0x30` | `TIMEOUT` | detail: what was awaited — 1 credit, 2 `AWAIT`, 3 barrier, 4 mover, 5 doorbell, 6 the mailbox's outbound slot, 7 slot, 8 RX, 9 ack, 10 ring, 11 the dispatch engine (value: its raw `STAT`, decoded on the host) |
| `0x40` | `BAD_OP` | an unknown submission opcode, or a `LIST` descriptor item whose opcode is not `RUN` (detail: the opcode) |
| `0x50` | `NO_MEMORY` | Reserved in the dispatcher queue protocol |
| `0x51` | `NO_SLOTS` | Reserved in the dispatcher queue protocol |
| `0x52` | `BAD_FREE` | Reserved in the dispatcher queue protocol |
| `0x53` | `BAD_ARG` | LIST header refusal: detail 1 epoch mismatch; detail 2 version/flags mismatch; value submitted epoch |
| `0x54` | `HEAP_BUSY` | Reserved in the dispatcher queue protocol |

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

The host stdin writer reads the consumed count and refuses input exceeding unread ring capacity before writing. Stdin publication writes whole ring lines before publishing the written count. Firmware reads the containing word, takes the byte and advances its consumed count. (code: software/kohakuaccel/driver/kohakuaccel/driver/node/queue/client.py:1283-1306; software/kohakuaccel/firmware/queue/service.c:90-103; evidence: implementation inspection only; no card or named RTL witness established for this complete contract.)

## 6. The package heap

The rest of the region, past the rings, is the host's: it holds a binding array
per SQ slot (64 addresses each) and then the packages. The firmware never
allocates in it — a submission names the package's address and length. Runtime packages use the shared host allocator. Submitted tags retain them;
the final completion retires its tag; preparation pins or resident caching may retain the package span. Identical in-flight
packages share one upload. Recording retains host package bytes; prepared replay may also pin uploaded package storage. The queue-local fallback coalesces retired
spans and never wraps over outstanding work.

### Retained multi-node lists

`PreparedQueueGroup` in the queue driver's `group.py` prepares one immutable LIST
per node before publishing any of them. Each node consumes its own package through
the existing RUN item protocol. Submission publishes and flushes every queue
before collection waits for any of the group's terminal completions. Replays reuse
the package and descriptor uploads. The group is single-flight and its completion
object joins every participant; an individual successful CQ is not group success.
A failed or uncertain publication quarantines all participating queues and keeps
their uploads. A failed collection still attempts the other published completions,
then keeps the epoch quarantined. No firmware queue wire changes are required.

The application `PreparedFirmwareGraph` adapter holds an immutable
`BoundGraphArtifact` and retains its allocations and receiver-scoped slots for the
whole retained instance. The caller supplies the same card-wide `SlotLeases` pool
to both firmware and OS adapters. Closing an active or failed instance refuses;
only a stopped/reset card establishes safe reclamation after uncertainty. Slot
leases do not alter the queue's epoch/version checks or the firmware's DE ring
handling. A captured `PreparedSplitGraph` exposes `bind_artifact`,
`prepare_firmware` and `prepare_kaos`: private scratch and physical aliases bind
once under retained allocation generations, and both backend constructors consume
that cached artifact. The common capability set supports static event effects;
operations unsupported by the KAOS adapter refuse at preparation. The ordinary
firmware submission path retains its existing capability set.

## 7. Host-owned boot regions

The driver reserves each sysnode's QUEUE region from its staging allocator and
DEBUG regions from shared DRAM. The boot table supplies `(base, bytes, use)`;
DEBUG is use 1 and QUEUE is use 2. Populated regions must have nonempty,
nonoverflowing, nonoverlapping extents and distinct uses. The queue pointer
must match its region, which must cover all declared rings and run records.
The dispatcher refuses invalid queue regions before ready. Reservations remain
owned until the sysnode stops. Dispatcher images carry no `os/heap` allocator.
See [the host memory model](stack/l0/memory.md) for lifetimes and proof vectors.

## 8. What a host owes

1. Write the region's header and zero its indices before the firmware boots;
   write the boot block after the image and before `HR_BOOT`.
2. Wait for the firmware state to read *ready* before the first submission.
3. Write whole 64-byte lines, and the SQ entry before the SQ tail.
4. Read the CQ tail before the entries it publishes, and advance the CQ head
   after — the firmware will not post into a full CQ.
5. Read results from memory only after the package's completion: with acknowledgement fencing enabled, endpoint completion waits for tracked local writes; remote or otherwise untracked writes require their own ordering
   ([package-format.md](package-format.md) §4.4).

## 9. Through the card daemon

The card daemon (`python -m kohakutpu.driver.daemon`) can be the host: it holds
each sysnode's queue (`NodeQueue`) and polls it itself, so a remote client
(`kohakuaccel.driver.daemon.client.RemoteNodeQueue`, a proxy for the operations below) pays one
round trip per operation:

| Op | Does | Returns |
|---|---|---|
| `node_attach` | a queue at `base` for sysnode `n` (geometry as §3), header written | `base`, heap offset, size |
| `node_load` | the ELF (sent whole) and a boot block onto the sysnode, started | load sizes and seconds |
| `node_ready` | polls until the state reads *ready* | the firmware's line |
| `node_submit` / `node_wait` | one entry; its final completion | tag; completion (+ stdout on failure) |
| `node_run`, `node_nop`, `node_stop` | submit and wait in one | completion |
| `node_stdout`, `node_state`, `node_units`, `node_counters` | reads | |

A waiting op polls in steps on the daemon's hardware thread and gives the
thread to other clients between steps; on the Verilator backend
(`--backend verilator --build <dir>`) each step also advances the model
(`--poll-cycles`, 600).

The daemon library knows no board, so three pieces are injected, all optional:
`node_mem`, a transport addressed by unit-global address (the board's rebase
over the card transport, with staging addresses reached through their mesh's
window); `node_loader(node, elf_path, BootArgs, mode)`, which puts firmware on
a sysnode and starts it; and `poll_idle()`, run on the hardware thread between two
polls, where a simulated card advances its clock. Without `poll_idle` the loop
sleeps `poll_seconds` (2 ms) between polls. The CLI builds all three from the
board's address map; a board whose map names no per-sysnode DRAM, staging and
control runs the daemon without sysnode ops.
