<script setup>
/* Every measured KohakuTPU figure, with its conditions.
 *
 * PROVENANCE. The device is xcvu13p-fhgb2104-2L-e and the tool is Vivado
 * 2024.2 for every row unless a row says otherwise. Almost everything here is
 * OUT-OF-CONTEXT SYNTHESIS: nothing is placed and the route is estimated. The
 * placed rows say so explicitly and are the only ones that are.
 *
 * NO FREQUENCY ON THIS PAGE IS A CLOSED-TIMING FIGURE. Every one is a
 * synthesis estimate against a stated target, and synthesis slack is
 * optimistic — one module in this tree lost 0.740 ns from synthesis to
 * routing. Rates in GFLOP/s are measured CYCLE COUNTS multiplied by a NOMINAL
 * 300 MHz; the cycles are the measurement and the clock is an assumption. */

const reading = {
  cols: [
    { key: "w", label: "" },
    { key: "v", label: "" },
  ],
  rows: [
    {
      w: "<b>utilisation</b>",
      v: "reliable — LUT, FF, BRAM, URAM and DSP counts are what the netlist contains",
      _tone: "good",
    },
    {
      w: "<b>frequency</b>",
      v: "an <b>upper bound</b>. It answers “is the logic deep enough to fail?”, never “will it place”",
      _tone: "warn",
    },
    {
      w: "a run that <b>met</b> its target",
      v: "a <b>lower bound</b> on that block's frequency: it cleared the constraint with the reported slack and the tool stopped trying",
    },
    {
      w: "a run that <b>missed</b>",
      v: "a <b>ceiling</b>: that is what the logic would do and it was not enough",
    },
  ],
};

/* The part. */
const device = {
  cols: [
    { key: "r", label: "resource" },
    { key: "s", label: "per SLR", mono: true, align: "right" },
    { key: "d", label: "device", mono: true, align: "right" },
  ],
  rows: [
    { r: "CLB LUT", s: "432,000", d: "1,728,000" },
    { r: "CLB FF", s: "864,000", d: "3,456,000" },
    { r: "BRAM36", s: "672", d: "2,688" },
    { r: "URAM288", s: "320", d: "1,280" },
    { r: "DSP48E2", s: "3,072", d: "12,288" },
    { r: "clock regions", s: "32 (8 wide x 4 tall)", d: "128" },
    { r: "Laguna sites", s: "3,840 end dies, 7,680 middle", d: "23,040" },
  ],
};

const crossing = {
  cols: [
    { key: "k", label: "" },
    { key: "v", label: "", mono: true },
  ],
  rows: [
    { k: "boundaries", v: "3" },
    {
      k: "SLLs per boundary",
      v: "23,040, <b>shared between both directions</b>",
    },
    {
      k: "measured crossing delay",
      v: "<b>0.755 ns</b> (0.096 clock-to-Q + 0.659 SLL route), -2L",
    },
    { k: "latency", v: "1 cycle, transmit register to receive register" },
    {
      k: "at 300 MHz",
      v: "the crossing alone is about <b>23% of the period</b>",
    },
  ],
};

const channels = {
  cols: [
    { key: "c", label: "channel", mono: true },
    { key: "s", label: "SLR", mono: true },
    { key: "n", label: "notes" },
  ],
  rows: [
    { c: "ddr4_c0", s: "SLR3", n: "" },
    { c: "ddr4_c1", s: "SLR2", n: "the single-mesh design on the card today" },
    { c: "ddr4_c2", s: "SLR0", n: "" },
    {
      c: "ddr4_c3",
      s: "SLR1",
      n: "<b>XDMA/PCIe is also here</b>",
      _tone: "warn",
    },
  ],
};

/* §2 the matmul path. */
const matmulPath = {
  cols: [
    { key: "b", label: "block", mono: true },
    { key: "l", label: "LUT", mono: true, align: "right" },
    { key: "f", label: "FF", mono: true, align: "right" },
    { key: "r", label: "BRAM36", mono: true, align: "right" },
    { key: "d", label: "DSP", mono: true, align: "right" },
    { key: "m", label: "Fmax", mono: true, align: "right" },
    { key: "n", label: "bound" },
  ],
  rows: [
    {
      b: "<code>mx_mac</code> (one DSP48E2)",
      l: "<b>0</b>",
      f: "<b>0</b>",
      r: "0",
      d: "1",
      m: "—",
      n: "—",
      _tone: "good",
    },
    {
      b: "<code>mx_tcu</code> (4x8x4) ‡",
      l: "336",
      f: "790",
      r: "0",
      d: "64",
      m: "1072.6",
      n: "lower",
    },
    {
      b: "<code>mx_cluster</code> (core + exact accumulator) ‡",
      l: "4,751",
      f: "4,789",
      r: "0",
      d: "256",
      m: "353.6",
      n: "lower",
    },
    {
      b: "<code>mx_acu_fp</code> (FP22, MW=14, DEPTH=16, block RAM)",
      l: "9,901",
      f: "5,585",
      r: "5",
      d: "48",
      m: "<b>343.4</b>",
      n: "lower",
    },
    {
      b: "<code>mx_cluster_cu</code> (one-port cluster, current)",
      l: "15,306",
      f: "17,754",
      r: "5",
      d: "<b>304</b>",
      m: "<b>346.6</b>",
      n: "lower",
      _tone: "good",
    },
    {
      b: "<code>mx_matmul_cu</code> (single-port baseline) ‡",
      l: "12,973",
      f: "11,486",
      r: "5",
      d: "256",
      m: "306.4",
      n: "lower",
    },
    {
      b: "<code>mx_cluster_cu</code> <b>in the shape that ships</b>",
      l: "16,390",
      f: "18,404",
      r: "35",
      d: "304",
      m: "<b>344.3</b>",
      n: "lower",
      _tone: "good",
    },
  ],
};

const clusterLuts = [
  {
    label: "mx_mac x256 — the whole MAC array",
    value: 0,
    note: "LUT · and 0 FF, 256 DSP",
    tone: "good",
  },
  { label: "TCU 0", value: 336, note: "224 SRL" },
  { label: "TCU 1", value: 448, note: "280 SRL" },
  { label: "TCU 2", value: 476, note: "280 SRL" },
  { label: "TCU 3", value: 476, note: "280 SRL" },
  { label: "operand delay (top)", value: 450, note: "394 SRL" },
  {
    label: "accumulator (exact variant)",
    value: 2565,
    note: "54% of the cluster, and the critical path",
    tone: "accent",
  },
];

const shiftTrade = {
  cols: [
    { key: "w", label: "" },
    { key: "m", label: "Fmax", mono: true, align: "right" },
    { key: "l", label: "LUT", mono: true, align: "right" },
    { key: "f", label: "FF", mono: true, align: "right" },
    { key: "d", label: "DSP", mono: true, align: "right" },
  ],
  rows: [
    {
      w: "<code>mx_acu_fp</code>, barrel shifter in fabric",
      m: "327.7",
      l: "10,616",
      f: "5,928",
      d: "16",
    },
    {
      w: "<code>mx_acu_fp</code>, shift as a DSP multiply",
      m: "<b>343.4</b>",
      l: "<b>9,901</b>",
      f: "<b>5,585</b>",
      d: "48",
      _tone: "good",
    },
    {
      w: "<code>mx_cluster_cu</code>, in fabric",
      m: "325.6",
      l: "17,629",
      f: "17,782",
      d: "272",
    },
    {
      w: "<code>mx_cluster_cu</code>, as a DSP multiply",
      m: "<b>346.6</b>",
      l: "<b>15,306</b>",
      f: "<b>17,754</b>",
      d: "304",
      _tone: "good",
    },
    {
      w: "16 copies of one variable shift — fabric barrel shifter",
      m: "—",
      l: "1,200",
      f: "704",
      d: "0",
    },
    {
      w: "16 copies of one variable shift — multiply by a one-hot",
      m: "—",
      l: "<b>288</b>",
      f: "496",
      d: "16",
      _tone: "good",
    },
  ],
};

const outputScale = {
  cols: [
    { key: "w", label: "" },
    { key: "m", label: "Fmax", mono: true, align: "right" },
    { key: "l", label: "LUT", mono: true, align: "right" },
    { key: "d", label: "DSP", mono: true, align: "right" },
  ],
  rows: [
    { w: "feature off", m: "343.4", l: "9,821", d: "48", _tone: "good" },
    { w: "feature on", m: "330.7", l: "10,673", d: "48" },
    {
      w: "always present, scale at 1.0 — bit-identical, <b>not cost-identical</b>",
      m: "330.7",
      l: "10,297",
      d: "48",
      _tone: "bad",
    },
  ],
};

