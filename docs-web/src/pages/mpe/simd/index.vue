<script setup>
/* SIMD PE — what the machine is and what it costs.
 *
 * PROVENANCE. Resource and frequency figures are out-of-context synthesis on
 * xcvu13p-fhgb2104-2L-e, Vivado 2024.2, synth only. TWO ASKS are in play and
 * every table says which: 2.857 ns (350 MHz) for the reference float build,
 * which does not close with slack to spare, and 3.333 ns (300 MHz) for the
 * integer-only unit and the block probes. Rows from the two are NOT comparable.
 * Cycle figures: the PE's own CTL_CYCLE counter on the full system.
 * Source: docs/projects/kohakumpe/simd/.
 */

const reference = `SIMD PE, 8 integer lanes + 4 float lanes

  13,772 LUT  ·  10,126 FF  ·  13 BRAM  ·  72 DSP48  ·  353.4 MHz`;

const dtype = `FP32 or FP16 operands in   ->   E8M15 compute   ->   FP32 or FP16 out`;

const granule = `   8 lanes x 32 bit  =  256 bit  =  one native memory entry  =  one flit

   integer lanes    the memory granule                      8, FIXED
   float ELEMENTS   register width / element width         16, DERIVED
   float LANES      arithmetic demand (throughput vs LUT)    4, A KNOB`;

const meshSum = `   8 SIMD PEs    8 x 13,772  =  110,176
   4 SIMT PEs    4 x 21,586  =   86,344
                              --------
                               196,520      the PE array, under 200k
   2 controllers                  4,954
                              --------
                               201,474      against a ~350k budget

   float throughput   8 x 4  +  4 x 8  =  64 FMA / clock`;

const dspSum = `   8 integer lanes  x  (4 khs_mul + 4 cascaded DSP48 for the dot sum)  =  64
   4 float lanes    x  2 (vec_alu's DSP-E and DSP-M)                     =   8
                                                                      -------
                                                                          72`;

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
      label: "the base RV32I core",
      sub: "addresses, trip counts, branches",
    },
    {
      id: "vu",
      x: 19,
      y: 6,
      w: 16,
      h: 4.4,
      label: "the vector unit — khs_unit",
      sub: "the elements, all of them",
      accent: true,
    },
    {
      id: "x",
      x: 0,
      y: 12.4,
      w: 14,
      h: 3.2,
      label: "x0..x31 · 32 bits",
      sub: "NO multiply, NO float",
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
      id: "acc",
      x: 19,
      y: 16.4,
      w: 16,
      h: 3.2,
      label: "acc0, acc1 · 8 × int32",
      sub: "one vector register wide",
    },
    {
      id: "facc",
      x: 19,
      y: 20.4,
      w: 16,
      h: 3.2,
      label: "facc0, facc1 · 16 E8M15 slots",
      sub: "over 16 rotating partials",
    },
    {
      id: "vsp",
      x: 19,
      y: 24.4,
      w: 16,
      h: 3.2,
      label: "vector scratchpad · 256-bit",
      sub: "1024 × 256, 8 BRAM = SIMD banks",
    },
    {
      id: "lanes",
      x: 19,
      y: 28.4,
      w: 16,
      h: 3.4,
      label: "lane0 … lane7   flane0 … flane3",
      sub: "8 integer, 4 float",
      accent: true,
    },
  ],
  edges: [
    { from: "pc:b", to: "sc:t", dir: "v" },
    { from: "pc:b", to: "vu:t", dir: "v", accent: true },
    { from: "sc:b", to: "x:t", dir: "v" },
    { from: "x:b", to: "sp:t", dir: "v" },
    { from: "vu:b", to: "v:t", dir: "v" },
    { from: "v:b", to: "acc:t", dir: "v" },
    { from: "acc:b", to: "facc:t", dir: "v" },
    { from: "facc:b", to: "vsp:t", dir: "v" },
    { from: "vsp:b", to: "lanes:t", dir: "v" },
    { from: "sc:r", to: "vu:l", dir: "h", label: "rs1 + imm" },
  ],
};

const counts = {
  cols: [
    { key: "n", label: "quantity", mono: true },
    { key: "k", label: "kind" },
    { key: "v", label: "at the reference", align: "right", mono: true },
    { key: "w", label: "What it is" },
  ],
  rows: [
    {
      n: "integer lanes",
      k: "parameter, but fixed in practice",
      v: "8",
      w: "<b>the address path.</b> 8 × 32 bit is one flit and one <code>MEM_RD_REQ</code>; narrowing it turns every coalesced load into two or more requests, permanently",
    },
    {
      n: "float elements",
      k: "<b>derived</b>",
      v: "16",
      w: "a register width divided by an element width. Nobody's choice, and it is what the accumulator is sized in",
    },
    {
      n: "float lanes",
      k: "<b>parameter</b>",
      v: "<b>4</b>",
      w: "how many float lanes are BUILT. Also <b>architectural</b> — it changes the accumulation order, and float addition does not associate",
      _tone: "good",
    },
    {
      n: "passes",
      k: "derived",
      v: "4",
      w: "<code>elements / lanes</code> — the issue interval. A <code>vfmacc</code> holds MEM for that many cycles and <b>retires once</b>",
    },
  ],
};

