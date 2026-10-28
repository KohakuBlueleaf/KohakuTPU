---
title: KohakuAccel OS memory
summary: The physical map a system node sees, the user address space, the soft MMU (Sv39 tables owned by the machine layer), the private DRAM span and its layout, the staging store, the uncached alias, and where cached shared memory becomes coherent.
tags:
  - os
  - memory
---

# Memory

## The physical map of one node

What the RV64 system core addresses (`include/kos/hw.h`):

| range | what | used by |
|---|---|---|
| `0x0000_0000` – `0x0000_7FFF` | instruction window, 32 KB, fetch only, host-loaded | L0 and kernel text |
| `0x0001_0000` – `0x0001_7FFF` | scratchpad, 32 KB | boot table, L0 data, kernel data and stack |
| `0x0002_0000` + offset | the control region (console, exit, mailbox, interlink, mover, dispatch engine, `DCACHE`) | L0 and kernel only |
| `≥ 0x1000_0000` | the node port: DRAM through the L1 | private spans, shared region |
| bit 38 set | the **uncached alias** of the same DRAM: bypasses the L1 | logs, landing zones, page tables in DRAM, atomics, uncached shared memory |
| bit 39 set | the special space: staging stores and apertures, always uncached | page tables, message rings, start handshake |
| bits 37:36 | the mesh a node-port address goes to | another node's DRAM and staging |

So one physical address names a mesh (bits 37:36), whether it bypasses the
cache (bit 38) and whether it is DRAM or the special space (bit 39). Mesh
*m*'s staging store is at `1 << 39 | m << 36`.

### Scratchpad and instruction window

| | contents | size (`kos` image) |
|---|---|---|
| IMEM `0x0` | L0 + kernel text and their library copy | 28 240 B of 32 KB |
| SPAD `0x1_0000` | boot table (host-written) | 2 KB |
| SPAD `0x1_0800` | L0 data and its 2 KB stack | 3056 B |
| SPAD `0x1_2000` | kernel data, thread frames, unit table, 4 KB stack | 19 424 B |

## The user address space

Every node maps the same user layout (`include/kos/abi.h`); only the
physical pages behind it differ:

| user address | what | mapped | backed by |
|---|---|---|---|
| `0x1000_0000` | user text, rodata, the app table | R X U | this node's private span + 0 |
| `0x1100_0000` | user data and bss (per node) | R W U | this node's private span, after the text |
| `0x2000_0000` | the shared region: `KOS_SHARED` globals first, then the shared heap | R W U (cached unless `shared_uncached`) | the board's shared DRAM |
| `0x2800_0000` | the same shared region, uncached | R W U uncached | the same DRAM through the alias |
| `0x3000_0000` + node × 2 MB + slot × 128 KB | thread stacks: 16 KB each under a guard gap | R W U | frames from this node's span |
| `0x3800_0000` | this node's landing zone, 64 KB | R W U uncached | the tail of this node's span |
| `0x4000_0000` + node × 256 MB | `kos_mmap` | R W U | physically contiguous frames |
| `0x0` – end of kernel text | kernel text (not U) | R X | IMEM — fetched by the trap entry while translation is still on |

A plain global in a user program is therefore **per node**: each node has
its own copy in its own span. A global marked `KOS_SHARED`, or memory from
`kos_shalloc()`, is **one** copy, at the same address everywhere. On a board
with no shared region (`card_v9t6_2n`, `card_v9t6_4n`, `core2`) the shared
addresses are unmapped and `kos_shared_ok()` returns 0; applications that
need shared memory check it.

## The private span

Each node owns one contiguous DRAM span (its size and place come from the
board). From its head:

```
  + 0                    user image: text, rodata, app table | data, bss
  + image size           kernel frames: stacks, mmap pages, kernel tables
                         (inbox, channel queue, pending requests, barriers)
  [pt_dram]              L0's page-table pool, 128 KB
  size - 80 KB           log area, 16 KB (uncached): header + console bytes
  size - 64 KB           landing zone, 64 KB (uncached): where units and
                         movers deliver for this node
```