const acuHistory = {
  cols: [
    { key: "s", label: "step" },
    { key: "m", label: "Fmax", mono: true, align: "right" },
    { key: "l", label: "LUT", mono: true, align: "right" },
    { key: "f", label: "FF", mono: true, align: "right" },
  ],
  rows: [
    {
      s: "unpipelined: normalise + add in one cycle",
      m: "84.7",
      l: "13,037",
      f: "—",
      _tone: "bad",
    },
    {
      s: "split <code>normalise | add</code>",
      m: "129.7",
      l: "11,787",
      f: "—",
    },
    {
      s: "narrow the normaliser input 30 → 22 bits",
      m: "136.3",
      l: "11,185",
      f: "—",
    },
    {
      s: "split the adder <code>align | round</code>, 2 banks",
      m: "217.5",
      l: "13,912",
      f: "—",
    },
    {
      s: "move the add across the align/round seam",
      m: "208.6",
      l: "14,344",
      f: "—",
      _tone: "warn",
    },
    {
      s: "split the normaliser <code>leading-one | assemble</code>",
      m: "233.7",
      l: "13,654",
      f: "17,497",
    },
    {
      s: "resident tile as LUTRAM, load mux off the tail",
      m: "219.7",
      l: "11,086",
      f: "5,210",
      _tone: "warn",
    },
    {
      s: "split the round stage, 3 banks",
      m: "242.4",
      l: "11,091",
      f: "6,724",
    },
    {
      s: "register the align-stage selects",
      m: "238.9",
      l: "11,263",
      f: "6,744",
      _tone: "warn",
    },
    {
      s: "parallel leading-one and sticky",
      m: "234.3",
      l: "11,708",
      f: "6,744",
      _tone: "warn",
    },
    {
      s: "one-level operand mux, zero-ness as control",
      m: "293.2",
      l: "11,310",
      f: "6,764",
    },
    {
      s: "concatenated <code>{exp,mant}</code> compare",
      m: "302.3",
      l: "11,369",
      f: "6,758",
    },
    {
      s: "explicit BRAM tile, 4 banks, <code>READ_LAT=1</code>",
      m: "241.2",
      l: "10,469",
      f: "6,310",
      _tone: "warn",
    },
    {
      s: "<b>explicit BRAM tile, 1 bank, <code>READ_LAT=2</code></b>",
      m: "<b>349.4</b>",
      l: "<b>9,945</b>",
      f: "<b>6,232</b>",
      _tone: "good",
    },
  ],
};

const tileMemory = {
  cols: [
    { key: "w", label: "tile memory" },
    { key: "l", label: "LUT", mono: true, align: "right" },
    { key: "b", label: "BRAM", mono: true, align: "right" },
    { key: "m", label: "Fmax", mono: true, align: "right" },
  ],
  rows: [
    {
      w: "inferred LUTRAM, 3 banks, async read",
      l: "11,049",
      b: "0",
      m: "312.3",
    },
    {
      w: "explicit BRAM, 4 banks, <code>READ_LAT=1</code>",
      l: "10,469",
      b: "20",
      m: "241.2",
      _tone: "bad",
    },
    {
      w: "explicit BRAM, 1 bank, <code>READ_LAT=2</code>",
      l: "<b>9,945</b>",
      b: "5",
      m: "<b>349.4</b>",
      _tone: "good",
    },
    {
      w: "the same 352-bit memory standing alone",
      l: "—",
      b: "—",
      m: "<b>837</b>",
      _tone: "good",
    },
  ],
};

const configFigures = {
  cols: [
    { key: "w", label: "" },
    { key: "l", label: "LUT", mono: true, align: "right" },
    { key: "m", label: "Fmax", mono: true, align: "right" },
  ],
  rows: [
    { w: "shift amount declared 8 bits", l: "6,109", m: "396.1" },
    {
      w: "shift amount clamped to 5 bits",
      l: "<b>4,751</b>",
      m: "353.6",
      _tone: "good",
    },
    {
      w: "operand buffer written with a variable part-select",
      l: "45,956",
      m: "273.7",
      _tone: "bad",
    },
    {
      w: "the same loop unrolled so each index is constant",
      l: "<b>13,664</b>",
      m: "<b>292.9</b>",
      _tone: "good",
    },
  ],
};

/* §3 the vector path. */
const vectorLane = {
  cols: [
    { key: "k", label: "" },
    { key: "m", label: "measured", mono: true, align: "right" },
    { key: "e", label: "estimated beforehand", mono: true, align: "right" },
  ],
  rows: [
    {
      k: "<b>Fmax</b>",
      m: "<b>324.8 MHz</b> (WNS +0.147 ns at a 310 MHz target) — lower bound",
      e: "—",
      _tone: "good",
    },
    { k: "LUT", m: "<b>1,249</b>", e: "~750" },
    { k: "FF", m: "<b>705</b>", e: "—" },
    { k: "DSP", m: "<b>3</b>", e: "3" },
    { k: "BRAM / URAM", m: "<b>0</b>", e: "0" },
    { k: "latency", m: "<b>14 cycles, II = 1</b>", e: "—" },
  ],
};

const vecShrink = [
  {
    label: "vec_lanes — before",
    value: 37916,
    note: "0 BRAM · 48 DSP · 358.4 MHz",
  },
  {
    label: "vec_lanes — after",
    value: 24683,
    note: "40 tiles · 48 DSP · 358.4 MHz  (−34.9%)",
    tone: "good",
  },
  {
    label: "vec_cu — before",
    value: 48415,
    note: "4 BRAM · 51 DSP · 336.8 MHz",
  },
  {
    label: "vec_cu — after",
    value: 35629,
    note: "44 tiles · 51 DSP · 358.4 MHz  (−26.4%)",
    tone: "good",
  },
];

const shrinkSteps = {
  cols: [
    { key: "c", label: "change" },
    { key: "l", label: "LUT", mono: true, align: "right" },
  ],
  rows: [
    {
      c: "operand network (phase window + constant indices)",
      l: "<b>−3,404</b>",
      _tone: "good",
    },
    { c: "coefficient ROMs to block RAM", l: "<b>−3,575</b>", _tone: "good" },
    {
      c: "register file to block RAM",
      l: "−3,352, of which <b>+1,129 came back</b>",
      _tone: "warn",
    },
    { c: "predicate write-back", l: "−1,987", _tone: "good" },
    { c: "stage-0 narrowing", l: "−1,256", _tone: "good" },
    { c: "write crossbar", l: "−1,089", _tone: "good" },
    { c: "lane rotate", l: "−565", _tone: "good" },
    {
      c: "fused exp-and-sum leaf write-back",
      l: "+249, but <b>+42 in <code>vec_cu</code></b>",
    },
  ],
};

/* §4 blocks measured in this ship. */
const shipBlocks = {
  cols: [
    { key: "b", label: "block", mono: true },
    { key: "l", label: "LUT", mono: true, align: "right" },
    { key: "f", label: "FF", mono: true, align: "right" },
    { key: "r", label: "BRAM", mono: true, align: "right" },
    { key: "d", label: "DSP", mono: true, align: "right" },
    { key: "m", label: "Fmax", mono: true, align: "right" },
    { key: "n", label: "note" },
  ],
  rows: [
    {
      b: "<code>mx_quant</code> (the MXFP7 quantiser)",
      l: "4,267",
      f: "—",
      r: "0",
      d: "32",
      m: "<b>400.6</b>",
      n: "310 MHz-target run; it is KohakuTPU's, on the memory-agent side — <b>not</b> in any cluster figure above",
    },
    {
      b: "<code>mag_mem_port</code>",
      l: "—",
      f: "—",
      r: "—",
      d: "—",
      m: "330.0",
      n: "",
    },
    {
      b: "<code>NoCRouter</code>",
      l: "3,281",
      f: "—",
      r: "—",
      d: "—",
      m: "<b>≥450</b>",
      n: "2.5 ns with +0.278 ns slack, 7 logic levels",
    },
    {
      b: "router (earlier run)",
      l: "—",
      f: "—",
      r: "—",
      d: "—",
      m: "406",
      n: "452 for two routers linked",
    },
    {
      b: "output port switch",
      l: "—",
      f: "—",
      r: "—",
      d: "—",
      m: "644",
      n: "",
    },
    { b: "input port switch", l: "—", f: "—", r: "—", d: "—", m: "732", n: "" },
    {
      b: "<code>noc_orchestrator</code>",
      l: "2,563",
      f: "2,465",
      r: "0",
      d: "0",
      m: "570.0",
      n: "300 MHz-target run",
    },
    {
      b: "<code>axi_n1</code> (N=4)",
      l: "955",
      f: "—",
      r: "—",
      d: "—",
      m: "604",
      n: "replaces a vendor interconnect measured at 43,714 LUT at the root",
    },
    {
      b: "352-bit block memory, standing alone",
      l: "—",
      f: "—",
      r: "—",
      d: "—",
      m: "<b>837</b>",
      n: "",
    },
    {
      b: "352 x 4096 in URAM, standing alone",
      l: "—",
      f: "—",
      r: "—",
      d: "—",
      m: "<b>585</b>",
      n: "",
    },
  ],
};

/* §5 device level. */
const scaling = {
  cols: [
    { key: "r", label: "" },
    { key: "p", label: "per cluster", mono: true, align: "right" },
    { key: "a", label: "x32", mono: true, align: "right" },
    { key: "b", label: "x40", mono: true, align: "right" },
    { key: "d", label: "of device (x40)", mono: true, align: "right" },
  ],
  rows: [
    { r: "LUT", p: "16,390", a: "524,480", b: "655,600", d: "<b>37.9%</b>" },
    { r: "FF", p: "18,404", a: "588,928", b: "736,160", d: "21.3%" },
    {
      r: "BRAM36",
      p: "35",
      a: "1,120",
      b: "1,400",
      d: "<b>52.1%</b>",
      _tone: "warn",
    },
    {
      r: "DSP",
      p: "<b>304</b>",
      a: "9,728",
      b: "12,160",
      d: "<b>99.0%</b>",
      _tone: "bad",
    },
    { r: "URAM", p: "0", a: "0", b: "0", d: "0%" },
    { r: "mesh ports", p: "1", a: "32", b: "40", d: "—" },
    { r: "MACs/cycle", p: "512", a: "16,384", b: "20,480", d: "—" },
  ],
};

