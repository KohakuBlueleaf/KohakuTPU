---
title: KohakuAccel OS dispatcher (kds)
summary: The user-mode dispatcher — one server per node owning that node's units, an arena of physically contiguous memory and the parts of task graphs; graphs of unit instructions, mover copies, CPU functions and joins with dependencies, loaded across nodes and run with completion collected at the graph's home; buffers named by references that bind on any die.
tags:
  - os
  - reference
  - dispatch
  - dispatcher
---

# The dispatcher (kds)

`software/os/dispatch/` (the API in `include/kos/kds.h`). It runs in user
mode, linked into the image like `libkos`; the kernel gives it the
primitives listed [below](#kernel-primitives-it-uses), and nothing in it
knows any unit's instruction set.

```
  client thread ──SUBMIT──▶ home server (its node) ──PART/GO──▶ other servers
       ▲                         │  ▲                              │
       └────────── reply ────────┘  └────── NDONE, DEP ────────────┘
  each server: its node's units (SYS_DISPATCH / SYS_ENGINE), its mover,
               its arena (SYS_MMAP + SYS_VTOP), its parts of graphs
```

## Servers

`kds_start(flags, arena_bytes, node_mask)` spawns one server thread on
every node in the mask and returns when each has answered READY (status
from its set-up). `kds_stop()`, on the same node, sends QUIT to each and
joins them. While servers run, the first 32 KB of every landing zone belong
to them (`KDS_LZ_USER` is the first byte left to applications).

| flag | effect |
|---|---|
| `KDS_F_ENGINE` | unit tasks go in batches of up to 8 through `SYS_ENGINE` (a unit the engine refuses goes through `SYS_DISPATCH`) |
| `KDS_F_UNCACHED` | arenas are mapped through the uncached alias: no release/acquire sweeps around unit tasks |
| `KDS_F_BESTFIT` | the arena takes the smallest free extent that fits (else the first) |

A server's state is one `SYS_MMAP` (4 pages) and a plain per-node global
pointer to it, so every thread of a node sees its own node's server. A
stopped server unmaps its arena chunks and its state: the node's free frames
are what they were before it started.

`kds_server_stat(&s)` reads this node's server's counters: loop passes,
retirement wakes, tasks finished, unit calls, messages in and out, and the
cycles spent taking messages, running tasks, issuing, sending, waiting in
`SYS_EVENT` and retiring.

### The loop

User code runs from DRAM, so a pass fetches only the phases that have work
(flags for ready tasks, pending unit tasks, DEPs, the outbox, instructions
in flight):

```
loop:
    take the messages the last SYS_EVENT counted on the server channel
    run every ready task (NOP, CPU, MOVE at once; UNIT onto its unit's list)
    issue pending unit tasks within each unit's credit
    send the coalesced DEPs and whatever waits in the outbox
    SYS_EVENT(retirement target, timeout, counts, units, server channel)
        -> returns when the node's retirement count reaches the target or a
           message is queued; the copy of every unit's retired and fault
           counts retires unit tasks
```

The **target** is the retirement count now plus the fewest retirements that
change anything: per unit, the first in-flight task with successors, the
last in-flight task of a part whose every remaining task is in flight, half
the ring when tasks wait for the unit's credit, else the ring's last; the
least over the units. A stream of instructions with nothing depending on
them is retired in one wake, a chain wakes once per instruction. Every
retirement on the node counts toward the target, so the wait can end early
but never late. The wait's timeout is the nearest graph deadline, or the
outbox's retry (20 000 cycles) when a send found no room. Sends never wait
(`KOS_SEND_NOWAIT`): a message that finds no room stays in the outbox, in
order per destination, so two servers sending to each other with full queues
do not deadlock.

## Messages

Every message is 40 bytes (`struct kds_msg`: type, flag, count, graph id,
four words) on a server's channel `KDS_CHAN`; a client request carries its
reply channel (`KDS_REPLY(tid)`) and gets exactly one reply. Messages carry
no shared-memory data, so the dispatcher sends them with plain `SYS_SEND` /
`SYS_RECV` (no release/acquire sweeps).

| message | from → to | carries |
|---|---|---|
| SUBMIT | client → its node's server | the task array's address and count, a timeout |
| PART | home → executor | a chunk of the executor's tasks in its landing-zone slice; first: the part's size |
| PART_ACK | executor → home | the chunk is copied out |
| GO | home → executor | every part is loaded: run |
| DEP | executor → executor | up to 16 finished task ids whose successors run there |
| NDONE | executor → home | the part is over: status, failed task, detail, busy cycles |
| ABORT | home → executor | stop the part (it answers NDONE) |
| ALLOC, FREE, FREE_OWNER, TRIM, STAT | client → any server | arena requests |
| QUIT, READY | starter ↔ server | |

## Graphs

A graph is an array of 128-byte task records (`struct kds_task`) in the
client's node's memory, built in order with:

| call | task | runs on |
|---|---|---|
| `kds_nop(g, node, pred, n)` | a join or fan-out point | `node` |
| `kds_unit(g, unit, w, pred, n)` | one instruction (four opaque words) to `unit` | the node on the unit's mesh |
| `kds_patch(g, task, ref, lo, width, shift)` | bits `[lo, lo + width)` of the task's instruction get `pa(ref) >> shift` at bind time (up to 3 per task; a field split over two places is two patches) | — |
| `kds_move(g, src, dst, words, pred, n)` | one mover copy of `words` 32-byte words from a buffer to a buffer of the same node or any node's landing zone | the source's node |
| `kds_cpu(g, node, fn, a0, a1, a2, pred, n)` | `fn(args)` with `args` = {a0, a1, a2}; a nonzero return fails the task (the image is the same on every node, so `fn` is valid everywhere) | `node` |

Each task names up to **4 predecessors, each an earlier task** — every graph
is acyclic by construction, and the server refuses any record that breaks it
(`KOS_EINVAL` with the task's index). Fan-in or fan-out beyond 4 goes through
NOP tasks. At most 65 535 tasks per graph.

`kds_run(g, timeout, &r)` submits the graph to this node's server (the
**home**) and waits for its answer:

| `r.status` | when | `r.failed`, `r.detail` |
|---|---|---|
| `KOS_OK` | every task finished | — |
| `KOS_EINVAL` | a bad record (kind, predecessor, patch, unit, move size, node) | its index |
| `KOS_ENOENT` | a task's node has no server | its index |
| `KOS_EFAULT` | a task failed | its index; a CPU task's return, a unit's fault count, a mover's fault |
| `KOS_ETIME` | the deadline (`timeout` cycles from the submit) passed | — |
| `KOS_EAGAIN` | the home tracks 4 graphs already | — |

`r.cycles` is the home's count from the submit to its answer.

### What the home does

1. **Check.** Every record as above; each task's node (UNIT: the unit's
   mesh's node; MOVE: the source's node); each task's `succ_nodes`, the
   nodes its successors run on.
