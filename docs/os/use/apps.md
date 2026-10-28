---
title: KohakuAccel OS applications
summary: init and its command line, the shell, and every application in the image — what it does, its arguments, what it checks and the measurements it reports.
tags:
  - os
  - apps
---

# Applications, init and the shell

Every node runs the same user image, which holds every application. An
application is a function registered by name:

```c
static int app(int argc, char **argv) { ... return failures; }
KOS_APP("app", app);
```

`argv[0]` is the name; the rest is the argument text split at spaces (at
most 40 bytes of it). The exit code is the number of failed checks, 0 when
everything held.

Conventions every application here follows (`apps/apps.h`):

- **`@ name value`** lines are measurements; `run_core.py` and
  `run_card.py` collect them into their JSON (`measures`).
- **`FAIL …`** lines are broken checks; each one counts in the exit code.
  A run passes when every command exits 0 and no `FAIL` line appears.
- `run_workers(fn, n)` starts worker *i* on node *i* mod *nodes* and joins
  them all; `[tpn]` arguments below are threads per node, so *workers* =
  *nodes* × *tpn*.
- Shared arrays are split with `chunk()` on 64-byte boundaries (one writer
  per cache line per epoch).
- Applications that need shared memory print `needs shared memory` and
  exit 0 on boards without a shared region.

## init

`init` is node 0's first thread. Its arguments are the boot table's command
line (`--cmd`): **`;`-separated commands, each `app args [@node]`**, run in
order, each joined before the next starts. `@node` starts it on that node
(default 0). It prints

```
init: 4 nodes, commands: hello; units; mg
init: hello exit 0 in 496532 cycles
...
init: 3 commands, 0 failed
```

and then shuts every node down with the number of failed commands as the
exit code. The command `shell` hands the console to the shell.

## The shell

Reads commands from the console, one per line:

| command | does |
|---|---|
| `ps` | runs `ps` on every node: its threads, state, cycles, switches |
| `stat` | runs `kstat` on every node: its kernel counters |
| `units` | runs `units` on node 0 |
| `nodes` | node count, this node's mesh, the shared region, unit count, flags |
| `run APP ARGS [@N]` | starts APP on node N and waits for it |
| `exit [CODE]` | leaves the shell; init goes on with its next command (a nonzero CODE counts as a failed command) |

On a model, `run_core.py --stdin` feeds it; on the card, the JTAG console.

## Utilities

| app | prints |
|---|---|
| `ps` | this node's threads: `node N tid T state name cycles C switches S` (states `free`, `ready`, `run`, `blocked`, `zombie`) |
| `kstat` | this node's kernel counters: traps, syscalls, interrupts, timer ticks, messages sent/received, credit waits, channel-queue-full events, idle polls, free/total frames, L0 maps/tables/traps, dispatches, completions, faults |

## Probes

