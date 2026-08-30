<script setup>
// ===========================================================================
// Vector core — microarchitecture.
// The ALU as it is BUILT: which term lands on which DSP port, why the
// alignment is one shifter, how the four seeds share one normaliser, and what
// the array around sixteen of them costs. Every figure is out-of-context
// synthesis on xcvu13p-fhgb2104-2L-e.
// ===========================================================================

// ------------------------------------------------------------- the lane
// Landscape, because the subject is: cycles run left to right. Three tracks —
// polynomial on top, exponent in the middle with the shifter on its end,
// significand below — converge on DSP-M, and the last row is shared by the FMA
// and all four seeds.
const lane = {
  nodes: [
    // ---- the polynomial track
    {
      id: "rr",
      x: 11.5,
      y: 0,
      w: 10.5,
      h: 3.4,
      label: "cycle 1 · exp2 reduce",
      sub: "{1,M,10'b0} >> (134 − e_a) · ONE right shift",
    },
    {
      id: "neg",
      x: 23,
      y: 0,
      w: 10.5,
      h: 3.4,
      label: "cycle 2 · round+negate",
      sub: "−(R+g) = ~R + ~g · in PARALLEL",
    },
    {
      id: "tab",
      x: 34.5,
      y: 0,
      w: 10.5,
      h: 3.4,
      label: "vec_tables · cycle 3",
      sub: "block RAM · 32 segments · c0 c1 c2",
      accent: true,
    },
    {
      id: "dspp",
      x: 57.5,
      y: 0,
      w: 10.5,
      h: 4.2,
      label: "DSP-P · Horner stage 1",
      sub: "c2·u + (c1<<16) + 2^15 → h = P[37:16]",
      accent: true,
    },

    // ---- the exponent track, and the shifter it feeds
    {
      id: "in",
      x: 0,
      y: 6,
      w: 10.5,
      h: 3.4,
      label: "cycle 0 · input regs",
      sub: "a · b · c · op · zero flags precede it",
    },
    {
      id: "cmp",
      x: 11.5,
      y: 6,
      w: 10.5,
      h: 3.4,
      label: "ordered compare",
      sub: "unsigned order of a[22:0] · ~20 LUT",
    },
    {
      id: "dspe",
      x: 23,
      y: 6,
      w: 10.5,
      h: 4.2,
      label: "DSP-E · (A+D)·B + C",
      sub: "A=e_a D=e_b B=2^12+1 C={129, 385−e_c}",
      accent: true,
    },
    {
      id: "sha",
      x: 34.5,
      y: 6,
      w: 10.5,
      h: 3.4,
      label: "cycle 4 · s and ebase",
      sub: "s = P[11:0] − 495 · ebase = P[23:12] − 286",
    },
    {
      id: "algn",
      x: 46,
      y: 6,
      w: 10.5,
      h: 4.2,
      label: "cycle 5 · alignment",
      sub: "{sig_c, 32'b0} >> s · ONE direction",
      accent: true,
    },

    // ---- the significand track, and the join
    {
      id: "sel",
      x: 11.5,
      y: 12,
      w: 10.5,
      h: 3.4,
      label: "operand select",
      sub: "cycle 1 · va vb vc · sub is a sign flip",
    },
    {
      id: "dly",
      x: 23,
      y: 12,
      w: 10.5,
      h: 3.4,
      label: "vec_delay lines",
      sub: "u_d_ga/gb D=6 → DSP-M · u_d_gc D=4 → shifter",
    },
    {
      id: "dspm",
      x: 46,
      y: 12,
      w: 13,
      h: 4.6,
      label: "DSP-M · result in cycle 10",
      sub: "sig_a·sig_b ± aligned_c, or h·u + c0",
      accent: true,
    },

    // ---- the shared back end
    {
      id: "mag",
      x: 0,
      y: 18,
      w: 10.5,
      h: 3.4,
      label: "magnitude and sign",
      sub: "res_neg = neg & (s ≠ 0) & P[47]",
    },
    {
      id: "l1s",
      x: 11.5,
      y: 18,
      w: 10.5,
      h: 3.4,
      label: "cycle 11 · lead1",
      sub: "smear-isolate-encode, log depth",
    },
    {
      id: "nrm",
      x: 23,
      y: 18,
      w: 10.5,
      h: 3.4,
      label: "cycle 12 · normalise",
      sub: "mag << (47 − pos)",
    },
    {
      id: "rnd",
      x: 34.5,
      y: 18,
      w: 10.5,
      h: 3.4,
      label: "cycle 13 · round",
      sub: "nearest-even · e_fin = pos + ebase",
    },
    {
      id: "out",
      x: 46,
      y: 18,
      w: 10.5,
      h: 3.4,
      label: "cycle 14 · out_valid",
      sub: "24 bits + out_pred · II = 1",
      accent: true,
    },
  ],
  edges: [
    { from: "in:r", to: "cmp:l", dir: "h" },
    { from: "in:r", to: "rr:l" },
    { from: "in:r", to: "sel:l" },
    { from: "cmp:b", to: "sel:t", dir: "v" },
    { from: "cmp:r", to: "dspe:l", dir: "h" },
    { from: "sel:r", to: "dspe:l" },
    { from: "rr:r", to: "neg:l", dir: "h" },
    { from: "neg:r", to: "tab:l", dir: "h" },
    { from: "tab:r", to: "dspp:l", dir: "h", accent: true },
    { from: "dspe:r", to: "sha:l", dir: "h", accent: true },
    { from: "sha:r", to: "algn:l", dir: "h", label: "s" },
    { from: "sel:r", to: "dly:l", dir: "h" },
    { from: "dly:r", to: "algn:b", dir: "h", label: "sig_c" },
    { from: "dly:r", to: "dspm:l", dir: "h", label: "sig_a sig_b" },
    { from: "algn:b", to: "dspm:t", dir: "v", accent: true },
    { from: "dspp:b", to: "dspm:r", dir: "v", accent: true, label: "h" },
    { from: "dspm:b", to: "mag:t", dir: "v", accent: true },
    { from: "mag:r", to: "l1s:l", dir: "h" },
    { from: "l1s:r", to: "nrm:l", dir: "h" },
    { from: "nrm:r", to: "rnd:l", dir: "h" },
    { from: "rnd:r", to: "out:l", dir: "h", accent: true },
  ],
  groups: [
    {
      x: -1,
      y: 17.2,
      w: 59,
      h: 5,
      label:
        "ONE lead1, ONE normalising shift, ONE rounder — the FMA and all four seeds",
    },
  ],
};

// The DSP latency contract, drawn. vec_dsp.v states it as a rule; this is the
// rule applied to DSP-M's two operand sets.
const dspContract = {
  rows: [
    {
      name: "A / B / D",
      kind: "bus",
      values: [null, null, null, "drive", null, null],
    },
    {
      name: "C / ALUMODE",
      kind: "bus",
      values: [null, null, null, null, "drive", null],
    },
    { name: "AREG BREG DREG", kind: "bit", values: [0, 0, 0, 1, 0, 0] },
    { name: "MREG", kind: "bit", values: [0, 0, 0, 0, 1, 0] },
    { name: "PREG", kind: "bit", values: [0, 0, 0, 0, 0, 1] },
    {
      name: "P readable",
      kind: "bus",
      values: [null, null, null, null, null, "P"],
      mark: [5],
    },
    {
      name: "",
      kind: "text",
      values: ["", "", "", "N − 3", "N − 2", "N"],
    },
  ],
  notes: [
    {
      text: "A/B/D driven in cycle k gives P in k+3; C and ALUMODE are driven one cycle later and still land on the same P. ALUMODE travels WITH C — ALUMODEREG = 1 — because without it the mode arrives a cycle after the addend it applies to: right on stable operands, wrong on a stream.",
    },
    {
      text: "ADREG = 0 on purpose. At ADREG = 1 the A/D path reaches the multiplier through two registers and B through one, so B arrives a cycle early and multiplies against the wrong operand. Every port is kept at the same 3-cycle depth, so nothing needs compensating.",
    },
  ],
};

