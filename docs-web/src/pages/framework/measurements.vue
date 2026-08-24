<script setup>
/**
 * /framework/measurements — what the FRAMEWORK's own RTL has been synthesised
 * at: the station bus, the four-station line, and the vendor interconnects it
 * is measured against.
 *
 * A project's sweeps are the project's page. The eight `gpu-*` suites measure
 * src/kohakumpe/simt/ and moved to /mpe/measurements when this page was split;
 * the method sections stayed here, because they are the method for all of them.
 *
 * Drawn, in full, from:
 *   build/sweep_*.md              (9 suites, the raw records)
 *   scripts/py/ooc_sweep.py       (what each suite varies, and what it holds)
 *   scripts/tcl/ooc_class.tcl     (ooc_record: how one synthesis becomes a row)
 *   scripts/tcl/ooc_station.tcl
 *   docs/projects/kohakuaxi/station-bus.md
 *
 * Every number is transcribed in src/content/ooc.js, not typed here. One part,
 * xcvu13p-fhgb2104-2L-e, out-of-context SYNTHESIS ONLY — no opt, no place, no
 * route.
 */
import { SWEEPS, PART, TOOL, rowsOf, allRowsOf, bindingClock, KNOWN_DISAGREEMENTS } from "@/content/ooc"

const n = (v) => (v == null ? "—" : v.toLocaleString())
const mhz = (v) => (v == null ? "—" : v.toFixed(1))

/* ---- the index of suites ------------------------------------------------ */
const ORDER = [
  "station-fw512", "station-width", "station-ports",
  "line-ports", "line-width", "line-preset", "line-freq",
  "xbar-anchor", "smc-base",
]

const index = {
  cols: [
    { key: "suite", label: "suite", mono: true },
    { key: "varies", label: "what it varies" },
    { key: "top", label: "top", mono: true },
    { key: "got", label: "rows", mono: true, align: "right" },
  ],
  rows: ORDER.map((k) => {
    const s = SWEEPS[k]
    const got = rowsOf(k).length
    const all = allRowsOf(k).length
    return {
      suite: k,
      varies: s.varies,
      top: s.top,
      got: got === all ? `${got}` : `${got} <span class="opacity-60">of ${all}</span>`,
      _tone: got === 0 ? "bad" : got < all ? "warn" : undefined,
    }
  }),
}

/* ---- generic table builders --------------------------------------------- */
function resTable(name, opts = {}) {
  const cols = [
    { key: "config", label: "config", mono: true },
    { key: "lut", label: "LUT", mono: true, align: "right" },
    ...(opts.lutram ? [{ key: "lutram", label: "LUTRAM", mono: true, align: "right" }] : []),
    { key: "ff", label: "FF", mono: true, align: "right" },
    ...(opts.noBram ? [] : [{ key: "bram", label: "BRAM", mono: true, align: "right" }]),
    { key: "cs", label: "ctrl sets", mono: true, align: "right" },
    { key: "fmax", label: "binding clock, MHz", mono: true, align: "right" },
  ]
  const rows = rowsOf(name).map((r) => {
    const b = bindingClock(name, r.config)
    return {
      config: r.config,
      lut: `<b>${n(r.lut)}</b>`,
      lutram: n(r.lut_dram),
      ff: n(r.ff),
      bram: n(r.bram),
      cs: n(r.ctrlsets),
      fmax: b ? `${mhz(b[1])} <span class="opacity-60">${b[0]}</span>` : "—",
    }
  })
  return { cols, rows }
}

const src = (name) => {
  const s = SWEEPS[name]
  return `${s.file} · ${s.top} · ${s.fixed} · ${PART}`
}

/* ---- station bus -------------------------------------------------------- */
const presetTable = resTable("station-fw512", { lutram: true })
const widthTable = resTable("station-width", { noBram: true })

const P = SWEEPS["station-ports"].rows
const pQ = [P[4], P[5], P[2]] // p-3x1, p-3x2, p-3x4
const portSlope = (a, b, k) => Math.round((b[k] - a[k]) / (b.q - a.q))
const pRows = [{ ...pQ[0], q: 1 }, { ...pQ[1], q: 2 }, { ...pQ[2], q: 4 }]

