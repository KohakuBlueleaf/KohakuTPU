<script setup>
/* SIMD PE — what the machine is, what it costs, and what each width buys.
 *
 * PROVENANCE. Every resource and frequency figure is out-of-context SYNTHESIS
 * on xcvu13p-fhgb2104-2L-e, Vivado 2024.2, -directive default, at a 3.333 ns
 * target, and nothing is placed or routed. Every table names its
 * -flatten_hierarchy setting and the frozen source tree it was taken on, and
 * rows from different trees are never subtracted.
 *
 * The delta tables are transcribed from docs/projects/kohakumpe/unit-counts.md,
 * which is the price list and names, per table, the tree behind each row. The
 * ISA tables are read out of src/kohakumpe/simd/khs_unit.v and its generated
 * decode header rather than restated from prose.
 */

const reference = `SIMD PE reference row -- 8 slots, 8 integer lanes, 4 binary32 FMA units

  15,682 LUT  ·  9,836 FF  ·  13 BRAM  ·  56 DSP48  ·  130 control sets

  requested 3.333 ns (300.0 MHz)   ->   achieved 349.3 MHz`;

const refCfg = `SIMD_EN 1   SIMD 8   ILANES 8   MULS 4   NACC 2   VREGS 8
VSPAD 1024  NPART 16  SHIFT_UNITS 8 (full)  PERM_UNITS 8 (full)  RED_PIPE 1
FLOAT_LANES 4   FSFU_UNITS 0   HAS_FALU 1   HAS_FACC 0   HAS_FCVT 0
WB_STAGE 0   VREG_PRIM distributed   MEM_PRIM block   RECV_MEM distributed`;

const dtype = `IEEE binary32 in  ->  binary32 compute  ->  binary32 out`;

const granule = `   SLOTS    32-bit words in a vector register    8, FIXED (VW = 32 * SIMD)
   UNITS    how many are BUILT for a feature     0, 1, 2, 4, 8, or -1 for full
   PASSES   slots / units, one issued per cycle  derived

   8 slots x 32 bit  =  256 bit  =  one native memory entry  =  one flit payload

   integer width   the memory granule (coalescing)         FIXED by the granule
   float width     arithmetic demand (throughput vs LUT)    A KNOB`;

/* The per-unit DSP and BRAM costs of the float tier are identical on both cores
 * because both instantiate the same units. The SIMT closed form is current and
 * reproduces every measured SIMT row with zero error. The equivalent SIMD form
 * is WITHDRAWN: it carried a term for the integer lane's multipliers sized by a
 * parameter that no longer exists. */
const dspForm = `   a binary32 FMA unit          2 DSP48                    both cores
   the seed capability          + 1 DSP48 and 1.5 BRAM     both cores
                                three RAMB18, one ROM per polynomial coefficient

   SIMT, checked with zero error against every measured row:
       DSP  = 2*FLANES + 4*LANES
       BRAM = 26.5 + (FLANES>0 ? 4 : 0) + 1.5*FSFU_UNITS

   SIMD: WITHDRAWN. The fitted form carried a 6*SIMD term for the integer
         lane's multipliers, and the parameter that sized them is gone.`;

/* ------------------------------------------------------------ the shape */
const shape = {
  nodes: [
    {
      id: "pc",
      x: 9,
      y: 0,
      w: 15,
      h: 3.4,
      label: "ONE program counter",
      sub: "one instruction stream, decoded ONCE",
      accent: true,
    },
    {
      id: "sc",
      x: 0,
      y: 6,
      w: 14,
      h: 4.4,
      label: "the base core — rv_core",
      sub: "RV32IM: addresses, trip counts, branches",
    },
    {
      id: "vu",
      x: 19,
      y: 6,
      w: 16,
      h: 4.4,
      label: "the vector unit — khs_unit",
      sub: "the slots, all of them",
      accent: true,
    },
    {
      id: "x",
      x: 0,
      y: 12.4,
      w: 14,
      h: 3.2,
      label: "x0..x31 · 32 bits",
      sub: "RV32IM — mul yes, div faults",
    },
    {
      id: "sp",
      x: 0,
      y: 16.4,
      w: 14,
      h: 3.2,
      label: "scratchpad · 32-bit face",
      sub: "2048 × 32, 2 BRAM",
    },
    { id: "v", x: 19, y: 12.4, w: 16, h: 3.2, label: "v0..v7 · 256 bits" },
    {
      id: "facc",
      x: 19,
      y: 16.4,
      w: 16,
      h: 3.2,
      label: "float accumulator · HAS_FACC",
      sub: "NACC banks of NPART rotating partials — OFF by default",
    },
    {
      id: "vsp",
      x: 19,
      y: 20.4,
      w: 16,
      h: 3.2,
      label: "vector scratchpad · 256-bit",
      sub: "1024 × 256, SIMD banks of block RAM",
    },
    {
      id: "lanes",
      x: 19,
      y: 24.4,
      w: 16,
      h: 3.4,
      label: "ILANES integer · FLOAT_LANES float",
      sub: "8 and 4 at the reference row; either may be 0",
      accent: true,
    },
  ],
  edges: [
    { from: "pc:b", to: "sc:t", dir: "v" },
    { from: "pc:b", to: "vu:t", dir: "v", accent: true },
    { from: "sc:b", to: "x:t", dir: "v" },
    { from: "x:b", to: "sp:t", dir: "v" },
    { from: "vu:b", to: "v:t", dir: "v" },
    { from: "v:b", to: "facc:t", dir: "v" },
    { from: "facc:b", to: "vsp:t", dir: "v" },
    { from: "vsp:b", to: "lanes:t", dir: "v" },
    { from: "sc:r", to: "vu:l", dir: "h", label: "rs1 + imm" },
  ],
};

const counts = {
  cols: [
    { key: "n", label: "quantity", mono: true },
    { key: "k", label: "kind" },
    { key: "v", label: "reference row", align: "right", mono: true },
    { key: "w", label: "What it is" },
  ],
  rows: [
    {
      n: "SIMD",
      k: "architecture",
      v: "8",
      w: "32-bit slots in a vector register, so <code>VW = 256</code>. <b>The address path.</b> 8 × 32 bit is one flit payload and one memory-agent entry; narrowing it turns every coalesced load into two or more requests, permanently. Settable at 2, 4, 8 or 16, and priced rather than offered below 8",
    },
    {
      n: "ILANES",
      k: "<b>unit count</b>",
      v: "8",
      w: "integer <b>IM</b> lanes — the packed ALU and its multipliers are one unit with one operand path and one result path. It narrows the <b>ALU and not the multipliers</b>, and the DSP column proves it: flat at 8, 4 and 2. <code>0</code> builds none and every integer vector encoding faults",
    },
    {
      n: "FLOAT_LANES",
      k: "<b>unit count</b>",
      v: "<b>4</b>",
      w: "binary32 fused multiply-add units. <code>0</code> builds no float tier at all and every float encoding faults. <b>Architectural when the accumulator is built</b>, because it changes the answers and not only the area",
      _tone: "good",
    },
    {
      n: "FSFU_UNITS",
      k: "<b>unit count</b>",
      v: "0",
      w: "how many of those units are <b>seed-capable</b> — <code>exp2</code>, <code>log2</code>, <code>rcp</code>, <code>rsqrt</code>. A subset of <code>FLOAT_LANES</code>, and a nonzero count <b>deepens the whole tier</b> from 6 cycles to 10 so the tier has one latency and one retire shadow",
    },
    {
      n: "PASSES",
      k: "derived",
      v: "2",
      w: "<code>SIMD / units</code> — the issue interval. A feature below full width holds the memory stage for that many cycles and <b>retires once</b>",
    },
  ],
};