// ------------------------------------------------------- DSP-E, two answers
const dspeP = [
  { name: "unused", bits: 24, value: "P[47:24]" },
  {
    name: "u = e_a + e_b + 129",
    bits: 12,
    value: "P[23:12] → ebase",
    accent: true,
  },
  { name: "e_a + e_b + 385 − e_c", bits: 12, value: "P[11:0] → s" },
];

const dspe = {
  nodes: [
    { id: "ea", x: 0, y: 0, w: 11, label: "A = e_a", sub: "8 bits" },
    { id: "eb", x: 0, y: 4, w: 11, label: "D = e_b", sub: "8 bits" },
    {
      id: "pre",
      x: 14,
      y: 2,
      w: 11,
      label: "pre-adder",
      sub: "INMODE 00100 · (A+D)",
    },
    {
      id: "bk",
      x: 14,
      y: 8,
      w: 11,
      label: "B = 2^12 + 1",
      sub: "TWO TAPS — one constant",
      accent: true,
    },
    {
      id: "mul",
      x: 28,
      y: 4,
      w: 13,
      label: "(e_a + e_b) at TWO positions",
      sub: "bit 12 and bit 0 of the same product",
      accent: true,
    },
    {
      id: "ck",
      x: 28,
      y: 10.5,
      w: 13,
      label: "C = {129, 385 − e_c}",
      sub: "one bias per field, independent",
    },
    {
      id: "p",
      x: 45,
      y: 6.5,
      w: 13,
      h: 4.4,
      label: "P — both answers, one pass",
      sub: "high field always positive · low field spans [133, 892]",
      accent: true,
    },
  ],
  edges: [
    { from: "ea:r", to: "pre:l", dir: "h" },
    { from: "eb:r", to: "pre:l", dir: "h" },
    { from: "pre:r", to: "mul:l", dir: "h" },
    { from: "bk:r", to: "mul:l", dir: "h", accent: true },
    { from: "mul:r", to: "p:l", dir: "h", accent: true },
    { from: "ck:r", to: "p:l", dir: "h" },
  ],
};

// ------------------------------------------------------- alignment shifter
const align = {
  nodes: [
    {
      id: "sraw",
      x: 0,
      y: 0,
      w: 13,
      label: "s_raw = P[11:0] − 495",
      sub: "signed, 13 bits",
    },
    { id: "cz", x: 0, y: 5, w: 13, label: "vc_z", sub: "the addend is zero" },
    {
      id: "byp",
      x: 16,
      y: 0,
      w: 13,
      label: "byp = s_raw < 0",
      sub: "cs ≤ −18 — the addend IS the rounded answer",
      accent: true,
    },
    {
      id: "s",
      x: 16,
      y: 5.5,
      w: 13,
      h: 4,
      label: "s, clamped to [0, 48]",
      sub: "48 if vc_z · 0 if byp · else s_raw",
      accent: true,
    },
    {
      id: "sh",
      x: 33,
      y: 2.5,
      w: 15,
      h: 4.4,
      label: "{sig_c, 32'b0} >> s",
      sub: "48 bits, ONE barrel shifter, 6-bit amount",
      accent: true,
    },
    {
      id: "stk",
      x: 33,
      y: 9,
      w: 15,
      label: "sticky",
      sub: "only sig_c can leave — nothing goes until s > 32",
    },
    {
      id: "c",
      x: 52,
      y: 2.5,
      w: 12,
      h: 4.4,
      label: "DSP-M  C port",
      sub: "raw 48-bit pattern; bit 47 may be a VALUE bit",
    },
  ],
  edges: [
    { from: "sraw:r", to: "byp:l", dir: "h" },
    { from: "sraw:b", to: "s:l", dir: "h" },
    { from: "cz:r", to: "s:l", dir: "h" },
    { from: "byp:b", to: "s:t", dir: "v" },
    { from: "s:r", to: "sh:l", dir: "h", accent: true },
    { from: "s:r", to: "stk:l", dir: "h" },
    { from: "sh:r", to: "c:l", dir: "h", accent: true },
  ],
};

const headroom = {
  cols: [
    { key: "s", label: "significand", mono: true, align: "right" },
    { key: "p", label: "product", mono: true, align: "right" },
    { key: "h", label: "headroom needed", mono: true, align: "right" },
    { key: "o", label: "what the datapath becomes" },
  ],
  rows: [
    {
      s: "<b>16 (E8M15)</b>",
      p: "<b>32</b>",
      h: "<b>17</b>",
      o: "<b>one unidirectional shifter, one bypass, addend on C</b>",
      _tone: "good",
    },
    {
      s: "17 (E8M16)",
      p: "34",
      h: "18",
      o: "B port is fine — C overflows by 4, so the shift leaves the DSP",
      _tone: "warn",
    },
    {
      s: "18 (E8M17)",
      p: "36",
      h: "19",
      o: "overflows by 7, <i>and</i> an 18-bit unsigned reads negative on the 18-bit signed B",
      _tone: "bad",
    },
    {
      s: "24 (FP32)",
      p: "48",
      h: "25",
      o: "<b>overflows by 25 — the product alone fills the window</b>",
      _tone: "bad",
    },
  ],
};

// ------------------------------------------------------- the 17-bit split
// Layout: a and b both feed both partial products (a K2,2), which always
// crosses when the two operands share a column. So b sits BETWEEN the two
// partial products and feeds them top and bottom; a fans out from the left.
const split = {
  nodes: [
    {
      id: "a",
      x: 0,
      y: 4.7,
      w: 12,
      label: "a — 24-bit significand",
      sub: "split at bit 17",
    },
    { id: "b", x: 16, y: 4.7, w: 12, label: "b", sub: "the other operand" },
    {
      id: "hi",
      x: 16,
      y: 0,
      w: 12,
      label: "high partial product",
      sub: "→ DSP-P",
    },
    {
      id: "lo",
      x: 16,
      y: 9.4,
      w: 12,
      label: "low partial product",
      sub: "→ DSP-M",
    },
    {
      id: "dspp",
      x: 31,
      y: 0,
      w: 13,
      h: 4,
      label: "DSP-P",
      sub: "PCOUT — cascade only, no fabric",
      accent: true,
    },
    {
      id: "dspm",
      x: 31,
      y: 8.3,
      w: 13,
      h: 5.4,
      label: "DSP-M — three ALU operands",
      sub: "X+Y = M · Z = PCIN >> 17 · W = C",
      accent: true,
    },
    {
      id: "cc",
      x: 31,
      y: 16,
      w: 13,
      label: "C = the addend",
      sub: "free, on the W mux",
    },
    {
      id: "out",
      x: 48,
      y: 8.3,
      w: 13,
      h: 5.4,
      label: "24×24 + addend, one pass",
      sub: "rel. error 6.0e-8 · DSP48E2 ONLY",
      accent: true,
    },
  ],
  edges: [
    { from: "a:r", to: "hi:l", dir: "h" },
    { from: "a:r", to: "lo:l", dir: "h" },
    { from: "b:t", to: "hi:b", dir: "v" },
    { from: "b:b", to: "lo:t", dir: "v" },
    { from: "hi:r", to: "dspp:l", dir: "h" },
    { from: "lo:r", to: "dspm:l", dir: "h" },
    { from: "dspp:b", to: "dspm:t", dir: "v", accent: true, label: "cascade" },
    { from: "cc:t", to: "dspm:b", dir: "v" },
    { from: "dspm:r", to: "out:l", dir: "h", accent: true },
  ],
};

