<script setup>
/**
 * /mpe/measurements — the SIMT PE's out-of-context sweeps.
 *
 * Split out of /framework/measurements, which now carries only the framework's
 * own suites. `kht_*` is KohakuMPE's, under src/kohakumpe/simt/, and a
 * framework page may not describe a project's module.
 *
 * Drawn, in full, from:
 *   build/sweep_gpu-*.md          (8 suites, the raw records)
 *   scripts/py/ooc_sweep.py       (what each suite varies, and what it holds)
 *   scripts/tcl/ooc_simt_pe.tcl, scripts/xdc/ooc_khg.xdc
 *   docs/projects/kohakumpe/simt/ladder.md, status.md
 *
 * Every number is transcribed in src/content/ooc.js, not typed here. One part,
 * xcvu13p-fhgb2104-2L-e, out-of-context SYNTHESIS ONLY — no opt, no place, no
 * route.
 */
import { SWEEPS, PART, TOOL, rowsOf, bindingClock } from "@/content/ooc";

const n = (v) => (v == null ? "—" : v.toLocaleString());
const sign = (v) => (v >= 0 ? `+${n(v)}` : n(v));
const mhz = (v) => (v == null ? "—" : v.toFixed(1));

const src = (name) => {
  const s = SWEEPS[name];
  return `${s.file} · ${s.top} · ${s.fixed} · ${PART}`;
};

function resTable(name, opts = {}) {
  const cols = [
    { key: "config", label: "config", mono: true },
    { key: "lut", label: "LUT", mono: true, align: "right" },
    ...(opts.lutram
      ? [{ key: "lutram", label: "LUTRAM", mono: true, align: "right" }]
      : []),
    { key: "ff", label: "FF", mono: true, align: "right" },
    ...(opts.noBram
      ? []
      : [{ key: "bram", label: "BRAM", mono: true, align: "right" }]),
    { key: "cs", label: "ctrl sets", mono: true, align: "right" },
    { key: "fmax", label: "binding clock, MHz", mono: true, align: "right" },
  ];
  const rows = rowsOf(name).map((r) => {
    const b = bindingClock(name, r.config);
    return {
      config: r.config,
      lut: `<b>${n(r.lut)}</b>`,
      lutram: n(r.lut_dram),
      ff: n(r.ff),
      bram: n(r.bram),
      cs: n(r.ctrlsets),
      fmax: b ? `${mhz(b[1])} <span class="opacity-60">${b[0]}</span>` : "—",
    };
  });
  return { cols, rows };
}

/* ---- the suites on this page -------------------------------------------- */
const GPU = [
  "gpu-ladder",
  "gpu-waves",
  "gpu-sched",
  "gpu-lanes",
  "gpu-lds",
  "gpu-shfl",
  "gpu-vregprim",
  "gpu-pe",
];

const index = {
  cols: [
    { key: "suite", label: "suite", mono: true },
    { key: "varies", label: "what it varies" },
    { key: "top", label: "top", mono: true },
    { key: "got", label: "rows", mono: true, align: "right" },
  ],
  rows: GPU.map((k) => {
    const s = SWEEPS[k];
    return {
      suite: k,
      varies: s.varies,
      top: s.top,
      got: `${rowsOf(k).length}`,
    };
  }),
};

/* ---- the ladder --------------------------------------------------------- */
const L = SWEEPS["gpu-ladder"].rows;
const ladderBars = L.map((r, i) => ({
  label: `${["G0 substrate", "G1 sixteen waves", "G2 active mask", "G3 split/join"][i]}`,
  value: r.lut,
  note: i === 0 ? "baseline" : `${sign(r.lut - L[0].lut)} on G0`,
  tone: i === 1 ? "good" : "accent",
}));

