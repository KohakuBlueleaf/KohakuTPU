<script setup>
/**
 * /component/station-bus — the AXI station bus: a line of stations that
 * carries host and control traffic to endpoints of many widths and clocks
 * across the dies. It replaced the SmartConnect tree.
 *
 * Drawn from, in full:
 *   docs/projects/kohakuaxi/station-bus.md
 *   src/kohakuaccel/axi/station/
 *   scripts/tcl/ooc_line_d2.tcl · scripts/tcl/ooc_station.tcl
 *
 * Every figure is out-of-context synthesis on xcvu13p-fhgb2104-2L-e, Vivado
 * 2024.2, except the one placed-and-routed result, which says so.
 */

// ------------------------------------------------------------ station line
const line = {
  groups: [
    {
      x: -1.5,
      y: 4.5,
      w: 59,
      h: 12,
      label: "sb_bd_line4 — one module, four pblocks",
    },
  ],
  nodes: [
    /* The masters sit symmetrically over station 1 and high enough that
     * each wire jogs to its slot above the group, so the verticals cross
     * the group's top edge right of its label. */
    {
      id: "mj",
      x: 8.25,
      y: -2.5,
      w: 7.5,
      h: 3,
      label: "JTAG-AXI",
      sub: "64b @100",
    },
    {
      id: "mx",
      x: 16.75,
      y: -2.5,
      w: 7.5,
      h: 3,
      label: "XDMA",
      sub: "512b @250",
    },
    {
      id: "ml",
      x: 25.25,
      y: -2.5,
      w: 7.5,
      h: 3,
      label: "XDMA-Lite",
      sub: "32b @250",
    },

    {
      id: "s0",
      x: 0,
      y: 6,
      w: 11,
      h: 4,
      label: "station 0",
      sub: "SLR0 · own fabric clock",
    },
    {
      id: "s1",
      x: 15,
      y: 6,
      w: 11,
      h: 4,
      label: "station 1",
      sub: "SLR1 · 3 NMUs",
      accent: true,
    },
    { id: "s2", x: 30, y: 6, w: 11, h: 4, label: "station 2", sub: "SLR2" },
    { id: "s3", x: 45, y: 6, w: 11, h: 4, label: "station 3", sub: "SLR3" },

    { id: "l01", x: 11.5, y: 7, w: 3, h: 2, label: "link", sub: "CDC" },
    { id: "l12", x: 26.5, y: 7, w: 3, h: 2, label: "link", sub: "CDC" },
    { id: "l23", x: 41.5, y: 7, w: 3, h: 2, label: "link", sub: "CDC" },

    {
      id: "q0",
      x: 0,
      y: 12.5,
      w: 11,
      h: 3.4,
      label: "4 subordinates",
      sub: "MEM 256b · CTRL · DDR · clk_wiz",
    },
    {
      id: "q1",
      x: 15,
      y: 12.5,
      w: 11,
      h: 3.4,
      label: "4 subordinates",
      sub: "two of them AXI4-Lite",
    },
    { id: "q2", x: 30, y: 12.5, w: 11, h: 3.4, label: "4 subordinates" },
    { id: "q3", x: 45, y: 12.5, w: 11, h: 3.4, label: "4 subordinates" },
  ],
  edges: [
    { from: "mj:b", to: "s1:t", accent: true },
    { from: "mx:b", to: "s1:t", accent: true },
    { from: "ml:b", to: "s1:t", accent: true },
    { from: "s1:l", to: "l01:r", label: "REQ", dir: "h" },
    { from: "l01:l", to: "s0:r", label: "REQ", dir: "h" },
    { from: "s1:r", to: "l12:l", label: "REQ", dir: "h" },
    { from: "l12:r", to: "s2:l", label: "REQ", dir: "h" },
    { from: "s2:r", to: "l23:l", label: "REQ", dir: "h" },
    { from: "l23:r", to: "s3:l", label: "REQ", dir: "h" },
    { from: "s0:b", to: "q0:t" },
    { from: "s1:b", to: "q1:t" },
    { from: "s2:b", to: "q2:t" },
    { from: "s3:b", to: "q3:t" },
  ],
};

// ------------------------------------------------------------- the switch
/* Laid out as the station sits on the line: the left link enters at the left
 * and leaves through to_right beside it, the right link enters at the right
 * and leaves through to_left, the K masters inject from above into both, and
 * eject collects both link inputs below and broadcasts to the Q slaves. The
 * six input/mux wires form a 6-cycle, which two columns cannot draw without
 * a crossing; this ring can. */
const sw = {
  nodes: [
    { id: "fl", x: 0, y: 6, w: 12, h: 3, label: "from left" },
    {
      id: "mr",
      x: 17,
      y: 6,
      w: 14,
      h: 3,
      label: "to_right",
      sub: "mux2(from_left, inject)",
    },
    {
      id: "inj",
      x: 26,
      y: 0,
      w: 15,
      h: 3,
      label: "inject",
      sub: "K:1 round robin",
      accent: true,
    },
    {
      id: "ml2",
      x: 36,
      y: 6,
      w: 14,
      h: 3,
      label: "to_left",
      sub: "mux2(from_right, inject)",
    },
    { id: "fr", x: 55, y: 6, w: 12, h: 3, label: "from right" },
    {
      id: "ej",
      x: 27,
      y: 12,
      w: 13,
      h: 3,
      label: "eject",
      sub: "mux2(from_left, from_right)",
    },
    {
      id: "q",
      x: 25.5,
      y: 18,
      w: 16,
      h: 3,
      label: "Q subordinates",
      sub: "valid gated by dst_port",
      accent: true,
    },
  ],
  edges: [
    { from: "fl:r", to: "mr:l", dir: "h" },
    { from: "fr:l", to: "ml2:r", dir: "h" },
    { from: "inj:b", to: "mr:t" },
    { from: "inj:b", to: "ml2:t" },
    { from: "fl:b", to: "ej:l" },
    { from: "fr:b", to: "ej:r" },
    { from: "ej:b", to: "q:t", label: "broadcast", accent: true },
  ],
};

