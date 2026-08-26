# sim/verilator — a simulated card

The goal is one thing:

> **An external AXI master → station bus → system node → mesh, running a real
> program, driven from the software stack. A card you can test a driver against
> without touching silicon.**

Not a faster bench runner. The benches under `tests/` are a way to *validate the
model*; they are not the product. The product is a C++ object the Python driver
talks to exactly as it talks to JTAG or XDMA.

## There is no wall

Every "Verilator can't do X" in this tree turned out to be a shim or a harness
question. Recorded so nobody re-derives them:

| Claimed blocker | Actual status |
|---|---|
| XPM macros | Four cells, four shims, cross-checked against the real Xilinx cells. [shims.md](docs/shims.md) |
| `deassign` in Vivado's `xpm_memory.sv` | Real, and confined to `xpm_memory_base`. Routed around, not fought. |
| Double-pumped matmul needs `BUFGCE_DIV` | **Never a blocker.** The repo already guards it with `` `ifdef SYNTHESIS `` and simulates a behavioural divider. `cluster_node_pump` passes: 7524 checks. |
| `MX_MODEL=0` / DSP48E2 | Not needed for a card. `MX_MODEL=1` is the default everywhere. |
| X-propagation loss | Irrelevant to a card model — the driver supplies stimulus, not X. [divergences.md](docs/divergences.md) |
| `disable`, `$random` in benches | Testbench-only. A C++ harness has no testbench. |

## The documents

| | |
|---|---|
| **[docs/card-backend.md](docs/card-backend.md)** | **The point of all this.** Verilator as a real backend for the software stack, and how it plugs into `driver/kohakuaccel` with no driver changes. |
| [docs/status.md](docs/status.md) | Work log. What runs today, what is open, what was measured. Updated as work lands. |
| [docs/setup.md](docs/setup.md) | Install, and `scripts/py/vlt.py`. |
| [docs/shims.md](docs/shims.md) | The primitive replacements, why each exists, and the cross-check method that validates them. |
| [docs/divergences.md](docs/divergences.md) | Where Verilator and xsim disagree, and which disagreements matter. |

## Layout

```
sim/verilator/
  README.md            this file
  docs/                the five documents above
  shims/               simulation-only primitive replacements
  examples/            cross-check benches: same bench, both simulators
scripts/py/vlt.py      runs a bench under Verilator, reusing xsim.py's BENCHES
```

Nothing here modifies `src/`, `tests/`, or `scripts/py/xsim.py`. The xsim flow is
untouched and stays the flow of record.

## One command

```
python scripts/py/vlt.py cluster_node
python scripts/py/vlt.py ctrlpe_mesh --lint-only
```