const hostIp = [
  {
    label: "XDMA",
    value: 76319,
    note: "72,059 FF · 124 BRAM · 17.7% of an SLR",
    tone: "bad",
  },
  { label: "smartconnect_0_0", value: 20104, note: "29,602 FF" },
  { label: "axi_smc_0", value: 19709, note: "30,115 FF" },
  { label: "DDR4 MIG", value: 19944, note: "21,263 FF · 25.5 BRAM · 3 DSP" },
  { label: "JTAG-AXI", value: 867, note: "2,300 FF · 4 BRAM" },
  { label: "AXI GPIO", value: 62, note: "" },
];

const placed = {
  cols: [
    { key: "k", label: "" },
    { key: "v", label: "", mono: true },
  ],
  rows: [
    { k: "URAM", v: "<b>120 of 1,280 — 9.38%</b>" },
    { k: "the most crowded die", v: "<b>95.80% CLB</b>", _tone: "warn" },
    { k: "another die", v: "93.6% CLB" },
    { k: "SLL use, one boundary", v: "2,765 of 23,040 (12.0%)" },
    { k: "SLL use, another", v: "1,355 (5.9%)" },
    { k: "SLL use, the third", v: "none" },
  ],
};

/* §6 accuracy. */
const aluChecks = {
  cols: [
    { key: "w", label: "what", mono: true },
    { key: "r", label: "result" },
  ],
  rows: [
    {
      w: "mov neg abs max min select cmp",
      r: "<b>bit exact</b>",
      _tone: "good",
    },
    {
      w: "products and sums of powers of two",
      r: "<b>bit exact</b>",
      _tone: "good",
    },
    {
      w: "a*b - a*b, x - x  (and x - x is +0)",
      r: "<b>bit exact</b>",
      _tone: "good",
    },
    {
      w: "exp2(k), log2(2^k), inv(2^k), rsqrt(2^even)",
      r: "<b>bit exact</b>",
      _tone: "good",
    },
    {
      w: "<b>FMA</b>, including the full alignment sweep",
      r: "<b>0.500 ulp on this suite</b> — one subtractive corner later moved the known bound to 1 ulp",
    },
    { w: "exp2", r: "0.509 ulp" },
    { w: "inv", r: "0.546 ulp" },
    { w: "rsqrt", r: "0.549 ulp" },
    { w: "log2", r: "0.64x its limit (0.99 ulp or 2^-18 absolute)" },
  ],
};

const tables = {
  cols: [
    { key: "f", label: "function", mono: true },
    { key: "d", label: "domain", mono: true },
    { key: "p", label: "predicted", mono: true, align: "right" },
    { key: "m", label: "measured", mono: true, align: "right" },
    { key: "g", label: "margin over 2^-16", mono: true, align: "right" },
  ],
  rows: [
    {
      f: "exp2(f)",
      d: "[0,1)",
      p: "2^-23.2",
      m: "<b>2^-19.9</b>",
      g: "3.9 bits",
    },
    {
      f: "log2(m)",
      d: "[1,2)",
      p: "2^-21.1",
      m: "<b>2^-19.5</b>",
      g: "3.5 bits",
    },
    {
      f: "inv(m)",
      d: "[1,2)",
      p: "2^-20.0",
      m: "<b>2^-19.4</b>",
      g: "3.4 bits",
    },
    {
      f: "rsqrt(m)",
      d: "[1,2)+[2,4)",
      p: "2^-21.7",
      m: "<b>2^-19.8</b>",
      g: "3.8 bits",
    },
  ],
};

const endToEnd = {
  cols: [
    { key: "b", label: "bench", mono: true },
    { key: "s", label: "shape", mono: true },
    { key: "r", label: "result" },
  ],
  rows: [
    {
      b: "mx_system_tb",
      s: "4x256x4 on a 1x5 mesh",
      r: "worst 3.97e-4 → <b>0.41 FP16 ULP</b>, 35 checks",
    },
    {
      b: "mx_system32_tb",
      s: "32x32x32 on a 1x5 mesh",
      r: "worst 4.86e-4 → <b>0.50 ULP</b>, mean 1.41e-4 → 0.14 ULP over 1,024",
    },
    {
      b: "mx_cluster_node_tb",
      s: "32x32x32, one GEMM",
      r: "2,112 checks, 0.50 ULP worst",
    },
    {
      b: "mag_system_tb",
      s: "16x32x16, agent + 2 clusters",
      r: "257 checks, 0.49 ULP",
    },
  ],
};

/* §8 throughput. */
const baseline = {
  cols: [
    { key: "s", label: "shape M x K x N", mono: true },
    { key: "c", label: "run cycles", mono: true, align: "right" },
    { key: "f", label: "fill", mono: true, align: "right" },
    { key: "g", label: "gemm", mono: true, align: "right" },
    { key: "d", label: "drain", mono: true, align: "right" },
    { key: "i", label: "idle", mono: true, align: "right" },
    { key: "r", label: "GFLOP/s", mono: true, align: "right" },
    { key: "p", label: "% peak", mono: true, align: "right" },
  ],
  rows: [
    {
      s: "64x128x64",
      c: "8,213",
      f: "56.7%",
      g: "17.3%",
      d: "12.1%",
      i: "13.8%",
      r: "38.3",
      p: "6.2%",
    },
    {
      s: "128x128x128",
      c: "32,361",
      f: "61.3%",
      g: "17.1%",
      d: "11.6%",
      i: "9.8%",
      r: "38.9",
      p: "6.3%",
    },
    {
      s: "256x256x256",
      c: "239,786",
      f: "71.9%",
      g: "13.4%",
      d: "6.7%",
      i: "7.9%",
      r: "42.0",
      p: "6.8%",
    },
    {
      s: "512x512x512",
      c: "1,983,413",
      f: "73.0%",
      g: "10.3%",
      d: "3.4%",
      i: "13.3%",
      r: "40.6",
      p: "6.6%",
    },
  ],
};

const current = {
  cols: [
    { key: "n", label: "clusters", mono: true, align: "right" },
    { key: "s", label: "shape M x K x N", mono: true },
    { key: "c", label: "run cycles", mono: true, align: "right" },
    { key: "r", label: "GFLOP/s", mono: true, align: "right" },
    { key: "p", label: "% peak", mono: true, align: "right" },
  ],
  rows: [
    {
      n: "2",
      s: "256x256x256",
      c: "18,701",
      r: "538.3",
      p: "<b>87.6%</b>",
      _tone: "good",
    },
    {
      n: "2",
      s: "256x256x1024",
      c: "72,684",
      r: "554.0",
      p: "<b>90.2%</b>",
      _tone: "good",
    },
    { n: "4", s: "256x256x512", c: "20,647", r: "975.1", p: "79.4%" },
    { n: "4", s: "256x256x1024", c: "41,638", r: "967.0", p: "78.7%" },
    { n: "8", s: "256x256x1024", c: "24,115", r: "1669.7", p: "67.9%" },
    { n: "8", s: "512x256x1024", c: "43,382", r: "1856.3", p: "75.5%" },
  ],
};

const steps = {
  cols: [
    { key: "c", label: "change" },
    { key: "g", label: "GFLOP/s", mono: true, align: "right" },
  ],
  rows: [
    { c: "operands pre-quantised in DRAM", g: "217.4 → 303.9" },
    { c: "banked L1 with a non-blocking fill", g: "303.9 → 362.0" },
    { c: "the drain fused into the sweep's last K block", g: "362.0 → 391.1" },
    { c: "resident tile 64 → 512 sub-tiles", g: "85.1 → 173.4" },
  ],
};

const mistakes = {
  cols: [
    { key: "m", label: "the mistake" },
    { key: "r", label: "what it read" },
    { key: "t", label: "what was true" },
  ],
  rows: [
    {
      m: "reads and writes summed, then added to both mesh directions",
      r: "91.5% “of data movement” — scheduling exhausted",
      t: "the busiest path was <b>28.3%</b>; the missing 30% was <b>latency</b>, and two one-line fixes took 69.6% → 80.7%",
    },
    {
      m: "per-port sums divided by one port",
      r: "<code>mem_rd 101.9%</code>, <code>noc_out 110.4%</code> at 8 clusters — a saturated bus",
      t: "<code>mem_rd 25.5%</code>, <code>noc_out 27.6%</code> against <code>flops 67.9%</code>",
    },
    {
      m: "a stall counter whose <i>event</i> changed meaning",
      r: "one counter went 0.0% → 39.8% at an unchanged cycle count",
      t: "the port now declines flits routinely because it demultiplexes two consumers by type; it counts demultiplexing, not congestion",
    },
  ],
};

