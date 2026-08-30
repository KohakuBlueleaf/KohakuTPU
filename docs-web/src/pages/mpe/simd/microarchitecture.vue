<script setup>
// ===========================================================================
// SIMD PE — microarchitecture. How the datapath is actually CONSTRUCTED for the
// operations it supports, module by module, read out of the RTL rather than
// restated from prose.
//
// PROVENANCE. Every resource and frequency figure is out-of-context SYNTHESIS
// on xcvu13p-fhgb2104-2L-e, Vivado 2024.2, at a 3.333 ns target. No placement
// and no routing, so an Fmax here is an upper bound.
//
// The path-delay and logic-level figures are read out of the module headers
// they were recorded in; the block probes measure a module in isolation and
// say so. Per-block figures here are `-flatten_hierarchy none`, which is the
// setting that makes them attributable and is NOT what the ship synthesises at.
// Whole-PE totals are on the SIMD PE page and come from
// docs/projects/kohakumpe/unit-counts.md, which names the tree per table.
// ===========================================================================

const PART = "xcvu13p-fhgb2104-2L-e";

// ------------------------------------------------------------- module map
//
// Rows in flow order so no wire cuts another: the control column on the left,
// the vector file as ONE operand bar on top, every unit it feeds in one row
// under it, the result mux as one bar below, and the write-back as a single
// wire up the right-hand side. The operand fan-out is vertical wires off the
// bar, the result fan-in vertical wires into the bar. Unit boxes are 7 wide on
// a 9.5 pitch, which is also the bars' slot pitch, so the wires land straight.
// The float tier hangs BELOW the row as a column — khs_fp32_alu, then
// khs_facc with khs_ffold beside it — so facc lands on the mux's fifth slot,
// and khs_reduce is the LAST box on the row (w_sc sits on its left) so the
// write-back has exactly one lane to climb and cuts nothing on the way.
const map = {
  nodes: [
    {
      id: "dec",
      x: -2.25,
      y: 0,
      w: 11,
      h: 3.6,
      label: "decode — in EX",
      sub: "the DECODE is registered, ~90 flops",
      accent: true,
    },
    {
      id: "msk",
      x: -2.25,
      y: 5.5,
      w: 11,
      h: 3.6,
      label: "shift + element masks",
      sub: "built ONCE, not SIMD times",
      accent: true,
    },
    {
      id: "vsp",
      x: -2.25,
      y: 28.9,
      w: 11,
      h: 3.2,
      label: "khs_vspad",
      sub: "SIMD banks × 1024 × 32b",
    },

    {
      id: "vrf",
      x: 22.5,
      y: 0,
      w: 57,
      h: 3.6,
      label: "khs_vregfile",
      sub: "2 mirrored sdpram · read latency 1",
    },

    {
      id: "psh",
      x: 18.5,
      y: 11,
      w: 8,
      h: 4,
      label: "khs_pshift32",
      sub: "one rotate, masked",
    },
    {
      id: "padd",
      x: 28.5,
      y: 11,
      w: 7,
      h: 4,
      label: "khs_padd32",
      sub: "ONE native carry chain",
      accent: true,
    },
    {
      id: "mul",
      x: 38,
      y: 11,
      w: 7,
      h: 4,
      label: "khs_mul × 4",
      sub: "17×17 ×2 · 9×9 ×2 — part of the IM unit",
    },
    {
      id: "perm",
      x: 47.5,
      y: 11,
      w: 7,
      h: 4,
      label: "khs_perm",
      sub: "2·SIMD : 1 per output lane",
    },
    {
      id: "red",
      x: 66.5,
      y: 11,
      w: 7,
      h: 4,
      label: "khs_reduce",
      sub: "a tree, root registered",
    },

    {
      id: "rnd",
      x: 18.5,
      y: 17.3,
      w: 8,
      h: 4,
      label: "khs_padd32 u_rnd",
      sub: "the vsrari increment, its OWN adder",
    },
    {
      id: "fl",
      x: 57,
      y: 17.3,
      w: 7,
      h: 4,
      label: "khs_fp32_alu",
      sub: "FLOAT_LANES FMA units, FSFU_UNITS seed-capable",
    },
    {
      id: "fac",
      x: 57,
      y: 23.3,
      w: 7,
      h: 3.6,
      label: "khs_facc — HAS_FACC only",
      sub: "NPART rotating partials per bank",
    },
    {
      id: "ffo",
      x: 66.5,
      y: 23.3,
      w: 6,
      h: 3.6,
      label: "khs_ffold",
      sub: "serial, once per PASS",
    },
    {
      id: "wsc",
      x: 74,
      y: 23.3,
      w: 6,
      h: 3.6,
      label: "w_sc → rv_mem",
      sub: "vextr · vredsum · vredmax",
    },

    {
      id: "res",
      x: 13,
      y: 28.9,
      w: 57,
      h: 3.2,
      label: "the result mux",
      sub: "every select is a decode bit",
      accent: true,
    },
  ],
  edges: [
    // the write-back first: it takes the outermost lane, up the right side
    { from: "res:r", to: "vrf:r", accent: true },
    // operand fan-out off the bar, all straight: the y2 cell over fp32_alu is
    // left empty so its wire drops through
    { from: "vrf:b", to: "red:t", dir: "v" },
    { from: "vrf:b", to: "fl:t", dir: "v" },
    { from: "vrf:b", to: "padd:t", dir: "v", accent: true },
    { from: "vrf:b", to: "mul:t", dir: "v" },
    { from: "vrf:b", to: "perm:t", dir: "v" },
    // the masks: padd's wire listed first so it takes the upper slot and
    // passes over psh's
    { from: "msk:r", to: "padd:l", accent: true },
    { from: "msk:r", to: "psh:l", accent: true },
    { from: "dec:r", to: "vrf:l", dir: "h" },
    { from: "dec:b", to: "msk:t", dir: "v" },
    // result fan-in into the bar
    { from: "padd:b", to: "res:t", dir: "v", accent: true },
    { from: "rnd:b", to: "res:t", dir: "v" },
    { from: "mul:b", to: "res:t", dir: "v" },
    { from: "perm:b", to: "res:t", dir: "v" },
    { from: "fac:b", to: "res:t", dir: "v", dash: true },
    { from: "vsp:r", to: "res:l", dir: "h", dash: true },
    // inside the rows
    { from: "psh:b", to: "rnd:t", dir: "v" },
    { from: "red:r", to: "wsc:t" },
    { from: "fl:b", to: "fac:t", dir: "v" },
    { from: "ffo:l", to: "fac:r", dir: "h" },
  ],
  groups: [
    {
      x: 16.5,
      y: 10.1,
      w: 29.5,
      h: 11.8,
      label: "khs_lane × ILANES — IM unit",
    },
    { x: 56.75, y: 16.4, w: 16.25, h: 11.1 },
    // The float group's caption rides on a zero-height group along its top
    // edge, starting to the RIGHT of the fp32_alu feed so no wire runs
    // through the text (a caption starts at its group's left corner).
    {
      x: 61,
      y: 16.4,
      w: 12,
      h: 0.1,
      label: "float tier — FLOAT_LANES > 0 only",
    },
  ],
};

// ============================================================ khs_padd32
const PA_EL = [
  {
    bit: "31",
    aM: "0",
    aB: "0x0A",
    bM: "0",
    bB: "0x01",
    rM: "0",
    rB: "0x0B",
    fM: "1",
    yM: "1",
    yB: "0x0B",
    out: "0x8B",
    dec: "−118 + 1 = −117",
  },
  {
    bit: "23",
    aM: "0",
    aB: "0x6D",
    bM: "0",
    bB: "0x14",
    rM: "1",
    rB: "0x01",
    fM: "0",
    yM: "1",
    yB: "0x01",
    out: "0x81",
    dec: "109 + 20 → −127",
  },
  {
    bit: "15",
    aM: "0",
    aB: "0x72",
    bM: "0",
    bB: "0x3E",
    rM: "1",
    rB: "0x30",
    fM: "1",
    yM: "0",
    yB: "0x30",
    out: "0x30",
    dec: "−14 + 62 = 48",
  },
  {
    bit: "7",
    aM: "0",
    aB: "0x12",
    bM: "0",
    bB: "0x0B",
    rM: "0",
    rB: "0x1D",
    fM: "1",
    yM: "1",
    yB: "0x1D",
    out: "0x9D",
    dec: "−110 + 11 = −99",
  },
];
const PA_X = [96, 288, 480, 672];
const PA_ROWS = [
  { y: 54, label: "a & ~M", m: "aM", b: "aB" },
  { y: 92, label: "b & ~M", m: "bM", b: "bB" },
  { y: 164, label: "raw = a + b", m: "rM", b: "rB" },
  { y: 202, label: "(a ^ b) & M", m: "fM", b: null },
  { y: 240, label: "y", m: "yM", b: "yB", accent: true },
];

const masks = {
  cols: [
    { key: "et", label: "et", mono: true },
    { key: "m", label: "mask — each element's MSB", mono: true },
    { key: "top", label: "top — bytes that END an element", mono: true },
    {
      key: "sp",
      label: "spread(e, f) — the flag over its whole element",
      mono: true,
    },
  ],
  rows: [
    { et: "ET_S8", m: "32'h8080_8080", top: "4'b1111", sp: "f" },
    {
      et: "ET_S16",
      m: "32'h8000_8000",
      top: "4'b1010",
      sp: "{f[3], f[3], f[1], f[1]}",
    },
    { et: "ET_S32", m: "32'h8000_0000", top: "4'b1000", sp: "{4{f[3]}}" },
  ],
};

