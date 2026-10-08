---
title: Cluster tiling
summary: How a matmul-shaped kernel picks gm, gn and nk per call — the machine's limits, the measured cost model, and what it chooses against the card model.
tags:
  - kohakutpu
  - compiler
  - matmul
---

# Cluster tiling: how a matmul-shaped kernel picks `gm`, `gn`, `nk`

`compiler/kohakutpu/tiling.py` chooses the cluster tiling per call. A kernel
opts in with `@kernel(tiler=MatmulTiler("M", "K", "N"))` (`ops.matmul`,
`kernels.attention.project_heads`); a caller that names any of `gm`, `gn`,
`nk` keeps its own and the tiler is not consulted.

## The machine it tiles for

- `gm x gn` sub-tiles of 4x4 stay resident in the accumulator (`TILES`, 4096
  in every generated mesh top), so `gm * gn <= 4096`.
- Each L1 side has two banks of 256 entries. One bank fills while the sweep
  reads the other; a FILL counts entries in 8 bits, so `gm * nk` and
  `gn * nk` are at most 255.
- A K-block is 32 elements. The double-pumped array issues K-blocks **in
  pairs**: a sweep of `nk` blocks costs `gm * gn * (nk + nk % 2) * 0.5` node
  cycles. An odd `nk` pays for the pair — `nk = 1` at 256x512x256 runs 72,250
  cycles against 39,440 at `nk = 2`.

## The cost model

Per K-step one bank fills (A then B) while the other is swept, so a step costs
the larger of the two; the first fill and the last sweep are exposed, the
drain adds its per-sub-tile tail after the last sweep, and every instruction
word passes the node dispatcher. A package adds a fixed cost for its header,
bindings and barrier.

| Constant | Value | Measured as |
|---|---|---|
| `ENTRY_CYCLES` | 4.25 | a 128-entry FILL in 595 cycles, staging or DRAM |
| `FILL_CYCLES` | 50 | the descriptor round trip before the first entry |
| `ISSUE_CYCLES` | 0.5 | one 4x4x32 issue, paired per the above |
| `SWEEP_CYCLES` | 12 | a 64x64, nk 2 sweep (8,192 issues) in 4,108 cycles |
| `DRAIN_CYCLES` | 1.4 | per sub-tile, after the last sweep |
| `WORD_CYCLES` | 600 | dispatcher cost per word, firmware at `-O2` (median 598) |
| `PACKAGE_CYCLES` | 2,200 | header, bindings and final barrier |

All on the Verilated card (`card_v8t8_2n`, real Xache, sys/NoC/unit 300 MHz,
mat2x 600 MHz, one MG).

## What it picks, and how close it is

| Shape | Choice | Predicted | Measured | Floor | Efficiency |
|---|---|---|---|---|---|
| 64x256x64 | per the model | — | 4,933 | 1,024 | 21% |
| 128x256x128 | per the model | — | 8,707 | 4,096 | 47% |
| 256x256x256 | 64x64, nk 2 | 25,554 | 23,091 | 16,384 | 71% |
| 256x512x256 | 64x64, nk 2 | 41,986 | 39,439 | 32,768 | 83% |
| 256x1024x256 | 64x64, nk 2 | 74,850 | 72,392 | 65,536 | 91% |
| 512x512x512 | 64x64, nk 2 | 161,346 | 151,235 | 131,072 | 87% |

The floor is the cluster's issue rate alone. The two small shapes are bound by
the per-package and per-word dispatcher cost, not by the cluster.