const conversions = {
  cols: [
    { key: "c", label: "conversion", mono: true },
    { key: "w", label: "What it costs" },
  ],
  rows: [
    {
      c: "FP16 → E8M15",
      w: "<b>exact.</b> An 8-bit exponent covers FP16's range with room, and a subnormal normalises into an ordinary value",
      _tone: "good",
    },
    {
      c: "FP32 → E8M15",
      w: "<b>mantissa only.</b> E8 <i>is</i> FP32's exponent field, verbatim, so nothing about the range is lost; 23 mantissa bits round to 15, and an FP32 subnormal flushes because E8M15's smallest normal is 2⁻¹²⁶",
      _tone: "good",
    },
    {
      c: "E8M15 → FP16",
      w: "<b>the one conversion that is both lossy and range-limited.</b> It rounds to 10 mantissa bits and <b>saturates a finite overflow at 65504</b> rather than producing an infinity",
      _tone: "warn",
    },
  ],
};

const precision = {
  cols: [
    { key: "f", label: "Format" },
    { key: "e", label: "rel. error, half ulp", align: "right", mono: true },
  ],
  rows: [
    { f: "FP16 — what a mobile fragment shader runs at", e: "4.9e-4" },
    {
      f: "<b>E8M15 — what every partial sum here carries</b>",
      e: "<b>1.5e-5</b>",
      _tone: "good",
    },
    { f: "FP32", e: "6.0e-8" },
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
      c: "<b>not a parameter at all.</b> E8M15, always, in every build",
      _tone: "good",
    },
    {
      t: "operand width",
      c: "<b>not a parameter</b> — it is a field of the instruction word",
      _tone: "good",
    },
    {
      t: "SIMD_FLOAT",
      c: "a <b>presence</b> switch: float tier, or no float tier and custom-1 unmapped. It does not select a format",
    },
    {
      t: "SIMD_FLOAT_LANES",
      c: "a <b>width</b> knob — and <b>architectural</b>, because it changes the accumulation order and therefore the answers",
    },
    {
      t: "SIMD_NPART",
      c: "<b>fixed protocol</b> — the rotation count is part of the ISA's stated order",
    },
    {
      t: "SIMD_LANES, SIMD_VREGS, SIMD_NACC, SIMD_MULS, SIMD_SHIFT, SIMD_PERM, SIMD_VSPAD",
      c: "ordinary parameters, each measured as itself",
    },
    {
      t: "SIMD_DOTDSP, SIMD_WB",
      c: "parameters, <b>both defaulting to 1</b> on <code>rv_pe</code> — they pay only at a binding constraint, and each changes a latency",
      _tone: "warn",
    },
    {
      t: "SIMD_EN = 0",
      c: "the unit disappears — generate, not zero-width — and the PE is the base core bit for bit",
    },
  ],
};

const knobs = {
  cols: [
    { key: "a", label: "ask", align: "right", mono: true },
    { key: "k", label: "knobs" },
    { key: "l", label: "LUT", align: "right", mono: true },
    { key: "f", label: "Fmax", align: "right", mono: true },
    { key: "w", label: "" },
  ],
  rows: [
    {
      a: "2.857 ns",
      k: "neither",
      l: "14,982",
      f: "322.0",
      w: "the assembled 8 int + 4 float PE at the ask it is now constrained to",
    },
    {
      a: "<b>2.857 ns</b>",
      k: "<b>both</b>",
      l: "<b>13,772</b>",
      f: "<b>353.4</b>",
      w: "<b>the shipped reference</b> — 10,126 FF, 13 BRAM, 72 DSP48",
      _tone: "good",
    },
    {
      a: "3.333 ns",
      k: "<code>SIMD_WB</code> alone",
      l: "+89",
      f: "−28 MHz",
      w: "where the PE closes with positive slack, an extra register is pure cost",
      _tone: "warn",
    },
  ],
};

const knobCost = {
  cols: [
    { key: "k", label: "knob", mono: true },
    { key: "b", label: "what it buys" },
    { key: "c", label: "what it costs in cycles" },
  ],
  rows: [
    {
      k: "SIMD_DOTDSP",
      b: "the dot sum stays in the DSP48 column — 256 LUT + 32 CARRY8 back at eight lanes, for 32 more DSP columns",
      c: "<code>DOT_LAT</code> goes <b>2 → 4</b>, so <code>vaccrd</code>/<code>vaccz</code>/<code>vaccwr</code> behind a dot in flight wait 4 cycles",
      _tone: "warn",
    },
    {
      k: "SIMD_WB",
      b: "the vector file's write path is halved",
      c: "a distance-1 dependency costs a second stall, <b>and distance 2 becomes a hazard that did not exist</b>",
      _tone: "warn",
    },
  ],
};