const flags = {
  nodes: [
    {
      id: "ams",
      x: 0,
      y: 0,
      w: 12,
      label: "a_ms",
      sub: "{a[31],a[23],a[15],a[7]}",
    },
    { id: "bms", x: 0, y: 4.2, w: 12, label: "b_ms", sub: "inverted when sub" },
    {
      id: "yms",
      x: 0,
      y: 8.4,
      w: 12,
      label: "y_ms",
      sub: "the SUM's sign bits",
    },
    {
      id: "cin",
      x: 16,
      y: 4.2,
      w: 13,
      label: "cin = y_ms ^ a_ms ^ b_ms",
      sub: "the carry INTO the MSB",
      accent: true,
    },
    {
      id: "cout",
      x: 33,
      y: 0,
      w: 13,
      label: "cout = majority",
      sub: "(a&b) | (cin & (a^b))",
      accent: true,
    },
    {
      id: "ovf",
      x: 33,
      y: 6.5,
      w: 13,
      label: "ovf = (cin ^ cout) & top",
      sub: "signed overflow",
      accent: true,
    },
    {
      id: "lt",
      x: 50,
      y: 3,
      w: 13,
      label: "lt = (y_ms ^ ovf) & top",
      sub: "signed a < b",
    },
    {
      id: "sat",
      x: 50,
      y: 9.5,
      w: 13,
      label: "8'h80 / 8'h7F",
      sub: "toward the operands' sign",
    },
    {
      id: "sel",
      x: 67,
      y: 3,
      w: 13,
      label: "min / max",
      sub: "ONE mux per byte",
    },
  ],
  edges: [
    { from: "ams:r", to: "cin:l", dir: "h" },
    { from: "bms:r", to: "cin:l", dir: "h" },
    { from: "yms:r", to: "cin:l", dir: "h" },
    { from: "cin:r", to: "cout:l", dir: "h", accent: true },
    { from: "cin:r", to: "ovf:l", dir: "h", accent: true },
    { from: "cout:b", to: "ovf:t", dir: "v" },
    { from: "ovf:r", to: "lt:l", dir: "h" },
    { from: "ovf:r", to: "sat:l", dir: "h" },
    { from: "lt:r", to: "sel:l", dir: "h", accent: true },
  ],
};

// ========================================================== khs_pshift32
const SH_X = 0xb6,
  SH_E0 = 0x4d;
const SH_BITS = (v, n) =>
  Array.from({ length: n }, (_, k) => (v >> (n - 1 - k)) & 1);
const sh_x = [...SH_BITS(SH_X, 8), ...SH_BITS(SH_E0, 8)];
const sh_rr = Array.from({ length: 16 }, (_, k) => {
  const bit = 15 - k;
  const src = bit + 3;
  return src > 15 ? "?" : sh_x[15 - src];
});
const sh_keep = Array.from({ length: 16 }, (_, k) =>
  (15 - k) % 8 <= 4 ? 1 : 0,
);
const sh_y = sh_rr.map((v, k) => (v === "?" ? 0 : v & sh_keep[k]));
const sh_alien = (k) => (15 - k) % 8 >= 5;

const SH_ROWS = [
  { y: 46, label: "x", vals: sh_x, kind: "src" },
  { y: 84, label: "rr = {x,x} >> 3", vals: sh_rr, kind: "rot" },
  { y: 122, label: "keep", vals: sh_keep, kind: "mask" },
  { y: 160, label: "y = rr & keep", vals: sh_y, kind: "out" },
];

const shiftForms = {
  cols: [
    { key: "op", label: "Form", mono: true },
    { key: "rot", label: "rot", mono: true },
    { key: "how", label: "How khs_pshift32 builds it" },
  ],
  rows: [
    {
      op: "vsrli",
      rot: "s",
      how: "<code>(x rot&gt;&gt; s) &amp; keep</code> — <code>keep</code> is the low <code>EW−s</code> bits of every element",
    },
    {
      op: "vsrai",
      rot: "s",
      how: "the same, then <code>| (~keep &amp; sgn)</code> — each element's own sign, replicated across the element",
    },
    {
      op: "vslli",
      rot: "32 − s",
      how: "<code>(x rot&gt;&gt; 32−s) &amp; keep</code>, with <code>keep</code> built as the <b>high</b> <code>EW−s</code> bits instead",
    },
    {
      op: "vsrari",
      rot: "s",
      how: "<code>vsrai</code>, plus bit <code>s−1</code> of each element as a <b>carry-in</b> — an increment, not an addition of half an ulp before the shift",
    },
  ],
};

// ============================================================== khs_mul
const mulMap = {
  nodes: [
    {
      id: "a",
      x: 0,
      y: 0,
      w: 9,
      h: 5,
      label: "a[31:0]",
      sub: "one lane of v1",
    },
    {
      id: "b",
      x: 0,
      y: 6.5,
      w: 9,
      h: 5,
      label: "b[31:0]",
      sub: "one lane of v2",
    },
    {
      id: "et",
      x: 0,
      y: 13,
      w: 9,
      h: 5,
      label: "et",
      sub: "the element type, in the instruction",
      accent: true,
    },
    {
      id: "m0",
      x: 14,
      y: 0,
      w: 9,
      h: 4.4,
      label: "u_m0 — 17 × 17",
      sub: "s8 a[7:0] · s16 a[15:0]",
      accent: true,
    },
    {
      id: "m1",
      x: 14,
      y: 5.6,
      w: 9,
      h: 4.4,
      label: "u_m1 — 17 × 17",
      sub: "s8 a[15:8] · s16 a[31:16]",
      accent: true,
    },
    {
      id: "m2",
      x: 14,
      y: 11.2,
      w: 9,
      h: 4.4,
      label: "u_m2 — 9 × 9",
      sub: "a[23:16], int8 ONLY",
    },
    {
      id: "m3",
      x: 14,
      y: 16.8,
      w: 9,
      h: 4.4,
      label: "u_m3 — 9 × 9",
      sub: "a[31:24], int8 ONLY",
    },
    {
      id: "lo",
      x: 28,
      y: 7,
      w: 9,
      h: 7,
      label: "mul_lo",
      sub: "the products, at +1, into the result mux",
      accent: true,
    },
  ],
  edges: [
    { from: "a:r", to: "m0:l", dir: "h" },
    { from: "b:r", to: "m1:l", dir: "h" },
    { from: "et:r", to: "m2:l", dir: "h", accent: true, label: "operand mux" },
    { from: "et:r", to: "m3:l", dir: "h" },
    { from: "m0:r", to: "lo:l", dir: "h", accent: true },
    { from: "m1:r", to: "lo:l", dir: "h" },
    { from: "m2:r", to: "lo:l", dir: "h" },
    { from: "m3:r", to: "lo:l", dir: "h" },
  ],
};

const mulWidths = {
  cols: [
    { key: "et", label: "et", mono: true },
    { key: "m01", label: "u_m0 · u_m1  (17×17 → 34)", mono: true },
    { key: "m23", label: "u_m2 · u_m3  (9×9 → 18)", mono: true },
    { key: "r", label: "What the unit does" },
  ],
  rows: [
    {
      et: "s8",
      m01: "a[7:0]·b[7:0], a[15:8]·b[15:8]",
      m23: "a[23:16]·b[23:16], a[31:24]·b[31:24]",
      r: "four independent low-half products per lane. At <code>ILANES = 8</code> that is <b>32 int8 products from one instruction</b>",
      _tone: "good",
    },
    {
      et: "s16",
      m01: "a[15:0]·b[15:0], a[31:16]·b[31:16]",
      m23: "<b>idle</b> — nothing reads them at this width",
      r: "two products per lane, 16 per instruction",
    },
    {
      et: "s32",
      m01: "—",
      m23: "—",
      r: "<b>not encoded for <code>vmul</code>.</b> A 32×32 product does not fit the lane's result path, so the element type is refused rather than truncated",
      _tone: "bad",
    },
  ],
};

// ============================================================= khs_perm
const PERM_IDX = 3;
const PERM_IN = Array.from({ length: 16 }, (_, i) =>
  i < 8 ? `a${i}` : `b${i - 8}`,
);
const PERM_PICK = Array.from({ length: 8 }, (_, j) => (PERM_IDX + j) % 16);

const packOps = {
  cols: [
    { key: "op", label: "op4", mono: true },
    { key: "n", label: "Elements", mono: true },
    { key: "how", label: "Construction, from khs_perm" },
  ],
  rows: [
    {
      op: "PACK_S16",
      n: "VW/16 per SOURCE",
      how: "int16 → int8. It fits when every discarded bit is a copy of the sign bit that will be kept: <code>fa = (&amp;a[15:7]) | (~|a[15:7])</code> — <b>one AND and one NOR</b>, not two magnitude compares. Both sources are consumed; <code>v1</code>'s go in the low half",
    },
    {
      op: "PACK_S32",
      n: "SIMD per source",
      how: "int32 → int16, the same test on <code>a[31:15]</code>. This is what a requantise epilogue ends with",
    },
    {
      op: "UNPKL / UNPKH",
      n: "half a vector",
      how: "sign-extend the low or the high half. <b>Pure wiring plus a sign bit</b> — no mux, no compare",
    },
    {
      op: "default → sldw",
      n: "SIMD lanes",
      how: "<code>cat_l[(idx + i) % (2*SIMD)]</code>. A ROTATE of the concatenation, not a clamp, so every index is defined at every SIMD width",
    },
  ],
};

