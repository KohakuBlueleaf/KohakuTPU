/**
 * Out-of-context synthesis measurements, harvested from `build/sweep_*.md`.
 *
 * Every row here is a transcription of one line of one sweep file. Nothing is
 * computed, rounded or reconciled on the way in; where two sweeps disagree
 * about a nominally identical configuration, BOTH numbers are kept and the
 * disagreement is recorded in `KNOWN_DISAGREEMENTS` below.
 *
 * The unit throughout is the **CLB LUT site** from Vivado's utilization report,
 * not a primitive count — see `docs/projects/kohakuaxi/station-bus.md` §2.14 for
 * the ~1.6x conversion. `bram` is tiles and may be fractional (a lone RAMB18 is
 * a half tile).
 *
 * A figure from here is only quotable WITH its configuration. `SWEEPS[name].
 * fixed` and each row's `settings` carry the top module, the generics and the
 * target period; the number on its own describes nothing.
 *
 *   import { SWEEPS, rowsOf, PART } from "@/content/ooc"
 *
 * Produced by `python scripts/py/ooc_sweep.py <suite>`, which runs each
 * configuration in its own working directory under its own Vivado and collects
 * the `@@@REC` / `@@@FMAX` / `@@@HIER` lines that `ooc_record`
 * (`scripts/tcl/ooc_class.tcl`) emits from a SINGLE synthesis — so a sweep's
 * resource table, Fmax table and hierarchical breakdown all describe the same
 * netlist.
 */

export const PART = "xcvu13p-fhgb2104-2L-e"
export const TOOL = "Vivado 2024.2"
export const SCRIPT = "scripts/py/ooc_sweep.py"
export const TCL = "scripts/tcl/ooc_class.tcl"

/** Synthesis only. No opt, no place, no route. */
export const FLOW = "out-of-context synthesis"

/** The columns `ooc_record` emits, in the order the sweep files print them. */
export const METRICS = [
  { key: "lut", label: "CLB LUTs" },
  { key: "lut_log", label: "as logic" },
  { key: "lut_mem", label: "as memory" },
  { key: "lut_dram", label: "as distributed RAM" },
  { key: "lut_srl", label: "as shift register" },
  { key: "ff", label: "CLB registers" },
  { key: "bram", label: "BRAM tiles" },
  { key: "ctrlsets", label: "control sets" },
]

/* ------------------------------------------------------------------------ */
/* GPU — the KohakuSIMT measurement ladder. One module, generics per gate.    */
/* ------------------------------------------------------------------------ */

const gpuLadder = {
  file: "build/sweep_gpu-ladder.md",
  suite: "gpu-ladder",
  family: "simt",
  title: "The SIMT gate ladder",
  varies: "WAVES, HAS_MASK, HAS_IPDOM — one generic per row",
  top: "kht_unit",
  fixed: "LANES=8, VREG_PRIM=block, HAS_SHFL=0, period=3.333 ns",
  doc: "docs/projects/kohakumpe/simt/ladder.md",
  rows: [
    { config: "g0-substrate", lut: 2952, lut_log: 2952, lut_mem: 0, lut_dram: 0, lut_srl: 0, ff: 307, bram: 8, ctrlsets: 2,
      settings: "top=kht_unit lanes=8 waves=1 mask=0 ipdom=0 period=3.333" },
    { config: "g1-waves", lut: 2952, lut_log: 2952, lut_mem: 0, lut_dram: 0, lut_srl: 0, ff: 311, bram: 8, ctrlsets: 2,
      settings: "top=kht_unit lanes=8 waves=16 mask=0 ipdom=0 period=3.333" },
    { config: "g2-mask", lut: 3016, lut_log: 3016, lut_mem: 0, lut_dram: 0, lut_srl: 0, ff: 447, bram: 8, ctrlsets: 18,
      settings: "top=kht_unit lanes=8 waves=16 mask=1 ipdom=0 period=3.333" },
    { config: "g3-ipdom", lut: 3204, lut_log: 3184, lut_mem: 20, lut_dram: 20, lut_srl: 0, ff: 516, bram: 8, ctrlsets: 36,
      settings: "top=kht_unit lanes=8 waves=16 mask=1 ipdom=1 period=3.333" },
  ],
  fmax: {
    "g0-substrate": { noc_clk: 324.1 },
    "g1-waves": { noc_clk: 324.1 },
    "g2-mask": { noc_clk: 324.1 },
    "g3-ipdom": { noc_clk: 324.1 },
  },
}

const gpuWaves = {
  file: "build/sweep_gpu-waves.md",
  suite: "gpu-waves",
  family: "simt",
  title: "Wave contexts, at the full gate set",
  varies: "WAVES 1 → 16, mask and IPDOM both ON",
  top: "kht_unit",
  fixed: "LANES=8, VREG_PRIM=block, HAS_SHFL=0, period=3.333 ns",
  doc: "docs/projects/kohakumpe/simt/ladder.md",
  rows: [
    { config: "wv-1", lut: 3076, lut_log: 3064, lut_mem: 12, lut_dram: 12, lut_srl: 0, ff: 332, bram: 8, ctrlsets: 6, settings: "waves=1" },
    { config: "wv-2", lut: 3105, lut_log: 3093, lut_mem: 12, lut_dram: 12, lut_srl: 0, ff: 345, bram: 8, ctrlsets: 8, settings: "waves=2" },
    { config: "wv-4", lut: 3097, lut_log: 3085, lut_mem: 12, lut_dram: 12, lut_srl: 0, ff: 370, bram: 8, ctrlsets: 12, settings: "waves=4" },
    { config: "wv-8", lut: 3148, lut_log: 3136, lut_mem: 12, lut_dram: 12, lut_srl: 0, ff: 419, bram: 8, ctrlsets: 20, settings: "waves=8" },
    { config: "wv-16", lut: 3200, lut_log: 3180, lut_mem: 20, lut_dram: 20, lut_srl: 0, ff: 516, bram: 8, ctrlsets: 36, settings: "waves=16" },
  ],
  fmax: {
    "wv-1": { noc_clk: 324.1 }, "wv-2": { noc_clk: 324.1 }, "wv-4": { noc_clk: 324.1 },
    "wv-8": { noc_clk: 324.1 }, "wv-16": { noc_clk: 324.1 },
  },
}