// ------------------------------------------------------- transcendentals
const seeds = {
  nodes: [
    { id: "x", x: 0, y: 4, w: 12, label: "the operand", sub: "E · M, sliced" },
    {
      id: "idx",
      x: 15,
      y: 0,
      w: 13,
      label: "idx[5:0]",
      sub: "M[14:10], or exp2's f[16:12] · idx[5] = octave parity, rsqrt only",
      accent: true,
    },
    {
      id: "u",
      x: 15,
      y: 6.5,
      w: 13,
      label: "u[11:0]",
      sub: "{M[9:0], 2'b00} — zero-padding is exact",
    },
    {
      id: "rom",
      x: 31,
      y: 0,
      w: 14,
      h: 4.4,
      label: "vec_tables",
      sub: "block RAM · fsel picks one of four · 22-bit c0 c1 c2",
      accent: true,
    },
    {
      id: "h1",
      x: 31,
      y: 6.5,
      w: 14,
      h: 4.4,
      label: "DSP-P — stage 1",
      sub: "A = c2 · B = u · C = {c1, 1'b1, 15'b0}",
      accent: true,
    },
    {
      id: "h2",
      x: 51,
      y: 6.5,
      w: 14,
      h: 4.4,
      label: "DSP-M — stage 2",
      sub: "A = h · B = u · C = {c0_full, 1'b1, 15'b0}",
      accent: true,
    },
    {
      id: "id",
      x: 51,
      y: 0,
      w: 14,
      label: "identity substitute",
      sub: "idx = 0 and u = 0 — the exact value, muxed on c0",
    },
    {
      id: "f",
      x: 68,
      y: 6.5,
      w: 12,
      h: 4.4,
      label: "F",
      sub: "and an ebase — into the shared normaliser",
    },
  ],
  edges: [
    { from: "x:r", to: "idx:l", dir: "h" },
    { from: "x:r", to: "u:l", dir: "h" },
    { from: "idx:r", to: "rom:l", dir: "h", accent: true },
    { from: "rom:b", to: "h1:t", dir: "v", label: "c2, c1" },
    { from: "u:r", to: "h1:l", dir: "h" },
    { from: "h1:r", to: "h2:l", dir: "h", accent: true, label: "h = P[37:16]" },
    { from: "id:b", to: "h2:t", dir: "v", dash: true },
    { from: "h2:r", to: "f:l", dir: "h", accent: true },
  ],
};

const normal = {
  cols: [
    { key: "op", label: "op", mono: true },
    { key: "mag", label: "magnitude into lead1", mono: true },
    { key: "eb", label: "ebase", mono: true },
    { key: "n", label: "what the assembly costs" },
  ],
  rows: [
    {
      op: "FMA",
      mag: "|C ± M|",
      eb: "u − 286",
      n: "the full path — lead1, shift, round",
    },
    {
      op: "exp2",
      mag: "2^20 + F",
      eb: "k + 107",
      n: "<b>no normaliser and no leading-zero count</b> — 2^f is in [1,2) by construction",
      _tone: "good",
    },
    {
      op: "log2",
      mag: "|(E−127)·2^20 + F|",
      eb: "107",
      n: "the E term is an integer ADD, riding beside c0",
    },
    {
      op: "inv",
      mag: "F",
      eb: "234 − e_a",
      n: "1 bit of normalise, folded into the output slice",
    },
    {
      op: "rsqrt",
      mag: "F",
      eb: "107 − K",
      n: "K = (e_a−127) >>> 1 — an arithmetic shift is floor for both signs",
    },
  ],
};

const tableAcc = {
  cols: [
    { key: "f", label: "seed", mono: true },
    { key: "p", label: "minimax prediction", mono: true, align: "right" },
    { key: "m", label: "the circuit, measured", mono: true, align: "right" },
    { key: "g", label: "margin over 2^-16", align: "right" },
    { key: "n", label: "Newton refinement" },
  ],
  rows: [
    {
      f: "exp2(f)",
      p: "2^-23.2",
      m: "<b>2^-19.9</b>",
      g: "3.9 bits",
      n: "<b>none exists</b>",
      _tone: "warn",
    },
    {
      f: "log2(m)",
      p: "2^-21.1",
      m: "<b>2^-19.5</b>",
      g: "3.5 bits",
      n: "<b>none exists</b>",
      _tone: "warn",
    },
    {
      f: "inv(m)",
      p: "2^-20.0",
      m: "<b>2^-19.4</b>",
      g: "3.4 bits",
      n: "y' = y(2 − ay) — two FMAs",
    },
    {
      f: "rsqrt(m)",
      p: "2^-21.7",
      m: "<b>2^-19.8</b>",
      g: "3.8 bits",
      n: "y' = y(1.5 − 0.5ay²) — three FMAs",
    },
  ],
};

// ------------------------------------------------------------- conversion
const cvt = {
  nodes: [
    {
      id: "mem",
      x: 0,
      y: 0,
      w: 13,
      label: "L1 word",
      sub: "256 bits, in the MEMORY format",
    },
    {
      id: "peer",
      x: 0,
      y: 6.5,
      w: 13,
      label: "a cluster's peer port",
      sub: "S1 E7 M14 — a MESH format, never a memory one",
    },
    {
      id: "i16",
      x: 17,
      y: -2,
      w: 14,
      label: "f16 → e8",
      sub: "EXACT · a subnormal is a lead1 and a shift",
      accent: true,
    },
    {
      id: "i32",
      x: 17,
      y: 2.5,
      w: 14,
      label: "f32 → e8",
      sub: "exponent field verbatim, 23 → 15 round",
    },
    {
      id: "iacc",
      x: 17,
      y: 6.5,
      w: 14,
      label: "vec_cvt_acc",
      sub: "e8 = e7 + 64 — E7 sits INSIDE E8, so there is no overflow path",
      accent: true,
    },
    {
      id: "rf",
      x: 36,
      y: 2.5,
      w: 13,
      h: 4.4,
      label: "register file",
      sub: "E8M15, 3R1W per lane",
      accent: true,
    },
    {
      id: "o16",
      x: 54,
      y: 0,
      w: 14,
      label: "e8 → f16",
      sub: "the ONLY lossy, range-limited direction",
    },
    {
      id: "o32",
      x: 54,
      y: 5,
      w: 14,
      label: "e8 → f32",
      sub: "EXACT — zero-extend, exponent unchanged",
    },
    {
      id: "st",
      x: 72,
      y: 2.5,
      w: 13,
      label: "L1 / mesh",
      sub: "software sees FP16 or FP32, nothing else",
    },
  ],
  edges: [
    { from: "mem:r", to: "i16:l", dir: "h" },
    { from: "mem:r", to: "i32:l", dir: "h" },
    { from: "peer:r", to: "iacc:l", dir: "h" },
    { from: "i16:r", to: "rf:l", dir: "h", accent: true },
    { from: "i32:r", to: "rf:l", dir: "h" },
    { from: "iacc:r", to: "rf:l", dir: "h" },
    { from: "rf:r", to: "o16:l", dir: "h", accent: true },
    { from: "rf:r", to: "o32:l", dir: "h" },
    { from: "o16:r", to: "st:l", dir: "h" },
    { from: "o32:r", to: "st:l", dir: "h" },
    {
      from: "o16:t",
      to: "i16:t",
      dir: "v",
      dash: true,
      label: "VCVT reuses these",
    },
  ],
};

// ------------------------------------------------------------- chaining
const CHAIN_OPS = [
  "mul",
  "exp2",
  "add",
  "inv",
  "mul",
  "exp2",
  "add",
  "inv",
  "mul",
  "exp2",
  "add",
  "inv",
  "mul",
  "exp2",
  "add",
  "inv",
];
const CHAIN_STAGE = [
  "0",
  "1",
  "2",
  "3",
  "0",
  "1",
  "2",
  "3",
  "0",
  "1",
  "2",
  "3",
  "0",
  "1",
  "2",
  "3",
];
const CHAIN_GROUP = [
  "0",
  "0",
  "0",
  "0",
  "1",
  "1",
  "1",
  "1",
  "2",
  "2",
  "2",
  "2",
  "3",
  "3",
  "3",
  "3",
];
const CHAIN_SRC = [
  "V",
  "chain",
  "chain",
  "chain",
  "V",
  "chain",
  "chain",
  "chain",
  "V",
  "chain",
  "chain",
  "chain",
  "V",
  "chain",
  "chain",
  "chain",
];
const HEADS = [1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0];

