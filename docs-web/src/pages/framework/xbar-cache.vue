<script setup>
/**
 * /framework/xbar-cache — the fused crossbar-cache, kx_mempath_e.
 *
 * Drawn from, in full:
 *   docs/projects/kohakuaxi/xbar-cache.md
 *   src/kohakuaxi/{kx_mempath_e,kx_carray,kx_rd_engine,kx_wr_engine,kx_link,kx_scdc}.v
 *   scripts/tcl/ooc_kx.tcl · scripts/tcl/ooc_vendor_xc.tcl · scripts/tcl/ooc_syscache.tcl
 *
 * Every number is transcribed once, in src/content/estimator.js, and read from
 * there. One part, xcvu13p-fhgb2104-2L-e, out-of-context SYNTHESIS at a 300 MHz
 * ask — nothing is placed.
 */
import { PART, TARGET_MHZ, KX_ROWS, KX_VENDOR } from "@/content/estimator";

const n = (v) => (v == null ? "—" : Math.round(v).toLocaleString());

/* ---------------------------------------------------------------- structure */
const fused = {
  groups: [
    {
      x: -1.5,
      y: 5,
      w: 62,
      h: 14.5,
      label: "kx_mempath_e — ONE clock, clk. Nothing AXI-shaped inside",
    },
  ],
  nodes: [
    { id: "m0", x: 0, y: 0, w: 9, h: 3, label: "master 0", sub: "AXI4 · 512b" },
    { id: "m1", x: 11, y: 0, w: 9, h: 3, label: "master 1", sub: "AXI4 · 512b" },
    { id: "mx", x: 22, y: 0, w: 9, h: 3, label: "master M−1", sub: "AXI4 · 512b" },
    {
      id: "me",
      x: 0,
      y: 6,
      w: 31,
      h: 3,
      label: "master edges — kx_link × 5 per port",
      sub: "wire when MCDC[m]=0 · async FIFO when 1",
    },
    {
      id: "re",
      x: 0,
      y: 11,
      w: 14,
      h: 3.4,
      label: "read engines",
      sub: "control only · one per home (SAMD) or one for all (SASD)",
      accent: true,
    },
    {
      id: "we",
      x: 16,
      y: 11,
      w: 15,
      h: 3.4,
      label: "write engines",
      sub: "control only · RSAMD and WSAMD independent",
      accent: true,
    },
    {
      id: "xb",
      x: 34,
      y: 6,
      w: 26,
      h: 3,
      label: "the crossbar is not a module",
      sub: "N:1 per master (R) · M:1 per home (W) · registered binary indices",
      accent: true,
    },
    {
      id: "c0",
      x: 34,
      y: 11,
      w: 8,
      h: 3.4,
      label: "kx_carray 0",
      sub: "64 URAM · {v, tag, K×W}",
    },
    {
      id: "c1",
      x: 43,
      y: 11,
      w: 8,
      h: 3.4,
      label: "kx_carray 1",
      sub: "the only wide store",
    },
    {
      id: "cx",
      x: 52,
      y: 11,
      w: 8,
      h: 3.4,
      label: "kx_carray N−1",
      sub: "fill straight off R",
    },
    {
      id: "he",
      x: 0,
      y: 16,
      w: 60,
      h: 3,
      label: "DRAM edges — kx_link × 5 per home",
      sub: "wire when HCDC[h]=0 · async FIFO when 1 · W/R FIFOs in block RAM",
    },
    { id: "d0", x: 0, y: 21.5, w: 9, h: 3, label: "DRAM ch 0", sub: "AXI4 · 512b" },
    { id: "d1", x: 11, y: 21.5, w: 9, h: 3, label: "DRAM ch 1", sub: "AXI4 · 512b" },
    { id: "dx", x: 22, y: 21.5, w: 9, h: 3, label: "DRAM ch N−1", sub: "AXI4 · 512b" },
  ],
  edges: [
    { from: "m0:b", to: "me:t" },
    { from: "m1:b", to: "me:t" },
    { from: "mx:b", to: "me:t" },
    { from: "me:b", to: "re:t", label: "AR" },
    { from: "me:b", to: "we:t", label: "AW · W" },
    { from: "me:r", to: "xb:l", label: "W data", dir: "h", accent: true },
    { from: "xb:b", to: "c0:t", label: "word / write", accent: true },
    { from: "xb:b", to: "c1:t" },
    { from: "xb:b", to: "cx:t" },
    { from: "re:r", to: "c0:l", label: "idx · tag · sub", dir: "h" },
    { from: "re:b", to: "he:t", label: "AR, one line" },
    { from: "we:b", to: "he:t", label: "AW · W beats" },
    { from: "c0:b", to: "he:t", label: "R fills the line", dash: true },
    { from: "he:b", to: "d0:t" },
    { from: "he:b", to: "d1:t" },
    { from: "he:b", to: "dx:t" },
  ],
};

