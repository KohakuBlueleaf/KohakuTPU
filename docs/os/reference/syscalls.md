---
title: KohakuAccel OS system calls
summary: Every system call (U to S) and every machine-layer call (S to M) — number, arguments, results and errors — plus the libkos API user programs call them through.
tags:
  - os
  - reference
  - abi
---

# System calls and the ABI

The numbers and constants are in `include/kos/abi.h`; user programs call
through `libkos` (`include/kos/user.h`).

## Calling convention

A system call is `ecall` from U with the number in `a7` and arguments in
`a0`–`a3`. On return **`a0` is the status or result** (negative = an error
below) and **`a1` a second value** where the result can be any 64-bit word
(tickets, atomics' old values, `CU_CTRL` words, exit codes, the source node
of a message). A call that has to wait blocks the calling thread; the node
runs its other threads meanwhile, and the result is delivered when the wait
ends.

Pointers are user addresses. The kernel reaches them through the user page
tables; an unmapped buffer gives `KOS_EFAULT`.

## Status codes

| code | value | meaning |
|---|---|---|
| `KOS_OK` | 0 | |
| `KOS_EINVAL` | −1 | bad argument: number out of range, misaligned, not shared, too long |
| `KOS_EPERM` | −2 | L0: a mapping the boot table does not allow |
| `KOS_ENOMEM` | −3 | no frames, no page tables |
| `KOS_EBUSY` | −4 | the resource is in use (a `CU_CTRL` read already outstanding; a mapping already there) |
| `KOS_ENOENT` | −5 | no such thread, app, or mapping |
| `KOS_EAGAIN` | −6 | no free thread slot; barrier table full |
| `KOS_EFAULT` | −7 | a user buffer is not mapped; a unit reported a fault |
| `KOS_ETIME` | −8 | a wait's timeout passed |

Timeouts are in cycles of the global time (`SYS_TIME`); **0 means wait
forever**.

## System calls

