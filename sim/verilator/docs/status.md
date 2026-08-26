# Status and work log

Updated as work lands. Every number here was produced by a run on this machine,
not estimated. Environment: Verilator 5.020 (Debian `5.020-1`) in WSL
`Ubuntu-24.04`, g++ 13.3, against Vivado 2024.2. Session date 2026-08-25.

## Headline

**The full card chain elaborates and runs.** `ctrlpe_mesh` — external AXI master
→ `sb_line4` station bus → `sysnode` → populated mesh with a control processor,
90 source files — **lints clean with zero errors**, builds, and runs a program to
completion in ~50 s wall.

It does not yet produce the right answer. That is one open bug, tracked below.

## Bench matrix

Same `--build-root`, both simulators, same day.

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

Broken in the working tree under **both** simulators (the in-flight
`src/kohakuaccel/sysnode/` restructure, not Verilator): `mm_mesh`, `saxpy_mesh` —
`module 'mag' does not have a parameter named MEM_PORTS`, and
`src/kohakutpu/top/mag_1m.v` no longer exists. Verilator reported it in 1.4 s
against xsim's ~40 s.

## Speed

The honest reading: **Verilator's run is 100–300× faster; its C++ build is not.**

- `cluster_node`: 0.044 s of simulation against xsim's 15.2 s whole flow.
- `sb_line4` reached **200 ms of simulated time in 495 s** — roughly 80 M clock
  periods across twelve clocks. That is the number that matters for a card:
  long runs are affordable.
- Build is 5–50 s and is paid once per RTL change, not per run. For a card
  backend the model is built once and then driven for hours, so build time
  stops being on the critical path entirely.

## Fixed, with the measurement that found it

**FIFO capacity was wrong in both shims.** A fwft XPM FIFO carries words in
output stages *beyond* the array, so a shim sized to `FIFO_WRITE_DEPTH` is
shallower than the real cell. Anything sizing credit against the real depth then
deadlocks with no error message.

Found by reporting peak occupancy in the cross-check benches and comparing:

| Cell | real (xsim) | shim before | shim now |
|---|---|---|---|
| `xpm_fifo_sync`, depth 16 | 18 | 16 | **18** |
| `xpm_fifo_async`, depth 32 | 33 | 32 | **33** |

Note they are **not symmetric** — sync carries two, async carries one. Assuming
symmetry would have left the async one wrong.

**`PE_DIR` was missing from `vlt.py`.** `xsim.py` writes a `kohaku_predef.vh`
defining it as the first source; without it the nine PE benches resolve
`../../tests/pe/build` against the run directory and report "no cases" while the
images exist. Fixed.

**`--timescale 1ns/1ps`** now matches `xelab`'s. Most RTL here carries no
`timescale` of its own.

## Open

**1. Three benches fail with both FIFO shims validated.** `axi_n1` is the
smallest: its entire file list is `sync_fifo.v`, `async_fifo.v`, `axi_n1.v`,
`axi_n1_tb.v`. Both shims now match the real cells on ordering *and* capacity,
so the cause is elsewhere. Two candidates, not yet separated:

- **Shim behaviour still unmodelled.** `wr_rst_busy` duration is the prime
  suspect: the real XPM holds it for many cycles while it clears the array; the
  shims release after one. Releasing *early* is permissive, so this would show as
  a race rather than a hang — but it is untested either way.
- **Testbench idioms.** `axi_n1_tb.v` carries 19 non-blocking assignments inside
  `initial` blocks driving stimulus against `@(posedge aclk)` (Verilator reports
  them as `INITIALDLY`). That is a clock-edge race that the two simulators
  resolve differently, and no shim fix will change it.

The way to separate them is a cross-check bench for reset-during-traffic, the
same method that found the capacity bug. **This is the next piece of work.**

For the card goal these three matter only as model validation — the C++ harness
has no testbench, so `INITIALDLY` and `disable` cannot affect it.

**2. `vec_cvt` diverges deterministically and it is not X.** Run under
`--x-assign 0`, `1`, and `unique`: **13912 errors in all three, identical.** No
XPM in its file list. The failures are directed FP32 extremes (`0xff7fffff` =
−FLT_MAX). One of the two simulators is wrong about this repo's FP32 saturation
path, which is worth chasing on its own merits.

**3. `ctrlpe_mesh` fails at "the processor never started".** The dispatcher
drains its program (that check passes), so flits leave the orchestrator and the
PE does not run. Given `sysnode_ctrlpe` passes, the difference is the station bus
and NoC in front of it — likely the same root cause as item 1.

## Next three

1. Cross-check reset-during-traffic for both FIFO shims; settle item 1.
2. With the model trusted, stand up the `--cc` harness and prove one
   `write64`/`read64` against `S_AXI_CTRL` ([card-backend.md](card-backend.md)).
3. Chase the `vec_cvt` FP32 divergence to a single expression.