/* --------------------------------------------------------------- read engine */
const rdStates = [
  { id: "idle", x: 0, y: 0, label: "IDLE", sub: "round robin" },
  { id: "issue", x: 6, y: 0, label: "ISSUE", sub: "rd_en" },
  { id: "wait", x: 12, y: 0, label: "WAIT", sub: "RD_LAT+1" },
  { id: "chk", x: 18, y: 0, label: "CHK", sub: "hit_q?" },
  { id: "fetch", x: 24, y: 4, label: "FETCH", sub: "AR → R fills", accent: true },
  { id: "drain", x: 24, y: -4, label: "DRAIN", sub: "hold r_val", accent: true },
];
const rdEdges = [
  { from: "idle", to: "issue", label: "grant" },
  { from: "issue", to: "wait" },
  { from: "wait", to: "chk", label: "5 cycles, URAM" },
  { from: "chk", to: "drain", label: "hit" },
  { from: "chk", to: "fetch", label: "miss: m_arvalid" },
  { from: "fetch", to: "drain", label: "fill_done", curve: -30 },
  { from: "drain", to: "idle", label: "last beat", curve: 90 },
  { from: "drain", to: "issue", label: "next beat: +W/8", curve: 40 },
];

/* ----------------------------------------------------------------- knobs */
const knobCols = [
  { key: "p", label: "parameter", mono: true },
  { key: "v", label: "measured at", mono: true },
  { key: "m", label: "meaning" },
];
const knobRows = [
  { p: "M", v: "2, 4, 8", m: "masters" },
  { p: "N_HOME", v: "4, 8", m: "homes — DRAM channels, each with its cache" },
  { p: "W", v: "512", m: "IO width, <b>shared</b> by every port and the array word" },
  { p: "K", v: "1, 2, 4", m: "line width in IO words. K=1 allocates on a full-strobe write; K&gt;1 invalidates and fills from the channel's burst" },
  { p: "RSAMD", v: "0, 1", m: "1: one read engine per home, every home served in parallel. 0: one engine for all homes" },
  { p: "WSAMD", v: "0, 1", m: "the same on the write side, <b>independently</b>" },
  {
    p: "MCDC[m] · HCDC[h]",
    v: "none · 1111",
    m: "per port: 1 = this port is on its own clock and crosses at its edge; 0 = it is on <code>clk</code> and its edge is five wires",
    _tone: "good",
  },
  { p: "SETS · SET_W", v: "32768 · 15", m: "rows per home: 2 MB per home at K=1, <b>64 URAM</b>" },
  { p: "RAM_STYLE", v: "ultra", m: "the array primitive; <code>block</code> shortens RD_LAT from 4 to 1" },
  { p: "CDC_DEPTH", v: "16", m: "per-channel crossing FIFO depth — XPM's minimum" },
];

const clkCols = [
  { key: "c", label: "where clk comes from" },
  { key: "m", label: "MCDC", mono: true },
  { key: "h", label: "HCDC", mono: true },
  { key: "x", label: "crossings", mono: true, align: "right" },
];
const clkRows = [
  {
    c: "<b>the consumers' clock — ship</b>",
    m: "0000",
    h: "1111",
    x: "4, DRAM side",
    _tone: "good",
  },
  { c: "the DRAM clock", m: "1111", h: "0000", x: "4, master side" },
  { c: "a third clock", m: "1111", h: "1111", x: "8" },
  { c: "one clock everywhere", m: "0000", h: "0000", x: "0" },
  {
    c: "master 2 on another die, all else on clk",
    m: "0100",
    h: "0000",
    x: "1 — a die boundary is a port that differs",
  },
];

