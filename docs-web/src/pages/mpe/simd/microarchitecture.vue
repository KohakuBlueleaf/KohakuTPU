<script setup>
// ===========================================================================
// SIMD PE — microarchitecture. How the datapath is actually CONSTRUCTED for the
// operations it supports, module by module, read out of the RTL rather than
// restated from prose.
//
// TWO ASKS. The khs_unit sweeps and the block probes here are OOC synthesis on
// xcvu13p-fhgb2104-2L-e at 3.333 ns, integer only, with SIMD_DOTDSP = 0 and
// WB_STAGE = 0. The assembled reference build is at 2.857 ns with both on.
// Rows from the two are not comparable; each one below says which it is.
// ===========================================================================

const PART = "xcvu13p-fhgb2104-2L-e";

// ------------------------------------------------------------- module map
const map = {
  nodes: [
    {
      id: "dec",
      x: 0,
      y: 0,
      w: 15,
      label: "decode — in EX",
      sub: "the DECODE is registered, ~90 flops",
      accent: true,
    },
    {
      id: "msk",
      x: 0,
      y: 4.5,
      w: 15,
      label: "shift + element masks",
      sub: "built ONCE, not SIMD times",
      accent: true,
    },

    {
      id: "vrf",
      x: 18,
      y: 0,
      w: 15,
      label: "khs_vregfile",
      sub: "2 mirrored sdpram · read latency 1",
    },
    {
      id: "vsp",
      x: 18,
      y: 4.5,
      w: 15,
      label: "khs_vspad",
      sub: "SIMD banks × 1024 × 32b",
    },

    {
      id: "padd",
      x: 36,
      y: 0,
      w: 15,
      label: "khs_padd32",
      sub: "ONE native carry chain",
      accent: true,
    },
    {
      id: "psh",
      x: 36,
      y: 4.5,
      w: 15,
      label: "khs_pshift32",
      sub: "one rotate, masked",
    },
    {
      id: "rnd",
      x: 36,
      y: 9,
      w: 15,
      label: "khs_padd32  u_rnd",
      sub: "the vsrari increment, its OWN adder",
    },
    {
      id: "mul",
      x: 36,
      y: 13.5,
      w: 15,
      label: "khs_mul × MULS",
      sub: "17×17 ×2 · 9×9 ×2, +4 cascaded",
    },

    {
      id: "perm",
      x: 54,
      y: 0,
      w: 15,
      label: "khs_perm",
      sub: "2·SIMD : 1 per output lane",
    },
    {
      id: "red",
      x: 54,
      y: 4.5,
      w: 15,
      label: "khs_reduce",
      sub: "a tree, root registered",
    },
    {
      id: "acc",
      x: 54,
      y: 9,
      w: 15,
      label: "acc[NACC × SIMD]",
      sub: "int32 · one-cycle recurrence",
    },
    {
      id: "res",
      x: 54,
      y: 13.5,
      w: 15,
      label: "the result mux",
      sub: "every select is a decode bit",
      accent: true,
    },
    {
      id: "wsc",
      x: 72,
      y: 4.5,
      w: 14,
      label: "w_sc → rv_mem",
      sub: "vextr · vredsum · vredmax",
    },

    {
      id: "fl",
      x: 18,
      y: 19.5,
      w: 16,
      label: "khs_float_lane × LANES",
      sub: "vec_alu, op tied to FMA · 4 at the reference",
    },
    {
      id: "fac",
      x: 37,
      y: 19.5,
      w: 16,
      label: "khs_facc",
      sub: "NPART partials, LANES wide",
    },
    {
      id: "ffo",
      x: 56,
      y: 19.5,
      w: 15,
      label: "khs_ffold",
      sub: "serial, once per PASS",
    },
  ],
  edges: [
    { from: "dec:r", to: "vrf:l", dir: "h" },
    { from: "dec:b", to: "msk:t", dir: "v" },
    { from: "msk:r", to: "psh:l", dir: "h", accent: true },
    { from: "msk:r", to: "padd:l", dir: "h", accent: true },
    { from: "vrf:r", to: "padd:l", dir: "h", accent: true },
    { from: "vrf:b", to: "mul:l", dir: "v" },
    { from: "vsp:r", to: "res:l", dir: "h", dash: true },
    { from: "padd:r", to: "res:l", dir: "h", accent: true },
    { from: "psh:b", to: "rnd:t", dir: "v" },
    { from: "rnd:r", to: "res:l", dir: "h" },
    { from: "mul:r", to: "acc:l", dir: "h" },
    { from: "acc:b", to: "res:t", dir: "v" },
    { from: "vrf:r", to: "perm:l", dir: "h" },
    { from: "perm:r", to: "res:r", dir: "h" },
    { from: "vrf:r", to: "red:l", dir: "h" },
    { from: "red:r", to: "wsc:l", dir: "h" },
    { from: "res:l", to: "vrf:b", dir: "v", accent: true },
    { from: "vrf:b", to: "fl:t", dir: "v" },
    { from: "fl:r", to: "fac:l", dir: "h" },
    { from: "fac:t", to: "res:b", dir: "v", dash: true },
    { from: "ffo:l", to: "fac:r", dir: "h" },
  ],
  groups: [
    { x: 35, y: -1.3, w: 17, h: 18, label: "khs_lane × SIMD" },
    {
      x: 17,
      y: 18.3,
      w: 55,
      h: 5.6,
      label: "the float tier — SIMD_FLOAT = 1 only",
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
    { id: "a", x: 0, y: 0, w: 13, label: "a[31:0]", sub: "one lane of v1" },
    { id: "b", x: 0, y: 4.5, w: 13, label: "b[31:0]", sub: "one lane of v2" },
    {
      id: "et",
      x: 0,
      y: 9,
      w: 13,
      label: "et",
      sub: "the element type, in the instruction",
      accent: true,
    },
    {
      id: "m0",
      x: 17,
      y: 0,
      w: 17.5,
      label: "u_m0 — 17 × 17",
      sub: "s8 a[7:0] · s16 a[15:0]",
      accent: true,
    },
    {
      id: "m1",
      x: 17,
      y: 4.5,
      w: 17.5,
      label: "u_m1 — 17 × 17",
      sub: "s8 a[15:8] · s16 a[31:16]",
      accent: true,
    },
    {
      id: "m2",
      x: 17,
      y: 9,
      w: 17.5,
      label: "u_m2 — 9 × 9",
      sub: "a[23:16], int8 ONLY",
    },
    {
      id: "m3",
      x: 17,
      y: 13.5,
      w: 17.5,
      label: "u_m3 — 9 × 9",
      sub: "a[31:24], int8 ONLY",
    },
    {
      id: "lo",
      x: 38,
      y: 11.2,
      w: 14,
      label: "mul_lo",
      sub: "the products, at +1 — the FANOUT that forces the second set",
      accent: true,
    },
    {
      id: "casc",
      x: 55,
      y: 2.2,
      w: 16,
      h: 4.4,
      label: "c0 → c1 → c2 → c3",
      sub: "a SECOND set, PCIN cascade, dot_sum at +4",
      accent: true,
    },
    {
      id: "fab",
      x: 55,
      y: 9,
      w: 16,
      label: "sum_r ← p0+p1+hi",
      sub: "SIMD_DOTDSP = 0 · dot_sum at +2",
    },
  ],
  edges: [
    { from: "a:r", to: "m0:l", dir: "h" },
    { from: "b:r", to: "m1:l", dir: "h" },
    { from: "et:r", to: "m2:l", dir: "h", accent: true, label: "operand mux" },
    { from: "et:r", to: "m3:l", dir: "h" },
    { from: "m0:b", to: "lo:l", dir: "v", accent: true },
    { from: "m2:r", to: "lo:l", dir: "h" },
    { from: "a:r", to: "casc:l", dir: "h", dash: true },
    { from: "m0:r", to: "fab:l", dir: "h" },
    { from: "m1:r", to: "fab:l", dir: "h" },
  ],
};

const dotLat = {
  cols: [
    { key: "b", label: "build", mono: true },
    { key: "d", label: "DOT_LAT", mono: true, align: "right" },
    { key: "s", label: "where the sum comes from" },
    { key: "c", label: "at eight lanes" },
  ],
  rows: [
    {
      b: "SIMD_DOTDSP = 0",
      d: "2",
      s: "a fabric adder tree, one register behind the products",
      c: "32 DSP48 · 256 LUT + 32 CARRY8 of adder",
    },
    {
      b: "<b>SIMD_DOTDSP = 1, MULS ≥ 4</b>",
      d: "<b>4</b>",
      s: "a <b>DSP48 PCIN cascade</b> — four terms, one hop per cycle, operands pipelined so term <i>k</i> waits <i>k</i> cycles",
      c: "<b>64 DSP48 · 0 LUT + 0 CARRY8</b>",
      _tone: "good",
    },
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
      r: "four products, summed to one int32 per lane. <b>32 MACs per instruction</b> at SIMD 8",
      _tone: "good",
    },
    {
      et: "s16",
      m01: "a[15:0]·b[15:0], a[31:16]·b[31:16]",
      m23: "<b>idle</b> — <code>hi</code> is tied to <code>34'sd0</code>",
      r: "two products per lane, 16 MACs per instruction",
    },
    {
      et: "s32",
      m01: "—",
      m23: "—",
      r: "<b>ILLEGAL → FAULT.</b> An int32 product does not fit a 34-bit lane sum, for <code>vdot</code> and <code>vmul</code> alike",
      _tone: "bad",
    },
    {
      et: "s8, MULS = 2",
      m01: "built",
      m23: "<b>not elaborated</b>",
      r: "<b>ILLEGAL → FAULT.</b> A two-multiplier lane would return zero for the top two elements, so the encoding is refused instead",
      _tone: "bad",
    },
  ],
};

