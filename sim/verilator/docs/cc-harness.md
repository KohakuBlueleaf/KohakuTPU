# The `--cc` harnesses

`scripts/py/vlt.py` builds a bench two ways:

| Mode | Command | Result |
|---|---|---|
| `--binary` (default) | `vlt.py <bench>` | a standalone executable running the bench's Verilog testbench; prints PASS/FAIL |
| `--cc` | `vlt.py <bench> --cc <harness.cpp> --keep [--vlt-config f.vlt]` | the design as a C++ class plus a harness that owns `main()`, every clock and `eval()`; kept in `build/vlt_<bench>/obj_dir/vsim` |

`--cc` builds with `--no-timing`: the harness advances time itself, so a Verilog
`#` delay would schedule a resume the harness's clock jumps past. The model class
is named to the harness as `VTOP` (`-DVTOP=V<top>`), so one harness serves every
top of the same port shape.

## The harnesses

All are in `sim/verilator/harness/`.

| Harness | Top | What it does |
|---|---|---|
| `card_main.cpp` | `card_v8t8_2n` (any `gen_card.py` card) | the card backend: every clock, host manager 0, the line protocol `VerilatorTransport` speaks — [card-backend.md](card-backend.md) |
| `rv64_*_main.cpp` | the RV64 benches (`rv64_core`, `rv64_syscore`, `rv64_node_pair`, `rv64_mesh_2p2`, `rv64_win_2p2`, ...) | one core or node with its memories behind C++; ELF loading and console |
| `rv64_decode_main.cpp`, `rv64_alu_main.cpp`, ... | single RV64 blocks | differential checks of one block against a C++ reference |

## What a harness owns

- **The clock plan.** One clock for a bare core; for the card, every domain at its own period, with the mesh-wizard outputs re-timed from `clk_wiz_model`.
- **Memory.** A C++ map behind the core's ports, or the design's own `axi_ram`, reached through backdoor ports.
- **Program load.** The image is written before reset releases. On the card it goes through the load window, or straight into the public arrays (`burn_elf`).
- **The outside interface.** The card harness speaks a line protocol on stdin/stdout, which `kohakuaccel.driver.transport.verilator` drives. The daemon's `--backend verilator` wraps that transport, so any driver client reaches the model.

## What Verilator does not check

- **Block RAM and URAM inference.** A design that simulates correctly can still fall out of block RAM in Vivado.
- **LUT, DSP count or Fmax.** These need Vivado OOC synthesis.
- **Xilinx timing.** A passing run says the function is right, never that the frequency holds.

So iterate on Verilator, gate on Vivado OOC for resources and timing.
