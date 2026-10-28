---
title: KohakuAccel OS architecture
summary: The three layers (L0 machine layer in M, L1 kernel in S, L2 user in U), how privilege and translation map onto them, one kernel per node, and how an SMP-like programming model is built over AMP hardware.
tags:
  - os
  - architecture
---

# Architecture

## The machine it runs on

Each system node has one RV64 system core (RV64IMA + Zicsr, M/S/U, Sv39,
a 64-line × 32-byte write-back L1; [arch/cpu/rv64-sys](../../arch/cpu/rv64-sys/README.md)).
What the OS assumes about it:

- **No coherence.** Each core's L1 is private. A line written on one node is
  not seen by another until the writer writes it back and the reader drops
  its copy. The L1 is maintained by whole-cache sweeps (control register
  `DCACHE`, 0x1C8: flush, invalidate, or both).
- **The interlink is the only way between nodes.** A node reaches another
  mesh's DRAM, staging store and signal slots through the interlink, with
  posted stores and fenced doorbell rings; loads do not cross it on the card.
- **Atomics are local.** AMO and LR/SC are not atomic across nodes on shared
  DRAM ([hardware/constraints.md](../hardware/constraints.md)).
- **Each node has its own compute units, mailbox, completion queue, dispatch
  engine and mover**, all on its own mesh.

That is AMP: independent processors that cooperate by messages. The OS keeps
it that way inside (one kernel per node, every cross-node operation a
message) and presents user programs with a model closer to SMP.

## Three layers

```
  U   L2  libkos, init, shell, apps         translated (Sv39), DRAM only
  ──────────── ecall: system calls (a7 = number) ─────────────────────────
  S   L1  kernel: threads, syscalls,        untranslated (satp 0), one per node
          messages, units, mover, log
  ──────────── ecall: L0 calls (a7 = function) ───────────────────────────
  M   L0  machine layer: boot check,        untranslated, never preempts
          soft MMU, delegation, timer
```

### L0 — the machine layer (M)

`software/os/machine/`. It runs first on every node and stays resident as a
service layer under the kernel:

- **Boot.** Checks the boot table (magic, version, node count, region and
  unit counts), finds this node's private span, the shared region and every
  mesh's staging store, and refuses to start a node whose table is wrong
  (exit code 0xB0) or whose user image does not fit its span.