/* ------------------------------------------------------------- the table */
const tblCols = [
  { key: "m", label: "M", mono: true, align: "right" },
  { key: "nh", label: "N", mono: true, align: "right" },
  { key: "k", label: "K", mono: true, align: "right" },
  { key: "r", label: "read" },
  { key: "w", label: "write" },
  { key: "c", label: "crossings", mono: true, align: "right" },
  { key: "lut", label: "LUT", mono: true, align: "right" },
  { key: "ff", label: "FF", mono: true, align: "right" },
  { key: "u", label: "URAM", mono: true, align: "right" },
  { key: "b", label: "BRAM", mono: true, align: "right" },
  { key: "f", label: "Fmax MHz", mono: true, align: "right" },
];
const isShip = (r) => r[0] === 4 && r[1] === 4 && r[2] === 1 && r[3] && r[4];
const tblRows = KX_ROWS.map((r) => ({
  m: `${r[0]}`,
  nh: `${r[1]}`,
  k: `${r[2]}`,
  r: r[3] ? "SAMD" : "SASD",
  w: r[4] ? "SAMD" : "SASD",
  c: r[5] ? `${r[5]}` : "—",
  lut: isShip(r) ? `<b>${n(r[6])}</b>` : n(r[6]),
  ff: n(r[7]),
  u: n(r[8]),
  b: n(r[9]),
  f: r[10].toFixed(0),
  _tone: isShip(r) ? "good" : undefined,
}));
/* the LUTRAM-crossing row is not in the estimator's rows: it is the same
   design with the W/R FIFOs in distributed RAM, kept to price the choice */
tblRows.splice(tblRows.findIndex((r) => r.c === "4") + 1, 0, {
  m: "4",
  nh: "4",
  k: "1",
  r: "SAMD",
  w: "SAMD",
  c: "4",
  lut: "14,382",
  ff: "19,560",
  u: "256",
  b: "0",
  f: "456",
  _tone: "warn",
});

const knobCostCols = [
  { key: "k", label: "knob", mono: true },
  { key: "ft", label: "from → to", mono: true },
  { key: "l", label: "ΔLUT", mono: true, align: "right" },
  { key: "f", label: "ΔFF", mono: true, align: "right" },
  { key: "u", label: "ΔURAM", mono: true, align: "right" },
  { key: "w", label: "where it goes" },
];
const knobCostRows = [
  { k: "M", ft: "2 → 4", l: "+3,677", f: "+80", u: "0", w: "+1,839 per master at N=4" },
  { k: "M", ft: "4 → 8", l: "+5,218", f: "+118", u: "0", w: "+1,305 per master: a fixed per-master part plus an M·N crossbar part" },
  { k: "N_HOME", ft: "4 → 8", l: "+8,305", f: "+7,388", u: "+256", w: "≈ +2,076 per home: array 815, engines 257, crossbar leg ≈ 1,000" },
  { k: "M × N", ft: "4×4 → 8×8", l: "+4,757 over ΔM+ΔN", f: "", u: "", w: "the crossbar scales as M·N, not M+N", _tone: "warn" },
  { k: "K", ft: "1 → 2", l: "+4,553", f: "+6,162", u: "+224", w: "per-home line buffer and the wider row" },
  { k: "K", ft: "2 → 4", l: "+8,380", f: "+8,136", u: "+448", w: "≈ +4.2k per extra IO word of line, linear" },
  { k: "read SASD", ft: "SAMD → SASD", l: "−371", f: "−191", u: "0", w: "the per-home read engine is 140 LUT; sharing saves control only. −41 MHz" },
  { k: "write SASD", ft: "SAMD → SASD", l: "−2,220", f: "−373", u: "0", w: "collapses N write paths and each path's M:1 fan-in. −19 MHz" },
  { k: "both SASD", ft: "4×4", l: "−2,564", f: "−560", u: "0", w: "additive: −371 − 2,220 = −2,591 predicted" },
  { k: "both SASD", ft: "8×8", l: "−10,476", f: "", u: "", w: "the saving grows with M·N, not as a constant" },
  { k: "crossing, W/R in BRAM", ft: "per port", l: "<b>+488</b>", f: "+850", u: "+16 BRAM", w: "five async FIFOs; the two wide ones in RAMB36 at 1/32 occupancy", _tone: "good" },
  { k: "crossing, W/R in LUTRAM", ft: "per port", l: "+1,117", f: "+3,043", u: "0", w: "the same five FIFOs in distributed RAM", _tone: "warn" },
];