const dotPipe = {
  rows: [
    {
      name: "MEM",
      kind: "bus",
      values: ["vdot #1", "vdot #2", "vdot #3", "", "", ""],
    },
    { name: "mul_en", kind: "bit", values: [1, 1, 1, 0, 0, 0] },
    {
      name: "p0..p3",
      kind: "bus",
      values: [null, "#1", "#2", "#3", null, null],
      mark: [1],
    },
    {
      name: "sum_r",
      kind: "bus",
      values: [null, null, "#1", "#2", "#3", null],
      mark: [2],
    },
    {
      name: "acc_pipe",
      kind: "bus",
      values: ["00", "01", "11", "11", "10", "00"],
    },
    {
      name: "the add fires",
      kind: "bus",
      values: [null, null, "#1", "#2", "#3", null],
      mark: [2, 3, 4],
    },
    {
      name: "",
      kind: "text",
      values: ["issue", "issue", "issue", "", "", "acc holds #3"],
    },
  ],
  notes: [
    {
      text: "Drawn at DOT_LAT = 2. Each stage is a register, so the stages are a pipeline and not a latency to wait out — the accumulator index and the negate bit travel down beside the sum, so each one reaches the accumulate stage in ISSUE ORDER.",
      tone: "good",
    },
    {
      cycle: 2,
      text: "A DOT DOES NOT WAIT FOR A DOT. Making vdot wait like vaccrd / vaccz / vaccwr cost 3 cycles per dot on dot2_i8_v — 80 cycles for 58 instructions, where every other hazard in that kernel accounts for 9.",
      tone: "good",
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

// ========================================================= the float lane
const fLane = {
  nodes: [
    {
      id: "a",
      x: 0,
      y: 0,
      w: 13,
      label: "a[31:0]",
      sub: "FP16 in [15:0], or FP32",
    },
    {
      id: "b",
      x: 0,
      y: 4.5,
      w: 13,
      label: "b[31:0]",
      sub: "sign-flipped for vfmsac",
    },
    {
      id: "p",
      x: 0,
      y: 13.5,
      w: 13,
      label: "the partial",
      sub: "from khs_facc, E8M15",
    },
    {
      id: "ca",
      x: 16,
      y: 0,
      w: 16,
      label: "vec_cvt_f16_to_e8  × 2",
      sub: "one per operand · EXACT",
      accent: true,
    },
    {
      id: "cw",
      x: 16,
      y: 4.5,
      w: 16,
      label: "vec_cvt_f32_to_e8  × 2",
      sub: "one per operand · exponent verbatim",
      accent: true,
    },
    {
      id: "wid",
      x: 16,
      y: 9,
      w: 16,
      label: "wide",
      sub: "TIED LOW in khs_unit — live in the SIMT PE",
    },
    {
      id: "sel",
      x: 35,
      y: 4.5,
      w: 14,
      label: "a_sel / b_sel",
      sub: "wide picks the edge; raw_e8 picks the fold path",
    },
    {
      id: "alu",
      x: 52,
      y: 4.5,
      w: 17,
      h: 4.4,
      label: "vec_alu  op = OP_FMA",
      sub: "ALAT = 14 + PIPE_MUX = 15 · II = 1 · 2 DSP48",
      accent: true,
    },
    {
      id: "out",
      x: 52,
      y: 13.5,
      w: 17,
      label: "out — E8M15",
      sub: "back into the partial it came from",
    },
  ],
  edges: [
    { from: "a:r", to: "ca:l", dir: "h" },
    { from: "b:r", to: "ca:l", dir: "h" },
    { from: "a:r", to: "cw:l", dir: "h" },
    { from: "ca:r", to: "sel:l", dir: "h", accent: true },
    { from: "cw:r", to: "sel:l", dir: "h" },
    { from: "wid:r", to: "sel:l", dir: "h" },
    { from: "sel:r", to: "alu:l", dir: "h", accent: true },
    { from: "p:r", to: "alu:l", dir: "h", label: "c" },
    { from: "alu:b", to: "out:t", dir: "v", accent: true },
  ],
};

const laneBlocks = [
  {
    label: "normaliser — leading-one search and a 48-bit shift",
    value: 156,
    note: "26 %",
  },
  {
    label: "aligner — a 48-bit shift and its sticky",
    value: 113,
    note: "19 %",
  },
  { label: "magnitude and sign recovery", value: 67, note: "11 %" },
  { label: "exponent base and shift amount", value: 39, note: "6 %" },
  { label: "rounder, exponent bounds, assemble", value: 29, note: "5 %" },
  { label: "specials", value: 24, note: "4 %" },
  { label: "the pipeline's delay lines, as SRLs", value: 139, note: "23 %" },
];

const dspPrice = {
  cols: [
    { key: "b", label: "Block", mono: true },
    { key: "f", label: "In fabric — SHIPPED", align: "right" },
    { key: "d", label: "Rebuilt on a DSP48", align: "right" },
    { key: "n", label: "" },
  ],
  rows: [
    {
      b: "aligner",
      f: "113 LUT",
      d: "<b>44 LUT + 1 DSP</b>",
      n: "the DSP's B port is 18 bits, so a 7-bit shift splits 4 + 2: a multiply by a one-hot power of two, then a placement mux over the 48-bit field",
    },
    {
      b: "normaliser",
      f: "156 LUT",
      d: "<b>114 LUT + 1 DSP</b>",
      n: "only <b>seventeen bits</b> of the normalised value are ever used, so it extracts a 27-bit window (the DSP's A port) by <code>pos[5:3]</code> and multiplies by <code>2^(7−pos[2:0])</code>; the answer is <code>P[26:10]</code>",
    },
  ],
};

// =============================================== khs_facc, the rotation
const NPART = 16,
  ALAT = 15;
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
    note: "The lane will not return that first result for fourteen more cycles. Nothing waits, because the next accumulate is reading a DIFFERENT partial.",
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
    title: "cycle 14 — fourteen in flight, one partial still untouched",
    note: "This is the state the rotation exists to reach: the lane is full, II is still 1, and the recurrence has never once been closed.",
    ...strip(14, null, [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13]),
    cyc: "14",
    inflight: 14,
  },
  {
    title: "cycle 15 — the LAST free partial, and the first result returns",
    note: "wr_idx is rd_idx delayed by exactly ALAT, so accumulate #0's result lands on partial 0 — the partial its addend came from. Fifteen are in flight and the sixteenth is being read: the lane is exactly saturated.",
    ...strip(15, 0, [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14]),
    cyc: "15",
    inflight: 15,
  },
  {
    title:
      "cycle 16 — the counter wraps onto the partial written ONE cycle ago",
    note: "NPART > ALAT is the whole guarantee, and this is where it is spent: partial 0 was written at the cycle-15 edge and is read at cycle 16. With NPART = 15 the write and the re-read would be the same cycle, and a read-first array would hand back the stale value.",
    ...strip(0, 1, [2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]),
    cyc: "16",
    inflight: 15,
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
      w: "each step depends on the last, so the steps are <code>ALAT</code> apart. Through <b>the same lane</b> the accumulate used — combining partials in a second adder would round differently from the path it is meant to finish. <b>One fold per PASS</b>, over that pass's own strided subset",
    },
    {
      s: "packing back to FP16, per pass",
      c: "lanes",
      w: "ONE walked <code>vec_cvt_e8_to_f16</code>. It carries a 48-bit subnormal shifter at <b>161 LUT</b>; sixteen of them would be <b>2,576 LUT</b> hung on the end of an instruction that already holds the stage for hundreds of cycles",
    },
    {
      s: "vfaccrd, total",
      c: "≈ 270",
      w: "<b>the same at every lane count</b> — <code>passes</code> folds of <code>NPART/passes</code> steps is <code>NPART</code> steps either way. <b>Once per reduction, against a kernel of thousands.</b> A tree would be log2(NPART) float adders standing idle the rest of the time",
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
      m: "khs_lane",
      s: "with <code>SIMD_DOTDSP = 1</code> the dot result never arrives",
      c: "the cascade was gated by <code>mul_en</code>. That is a per-instruction pulse — the fabric path gates only the products with it and lets the sum register flow — so gating a multi-stage cascade <b>freezes hops 2..N</b>. It must free-run, as the accumulator's own shift register does",
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
      s: "the assembled PE bound at 318.3 MHz on the read path",
      c: "a stall held the <b>address</b>. That puts the MEM stage's stall — which carries a cache miss and a push handshake — in front of the array. The read enable reaches the primitive's <b>enable</b> instead, which is a clock-enable arc",
    },
    {
      m: "khs_unit",
      s: "the unit closed at 172.7 MHz against a 339.7 MHz design otherwise identical",
      c: "decode in MEM, from a registered instruction word. That puts <code>funct7</code> through the operation select and the element width through the mask shifters <b>in series</b> with the lane array and the result mux: <b>24 logic levels</b>",
    },
    {
      m: "khs_unit",
      s: "the assembled PE lost 93.6 MHz — 284.3 against a 377.9 baseline",
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
      m: "khs_float_lane",
      s: "every result reads as a dead accumulator",
      c: "an unconnected <code>raw_e8</code> is <code>z</code>, the operand select goes X, and it reads as a missing <i>value</i> rather than a missing <b>port</b>. It cost two benches a run, and there is now a non-synthesis check that names it",
    },
  ],
};
</script>