const gpuSched = {
  file: "build/sweep_gpu-sched.md",
  suite: "gpu-sched",
  family: "simt",
  title: "G7 — the wave scheduler, one level up",
  varies: "the same WAVES sweep, on the pipeline instead of the unit",
  top: "kht_core",
  fixed: "LANES=8, VREG_PRIM=block, HAS_SHFL=0, period=3.333 ns",
  doc: "docs/projects/kohakumpe/simt/ladder.md",
  rows: [
    { config: "sc-w1", lut: 7754, lut_log: 7614, lut_mem: 140, lut_dram: 140, lut_srl: 0, ff: 1094, bram: 8, ctrlsets: 29, settings: "top=kht_core waves=1" },
    { config: "sc-w2", lut: 8054, lut_log: 7786, lut_mem: 268, lut_dram: 268, lut_srl: 0, ff: 1140, bram: 8, ctrlsets: 33, settings: "top=kht_core waves=2" },
    { config: "sc-w4", lut: 8231, lut_log: 8011, lut_mem: 220, lut_dram: 220, lut_srl: 0, ff: 1234, bram: 8, ctrlsets: 41, settings: "top=kht_core waves=4" },
    { config: "sc-w8", lut: 8661, lut_log: 8233, lut_mem: 428, lut_dram: 428, lut_srl: 0, ff: 1420, bram: 8, ctrlsets: 55, settings: "top=kht_core waves=8" },
    { config: "sc-w16", lut: 9653, lut_log: 8801, lut_mem: 852, lut_dram: 852, lut_srl: 0, ff: 1797, bram: 8, ctrlsets: 84, settings: "top=kht_core waves=16" },
  ],
  fmax: {
    "sc-w1": { noc_clk: 277.9 }, "sc-w2": { noc_clk: 294.6 }, "sc-w4": { noc_clk: 273.5 },
    "sc-w8": { noc_clk: 262.7 }, "sc-w16": { noc_clk: 279.5 },
  },
}

const gpuLanes = {
  file: "build/sweep_gpu-lanes.md",
  suite: "gpu-lanes",
  family: "simt",
  title: "Lane scaling",
  varies: "LANES 4 → 32 at the full gate set",
  top: "kht_unit",
  fixed: "WAVES=16, mask=1, ipdom=1, VREG_PRIM=block, HAS_SHFL=0, period=3.333 ns",
  doc: "docs/projects/kohakumpe/simt/ladder.md",
  rows: [
    { config: "l-4", lut: 1659, lut_log: 1649, lut_mem: 10, lut_dram: 10, lut_srl: 0, ff: 316, bram: 4, ctrlsets: 36, settings: "lanes=4" },
    { config: "l-8", lut: 3204, lut_log: 3184, lut_mem: 20, lut_dram: 20, lut_srl: 0, ff: 516, bram: 8, ctrlsets: 36, settings: "lanes=8" },
    { config: "l-16", lut: 6355, lut_log: 6315, lut_mem: 40, lut_dram: 40, lut_srl: 0, ff: 916, bram: 16, ctrlsets: 36, settings: "lanes=16" },
    { config: "l-32", lut: 12478, lut_log: 12404, lut_mem: 74, lut_dram: 74, lut_srl: 0, ff: 1716, bram: 32, ctrlsets: 36, settings: "lanes=32" },
  ],
  fmax: {
    "l-4": { noc_clk: 324.1 }, "l-8": { noc_clk: 324.1 },
    "l-16": { noc_clk: 324.0 }, "l-32": { noc_clk: 324.1 },
  },
}

const gpuLds = {
  file: "build/sweep_gpu-lds.md",
  suite: "gpu-lds",
  family: "simt",
  title: "G4 — the banked LDS and its conflict resolver",
  varies: "LANES 4 → 32 on the shared-memory block",
  top: "kht_lds",
  fixed: "MEM_PRIM=block, period=3.333 ns",
  doc: "docs/projects/kohakumpe/simt/ladder.md",
  rows: [
    { config: "lds-4", lut: 509, lut_log: 509, lut_mem: 0, lut_dram: 0, lut_srl: 0, ff: 319, bram: 4, ctrlsets: 8, settings: "top=kht_lds lanes=4" },
    { config: "lds-8", lut: 1633, lut_log: 1633, lut_mem: 0, lut_dram: 0, lut_srl: 0, ff: 603, bram: 8, ctrlsets: 12, settings: "top=kht_lds lanes=8" },
    { config: "lds-16", lut: 6194, lut_log: 6194, lut_mem: 0, lut_dram: 0, lut_srl: 0, ff: 1171, bram: 16, ctrlsets: 20, settings: "top=kht_lds lanes=16" },
    { config: "lds-32", lut: 25961, lut_log: 25961, lut_mem: 0, lut_dram: 0, lut_srl: 0, ff: 2307, bram: 32, ctrlsets: 36, settings: "top=kht_lds lanes=32" },
  ],
  fmax: {
    "lds-4": { noc_clk: 643.1 }, "lds-8": { noc_clk: 514.9 },
    "lds-16": { noc_clk: 339.2 }, "lds-32": { noc_clk: 317.7 },
  },
}

const gpuShfl = {
  file: "build/sweep_gpu-shfl.md",
  suite: "gpu-shfl",
  family: "simt",
  title: "G8 — the subgroup butterfly, off against on",
  varies: "HAS_SHFL 0/1 at four lane counts",
  top: "kht_unit",
  fixed: "WAVES=16, mask=1, ipdom=1, VREG_PRIM=block, period=3.333 ns",
  doc: "docs/projects/kohakumpe/simt/ladder.md",
  rows: [
    { config: "sh-4-off", lut: 1659, lut_log: 1649, lut_mem: 10, lut_dram: 10, lut_srl: 0, ff: 316, bram: 4, ctrlsets: 36, settings: "lanes=4 shfl=0" },
    { config: "sh-4-on", lut: 1927, lut_log: 1917, lut_mem: 10, lut_dram: 10, lut_srl: 0, ff: 323, bram: 4, ctrlsets: 36, settings: "lanes=4 shfl=1" },
    { config: "sh-8-off", lut: 3204, lut_log: 3184, lut_mem: 20, lut_dram: 20, lut_srl: 0, ff: 516, bram: 8, ctrlsets: 36, settings: "lanes=8 shfl=0" },
    { config: "sh-8-on", lut: 4139, lut_log: 4119, lut_mem: 20, lut_dram: 20, lut_srl: 0, ff: 542, bram: 8, ctrlsets: 36, settings: "lanes=8 shfl=1" },
    { config: "sh-16-off", lut: 6355, lut_log: 6315, lut_mem: 40, lut_dram: 40, lut_srl: 0, ff: 916, bram: 16, ctrlsets: 36, settings: "lanes=16 shfl=0" },
    { config: "sh-16-on", lut: 9374, lut_log: 9334, lut_mem: 40, lut_dram: 40, lut_srl: 0, ff: 934, bram: 16, ctrlsets: 36, settings: "lanes=16 shfl=1" },
    { config: "sh-32-off", lut: 12478, lut_log: 12404, lut_mem: 74, lut_dram: 74, lut_srl: 0, ff: 1716, bram: 32, ctrlsets: 36, settings: "lanes=32 shfl=0" },
    { config: "sh-32-on", lut: 18478, lut_log: 18404, lut_mem: 74, lut_dram: 74, lut_srl: 0, ff: 1788, bram: 32, ctrlsets: 36, settings: "lanes=32 shfl=1" },
  ],
  fmax: {
    "sh-4-off": { noc_clk: 324.1 }, "sh-4-on": { noc_clk: 302.3 },
    "sh-8-off": { noc_clk: 324.1 }, "sh-8-on": { noc_clk: 285.5 },
    "sh-16-off": { noc_clk: 324.0 }, "sh-16-on": { noc_clk: 303.7 },
    "sh-32-off": { noc_clk: 324.1 }, "sh-32-on": { noc_clk: 301.7 },
  },
}