/* ------------------------------------------------------------- vendor */
const V = KX_VENDOR;
const ship = KX_ROWS.find((r) => isShip(r) && r[5] === 4);
const noCdc = KX_ROWS.find((r) => isShip(r) && r[5] === 0);
const vendorCols = [
  { key: "c", label: "4×4 @ 512, one synthesis each" },
  { key: "lut", label: "LUT", mono: true, align: "right" },
  { key: "ff", label: "FF", mono: true, align: "right" },
  { key: "b", label: "BRAM", mono: true, align: "right" },
  { key: "u", label: "URAM", mono: true, align: "right" },
  { key: "f", label: "Fmax", mono: true, align: "right" },
];
const vendorRows = [
  {
    c: "SmartConnect 4×4",
    lut: n(V.smc_4x4.lut),
    ff: n(V.smc_4x4.ff),
    b: "0",
    u: "0",
    f: "",
  },
  {
    c: "system_cache × 4, as the block design built them — <b>≈150 KB each, not 2 MB</b>",
    lut: n(V.syscache_default_x4.lut),
    ff: n(V.syscache_default_x4.ff),
    b: n(V.syscache_default_x4.bram),
    u: "0",
    f: "",
    _tone: "warn",
  },
  {
    c: "<b>vendor, composed, at its defaults</b>",
    lut: `<b>${n(V.composed_default.lut)}</b>`,
    ff: n(V.composed_default.ff),
    b: n(V.composed_default.bram),
    u: "0",
    f: "",
  },
  {
    c: "system_cache × 4 at a real 2 MB, data-memory type 2 — still block RAM",
    lut: n(4 * V.syscache_2mb.lut),
    ff: n(4 * V.syscache_2mb.ff),
    b: n(4 * V.syscache_2mb.bram),
    u: "0",
    f: `${V.syscache_2mb.fmax} at a 10 ns request`,
    _tone: "bad",
  },
  {
    c: "<b>vendor, composed, like-for-like memory</b>",
    lut: `<b>${n(V.composed_2mb.lut)}</b>`,
    ff: n(V.composed_2mb.ff),
    b: n(V.composed_2mb.bram),
    u: "0",
    f: "≤ 244",
    _tone: "bad",
  },
  {
    c: "<b>fused kx_mempath_e, no crossing</b> — 2 MB per home present",
    lut: `<b>${n(noCdc[6])}</b>`,
    ff: n(noCdc[7]),
    b: "0",
    u: n(noCdc[8]),
    f: `${noCdc[10].toFixed(0)}`,
    _tone: "good",
  },
  {
    c: "<b>fused kx_mempath_e, ship</b> — 4 DRAM-side crossings",
    lut: `<b>${n(ship[6])}</b>`,
    ff: n(ship[7]),
    b: n(ship[9]),
    u: n(ship[8]),
    f: `${ship[10].toFixed(0)}`,
    _tone: "good",
  },
];

const bars = [
  { label: "vendor: SmartConnect + 4 × system_cache at 2 MB", value: V.composed_2mb.lut, note: "Σ of standalone synths", tone: "bad" },
  { label: "vendor: SmartConnect + 4 × system_cache at its defaults (≈150 KB each)", value: V.composed_default.lut, note: "one block design", tone: "warn" },
  { label: "fused, ship — 4 DRAM-side crossings, 4 × 2 MB", value: ship[6], note: "one synthesis", tone: "good" },
  { label: "fused, no crossing, 4 × 2 MB", value: noCdc[6], note: "one synthesis", tone: "good" },
];

/* ----------------------------------------------------------- performance */
const perfCols = [
  { key: "w", label: "" },
  { key: "c", label: "cycles on clk", mono: true, align: "right" },
  { key: "g", label: "where they go" },
];
const perfRows = [
  { w: "read hit, first beat, URAM (RD_LAT=4)", c: "<b>9</b>", g: "ISSUE 1 + WAIT 5 + CHK 1 + DRAIN 1 + the index flop 1, AR accept to R valid" },
  { w: "read hit, first beat, BRAM (RD_LAT=1)", c: "6", g: "WAIT is 2" },
  { w: "each further beat of a read burst", c: "+9", g: "the engine walks a burst one IO word per round" },
  { w: "read miss", c: "hit + DRAM round trip + 2", g: "the fill is taken straight off R; the served word lands on the last beat" },
  { w: "write, W beats", c: "1 per cycle", g: "once AW is accepted; array and DRAM W take the same beat" },
  { w: "write, per burst", c: "AW + beats + B round trips", g: "one write outstanding per master" },
  { w: "each crossing on the path", c: "not measured", g: "one async_fifo traversal each way; no cycle figure is claimed", _tone: "warn" },
];