const FLAT16 = Array(16).fill(1);
const flatOps = (op) => Array(16).fill(op);
const flatSrc = Array(16).fill("V");
const flatStage = Array(16).fill("0");
const flatGroup = Array.from({ length: 16 }, (_, i) => String(i));

const chaining = [
  {
    title: "FLAT, pass 1 of 4 — mul",
    note: "16 × 1. Every ALU is a chain head, every ALU reads a vector register, and the result goes back to the register file as v1. One instruction, one beat per chunk.",
    mask: FLAT16,
    ops: flatOps("mul"),
    src: flatSrc,
    stage: flatStage,
    group: flatGroup,
    rfw: "16 writes → v1",
    stall: "—",
    kind: "flat",
  },
  {
    title: "FLAT, pass 2 of 4 — exp2, and it cannot issue yet",
    note: "The lane is 14 deep with NO bypass, so the sequencer holds beat 0 while any write to a source register is still pending. exp2 reads v1, and v1's writes are still in flight.",
    mask: FLAT16,
    ops: flatOps("exp2"),
    src: flatSrc,
    stage: flatStage,
    group: flatGroup,
    rfw: "16 reads v1 → 16 writes v2",
    stall: "beat 0 waits on pend[v1]",
    kind: "flat",
  },
  {
    title: "FLAT, pass 3 of 4 — add",
    note: "Again: fetch four cycles, wait for the previous op's writes to retire, then one beat per chunk. Every intermediate has been materialised in the register file and read straight back out.",
    mask: FLAT16,
    ops: flatOps("add"),
    src: flatSrc,
    stage: flatStage,
    group: flatGroup,
    rfw: "16 reads v2 → 16 writes v3",
    stall: "beat 0 waits on pend[v2]",
    kind: "flat",
  },
  {
    title: "FLAT, pass 4 of 4 — inv",
    note: "Four instructions, three intermediate vectors written and re-read, three scoreboard stalls of a 14-deep pipeline. This is what the compiler's fusion pass exists to delete.",
    mask: FLAT16,
    ops: flatOps("inv"),
    src: flatSrc,
    stage: flatStage,
    group: flatGroup,
    rfw: "16 reads v3 → 16 writes vd",
    stall: "beat 0 waits on pend[v3]",
    kind: "flat",
  },
  {
    title: "D4 — the same sigmoid, one pass",
    note: "4 groups × 4 stages. Only stage 0 may read a vector register; stages 1–3 take their upstream neighbour's out and its out_valid directly. Nothing touches the register file in between, so there is no pending write to wait on and no scoreboard stall at all.",
    mask: HEADS,
    ops: CHAIN_OPS,
    src: CHAIN_SRC,
    stage: CHAIN_STAGE,
    group: CHAIN_GROUP,
    rfw: "16 reads → 16 writes, ONCE",
    stall: "none — no operand crosses the file",
    kind: "d4",
  },
];

const chainCost = {
  cols: [
    { key: "w", label: "per 128-element vector (8 chunks)" },
    { key: "f", label: "four FLAT ops", mono: true, align: "right" },
    { key: "d", label: "one D4 chain", mono: true, align: "right" },
  ],
  rows: [
    { w: "instructions fetched", f: "4", d: "4" },
    { w: "issue beats", f: "8 × 4 = 32", d: "8 × 4 = 32" },
    {
      w: "intermediate vectors in the register file",
      f: "<b>3</b>",
      d: "<b>0</b>",
      _tone: "good",
    },
    {
      w: "scoreboard stalls of a 14-deep lane",
      f: "<b>3</b>",
      d: "<b>0</b>",
      _tone: "good",
    },
    {
      w: "register-file read ports touched",
      f: "4 × 16",
      d: "1 × 16",
      _tone: "good",
    },
  ],
};

// ------------------------------------------------- operand network + xbar
const network = {
  nodes: [
    {
      id: "rf",
      x: 0,
      y: 4,
      w: 14,
      h: 4.4,
      label: "16 register-file slices",
      sub: "lane i holds elements i, i+16, i+32 …",
      accent: true,
    },
    {
      id: "win",
      x: 18,
      y: 0,
      w: 15,
      h: 4.4,
      label: "the phase window",
      sub: "the beat reads slices [ph·W, ph·W+W−1] — selected ONCE, not per ALU",
      accent: true,
    },
    {
      id: "src",
      x: 18,
      y: 6.5,
      w: 15,
      h: 4.4,
      label: "per-ALU source mux",
      sub: "V, chain, or a constant — G and T are per-mode LOCALPARAMS",
      accent: true,
    },
    {
      id: "alu",
      x: 37,
      y: 3,
      w: 13,
      h: 4.4,
      label: "16 × vec_alu",
      sub: "1,249 LUT each, standalone",
    },
    {
      id: "xbar",
      x: 37,
      y: 10.5,
      w: 13,
      h: 4.4,
      label: "write crossbar",
      sub: "slice d's producer is a function of the MODE ALONE",
      accent: true,
    },
    {
      id: "wr",
      x: 0,
      y: 10.5,
      w: 14,
      h: 4.4,
      label: "write-enable decode",
      sub: "taken ONE STAGE EARLY and registered",
    },
  ],
  edges: [
    { from: "rf:r", to: "win:l", dir: "h" },
    { from: "win:b", to: "src:t", dir: "v", accent: true },
    { from: "src:r", to: "alu:l", dir: "h", accent: true },
    { from: "alu:b", to: "xbar:t", dir: "v" },
    { from: "xbar:l", to: "wr:r", dir: "h" },
    { from: "wr:t", to: "rf:b", dir: "v", accent: true },
  ],
  groups: [
    {
      x: 17,
      y: -1.2,
      w: 17,
      h: 12.9,
      label: "the operand network — what a SIMT lane does not have",
    },
  ],
};

const shrink = [
  {
    label: "operand network — phase window + constant indices",
    value: 3404,
    note: "vec_lanes",
  },
  {
    label: "coefficient ROMs to block RAM",
    value: 3575,
    note: "per core, → 16 RAMB36",
  },
  {
    label: "register file to block RAM",
    value: 3352,
    note: "of which +1,129 came back",
  },
  { label: "predicate write-back", value: 1987, note: "vec_lanes" },
  { label: "stage-0 narrowing", value: 1256, note: "vec_lanes" },
  {
    label: "write crossbar — a 3:1 dressed as a 16:1",
    value: 1089,
    note: "vec_lanes",
  },
  {
    label: "16-lane rotate — two 4:1 stages, not a flat 16:1",
    value: 565,
    note: "vec_core",
  },
];

// --------------------------------------------------------------- vec_delay
const delays = {
  cols: [
    { key: "n", label: "line", mono: true },
    { key: "w", label: "W", mono: true, align: "right" },
    { key: "d", label: "D", mono: true, align: "right" },
    { key: "p", label: "primitive", align: "center" },
    { key: "c", label: "what it carries" },
  ],
  rows: [
    {
      n: "u_d_al",
      w: "48",
      d: "3",
      p: "<b>flops</b>",
      c: "the aligned addend, cycle 5 → cycle 8 — the widest line in the lane",
      _tone: "good",
    },
    {
      n: "u_d_c0",
      w: "30",
      d: "5",
      p: "<b>flops</b>",
      c: "c0 plus the per-function term, waiting for Horner stage 2",
    },
    {
      n: "u_d_ga / u_d_gb",
      w: "16",
      d: "6",
      p: "<b>flops</b>",
      c: "the two significands, cycle 1 → DSP-M's A/B in cycle 7",
    },
    {
      n: "u_d_gc",
      w: "16",
      d: "4",
      p: "<b>flops</b>",
      c: "sig_c, waiting for the shift amount to be computed",
    },
    {
      n: "u_d_ebA",
      w: "12",
      d: "6",
      p: "<b>flops</b>",
      c: "ebase, cycle 4 → the rounder in cycle 13",
    },
    { n: "u_d_ng8", w: "1", d: "7", p: "SRL", c: "fma_neg → DSP-M's ALUMODE" },
    {
      n: "u_d_nan / inf / zro / ssg",
      w: "1",
      d: "11",
      p: "SRL",
      c: "the specials, decoded in cycle 1 and applied in cycle 14",
    },
    {
      n: "u_d_can / u_d_prd",
      w: "1",
      d: "12",
      p: "SRL",
      c: "exact cancellation, and the compare bit",
    },
  ],
};

