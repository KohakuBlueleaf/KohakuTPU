# Where the two simulators disagree

Sorted by whether it matters for a card model. Most of it does not — that is the
point of the sort.

## Does not matter for a card

The C++ harness has no Verilog testbench. Everything in this section is a
property of `tests/`, and vanishes the moment stimulus comes from the driver.

**`$random` produces different stimulus.** 27 of the 91 benches use it. `fpacc`
runs 15756 checks under xsim and 14097 under Verilator — both PASS, but they are
not the same test. Irrelevant to a card, where the driver supplies every byte.

**`disable <named fork branch>` does not compile.** Five bench files use it after
`join_any`: `sb_root9_tb.v:588`, `sb_width_tb.v:197`, `sb_quad_tb.v`,
`mag_switch_tb.v`, `fp_alu_tb.v`. Verilator: *"disable isn't underneath a begin
with name"*. No RTL uses it.

**Non-blocking assignment inside `initial` driving stimulus.** 19 in
`axi_n1_tb.v` alone (`INITIALDLY`). The pattern `bready <= x; @(posedge aclk);
while (!(bvalid && bready))` races the DUT's own edge, and the two simulators
resolve that race differently. A bench-writing convention, not a tool defect.

**X-propagation.** Verilator is two-state. The 38 `=== 1'bX` checks across 21
files (e.g. `mover_chain_tb.v`, `ctrlpe_mesh_tb.v`) cannot port and should not —
a two-state model has no X to find. Those stay on xsim.

But note the expectation that X would be the *dominant* loss turned out wrong;
see below.

## Does matter, and is unresolved

**`vec_cvt`: 13912 errors of 340979, and it is not X.** Tested directly rather
than assumed — run under three X policies:

| | errors |
|---|---|
| `--x-assign 0` | 13912 |
| `--x-assign 1` | 13912 |
| `--x-assign unique --x-initial unique` | 13912 |

Byte-identical. The divergence is deterministic and has nothing to do with
two-state modelling. `vec_cvt`'s file list is `mx_fpacc.v`, `vec_cvt.v`,
`vec_cvt_tb.v` — **no XPM**, so the shims are not implicated either. The failures
are directed FP32 extremes (`0xff7fffff` = −FLT_MAX and neighbours) in the
`f32->e8 over half ulp` section.

One of the two simulators is wrong about this repo's FP32 saturation path. That
is worth knowing regardless of Verilator, and it outranks the migration.

**`mm_mover`: same 503 checks, 2 different results.** Identical check count means
identical stimulus and control flow, so this is a pure datapath divergence. Uses
`sync_fifo` and `kohaku_sdpram`, so a shim is a live suspect.

**Three benches hang with both FIFO shims validated.** `mag_link`, `axi_n1`,
`sb_line4`. See [status.md](status.md) for the two candidate causes and the
experiment that separates them.

## Fixed, recorded so it is not re-learned

**FIFO capacity.** Both shims were shallower than the real cells and deadlocked
credit-based flow control. Sync carries two extra words, async carries one, and
they are not symmetric. Full account in [shims.md](shims.md).

**Attribution traps.** Two failures looked like Verilator and were not:

- `mm_mesh` and `saxpy_mesh` fail under **both** simulators — the in-flight
  `sysnode/` restructure removed `mag`'s `MEM_PORTS` parameter and deleted
  `mag_1m.v`. Always get the xsim baseline before blaming the new tool.
- `rv_core`'s "no cases" was a missing `PE_DIR` in `vlt.py`, not a bench problem.

## Things Verilator is simply better at

- **`--lint-only` in seconds.** It found the `MEM_PORTS` breakage in 1.4 s where
  xsim took ~40 s to reach the same error.
- **Warnings xsim never emits.** 160 `LATCH` in `cluster_node`; width
  truncations such as `vec_alu.v:440` dropping 16 bits of a shift. Independent of
  any migration, these are worth a review pass.