| app | args | what it proves | reports |
|---|---|---|---|
| `hello` | — | one thread on every node; each prints where it runs and exits with a value only that node computes; the joins from node 0 check every tid's node and exit code (spawn, remote spawn, join, exit codes) | — |
| `bench` | `[iters]` (16) | the cost of each primitive, averaged | `bench.syscall` (null syscall), `.l0` (kernel → L0 call), `.yield` (with one other ready thread), `.spawn_local`, `.spawn_remote` (spawn + join of an empty thread), `.atomic_local`, `.atomic_remote` (fetch-add whose home is this / the next node; shared boards), `.release`, `.acquire` (the cache sweeps; shared boards), `.chan_rtt` (message to node 1 and its echo), `.barrier` (one thread per node), `.mmap` (4 pages) |
| `kprof` | `[iters]` (32) | on a `-D KOS_PROF` kernel (exits 1 on another): the trap path's stages for a null call, a yield, a channel send to this node, a receive that finds its message, a send to node 1; each selection must profile exactly the calls made, every stage average nonzero | `kprof.<call>.<stage>` (average cycles; stages in [performance](../hardware/performance.md#the-trap-path)), `kprof.<call>.calls`, `kprof.self.out`, `.total` |
| `units` | — | every unit the board names: the four `CU_CTRL` words read through the kernel (through the unit's node for a unit on another die); capabilities' type and depth must match the board | `units.count`, `units.ctrl_read_cycles` (unit 0) |
| `mover` | `[words]` (64) | a local copy checked word for word; a copy into every other node's landing zone, checked there by a receiver thread told over a channel; a pattern per destination so a wrong node or offset fails | `mover.local_cycles`, `mover.local_bytes_per_kcycle`, `mover.land_cycles` (into the next node) |

## Multi-node CPU algorithms

| app | args | algorithm | checks | reports |
|---|---|---|---|---|
| `preduce` | `N [tpn]` | parallel sum over shared memory: each worker writes its chunk, then after a barrier sums the **next** worker's chunk (every partial reads data another node wrote) | total equals the serial sum | `preduce.n`, `.workers`, `.cycles_1` (one worker), `.cycles_w`, `.speedup_x100` |
| `psort` | `N [tpn]` | parallel merge sort: *W* (largest power of two the nodes hold) workers heap-sort their chunks in place, then log₂*W* merge rounds between two buffers, a barrier between rounds | sorted; same sum and xor as the input | `psort.n`, `.workers`, `.cycles_1`, `.cycles_w`, `.speedup_x100` |
| `pmatmul` | `M [tpn]` | `C = A × B`, int64, M × M, rows of C split over the workers in whole cache lines | equal to the one-worker result entry for entry | `pmatmul.m`, `.workers`, `.cycles_1`, `.cycles_w`, `.speedup_x100`, `.macs_per_kcycle_w` |
| `workq` | `TASKS [tpn]` | dynamic distribution: workers take task numbers from one shared counter (a home-node atomic); a task is the Collatz step count over a 64-number block (uneven cost) | every task done exactly once; total equals the serial sum; prints tasks per worker | `workq.tasks`, `.workers`, `.cycles_serial`, `.cycles_parallel`, `.speedup_x100` |
| `stress` | `ROUNDS [tpn] [iters]` | locks, atomics, barriers under contention: each round every worker takes the lock `iters` times and increments a **plain** shared counter inside it, adds to an atomic counter, then meets the others at a barrier | both counters exact after every round (mutual exclusion and the lock's release/acquire) | `stress.lock_ops`, `.cycles`, `.cycles_per_lock_op` |
| `pipeline` | `COUNT` (64) | producer/consumer chain through channels, one stage per node: stage 0 makes 1…COUNT, each later stage maps `v → 3v + k` and forwards, the last folds a checksum back to node 0 | checksum | `pipeline.messages`, `.cycles`, `.cycles_per_item` |
| `msgsum` | `N` (4096) | reduction with no shared memory: one worker per node fills its share in its own `kos_mmap` pages, sums it and sends the partial to node 0 over a channel | total, and one partial from every node | `msgsum.n`, `.cycles` |

`pipeline`, `msgsum` and `mover` use messages only, so they run on every
board with two or more nodes, including the card boards without a shared
region.

## Unit applications

| app | what it does | checks | reports |
|---|---|---|---|
| `mg` | for every MG unit on the board, from node 0: one FILL A, FILL B, DRAIN round through `kos_dispatch` (through the unit's node when it is on another die) waited for with `kos_unit_wait`; for a unit on node 0's mesh with the `engine` flag, a second round through `kos_engine` | wait OK with no fault; the unit's retired count rose by exactly 3; the DRAIN wrote exactly its 4 words into node 0's landing zone and none of the 124 sentinels around them | `mg.mailbox_round_cycles`, `mg.engine_round_cycles` (one per round), `mg.units` |

`mg` and `kdsmg` are the places in the OS tree that know a unit's
instruction set (`apps/ktpu/`); everything they use is the generic dispatch
interface ([dispatch.md](../reference/dispatch.md),
[dispatcher.md](../reference/dispatcher.md)).

## Dispatcher applications

Each starts a dispatcher server on every node (`kds_start`, 64 KB arenas),
runs its graphs and stops the servers. Flags in the arguments: `-e` unit
batches through the engine (`KDS_F_ENGINE`), `-u` uncached arenas, `-b`
best-fit arenas.

| app | args | what it does | checks | reports |
|---|---|---|---|---|
| `kdschain` | `K` (8) | a K-step chain over every node: step *s* is a CPU task on node *s* mod *N* that reads `{value, tag}` from its landing-zone slot, writes `{LCG(value, s), s + 1}`, and a move carries that word to the next step's node | each step's tag is its own index (it ran after its predecessor's word landed); the last word equals the serial result | `kdschain.steps`, `.cycles`, `.cycles_per_step` |
| `kdsreduce` | `E` (256) | every node fills and sums E values of its own arena (two CPU tasks), moves the sum into node 0's landing zone; a CPU task on node 0 adds the N sums | total equals the serial sum | `kdsreduce.cycles` |
| `kdsbad` | — | a forward predecessor; a cycle over two nodes; an unknown unit; a failing CPU task here and on the far node; a deadline passing while a part sleeps on the far node; a plain graph afterwards; a task on a node with no server | `EINVAL` with the task, `EINVAL`, `EINVAL`, `EFAULT` with the task and its return, `ETIME`, `OK`, `ENOENT` with the task | — |
| `kdsmulti` | `K` (8) | one client thread per node, each the home of its own K-step chain, all at once | every chain's checks | `kdsmulti.cycles` |
| `kdsalloc` | `OPS` (64) | on every node, OPS random allocations and frees (1–4096 bytes); a graph moves the survivors' references to their node, where a CPU task compares `SYS_VTOP` of each with the address the reference gives; then the arena checks; on a board with a shared region, a channel message sent from a shared buffer two pages into the region | blocks 64-byte aligned, on the right node, never overlapping; patterns in this node's blocks intact; translations equal; after freeing everything at most one free extent per chunk and block; the largest free block fits without a new chunk; a block bigger than any free extent takes one, which `kds_trim` returns; `kds_free_owner` returns the thread's 5 blocks; the message holds the buffer's bytes | `kdsalloc.extents`, `kdsalloc.shared_copy` |
| `kdsbench` | `M` (32) | graphs that isolate one cost each (below) | every graph `OK` | `kdsbench.<case>` (home cycles per task), `kdsbench.<case>_client` (around `kds_run`), `kdsbench.alloc_here`, `.alloc_far` |
| `kdsmg` | `R` (8) | for every MG unit, on its node: CPU sentinels in an arena block, FILL A, FILL B, DRAIN with the block's addresses patched at bind time (MG's address is two fields), a CPU check; a unit on another die also DRAINs into node 0's landing zone; then R FILL, FILL, DRAIN rounds on the first MG unit of each node, issued without waits | the DRAIN changed exactly its 4 words at the patched offset and none of the 28 sentinels around them (on the unit's node, through the release/acquire around the unit, and across the interlink) | `kdsmg.units`, `.bind_cycles`, `.bind_far_cycles`, `.round_cycles`, `.round_far_cycles` |

`kdsbench` cases: `one` (a graph of one NOP), `chain` (M NOPs in a chain),
`wide` (M independent NOPs), `cpu` (M empty CPU tasks in a chain), `hop` (M
NOPs alternating node 0 and node 1: each step a DEP), `load` (M NOPs all on
the last node: its part's load through the mover and PART chunks, and the
run).