// --------------------------------------------------------------- bitfields
const reqFlit = [
  { name: "dst_stn", bits: 2, accent: true },
  { name: "dst_port", bits: 2, accent: true },
  { name: "src_stn", bits: 2 },
  { name: "src_port", bits: 2 },
  { name: "tag", bits: 4 },
  { name: "wr", bits: 1 },
  { name: "head", bits: 1 },
  { name: "last", bits: 1 },
  { name: "addr", bits: 43, value: "translated" },
  { name: "len", bits: 8 },
  { name: "size", bits: 3 },
  { name: "data", bits: 256, value: "FW", accent: true },
  { name: "strb", bits: 32, value: "FW/8" },
];

const rspFlit = [
  { name: "dst_stn", bits: 2, accent: true },
  { name: "dst_port", bits: 2, accent: true },
  { name: "tag", bits: 4 },
  { name: "wr", bits: 1 },
  { name: "last", bits: 1 },
  { name: "resp", bits: 2 },
  { name: "data", bits: 256, value: "FW", accent: true },
];

// ------------------------------------------------------------ wave traces
const liteBroken = {
  rows: [
    {
      name: "from the NSU",
      kind: "bus",
      values: ["AW len=3", "W0", "W1", "W2", "W3", "AW (next)"],
    },
    {
      name: "to the Lite port",
      kind: "bus",
      values: ["AWADDR", "W0", "—", "—", "—", "AWADDR"],
    },
    {
      name: "orphan W parked",
      kind: "bit",
      values: [0, 0, 1, 1, 1, 1],
      mark: [2, 3, 4, 5],
    },
    {
      name: "read answers at",
      kind: "text",
      values: ["", "", "", "", "", "flit lane 0 only"],
    },
  ],
  notes: [
    {
      cycle: 2,
      text: "A Lite slave cannot see AWLEN. Terminating the burst signals instead leaves orphan W beats parked at the endpoint: every later AW pairs with a stale beat, and reads answer only at flit lane 0.",
      tone: "bad",
    },
  ],
};

const liteFixed = {
  rows: [
    {
      name: "from the NSU",
      kind: "bus",
      values: ["AW len=3", "W0", "W1", "W2", "W3", "AW (next)"],
    },
    {
      name: "to the Lite port",
      kind: "bus",
      values: ["AW+W @a", "AW+W @a+4", "AW+W @a+8", "AW+W @a+12", "—", "AW+W"],
    },
    {
      name: "B collected",
      kind: "bus",
      values: ["—", "b0", "b1", "b2", "b3", "—"],
    },
    {
      name: "one BRESP up",
      kind: "text",
      values: ["", "", "", "", "worst case", ""],
    },
    {
      name: "zero-strobe beat",
      kind: "text",
      values: ["", "consumed, never issued", "", "", "", ""],
    },
  ],
  notes: [
    {
      text: "sb_axi2lite walks the burst itself: one complete Lite AW+W+B or AR+R handshake per beat, one read per slice, IDs and last regenerated.",
      tone: "good",
    },
    {
      text: "Zero-strobe write beats are consumed but never issued — a Lite slave may legally ignore WSTRB and would corrupt. Responses are coalesced worst-case, DECERR over SLVERR over OKAY.",
      tone: "good",
    },
  ],
};

const credited = {
  rows: [
    {
      name: "CRED = 16",
      kind: "bus",
      values: ["f0", "f1", "f2", "f3", "f4", "f5", "f6", "f7"],
    },
    {
      name: "CRED = 1",
      kind: "bus",
      values: ["f0", "—", "—", "—", "—", "f1", "—", "—"],
    },
    {
      name: "credit back",
      kind: "bit",
      values: [0, 0, 0, 0, 1, 0, 0, 0],
      mark: [4],
    },
    {
      name: "correct?",
      kind: "text",
      values: ["yes", "yes", "yes", "yes", "yes", "yes", "yes", "yes"],
    },
  ],
  notes: [
    {
      text: "valid/ready does not pipeline: n stages cost a bubble each or a skid each, and ready becomes a long backwards chain — the last signal you want crossing a die. With credits, pipeline depth affects only the credit count, never correctness, never Fmax.",
      tone: "good",
    },
    {
      text: "CRED ≥ 2·PIPE is a throughput condition, not a safety one: below it the link stalls waiting for credit returns instead of overflowing. SB_FW256 SB_CRED1 passes 673 checks.",
      tone: "good",
    },
  ],
};

// ------------------------------------------------------------------ tables
const hopCols = [
  { key: "cmp", label: "dst_stn against mine", mono: true },
  { key: "act", label: "Action" },
];
const hopRows = [
  { cmp: "equal", act: "eject to <code>dst_port</code>" },
  { cmp: "less", act: "pass left" },
  { cmp: "greater", act: "pass right" },
];

const invCols = [
  { key: "n", label: "#", align: "right" },
  { key: "inv", label: "Invariant" },
];
const invRows = [
  {
    n: "1",
    inv: "A master may not inject until response buffer space is reserved.",
  },
  { n: "2", inv: "REQ and RSP never share a buffer." },
  {
    n: "3",
    inv: "Arbitration is packet-atomic — a grant is held until <code>last</code>, which gives AXI4's no-write-interleaving rule for free.",
  },
  {
    n: "4",
    inv: "IDs do not cross the fabric; routes do. The flit carries <code>{src_stn, src_port}</code> and each slave shim issues its own local id.",
  },
];

const widthCols = [
  { key: "case", label: "Case" },
  { key: "what", label: "What the shim does" },
  { key: "bound", label: "Where it stops" },
];
const widthRows = [
  {
    case: "<b>master at or below FW</b>",
    what: "<code>sb_nmu</code> packs beats into whole flits after its request FIFO. Any <code>AxSIZE</code> up to the port width, any alignment, and bursts crossing flit boundaries are all legal",
    bound:
      "none — reads run the same conversion backwards, walking a per-tag byte offset",
    _tone: "good",
  },
  {
    case: "<b>master wider than FW</b>",
    what: "one port beat splits into <code>SUB = MW/FW</code> flits; its beats must be full-width, the shape a DMA data engine produces",
    bound:
      "the declared contract <b>excludes a split manager addressing a sub-flit port</b>, and the simulation guard names it",
    _tone: "warn",
  },
  {
    case: "<b>subordinate at or below FW</b>",
    what: "<code>sb_nsu</code> unpacks before its FIFO on writes and packs after its response FIFO on reads; a port whose width equals the flit has the machinery pruned at synthesis",
    bound: "none",
    _tone: "good",
  },
  {
    case: "<b>subordinate wider than FW</b>",
    what: "<b>not supported.</b> There is a scatter path and no gather path",
    bound:
      "a generate block instantiates an undefined module to make it an <b>elaboration error</b> instead of a silent corruption",
    _tone: "bad",
  },
];