const ladderTable = {
  cols: [
    { key: "gate", label: "gate" },
    { key: "gen", label: "generics", mono: true },
    { key: "lut", label: "LUT", mono: true, align: "right" },
    { key: "d", label: "ΔLUT", mono: true, align: "right" },
    { key: "ff", label: "FF", mono: true, align: "right" },
    { key: "bram", label: "BRAM", mono: true, align: "right" },
    { key: "cs", label: "ctrl sets", mono: true, align: "right" },
    { key: "fmax", label: "Fmax, MHz", mono: true, align: "right" },
  ],
  rows: L.map((r, i) => ({
    gate: [
      "G0 the arithmetic substrate",
      "G1 wave-indexed storage",
      "G2 the active mask",
      "G3 the IPDOM stack",
    ][i],
    gen: ["WAVES 1", "WAVES 16", "HAS_MASK 1", "HAS_IPDOM 1"][i],
    lut: `<b>${n(r.lut)}</b>`,
    d: i === 0 ? "—" : `<b>${sign(r.lut - L[i - 1].lut)}</b>`,
    ff: n(r.ff),
    bram: n(r.bram),
    cs: n(r.ctrlsets),
    fmax: "324.1",
    _tone: i === 1 ? "good" : undefined,
  })),
};

/* ---- the three prices of a wave ----------------------------------------- */
const W = SWEEPS["gpu-waves"].rows;
const S = SWEEPS["gpu-sched"].rows;
const wavePrice = {
  cols: [
    { key: "what", label: "sixteen wave contexts…" },
    { key: "top", label: "measured on", mono: true },
    { key: "lut", label: "ΔLUT", mono: true, align: "right" },
    { key: "ff", label: "ΔFF", mono: true, align: "right" },
  ],
  rows: [
    {
      what: "…in <b>storage</b>, mask and IPDOM off",
      top: "kht_unit",
      lut: "<b>+0</b>",
      ff: "+4",
      _tone: "good",
    },
    {
      what: "…in storage, mask and IPDOM <b>on</b>",
      top: "kht_unit",
      lut: `+${n(W[4].lut - W[0].lut)}`,
      ff: `+${n(W[4].ff - W[0].ff)}`,
    },
    {
      what: "…<b>scheduled</b> — the front end",
      top: "kht_core − kht_unit",
      lut: `<b>+${n(S[4].lut - S[0].lut - (W[4].lut - W[0].lut))}</b>`,
      ff: `+${n(S[4].ff - S[0].ff - (W[4].ff - W[0].ff))}`,
      _tone: "warn",
    },
  ],
};

const schedTable = {
  cols: [
    { key: "w", label: "WAVES", mono: true, align: "right" },
    { key: "u", label: "kht_unit LUT", mono: true, align: "right" },
    { key: "c", label: "kht_core LUT", mono: true, align: "right" },
    { key: "d", label: "the scheduler", mono: true, align: "right" },
    { key: "cff", label: "core FF", mono: true, align: "right" },
    { key: "f", label: "core Fmax", mono: true, align: "right" },
  ],
  rows: [1, 2, 4, 8, 16].map((w, i) => ({
    w: `${w}`,
    u: n(W[i].lut),
    c: n(S[i].lut),
    d: n(S[i].lut - W[i].lut),
    cff: n(S[i].ff),
    f: `${SWEEPS["gpu-sched"].fmax[S[i].config].noc_clk} MHz`,
  })),
};

/* ---- lanes, LDS, butterfly ---------------------------------------------- */
const LN = SWEEPS["gpu-lanes"].rows;
const LD = SWEEPS["gpu-lds"].rows;
const SH = SWEEPS["gpu-shfl"].rows;
const LANES = [4, 8, 16, 32];

const crossLane = {
  cols: [
    { key: "l", label: "LANES", mono: true, align: "right" },
    { key: "u", label: "kht_unit", mono: true, align: "right" },
    { key: "uf", label: "Fmax", mono: true, align: "right" },
    { key: "g8", label: "G8 butterfly ΔLUT", mono: true, align: "right" },
    { key: "g8f", label: "Fmax", mono: true, align: "right" },
    { key: "g4", label: "G4 kht_lds", mono: true, align: "right" },
    { key: "g4f", label: "Fmax", mono: true, align: "right" },
  ],
  rows: LANES.map((l, i) => ({
    l: `${l}`,
    u: n(LN[i].lut),
    uf: mhz(SWEEPS["gpu-lanes"].fmax[LN[i].config].noc_clk),
    g8: `<b>+${n(SH[i * 2 + 1].lut - SH[i * 2].lut)}</b>`,
    g8f: mhz(SWEEPS["gpu-shfl"].fmax[SH[i * 2 + 1].config].noc_clk),
    g4: `<b>${n(LD[i].lut)}</b>`,
    g4f: mhz(SWEEPS["gpu-lds"].fmax[LD[i].config].noc_clk),
    _tone: l === 32 ? "bad" : undefined,
  })),
};

