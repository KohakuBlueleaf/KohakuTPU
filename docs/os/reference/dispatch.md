---
title: KohakuAccel OS dispatch
summary: How user programs reach compute units through the OS — the unit registry from the board, SYS_DISPATCH through the mailbox or the dispatch engine, completion counting and waits, CU_CTRL reads, units on other dies, the mover and landing zones.
tags:
  - os
  - reference
  - dispatch
---

# Dispatch: units, the engine and the mover

The kernel's unit code (`kernel/units.c`) is generic: **a unit is a
coordinate, a two-letter type and a credit depth from the board; an
instruction is four opaque 64-bit words.** Nothing in L0 or L1 knows what a
unit computes. The one application that knows an instruction set is
`apps/ktpu/mg.c` (KohakuTPU's matmul cluster).

## The unit registry

The board lists the units (`{type, mesh, x, y, depth}`,
[boards.md](boards.md)); the boot table carries them; every node sees **the
same list in the same order**, so a unit index means the same unit on every
node. A unit is **local** to the node on its mesh.

`kos_units(buf, max)` copies the list (`struct kos_unit`: `type` as two
ASCII bytes, `mesh`, `x`, `y`, `flags`, `depth`); `kos_info(KOS_I_NUNITS)`
gives the count. `kos_stat(KOS_S_UNITS)` gives this node's view with counts
(`struct kos_unit_stat`: `local`, instructions `sent`, `done`, `faults`, the
last `fault_arg`).

A board with no units (the `core*` boards) runs everything on this page
except dispatch; `units` reports 0 and `mg` finds nothing to drive.

## Dispatching an instruction

```c
int64_t ticket = kos_dispatch(u, w);                 /* w: 4 x u64 */
int st = kos_unit_wait(u, ticket, timeout, &faults); /* KOS_OK, EFAULT, ETIME */
```

`SYS_DISPATCH(unit, words)` on the unit's own node:

1. **Credit.** Each unit has a credit of `depth` instructions (16 if the
   board gives 0): while `sent − retired ≥ depth` the kernel polls the
   completion queue.
2. **The flit.** Through the node's **mailbox** (`DST`, `ARG0`–`ARG3`,
   `GO`; it waits while a flit is still offered), or — once the dispatch
   engine owns the unit — through **the engine's queue** (six entries per
   flit; it waits for room).
3. **The ticket** is the unit's count of instructions sent, after this one.

**Completions are counted per unit.** The kernel drains the node's
completion queue whenever it polls: an instruction or batch completion
increments the unit's `done`; a fault increments `faults` and records the
fault argument; both count as **retired**. For a unit the engine owns, the
engine's per-unit completion counter is mirrored instead (12-bit, read as a
difference). `SYS_UNIT_WAIT(unit, ticket)` returns `KOS_OK` once `retired ≥
ticket`, `KOS_EFAULT` (with the fault count in `a1`) if a fault arrived
while waiting, `KOS_ETIME` after the timeout. Until then the calling thread
is blocked; the node runs other threads and wakes it from its poll loop
(the completion queue's interrupt brings the kernel in).

### `CU_CTRL`

`kos_unit_ctrl(u, index, &value)` sends a typed `CU_CTRL` flit asking the
unit for word `index` (0 capabilities, 1 status, 2 counters, 3 debug) and
blocks until the reply arrives on the node's RX queue. One request per unit
at a time (`KOS_EBUSY` otherwise). `mg` reads counters before and after a
round to check that exactly the instructions it sent were retired (`value
>> 32`).

## Units on another die

A unit is driven by **its own node's kernel**. When the unit is not local,
`SYS_DISPATCH`, `SYS_UNIT_WAIT` and `SYS_UNIT_CTRL` become `M_DISPATCH`,
`M_UNIT_WAIT` and `M_UNIT_CTRL` requests to that node
([messaging.md](messaging.md#kernel-messages)); that kernel dispatches with
its own credit, counts the completion on its own queue, and replies. The
caller sees the same ticket, status and value as for a local unit; only the
cost differs (a dispatch, wait or `CU_CTRL` read on another die is two
kernel messages more).

Where a unit **writes its results** is the instruction's business: the
address is in the instruction words. A unit on another mesh can write into
this node's landing zone across the interlink — that is how `mg` checks
remote rounds (each DRAIN writes 4 words into node 0's landing zone; the
other 124 of 128 sentinel words around them must stay untouched).

## The dispatch engine

`SYS_ENGINE(unit, words, count 1–8)` hands a **local** unit to the node's
dispatch engine (once; the engine then owns the node's mailbox registers)
and queues `count` instructions in one system call; it returns the ticket of
the last, waited for with `kos_unit_wait` as usual. The board's `engine`
says whether the nodes have an engine; `SYS_ENGINE` returns `KOS_EINVAL`
when the node has none, the unit is not local, or the unit index is 16 or
more (the engine has 16 counters).

With the `engine` boot flag, `SYS_DISPATCH` itself hands every local unit to
the engine on first use, so plain dispatch goes through the engine queue
too. Without it, the mailbox is used until an application calls
`SYS_ENGINE`.

Measured per FILL, FILL, DRAIN round on an MG unit (dispatch + wait; cycles;
[performance.md](../hardware/performance.md#dispatch)):

| path | silicon, 4 dies (`kos_card_engine`) | silicon, 4 dies (`kos_card`) |
|---|---|---|
| three `SYS_DISPATCH`, mailbox | — | 11 493–11 567 |
| three `SYS_DISPATCH`, engine queue (`engine` flag) | 8538–8606 | — |
| one `SYS_ENGINE` of 3 | 5839–5880 | — |
| three `SYS_DISPATCH` to a unit on another die | 26 209–31 669 | 24 109–28 419 |

## The mover

`kos_move(src, dst, words, flags)` runs **one copy of `words` 32-byte words
on this node's mover** and returns when it is done: `KOS_OK`, or the
mover's fault code negated.

- `src` and `dst` are user addresses; each range must be physically
  contiguous and 32-byte aligned — private pages from `kos_mmap` (which
  hands out contiguous frames), the shared region, or the landing zone.
- `flags = KOS_MV_LAND(n)`: `dst` is an **offset in node *n*'s landing
  zone** (0 – 64 KB), on any node, including across the interlink. The
  landing zone is the only place in another node's span a node may write.
- Up to 16 384 words (512 KB) per call.
- The kernel writes the caller's L1 back before the copy and drops it after,
  so cached source data is in DRAM and the destination is read fresh.

`mover` checks both forms: a local copy compared word for word, and a copy
into every other node's landing zone that a receiver thread on that node
checks where it landed.

## Landing zones

Each node's last 64 KB of private span, mapped at `0x3800_0000` uncached
(`kos_land()`). It is where **units and movers deliver for this node**:
`kos_info(KOS_I_LANDPA)` is the address a unit or mover names it by, to put
in an instruction. Uncached, so a thread reads what a unit wrote without a
cache sweep.

`kos_info(KOS_I_SHPA)` is the same for the shared region.

## Engine and mailbox are reset with the node

The mailbox, completion queue and dispatch engine are reset with the node's
processor (`HR_BOOT`), so each boot starts with an empty completion queue and
an engine that owns no unit. The interlink is not
([constraints](../hardware/constraints.md#interlink-state-survives-a-processor-reset)).