/* ---- the price list, tree W, against 15,682 ---------------------------- */
const widthsW = {
  cols: [
    { key: "k", label: "knob", mono: true },
    { key: "v", label: "value", mono: true, align: "right" },
    { key: "lut", label: "LUT", mono: true, align: "right" },
    { key: "d", label: "ΔLUT", mono: true, align: "right" },
    { key: "ff", label: "FF", mono: true, align: "right" },
    { key: "bram", label: "BRAM", mono: true, align: "right" },
    { key: "dsp", label: "DSP", mono: true, align: "right" },
    { key: "f", label: "Fmax", mono: true, align: "right" },
  ],
  rows: [
    {
      k: "—",
      v: "the reference row",
      lut: "<b>15,682</b>",
      d: "—",
      ff: "9,836",
      bram: "13",
      dsp: "56",
      f: "349.3",
      _tone: "good",
    },
    {
      k: "PERM_UNITS",
      v: "2",
      lut: "15,185",
      d: "<b>−497</b>",
      ff: "10,077",
      bram: "13",
      dsp: "56",
      f: "335.8",
    },
    {
      k: "",
      v: "1",
      lut: "14,459",
      d: "<b>−1,223</b>",
      ff: "10,080",
      bram: "13",
      dsp: "56",
      f: "318.3",
    },
    {
      k: "SHIFT_UNITS",
      v: "4",
      lut: "14,970",
      d: "<b>−712</b>",
      ff: "10,085",
      bram: "13",
      dsp: "56",
      f: "344.4",
    },
    {
      k: "",
      v: "2",
      lut: "14,623",
      d: "<b>−1,059</b>",
      ff: "10,083",
      bram: "13",
      dsp: "56",
      f: "320.6",
      _tone: "good",
    },
    {
      k: "",
      v: "1",
      lut: "14,779",
      d: "<b>−903</b>",
      ff: "10,098",
      bram: "13",
      dsp: "56",
      f: "345.7",
    },
    {
      k: "the shifter GATE",
      v: "0 — shifts <b>fault</b>",
      lut: "14,757",
      d: "−925",
      ff: "9,749",
      bram: "13",
      dsp: "56",
      f: "343.5",
      _tone: "warn",
    },
    {
      k: "FLOAT_LANES",
      v: "8",
      lut: "20,063",
      d: "<b>+4,381</b>",
      ff: "13,499",
      bram: "13",
      dsp: "64",
      f: "310.4",
    },
    {
      k: "",
      v: "2",
      lut: "13,676",
      d: "<b>−2,006</b>",
      ff: "8,011",
      bram: "13",
      dsp: "52",
      f: "324.4",
    },
    {
      k: "FSFU_UNITS",
      v: "1",
      lut: "16,674",
      d: "<b>+992</b>",
      ff: "10,232",
      bram: "14.5",
      dsp: "57",
      f: "318.1",
    },
    {
      k: "",
      v: "4 — full rate",
      lut: "16,668",
      d: "<b>+986</b>",
      ff: "11,292",
      bram: "19",
      dsp: "60",
      f: "318.1",
      _tone: "warn",
    },
    {
      k: "NACC",
      v: "1",
      lut: "15,128",
      d: "−554",
      ff: "9,564",
      bram: "13",
      dsp: "56",
      f: "321.6",
    },
    {
      k: "",
      v: "4",
      lut: "16,005",
      d: "+323",
      ff: "10,362",
      bram: "13",
      dsp: "56",
      f: "341.9",
    },
    {
      k: "VREGS",
      v: "4 — half the file",
      lut: "15,647",
      d: "<b>−35</b>",
      ff: "9,846",
      bram: "13",
      dsp: "56",
      f: "327.3",
    },
    {
      k: "WB_STAGE",
      v: "1 — <b>what rv_pe ships</b>",
      lut: "15,736",
      d: "+54",
      ff: "10,110",
      bram: "13",
      dsp: "56",
      f: "341.9",
    },
    {
      k: "VREG_PRIM",
      v: "block",
      lut: "15,674",
      d: "−8",
      ff: "9,068",
      bram: "<b>25</b>",
      dsp: "56",
      f: "<b>253.7</b>",
      _tone: "bad",
    },
    {
      k: "SIMD",
      v: "4",
      lut: "11,282",
      d: "−4,400",
      ff: "9,017",
      bram: "9",
      dsp: "32",
      f: "322.9",
    },
    {
      k: "",
      v: "16",
      lut: "24,830",
      d: "+9,148",
      ff: "11,448",
      bram: "21",
      dsp: "104",
      f: "307.7",
    },
    {
      k: "SIMD_EN",
      v: "0 — the base core alone",
      lut: "2,661",
      d: "−13,021",
      ff: "4,140",
      bram: "5",
      dsp: "0",
      f: "396.5",
      _tone: "warn",
    },
    {
      k: "<b>combined</b>",
      v: "<b>PERM 1 + SHIFT 2</b>",
      lut: "<b>13,586</b>",
      d: "<b>−2,096</b>",
      ff: "10,313",
      bram: "13",
      dsp: "56",
      f: "367.8",
      _tone: "good",
    },
  ],
};

/* ---- three knobs that exist only on the later tree ---------------------- */
const widthsWp = {
  cols: [
    { key: "c", label: "change", mono: true },
    { key: "d", label: "ΔLUT", mono: true, align: "right" },
    { key: "n", label: "note" },
  ],
  rows: [
    {
      c: "ILANES 8 → 4",
      d: "<b>−1,558</b>",
      n: "<b>DSP stays at 61 across 8, 4 and 2.</b> Fabric adders and DSP columns are two budgets and one knob must not span both",
      _tone: "good",
    },
    {
      c: "ILANES 4 → 2",
      d: "−346",
      n: "FF rises 92 then 72 — the staging register the walk needs",
    },
    { c: "SHIFT_UNITS 8 → 2", d: "−777", n: "" },
    { c: "PERM_UNITS 8 → 2", d: "−742", n: "" },
    {
      c: "RED_UNITS 1 → 0",
      d: "−551",
      n: "removes <code>vredsum</code> and <code>vredmax</code>; both encodings then fault",
    },
    {
      c: "HAS_SHROUND 1 → 0",
      d: "−398",
      n: "<code>vsrari</code> rounds toward zero rather than faulting — it removes a rounding step, not an instruction",
    },
    { c: "FSFU_UNITS 1 → 0", d: "−847", n: "" },
    {
      c: "FSFU_UNITS 1 → 4 — <b>full rate</b>",
      d: "<b>−66</b>",
      n: "<b>full rate is cheaper than one unit, for four times the rate.</b> Not an artefact — see the trap below",
      _tone: "good",
    },
    {
      c: "FLOAT_LANES 4 → 8",
      d: "+4,203",
      n: "<b>1,051 LUT per fused multiply-add unit</b>",
    },
  ],
};

const marginal = {
  cols: [
    { key: "u", label: "unit" },
    { key: "a", label: "the arithmetic behind it", mono: true },
    { key: "p", label: "per unit", align: "right", mono: true },
  ],
  rows: [
    {
      u: "SIMD FP FMA",
      a: "(20,063 − 15,682)/4 and (15,682 − 13,676)/2",
      p: "<b>1,095</b> and <b>1,003</b>",
      _tone: "good",
    },
    {
      u: "SIMT FP FMA",
      a: "(19,461 − 16,307)/4 and (16,307 − 14,100)/2",
      p: "<b>789</b> and <b>1,104</b>",
      _tone: "good",
    },
    {
      u: "SIMD 32-bit slot — <code>SIMD</code>",
      a: "(15,682 − 11,282)/4 and (24,830 − 15,682)/8",
      p: "1,100 and 1,144",
    },
    {
      u: "SIMD float accumulator bank",
      a: "(15,682 − 15,128)/1 and (16,005 − 15,682)/2",
      p: "554 then 162",
    },
    {
      u: "SIMD multiply depth — the retired <code>MULS</code>",
      a: "4 → 2 is +580 LUT for −24 DSP",
      p: "a LUT-for-DSP trade, the wrong direction here",
      _tone: "warn",
    },
  ],
};

const modelErr = {
  cols: [
    { key: "r", label: "prediction row" },
    { key: "n", label: "knobs", mono: true, align: "right" },
    { key: "p", label: "predicted", mono: true, align: "right" },
    { key: "m", label: "measured", mono: true, align: "right" },
    { key: "d", label: "error", mono: true, align: "right" },
  ],
  rows: [
    {
      r: "<b>SIMD</b> — <code>PERM_UNITS</code> 1 + <code>SHIFT_UNITS</code> 2",
      n: "2",
      p: "13,400",
      m: "<b>13,586</b>",
      d: "<b>+186 · +1.4%</b>",
      _tone: "good",
    },
    {
      r: "<b>SIMT</b> — <code>WAVES</code> 8 + <code>IPDOM_D</code> 4 + a memory format off",
      n: "3",
      p: "16,812",
      m: "<b>16,552</b>",
      d: "<b>−260 · −1.5%</b>",
      _tone: "good",
    },
    {
      r: "DSP and BRAM, on both predictions",
      n: "—",
      p: "56 / 13 and 48 / 30.5",
      m: "<b>exact</b>",
      d: "<b>0</b>",
      _tone: "good",
    },
    {
      r: "rows that moved <b>seven</b> knobs at once",
      n: "7",
      p: "—",
      m: "—",
      d: "<b>up to 44%</b>, one-directional",
      _tone: "bad",
    },
  ],
};