const gpuVregprim = {
  file: "build/sweep_gpu-vregprim.md",
  suite: "gpu-vregprim",
  family: "simt",
  title: "The vector register file primitive",
  varies: "VREG_PRIM block against distributed",
  top: "kht_unit",
  fixed: "LANES=8, WAVES=16, mask=1, ipdom=1, period=3.333 ns",
  doc: "docs/projects/kohakumpe/simt/ladder.md",
  rows: [
    { config: "vp-block", lut: 3204, lut_log: 3184, lut_mem: 20, lut_dram: 20, lut_srl: 0, ff: 516, bram: 8, ctrlsets: 36, settings: "VREG_PRIM=block" },
    { config: "vp-dist", lut: 9430, lut_log: 4290, lut_mem: 5140, lut_dram: 5140, lut_srl: 0, ff: 1028, bram: 0, ctrlsets: 165, settings: "VREG_PRIM=distributed" },
  ],
  fmax: { "vp-block": { noc_clk: 324.1 }, "vp-dist": { noc_clk: 475.3 } },
}

const gpuPe = {
  file: "build/sweep_gpu-pe.md",
  suite: "gpu-pe",
  family: "simt",
  title: "The assembled SIMT PE",
  varies: "LANES and WAVES on kht_pe — the whole unit with L1, requestor and port",
  top: "kht_pe",
  fixed: "HAS_SHFL=1 (G8 IN), mask=1, ipdom=1, VREG_PRIM=block, period=3.333 ns",
  doc: "docs/projects/kohakumpe/simt/status.md",
  /** The only rows on this page that are an assembled PE rather than a submodule. */
  note: "Every other GPU suite tops out at a submodule. These are the rows the 20-25k budget is about.",
  rows: [
    { config: "pe-l8-w16", lut: 16115, lut_log: 12567, lut_mem: 3548, lut_dram: 3548, lut_srl: 0, ff: 7342, bram: 19, ctrlsets: 162,
      settings: "top=kht_pe lanes=8 waves=16 mask=1 ipdom=1 shfl=1 period=3.333" },
    { config: "pe-l8-w1", lut: 14709, lut_log: 11873, lut_mem: 2836, lut_dram: 2836, lut_srl: 0, ff: 6642, bram: 19, ctrlsets: 106,
      settings: "top=kht_pe lanes=8 waves=1 mask=1 ipdom=1 shfl=1 period=3.333" },
    { config: "pe-l4-w16", lut: 11487, lut_log: 7949, lut_mem: 3538, lut_dram: 3538, lut_srl: 0, ff: 6595, bram: 11, ctrlsets: 154,
      settings: "top=kht_pe lanes=4 waves=16 mask=1 ipdom=1 shfl=1 period=3.333" },
    { config: "pe-l16-w16", lut: 29961, lut_log: 26393, lut_mem: 3568, lut_dram: 3568, lut_srl: 0, ff: 8834, bram: 35, ctrlsets: 178,
      settings: "top=kht_pe lanes=16 waves=16 mask=1 ipdom=1 shfl=1 period=3.333" },
  ],
  fmax: {
    "pe-l8-w16": { noc_clk: 182.0 },
    "pe-l8-w1": { noc_clk: 190.5 },
    "pe-l4-w16": { noc_clk: 174.9 },
    "pe-l16-w16": { noc_clk: 170.9 },
  },
}

/* ------------------------------------------------------------------------ */
/* Station bus — one station, sb_root9, three managers and nine subordinates */
/* ------------------------------------------------------------------------ */

const stationFw512 = {
  file: "build/sweep_station-fw512.md",
  suite: "station-fw512",
  family: "station",
  title: "One station, every preset, with and without block RAM",
  varies: "OST, STORE_FWD, LUT_PER_BRAM",
  top: "sb_root9",
  fixed: "3 managers, 9 subordinates, FW=512, AW=43, 4 clock domains",
  doc: "docs/projects/kohakuaxi/station-bus.md",
  rows: [
    { config: "st-minarea-nobram", lut: 14731, lut_log: 8193, lut_mem: 6538, lut_dram: 6538, lut_srl: 0, ff: 25318, bram: 0, ctrlsets: 487,
      settings: "preset=0 fw=512 aw=43 ost=1 sfwd=0 lpb=820" },
    { config: "st-minarea-bram", lut: 12814, lut_log: 8196, lut_mem: 4618, lut_dram: 4618, lut_srl: 0, ff: 21964, bram: 24, ctrlsets: 478,
      settings: "preset=0 fw=512 aw=43 ost=1 sfwd=0 lpb=0" },
    { config: "st-balanced-nobram", lut: 15780, lut_log: 9046, lut_mem: 6734, lut_dram: 6734, lut_srl: 0, ff: 26080, bram: 0, ctrlsets: 541,
      settings: "preset=0 fw=512 aw=43 ost=4 sfwd=1 lpb=820" },
    { config: "st-balanced-bram", lut: 13848, lut_log: 9046, lut_mem: 4802, lut_dram: 4802, lut_srl: 0, ff: 22708, bram: 24, ctrlsets: 532,
      settings: "preset=0 fw=512 aw=43 ost=4 sfwd=1 lpb=0" },
    { config: "st-perf-nobram", settings: "" },
    { config: "st-perf-bram", settings: "" },
    { config: "st-safe-nobram", settings: "" },
    { config: "st-balanced-fw256", settings: "" },
  ],
  fmax: {
    "st-minarea-nobram": { bus_clk: 293.3, clk_ctrl: 443.1, clk_xdma: 400.8, clk_mesh: 535.6, clk_ddr: 559.6 },
    "st-minarea-bram": { bus_clk: 293.3, clk_ctrl: 443.1, clk_xdma: 404.7, clk_mesh: 535.6, clk_ddr: 559.6 },
    "st-balanced-nobram": { bus_clk: 326.3, clk_ctrl: 423.4, clk_xdma: 401.3, clk_mesh: 515.5, clk_ddr: 565.0 },
    "st-balanced-bram": { bus_clk: 326.2, clk_ctrl: 423.4, clk_xdma: 401.3, clk_mesh: 515.5, clk_ddr: 565.0 },
  },
}

