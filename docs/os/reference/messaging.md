---
title: KohakuAccel OS messaging
summary: How node kernels talk over the interlink (signal slots, staging rings, fenced doorbells, credits) and the user-level primitives built on it — channels with flow control, barriers, home-node atomics, waits and locks.
tags:
  - os
  - reference
  - interlink
---

# Messaging, channels, barriers and atomics

Everything between nodes is a kernel message (`kernel/msg.c`). The user
primitives on this page are built from those messages, plus uncached shared
memory where the board has a shared region.

## What the interlink gives

| | |
|---|---|
| **posted stores** | any node can store into any mesh's staging store (and DRAM); stores are not acknowledged |
| **rings** | `IL_RING` adds `amount` to signal slot `slot` of the node on `mesh`; a **fenced** ring counts only once every earlier posted store from this node has landed |
| **signal slots** | 16 counters per node; a ring raises the receiver's doorbell count, which is its external interrupt |
| **the sig word** | one register: the selected slot's count, the slot field, this node's count of rings sent (16 bits), fault bits |
| **ring room** | at most 4 rings in flight per node: a node must wait while `rings issued − rings sent ≥ 4` |

Reading a slot's count: write the slot to `IL_SELECT`, read the sig word
until its slot field names the slot, then read the word **once more** — the
word whose field first matches still carries the previously selected slot's
count ([constraints](../hardware/constraints.md#a-slot-read-needs-one-more-read)).
`hw_slot()` in `include/kos/hw.h` does this; it costs 19 cycles (m0
`lat.slot_select_read`, the same on silicon and the models).

The OS divides each node's slots:

| slot | counts |
|---|---|
| 0–3 `KOS_SLOT_MSG(src)` | messages node `src` has put in this node's ring |
| 4–7 `KOS_SLOT_CRED(dst)` | ring slots node `dst` has drained (credits back to this node) |
| 8 `KOS_SLOT_BARRIER` | barrier arrivals of the bare-metal probe image (`tests/m0`); the kernel's barriers are messages |
| 9–15 | free |

## Kernel messages

A message is 64 bytes: a header word (`type | src << 8`) and seven payload
words. Node *s* sends to node *d* through **a ring of 16 slots in *d*'s
staging** (`0x20000 + s × 1 KB`):

1. **Credit check.** *s* may have at most 16 messages to *d* not yet
   drained: `sent[d] − slot CRED(d) < 16`. Out of credit, *s* keeps draining
   its own rings and polling its units while it waits (so two nodes sending
   to each other never deadlock); its progress word reads `send`.
2. **Eight posted stores** of the message into ring slot `sent[d] mod 16`.
3. **One fenced ring** of *d*'s slot `MSG(s)`, amount 1: it counts only
   after the eight stores have landed, so a count *d* sees always covers
   complete messages.

*d* **drains** whenever it polls — on an interrupt, after a system call
that blocks its caller (after every system call with `poll`), and in a loop
while no thread is ready: it clears its
doorbell count, reads each peer's `MSG` slot, copies every new message into
its **inbox** (128 entries, private DRAM) in arrival order, and returns the
number taken as **credits** with an unfenced ring of the sender's
`CRED(d)` slot. It then handles the inbox in order.

| type | request | reply |
|---|---|---|
| `M_SPAWN` | start `fn(arg)` here | status, tid |
| `M_RUN` | start registered app #i with these args here | status, tid |
| `M_JOIN` | tell me when tid exits | status, exit code (when it exits) |
| `M_ATOMIC` | apply op to this shared word (I am its home) | old value |
| `M_CHAN` | a channel message | — |
| `M_DISPATCH` | dispatch this instruction to my unit | status, ticket |
| `M_UNIT_WAIT` | tell me when my unit has retired ticket *t* | status (when it has) |
| `M_UNIT_CTRL` | read `CU_CTRL[i]` of my unit | status, value (when it comes) |
| `M_BAR` | arrival at barrier *id* of *n* (to node 0) | status (when all arrived) |
| `M_PING` | — | node 0's time |
| `M_SHUTDOWN` | stop with this code | — |
| `M_REPLY` | — | carries `{request id, status, value}` |

A request carries a request id (`node << 56 | counter`); the caller is
blocked waiting for a reply with that id, and the reply's status and value
land in its `a0` and `a1`. Requests that finish later (`M_JOIN`,
`M_UNIT_WAIT`, `M_UNIT_CTRL`) are parked at the receiver — on the thread, or
in a table of 32 pending unit requests (`EAGAIN` when full) — and answered
from its poll loop.

When the kernel waits for ring room or credit it marks its progress word
(`ring`: rings issued and the sig word; `send`: destination, messages sent
and credits returned), so a host sees from the log header which of the two a
stuck node is in ([boot.md](../design/boot.md#the-log-area-and-the-progress-word)).

## Channels

`kos_send(node, chan, buf, len ≤ 40)` and `kos_recv(chan, buf, timeout,
&src)`. A channel is a 32-bit number on the receiving node; any thread on
any node may send to it, and any thread on that node may receive from it.

- **Each node has one channel queue** of 64 messages (private DRAM),
  shared by all its channels. A send to this node goes straight into it; a
  send to another node is one `M_CHAN` kernel message.
- **Order.** Messages from one sender to one channel arrive in order:
  the ring is FIFO, the inbox keeps arrival order, and `kos_recv` takes the
  oldest message on the channel and closes the gap.
- **Flow control.** When the queue is full, an `M_CHAN` stays in the inbox,
  and so does every later `M_CHAN` (so channel order holds), while the other
  messages are handled. When the inbox fills, the node stops draining its
  rings, the senders' credits run out, and their `kos_send` blocks (`W_SEND`)
  until the receiver takes messages. No message is ever dropped.
  `kstat`'s `channel full` counts the times the queue was full.
- `kos_recv` with timeout 0 waits forever; otherwise it returns `ETIME`.
- In cached-shared mode `kos_send` releases first and `kos_recv` acquires
  after a message, so data written before a send is visible after the
  matching receive.

`tests/host/test_core.py::test_channel_flow_control` pushes 240 messages
(`pipeline 80` on core4s: 80 items through three 64-entry queues) and
checks that every one arrives and the checksum holds.

## Barriers

`kos_barrier(id, parties)`: **node 0 counts arrivals.** Each caller sends
`M_BAR` (node 0 counts its own directly) and blocks; when `parties`
arrivals have been counted, node 0 replies to every waiter and the barrier
object is free again. Up to 8 barrier ids may be in progress at once, with
up to 64 waiters each; beyond that the arrival gets `EAGAIN`. Threads from
any node, and several threads per node, may take part. The wrapper releases
before and acquires after.

## Atomics at the home node

The core's AMO and LR/SC instructions are atomic only within one node
([constraints](../hardware/constraints.md#atomics-are-not-atomic-across-nodes)).
`SYS_ATOMIC` therefore **executes every operation on a shared word at that
word's home node**:

```
home(word) = ((word − shared base) >> 12) mod nodes      (per 4 KB page)
```

On the home node the kernel reads and writes the word through its uncached
alias with no other code running between the read and the write (the kernel
is never preempted), so operations on one word from all nodes are
serialised by its home kernel. From another node the operation is an
`M_ATOMIC` request; the caller blocks for the old value. The home is per
page, so independent counters can be put on different nodes by placing them
in different pages (`bench` places one on its own node and one on the next).

Rules: the word is in the shared region, 8-byte aligned, and is touched only
through `kos_atomic`/`kos_fetch_add`/`kos_load` (or `SYS_WAIT`, which reads
the same uncached word). `KOS_A_ADD`, `SWAP`, `CAS`, `LOAD`, `MAX` (signed).

## Waiting on a shared word

`kos_wait(word, value, timeout)` blocks until the shared word (read through
its uncached alias) is no longer `value`; the kernel re-reads it each time it
polls. It is the sleep under the ticket lock and under any user-built
condition.

## Locks

`kos_lock_t` lives in shared memory (`kos_shalloc` or a `KOS_SHARED`
global). Two implementations, chosen per boot:

| | ticket lock (default) | bakery (`lock_bakery`) |
|---|---|---|
| take | `my = fetch_add(next, 1)` at the home node; `kos_wait(serving)` until `serving == my` | Lamport's bakery over 64 slots (`choosing[]`, `number[]`) with plain uncached loads and stores; a thread's slot is `(node × 16 + slot) mod 64` |
| give | `serving += 1` (uncached store; only the holder writes it) | `number[me] = 0` |
| messages | one atomic per take (a request unless this node is the home) | none |

Both are followed by an acquire on take and preceded by a release on give
when shared memory is cached. `stress` checks mutual exclusion by
incrementing a plain shared counter under the lock from every node and
comparing it with an atomic counter.