/* §9 defects. */
const offsetWrap = {
  cols: [
    { key: "s", label: "shape M x K x N", mono: true },
    { key: "a", label: "p50 before", mono: true, align: "right" },
    { key: "b", label: "over 10% before", mono: true, align: "right" },
    { key: "c", label: "p50 after", mono: true, align: "right" },
    { key: "d", label: "over 10% after", mono: true, align: "right" },
  ],
  rows: [
    {
      s: "64x576x64",
      a: "1.652e-01",
      b: "2,778 of 4,096",
      c: "<b>2.182e-05</b>",
      d: "<b>0</b>",
      _tone: "good",
    },
    {
      s: "64x640x64",
      a: "1.699e-01",
      b: "2,847",
      c: "<b>2.531e-05</b>",
      d: "<b>0</b>",
      _tone: "good",
    },
    {
      s: "64x1024x64",
      a: "1.650e-01",
      b: "2,814",
      c: "<b>2.489e-05</b>",
      d: "<b>0</b>",
      _tone: "good",
    },
    {
      s: "64x1280x64",
      a: "3.016e-05",
      b: "443, max 0.73",
      c: "<b>2.483e-05</b>",
      d: "<b>0</b>",
      _tone: "good",
    },
    {
      s: "77x2048x64",
      a: "6.36e-05",
      b: "1,197 of 4,928, max 1.08",
      c: "<b>2.433e-05</b>",
      d: "<b>0</b>",
      _tone: "good",
    },
    {
      s: "128x640x64",
      a: "1.43e-01",
      b: "5,191 of 8,192",
      c: "<b>2.361e-05</b>",
      d: "<b>0</b>",
      _tone: "good",
    },
  ],
};

/* §10 verification. */
const benches = {
  cols: [
    { key: "b", label: "bench", mono: true },
    { key: "w", label: "what it covers" },
    { key: "c", label: "checks", mono: true, align: "right" },
  ],
  rows: [
    { b: "mx_tcu_tb", w: "one tensor CU, raw packed partials", c: "1,520" },
    { b: "mx_cluster_tb", w: "full cluster, extracted and scaled", c: "4,176" },
    { b: "mx_fp24_tb", w: "accumulator float primitives", c: "13,208" },
    { b: "mx_acu_fp_tb", w: "accumulator ops, resident tile, peer", c: "384" },
    { b: "mx_cluster_node_tb", w: "32x32x32, one GEMM", c: "2,112" },
    { b: "mx_system_tb", w: "4x256x4 through a 1x5 mesh", c: "35" },
    { b: "mx_system32_tb", w: "32x32x32 through a 1x5 mesh", c: "2,051" },
    { b: "mag_system_tb", w: "16x32x16, agent + 2 clusters", c: "257" },
    { b: "mag_driver_tb", w: "up to 256x256x256, tiled by the driver", c: "—" },
    { b: "vec_alu_tb", w: "one vector lane, streamed", c: "26,897" },
    {
      b: "mx_cluster_data_tb",
      w: "unit-to-unit bulk transfer, both directions",
      c: "—",
    },
  ],
};

/* §11 what closed. */
const closed = {
  cols: [
    { key: "b", label: "block", mono: true },
    { key: "r", label: "result" },
  ],
  rows: [
    {
      b: "mx_cluster_cu",
      r: "<b>346.6 MHz</b> current, 304 DSP — lower bound",
      _tone: "good",
    },
    {
      b: "mx_acu_fp",
      r: "<b>343.4 MHz</b> at MW=14 — lower bound",
      _tone: "good",
    },
    {
      b: "vec_alu (one lane)",
      r: "<b>324.8 MHz</b>, WNS +0.147 ns at 310 — lower bound",
      _tone: "good",
    },
    {
      b: "vec_cu (assembled core)",
      r: "<b>358.4 MHz</b> after the shrink — lower bound",
      _tone: "good",
    },
    { b: "mx_quant", r: "<b>400.6 MHz</b> — lower bound", _tone: "good" },
    {
      b: "mm_mesh (agent + cluster + vector core + 2 routers)",
      r: "<b>328.8 MHz</b> — lower bound",
      _tone: "good",
    },
  ],
};

const ceilings = {
  cols: [
    { key: "b", label: "did not close" },
    { key: "r", label: "recorded as a ceiling" },
  ],
  rows: [
    {
      b: "a mesh spanning three SLRs",
      r: "<b>4.6 ns worst path at 98.3% routing with zero logic levels</b> — rejected on measurement, and the reason four independent meshes exist",
      _tone: "bad",
    },
    {
      b: "vec_cu with all three register-file ports in block RAM",
      r: "<b>286.0 MHz</b> against a 300 floor",
      _tone: "bad",
    },
    {
      b: "the accumulator's tile as inferred LUTRAM",
      r: "<b>287.3 MHz</b>, at 22,845 LUT",
      _tone: "bad",
    },
    {
      b: "the quantiser packing a whole entry in one cycle",
      r: "<b>32.5 MHz</b> — 128 parallel barrel shifters, nine times over budget",
      _tone: "bad",
    },
    {
      b: "mx_acu_fp unpipelined",
      r: "<b>84.7 MHz</b> — the starting point of the timing history",
      _tone: "bad",
    },
  ],
};

const notMeasured = [
  "<b>No place-and-route on a populated die</b> for any cluster-count configuration. Every scaling figure is arithmetic on one synthesised cluster.",
  "<b>The resident tile in URAM has not been re-measured in context.</b> The standalone probe is 585 MHz against a cluster that closes at 344, and the pipeline argument says the seam does not move — but URAM's clock-to-out is worse than block RAM's and the accumulator is what the cluster closes on, so treat the in-context figure as unmeasured rather than unchanged.",
  "<b><code>mx_cluster_core</code> was never synthesised standalone.</b> Where a figure for it appears it was inferred from the cluster minus its parts.",
  "<b>MW=16 has not been synthesised since MW=14 became the default.</b> Its last figure was 302.3 MHz from a 300 MHz-target run several steps earlier. “Costs less and carries more slack” is sound on the evidence that chose the operating point and is <i>not</i> a claim about what FP24 would measure on today's block.",
  "<b>The online quantisation path no longer exists</b> and will not be re-run. Its last measurement was 408.6 GFLOP/s at 66.5% on per-row memory ports. The transform slot moved off the fetch path entirely — a fetch is never transformed — so that figure stands as history and is <i>not</i> a number this machine can produce again.",
];

/* §9.5 — the measured clock ladder. */
const clocks = {
  cols: [
    { key: "d", label: "domain", mono: true },
    { key: "c", label: "clean to", mono: true, align: "right" },
    { key: "f", label: "first degradation", mono: true, align: "right" },
    { key: "x", label: "dies", mono: true, align: "right" },
    { key: "s", label: "the ship profile asks", mono: true, align: "right" },
    { key: "v", label: "verdict" },
  ],
  rows: [
    {
      d: "mat2x",
      c: "<b>400</b>",
      f: "450",
      x: "700",
      s: "<b>600</b>",
      v: "<b>unreachable</b> — asks 1.5x what it does",
      _tone: "bad",
    },
    {
      d: "vec",
      c: "<b>350</b>",
      f: "400",
      x: "450",
      s: "300",
      v: "met, with margin",
      _tone: "good",
    },
    {
      d: "noc",
      c: "<b>300</b>",
      f: "—",
      x: "350",
      s: "300",
      v: "met, and <b>exactly at its cliff</b>",
      _tone: "warn",
    },
    {
      d: "mag",
      c: "<b>250</b>",
      f: "300",
      x: "350",
      s: "<b>300</b>",
      v: "<b>unreachable</b>",
      _tone: "bad",
    },
  ],
};

const profiles = {
  cols: [
    { key: "p", label: "profile", mono: true },
    { key: "n", label: "noc", mono: true, align: "right" },
    { key: "m", label: "mat2x", mono: true, align: "right" },
    { key: "v", label: "vec", mono: true, align: "right" },
    { key: "g", label: "mag", mono: true, align: "right" },
    { key: "w", label: "" },
  ],
  rows: [
    { p: "low", n: "100", m: "200", v: "100", g: "100", w: "the idle profile" },
    { p: "mid", n: "200", m: "400", v: "200", g: "200", w: "" },
    {
      p: "<b>safe</b>",
      n: "200",
      m: "300",
      v: "300",
      g: "200",
      w: "<b>inside all four measured ceilings</b> — what v7 silicon runs",
      _tone: "good",
    },
    {
      p: "ship",
      n: "300",
      m: "600",
      v: "300",
      g: "300",
      w: "<b>three of the four are above what the silicon does.</b> What v7.1 exists to earn",
      _tone: "bad",
    },
  ],
};

/* §9.1 — the L1 footprint band. */
const band = {
  cols: [
    { key: "w", label: "L1 words used", mono: true, align: "right" },
    { key: "r", label: "two independent kernels" },
  ],
  rows: [
    { w: "256", r: "clean", _tone: "good" },
    { w: "288", r: "clean", _tone: "good" },
    { w: "320", r: "clean", _tone: "good" },
    { w: "352", r: "<b>wrong</b>", _tone: "bad" },
    { w: "384", r: "<b>wrong</b>", _tone: "bad" },
    { w: "416", r: "<b>wrong</b>", _tone: "bad" },
    { w: "448", r: "<b>wrong</b>", _tone: "bad" },
    { w: "480", r: "<b>wrong</b>", _tone: "bad" },
    { w: "512", r: "clean", _tone: "good" },
  ],
};