- **The soft MMU.** Owns this node's Sv39 tables: builds the user address
  space at boot, and serves `MAP`, `UNMAP` and `QUERY` for the kernel. A map
  request is checked against the memory the boot table gave this node; the
  page tables themselves are never mappable. See
  [memory.md](memory.md#the-soft-mmu).
- **Delegation.** Every exception a supervisor can handle and every
  interrupt (software, timer, external) is delegated to S; M sees only
  `ecall` from S and faults inside L0 itself.
- **The machine timer.** `mtimecmp` is an M-only CSR on this core; the kernel
  sets its preemption timer through `SET_TIMER`.
- **The call interface.** Nine calls, each a short bounded piece of work
  ([reference/syscalls.md](../reference/syscalls.md#l0-calls)). L0 never
  preempts and never blocks.

### L1 — the kernel (S, untranslated)

`software/os/kernel/`. One instance per node, each managing only its own
node, talking to the others by messages. The kernel runs with `satp` 0 and
names physical addresses directly; translation is on only while user code
runs. The trap entry turns translation off before its first memory access
and `k_resume` turns it on after its last, so the kernel's own code is never
subject to the back-to-back access behaviour described in
[hardware/constraints.md](../hardware/constraints.md#sv39-back-to-back-accesses).
User memory is reached through copy helpers that ask L0 for the physical
page (`QUERY`), one page at a time.

The kernel is never interrupted: it runs with `sstatus.SIE` clear and, when
no thread is ready, polls its sources — the completion queue, the RX queue,
its message rings, pending remote requests, blocked threads' wait
conditions. Interrupts (external: a completion or a doorbell; timer: the
preemption quantum) only bring user code into the kernel.

What it holds per node:

| object | count | where |
|---|---|---|
| threads | 16 slots (`K_THREADS`) | scratchpad (frames), private DRAM (stacks) |
| inbox of kernel messages | 128 (`K_INBOX`) | private DRAM |
| channel queue | 64 messages (`K_CHANQ`) | private DRAM |
| pending remote unit requests | 32 (`K_PENDING`) | private DRAM |
| barriers (node 0 only) | 8 at once, 64 waiters each | private DRAM |
| units | up to 32, from the boot table | scratchpad |

A thread id is `node << 8 | slot`, so any node can tell which node a thread
lives on.

### L2 — user (U, Sv39)

`software/os/user/` (`libkos`), `dispatch/` (the dispatcher:
[reference/dispatcher.md](../reference/dispatcher.md)), `utils/` (`init`, the
shell, `ps`, `kstat`), `apps/`. Every node runs the same user image; each node has its own copy of
its writable data. An application is a `main(argc, argv)` registered by name
with `KOS_APP("name", fn)`; the kernel starts it by name on any node
(`SYS_RUN`), with up to 40 bytes of argument text. Threads within an
application are `kos_spawn(fn, arg, node)` on any node.

User code touches no scratchpad and no control register: every user page is
DRAM, and every device interaction is a system call.

## Privilege and translation

| | L0 | L1 | L2 |
|---|---|---|---|
| privilege | M | S | U |
| `satp` while it runs | 0 | 0 | this node's user table |
| addresses it names | physical | physical (DRAM cached, or its uncached alias) | user virtual |
| control region | yes | yes | no |
| entered by | reset; `ecall` from S | delegated trap; L0's boot jump | `sret` from the kernel |
| interrupts while running | masked | masked (polls) | enabled (external; timer when `tick` > 0) |

## One kernel per node: the multikernel

Nothing in a kernel's state is shared with another node. Every operation
that involves another node is a request message to that node's kernel and a
reply:

| user call | goes to | message |
|---|---|---|
| `kos_spawn(fn, arg, node)` | `node` | `M_SPAWN` → reply with the tid |
| `kos_run(app, args, node)` | `node` | `M_RUN` → reply with the tid |
| `kos_join(tid)` | the tid's node | `M_JOIN` → reply when the thread exits |
| `kos_atomic(word, …)` | the word's home node | `M_ATOMIC` → reply with the old value |
| `kos_barrier(id, n)` | node 0 | `M_BAR` → reply when `n` have arrived |
| `kos_dispatch(unit, w)` | the unit's node | `M_DISPATCH` → reply with the ticket |
| `kos_unit_wait`, `kos_unit_ctrl` | the unit's node | `M_UNIT_WAIT`, `M_UNIT_CTRL` → reply when done |
| `kos_send(node, chan, …)` | `node` | `M_CHAN` (no reply; flow-controlled by credits) |
| `kos_shutdown(code)` | every node | `M_SHUTDOWN` |

The calling thread blocks until the reply arrives; the node goes on running
its other threads meanwhile. How messages travel is in
[reference/messaging.md](../reference/messaging.md).

## SMP in user over AMP in hardware

What user programs see, and what makes it true:

| user sees | built from |
|---|---|
| threads on any node, joined from any node | remote spawn/join messages; tids carry the node |
| one copy of shared globals at one address on every node | a shared DRAM region mapped at `0x2000_0000` in every node's tables (boards that have one) |
| data written before a release is seen after the matching acquire | each synchronisation call writes this node's L1 back before it releases and drops it after it acquires (cached mode), or shared memory mapped uncached (`shared_uncached`) |
| atomic read-modify-write on shared words | the operation executes at the word's **home node**, on the word's uncached alias, as a message to that node |
| locks | ticket locks on a home-node atomic, or Lamport's bakery on uncached shared words (`lock_bakery`) |
| barriers | node 0 counts arrivals and replies to all |
| message channels between any two threads | a per-node channel queue fed by kernel messages with credit flow control |

What it does not hide: L1 lines are written back whole, so two nodes must
never write the same 64-byte line between two synchronisation points (the
applications split work on 64-byte boundaries), and a plain load of a shared
word another node is changing sees the value as of this node's last acquire.

## Knobs

Every knob selects between implementations built side by side, set per boot
in the boot table's flags:

| flag | effect |
|---|---|
| `shared_uncached` | map shared memory uncached; synchronisation calls skip the cache sweeps |
| `pt_dram` | page tables in the private span (written through the uncached alias) instead of the local staging store |
| `lock_bakery` | `kos_lock` is the bakery over uncached memory instead of a ticket lock on a home atomic |
| `poll` | the kernel waits by polling only; no external interrupt |
| `engine` | unit dispatch goes through the node's dispatch engine where it has one |
| `trace` | the kernel prints every system call and message; L0 runs a watchdog |

and `tick` (cycles): the preemption quantum; 0 is cooperative scheduling.
Their measured costs are in [hardware/performance.md](../hardware/performance.md#knobs).