/* ---------------------------------------------------- what it buys, in cycles */
const buys = {
  cols: [
    { key: "f", label: "Feature" },
    { key: "k", label: "Kernel", mono: true },
    { key: "s", label: "scalar", align: "right", mono: true },
    { key: "v0", label: "vector, WB 0", align: "right", mono: true },
    { key: "v1", label: "vector, <b>WB 1</b>", align: "right", mono: true },
    { key: "x", label: "speedup, <b>WB 1</b>", align: "right", mono: true },
  ],
  rows: [
    {
      f: "the permute — <code>vsldw</code>, <code>vunpk</code>, saturating <code>vpack</code>",
      k: "fir_i16_v",
      s: "3,407",
      v0: "559",
      v1: "<b>745</b>",
      x: "<b>4.6×</b>",
    },
    {
      f: "the permute, and the packed shift — <code>vslli</code> / <code>vsrari</code>",
      k: "epilogue_v",
      s: "8,028",
      v0: "245",
      v1: "<b>325</b>",
      x: "<b>24.7×</b>",
      _tone: "good",
    },
    {
      f: "the reduction trees",
      k: "reduce_i32_v",
      s: "3,093",
      v0: "251",
      v1: "<b>283</b>",
      x: "<b>10.9×</b>",
    },
    {
      f: "vector <code>vld</code> / <code>vst</code>",
      k: "memcpy32_v",
      s: "783",
      v0: "239",
      v1: "<b>271</b>",
      x: "<b>2.9×</b>",
    },
    {
      f: "<b>the seed units</b>",
      k: "<b>none exists</b>",
      s: "—",
      v0: "—",
      v1: "—",
      x: "—",
      _tone: "bad",
    },
    {
      f: "<b>the float tier itself</b>",
      k: "<b>none exists</b>",
      s: "—",
      v0: "—",
      v1: "—",
      x: "—",
      _tone: "bad",
    },
  ],
};

const params = {
  cols: [
    { key: "t", label: "Thing", mono: true },
    { key: "c", label: "Category" },
  ],
  rows: [
    {
      t: "the compute format",
      c: "<b>not a parameter at all.</b> IEEE binary32, always, in every build. There is no <code>f2f</code> convert because there is no second format to convert to",
      _tone: "good",
    },
    {
      t: "the vector width — <code>SIMD</code>",
      c: "<b>architecture.</b> It sets <code>VW = 32 · SIMD</code>, which moves the register file, the scratchpad row, the permute network and the reduction trees together. It is not a unit count",
    },
    {
      t: "ILANES · SHIFT_UNITS · PERM_UNITS · FLOAT_LANES · FSFU_UNITS · FCVT_UNITS",
      c: "<b>unit counts</b> — 0, 1, 2, 4, 8, or −1 for full rate, and a nonzero value must divide <code>SIMD</code>. A width costs cycles, never encodings; <code>0</code> means the hardware is absent and its encodings <b>fault at decode</b>",
      _tone: "good",
    },
    {
      t: "RED_UNITS · HAS_SHROUND · HAS_FALU · HAS_FACC",
      c: "<b>gates</b>, spelled as counts whose only values are 0 and 1 — one vocabulary, one way to say none. <code>HAS_SHROUND</code> is the one entry that does not fault at 0, because it removes a rounding step rather than an instruction",
    },
    {
      t: "NPART",
      c: "<b>fixed protocol when HAS_FACC is on.</b> The rotation count is part of the ISA's stated accumulation order, and float addition does not associate, so it changes the answers",
      _tone: "warn",
    },
    {
      t: "FLOAT_LANES, <i>when the accumulator is built</i>",
      c: "<b>fixed protocol too, and this is the one a reader will miss.</b> With fewer units an element's accumulate chain is a shorter strided subset of the partials, so the order changes and so do the answers. The elementwise groups carry no such contract",
      _tone: "warn",
    },
    {
      t: "NACC · VREGS · VSPAD_ENTRIES · WB_STAGE · RED_PIPE · VREG_PRIM · MEM_PRIM · USE_DSP",
      c: "structural parameters, each measured as itself",
    },
    {
      t: "SIMD_EN = 0",
      c: "the unit disappears — a <code>generate</code>, not a zero width — and the PE is the base core bit for bit",
    },
  ],
};

/* --------------------------------------------------------------- the ISA */
const rType = [
  { name: "funct7", bits: 7, value: "op<<2 | et" },
  { name: "rs2", bits: 5 },
  { name: "rs1", bits: 5 },
  { name: "funct3", bits: 3, value: "the group" },
  { name: "rd", bits: 5 },
  { name: "0001011", bits: 7, value: "custom-0", accent: true },
];

const fType = [
  { name: "funct7", bits: 7, value: "op<<2 | ft" },
  { name: "rs2", bits: 5 },
  { name: "rs1", bits: 5 },
  { name: "funct3", bits: 3, value: "FMAC/FRED/FCVT/FALU/FSFU" },
  { name: "rd", bits: 5 },
  { name: "0101011", bits: 7, value: "custom-1", accent: true },
];

const fields = {
  cols: [
    { key: "f", label: "Field", mono: true },
    { key: "w", label: "Width", align: "right", mono: true },
    { key: "p", label: "Position", mono: true },
    { key: "o", label: "Owner" },
  ],
  rows: [
    {
      f: "opcode",
      w: "7",
      p: "[6:0]",
      o: "<b>fixed.</b> <code>0x0B</code> custom-0 is the integer tier; <code>0x2B</code> custom-1 is the float tier. A build with <code>FLOAT_LANES = 0</code> leaves custom-1 unmapped, so a float instruction faults <b>at the opcode major</b> rather than inside a decode case",
    },
    {
      f: "rd",
      w: "5",
      p: "[11:7]",
      o: "the instruction. On <code>vst</code> it carries the <b>data</b> register: RV32's S-format exists to keep <code>rs1</code> and <code>rs2</code> in place for the scalar read, and a vector store's data comes from the vector file, so that constraint does not apply",
    },
    {
      f: "funct3",
      w: "3",
      p: "[14:12]",
      o: "<b>fixed.</b> Names the group — the table below is the allocation, and slot 5 on custom-0 and slot 1 on custom-1 are both spoken for by things that do not exist",
    },
    { f: "rs1", w: "5", p: "[19:15]", o: "the instruction" },
    { f: "rs2", w: "5", p: "[24:20]", o: "the instruction, or an immediate shift amount on <code>VSHI</code>" },
    {
      f: "funct7[6:2]",
      w: "5",
      p: "[31:27]",
      o: "<b>the group.</b> The operation within it — except on <code>VPRM</code>, which spends three bits on a lane index instead, because <code>vsldw</code> needs somewhere to put one and an R-type has no field left",
    },
    {
      f: "funct7[1:0]<br>on a typed integer group",
      w: "2",
      p: "[26:25]",
      o: "<b>fixed:</b> 0 = int8, 1 = int16, 2 = int32. Read <b>straight off the instruction word</b> rather than out of a decode case, so two adjacent instructions may use different widths with no state to change",
      _tone: "warn",
    },
    {
      f: "funct7[1:0]<br>on custom-1",
      w: "2",
      p: "[26:25]",
      o: "<b>fixed:</b> <code>f32</code> is the only value a build accepts. Every other value is an unmapped encoding rather than a silent reinterpretation — the field is kept where the integer tier's is precisely so that no reader has to check whether it moved",
      _tone: "warn",
    },
  ],
};