// =========================================================== khs_reduce
const tree = {
  nodes: [
    { id: "l8", x: 0, y: 0, w: 7.5, h: 2.6, label: "s[8]", sub: "v[0]" },
    { id: "l9", x: 8.5, y: 0, w: 7.5, h: 2.6, label: "s[9]", sub: "v[1]" },
    { id: "l10", x: 17, y: 0, w: 7.5, h: 2.6, label: "s[10]", sub: "v[2]" },
    { id: "l11", x: 25.5, y: 0, w: 7.5, h: 2.6, label: "s[11]", sub: "v[3]" },
    { id: "l12", x: 34, y: 0, w: 7.5, h: 2.6, label: "s[12]", sub: "v[4]" },
    { id: "l13", x: 42.5, y: 0, w: 7.5, h: 2.6, label: "s[13]", sub: "v[5]" },
    { id: "l14", x: 51, y: 0, w: 7.5, h: 2.6, label: "s[14]", sub: "v[6]" },
    { id: "l15", x: 59.5, y: 0, w: 7.5, h: 2.6, label: "s[15]", sub: "v[7]" },
    { id: "n4", x: 4.2, y: 5.5, w: 8, h: 2.6, label: "s[4]" },
    { id: "n5", x: 21.2, y: 5.5, w: 8, h: 2.6, label: "s[5]" },
    { id: "n6", x: 38.2, y: 5.5, w: 8, h: 2.6, label: "s[6]" },
    { id: "n7", x: 55.2, y: 5.5, w: 8, h: 2.6, label: "s[7]" },
    { id: "n2", x: 12.7, y: 11, w: 8, h: 2.6, label: "s[2]", accent: true },
    { id: "n3", x: 46.7, y: 11, w: 8, h: 2.6, label: "s[3]", accent: true },
    {
      id: "root",
      x: 27,
      y: 17.5,
      w: 13,
      h: 3,
      label: "s[1] — the root",
      sub: "from REGISTERED children",
      accent: true,
    },
  ],
  edges: [
    { from: "l8:b", to: "n4:t", dir: "v" },
    { from: "l9:b", to: "n4:t", dir: "v" },
    { from: "l10:b", to: "n5:t", dir: "v" },
    { from: "l11:b", to: "n5:t", dir: "v" },
    { from: "l12:b", to: "n6:t", dir: "v" },
    { from: "l13:b", to: "n6:t", dir: "v" },
    { from: "l14:b", to: "n7:t", dir: "v" },
    { from: "l15:b", to: "n7:t", dir: "v" },
    { from: "n4:b", to: "n2:t", dir: "v" },
    { from: "n5:b", to: "n2:t", dir: "v" },
    { from: "n6:b", to: "n3:t", dir: "v" },
    { from: "n7:b", to: "n3:t", dir: "v" },
    { from: "n2:b", to: "root:t", dir: "v", accent: true },
    { from: "n3:b", to: "root:t", dir: "v", accent: true },
  ],
  groups: [
    {
      x: -1,
      y: 14.4,
      w: 69,
      h: 0.1,
      label:
        "RED_PIPE = 1 — the register boundary sits HERE, below the root's own children",
    },
  ],
};

// ============================================================ khs_vspad
const spad = {
  nodes: [
    {
      id: "noc",
      x: 0,
      y: 0,
      w: 16,
      label: "the NoC window writer",
      sub: "buf_id 2 / 6 · one 32-bit word",
      accent: true,
    },
    {
      id: "core",
      x: 0,
      y: 11,
      w: 16,
      label: "vld · vst · scalar sw",
      sub: "the CORE, one row",
      accent: true,
    },
    {
      id: "pa",
      x: 23,
      y: 0,
      w: 40,
      h: 3,
      label: "port A — a_bank selects ONE bank",
      sub: "byte enabled · the NoC's ALONE, which keeps the receive FIFO out of the core's stall network",
    },
    {
      id: "b0",
      x: 23,
      y: 5.5,
      w: 9,
      h: 2.8,
      label: "bank 0",
      sub: "1024 × 32b",
    },
    {
      id: "b1",
      x: 33,
      y: 5.5,
      w: 9,
      h: 2.8,
      label: "bank 1",
      sub: "1024 × 32b",
    },
    { id: "bd", x: 43, y: 5.5, w: 9, h: 2.8, label: "· · ·", sub: "banks 2…6" },
    {
      id: "b7",
      x: 53,
      y: 5.5,
      w: 10,
      h: 2.8,
      label: "bank 7",
      sub: "1024 × 32b",
    },
    {
      id: "pb",
      x: 23,
      y: 11,
      w: 40,
      h: 3,
      label: "port B — one row, EVERY bank",
      sub: "b_we is 4 bits PER BANK: all of them for a vst, one bank's bytes for a scalar sw",
      accent: true,
    },
  ],
  edges: [
    { from: "noc:r", to: "pa:l", dir: "h" },
    { from: "core:r", to: "pb:l", dir: "h", accent: true },
    { from: "pa:b", to: "b0:t", dir: "v" },
    { from: "pa:b", to: "b1:t", dir: "v" },
    { from: "pa:b", to: "bd:t", dir: "v" },
    { from: "pa:b", to: "b7:t", dir: "v" },
    { from: "b0:b", to: "pb:t", dir: "v", accent: true },
    { from: "b1:b", to: "pb:t", dir: "v", accent: true },
    { from: "bd:b", to: "pb:t", dir: "v", accent: true },
    { from: "b7:b", to: "pb:t", dir: "v", accent: true },
  ],
  groups: [
    {
      x: 22,
      y: -1.3,
      w: 42,
      h: 16.5,
      label: 'khs_vspad — rv_ram_be × SIMD, MEM_PRIM = "block", XPORT_OK(0)',
    },
  ],
};

// ========================================================= the float array
const fLane = {
  nodes: [
    {
      id: "a",
      x: 0,
      y: 0,
      w: 9,
      h: 5.5,
      label: "v1 · v2",
      sub: "VW bits — VW/32 binary32 elements",
    },
    {
      id: "d",
      x: 0,
      y: 7,
      w: 9,
      h: 5.5,
      label: "vd",
      sub: "the addend, for vfma only",
    },
    {
      id: "p",
      x: 0,
      y: 14,
      w: 9,
      h: 5.5,
      label: "pass",
      sub: "sized for the SEED walk, the longer of the two",
      accent: true,
    },
    {
      id: "sel",
      x: 14,
      y: 7,
      w: 9,
      h: 5.5,
      label: "element select",
      sub: "unit u on pass p serves element p·U + u",
      accent: true,
    },
    {
      id: "fma",
      x: 28,
      y: 0,
      w: 9,
      h: 6.5,
      label: "FMA × FLOAT_LANES",
      sub: "6 deep · II = 1 · 2 DSP48 each",
      accent: true,
    },
    {
      id: "sfu",
      x: 28,
      y: 9,
      w: 9,
      h: 6.5,
      label: "seed × FSFU_UNITS",
      sub: "exp2 log2 rcp rsqrt · 10 deep · beside the FMA, not inside it",
      accent: true,
    },
    {
      id: "pad",
      x: 42,
      y: 0,
      w: 9,
      h: 6.5,
      label: "flip-flop pad",
      sub: "6 → 10 whenever seeds are built, nothing when they are not",
    },
    {
      id: "out",
      x: 42,
      y: 9,
      w: 9,
      h: 6.5,
      label: "out",
      sub: "one 32-bit slot per UNIT; the caller places them",
    },
  ],
  edges: [
    { from: "a:r", to: "sel:l", dir: "h" },
    { from: "d:r", to: "sel:l", dir: "h", label: "c" },
    { from: "p:r", to: "sel:l", dir: "h", accent: true },
    { from: "sel:r", to: "fma:l", dir: "h", accent: true },
    { from: "sel:r", to: "sfu:l", dir: "h" },
    { from: "fma:r", to: "pad:l", dir: "h", accent: true },
    { from: "pad:b", to: "out:t", dir: "v", accent: true },
    { from: "sfu:r", to: "out:l", dir: "h" },
  ],
};

// =============================================== khs_facc, the rotation
const NPART = 16,
  ALAT = 10;
const strip = (rd, wr, flight) => ({ rd, wr, flight });

const rot = [
  {
    title: "cycle 0 — the first accumulate reads partial 0",
    note: "rd_idx is a plain counter and it advances on every ACCEPTED accumulate — which means once per PASS, not once per instruction. Nothing is in flight yet, and this is the only step where that is true.",
    ...strip(0, null, []),
    cyc: "0",
    inflight: 0,
  },
  {
    title: "cycle 1 — the second reads partial 1, one cycle later",
    note: "The array will not return that first result for nine more cycles. Nothing waits, because the next accumulate is reading a DIFFERENT partial.",
    ...strip(1, null, [0]),
    cyc: "1",
    inflight: 1,
  },
  {
    title: "cycle 2 — and the third",
    note: "Consecutive accumulates go to different partials because the counter says so. The program sees ONE accumulator and never learns the latency.",
    ...strip(2, null, [0, 1]),
    cyc: "2",
    inflight: 2,
  },
  {
    title: "cycle 9 — nine in flight, six partials still untouched",
    note: "This is the state the rotation exists to reach: the array is full, II is still 1, and the recurrence has never once been closed.",
    ...strip(9, null, [0, 1, 2, 3, 4, 5, 6, 7, 8]),
    cyc: "9",
    inflight: 9,
  },
  {
    title: "cycle 10 — the first result returns",
    note: "wr_idx is rd_idx delayed by exactly ALAT, so accumulate #0's result lands on partial 0 — the partial its addend came from. Ten are in flight and the eleventh is being read.",
    ...strip(10, 0, [1, 2, 3, 4, 5, 6, 7, 8, 9]),
    cyc: "10",
    inflight: 10,
  },
  {
    title: "cycle 16 — the counter wraps onto a partial written SIX cycles ago",
    note: "NPART > ALAT is the whole guarantee, and this is where it is spent: partial 0 was written at the cycle-10 edge and is read again at cycle 16. With NPART equal to ALAT the write and the re-read would be the same cycle, and a read-first array would hand back the stale value. ALAT is 6 with no seed units and 10 with any, so NPART has to clear the larger.",
    ...strip(0, 6, [7, 8, 9, 10, 11, 12, 13, 14, 15]),
    cyc: "16",
    inflight: 9,
  },
  {
    title: "vfaccz / vfaccwr — a SWEEP, because a memory has one write port",
    note: "The partials are two mirrored distributed RAMs, not an indexed flop array, so a clear cannot be parallel: the sweep counter walks all NPART entries and the instruction holds MEM until it falls. rv_l1's invalidate-all is the same shape for the same reason — and the first PASSES partials take the seed, not just partial 0.",
    ...strip(null, 5, []),
    cyc: "sweep",
    inflight: 0,
    sweep: true,
  },
];