const stationWidth = {
  file: "build/sweep_station-width.md",
  suite: "station-width",
  family: "station",
  title: "One station, flit width and address width",
  varies: "FW 128 → 1024, then AW 32 → 64 at FW=512",
  top: "sb_root9",
  fixed: "3 managers, 9 subordinates, BALANCED (ost=4, sfwd=1), no block RAM",
  doc: "docs/projects/kohakuaxi/station-bus.md",
  rows: [
    { config: "w-fw128-aw43", lut: 10076, lut_log: 5966, lut_mem: 4110, lut_dram: 4110, lut_srl: 0, ff: 15940, bram: 0, ctrlsets: 548, settings: "fw=128 aw=43" },
    { config: "w-fw256-aw43", lut: 12398, lut_log: 7222, lut_mem: 5176, lut_dram: 5176, lut_srl: 0, ff: 19501, bram: 0, ctrlsets: 544, settings: "fw=256 aw=43" },
    { config: "w-fw512-aw43", lut: 15780, lut_log: 9046, lut_mem: 6734, lut_dram: 6734, lut_srl: 0, ff: 26080, bram: 0, ctrlsets: 541, settings: "fw=512 aw=43" },
    { config: "w-fw1024-aw43", lut: 22641, lut_log: 11903, lut_mem: 10738, lut_dram: 10738, lut_srl: 0, ff: 38755, bram: 0, ctrlsets: 542, settings: "fw=1024 aw=43" },
    { config: "w-fw512-aw32", lut: 15416, lut_log: 8842, lut_mem: 6574, lut_dram: 6574, lut_srl: 0, ff: 25269, bram: 0, ctrlsets: 531, settings: "fw=512 aw=32" },
    { config: "w-fw512-aw40", lut: 15733, lut_log: 9027, lut_mem: 6706, lut_dram: 6706, lut_srl: 0, ff: 25884, bram: 0, ctrlsets: 541, settings: "fw=512 aw=40" },
    { config: "w-fw512-aw48", lut: 15904, lut_log: 9074, lut_mem: 6830, lut_dram: 6830, lut_srl: 0, ff: 26405, bram: 0, ctrlsets: 541, settings: "fw=512 aw=48" },
    { config: "w-fw512-aw64", lut: 16440, lut_log: 9250, lut_mem: 7190, lut_dram: 7190, lut_srl: 0, ff: 27461, bram: 0, ctrlsets: 541, settings: "fw=512 aw=64" },
  ],
  fmax: {
    "w-fw128-aw43": { bus_clk: 335.6, clk_ctrl: 442.9, clk_xdma: 415.6, clk_mesh: 502.5, clk_ddr: 502.5 },
    "w-fw256-aw43": { bus_clk: 304.5, clk_ctrl: 444.0, clk_xdma: 410.0, clk_mesh: 502.5, clk_ddr: 502.5 },
    "w-fw512-aw43": { bus_clk: 326.3, clk_ctrl: 423.4, clk_xdma: 401.3, clk_mesh: 515.5, clk_ddr: 565.0 },
    "w-fw1024-aw43": { bus_clk: 296.3, clk_ctrl: 423.4, clk_xdma: 408.0, clk_mesh: 502.5, clk_ddr: 502.5 },
    "w-fw512-aw32": { bus_clk: 326.3, clk_ctrl: 436.3, clk_xdma: 413.6, clk_mesh: 529.4, clk_ddr: 578.7 },
    "w-fw512-aw40": { bus_clk: 326.3, clk_ctrl: 451.1, clk_xdma: 407.5, clk_mesh: 515.5, clk_ddr: 565.0 },
    "w-fw512-aw48": { bus_clk: 326.3, clk_ctrl: 401.9, clk_xdma: 407.0, clk_mesh: 527.7, clk_ddr: 577.0 },
    "w-fw512-aw64": { bus_clk: 326.3, clk_ctrl: 395.7, clk_xdma: 412.4, clk_mesh: 513.6, clk_ddr: 560.5 },
  },
}

const stationPorts = {
  file: "build/sweep_station-ports.md",
  suite: "station-ports",
  family: "station",
  title: "One station, port count, EVERY port 512-bit",
  varies: "subordinate count Q, at a uniform width",
  top: "sb_p3xQf512 (generated per shape)",
  fixed: "3 × 512-bit managers, Q × 512-bit subordinates, FW=512, AW=43, ost=4, sfwd=1, no block RAM",
  doc: "docs/projects/kohakuaxi/station-bus.md",
  /** `sb_stn_root` fixes DSTW=3 and SRCW=2, so Q>8 and K>4 cannot be built. */
  note: "The wrappers are build artifacts, generated per run by ooc_sweep.py build_ports().",
  rows: [
    { config: "p-1x4", settings: "" },
    { config: "p-2x4", settings: "" },
    { config: "p-3x4", lut: 18385, lut_log: 7819, lut_mem: 10566, lut_dram: 10566, lut_srl: 0, ff: 27802, bram: 0, ctrlsets: 324,
      settings: "top=sb_p3x4f512 fw=512 aw=43 ost=4 sfwd=1 lpb=820" },
    { config: "p-6x4", settings: "" },
    { config: "p-3x1", lut: 13521, lut_log: 6195, lut_mem: 7326, lut_dram: 7326, lut_srl: 0, ff: 15709, bram: 0, ctrlsets: 198,
      settings: "top=sb_p3x1f512 fw=512 aw=43 ost=4 sfwd=1 lpb=820" },
    { config: "p-3x2", lut: 15167, lut_log: 6761, lut_mem: 8406, lut_dram: 8406, lut_srl: 0, ff: 19742, bram: 0, ctrlsets: 240,
      settings: "top=sb_p3x2f512 fw=512 aw=43 ost=4 sfwd=1 lpb=820" },
    { config: "p-3x8", settings: "" },
    { config: "p-3x16", settings: "" },
    { config: "p-6x9", settings: "" },
    { config: "p-3x9", settings: "" },
  ],
  fmax: {
    "p-3x1": { bus_clk: 413.2, clk_ctrl: 415.6, clk_xdma: 398.7 },
    "p-3x2": { bus_clk: 346.1, clk_ctrl: 415.6, clk_xdma: 398.7 },
    "p-3x4": { bus_clk: 328.3, clk_ctrl: 415.6, clk_xdma: 398.7 },
  },
}

/* ------------------------------------------------------------------------ */
/* Line — four stations, three links, managers on station 1                  */
/* ------------------------------------------------------------------------ */