const isa = {
  cols: [
    { key: "g", label: "funct3", mono: true },
    { key: "i", label: "Instructions", mono: true },
    { key: "w", label: "What they do, and what faults" },
  ],
  rows: [
    {
      g: "0 · 1 — <code>VLD</code> / <code>VST</code>",
      i: "vld · vst",
      w: "a whole vector to or from <code>xs1 + imm</code>. <b>Vector-aligned by contract</b> — a misaligned address faults in the same stage and by the same path as any other bad address, which is what lets the wide face be a plain row index with no rotate, no merging and no second read",
    },
    {
      g: "2 — <code>VINT</code>",
      i: "vadd · vsub · vsadd · vssub · vmin · vmax · vmul",
      w: "element-wise over <code>.s8</code> / <code>.s16</code> / <code>.s32</code>: wrapping and saturating add and subtract, signed minimum and maximum, and the low half of the product — <code>vmul</code> in <code>.s8</code> and <code>.s16</code> only. Faults at <code>ILANES = 0</code>",
    },
    {
      g: "3 — <code>VBIT</code>",
      i: "vand · vor · vxor · vandn",
      w: "bitwise, untyped. <code>vandn</code> is <code>vs1 &amp; ~vs2</code>, and it is what blends a float compare's mask",
    },
    {
      g: "4 — <code>VSHI</code>",
      i: "vslli · vsrli · vsrai · vsrari",
      w: "immediate shifts at three element widths. <code>vsrari</code> is the rounding right shift — the requantise primitive, and the one operation a plain <code>vsrai</code> gets subtly wrong. Faults at <code>SHIFT_UNITS = 0</code>",
    },
    {
      g: "<b>5 — <code>VMAC</code></b>",
      i: "—",
      w: "<b>reserved and unmapped.</b> The retired integer dot unit and its accumulators lived here. Keeping the group unmapped is what makes an old binary <b>fault</b> rather than decode as something adjacent",
      _tone: "warn",
    },
    {
      g: "6 — <code>VMOV</code>",
      i: "vsplat · vextr · vredsum · vredmax",
      w: "a scalar into every slot; one slot out to a scalar register; the sum or the signed maximum of the 32-bit slots into a scalar register. A <code>vextr</code> lane index at or above <code>SIMD</code> <b>faults</b> rather than wrapping — one encoding must not mean element 5 on an eight-lane build and element 1 on a four-lane one. The two reductions fault at <code>RED_UNITS = 0</code>",
    },
    {
      g: "7 — <code>VPRM</code>",
      i: "vsldw · vpack.s16 · vpack.s32 · vunpkl/h.s8 · vunpkl/h.s16",
      w: "the cross-lane network. <code>vsldw</code> is a <b>rotate</b> of <code>{vs2, vs1}</code>, so every index is defined at every width rather than leaving a hole the RTL and the model could disagree about; pack narrows two vectors to one with signed saturation and unpack widens half a vector with sign extension. Faults at <code>PERM_UNITS = 0</code>",
    },
  ],
};

const fisa = {
  cols: [
    { key: "g", label: "funct3", mono: true },
    { key: "i", label: "Instructions", mono: true },
    { key: "w", label: "What they do, and what faults" },
  ],
  rows: [
    {
      g: "3 — <code>FALU</code>",
      i: "vfmul · vfadd · vfsub · vfma · vfmin · vfmax · vfcmplt · vfcmpgt · vfcmpeq",
      w: "the elementwise base, and what every CPU SIMD ISA ships as its float tier. <code>vfmin</code> and <code>vfmax</code> are IEEE minNum and maxNum, so <b>a NaN operand loses</b>; the three compares write <b>all ones or all zeros per element</b> into an ordinary vector register, so a branchless conditional is a compare and a bitwise blend with no new architectural state",
      _tone: "good",
    },
    {
      g: "4 — <code>FSFU</code>",
      i: "vfexp2 · vflog2 · vfrcp · vfrsqrt",
      w: "the four base-2 seeds. <b>A unit count, not a boolean:</b> <code>FSFU_UNITS</code> of the FMA units carry a seed table beside their multiply-add, and Newton refinement is an instruction sequence deliberately — <code>1/a</code> is two multiply-adds and <code>rsqrt</code> is three. Faults at 0",
    },
    {
      g: "0 — <code>FMAC</code>",
      i: "vfmacc · vfmsac · vfaccz · vfaccrd · vfaccwr",
      w: "the rotating accumulator. <b>Off by default</b> — it is the tier's <i>extra</i> rather than its floor: a vertex transform, a float dot or a long reduction justify it, and elementwise colour work does not. Faults at <code>HAS_FACC = 0</code>",
      _tone: "warn",
    },
    {
      g: "2 — <code>FCVT</code>",
      i: "vfcvt.f2i · vfcvt.i2f",
      w: "binary32 ↔ int32, <code>FCVT_UNITS</code> converters per pass. <b>Built, and never priced</b> — the group had no datapath when the campaigns below were run, so every published figure for it measures a converter that did not convert. It has one now and has not been re-measured. Faults at 0",
      _tone: "warn",
    },
    {
      g: "<b>1 — <code>FRED</code></b>",
      i: "vfredsum.f32",
      w: "<b>encoded and NOT built. It faults.</b> A cross-slot sum is a second pass that does not exist, and returning slot 0 alone would be the plausible wrong answer a refusal exists to prevent. The <b>golden model does implement it</b>, so the model is ahead of the RTL here — a kernel finishes the cross-slot sum with the integer reduction or in scalar code",
      _tone: "bad",
    },
  ],
};

const timing = {
  cols: [
    { key: "e", label: "Event" },
    { key: "c", label: "Cost" },
  ],
  rows: [
    {
      e: "ALU, logic, shift, permute, moves, <code>vld</code>, <code>vst</code> at full width",
      c: "1 cycle",
    },
    {
      e: "any of the above at <code>U &lt; SIMD</code> units",
      c: "<code>SIMD / U</code> cycles — the memory stage is held until the last pass issues, and the instruction <b>retires once</b>",
    },
    { e: "<code>vmul</code>", c: "2 cycles" },
    {
      e: "<code>vredsum</code> / <code>vredmax</code> at more than two slots",
      c: "2 cycles — <code>RED_PIPE</code> registers the level below the root",
    },
    {
      e: "read-after-write on a vector register, distance 1",
      c: "1 stall",
    },
    {
      e: "read-after-write at distance 2",
      c: "<b>1 stall at <code>SIMD_WB = 1</code>, which is what <code>rv_pe</code> ships.</b> Free at 0",
      _tone: "warn",
    },
    { e: "<code>vld</code> behind a <code>vst</code>", c: "1 stall" },
    {
      e: "a <code>vld</code> in decode behind any scalar store into the vector window",
      c: "1 cycle — a <b>bubble</b>, not a stall",
    },
    {
      e: "an elementwise float instruction",
      c: "issues once every <code>ALAT + passes</code> cycles — 7 at full width with no seed units, 11 with them",
    },
    {
      e: "<code>vfmacc</code> / <code>vfmsac</code>, <b>including back to back into the same accumulator</b>",
      c: "<code>passes</code> cycles. It retires once and each pass's accumulate lands <code>ALAT</code> later in the background",
      _tone: "good",
    },
    {
      e: "<code>vfaccz</code> / <code>vfaccwr</code>",
      c: "<code>NPART</code> cycles — a sweep of a one-write-port memory, 16 by default",
    },
    {
      e: "<code>vfaccrd</code>",
      c: "<code>NPART × (ALAT+1) + passes</code> — <b>112 + passes</b> at the defaults, and the same at every unit count",
    },
    {
      e: "<code>vfaccz</code> / <code>vfaccwr</code> / <code>vfaccrd</code> behind a float accumulate in flight",
      c: "up to <code>ALAT</code> stalls. It happens once at the end of a reduction, not inside it",
    },
  ],
};

const rules = {
  cols: [
    { key: "r", label: "Rule" },
    { key: "w", label: "What breaks otherwise" },
  ],
  rows: [
    {
      r: "<b>Vector addresses are aligned</b> to the vector width",
      w: "a misaligned <code>vld</code> or <code>vst</code> faults rather than splitting. A stencil therefore cannot reach one element earlier with a misaligned load — it loads two aligned vectors and slides them past each other",
    },
    {
      r: "<b>The vector scratchpad is store-only from the scalar side</b>",
      w: "stage data with <code>sw</code>, read it with <code>vld</code>; a scalar load of the region faults. Adding a fifth region to the scalar load mux would cost frequency on every load in every program, to serve an access a <code>vld</code> already performs better",
    },
    {
      r: "<b><code>vsldw</code> slides by slots, not by elements</b>",
      w: "sliding int16 data by one element means widening it to int32 first. The index is a range limit, not a meaning that changes with the width — the operation is “rotate <code>{v2,v1}</code> left by <i>idx</i> words” at every <code>SIMD</code>",
    },
    {
      r: "<b>Reductions cross slots; arithmetic does not</b>",
      w: "keep <code>SIMD</code> running totals in a vector register and reduce once at the end, rather than reducing inside a loop",
    },
    {
      r: "<b>A float accumulator must be zeroed before it is used</b>",
      w: "the partials are a memory and a memory has no reset, so a kernel that skips <code>vfaccz</code> reads whatever the RAM powered up holding",
      _tone: "warn",
    },
    {
      r: "<b><code>NPART</code> and <code>FLOAT_LANES</code> change float answers</b>",
      w: "neither is a tuning knob when the accumulator is built. The order is architectural and float addition does not associate, so a kernel validated at one value is <b>not validated at another</b> — and the component bench carries the unit count in its configuration guard so a mismatch names itself instead of failing as arithmetic",
      _tone: "warn",
    },
    {
      r: "<b>A build that lacks a feature faults on its encodings</b>",
      w: "a kernel written for one configuration and run on a narrower one halts with an illegal instruction at the offending program counter. A build that merely has the feature <i>narrower</i> runs the same kernel with the same answers and more cycles",
    },
    {
      r: "<b>Scalar arithmetic is RV32IM</b>",
      w: "<code>mul</code> and its three high halves are there; divide, remainder and scalar float are not. A C expression that divides calls libgcc — move the work into a vector instruction, or let the compiler strength-reduce a constant divisor to <code>mulhu</code>",
    },
  ],
};