const ldsBars = LD.map((r, i) => ({
  label: `kht_lds, ${LANES[i]} lanes`,
  value: r.lut,
  note:
    i === 0
      ? `${SWEEPS["gpu-lds"].fmax[r.config].noc_clk} MHz`
      : `${(r.lut / LD[i - 1].lut).toFixed(1)}× · ${SWEEPS["gpu-lds"].fmax[r.config].noc_clk} MHz`,
  tone: LANES[i] >= 16 ? "bad" : "accent",
}));

const vregTable = resTable("gpu-vregprim", { lutram: true });

/* ---- scope: submodules against the assembled PE ------------------------- */
const PE = Object.fromEntries(SWEEPS["gpu-pe"].rows.map((r) => [r.config, r]));
const peF = (c) => SWEEPS["gpu-pe"].fmax[c].noc_clk;

const scopeTable = {
  cols: [
    { key: "top", label: "top", mono: true },
    { key: "what", label: "what it contains" },
    { key: "lut", label: "LUT", mono: true, align: "right" },
    { key: "f", label: "Fmax, MHz", mono: true, align: "right" },
    { key: "src", label: "source", mono: true },
  ],
  rows: [
    {
      top: "kht_unit",
      what: "G0–G3, butterfly off",
      lut: n(SWEEPS["gpu-ladder"].rows[3].lut),
      f: "324.1",
      src: "gpu-ladder",
    },
    {
      top: "kht_unit",
      what: "G0–G3 <b>+ G8</b>",
      lut: n(SH[3].lut),
      f: "285.5",
      src: "gpu-shfl",
    },
    {
      top: "kht_lds",
      what: "G4, on its own",
      lut: n(LD[1].lut),
      f: "514.9",
      src: "gpu-lds",
    },
    {
      top: "kht_core",
      what: "the pipeline — <b>a kht_unit is inside it</b>",
      lut: n(S[4].lut),
      f: "279.5",
      src: "gpu-sched",
    },
    {
      top: "kht_pe",
      what: "<b>the assembled PE</b> — the L1, the requestor, the fabric port",
      lut: `<b>${n(PE["pe-l8-w16"].lut)}</b>`,
      f: `<b>${mhz(peF("pe-l8-w16"))}</b>`,
      src: "gpu-pe",
      _tone: "warn",
    },
  ],
};

const peTable = {
  cols: [
    { key: "l", label: "LANES", mono: true, align: "right" },
    { key: "w", label: "WAVES", mono: true, align: "right" },
    { key: "lut", label: "LUT", mono: true, align: "right" },
    { key: "ram", label: "of which LUTRAM", mono: true, align: "right" },
    { key: "ff", label: "FF", mono: true, align: "right" },
    { key: "bram", label: "BRAM", mono: true, align: "right" },
    { key: "cs", label: "ctrl sets", mono: true, align: "right" },
    { key: "f", label: "Fmax, MHz", mono: true, align: "right" },
  ],
  rows: [
    ["pe-l4-w16", 4, 16],
    ["pe-l8-w1", 8, 1],
    ["pe-l8-w16", 8, 16],
    ["pe-l16-w16", 16, 16],
  ].map(([c, l, w]) => {
    const r = PE[c];
    const ship = c === "pe-l8-w16";
    return {
      l: `${l}`,
      w: `${w}`,
      lut: ship ? `<b>${n(r.lut)}</b>` : n(r.lut),
      ram: n(r.lut_dram),
      ff: n(r.ff),
      bram: n(r.bram),
      cs: n(r.ctrlsets),
      f: ship ? `<b>${mhz(peF(c))}</b>` : mhz(peF(c)),
      _tone: l === 16 ? "bad" : ship ? "warn" : undefined,
    };
  }),
};