The host writes the user image at the span's base from the ELF's user
segments (load address `1 << 40`, not a machine address; `tools/image.py`).
Frames are 4 KB, handed out from a bitmap and zeroed when taken.

## The staging store

Each mesh has a staging store in the special space, reachable from every
node by posted stores over the interlink. The OS divides each mesh's
staging into:

| offset | size | what |
|---|---|---|
| `0x00000` | 128 KB | L0's page-table pool: 32 tables (unless `pt_dram`) |
| `0x20000` | 4 KB | message rings: one ring of 16 × 64-byte slots per sending node |
| `0x21000` | 64 B | the start handshake ([boot.md](boot.md#the-start-handshake)) |

Staging is the place every node can **write** for another: messages are
eight posted stores into the receiver's staging followed by a fenced ring.

## The soft MMU

The machine layer owns each node's Sv39 tables; the kernel never writes a
PTE. Properties the design follows from:

- **The walker reads memory uncached** and **does not set A or D**. L0 sets
  A and D in every leaf it writes, and keeps the tables where the walker
  sees writes immediately: in the local staging store, or with `pt_dram` in
  the private span through the uncached alias.
- **Every change is followed by `sfence.vma`.** Without it the TLB keeps
  the old translation (measured: the old frame is still read).
- **A superpage's physical address must be aligned to its size** (a
  gigapage whose PPN is not 1 GB aligned faults). L0 uses a 1 GB or 2 MB leaf
  only where both the virtual and the physical address and the remaining
  size allow it, and 4 KB leaves otherwise.
- **Requests are checked.** A kernel `MAP` must name a virtual address at or
  above 16 MB, page-aligned, and a physical range inside this node's private
  span, the shared region, or any mesh's staging from the message area up;
  never the page-table pool. A mapping over an existing one is refused
  (`KOS_EBUSY`). Uncached DRAM mappings are made by setting bit 38 in the
  leaf. `UNMAP` removes whole leaves at or above 16 MB; `QUERY` returns the
  leaf PTE of any address, 0 if none.

There is one user table per node: every thread on a node runs in the same
address space.

The pool is 32 tables; `kstat` reports how many are in use (`l0_tables`).

## The uncached alias

Setting bit 38 of a DRAM address reaches the same DRAM cell without the L1.
The OS uses it where a value must be in DRAM the moment it is written, or
must be read fresh:

| what | why |
|---|---|
| the log area | a host reads it while the node runs or after it hangs |
| landing zones | units and movers write them behind the L1's back |
| page tables in DRAM (`pt_dram`) | the walker reads uncached |
| home-node atomics | the operation reads and writes the word itself, not a cached copy |
| the `0x2800_0000` shared view, bakery locks, ticket `serving` words | values other nodes change |
| `shared_uncached` | every shared access |

## Coherence at synchronisation points

Shared memory is cached by default. The L1s are not coherent, so the
runtime makes them agree at the points where one thread hands data to
another:

| call | before (release) | after (acquire) |
|---|---|---|
| `kos_spawn` | write this node's L1 back | — the new thread drops its L1 first |
| thread exit (incl. app exit) | write back | — |
| `kos_join` | — | write back, then drop every line |
| `kos_barrier` | write back | write back and drop |
| `kos_lock` / `kos_unlock` | unlock: write back | lock: write back and drop |
| `kos_send` / `kos_recv` | send: write back | recv: write back and drop |
| `kos_release()` / `kos_acquire()` | explicit | explicit |

A release is a whole-L1 flush (64 lines); an acquire is flush + invalidate.
With `shared_uncached` the shared region is mapped uncached and the sweeps
are skipped.

Two rules follow, and the applications keep them:

1. **One writer per 64-byte line per epoch.** A line is written back whole,
   so two nodes writing different words of one line between two
   synchronisation points lose one of the writes. Work is split on 64-byte
   boundaries (`chunk()` in `apps/apps.h`).
2. **Words used with `kos_atomic` are touched only by `kos_atomic`.** They
   live at their home node's uncached view; a cached copy elsewhere is stale.

The mover reads and writes DRAM directly, so `SYS_MOVE` writes the caller's
L1 back before the copy and drops it after.