/* per die at the SHIP RECIPE (ooc_line_d2.tcl, the loop-3 design point):
   each station row is its hub set + its NSU shims (+ the NMU shims on SLR1)
   read from the hierarchy report; rows sum to the netlist exactly —
   23,053 LUT with 32 of top-level glue, 42,223 FF. The earlier no-block-RAM
   configuration (22,106 / 48,167) is the row the FW and AW sweeps quote. */
const perDieCols = [
  { key: "die", label: "" },
  { key: "smc", label: "SMC LUTs", mono: true, align: "right" },
  { key: "line", label: "line LUTs", mono: true, align: "right" },
  { key: "ratio", label: "ratio", mono: true, align: "right" },
  { key: "smcff", label: "SMC FF", mono: true, align: "right" },
  { key: "lff", label: "line FF", mono: true, align: "right" },
  { key: "bram", label: "line BRAM", mono: true, align: "right" },
  { key: "prov", label: "SMC provenance", mono: true },
];
const perDieRows = [
  {
    die: "SLR0 — hub set 1,209 + NSUs 752 / 808 / 808 / 808",
    smc: "8,516",
    line: "4,385",
    ratio: "1.94×",
    smcff: "16,575",
    lff: "6,323",
    bram: "15 + 3",
    prov: "multimesh_<b>v4</b>_leaf_smc_0_0",
    _tone: "warn",
  },
  {
    die: "SLR1 — hub set 2,122 + NMUs 1,158 / 967 / 609 + NSUs 760 / 811 / 808 / 808",
    smc: "41,788",
    line: "<b>8,043</b>",
    ratio: "<b>5.20×</b>",
    smcff: "57,002",
    lff: "11,031",
    bram: "39 + 4",
    prov: "multimesh_v5_root_smc_0",
  },
  {
    die: "SLR2 — hub set 1,732 (forwards both ways) + NSUs 752 / 808 / 808 / 808",
    smc: "13,266",
    line: "4,908",
    ratio: "2.70×",
    smcff: "24,984",
    lff: "7,054",
    bram: "15 + 3",
    prov: "multimesh_v5_leaf_smc_2_0",
  },
  {
    die: "SLR3 — hub set 1,216 + NSUs 752 / 808 / 808 / 808",
    smc: "8,516",
    line: "4,392",
    ratio: "1.94×",
    smcff: "16,575",
    lff: "6,329",
    bram: "15 + 3",
    prov: "multimesh_<b>v4</b>_leaf_smc_0_0",
    _tone: "warn",
  },
  {
    die: "inter-die — three link pairs, 241 + 190 each",
    smc: "9,795",
    line: "1,293",
    ratio: "7.58×",
    smcff: "14,988",
    lff: "11,486",
    bram: "0",
    prov: "3 × multimesh_<b>v2</b>_slr_cross_2_0",
    _tone: "warn",
  },
  {
    die: "top-level glue",
    smc: "",
    line: "32",
    ratio: "",
    smcff: "",
    lff: "0",
    bram: "",
    prov: "",
  },
  {
    die: "<b>total</b>",
    smc: "<b>81,881</b>",
    line: "<b>23,053</b>",
    ratio: "<b>3.55×</b>",
    smcff: "<b>130,124</b>",
    lff: "<b>42,223</b>",
    bram: "84 + 13",
    prov: "only two of five rows are v5",
  },
];

const totals = [
  {
    label: "SmartConnect tree — SUPERSEDED baseline",
    value: 81881,
    note: "mixed provenance",
    tone: "bad",
  },
  {
    label: "station line — what is built, the ship recipe",
    value: 23053,
    note: "one synthesis, 90 BRAM",
    tone: "good",
  },
  {
    label: "SLR1 — root_smc, superseded",
    value: 41788,
    note: "v5 report",
    tone: "bad",
  },
  {
    label: "SLR1 — station 1, built",
    value: 8043,
    note: "3 NMUs, 4 NSUs, 8 hubs",
    tone: "good",
  },
];

const sllCols = [
  { key: "b", label: "Boundary" },
  { key: "m", label: "SLLs measured", mono: true, align: "right" },
  { key: "pct", label: "% of 23,040", mono: true, align: "right" },
  { key: "out", label: "outward", mono: true, align: "right" },
  { key: "in", label: "inward", mono: true, align: "right" },
];
const sllRows = [
  { b: "SLR1 ↔ SLR0", m: "639", pct: "2.77%", out: "365", in: "274" },
  { b: "SLR2 ↔ SLR1", m: "634", pct: "2.75%", out: "363", in: "271" },
  { b: "SLR3 ↔ SLR2", m: "644", pct: "2.80%", out: "364", in: "280" },
  {
    b: "<i>derived from sb_line4.v:121</i>",
    m: "<i>629</i>",
    pct: "<i>2.73%</i>",
    out: "<i>359</i>",
    in: "<i>270</i>",
    _tone: "warn",
  },
];