const nameTheTop = {
  cols: [
    { key: "top", label: "top", mono: true },
    { key: "f", label: "Fmax", mono: true, align: "right" },
    { key: "how", label: "found by" },
  ],
  rows: [
    { top: "kht_unit", f: "324.1 MHz", how: "the ladder — every G0–G3 row" },
    {
      top: "kht_core",
      f: "<b>71.7 MHz</b>",
      how: "measuring G7 — a 44-level serial cross-lane reduction, since rebuilt as a pipelined tree",
      _tone: "bad",
    },
    {
      top: "kht_pe",
      f: "<b>182.0 MHz</b>",
      how: "the gpu-pe suite — binding path not yet read out",
      _tone: "warn",
    },
  ],
};

/* ---- the cross-lane chart ----------------------------------------------- */
const CW = 640;
const CH = 260;
const PADL = 54;
const PADR = 96;
const PADT = 16;
const PADB = 34;
const yMax = 26000;
const xOf = (i) => PADL + (i / 3) * (CW - PADL - PADR);
const yOf = (v) => CH - PADB - (v / yMax) * (CH - PADT - PADB);
const path = (vals) =>
  vals
    .map((v, i) => `${i ? "L" : "M"}${xOf(i).toFixed(1)},${yOf(v).toFixed(1)}`)
    .join(" ");

const series = [
  {
    name: "kht_lds (N²)",
    vals: LD.map((r) => r.lut),
    stroke: "var(--gem-main)",
    w: 2.5,
  },
  {
    name: "kht_unit",
    vals: LN.map((r) => r.lut),
    stroke: "currentColor",
    w: 1.6,
    dash: "0",
  },
  {
    name: "G8 butterfly",
    vals: LANES.map((_, i) => SH[i * 2 + 1].lut - SH[i * 2].lut),
    stroke: "currentColor",
    w: 1.6,
    dash: "5 3",
  },
];
const yTicks = [0, 5000, 10000, 15000, 20000, 25000];
const tick = (t) => (t === 0 ? "0" : `${t / 1000}k`);
</script>