| # | name | arguments | result (`a0`) / `a1` | notes |
|---|---|---|---|---|
| 0 | `SYS_EXIT` | code | — | ends the calling thread; a joiner gets `code` |
| 1 | `SYS_YIELD` | — | 0 | to the back of the ready queue |
| 2 | `SYS_WRITE` | buf, len ≤ 4096 | len, or `EFAULT` | to the console and the log area |
| 3 | `SYS_READ` | buf, len | bytes read (≥ 1), or `EFAULT` | waits for console input; returns at a newline or when `len` (at most 256) bytes are in |
| 4 | `SYS_SPAWN` | entry, arg, node | `OK` / tid; `EAGAIN` | starts `entry(arg)` on `node` (this node if `node` ≥ node count). Through libkos the entry is `fn(arg)` |
| 5 | `SYS_JOIN` | tid | `OK` / exit code; `ENOENT` | waits for the thread to exit; any node's tid |
| 6 | `SYS_SELF` | — | tid | `node << 8 \| slot` |
| 7 | `SYS_INFO` | key | value, or `EINVAL` | keys below |
| 8 | `SYS_SLEEP` | cycles | 0 | |
| 9 | `SYS_TIME` | — | global time in cycles | |
| 10 | `SYS_MMAP` | size, flags | user address, or `ENOMEM` | physically contiguous zeroed pages on this node, mapped RW, cached; `KOS_MMAP_UNCACHED` (1): through the uncached alias |
| 11 | `SYS_WAIT` | addr, value, timeout | `OK` once `*addr != value`; `ETIME`; `EINVAL` | `addr` a shared word, 8-byte aligned; tested on its uncached view |
| 12 | `SYS_ATOMIC` | addr, op, a, b | `OK` / old value; `EINVAL` | `addr` a shared word, 8-byte aligned; runs at the word's home node; ops below |
| 13 | `SYS_CACHE` | `KOS_C_RELEASE` (1) or `KOS_C_ACQUIRE` (2) | 0 | release: write this node's L1 back; acquire: write back, then drop every line |
| 14 | `SYS_UNITS` | buf, max | count, or `EFAULT` | copies up to `max` `struct kos_unit` |
| 15 | `SYS_DISPATCH` | unit, words (4 × u64) | `OK` / ticket; `EINVAL` | one instruction; to the unit's node if it is on another mesh; waits for credit |
| 16 | `SYS_UNIT_WAIT` | unit, ticket, timeout | `OK`, `EFAULT` (the unit faulted), `ETIME`, `EINVAL`, `EAGAIN` (the unit's node has 32 requests pending) / the unit's fault count | waits until the unit has retired `ticket` instructions |
| 17 | `SYS_UNIT_CTRL` | unit, index | `OK` / the 64-bit `CU_CTRL` word; `EBUSY`, `EINVAL` | reads `CU_CTRL[index]` (0 caps, 1 status, 2 counters, 3 debug) |
| 18 | `SYS_MOVE` | src, dst, words, flags | `OK`; `−fault`; `EINVAL` | one mover copy of `words` 32-byte words; flags `KOS_MV_LAND(n)`: `dst` is an offset in node `n`'s landing zone |
| 19 | `SYS_SEND` | node, chan, buf, len ≤ 40 | `OK`; `EINVAL`; `EAGAIN` | a channel message to `node`; waits while there is no room, or with `len \| KOS_SEND_NOWAIT` (0x100) returns `EAGAIN` |
| 20 | `SYS_RECV` | chan, buf, timeout | length / source node; `ETIME`; `EFAULT` | the oldest message on `chan` from any node; `buf` must hold 40 bytes; timeout `KOS_NOWAIT` (~0) returns `ETIME` at once when none is queued |
| 21 | `SYS_STAT` | what, buf, max | bytes written; `EINVAL`; `EFAULT` | `KOS_S_THREADS` (0), `KOS_S_KERNEL` (1), `KOS_S_UNITS` (2) — `include/kos/stat.h` |
| 22 | `SYS_RUN` | name, args, node | `OK` / tid; `ENOENT`, `EAGAIN`, `EFAULT` | starts a registered app; `name` ≤ 23 bytes, `args` ≤ 40 bytes |
| 23 | `SYS_BARRIER` | id, parties | `OK`; `EAGAIN` | returns when `parties` threads (any nodes) have arrived at barrier `id` |
| 24 | `SYS_ENGINE` | unit, words, count 1–8 | `OK` / ticket of the last; `EINVAL` | `count` instructions through this node's dispatch engine; the unit must be on this node's mesh |
| 25 | `SYS_SHUTDOWN` | code | — | stops every node; this node last, with `code` |
| 26 | `SYS_LAND` | node | this node's landing zone's user address; `EINVAL` | `node` must be this node |
| 27 | `SYS_EVENT` | target, timeout, buf, `n \| chan << 32` | messages queued on `chan` (≥ 0) / the retirement count; `ETIME`; `EFAULT` | waits until this node's **retirement count** (every unit completion or fault counted, engine counters included) reaches `target` or `chan` has a message; then copies units [0, `n`)'s retired and fault counts to `buf` (two u64 each) |
| 28 | `SYS_VTOP` | va, len | the physical address; `EINVAL` | of a user range that is one physically contiguous piece (shared region, `mmap` pages, the landing zone), as this node's units and mover name it |
| 29 | `SYS_MUNMAP` | va, size | `OK`; `EINVAL` | unmaps a physically contiguous range of the `mmap` area (whole leaves) and frees its frames |

### `SYS_INFO` keys

| key | value |
|---|---|
| `KOS_I_NODE` (0) | this node's index |
| `KOS_I_NODES` (1) | node count |
| `KOS_I_MESH` (2) | this node's mesh |
| `KOS_I_FLAGS` (3) | the boot flags |
| `KOS_I_SHARED` (4) | user address of the shared region, 0 if the board has none |
| `KOS_I_SHSIZE` (5) | its size |
| `KOS_I_SHHEAP` (6) | user address of the shared heap's next-free word |
| `KOS_I_CLOCK` (7) | node clock in Hz |
| `KOS_I_NUNITS` (8) | units on the board |
| `KOS_I_L0RTT` (9) | cycles of one kernel → L0 call round trip, measured now over 16 calls |
| `KOS_I_SHPA` (10) | the physical address units and the mover use for the shared region |
| `KOS_I_LANDPA` (11) | the physical address units and the mover use for this node's landing zone |
| `KOS_I_SPAN` (12) | with `a1` = node: that node's private span as this node's units and mover name it (`EINVAL` past the node count) |
| `KOS_I_SPANSZ` (13) | with `a1` = node: its size |
| `KOS_I_MESHOF` (14) | with `a1` = node: its mesh |
| `KOS_I_PROF` (15) | kernels built with `-D KOS_PROF` only (`EINVAL` otherwise): `a1` = 0x100 + nr clears the trap-profile sums and selects system call nr; `a1` < 7 is the sum of stage `a1` over the selected calls since, 7 their count (stages: [performance](../hardware/performance.md#the-trap-path)) |

### `SYS_ATOMIC` operations

| op | effect on the word | returns |
|---|---|---|
| `KOS_A_ADD` (0) | `w += a` | old |
| `KOS_A_SWAP` (1) | `w = a` | old |
| `KOS_A_CAS` (2) | `if (w == a) w = b` | old |
| `KOS_A_LOAD` (3) | — | current |
| `KOS_A_MAX` (4) | `w = max(w, a)`, signed | old |

### Limits

| | |
|---|---|
| threads per node | 16 |
| user stack | 16 KB per thread |
| channel message | 40 bytes |
| app argument text | 40 bytes |
| boot command line | 511 bytes |
| units on a board | 32 |
| nodes | 4 |
| concurrent barriers / waiters each | 8 / 64 |
| `SYS_ENGINE` batch | 8 instructions |
| `SYS_MOVE` | 1–16 384 words of 32 bytes, both ends 32-byte aligned, each end one physically contiguous range |

## The libkos API

`include/kos/user.h`. Each wrapper is one system call unless noted;
the synchronisation wrappers add the release/acquire sweeps of
[design/memory.md](../design/memory.md#coherence-at-synchronisation-points)
when shared memory is cached.

| function | call | notes |
|---|---|---|
| `kos_exit(code)` | `SYS_EXIT` | |
| `kos_yield()` | `SYS_YIELD` | |
| `kos_spawn(fn, arg, node)` | `SYS_SPAWN` | release before; the new thread acquires first and releases at exit |
| `kos_join(tid, &code)` | `SYS_JOIN` | acquire after |
| `kos_run(app, args, node)` | `SYS_RUN` | |
| `kos_node()`, `kos_nodes()`, `kos_info(key)`, `kos_info_of(key, node)` | `SYS_INFO` | |
| `kos_time()`, `kos_sleep(cycles)` | `SYS_TIME`, `SYS_SLEEP` | |
| `kos_now()` | — | the same global time from the `time` CSR, no system call |
| `kos_shutdown(code)` | `SYS_SHUTDOWN` | |
| `kos_write(buf, len)`, `kos_read(buf, len)`, `kos_printf(fmt, …)` | `SYS_WRITE`, `SYS_READ` | `kos_printf` formats into 256 bytes |
| `kos_mmap(size)`, `kos_mmap_flags(size, flags)` | `SYS_MMAP` | null on failure |
| `kos_munmap(p, size)` | `SYS_MUNMAP` | |
| `kos_vtop(p, len)` | `SYS_VTOP` | |
| `kos_shalloc(size)` | `SYS_ATOMIC` on the heap word | shared, 64-byte aligned, never freed; null when the board has no shared region or it is full |
| `kos_shared_ok()` | `SYS_INFO` | the board has a shared region |
| `kos_release()`, `kos_acquire()` | `SYS_CACHE` | |
| `kos_unc(p)` | — | the uncached view of a shared address (`+ 0x0800_0000`) |
| `kos_atomic(w, op, a, b)`, `kos_fetch_add(w, v)`, `kos_load(w)` | `SYS_ATOMIC` | |
| `kos_wait(w, value, timeout)` | `SYS_WAIT` | |
| `kos_barrier(id, parties)` | `SYS_BARRIER` | release before, acquire after |
| `kos_lock(l)`, `kos_unlock(l)` | `SYS_ATOMIC` + `SYS_WAIT`, or plain uncached accesses (`lock_bakery`) | `kos_lock_t` lives in shared memory; acquire after lock, release before unlock |
| `kos_send(node, chan, buf, len)`, `kos_send_nowait(…)` | `SYS_SEND` | release before |
| `kos_recv(chan, buf, timeout, &src)` | `SYS_RECV` | acquire after a message |
| `kos_event(seen, timeout, counts, n, chan, &now)` | `SYS_EVENT` | |
| `kos_units(buf, max)` | `SYS_UNITS` | |
| `kos_dispatch(unit, w)` | `SYS_DISPATCH` | returns the ticket |
| `kos_unit_wait(unit, ticket, timeout, &faults)` | `SYS_UNIT_WAIT` | |
| `kos_unit_ctrl(unit, index, &value)` | `SYS_UNIT_CTRL` | |
| `kos_engine(unit, w, count)` | `SYS_ENGINE` | |
| `kos_move(src, dst, words, flags)` | `SYS_MOVE` | |
| `kos_land()` | `SYS_LAND` | this node's landing zone, uncached |
| `kos_stat(what, buf, max)` | `SYS_STAT` | |

An application:

```c
#include <kos/user.h>

static int64_t work(uint64_t i) { return (int64_t)(i * i); }

static int demo(int argc, char **argv)
{
    int64_t tid[4];
    for (unsigned i = 0; i < kos_nodes(); ++i) {
        tid[i] = kos_spawn(work, i, i);        /* one thread on each node */
    }
    for (unsigned i = 0; i < kos_nodes(); ++i) {
        int64_t code;
        kos_join(tid[i], &code);
        kos_printf("node %u: %ld\n", i, code);
    }
    return 0;
}
KOS_APP("demo", demo);
```

## L0 calls

The kernel calls the machine layer with `ecall` from S: function in `a7`,
arguments in `a0`–`a3`, status in `a0`, value in `a1`. Each call is short and
bounded; L0 never blocks.

| # | name | arguments | result / value |
|---|---|---|---|
| 0 | `KOS_M_PUTC` | byte | `OK` |
| 1 | `KOS_M_WRITE` | scratchpad address, length ≤ 4096 | `OK`; `EFAULT` outside the kernel's scratchpad data |
| 2 | `KOS_M_SET_TIMER` | absolute time | `OK` (programs `mtimecmp`; `~0` disables) |
| 3 | `KOS_M_MAP` | va, pa, size, attr (`KOS_MAP_R/W/X/U/UNCACHED`) | `OK`; `EINVAL`, `EPERM`, `ENOMEM`, `EBUSY` |
| 4 | `KOS_M_UNMAP` | va, size | `OK`; `EINVAL`, `ENOENT` |
| 5 | `KOS_M_QUERY` | va | `OK` / the leaf PTE, 0 if none; inside a superpage, with the page number of `va`'s 4 KB page |
| 6 | `KOS_M_EXIT` | code | does not return: the node stops with `code` |
| 7 | `KOS_M_INFO` | key: maps served (0), tables in use (1), traps taken (2), pool pages (3), user `satp` (4) | `OK` / value |
| 8 | `KOS_M_NOP` | — | `OK` (its round trip is the cost of a call) |
