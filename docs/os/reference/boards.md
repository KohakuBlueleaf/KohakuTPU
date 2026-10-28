---
title: KohakuAccel OS boards
summary: The board description format (nodes, meshes, DRAM layout and address spelling, staging, units, which model or card runs it) and every board the OS ships with, including the non-TPU board.
tags:
  - os
  - reference
  - boards
---

# Boards

One OS image runs on every board; **a board description is the only thing
that differs**. It lives in `software/os/boards/<name>.json`; `tools/boot.py`
turns it into one boot table per node ([boot.md](../design/boot.md#the-boot-table)).

## Format

```json
{
  "name": "card_v9t6_4n",
  "description": "...",
  "nodes": 4,
  "mesh_of": [0, 1, 2, 3],
  "clock_hz": 100000000,
  "engine": true,
  "dram": {"base": "0x20000000", "layout": "carved", "private": "0x100000", "shared": "0x0"},
  "spelling": "own",
  "staging": "0x80000",
  "units": [{"type": "MG", "mesh": 0, "x": 1, "y": 1, "depth": 512}, "..."],
  "card": {"board": "multimesh_v9t6", "meshes": 4}
}
```

| key | meaning |
|---|---|
| `nodes` | node count, 1–4 |
| `mesh_of` | the mesh each node sits on (the address bits 37:36 that reach it) |
| `clock_hz` | node clock (default 100 MHz) |
| `engine` | the nodes have a dispatch engine |
| `dram` | the memory, below |
| `spelling` | which mesh bits a node puts on DRAM addresses, below |
| `staging` | the size of each mesh's staging store (the OS needs at least `0x21040`) |
| `units` | the compute units: `type` (two ASCII letters), `mesh`, `x`, `y`, optional `flags`, `depth` (credit; 0 = 16) — at most 32 |
| `model` | for a core-only model: `{kind: "core", nodes, shared, build}` |
| `card` | for a card or a Verilated card model: `{board, meshes}` (the card's board name and how many meshes it has) |

### `dram`

`base`, `private` (bytes per node), `shared` (bytes; 0 = no shared region),
and `layout`:

| layout | private span of node *j* | shared region |
|---|---|---|
| `separate` | `base` on every node (each node has its own DRAM behind its mesh) | `base + private` |
| `carved` | `base + j × private` (one DRAM, cut into consecutive spans) | `base + nodes × private` |

Every node's span must be at least 256 KB. The OS places in it the user
image, frames, (with `pt_dram`) the page-table pool, the 16 KB log area and
the 64 KB landing zone ([memory.md](../design/memory.md#the-private-span)).

### `spelling` (DRAM addresses and mesh bits)

Addresses in `dram` carry no mesh bits. `spelling` says what a node puts on
them for **its own** span and the shared region: `"own"` (its own mesh's
bits) or `"mesh0"` (none), for both kinds, or per kind as `{"private": …,
"shared": …}`. Another node's span is always named with **that** node's mesh
bits — that is the address a mover uses to deliver into its landing zone.

## The boards

| board | nodes | runs on | memory | units | engine |
|---|---|---|---|---|---|
| `core2` | 2 | core-only model, 2 nodes | `separate`: 4 MB each at `0x1000_0000`, no shared region | — | yes |
| `core4s` | 4 | core-only model, 4 nodes on one RAM | `carved`: 512 KB each from `0x1000_0000`, 2 MB shared | — | yes |
| `core3s` | 3 | core-only model, 3 nodes on one RAM | `carved`: 1 MB each, 1 MB shared | — | **no** |
| `card_v9t6_1n` | 1 | the 1-die Verilated card model (`vlt_v9t6_2n`, 1 mesh) | `carved` from `0x2000_0000`: 1 MB private + 1 MB shared | 2 MG + 2 VC | yes |
| `card_v9t6_2n` | 2 | the 2-die Verilated card model (`vlt_v9t6_2n`) | `carved` from `0x2000_0000`: 1 MB per die, no shared region | 8 (2 MG + 2 VC per die) | yes |
| `card_v9t6_4n` | 4 | KohakuTPU v9t6 silicon (`multimesh_v9t6`) | `carved` from `0x2000_0000`: 1 MB per die, no shared region | 16 | yes |
| `card_v9t6_4n_solo` | 1 | the same silicon, die 0's node alone | die 0's span as in `card_v9t6_4n` | die 0's 4 | yes |

Notes on the choices the boards encode:

- **Card boards have no shared region on several dies.** A load does not
  cross the interlink on the card, so no DRAM is readable by every die;
  applications that need shared memory (`preduce`, `psort`, `pmatmul`,
  `workq`, `stress`) say so and skip on those boards, while the
  message-passing ones (`pipeline`, `msgsum`, `mover`, `mg`) run.
- **Card spans are carved at different offsets.** On the 2-die card model the
  DRAM behind mesh 0 and mesh 1 is one array (the same offset under either
  mesh is one cell), so two spans at the same offset would overwrite each
  other; carving keeps them apart on the model and on silicon alike.
- **`core3s` is the non-TPU board**: three nodes, shared memory, no compute
  units and no dispatch engine. The same image boots on it and runs every
  CPU application; `units` reports 0. It is the check that nothing in L0/L1
  depends on a KohakuTPU being there.
- **`card_v9t6_4n_solo`** loads and starts only die 0 of the 4-die card —
  the smallest silicon run (one node, its four units).

## Adding a board

Write the JSON; nothing in the OS changes. `python software/os/tools/boot.py
<board> --out DIR` checks it (node count, regions and units fit the boot
table) and writes the tables. A core-only model for a new shape is built with
`sim/build.py --nodes N --shared 0|1` ([building.md](../use/building.md)).