<template>
  <DocPage
    title="SIMD microarchitecture"
    summary="How the datapath is actually constructed: one native 32-bit carry chain cut into 2 × int16 or 4 × int8 by a mask, a packed shifter that is one rotate, four multipliers on DSP48E2 and four more when the dot sum stays in the column, a full permutation network, and a float lane whose fifteen-cycle recurrence is broken rather than shortened."
    domain="simd"
    status="measured"
    source="src/kohakumpe/simd/ · docs/projects/kohakumpe/simd/ · OOC on xcvu13p-fhgb2104-2L-e at 3.333 ns (unit probes) and 2.857 ns (assembled reference)"
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
      caption="khs_mul exists as its own module because `use_dsp` takes a string LITERAL and not a parameter, so choosing between a DSP48 and fabric has to be a generate somewhere; doing it once there keeps the choice out of khs_lane's datapath and makes it a configuration row rather than a synthesis guess. The accented path is the one that forces the second set of multipliers: p0..p3 must surface for mul_lo, and an operand with two consumers cannot be cascaded."
      zoom
      wide
    >
      <BlockDiagram :nodes="mulMap.nodes" :edges="mulMap.edges" />
    </Fig>

    <Callout kind="rule" title="There is no cheaper arrangement on this device">
      <p>
        A <code>DSP48E2</code>'s B port is 18 bits <b>signed</b>, which holds
        one int8 operand and not two. The well-known trick of packing two int8
        MACs into one DSP48 requires the two products to
        <b>share an operand</b> — and a dot product's operands both vary, so
        they cannot.
      </p>
    </Callout>

    <SpecTable
      :cols="mulWidths.cols"
      :rows="mulWidths.rows"
      caption="What the four multipliers do at each element width. The output register is the DSP's own MREG when the tool takes it, so the latency is one cycle either way and the lane's timing does not move with the primitive"
    />

    <WaveTrace
      label="three vdot back to back — II = 1 into the SAME accumulator"
      :rows="dotPipe.rows"
      :notes="dotPipe.notes"
    />

    <SpecTable
      :cols="dotLat.cols"
      :rows="dotLat.rows"
      caption="DOT_LAT is a CONTRACT between khs_unit and khs_lane: both derive it from the same two parameters, and a disagreement lands an accumulate on the wrong destination. It is also visible to a program — vaccrd, vaccz and vaccwr behind a dot in flight wait DOT_LAT cycles, so 4 as shipped rather than 2"
    />

    <Callout
      kind="trap"
      title="The cascade free-runs; gating it with mul_en freezes it"
    >
      <p>
        <code>mul_en</code> is a per-instruction pulse, and the fabric path can
        gate the products with it because the sum register flows anyway.
        <b
          >Gating a multi-stage cascade instead freezes hops 2..N and the result
          never arrives.</b
        >
        The accumulator's own shift register free-runs on the same clock, which
        is what keeps the two aligned.
      </p>
      <p>
        The other half of the same lesson is upstream: folding several terms
        into one register makes the DSP48 do multiply-then-post-add
        <i>combinationally</i> into PREG — an unpipelined column, which cost
        <b>12 MHz</b> when it was written that way.
        <b>One multiply per stage</b>, and flops are the cheap resource here.
      </p>
    </Callout>

    <h2 class="doc-h2">khs_perm — the full permutation network</h2>

    <p class="doc-p">
      Element-wise work never crosses lanes, which is what keeps the lane array
      cheap.
      <code>khs_perm</code> is everything that does: the slide, the saturating
      pack, and the widening unpack. <b>The slide is the expensive one</b>, and
      it is why <code>SIMD_PERM</code> exists.
    </p>

    <Fig
      caption="The whole network at SIMD 8: each of eight output lanes selects one of 2 × SIMD = 16 inputs, so it is a 32-bit mux per lane whose width grows with SIMD — the only structure here that does. The filled crosspoints are vsldw3 vd, v1, v2: lane i takes lane (idx + i) mod 16 of the concatenation. It is a ROTATE and not a clamp, so every index is defined at every SIMD width rather than leaving a “what happens past the end” hole. Measured at 1,634 LUT and 33.9 MHz — the largest optional block on both counts."
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
        lands on the read path:
        <b>the assembled PE bound there at 318.3 MHz</b>. The read enable
        reaches the primitive's <b>enable</b>
        instead, which is a clock-enable arc, and the address arrives straight
        from the instruction. The two are equivalent: with the enable low the
        array holds its last value, which is exactly what re-reading a held
        address produced.
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
        <b>93.6 MHz: 284.3 against a 377.9 baseline</b>, because the arbitration
        needs the NoC's write enable, which is combinational from the receive
        FIFO's empty flag, and that signal then reaches the MEM stage's stall,
        the fetch hold, and the instruction window's address.
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
      The float lane — one port, two widths, one compute format
    </h2>

    <p class="doc-p">
      An E8M15 fused multiply-add on this device is <b>fifteen cycles deep</b>,
      so <code>acc = a*b + acc</code> on a single accumulator issues one
      operation every fifteen cycles and a tier built that way would be slower
      than the scalar core it sits in. Everything in the accumulator follows
      from breaking that recurrence without shortening it.
    </p>

    <Fig
      caption="khs_float_lane. The tier needs NO new float arithmetic: vec_alu is the vector core's shipped, verified module with its operation tied to FMA, and the conversions sit on the lane's edges rather than being instructions. BOTH converters are unconditional — the lane's header calls both input formats and the one compute format a contract rather than options — and `wide` picks between them per operation."
      zoom
      wide
    >
      <BlockDiagram :nodes="fLane.nodes" :edges="fLane.edges" />
    </Fig>

    <Callout
      kind="trap"
      title="khs_unit ties that width bit LOW, so the FP32 edge is built and unreachable"
    >
      <p>
        Every float lane in <code>khs_unit</code> is instantiated with
        <code>wide</code> tied to <code>1'b0</code>, and the decoder refuses any
        float element type but <code>f16</code> — except <code>vfaccz</code>,
        which is untyped. <b>The area is spent and the encoding faults.</b> That
        is a half-finished transition, not a capability.
      </p>
      <p>
        The blocker is structural:
        <b>a 256-bit register holds 8 FP32 against 16 FP16</b>, so the element
        count, the partial count and the fold order all change with the operand
        width — and float addition does not associate. The
        <RouterLink to="/mpe/simt" class="doc-link">SIMT PE</RouterLink> drives
        the same lane with the bit live because it has one element per lane in
        both formats and pays none of that.
      </p>
    </Callout>

    <Callout kind="rule" title="ALAT is a contract, not a detail">
      <p>
        <code>localparam ALAT = 14 + (PIPE_MUX ? 1 : 0)</code> — 15 as it ships
        — <b>must equal what vec_alu actually is</b>, because the accumulator
        above rotates its partials by exactly this number. Get it wrong and a
        write lands on a partial that has already been read again.
        <code>vec_lanes.v</code> derives its own copy the same way and says the
        same thing.
      </p>
      <p>
        <code>khs_e8_fma</code> is the same lane with everything except the FMA
        made unreachable and a register on each side, so an out-of-context run
        measures the lane and not its pins.
        <b>Nothing in <code>vec_alu</code> is modified</b> — what a stripped
        lane costs is a measurement rather than a subtraction done on paper.
      </p>
    </Callout>

    <h3 class="doc-h3">Where the LUT is inside the FMA</h3>

    <ResourceBars
      :items="laneBlocks"
      unit="LUT"
      caption="vec_alu's FMA taken apart stage by stage, from the block probes on xcvu13p-fhgb2104-2L-e at 3.333 ns. Two barrel shifters are 44 % of it. This is NOT a lane total — the lane also carries the two operand converters, which the block probe does not measure"
    />

    <SpecTable
      :cols="dspPrice.cols"
      :rows="dspPrice.rows"
      caption="A variable shift is a multiply by a one-hot power of two, so both shifters can be rebuilt against a DSP48 — and the split is set by the port width. NEITHER is in the shipped lane: they are measurement probes that nothing instantiates, and taking them would mean this unit owning a fork of another project's verified FMA. They are what the shifters WOULD be worth, not what they cost"
    />

    <h3 class="doc-h3">khs_facc — rotating partials, and the counter</h3>

    <p class="doc-p">
      One architectural accumulator, <code>NPART</code> partials underneath it,
      and a counter. <code>rd_idx</code> advances on every accepted accumulate —
      <b>once per PASS</b>, not once per instruction — and
      <code>wr_idx</code> is that same counter
      <b>delayed by exactly the lane's latency</b>, so a result lands on the
      partial its addend came from. Scrub it: the row to watch is how many are
      in flight.
    </p>

    <StepPlayer :steps="rot" label="the rotation at NPART = 16, ALAT = 15">
      <template #default="{ state }">
        <svg viewBox="0 0 800 158" class="dgm" role="img">
          <text x="20" y="14" class="dgm-sub">
            partials — one distributed-RAM row each, E8M15
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
      khs_ffold — serial, through the same lane, once per pass
    </h3>

    <SpecTable
      :cols="foldCost.cols"
      :rows="foldCost.rows"
      caption="The fold multiplies a partial — already E8M15 — by E8_ONE, which is why the lane carries the raw_e8 / a_e8 operand path: the fold needs the lane WITHOUT the conversion in front of it. Every lane folds at once, so the whole accumulator folds in the time one lane takes"
    />

    <Callout kind="trap" title="A flat fold would sum ACROSS elements">
      <p>
        Element <i>e</i>'s chain is the turns congruent to its pass, so folding
        flat over all <code>NPART</code> partials returns a
        <b>plausible wrong answer</b>. The unit runs one fold per pass instead,
        each over that pass's own strided subset — and the tier probe, which
        does fold flat, is therefore comparable in lane count and <b>not</b> in
        arithmetic. Its own header says so.
      </p>
      <p>
        A float accumulate is still in flight
        <b>fifteen cycles after its instruction retires</b>, so
        <code>vfaccz</code>, <code>vfaccwr</code> and <code>vfaccrd</code> stall
        until the shadow — a fifteen-bit shift register read in EX — is clear.
        <code>vfmacc</code> is deliberately <b>not</b> on that list: rotation is
        what lets one issue every cycle.
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
        The <code>khs_unit</code> sweeps and the block probes on this page are
        out-of-context synthesis on <b>{{ PART }}</b> at
        <code>SIMD = 8</code> and a <b>3.333 ns</b> request, integer only, with
        <code>SIMD_DOTDSP = 0</code> and <code>WB_STAGE = 0</code>. The
        assembled reference build — 8 int + 4 float,
        <b>13,772 LUT · 10,126 FF · 13 BRAM · 72 DSP48 · 353.4 MHz</b> — is at
        <b>2.857 ns</b> with both knobs on. Rows from the two are not
        comparable, and Vivado 2024.2 synthesis only means a routed result will
        be somewhat worse.
      </p>
      <p>
        Cycle figures come from the PE's own <code>CTL_CYCLE</code> counter on
        the full system, and <b>predate both shipped knobs</b>, each of which
        adds a stall class. The full tables, the mesh arithmetic and the list of
        figures that were <i>not</i> carried forward are on
        <RouterLink to="/mpe/simd" class="doc-link">the SIMD PE page</RouterLink
        >.
      </p>
    </Callout>
  </DocPage>
</template>