const portTable = {
  cols: [
    { key: "q", label: "Q · 512-bit subordinates", mono: true, align: "right" },
    { key: "lut", label: "LUT", mono: true, align: "right" },
    { key: "lutram", label: "of which LUTRAM", mono: true, align: "right" },
    { key: "ff", label: "FF", mono: true, align: "right" },
    { key: "cs", label: "ctrl sets", mono: true, align: "right" },
    { key: "bus", label: "bus_clk", mono: true, align: "right" },
  ],
  rows: pRows.map((r) => ({
    q: `${r.q}`,
    lut: `<b>${n(r.lut)}</b>`,
    lutram: n(r.lut_dram),
    ff: n(r.ff),
    cs: n(r.ctrlsets),
    bus: `${SWEEPS["station-ports"].fmax[r.config].bus_clk} MHz`,
  })),
}

const portFit = {
  cols: [
    { key: "m", label: "metric" },
    { key: "slope", label: "per added 512-bit port", mono: true, align: "right" },
    { key: "check", label: "predicting Q=2 from Q=1 and Q=4", mono: true, align: "right" },
  ],
  rows: [
    { m: "CLB LUTs", slope: `<b>+${n(portSlope(pRows[0], pRows[2], "lut"))}</b>`, check: "15,142 against 15,167 · <b>0.16%</b>", _tone: "good" },
    { m: "of which LUTRAM", slope: `+${n(portSlope(pRows[0], pRows[2], "lut_dram"))}`, check: "exact at both intervals" },
    { m: "CLB registers", slope: `+${n(portSlope(pRows[0], pRows[2], "ff"))}`, check: "11,678 + 4,031·Q" },
    { m: "control sets", slope: `+${n(portSlope(pRows[0], pRows[2], "ctrlsets"))}`, check: "<b>exact at all three points</b>", _tone: "good" },
  ],
}

const linePortTable = {
  cols: [
    { key: "cfg", label: "config", mono: true },
    { key: "what", label: "what changed" },
    { key: "lut", label: "LUT", mono: true, align: "right" },
    { key: "d", label: "ΔLUT", mono: true, align: "right" },
    { key: "ff", label: "FF", mono: true, align: "right" },
  ],
  rows: (() => {
    const r = SWEEPS["line-ports"].rows
    const q = r.slice(0, 5)
    const m = r.slice(5)
    const out = q.map((x, i) => ({
      cfg: x.config,
      what: `NQ = ${[1, 2, 4, 6, 8][i]} · ${[4, 8, 16, 24, 32][i]} subordinates on the line`,
      lut: n(x.lut),
      d: i === 0 ? "—" : `+${n(x.lut - q[i - 1].lut)}`,
      ff: n(x.ff),
    }))
    out.push({ cfg: m[0].config, what: "NM = 1 — one 32-bit manager", lut: n(m[0].lut), d: "—", ff: n(m[0].ff) })
    out.push({ cfg: m[1].config, what: "NM = 2 — <b>the 512-bit one added</b>", lut: n(m[1].lut), d: `<b>+${n(m[1].lut - m[0].lut)}</b>`, ff: n(m[1].ff), _tone: "warn" })
    out.push({ cfg: "q-4", what: "NM = 3 — a 32-bit manager added", lut: n(q[2].lut), d: `<b>+${n(q[2].lut - m[1].lut)}</b>`, ff: n(q[2].ff) })
    out.push({ cfg: m[2].config, what: "NM = 6 — three more 32-bit", lut: n(m[2].lut), d: `+${n(m[2].lut - q[2].lut)}`, ff: n(m[2].ff) })
    return out
  })(),
}