// ---------------------------------------------------------------- measured
const measured = {
  cols: [
    { key: "w", label: "" },
    { key: "v", label: "measured", mono: true },
  ],
  rows: [
    {
      w: "one lane, <code>vec_alu</code>",
      v: "<b>1,249 LUT</b> · 705 FF · <b>3 DSP</b> · 0 BRAM · <b>324.8 MHz</b> (WNS +0.147 ns at a 310 MHz target) · latency 14, <b>II = 1</b>",
      _tone: "good",
    },
    {
      w: "E8M15 relative error, ½ ulp",
      v: "<b>1.5e-5</b> — 32× better than FP16, 256× worse than FP32",
    },
    {
      w: "16 standalone lanes — arithmetic, not a build",
      v: "16 × 1,249 = <b>19,984 LUT</b>",
    },
    {
      w: "<code>vec_lanes</code> assembled, after the shrink",
      v: "<b>24,683 LUT</b> · 15,032 FF · 40 BRAM tiles · 48 DSP · <b>358.4 MHz</b> — started at <b>305.1</b>",
      _tone: "good",
    },
    {
      w: "<code>vec_cu</code> assembled, after the shrink",
      v: "<b>35,629 LUT</b> · 22,145 FF · 44 BRAM tiles · 51 DSP · <b>358.4 MHz</b> — started at <b>229.3</b>",
      _tone: "good",
    },
  ],
};

const bounders = {
  cols: [
    { key: "p", label: "the path that bound it" },
    { key: "f", label: "MHz", mono: true, align: "right" },
    { key: "w", label: "what it actually was" },
  ],
  rows: [
    {
      p: "<code>mode</code> → width → slice index → a 16-way write-enable decode → the register file's write port",
      f: "<b>229.3</b>",
      w: "control. <b>It hides standalone</b>, because there <code>mode</code> is an input with an ideal driver",
      _tone: "bad",
    },
    {
      p: "<code>vl</code> → a 128-bit barrel shift and a 128-bit decrement → the predicate reduce",
      f: "304.2",
      w: "control. <code>vl</code> only moves on <code>VSETVL</code>, three states before any consumer — so it is a register now",
      _tone: "bad",
    },
    {
      p: "bound register → three chained 32-bit multiplies → an FSM state",
      f: "<b>129</b>",
      w: "control. The AGU carries no multipliers at all now — each dimension keeps a running partial sum",
      _tone: "bad",
    },
    {
      p: "<code>idx_i · stride_i</code> recomputed every cycle, between a stride register and the address consumer",
      f: "178",
      w: "control, same block, same fix",
      _tone: "bad",
    },
    {
      p: "<code>l1_q</code> straight off the block RAM → a 16-lane FP16 normalise",
      f: "286.9",
      w: "a memory's clock-to-out landing on a datapath",
      _tone: "warn",
    },
    {
      p: "…then <code>ls_kind</code> muxed at the converter input, in series with that same normalise",
      f: "310.4",
      w: "control again — the fix is to select the converter's source <b>a cycle early</b>",
      _tone: "warn",
    },
    {
      p: "<code>s1_b</code> → compare → the <code>va</code> mux → the DSP pre-adder",
      f: "377",
      w: "inside the ALU, and still control reaching a datapath",
      _tone: "warn",
    },
  ],
};

const opcodes = {
  cols: [
    { key: "o", label: "opcode", mono: true },
    { key: "a", label: "va", mono: true },
    { key: "b", label: "vb", mono: true },
    { key: "c", label: "vc", mono: true },
  ],
  rows: [
    { o: "add", a: "a", b: "1.0", c: "c" },
    { o: "sub", a: "a", b: "1.0", c: "−c &nbsp;<i>(a sign flip)</i>" },
    { o: "mul", a: "a", b: "b", c: "0" },
    { o: "neg", a: "−a", b: "1.0", c: "0" },
    { o: "abs", a: "|a|", b: "1.0", c: "0" },
    { o: "fma", a: "a", b: "b", c: "c" },
    { o: "fnma", a: "−a", b: "b", c: "c" },
    { o: "max", a: "cmp_lt ? b : a", b: "1.0", c: "0", _tone: "good" },
    { o: "min", a: "cmp_gt ? b : a", b: "1.0", c: "0", _tone: "good" },
    { o: "sel", a: "|c| ? a : b", b: "1.0", c: "0", _tone: "good" },
    {
      o: "cmplt / cmpgt / cmpeq",
      a: "pred ? 1.0 : 0",
      b: "1.0",
      c: "0",
      _tone: "good",
    },
  ],
};
</script>

