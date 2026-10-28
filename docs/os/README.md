---
title: KohakuAccel OS
summary: A small operating system for the system nodes of any KohakuAccel machine — one image on every node, a board description for what differs, threads and shared memory over hardware that has neither coherence nor shared caches, and generic access to compute units, the dispatch engine and the mover.
tags:
  - os
  - software
  - rv64
---

# KohakuAccel OS

`software/os/` — an operating system for the **RV64 system core** of every
system node in a KohakuAccel machine. One image runs on every node; a
**board description** says what differs between machines (how many nodes,
which meshes they sit on, where their DRAM is, which compute units exist).

The hardware is **AMP**: one RV64 core per system node, a private L1 per
core with no coherence between them, and the interlink between meshes as the
only path from one node to another. The OS gives user programs an **SMP-like
view** on top of it: threads that run on any node, one shared address space
for shared data, release/acquire at every synchronisation call, locks,
barriers, channels and home-node atomics — and a generic interface to the
machine's compute units that knows nothing about any unit's instruction set.

It serves every KohakuAccel project. Nothing in the machine layer or the
kernel names a project: a unit is `{type, mesh, x, y, depth}` from the board,
and an instruction is four opaque 64-bit words. The one place that knows a
KohakuTPU instruction is the `mg` application.

## What it is

| layer | privilege | runs from | job |
|---|---|---|---|
| **L0** machine layer (`machine/`) | M | instruction window + scratchpad | checks the boot table, owns the Sv39 page tables (the soft MMU), delegates every trap and interrupt to S, serves a small call interface to the kernel |
| **L1** kernel (`kernel/`) | S, untranslated | instruction window + scratchpad + private DRAM | one kernel per node (a multikernel): threads, system calls, node-to-node messages, remote spawn/join/run, home-node atomics, barriers, channels, units, dispatch engine, mover, console and log |
| **L2** user (`user/`, `dispatch/`, `utils/`, `apps/`) | U, Sv39 | DRAM | the `libkos` runtime, the dispatcher (one server per node: units, arenas, task graphs across nodes), `init`, the shell, applications |

## Pages

| page | what it covers |
|---|---|
| [design/architecture.md](design/architecture.md) | the three layers, the privilege mapping, the multikernel, how SMP-in-user is built over AMP hardware |
| [design/memory.md](design/memory.md) | physical and user address maps, the soft MMU, private spans, staging, the uncached alias, coherence at synchronisation points |
| [design/boot.md](design/boot.md) | the boot table, what the host writes, the boot sequence, the start handshake and run id, the log area and progress word |
| [reference/syscalls.md](reference/syscalls.md) | every system call and L0 call: number, arguments, results, errors; the `libkos` API |
| [reference/messaging.md](reference/messaging.md) | signal slots, fenced rings, kernel messages and credits, channels and their flow control, barriers, home-node atomics, locks |
| [reference/dispatch.md](reference/dispatch.md) | the unit registry, `SYS_DISPATCH`, the mailbox and the dispatch engine, `CU_CTRL`, completion and wake, the mover, landing zones |
| [reference/dispatcher.md](reference/dispatcher.md) | the dispatcher (kds): servers, task graphs with dependencies loaded across nodes, unit batches, arenas and references that bind on any die |
| [reference/boards.md](reference/boards.md) | the board description format and every board in the tree, including the non-TPU board |
| [use/apps.md](use/apps.md) | `init`, the shell, the probes and the multi-node CPU algorithms |
| [use/building.md](use/building.md) | building images, the memory-gap pass, the core-only models, running on models and on the card, the tests |
| [hardware/performance.md](hardware/performance.md) | measured costs on silicon and on the models, each with the run it came from |
| [hardware/constraints.md](hardware/constraints.md) | how the hardware behaves where it matters to the OS, and what the OS does about it |

## Status

The OS runs on KohakuTPU v9t6 silicon, four dies at 100 MHz: the
machine-layer probes, the page-table walker probe, a single-node boot, and
the full OS across four dies (remote spawn, units on every die, MG dispatch
locally and across dies, the dispatch engine, mover landing across dies,
channels and barriers) all pass, with every number in
[hardware/performance.md](hardware/performance.md). It runs on three
core-only Verilator models (2, 3 and 4 nodes) and on the 1-die and 2-die
Verilated card models.

What it does not do: there is no file system, no dynamic loading (every
application is linked into the one image and registered by name), no
paging to backing store, no memory protection between threads of one node
beyond what Sv39 gives user mode as a whole, and no user-visible cache
coherence beyond the release/acquire points listed in
[design/memory.md](design/memory.md).