const lineWidthTable = {
  cols: [
    { key: "fw", label: "FW", mono: true, align: "right" },
    { key: "lut", label: "LUT", mono: true, align: "right" },
    { key: "log", label: "logic", mono: true, align: "right" },
    { key: "ram", label: "LUTRAM", mono: true, align: "right" },
    { key: "ff", label: "FF", mono: true, align: "right" },
    { key: "cs", label: "ctrl sets", mono: true, align: "right" },
    { key: "bw", label: "GB/s @200 MHz · derived", mono: true, align: "right" },
  ],
  rows: [0, 1, 2, 3].map((i) => {
    const r = SWEEPS["line-width"].rows[i]
    const fw = [128, 256, 512, 1024][i]
    return {
      fw: `${fw}`,
      lut: `<b>${n(r.lut)}</b>`,
      log: n(r.lut_log),
      ram: n(r.lut_dram),
      ff: n(r.ff),
      cs: n(r.ctrlsets),
      bw: (fw / 8 * 200 / 1000).toFixed(1),
      _tone: fw === 256 ? "good" : undefined,
    }
  }),
}

const freqTable = {
  cols: [
    { key: "t", label: "target", mono: true, align: "right" },
    { key: "p", label: "period", mono: true, align: "right" },
    { key: "lut", label: "LUT", mono: true, align: "right" },
    { key: "f", label: "bus_clk1 Fmax", mono: true, align: "right" },
    { key: "closes", label: "closes?" },
  ],
  rows: SWEEPS["line-freq"].rows.map((r) => {
    const f = SWEEPS["line-freq"].fmax[r.config].bus_clk1
    const ok = f >= r.target_mhz
    return {
      t: `${r.target_mhz} MHz`,
      p: `${r.period.toFixed(3)} ns`,
      lut: n(r.lut),
      f: `${f}`,
      closes: ok ? "yes" : "<b>no</b>",
      _tone: ok ? undefined : "bad",
    }
  }),
}

/* ---- vendor ------------------------------------------------------------- */
const X = SWEEPS["xbar-anchor"].rows
const M = SWEEPS["smc-base"].rows
const byCfg = (rows) => Object.fromEntries(rows.map((r) => [r.config, r]))
const x = byCfg(X)
const m = byCfg(M)

const vendorTable = {
  cols: [
    { key: "ip", label: "IP", mono: true },
    { key: "shape", label: "shape", mono: true, align: "right" },
    { key: "w", label: "width", mono: true, align: "right" },
    { key: "clk", label: "clocks", mono: true, align: "right" },
    { key: "strat", label: "strategy" },
    { key: "lut", label: "LUT", mono: true, align: "right" },
    { key: "ram", label: "LUTRAM", mono: true, align: "right" },
    { key: "srl", label: "SRL", mono: true, align: "right" },
  ],
  rows: [
    ["xbar-3x9-perf", "axi_interconnect", "3×9", 512, 4, "max-performance"],
    ["xbar-3x9-area", "axi_interconnect", "3×9", 512, 4, "minimum-area (SASD)"],
    ["xbar-3x9-1clk-perf", "axi_interconnect", "3×9", 512, 1, "max-performance"],
    ["xbar-3x9-1clk-area", "axi_interconnect", "3×9", 512, 1, "minimum-area (SASD)"],
    ["xbar-3x5-perf", "axi_interconnect", "3×5", 512, 4, "max-performance"],
    ["xbar-3x5-area", "axi_interconnect", "3×5", 512, 4, "minimum-area (SASD)"],
    ["xbar-3x9-256-perf", "axi_interconnect", "3×9", 256, 4, "max-performance"],
    ["xbar-3x9-256-area", "axi_interconnect", "3×9", 256, 4, "minimum-area (SASD)"],
  ].map(([k, ip, shape, w, clk, strat]) => ({
    ip, shape, w: `${w}`, clk: `${clk}`, strat, lut: `<b>${n(x[k].lut)}</b>`, ram: n(x[k].lut_dram), srl: n(x[k].lut_srl),
  })).concat([
    ["smc-3x9-4clk", "3×9", 4], ["smc-3x9-1clk", "3×9", 1],
    ["smc-3x5-4clk", "3×5", 4], ["smc-1x5-2clk", "1×5", 2],
  ].map(([k, shape, clk]) => ({
    ip: "SmartConnect", shape, w: "512", clk: `${clk}`, strat: "—",
    lut: `<b>${n(m[k].lut)}</b>`, ram: n(m[k].lut_dram), srl: n(m[k].lut_srl),
  }))),
}