const foldCost = {
  cols: [
    { key: "s", label: "Step", mono: true },
    { key: "c", label: "Cycles", mono: true, align: "right" },
    { key: "w", label: "" },
  ],
  rows: [
    {
      s: "the fold, per pass",
      c: "(NPART / passes) × (ALAT+1)",
      w: "each step depends on the last, so the steps are <code>ALAT</code> apart. It runs through <b>the same array</b> the accumulate used — combining partials in a second adder would round differently from the path it is meant to finish. <b>One fold per PASS</b>, over that pass's own strided subset",
    },
    {
      s: "<code>vfaccrd</code>, total",
      c: "≈ NPART × (ALAT+1)",
      w: "<b>the same at every unit count</b> — <code>passes</code> folds of <code>NPART/passes</code> steps is <code>NPART</code> steps either way. <b>Once per reduction, against a kernel of thousands.</b> A tree would be log2(NPART) float adders standing idle the rest of the time. This is arithmetic on the two parameters, not a measured cycle count",
      _tone: "good",
    },
  ],
};

// ------------------------------------------------------------ critical path
const cpath = {
  nodes: [
    {
      id: "c1",
      x: 0,
      y: 0,
      w: 15,
      label: "m_alu_op_reg/C",
      sub: "FDRE, LUT4 → the adder's sub input · 0.409 ns",
      accent: true,
    },
    {
      id: "c2",
      x: 17,
      y: 0,
      w: 15,
      label: "into the carry chain",
      sub: "LUT3 · 0.705 ns",
    },
    {
      id: "c3",
      x: 34,
      y: 0,
      w: 15,
      label: "khs_padd32",
      sub: "CARRY8 × 4 · 1.121 ns",
      accent: true,
    },
    {
      id: "c4",
      x: 51,
      y: 0,
      w: 15,
      label: "the signed-compare spread",
      sub: "LUT4, LUT6 · 1.741 ns",
    },
    {
      id: "c5",
      x: 0,
      y: 7,
      w: 15,
      label: "min/max select, op mux",
      sub: "LUT6, LUT4 · 2.215 ns",
    },
    {
      id: "c6",
      x: 17,
      y: 7,
      w: 15,
      label: "the result mux",
      sub: "LUT6 × 2 · 2.661 ns",
    },
    {
      id: "c7",
      x: 34,
      y: 7,
      w: 15,
      label: "u_vrf write port",
      sub: "RAMD32 · 2.888 ns",
      accent: true,
    },
  ],
  edges: [
    { from: "c1:r", to: "c2:l", dir: "h", accent: true },
    { from: "c2:r", to: "c3:l", dir: "h", accent: true },
    { from: "c3:r", to: "c4:l", dir: "h", accent: true },
    { from: "c4:b", to: "c5:t", dir: "v", accent: true },
    { from: "c5:r", to: "c6:l", dir: "h", accent: true },
    { from: "c6:r", to: "c7:l", dir: "h", accent: true },
  ],
};

// --------------------------------------------------------------- the traps
const traps = {
  cols: [
    { key: "m", label: "Module", mono: true },
    { key: "s", label: "Symptom" },
    { key: "c", label: "Cause" },
  ],
  rows: [
    {
      m: "khs_padd32",
      s: "the datapath is slow and nothing looks wrong",
      c: "four byte adders with the carry between them <b>gated</b> by the element width. A LUT in the carry path stops the tool using one <code>CARRY8</code> chain: four gated bytes become <b>seven chains in series</b>, measured at <b>2.05 ns of a 4.72 ns critical path</b>",
    },
    {
      m: "khs_padd32",
      s: "a <code>vmin.s8</code> takes four bytes from the wrong operand",
      c: "<code>spread</code> read the element width as a module-level net instead of an argument. A continuous assignment calling it is <b>not reliably sensitive</b> to a net read inside it, so the spread kept whatever width was current when its flag last changed — a <code>vmin.s8</code> after a <code>vsrli.s16</code> spread an s8 compare with the s16 pattern",
    },
    {
      m: "khs_lane",
      s: "add, min, max and every compare all got slower",
      c: "<code>vsrari</code>'s increment borrowed the main adder's second input. The mux in front of the adder is in its cone <b>whether or not a shift is issued</b>: ~0.8 ns of a 4.72 ns path, paid by every instruction. A second SWAR adder is four <code>CARRY8</code> and a handful of LUTs",
    },
    {
      m: "khs_lane",
      s: "a build “without” the shifter measured <b>32 LUT LARGER</b>",
      c: "refusing the ENCODING is not removing the hardware. With <code>khs_pshift32</code> still instantiated the only thing that changed was a decode term",
    },
    {
      m: "khs_lane",
      s: "the DSP48 column lost 12 MHz",
      c: "several terms folded into one register makes the DSP48 do multiply-then-post-add <b>combinationally</b> into PREG — an unpipelined column. <b>One multiply per stage</b>; flops are the cheap resource here and an unpipelined DSP48 is not",
    },
    {
      m: "khs_perm",
      s: "X spreads through everything downstream",
      c: "the pack loop ran to half the element count. Both sources are consumed, so half leaves the top half of the result <b>undriven</b> — which reads as high-Z",
    },
    {
      m: "khs_perm",
      s: "the slide got 224 LUT bigger when “optimised”",
      c: "rewriting the indexed select as an explicit <code>if (idx == k)</code> loop builds a <b>priority chain</b>, which is not a mux. The tool was already pruning the modulo to an 8-way select: 1,600 → 1,824 LUT",
    },
    {
      m: "khs_vregfile",
      s: "the assembled PE binds on the vector file's read path, and the unit alone reports it is fine",
      c: "a stall held the <b>address</b>. That puts the MEM stage's stall — which carries a cache miss and a push handshake — in front of the array. Sending it to the primitive's <b>enable</b> instead is a clock-enable arc: <b>+22.2 MHz on the PE, and 12 logic levels down to 8</b>",
    },
    {
      m: "khs_unit",
      s: "the unit closed at 172.7 MHz against a 339.7 MHz design otherwise identical",
      c: "decode in MEM, from a registered instruction word. That puts <code>funct7</code> through the operation select and the element width through the mask shifters <b>in series</b> with the lane array and the result mux: <b>24 logic levels</b>",
    },
    {
      m: "khs_unit",
      s: "the assembled PE gave up <b>55.4 MHz for 164 LUT</b> on a path carrying no vector logic at all",
      c: "the scalar store into the vector window shared the <b>NoC's</b> port and arbitrated. The arbitration put the receive FIFO's empty flag into the MEM stall and from there into the fetch address",
    },
    {
      m: "khs_unit",
      s: "elaboration hangs on a combinational loop",
      c: "the pass-walk hold term read the <i>combined</i> hold signal to decide whether a pass may issue — and the combined signal <b>contains it</b>. It reads the other two hold terms individually instead",
    },
    {
      m: "khs_unit",
      s: "every folded element comes back X",
      c: "at more than one pass the fold index is narrower than the partial index. Connected directly, <b>the top bits stay undriven</b>. It drives a local wire and is zero-extended",
    },
    {
      m: "khs_facc",
      s: "every partial read comes back zero",
      c: "at <code>NACC = 1</code> the accumulator select is <code>$clog2(1) = 0</code> bits wide, so <b>concatenating</b> it builds a malformed address. The address is arithmetic — <code>acc_sel * NPART + idx</code> — for that reason",
    },
    {
      m: "khs_facc",
      s: "every pass but the first accumulates from zero",
      c: "a seed lands at turn 0, which belongs to pass 0. With disjoint per-pass chains, <b>the first PASSES partials must all take the seed</b> — hence the sweep's own index and the widened seed window",
    },
    {
      m: "khs_unit",
      s: "every accumulated element reads back X in simulation and <code>a × 1.0 + 0</code> in synthesis",
      c: "the float array's <code>op</code> port was left <b>unconnected</b> on every unit. An unconnected input is <code>z</code> in simulation and <code>0</code> — which is <code>OP_MOV</code> — in synthesis, so it reads as a missing <i>value</i> rather than a missing <b>port</b>. Connecting it took the unit's own float stream from 13 errors to 1, and <b>cost no area</b>: the tier measures 3,936 LUT working against 4,174 broken",
    },
    {
      m: "khs_fp32_alu",
      s: "<code>vfadd vd, vs1, vs2</code> adds <code>vd</code> instead of <code>vs2</code>",
      c: "add and subtract take their second operand on the <b>addend</b> port, because the underlying unit computes <code>a × 1.0 + c</code> for both. Wiring <code>c</code> to the destination is right for <code>vfma</code> and only for <code>vfma</code>. It answers a plausible finite, and it cost six checks across three cases before the operand select came back",
    },
    {
      m: "khs_fp32_alu",
      s: "an FMA unit builds a wider element select than it can reach",
      c: "<code>pass</code> is sized for the <b>seed</b> walk, which is the longer of the two whenever there are fewer seed units than FMA units. Each walk must index at its <b>own</b> width — that is what <code>PSW_A</code> exists for — or the placement mux is built across elements no unit on that walk ever addresses",
    },
  ],
};
</script>