const sweepBars = {
  items: [
    {
      label: "s8 — the baseline of this table",
      value: 7961,
      note: "368.7 MHz",
    },
    {
      label: "s8, no permute network",
      value: 6327,
      note: "−1,634 LUT · +33.9 MHz",
    },
    {
      label: "s8, no packed shifter",
      value: 6448,
      note: "−1,513 LUT · +24.4 MHz",
    },
    { label: "s8, one accumulator", value: 7575, note: "−386 LUT · +0.2 MHz" },
    {
      label: "s8, 32 vector registers",
      value: 7853,
      note: "−108 LUT · bigger file, SMALLER unit",
    },
    {
      label: "s8, 2 multipliers per lane",
      value: 7330,
      note: "−631 LUT · −16 DSP · int8 gone",
    },
    { label: "s4 — four lanes", value: 4138, note: "−3,823 LUT · −1.3 MHz" },
    { label: "s2 — two lanes", value: 2091, note: "−5,870 LUT · +33.4 MHz" },
    {
      label: "s8, multipliers in fabric ✗",
      value: 15068,
      note: "+7,107 LUT · −73.4 MHz",
      tone: "bad",
    },
    {
      label: "s8, block-RAM vector file ✗",
      value: 8158,
      note: "+197 LUT · +8 BRAM · −97.9 MHz",
      tone: "bad",
    },
  ],
};

const dropped = {
  cols: [
    { key: "d", label: "Dropped" },
    { key: "w", label: "Why" },
  ],
  rows: [
    {
      d: "<b>every float-tier LUT figure taken before the operand edge became unconditional</b> — the assembled 14,579 / 17,844 / 22,743 rows at 3.333 ns",
      w: "the float lane now carries <b>both</b> operand converters unconditionally; those builds carried only the narrow one. It is a different lane, so the totals are not comparable and every one of them is low",
      _tone: "bad",
    },
    {
      d: "<b>the tier-alone lane-count curve</b> — 11,432 / 6,353 / 3,808 / 2,475 and <code>1,270 + 635 × lanes</code>",
      w: "<b>withdrawn for provenance, not because it was wrong.</b> It came from the tier probe, whose script and module have both since been renamed, so no run can be tied to the module that exists now — and the probe's accumulator <b>folds differently</b> from the unit's in any case. It needs re-measuring, not reinstating",
      _tone: "warn",
    },
    {
      d: "<b>8 int + 4 float = 15,119 LUT / 9,720 FF / 40 DSP</b>",
      w: "a probe delta subtracted from an assembled build — arithmetic across two scopes, never a measurement. The measured reference is <b>13,772 / 10,126 / 72</b>",
      _tone: "bad",
    },
    {
      d: "<b>8 int + 8 float = 16,214 LUT</b>",
      w: "derived from a SIMD = 4 proxy whose vector register is 128 bits rather than 256 — a floor rather than an estimate, and it never paid for the walk sequencer",
      _tone: "bad",
    },
    {
      d: "“the float tier costs 12,400 LUT and 10.6 MHz”, and “sixteen float lanes, unconditionally”",
      w: "both sentences rested on one float lane per element being the only expressible build. The lane count is a parameter and the reference is four",
      _tone: "bad",
    },
  ],
};

const thesis = {
  cols: [
    { key: "k", label: "Kernel" },
    { key: "s", label: "scalar", align: "right", mono: true },
    { key: "v", label: "vector", align: "right", mono: true },
    { key: "x", label: "speedup", align: "right", mono: true },
    { key: "a", label: "against" },
  ],
  rows: [
    {
      k: "int8 dot, 128 elements",
      s: "8,221",
      v: "52",
      x: "<b>158.1×</b>",
      a: "the core as it ships",
    },
    {
      k: "int8 dot, 128 elements",
      s: "1,297",
      v: "52",
      x: "<b>24.9×</b>",
      a: "a scalar core that has a multiplier — <b>the honest SIMD number</b>",
      _tone: "good",
    },
    {
      k: "requantise epilogue, 256 elements",
      s: "8,025",
      v: "242",
      x: "33.2×",
      a: "—",
    },
    {
      k: "int32 sum and signed max, 256",
      s: "3,090",
      v: "248",
      x: "12.5×",
      a: "—",
    },
    {
      k: "8-tap int16 FIR, constant taps",
      s: "3,404",
      v: "556",
      x: "6.1×",
      a: "—",
    },
    { k: "256-word copy", s: "780", v: "236", x: "3.3×", a: "—" },
  ],
};