const chooseCols = [
  { key: "knob", label: "Knob", mono: true },
  { key: "val", label: "Choose", mono: true },
  { key: "why", label: "Why, in numbers" },
];
const chooseRows = [
  {
    knob: "FW",
    val: "256",
    why: "the widest slave, <code>S_AXI_MEM</code>, is 256 bits, so the fabric meets it exactly and the 512-bit XDMA master splits 2:1. 22,106 LUTs against 30,785 at 512 — 28% — and 629 cross-SLR wires against 1,173, swept in the no-block-RAM configuration. At 200 MHz it carries 6.4 GB/s, exactly what the 512-bit-at-100 MHz SmartConnect provided",
  },
  {
    knob: "AW",
    val: "43",
    why: "forced: mesh MEM sits at <code>(id+1) &lt;&lt; 40</code>. Costs 3.6% over 32 bits (21,345 → 22,106, same sweep)",
  },
  {
    knob: "bus clock",
    val: "200 MHz",
    why: "OOC binding clock is <code>bus_clk1</code> at 357.9 MHz, 1.79× the target. Area is flat from 150 to 300 MHz (0.3%), so the constraint is free anywhere in that range",
  },
  {
    knob: "LINK_FULL",
    val: "0",
    why: "<b>not a saving — a declaration.</b> Every master sits on station 1, so each boundary needs one REQ and one RSP stream, which is what 0 builds. 1 builds four and is only correct if masters sit on both sides",
  },
  {
    knob: "LINK_CDC",
    val: "1",
    why: "each die gets its own fabric clock, which is the point — a shared clock couples every station to the worst one, and the SLL hop alone is 0.755 ns. Collapsing to one clock <i>costs</i> 328 LUTs",
  },
  {
    knob: "OST / STORE_FWD",
    val: "BALANCED",
    why: "the whole outstanding range spans 4.3% (30,512 → 31,838). Not a lever; take the middle",
  },
  {
    knob: "LUT_PER_BRAM",
    val: "820",
    why: "block RAM saves 5,804 LUTs for 130.5 tiles — 44 LUTs per tile, against the ~820 a tile is worth on this device. Per station, so a die with spare block RAM may choose otherwise",
  },
  {
    knob: "MAX_BURST",
    val: "1 on 32-bit ports",
    why: "single-beat is a <b>protocol guarantee</b> on <code>M_AXI_LITE</code>. Declaring it keeps their response FIFOs at depth 16 instead of the 256 the 4 KB rule would demand — worth +5,013 LUTs on the 3×9 station",
  },
];

const checkCols = [
  { key: "cfg", label: "Configuration", mono: true },
  { key: "n", label: "Checks", mono: true, align: "right" },
];
const checkRows = [
  {
    cfg: "<b>deployed config — FW256 NQ4 HALFLINK LINKCDC</b>",
    n: "<b>671</b>",
    _tone: "good",
  },
  { cfg: "default, FW=512", n: "673" },
  { cfg: "SB_FW256, 512-bit master over a 256-bit fabric", n: "673" },
  { cfg: "SB_NQ4", n: "670" },
  { cfg: "SB_MIXPRESET, four unlike stations on one line", n: "673" },
  { cfg: "SB_FW256 SB_CRED1, one credit per link", n: "673" },
  {
    cfg: "SB_WIDE512, 512-bit slave under a 256-bit fabric",
    n: "rejected at elaboration",
    _tone: "bad",
  },
];

const catCols = [
  { key: "t", label: "Thing" },
  { key: "c", label: "Category" },
];
const catRows = [
  {
    t: "AXI4 itself — the five channels, the handshake, the burst rules",
    c: "<b>fixed protocol</b>, and not ours. It is the reason this layer exists",
  },
  {
    t: "the flit a station carries, and its <code>dst_stn</code> / <code>dst_port</code> routing",
    c: "<b>fixed protocol</b> within the line. A station forwards it verbatim, so anything in the flit is line-global",
  },
  {
    t: "the four invariants — reserve before inject, REQ and RSP never share, packet-atomic arbitration, routes not IDs",
    c: "<b>fixed protocol.</b> Breaking any one of them is a deadlock, not a slowdown",
  },
  {
    t: "the address map's window prefix and the control region's positional decode",
    c: "<b>fixed protocol</b>, owned by <span class='chip'>ship</span> and consumed here",
  },
  {
    t: "<code>FW</code>, <code>AW</code>, <code>CRED</code>, <code>OST</code>, <code>LUT_PER_BRAM</code>, station count, ports per station",
    c: "<b>customizable</b> — sized per deployment, and the page above says which were forced by measurement",
  },
  {
    t: "<code>LINK_CDC</code>, and therefore whether each die runs its own fabric clock",
    c: "<b>customizable</b>, and free in both directions — collapsing it <i>costs</i> 328 LUTs",
  },
  {
    t: "which station a manager sits on",
    c: "<b>convention</b>, forced here by where the host bridge is anchored rather than by the structure",
  },
  {
    t: "declaring a control port single-beat",
    c: "<b>convention</b> with teeth: it is a promise, and it is worth 5,013 LUTs on one station",
  },
  {
    t: "what any master or subordinate on the line actually does",
    c: "<b>yours</b>",
  },
];

const notOwnedCols = [
  { key: "n", label: "Not owned" },
  { key: "w", label: "Who owns it" },
];
const notOwnedRows = [
  {
    n: "anything that speaks flits on the mesh",
    w: "noc. The two flits share a word and nothing else — a station flit never enters a router",
  },
  {
    n: "the memory path from the meshes' DRAM masters to the channels",
    w: "<RouterLink to='/component/xache' class='doc-link'>Kohaku Xache</RouterLink>. Host memory traffic reaches DRAM through MAG's memory window and its upload master, never through this line",
  },
  {
    n: "what a memory request means, and the descriptor behind a burst",
    w: "sysnode. This layer sees the beats, never the intent",
  },
  {
    n: "which window a mesh answers at",
    w: "ship. The map is a ship-level fact; the interconnect only consumes the prefix",
  },
  {
    n: "which die a station lands on, and the pblock it lands in",
    w: "physical",
  },
  {
    n: "the DDR4 controller, the host bridge, the debug bridge",
    w: "the vendor. Converting to them once, in modules whose job is only conversion, is this layer's entire purpose",
  },
  {
    n: "whether a master pipelines its bursts",
    w: "that master. The line does not queue on its behalf, and a single-outstanding master gets the bandwidth it asked for",
  },
];
</script>