const linePorts = {
  file: "build/sweep_line-ports.md",
  suite: "line-ports",
  family: "line",
  title: "Four-station line, port count",
  varies: "NQ (subordinates per station) 1 → 8, then NM (managers) 1 → 6",
  top: "sb_line4",
  fixed: "FW=512, AW=43, BALANCED, no block RAM, LINK_CDC=1, LINK_FULL=0, period=5.000 ns",
  doc: "docs/projects/kohakuaxi/station-bus.md",
  note: "Port 0 of each station is FW-wide; the rest are 32-bit. Manager 1 is 512-bit, the others 32-bit.",
  rows: [
    { config: "q-1", lut: 21030, lut_log: 12628, lut_mem: 8402, lut_dram: 8402, lut_srl: 0, ff: 62940, bram: 0, ctrlsets: 440, settings: "nq=1 nm=3" },
    { config: "q-2", lut: 25085, lut_log: 15915, lut_mem: 9170, lut_dram: 9170, lut_srl: 0, ff: 66718, bram: 0, ctrlsets: 620, settings: "nq=2 nm=3" },
    { config: "q-4", lut: 30785, lut_log: 20073, lut_mem: 10712, lut_dram: 10712, lut_srl: 0, ff: 74115, bram: 0, ctrlsets: 975, settings: "nq=4 nm=3" },
    { config: "q-6", lut: 37885, lut_log: 25631, lut_mem: 12254, lut_dram: 12254, lut_srl: 0, ff: 81599, bram: 0, ctrlsets: 1330, settings: "nq=6 nm=3" },
    { config: "q-8", lut: 43948, lut_log: 30158, lut_mem: 13790, lut_dram: 13790, lut_srl: 0, ff: 89053, bram: 0, ctrlsets: 1679, settings: "nq=8 nm=3" },
    { config: "m-1", lut: 25751, lut_log: 16747, lut_mem: 9004, lut_dram: 9004, lut_srl: 0, ff: 61630, bram: 0, ctrlsets: 886, settings: "nq=4 nm=1" },
    { config: "m-2", lut: 29963, lut_log: 19621, lut_mem: 10342, lut_dram: 10342, lut_srl: 0, ff: 72317, bram: 0, ctrlsets: 924, settings: "nq=4 nm=2" },
    { config: "m-6", lut: 34437, lut_log: 22581, lut_mem: 11856, lut_dram: 11856, lut_srl: 0, ff: 79556, bram: 0, ctrlsets: 1128, settings: "nq=4 nm=6" },
  ],
  fmax: {
    "q-1": { bus_clk0: 426.8, bus_clk1: 374.3, bus_clk2: 426.6, bus_clk3: 426.8, clk_ctrl: 412.5, clk_xdma: 402.9 },
    "q-2": { bus_clk0: 426.8, bus_clk1: 371.9, bus_clk2: 419.5, bus_clk3: 421.2, clk_ctrl: 434.6, clk_xdma: 402.3 },
    "q-4": { bus_clk0: 425.9, bus_clk1: 371.9, bus_clk2: 419.6, bus_clk3: 421.2, clk_ctrl: 412.7, clk_xdma: 410.7 },
    "q-6": { bus_clk0: 329.9, bus_clk1: 329.9, bus_clk2: 330.5, bus_clk3: 330.5, clk_ctrl: 357.5, clk_xdma: 357.5 },
    "q-8": { bus_clk0: 321.8, bus_clk1: 319.5, bus_clk2: 321.8, bus_clk3: 318.0, clk_ctrl: 353.0, clk_xdma: 357.0 },
    "m-1": { bus_clk0: 425.9, bus_clk1: 352.9, bus_clk2: 419.6, bus_clk3: 421.2, clk_ctrl: 412.7 },
    "m-2": { bus_clk0: 425.9, bus_clk1: 400.5, bus_clk2: 386.0, bus_clk3: 421.2, clk_ctrl: 412.7, clk_xdma: 410.7 },
    "m-6": { bus_clk0: 425.2, bus_clk1: 287.4, bus_clk2: 419.6, bus_clk3: 421.2, clk_ctrl: 412.7, clk_xdma: 371.1 },
  },
}

const lineWidth = {
  file: "build/sweep_line-width.md",
  suite: "line-width",
  family: "line",
  title: "Four-station line, flit width and address width",
  varies: "FW 128 → 1024, then AW 32/64 at FW=256 and FW=512",
  top: "sb_line4",
  fixed: "NQ=4, NM=3, BALANCED, no block RAM, LINK_CDC=1, LINK_FULL=0, period=5.000 ns",
  doc: "docs/projects/kohakuaxi/station-bus.md",
  note: "lw-fw256 is the DEPLOYED configuration; its Fmax rows are station-bus.md §2.9's timing table.",
  rows: [
    { config: "lw-fw128", lut: 17120, lut_log: 11090, lut_mem: 6030, lut_dram: 6030, lut_srl: 0, ff: 35113, bram: 0, ctrlsets: 981, settings: "fw=128 aw=43" },
    { config: "lw-fw256", lut: 22106, lut_log: 14492, lut_mem: 7614, lut_dram: 7614, lut_srl: 0, ff: 48167, bram: 0, ctrlsets: 975, settings: "fw=256 aw=43" },
    { config: "lw-fw512", lut: 30785, lut_log: 20073, lut_mem: 10712, lut_dram: 10712, lut_srl: 0, ff: 74115, bram: 0, ctrlsets: 975, settings: "fw=512 aw=43" },
    { config: "lw-fw1024", lut: 49008, lut_log: 32670, lut_mem: 16338, lut_dram: 16338, lut_srl: 0, ff: 114637, bram: 0, ctrlsets: 979, settings: "fw=1024 aw=43" },
    { config: "lw-fw256-aw32", lut: 21345, lut_log: 14033, lut_mem: 7312, lut_dram: 7312, lut_srl: 0, ff: 46609, bram: 0, ctrlsets: 977, settings: "fw=256 aw=32" },
    { config: "lw-fw256-aw64", lut: 24255, lut_log: 15865, lut_mem: 8390, lut_dram: 8390, lut_srl: 0, ff: 51111, bram: 0, ctrlsets: 977, settings: "fw=256 aw=64" },
    { config: "lw-fw512-aw32", lut: 30339, lut_log: 19931, lut_mem: 10408, lut_dram: 10408, lut_srl: 0, ff: 72619, bram: 0, ctrlsets: 975, settings: "fw=512 aw=32" },
    { config: "lw-fw512-aw64", lut: 33251, lut_log: 21753, lut_mem: 11498, lut_dram: 11498, lut_srl: 0, ff: 77118, bram: 0, ctrlsets: 975, settings: "fw=512 aw=64" },
  ],
  fmax: {
    "lw-fw128": { bus_clk0: 435.4, bus_clk1: 345.7, bus_clk2: 399.0, bus_clk3: 435.4, clk_ctrl: 377.6, clk_xdma: 377.6, clk_s0: 502.5, clk_s1: 520.3, clk_s2: 502.5, clk_s3: 502.5, clk_ddr: 520.3 },
    "lw-fw256": { bus_clk0: 428.1, bus_clk1: 357.9, bus_clk2: 392.6, bus_clk3: 428.1, clk_ctrl: 395.9, clk_xdma: 396.8, clk_s0: 502.5, clk_s1: 520.3, clk_s2: 502.5, clk_s3: 502.5, clk_ddr: 520.3 },
    "lw-fw512": { bus_clk0: 425.9, bus_clk1: 371.9, bus_clk2: 419.6, bus_clk3: 421.2, clk_ctrl: 412.7, clk_xdma: 410.7, clk_s0: 515.5, clk_s1: 537.3, clk_s2: 515.5, clk_s3: 515.5, clk_ddr: 575.4 },
    "lw-fw1024": { bus_clk0: 419.1, bus_clk1: 347.8, bus_clk2: 418.8, bus_clk3: 419.1, clk_ctrl: 375.8, clk_xdma: 366.3, clk_s0: 502.5, clk_s1: 520.3, clk_s2: 502.5, clk_s3: 502.5, clk_ddr: 520.3 },
    "lw-fw256-aw32": { bus_clk0: 431.0, bus_clk1: 357.9, bus_clk2: 429.6, bus_clk3: 431.0, clk_ctrl: 406.7, clk_xdma: 417.2, clk_s0: 509.4, clk_s1: 524.1, clk_s2: 509.4, clk_s3: 509.4, clk_ddr: 524.1 },
    "lw-fw256-aw64": { bus_clk0: 431.2, bus_clk1: 382.6, bus_clk2: 392.6, bus_clk3: 427.5, clk_ctrl: 397.9, clk_xdma: 376.2, clk_s0: 501.0, clk_s1: 517.6, clk_s2: 501.0, clk_s3: 501.0, clk_ddr: 517.6 },
    "lw-fw512-aw32": { bus_clk0: 425.9, bus_clk1: 372.0, bus_clk2: 419.8, bus_clk3: 419.8, clk_ctrl: 400.8, clk_xdma: 398.7, clk_s0: 529.1, clk_s1: 538.2, clk_s2: 529.1, clk_s3: 529.1, clk_ddr: 587.9 },
    "lw-fw512-aw64": { bus_clk0: 425.9, bus_clk1: 370.8, bus_clk2: 386.0, bus_clk3: 418.9, clk_ctrl: 399.5, clk_xdma: 375.8, clk_s0: 513.6, clk_s1: 535.3, clk_s2: 513.6, clk_s3: 513.6, clk_ddr: 570.8 },
  },
}