const catCols = [
  { key: "t", label: "Thing" },
  { key: "c", label: "Category" },
];
const catRows = [
  { t: "AXI4 at both edges — five channels, the handshake, the burst and 4 KB rules", c: "<b>fixed protocol</b>, and not ours" },
  { t: "home selection by <code>addr[HOME_LSB +: log2 N]</code>; the master index prepended to the DRAM ID", c: "<b>fixed protocol</b> within the system — a channel sees the whole address and an ID it must echo" },
  { t: "the per-port clock bits, and which clock <code>clk</code> is", c: "<b>yours</b>, per deployment; the clock table above says what each choice costs" },
  { t: "<code>M</code>, <code>N_HOME</code>, <code>K</code>, <code>RSAMD</code>, <code>WSAMD</code>, <code>SETS</code>, <code>W</code>", c: "<b>customizable</b> — every point of the grid is a target and the table prices each" },
  { t: "that the fabric is one clock and every select is a registered binary index", c: "<b>convention with teeth</b>: it is where the LUT figure comes from" },
  { t: "what a master does with the memory behind it", c: "<b>yours</b>" },
];
</script>

<template>
  <DocPage
    title="The fused crossbar-cache"
    summary="M AXI masters to N cached DRAM channels as one system: AXI only at the edges, one wide array per home, control-only engines, registered binary-index muxes, and a clock crossing only at a port that declares one."
    domain="framework"
    status="measured"
    :source="`src/kohakuaxi/ · docs/projects/kohakuaxi/xbar-cache.md · scripts/tcl/ooc_kx.tcl · ${PART} · ${TARGET_MHZ} MHz ask · synthesis only`"
  >
    <p class="doc-p">
      A vendor memory path is a crossbar IP in front of a cache IP in front of
      each DRAM controller. Each is an AXI endpoint, so the wide data crosses an
      AXI boundary twice on the way in and twice on the way out, and every
      boundary carries its own buffering, width and ID machinery — and, if the
      clocks differ, its own converters. <code>kx_mempath_e</code> speaks AXI at
      exactly two places, where a master attaches and where a DRAM channel
      attaches, and has nothing AXI-shaped between them.
    </p>

    <Callout kind="rule" title="Two systems, never one">
      <p>
        This is the second of KohakuAXI's two systems. The
        <RouterLink to="/framework/axi" class="doc-link">station bus</RouterLink>
        carries <i>host</i> traffic to endpoints of many widths and clocks across
        the dies; this carries <i>memory</i> traffic at one width into DRAM with
        a cache in the path. They share no module.
      </p>
    </Callout>

    <Fig
      caption="Three kinds of module, each carrying one kind of thing: the per-home array carries wide data and nothing else; the engines carry control and publish the INDEX of the home or master the fabric should select; the edges are a wire or an asynchronous FIFO, per port and per channel. The crossbar is two families of muxes inside kx_mempath_e on those registered indices — an N:1 per master on the read side, an M:1 per home on the write side."
      zoom
      wide
    >
      <BlockDiagram
        :nodes="fused.nodes"
        :edges="fused.edges"
        :groups="fused.groups"
      />
    </Fig>

    <h2 class="doc-h2">How a read is served</h2>
    <Fig
      caption="One request at a time per engine. The array's hit and served word are registered at the row-valid sample, so CHK reads a flop; on a miss the home's DRAM R channel fills the array directly and the engine sees only fill_done. DRAIN holds the response until the master's edge accepts the flop-delayed valid — that flop is what lets the N:1 pack as one LUT6 + MUXF7 per bit."
      zoom
    >
      <StateMachine :states="rdStates" :edges="rdEdges" />
    </Fig>

    <Callout kind="rule" title="The DRAM request is always one line">
      <p>
        <code>arlen = K − 1</code>, <code>arsize = W</code>, <code>INCR</code>,
        line-aligned, whatever the master asked for; the master's burst is
        walked one IO word per engine round. Writes go <b>through</b>: each W
        beat drives the home's DRAM W port and the array's write port on the
        same cycle, so the array is never dirty and a flush is a valid-clear,
        not a drain. At <code>K = 1</code> a full-strobe write allocates the row;
        at <code>K &gt; 1</code> a write invalidates it, because a line cannot be
        assembled from one IO word and merging into a URAM row is not a
        single-port write.
      </p>
    </Callout>

    <h2 class="doc-h2">The clock model: a crossing only where a port differs</h2>
    <p class="doc-p">
      The whole fabric — arrays, engines, crossbar — runs on one clock. Every
      port carries one bit saying whether it is on that clock. A port that is
      has five wires for an edge; a port that is not has five
      <code>async_fifo</code>s, the two wide ones in block RAM. Which clock the
      fabric runs on is the integrator's choice, made by setting the bits.
    </p>
    <SpecTable
      :cols="clkCols"
      :rows="clkRows"
      caption="The same RTL under five clock plans. A cross-SLR port has its own clock in this model whether or not the frequency is nominally equal, so the die boundary and the clock boundary are one edge, paid once"
    />

    <h2 class="doc-h2">Knobs</h2>
    <SpecTable
      :cols="knobCols"
      :rows="knobRows"
      caption="Every configuration is a target — the RTL is the same at every point of the grid, and the table below measures the grid rather than one point. The ship point is M=4, N=4, K=1, SAMD both sides, 64 URAM per home, four DRAM-side crossings"
    />

    <h2 class="doc-h2">What it costs — the whole table</h2>
    <SpecTable
      :cols="tblCols"
      :rows="tblRows"
      :caption="`W=512, AW=40, ID_W=4, 64 URAM per home (2 MB per home at K=1), one ooc_kx.tcl synthesis per row at a ${TARGET_MHZ} MHz ask, ${PART}. LUT and FF are the ENTIRE fused system — caches, engines, crossbar, edges. The amber row is the ship design with its wide crossing FIFOs in distributed RAM instead of block RAM`"
    />

    <Callout kind="measured" title="Fmax is flat across every M and N shape">
      <p>
        ~495 MHz at every <code>M</code>/<code>N</code> point at
        <code>K = 1</code>: a binary-index mux adds one LUT6 + MUXF7 level per
        doubling, so the crossbar's depth does not grow with port count.
        <code>K</code> is the only knob that moves timing, through the wider row
        and the sub-word select — 362 MHz at 2, 342 at 4, still past the ask.
      </p>
    </Callout>

    <h3 class="doc-h3">Per knob</h3>
    <SpecTable
      :cols="knobCostCols"
      :rows="knobCostRows"
      caption="Marginal costs read from adjacent rows of the table. SETS does not appear: the array is URAM and LUT is independent of the row count. W was held at 512 throughout"
    />

    <Callout
      kind="trap"
      title="Where the LUT figure comes from: the form of every wide select"
    >
      <p>
        A one-hot AND-OR over the homes' words, or a loop of
        <code>if (sel == j)</code> on a combinational select, builds LUT4/LUT5
        trees at <b>~8 LUT per data bit</b>. The same N:1 on a <b>registered
        binary</b> index packs as one LUT6 + MUXF7 per bit. Indexing a payload
        by <code>sel × PW</code> with a width that is not a power of two is a
        multiply and synthesises a barrel shifter. Registering the array's
        write-side inputs one cycle early keeps the fabric's M:1, the
        fill-or-word 2:1 and the strobe gating out of one cone. None of these
        changes what the design does; together they are the difference between
        the table above and several times it.
      </p>
    </Callout>

    <h2 class="doc-h2">Against the vendor path at the same shape</h2>
    <ResourceBars
      :items="bars"
      unit="CLB LUT sites"
      :caption="`4×4 at 512 bits, ${PART}, ${TARGET_MHZ} MHz ask, synthesis only. The vendor rows are a Σ of standalone synths as the station-bus page's vendor rows are; each fused row is one synthesis of one netlist`"
    />
    <SpecTable
      :cols="vendorCols"
      :rows="vendorRows"
      caption="Two vendor rows, both kept. The default row compares the crossbar and cache MACHINERY, because in that block design the vendor cache mapped 17 BRAM per instance at its default data-memory type — about 150 KB, not the 2 MB it was asked for. The 2 MB row is like-for-like on memory: the vendor cache then maps 561 BRAM per instance and tops out at 244 MHz"
    />

    <Callout kind="measured" title="At the real memory size">
      <p>
        <b>4.2× fewer LUT</b> with no crossings and 3.5× at the ship point with
        its four; the memory in URAM where 2,244 BRAM would be 83% of the part's
        2,688; and 300 MHz met where the vendor cache's 244 MHz Fmax cannot
        reach it. At the vendor's
        defaults — where its cache is a fraction of the size — the fused system
        is still 38% fewer LUT and 45% fewer FF with the full 2 MB per home
        present.
      </p>
    </Callout>

    <h2 class="doc-h2">Performance</h2>
    <p class="doc-p">
      Nothing here is a routed figure and no cycle counter was run against the
      RTL: the cycle counts are read off the state machines and are exact for
      the same-clock case.
    </p>
    <SpecTable :cols="perfCols" :rows="perfRows" />
    <Callout kind="note" title="What bounds a read-heavy master">
      <p>
        One outstanding request per master and one request per engine: with
        SAMD, every home proceeds in parallel, each delivering one IO word every
        9 cycles on a hit at URAM latency — 64 bytes per 9 cycles per home,
        <b>2.1 GB/s per home at 300 MHz on hits</b>, 8.5 GB/s over the ship's
        four. Writes stream at one beat per cycle per home inside a burst,
        19.2 GB/s per home. That read figure is the serial read engine — one
        array lookup per beat, not one per line — and it, not the crossbar or
        the array, is the ceiling on a read-heavy master.
      </p>
    </Callout>

    <h2 class="doc-h2">Verification</h2>
    <p class="doc-p">
      <code>tests/axi/kx_mempath_tb.v</code> drives the whole system between AXI
      masters and one <code>axi4_ram</code> per home, under Verilator and xsim,
      with the RTL's parameters exposed as bench defines — including
      <code>TB_TWOCLK</code>, every DRAM edge crossing at a 4.2 ns DRAM clock
      against a 3.334 ns fabric. Per configuration it drives single-beat and
      burst writes and reads through every master to every home against an
      address-derived pattern with <code>rlast</code> checked per beat, the same
      line read back through a <b>different</b> master, two masters to one home
      concurrently, partial-strobe writes, and at <code>K &gt; 1</code> the
      sub-word aliasing of one line. Configurations run: 4×4 at K 1/2/4, 2×4,
      8×4, 4×8, 8×8, each at SAMD and SASD on either and both sides, at 64 URAM
      per home, and the ship's two-clock case. Each of <code>kx_carray</code>,
      <code>kx_rd_engine</code>, <code>kx_wr_engine</code>, <code>kx_link</code>
      and <code>kx_scdc</code> has its own bench beneath it.
    </p>

    <h2 class="doc-h2">What it deliberately does not do</h2>
    <Callout kind="note" title="Not built, and not an omission">
      <p>
        <b>No associativity and no replacement policy</b> — direct-mapped, a
        conflicting line evicts on fill; the URAM budget goes to rows, not ways.
        <b>No writeback</b> — write-through only. <b>No read pipelining within
        an engine</b> — one request in flight per engine, one beat per array
        round, and the performance table is the consequence. <b>No exclusive
        access, cache or protection attributes</b>; every DRAM request is
        <code>INCR</code> at the line size, and <code>WRAP</code>/<code>FIXED</code>
        execute as <code>INCR</code>. <b>No coherence between homes</b> — an
        address belongs to exactly one. <b>No error recovery</b> and
        <b>no runtime observability</b>.
      </p>
    </Callout>

    <h2 class="doc-h2">Fixed protocol, addon, convention, or yours</h2>
    <SpecTable :cols="catCols" :rows="catRows" />

    <p class="doc-p">
      The per-knob model fitted to the table above, with its validation against
      every row — worst error 2.29% on LUT and 1.02% on FF — is the
      <RouterLink to="/framework/estimator" class="doc-link">resource estimator</RouterLink>.
    </p>
  </DocPage>
</template>