/* §9.4 — the bring-up ladder, as rungs rather than as a bug story. */
const ladder = {
  cols: [
    { key: "r", label: "rung" },
    { key: "a", label: "what it adds" },
    { key: "v", label: "result on multimesh_v7", mono: true },
  ],
  rows: [
    { r: "master width", a: "the transport itself", v: "64-bit" },
    {
      r: "write path, byte-exact",
      a: "that a written word reads back",
      v: "clean on all 4 meshes",
    },
    {
      r: "<code>A_CAPS</code>",
      a: "that the agent answers",
      v: "0x01040120 on all 4",
    },
    {
      r: "enumeration",
      a: "that every unit is present",
      v: "<b>38 units — 8+2 / 6+2 / 8+2 / 8+2</b>, CU_VERSION 4",
    },
    {
      r: "<b>one flit to one unit</b>",
      a: "dispatch, and nothing else",
      v: "<b>10 of 10 on mesh 0</b>",
      _tone: "good",
    },
    {
      r: "32x64x64 against fp32",
      a: "the whole datapath",
      v: "p50 1.72e-03, p99 6.60e-03, peak 0.59",
    },
    {
      r: "two runs, same operands",
      a: "determinism",
      v: "<b>identical, 2,048 of 2,048</b>",
      _tone: "good",
    },
  ],
};
</script>