2. **Load**, one graph at a time (each peer has one landing-zone slice per
   home): the home's own part is copied in directly; every other node's part
   goes in chunks of up to 64 records, gathered into a staging block of the
   home's arena, **moved by the mover** into the executor's slice
   `[home × 8 KB, + 8 KB)`, and announced by PART; the executor copies the
   records out and answers PART_ACK; the next chunk follows.
3. **GO** to every part once all are loaded.
4. **Collect** NDONE from every part; answer the client when all have
   reported. A failure, or the deadline, sends ABORT to every part still
   running and answers at once (later reports are counted and ignored).

### What an executor does with its part

- **Load.** The records in id order; each task's count of unfinished
  predecessors; a successor index (sorted `(predecessor id, local task)`
  pairs, binary search); the tasks with no predecessors in the ready ring.
  DEPs that arrive before GO count down, but nothing runs before GO.
- **Run.** NOP: done. CPU: the function, in the server thread. MOVE:
  `SYS_MOVE` (into a landing zone with `KOS_MV_LAND(n)`). UNIT: the patches
  are bound (physical address of each reference *as this node names it*),
  then the task joins its unit's pending list.
- **Issue.** Per unit, at most `min(depth, 32)` instructions in flight;
  consecutive pending tasks of a part go as one `SYS_ENGINE` batch of up to 8
  (`KDS_F_ENGINE`) or one `SYS_DISPATCH` each. A unit runs its instructions
  in issue order, so tickets retire in order and the retired count from
  `SYS_EVENT` completes a prefix of the unit's in-flight ring. A fault
  (the unit's fault count rose) fails the oldest task retired with it.
- **Finish.** A finished task counts down its successors here; its id goes
  into the DEP for each other node in its `succ_nodes` (coalesced, up to 16
  ids per message, sent at the end of the loop pass). When the part's last
  task finishes, or one fails, or ABORT comes, the executor sends NDONE
  (after its DEPs) and frees the part's memory once no unit instruction of
  it is in flight.

### Coherence on an executor

Units write DRAM behind the L1; the cache is per node and shared by every
thread of the node. With cached arenas the executor writes the L1 back
(**release**) before issuing a unit task when a CPU task ran, or a new part
opened (a client thread here may have written what its units read), since
the last release; it writes back and drops the L1 (**acquire**) before a CPU
task when a unit task finished here, or a DEP came (a unit on another die
may have written here), since the last acquire. The home acquires once more
before it answers, so the client reads what units wrote. `SYS_MOVE` does
both sweeps around a copy. `KDS_F_UNCACHED` maps the arenas uncached and
skips every sweep.

## Memory: arenas and references

Each server owns its node's **arena**: chunks, each one `SYS_MMAP`
(physically contiguous, zeroed) whose physical address `SYS_VTOP` gives once;
the first of `arena_bytes` at start, another of `max(arena_bytes, request)`
when nothing fits. Blocks are 64-byte aligned multiples of 64 (one writer
per L1 line; the mover needs 32). Free extents are kept sorted by (chunk,
offset) and merge with both neighbours on free.

A block is named by a **reference**, `node << 56 | offset in that node's
private span` (`kds_ref`). Any node turns it into the physical address its
units and mover use: `SYS_INFO(KOS_I_SPAN, node) + offset` — that node's
span as *this* node names it (its own spelling for its own span, the owner's
mesh bits for a peer's). So one graph binds the same buffer correctly on
whichever die's unit uses it. `kds_land(node, off)` is the reference of a
landing-zone byte.

| call | does |
|---|---|
| `kds_alloc(node, bytes, &ref)` | a block on `node`'s arena, owned by the calling thread |
| `kds_free(ref)` | gives it back |
| `kds_free_owner(node)` | gives back every block the calling thread took there; how many |
| `kds_trim(node)` | unmaps every fully free chunk (`SYS_MUNMAP`); bytes returned |
| `kds_arena_stat(node, &s)` | chunks, bytes, free bytes, largest free extent, used and free extents |
| `kds_va(ref)` | on the reference's own node: its user address (an arena block or the landing zone); else null |
| `kds_pa(ref)` | the physical address this node's units and mover name it by |

## Kernel primitives it uses

| call | for |
|---|---|
| `SYS_EVENT` | the loop's one wait: a retirement target or a message on the server channel; copies every unit's retired and fault counts |
| `SYS_RECV(.., KOS_NOWAIT)`, `SYS_SEND(.., KOS_SEND_NOWAIT)` | draining without blocking; the outbox |
| `SYS_VTOP` | a chunk's physical address |
| `SYS_MUNMAP` | trim |
| `SYS_MMAP(.., KOS_MMAP_UNCACHED)` | uncached arenas |
| `SYS_INFO(KOS_I_SPAN / KOS_I_SPANSZ / KOS_I_MESHOF, node)` | binding references; units to nodes |

Details in [syscalls.md](syscalls.md).

## Applications

`kdschain`, `kdsreduce`, `kdsbad`, `kdsmulti`, `kdsalloc`, `kdsbench`
(`apps/kds/`) and `kdsmg` (`apps/ktpu/`, the one that knows MG's
instruction format); [use/apps.md](../use/apps.md#dispatcher-applications).
Measured costs: [performance.md](../hardware/performance.md#the-dispatcher).

## Limits

| | |
|---|---|
| tasks per graph | 65 535 |
| predecessors / patches per task | 4 / 3 |
| graphs a node runs parts of at once / a home tracks | 4 / 4 |
| unit instructions in flight per unit | `min(depth, 32)` |
| arena chunks / free and used extents per node | 16 / 128 each |
| graph records | in the submitting client's node's memory (the home reads them in place) |