const dropped = {
  cols: [
    { key: "d", label: "Withdrawn" },
    { key: "w", label: "Why" },
  ],
  rows: [
    {
      d: "<b>every absolute total for a PE the current RTL can build</b>",
      w: "there is none. Every published total predates the float tier's rebuild in binary32, the removal of the integer dot unit, <code>MULS</code> and <code>DOT_DSP</code>, and the converter group gaining its datapath. <b>Re-measurement against the current parameter set has not been published.</b> The delta tables above are kept because the shapes they establish are the findings",
      _tone: "bad",
    },
    {
      d: "<b>every assembled-PE total taken at <code>-flatten_hierarchy none</code></b>, and the mesh arithmetic built on them",
      w: "<b>the ship does not synthesise at <code>none</code>.</b> Nothing in the build scripts sets the flatten setting on the ship's run, so it takes Vivado's default, <code>rebuilt</code>. The gap between the two is configuration-dependent — 647 LUT at one knob setting and 243 at another, because at the first all of the difference was the tool inferring DSP48 post-adders the RTL placed explicitly at the second — so it cannot be carried as a correction. <b>A difference between two rows of one <code>none</code> flow is still sound; an absolute total from one is not.</b> The per-block census taken at <code>none</code> is unaffected and stands, because attribution <i>has</i> to be taken there",
      _tone: "bad",
    },
    {
      d: "<b>every figure for the integer dot unit, its accumulator, <code>MULS</code> and <code>DOT_DSP</code></b>",
      w: "<code>vdot</code>, <code>vdotn</code>, <code>vaccz</code>, <code>vaccrd</code>, <code>vaccwr</code>, the integer accumulator banks and both knobs are <b>gone from the RTL</b>. Those rows measure a different machine, not an older measurement of this one",
      _tone: "bad",
    },
    {
      d: "<b>every float-tier figure taken on the E8M15 datapath</b>, including both operand-format gates and every total containing them",
      w: "it is a different tier: different arithmetic, different converters, different element granularity. <b>KohakuMPE holds no E8M15 at all</b> — the compute format is IEEE binary32 and a 32-bit word holds exactly one element, so the slot count is 8 rather than 16 and nothing converts at either edge. The totals are not comparable in either direction",
      _tone: "bad",
    },
    {
      d: "<b>every accumulator area figure taken before its operation port was connected</b>",
      w: "those units were a pass-through rather than a fused multiply-add — an unconnected input port is tied to zero, and zero is a legal opcode. Any accumulator figure published before the fix priced the wrong thing",
      _tone: "bad",
    },
    {
      d: "<b>every converter-group figure</b>",
      w: "the group had no datapath when it was priced. It has one now and has not been re-measured",
      _tone: "warn",
    },
    {
      d: "<b>the tier-alone unit-count curve</b> and the linear fit taken from it",
      w: "<b>withdrawn for provenance rather than for being wrong.</b> The probe's script and module have both since been renamed, so no run can be tied to the module that exists now. It needs re-measuring, not reinstating",
      _tone: "warn",
    },
    {
      d: "<b>any total derived by subtracting a probe delta from an assembled build</b>",
      w: "arithmetic across two scopes is never a measurement",
      _tone: "bad",
    },
    {
      d: "<b>“the float tier costs <i>N</i> LUT and <i>M</i> MHz”</b>, in every form it was written",
      w: "each rested on one float unit per element being the only expressible build. Both halves of that are gone",
      _tone: "bad",
    },
  ],
};

const laneRows = [
  {
    name: "v1",
    values: [
      "01020304",
      "05060708",
      "090A0B0C",
      "0D0E0F10",
      "11121314",
      "15161718",
      "191A1B1C",
      "1D1E1F20",
    ],
  },
  {
    name: "v2",
    values: [
      "00100010",
      "00100010",
      "00100010",
      "00100010",
      "00100010",
      "00100010",
      "00100010",
      "00100010",
    ],
  },
  {
    name: "vd",
    values: [
      "01120314",
      "05160718",
      "091A0B1C",
      "0D1E0F20",
      "11221324",
      "15261728",
      "192A1B2C",
      "1D2E1F30",
    ],
  },
];

const passWalk = [
  { name: "MEM", kind: "bus", values: ["vfma", "vfma", "vfma", "vfma", "next"] },
  { name: "pass", kind: "bus", values: ["0", "1", "2", "3", "—"] },
  {
    name: "slots driven",
    kind: "bus",
    values: ["1..0", "3..2", "5..4", "7..6", "—"],
  },
  { name: "MEM held", kind: "bit", values: [1, 1, 1, 0, 0] },
  { name: "retires", kind: "bit", values: [0, 0, 0, 1, 0], mark: [3] },
];

const passBroken = [
  { name: "MEM", kind: "bus", values: ["vfma", "vfma", "vfma", "next", "—"] },
  { name: "pass", kind: "bus", values: ["0", "1", "2", "—", "—"] },
  {
    name: "slots driven",
    kind: "bus",
    values: ["1..0", "3..2", "5..4", "—", "—"],
  },
  { name: "MEM held", kind: "bit", values: [1, 1, 0, 0, 0] },
  {
    name: "slots 7..6",
    kind: "text",
    values: ["", "", "", "never written", ""],
    mark: [3],
  },
];
</script>