<template>
  <DocPage
    title="What was measured"
    summary="Every measured KohakuTPU figure with its conditions — resources, Fmax by block, the per-SLR budget it sits in, accuracy, throughput, and what closed and what did not."
    domain="tpu"
    status="measured"
    source="Every figure is xcvu13p-fhgb2104-2L-e, Vivado 2024.2, unless a row says otherwise · docs/projects/kohakutpu/results.md · ship.md"
  >
    <Callout
      kind="rule"
      title="Almost everything here is out-of-context synthesis, and no frequency here is a closed-timing figure"
    >
      <p>
        Nothing is placed and the route is estimated, except on the few rows
        that say otherwise. These numbers describe
        <b>one accelerator on one part</b> — they are evidence the framework
        reaches real silicon, not specifications of it.
      </p>
      <p>
        <b>Every megahertz on this page is a synthesis estimate against a
        stated target.</b> Synthesis slack is optimistic: one module in this
        tree lost <b>0.740&nbsp;ns</b> going from synthesis to routing, so a
        small positive slack here is not a promise that a placed design meets
        the same period. Nothing on this page says a placed design has run at
        300&nbsp;MHz, because none has been built.
      </p>
    </Callout>

    <SpecTable :cols="reading.cols" :rows="reading.rows" />

    <p class="doc-p">
      Three conventions hold throughout.
      <b>Shapes are <code>M x K x N</code></b> — some figures were recorded when
      they were written <code>M x N x K</code>, and those have been converted,
      so the numbers are the ones measured and only the labels moved.
      <b>GFLOP/s is <code>2 · MACs / cycles · 300 MHz</code></b
      >, where <b>the cycle counts are the measurement and 300 MHz is a nominal
      clock</b> — no placed design has been shown to run at it, so a rate here
      is a cycle count wearing a unit. And <b>one MAC is 2 FLOP</b>, with the
      unit FLOPS rather than IOPS because MXFP7 is a floating-point format.
    </p>

    <h2 class="doc-h2">The part it is all measured on</h2>

    <SpecTable
      :cols="device.cols"
      :rows="device.rows"
      caption="xcvu13p-fhgb2104-2L-e. The four SLRs are identical — an exhaustive site census shows the same hard IP in all four, with two asymmetries only: the end dies have one Laguna face rather than two, and SLR1 is the master, so configuration and the device-DNA and user-eFUSE primitives live there. There is no hard DDR controller on this family and no HBM on this part"
    />

    <SpecTable
      :cols="crossing.cols"
      :rows="crossing.rows"
      caption="Crossing an SLR. One hard rule follows: carry chains, DSP cascades and BRAM/URAM cascades do not propagate across a boundary, so every cluster must be SLR-resident — the DSP cascade is a physical object that cannot be cut. A crossing also has to be flop → SLL → flop with nothing in between, because a Laguna site IS a flip-flop and a single combinational gate on the path forfeits it"
    />

    <SpecTable
      :cols="channels.cols"
      :rows="channels.rows"
      caption="Exactly one DDR4 controller per SLR, and the channel numbering does not match the die numbering. A DDR4 interface cannot span SLRs, which is what makes this a constraint rather than a preference"
    />

    <h2 class="doc-h2">The matmul path</h2>

    <SpecTable
      :cols="matmulPath.cols"
      :rows="matmulPath.rows"
      caption="Out-of-context. The cluster and accumulator rows come from a 310 MHz target (3.2258 ns) re-measurement; rows marked ‡ are older 300 MHz target (3.3333 ns) runs, not re-measured since. mx_matmul_cu is the superseded single-port design, kept as a measured baseline: it cannot be fed at rate and it is not on the current path, but it is what two of the older system benches drive"
    />

    <Callout
      kind="trap"
      title="Two cluster figures are in circulation and both are real"
    >
      <p>
        Before the normalising shift moved into DSPs the cluster measured
        <b>17,629 LUT / 17,782 FF / 272 DSP at 325.6 MHz</b>; after,
        <b>15,306 / 17,754 / 304 at 346.6</b>. A separate standalone run of the
        earlier configuration reported 17,521 LUT and 17,612 FF at 325.6 MHz
        with WNS +0.155 ns.
        <b
          >The 272-DSP rows are the older configuration and the 304-DSP rows are
          current</b
        >, and the difference matters more than it looks — it is what takes the
        DSP-bound cluster count from 45 to 40.
      </p>
    </Callout>

    <p class="doc-p">
      Three things read off that table.
      <b>Every <code>mx_mac</code> is 0 LUT, 0 FF, 1 DSP</b> — the multiply and
      the entire K=32 reduction happen inside the DSPs, which was the design's
      central claim and it holds exactly.
      <b>The accumulator is the block the whole cluster closes on</b>: after all
      the timing work the cluster closes within a few MHz of what the
      accumulator measures standing alone. And
      <b>the resident tile is 5 BRAM36 at any depth up to 512</b>, because a
      352-bit port needs <code>ceil(352/72) = 5</code> primitives and depth is
      then free.
    </p>

    <h3 class="doc-h3">Where a cluster's LUTs are</h3>

    <ResourceBars
      :items="clusterLuts"
      unit="LUT"
      :max="2600"
      caption="The breakdown of mx_cluster, ‡ 300 MHz-target run. It SUMS to its parent, which is what makes it a breakdown rather than rows from different runs: 336 + 448 + 476 + 476 + 450 + 2,565 = 4,751 LUT, 784 + 728 + 728 + 728 + 581 + 1,240 = 4,789 FF, and 4 x 64 = 256 DSP — all three the mx_cluster row above. A tensor CU's LUTs are almost all operand skew, and the SRL column of this build sums to 1,458 LUT, 31% of the cluster, spent on nothing but making operands arrive on the right cycle. The accumulator is 54% of the cluster and holds the critical path, and it is the only part that is real fabric arithmetic"
    />

    <h3 class="doc-h3">The normalising shift as a DSP multiply</h3>

    <SpecTable
      :cols="shiftTrade.cols"
      :rows="shiftTrade.rows"
      caption="Measured as a matched before/after pair on the same tree. −6.7% LUT and +15.7 MHz standing alone; −13% and +21.0 MHz inside the cluster — a larger win in context than alone, because the cluster was tight enough that the tool had been replicating logic to hold the frequency"
    />

    <Callout
      kind="rule"
      title="Measure LUTs unflattened when the block is timing-critical"
    >
      <p>
        Three output-identical simplifications taken alongside this were
        <b>−458 LUT of 9,060 with no clock constraint and +307 with one</b>: at
        WNS +0.06 ns the tool spends LUTs replicating logic, and the replication
        moves more than the logic does. The flattened, constrained number is the
        one that ships; the unflattened one is the one that says whether the
        <i>logic</i> shrank.
      </p>
    </Callout>

    <h3 class="doc-h3">
      The per-tile output scale: built, measured, cancelled
    </h3>

    <SpecTable
      :cols="outputScale.cols"
      :rows="outputScale.rows"
      caption="+852 LUT, −12.7 MHz, no extra DSP. Bit-identity was verified two ways, and it was cancelled anyway because the host can fold the constant into an operand for nothing. The third row is the reason a feature that changes a width has to be a compile-time parameter rather than a neutral run-time value: widening the block-scale product widens the normaliser datapath from 30 to 39 bits whatever value is in it"
    />

    <h3 class="doc-h3">The accumulator's timing history</h3>

    <SpecTable
      :cols="acuHistory.cols"
      :rows="acuHistory.rows"
      caption="84.7 MHz to 349.4 MHz in fourteen measured steps, out-of-context against a 300 MHz target, with the full 384-check suite re-run after each. The worst relative error stayed at 3.339790e-04 throughout — bit-identical, step for step — so none of it was bought with precision"
    />

    <Callout kind="measured" title="The dead ends are the informative part">
      <p>
        <b
          >Six of the fourteen steps moved Fmax by less than 10%, and three
          moved it backwards.</b
        >
        The six pipeline splits were worth +150 MHz between them, and then
        fixing three combinational loops that carried a value between iterations
        — which synthesise as ~25-level LUT chains inside a single pipeline
        stage, where no seam elsewhere can reach them — was worth
        <b>+68 MHz on its own</b>.
      </p>
      <p>
        Three later steps took the whole cluster over the line: 294.9 → 296.4
        (magnitude taken
        <b>before</b> the multiply rather than after) → 299.9 (a fabric multiply
        replaced by the predicate its consumer wanted) → <b>325.6</b> (a
        per-instruction boolean decoded once instead of per cycle).
      </p>
    </Callout>

    <SpecTable
      :cols="tileMemory.cols"
      :rows="tileMemory.rows"
      caption="The primitive was never the problem — the same 352-bit memory measures 837 MHz standing alone, so anything slower is the module's own logic, and a memory's standalone figure is the way to tell the two apart. Without the block RAM's output register the path begins at the RAM's clock-to-out, about 1.2 ns, rather than at a flip-flop, and that alone cost about 70 MHz"
    />

    <SpecTable
      :cols="configFigures.cols"
      :rows="configFigures.rows"
      caption="Two configuration figures. 1,358 LUT — 22% of that cluster — for range that cannot be used, since a shift past the accumulator width pushes the value out regardless; correctness unchanged, both benches still passed. And −70%, 32,292 LUTs, for one loop rewrite: a variable part-select tells synthesis that any of 896 bits might come from any position, so it builds a barrel mux across the entire buffer, twice"
    />

    <h2 class="doc-h2">The vector path</h2>

    <SpecTable
      :cols="vectorLane.cols"
      :rows="vectorLane.rows"
      caption="One lane, out-of-context. The LUT estimate was 40% low and the reason is worth recording: a 14-stage pipeline at II=1 has to carry about twenty control signals from where they are produced to where they are consumed. The datapath is roughly what was predicted; the delay lines are what was not"
    />

    <ResourceBars
      :items="vecShrink"
      unit="LUT"
      caption="The assembled core, before and after the shrink. vec_lanes and vec_cu started at 305.1 and 229.3 MHz and the paths that bound them were all control reaching a datapath, never the arithmetic; after the shrink Fmax is UP and the worst path is no longer in the core at all — it is inside the ALU, so the core now sits at the ALU floor. BRAM became a counted resource: 44 tiles per core"
    />

    <SpecTable :cols="shrinkSteps.cols" :rows="shrinkSteps.rows" />

    <Callout
      kind="trap"
      title="The +1,129 that came back is the load-bearing one"
    >
      <p>
        <b
          >Moving storage to block RAM moves its clock-to-out onto every
          consumer's path, and port granularity is the unit that matters, not
          the module.</b
        >
        A RAMB18's clock-to-out is about 1.5 ns on this speed grade. Of the
        register file's three read ports, two feed ALU operands and have a whole
        cycle; the third feeds the store converters and did not —
        <code>vec_cu</code> fell to <b>286.0 MHz</b>. The load side had already
        hit this and left another block at <b>286.9 MHz</b>, the same number one
        direction earlier.
      </p>
    </Callout>

    <p class="doc-p">
      Extrapolated to 128 lanes — <b>PROJECTED</b>, never built — the core is
      ~160k LUT and 384 DSP, about
      <b>37% of an SLR's LUTs against 12.5% of its DSPs</b>, so the vector core
      is fabric-bound rather than DSP-bound. One assembled core measures roughly
      <b>33,000 LUT</b>, which is the number to use when costing a new
      instruction: something costing ~3,000 LUT lands in every core, so at six
      cores it is ~18,000 — half a core's worth of area for a capability every
      core gains. <code>mm_mesh</code> — the memory agent with the mover, one
      matmul cluster, one vector core and two routers — measures
      <b>328.8 MHz</b> after the shrink.
    </p>

    <h2 class="doc-h2">Blocks measured in this ship</h2>

    <SpecTable
      :cols="shipBlocks.cols"
      :rows="shipBlocks.rows"
      caption="Measured here because KohakuTPU is what was built; the blocks themselves belong to the framework. The memory-primitive probes are far above anything they sit inside, which is what makes “blame the module, not the primitive” a checkable claim rather than a slogan"
    />

    <h2 class="doc-h2">Device level</h2>

    <SpecTable
      :cols="scaling.cols"
      :rows="scaling.rows"
      caption="PROJECTED — one cluster is what was synthesised, nothing at 32 or 40 clusters has been built, so every column but the first is multiplication. The per-cluster column is mx_cluster_cu in the shape that ships. At a NOMINAL 300 MHz, 40 clusters is ~12.3 TFLOPS of AMP FP16-MXFP7 and the 32-cluster configuration — what a four-partition floorplan would build — is ~9.8 TFLOPS; the MAC counts are the structural claim and the clock is an assumption no placed design has met"
    />

    <Callout
      kind="trap"
      title="The DSP-bound cluster count has been quoted as 48, 45 and 40, and only one of those is current"
    >
      <p>
        The measured cluster is <b>304 DSP</b> once both the block-scale
        multiply (16, one per lane) and the normalising shift (32, two per lane)
        are counted, which puts the DSP-bound count at
        <code>12,288 / 304 = 40</code>. The older 272-DSP cluster gives 45, and
        a draft counting only the cascade's 256 gave 48.
        <b>All three have been quoted somewhere; 40 is the one the current
        cluster supports</b>, and the table above uses it.
      </p>
      <p>
        The conclusion — <b>DSP-bound</b>, which is the right place to be bound
        on this part — moves further in the same direction with each correction,
        which is exactly why it was never caught by the answer looking wrong.
        What did move is BRAM: the shipping cluster carries 35 BRAM36 against
        the 5 of the bare one-port build, so at 40 clusters block RAM is at
        <b>52%</b> of the device rather than the 8% an earlier table showed.
      </p>
    </Callout>

    <Callout
      kind="measured"
      title="What was actually built is 30 clusters, and that is not a contradiction of the 40"
    >
      <p>
        <code>multimesh_v7</code> enumerates
        <b>38 units on the card — 8+2 / 6+2 / 8+2 / 8+2</b>, which is 30 matmul
        clusters and 8 vector cores across four meshes, verified 2026-08-23 by
        reading the control plane rather than by trusting a plan. That is a
        placed, routed, programmed design.
      </p>
      <p>
        <b>The 40 and the 30 are different kinds of number and must not be
        subtracted.</b> The 40 is one out-of-context cluster's DSP count divided
        into the device's, on a die holding nothing else. The 30 is what fits
        beside eight vector cores, four memory agents, four DDR4 controllers, the
        AXI fabric and a 76,319-LUT host DMA block, at a placement that already
        measures 95.80% CLB on its worst die.
        <b>The binding resource on a populated die is fabric and placement</b>,
        not the hard block the arithmetic divides by — which is why the
        arithmetic is a ceiling and the enumeration is a population.
      </p>
    </Callout>

    <h3 class="doc-h3">What the host IP costs</h3>

    <ResourceBars
      :items="hostIp"
      unit="LUT, against one SLR's 432,000"
      :max="432000"
      caption="Per-IP utilisation read out of the IMPLEMENTED single-mesh design on xcvu13p-fhgb2104-2L-e — a placed figure for each IP inside a placed image, not an out-of-context run. XDMA is 17.7% of one die region on its own, so whichever die hosts PCIe gives up roughly a vector core's worth of fabric to do it, which is the constraint behind the floorplan"
    />

    <h3 class="doc-h3">Placed occupancy</h3>

    <SpecTable
      :cols="placed.cols"
      :rows="placed.rows"
      caption="From a placed multi-mesh run. A full 288-bit flit link is about 5% of one boundary, so the interlink is not what constrains the crossing — fabric occupancy is. The single-mesh design on the card places nothing at all in one SLR"
    />

    <Callout kind="note" title="What the machine is bound by, at each level">
      <p>
        At the cluster level, <b>DSP-bound</b> — a cluster is essentially all
        DSP and its fabric cost is the manager, the sequencer and the mesh
        attachment rather than the arithmetic. At the vector level, the
        opposite: <b>fabric-bound</b>, at roughly 37% of an SLR's LUTs for 128
        lanes against 12.5% of its DSPs. At the <i>device</i> level neither is
        what ran out first — the placed multi-mesh design measured URAM at 9.38%
        and one die at 95.80% CLB, so the binding resource on a populated die is
        <b>fabric and placement</b> rather than any hard block.
      </p>
    </Callout>

    <h2 class="doc-h2">Accuracy</h2>

    <SpecTable
      :cols="aluChecks.cols"
      :rows="aluChecks.rows"
      caption="26,897 checks on the vector ALU, streamed at one instruction per cycle, against both a behavioural DSP and a real DSP48E2. log2 needs both bounds and neither alone is meetable by any implementation: near x = 1 the result approaches zero while its absolute error does not, and at large |x| the result spans decades"
    />

    <Callout
      kind="measured"
      title="The alignment sweep is the load-bearing test"
    >
      <p>
        It walks the exponent difference across every barrel-shifter position,
        which is the only way to reach the case where the product's top bit is a
        value bit rather than a sign bit, and the bypass below it.
        <b>Random operands never land on either.</b>
      </p>
      <p>
        A later bench moved the FMA's known bound to <b>one ulp</b>: the SIMD-PE
        float-lane bench, built to oversample exponent-distant addends, reached
        an effective-subtraction corner this suite's four mantissa patterns per
        shifter position never land on — discarded alignment residue carried as
        a plain sticky reads exactly one ulp high,
        <b>19 of 4,000 on that stream, 0 of 6,000 in this suite's random band</b
        >.
      </p>
    </Callout>

    <SpecTable
      :cols="tables.cols"
      :rows="tables.rows"
      caption="Transcendental table quality, predicted against measured. Measured is consistently ~1.5 bits worse than the minimax prediction, and that gap is the point of measuring: the approximation is no longer what limits these functions — the coefficient and Horner quantisation is. Adding segments would buy almost nothing"
    />

    <SpecTable
      :cols="endToEnd.cols"
      :rows="endToEnd.rows"
      caption="End to end. The 4-block worst case is CANCELLATION — a property of the problem, not of the hardware — which is why the mean matters more than the maximum for judging the accumulator. At these sizes the accumulator is not the limiting factor; the output format is"
    />

    <Callout
      kind="trap"
      title="In simulation two runs of the same shape are bit-identical. On the card they are not."
    >
      <p>
        Measured on <code>multimesh_v3</code>: repeating one matmul with fixed
        operands leaves <b>~0.6% of elements differing between runs</b> by up to
        40% of peak, and a further <b>~0.3% reproducibly wrong</b> by up to
        11,000x. The two sets never overlap. So a difference between two
        <i>hardware</i> runs is not automatically a real difference — expect
        ~0.5% disagreement before concluding anything.
      </p>
      <p>
        The blown elements are <b>operand range</b>, and that one is closed. The
        flickering is separate and still open: non-deterministic unit assignment
        is eliminated, as is stale tile state and transport, and between runs on
        byte-identical operands <code>run1/run0</code> is an exact power of two
        in <b>75 of 85</b> cases with neither run correct, which points at the
        per-block scale rather than the multiply-accumulate.
      </p>
    </Callout>

    <h2 class="doc-h2">Throughput</h2>

    <p class="doc-p">
      Peak is <b>512 MAC/cycle per cluster</b>, so a two-cluster machine peaks
      at 1,024 MAC/cycle. At a nominal 300 MHz that is 614 GFLOP/s —
      <b>the MAC rate is the structural figure and the clock is the
      assumption</b>, so the “% peak” column below is the one that means
      something and is clock-independent.
    </p>

    <SpecTable
      :cols="baseline.cols"
      :rows="baseline.rows"
      caption="The baseline: two clusters, a small resident tile, both operands quantised online. The rate is FLAT across a 512x range in problem size, which is the signature of a structural limit rather than a small-problem artifact — the machine had one operating point and tiling did not move it. Result write amplification was exactly 1.00x at every size"
    />

    <Callout
      kind="measured"
      title="Starvation with every backpressure counter at zero is the whole diagnosis"
    >
      <p>
        At the 128-cube the memory agent's service FSM spent 31.3% idle, 35.0%
        qfill, 7.8% qwait, 27.2% qemit and 11.7% wr, while its stall counters
        read <code>in_bp 0.0%</code>, <code>out_bp 0.0%</code>,
        <code>cu_send 0.1%</code> and <b><code>cu_dry 64.7%</code></b
        >. Nothing was pushing back; there simply was no data to take, so the
        fault was upstream of the fabric in how operands were requested and
        served. Per L1 entry — 256 B in memory, 8 AXI beats — the floor is 8
        cycles, the agent actually took <b>18</b>, and the CU <b>~37</b> with
        serial requests.
      </p>
    </Callout>

    <SpecTable
      :cols="current.cols"
      :rows="current.rows"
      caption="Current: both operands pre-quantised, one memory port per mesh row. 42.0 → 538.3 GFLOP/s on the 256-cube — 12.8x"
    />

    <SpecTable
      :cols="steps.cols"
      :rows="steps.rows"
      caption="The individual steps, each measured on its own"
    />

    <Callout
      kind="trap"
      title="Every row of the current table predates a mesh layout change"
    >
      <p>
        Those runs were measured when a cluster's two endpoints straddled
        another cluster's router. The layout since places a cluster as one
        column of a band, and measured, the new layout
        <b>costs about three points of peak at 8 clusters</b> — 72.7% against
        75.7% on the same work with the same arithmetic. That cost is
        <b>not</b> the memory system: fetch was unchanged within noise at 7.1 →
        7.2 cycles per entry, and write-slot pressure more than halved, 5.5% →
        2.1%. What is left is routing.
        <b>Treat every figure above as a figure for the previous topology.</b>
      </p>
    </Callout>

    <Callout
      kind="rule"
      title="The five resource budgets are independent and must never be added"
    >
      <p>
        Read and write are different AXI channels served in the same cycle, and
        the mesh's two directions are different wires. Adding them prices
        capacity that never competes.
        <b>What binds is the largest, never the total.</b> Read correctly, on
        the current machine: 2 CU at 256x256x256 is
        <code
          >flops 87.6% · mem_rd 30.3% · mem_wr 20.2% · noc_in 22.8% · noc_out
          32.8%</code
        >; 8 CU at 512x256x1024 is
        <code
          >flops 75.5% · mem_rd 23.6% · mem_wr 18.9% · noc_in 21.3% · noc_out
          26.0%</code
        >.
      </p>
      <p>
        <b
          >No memory budget exceeds a third at any cluster count, and the
          busiest thing in the machine is the array.</b
        >
        That is what withdrew a wider bus as a lever.
      </p>
    </Callout>

    <SpecTable
      :cols="mistakes.cols"
      :rows="mistakes.rows"
      caption="Getting this wrong pointed falsely at bandwidth three times. An impossible percentage looks exactly like a saturated bus: a derived figure needs its PREDICATE re-checked as well as its denominator whenever the thing it counts changes shape, and a stall counter that moves while the cycle count does not is a change in meaning until proven otherwise"
    />

    <Callout kind="measured" title="The roofline that said this was impossible">
      <p>
        At the baseline tile the arithmetic gave a hard ceiling of
        <b>154 GFLOP/s, 25% of peak</b>, and concluded that reaching 500 was a
        memory-hierarchy decision rather than a scheduling one.
        <b>538.3 was reached without widening a single bus.</b> The arithmetic
        was sound on an assumption the <i>schedule</i> controls — that each
        operand byte is fetched once — and the schedule was re-reading B once
        per m-tile, a quarter of all traffic that no intensity figure shows.
        <b
          >The ceiling is a function of the schedule, so a schedule change moves
          it.</b
        >
      </p>
      <p>
        Two rate improvements that were not: shared fetch armed without a
        rendezvous went 85.1 → 105.1 GFLOP/s
        <b>and the worst element went from 1.0 to 2.2e+02</b>; and a third
        change measured <b>499.6 GFLOP/s while computing nothing at all</b>. The
        rule both produced: bound the worst element against the software model,
        not just the median — and treat a rate improvement with no matching
        component counter as unexplained.
      </p>
    </Callout>

    <h2 class="doc-h2">What the silicon's clocks actually do</h2>

    <p class="doc-p">
      Everything above this heading is synthesis. This section is not: it is a
      frequency ladder run <b>on the card</b>, and it is the only place on this
      page where a frequency is a property of a placed, routed, programmed
      design rather than of a netlist.
    </p>

    <SpecTable
      :cols="clocks.cols"
      :rows="clocks.rows"
      caption="Measured on multimesh_v7, mesh 0, 2026-08-23, by scripts/py/fmax_ladder.py. Each domain is laddered while the other three are held at the low profile, and each is scored as relative error against fp32 — never as pass/fail. MHz throughout"
    />

    <Callout
      kind="trap"
      title="Three of the four clocks the ship profile asks for are above what this silicon does"
    >
      <p>
        <code>mat2x</code> is asked for 600 and is clean to 400.
        <code>mag</code> is asked for 300 and is clean to 250.
        <code>noc</code> is asked for exactly the number at which it stops. Only
        <code>vec</code> has margin.
      </p>
      <p>
        <b><code>mat2x</code>'s floor is the number format, not the clock.</b> It
        is bit-identical from 200 to 400 — p50 1.561e-02, p99 1.064, max 9.425 at
        every step — and then degrades to p50 1.574, which is 157%, at the 600
        the profile asks for. So a frequency sweep that scores error tells you
        <i>which</i> of the two you are looking at; one that scores pass/fail
        does not.
      </p>
      <p>
        <b><code>noc</code> has no graceful degradation.</b> Clean at 300, hung
        at 350, with no intermediate error at all. A routing fabric loses a
        packet rather than corrupting a number, so a timing failure there is a
        <b>timeout</b>, and a domain that shows no error gradient is not
        therefore safe.
      </p>
    </Callout>

    <SpecTable
      :cols="profiles.cols"
      :rows="profiles.rows"
      caption="The board file's clock profiles against those ceilings, in MHz. safe is the one that fits inside all four measured limits, and it is what v7 silicon runs"
    />

    <Callout
      kind="rule"
      title="Two ways a frequency ladder measures nothing, both of which read as a pass"
    >
      <p>
        <b>The workload must drive the unit the domain clocks.</b> Laddering
        <code>vec</code> against a matmul returns bit-identical error at all
        eleven steps — because the matmul cluster runs it and the vector core
        sits idle. That reads as “clean to 600” and is
        <i>no measurement at all</i>. Each domain here is driven by something
        that reaches it: a matmul for <code>mat2x</code>, an
        <code>rmsnorm</code> for <code>vec</code>.
      </p>
      <p>
        <b>A pass/fail verdict hides the answer.</b> The first version of this
        ladder reported <code>ok</code> per rung, which meant only “the unit
        signalled” — and a <code>FILL</code> has no numeric output to be wrong.
        Every rung passed at every frequency and the table said nothing.
      </p>
    </Callout>

    <h2 class="doc-h2">Defects found by measurement</h2>

    <h3 class="doc-h3">An L1 footprint band that returns wrong data</h3>

    <SpecTable
      :cols="band.cols"
      :rows="band.rows"
      caption="Measured, unexplained, and guarded rather than fixed. A vector kernel whose buffers occupy 352 to 480 of the core's 512 L1 words returns wrong data; 320 and below is clean and so is exactly 512, which is what makes it a band rather than a capacity limit. The corruption is 16 L1 words wide. The cost is a capability limit rather than a wrong answer, because the driver caps the footprint below the band: at a channel count of 320 with 32 groups a normalisation group is 10·hw elements, so the spatial extent is capped at hw ≤ 128 — an 8x16 tile works and a 12x16 does not"
    />

    <h3 class="doc-h3">The 8-bit L1 offset wrap</h3>

    <SpecTable
      :cols="offsetWrap.cols"
      :rows="offsetWrap.rows"
      caption="The 8-bit L1 offset wrap, measured on the card against the machine's own software MXFP7 model, before and after wiring the bank bits through. Eleven of eleven measured shapes follow the rule exactly and it is not a capacity threshold: 576 B entries passes at one shape and fails at another with the same entry count and the dimensions swapped. Only the PRODUCT against 256 predicts every case"
    />

    <p class="doc-p">
      End to end on a transformer block, the same fix took the per-head worst
      element
      <b>1.21e-02 → 1.07e-03</b>, with the full-width path unchanged at 1.07e-03
      — which is the format's own cost of 1.06e-03, so the error that remains is
      MXFP7 and nothing else. Traffic fell from 122 matmuls to 68. On the
      compiler path the same defect was worse and completely silent: the bank
      fields were absent entirely and the offset addressed past two banks, and
      adding the fields with a range check <b>immediately failed 25 tests</b> on
      an entry the path had been reaching only by relying on 8-bit wrap.
    </p>

    <Callout kind="trap" title="Bugs the two-model discipline caught">
      <p>
        <b>A DSP input register had to be 2, not 1.</b> With the pre-adder path
        selected, the A/D operands reach the multiplier through two register
        stages while B was given one, so B arrives a cycle early and multiplies
        against the wrong operand.
        <b>This is invisible with stable operands</b> — every stage happens to
        be looking at the same tile, so the misalignment cancels — and only
        appears when a new tile enters every cycle. The behavioural model passed
        and the real DSP failed <b>only</b> in the streaming section, which
        pointed straight at the DSP configuration rather than at the arithmetic.
      </p>
      <p>
        The simulation library also holds global set/reset asserted for the
        first 100 ns, so unisim registers ignore everything before that
        regardless of the design's own reset. Without waiting past it, the first
        tile silently produces nothing.
      </p>
    </Callout>

    <h2 class="doc-h2">Verification</h2>

    <h3 class="doc-h3">The ladder that finds a defect, rung by rung</h3>

    <p class="doc-p">
      A matmul engages the memory agent, DRAM, staging and every cluster at
      once, so when it fails it cannot say which of them is broken.
      <b>Every rung below adds exactly one thing</b>, and that is the whole
      method: the first rung that fails names the layer.
    </p>

    <SpecTable
      :cols="ladder.cols"
      :rows="ladder.rows"
      caption="Run on multimesh_v7, 2026-08-23, at 100/200/100/100. The one-flit rung is the load-bearing one and is now a driver call in its own right — it is dispatch with the datapath, the memory system and the schedule all removed"
    />

    <Callout
      kind="trap"
      title="Three failures that present identically: a unit that never signals, with the bus healthy throughout"
    >
      <p>
        None of the three is an RTL fault and none raises an error on the card.
        Each is a rule the hardware already stated and the software did not
        keep, so each is worth knowing as a rule rather than as a story.
      </p>
      <p>
        <b>A program's registers have a required write order.</b> Seeding the
        destination, base, length and credit <i>before</i> the kick leaves the
        program register reading <code>run=1</code> with
        <code>flits_left&gt;0</code> and <code>credit=0</code> — running, with
        nothing to spend.
      </p>
      <p>
        <b>A write shadow must not elide a register a later kick shares.</b>
        Dropping a length write because the previous kick used the same length
        sends every completion to the first node and none to the second, so node
        2 signals nothing at all.
      </p>
      <p>
        <b>A DRAM write shorter than 32 bytes zeroes the rest of the line.</b>
        A loop of consecutive single-word writes leaves only its
        <i>last</i> word. This is the sharpest of the three because the preflight
        hides it: a byte-exact write-path check that uses block writes certifies
        the path clean while every hand-written single word silently destroys
        its neighbours. Writing a full 32-byte line and then poking one word into
        it returns
        <code>['0x0', '0xdeadbeefdeadbeef', '0x0', '0x0']</code> —
        <b>deterministically</b>, which supersedes the earlier account that single
        writes “vanish about half the time”.
      </p>
    </Callout>

    <SpecTable
      :cols="benches.cols"
      :rows="benches.rows"
      caption="Everything in the matmul datapath is exact integer arithmetic checked bit-for-bit against a model computed in the bench. No tolerances. The coverage that matters is the cases random operands never reach: the packing worst case with all three operands at −64; full-scale sums so a K=32 sum reaches ±131,072 and uses all five guard bits; the borrow correction with the lower field forced negative on all eight chains; streaming, a new tile every cycle; non-uniform scales per row and column; the alignment sweep; and a peer round trip that adds a tile to itself, so the answer must be exactly 2T"
    />

    <Callout kind="rule" title="A test never seen to fail is not a test">
      <p>
        Two reduction kinds had no coverage anywhere, and the vector-length mask
        they reduce under is the kind of thing a <i>passing</i> test can miss
        entirely: a uniform predicate gives the same answer whether the mask is
        right or stuck at all-ones. The test that closed it splits the predicate
        mid-vector so a stale mask is wrong in both directions —
        <b
          >and it was then verified by forcing the mask to all-ones and watching
          it fail.</b
        >
        Do that for anything whose failure mode is “quietly returns a plausible
        value”.
      </p>
    </Callout>

    <h2 class="doc-h2">What met its synthesis target, and what did not</h2>

    <Callout
      kind="rule"
      title="“Met its target” is not “closed timing”, and the difference is the whole point of this section"
    >
      <p>
        A block that met its target cleared an out-of-context
        <i>synthesis</i> constraint and the tool stopped trying. It was not
        placed, it was not routed, and no design containing it has been shown to
        run at that period. <b>Every retracted claim in this repository was made
        by reading one of these rows as a closed frequency.</b>
      </p>
    </Callout>

    <SpecTable
      :cols="closed.cols"
      :rows="closed.rows"
      caption="Met their out-of-context SYNTHESIS target of 300 or 310 MHz, on xcvu13p-fhgb2104-2L-e with Vivado 2024.2. Each is a lower bound on what that block's logic can do and an upper bound on what a placed design containing it would achieve"
    />

    <SpecTable :cols="ceilings.cols" :rows="ceilings.rows" />

    <h3 class="doc-h3">Not measured, and should not be assumed</h3>

    <ul
      class="kt-text-body text-warm-700 dark:text-warm-300 leading-7 my-4 max-w-[70ch] list-disc pl-5"
    >
      <li v-for="(n, i) in notMeasured" :key="i" v-html="n" class="my-2" />
    </ul>

    <h2 class="doc-h2">The superseded FP8 baseline</h2>

    <p class="doc-p">
      Kept because it is the only measured baseline for the FP16 ALU path that
      exists, and because it is what the MXFP7 design is measured against.
      <b
        >These describe the previous FP8 → FP12 → FP16 design and say nothing
        about the current element format.</b
      >
      <b
        >10,656 of the FP8-FP12 tensor core's 12,731 LUTs were its 32
        floating-point adder units</b
      >
      — 84% of the core was its adder tree. Per 128 MACs: old
      <b>12,731 LUT + 64 DSP</b>, new <b>1,188 LUT + 64 DSP</b>, about 10.7x
      fewer LUTs. Same DSP count, same MAC count, different numerics, and
      essentially all of the difference is accumulation leaving the fabric.
      <i
        >Both figures are out-of-context synthesis of one tensor core on
        xcvu13p-fhgb2104-2L-e; the old one predates the current tool version and
        is quoted as the baseline the replacement was judged against, not as a
        current measurement.</i
      >
    </p>
  </DocPage>
</template>
