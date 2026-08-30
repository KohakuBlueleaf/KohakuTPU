<script setup>
/**
 * /component/xache — Kohaku Xache (the Kohaku-Xache System, KX): the fused
 * crossbar-cache, kx_xache.
 *
 * Drawn from, in full:
 *   docs/projects/kohakuaxi/xbar-cache.md
 *   src/kohakuaxi/{kx_xache,kx_carray,kx_rd_engine,kx_rd_pipe,kx_wr_engine,kx_perm,kx_link,kx_scdc}.v
 *   scripts/tcl/ooc_kx.tcl · scripts/tcl/ooc_vendor_xc.tcl · scripts/tcl/ooc_syscache.tcl
 *
 * Every number is transcribed once, in src/content/estimator.js, and read from
 * there. One part, xcvu13p-fhgb2104-2L-e, out-of-context SYNTHESIS at a 300 MHz
 * ask — nothing is placed.
 */
import {
  PART,
  TARGET_MHZ,
  KX_ROWS,
  KX_ROWS_R1,
  KX_VENDOR,
  KX_PX,
} from "@/content/estimator";

const n = (v) => (v == null ? "—" : Math.round(v).toLocaleString());

/* ------------------------------------------------- the partitioned form */
const pxCols = [
  { key: "c", label: "" },
  { key: "p", label: "P", mono: true, align: "right" },
  { key: "lut", label: "LUT", mono: true, align: "right" },
  { key: "ff", label: "FF", mono: true, align: "right" },
  { key: "u", label: "URAM", mono: true, align: "right" },
  { key: "b", label: "BRAM", mono: true, align: "right" },
  { key: "w", label: "WNS ns", mono: true, align: "right" },
  { key: "f", label: "Fmax MHz", mono: true, align: "right" },
];
const pxRows = KX_PX.map((r, i) => ({
  c: r[0],
  p: r[1] == null ? "—" : `${r[1]}`,
  lut: i === 1 || i === 2 ? `<b>${n(r[2])}</b>` : n(r[2]),
  ff: n(r[3]),
  u: n(r[4]),
  b: `${r[5]}`,
  w: `+${r[6].toFixed(3)}`,
  f: r[7].toFixed(0),
  _tone: i === 1 || i === 2 ? "good" : i === 4 ? "warn" : undefined,
}));
/* kx_pxache_tb with TB_PERF beside kx_xache_tb: 4×4 K1, block-RAM arrays,
   4 KB interleave, 24-cycle DRAM, 64-beat bursts, 4 outstanding; cycles on
   the fabric clock */
const pxPerfCols = [
  { key: "s", label: "scenario" },
  { key: "x", label: "kx_xache", mono: true, align: "right" },
  { key: "a", label: "kx_pxache P=1", mono: true, align: "right" },
  { key: "b", label: "P=4, one per partition", mono: true, align: "right" },
];
const pxPerfRows = [
  {
    s: "1 master reads 64 KB, hits",
    x: "1,044 · 18.8 GB/s",
    a: "<b>1,038</b> · 18.9",
    b: "<b>1,036</b> · 19.0",
    _tone: "good",
  },
  {
    s: "4 masters read 16 KB each, hits",
    x: "465 · 42.3",
    a: "<b>467</b> · 42.1",
    b: "<b>473</b> · 41.6",
    _tone: "good",
  },
  {
    s: "4 masters read 16 KB each under one ID",
    x: "—",
    a: "467 · 42.1",
    b: "473 · 41.6",
  },
  {
    s: "1 master writes 64 KB",
    x: "1,104 · 17.8",
    a: "1,120 · 17.6",
    b: "1,264 · 15.6 (the bench waits for each remote B)",
  },
  {
    s: "4 masters write 16 KB each",
    x: "477 · 41.2",
    a: "481 · 40.9",
    b: "508 · 38.7",
  },
  {
    s: "32-beat hit, master 0 to the home in partition 0 / 1 / 2 / 3",
    x: "39",
    a: "39 / 39 / 39 / 39",
    b: "<b>39 / 45 / 51 / 57</b>",
  },
];

/* ---------------------------------------------------------------- structure */
/* Every node is a real module or a real family of muxes in kx_xache.v; every
   edge is a port group between them. Validated against the RTL: the read
   engine drives the array's lookup port and the fill address, the write
   engine drives the home's AW/W and the array's write port through the W mux,
   the served word returns through the R mux, and the DRAM R channel writes the
   array directly (fill_go). */
/* Four rows in flow order, the two AXI edges as full-width bars: the M masters
   and the N channels are what those bars ARE, so they are not drawn again
   above and below them. */
const fused = {
  groups: [
    {
      x: -1.5,
      y: -2,
      w: 72.5,
      h: 25.4,
      label: "kx_xache — one clock, clk; AXI only at the two edges",
    },
  ],
  nodes: [
    {
      id: "me",
      x: 0,
      y: 0,
      w: 69.5,
      h: 3.6,
      label: "master edges — M × AXI4 · 512b — kx_link × 5 per port",
      sub: "wires on clk · async FIFOs when MCDC[m] = 1 · the address permutation (kx_perm) sits here",
    },
    {
      id: "xw",
      x: 36,
      y: 6.2,
      w: 13.5,
      h: 3.4,
      label: "W mux, M:1 per home",
      sub: "on the write engine's registered master index",
      accent: true,
    },
    {
      id: "xr",
      x: 55,
      y: 6.2,
      w: 14.5,
      h: 3.4,
      label: "R mux, N:1 per master",
      sub: "on the read engine's registered home index",
      accent: true,
    },
    {
      id: "we",
      x: 0,
      y: 12.6,
      w: 12,
      h: 3.8,
      label: "write engines",
      sub: "kx_wr_engine · control only · one per home or one for all",
      accent: true,
    },
    {
      id: "re",
      x: 14,
      y: 12.6,
      w: 15,
      h: 3.8,
      label: "read engines",
      sub: "kx_rd_engine or kx_rd_pipe · control only · one per home or one for all",
      accent: true,
    },
    {
      id: "c0",
      x: 36,
      y: 12.6,
      w: 9,
      h: 3.8,
      label: "kx_carray 0",
      sub: "64 URAM · {v, tag, K×W} · served word",
    },
    {
      id: "c1",
      x: 46.5,
      y: 12.6,
      w: 9,
      h: 3.8,
      label: "kx_carray 1",
      sub: "the only wide store",
    },
    {
      id: "cx",
      x: 60.5,
      y: 12.6,
      w: 9,
      h: 3.8,
      label: "kx_carray N−1",
      sub: "one per home",
    },
    {
      id: "he",
      x: 0,
      y: 19.4,
      w: 69.5,
      h: 3.6,
      label: "DRAM edges — N × AXI4 · 512b — kx_link × 5 per home",
      sub: "wires on clk · async FIFOs when HCDC[h] = 1, the W and R ones in block RAM",
    },
  ],
  edges: [
    { from: "me:b", to: "we:t", label: "AW · W", dir: "v" },
    { from: "me:b", to: "re:t", label: "AR", dir: "v" },
    { from: "xr:t", to: "me:b", label: "R data", dir: "v", accent: true },
    { from: "me:b", to: "xw:t", label: "W data", dir: "v", accent: true },
    { from: "xw:b", to: "c0:t", label: "write word", dir: "v", accent: true },
    { from: "xw:b", to: "c1:t", dir: "v" },
    { from: "c1:t", to: "xr:b", dir: "v" },
    { from: "cx:t", to: "xr:b", label: "served word", dir: "v", accent: true },
    {
      from: "re:r",
      to: "c0:l",
      label: "lookup idx · tag",
      dir: "h",
    },
    {
      from: "re:b",
      to: "he:t",
      label: "AR: one line, or rest of the burst",
      dir: "v",
    },
    { from: "we:b", to: "he:t", label: "AW · W, write-through", dir: "v" },
    {
      from: "he:t",
      to: "c0:b",
      label: "R fills the line",
      dash: true,
      dir: "v",
    },
  ],
};

