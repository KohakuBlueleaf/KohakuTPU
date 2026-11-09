---
title: KohakuAccel OS boot
summary: What the host writes before a node starts (the boot table, the image), what each layer does at boot, the start handshake that makes a boot independent of whatever an earlier run left in the interlink, and the log area a host reads while a node runs or after it hangs.
tags:
  - os
  - boot
---

# Boot

## What the host does

For every node of the board, with the node's processor held in reset
(`HR_BOOT` = 0):

1. **On the card, zero the node's private span and the shared region.** A
   DRAM line never written is an ECC error, and a cached store fills its line
   first. (A fresh Verilated card model's DRAM reads zero, so the model runs
   skip this; `run_card.py --paint`.)
2. **Write the private image** at the start of the node's private span: the
   ELF segments linked at load address `1 << 40`. These contain boot-only
   kernel helpers followed by user text, constants, the app table and data.
3. **Write the instruction window** (kernel-side text) and the
   **scratchpad** (L0 and kernel data, with the node's **boot table** at
   `0x1_0000`).
4. Then start the nodes, **last to first**, so node 0 starts after every
   other node is running.

`tools/run_card.py` (the card over JTAG, or a Verilated card model) and
`tools/run_core.py` (the core-only models) do exactly this.

## The boot table

`include/kos/boot.h`, packed by `tools/boot.py` from the board description.
2 KB at the scratchpad's base; little-endian; the image is the same on every
node and every board, and this table is the only thing that differs.