const vtiming = {
  cols: [
    { key: "e", label: "Event" },
    { key: "c", label: "Cost" },
  ],
  rows: [
    {
      e: "ALU, logic, shift, permute, moves, <code>vld</code>, <code>vst</code>",
      c: "1 cycle",
    },
    {
      e: "<code>vdot</code>, including back to back",
      c: "1 cycle; the accumulate lands <code>DOT_LAT</code> later — <b>4 as shipped</b>, 2 at <code>SIMD_DOTDSP = 0</code>",
    },
    { e: "<code>vmul</code>", c: "2 cycles" },
    {
      e: "<code>vredsum</code> / <code>vredmax</code> at more than two lanes",
      c: "2 cycles",
    },
    { e: "RAW on a vector register, distance 1", c: "1 stall" },
    {
      e: "RAW at distance 2",
      c: "<b>1 stall at <code>SIMD_WB = 1</code></b>, which is what ships",
    },
    { e: "<code>vld</code> behind a <code>vst</code>", c: "1 stall" },
    {
      e: "<code>vaccrd</code> / <code>vaccz</code> / <code>vaccwr</code> behind a dot in flight",
      c: "up to <code>DOT_LAT</code> — <b>4 as shipped</b>",
    },
    {
      e: "<code>vfmacc</code> / <code>vfmsac</code>, <b>including back to back into the same accumulator</b>",
      c: "<code>passes</code> cycles — <b>4 at four float lanes</b>, 1 at one lane per element. It retires once, and each pass's accumulate lands 15 cycles later in the background",
    },
    {
      e: "<code>vfaccz</code>",
      c: "<code>NPART</code> cycles — a sweep of a one-write-port memory",
    },
    {
      e: "<code>vfaccwr</code>",
      c: "the element count again to walk one converter over the seed, then the <code>NPART</code> sweep",
    },
    {
      e: "<code>vfaccrd</code>",
      c: "≈ <b>270 cycles</b>, and <b>the same at every lane count</b>",
    },
    {
      e: "<code>vfaccz</code> / <code>vfaccwr</code> / <code>vfaccrd</code> behind a float accumulate in flight",
      c: "up to 15 stalls",
    },
  ],
};

const isa = {
  cols: [
    { key: "i", label: "Instruction", mono: true },
    { key: "w", label: "What it does" },
  ],
  rows: [
    {
      i: "vld / vst",
      w: "a whole vector to or from <code>xs1 + imm</code>. <b>Line-aligned by contract</b> — a misaligned address faults",
    },
    {
      i: "vadd / vsub",
      w: "element-wise, wrapping — <code>.s8</code>, <code>.s16</code>, <code>.s32</code>",
    },
    { i: "vsadd / vssub", w: "element-wise, signed saturating" },
    {
      i: "vmin / vmax",
      w: "element-wise signed minimum / maximum — one mux each, off the same adder",
    },
    {
      i: "vmul",
      w: "element-wise product, low half kept (<code>.s8</code> and <code>.s16</code> only)",
    },
    {
      i: "vand / vor / vxor / vandn",
      w: "bitwise, untyped. <code>vandn</code> is <code>vs1 &amp; ~vs2</code>",
    },
    { i: "vslli / vsrli / vsrai", w: "immediate shifts, three element widths" },
    {
      i: "vsrari",
      w: "right arithmetic, <b>rounding</b> — the requantise primitive",
    },
    {
      i: "vdot / vdotn",
      w: "<code>acc[ad] ±=</code> the dot product within each 32-bit lane. <code>.s32</code> <b>faults</b> — an int32 product does not fit a 34-bit lane sum",
    },
    {
      i: "vaccz / vaccwr / vaccrd",
      w: "clear, seed from a vector register (the bias), read back as int32 lanes",
    },
    {
      i: "vsplat / vextr",
      w: "a scalar into every lane; one lane out to a scalar register",
    },
    {
      i: "vredsum / vredmax",
      w: "the sum, or the signed maximum, of the 32-bit lanes into a scalar register",
    },
    {
      i: "vsldw0..7",
      w: "lane <i>i</i> ← lane <code>(k+i)</code> of <code>{vs2, vs1}</code> — a <b>rotate</b> of the concatenation, so every index is defined at every width",
    },
    {
      i: "vpack / vunpkl / vunpkh",
      w: "two vectors narrowed to one with signed saturation; or half a vector widened and sign-extended",
    },
  ],
};

const fisa = {
  cols: [
    { key: "i", label: "Instruction", mono: true },
    { key: "w", label: "What it does" },
  ],
  rows: [
    {
      i: "vfaccz ad",
      w: "<code>facc[ad] ← 0</code>, every slot. <b>Untyped</b> — zero is zero in either width",
    },
    {
      i: "vfmacc.f16 ad, vs1, vs2",
      w: "<code>facc[ad][i] += vs1[i] * vs2[i]</code>, elementwise",
    },
    { i: "vfmsac.f16 ad, vs1, vs2", w: "the same, subtracted" },
    {
      i: "vfaccwr.f16 ad, vs1",
      w: "seed from a vector register — the bias vector",
    },
    {
      i: "vfaccrd.f16 vd, as1",
      w: "fold the partials and return one FP16 per element",
    },
    {
      i: "vfredsum.f16 xd, as1",
      w: "encoded and <b>NOT BUILT</b>; it faults",
      _tone: "bad",
    },
    {
      i: "any .f32 form",
      w: "encoded and <b>refused by this unit today</b> — the lane has the edge, the unit ties the width bit low",
      _tone: "warn",
    },
  ],
};

const rType = [
  { name: "funct7", bits: 7, value: "op<<2 | et" },
  { name: "rs2", bits: 5 },
  { name: "rs1", bits: 5 },
  { name: "funct3", bits: 3, value: "the group" },
  { name: "rd", bits: 5 },
  { name: "0001011", bits: 7, value: "custom-0", accent: true },
];