<template>
  <DocPage
    title="The station bus"
    summary="A line of stations, one per die, that carries host and control AXI traffic to endpoints of many widths and clocks without a crossbar. It replaced the SmartConnect tree at 3.55× fewer LUTs — 5.2× on the die that carried the tree's root — and every station runs its own clock for free."
    domain="framework"
    status="measured"
    source="src/kohakuaccel/axi/station/ · docs/projects/kohakuaxi/station-bus.md · docs/address-map.md"
  >
    <p class="doc-p">
      Many AXI masters to many slaves without a crossbar, when the endpoints
      span multiple clock domains, data widths and dies. This is the first of
      the framework's two AXI systems and the one the host talks to: XDMA, the
      JTAG bridge and the host-side control engine reach every MAG control
      window, the clock wizard and the DDR controller's register port through
      it. The other system,
      <RouterLink to="/component/xache" class="doc-link"
        >Kohaku Xache</RouterLink
      >, carries the meshes' memory traffic into DRAM; the two share no module.
      <RouterLink to="/framework/axi" class="doc-link"
        >AXI in this machine</RouterLink
      >
      says which kind of AXI goes where.
    </p>

    <Callout
      kind="note"
      title="Status: the station bus replaced the SmartConnect tree"
    >
      <p>
        The line of stations described below is what is on the die. It
        <b>replaced</b> the SmartConnect tree, and every SmartConnect,
        <code>root_smc</code>, <code>leaf_smc</code> and
        <code>slr_cross</code> figure on this page is the
        <b>superseded baseline</b> the replacement was measured against — none
        of it is in the design any more.
      </p>
      <p>
        The comparison is kept because it is the argument for the structure, not
        because either side is still a live option. Read every SMC number as
        “what this replaced”, and mind the provenance caveat below: that
        baseline is not uniformly v5.
      </p>
    </Callout>

    <h2 class="doc-h2">A line, not a star</h2>
    <Callout kind="rule" title="S stations on a line. There is no root.">
      <p>
        Every station is the same module, carrying any number of local AXI
        masters and any number of local AXI slaves,
        <b>including zero of either</b>, and having exactly two neighbours. S is
        arbitrary and the ends simply lack one neighbour. Where the masters sit
        is a deployment choice: one station, several, or none on a given
        station.
      </p>
    </Callout>

    <Fig
      caption="The deployed shape: four stations, one per SLR, three masters on station 1, four subordinates each. Each link also carries an RSP stream back — that pair is exactly what LINK_FULL=0 builds, and it is the minimum, not an economy."
      zoom
      wide
    >
      <BlockDiagram
        :nodes="line.nodes"
        :edges="line.edges"
        :groups="line.groups"
      />
    </Fig>

    <Callout kind="note" title="A tree pays AXI ports to reach itself">
      <p>
        A tree reaches another die through an AXI master port on the parent, an
        AXI slave port on the child, and a register slice between them. A line
        reaches it through a link, which is not an AXI port and appears in
        neither station's port count. For S dies that is
        <b>2(S−1) AXI ports the line does not instantiate</b>, and those ports
        are full-width shims, not wires.
      </p>
      <p>
        Measured, the connective tissue between dies — three
        <code>slr_cross</code> register slices against three credited links — is
        <b>9,795 LUTs against 1,293</b> at the ship recipe, a factor of 7.6
        (1,641 and 6.0 in the earlier no-block-RAM configuration).
      </p>
    </Callout>

    <h3 class="doc-h3">The switch is three muxes</h3>
    <Fig
      caption="Ejection to Q slaves is free — broadcast the payload, gate each valid with dst_port == my_index. A demux is decode, not a mux tree, which is why the K×Q term never appears; preserve it above all else. Injection from K masters is the only place port count multiplies width, and it is O(K). Every station output is registered."
      zoom
      wide
    >
      <BlockDiagram :nodes="sw.nodes" :edges="sw.edges" />
    </Fig>

    <SpecTable
      :cols="hopCols"
      :rows="hopRows"
      caption="Routing is one comparison per hop. The address map is static and known at build time, so the NMU does not route — it labels. Each hop decides for itself, which is what lets a flit pass through a station it does not belong to"
    />

    <SpecTable :cols="invCols" :rows="invRows" caption="Four invariants" />

    <h3 class="doc-h3">The flit</h3>
    <p class="doc-p">
      A flit is forwarded verbatim through intermediate stations — the switch
      does no width or format conversion — so anything appearing in the flit is
      line-global. Everything private to a shim is per-station.
    </p>

    <BitField
      :fields="reqFlit"
      caption="REQ, at the deployed shape: FW=256, AW=43, STNW=2, PORTW=2, SRCW=2, TAGW=4, summing to the 357 bits sb_line4.v:140 declares. Widths and the field set are the RTL's; the order is the published field table, and the packing inside the payload bus is not part of the contract — read the ranges as proportions. The header rides sideband alongside the first payload beat, because a separate header flit costs 100% overhead on a single-beat transfer, which is what control traffic is"
    />
    <BitField
      :fields="rspFlit"
      caption="RSP, 268 bits at the same shape. The return route rides the RSP payload too, because a forwarding station routes to one link and a dst consumed by the hub would not reach the far end. {src_stn, src_port} is packed as one opaque field that the slave shim echoes back, so sb_nsu needs no knowledge of the line at all"
    />

    <h3 class="doc-h3">Credits, not ready</h3>
    <WaveTrace v-bind="credited" label="one link, two credit settings" />

    <Callout
      kind="trap"
      title="The credit return must be a gray counter, not a pulse"
    >
      <p>
        Each station runs its own fabric clock, so every inter-station link is
        also a clock crossing. Pops occur in the far clock and can outpace a
        slower near clock;
        <b
          >a pulse synchroniser drops them and the link starves silently instead
          of failing.</b
        >
      </p>
      <p>
        The link exposes no runtime status because it has none to expose: a flit
        departs only against a credit and the RX FIFO is sized
        <code>RXD = max(16, CRED)</code>, so overflow is impossible by
        construction at every <code>CRED</code>, rather than by a check anyone
        has to run. A credited link that ties <code>tready</code> high instead —
        as the interlink's <code>mag_link_cdc</code> does — <i>can</i> overflow,
        and then a sticky fault bit and a software poll are load bearing. That
        is a property of that flow-control choice, not of clock crossing.
      </p>
    </Callout>

    <Callout
      kind="measured"
      title="Per-station clock domains are free — and that is the central claim"
    >
      <p>
        Collapsing the line onto one clock does not save anything; it
        <b>costs 328 LUTs</b> and saves 670 flip-flops, a wash either way at 1%
        of the design. The same change on <code>axi_interconnect</code> costs
        <b>9,833 LUTs at max-performance and 5,597 at minimum-area</b> — 2.47×
        and 2.26×.
      </p>
      <p>
        A shim takes <b>no parameter describing the clock relationship</b>.
        SmartConnect — the IP this replaced — needs <code>NUM_CLKS</code> plus
        per-port assignment and fails silently when domains do not propagate. A
        structure that cannot be misconfigured that way is worth more than the
        LUT difference.
      </p>
      <p class="kt-text-caption">
        <code>xcvu13p-fhgb2104-2L-e</code>, out-of-context synthesis, Vivado
        2024.2, four-station line at FW=512, AW=43, BALANCED, no block RAM.
      </p>
    </Callout>

    <h2 class="doc-h2">Width conversion happens at the port, once</h2>
    <p class="doc-p">
      <code>FW</code> is independent of the AXI port widths on either side. The
      shims convert; the fabric only ever carries whole flits. The two
      directions are not symmetric.
    </p>

    <SpecTable :cols="widthCols" :rows="widthRows" />

    <Callout
      kind="trap"
      title="An undersized response FIFO does not overflow. It hangs."
    >
      <p>
        A read reserves its entire response before injecting, so the response
        FIFO is sized in
        <b>flits, not beats</b>: <code>RSP_DEPTH</code> must cover
        <code>MAXB × SUB</code>. A 512-bit master over a 256-bit fabric needs
        128 and <b>the obvious value of 64 is silently fatal</b> —
        <code>ar_ok</code> never asserts and the port hangs with no error.
        <code>sb_nmu</code> clamps rather than trusting the parameter.
      </p>
      <p>
        Before that clamp, the <code>SB_FW256</code> configuration hung with no
        error. It is the configuration the results recommend, so the clamp is
        load-bearing and not a defensive nicety.
      </p>
    </Callout>

    <Callout
      kind="trap"
      title="512 wrong answers, elaborating and synthesising cleanly"
    >
      <p>
        <code>SB_WIDE512</code> — a slave wider than the flit — once returned
        512 wrong answers while elaborating, synthesising and simulating
        <b>without a warning</b>, because the scatter path drove a wide slave
        from a single narrow flit and replicated read data back. It now fails to
        elaborate at all. Such a slave needs an
        <code>axi_dwidth_converter</code> in front of it, or
        <code>FW</code> raised to its width.
      </p>
    </Callout>

    <Callout
      kind="measured"
      title="The conversion cost lands exactly where conversion happens"
    >
      <p>
        Pricing the converting shims against a pass-through revision at the
        deployed configuration: <b>+4,472 LUT and +4,530 FF</b> over the whole
        line. Per manager shim, the 512-bit NMU — a splitter, whose path did not
        change — is <b>971 LUTs in both columns</b>; the 64-bit AXI4 NMU goes
        1,146 → 2,135 and the 32-bit Lite NMU 1,108 → 1,979. The matched-width
        256-bit NSU moves 635 → 681, because a port needing no conversion has
        its conversion registers pruned at synthesis. The links are untouched
        because the flits they carry are unchanged.
      </p>
      <p class="kt-text-caption">
        Both columns are the same <code>sb_bd_line4</code> at FW=256, AW=43,
        block-RAM FIFOs, fabric 400 MHz / AXI ports 300 MHz, differing only in
        the shim RTL. Worst setup slack +0.185 ns against +0.031, so the
        conversion logic does not set the critical path.
      </p>
    </Callout>

    <h2 class="doc-h2">An AXI4-Lite converter at every Lite port</h2>
    <p class="doc-p">
      A port declared Lite in the generator is emitted as a true AXI4-Lite
      interface — no ID signals, the AXI4-only fields tied to the constants Lite
      implies — so a MIG's
      <code>C0_DDR4_S_AXI_CTRL</code> or a <code>clk_wiz</code> register port
      connects directly, with no vendor protocol-converter IP and none of the
      1×1 SmartConnect shims the tree needed. The conversion is
      <code>sb_axi2lite</code>, inside <code>sb_nsu_lite</code>: the deployed
      wrapper carries eight of them, two Lite subordinates per station across
      four stations, all at <code>DW=32, AW=43, IDW=4</code>.
    </p>

    <WaveTrace
      v-bind="liteBroken"
      variant="broken"
      label="terminate the burst signals — what a Lite port cannot survive"
    />
    <WaveTrace
      v-bind="liteFixed"
      variant="fixed"
      label="sb_axi2lite walks the burst"
    />

    <Callout
      kind="note"
      title="Why the NSU cannot simply pass the burst through"
    >
      <p>
        <code>sb_nsu</code> re-expresses fabric flits as <code>SDW</code>-wide
        bursts, and <b>a Lite slave cannot see AWLEN</b>. So the converter walks
        the burst itself, with single acceptance per direction and the write and
        read channels running independently.
      </p>
      <p class="kt-text-caption">
        <code>src/kohakuaccel/axi/station/sb_axi2lite.v</code> and
        <code>sb_nsu_lite.v</code>. On the manager side a Lite master is an AXI4
        manager with LEN=0, SIZE=full width, BURST=INCR and no ID, so
        <code>sb_nmu_lite</code> is <code>sb_nmu</code> with constants — the
        constants propagate, the tag table collapses to a flag, and the burst
        counters disappear.
      </p>
    </Callout>

    <h2 class="doc-h2">What the line measures, against the tree it replaced</h2>
    <p class="doc-p">
      The station line is what is built. The SmartConnect rows below are the
      <b>superseded baseline</b> — the tree it replaced, kept because the
      comparison is the argument for the structure.
    </p>
    <ResourceBars
      :items="totals"
      unit="CLB LUT sites"
      caption="xcvu13p-fhgb2104-2L-e, Vivado 2024.2, out-of-context SYNTHESIS. The line figures are the per-instance breakdown of ONE synthesis of the ship recipe — FW=256, AW=43, NQ=4, LINK_FULL=0, LINK_CDC=1, block-RAM FIFOs, outstanding 4 / 8 / 2, the control manager placing — not a per-die extrapolation. The SmartConnect column is the superseded tree"
    />

    <SpecTable
      :cols="perDieCols"
      :rows="perDieRows"
      caption="The station line as built (the ship recipe, scripts/tcl/ooc_line_d2.tcl), against the SmartConnect tree it replaced; the line rows sum to the netlist exactly. The earlier no-block-RAM configuration measured 22,106 LUT / 48,167 FF — 3.70× overall, 4.77× on SLR1 — and is the row the flit-width and address-width knobs below quote. That baseline is NOT uniformly v5 — the rows say which run each figure came from, and those IPs were never re-synthesised for v5, so it is of mixed provenance and must not be quoted as a clean number"
    />

    <Callout
      kind="trap"
      title="Take vendor baselines from the vendor's own OOC report, never a reconstruction"
    >
      <p>
        How far a rebuild reads low depends on what is attached to it:
        <code>axi_vip</code>
        slaves gave 12,481 against the real 41,788 (<b>3.3×</b>), and real
        <code>axi_bram_ctrl</code> endpoints with declared widths and
        frequencies gave 21,885 (<b>1.9×</b>). Neither reaches the instance that
        actually shipped in v5, because neither reproduces its address ranges,
        ID widths and protocol conversions.
      </p>
      <p>
        Worse,
        <b
          >the four-clock SmartConnect rows do not contain four clock
          domains.</b
        >
        3×9 at one clock and at four report the <i>same</i> 4,522 LUTRAM, and an
        added asynchronous domain cannot be free in a structure whose crossings
        are LUTRAM FIFOs. Use a reconstruction to compare <i>shapes</i> against
        each other, and the vendor's own report for the instance it replaced.
      </p>
    </Callout>

    <Callout kind="trap" title="CONFIG.XBAR_DATA_WIDTH silently defaults to 32">
      <p>
        It is not inferred from the connected ports, and
        <b
          >the tell of that failure is the two strategies coming out the wrong
          way round</b
        >
        — max-performance measuring less than minimum-area. Set it explicitly
        and assert the readback: a vendor IP that silently accepts a
        configuration it did not apply produces a number that describes a
        different design.
      </p>
    </Callout>

    <SpecTable
      :cols="sllCols"
      :rows="sllRows"
      caption="Cross-SLR wires: 23,040 SLLs per boundary, shared between both directions. The derivation is good to about 2%, and the asymmetry is the expected one — requests leave the master station, responses return"
    />

    <Callout kind="measured" title="Placed and routed, not just synthesised">
      <p>
        The same line in a block design on
        <code>xcvu13p-fhgb2104-2L-e</code> with Vivado 2024.2, one pblock per
        SLR with the links deliberately unpinned, driven by three JTAG masters
        into sixteen block-RAM endpoints: <b>WNS +0.018 ns</b>, TNS 0.000 with 0
        failing of 152,262 endpoints, WHS +0.010 ns, pulse width +1.125 ns,
        <code>write_bitstream</code> reached. Ten clocks, each with a real
        <code>create_clock</code> or MMCM-derived constraint.
        <b>This is the one placed-and-routed result on this page</b>, and it is
        the fabric with block-RAM endpoints rather than the finished system.
      </p>
      <p>
        Latency by hop, 32-bit single-beat read at a 100 MHz control clock:
        <b>21</b> cycles to the local station, <b>27</b> and <b>28</b> one hop
        away, <b>31</b> two hops away. Station 0 and station 2 are both one hop
        and differ by a cycle because each station runs its own fabric clock;
        <b>the crossing, not the hop, sets the cost</b>.
      </p>
      <p class="kt-text-caption">
        The endpoints there are block RAM, not the mesh, so this measures the
        fabric and its floorplan rather than the finished system. Everything
        above the placement is out-of-context synthesis, which bounds what the
        RTL can do and nothing about a routed design.
      </p>
    </Callout>

    <Callout
      kind="note"
      title="Sustained write bandwidth is a floor, not a ceiling"
    >
      <p>
        Eight consecutive maximum-length bursts from the 512-bit manager, one at
        a time waiting for <code>B</code>: 4.97 GB/s to the local station, 4.06
        and 3.38 one hop away, 3.52 two hops away, at FW=256. At one outstanding
        burst roughly half the elapsed time is turnaround, so the figure is
        <b>latency-bound rather than width-bound</b>. Doubling the flit width
        returns 1.35× here while costing 1.39× the LUT, because only the
        transfer half of each burst scales. Nothing here measures a pipelined
        master.
      </p>
    </Callout>

    <h2 class="doc-h2">The values it ships with, and what forced each</h2>
    <SpecTable
      :cols="chooseCols"
      :rows="chooseRows"
      caption="These are the settings the deployed line carries, not an open menu. Each was forced by a measurement or by the port set and address map the replacement inherited"
    />

    <Callout
      kind="measured"
      title="The ship recipe, per port: 23,053 LUT for the bus, 8,043 on the manager station"
    >
      <p>
        The line as the block design builds it today — block-RAM FIFOs,
        outstanding 4 / 8 / 2 on the JTAG, XDMA and control managers, the
        control manager placing rather than packing — measures
        <b>23,053 LUT, 42,223 FF, 90 BRAM</b>, one synthesis via
        <code>scripts/tcl/ooc_line_d2.tcl</code>. The station every manager
        crosses is <b>8,043</b>: hub set 2,122; the 64-bit and 512-bit managers
        1,158 and 967; the 32-bit control manager 609; the 512-bit subordinate
        760; each 32-bit subordinate 808–811. A leaf station's hub set is 1,215
        and a link pair 431.
      </p>
      <p>
        Three facts set those figures. The hub's select is a case on the binary
        grant and sits at one LUT per payload bit — indexing by
        <code>sel × PW</code> is a barrel shifter (+6,263 over the bus) and a
        <code>keep</code> on the selected payload adds a stage (+4,054). A
        subordinate whose 4 KB bound fits one burst (<code>NSPM ≤ 1</code>,
        every port ≥ 128 bits) has no splitter — a load-and-hold instead of a
        40-bit adder, −115 per wide port. Lane and slice writes are per-lane
        enables, never a variable part-select assignment — −190 on the JTAG
        manager, −45 on XDMA.
      </p>
      <p>
        Two knobs exist for configurations the ship does not have:
        <code>SAME_CLK</code> makes a shim's request and response queues
        synchronous when the port is on the station's fabric clock, and
        <code>SINGLE_BEAT</code> (<code>NSB</code> at the line) replaces a
        subordinate's three depth-16 channel queues with skids and folds the
        slice walk to constants — <b>−318 LUT per port</b>, verified on a Lite
        endpoint. The ship's 32-bit subordinates take bursts, so it runs with
        <code>NSB = 0</code>.
      </p>
    </Callout>

    <Callout
      kind="trap"
      title="A conservative buffer floor hides where the area goes"
    >
      <p>
        The 4 KB rule permits 256 beats on a 32-bit port, so clamping every port
        to its theoretical maximum gives each control shim a 256-deep response
        FIFO: <b>+5,013 LUTs</b> on the 3×9 station and +2,788 on the
        four-station line, for bursts those ports never issue. Before the
        control ports declared themselves single-beat, each measured ~3,880 —
        four times its real cost, and
        <b>indistinguishable from the 512-bit port</b>.
      </p>
    </Callout>

    <Callout kind="trap" title="Never index a payload by a variable bit offset">
      <p>
        <code>i_pay[sel*PW +: PW]</code> builds a barrel shifter: measured
        <b>14,632 LUT</b> for two hubs against ~1,700 for a constant-offset mux
        with an equality compare. This is an FPGA-only divergence — on an ASIC
        both forms are the same one-hot pass-gate mux.
      </p>
    </Callout>

    <Callout
      kind="trap"
      title="Vivado merges your per-shim reset copies back into one net"
    >
      <p>
        Each shim registers a local <code>bus_rst</code> copy to break fanout,
        and Vivado merged them all back because the flops were identical;
        <code>(* dont_touch *)</code> pins them. Read the reset census from the
        <b>netlist</b>, never the RTL — on the 3×9 station 11,836 of 25,884
        flops carry a reset and essentially all of them are inside XPM's FIFO
        output stage, which resets because <code>DOUT_RESET_VALUE</code> makes
        XPM guarantee <code>dout == 0</code>, not something RTL can decline.
      </p>
      <p>
        Judge reset work by LUT and control sets —
        <b>never by OOC Fmax, which cannot show a reset-fanout gain</b>.
      </p>
    </Callout>

    <h2 class="doc-h2">Verification</h2>
    <SpecTable
      :cols="checkCols"
      :rows="checkRows"
      caption="sb_line4_tb, re-measured 2026-08-21, every row run in one sitting. Every run carries 3 master-side and 16 slave-side protocol monitors"
    />

    <Callout
      kind="rule"
      title="Three of the seven items get skipped, and they are the ones that matter"
    >
      <p>
        Deadlock stress (every master to one slave, max outstanding, full
        backpressure, run until every buffer is full, then release); decode (an
        unmapped address returns
        <code>DECERR</code> and injects <b>zero flits</b> — assert the flit
        count, not just the response); and credit exhaustion (credits at 1;
        correctness must not depend on count).
      </p>
    </Callout>

    <Callout
      kind="trap"
      title="Two trees hold their own copies of these modules, and only one of them says so"
    >
      <p>
        <code>tests/axi/build-jtagdbg/rtl/</code> is a frozen pre-fix snapshot
        of the whole station bus — <code>sb_line4</code>, <code>sb_nmu</code>,
        <code>sb_axi2lite</code> and the rest. That one is <b>deliberate</b>,
        and its two real differences from the live RTL are named in that
        directory's own runner, which is what makes it a measurement of
        something rather than a measurement of nothing.
      </p>
      <p>
        <code>src/reference/poc/</code> is not. It holds proof-of-concept copies
        of framework modules — a second <code>noc_cu_base</code> among them —
        with no note saying how they differ, and nothing compiles them.
        <b
          >A harness carrying its own undeclared copy of the module under test
          is the one arrangement guaranteed to produce numbers that describe
          nothing</b
        >, so before quoting any figure, check which tree it was built from.
      </p>
    </Callout>

    <h2 class="doc-h2">Where it does not help, and when not to build it</h2>
    <Callout
      kind="measured"
      title="The saving lands where there was already room"
    >
      <p>
        SLR1 was the <b>least</b> utilised die in the placed v5 design — 88.61%
        CLB against SLR0's 95.49%, and 45% DSP against 79.6% — because in that
        build it carried the interconnect but the smallest share of mesh.
        Replacing the tree freed about 33,000 LUTs there, and XDMA at one
        channel per direction would free roughly 17,000 more, but it does not
        relieve the binding constraint, which is SLR0.
      </p>
      <p>
        Every die also sits at 88–95% CLB occupancy while using 61–69% of its
        LUTs, so the design is <b>packing-bound</b> and a LUT saving converts
        into placeable sites only as well as the placer packs what remains.
      </p>
    </Callout>

    <Callout kind="rule" title="The case for it is heterogeneity, not size">
      <p>
        Strip the heterogeneity away and the margin collapses. The single-die
        3×9 comparison wins <b>0.72×</b> on LUTs and <b>loses</b> on flip-flops
        (1.15×), while the four-die replacement wins 3.55× overall and 7.6× on
        the inter-die tissue alone. If a design has one clock, one width and one
        die, the interesting number is 0.72× and it is not worth new RTL.
      </p>
      <p>
        Also not worth it if flip-flops are the scarce resource: the station bus
        registers every station output by construction, and the die-crossing
        pipelines are flops by design — six link instances carry 11,486 of the
        deployed line's 42,223 flip-flops.
      </p>
    </Callout>

    <Callout kind="open" title="Still open">
      <p>
        <b>Multicast writes</b> are nearly free in a broadcast-ejection station
        and the invariant break has a known fix, but they are
        <b>not built — nothing issues one</b>, and adding a deadlock-relevant
        path with no test that exercises it is worse than not having the
        feature. <b>The config port's own path</b>: a config port reachable only
        through the fabric cannot debug a hung fabric. <b>Power</b>: a routed
        5.440 W estimate exists but it is vectorless at Medium confidence with
        no traffic behind it, and no counterpart exists for the tree.
        <b
          >No latency or throughput was ever measured for the tree it
          replaced</b
        >, so the comparison is resources only and will stay that way.
      </p>
    </Callout>

    <h2 class="doc-h2">Fixed protocol, addon, convention, or yours</h2>
    <SpecTable :cols="catCols" :rows="catRows" />

    <h2 class="doc-h2">What the station bus does not own</h2>
    <SpecTable :cols="notOwnedCols" :rows="notOwnedRows" />
  </DocPage>
</template>