const linePreset = {
  file: "build/sweep_line-preset.md",
  suite: "line-preset",
  family: "line",
  title: "Four-station line, the option space",
  varies: "OST, STORE_FWD, LUT_PER_BRAM",
  top: "sb_line4",
  fixed: "FW=512, NQ=4, NM=3, AW=43, LINK_CDC=1, LINK_FULL=0, period=5.000 ns",
  doc: "docs/projects/kohakuaxi/station-bus.md",
  rows: [
    { config: "ln-minarea", lut: 30512, lut_log: 20062, lut_mem: 10450, lut_dram: 10450, lut_srl: 0, ff: 73844, bram: 0, ctrlsets: 947, settings: "ost=1 sfwd=0 lpb=820" },
    { config: "ln-balanced", lut: 30785, lut_log: 20073, lut_mem: 10712, lut_dram: 10712, lut_srl: 0, ff: 74115, bram: 0, ctrlsets: 975, settings: "ost=4 sfwd=1 lpb=820" },
    { config: "ln-perf", lut: 31838, lut_log: 21062, lut_mem: 10776, lut_dram: 10776, lut_srl: 0, ff: 74315, bram: 0, ctrlsets: 977, settings: "ost=8 sfwd=1 lpb=820" },
    { config: "ln-bram", lut: 24981, lut_log: 20057, lut_mem: 4924, lut_dram: 4924, lut_srl: 0, ff: 56491, bram: 130.5, ctrlsets: 861, settings: "ost=4 sfwd=1 lpb=0" },
    { config: "ln-fw256", settings: "" },
    { config: "ln-fw256-bram", settings: "" },
    { config: "ln-full", settings: "" },
    { config: "ln-nocdc", settings: "" },
  ],
  fmax: {
    "ln-minarea": { bus_clk0: 425.9, bus_clk1: 374.4, bus_clk2: 419.6, bus_clk3: 421.2, clk_ctrl: 410.3, clk_xdma: 373.6, clk_s0: 538.8, clk_s1: 548.2, clk_s2: 538.8, clk_s3: 538.8, clk_ddr: 592.1 },
    "ln-balanced": { bus_clk0: 425.9, bus_clk1: 371.9, bus_clk2: 419.6, bus_clk3: 421.2, clk_ctrl: 412.7, clk_xdma: 410.7, clk_s0: 515.5, clk_s1: 537.3, clk_s2: 515.5, clk_s3: 515.5, clk_ddr: 575.4 },
    "ln-perf": { bus_clk0: 425.2, bus_clk1: 371.9, bus_clk2: 419.6, bus_clk3: 421.2, clk_ctrl: 412.7, clk_xdma: 371.1, clk_s0: 512.6, clk_s1: 521.1, clk_s2: 512.6, clk_s3: 512.6, clk_ddr: 547.3 },
    "ln-bram": { bus_clk0: 396.0, bus_clk1: 384.6, bus_clk2: 398.6, bus_clk3: 396.0, clk_ctrl: 383.4, clk_xdma: 383.4, clk_s0: 464.7, clk_s1: 482.4, clk_s2: 464.7, clk_s3: 464.7, clk_ddr: 514.7 },
  },
}

const lineFreq = {
  file: "build/sweep_line-freq.md",
  suite: "line-freq",
  family: "line",
  title: "Four-station line, area against the constraint",
  varies: "the target period only — 6.667 ns down to 2.000 ns",
  top: "sb_line4",
  fixed: "FW=512, NQ=4, NM=3, AW=43, BALANCED, no block RAM, LINK_CDC=1",
  doc: "docs/projects/kohakuaxi/station-bus.md",
  note: "bus_clk1 is the binding clock throughout — the station carrying the three managers.",
  rows: [
    { config: "f-150", target_mhz: 150, period: 6.667, lut: 30778, lut_log: 20066, lut_mem: 10712, lut_dram: 10712, lut_srl: 0, ff: 74115, bram: 0, ctrlsets: 975, settings: "period=6.667" },
    { config: "f-200", target_mhz: 200, period: 5.0, lut: 30785, lut_log: 20073, lut_mem: 10712, lut_dram: 10712, lut_srl: 0, ff: 74115, bram: 0, ctrlsets: 975, settings: "period=5.000" },
    { config: "f-250", target_mhz: 250, period: 4.0, lut: 30818, lut_log: 20106, lut_mem: 10712, lut_dram: 10712, lut_srl: 0, ff: 74119, bram: 0, ctrlsets: 975, settings: "period=4.000" },
    { config: "f-300", target_mhz: 300, period: 3.333, lut: 30883, lut_log: 20171, lut_mem: 10712, lut_dram: 10712, lut_srl: 0, ff: 74119, bram: 0, ctrlsets: 975, settings: "period=3.333" },
    { config: "f-350", target_mhz: 350, period: 2.857, lut: 32833, lut_log: 22121, lut_mem: 10712, lut_dram: 10712, lut_srl: 0, ff: 74119, bram: 0, ctrlsets: 975, settings: "period=2.857" },
    { config: "f-400", target_mhz: 400, period: 2.5, lut: 34217, lut_log: 23505, lut_mem: 10712, lut_dram: 10712, lut_srl: 0, ff: 74119, bram: 0, ctrlsets: 975, settings: "period=2.500" },
    { config: "f-450", target_mhz: 450, period: 2.222, lut: 34269, lut_log: 23557, lut_mem: 10712, lut_dram: 10712, lut_srl: 0, ff: 74119, bram: 0, ctrlsets: 975, settings: "period=2.222" },
    { config: "f-500", target_mhz: 500, period: 2.0, lut: 34396, lut_log: 23684, lut_mem: 10712, lut_dram: 10712, lut_srl: 0, ff: 74119, bram: 0, ctrlsets: 975, settings: "period=2.000" },
  ],
  fmax: {
    "f-150": { bus_clk0: 425.2, bus_clk1: 353.4, bus_clk2: 419.6, bus_clk3: 421.2 },
    "f-200": { bus_clk0: 425.9, bus_clk1: 371.9, bus_clk2: 419.6, bus_clk3: 421.2 },
    "f-250": { bus_clk0: 425.2, bus_clk1: 371.9, bus_clk2: 419.6, bus_clk3: 421.2 },
    "f-300": { bus_clk0: 431.0, bus_clk1: 385.7, bus_clk2: 414.1, bus_clk3: 427.0 },
    "f-350": { bus_clk0: 431.0, bus_clk1: 385.7, bus_clk2: 414.1, bus_clk3: 427.0 },
    "f-400": { bus_clk0: 437.1, bus_clk1: 390.5, bus_clk2: 425.4, bus_clk3: 432.9 },
    "f-450": { bus_clk0: 437.1, bus_clk1: 390.5, bus_clk2: 425.4, bus_clk3: 432.9 },
    "f-500": { bus_clk0: 437.1, bus_clk1: 390.5, bus_clk2: 425.4, bus_clk3: 432.9 },
  },
}