<template>
  <DocPage
    title="SIMD microarchitecture"
    summary="How the datapath is actually constructed: one native 32-bit carry chain cut into 2 × int16 or 4 × int8 by a mask, a packed shifter that is one rotate, per-lane multipliers on DSP48E2, a full permutation network, and a float accumulator whose six-cycle recurrence is broken rather than shortened."
    domain="simd"
    status="measured"
    source="src/kohakumpe/simd/ · docs/projects/kohakumpe/simd/ · OOC synthesis on xcvu13p-fhgb2104-2L-e, Vivado 2024.2, 3.333 ns"
  >
    <p class="doc-p">
      The
      <RouterLink to="/mpe/simd" class="doc-link">SIMD PE page</RouterLink> says
      what the datapath computes and what it costs. This page is how it is
      <b>built</b> — module by module, from the RTL — and the interesting
      decisions are all in one place:
      <b
        >what is allowed to sit inside the read-compute-write loop that runs
        from the register file back to its own write port.</b
      >
    </p>

    <Fig
      caption="khs_unit at SIMD 8 with four float lanes. The accented ring is the loop that sets the frequency: khs_vregfile out, through one lane's adder, through the result mux, back into the write port — 12 logic levels and 2.888 ns. Everything to the left of it is EX and is REGISTERED, which is why the control cone is not in the cycle."
      zoom
      wide
    >
      <BlockDiagram
        :nodes="map.nodes"
        :edges="map.edges"
        :groups="map.groups"
      />
    </Fig>

    <Callout
      kind="rule"
      title="Decode is in EX and its RESULT is registered, not the instruction"
    >
      <p>
        Decoding in MEM from a registered instruction word looks cheaper and is
        not: it puts
        <code>funct7</code> through the operation select, and the element width
        through the barrel shifters that build the shift masks,
        <b>in series</b> with the lane datapath and the result mux on the way to
        the write port. That is 24 logic levels, and the unit closed at
        <b>172.7 MHz against a 339.7 MHz design that is otherwise identical</b>.
        Registering the decode costs ~90 flops.
      </p>
    </Callout>

    <h2 class="doc-h2">khs_padd32 — one adder, cut by a mask</h2>

    <p class="doc-p">
      This is the module the word “packed” means, and it is one 32-bit add. The
      carry chain is not gated at the element boundaries — <b>it is starved</b>,
      by clearing the one bit at each boundary that could have produced a carry
      out.
    </p>

    <Callout
      kind="trap"
      title="The obvious construction is the slow one, and it was built first"
    >
      <p>
        Four byte adders per lane with the carry between them gated by the
        element width computes the right answer. Gating a carry means putting a
        LUT between the bytes, and
        <b
          >a LUT in the carry path stops the FPGA using its dedicated carry
          chain</b
        >: four gated bytes become seven chains in series, measured at
        <b>2.05 ns of a 4.72 ns critical path</b> on {{ PART }}.
      </p>
    </Callout>

    <Fig
      caption="One lane of vadd.s8, drawn at the boundary. The gem cells are M — each element's most significant bit, 0x80808080 at .s8. Clearing them in both operands makes every field at most 0x7F, so a field sum is at most 0xFE: the carry chain runs the whole 32 bits as ONE CARRY8 group and still cannot cross an element, because the bit that would have carried is 0 + 0. The top bits are XOR-ed back afterwards, which is exactly what they would have contributed."
      zoom
      wide
    >
      <svg viewBox="0 0 880 306" class="dgm" role="img">
        <text x="96" y="24" class="dgm-label" font-weight="600">
          M = 32'h8080_8080 — each element's MSB. Cleared going in, XOR-ed back
          coming out.
        </text>

        <text
          v-for="(e, i) in PA_EL"
          :key="`bi${i}`"
          :x="PA_X[i] + 12"
          y="46"
          text-anchor="middle"
          class="dgm-sub"
        >
          {{ e.bit }}
        </text>
        <text x="856" y="46" text-anchor="middle" class="dgm-sub">0</text>

        <g v-for="(r, ri) in PA_ROWS" :key="`r${ri}`">
          <text x="88" :y="r.y + 19" text-anchor="end" class="dgm-label">
            {{ r.label }}
          </text>
          <template v-for="(e, i) in PA_EL" :key="`c${ri}-${i}`">
            <rect
              :x="PA_X[i]"
              :y="r.y"
              width="24"
              height="28"
              rx="3"
              class="dgm-box-accent"
            />
            <text
              :x="PA_X[i] + 12"
              :y="r.y + 19"
              text-anchor="middle"
              class="dgm-label"
            >
              {{ e[r.m] }}
            </text>
            <rect
              :x="PA_X[i] + 26"
              :y="r.y"
              width="166"
              height="28"
              rx="3"
              :class="r.accent ? 'dgm-box-accent' : 'dgm-box'"
            />
            <text
              :x="PA_X[i] + 109"
              :y="r.y + 19"
              text-anchor="middle"
              class="dgm-label"
            >
              {{ r.b ? e[r.b] : "0" }}
            </text>
          </template>
        </g>

        <text x="88" y="136" text-anchor="end" class="dgm-sub">carry</text>
        <g v-for="(e, i) in PA_EL" :key="`cy${i}`">
          <path
            :d="`M${PA_X[i] + 190},132 H${PA_X[i] + 6}`"
            class="dgm-edge-accent"
            marker-end="url(#pa-carry)"
          />
        </g>
        <!-- coral DEFAULT, not coral-light: in dark mode coral-light is #F5D5D5
             and reads as the ordinary text colour. -->
        <g
          v-for="x in [288, 480, 672]"
          :key="`brk${x}`"
          class="text-coral-shadow dark:text-coral"
        >
          <path
            :d="`M${x - 11},125 L${x + 3},139 M${x + 3},125 L${x - 11},139`"
            stroke="currentColor"
            stroke-width="2.5"
            fill="none"
          />
        </g>
        <text x="96" y="152" class="dgm-sub">
          ✕ — the carry out of an element's MSB is 0 + 0, at every width, always
        </text>

        <text
          v-for="(e, i) in PA_EL"
          :key="`o${i}`"
          :x="PA_X[i] + 96"
          y="286"
          text-anchor="middle"
          class="dgm-sub"
        >
          = {{ e.out }} · {{ e.dec }}
        </text>

        <defs>
          <marker
            id="pa-carry"
            viewBox="0 0 8 8"
            refX="7"
            refY="4"
            markerWidth="6"
            markerHeight="6"
            orient="auto-start-reverse"
          >
            <path d="M0,0 L8,4 L0,8 z" fill="var(--gem-main)" />
          </marker>
        </defs>
      </svg>
    </Fig>

    <p class="doc-p">
      Subtract is the same trick pointed the other way: <code>a | mask</code>
      <b>sets</b> the top bit instead, so the field is at least its own MSB and
      a subtrahend below that cannot borrow out. Both forms are one line of RTL
      and one native chain.
    </p>

    <Fig
      caption="Three widths, one adder. mask is an INPUT and is not derived in the lane: it depends only on the element width, which is identical in every lane, so khs_unit builds it once in EX and registers it. Deriving it per lane would put a mux in front of the adder in the one cycle this module exists to keep short."
    >
      <SpecTable :cols="masks.cols" :rows="masks.rows" />
    </Fig>

    <h3 class="doc-h3">
      Saturation and compare come back out of three sign bits
    </h3>

    <Fig
      caption="The construction deliberately destroys the carry out of each element — which is the bit saturation and signed comparison would normally be built from — and it is recoverable cheaply. Every element-wise integer operation in the tier is this one adder plus a mux, which is why vmin and vmax cost one mux each rather than a comparator array of their own."
      zoom
      wide
    >
      <BlockDiagram :nodes="flags.nodes" :edges="flags.edges" />
    </Fig>

    <Callout kind="trap" title="`e` IS AN ARGUMENT AND MUST STAY ONE">
      <p>
        Read as a module-level net from inside <code>spread</code>, a continuous
        assignment calling it is <b>not reliably sensitive</b> to that net, and
        the spread keeps whatever width was current when its flag last changed.
        Measured:
        <i
          >a <code>vmin.s8</code> after a <code>vsrli.s16</code> spread an s8
          compare with the s16 pattern and took four bytes from the wrong
          operand.</i
        >
      </p>
    </Callout>

    <h2 class="doc-h2">
      khs_pshift32 — one rotate, and why it is not the adder
    </h2>

    <p class="doc-p">
      A packed shift looks like it needs a left barrel shifter, a right barrel
      shifter, and a per-element bit reversal to share one of them between the
      two. It needs <b>one rotate</b>. Every bit that arrives from the wrong
      element — including the ones the rotate carried around the end of the word
      — lands exactly where a per-element mask is already zero.
    </p>

    <Fig
      caption="One 16-bit slice of a vsrli.s8 by 3, drawn bit by bit. The coral cells in the rotate row came from the element ABOVE; keep is zero in exactly those positions, so they vanish without a single gate deciding it. The masks depend only on the element width and the shift amount — both identical in every lane — so they are built ONCE for the whole unit in EX and registered, not rebuilt SIMD times in MEM."
      zoom
      wide
    >
      <svg viewBox="0 0 600 196" class="dgm" role="img">
        <text x="170" y="24" text-anchor="middle" class="dgm-sub">
          element 1 — bits 15:8
        </text>
        <text x="410" y="24" text-anchor="middle" class="dgm-sub">
          element 0 — bits 7:0
        </text>

        <g v-for="(r, ri) in SH_ROWS" :key="`s${ri}`">
          <text x="102" :y="r.y + 18" text-anchor="end" class="dgm-label">
            {{ r.label }}
          </text>
          <template v-for="(v, k) in r.vals" :key="`sc${ri}-${k}`">
            <rect
              :x="110 + k * 30"
              :y="r.y"
              width="28"
              height="26"
              rx="3"
              :class="[
                (r.kind === 'out' || r.kind === 'mask') &&
                v === 1 &&
                !sh_alien(k)
                  ? 'dgm-box-accent'
                  : 'dgm-box',
                r.kind === 'rot' && sh_alien(k)
                  ? 'text-coral-shadow dark:text-coral'
                  : '',
              ]"
              :style="
                r.kind === 'rot' && sh_alien(k)
                  ? { stroke: 'currentColor', strokeWidth: 1.8 }
                  : null
              "
            />
            <!-- The coral fill is an inline style on purpose: `.dgm .dgm-label`
                 sets fill, and a presentation attribute would lose to it. -->
            <text
              v-if="r.kind === 'rot' && sh_alien(k)"
              :x="124 + k * 30"
              :y="r.y + 18"
              text-anchor="middle"
              class="text-coral-shadow dark:text-coral"
              style="fill: currentColor; font-size: 11px; font-weight: 600"
            >
              {{ v }}
            </text>
            <text
              v-else
              :x="124 + k * 30"
              :y="r.y + 18"
              text-anchor="middle"
              class="dgm-label"
            >
              {{ v }}
            </text>
          </template>
        </g>

        <path
          d="M350,40 V186"
          stroke="currentColor"
          stroke-width="1"
          stroke-dasharray="3 3"
          opacity="0.4"
          fill="none"
        />
      </svg>
    </Fig>

    <SpecTable
      :cols="shiftForms.cols"
      :rows="shiftForms.rows"
      caption="Four forms, one barrel. The bit reversal the base core's EX stage uses for the same trick is free there because it is one 32-bit word; here it would be a three-way mux on 32 bits per lane — 512 LUT at SIMD 8 — because the reversal has to happen WITHIN an element and the element width is a runtime field."
    />

    <Callout
      kind="trap"
      title="vsrari's round bit comes out of the ORIGINAL word"
    >
      <p>
        The rotate lands <code>x[e·EW + s − 1]</code> at the top of the element
        <b>below</b> <code>e</code>, so picking it out of the rotated word reads
        the wrong element's bit. <code>rmask</code> selects it <b>in place</b> —
        one bit per element, built once per unit like the others — and an
        OR-reduce over each element puts it at that element's LSB, where it is a
        carry-in.
      </p>
    </Callout>

    <Callout
      kind="trap"
      title="Sharing the adder taxed every instruction that never shifts"
    >
      <p>
        <code>vsrari</code> reads only one source vector, so the main adder's
        second input looked free — and it borrowed it. The mux in front of the
        adder is in its cone <b>whether or not a shift is issued</b>:
        <b>~0.8 ns of a 4.72 ns path</b>, paid by add, min, max and every
        compare. <code>khs_lane</code> now instantiates <code>khs_padd32</code>
        <b>twice</b>, and the second one is four <code>CARRY8</code> and a
        handful of LUTs — cheaper than what sharing cost in delay.
      </p>
      <p>
        <b>Refusing the encoding is not removing the hardware.</b> With
        <code>khs_pshift32</code> still instantiated, a build “without” the
        shifter measured <b>32 LUT LARGER</b> than the one with it, because the
        only thing that changed was a decode term.
      </p>
    </Callout>

    <h2 class="doc-h2">
      khs_mul — four multipliers, and four more when the sum stays in the column
    </h2>

    <p class="doc-p">
      A 4-way int8 dot needs four products; a 2-way int16 dot needs two, and
      those two need to be 16×16. So each lane carries
      <b
        >two 16×16 multipliers with muxed operands, and two 8×8 multipliers that
        exist only for int8</b
      >
      — operands sign-extended to 17 and 9 bits so the products are signed, and
      17 fits a <code>DSP48E2</code>'s 18 signed B bits with a bit to spare.
    </p>

    <Fig
      caption="khs_mul exists as its own module because `use_dsp` takes a string LITERAL and not a parameter, so choosing between a DSP48 and fabric has to be a generate somewhere; doing it once there keeps the choice out of khs_lane's datapath and makes the primitive a configuration row rather than a synthesis guess. Four multipliers is what the widest element type needs and nothing narrows them, because the integer ALU is ONE IM unit: add, subtract, compare, bitwise and multiply share one operand path and one result path, so there is no dispatch mux between an ALU and a multiplier array."
      zoom
    >
      <BlockDiagram :nodes="mulMap.nodes" :edges="mulMap.edges" />
    </Fig>

    <Callout
      kind="rule"
      title="Multiplexers are the expensive primitive here, not logic"
    >
      <p>
        Two units serving one issue port need an operand mux in front and a
        result mux behind, and both are
        <b>wider than the logic they arbitrate</b>. One fused IM unit has
        neither. That is why the multiplier count is not a knob on either core —
        it follows <code>ILANES</code> here and <code>LANES</code> on the SIMT
        PE — and why narrowing it would trade a LUT-bound resource for a
        DSP-bound one on a part where DSP is the cheap column.
      </p>
      <p>
        The primitive itself is not negotiable either. A
        <code>DSP48E2</code>'s B port is 18 bits <b>signed</b>, which holds one
        int8 operand and not two, and the well-known trick of packing two int8
        MACs into one DSP48 requires the two products to
        <b>share an operand</b>.
      </p>
    </Callout>

    <SpecTable
      :cols="mulWidths.cols"
      :rows="mulWidths.rows"
      caption="What the four multipliers do at each element width. The output register is the DSP's own MREG when the tool takes it, so the latency is one cycle either way and the lane's timing does not move with the primitive"
    />

    <Callout
      kind="trap"
      title="Folding several terms into one register unpipelines the DSP column"
    >
      <p>
        It makes the DSP48 do multiply-then-post-add
        <i>combinationally</i> into PREG, which cost <b>12 MHz</b> when it was
        written that way. <b>One multiply per stage</b> — flops are the cheap
        resource here and an unpipelined DSP48 is not.
      </p>
    </Callout>

    <h2 class="doc-h2">khs_perm — the full permutation network</h2>

    <p class="doc-p">
      Element-wise work never crosses lanes, which is what keeps the lane array
      cheap.
      <code>khs_perm</code> is everything that does: the slide, the saturating
      pack, and the widening unpack. <b>The slide is the expensive one</b>, and
      it is why <code>PERM_UNITS</code> is a width rather than a gate.
    </p>

    <Fig
      caption="The whole network at SIMD 8 and PERM_UNITS 8: each of eight output words selects one of 2 × SIMD = 16 inputs, so it is a 32-bit mux per output whose width grows with SIMD — the only structure here that does. The filled crosspoints are vsldw3 vd, v1, v2: output i takes word (idx + i) mod 16 of the concatenation. It is a ROTATE and not a clamp, so every index is defined at every SIMD width rather than leaving a “what happens past the end” hole."
      zoom
      wide
    >
      <svg viewBox="0 0 730 268" class="dgm" role="img">
        <text x="90" y="22" class="dgm-sub">
          {{
            "the concatenation { v2, v1 } — 2 × SIMD inputs, a0..a7 then b0..b7"
          }}
        </text>

        <text
          v-for="(nm, i) in PERM_IN"
          :key="`pin${i}`"
          :x="90 + i * 40"
          y="44"
          text-anchor="middle"
          class="dgm-sub"
        >
          {{ nm }}
        </text>
        <path
          v-for="(nm, i) in PERM_IN"
          :key="`pv${i}`"
          :d="`M${90 + i * 40},52 V236`"
          class="dgm-edge"
          opacity="0.35"
        />

        <g v-for="j in 8" :key="`po${j}`">
          <text
            x="76"
            :y="64 + (j - 1) * 24"
            text-anchor="end"
            class="dgm-label"
          >
            vd[{{ j - 1 }}]
          </text>
          <path
            :d="`M84,${60 + (j - 1) * 24} H700`"
            class="dgm-edge"
            opacity="0.35"
          />
        </g>

        <g v-for="j in 8" :key="`px${j}`">
          <circle
            v-for="(nm, i) in PERM_IN"
            :key="`pc${j}-${i}`"
            :cx="90 + i * 40"
            :cy="60 + (j - 1) * 24"
            r="2"
            fill="currentColor"
            opacity="0.22"
          />
          <circle
            :cx="90 + PERM_PICK[j - 1] * 40"
            :cy="60 + (j - 1) * 24"
            r="5.5"
            fill="var(--gem-main)"
          />
        </g>

        <text x="90" y="262" class="dgm-sub">
          idx = 3 — vd = [ a3 a4 a5 a6 a7 b0 b1 b2 ]
        </text>
      </svg>
    </Fig>

    <SpecTable
      :cols="packOps.cols"
      :rows="packOps.rows"
      caption="Pack and unpack are per-element and nearly free; unpack is pure wiring plus a sign bit. Only the slide grows with SIMD"
    />

    <Callout kind="trap" title="Half a pack loop leaves the top half UNDRIVEN">
      <p>
        There are <code>VW/16</code> int16 elements per <b>source</b>, and both
        sources are consumed. Running the loop to half that count leaves the top
        half of the result undriven,
        <b
          >which reads as high-Z and then spreads X through everything
          downstream</b
        >
        — a whole-vector failure whose first visible symptom is nowhere near
        this module.
      </p>
    </Callout>

    <h2 class="doc-h2">khs_reduce — a tree, and where it is cut</h2>

    <Fig
      caption="Nodes are indexed heap-style — leaves at SIMD..2·SIMD−1, node n combining 2n and 2n+1 — so the depth is structural rather than something the tool has to find: log2(SIMD) levels, three at SIMD 8. AND EVEN LOG DEPTH IS TOO MUCH FOR ONE CYCLE: measured once the lane adder stopped being the limit, the max tree became the critical path at 12 logic levels and 3.43 ns, five CARRY8 in series, because every node is a 32-bit signed compare and a mux. RED_PIPE = 1 halves that for one cycle of latency on an instruction that runs ONCE PER REDUCTION rather than once per element."
      zoom
      wide
    >
      <BlockDiagram
        :nodes="tree.nodes"
        :edges="tree.edges"
        :groups="tree.groups"
      />
    </Fig>

    <Callout kind="rule" title="A loop would have synthesised as a chain">
      <p>
        A reduction written as a loop carrying a value between iterations
        synthesises as exactly that serial chain — <code>SIMD</code> adders deep
        in one stage. That is the shape that cost the matmul accumulator
        <b>~68 MHz</b> once, and <code>mx_fpacc.v</code>'s header calls it out
        for the same reason.
      </p>
    </Callout>

    <h2 class="doc-h2">The arrays</h2>

    <h3 class="doc-h3">
      khs_vregfile — two mirrored arrays, and an enable rather than an address
    </h3>

    <p class="doc-p">
      <code>VREGS × VW</code> bits, two read ports, one write port, read latency
      1 — as two mirrored <code>kohaku_sdpram</code> written in lockstep,
      because <b>no primitive offers two independent read ports</b>. There is
      deliberately no write-through bypass: a bypass mux at 256 bits is 256 LUT
      on the widest path in the unit, against 32 for the scalar file's, so the
      hazard is handled a stage earlier by a stall instead.
    </p>

    <Callout
      kind="trap"
      title="A stall holds the OUTPUT REGISTER, not the address"
    >
      <p>
        Holding the address puts the MEM stage's stall — which carries a cache
        miss and a push handshake — in front of the array, so the whole of it
        lands on the read path and the assembled PE binds there. Sending the
        stall to the primitive's <b>enable</b> instead is a clock-enable arc
        rather than a mux in front of the array, and the address then arrives
        straight from the instruction. <b>The two are equivalent</b>: with the
        enable low the array holds its last value, which is exactly what
        re-reading a held address produced.
        <b
          >Worth +22.2 MHz on the assembled PE, and it took the binding path
          from 12 logic levels to 8.</b
        >
      </p>
      <p>
        <b
          >The general rule is worth more than the megahertz: let a stall reach
          an enable pin, never an address or a datapath.</b
        >
        It is the same idea that removed the network arbitration one level up.
      </p>
    </Callout>

    <Callout
      kind="rule"
      title="Aim timing work at the assembled PE — a unit-level gain can be a PE-level loss"
    >
      <p>
        Two aimed timing changes in one campaign were each measured at
        <b>both</b> levels, and they disagree. Shortening the unit's own
        read-compute-write loop by deciding a compare in EX made
        <b>the unit 31.0 MHz faster and the PE 21.4 MHz slower</b>. It did not
        make anything worse — it moved the binding path somewhere the unit-alone
        measurement cannot see, into the memory stage's stall and from there
        into the vector file's read <i>address</i>. That path only exists when
        the unit is inside a core, because the hold it rides is the core's and
        carries a cache miss and a push handshake with it.
      </p>
      <p>
        The configuration matrix compares the <b>size</b> of many builds and is
        right to measure the unit alone.
        <b>The clock is a property of the whole PE</b>, and the fix above is
        what the exposed path then needed.
      </p>
      <p>
        <b
          >The absolute frequencies of those rows are deliberately not
          printed.</b
        >
        They are assembled-PE totals taken at
        <code>-flatten_hierarchy none</code> on a source tree that predates the
        current float tier, and every figure of that shape is
        <RouterLink to="/mpe/simd" class="doc-link">withdrawn</RouterLink>. The
        <i>differences</i> between two rows of one flow survive; the endpoints
        do not.
      </p>
    </Callout>

    <h3 class="doc-h3">khs_vspad — SIMD banks, and two faces for free</h3>

    <Fig
      caption="Banking is explicit because a wide face is several tiles either way — a RAMB36E2 is 36 bits per port in true-dual-port mode. Bank b holds every word whose index mod SIMD is b, at row index/SIMD. That is what lets the NoC write one 32-bit word with no read-modify-write anywhere, and what makes a vld one cycle. Every tile is fully depth-utilised: 1024 rows is a RAMB36E2's natural depth at the 1K x 36 aspect a 32-bit port selects."
      zoom
      wide
    >
      <BlockDiagram
        :nodes="spad.nodes"
        :edges="spad.edges"
        :groups="spad.groups"
      />
    </Fig>

    <Callout
      kind="trap"
      title="Which port a scalar store uses is a TIMING decision, not a plumbing one"
    >
      <p>
        A scalar <code>sw</code> into the vector window writes through the port
        the <b>vector unit</b> owns — the wide one — with the byte enables of a
        single bank, because only one instruction is in MEM at a time. Sharing
        the NoC's port and arbitrating instead cost the assembled PE
        <b>55.4 MHz, and 164 LUT to undo</b>, because the arbitration needs the
        NoC's write enable, which is combinational from the receive FIFO's empty
        flag, and that signal then reaches the MEM stage's stall, the fetch
        hold, and the instruction window's address.
      </p>
      <p>
        That is a <b>difference between two rows of one sweep</b> —
        <code>scripts/tcl/ooc_simd_pe.tcl</code>, the whole <code>rv_pe</code>
        with the extension built, at a 3.333 ns request. The two rows'
        <i>absolute</i> frequencies are deliberately not printed: they are
        assembled-PE totals taken at <code>-flatten_hierarchy none</code>, and
        every figure of that shape is
        <RouterLink to="/mpe/simd" class="doc-link">withdrawn</RouterLink> — the
        ship synthesises at Vivado's default, <code>rebuilt</code>, and the gap
        between the two settings varies with configuration, so it cannot be
        applied as a correction either. Only the within-flow difference
        survives.
      </p>
    </Callout>

    <Callout
      kind="rule"
      title="XPORT_OK(0) — no cross-port bypass, and the assertion says why"
    >
      <p>
        <code>rv_spad</code> carries a byte-wise write-through and pays 38 LUT
        for it, because there <b>the collision IS the doorbell</b>. Here the
        doorbell still lives in the scalar scratchpad and this array carries
        bulk data, which push-and-doorbell orders: the payload is written
        <i>before</i> the doorbell the consumer is waiting on.
        <b
          >A program that reads a row while the NoC is writing it has violated
          the protocol rather than used it</b
        >, and <code>rv_ram_be</code>'s permanent assertion says so at zero LUT.
      </p>
      <p>
        The read enable is a real signal for the same reason: left at constant 1
        the port would read whatever row the EX adder produced for a scalar
        instruction, so every NoC write would look like a collision and the
        assertion would fire continuously in a working machine.
      </p>
    </Callout>

    <h2 class="doc-h2">
      The float array — one element width, two walks, one array
    </h2>

    <p class="doc-p">
      <code>khs_fp32_alu</code> is <code>FLOAT_LANES</code> fused multiply-add
      units walking a vector that is <b>32 bits per element</b>. With binary32
      the only compute type there are no narrow slots, no width select and no
      packing: a <code>VW</code>-bit vector is <code>VW/32</code> elements, and
      the walk is <code>elements / FLOAT_LANES</code> passes.
    </p>

    <Fig
      caption="Unit u on pass p serves element p·U + u, and the caller places the results because where one belongs depends on the pass that is retiring. A seed unit sits BESIDE the FMA rather than inside it, and there are FSFU_UNITS of them, so the two walks have different lengths over the same array."
      zoom
      wide
    >
      <BlockDiagram :nodes="fLane.nodes" :edges="fLane.edges" />
    </Fig>

    <Callout
      kind="trap"
      title="Two walks over one array means two pass widths, and only one of them is the port"
    >
      <p>
        The seed walk is the <b>longer</b> of the two whenever there are fewer
        seed units than FMA units, so the <code>pass</code> port is sized for
        it. An FMA unit indexing with all of that builds an element select
        <b>wider than it can reach</b> — a placement mux across elements no unit
        on its walk ever addresses. Each walk indexes at its own width, which is
        what the array's <code>PSW_A</code> local exists for.
      </p>
      <p>
        The same fix on the enclosing unit's staging register is worth
        <b>489 LUT</b> at one seed unit of four, and it is what flips a
        fractional-rate seed tier from a LUT loss into a saving.
      </p>
    </Callout>

    <Callout
      kind="rule"
      title="The depths are 6 and 10, and the pad is flip-flops"
    >
      <p>
        An FMA is <b>6 cycles</b> deep and a seed is <b>10</b>, so the FMA is
        padded to match <i>whenever seeds are built</i> and by nothing when they
        are not — which is why a nonzero <code>FSFU_UNITS</code> deepens the
        whole tier. The enclosing unit derives the same
        <code>FLOAT_ALAT</code> from the same parameter and the two are checked
        against each other at elaboration, because a mismatch lands a result on
        the wrong register with no witness.
      </p>
      <p>
        <b>The pad is flip-flops, deliberately.</b> LUT is what binds this PE
        and an <code>SRL16E</code> is one LUT per bit at any depth, so the
        cheaper-looking primitive is the expensive one here.
      </p>
    </Callout>

    <Callout
      kind="rule"
      title="A seed count is NOT monotonic in LUT, and the middle is the worst place to sit"
    >
      <p>
        BRAM and DSP are exactly linear in the count. LUT is not: the residual
        after the seed hardware is the sum of two terms that move in
        <b>opposite</b> directions — the placement mux grows with the unit count
        (at one unit every element's seed source is the same unit, so there is
        no mux at all) and the pass decode shrinks with it. It is smallest at
        both ends.
      </p>
      <p>
        Full rate builds <b>no walk at all</b>, because both index arms become
        the same expression and the placement mux folds. Quarter rate — the
        ratio every desktop GPU provisions transcendentals at — saves three
        quarters of the BRAM and DSP and rather less of the LUT.
      </p>
    </Callout>

    <h3 class="doc-h3">khs_facc — rotating partials, and the counter</h3>

    <p class="doc-p">
      A binary32 fused multiply-add is six or ten cycles deep, so
      <code>acc = a*b + acc</code> on a single accumulator would issue one
      operation every ten cycles and a tier built that way would be slower than
      the scalar core it sits in.
      <b>The rotation breaks that recurrence without shortening it</b>: one
      architectural accumulator, <code>NPART</code> partials underneath it, and
      a counter. <code>rd_idx</code> advances on every accepted accumulate —
      <b>once per PASS</b>, not once per instruction — and
      <code>wr_idx</code> is that same counter
      <b>delayed by exactly the array's latency</b>, so a result lands on the
      partial its addend came from. Scrub it: the row to watch is how many are
      in flight.
    </p>

    <Callout kind="note" title="The whole accumulator group is off by default">
      <p>
        <code>HAS_FACC</code> is <b>0</b> in the shipped parameter list. It is
        the SIMD PE's extra — justified by vertex transform, float dot and long
        reductions — and a shader doing elementwise colour work pays nothing for
        it. At the anchor it is worth <b>+6,664 LUT, +7,388 FF and +16 DSP</b>,
        which makes it the single largest optional block in the unit.
      </p>
    </Callout>

    <StepPlayer :steps="rot" label="the rotation at NPART = 16, ALAT = 10">
      <template #default="{ state }">
        <svg viewBox="0 0 800 158" class="dgm" role="img">
          <text x="20" y="14" class="dgm-sub">
            partials — one distributed-RAM row each, binary32
          </text>

          <g v-for="k in NPART" :key="`p${k}`">
            <rect
              :x="20 + (k - 1) * 48"
              y="58"
              width="42"
              height="34"
              rx="4"
              :class="state.rd === k - 1 ? 'dgm-box-accent' : 'dgm-box'"
              :stroke-dasharray="
                state.flight.includes(k - 1) ? '3 3' : undefined
              "
            />
            <text
              :x="41 + (k - 1) * 48"
              y="80"
              text-anchor="middle"
              class="dgm-label"
            >
              {{ k - 1 }}
            </text>
          </g>

          <g v-if="state.rd !== null">
            <polygon
              :points="`${35 + state.rd * 48},40 ${47 + state.rd * 48},40 ${41 + state.rd * 48},52`"
              fill="var(--gem-main)"
            />
            <text
              :x="41 + state.rd * 48"
              y="34"
              text-anchor="middle"
              class="dgm-label"
              font-weight="600"
            >
              rd_idx
            </text>
          </g>

          <g v-if="state.wr !== null">
            <polygon
              :points="`${35 + state.wr * 48},112 ${47 + state.wr * 48},112 ${41 + state.wr * 48},100`"
              fill="var(--gem-main)"
            />
            <text
              :x="41 + state.wr * 48"
              y="130"
              text-anchor="middle"
              class="dgm-label"
              font-weight="600"
            >
              {{ state.sweep ? "sweep_k" : "wr_idx" }}
            </text>
          </g>

          <text x="20" y="152" class="dgm-sub">
            dashed = a write is still in flight for that partial
          </text>
        </svg>
        <div class="flex flex-wrap gap-2 mt-3">
          <span class="chip">cycle = {{ state.cyc }}</span>
          <span class="chip"
            >rd_idx = {{ state.rd === null ? "—" : state.rd }}</span
          >
          <span class="chip"
            >wr_idx = {{ state.wr === null ? "—" : state.wr }}</span
          >
          <span class="chip">in flight = {{ state.inflight }}/{{ ALAT }}</span>
          <span class="chip">II = 1</span>
        </div>
      </template>
    </StepPlayer>

    <Callout
      kind="rule"
      title="The rotation is ARCHITECTURAL, not an implementation detail"
    >
      <p>
        The scrubber runs at one pass, where every turn belongs to the same
        element.
        <b
          >At the reference's four lanes each element owns only the turns
          congruent to its own pass</b
        >
        — four of the sixteen, a disjoint set — so an element's accumulation
        chain is <code>NPART / passes</code> partials rather than
        <code>NPART</code>.
      </p>
      <p>
        Float addition does not associate, so
        <b
          >a build with a different <code>NPART</code> — or a different lane
          count — computes different answers on the same program</b
        >. The golden model rotates identically and the ISA states the order:
        element <i>i</i>, on the <i>n</i>th accumulate since
        <code>vfaccz</code>, lands on partial
        <code>(n × passes + i / lanes) mod NPART</code>.
        <code>khs_unit_tb</code> carries the lane count in its configuration
        guard for exactly this reason: a vector set generated for one lane count
        and run against another has to <b>name the mismatch</b> rather than fail
        as arithmetic.
      </p>
    </Callout>

    <Callout
      kind="measured"
      title="The partials are a MEMORY, and that is 28k LUT"
    >
      <p>
        As flops the array was <b>29,409 LUT of a 52,532-LUT unit</b> — 56 % of
        the whole thing — because every one of 12,288 bits carried a D-input mux
        between an accumulate result, a seed and zero, with two variable-index
        read muxes on top. As two mirrored distributed RAMs, the construction
        <code>khs_vregfile</code> already uses, it is <b>843</b>.
      </p>
      <p>
        The cost of the memory is that a write port is a write port:
        <code>vfaccz</code> and <code>vfaccwr</code> become an
        <code>NPART</code>-cycle <b>sweep</b> rather than a parallel clear — the
        last step of the scrubber above.
      </p>
    </Callout>

    <h3 class="doc-h3">
      khs_ffold — serial, through the same array, once per pass
    </h3>

    <SpecTable
      :cols="foldCost.cols"
      :rows="foldCost.rows"
      caption="The fold runs through the same array the accumulate used rather than through a second adder, because a second adder would round differently from the path it is meant to finish. Every unit folds at once, so the whole accumulator folds in the time one unit takes"
    />

    <Callout kind="trap" title="A flat fold would sum ACROSS elements">
      <p>
        Element <i>e</i>'s chain is the turns congruent to its pass, so folding
        flat over all <code>NPART</code> partials returns a
        <b>plausible wrong answer</b>. The unit runs one fold per pass instead,
        each over that pass's own strided subset.
      </p>
      <p>
        A float accumulate is still in flight
        <b><code>ALAT</code> cycles after its instruction retires</b>, so
        <code>vfaccz</code>, <code>vfaccwr</code> and <code>vfaccrd</code> stall
        until the shadow — a shift register of that depth, read in EX — is
        clear. <code>vfmacc</code> is deliberately <b>not</b> on that list:
        rotation is what lets one issue every cycle.
      </p>
    </Callout>

    <h2 class="doc-h2">The path all of it lands on</h2>

    <Fig
      caption="m_alu_op_reg/C → u_vrf/.../mem_reg/RAMF_D1/I — 12 logic levels, 2.860 ns, on the integer unit at 3.333 ns. FOUR OF THE TWELVE LEVELS ARE THE CARRY CHAIN AND THEY COST 0.23 ns BETWEEN THEM: 0.027 ns per level against 0.038–0.090 for every LUT on the path. Counting levels without reading them would call this path half again as deep as it is, and would point at the one structure that made the datapath fast."
      zoom
      wide
    >
      <BlockDiagram :nodes="cpath.nodes" :edges="cpath.edges" />
    </Fig>

    <p class="doc-p">
      The other reading is that <b>77 % of the delay is interconnect</b>,
      concentrated on the broadcast of the decode bits to every lane. That is
      the shape of a wide uniform-control datapath: one decode, many consumers —
      and it is why the masks are built once in EX rather than
      <code>SIMD</code> times in MEM.
    </p>

    <h2 class="doc-h2">Traps, collected</h2>

    <p class="doc-p">
      Every row is a failure that <b>happened</b>, and each one is recorded in
      the header of the module it happened in.
    </p>

    <SpecTable :cols="traps.cols" :rows="traps.rows" />

    <Callout kind="note" title="Where the numbers are">
      <p>
        Every path delay, logic-level count and block figure on this page is
        <b>out-of-context synthesis</b> on <b>{{ PART }}</b
        >, Vivado 2024.2, at <code>SIMD = 8</code> and a
        <b>3.333 ns</b> request. Nothing is placed and nothing is routed, so a
        frequency here is an upper bound — this project has measured a module
        lose 0.740 ns from synthesis to routing. Where a figure measures a
        module in isolation rather than the assembled unit, the row says so.
      </p>
      <p>
        <b>The whole-PE totals are not here.</b> The reference row, every
        per-feature delta, and the list of figures that were <i>not</i> carried
        forward are on
        <RouterLink to="/mpe/simd" class="doc-link">the SIMD PE page</RouterLink
        >, taken from <code>docs/projects/kohakumpe/unit-counts.md</code>, which
        names the frozen source tree behind each of its tables.
      </p>
      <p>
        <b
          >The two flatten settings answer different questions and are not
          mixed.</b
        >
        The per-block figures on this page are
        <code>-flatten_hierarchy none</code>, and they have to be: it is the
        setting that preserves module boundaries, so a census there names real
        cells. <code>rebuilt</code> <b>re-parents leaves</b>, so a per-block row
        taken there is not that block's cost — from one reference build the
        vector register file reported <b>5,657 LUT while holding 444 LUTRAM</b>,
        having absorbed several thousand LUT of logic that is not its own.
        <b
          >A subtraction between two <code>rebuilt</code> totals is sound; a
          <code>rebuilt</code> hierarchy row is not.</b
        >
      </p>
    </Callout>
  </DocPage>
</template>