<template>
  <DocPage
    title="SIMT PE measurements"
    summary="Eight synthesis sweeps over the SIMT unit, its scheduler, its cross-lane networks and the assembled PE. One part, synthesis only."
    domain="simt"
    status="measured"
    :source="`build/sweep_gpu-*.md · scripts/py/ooc_sweep.py · scripts/tcl/ooc_simt_pe.tcl · ${PART} · ${TOOL}`"
  >
    <Callout
      kind="rule"
      title="Synthesis is not route, and a number without its configuration is not a result"
    >
      <p>
        Everything on this page is <b>out-of-context synthesis</b> — no
        optimisation, no placement, no routing. It sizes designs and ranks
        options. It closes nothing, and this repository has measured a module
        lose <b>0.740&nbsp;ns</b> between synthesis and routing, so a small
        negative slack here is not something placement absorbs.
        <b>LUT is not independent of the constraint</b> either: synthesis spends
        area chasing timing it cannot reach, so every table carries the period
        it was asked for. How a row is produced, and the two guards in that path
        that failed silently first, are on
        <RouterLink to="/framework/measurements" class="doc-link"
          >the framework's measurement page</RouterLink
        >.
      </p>
    </Callout>

    <Callout
      kind="trap"
      title="Every row on this page is -flatten_hierarchy none, and none is not the ship"
    >
      <p>
        Nothing in <code>scripts/tcl</code> sets
        <code>FLATTEN_HIERARCHY</code> on the ship's synthesis run, so the ship
        takes Vivado's own default, <code>rebuilt</code>. Measured on the
        assembled PE at the same request, <code>none</code> read
        <b>636 LUT high</b> — 22,257 against 21,621 — because a preserved
        boundary cannot trim an unread output port or fold a constant across a
        module edge.
      </p>
      <p>
        <code>none</code> is what makes a per-block row <b>attributable</b>,
        which is exactly what a ladder needs, so it stays the diagnostic and it
        is the right setting for everything here.
        <b>A row quoted against a budget must be <code>rebuilt</code></b>, and
        the arithmetic-tier tables on the
        <RouterLink to="/mpe/simt" class="doc-link">SIMT PE page</RouterLink>
        are — so the two sets of figures do not subtract. The symptom of mixing
        them is a delta of a few hundred LUT that reads as a design change.
      </p>
    </Callout>

    <h2 class="doc-h2">The suites</h2>
    <SpecTable
      :cols="index.cols"
      :rows="index.rows"
      caption="Eight suites over src/kohakumpe/simt/. The framework's own suites — the station bus, the line, the vendor anchors — are measured separately."
    />

    <h2 class="doc-h2">The result that matters most: sixteen waves are free</h2>
    <p class="doc-p">
      The ladder is arranged so that one gate turns one generic on one module,
      which makes each delta attributable to that parameter and nothing else. A
      gate that is not built elaborates <i>none</i> of its logic — that property
      is what lets the deltas be added up, and a gate that leaks any logic when
      off makes every delta below it unattributable.
    </p>

    <ResourceBars
      :items="ladderBars"
      unit="CLB LUT · kht_unit, 8 lanes, block register file, 3.333 ns"
      caption="The SIMT gate ladder"
    />

    <SpecTable
      :cols="ladderTable.cols"
      :rows="ladderTable.rows"
      :caption="src('gpu-ladder')"
    />

    <Callout
      kind="measured"
      title="Sixteen resident wave contexts cost zero LUT, zero BRAM, zero control sets and four flip-flops"
    >
      <p>
        Not “almost nothing” — nothing, to within the four flops of a wave-id
        register. The register file was already a memory, so a wave id is
        <b>address bits</b>: sixteen contexts deepen a BRAM that was already
        there rather than adding one.
      </p>
      <p>
        This is the load-bearing measurement, because many resident contexts is
        the part of SIMT that sounds expensive and is the whole reason the model
        tolerates a cache miss. It was the design's headline projection
        <b>and</b> its single largest exposure, because Vortex measures the
        opposite — <b>+13.9% LUT for one doubling of warps</b>. On this fabric,
        at this width, it is +0.
      </p>
      <p>
        Masks and the divergence stack together cost <b>252 LUT</b> on a 2,952
        substrate, and <b>Fmax never moves</b>: all four gates land at
        324.1&nbsp;MHz. The usual objection to SIMT is that divergence tracking
        sits on the critical path. Here it measurably does not.
      </p>
    </Callout>

    <Callout kind="trap" title="This is the SIMT unit, NOT an assembled PE">
      <p>
        Every row above is <code>kht_unit</code> — one submodule. The 20–25k LUT
        budget the GPU PE is judged against is about <code>kht_pe</code>, which
        is a different measurement and is <b>five times larger</b>. A ladder
        whose top is one submodule cannot even see a path that leaves it.
      </p>
    </Callout>

    <SpecTable
      :cols="scopeTable.cols"
      :rows="scopeTable.rows"
      caption="Five scopes, and they do not add up: kht_core contains a kht_unit, and kht_pe contains both."
    />

    <h3 class="doc-h3">The assembled PE: the area is fine, the clock is not</h3>
    <SpecTable
      :cols="peTable.cols"
      :rows="peTable.rows"
      :caption="src('gpu-pe')"
    />

    <Callout
      kind="measured"
      title="16,115 LUT at eight lanes — inside the band, and 118 MHz short of the mesh clock"
    >
      <p>
        The area lands where the design wanted it: <b>16,115 LUT</b> against the
        20–25k band this PE is judged against, with room. But
        <b>it must not be read against a budget that assumes arithmetic</b>:
        this row carries <b>no float units and no multiplier</b>.
        <code>kht_valu</code> is <code>LANES</code> copies of the base RV32I
        ALU — ten operations, no multiplier and no float
        <i>inside that module</i> — and the float units and the multiplier
        arrive as <b>sibling</b> modules beside it,
        <code>kht_fpu</code> and <code>kht_imul</code>. That is exactly why the
        ladder never sees them, and why the ladder's totals are not the PE. The
        SIMT PE <b>does</b> have a float tier and RV32M, both built and both
        run; what they cost is a
        <RouterLink to="/mpe/simt" class="doc-link">separate table</RouterLink>
        taken at a different flatten setting.
      </p>
      <p>
        <b>Sixteen lanes is 29,961</b>: past the 25k band, at the 30k review
        line, and slower still. Its LUTRAM barely moves — 3,538 to 3,568 across
        a fourfold change in <code>LANES</code> — so all of that growth is
        logic.
      </p>
      <p>
        And <b>182.0&nbsp;MHz against a 300&nbsp;MHz mesh clock.</b> Every
        <code>kht_unit</code> row sits at 324.1; the assembly does not.
      </p>
    </Callout>

    <Callout
      kind="trap"
      title="These assembled rows are a starting point, not a result"
    >
      <p>
        They <b>predate the frequency work</b> described on the
        <RouterLink to="/mpe/simt/microarchitecture" class="doc-link"
          >microarchitecture page</RouterLink
        >, which took this same shape from <b>182 to 394 MHz on 321 LUT
        fewer</b> — breaking long combinational cones lets the tool pack simpler
        logic, and the only thing that grew was flip-flops. They also predate
        the arithmetic tier entirely.
      </p>
      <p>
        The 4- and 16-lane rows have <b>never been re-swept</b>, so they remain
        the only figures that exist for those widths. Read them as the shape of
        lane scaling on an assembled PE, and not as what the PE measures today.
      </p>
    </Callout>

    <SpecTable
      :cols="nameTheTop.cols"
      :rows="nameTheTop.rows"
      caption="Name the top. Each figure below came from a different level, and a frequency quoted without its top module names none of them."
    />

    <h2 class="doc-h2">What a wave actually costs, in three parts</h2>
    <p class="doc-p">
      G1's +0 was measured with the mask and the stack <i>off</i>. Turning them
      on, and then moving the top up a level to where waves genuinely issue
      rather than merely exist, splits the question into three answers — which
      is exactly why they are swept separately.
    </p>

    <SpecTable
      :cols="wavePrice.cols"
      :rows="wavePrice.rows"
      caption="build/sweep_gpu-waves.md against build/sweep_gpu-sched.md · WAVES 1 → 16 · 8 lanes · 3.333 ns"
    />

    <p class="doc-p">
      “Sixteen waves are free” is true of the first line only. The scheduler
      <b>refines</b> G1 rather than contradicting it: storage is address bits,
      but the active mask and the divergence stack are per-wave arrays, and
      issuing is a front end.
    </p>

    <SpecTable
      :cols="schedTable.cols"
      :rows="schedTable.rows"
      :caption="src('gpu-sched')"
    />

    <Callout kind="note" title="Core Fmax does not trend with WAVES">
      The 262.7–294.6&nbsp;MHz spread across the five rows has no slope in it.
      It is noise, and reading a scheduling cost into it would be reading noise.
    </Callout>

    <h2 class="doc-h2">Where the lanes stop being free</h2>
    <p class="doc-p">
      <code>kht_unit</code> is straight in LANES and flat in frequency across a
      factor of eight, because <b>there is no cross-lane network in it</b> —
      lanes are independent, so a wider array is wider and not deeper. Two
      structures break that caveat: the banked LDS is quadratic and the
      butterfly is N&nbsp;log&nbsp;N, and both cost clock.
    </p>

    <Fig
      caption="LUT against lane count. The resolver's slope is the complexity class becoming visible; nothing else on the page bends like it."
    >
      <svg :viewBox="`0 0 ${CW} ${CH}`" class="dgm" role="img">
        <g stroke="currentColor" stroke-opacity="0.18" stroke-width="1">
          <line
            v-for="t in yTicks"
            :key="t"
            :x1="PADL"
            :x2="CW - PADR"
            :y1="yOf(t)"
            :y2="yOf(t)"
          />
        </g>
        <g
          fill="currentColor"
          fill-opacity="0.55"
          font-size="10"
          text-anchor="end"
        >
          <text v-for="t in yTicks" :key="t" :x="PADL - 8" :y="yOf(t) + 3">
            {{ tick(t) }}
          </text>
        </g>
        <g
          fill="currentColor"
          fill-opacity="0.55"
          font-size="10"
          text-anchor="middle"
        >
          <text
            v-for="(l, i) in LANES"
            :key="l"
            :x="xOf(i)"
            :y="CH - PADB + 16"
          >
            {{ l }}
          </text>
          <text :x="(PADL + CW - PADR) / 2" :y="CH - 4" font-size="9">
            LANES
          </text>
        </g>
        <g fill="none" stroke-linejoin="round" stroke-linecap="round">
          <path
            v-for="s in series"
            :key="s.name"
            :d="path(s.vals)"
            :stroke="s.stroke"
            :stroke-width="s.w"
            :stroke-dasharray="s.dash"
            :stroke-opacity="s.stroke === 'currentColor' ? 0.75 : 1"
          />
        </g>
        <g>
          <circle
            v-for="(v, i) in series[0].vals"
            :key="i"
            :cx="xOf(i)"
            :cy="yOf(v)"
            r="3"
            fill="var(--gem-main)"
          />
        </g>
        <g font-size="10" fill="currentColor">
          <text
            v-for="(s, i) in series"
            :key="s.name"
            :x="CW - PADR + 10"
            :y="yOf(s.vals[3]) + (i === 0 ? 4 : i === 1 ? 10 : -6)"
            :fill-opacity="i === 0 ? 1 : 0.7"
          >
            {{ s.name }}
          </text>
        </g>
      </svg>
    </Fig>

    <SpecTable
      :cols="crossLane.cols"
      :rows="crossLane.rows"
      caption="build/sweep_gpu-lanes.md, gpu-shfl.md and gpu-lds.md · kht_unit and kht_lds · 16 waves · block · 3.333 ns. Fmax in MHz."
    />

    <ResourceBars
      :items="ldsBars"
      unit="CLB LUT · kht_lds, 3.333 ns"
      caption="Every doubling of LANES costs about four times as much — the resolver is a LANES × LANES comparison"
    />

    <Callout
      kind="measured"
      title="G4 is the first gate to touch the clock, and 32 lanes is out"
    >
      <p>
        G0 through G3 all sat at 324.1&nbsp;MHz. The LDS runs 643 → 515 → 339 →
        <b>317.7</b>, and at 32 lanes it is
        <b>below the clock the rest of the unit meets</b>: the resolver becomes
        the binding path. Flops and BRAM stay linear throughout; only the LUT
        squares.
      </p>
      <p>
        <code>kht_unit</code> plus <code>kht_lds</code> alone is
        <b>38,439 LUT</b> at 32 lanes — past the 35k ceiling before the core,
        the L1, the requestor, the port or any unbuilt gate is counted, and it
        misses the clock while doing it. Eight lanes is 4,837; sixteen is
        12,549. Wanting 16+ means
        <b>a cheaper resolver, not a bigger budget</b>.
      </p>
    </Callout>

    <Callout
      kind="note"
      title="The butterfly is the same argument from the other side"
    >
      Both G8 and G4 are cross-lane networks and they differ in complexity class
      and in nothing else that matters. Per doubling of LANES, G8 grows about
      2–3.5× and G4 approaches 4×; at four lanes they are within a factor of
      two, at thirty-two the resolver costs
      <b>4.3×</b> the butterfly and is still climbing faster. G8 costs
      20–39&nbsp;MHz at every width and does <i>not</i> get worse — its depth
      grows as log while everything it competes with grows faster.
    </Callout>

    <h3 class="doc-h3">
      The register file is the largest single lever, and the default is right
    </h3>
    <SpecTable
      :cols="vregTable.cols"
      :rows="vregTable.rows"
      :caption="src('gpu-vregprim')"
    />
    <p class="doc-p">
      <code>distributed</code> costs <b>+6,226 LUT</b> — more than twice the
      entire G0 substrate — to buy 151&nbsp;MHz the design does not need, since
      the clock is already met at 324.1. It trades 8 BRAM for it.
      <code>block</code> ships.
    </p>

    <h2 class="doc-h2">Reproducing any of it</h2>
    <Fig
      pad
      caption="One command per suite; each overwrites the file named in the table above. The last form runs a single row."
    >
      <pre
        class="kt-text-caption font-mono text-warm-600 dark:text-warm-400 overflow-x-auto leading-6"
      ><code>python scripts/py/ooc_sweep.py gpu-ladder      # -&gt; build/sweep_gpu-ladder.md
python scripts/py/ooc_sweep.py gpu-pe

# one row on its own -- top lanes waves mask ipdom period vreg_prim has_shfl
vivado -mode batch -source scripts/tcl/ooc_simt_pe.tcl -tclargs \
    kht_unit 8 16 1 1 3.333 block 0</code></pre>
    </Fig>
  </DocPage>
</template>
