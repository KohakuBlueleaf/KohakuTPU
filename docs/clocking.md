# The mesh clock, and making it runtime-controllable

Status: **design, not built.** Written for the build after the 2026-08-11
implementation.

## 1. Why

A negative WNS on the mesh clock costs a full resynthesis today: the frequency
is baked into `clk_wiz_0` at build time, so "300 MHz did not close" means eight
hours to try 250. That is the wrong unit of iteration for a number nobody can
predict.

It also means **the real Fmax of the silicon has never been measured.** WNS is a
static-analysis verdict at worst-case process, voltage and temperature. It is
not the frequency at which the part stops computing correctly, and the gap
between the two is unknown and unknowable from the reports.

A runtime-controllable mesh clock changes both. A frequency that does not work
costs a register write, at worst a reprogram. And the true Fmax becomes
measurable: run a known-answer GEMM, walk the clock up, find where the answers
break.

## 2. The arithmetic

An MMCME4 produces

```
    Fvco = (Fin / D) * M          800 MHz <= Fvco <= 1600 MHz
    Fout = Fvco / k
```

so one step of `M` moves the output by **`Fin / (D * k)`**. That is the
granularity knob, and `k` sets which band the range lands in.

With `Fin = 100 MHz` and `k = 4`, the output range is 200-400 MHz whatever `D`
is, because the VCO band is fixed. `D` buys resolution:

| D | PFD = Fin/D | M range | VCO step | output step | output range |
|---|---|---|---|---|---|
| 1 | 100 MHz | 8-16 | 100 MHz | 25 MHz | 200-400 |
| 2 | 50 MHz | 16-32 | 50 MHz | 12.5 MHz | 200-400 |
| **4** | **25 MHz** | **32-64** | **25 MHz** | **6.25 MHz** | **200-400** |
| 8 | 12.5 MHz | 64-128 | 12.5 MHz | 3.125 MHz | 200-400 |

**Chosen: `Fin = 100 MHz`, `D = 4`, `k = 4`, `M` swept over 32-64.** 200 to 400
MHz in 6.25 MHz steps, and a sweep is a write to one register field
(`CLKFBOUT_MULT_F`) with `D` and `k` left alone.

### Why not push the PFD lower

10 MHz is the documented MMCME4 minimum, so `D = 10` is legal. Two reasons not
to, and the second is the one that matters:

- **`CLKFBOUT_MULT_F` maxes at 128.** At PFD 10 MHz the VCO only reaches
  10 * 128 = 1280 MHz, so the output range truncates to 200-320 MHz -- losing
  exactly the top of the range being hunted.
- **Jitter rises as the PFD falls, and jitter is clock uncertainty on real
  silicon.** It eats setup margin the same way a slow path does, so a low-PFD
  configuration finds a *lower* apparent Fmax than the design has. The
  measurement would be of the clock generator, not the design.

For finer resolution near a chosen frequency, use the **fractional** part of
`CLKFBOUT_MULT_F` (1/8 steps) at a high PFD, rather than dropping the phase
detector into its noisy corner.

## 3. Structure

Two clock generators, because the control plane must never stand on the clock
it is changing.

```
  system clk ─┬─→ clk_wiz_0      FIXED 200 MHz ─→ jtag_axi, axi_smc, gpio, resets
              │
              └─→ clk_wiz_mesh   VARIABLE ─────→ mesh_0..3 axi_aclk
                     ▲
                     └── AXI4-Lite (dynamic reconfiguration), clocked from the
                         FIXED domain, reached through axi_smc
```

`clk_wiz_mesh` is an ordinary Clocking Wizard with *Dynamic Reconfiguration*
enabled and an AXI4-Lite interface: write M/D/k, pulse load, it relocks. No
custom RTL. Its `s_axi_aclk` must be the fixed 200 MHz.

DDR and XDMA are untouched -- they already run on their own clocks.

## 4. Why this is nearly free here

The design is already asynchronous everywhere it needs to be:

- **`mag_dram_port` crosses mesh <-> DDR through `async_fifo`**, so each DDR
  keeps its own `ui_clk` and does not care what the mesh clock does.
- **`axi_smc` is already multi-clock** (`NUM_CLKS` 6), so the host path already
  crosses domains.
- **The reset chain already gates on `locked`** -- when the MMCM drops lock
  during a retune the meshes go into reset and come out when it relocks, so the
  sequencing mostly falls out.

## 5. What it costs, and the rules

**One knob, not four.** The interlink spans all four meshes on a shared
`axi_aclk`, so they retune together. There is no per-mesh frequency.

**A retune resets every mesh.** L1 residents and accumulator contents are lost.
DRAM survives -- it is on its own controller and clock.

**Quiesce before retuning.** The interlink is credit-based; retuning with flits
in flight leaves credits inconsistent on both sides of a link. Drain first.

The sequence is therefore: quiesce -> retune -> wait for lock -> reset ->
re-initialise -> re-upload anything that lived in L1.

## 6. Measuring the real Fmax

The procedure this exists for:

1. Bring up at a frequency known to work (200 MHz).
2. Upload a GEMM with a known-correct answer, wide enough to exercise the
   critical paths -- the shapes in `docs/limits.md` s6.8 are a good set because
   their failure mode is understood.
3. Step `M` up by one, retune, re-run, compare.
4. The last frequency whose answers are exact is the measured Fmax.

The gap between that number and the reported WNS is the margin static analysis
was holding in reserve. It is worth knowing once, and it turns "did timing
fail?" from a question that blocks a build into one answered after it.