const fType = [
  { name: "funct7", bits: 7, value: "op<<2 | WIDTH" },
  { name: "rs2", bits: 5 },
  { name: "rs1", bits: 5 },
  { name: "funct3", bits: 3, value: "FMAC / FRED / FCVT" },
  { name: "rd", bits: 5 },
  { name: "0101011", bits: 7, value: "custom-1", accent: true },
];

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

const fmaccPass = [
  {
    name: "MEM",
    kind: "bus",
    values: ["vfmacc", "vfmacc", "vfmacc", "vfmacc", "next"],
  },
  { name: "pass", kind: "bus", values: ["0", "1", "2", "3", "—"] },
  {
    name: "elements driven",
    kind: "bus",
    values: ["3..0", "7..4", "11..8", "15..12", "—"],
  },
  { name: "MEM held", kind: "bit", values: [1, 1, 1, 0, 0] },
  { name: "retires", kind: "bit", values: [0, 0, 0, 1, 0], mark: [3] },
];
</script>

<template>
  <DocPage
    title="SIMD PE"
    summary="The controller PE with a wide datapath behind it — one program counter, one instruction, eight integer lanes and four float ones. What it is, why the float lane count is a different number from the element count, why there is no dtype knob anywhere, and what a mesh of these holds."
    domain="simd"
    status="measured"
    source="src/kohakumpe/simd/ · docs/projects/kohakumpe/simd/ · OOC on xcvu13p-fhgb2104-2L-e at 2.857 ns (reference) and 3.333 ns"
  >
    <p class="doc-p">
      Everything the base core is stays true: RV32I, one port on the fabric, the
      same kick and the same completion. What is added is a second register
      file, a second scratchpad, and an array of lanes that all execute
      <b>the same instruction at the same time</b>.
    </p>

    <Callout kind="measured" title="The reference configuration">
      <div
        class="font-mono kt-text-body whitespace-pre text-warm-700 dark:text-warm-300 leading-7 overflow-x-auto my-1"
      >
        {{ reference }}
      </div>
      <p>
        Assembled <code>rv_pe</code> on <code>xcvu13p-fhgb2104-2L-e</code>, OOC
        synthesis at the <b>2.857 ns ask (350 MHz)</b>,
        <code>SIMD_DOTDSP = 1</code>, <code>SIMD_WB = 1</code>. Against a 300
        MHz mesh clock that is <b>18 % margin</b>. Hold beside it the same PE
        with the extension switched off — 2,477 LUT at 377.9 MHz, measured at
        3.333 ns, so read it as a scale and not as a subtraction.
      </p>
      <p>
        <b>This is the build, not a point on a menu.</b> Eight integer lanes are
        fixed by the memory granule, four float lanes are the chosen width, and
        the float datapath has exactly one arithmetic format.
      </p>
    </Callout>

    <Fig
      caption="The scalar core keeps doing what it is good at: addresses, trip counts, branches. The vector unit never computes an address and never takes a branch. A loop is a scalar loop whose body happens to move 32 bytes at a time — and with SIMD_EN = 0 all of the right-hand column disappears, generate rather than zero-width, leaving the base core bit for bit."
      zoom
    >
      <BlockDiagram :nodes="shape.nodes" :edges="shape.edges" />
    </Fig>

    <h2 class="doc-h2">Two halves, and only one of them is a choice</h2>

    <div
      class="font-mono kt-text-caption whitespace-pre text-warm-700 dark:text-warm-300 leading-6 overflow-x-auto my-3"
    >
      {{ granule }}
    </div>

    <Callout
      kind="rule"
      title="The integer lanes are the address path; the float lanes are not"
    >
      <p>
        A contiguous 32-bit load by eight lanes is exactly one
        <code>MEM_RD_REQ</code>, and that is the strongest machine-level
        alignment in the design — narrow the integer side and every coalesced
        load becomes two or more requests, for every kernel, forever. The lane
        is 32 bits for a second reason pointing the same way:
        <code>vdot</code> reduces <i>within</i> a lane into one int32, so an
        accumulator is exactly one vector register wide and
        <code>vaccrd</code> is a move rather than a narrowing.
      </p>
      <p>
        Float has no such tie. Fewer lanes cost an <b>issue interval</b> of
        <code>elements / lanes</code>, and latency is the cheapest thing to
        trade in a datapath that is already fifteen cycles deep. That asymmetry
        is why <code>int 4 / float 4</code> is rejected — it halves the memory
        alignment to buy what the float knob already buys — and why
        <code>int &lt; float</code> is rejected outright.
      </p>
    </Callout>

    <SpecTable
      :cols="counts.cols"
      :rows="counts.rows"
      caption="Four numbers that are routinely collapsed into one. Elements come from arithmetic on widths; lanes are the parameter; passes fall out of the two"
    />

    <WaveTrace
      :rows="fmaccPass"
      label="one vfmacc at four float lanes — four passes, one instruction"
      :notes="[
        {
          cycle: 0,
          text: 'A float lane issues one operation per cycle at every lane count. What a lane count costs the INSTRUCTION is the pass walk: the MEM stage holds until the last pass has gone.',
          tone: 'good',
        },
        {
          cycle: 3,
          text: 'The vector file is written, the scalar writeback fires and the accumulator index advances exactly ONCE, whatever the lane count. Nothing in the program sees the passes.',
          tone: 'good',
        },
      ]"
    />

    <h2 class="doc-h2">There is one float format, and it is not a setting</h2>

    <div
      class="font-mono kt-text-body whitespace-pre text-warm-700 dark:text-warm-300 leading-7 overflow-x-auto my-3"
    >
      {{ dtype }}
    </div>

    <Callout
      kind="rule"
      title="Operand width is a property of an instruction, not of a build"
    >
      <p>
        <code>khs_float_lane</code> carries one operand port and
        <b>both converters, unconditionally</b>; a width bit picks which one
        drives the datapath, and the datapath below is E8M15 either way. Its own
        header states it as a contract rather than an option:
        <i
          >“BOTH INPUT FORMATS AND THE ONE COMPUTE FORMAT ARE THE CONTRACT, not
          options: there is no parameter here that removes either edge.”</i
        >
      </p>
      <p>
        So there is <b>no dtype axis anywhere in this PE</b>.
        <code>SIMD_FLOAT</code> is a presence switch — float tier or no float
        tier — and <code>SIMD_FLOAT_LANES</code> is a width knob. Neither is a
        format knob, and a reader should not go looking for one.
      </p>
    </Callout>

    <SpecTable
      :cols="conversions.cols"
      :rows="conversions.rows"
      caption="An FP16 value round-trips unchanged through a kernel that reads and writes FP16, so the only conversion a kernel has to think about is the way out"
    />

    <Callout
      kind="trap"
      title="The SIMD tier builds the FP32 edge and cannot reach it"
    >
      <p>Three facts, and they belong together:</p>
      <ol class="list-decimal ml-5 space-y-1">
        <li>
          <code>khs_float_lane</code> takes <b>both widths unconditionally</b> —
          no parameter removes either edge, so both converters are elaborated in
          every float lane and both are paid for.
        </li>
        <li>
          <code>khs_unit.v:819</code> ties <code>.wide(1'b0)</code> on every one
          of those lanes, and <code>khs_unit.v:286</code>'s <code>bad_fet</code>
          <b>faults every <code>.f32</code> encoding</b> — except
          <code>vfaccz</code>, which is untyped because zero is zero in either
          width.
        </li>
        <li>
          <b
            >Net: the SIMD PE carries the FP32 converters in its 13,772 LUT and
            cannot issue an FP32 instruction.</b
          >
        </li>
      </ol>
      <p>
        That is a half-finished transition — not a capability, and not a planned
        one. It is written here as what it is rather than listed as supported or
        as coming.
      </p>
    </Callout>

    <Callout
      kind="open"
      title="Why it is not one wire away — and the two decisions left"
    >
      <p>
        The blocker is a <b>correctness</b> constraint, not an unfinished chore.
        <b>A 256-bit register holds 8 FP32 against 16 FP16</b>, so the element
        count, the partial count and the fold order all change with the operand
        width — and float addition does not associate.
        <b>Changing the fold order changes the answer.</b> Untying the bit
        without deciding what follows would not produce a slower or a bigger
        machine; it would produce a different one, silently — the same class of
        change as altering <code>NPART</code> or the lane count, both of which
        this design treats as architectural.
      </p>
      <p>
        Whoever finishes this has two decisions to make, and neither is
        mechanical:
      </p>
      <ul class="list-disc ml-5 space-y-1">
        <li>
          <b>What the element count means at the wide width.</b> It is a
          register width divided by an element width, so it halves — which means
          the accumulator, the seed walk and the pack walk are all addressing a
          different number of slots depending on an instruction field.
        </li>
        <li>
          <b>What happens to the accumulator's fold.</b> Each element's chain is
          the subset of partial-turns congruent to its pass; halving the element
          count re-partitions those subsets, so a build that accepts both widths
          has to define the fold order for each — and
          <b>state it in the ISA</b>, because a kernel validated at one width
          would not be validated at the other.
        </li>
      </ul>
      <p>
        The
        <RouterLink to="/mpe/simt" class="doc-link">SIMT PE</RouterLink> drives
        the same lane with the width bit live because it has one element per
        lane in both formats and pays none of that. The arithmetic is
        single-sourced — the operand edge is shared, and only the decision to
        reach it differs.
      </p>
    </Callout>

    <SpecTable
      :cols="precision.cols"
      :rows="precision.rows"
      caption="E8M15 is not a compromise: 1.5e-5 is 32x better than the FP16 a mobile fragment shader runs at, with an 8-bit exponent — more range than the FP24 of the DX9 era had. A dot product of FP16 inputs accumulates 32x more accurately than its own operands, in a format whose range covers FP32's verbatim"
    />

    <h2 class="doc-h2">What is a parameter, and what is not</h2>

    <SpecTable
      :cols="params.cols"
      :rows="params.rows"
      caption="Two things were routinely conflated here and the docs are where that conflation lived: whether the float tier EXISTS, and how WIDE it is, are both real knobs — which format it computes in is not one, and never was"
    />

    <h2 class="doc-h2">What it costs</h2>

    <div
      class="font-mono kt-text-caption whitespace-pre text-warm-700 dark:text-warm-300 leading-6 overflow-x-auto my-3"
    >
      {{ dspSum }}
    </div>

    <p class="doc-p">
      Where the 72 DSP48 comes from is worth writing out, because it is a
      decision rather than a rounding: <code>SIMD_DOTDSP = 1</code> keeps the
      dot sum inside the DSP48 column, and does that by adding a
      <i>second</i> set of multipliers per integer lane — because
      <code>p0..p3</code> must still surface for <code>vmul</code>, and an
      operand with two consumers cannot be cascaded. At
      <code>SIMD_DOTDSP = 0</code> the total is 40.
    </p>

    <SpecTable
      :cols="knobs.cols"
      :rows="knobs.rows"
      caption="rv_pe defaults SIMD_DOTDSP = 1 and SIMD_WB = 1. khs_unit's own parameter defaults are still 0, so a probe or a bench that instantiates the unit directly gets the other machine unless it says otherwise — khs_unit_tb defaults both to 1 to match what ships"
    />

    <SpecTable
      :cols="knobCost.cols"
      :rows="knobCost.rows"
      caption="Neither knob is free in cycles, and that is the part a resource table hides. The kernel figures further down were taken with BOTH OFF and have not been re-measured against the shipped pair"
    />

    <h3 class="doc-h3">What a mesh holds</h3>

    <Fig
      caption="PROJECTED — arithmetic over per-PE measurements, not a placed mesh. The SIMT PE figure is that unit's own; the controller figure is 2,477 each. The float throughput of the array is exactly one Mali-G610 shader core's worth — computed at 1.5e-5 rather than the 4.9e-4 such a core would run at."
    >
      <div
        class="font-mono kt-text-caption whitespace-pre text-warm-700 dark:text-warm-300 leading-6 overflow-x-auto"
      >
        {{ meshSum }}
      </div>
    </Fig>

    <h3 class="doc-h3">The integer configuration sweep</h3>

    <p class="doc-p">
      <b
        >These are <code>khs_unit</code> alone, integer only, at 3.333 ns, with
        <code>SIMD_DOTDSP = 0</code> and <code>WB_STAGE = 0</code>.</b
      >
      They are not the shipped configuration and their absolute LUT is not the
      reference's. What they remain good for is the question they were run to
      answer — what each optional block costs <i>relative to the others</i>, on
      modules that have not changed since.
    </p>

    <ResourceBars
      :items="sweepBars.items"
      unit="LUT · khs_unit alone, integer only, OOC at 3.333 ns on xcvu13p-fhgb2104-2L-e"
      caption="✗ marks a configuration that does not meet the 3.333 ns request. Both are priced for the comparison rather than offered: fabric multipliers and a block-RAM register file are the two ways to build this unit that are worse on every axis at once"
    />

    <Callout kind="measured" title="What the rows say">
      <p>
        <b
          >Everything on the critical path is the register file's read-to-write
          loop</b
        >, so what moves the frequency is what sits <i>in</i> that loop.
        Removing the permute network buys 33.9 MHz and the shifter 24.4 — both
        shorten the result mux that feeds the write port. Fabric multipliers
        cost 73.4 MHz and a block-RAM register file 97.9, because both put
        something slower <i>into</i> it. The knobs that touch neither — register
        count, accumulator count, multipliers per lane — move it by less than
        half a megahertz.
      </p>
      <p>
        <b>A DSP column is worth about 230 LUT</b>, which is the same argument
        that buys <code>SIMD_DOTDSP</code> 32 more of them.
        <b>The vector register count is free in both directions</b> — thirty-two
        entries are <i>cheaper</i> than eight, because a distributed-RAM
        primitive is 32 deep either way and a small file wastes the depth it
        does not use.
        <b>Accumulators are the one structure that grows badly</b>: two to four
        costs 2,203 LUT, because the read mux in front of the array grows with
        both count and width.
      </p>
    </Callout>

    <Callout kind="open" title="Figures that were NOT carried forward">
      <p>
        The SIMD PE changed substantially, so a number that was true of the old
        build is not automatically a worse measurement of this one — it can be a
        measurement of a different machine. These were dropped rather than
        updated, and each is listed so nobody re-derives it from an older page.
      </p>
    </Callout>

    <SpecTable :cols="dropped.cols" :rows="dropped.rows" />

    <h2 class="doc-h2">What the instructions buy</h2>

    <SpecTable
      :cols="thesis.cols"
      :rows="thesis.rows"
      caption="Kernel-only cycles, same PE, same data, same independently computed reference for both forms — MEASURED on the integer-only build with SIMD_DOTDSP = 0 and SIMD_WB = 0. The base core is RV32I and has no multiplier, so an int8 dot's scalar loop spends 84 % of its cycles in a software multiply at about 54 cycles each; quoting 158x would be mostly a statement that the base core cannot multiply, which is why every multiplying kernel carries a twin whose multiply is costed at one instruction"
    />

    <Callout kind="note" title="Two things bound every kernel here">
      <p>
        <b>Loop overhead bounds everything at short vectors.</b> The copy moves
        eight times the data per instruction and measures 3.3×, because the two
        pointer bumps, the counter and the branch do not shrink. That is the
        Amdahl ceiling for any kernel on this machine, and it is a property of
        the loop rather than of the datapath — at two lanes the vector copy
        actually <i>loses</i> to the scalar one, 908 cycles against 780.
      </p>
      <p>
        <b>A kernel whose scalar form is already good wins the least.</b> The
        FIR's taps are compile-time constants, so its scalar form
        strength-reduces to two instructions per tap with no software multiply
        to remove. 6.1× is width alone, and it is the narrowest frontier in the
        suite.
      </p>
    </Callout>

    <SpecTable
      :cols="vtiming.cols"
      :rows="vtiming.rows"
      caption="The measured CPI of the integer vector kernels is 1.17 to 1.53 — the stalls above, plus the loop's own mispredicted exit"
    />

    <h2 class="doc-h2">One instruction, eight lanes</h2>

    <LaneGrid
      :lanes="8"
      :rows="laneRows"
      caption="One vadd.s16, eight lanes, sixteen int16 additions. Lane L reads bits 32L+31..32L of each source and writes the same bits of the destination — nothing is broadcast, nothing is muxed, no lane can see another lane's data. That is why the array costs almost exactly SIMD times one lane, and why there is nothing to diverge and nothing to serialise"
    />

    <p class="doc-p">
      Every arithmetic instruction carries a two-bit <b>element type</b>, so a
      <code>vadd.s8</code> at eight lanes adds 32 pairs of bytes in one cycle:
      eight lanes times four elements. Element 0 is at the <i>low</i> end — the
      same order a little-endian byte array arrives in — and the type is a field
      of the instruction word rather than a mode, so two adjacent instructions
      may use different widths with no state to change. How that is built out of
      one native carry chain is on the
      <RouterLink to="/mpe/simd/microarchitecture" class="doc-link"
        >microarchitecture page</RouterLink
      >.
    </p>

    <h2 class="doc-h2">The encoding</h2>

    <p class="doc-p">
      RISC-V custom-0 (<code>0x0B</code>) carries the whole integer tier;
      custom-1 (<code>0x2B</code>) carries the float tier.
      <code>funct3</code> selects the group; <code>funct7</code> selects the
      operation within it, and its low two bits are the element type on custom-0
      and the <b>operand width</b> on custom-1.
      <b>69 instructions — 63 integer and 6 float.</b>
    </p>

    <BitField
      :fields="rType"
      caption="R-type on custom-0: funct7[1:0] is the element type — 0 = int8, 1 = int16, 2 = int32. vld and vst use an I-type layout with the store's data register in the rd position, because a vector store's data comes from the VECTOR file and RV32's S-format constraint does not apply"
    />
    <BitField
      :fields="fType"
      caption="R-type on custom-1: funct7[1:0] is the OPERAND WIDTH — 0 = f16, 1 = f32. That field is the only place a format appears anywhere in this machine, and it is per-instruction"
    />

    <Callout kind="rule" title="One table, four consumers">
      <p>
        The encoding is defined once, in a field table, and four consumers are
        generated from or checked against it: the assembler, the golden model,
        the RTL decode (<code>khs_isa.vh</code>), and the C intrinsic header
        (<code>khs_intrin.h</code>). A test encodes and decodes every
        instruction through all four and fails on any disagreement, which is
        what makes “one source of truth” a property rather than an intention.
      </p>
    </Callout>

    <SpecTable
      :cols="isa.cols"
      :rows="isa.rows"
      caption="The integer tier. Accumulators are deliberately not spelled a0 — that is a scalar ABI name, and a program that meant one and wrote the other should not assemble"
    />

    <SpecTable
      :cols="fisa.cols"
      :rows="fisa.rows"
      caption="The float tier. SIMD_FLOAT = 0 elaborates none of it and leaves custom-1 unmapped, so a float instruction faults as an illegal encoding rather than landing in a decode case that computes something plausible"
    />

    <Callout kind="rule" title="Rules a kernel must respect">
      <p>
        <b>Vector addresses are aligned</b> to the vector width, or they fault.
        <b>The vector scratchpad is store-only from the scalar side</b> — stage
        data with <code>sw</code>, read it with <code>vld</code>.
        <b><code>vsldw</code> slides by lanes, not elements.</b>
        <b>Reductions cross lanes; arithmetic does not</b> — keep
        <code>SIMD</code> running totals in a vector register and reduce once at
        the end.
      </p>
      <p>
        <b>A float accumulator must be zeroed before it is used</b>: the
        partials are a memory and a memory has no reset, so a kernel that skips
        <code>vfaccz</code> reads whatever the RAM powered up holding. And
        <b
          ><code>SIMD_NPART</code> and <code>SIMD_FLOAT_LANES</code> change
          float answers</b
        >
        — neither is a tuning knob, because the accumulation order is
        architectural and a kernel validated at one value is not validated at
        another.
      </p>
    </Callout>
  </DocPage>
</template>
