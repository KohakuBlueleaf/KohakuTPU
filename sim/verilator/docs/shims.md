# The shims

Simulation-only replacements for vendor primitives, under `sim/verilator/shims/`.
Xilinx sources are **never copied and never patched** — they are read in place
from the Vivado install when they work, and modelled independently when they do
not.

## Why any exist

Vivado's own behavioural source is at
`D:\Xilinx\Vivado\2024.2\data\ip\xpm\{xpm_cdc,xpm_fifo,xpm_memory}\hdl\*.sv`, and
the natural first move is to compile it directly. Two things stop that, and only
one is fatal:

**Assertions — solvable.** The XPM sources carry SVA that Verilator 5.020 rejects
(`## () cycle delay range`, `[*] boolean abbrev`). `+define+OBSOLETE` clears
**every** one of them across all three libraries. (`DISABLE_XPM_ASSERTIONS`
clears the FIFO's but not the CDC's — `OBSOLETE` is the one that covers both.)

**`deassign` — fatal.** After that define, the only remaining error is eight
Verilog-1995 `deassign` statements, all inside **`xpm_memory_base`**. Verilator
rejects `deassign` outright, and `xpm_fifo` instantiates `xpm_memory_base`, so
that single module blocks every FIFO and every RAM in the repo.

`data/verilog/src/unisims/BUFGCE_DIV.v` has the same problem at lines 504–505.

## What this repo actually needs

Only four XPM cells, each named by exactly one wrapper:

| Cell | Wrapper |
|---|---|
| `xpm_fifo_sync` | `src/kohakuaccel/common/sync_fifo.v` |
| `xpm_fifo_async` | `src/kohakuaccel/common/async_fifo.v` |
| `xpm_memory_sdpram` | `src/kohakuaccel/common/kohaku_sdpram.v` |
| `xpm_memory_tdpram` | `src/kohakuaccel/pe/rv32/mem/rv_ram_be.v` |

Because each wrapper pins every mode (no ECC, no byte enables on the SDP, fwft,
`FIFO_READ_LATENCY(0)`, `USE_ADV_FEATURES(0)`, `no_change` on the TDP), the
surface to model is small. Every shim `$fatal`s on a mode it does not implement
rather than approximating one — a wrong waveform that looks plausible is worse
than a stopped run.

## What does NOT need a shim

**`BUFGCE_DIV`, and therefore the double-pumped matmul core.** The repo already
guards every instantiation with `` `ifdef SYNTHESIS `` and simulates a
behavioural divider in the `else` branch — see `ktpu_div2.v:20-35` and
`mx_cluster_cu_pump.v:49-62`, both of which note that the model exists so `O`
rises *with* an `I` edge rather than a delta after it.

`cluster_node_pump` passes under Verilator: **7524 checks, 0 errors.**

## Uninitialised memory is left uninitialised

No shim zeroes its array. Reading an address never written stays a real X under a
four-state simulator and is randomised per-run under `--x-initial unique`.
Zeroing would hide exactly the read-before-write bugs the benches exist to catch.

## How a shim is validated

**One bench, both simulators.** xsim binds the real Xilinx cell; Verilator binds
the shim; the results must agree. The benches live in `sim/verilator/examples/`
and are deliberately free of `$random` and of non-blocking assignment in
`initial`, so any disagreement is the DUT and not the stimulus.

```
# real cell
python <scratch>/xrun.py <work> vlt_sync_fifo_tb \
    src/kohakuaccel/common/sync_fifo.v sim/verilator/examples/vlt_sync_fifo_tb.v
# shim
verilator --binary -sv --timing --timescale 1ns/1ps \
    sim/verilator/shims/xpm_fifo_sync.v src/kohakuaccel/common/sync_fifo.v \
    sim/verilator/examples/vlt_sync_fifo_tb.v
```

### Ordering alone is not enough — the bug that proves it

The first async cross-check passed on ordering (512 words, 0 errors, both
simulators) while the shim was still **wrong**. What it did not measure was
capacity.

A fwft XPM FIFO holds words in output stages beyond the array, so a shim sized to
`FIFO_WRITE_DEPTH` is shallower than the real cell. That is not a margin
question: `mag_link` sizes link credit against the real depth, so a shallow FIFO
makes the sender push credit the FIFO cannot hold, and the link deadlocks with no
error printed anywhere.

Reporting peak occupancy alongside the ordering check found it immediately:

| Cell | real (xsim) | shim before | shim now |
|---|---|---|---|
| `xpm_fifo_sync`, depth 16 | 18 | 16 | **18** |
| `xpm_fifo_async`, depth 32 | 33 | 32 | **33** |

**They are not symmetric.** Sync carries two extra words, async carries one.
Assuming symmetry — the obvious move — leaves the async cell wrong by one.

The lesson generalises: **a cross-check must measure every property the design
depends on, not just the one that is easy to check.** Ordering, capacity, and
(still outstanding) reset-during-traffic are three separate measurements.

## Still unvalidated

- **`wr_rst_busy` / `rd_rst_busy` duration.** The real cell holds these for many
  cycles while clearing the array; the shims release after one. No cross-check
  covers it yet, and it is the leading suspect for the open bench failures in
  [status.md](status.md).
- **`xpm_memory_sdpram` and `xpm_memory_tdpram`** have no cross-check bench at
  all. They are exercised indirectly (`cluster_node`, `sysnode_ctrlpe`,
  `mag_stage` all pass) but that is evidence, not proof.

## The Xilinx sources, for reference

| What | Path |
|---|---|
| Primitives, 476 files | `D:\Xilinx\Vivado\2024.2\data\verilog\src\unisims\` |
| `glbl` | `D:\Xilinx\Vivado\2024.2\data\verilog\src\glbl.v` |
| XPM | `D:\Xilinx\Vivado\2024.2\data\ip\xpm\*\hdl\*.sv` |

Linted individually with `glbl.v`: `DSP48E2`, `BUFGCE`, `RAMB36E2`, `RAMB18E2`
and `URAM288` all parse, but reference `glbl.GSR` hierarchically and so need a
wrapper top that instantiates `glbl` (Verilator takes one top; xsim takes
`w.glbl` as a second). `BUFGCE_DIV` is blocked by `deassign`. `LUT6` triggers a
Verilator **internal error** (`V3Gate.cpp:1043`) — that one is a Verilator bug.

None of this is on the path to a card, because `MX_MODEL=1` is the default and
the clock primitives are already `ifdef`-guarded.

## Gotcha worth one line

A comment whose first word is `Verilator` is parsed as a metacomment and fails
the build (`Unknown verilator comment`). Cost two builds. Start the line
differently.