/* ------------------------------------------------------------------------ */
/* Vendor anchors. WHOLE-HARNESS totals, not instance figures.              */
/* ------------------------------------------------------------------------ */

const xbarAnchor = {
  file: "build/sweep_xbar-anchor.md",
  suite: "xbar-anchor",
  family: "vendor",
  title: "axi_interconnect anchor points",
  varies: "shape, strategy, data width, clock count",
  top: "block-design pseudo-top",
  fixed: "3 slave interfaces throughout; strat 2 = max-performance, 1 = minimum-area (SASD)",
  doc: "docs/projects/kohakuaxi/station-bus.md",
  scope: "whole harness, NOT the instance — station-bus.md §2.6's tables are instance figures and are 1.2–2.1k lower.",
  rows: [
    { config: "xbar-3x9-perf", lut: 18627, lut_log: 15727, lut_mem: 2900, lut_dram: 2848, lut_srl: 52, ff: 21428, bram: 37.5, ctrlsets: 1037, settings: "nsi=3 nmi=9 dw=512 nclk=4 strat=2" },
    { config: "xbar-3x9-area", lut: 12068, lut_log: 9498, lut_mem: 2570, lut_dram: 2560, lut_srl: 10, ff: 13729, bram: 6, ctrlsets: 724, settings: "nsi=3 nmi=9 dw=512 nclk=4 strat=1" },
    { config: "xbar-3x9-1clk-perf", lut: 7934, lut_log: 7449, lut_mem: 485, lut_dram: 448, lut_srl: 37, ff: 6103, bram: 1.5, ctrlsets: 570, settings: "nsi=3 nmi=9 dw=512 nclk=1 strat=2" },
    { config: "xbar-3x9-1clk-area", lut: 5674, lut_log: 5225, lut_mem: 449, lut_dram: 448, lut_srl: 1, ff: 4342, bram: 0, ctrlsets: 343, settings: "nsi=3 nmi=9 dw=512 nclk=1 strat=1" },
    { config: "xbar-3x5-perf", lut: 10297, lut_log: 8836, lut_mem: 1461, lut_dram: 1424, lut_srl: 37, ff: 12767, bram: 31.5, ctrlsets: 648, settings: "nsi=3 nmi=5 dw=512 nclk=4 strat=2" },
    { config: "xbar-3x5-area", lut: 7656, lut_log: 5601, lut_mem: 2055, lut_dram: 2048, lut_srl: 7, ff: 10213, bram: 3, ctrlsets: 439, settings: "nsi=3 nmi=5 dw=512 nclk=4 strat=1" },
    { config: "xbar-3x9-256-perf", lut: 14898, lut_log: 13068, lut_mem: 1830, lut_dram: 1776, lut_srl: 54, ff: 16930, bram: 25.5, ctrlsets: 1035, settings: "nsi=3 nmi=9 dw=256 nclk=4 strat=2" },
    { config: "xbar-3x9-256-area", lut: 10851, lut_log: 8857, lut_mem: 1994, lut_dram: 1984, lut_srl: 10, ff: 13464, bram: 6, ctrlsets: 764, settings: "nsi=3 nmi=9 dw=256 nclk=4 strat=1" },
  ],
  /** clk_out1_bd_ck1_0 reports 4219.4 MHz — an unloaded harness clock, not a result. */
  fmax: {
    "xbar-3x9-perf": { ck0: 243.5, ck1: 4219.4, ck2: 311.6, ck3: 305.2 },
    "xbar-3x9-area": { ck0: 306.0, ck1: 4219.4, ck2: 295.0, ck3: 288.4 },
    "xbar-3x9-1clk-perf": { ck0: 246.5 },
    "xbar-3x9-1clk-area": { ck0: 317.9 },
    "xbar-3x5-perf": { ck0: 245.2, ck1: 4219.4, ck2: 312.6, ck3: 305.2 },
    "xbar-3x5-area": { ck0: 320.6, ck1: 4219.4, ck2: 295.2, ck3: 290.1 },
    "xbar-3x9-256-perf": { ck0: 259.8, ck1: 4219.4, ck2: 313.3, ck3: 304.7 },
    "xbar-3x9-256-area": { ck0: 305.6, ck1: 4219.4, ck2: 317.2, ck3: 314.2 },
  },
}

const smcBase = {
  file: "build/sweep_smc-base.md",
  suite: "smc-base",
  family: "vendor",
  title: "SmartConnect anchor points",
  varies: "shape and clock count",
  top: "block-design pseudo-top",
  fixed: "512-bit ports throughout",
  doc: "docs/projects/kohakuaxi/station-bus.md",
  scope: "whole harness, NOT the instance. The lut_dram column IS the instance's — 4,522 at 3x9 in both clock counts.",
  rows: [
    { config: "smc-3x9-4clk", lut: 23178, lut_log: 16570, lut_mem: 6608, lut_dram: 4522, lut_srl: 2086, ff: 23685, bram: 9, ctrlsets: 1619, settings: "nsi=3 nmi=9 dw=512 nclk=4" },
    { config: "smc-3x9-1clk", lut: 22653, lut_log: 16052, lut_mem: 6601, lut_dram: 4522, lut_srl: 2079, ff: 20950, bram: 9, ctrlsets: 1624, settings: "nsi=3 nmi=9 dw=512 nclk=1" },
    { config: "smc-3x5-4clk", lut: 15183, lut_log: 10197, lut_mem: 4986, lut_dram: 2938, lut_srl: 2048, ff: 15659, bram: 5, ctrlsets: 1104, settings: "nsi=3 nmi=5 dw=512 nclk=4" },
    { config: "smc-1x5-2clk", lut: 6714, lut_log: 4737, lut_mem: 1977, lut_dram: 1860, lut_srl: 117, ff: 7378, bram: 5, ctrlsets: 614, settings: "nsi=1 nmi=5 dw=512 nclk=2" },
    { config: "smc-2x6-3clk", settings: "" },
    { config: "smc-3x9-256", settings: "" },
    { config: "smc-6x9-4clk", settings: "" },
    { config: "smc-3x16-4clk", settings: "" },
  ],
  fmax: {
    "smc-3x9-4clk": { ck0: 521.4, ck1: 1934.2, ck2: 525.2, ck3: 579.4 },
    "smc-3x9-1clk": { ck0: 519.5 },
    "smc-3x5-4clk": { ck0: 521.4, ck1: 1934.2, ck2: 525.2, ck3: 579.4 },
    "smc-1x5-2clk": { ck0: 514.4, ck1: 1934.2 },
  },
}

/* ------------------------------------------------------------------------ */

export const SWEEPS = {
  "gpu-ladder": gpuLadder,
  "gpu-waves": gpuWaves,
  "gpu-sched": gpuSched,
  "gpu-lanes": gpuLanes,
  "gpu-lds": gpuLds,
  "gpu-shfl": gpuShfl,
  "gpu-vregprim": gpuVregprim,
  "gpu-pe": gpuPe,
  "station-fw512": stationFw512,
  "station-width": stationWidth,
  "station-ports": stationPorts,
  "line-ports": linePorts,
  "line-width": lineWidth,
  "line-preset": linePreset,
  "line-freq": lineFreq,
  "xbar-anchor": xbarAnchor,
  "smc-base": smcBase,
}