<template>
  <DocPage
    title="SIMD PE"
    summary="The framework's RV32IM controller core with a wide uniform datapath attached to its execute stage — one program counter, one instruction, eight 32-bit slots. What it is, why every compute feature is a unit count rather than a switch, why there is no format knob anywhere, and what each width costs."
    domain="simd"
    status="measured"
    source="src/kohakumpe/simd/ · docs/projects/kohakumpe/simd/ · configurable-widths.md · unit-counts.md"
  >
    <h2 class="doc-h2">What it owns</h2>
    <p class="doc-p">Four things, and nothing else.</p>
    <div class="grid gap-3 sm:grid-cols-2 my-5">
      <div class="card p-4">
        <div
          class="kt-text-caption font-semibold text-warm-800 dark:text-warm-200 mb-1"
        >
          A packed integer tier
        </div>
        <p class="kt-text-caption text-warm-500 dark:text-warm-400 leading-6">
          Eight 32-bit slots, each cut into 4 × int8, 2 × int16 or 1 × int32 by
          a mask — so one instruction is 32 byte additions.
        </p>
      </div>
      <div class="card p-4">
        <div
          class="kt-text-caption font-semibold text-warm-800 dark:text-warm-200 mb-1"
        >
          A binary32 float tier
        </div>
        <p class="kt-text-caption text-warm-500 dark:text-warm-400 leading-6">
          Elementwise arithmetic, four seeds, converters, and a
          <b>rotating accumulator</b> whose order is part of the ISA.
        </p>
      </div>
      <div class="card p-4">
        <div
          class="kt-text-caption font-semibold text-warm-800 dark:text-warm-200 mb-1"
        >
          Two more memories
        </div>
        <p class="kt-text-caption text-warm-500 dark:text-warm-400 leading-6">
          A 256-bit vector scratchpad with two faces, and a vector register file
          — both this project's design, both behind the framework's port.
        </p>
      </div>
      <div class="card p-4">
        <div
          class="kt-text-caption font-semibold text-warm-800 dark:text-warm-200 mb-1"
        >
          A framework slot occupant
        </div>
        <p class="kt-text-caption text-warm-500 dark:text-warm-400 leading-6">
          <span class="chip">khs_unit</span> is what the framework's
          <span class="chip">SIMD_EN</span> parameter names, and at 0 the PE is
          the base core bit for bit.
        </p>
      </div>
    </div>

    <p class="doc-p">
      Everything the base core is stays true: RV32IM, in order, single issue,
      one port on the fabric, the same kick and the same completion. What is
      added is a second register file, a second scratchpad, and an array of
      units that all execute <b>the same instruction at the same time</b>. The
      scalar half keeps doing what it is good at — addresses, trip counts,
      branches — and <b>the vector unit never computes an address and never
      takes a branch</b>. A loop is a scalar loop whose body happens to move 32
      bytes at a time.
    </p>

    <p class="doc-p">
      The alternative that was rejected is per-lane control: masks, predication
      and an address per lane, which would make this one machine that covers
      both the uniform and the divergent case. It loses on cost in the uniform
      direction — an active mask, a divergence stack and a lane-serialising
      load/store unit are hardware a uniform kernel cannot use and would still
      pay for. That case is a
      <RouterLink to="/mpe/simt" class="doc-link">different machine</RouterLink>,
      and the two land within 1% of each other at matched widths, which is what
      makes the split a design rather than a preference.
    </p>

    <h2 class="doc-h2">What it costs</h2>

    <Fig
      caption="The scalar core keeps the addresses and the control flow; the vector unit keeps the elements. Three things cross the boundary between the two halves and only three: the address rs1 + imm computed by the EX adder, a scalar operand for vsplat, and — in the other direction — a stall. With SIMD_EN = 0 the whole right-hand column disappears, a generate rather than a zero width, leaving the base core bit for bit."
      zoom
    >
      <BlockDiagram :nodes="shape.nodes" :edges="shape.edges" />
    </Fig>

    <Callout kind="measured" title="The reference row">
      <div
        class="font-mono kt-text-body whitespace-pre text-warm-700 dark:text-warm-300 leading-7 overflow-x-auto my-1"
      >
        {{ reference }}
      </div>
      <p>
        Assembled <code>rv_pe</code> on
        <code>xcvu13p-fhgb2104-2L-e</code>, Vivado 2024.2,
        <b>out-of-context synthesis only</b>,
        <code>-flatten_hierarchy rebuilt</code>,
        <code>-directive default</code>, at a 3.333 ns request. Nothing is
        placed and nothing is routed, and the frequency is a screen for a
        structural problem rather than a result.
      </p>
      <p>
        <b>This is one named generic set, not every feature at maximum.</b> A
        LUT figure without its configuration is not a measurement — the reader
        fills the gaps with zeros and prices a bare core against a fully
        featured one. Three things a reader will assume are on are off here:
        the float accumulator, the converters, and the seed units.
      </p>
      <div
        class="font-mono kt-text-caption whitespace-pre text-warm-700 dark:text-warm-300 leading-6 overflow-x-auto my-2"
      >
        {{ refCfg }}
      </div>
    </Callout>

    <Callout
      kind="trap"
      title="No absolute total on this page describes a PE the RTL can build today"
    >
      <p>
        The float tier was rebuilt from an E8M15 datapath with two operand
        formats into a binary32-only one, which deleted both operand converters;
        the integer dot unit, its accumulator, the
        <code>MULS</code> multiplier-depth knob and the
        <code>DOT_DSP</code> mapping knob were removed; the
        <code>HAS_SHIFT</code> / <code>HAS_PERM</code> /
        <code>HAS_FLOAT</code> booleans went in favour of the counts alone; and
        the converter group gained the datapath it had been missing.
        <b>Re-measurement against the current parameter set has not been
        published.</b>
      </p>
      <p>
        The rows below are kept because <b>the shapes are the findings</b>: what
        a marginal unit costs, where a width pays and where it does not, and
        which knobs are not levers at all. The symptom of ignoring this is a
        mesh total — one of these figures multiplied by a PE count — which
        prices a machine that cannot be built, wrong in an unknown direction
        rather than merely stale.
      </p>
    </Callout>

    <h2 class="doc-h2">Every compute feature is a width, and 0 means not built</h2>

    <div
      class="font-mono kt-text-caption whitespace-pre text-warm-700 dark:text-warm-300 leading-6 overflow-x-auto my-3"
    >
      {{ granule }}
    </div>

    <Callout
      kind="rule"
      title="A width costs cycles, never encodings — and 0 faults"
    >
      <p>
        A feature with <code>U</code> units serving <code>SIMD</code> slots
        issues <code>SIMD / U</code> passes, one per cycle, sequenced by the
        hardware. <b>The ISA carries no count</b>: the same instruction, the
        same binary and the same golden memory image run at every width, and the
        only visible difference is cycles.
      </p>
      <p>
        <b>A width at zero means the feature is not built</b>, and every
        encoding that would need it <b>MUST</b> fault at decode. Faulting is
        part of the contract rather than a nicety — a feature that decodes with
        no datapath returns a plausible wrong answer, which is worse than
        refusing. And <b>a width at full costs nothing</b>: at
        <code>U == SIMD</code> the hardware is the plain un-walked array,
        because the sequencing logic exists only in the narrow branch.
      </p>
    </Callout>

    <SpecTable
      :cols="counts.cols"
      :rows="counts.rows"
      caption="Five numbers that are routinely collapsed into one. The vector width is architecture; the unit counts are the parameters; the pass count falls out of the two"
    />

    <WaveTrace
      :rows="passBroken"
      variant="broken"
      label="A width that does not divide the slot count — the pass count truncates"
      :notes="[
        {
          cycle: 2,
          text: 'Three units over eight slots. The pass count is a truncating divide, so the walk runs three passes and covers six slots.',
          tone: 'bad',
        },
        {
          cycle: 3,
          text: 'The instruction retires with the top two slots never written. The build elaborated cleanly, synthesised, and reported a plausible frequency — so this fails only in a component bench, on a workload, or on silicon. It is refused at ELABORATION instead.',
          tone: 'bad',
        },
      ]"
    />

    <WaveTrace
      :rows="passWalk"
      variant="fixed"
      label="Two float units over eight slots — four passes, one instruction"
      :notes="[
        {
          cycle: 0,
          text: 'A float unit issues one operation per cycle at every count. What a narrow count costs the INSTRUCTION is the pass walk: unit u on pass p serves element p·U + u, and the memory stage is held until the last pass has gone.',
          tone: 'good',
        },
        {
          cycle: 3,
          text: 'The vector file is written, the scalar writeback fires, and the accumulator index advances exactly ONCE, whatever the unit count. Nothing in the program sees the passes.',
          tone: 'good',
        },
      ]"
    />

    <Callout
      kind="trap"
      title="A width that does not divide the element count elaborates cleanly"
    >
      <p>
        That is the whole reason the rule is enforced at <b>elaboration</b>,
        written as an instantiation of a module that does not exist so the error
        names the rule that broke:
        <span class="chip"
          >Module &lt;khs_unit_requires_PERM_UNITS_to_divide_SIMD&gt; not
          found</span
        >. A refusal at elaboration is the only place the mistake is cheap.
      </p>
      <p>
        The rules: every width is 0, −1, or divides the element count and does
        not exceed it; <code>FSFU_UNITS &lt;= FLOAT_LANES</code>, because a seed
        unit <i>is</i> a float unit; a float <b>group</b> with no units is
        refused rather than silently given the widest tier;
        <code>HAS_SHROUND</code> requires <code>SHIFT_UNITS &gt; 0</code>; and
        the tier's declared latency must equal the depth its array builds.
      </p>
    </Callout>

    <Callout
      kind="rule"
      title="The integer width is the address path; the float width is not"
    >
      <p>
        A contiguous 32-bit load by eight slots is exactly one memory read
        request, one native memory entry, one flit payload — the strongest
        machine-level alignment in the design. Narrow the <i>vector</i> and
        every coalesced load becomes two or more requests, for every kernel,
        permanently. That asymmetry is the whole justification for the shape: it
        is why <i>four integer lanes and four float lanes</i> is rejected — it
        halves the memory alignment to buy what the float knob already buys —
        and why <i>fewer integer lanes than float lanes</i> is rejected
        outright, because it starves addressing to feed arithmetic.
      </p>
      <p>
        <b><code>ILANES</code> is nonetheless a real width</b>, and narrowing it
        costs cycles rather than alignment: it narrows the ALU and not the
        multipliers, and the register width does not move with it. What is fixed
        is <code>SIMD</code>, the register width; what is configurable is how
        many lanes serve it per pass.
      </p>
    </Callout>

    <h2 class="doc-h2">There is one float format, and it is not a setting</h2>

    <div
      class="font-mono kt-text-body whitespace-pre text-warm-700 dark:text-warm-300 leading-7 overflow-x-auto my-3"
    >
      {{ dtype }}
    </div>

    <p class="doc-p">
      <b>The compute format is IEEE binary32 throughout</b> and there is no knob
      for it. A 32-bit word holds exactly one element, so the float tier's slot
      count <i>is</i> <code>SIMD</code> and <b>nothing converts at either
      edge</b>. <b>Denormals flush to sign-preserved zero</b> on input and
      output, which is D3D11's functional requirement rather than a shortcut —
      gradual-underflow hardware would be non-conformant as well as expensive.
      There is no <code>f2f</code> convert, because there is no second format to
      convert to.
    </p>

    <Callout kind="trap" title="KohakuMPE holds no E8M15">
      <p>
        <code>khs_fp32_alu.v</code>, <code>khs_fp32_sfu.v</code> and
        <code>khs_fcvt.v</code> are this project's and compute in binary32.
        <b>KohakuTPU's vector core is the one that computes in E8M15</b>, a
        24-bit internal format, with its own modules — and none of it is on any
        KohakuMPE path. A precision figure quoted from one project says nothing
        about the other, and a total that contains an E8M15 datapath is not a
        stale measurement of this one: it is a measurement of a different
        machine.
      </p>
      <p>
        <code>rv_fpu.v</code> sits in the <b>framework</b> rather than in either
        project, because RV32F is a standard extension over binary32 and
        binary32 is nobody's private format. That single-sourcing is why a SIMD
        float result and a SIMT float result agree element for element.
      </p>
    </Callout>

    <h2 class="doc-h2">What is a parameter, and what is not</h2>

    <SpecTable
      :cols="params.cols"
      :rows="params.rows"
      caption="Both cores spell “none” the same way: a count of 0. The booleans that used to sit beside counts are gone, because a boolean beside a count is two ways to say one thing — and the same 0 once meant the WIDEST possible float tier here and NO float tier next door, so a caller that forgot the parameter got opposite machines from the two cores"
    />

    <h2 class="doc-h2">The price list</h2>

    <div
      class="font-mono kt-text-caption whitespace-pre text-warm-700 dark:text-warm-300 leading-6 overflow-x-auto my-3"
    >
      {{ dspForm }}
    </div>

    <SpecTable
      :cols="widthsW.cols"
      :rows="widthsW.rows"
      caption="One frozen source tree, out-of-context SYNTHESIS on xcvu13p-fhgb2104-2L-e, Vivado 2024.2, -flatten_hierarchy rebuilt, -directive default, at a 3.333 ns request. Every cell is measured; a knob point that was not synthesised is absent rather than inferred. Fmax in MHz, and it is a screen: no decision recorded here was made on it."
    />

    <Callout
      kind="trap"
      title="A width can beat deleting the feature, and a cross-lane width pays only at one or two units"
    >
      <p>
        <code>SHIFT_UNITS</code> 2 recovers <b>1,059 LUT</b> of the 925 that
        removing the shifter entirely saves — <b>more than deletion</b> — and
        keeps every shift instruction. The arithmetic that refuses a width here
        charges one operand mux per shifter <i>removed</i>, where a walk pays
        one mux per unit <i>kept</i>: at two units that is two muxes against six
        shifters, not one against one.
      </p>
      <p>
        The permute curve is the same shape from the other side and it is
        strongly non-linear: 8 units and 2 units differ by 497 LUT, and 2 and 1
        by 726. A narrow build is a <b>direct select</b>, not a narrowed
        network — a butterfly routes every lane at once and cannot be sliced, so
        one output lane is a <code>2 × SIMD</code>-to-1 32-bit mux either way.
        That mux is what the width pays for, and it is why one unit recovers
        only part of what deleting the feature saves.
      </p>
      <p>
        <b>The default costs exactly zero.</b> At full width the original
        full-width form is kept in its own elaboration branch and the walk
        exists only in the narrow one, so both knobs are byte-identical to the
        reference in every column until they are used.
      </p>
    </Callout>

    <Callout
      kind="trap"
      title="A fractional seed rate is worst in the middle, and this is a property rather than one campaign's oddity"
    >
      <p>
        Four seed units of eight measured <b>cheaper than two</b>, on the SIMT
        core, over five points on one frozen tree. Splitting each row into seed
        hardware at 276 LUT a unit leaves a residual — the walk — of 185, 674,
        −78 and 0 at one, two, four and eight units, and that residual is the
        sum of two terms moving in <b>opposite directions</b>. The
        <b>placement mux</b> does not exist at one unit and grows with the
        count; the <b>pass decode</b> has <code>SIMD/U</code> values and shrinks
        with it. At full rate both index arms are the same expression and the
        mux folds entirely, which is why a full-rate seed tier is pure seed
        hardware.
      </p>
      <p>
        <b>A second, independent measurement on this core gives the same
        shape</b>, which is what makes it a rule rather than an observation:
        full rate measured <b>66 LUT below</b> one unit, for four times the
        rate. <b>Take the seed count equal to the float count and spend the DSP
        and BRAM, or take one unit.</b> The middle is the one place not to sit.
      </p>
    </Callout>

    <SpecTable
      :cols="widthsWp.cols"
      :rows="widthsWp.rows"
      caption="ILANES, RED_UNITS and HAS_SHROUND do not exist on the tree above. Their reference row is a different one — 16,782 LUT, 10,487 FF, 61 DSP, 14.5 BRAM, at SIMD 8, ILANES 8, FLOAT_LANES 4, FSFU_UNITS 1 — and these deltas are never subtracted against the table before them."
    />

    <h2 class="doc-h2">Price a unit marginally, never by dividing the tier</h2>

    <Callout
      kind="rule"
      title="A tier's total divided by its unit count is a measurement of the overhead"
    >
      <p>
        A float tier is not <code>units × cost(unit)</code>. It is
        <code>units × cost(unit)</code>
        <b>plus a fixed overhead that does not scale with units at all</b>: the
        third register-file read port the fused multiply-add's addend needs, the
        retire path, the scoreboard, and the pass sequencer. That overhead is
        paid once, at any nonzero unit count.
      </p>
      <p>
        So dividing charges the units for it, and the error is not small.
        <b>The average is not a worse estimate of the marginal cost; it is a
        measurement of a different thing.</b> Every average ever computed from a
        tier total on either core has concluded that one core's float unit is
        far dearer than the other's — and the marginals below say the two
        <b>bracket each other</b>.
      </p>
    </Callout>

    <SpecTable
      :cols="marginal.cols"
      :rows="marginal.rows"
      caption="Each figure is the difference between two synthesised rows one step apart, divided by the change in that count, with the subtraction written out. The two float-unit columns disagree by design and both are given: this PE's unit is dearer at the wide end and the SIMT PE's at the narrow end, so a single number would be an average — and this page does not average."
    />

    <Callout
      kind="trap"
      title="SIMD does not beat SIMT on LUT at matched features"
    >
      <p>
        With the mask, the divergence stack, the shuffle and the banked shared
        memory all off, at 8 fused multiply-adds and 8 multiply units, the SIMT
        PE measures <b>16,118</b>. This PE's comparable figure — its own
        reference less the packed shifter and the permute — is
        <b>16,775</b>, which is <b>657 LUT, 4.1%, dearer.</b>
      </p>
      <p>
        That is not a defect to remove. This PE is <b>not a subset</b> of the
        other: it carries packed int8/int16/int32 lanes, a cross-lane permute
        network, a vector scratchpad and optionally a rotating float
        accumulator, and every one of those is priced above. And its base PE is
        <b>543 LUT cheaper</b> than the SIMT one — 10,309 against 10,852 — even
        while carrying the shifter, the permute network and thirty-two
        multipliers, so the divergence hardware is real and this PE does not pay
        for it.
      </p>
    </Callout>

    <SpecTable
      :cols="modelErr.cols"
      :rows="modelErr.rows"
      caption="Re-estimating a single-knob row proves nothing — that row IS the point its own term came from. These moved knobs no term was fitted on. Two prediction rows, five knobs, and the two LUT errors have OPPOSITE signs, so adding single-knob deltas is unbiased here rather than systematically optimistic. Where the model is weak: it cannot see two features that share control logic, so removing features TOGETHER saves more than removing them one at a time — an estimate used as a ceiling is safe, and one used as a floor is not."
    />

    <h2 class="doc-h2">One instruction, eight slots</h2>

    <LaneGrid
      :lanes="8"
      :rows="laneRows"
      caption="One vadd.s16, eight slots, sixteen int16 additions. Slot L reads bits 32L+31..32L of each source and writes the same bits of the destination — nothing is broadcast, nothing is muxed, and no slot can see another slot's data. That is why the array costs almost exactly ILANES times one lane, and why frequency barely moves as it widens: widening adds copies, not depth"
    />

    <p class="doc-p">
      Every integer arithmetic instruction carries a two-bit
      <b>element type</b>, so a <code>vadd.s8</code> at eight lanes adds 32
      pairs of bytes in one cycle. Element 0 is at the <i>low</i> end — the same
      order a little-endian byte array arrives in, so an int8 vector loaded from
      memory is already packed correctly with no shuffling. How that is built
      out of one native carry chain, and why the obvious construction is the
      slow one, is on the
      <RouterLink to="/mpe/simd/microarchitecture" class="doc-link"
        >microarchitecture page</RouterLink
      >.
    </p>

    <h2 class="doc-h2">The encoding</h2>

    <BitField
      :fields="rType"
      caption="R-type on custom-0 (0x0B): funct3 selects the group and funct7 the operation within it, with the low two bits carrying the element type"
    />
    <BitField
      :fields="fType"
      caption="R-type on custom-1 (0x2B): the same shape, and funct7[1:0] is the float type — f32 is the only value a build accepts"
    />

    <SpecTable
      :cols="fields.cols"
      :rows="fields.rows"
      caption="The owner column is what tells a reader which bits are theirs. Everything marked fixed is protocol between four consumers generated from or checked against one field table — the assembler, the golden model, the RTL decode header and the C intrinsic header — and a test encodes and decodes every instruction through all four and fails on any disagreement."
    />

    <SpecTable
      :cols="isa.cols"
      :rows="isa.rows"
      caption="The integer tier: 56 instructions on custom-0. Every group's encodings fault when its unit count is 0 — an encoding must be RECOGNISED to be refused, so the decode is not itself gated"
    />

    <SpecTable
      :cols="fisa.cols"
      :rows="fisa.rows"
      caption="The float tier: 21 instructions on custom-1, and the two rows that are not instructions the hardware performs. FLOAT_LANES = 0 elaborates none of it and leaves custom-1 unmapped, so a float instruction faults at the opcode major rather than landing in a decode case that computes something plausible"
    />

    <Callout
      kind="trap"
      title="An encoding test proves nothing about execution"
    >
      <p>
        An instruction can round-trip perfectly through all four consumers, set
        a write enable, and have <b>no datapath behind it</b>. Two instances
        have been fixed on this PE: a converter group whose registered decode
        signals had no branch in the <b>result mux</b>, so its instructions
        wrote the integer lane's output; and an accumulator whose float units
        were instantiated without connecting their operation port, so the tool
        tied it to opcode zero — a pass-through — and the tier neither
        multiplied nor accumulated.
      </p>
      <p>
        <b>No bench built from the decode can catch it</b>, because the
        generator, the golden model and the RTL are all written from one
        instruction table, so a feature missing from the <i>datapath</i> is
        missing from all three consistently. Three checks find the whole class:
        follow the decode register to the <b>result</b> and count its reads;
        grep the synthesis log for <code>[Synth 8-7071]</code> and keep only the
        <b>inputs</b>, because an unconnected input is tied to zero and zero is
        usually a legal opcode; and <b>read the area column</b> — those
        accumulator units synthesised at roughly a fifth of what a working unit
        costs, and a full multiply-add cannot be that small.
      </p>
    </Callout>

    <h2 class="doc-h2">Latencies, and where every stall comes from</h2>

    <SpecTable
      :cols="timing.cols"
      :rows="timing.rows"
      caption="ALAT is the float tier's latency: 6 cycles with no seed units and 10 with any, because a seed is four stages deeper and the multiply-add path pads to match so the tier has ONE latency and one retire shadow. WB_STAGE is where the vector file is written: 0 keeps the RAW hazard at distance 1 and puts read-compute-write in one cycle; 1 registers the result first, costing a second stall and halving the path. rv_pe defaults SIMD_WB to 1 and khs_unit's own WB_STAGE default is 0, so what a bench builds depends on which level it instantiates — a component bench must set it explicitly to match what ships."
    />

    <Callout
      kind="rule"
      title="One elementwise float instruction is in flight at a time, and the cause is the writeback"
    >
      <p>
        The issue interval for the elementwise groups is
        <code>ALAT + passes</code> rather than <code>passes</code> alone. Each
        pass places its results into a <b>single staging register</b> and the
        whole register is written to the vector file when the last pass lands.
        That makes the write port a mux rather than an arbitration and needs no
        per-element write enable — but the staging register is <b>shared</b>, so
        two instructions in flight would overwrite each other, and the
        scoreboard does not catch it because it only blocks <i>dependent</i>
        instructions.
      </p>
      <p>
        Serialising is the correct fix and the cheap one, and it is what ships.
        <b>The accumulator group does not have this limit</b>: a
        <code>vfmacc</code> issues back to back, including into the same
        accumulator, because the rotation breaks the recurrence. The
        <RouterLink to="/mpe/simt" class="doc-link">SIMT PE</RouterLink> does not
        have it either, and the difference is exactly the missing write enable —
        its register file has a per-lane one, so a pass writes straight into it
        with a constant source and there is no staging register to share.
      </p>
    </Callout>

    <h2 class="doc-h2">What the widths buy, in cycles</h2>

    <SpecTable
      :cols="buys.cols"
      :rows="buys.rows"
      caption="The same workload written twice, scalar and vector, from tests/pe/tools/rv_simd_kernels.py, run on the assembled PE with real routers, the real memory agent and RAM behind it. Both writeback settings are given because the bench does not default to the shipped one. No verdict moves between the two columns: the shipped writeback costs between 1.9% and 33.3% of a vector kernel's cycles, and every feature still wins by 2.9× to 24.7×."
    />

    <Callout
      kind="note"
      title="Why the two writeback columns are comparable, and what the control is"
    >
      <p>
        Both were taken on the same tree in the same session, differing only in
        the writeback define. That is the claim; this is the control that tests
        it. The writeback is inside the vector result path and touches nothing
        else, so a <b>scalar</b> kernel must be unaffected — and all seven
        scalar kernels are identical <b>to the cycle</b> across the pair. If
        anything else had drifted between the runs it would almost certainly
        have moved one of the seven. <b>A paired cycle measurement without a
        control of this kind is two runs, not a comparison.</b>
      </p>
      <p>
        The second half of believing it is that the cost lands where the
        mechanism predicts rather than evenly: the two kernels built from long
        chains of dependent vector operations pay +33%, and distance 2 is
        exactly what the shipped writeback turns into a stall, while the kernel
        that was never dependency-bound pays two cycles.
      </p>
    </Callout>

    <Callout
      kind="open"
      title="The float tier has no kernel evidence at all"
    >
      <p>
        The last two rows above are the weakest part of this page and are stated
        rather than omitted. Nothing under <code>compiler/</code> references
        this PE or any of its instructions, and its only kernel library contains
        <b>zero float instructions</b>. The integer features each have a paired
        kernel representing real work; the float tier — <b>the largest single
        block in the PE</b> — has a component bench and a golden model and no
        workload.
      </p>
      <p>
        So any statement that the float tier is validated means
        <i>validated against a model</i>. The question that raises is not
        whether the integer extras earn their LUT — measured, they do — but
        whether this PE has a float workload at all, which is a
        <b>compiler</b> question and not an RTL one.
      </p>
    </Callout>

    <h2 class="doc-h2">Rules a kernel must respect</h2>

    <SpecTable :cols="rules.cols" :rows="rules.rows" />

    <h2 class="doc-h2">Figures that were not carried forward</h2>

    <Callout
      kind="open"
      title="A number that was true of an older build is a measurement of a different machine"
    >
      <p>
        The PE changed substantially — the integer dot unit and its accumulators
        were removed, every feature became a unit count, and the float tier's
        format changed. A figure from before that is not a worse measurement of
        this machine; it is a measurement of another one. Each was dropped
        rather than updated, and each is listed so nobody re-derives it.
      </p>
    </Callout>

    <SpecTable :cols="dropped.cols" :rows="dropped.rows" />
  </DocPage>
</template>
