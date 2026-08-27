---
title: A dispatch convention to start with
summary: The minimal contract between the KohakuTPU compiler's artifact and an on-chip SysNode runner, for one node on one mesh. What mesh_art.c already does, written down as a starting point.
tags:
  - cpu
  - rv64
  - sysnode
  - dispatch
---

# A dispatch convention to start with

Scope: **one SysNode, one mesh, no interlink.** This is the tractable case — the
compiler owns tiling, addressing and ordering; the SysNode is a thin runner that
replays the artifact. Multi-node (each node running its slice, syncing over the
interlink) is a layer *above* this, not a change to it.

This is not a new design; it is what [`tests/rv64/mesh_art.c`](../../../../tests/rv64/mesh_art.c)
already does, named so we can build on it.

## The artifact (compiler-owned, unchanged)

`{ flits: [256-bit CU_INST payloads], steps: [seed | kick | await | barrier] }`,
exactly as `kohakuaccel.artifact.Artifact` serialises. The payloads are
**header-less** — the routing header is the dispatcher's, stamped by hardware.
Operands are **already in DRAM** before dispatch; the artifact never carries data,
because every unit L1 load is a self-fill descriptor reading DRAM.

## The C form (embedded, to start)

An artifact compiles to a C table, e.g. `vadd_artifact.h`:

```c
#define ART_NFLIT 26
static const unsigned long ART_FLITS[ART_NFLIT][4]; /* {ARG3,ARG2,ARG1,ARG0} per flit */
/* plus the operand/result DRAM addresses the compiler assigned */
```

Each row is one 256-bit payload split into the four mailbox words, high to low.
(Later this table lives in DRAM and the runner walks it there; embedding is the
starting point, not the endpoint.)

## The runner loop

One pass over `steps`, in order:

| step | on the SysNode |
|---|---|
| `seed(n)` | **no-op** — the mailbox has no credit register; the runner self-throttles instead |
| `kick(x, y, base, nflits)` | for each `flits[base .. base+nflits)`: poll `STAT[15]==0`, then write `DST=(y<<8)|x`, `ARG0..ARG3`, `GO`. Keep outstanding ≤ the target's `inst_depth` |
| `await(x, y, count)` | drain `count` completions (`STAT[7:0]` + read `HEAD` / write `HEAD` to pop); a `code == SIG_FAULT` is a failure |
| `barrier` | drain all outstanding completions |

The mailbox tags every flit `last=1`, so each retires as `SIG_BATCH_COMPLETE` —
the runner **counts** completions rather than trusting the batch flag.

## Two rules the hardware forces

1. **`GO` needs `STAT[15]==0` first.** A second `GO` while a flit is still offered
   is dropped silently. Poll between sends.
2. **A completion does not order the unit's DRAM writes.** A unit retires when its
   last write beat is *sent*, and this L1 is not coherent with another unit's
   writes. **Settle** (a delay, or a later dependent kick) before reading a result.

## The operand rule

Operands go to DRAM at the compiler's assigned addresses. The SysNode reaches DRAM
as `DRAM_BASE | addr` — the low bits decode to the memory node's `axi_ram`, which
is the same place a unit's self-fill lands, so the two agree by construction.

**Format is per unit.** A vector core's VFILL reads plain fp16 and converts to
E8M15 internally, so vec operands stay fp16. A matmul cluster's FILL reads
**MXFP7-packed entries** (128 B/entry: 7-bit fields + E5M3 scales, via
`to_mxfp7_words_tiled`), *not* fp16 — the offline model reads fp16 and quantises at
compute, which matches the numbers but not the DRAM bytes. Put the right format in
DRAM per the target unit.

## Multi-unit and fusion (what the steps already encode)

A real kernel (a matmul with a vec epilogue; attention) spans mat **and** vec
units, and the runner must honour the ordering the compiler put in `steps` — it
is not free. From a fused mat→vec artifact (`mm_silu`):

- The producer round kicks all the mat clusters, then **awaits both** the
  producers' own completions **and** the consumers' `SIG_DATA_RECEIVED` peer-acks
  — on vec coords the round never kicked. The mat→vec data is a **CU_DATA peer
  burst** drained straight into the vec core's L1, in fabric, not through the
  SysNode. The peer-ack await is what proves the tile is resident before the
  barrier; the vec kernel itself does not block on the burst.
- A **hard barrier** separates the two dispatches (the producer round fully
  retires before the consumer epilogue is staged).
- Two invariants: **restage every round from slot 0**, and a node kicked N times
  in a round is **awaited once for the cumulative total** (the poll is `==`, not
  `≥`). So the runner tracks per-node cumulative completion counts.

So the runner is still thin — it just interprets `kick`/`await`/`barrier`
faithfully, including awaits on coords it never kicked. The fusion is the
compiler's; the ordering is the steps'.

## Memory movement: a vec kernel, not the mover

Transpose and rearrange of intermediates compile to **vector-core relayout
kernels** (word-permute via `VFILL`/`VDRAIN`, or a 4×4 granule transpose via
`VSHUF`), dispatched like any other vec kernel — **never** a node-mover
descriptor. The hardware node mover (`mm_mover`, `MODE_TRANSPOSE`) exists and the
SysNode can drive it directly through the `0x80` window, but no compiler artifact
targets it. So "memory-movement requirements" in a compiled model are just more
vec dispatches; the runner needs nothing new for them.

## Deliberately out of scope (for now)

- **Cross-mesh.** `DST` is mesh-local (`x`,`y`, no mesh id). Multi-node dispatch,
  global addresses (mesh id at `addr[37:36]`) and interlink doorbells are the OS
  layer above this runner.
- **Scheduling.** `steps` run in order; the compiler already decided them.