export const FAMILIES = {
  gpu: "KohakuSIMT — the SIMT measurement ladder",
  station: "Station bus — one station",
  line: "Station bus — the four-station line",
  vendor: "Vendor interconnect anchors",
}

/** Rows that actually carry data. A blank row is a configuration that has a
 *  label in the sweep file and no measurement behind it. */
export function rowsOf(name) {
  return (SWEEPS[name]?.rows ?? []).filter((r) => r.lut != null)
}

/** Every row, including the blank ones — for showing what was NOT measured. */
export function allRowsOf(name) {
  return SWEEPS[name]?.rows ?? []
}

export function fmaxOf(name, config) {
  return SWEEPS[name]?.fmax?.[config] ?? {}
}

/** The lowest constrained clock of a row: the one that binds. Harness clocks
 *  reporting in the thousands of MHz are unloaded and excluded. */
export function bindingClock(name, config) {
  const f = fmaxOf(name, config)
  const real = Object.entries(f).filter(([, v]) => v < 1000)
  if (!real.length) return null
  return real.reduce((a, b) => (b[1] < a[1] ? b : a))
}

/* ------------------------------------------------------------------------ */
/* Fits and deltas computed FROM the rows above. Nothing here is measured;   */
/* each entry names the rows it was derived from.                           */
/* ------------------------------------------------------------------------ */

export const DERIVED = {
  simtGates: {
    from: "gpu-ladder",
    claim: "Every SIMT gate built so far costs 252 LUT",
    detail: "g3-ipdom 3,204 − g0-substrate 2,952, on a 2,952 substrate at 8 lanes.",
  },
  wavesAreFree: {
    from: "gpu-ladder",
    claim: "Sixteen resident wave contexts cost +0 LUT, +0 BRAM, +0 control sets and +4 FF",
    detail: "g1-waves against g0-substrate. Storage only — mask and IPDOM are off on both rows.",
  },
  wavesCostToSchedule: {
    from: ["gpu-waves", "gpu-sched"],
    claim: "+1,775 LUT to schedule sixteen waves, +124 LUT to store them",
    detail: "kht_core 9,653 − 7,754 = +1,899; kht_unit 3,200 − 3,076 = +124; the difference is the front end.",
  },
  laneFit: {
    from: "gpu-lanes",
    claim: "LUT = 112 + 386.4 × LANES, FF = 116 + 50 × LANES, BRAM = 1 × LANES",
    detail: "Exact at 4, 8 and 32; +61 LUT at 16. The FF fit is exact at all four points.",
  },
  ldsQuadratic: {
    from: "gpu-lds",
    claim: "LUT ≈ 25 × LANES², and at 32 lanes the LDS is BELOW the rest of the unit's clock",
    detail: "317.7 MHz against 324.1. Flops and BRAM stay linear; only the LANES × LANES resolver squares.",
  },
  shflNLogN: {
    from: "gpu-shfl",
    claim: "ΔLUT ≈ 39 × LANES × log2(LANES)",
    detail: "+268 / +935 / +3,019 / +6,000 at 4 / 8 / 16 / 32 lanes.",
  },
  stationPortSlope512: {
    from: "station-ports",
    claim: "1,621 LUT, 4,031 FF and exactly 42 control sets per 512-bit subordinate port",
    detail:
      "Fitted on p-3x1 and p-3x4 only; predicts p-3x2 to within 0.16%. LUTRAM is exactly " +
      "1,080 per port at both intervals.",
  },
  linePortSlope32: {
    from: "line-ports",
    claim: "818 LUT per 32-bit subordinate port on the line",
    detail: "Endpoints q-1 and q-8, 28 ports apart; interior points deviate 3.1%, 0.2% and 1.3%.",
  },
  areaFlatToTarget: {
    from: "line-freq",
    claim: "Area is flat to a 300 MHz ask (0.3%), then rises 6.3% at 350 and 11% beyond",
    detail:
      "bus_clk1 saturates at 390.5 MHz however hard it is pushed; 350 MHz is the highest " +
      "target that closes. At the deployed 200 MHz the constraint costs nothing.",
  },
  clockDomainCost: {
    from: ["xbar-anchor", "smc-base", "line-preset"],
    claim: "1 → 4 clock domains at 3x9: axi_interconnect +10,693 / +6,394, SmartConnect +525",
    detail:
      "The +525 is the rebuild failing to build the domains, not a saving — its LUTRAM is " +
      "4,522 in both rows. The station bus measures −328 for the same change.",
  },
  widthSensitivity: {
    from: ["line-width", "xbar-anchor"],
    claim: "512 → 256 returns 28.2% on the line, 20.0% on max-perf axi_interconnect, 10.1% on SASD",
    detail: "The more of a structure is datapath, the more halving the width returns.",
  },
  assembledPeIsInBudget: {
    from: "gpu-pe",
    claim: "The assembled SIMT PE at 8 lanes is 16,115 LUT — inside the 20–25k band, integer-only",
    detail:
      "16 lanes measures 29,961, past the 25k target and at the 30k review line. Every " +
      "submodule figure on the ladder is a fraction of these rows, not a substitute for them.",
  },
  assembledPeMissesTheClock: {
    from: ["gpu-pe", "gpu-ladder"],
    claim: "The assembled PE runs at 170–190 MHz where every submodule row sat at 324.1",
    detail:
      "182.0 MHz at the shipped 8x16 shape, against a 300 MHz mesh clock. This is the third " +
      "time 'a ladder whose top is one submodule cannot see a path that leaves it' has been " +
      "collected on — kht_core was found at 71.7 MHz the same way.",
  },
}

/* ------------------------------------------------------------------------ */

export const KNOWN_DISAGREEMENTS = [
  {
    what: "kht_unit at 8 lanes, 16 waves, full gate set, HAS_SHFL=0",
    values: "3,204 LUT in gpu-ladder, gpu-lanes and gpu-shfl; 3,200 in gpu-waves",
    detail:
      "Character-for-character identical configuration tags. FF, BRAM and control sets agree " +
      "exactly at 516 / 8 / 36; the split is 3,184+20 against 3,180+20. Four LUT, 0.12%. The " +
      "suites ran at different times and the tree moved between them (the same row read 3,232 " +
      "before the mux-trim fix), so tool non-determinism and a tree change cannot be separated " +
      "from the sweep files alone.",
  },
  {
    what: "Whether repeat OOC runs are bit-identical",
    values: "Yes on sb_line4 (station-bus.md §2.4); no on kht_unit (above)",
    detail:
      "A configuration of the line re-synthesised separately came back bit-identical. That is " +
      "not a project-wide property — it holds where it has been checked.",
  },
  {
    what: "axi_interconnect and SmartConnect totals",
    values: "The sweep files record the whole harness; station-bus.md §2.6 records the instance",
    detail:
      "3x9 four-clock max-performance is 18,627 in xbar-anchor and 16,532 as an instance; " +
      "SmartConnect 3x9 four-clock is 23,178 against 21,885. Difference only within one frame.",
  },
]