/* --------------------------------------------------------------- read engine */
const rdStates = [
  { id: "idle", x: 0, y: 0, label: "IDLE", sub: "round robin" },
  { id: "issue", x: 6, y: 0, label: "ISSUE", sub: "rd_en" },
  { id: "wait", x: 12, y: 0, label: "WAIT", sub: "RD_LAT+1" },
  { id: "chk", x: 18, y: 0, label: "CHK", sub: "hit_q?" },
  {
    id: "fetch",
    x: 24,
    y: 4,
    label: "FETCH",
    sub: "AR → R fills",
    accent: true,
  },
  {
    id: "drain",
    x: 24,
    y: -4,
    label: "DRAIN",
    sub: "hold r_val",
    accent: true,
  },
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
  {
    p: "W",
    v: "512",
    m: "IO width, <b>shared</b> by every port and the array word",
  },
  {
    p: "K",
    v: "1, 2, 4",
    m: "line width in IO words. K=1 allocates on a full-strobe write; K&gt;1 invalidates and fills from the channel's burst",
  },
  {
    p: "RSAMD",
    v: "0, 1",
    m: "1: one read engine per home, every home served in parallel. 0: one engine for all homes",
  },
  {
    p: "WSAMD",
    v: "0, 1",
    m: "the same on the write side, <b>independently</b>",
  },
  {
    p: "MCDC[m] · HCDC[h]",
    v: "none · 1111",
    m: "per port: 1 = this port is on its own clock and crosses at its edge; 0 = it is on <code>clk</code> and its edge is five wires",
    _tone: "good",
  },
  {
    p: "NSWAP · SWAP_A · SWAP_B",
    v: "0 · 20 pairs (i, i+2), i = 12..31",
    m: "address-bit swaps at the master edge: rotating a low field into the home field is channel interleaving at that field's granularity, for <b>0 LUT</b>. Every swapped bit must be ≥ max(LINE_LSB, 12), enforced at elaboration",
    _tone: "good",
  },
  {
    p: "SETS · SET_W",
    v: "32768 · 15",
    m: "rows per home: 2 MB per home at K=1, <b>64 URAM</b>",
  },
  {
    p: "RAM_STYLE",
    v: "ultra",
    m: "the array primitive; <code>block</code> shortens RD_LAT from 4 to 1",
  },
  {
    p: "CDC_DEPTH",
    v: "16",
    m: "per-channel crossing FIFO depth — XPM's minimum",
  },
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
const rowsOf = (rows) =>
  rows.map((r) => ({
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
/* the current array: the one-beat engine and the streaming engine (RD_OUTQ 4) */
const tblRowsP1 = rowsOf(KX_ROWS[1]);
const tblRowsR2 = rowsOf(KX_ROWS[0]);
/* the first array revision, kept as measured, with the two rows the estimator
   does not model: the ship with the 4 KB channel interleave (identical to the
   digit — wires), and the ship with its W/R crossing FIFOs in distributed RAM */
const tblRows = rowsOf(KX_ROWS_R1);
tblRows.splice(tblRows.findIndex((r) => r.c === "4") + 1, 0, {
  m: "4",
  nh: "4",
  k: "1",
  r: "SAMD",
  w: "SAMD",
  c: "4 + interleave 4 KB, rotation (20 pairs)",
  lut: "<b>11,865</b>",
  ff: "10,788",
  u: "256",
  b: "64",
  f: "456",
  _tone: "good",
});
tblRows.splice(
  tblRows.findIndex((r) => r.c.startsWith("4 + interleave")) + 1,
  0,
  {
    m: "4",
    nh: "4",
    k: "1",
    r: "SAMD",
    w: "SAMD",
    c: "4 + interleave 4 KB, plain field swap (idles ¾ of the sets)",
    lut: "11,865",
    ff: "10,788",
    u: "256",
    b: "64",
    f: "456",
  },
);
tblRows.splice(
  tblRows.findIndex((r) => r.c.startsWith("4 + interleave 4 KB, plain")) + 1,
  0,
  {
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
  },
);

const knobCostCols = [
  { key: "k", label: "knob", mono: true },
  { key: "ft", label: "from → to", mono: true },
  { key: "l", label: "ΔLUT", mono: true, align: "right" },
  { key: "f", label: "ΔFF", mono: true, align: "right" },
  { key: "u", label: "ΔURAM", mono: true, align: "right" },
  { key: "w", label: "where it goes" },
];
const knobCostRows = [
  {
    k: "M",
    ft: "2 → 4",
    l: "+3,677",
    f: "+80",
    u: "0",
    w: "+1,839 per master at N=4",
  },
  {
    k: "M",
    ft: "4 → 8",
    l: "+5,218",
    f: "+118",
    u: "0",
    w: "+1,305 per master: a fixed per-master part plus an M·N crossbar part",
  },
  {
    k: "N_HOME",
    ft: "4 → 8",
    l: "+8,305",
    f: "+7,388",
    u: "+256",
    w: "≈ +2,076 per home: array 815, engines 257, crossbar leg ≈ 1,000",
  },
  {
    k: "M × N",
    ft: "4×4 → 8×8",
    l: "+4,757 over ΔM+ΔN",
    f: "",
    u: "",
    w: "the crossbar scales as M·N, not M+N",
    _tone: "warn",
  },
  {
    k: "K",
    ft: "1 → 2",
    l: "+4,553",
    f: "+6,162",
    u: "+224",
    w: "per-home line buffer and the wider row",
  },
  {
    k: "K",
    ft: "2 → 4",
    l: "+8,380",
    f: "+8,136",
    u: "+448",
    w: "≈ +4.2k per extra IO word of line, linear",
  },
  {
    k: "read SASD",
    ft: "SAMD → SASD",
    l: "−371",
    f: "−191",
    u: "0",
    w: "the per-home read engine is 140 LUT; sharing saves control only. −41 MHz",
  },
  {
    k: "write SASD",
    ft: "SAMD → SASD",
    l: "−2,220",
    f: "−373",
    u: "0",
    w: "collapses N write paths and each path's M:1 fan-in. −19 MHz",
  },
  {
    k: "both SASD",
    ft: "4×4",
    l: "−2,564",
    f: "−560",
    u: "0",
    w: "additive: −371 − 2,220 = −2,591 predicted",
  },
  {
    k: "both SASD",
    ft: "8×8",
    l: "−10,476",
    f: "",
    u: "",
    w: "the saving grows with M·N, not as a constant",
  },
  {
    k: "crossing, W/R in BRAM",
    ft: "per port",
    l: "<b>+488</b>",
    f: "+850",
    u: "+16 BRAM",
    w: "five async FIFOs; the two wide ones in RAMB36 at 1/32 occupancy",
    _tone: "good",
  },
  {
    k: "crossing, W/R in LUTRAM",
    ft: "per port",
    l: "+1,117",
    f: "+3,043",
    u: "0",
    w: "the same five FIFOs in distributed RAM",
    _tone: "warn",
  },
  {
    k: "channel interleave, 4 KB",
    ft: "NSWAP 0 → 20 (rotation), 0 → 2 (swap)",
    l: "<b>0</b>",
    f: "0",
    u: "0",
    w: "wires: LUT, FF, WNS and Fmax identical to the digit at the ship point, in both forms",
    _tone: "good",
  },
];
/* the streaming engine's per-knob costs, from adjacent rows of its table
   (RD_PIPE=1, RD_OUTQ=4, current array); the last column is the one-beat
   engine on the same array */
const knobCostColsP1 = [
  { key: "k", label: "knob", mono: true },
  { key: "ft", label: "from → to", mono: true },
  { key: "l", label: "ΔLUT", mono: true, align: "right" },
  { key: "f", label: "ΔFF", mono: true, align: "right" },
  { key: "u", label: "ΔURAM", mono: true, align: "right" },
  { key: "o", label: "one-beat ΔLUT", mono: true, align: "right" },
];
const knobCostRowsP1 = [
  { k: "M", ft: "2 → 4", l: "+3,098", f: "+134", u: "0", o: "+3,121" },
  { k: "M", ft: "4 → 8", l: "+5,338", f: "+184", u: "0", o: "+5,834" },
  {
    k: "N_HOME",
    ft: "4 → 8",
    l: "+7,210",
    f: "+7,708",
    u: "+256",
    o: "+8,584",
  },
  {
    k: "M × N",
    ft: "4×4 → 8×8",
    l: "+4,901 over ΔM+ΔN",
    f: "",
    u: "",
    o: "+3,128",
    _tone: "warn",
  },
  { k: "K", ft: "1 → 2", l: "+2,042", f: "+4,048", u: "+224", o: "+4,119" },
  { k: "K", ft: "2 → 4", l: "+5,124", f: "+8,180", u: "+448", o: "+8,896" },
  {
    k: "read SASD",
    ft: "SAMD → SASD",
    l: "<b>−2,838</b>",
    f: "−550",
    u: "0",
    o: "+130",
    _tone: "good",
  },
  {
    k: "write SASD",
    ft: "SAMD → SASD",
    l: "−1,678",
    f: "−379",
    u: "0",
    o: "−1,652",
  },
  { k: "both SASD", ft: "4×4", l: "−4,473", f: "−929", u: "0", o: "−2,052" },
  { k: "both SASD", ft: "8×8", l: "−18,832", f: "", u: "", o: "−10,628" },
  {
    k: "K 1 → 2 under both SASD",
    ft: "4×4 / 8×8",
    l: "+1,690 / +4,106",
    f: "",
    u: "",
    o: "+3,790 / +8,257",
    _tone: "warn",
  },
  {
    k: "crossing, W/R in BRAM",
    ft: "per port",
    l: "+451",
    f: "+855",
    u: "+16 BRAM",
    o: "+479",
  },
  {
    k: "RD_OUTQ",
    ft: "1 → 8",
    l: "+71",
    f: "+84",
    u: "0",
    o: "—",
    _tone: "good",
  },
];

/* ------------------------------------------------------------- vendor */
const V = KX_VENDOR;
/* the fused rows beside the vendor: the shipped RTL (streaming engine) */
const ship = KX_ROWS[1].find((r) => isShip(r) && r[5] === 4);
const noCdc = KX_ROWS[1].find((r) => isShip(r) && r[5] === 0);
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
    c: "system_cache × 4 at a real 2 MB, data memory in block RAM (type 2)",
    lut: n(4 * V.syscache_2mb.lut),
    ff: n(4 * V.syscache_2mb.ff),
    b: n(4 * V.syscache_2mb.bram),
    u: "0",
    f: `${V.syscache_2mb.fmax} at a 10 ns request`,
    _tone: "warn",
  },
  {
    c: "vendor, composed, 2 MB in block RAM",
    lut: n(V.composed_2mb.lut),
    ff: n(V.composed_2mb.ff),
    b: n(V.composed_2mb.bram),
    u: "0",
    f: "≤ 244",
    _tone: "warn",
  },
  {
    c: "system_cache × 4 at a real 2 MB, data memory in URAM (type 3) — 64 URAM each, as a Xache home",
    lut: n(4 * V.syscache_2mb_uram.lut),
    ff: n(4 * V.syscache_2mb_uram.ff),
    b: n(4 * V.syscache_2mb_uram.bram),
    u: n(4 * V.syscache_2mb_uram.uram),
    f: `${V.syscache_2mb_uram.fmax}, the same path`,
    _tone: "bad",
  },
  {
    c: "system_cache × 4 at 2 MB, data and tags in URAM",
    lut: n(4 * V.syscache_2mb_uram_tag.lut),
    ff: n(4 * V.syscache_2mb_uram_tag.ff),
    b: n(4 * V.syscache_2mb_uram_tag.bram),
    u: n(4 * V.syscache_2mb_uram_tag.uram),
    f: `${V.syscache_2mb_uram_tag.fmax} — the tag lookup through URAM`,
    _tone: "warn",
  },
  {
    c: "<b>vendor, composed, like-for-like memory</b> — 2 MB in URAM",
    lut: `<b>${n(V.composed_2mb_uram.lut)}</b>`,
    ff: n(V.composed_2mb_uram.ff),
    b: n(V.composed_2mb_uram.bram),
    u: n(V.composed_2mb_uram.uram),
    f: "≤ 244",
    _tone: "bad",
  },
  {
    c: "<b>fused kx_xache, no crossing</b> — 2 MB per home present",
    lut: `<b>${n(noCdc[6])}</b>`,
    ff: n(noCdc[7]),
    b: "0",
    u: n(noCdc[8]),
    f: `${noCdc[10].toFixed(0)}`,
    _tone: "good",
  },
  {
    c: "<b>fused kx_xache, ship</b> — 4 DRAM-side crossings",
    lut: `<b>${n(ship[6])}</b>`,
    ff: n(ship[7]),
    b: n(ship[9]),
    u: n(ship[8]),
    f: `${ship[10].toFixed(0)}`,
    _tone: "good",
  },
];

const bars = [
  {
    label: "vendor: SmartConnect + 4 × system_cache at 2 MB in block RAM",
    value: V.composed_2mb.lut,
    note: "Σ of standalone synths",
    tone: "warn",
  },
  {
    label:
      "vendor: SmartConnect + 4 × system_cache at 2 MB in URAM — like-for-like",
    value: V.composed_2mb_uram.lut,
    note: "Σ of standalone synths",
    tone: "bad",
  },
  {
    label:
      "vendor: SmartConnect + 4 × system_cache at its defaults (≈150 KB each)",
    value: V.composed_default.lut,
    note: "one block design",
    tone: "warn",
  },
  {
    label: "fused, ship — 4 DRAM-side crossings, 4 × 2 MB",
    value: ship[6],
    note: "one synthesis",
    tone: "good",
  },
  {
    label: "fused, no crossing, 4 × 2 MB",
    value: noCdc[6],
    note: "one synthesis",
    tone: "good",
  },
];

/* ----------------------------------------------------------- performance */
const perfCols = [
  { key: "w", label: "" },
  { key: "c", label: "cycles on clk", mono: true, align: "right" },
  { key: "g", label: "where they go" },
];
const perfRows = [
  {
    w: "read hit, first beat, URAM (RD_LAT=4)",
    c: "<b>9</b>",
    g: "ISSUE 1 + WAIT 5 + CHK 1 + DRAIN 1 + the index flop 1, AR accept to R valid",
  },
  { w: "read hit, first beat, BRAM (RD_LAT=1)", c: "6", g: "WAIT is 2" },
  {
    w: "each further beat of a read burst",
    c: "+9",
    g: "the engine walks a burst one IO word per round",
  },
  {
    w: "read miss",
    c: "hit + DRAM round trip + 2",
    g: "the fill is taken straight off R; the served word lands on the last beat",
  },
  {
    w: "write, W beats",
    c: "1 per cycle",
    g: "once AW is accepted; array and DRAM W take the same beat",
  },
  {
    w: "write, per burst",
    c: "AW + beats + B round trips",
    g: "one write outstanding per master",
  },
  {
    w: "each crossing on the path",
    c: "not measured",
    g: "one async_fifo traversal each way; no cycle figure is claimed",
    _tone: "warn",
  },
];

/* kx_xache_tb with TB_PERF: 4x4 K1 SAMD, block-RAM arrays (RD_LAT=1), 64 lines
   per home, axi4_ram behind every home, one clock, 64-beat bursts, one
   outstanding per master. Cycles on the fabric clock; GB/s at 300 MHz. */
const streamCols = [
  { key: "s", label: "scenario" },
  { key: "a", label: "contiguous map", mono: true, align: "right" },
  { key: "b", label: "4 KB interleave", mono: true, align: "right" },
  { key: "h", label: "DRAM requests per home", mono: true },
];
const streamRows = [
  {
    s: "1 master writes 64 KB",
    a: "1,104 cyc · <b>17.8 GB/s</b>",
    b: "1,104 · 17.8",
    h: "16/0/0/0 → 4/4/4/4",
  },
  {
    s: "1 master reads 64 KB, misses",
    a: "9,248 cyc · <b>2.13 GB/s</b>",
    b: "9,248 · 2.13",
    h: "1024/0/0/0 → 256 each",
  },
  {
    s: "1 master reads 2 KB, misses",
    a: "290 cyc · 9.06 / beat",
    b: "same",
    h: "",
  },
  {
    s: "1 master reads 2 KB, hits",
    a: "194 cyc · <b>6.06 / beat</b>",
    b: "same",
    h: "",
  },
  {
    s: "4 masters write 16 KB each",
    a: "1,074 cyc · 18.3 GB/s",
    b: "<b>477 · 41.2 GB/s</b>",
    h: "16/0/0/0 → 4/4/4/4",
    _tone: "good",
  },
  {
    s: "4 masters read 16 KB each, misses",
    a: "9,233 cyc · 2.13 GB/s",
    b: "<b>4,043 · 4.86 GB/s</b>",
    h: "1024/0/0/0 → 256 each",
    _tone: "good",
  },
];

/* the granularity sweep: same four-master scenario, 16 KB of cache per home
   (64 KB total = the working set); DRAM reads per home over the pass */
const granCols = [
  { key: "g", label: "granularity", mono: true },
  { key: "w", label: "4 masters write", mono: true, align: "right" },
  { key: "r", label: "4 masters read", mono: true, align: "right" },
  { key: "d", label: "DRAM reads per home", mono: true },
  { key: "o", label: "1 master reads 64 KB", mono: true, align: "right" },
];
const granRows = [
  {
    g: "contiguous",
    w: "1,074 cyc · 18.3 GB/s",
    r: "9,233 · 2.13",
    d: "1024/0/0/0",
    o: "9,248 · 2.13 · misses",
  },
  {
    g: "4 KB",
    w: "477 · 41.2",
    r: "2,699 · 7.29",
    d: "0/0/0/0",
    o: "6,176 · 3.18 · all hits",
  },
  {
    g: "8 KB",
    w: "473 · 41.6",
    r: "2,697 · 7.29",
    d: "0/0/0/0",
    o: "6,176 · 3.18 · all hits",
  },
  {
    g: "16 KB",
    w: "<b>276 · 71.2</b>",
    r: "<b>1,544 · 12.7</b>",
    d: "0/0/0/0",
    o: "6,176 · 3.18 · all hits",
    _tone: "good",
  },
  {
    g: "32 KB",
    w: "538 · 36.6",
    r: "4,617 · 4.26",
    d: "512/0/512/0",
    o: "9,248 · 2.13 · misses",
    _tone: "warn",
  },
  {
    g: "64 KB",
    w: "1,074 · 18.3",
    r: "9,233 · 2.13",
    d: "1024/0/0/0",
    o: "9,248 · 2.13 · misses",
    _tone: "bad",
  },
];

/* ---- the streaming read engine (RD_PIPE=1) and the read queue (RD_OUTQ) ---- */
/* The chain sits on y = 0 and the three outcomes return to issue on arcs: take
   above (+), drop below (−), and fetch over the top of take (+330 clears its
   circle by 20 px). Circles grow to their subs, hence the 8- and 10-unit
   pitch. */
const rdpStates = [
  { id: "idle", x: 0, y: 0, label: "IDLE", sub: "round robin" },
  {
    id: "issue",
    x: 8,
    y: 0,
    label: "issue",
    sub: "a lookup per cycle",
    accent: true,
  },
  { id: "land", x: 16, y: 0, label: "landing", sub: "RD_LAT later" },
  {
    id: "take",
    x: 26,
    y: -4,
    label: "take",
    sub: "needed & hit & room",
    accent: true,
  },
  { id: "drop", x: 26, y: 4, label: "drop", sub: "stalled / not my turn" },
  {
    id: "miss",
    x: 36,
    y: 0,
    label: "fetch",
    sub: "one AR: rest of burst",
    accent: true,
  },
];
const rdpEdges = [
  { from: "idle", to: "issue", label: "burst" },
  { from: "issue", to: "land" },
  { from: "land", to: "take", label: "beat == next" },
  { from: "land", to: "drop", label: "else" },
  // A leftward edge bulges UP for a positive curve: take (above) is +, drop
  // (below) is −, and fetch's return goes over the top of take.
  { from: "drop", to: "issue", label: "restart at need", curve: -100 },
  { from: "take", to: "issue", label: "r_val, +1", curve: 100 },
  { from: "land", to: "miss", label: "needed & miss" },
  { from: "miss", to: "issue", label: "fills → fill_lim", curve: 330 },
];

const rdqKnobCols = [
  { key: "p", label: "parameter", mono: true },
  { key: "v", label: "measured at", mono: true },
  { key: "m", label: "meaning" },
];
const rdqKnobRows = [
  {
    p: "RD_PIPE",
    v: "0, 1",
    m: "0: the one-beat engine — one lookup, a full round per beat, one DRAM line per miss. 1: the streaming engine — a lookup per cycle, a miss fetches the rest of the burst, responses ordered per master",
    _tone: "good",
  },
  {
    p: "RD_OUTQ",
    v: "1, 2, 4, 8",
    m: "read bursts a master may have accepted at once across the homes; above 1 requires RD_PIPE=1 (refused at elaboration)",
  },
];

/* kx_xache_tb with TB_PERF, block-RAM arrays, one clock, 64-beat bursts,
   axi4_ram RD_LAT_CYC=24; GB/s at 300 MHz */
const oneHomeCols = [
  { key: "e", label: "engine" },
  { key: "a", label: "64 KB read, misses", mono: true, align: "right" },
  { key: "b", label: "2 KB, misses", mono: true, align: "right" },
  { key: "c", label: "2 KB, hits", mono: true, align: "right" },
];
const oneHomeRows = [
  {
    e: "one-beat (RD_PIPE=0)",
    a: "33,809 cyc · <b>0.58 GB/s</b>",
    b: "1,058 · 33 / beat",
    c: "194 · 6.06 / beat",
    _tone: "bad",
  },
  {
    e: "streaming, RD_OUTQ=1",
    a: "1,553 · <b>12.7 GB/s</b>",
    b: "66 · 2.06 / beat",
    c: "37 · <b>1.16 / beat · 16.6 GB/s</b>",
    _tone: "good",
  },
  {
    e: "streaming, RD_OUTQ=4",
    a: "1,538 · 12.8",
    b: "66",
    c: "37",
    _tone: "good",
  },
];
const queueCols = [
  { key: "q", label: "RD_OUTQ", mono: true },
  { key: "c", label: "cycles", mono: true, align: "right" },
  { key: "g", label: "GB/s", mono: true, align: "right" },
  { key: "h", label: "DRAM reads per home", mono: true },
];
const queueRows = [
  { q: "1", c: "1,553", g: "12.7", h: "4/4/4/4" },
  { q: "2", c: "1,080", g: "18.2", h: "4/4/4/4", _tone: "good" },
  { q: "4", c: "1,076", g: "<b>18.3</b>", h: "4/4/4/4", _tone: "good" },
  { q: "8 · eight homes", c: "1,074", g: "18.3", h: "2 each", _tone: "good" },
  {
    q: "4 · DRAM latency 60",
    c: "1,112",
    g: "17.7",
    h: "4/4/4/4",
    _tone: "good",
  },
  {
    q: "4 · 16 KB of cache, all hits",
    c: "1,044",
    g: "<b>18.8</b>",
    h: "0 (one-beat: 6,161 · 3.19)",
    _tone: "good",
  },
];
const fourCols = [
  { key: "s", label: "four masters, 16 KB each" },
  { key: "a", label: "one-beat engine", mono: true, align: "right" },
  { key: "b", label: "streaming, RD_OUTQ=4", mono: true, align: "right" },
];
const fourRows = [
  {
    s: "4 KB interleave, hits",
    a: "2,696 cyc · 7.29 GB/s",
    b: "465 · <b>42.3 GB/s</b>",
  },
  {
    s: "16 KB interleave, hits",
    a: "1,541 · 12.8",
    b: "270 · <b>72.8 GB/s</b>",
    _tone: "good",
  },
  {
    s: "4 KB, all misses (4 KB of cache)",
    a: "14,792 · 1.33",
    b: "581 · <b>33.8 GB/s</b>",
  },
  { s: "4 KB, all misses, DRAM latency 60", a: "", b: "725 · 27.1" },
  { s: "4 KB, all misses, eight homes, RD_OUTQ=8", a: "", b: "389 · 50.6" },
];
/* every measured shape, both engines, on the shipped RTL: K=1 SAMD, no
   crossing, 24-cycle DRAM, 16 KB cache/home (misses: 4 KB), M masters 16 KB
   each; GB/s one-beat → streaming RD_OUTQ=4 at 4 KB / 16 KB interleave */
const shapeCols = [
  { key: "s", label: "M × N", mono: true },
  { key: "l", label: "LUT one-beat → streaming", mono: true, align: "right" },
  { key: "w", label: "write 4 KB / 16 KB", mono: true, align: "right" },
  { key: "h", label: "read hits 4 KB / 16 KB", mono: true, align: "right" },
  { key: "m", label: "read misses 4 KB / 16 KB", mono: true, align: "right" },
  { key: "c", label: "ceiling", mono: true, align: "right" },
];
const shapeRows = [
  {
    s: "2 × 4",
    l: "5,287 → 4,741 (−546)",
    w: "28.7 / 35.6",
    h: "5.10 → <b>29.7</b> / 6.38 → <b>36.4</b>",
    m: "0.93 → 25.3 / 1.16 → 25.5",
    c: "38.4 (2 ports)",
  },
  {
    s: "4 × 4",
    l: "8,408 → 7,839 (−569)",
    w: "41.2 / 71.2",
    h: "7.29 → <b>42.3</b> / 12.8 → <b>72.8</b>",
    m: "1.33 → 33.8 / 2.33 → 50.9",
    c: "76.8",
    _tone: "good",
  },
  {
    s: "8 × 4",
    l: "14,242 → 13,177 (−1,065)",
    w: "52.8 / 73.1",
    h: "128 KB > 64 KB of cache: all miss",
    m: "1.69 → <b>40.8</b> / 2.33 → <b>51.1</b>",
    c: "76.8 (4 channels)",
    _tone: "warn",
  },
  {
    s: "4 × 8",
    l: "16,992 → 15,049 (−1,943)",
    w: "57.3 / 71.2",
    h: "10.2 → <b>59.4</b> / 12.8 → <b>72.8</b>",
    m: "1.86 → 50.6 / 2.33 → 50.9",
    c: "76.8 (4 ports)",
  },
  {
    s: "8 × 8",
    l: "26,370 → 25,288 (−1,082)",
    w: "82.4 / 142.5",
    h: "14.6 → <b>84.6</b> / 25.5 → <b>145.7</b>",
    m: "2.66 → 67.7 / 4.65 → 101.9",
    c: "153.6",
    _tone: "good",
  },
];
const magCols = [
  { key: "q", label: "RD_OUT", mono: true },
  { key: "l", label: "LUT", mono: true, align: "right" },
  { key: "f", label: "FF", mono: true, align: "right" },
  { key: "b", label: "BRAM", mono: true, align: "right" },
  { key: "x", label: "Fmax", mono: true, align: "right" },
  { key: "s", label: "20-word bursts", mono: true, align: "right" },
  { key: "t", label: "256-word bursts", mono: true, align: "right" },
];
const magRows = [
  {
    q: "1",
    l: "2,115",
    f: "1,894",
    b: "16",
    x: "384",
    s: "2,744 MB/s",
    t: "8,034",
  },
  { q: "2", l: "2,127", f: "1,904", b: "16", x: "385", s: "", t: "" },
  {
    q: "4",
    l: "2,244",
    f: "2,104",
    b: "16",
    x: "385",
    s: "<b>8,917 MB/s</b>",
    t: "9,375",
    _tone: "good",
  },
];

const catCols = [
  { key: "t", label: "Thing" },
  { key: "c", label: "Category" },
];
const catRows = [
  {
    t: "AXI4 at both edges — five channels, the handshake, the burst and 4 KB rules",
    c: "<b>fixed protocol</b>, and not ours",
  },
  {
    t: "home selection by <code>addr[HOME_LSB +: log2 N]</code>; the master index prepended to the DRAM ID",
    c: "<b>fixed protocol</b> within the system — a channel sees the whole address and an ID it must echo",
  },
  {
    t: "the per-port clock bits, and which clock <code>clk</code> is",
    c: "<b>yours</b>, per deployment; the clock table above says what each choice costs",
  },
  {
    t: "<code>M</code>, <code>N_HOME</code>, <code>K</code>, <code>RSAMD</code>, <code>WSAMD</code>, <code>SETS</code>, <code>W</code>",
    c: "<b>customizable</b> — every point of the grid is a target and the table prices each",
  },
  {
    t: "that the fabric is one clock and every select is a registered binary index",
    c: "<b>convention with teeth</b>: it is where the LUT figure comes from",
  },
  { t: "what a master does with the memory behind it", c: "<b>yours</b>" },
];
</script>

<template>
  <DocPage
    title="Kohaku Xache"
    summary="The Kohaku-Xache System (KX, Xache = crossbar-cache): M AXI masters to N cached DRAM channels as one fabric — AXI only at the two edges, one tagged wide array fused per channel, control-only engines, a streaming read engine with a per-master read queue, channel interleaving as wires, and a clock crossing only at a port that declares one."
    domain="framework"
    status="measured"
    :source="`src/kohakuaxi/ · docs/projects/kohakuaxi/xbar-cache.md · scripts/tcl/ooc_kx.tcl · ${PART} · ${TARGET_MHZ} MHz ask · synthesis only`"
  >
    <p class="doc-p">
      A vendor memory path is a crossbar IP in front of a cache IP in front of
      each DRAM controller. Each is an AXI endpoint, so the wide data crosses an
      AXI boundary twice on the way in and twice on the way out, and every
      boundary carries its own buffering, width and ID machinery — and, if the
      clocks differ, its own converters. <code>kx_xache</code> speaks AXI at
      exactly two places, where a master attaches and where a DRAM channel
      attaches, and has nothing AXI-shaped between them. <b>KX</b> is the family
      prefix (Kohaku-Xache System), <b>Xache</b> is the fabric, and every module
      of it is <code>kx_*</code>.
    </p>

    <Callout kind="rule" title="One of two AXI systems, never conflated">
      <p>
        Kohaku Xache carries the meshes' <i>memory</i> traffic at one width into
        DRAM with a cache in the path. The other framework component that speaks
        AXI, the
        <RouterLink to="/component/station-bus" class="doc-link"
          >station bus</RouterLink
        >, carries the <i>host's</i> control traffic to endpoints of many widths
        and clocks across the dies. They share no module;
        <RouterLink to="/framework/axi" class="doc-link"
          >AXI in this machine</RouterLink
        >
        says which kind of AXI goes where.
      </p>
    </Callout>

    <Fig
      caption="Three kinds of module, each carrying one kind of thing: the per-home array carries wide data and nothing else; the engines carry control and publish the INDEX of the home or master the fabric should select; the edges are a wire or an asynchronous FIFO, per port and per channel. The crossbar is two families of muxes on those registered indices — an M:1 per home on the write side, an N:1 per master on the read side — written inside kx_xache rather than as a module of their own, because a mux on a registered binary index packs as one LUT6 + MUXF7 per bit and a module boundary would only hide that."
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

    <Callout
      kind="rule"
      title="The one-beat engine's DRAM request is always one line"
    >
      <p>
        <code>arlen = K − 1</code>, <code>arsize = W</code>, <code>INCR</code>,
        line-aligned, whatever the master asked for; the master's burst is
        walked one IO word per engine round. (The streaming engine below fetches
        the rest of the burst instead.) Writes go <b>through</b>: each W beat
        drives the home's DRAM W port and the array's write port on the same
        cycle, so the array is never dirty and a flush is a valid-clear, not a
        drain. At <code>K = 1</code> a full-strobe write allocates the row; at
        <code>K &gt; 1</code> a write invalidates it, because a line cannot be
        assembled from one IO word and merging into a URAM row is not a
        single-port write.
      </p>
    </Callout>

    <h2 class="doc-h2">
      The clock model: a crossing only where a port differs
    </h2>
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

    <h3 class="doc-h3">One partition, and the partitioned form</h3>
    <p class="doc-p">
      The Xache is a single-partition fabric: everything in it is placed in one
      region of the part, and no register-to-register path inside it crosses a
      die. Every number on this page is that fabric's. Its partitioned form,
      <code>kx_pxache</code> (<code>src/kohakuaxi/pxache/</code>,
      docs/projects/kohakuaxi/pxache.md, and
      <RouterLink to="/component/pxache" class="doc-link"
        >its own page with the lane, hop and port diagrams</RouterLink
      >), assigns the masters and homes to <code>P</code> partitions of one
      clock and joins them with <b>per-source lanes</b>: a master's AR and AW/W
      stream, a home's R/B stream, each a chain of <code>kx_hop</code>s — one
      registered, credited hop per boundary, a tap at every partition, nothing
      muxed in transit — so every (master, home) pair keeps its own path and a
      boundary carries the crossbar's bandwidth. A hop is a TX register whose
      wire lands in a 16-entry ring RAM — the RAM's write port is the landing
      register — three cycles per direction; its destination and kind bits come
      out of distributed RAM, which is what took the lane from 469 to 666 MHz.
      <code>HOP_RXREG = 1</code> adds a flop before each landing RAM for a
      placement that wants one at both ends of a die crossing, at a cycle more
      per hop.
    </p>
    <Callout kind="rule" title="Nothing downstream of an engine ever waits">
      <p>
        The Xache orders one master's reads by holding a completed burst at its
        home until it is that master's oldest. Across a boundary that deadlocks:
        two masters, two homes, unequal latencies, each home holding the other
        master's burst. So <code>kx_pxache</code> holds nothing: a read reserves
        a <b>slot and a page of beats in a reorder ring</b> per master at the
        AR, beats from any home land the cycle they are offered at
        <code>{slot, beat}</code>, and the drain presents the oldest slot in
        issue order; a write takes a slot for its B and sends every beat before
        the next AW. The pick among sources is combinational and prefers the
        slot being drained — an engine's lookahead needs its ready in the cycle
        it presents (a cycle late: a beat per three cycles), and a plain
        lowest-valid pick let a near home land ahead of the drain (1,224 cycles
        against 1,044).
      </p>
    </Callout>
    <SpecTable
      :cols="pxCols"
      :rows="pxRows"
      caption="The ship shape at P partitions, one ooc_mod.tcl synthesis each, beside the kx_xache baseline. P=1 is the Xache within 22 LUT (the 30 BRAM are four reorder rings at the width floor); four partitions are 36 hops for 966 LUT over the Xache and 14,900 FF, BRAM at the width floor — 24 wide hops at 8.5, 12 narrow, 30 rings, 64 edges; the amber row is the same design with a register before every landing RAM, 432 fewer LUT and a cycle more per hop. Every ready and accept is gathered at elaboration over that partition's homes or masters only, so no unregistered path leaves a partition"
    />
    <SpecTable
      :cols="pxPerfCols"
      :rows="pxPerfRows"
      caption="kx_pxache_tb with TB_PERF beside kx_xache_tb: 4×4 K1, block-RAM arrays, the 4 KB interleave, a 24-cycle DRAM, 64-beat bursts, four outstanding, GB/s at 300 MHz. Reads across four partitions are within 2% of one; +8 cycles per boundary on a round trip is the whole latency cost; the single-ID row is the case the Xache's bench never ran, at the same rate because the ring orders by slot, never by ID"
    />

    <h2 class="doc-h2">Channel interleaving is an address permutation</h2>
    <p class="doc-p">
      The home is a field of the address and every home receives the whole
      address, so which bits <i>are</i> the home bits is only a question of
      which wires land on that field. <code>kx_perm</code> applies
      <code>NSWAP</code> bit-pair swaps to each master's address at the edge,
      before anything reads it. Interleaving at <code>2^G</code> bytes is a
      <b>rotation</b> of the field <code>[G, HOME_LSB + log2 N)</code> down by
      <code>log2 N</code> — pairs <code>(i, i+log2 N)</code> for
      <code>i = G … HOME_LSB−1</code> — which puts the page bits in the home
      field and shifts everything above them down, so consecutive 4 KB pages go
      to consecutive homes while each home's own address stays dense and every
      set-index bit keeps varying. Engines, arrays, tags and DRAM ports are
      untouched: they read fields, and a bijection on a dense space does not
      change what a field means. Any number of pairs, any <code>N</code>.
    </p>
    <Callout kind="trap" title="Rotate the field; do not swap the two fields">
      <p>
        Swapping <code>[33:32]</code> with <code>[13:12]</code> directly is also
        a bijection and also measures 0 LUT, but it parks the original bits
        33:32 — constant zero in any space under 16 GB — inside the set-index
        field <code>[20:6]</code>, so a 2 MB array would only ever use
        <b>a quarter of its sets</b>. The chain of <code>(i, i+log2 N)</code>
        swaps is the same mechanism with a different list, and keeps every index
        bit live.
      </p>
    </Callout>
    <Callout
      kind="rule"
      title="Two bounds, both enforced by an elaboration guard"
    >
      <p>
        Every swapped bit must be <b>≥ LINE_LSB</b>, so a cache line stays
        inside one home's array, and <b>≥ 12</b>, because AXI forbids a burst
        crossing a 4 KB boundary — so a burst never changes home mid-flight and
        neither engine needs a splitter. Finer than 4 KB is not a wire but a
        per-beat <code>AW</code> in the write engine, and is not built. The
        permutation assumes the address space is dense from 0 to
        <code>N × 2^HOME_LSB</code>.
      </p>
    </Callout>

    <h2 class="doc-h2">Knobs</h2>
    <SpecTable
      :cols="knobCols"
      :rows="knobRows"
      caption="Every configuration is a target — the RTL is the same at every point of the grid, and the table below measures the grid rather than one point. The ship point is M=4, N=4, K=1, SAMD both sides, 64 URAM per home, four DRAM-side crossings"
    />

    <h2 class="doc-h2">What it costs — the whole table</h2>
    <p class="doc-p">
      Every row: <code>W=512</code>, <code>AW=40</code>, <code>ID_W=4</code>, 64
      URAM per home (2 MB per home at K=1), one
      <code>ooc_kx.tcl</code> synthesis at a {{ TARGET_MHZ }} MHz ask on
      {{ PART }}. LUT and FF are the <b>entire</b> fused system — caches,
      engines, crossbar, edges. The array has two revisions and the read engine
      two forms, so there are three tables; the first two are the current array.
    </p>
    <h3 class="doc-h3">The streaming engine, RD_OUTQ 4 — the engine to ship</h3>
    <SpecTable
      :cols="tblCols"
      :rows="tblRowsP1"
      caption="RD_PIPE=1, RD_OUTQ=4, the current array. The ship at RD_OUTQ 1 / 2 / 8 is within 90 LUT of the 4 row: the queue depth is bookkeeping, not datapath. The ship with the 16 KB rotation (NSWAP 18, pairs (i, i+2) for i = 14..31) over a flat 16 GB is 9,994 LUT / 11,175 FF / 64 BRAM at 469 MHz — 352 above the un-rotated ship, one measurement"
    />
    <h3 class="doc-h3">The one-beat engine on the current array</h3>
    <SpecTable
      :cols="tblCols"
      :rows="tblRowsR2"
      caption="RD_PIPE=0, the current array (lookups pipelined beside the RAM latency, the fill address from the engine, fills yielding to writes). The array change alone took the ship from 11,865 to 10,323 and the no-crossing point from 9,914 to 8,408"
    />
    <h3 class="doc-h3">The first array revision</h3>
    <SpecTable
      :cols="tblCols"
      :rows="tblRows"
      caption="The rows every number above is compared against, kept as measured. The amber row is the ship design with its wide crossing FIFOs in distributed RAM instead of block RAM; the two interleave rows are bit-identical to the ship (wires)"
    />

    <Callout kind="measured" title="Fmax is flat across every M and N shape">
      <p>
        At <code>K = 1</code> every <code>M</code>/<code>N</code> point lands
        within a few MHz of the same figure (~495 on the first array, 449 on the
        current one with the one-beat engine, 437–469 with the streaming
        engine): a binary-index mux adds one LUT6 + MUXF7 level per doubling, so
        the crossbar's depth does not grow with port count. <code>K</code> is
        the knob that moves timing, through the wider row and the sub-word
        select — on the current array the binding path at
        <code>K &gt; 1</code> is the read address into the URAM cascade (403 MHz
        at 2, 378 at 4 one-beat; 379 / 358 streaming), still past the ask.
      </p>
    </Callout>

    <h3 class="doc-h3">Per knob — the streaming engine</h3>
    <SpecTable
      :cols="knobCostColsP1"
      :rows="knobCostRowsP1"
      caption="Marginal costs read from adjacent rows of the streaming engine's table, with the one-beat engine on the same array beside them. Read-SASD changed sign: a per-home streaming engine is about 950 LUT, so one for four homes saves 2,838 where the one-beat engine's 140-LUT engines saved nothing and their shared arbiter cost 130. SETS does not appear: the array is URAM and LUT is independent of the row count. W was held at 512 throughout"
    />
    <h3 class="doc-h3">Per knob — the first array revision</h3>
    <SpecTable
      :cols="knobCostCols"
      :rows="knobCostRows"
      caption="The first revision's marginal costs, kept as measured; the crossing and interleave rows are the ones the ship still carries"
    />

    <Callout
      kind="trap"
      title="Where the LUT figure comes from: the form of every wide select"
    >
      <p>
        A one-hot AND-OR over the homes' words, or a loop of
        <code>if (sel == j)</code> on a combinational select, builds LUT4/LUT5
        trees at <b>~8 LUT per data bit</b>. The same N:1 on a
        <b>registered binary</b> index packs as one LUT6 + MUXF7 per bit.
        Indexing a payload by <code>sel × PW</code> with a width that is not a
        power of two is a multiply and synthesises a barrel shifter. Registering
        the array's write-side inputs one cycle early keeps the fabric's M:1,
        the fill-or-word 2:1 and the strobe gating out of one cone. None of
        these changes what the design does; together they are the difference
        between the table above and several times it.
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
      caption="Three vendor rows, all kept. The default row compares the crossbar and cache MACHINERY, because in that block design the vendor cache mapped 17 BRAM per instance at its default data-memory type — about 150 KB, not the 2 MB it was asked for. The two 2 MB rows are the IP at a real 2 MB with its data memory in block RAM (561 per instance) and in URAM (64 per instance, the same 64 a Xache home uses — the IP's choice list is automatic / BRAM / URAM); the URAM row is the like-for-like one. Both top out at 244 MHz on the same path"
    />

    <Callout
      kind="measured"
      title="At the real memory size, in the same primitive"
    >
      <p>
        With the vendor cache's data memory in URAM — the fair row, and the one
        the IP offers as a choice — the fused system is
        <b>5.0× fewer LUT</b> with no crossings and 4.0× at the ship point with
        its four, 3.4× fewer FF, and meets 300 MHz where the vendor cache's 244
        MHz Fmax cannot reach it. The vendor's block-RAM build is 757 LUT larger
        per cache and takes 2,244 BRAM, 83% of the part's 2,688; it is kept
        because it is what the IP builds by default at that size. At the
        vendor's defaults — where its cache is a fraction of the size — the
        fused system is still 40% fewer LUT and 16% fewer FF with the full 2 MB
        per home present.
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
        four. Writes stream at one beat per cycle per home inside a burst, 19.2
        GB/s per home. That read figure is the serial read engine — one array
        lookup per beat, not one per line — and it, not the crossbar or the
        array, is the ceiling on a read-heavy master.
      </p>
    </Callout>

    <h3 class="doc-h3">Measured: streaming, with and without the interleave</h3>
    <SpecTable
      :cols="streamCols"
      :rows="streamRows"
      caption="kx_xache_tb with TB_PERF: 4×4 K1 SAMD, block-RAM arrays (RD_LAT=1), 64 lines (4 KB) per home so a 64 KB stream misses, axi4_ram behind every home, one clock, 64-beat bursts, one outstanding per master. Every scenario re-checks its data. Cycles on the fabric clock; GB/s at 300 MHz"
    />
    <Callout
      kind="measured"
      title="It pays under contention, and reads are engine-bound either way"
    >
      <p>
        A single stream is unchanged — one request outstanding, one engine at a
        time, whichever home it lands on. Four masters streaming distinct
        regions of the same 4 GB all serialise on home 0 under the contiguous
        map; interleaved they spread, <b>2.25× on writes and 2.28× on reads</b>.
        Not 4×: the four regions are 16 KB apart, a multiple of
        <code>N × 4 KB</code>, so all four streams start on home 0 together and
        march through homes 1, 2, 3 in step — the power-of-two-stride aliasing a
        hash removes and a bit swap alone does not.
      </p>
      <p>
        Writes run at one beat per cycle per home; reads at one array round per
        beat, 9.06 cycles on a miss and 6.06 on a hit at
        <code>RD_LAT = 1</code>, exactly the state-machine count. The read
        engine walks a burst one beat at a time although, under the 4 KB bound,
        every beat of a burst is in the same home and the array could take a
        lookup every cycle.
        <b>Pipelining that walk is the lever for read bandwidth</b>; neither the
        crossbar nor the arrays are in the way.
      </p>
    </Callout>

    <h3 class="doc-h3">The granularity</h3>
    <SpecTable
      :cols="granCols"
      :rows="granRows"
      caption="Same four-master scenario (16 KB per master, regions back to back), 16 KB of cache per home — 64 KB in total, equal to the working set — swept over the interleave granularity. Reads follow the masters' own writes; a re-read pass measured identical to the first in every row. With 4 KB of cache per home, where nothing fits: 4 KB → 4,043 cycles (4.86 GB/s), 16 KB → 2,312 (8.50), both all misses"
    />
    <Callout
      kind="measured"
      title="Burst length ≤ granularity ≤ cache per home, and match the stream"
    >
      <p>
        <b>Above the burst</b> is forced by the guard.
        <b>At or below the cache per home</b> is what turns the working set into
        hits: 64 KB only fits four 16 KB arrays when it is spread over all four,
        which is any granularity ≤ 16 KB — at 32 KB two homes each take 32 KB
        into 16 KB of array and thrash. <b>Equal to a stream's own extent</b> is
        what buys the parallelism: at 16 KB each master lives on its own home
        and the aggregate is exactly 4× one home —
        <b>71.2 GB/s written, 12.7 GB/s read on hits</b>, 8.50 on misses. At 4
        KB and 8 KB the four streams start on home 0 together and march in step,
        and the read pass takes 2,699 cycles against the 1,544 the four engines
        could deliver. A hash would remove that loss for any extent; a swap
        alone does not, and none is built.
      </p>
    </Callout>

    <h2 class="doc-h2">The streaming read engine and the read queue</h2>
    <p class="doc-p">
      The one-beat engine costs a full array round per beat. The streaming
      engine (<code>RD_PIPE = 1</code>) issues a lookup every cycle down the
      burst; a landing is <b>taken</b> — captured into the array's served-word
      register, valid raised — when it is the beat the master needs next and it
      hits, so hits stream at one beat per cycle. A landing that cannot be taken
      (the master stalled, or this burst is not its master's oldest) is
      <b>dropped and replayed</b>: the array is write-through, so any lookup may
      be re-done and nothing wide is ever buffered. A miss on the needed beat
      becomes <b>one DRAM read for the rest of the burst</b>, whose beats fill
      the array as they arrive while the lookups trail behind and hit.
    </p>
    <Fig
      caption="One burst at a time per engine, streaming. Responses to one master are ordered by a sequence number taken at AR accept; the fabric's R data select is the home of the burst that drains next, known from the AR and registered the cycle it becomes current, so a stream needs no valid delay."
      zoom
    >
      <StateMachine :states="rdpStates" :edges="rdpEdges" />
    </Fig>
    <SpecTable :cols="rdqKnobCols" :rows="rdqKnobRows" />
    <Callout
      kind="rule"
      title="RD_OUTQ is what lets one master use several channels"
    >
      <p>
        With <code>RD_OUTQ</code> bursts accepted at once and the 4 KB
        interleave sending consecutive pages to consecutive homes, the younger
        burst's home fetches while the older drains and then drains at hit speed
        when its turn comes. The fabric only ever presents a master's oldest
        burst, and the engine's <code>r_rdy</code> is qualified by
        <i>its own</i> visible valid — another engine's burst may be draining to
        the same master.
      </p>
    </Callout>

    <h3 class="doc-h3">Measured: one master, one home</h3>
    <SpecTable
      :cols="oneHomeCols"
      :rows="oneHomeRows"
      caption="kx_xache_tb with TB_PERF: 4×4 K1 SAMD, block-RAM arrays (RD_LAT=1), contiguous map, 16 KB of cache per home, axi4_ram with 24 cycles from AR to the first beat, one clock, 64-beat bursts. GB/s at 300 MHz"
    />
    <p class="doc-p">
      A miss stream on one home is 16 bursts × (64 beats + 24 latency + 8)
      cycles — one fetch per burst instead of one per line, <b>22×</b> the
      one-beat engine at this latency. Hits stream at one beat per cycle plus a
      5-cycle burst start. Contiguous 64 KB on one home is the one-channel case:
      one home serves one burst at a time whatever the queue, so
      <code>RD_OUTQ</code> needs the interleave to spread consecutive bursts
      over the homes.
    </p>

    <h3 class="doc-h3">Measured: one master across the homes</h3>
    <SpecTable
      :cols="queueCols"
      :rows="queueRows"
      caption="Same bench, 4 KB interleave, 4 KB of cache per home so every read misses, one master reading 64 KB. The master's own port — 512 bits per cycle, 19.2 GB/s — is the ceiling: 95% of it at latency 24, 92% at 60, 98% when the 64 KB hits"
    />
    <SpecTable
      :cols="fourCols"
      :rows="fourRows"
      caption="Four masters at once, 16 KB each, 16 KB of cache per home. The 16 KB row is four ports at their ceiling (their writes at the same interleave: 71.2 GB/s); at 4 KB the four streams march in lockstep through the homes, and eight homes halve that loss"
    />
    <Callout kind="measured" title="Against the goal: 3× one channel">
      <p>
        One channel at 300 MHz and 512 bits is 19.2 GB/s. A single master now
        reads at <b>18.3 GB/s</b> on misses and <b>18.8</b> on hits where the
        one-beat engine read at 0.58 and 3.19 under the same DRAM latency (32×
        and 5.9×), and four masters at <b>34–73 GB/s</b>, 1.8–3.8× one channel's
        peak. Every figure is the shipped RTL — loop 4 of the cost table —
        counted by kx_xache_tb under Verilator.
      </p>
    </Callout>

    <h3 class="doc-h3">Measured: across the shapes</h3>
    <SpecTable
      :cols="shapeCols"
      :rows="shapeRows"
      caption="Every measured shape on the shipped RTL, M masters at once with 16 KB each: K=1, SAMD both sides, no crossing, 24-cycle DRAM, 16 KB of cache per home (misses: 4 KB), GB/s at 300 MHz, one-beat → streaming. One master is the same at every shape: write 17.8; read hits 3.19 → 18.8; misses 0.58 → 18.3 (4 KB interleave). The 16 KB interleave with hits is every port at its ceiling; 8×8 misses read 102 GB/s from eight channels, 66% of their peak"
    />

    <h3 class="doc-h3">The master side</h3>
    <p class="doc-p">
      A master that waits for each burst to drain before issuing the next gets
      one burst in flight whatever <code>RD_OUTQ</code> is. Single-master speed
      therefore also needs the master's own outstanding depth — an
      <code>AR</code> issued while the previous <code>R</code> is still
      streaming; no new signalling, no ID scheme, the plain AXI decoupling. The
      framework's master onto DRAM, <code>mag_dram_port</code> in the system
      node, already carries that depth as <code>RD_OUT</code> (exposed on
      <code>mag</code> as <code>DRAM_RD_OUT</code>, default 1), verified at 2
      and 4 by its component bench with queued reads and by
      <code>mover_chain1/2/4</code>.
    </p>
    <SpecTable
      :cols="magCols"
      :rows="magRows"
      caption="mag_dram_port alone, N=5, SW=256, MW=512, s_aclk and m_aclk asynchronous, 300 MHz ask, one synthesis each (scripts/tcl/ooc_mod.tcl). Bandwidth from mag_dram_port_bw_tb at a 300 MHz mesh clock and a 106 ns DRAM; the 256-bit internal beat caps one requester at 9,600 MB/s"
    />

    <h2 class="doc-h2">Verification</h2>
    <p class="doc-p">
      <code>tests/axi/kx_xache_tb.v</code> drives the whole system between AXI
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
      <code>kx_rd_engine</code>, <code>kx_wr_engine</code>,
      <code>kx_link</code> and <code>kx_scdc</code> has its own bench beneath
      it.
    </p>

    <h2 class="doc-h2">What it deliberately does not do</h2>
    <Callout kind="note" title="Not built, and not an omission">
      <p>
        <b>No associativity and no replacement policy</b> — direct-mapped, a
        conflicting line evicts on fill; the URAM budget goes to rows, not ways.
        <b>No writeback</b> — write-through only.
        <b>No read pipelining within an engine</b> — one request in flight per
        engine, one beat per array round, and the performance table is the
        consequence. <b>No exclusive access, cache or protection attributes</b>;
        every DRAM request is <code>INCR</code> at the line size, and
        <code>WRAP</code>/<code>FIXED</code> execute as <code>INCR</code>.
        <b>No coherence between homes</b> — an address belongs to exactly one.
        <b>No error recovery</b> and <b>no runtime observability</b>.
      </p>
    </Callout>

    <h2 class="doc-h2">Fixed protocol, addon, convention, or yours</h2>
    <SpecTable :cols="catCols" :rows="catRows" />

    <p class="doc-p">
      The per-knob model fitted to the table above, with its validation against
      every row — worst error 2.29% on LUT and 1.02% on FF — is the
      <RouterLink to="/framework/estimator" class="doc-link"
        >resource estimator</RouterLink
      >.
    </p>
  </DocPage>
</template>