| field | meaning |
|---|---|
| `magic`, `version` | `"KOSBOOT1"`, 1 |
| `node`, `nnodes` | this node's index, the node count (1–4) |
| `mesh_of[4]` | the mesh each node sits on |
| `flags` | the knobs (`KOS_F_*`) |
| `clock_hz` | the node clock |
| `tick` | the preemption quantum in cycles; 0 = cooperative |
| `engine` | nonzero: this node has a dispatch engine |
| `nregions`, `region[16]` | memory: `{base, size, kind, owner}`; kinds: this node's private span, every other node's span (as this node's mover names it), the shared region, every mesh's staging store |
| `nunits`, `unit[32]` | the compute units: `{type (two ASCII bytes), mesh, x, y, flags, depth}` |
| `run` | a random nonzero id, the same on every node of one boot, new for every boot |
| `cmdline[512]` | what `init` runs |

Every region base is the physical address **this** node uses for it, with
the mesh bits the board's spelling gives it (see
[reference/boards.md](../reference/boards.md#dram)).

## What each layer does

### L0

1. Checks magic, version, node index and table sizes in IMEM; finds this
   node's private span and requires at least 256 KB. It enters
   `k_boot_machine` at `private base + linked address - KOS_UV_TEXT`.
   This boot-only PIC code resides in the already-loaded private image. It
   discovers shared and staging regions and requires staging to hold the OS
   layout. Validation uses the existing L0 diagnostics and exit `0xB0`.
2. Places the page-table pool (local staging, or the span with `pt_dram`)
   and builds the user address space: kernel text, user text, user data, the
   shared region (uncached with `shared_uncached`, unmapped with `shared_phys`).
3. Copies the boot table into kernel memory and populates the kernel's node
   view, including physical grants and the mapped shared size. Immutable
   scratchpad descriptors supply resident state, linker values and callbacks;
   the relocated code refers directly only to code/constants relocated with it.
4. Delegates traps and interrupts to S, sets `mtimecmp` to never, `satp` to 0,
   and jumps to the kernel in S.

### The kernel

1. Uses the node view populated by L0 to enter `k_boot_kernel` at its private
   physical address. This boot-only code initialises the frame pool and log
   area, asks L0 for the user `satp`, and zeroes the user bss.
2. Prints its banner: `KohakuAccel OS: node N of M (mesh m), private …,
   shared …, U units, flags F, tick T`.
3. Maps the landing zone (uncached) and the uncached view of the shared
   region unless `shared_phys`; node 0 initialises a mapped shared heap.
4. Allocates the mover index buffer and calls the resident cache-maintenance
   callback before allocating pending requests. The command and both busy-edge
   polls execute in IMEM: the hardware busy bit is a live sweep state, so the
   polling code must not wait for a DRAM instruction refill during that interval.
   It then reads the unit list from the table, marks the units on its own mesh
   local, and drains its completion queue (empty: it is reset with the processor).
5. Allocates the inbox, channel queue and barriers, initialises channel free
   lists and runs **the start handshake**, all in the private boot image.
   Owner-local setters install private allocator, unit and message state without
   exposing its addresses. MMU/log initialization and steady-state operations
   remain resident. Syscalls, dispatch, scheduling and polling retain `-O2` and
   the selected HOT flags; there is no boot-descriptor access in those paths.
6. Enables the external interrupt (unless `poll`) and the timer (if
   `tick` > 0).
7. Node 0 starts `init` with the boot table's command line; every node
   enters its scheduler.

### init

`init` (node 0) runs the command line: `;`-separated commands, each
`app args [@node]`, in order, each joined before the next; `shell` reads more
commands from the console. It prints `init: APP exit CODE in N cycles` for
each, `init: N commands, F failed` at the end, and shuts every node down with
the number of failed commands as the exit code. See
[use/apps.md](../use/apps.md#init).

## The start handshake

The interlink keeps its state across a processor reset: the per-node
signal-slot counts, the 16-bit count of rings each node has sent, and the
staging stores are reset only with the whole system, not by `HR_BOOT`
([hardware/constraints.md](../hardware/constraints.md#interlink-state-survives-a-processor-reset)).
A boot that follows another run on the same card finds those counts. Every
image therefore starts through `include/kos/start.h` before it rings any
other node:

1. **Each node clears its own signal slots** (`IL_CTL` bit 3, link enable
   kept) and waits for the sweep to finish.
2. **It takes the interlink's sent-ring count as its own ring count**, so
   the ring-room check (at most four rings queued) starts true.
3. **Node 0 waits until every other node has posted this boot's run id** into
   its slot of node 0's start area (staging `0x21000`, word *j*), then posts
   the run id into word 4 of every other node's start area. **Every other
   node** posts its run id to node 0 and waits for word 4 of its own area to
   hold the run id.

The handshake is posted stores, so it needs no ring. The run id is new for
every boot (`tools/boot.py`), so words an earlier boot left in staging never
match. When it ends, every node has clean slots and a correct ring count,
and node 0 knows every other node has reached the same point.

`run_core.py --boots 2` boots an image twice in one model with the
interlink, staging and DRAM kept between the boots, as a card is from one
run to the next.

## The log area and the progress word

Each node's log area is the 16 KB before its landing zone, written through
the uncached alias so that DRAM holds every byte as soon as it is written:

| offset | what |
|---|---|
| +0 | bytes ever written (`KOS_LOG_COUNT`) |
| +8 | **where**: what the node is doing now (`KOS_W_*`, below) |
| +16, +24 | two detail words (`a`, `b`) |
| +32 | thread switches: moves while the node schedules |
| +64 … | console bytes, wrapping at 16 KB − 64 |

| where | a | b | written when |
|---|---|---|---|
| `boot` (1) | boot step 1–4 | — | kernel boot steps |
| `start` (2) | run id | 0 clearing slots; 1 + *j* waiting for node *j*; 9 waiting for go | the start handshake |
| `ring` (3) | rings this node queued | the interlink status word | waiting for room to ring |
| `send` (4) | destination node | messages sent << 32 \| credits returned | waiting for message credit |
| `idle` (5) | — | — | nothing to run |
| `user` (6) | thread id | its pc | a thread is (re)started |
| `probe` (7) | probe index | — | the bare-metal probe image |
| `done` (8) | exit code | — | the node is exiting |
| `bar` (9) | barrier epoch | its slot count on entry | the bare-metal probe image's barrier |

A host reads the header at any time. `run_card.py` prints every node's
`where` line every 30 s while it waits, and after the run — also after a
timeout — every node's whole log and final `where`; both are in its JSON.
`run_core.py` reads the same from a model after the run; `--hold N` leaves
model node N in reset, so its peers stop in the start handshake with
`where start`, `b` = 1 + N.

The console register is also written byte by byte; on the card the host
drains it over JTAG, slower than a node prints, so the log area is the
complete record and `run_card.py` uses it when it is longer than what was
drained.
