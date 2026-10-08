# Status

Every number here was produced by a run on this machine. Environment: Verilator
5.020 (Debian `5.020-1`) in WSL `Ubuntu-24.04`, g++ 13.3, against Vivado 2024.2.

## The simulated card

`card_v8t8_2n` — the v8t8 image with nodes on dies 0 and 1, each a 1x1 mesh
(`ktpu_sim_1x1_1c1v_1m_nol2_pump`), dies 2 and 3 carrying station, Xache home
and DRAM channel ([card-backend.md](card-backend.md)). Measured 2026-10-08:

| step | result | wall |
|---|---|---|
| `vlt.py card_v8t8_2n --cc` build | links; 3 SLL boundaries of 1,062 wires | 76.1 s |
| `card_run.py --load burn`: model up (`--settle 20000`) | `READY`, `sys_rstn=f` | 14.8 s |
| A_CAPS, nodes 0 and 1 | `flit_width=288` both | |
| 1,024 B through node 0, read back via nodes 0 and 1 + DRAM backdoor | ok | 9.1 s write |
| mover copy, 4 channels x 16 words, read via node 1 | ok, no fault | 1.2 s |
| `hello_kohakuaccel` on each node's RV64 | exit 0 both | 14.3 s, 10.9 s |
| station DECERR | 0, after 71,606 sysnode cycles and 659 host calls | 52.3 s total |
| `card_staging_check.py`, nodes 0 and 1 | round trip ok, 8/8 words in staging banks, DECERR 0 | 2.4 s |

## Bench matrix

Same `--build-root`, both simulators, measured 2026-08-25.

| Bench | xsim | Verilator | Notes |
|---|---|---|---|
| `fpacc` | PASS 15756 chk, 11.4 s | PASS 14097 chk, 5.5 s build + **0.053 s** run | counts differ: `$random` |
| `cluster_node` | PASS 7260 chk, 15.2 s | PASS 7260 chk, 27.9 s build + **0.044 s** run | identical counts |
| `cluster_node_pump` | — | **PASS 7524 chk** | the double-pumped core |
| `vec_alu` | PASS, 13.5 s | PASS, 24.7 s | |
| `vec_regfile` | PASS 656 chk | PASS 656 chk | identical |
| `mag_stage` | PASS 42 chk | PASS 42 chk | identical |
| `sysnode_ctrlpe` | — | **PASS 13 chk** | system node + control processor |
| `mag_link_cdc` | — | **PASS 24 chk** | |
| `ctrlpe_mesh` | PASS 30 chk, 31.6 s | **FAIL**, 50 s | builds + runs; PE never starts |
| `mag_link` | PASS 5584 chk, 15.6 s | **FAIL** | link stops making progress |
| `axi_n1` | PASS 1188 chk, 18.1 s | **FAIL** watchdog | |
| `sb_line4` | PASS 673 chk, 24.9 s | **FAIL** watchdog | 495 s to reach 200 ms sim time |
| `vec_cvt` | PASS 334185 chk | **FAIL** 13912 err | no XPM involved |
| `mm_mover` | PASS 503 chk | **FAIL** 503 chk, 2 err | same count, different results |
| `sb_width`, `sb_root9` | PASS | **build fails** | `disable` on a fork branch |
| `rv_core` | — | needs `rv_gen.py` images | not a defect |

## Speed

**Verilator's run is 100–300× faster; its C++ build is not.**

- `cluster_node`: 0.044 s of simulation against xsim's 15.2 s whole flow.
- `sb_line4` reaches **200 ms of simulated time in 495 s** — roughly 80 M clock
  periods across twelve clocks.
- Build is 5–50 s for a bench and is paid once per RTL change; a card model is
  built once and then driven for hours.

## Shim capacity

A fwft XPM FIFO carries words in output stages beyond the array, so each shim
is sized to the real cell's peak occupancy, measured in the cross-check benches:

| Cell | real (xsim) | shim |
|---|---|---|
| `xpm_fifo_sync`, depth 16 | 18 | 18 |
| `xpm_fifo_async`, depth 32 | 33 | 33 |

They are not symmetric: sync carries two extra words, async one.

`vlt.py` writes `kohaku_predef.vh` (`PE_DIR`, absolute) as the first source and
passes `--timescale 1ns/1ps`, both as `xsim.py`/`xelab` do.

## Open

**1. Three benches fail with both FIFO shims validated.** `axi_n1` is the
smallest: its entire file list is `sync_fifo.v`, `async_fifo.v`, `axi_n1.v`,
`axi_n1_tb.v`. Two candidates, not yet separated:

- **Shim behaviour still unmodelled.** `wr_rst_busy` duration: the real XPM
  holds it for many cycles while it clears the array; the shims release after
  one.
- **Testbench idioms.** `axi_n1_tb.v` carries 19 non-blocking assignments inside
  `initial` blocks driving stimulus against `@(posedge aclk)` (`INITIALDLY`), a
  clock-edge race the two simulators resolve differently.

A cross-check bench for reset-during-traffic separates them. The card harness
has no testbench, so `INITIALDLY` and `disable` cannot affect it.

**2. `vec_cvt` diverges deterministically and it is not X.** Under
`--x-assign 0`, `1` and `unique`: 13912 errors in all three, identical. No XPM
in its file list. The failures are directed FP32 extremes (`0xff7fffff` =
−FLT_MAX).

**3. `ctrlpe_mesh` fails at "the processor never started".** The dispatcher
drains its program, so flits leave the orchestrator and the PE does not run;
`sysnode_ctrlpe` passes, so the difference is the station bus and NoC in front
of it.