const clockCost = {
  cols: [
    { key: "s", label: "structure" },
    { key: "d", label: "ΔLUT for 1 → 4 domains", mono: true, align: "right" },
    { key: "r", label: "ratio", mono: true, align: "right" },
  ],
  rows: [
    { s: "<code>axi_interconnect</code>, max-performance", d: `<b>+${n(x["xbar-3x9-perf"].lut - x["xbar-3x9-1clk-perf"].lut)}</b>`, r: "2.35×", _tone: "bad" },
    { s: "<code>axi_interconnect</code>, SASD", d: `<b>+${n(x["xbar-3x9-area"].lut - x["xbar-3x9-1clk-area"].lut)}</b>`, r: "2.13×", _tone: "bad" },
    { s: "SmartConnect (rebuild) — <b>did not build the domains</b>", d: `+${n(m["smc-3x9-4clk"].lut - m["smc-3x9-1clk"].lut)}`, r: "1.02×", _tone: "warn" },
    { s: "station bus, <code>LINK_CDC</code>", d: "<b>−328</b>", r: "0.99×", _tone: "good" },
  ],
}

const portSlopes = {
  cols: [
    { key: "s", label: "structure" },
    { key: "w", label: "port width", mono: true, align: "right" },
    { key: "d", label: "LUT per added subordinate port", mono: true, align: "right" },
  ],
  rows: [
    { s: "<code>axi_interconnect</code>, max-performance", w: "512", d: `<b>${n(Math.round((x["xbar-3x9-perf"].lut - x["xbar-3x5-perf"].lut) / 4))}</b>` },
    { s: "SmartConnect (rebuild)", w: "512", d: `<b>${n(Math.round((m["smc-3x9-4clk"].lut - m["smc-3x5-4clk"].lut) / 4))}</b>` },
    { s: "<b>station, one station</b>", w: "512", d: `<b>${n(portSlope(pRows[0], pRows[2], "lut"))}</b>`, _tone: "good" },
    { s: "<code>axi_interconnect</code>, SASD <span class='opacity-60'>does strictly less</span>", w: "512", d: `${n(Math.round((x["xbar-3x9-area"].lut - x["xbar-3x5-area"].lut) / 4))}` },
    { s: "station, the deployed line", w: "32", d: "818" },
  ],
}

/* ---- disagreements and repro -------------------------------------------- */
const disagree = {
  cols: [
    { key: "w", label: "what" },
    { key: "v", label: "the two values" },
  ],
  rows: KNOWN_DISAGREEMENTS.map((d) => ({ w: `<b>${d.what}</b><br><span class="opacity-70">${d.detail}</span>`, v: d.v ?? d.values })),
}
</script>