<template>
  <DocPage
    title="Vector ALU microarchitecture"
    summary="How one E8M15 lane is built for the operations it performs — which term lands on which DSP port, why the alignment is one shifter and not two, how four transcendental seeds share the FMA's normaliser, and what the network around sixteen of them costs."
    domain="tpu"
    status="measured"
    source="src/kohakutpu/vector/ · xcvu13p-fhgb2104-2L-e, Vivado 2024.2, out-of-context synthesis · docs/projects/kohakutpu/vector-core.md · results.md §3, §3.1, §6.3"
  >
    <p class="doc-p">
      <RouterLink to="/tpu/vector" class="doc-link"
        >The vector core page</RouterLink
      >
      is the <i>what</i>: the format, the mode table, what the core refuses to
      do. This is the <i>how</i> — <code>vec_alu.v</code> as it is written, one
      lane, three DSPs, fourteen cycles deep at II = 1.
    </p>

    <Callout kind="rule" title="Every opcode goes through the FMA">
      <p>
        There is one arithmetic primitive and one comparator. <code>max</code>,
        <code>min</code>, <code>sel</code>, <code>cmp</code>, <code>mov</code>,
        <code>neg</code> and <code>abs</code> pick an operand at cycle 1 and
        send it through as <code>winner × 1.0 + 0</code>, which is
        <b>bit-exact</b>. The four seeds share the normaliser too. So the lane
        holds <b>one</b> leading-one search, <b>one</b> normalising shift and
        <b>one</b> rounder — not one per operation.
      </p>
    </Callout>

    <SpecTable
      :cols="opcodes.cols"
      :rows="opcodes.rows"
      caption="The operand select in cycle 1, verbatim. Negation is a sign flip on an operand, so sub and fnma need no datapath mode; a compare feeds 1.0 or 0 into the multiplier and its own result out on out_pred."
    />

    <h2 class="doc-h2">The lane, cycle by cycle</h2>
    <p class="doc-p">
      Cycle numbers here are load-bearing. <code>vec_dsp</code>'s contract is
      <b>A/B/D at N−3 and C/ALUMODE at N−2</b> for a result in cycle N, and
      every cross-cycle signal in the lane travels in a named
      <code>vec_delay</code> whose <code>D</code> is
      <i>consume cycle − produce cycle</i>. That is what keeps the stage
      arithmetic checkable by inspection rather than by simulation.
    </p>

    <Fig
      caption="One lane, cycles running left to right. Three tracks — the polynomial path on top, the exponent path in the middle ending at the shifter, the significand path below — converge on DSP-M, and everything after cycle 10 is shared between the FMA and all four seeds. The significands do not go straight there: u_d_ga/gb carry them six cycles to DSP-M's A/B and u_d_gc carries sig_c four cycles to the shifter. Measured 1,249 LUT / 705 FF / 3 DSP at 324.8 MHz out-of-context on xcvu13p-fhgb2104-2L-e."
      zoom
      wide
    >
      <BlockDiagram
        :nodes="lane.nodes"
        :edges="lane.edges"
        :groups="lane.groups"
      />
    </Fig>

    <WaveTrace
      label="the DSP latency contract — the rule the whole pipeline is built on"
      :rows="dspContract.rows"
      :notes="dspContract.notes"
    />

    <h2 class="doc-h2">DSP-E: two different linear combinations, one pass</h2>
    <p class="doc-p">
      The FMA needs two values out of three exponents, and they are different
      sums of the same terms: <code>e_ab = e_a + e_b − 127</code> is the
      product's exponent, and <code>cs = e_ab − e_c</code> is how far the addend
      must travel. A <b>two-tap <code>B</code> constant</b> —
      <code>2^12 + 1</code> — makes the multiplier emit two copies of the
      pre-adder sum at two positions, and <code>C</code> then biases each copy
      independently.
    </p>

    <Fig
      caption="A pre-adder forms e_a + e_b; a constant with two set bits replicates it at bit 0 and bit 12; one C constant biases each copy. One DSP, one pass, both answers."
      zoom
      wide
    >
      <BlockDiagram :nodes="dspe.nodes" :edges="dspe.edges" />
    </Fig>

    <BitField
      :fields="dspeP"
      caption="The two fields cannot collide, and that is an argument rather than an observation: the high copy is always positive so its sign extension cannot eat the low field, and the low field spans [133, 892] against a 4096-wide field, so there is no carry into bit 12 and no borrow out of bit 0 for any legal input"
    />

    <h2 class="doc-h2">
      The alignment shifter — the hinge of the whole format
    </h2>
    <p class="doc-p">
      <code>s = 17 + cs</code>, so <code>s = P[11:0] − 495</code> comes straight
      off DSP-E with no second adder. Biasing the shift by the
      <b>17 bits of headroom</b> is what turns
      <i>“shift left or right depending on the sign”</i> into one barrel shifter
      — roughly 90 LUT and a logic level per ALU — and it works only because 16
      significand bits plus a 32-bit product is <b>exactly</b> the 48 bits the
      <code>C</code> port has.
    </p>

    <Fig
      caption="One direction, one bypass, one 6-bit shift amount. s < 0 IS the addend-dominates case: the correctly rounded result is the addend, so byp forces s to zero with a zeroed product and the ordinary path emits it. The 48-bit form ANDs 32 constant zeros and synthesis folds them, so the sticky is worth only 34 LUT — kept because it says what it means."
      zoom
      wide
    >
      <BlockDiagram :nodes="align.nodes" :edges="align.edges" />
    </Fig>

    <p class="doc-p">
      The sign never needs a magnitude comparison:
      <code>res_neg = neg &amp;&amp; (s != 0) &amp;&amp; P[47]</code>. Wherever
      <code>P[47]</code> <i>is</i> a sign bit the magnitude is below
      <code>2^32</code>, so recovering it is a <b>33-bit</b> two's complement
      rather than a 48-bit one.
    </p>

    <Callout
      kind="trap"
      title="At 24 significand bits the window is gone, and with it everything built on it"
    >
      <p>
        Read the outcome column, not the overflow count. E8M15's alignment
        shifter is one shifter because there are exactly 17 bits of room above a
        32-bit product. At
        <b
          >24 significand bits the product is 48 and fills the C port on its
          own</b
        >
        — the headroom “overflows by 25”, which is not a near miss, it is the
        entire window.
      </p>
      <p>
        So the unidirectional shifter, the single bypass, the 33-bit sign
        recovery and the plain 48-bit sticky are
        <b>all consequences of one arithmetic coincidence</b>, and none of them
        survives at FP32 width. The source keeps the three DSPs for the extended
        mode and is explicit that what it costs is
        <i>“control complexity and a wider normaliser”</i>, and that
        <b>“whether to build it is open”</b>.
      </p>
    </Callout>

    <SpecTable
      :cols="headroom.cols"
      :rows="headroom.rows"
      caption="There is a second, independent wall at the same place: the B port is 18-bit SIGNED, so it holds 17 significand bits, not 18 — an 18-bit unsigned significand has its MSB set and reads as negative, and the correction is + sig_a·2^17 on the only free ALU input, which the addend already owns"
    />

    <h2 class="doc-h2">The E8M15 multiply, and the 17-bit split above it</h2>
    <p class="doc-p">
      Native mode is one multiply: 16 × 16 on DSP-M, with the aligned addend on
      <code>C</code> as a <b>raw 48-bit pattern</b>. It can set bit 47 and read
      as negative — that is fine, because the ALU is modular, the sum is bounded
      below <code>2^48</code>, and the sign is recovered from
      <code>s</code> rather than from the bit. Full FP32 needs two partial
      products, and with three DSPs already allocated both come from the same
      silicon.
    </p>

    <Fig
      caption="The standard 17-bit split, cascade-adjacent. DSP-P's partial reaches DSP-M through PCIN rather than the fabric, and the W mux carrying C is what makes the addend free rather than a third DSP — three simultaneous ALU operands is exactly what that mux exists for. DSP48E2 only; a DSP48E1 cannot do it. DESIGNED AND NOT BUILT: this is the source's port allocation, not a measurement."
      zoom
      wide
    >
      <BlockDiagram :nodes="split.nodes" :edges="split.edges" />
    </Fig>

    <Callout kind="open" title="Nothing has demanded 6.0e-8">
      <p>
        The sequential form is 1 DSP and half rate; the parallel form above is 2
        DSPs at full rate. Both reach 6.0e-8 against the native lane's 1.5e-5.
        It is not built, and the reason is not cost — it is that no kernel has
        asked.
      </p>
    </Callout>

    <h2 class="doc-h2">Four seeds, two Horner stages, one pass</h2>
    <p class="doc-p">
      DSP-P runs stage 1 and DSP-M runs stage 2, so <code>exp2</code>,
      <code>log2</code>, <code>inv</code> and <code>rsqrt</code> retire at
      <b>II = 1</b> — the same throughput as an add, where a GPU SFU runs these
      at a quarter rate. The coefficients are fixed-point with stated weights:
      <code>u</code> at <code>2^-12</code>, <code>c2</code> at
      <code>2^-28</code>, <code>c1</code> at <code>2^-24</code>,
      <code>c0</code> at <code>2^-20</code>.
    </p>

    <Fig
      caption="h = (c2·u + (c1&lt;&lt;16) + 2^15) &gt;&gt; 16, then F = (h·u + (c0&lt;&lt;16) + 2^15) &gt;&gt; 16. The rounding constant is a CONCATENATION, not an add: the low sixteen bits of the shifted coefficient are zero, so 2^15 is simply bit 15. Written as + 48'sd32768 it is a 48-bit carry chain in front of a DSP input."
      zoom
      wide
    >
      <BlockDiagram :nodes="seeds.nodes" :edges="seeds.edges" />
    </Fig>

    <Callout
      kind="rule"
      title="Moving the ROMs to block RAM cost no cycle, because the address was already registered"
    >
      <p>
        The tables are <b>synchronous</b> and <code>rom_style</code> names the
        primitive rather than leaving it to a heuristic. The one cycle of
        latency the ROM introduces is paid for by
        <b>deleting the lane's own <code>u_d_ix</code> stage</b> — the address
        register that stage was, the ROM now is. So this is a register
        <i>move</i>, <code>c0/c1/c2</code> still land in cycle 3, and it saved
        <b>3,575 LUT per core</b> with the measured error unchanged to three
        decimals.
      </p>
      <p>
        32 segments is also the fabric sweet spot: a 32-entry constant table is
        a LUT5 — half a LUT6 — per output bit, and <code>rsqrt</code> needs both
        octave parities, so its 64 entries are exactly one LUT6 per bit.
        <code>idx[5]</code> <i>is</i> the parity, which is why one table covers
        [1,2) and [2,4) with no second shifter.
      </p>
    </Callout>

    <h3 class="doc-h3">
      exp2's range reduction: one shift, and a negate that does two jobs
    </h3>
    <p class="doc-p">
      <code>R = round(sig_a · 2^(e_a−125))</code> as <code>s8.17</code> fixed
      point, formed by a single right shift because the bias absorbs the
      direction. Then the round and the negate run
      <b>in parallel, not in series</b>: <code>−(R+g) = ~R + ~g</code> for a
      one-bit <code>g</code>. And negating the whole <code>s8.17</code> word
      <i>is</i> the floor/frac split for a negative argument —
      <code>2^x = 2^floor(x) · 2^frac(x)</code> needs <code>frac</code> in
      [0,1), and the two's complement produces exactly that pair with no
      separate compare.
    </p>
    <p class="doc-p">
      Seventeen fraction bits and not fifteen, because rounding the exponent
      argument to
      <code>2^-16</code> would cost <code>ln2 · 2^-16 = 0.35 ulp</code> — four
      times the table's own error. The whole reduction is split across two
      cycles with the seam after the shift; whole, it was the critical path of
      the entire ALU.
    </p>

    <h3 class="doc-h3">One normaliser closes for all five</h3>
    <p class="doc-p">
      Each seed produces a fixed-point magnitude and a signed exponent base, and
      <code>e_out = lead1(mag) + ebase</code> closes for every one of them as
      well as for the FMA. That is the reason the back end is shared rather than
      duplicated.
    </p>

    <SpecTable
      :cols="normal.cols"
      :rows="normal.rows"
      caption="The identity cases — exp2(k), log2(2^k), inv(2^k), rsqrt(2^even) — all sit at the origin of segment 0. Constraining the polynomial through that point costs 1.5 bits on the whole function, so the fit is UNCONSTRAINED and the exact value is substituted by a mux on c0 instead"
    />

    <Callout
      kind="measured"
      title="The tables are no longer what limits these functions"
    >
      <p>
        Measured against the <i>actual</i> fixed-point circuit — both Horner
        roundings and all three quantised coefficients — every seed lands about
        <b>1.5 bits worse than its minimax prediction</b>. The approximation
        stopped being the limit; the coefficient and Horner quantisation is.
        That is why the count stops at 32, and it is the opposite of the
        intuition that more segments means more accuracy.
      </p>
    </Callout>

    <SpecTable :cols="tableAcc.cols" :rows="tableAcc.rows" />

    <Callout
      kind="open"
      title="An FP32 build needs 256 segments for exactly the two seeds that cannot be refined"
    >
      <p>
        Newton refinement stays in software, and that is the right place: it
        makes accuracy a <i>program</i> choice rather than a synthesis choice,
        and a 15-bit seed plus one step reaches 30. But <code>inv</code> and
        <code>rsqrt</code> have a refinement form and
        <b><code>exp2</code> and <code>log2</code> do not</b>. So a 24-bit build
        cannot buy its way out of the table for those two: reaching 24 bits at
        degree 2 needs <b>256 segments × 3 coefficients</b> rather than 32.
      </p>
      <p>
        The source records this as an
        <b>open design question, not a costed one</b>. No area figure for a
        256-segment table is quoted anywhere on this site, and none should be
        invented.
      </p>
    </Callout>

    <h2 class="doc-h2">The conversion path</h2>
    <p class="doc-p">
      E8's range covers FP32's exactly, so
      <b>nothing on the way in range-checks</b> — there is no overflow,
      underflow or saturation logic on the load edge at all. Only one of the
      four directions is both lossy and range-limited.
    </p>

    <Fig
      caption="vec_cvt_acc is the peer path: a cluster's accumulator is S1 E7 M14 with BIAS 63, E7 spans real exponents −63..64 and E8 spans −126..127, so e8 = e7 + 64 ALWAYS lands inside. No saturation logic, no infinity generation, no range check — and at ACC_MW = 14 there is no adder in the mantissa path at all."
      zoom
      wide
    >
      <BlockDiagram :nodes="cvt.nodes" :edges="cvt.edges" />
    </Fig>

    <Callout
      kind="rule"
      title="A finite overflow SATURATES, and it does so to match the other converter"
    >
      <p>
        <code>vec_cvt_e8_to_f16</code> clamps a finite overflow at the largest
        finite FP16, matching <code>mx_fpacc_to_fp16</code>. A genuine infinity
        passes through as an infinity.
        <b
          >Two different answers for one overflow is worse than the loss
          itself</b
        >, and that place is where a GEMM's range dies.
      </p>
      <p>
        The other four dtype codes raise <code>bad</code> rather than
        converting. E8M15-raw and ACC24 are internal formats that would put
        24-bit words in RAM, and INT8/INT16 have no settled semantics —
        <code>bad</code> is a <b>decode fault, not a silent zero</b>.
      </p>
    </Callout>

    <Callout
      kind="trap"
      title="VCVT's round trip is two conversions, and back to back they are the longest path in the core"
    >
      <p>
        <code>VCVT.FP16</code> reuses the store converters feeding the load
        converters, so a round trip costs a mux rather than a third set — and
        <code>VCVT.FP32</code> is the identity, because E8M15 → FP32 → E8M15 is
        exact. Taking <code>l1_q</code> straight off the block RAM put its
        clock-to-out in series with a 16-lane FP16 normalise: <b>286.9 MHz</b>.
        Muxing the two sources at the converter input then put
        <code>ls_kind</code> in series with that same normalise:
        <b>310.4 MHz</b>.
      </p>
      <p>
        The fix is neither: the converter input is
        <b>one register, selected a cycle early</b>. <code>ls_kind</code> is set
        at decode and stable for the whole access, so choosing early chooses the
        same thing.
      </p>
    </Callout>

    <h2 class="doc-h2">Chaining is what makes the core compute-bound at all</h2>
    <p class="doc-p">
      <i
        >“Halving the pass count is worth exactly as much as doubling the ALU
        count and costs far less.”</i
      >
      The hardware reason is narrower than the throughput argument and it lives
      in two rules: <b>only stage 0 may read a vector register</b>, and an ALU
      that is never a chain head in a mode
      <b>has no register-file path in that mode</b> — eight of the sixteen
      become wires.
    </p>

    <StepPlayer
      :steps="chaining"
      label="sigmoid = inv(1 + exp2(−x·log2e)) — four FLAT passes, then one D4 chain"
    >
      <template #default="{ state }">
        <LaneGrid
          :lanes="16"
          :mask="state.mask"
          :rows="[
            { name: 'op', values: state.ops },
            { name: 'group', values: state.group },
            { name: 'stage', values: state.stage },
            { name: 'a source', values: state.src },
          ]"
        />
        <div class="flex flex-wrap gap-2 mt-3">
          <span class="chip">{{
            state.kind === "d4" ? "VMODE = D4 · 4 × 4" : "VMODE = FLAT · 16 × 1"
          }}</span>
          <span class="chip">register file: {{ state.rfw }}</span>
          <span class="chip">stall: {{ state.stall }}</span>
        </div>
      </template>
    </StepPlayer>

    <p class="doc-p">
      The masked lanes in the last step are the ones that
      <b>may read a vector register</b>, not the ones that compute — every ALU
      computes in every mode. A stage-<i>t</i>&gt;0 <code>SRC_V</code> would
      read a wrong <i>lane</i> here, so <code>vec_core</code> raises
      <code>F_VSRC</code> rather than letting the datapath answer it.
    </p>

    <SpecTable
      :cols="chainCost.cols"
      :rows="chainCost.rows"
      caption="DERIVED from the sequencer's own rules — nbeat = nchunk × dep, and alu_issue holds beat 0 while |pend[source] — not measured. The beat count is identical; what chaining deletes is the register-file traffic and the three scoreboard stalls of a 14-deep lane with no bypass"
    />

    <Callout kind="rule" title="A later stage runs 14·t cycles behind">
      <p>
        That is the whole reason for the rule. A vector operand read at stage
        <i>t</i> would need a 24-bit delay line per stage to arrive with the
        data it belongs to. Every chained kernel sources <code>C</code>,
        <code>S</code> or <code>K</code> past stage 0,
        <b>so the rule costs nothing</b> — and the datapath now <i>relies</i> on
        it rather than merely permitting it.
      </p>
    </Callout>

    <h2 class="doc-h2">The operand network and the write crossbar</h2>
    <p class="doc-p">
      A mode is a factorisation <code>W × D = 16</code>, so which slice feeds
      which ALU changes with the mode — and so does which ALU's output lands on
      which slice. These are the structures a SIMT core does not have, because
      there a lane reads its own file with its own index and writes back to
      itself. Here they were measured, and they were expensive.
    </p>

    <Fig
      caption="ALU s is group s/D stage s%D, and D is 1, 2 or 4 — so BOTH indices are a per-mode constant. Deriving them at runtime instead cost 3,404 LUT. Likewise slice d's producer is a function of the MODE ALONE, so the write crossbar is a 3:1 dressed as a 16:1; carrying a runtime ALU index there cost 1,089 LUT."
      zoom
      wide
    >
      <BlockDiagram
        :nodes="network.nodes"
        :edges="network.edges"
        :groups="network.groups"
      />
    </Fig>

    <Callout kind="rule" title="Select the window ONCE, not per ALU">
      <p>
        A beat reads the contiguous slices <code>[ph·W, ph·W+W−1]</code> and
        gives entry <i>j</i> to every ALU of group <i>j</i>. So the phase select
        belongs to the <b>window</b>, not to each ALU: entry <i>j</i>&lt;4
        reaches only <code>{j, j+4, j+8, j+12}</code>, and <i>j</i> in 4..7
        reaches <code>{j, j+8}</code>. Eight of the sixteen entries need no mux
        at all.
      </p>
    </Callout>

    <ResourceBars
      :items="shrink"
      unit="LUT saved"
      caption="The shrink, item by item. vec_lanes fell 34.9% and vec_cu 26.4% with Fmax UP; xcvu13p-fhgb2104-2L-e, out-of-context, results.md §3.1. Not shown, because it is a cost rather than a saving: the fused exp-and-sum leaf write-back is +249 LUT in vec_lanes and +42 in vec_cu"
    />

    <h2 class="doc-h2">vec_delay: the cheapest LUT in the lane to give back</h2>
    <p class="doc-p">
      A 14-stage pipeline at II = 1 has to carry about
      <b>twenty control signals</b> from where they are produced to where they
      are consumed. That is why the lane's LUT estimate was <b>40% low</b> — the
      datapath was roughly what was predicted; the delay lines were not.
    </p>

    <Callout kind="rule" title="An SRL16E is one LUT per bit at ANY depth">
      <p>
        So a 3-deep line pays a whole LUT to use 3 of its 16 stages. The device
        is
        <b>LUT-bound at 66% with FF at 34%</b>, so a shallow line belongs in the
        half of the CLB that is idle — <code>MAX_FF = 6</code>, flops at or
        below it, SRL past it, where the flops outrun the LUT saved.
      </p>
      <p>
        It is <b>not a memory</b>: no address, no read port. The rule about
        naming BRAM and URAM primitives exists because an inferred memory's read
        latency depends on a tool heuristic; a shift register's does not.
      </p>
    </Callout>

    <SpecTable
      :cols="delays.cols"
      :rows="delays.rows"
      caption="A representative selection. Naming each line keeps the stage arithmetic checkable by inspection — the instance says “produced at cycle 1, consumed at cycle 6” and D is the difference. Whether latency 14 can come down is open, and these lines are the first place to look if 128 lanes do not fit"
    />

    <Callout
      kind="trap"
      title="…and one of them had to be told not to swallow a register"
    >
      <p>
        <code>vec_cu</code>'s limiter was <code>s1_b_reg</code> → the specials
        SRL, <b>nine logic levels</b> — the specials case block. Registering it
        is free, because the delay lines below give back the stage it costs. But
        the register only stays put with <code>shreg_extract = "no"</code> on
        it: without that, the SRL absorbs it back and the nine levels return.
      </p>
    </Callout>

    <h2 class="doc-h2">What actually bound the assembly</h2>
    <p class="doc-p">
      One lane at 324.8 MHz says nothing about the assembled core.
      <code>vec_lanes</code> and <code>vec_cu</code> started at <b>305.1</b> and
      <b>229.3 MHz</b> — and every path that bound them was
      <b>control reaching a datapath</b>, never the arithmetic.
    </p>

    <SpecTable
      :cols="bounders.cols"
      :rows="bounders.rows"
      caption="Each row is a path that was measured and then removed. After the shrink the worst path is no longer in the core at all — it is inside the ALU, so the core now sits at the ALU floor"
    />

    <Callout
      kind="trap"
      title="The worst one hides when you synthesise the block alone"
    >
      <p>
        <code>mode</code> → width → slice index → a 16-way enable decode → the
        register file's write port is the longest path in the assembled machine.
        <b>Measured standalone it does not appear</b>, because there
        <code>mode</code> is a port with an ideal driver rather than a register
        in <code>vec_core</code>.
      </p>
      <p>
        The fix is the same shape in three places on this page: take the decode
        <b>one stage early and register it</b>. <code>alu_out</code> is only
        valid at the retire cycle, but what <i>selects</i> among its slices need
        not be.
      </p>
    </Callout>

    <h2 class="doc-h2">Measured</h2>

    <SpecTable :cols="measured.cols" :rows="measured.rows" />

    <Callout
      kind="note"
      title="The two rows are not the same build vintage, and the difference is not all control"
    >
      <p>
        <code>vec_lanes</code> at 24,683 LUT against 16 standalone lanes at
        19,984 is the array's own cost — the operand network, the write
        crossbar, the metadata line and the sixteen rotating accumulators. But
        the lane row carries <b>0 BRAM</b>, and the coefficient ROMs are block
        RAM today, so that row predates the ROM move and part of the gap is
        vintage rather than structure. The assembled figures carry 40 and 44
        BRAM tiles precisely because that move happened.
      </p>
      <p>
        For costing a new instruction the number to use is one assembled core at
        roughly
        <b>33,000 LUT</b>: something costing ~3,000 LUT lands in
        <i>every</i> core, so at six cores it is ~18,000 — half a core's worth
        of area for a capability every core gains.
      </p>
    </Callout>

    <Callout
      kind="measured"
      title="The rounding property, and the one corner that is not correctly rounded"
    >
      <p>
        Bits shifted out of the 48-bit alignment window are carried as a plain
        sticky. For an effective <b>addition</b> that is round-to-nearest-even —
        correct. For an effective <b>subtraction</b> the discarded residue is a
        borrow, and a plain sticky rounds the wrong way adjacent to the round
        boundary: the result reads
        <b>exactly one ulp high — always high, never low</b>.
      </p>
      <p>
        Reaching it takes three things at once: <code>s ≥ 33</code>, an
        effective subtraction, and a boundary-adjacent bit pattern. A stream
        built to oversample exponent-distant addends measures
        <b>19 in 4,000</b>; a banded random suite measures <b>0 in 6,000</b>.
        Correcting it means complementing the residue on subtraction — small,
        and natural at the next respin. Until then that paragraph is the
        contract, and the same construction is in <code>mx_fpacc</code>'s split
        path.
      </p>
    </Callout>
  </DocPage>
</template>