<template>
  <DocPage
    title="Out-of-context measurements"
    summary="Every synthesis sweep this project has run, what each one varied, and the configuration that makes its numbers quotable. One part, synthesis only."
    domain="framework"
    status="measured"
    :source="`build/sweep_*.md · scripts/py/ooc_sweep.py · scripts/tcl/ooc_class.tcl · ${PART} · ${TOOL}`"
  >
    <Callout kind="rule" title="Synthesis is not route, and a number without its configuration is not a result">
      <p>
        Everything on this page is <b>out-of-context synthesis</b> — no optimisation, no
        placement, no routing. It sizes designs and ranks options. It closes nothing. This
        project has measured a module lose <b>0.740&nbsp;ns</b> between synthesis and routing,
        so a small negative slack here is not something placement absorbs.
      </p>
      <p>
        And <b>LUT is not independent of the constraint</b>: synthesis spends area chasing
        timing it cannot reach. A resource figure only means something beside the target it was
        asked for, which is why every table below carries its period.
      </p>
    </Callout>

    <h2 class="doc-h2">How a row is produced</h2>
    <p class="doc-p">
      <code>ooc_sweep.py</code> runs each configuration of a suite in its own working directory
      under its own Vivado, three to five at a time. The Tcl emits <code>@@@REC</code>,
      <code>@@@FMAX</code> and <code>@@@HIER</code> lines from a <b>single</b> synthesis, and the
      script collects them into <code>build/sweep_&lt;suite&gt;.md</code>. That is what makes a
      sweep file internally consistent: the resource table, the per-clock Fmax table and the
      hierarchical breakdown all describe one netlist, so no two of them can disagree about
      which design they measure.
    </p>

    <Callout kind="trap" title="Two guards in that path, both of which failed silently first">
      <p>
        <b>An empty timing query reads exactly like a clean design.</b> Vivado returns nothing
        rather than erroring, so a run whose XDC did not apply prints no Fmax line and reports
        every LUT figure unconstrained — while looking fine. <code>ooc_simt_pe.tcl</code> now
        <b>errors if <code>get_clocks</code> comes back empty</b>.
      </p>
      <p>
        <b>A lone RAMB18 makes “Block RAM Tile” read 26.5.</b> An integer test drops that row and
        reports 0&nbsp;BRAM for a design that uses it, so <code>ooc_record</code> parses it as a
        double. Two more of the same shape are recorded in the Tcl itself:
        <code>PRIMITIVE_GROUP == CLB_LUT</code> matches nothing and returns zero silently, and
        <code>[</code> opens a character class in a Vivado glob, so a <code>g_stn[1]</code> prefix
        counted every bracketed instance as empty.
      </p>
    </Callout>

    <h2 class="doc-h2">The suites</h2>
    <SpecTable
      :cols="index.cols"
      :rows="index.rows"
      caption="Nine suites over the framework's own RTL. A row count short of the label count means the suite carries configurations that were never run or cannot be built — those are named where they matter."
    />

    <Callout kind="note" title="A project's sweeps are on the project's page">
      <p>
        Eight more suites — <code>gpu-ladder</code> through <code>gpu-pe</code> — measure the
        SIMT PE under <code>src/kohakumpe/simt/</code>, and they live at
        <a class="text-gem hover:underline" href="#/mpe/measurements">SIMT PE measurements</a>.
        They were on this page and should not have been: everything above is the method, which
        is shared, and everything below is the framework's own RTL, which is not.
      </p>
    </Callout>

    <h2 class="doc-h2">The station bus</h2>
    <p class="doc-p">
      Seven suites, at two levels: one station on its own, and the four-station line as deployed.
      All figures are <b>CLB LUT sites</b>, not primitive counts — a hand census of primitives
      overstates by about 1.6× because two <code>RAMD32</code> occupy one LUT6.
    </p>

    <h3 class="doc-h3">One station: the option space, and what is worth turning</h3>
    <SpecTable :cols="presetTable.cols" :rows="presetTable.rows" :caption="src('station-fw512')" />
    <SpecTable :cols="widthTable.cols" :rows="widthTable.rows" :caption="src('station-width')" />
    <p class="doc-p">
      Flit width spans 10,076 to 22,641 LUT over an eightfold range while the whole
      outstanding-transaction axis spans a few percent, and 43 address bits — which the mesh map
      forces — cost 2.4% over 32. <b>Width is the lever; the presets are not.</b>
    </p>

    <h3 class="doc-h3">Port cost at a uniform width</h3>
    <p class="doc-p">
      The deployed line mixes one wide port per station with 32-bit ones. This suite instead makes
      <b>every</b> port 512 bits — three managers against Q subordinates — so the marginal cost
      comes out at one width rather than averaged over a mix.
    </p>

    <SpecTable :cols="portTable.cols" :rows="portTable.rows" :caption="src('station-ports')" />
    <SpecTable
      :cols="portFit.cols"
      :rows="portFit.rows"
      caption="Fitted on the endpoints only, then checked against the interior point"
    />

    <Callout kind="measured" title="This is the flattest port-cost result in the project">
      <p>
        Control sets are <b>exactly 42 per port</b> and LUTRAM <b>exactly 1,080 per port</b> at
        both intervals, with no curvature at either. Fitting only Q=1 and Q=4 predicts Q=2 to
        within <b>0.16%</b>. The deployed line's 32-bit slope scatters ±20% around 818 and its
        worst interior prediction is 3.1% out, so this is the stronger of the two runs and it is
        the one to hold beside a vendor crossbar.
      </p>
      <p>
        <b>A 512-bit subordinate port costs 1,621 LUT; a 32-bit one costs 818.</b> Sixteen times
        the width for 1.98× the port — cost tracks width and buffering, not port count, and most
        of it is buffer.
      </p>
    </Callout>

    <Callout kind="trap" title="Only bus_clk moves, and it moves a long way">
      <code>clk_ctrl</code> and <code>clk_xdma</code> are identical to the tenth of a megahertz at
      every Q — 415.6 and 398.7 — which is the check that the sweep varies what it claims to.
      <code>bus_clk</code> loses <b>67&nbsp;MHz for the first added port</b> and 18 more for the
      next two: a widening fan-in, punished most by its first doubling. Q&gt;8 and K&gt;4 cannot be
      measured here at all — <code>sb_stn_root</code> fixes <code>DSTW=3</code> and
      <code>SRCW=2</code>, and beyond those the generator emits out-of-range part-selects.
    </Callout>

    <h3 class="doc-h3">The four-station line</h3>
    <SpecTable :cols="linePortTable.cols" :rows="linePortTable.rows" :caption="src('line-ports')" />
    <p class="doc-p">
      The manager steps are measured one at a time, so width separates cleanly:
      <b>4,212 LUT for the 512-bit manager</b> against 822 and 1,217 for the 32-bit ones. Each
      sweep holds the other side fixed, so they establish linearity in each count separately —
      <b>not</b> independence between them, which remains a structural argument.
    </p>

    <SpecTable :cols="lineWidthTable.cols" :rows="lineWidthTable.rows" :caption="src('line-width')" />
    <p class="doc-p">
      Control sets are flat across an eightfold change in width — the growth is entirely
      datapath — and cost per unit bandwidth falls monotonically, 5,350 LUT per GB/s at 128 down
      to 1,914 at 1024. The <code>FW=256</code> row is the deployed configuration, and
      <b>its Fmax rows are the same synthesis</b> that the deployment's eleven-clock timing table
      comes from.
    </p>

    <SpecTable :cols="freqTable.cols" :rows="freqTable.rows" :caption="src('line-freq')" />

    <Callout kind="measured" title="Area is flat to a 300 MHz ask, then synthesis starts paying for megahertz it will not get">
      0.3% across 150–300&nbsp;MHz, +6.3% at 350 and +11% beyond. The structure saturates at
      <b>390.5&nbsp;MHz</b> however hard it is pushed, and 350 is the highest target that closes.
      At the deployed 200&nbsp;MHz the constraint costs nothing at all — which is the general
      shape: below the ceiling a tighter ask buys area, not frequency.
    </Callout>

    <h2 class="doc-h2">Vendor anchors</h2>
    <Callout kind="trap" title="These are whole-harness totals, and they are not the instance figures">
      <p>
        The station-bus document reports the <code>smc</code> / <code>xbar</code> instance alone,
        lifted from the hierarchical report; the sweep files record the entire block design.
        3×9 four-clock max-performance is <b>18,627</b> here and <b>16,532</b> as an instance.
        <b>Difference within one frame, never across the two.</b>
      </p>
      <p>
        And take a vendor baseline from <b>the vendor's own report</b>, never a reconstruction:
        the shipped <code>root_smc</code> is 41,788 LUT, a rebuild with <code>axi_vip</code>
        endpoints gave 12,481 (3.3× low) and one with real <code>axi_bram_ctrl</code> endpoints
        gave 21,885 (1.9× low). A reconstruction compares shapes against each other, nothing more.
      </p>
    </Callout>

    <SpecTable :cols="vendorTable.cols" :rows="vendorTable.rows" :caption="`build/sweep_xbar-anchor.md and build/sweep_smc-base.md · 3 slave interfaces · ${PART}`" />

    <p class="doc-p">
      Four of those rows are new — <code>axi_interconnect</code> at 3×5 and at 256 bits, in both
      strategies — and they give the crossbar the two slopes only SmartConnect had before.
    </p>

    <SpecTable :cols="portSlopes.cols" :rows="portSlopes.rows" caption="Marginal subordinate port, 5 → 9 at three managers and four clock domains" />
    <p class="doc-p">
      The two full-AXI4 crossbars agree with each other to 4% and both cost about 2,000 LUT a
      port, against the station's 1,621 <b>at the same port width</b>. SASD is cheaper and is not
      a substitute: one outstanding transaction, one serialised path, AXI3 internally.
    </p>

    <SpecTable :cols="clockCost.cols" :rows="clockCost.rows" caption="1 → 4 clock domains, 3×9, 512-bit, same harness" />

    <Callout kind="trap" title="SmartConnect's +525 is the failure, not a saving">
      Its LUTRAM is <b>4,522 in both rows, to the digit</b>, and an added asynchronous domain
      cannot be free in a structure whose crossings are LUTRAM FIFOs. The three extra
      <code>clk_wiz</code> instances are common to both comparisons, so the gap between +525 and
      +10,693 is the IP: one built the domains and the other did not, silently, with every port
      driven from its own clock and a declared <code>FREQ_HZ</code>. A structure that cannot be
      misconfigured this way is worth more than the LUT difference.
    </Callout>

    <p class="doc-p">
      Halving the width returns <b>28.2%</b> on the station line, 20.0% on max-performance
      <code>axi_interconnect</code>, 10.1% on SASD and 10.0% on the SmartConnect rebuild. The
      ordering is the structural claim intact across four independent measurements: the more of a
      design is datapath, the more width returns.
    </p>

    <Callout kind="open" title="One row nothing here explains">
      SASD at 3×5 reports 2,048 LUTRAM against 2,560 at 3×9 and 448 at one clock — more
      distributed RAM at five ports than the max-performance build of the same shape (1,424). No
      claim on this page rests on it. It is recorded rather than smoothed over.
    </Callout>

    <h2 class="doc-h2">Where the sweeps disagree with each other</h2>
    <SpecTable :cols="disagree.cols" :rows="disagree.rows" />

    <Callout kind="rule" title="Do not subtract rows that came from different suites">
      Four LUT is nothing, and it matters only if someone differences two suites and reads the
      residue as a gate. Within a suite the rows are comparable by construction — one script, one
      module, one generic changing. Across suites they are comparable only as far as they have
      been checked to be.
    </Callout>

    <h2 class="doc-h2">Reproducing any of it</h2>
    <p class="doc-p">
      One command per suite. Each writes the file named in the table above, overwriting it, and
      prints per-configuration progress as the jobs land.
    </p>

    <Fig pad caption="Each configuration gets its own working directory and its own Vivado; concurrency is capped per suite because a line job costs ~10 GiB resident, not the ~3 GiB its own report calls a peak.">
      <pre class="kt-text-caption font-mono text-warm-600 dark:text-warm-400 overflow-x-auto leading-6"><code>python scripts/py/ooc_sweep.py station-fw512   # -&gt; build/sweep_station-fw512.md
python scripts/py/ooc_sweep.py station-ports   # regenerates the wrappers first
python scripts/py/ooc_sweep.py line-width
python scripts/py/ooc_sweep.py xbar-anchor

# one row on its own -- the suite names the tcl and the argument order
vivado -mode batch -source scripts/tcl/ooc_station.tcl -tclargs 512 3 4 1</code></pre>
    </Fig>

    <Callout kind="note" title="The frontier tools are a different shape and live elsewhere">
      <p>
        The PE tiers are not swept this way. <code>tests/pe/tools/rv_frontier.py</code> and
        <code>khs_frontier.py</code> measure one configuration at <b>several clock targets</b> and
        append CSV rows, because a frontier point is a design variant <i>crossed with</i> a
        constraint. They also carry the cycle counts along on every row — a low-LUT point that
        costs twice the cycles is a different animal from one at the same cycles — and
        <b>a configuration that fails its own component test contributes no row at all</b>.
      </p>
      <p>
        <code>khs_frontier.py</code> additionally stamps each row with a declared <b>RTL
        revision</b>, because a configuration measured before an RTL change is not comparable to
        one measured after it, and an unlabelled row is not a measurement. Their results are
        written up in
        <a class="text-gem hover:underline" href="#/mpe/cpu">the controller PE</a> and
        <a class="text-gem hover:underline" href="#/mpe/simd">the SIMD PE</a> pages.
      </p>
    </Callout>
  </DocPage>
</template>
