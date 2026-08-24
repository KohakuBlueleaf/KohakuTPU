<script setup>
/* The vector core. Every measured figure is out-of-context synthesis on
 * xcvu13p-fhgb2104-2L-e; the 128-lane row is an extrapolation and says so. */

const e8m15 = [
  { name: "S", bits: 1, value: "sign" },
  { name: "E", bits: 8, value: "bias 127", accent: true },
  { name: "M", bits: 15, value: "implicit leading 1" },
]

const conversions = {
  cols: [
    { key: "c", label: "conversion", mono: true },
    { key: "e", label: "exponent" },
    { key: "s", label: "significand" },
    { key: "r", label: "result" },
  ],
  rows: [
    { c: "FP16 → E8M15", e: "E5 ⊂ E8, rebias by +112", s: "10 → 15, zero-extend", r: "<b>exact</b>", _tone: "good" },
    { c: "FP32 → E8M15", e: "E8 = E8, <b>no change at all</b>", s: "23 → 15, round", r: "1.5e-5" },
    { c: "E8M15 → FP16", e: "rebias, range-check", s: "15 → 10, round", r: "4.9e-4" },
    { c: "E8M15 → FP32", e: "<b>no change at all</b>", s: "15 → 23, zero-extend", r: "<b>exact</b>", _tone: "good" },
  ],
}

const cPort = [
  { name: "addend headroom — sig_c, aligned", bits: 16, value: "", accent: true },
  { name: "product  sig_a · sig_b", bits: 32, value: "" },
]

const significands = {
  cols: [
    { key: "s", label: "significand", mono: true, align: "right" },
    { key: "p", label: "product", mono: true, align: "right" },
    { key: "h", label: "headroom needed", mono: true, align: "right" },
    { key: "c", label: "C port", mono: true, align: "right" },
    { key: "o", label: "outcome" },
  ],
  rows: [
    { s: "11 (FP16)", p: "22", h: "12", c: "48", o: "14 bits wasted" },
    { s: "<b>16 (E8M15)</b>", p: "<b>32</b>", h: "<b>17</b>", c: "<b>48</b>", o: "<b>exact</b>", _tone: "good" },
    { s: "17 (E8M16)", p: "34", h: "18", c: "48", o: "overflows by 4", _tone: "warn" },
    { s: "18 (E8M17)", p: "36", h: "19", c: "48", o: "overflows by 7", _tone: "bad" },
    { s: "24 (FP32)", p: "48", h: "25", c: "48", o: "overflows by 25", _tone: "bad" },
  ],
}

const precision = {
  cols: [
    { key: "f", label: "format" },
    { key: "e", label: "relative error, ½ ulp", mono: true, align: "right" },
  ],
  rows: [
    { f: "FP16", e: "4.9e-4" },
    { f: "<b>E8M15</b>", e: "<b>1.5e-5</b>", _tone: "good" },
    { f: "FP32", e: "6.0e-8" },
  ],
}

/* The core, left to right. */
const core = {
  nodes: [
    { id: "mesh", x: 0, y: 4, w: 13, h: 4, label: "mesh port", sub: "the same six signals a cluster uses", accent: true },
    { id: "l1", x: 17, y: 0, w: 14, label: "L1 scratchpad", sub: "256-bit word · block RAM · no tags" },
    { id: "im", x: 17, y: 9, w: 14, label: "instruction memory", sub: "32-bit words · distributed LUTRAM" },
    { id: "rf", x: 35, y: 0, w: 14, label: "register file", sub: "3 mirrors · 3R1W per lane · striped" },
    { id: "agu", x: 35, y: 9, w: 14, label: "address generator", sub: "base + 4 (stride, bound) pairs" },
    { id: "alu", x: 53, y: 3, w: 15, h: 6, label: "16 ALU lanes", sub: "DSP-E · DSP-M · DSP-P", accent: true },
  ],
  edges: [
    { from: "mesh:r", to: "l1:l", dir: "h", label: "fill / drain" },
    { from: "mesh:r", to: "im:l", dir: "h", label: "load and run" },
    { from: "l1:r", to: "rf:l", dir: "h", accent: true, label: "convert on read" },
    { from: "im:r", to: "agu:l", dir: "h" },
    { from: "rf:r", to: "alu:l", dir: "h", accent: true },
    { from: "agu:r", to: "alu:l", dir: "h" },
  ],
}

const dsps = {
  cols: [
    { key: "d", label: "", mono: true },
    { key: "f", label: "FMA mode" },
    { key: "t", label: "transcendental mode" },
    { key: "e", label: "extended (FP32) mode" },
  ],
  rows: [
    { d: "<b>DSP-E</b>", f: "exponent sum + alignment shift, one pass", t: "range reduction, segment index", e: "exponent" },
    { d: "<b>DSP-M</b>", f: "<code>sig_a*sig_b + aligned_c</code>", t: "Horner stage 2", e: "low partial product + addend" },
    { d: "<b>DSP-P</b>", f: "idle", t: "Horner stage 1", e: "high partial product" },
  ],
}

const align = {
  cols: [
    { key: "s", label: "s = 17 + cs", mono: true },
    { key: "w", label: "where the addend lands", mono: true },
    { key: "m", label: "meaning" },
  ],
  rows: [
    { s: "0", w: "[47:32]", m: "product entirely below the addend's LSB" },
    { s: "17", w: "[30:15]", m: "exponents equal (cs = 0)" },
    { s: "48", w: "gone", m: "product dominates, sticky only" },
  ],
}

const segments = {
  cols: [
    { key: "t", label: "target bits" },
    { key: "l", label: "pure LUT", mono: true, align: "right" },
    { key: "d1", label: "linear (d=1)", mono: true, align: "right" },
    { key: "d2", label: "quadratic (d=2)", mono: true, align: "right" },
  ],
  rows: [
    { t: "11 (FP16)", l: "2,048", d1: "64 x 2", d2: "16 x 3" },
    { t: "<b>16 (E8M15)</b>", l: "<b>65,536</b>", d1: "256 x 2", d2: "<b>32 x 3</b>", _tone: "good" },
    { t: "24 (FP32)", l: "16.8 M", d1: "4,096 x 2", d2: "256 x 3" },
  ],
}

const assembly = {
  cols: [
    { key: "f", label: "seed", mono: true },
    { key: "p", label: "polynomial result" },
    { key: "a", label: "assembly", mono: true },
    { key: "n", label: "normaliser" },
  ],
  rows: [
    { f: "exp2(x)", p: "F = poly(idx, u) in [0,1)", a: "{ 0, k+127, F[19:5] }", n: "<b>none</b> — and no leading-zero count either", _tone: "good" },
    { f: "log2(x)", p: "val = (E - 127) + F", a: "fix2float(val)", n: "the E term is an integer ADD" },
    { f: "inv(x)", p: "F = poly(idx, u) in (0.5,1]", a: "{ S, 253 - E, F[19:4] }", n: "1 bit, folded into the slice" },
    { f: "rsqrt(x)", p: "K = (E-127) &gt;&gt;&gt; 1", a: "{ 0, 126 - K, F[19:4] }", n: "parity of the exponent picks the octave table" },
  ],
}

const costs = {
  cols: [
    { key: "o", label: "op", mono: true },
    { key: "p", label: "passes", align: "right" },
    { key: "n", label: "note" },
  ],
  rows: [
    { o: "fma add mul max min select abs neg", p: "1", n: "" },
    { o: "<b>exp2 log2 inv rsqrt</b>", p: "<b>1</b>", n: "full rate, II = 1 — a GPU SFU runs these at a quarter rate", _tone: "good" },
    { o: "sqrt = x*rsqrt(x), div = a*inv(b)", p: "2", n: "" },
    { o: "exp = exp2(x*log2e), ln = log2(x)*ln2", p: "2", n: "1 if the constant folds" },
    { o: "<b>sigmoid = inv(1 + exp2(-x*log2e))</b>", p: "<b>4</b>", n: "exactly one depth-4 chain" },
    { o: "tanh = 2*sigmoid(2x) - 1", p: "5", n: "" },
    { o: "silu / swish = x*sigmoid(x)", p: "5", n: "" },
    { o: "gelu (tanh form)", p: "~9", n: "two chain traversals" },
    { o: "softmax", p: "3 + 2 + 1", n: "3 elementwise, 2 tree, 1 scalar" },
    { o: "layernorm / rmsnorm", p: "1 + 2 + 1", n: "γ·r and β−μγr fold into one FMA" },
  ],
}

const bound = {
  cols: [
    { key: "m", label: "mode", mono: true },
    { key: "o", label: "ops/result", align: "right" },
    { key: "c", label: "ops/cycle at the bandwidth ceiling", align: "right", mono: true },
    { key: "b", label: "bound by" },
  ],
  rows: [
    { m: "<code>FLAT</code>", o: "1", c: "10.7", b: "<b>memory</b>", _tone: "warn" },
    { m: "<code>D2</code>", o: "2", c: "21.4", b: "<b>compute</b>", _tone: "good" },
    { m: "<code>D4</code>", o: "4", c: "42.7", b: "<b>compute</b>", _tone: "good" },
  ],
}

const modes = {
  cols: [
    { key: "m", label: "mode", mono: true },
    { key: "s", label: "shape", mono: true },
    { key: "r", label: "results/cycle", align: "right", mono: true },
    { key: "f", label: "for" },
  ],
  rows: [
    { m: "<code>FLAT</code>", s: "16 x 1", r: "16", f: "elementwise at max rate" },
    { m: "<code>D2</code>", s: "8 x 2", r: "8", f: "mul-add-mul, scale-and-bias" },
    { m: "<code>D4</code>", s: "4 x 4", r: "4", f: "a whole <code>sigmoid</code> in one pass" },
    { m: "<code>TREE</code>", s: "8+4+2+1 + acc", r: "16 in → 1 out", f: "<code>sum max dot sumsq</code>" },
  ],
}

const treeRows = [
  {
    name: "role",
    values: [
      "leaf", "leaf", "leaf", "leaf", "leaf", "leaf", "leaf", "leaf",
      "comb", "comb", "comb", "comb", "comb", "comb", "comb", "acc",
    ],
  },
  {
    name: "level",
    values: ["0", "0", "0", "0", "0", "0", "0", "0", "1", "1", "1", "1", "2", "2", "3", "—"],
  },
]

/* The reduction recurrence: naive, then rotating partials. */
const recurBroken = {
  rows: [
    { name: "issue", kind: "bus", values: ["x0", null, null, null, null, null] },
    { name: "ALU stage of x0", kind: "bus", values: ["0", "1", "2", "3", "4", "5"] },
    { name: "acc ready", kind: "bit", values: [0, 0, 0, 0, 0, 0] },
    { name: "", kind: "text", values: ["", "stall", "stall", "stall", "stall", "stall"] },
  ],
  notes: [
    {
      cycle: 0,
      text: "The ALU is 14 cycles deep, so acc = acc + x has a 14-cycle loop-carried dependency and a naive accumulator runs at II=14. That is a 14x throughput cliff on every reduction, and it does not show up until the design is built.",
      tone: "bad",
    },
  ],
}

const recurFixed = {
  rows: [
    { name: "issue", kind: "bus", values: ["x0", "x1", "x2", "x3", "x4", "x5"] },
    { name: "target partial", kind: "bus", values: ["p0", "p1", "p2", "p3", "p4", "p5"], mark: [0, 1, 2, 3, 4, 5] },
    { name: "acc ready", kind: "bit", values: [1, 1, 1, 1, 1, 1] },
    { name: "", kind: "text", values: ["II = 1", "", "", "", "", ""] },
  ],
  notes: [
    {
      cycle: 0,
      text: "16 rotating partial accumulators, one per pipeline slot, then one final tree pass to combine them. Cost is 16 registers per core — nothing.",
      tone: "good",
    },
    {
      cycle: 5,
      text: "It also improves accuracy: 16 partial sums of length V/16 accumulate rounding like sqrt(V/16) rather than sqrt(V).",
      tone: "good",
    },
  ],
}

const measured = {
  cols: [
    { key: "w", label: "what" },
    { key: "v", label: "measured", mono: true },
  ],
  rows: [
    { w: "one lane, <code>vec_alu</code>", v: "<b>324.8 MHz</b> (WNS +0.147 ns at a 310 MHz target) · 1,249 LUT · 705 FF · 3 DSP · 0 BRAM · latency 14, II = 1", _tone: "good" },
    { w: "<code>vec_lanes</code>, after the shrink", v: "24,683 LUT · 15,032 FF · 40 BRAM tiles · 48 DSP · <b>358.4 MHz</b>", _tone: "good" },
    { w: "<code>vec_cu</code>, after the shrink", v: "35,629 LUT · 22,145 FF · 44 BRAM tiles · 51 DSP · <b>358.4 MHz</b>", _tone: "good" },
    { w: "one assembled core, for costing a new instruction", v: "roughly <b>33,000 LUT</b> — something costing ~3,000 LUT lands in every core, so at six cores it is ~18,000" },
    { w: "128 lanes — <b>PROJECTED</b>, extrapolated, never built", v: "~160k LUT and 384 DSP — about <b>37% of an SLR's LUTs against 12.5% of its DSPs</b>", _tone: "warn" },
  ],
}

const notThis = [
  "integer, bitwise and logical scalar arithmetic",
  "data-dependent control such as loop bounds, branching and early exit",
  "gather and scatter with computed indices",
  "sort, top-k, argsort and sampling",
  "shape and metadata arithmetic, allocation and scheduling",
  "pointer chasing",
]

const open = [
  "<b>E8M16 if the alignment ever leaves the DSP.</b> M15 is right only while the addend rides the C port.",
  "<b>Whether latency 14 can come down.</b> The delay lines are the largest single block of the lane and exist only because the pipeline is 14 deep. II=1 is what matters for throughput, but this is the first place to look if 128 lanes do not fit.",
  "<b>Whether the extended mode is worth building.</b> Nothing has demanded 6.0e-8.",
  "<b>Chain depth 4.</b> <code>sigmoid</code> fits exactly; the right way to settle it is to write the kernels that matter as op chains and look at the length histogram.",
  "<b>ALUs per core against cores.</b> The throughput table is known; the vector-length distribution of real kernels is not.",
  "<b>Accumulation width for long reductions.</b> Multiply width and accumulate width are independent, and a sum over thousands of terms compounds rounding far more than any single chain.",
]
</script>

<template>
  <DocPage
    title="The vector core"
    summary="A programmable elementwise and reduction engine: E8M15 chosen so an FMA fits one DSP exactly, four base-2 transcendental seeds at full rate, sixteen lanes because that is where two mesh ports meet a depth-2 chain — and a deliberate refusal to do anything data-dependent."
    domain="tpu"
    status="building"
    source="xcvu13p-fhgb2104-2L-e · docs/projects/kohakutpu/vector-core.md · results.md §3, §6.3"
  >
    <p class="doc-p">
      KohakuTPU's second compute unit. Where the cluster runs one macro-op, the vector core runs
      <i>kernels</i> — it is the part of the machine that is programmed rather than
      parameterised, and the only one whose instruction set can branch. Software sees
      <b>FP32 or FP16 in memory</b> and nothing else; everything below is internal.
    </p>

    <Callout kind="measured" title="Status matters here more than anywhere else in this project">
      <p>
        The ALU is <b>built and measured</b>: one lane at 324.8 MHz, 1,249 LUT, 3 DSP, no BRAM,
        latency 14 with II = 1, the FMA within one ulp — correctly rounded everywhere except one
        subtractive-alignment corner stated exactly below — and all four transcendental seeds
        faithful. The assembled <code>vec_lanes</code> and <code>vec_cu</code> are also built
        and measured. The instruction set around them is specified and partly built; the split-K
        path is design.
      </p>
    </Callout>

    <Fig
      caption="The core. L1 holds data in its memory format and converts on the read path into the ALU, because storing converted would cost 24 bits for FP16 data and make the buffer smaller in elements for no gain. The store converters write back into L1, and the third register-file mirror that feeds them is the one that had to stay in LUTRAM."
      zoom
      wide
    >
      <BlockDiagram :nodes="core.nodes" :edges="core.edges" />
    </Fig>

    <h2 class="doc-h2">The format: E8M15</h2>

    <BitField
      :fields="e8m15"
      caption="1 ≤ E ≤ 254 gives value = (-1)^S · 2^(E-127) · 1.M. E = 0 is zero, with no subnormals; E = 255 is inf (M=0) or NaN. The datapath sees sig = {1'b1, M}, 16 bits, always normalised"
    />

    <h3 class="doc-h3">Why E8: conversion becomes wiring</h3>

    <p class="doc-p">
      An 8-bit exponent covers FP32's range <b>exactly</b>, which makes conversion into the core
      range-lossless from both source formats.
    </p>

    <SpecTable :cols="conversions.cols" :rows="conversions.rows" />

    <p class="doc-p">
      Two consequences worth stating plainly. <b>There is no overflow, underflow or saturation
      logic on the way in</b> — with an E5 internal format every convert needs range checks in
      both directions, and an FP32 value outside FP16's range is destroyed rather than rounded.
      And <b>FP16 round-trips exactly</b>: a kernel that reads FP16, computes, and writes FP16
      loses precision only to the arithmetic, never to the format.
    </p>

    <h3 class="doc-h3">Why M15: the 48-bit C port fits the alignment range exactly</h3>

    <p class="doc-p">
      This is the load-bearing argument. An FMA has to align the addend against the product. The
      product of two 16-bit significands is 32 bits; the DSP's addend port <code>C</code> is 48
      bits. Lay the product at <code>[31:0]</code> and the addend's largest useful position is
      bit 47, which is the C port's top bit.
    </p>

    <BitField
      :fields="cPort"
      caption="16 significand bits + 32 product bits = 48. The fit is exact, with nothing wasted and nothing missing"
    />

    <SpecTable
      :cols="significands.cols"
      :rows="significands.rows"
      caption="There is a second, independent wall at the same place: the B port is 18-bit SIGNED, so it holds 17 significand bits, not 18. So E8M17 costs a second DSP or a 48-bit fabric adder in series with the alignment shifter, twice over; E8M15 costs neither"
    />

    <Callout kind="open" title="E8M16 is the honest middle">
      <p>
        Seventeen significand bits fill the B port exactly and cost nothing there, but still
        overflow the C port headroom by four bits. It is recorded because <b>if the alignment
        ever leaves the DSP for another reason, M16 becomes free and M15 stops being the right
        answer.</b>
      </p>
    </Callout>

    <h3 class="doc-h3">No subnormals, and why that is free</h3>

    <p class="doc-p">
      E8's range is so much wider than either source format that subnormals never arrive. An
      FP16 subnormal normalises into an ordinary E8M15 value — a leading-zero count and a shift,
      done once at the edge. An FP32 subnormal is below 2^-126, which no activation or weight
      reaches, and is flushed on entry. An E8M15 result that underflows past E=1 is flushed to
      zero. So <b>the implicit-bit mux, the subnormal exponent fixup and the denormal shifter
      disappear from every operation</b> — not just the FMA, but the comparator, the normaliser
      and all four transcendentals. In the older FP16 FMA the subnormal handling is roughly a
      third of the control logic; here it is zero.
    </p>

    <SpecTable
      :cols="precision.cols"
      :rows="precision.rows"
      caption="This is not an FP32 core. It accepts FP32 and immediately sits at 1.5e-5 — 32x better than FP16, 256x worse than FP32"
    />

    <h2 class="doc-h2">Three DSPs per ALU</h2>

    <SpecTable :cols="dsps.cols" :rows="dsps.rows" />

    <p class="doc-p">
      This is the same split the older FP16 FMA already used — one DSP for the exponent, one for
      the significand — with a third added for polynomials. What the third one buys, in order of
      value: <b>transcendentals at full rate</b>, with DSP-P doing Horner stage 1 and DSP-M
      stage 2, so <code>exp2</code>, <code>log2</code>, <code>inv</code> and <code>rsqrt</code>
      are one pass at II=1; full-rate FP32 as the two halves of a 24x24 product; and roughly
      35 LUTs and one logic level of exponent arithmetic.
    </p>

    <Callout kind="note" title="If DSP columns ever bind, DSP-E is the one to drop">
      <p>
        The exponent path is ~35 LUTs of adders in fabric, so the design degrades to 2 DSPs per
        ALU with transcendentals still at full rate. That is the graceful direction, and it is
        why the exponent went on the DSP that is also the least load-bearing. Measured, the
        worry turns out to be misdirected: the core is <b>fabric-bound, not DSP-bound</b>, so
        the third DSP is not the thing to economise on.
      </p>
    </Callout>

    <h2 class="doc-h2">The FMA, and the one place it is not correctly rounded</h2>

    <p class="doc-p">
      Two values are needed from the exponents and they are different linear combinations of the
      same three: <code>e_ab = e_a + e_b - 127</code> and <code>cs = e_ab - e_c</code>. A two-tap
      <code>B</code> constant — <code>2^12 + 1</code> — makes the multiplier emit two copies of
      the pre-adder sum at two positions, and <code>C</code> then biases each field
      independently, so one DSP produces both in one pass.
    </p>

    <SpecTable
      :cols="align.cols"
      :rows="align.rows"
      caption="The alignment is ONE unidirectional barrel shifter: aligned = ({sig_c, 32'b0}) >> s. A bidirectional shifter is two barrel shifters and a mux; biasing the shift by the headroom turns it into one, worth roughly 90 LUTs and a logic level per ALU, and it exists only because 16 + 32 landed on 48"
    />

    <p class="doc-p">
      The sign of the result never needs a magnitude comparison —
      <code>res_neg = neg &amp;&amp; (s != 0) &amp;&amp; P[47]</code>. The <code>s != 0</code>
      term is the non-obvious case: at <code>s == 0</code> the aligned addend is at least
      <code>2^47</code> and the product is below <code>2^32</code>, so the result is always
      positive and <code>P[47]</code> is a value bit rather than a sign bit. That case is
      unreachable by random operands and is exactly what the bench's alignment sweep exists to
      hit. There is <b>one bypass, and only one</b>: at <code>cs ≤ -18</code> the product is
      more than half an ulp below the addend, so the correctly rounded result <i>is</i> the
      addend and the output is <code>c</code> verbatim.
    </p>

    <Callout kind="trap" title="The rounding property, stated exactly">
      <p>
        “Bits shifted out of the 48-bit alignment window are carried as a plain sticky. For an
        effective addition that is round-to-nearest-even — correct. For an effective subtraction
        the discarded residue is a borrow, and a plain sticky rounds the wrong way in the cases
        adjacent to the round boundary: the result reads <b>exactly one ulp high — always high,
        never low</b>.”
      </p>
      <p>
        Reaching it takes three things at once: <code>s ≥ 33</code>, an effective subtraction,
        and a boundary-adjacent bit pattern. A stream built to oversample exponent-distant
        addends measures <b>19 in 4,000</b>; a banded random suite measures <b>0 in 6,000</b>.
        The same construction, and therefore the same property, is in <code>mx_fpacc</code>'s
        split path. Correcting it means complementing the residue on subtraction — small, and
        natural at the next respin of either datapath; until then this paragraph is the
        contract.
      </p>
    </Callout>

    <p class="doc-p">
      Every arithmetic opcode is this one FMA with different operand sources:
      <code>add = a*1 + c</code>, <code>mul = a*b + 0</code>, <code>affine = a*b + c</code>,
      <code>sub = a*1 - c</code>, <code>neg = a*(-1) + 0</code>, <code>fnma = -(a*b) + c</code>.
      And a reduction tree built from FMA nodes rather than adders computes more than sums —
      <code>a*a+c</code> is a sum of squares (variance in one pass), <code>a*b+c</code> is a dot
      product. That capability is free from choosing a three-input primitive.
    </p>

    <h2 class="doc-h2">Transcendentals: four seeds, chosen in base 2</h2>

    <p class="doc-p">
      The seeds are <code>exp2</code>, <code>log2</code>, <code>inv</code>, <code>rsqrt</code>.
      <b>Base 2, not base e</b>, and that choice earns its own paragraph: range reduction in
      base 2 is free and exact, because it is a bit slice —
      <code>log2(x) = (E - 127) + log2(1.M)</code> and <code>exp2(x) = 2^k · 2^f</code> with
      <code>k = floor(x)</code>. Base e needs <code>k = round(x · log2 e)</code> and
      <code>r = x - k·ln2</code>, which is two extra FMA passes <i>and</i> introduces its own
      rounding error before the table is even consulted. Nothing is lost, and in every kernel
      that matters the constant folds into a neighbouring scale that already exists.
      <code>rsqrt</code> is a <b>fourth table rather than a composition</b>, because every
      normalisation in a transformer hits it once per row.
    </p>

    <SpecTable
      :cols="segments.cols"
      :rows="segments.rows"
      caption="For a smooth function approximated by degree-d minimax polynomials over segments, reaching n bits needs segments ∝ 2^(n/(d+1)). 32 segments is also the fabric sweet spot: a 32-entry constant table is a LUT5 — half a LUT6 — per output bit, and rsqrt needs both octave parities so it is 64 entries, exactly one LUT6 per bit"
    />

    <Callout kind="measured" title="More segments would buy almost nothing">
      <p>
        The measured accuracy of each seed against the <i>actual</i> fixed-point circuit — both
        Horner roundings and all three quantised coefficients — is consistently about <b>1.5
        bits worse</b> than the minimax prediction. That gap is the point of measuring: the
        approximation is no longer what limits these functions, <b>the coefficient and Horner
        quantisation is</b>. That is the opposite of the intuition that more segments means more
        accuracy, and it is why the count stops at 32.
      </p>
    </Callout>

    <SpecTable
      :cols="assembly.cols"
      :rows="assembly.rows"
      caption="Three of the four need no normaliser at all — the result arrives pre-normalised and assembly is a concatenation. exp2 skipping it is the single biggest reason base 2 wins: 2^f for f in [0,1) is in [1,2) BY CONSTRUCTION, so the two most expensive stages of a float pipeline both vanish"
    />

    <Callout kind="note" title="The comparator is the best value per LUT in the design">
      <p>
        E8M15 is sign-magnitude with the exponent above the mantissa, so ordering by magnitude
        is the unsigned integer order of bits <code>[22:0]</code> — no decode, no unpacking. A
        full signed compare is that plus the sign bits: about <b>20 LUTs, zero DSP</b>, driving
        <code>max</code>, <code>min</code>, <code>select</code>, <code>clamp</code>,
        <code>relu</code> and the predicate output. Without it a <code>max</code> is a subtract,
        a sign test and two blended multiply-adds — <b>three passes for one max</b>, paid by
        every max reduction, every clamp, every ReLU and the first pass of every softmax.
      </p>
    </Callout>

    <SpecTable :cols="costs.cols" :rows="costs.rows" caption="Newton refinement stays in software: at this table accuracy the native result is already better than the format, so refinement buys nothing in native mode. Keeping it an instruction sequence makes accuracy a PROGRAM choice rather than a synthesis choice" />

    <h2 class="doc-h2">Sixteen ALUs, and why chaining is mandatory</h2>

    <p class="doc-p">
      A mesh flit payload is 256 bits, and a vector core is a two-port endpoint like a cluster,
      so 512 payload bit/cycle — 32 FP16 elements. A flat elementwise op reads two vectors and
      writes one: <b>3 elements of traffic per result</b>. That is a bandwidth ceiling of
      <code>32 / 3 = 10.7</code> results/cycle against a compute ceiling of 16.
    </p>

    <SpecTable :cols="bound.cols" :rows="bound.rows" />

    <p class="doc-p">
      <b>Flat mode is memory-bound and more lanes would not help it.</b> <code>D2</code> already
      saturates the ALUs. So 16 lanes is not a round number: it is where two ports of mesh
      bandwidth and a depth-2 chain meet. Halving the pass count is worth exactly as much as
      doubling the ALU count and costs far less — a <code>D4</code> chain writes no intermediate
      to the register file at all. <b>Chaining is not an optimisation here; it is what makes the
      core compute-bound at all.</b>
    </p>

    <SpecTable
      :cols="modes.cols"
      :rows="modes.rows"
      caption="A mode is a factorisation, W lanes x D chain depth with W·D = 16, plus a reduction tree. Every ALU is used in every mode, and 16 is the smallest N for which that is true: below 16 the tree wastes ALUs; above it the tree needs a fifth level and depth-4 chains stop dividing evenly"
    />

    <LaneGrid
      :lanes="16"
      :rows="treeRows"
      caption="TREE spends its sixteen ALUs as 8 leaves + 7 combine nodes + 1 accumulator — the tree physically IS the other eight ALUs."
    />

    <Callout kind="rule" title="A chain OR a tree, never both">
      <p>
        An 8-wide by 2-deep chain also spends all sixteen ALUs. So a reduction fused with a
        <b>two-stage</b> elementwise computation does not fit, and no amount of wiring makes it
        fit. That is the whole reason the fused exp-and-sum reduction is unary:
        <code>exp2(a)</code> is one stage, so it sits in the leaf and the tree survives;
        <code>exp2(a - m)</code> is two, and would need a fused single-stage leaf op inside the
        ALU — measured at ~800 LUT per core for the bias alone.
      </p>
      <p>
        The corollary is the useful one: <b>any unary elementwise op can be fused with a
        reduction for almost nothing</b>, because slice <i>s</i> comes from leaf
        <i>s mod 8</i> and below 8 that is the wire flat mode already selects.
      </p>
    </Callout>

    <h3 class="doc-h3">The accumulator recurrence</h3>

    <WaveTrace
      :rows="recurBroken.rows"
      :notes="recurBroken.notes"
      variant="broken"
      label="one architectural accumulator"
    />

    <WaveTrace
      :rows="recurFixed.rows"
      :notes="recurFixed.notes"
      variant="fixed"
      label="16 rotating partials, one per pipeline slot"
    />

    <h2 class="doc-h2">The register file, L1, and how little of this resembles the cluster</h2>

    <p class="doc-p">
      <b>Nothing about this core's storage is shared with the matmul cluster except the port it
      reaches the mesh through.</b> A 256-bit L1 word is 16 FP16 elements, which is exactly one
      flat-mode cycle of work for 16 lanes — the mesh word width, the lane count and the native
      element size line up so that a fill beat feeds exactly one cycle. At FP32 a word is 8
      elements and loads run at half rate, which is the honest cost of the wider input format
      rather than a surprise.
    </p>

    <p class="doc-p">
      The register file is <b>striped by lane</b>: lane <i>i</i> holds elements <i>i</i>,
      <i>i+16</i>, <i>i+32</i>, … That is what makes the file lane-local, which is what makes it
      affordable — a monolithic file serving 16 lanes x 3 ports would need 48 read ports.
      <b>Anything that crosses lanes must say so</b>, which is what the shuffle instruction is
      for. It is 3R1W per lane, built as three mirrored single-read memories written in
      lockstep.
    </p>

    <Callout kind="trap" title="The three copies are not the same primitive">
      <p>
        Two feed ALU operands and get a whole cycle, so they are block RAM. The third feeds the
        store converters, and a block RAM's clock-to-out in series with a 16-lane E8→FP16
        normalise measured <b>286.0 MHz</b>, below the 300 floor — so that copy stays in LUTRAM.
        A whole-module primitive parameter hid the fact that only one of three consumers could
        not afford the trade; splitting it kept two thirds of the area win for one eighth of the
        memory.
      </p>
      <p>
        <b>Moving storage to block RAM moves its clock-to-out onto every consumer's path, and
        port granularity is the unit that matters, not the module.</b>
      </p>
    </Callout>

    <p class="doc-p">
      <b>L1 is a scratchpad, not a cache.</b> No tags: the access pattern is strided streaming
      produced by the address generator, so every address is known in advance, and tags would
      buy nothing and cost a lookup in the load path. It <b>ships as block RAM</b>, which is the
      opposite verdict to the accumulator's and for the opposite reason — the accumulator is
      already <code>READ_LAT = 2</code>, so URAM is free there; the vector core runs
      <code>READ_LAT = 1</code>, <b>URAM cannot do <code>READ_LAT = 1</code> at all</b>, and the
      core is schedule-bound rather than capacity-bound, so a cycle on the load path is the
      wrong thing to spend.
    </p>

    <Callout kind="open" title="A deeper vector L1 is blocked somewhere else entirely">
      <p>
        The fill protocol's tag ports are 9 bits, so it cannot name an entry past 511 however
        wide the address inside the core is. That is a protocol change, not a memory one, and it
        is what to fix first if a deeper L1 is ever wanted.
      </p>
    </Callout>

    <h2 class="doc-h2">Addressing: the difference between a kernel language and a fixed function</h2>

    <p class="doc-p">
      An address descriptor is a base plus four <code>(stride, bound)</code> pairs:
      <code>addr = base + Σ idx_i · stride_i</code>, with <code>idx_i &lt; bound_i</code>.
      Reshape, permute, expand, pad, slice and broadcast are not arithmetic — they are
      <i>views</i>, and with strides they are all free: permute is a permutation of the stride
      list, broadcast is stride 0, pad and slice are bounds and an offset. Without a strided
      address generator every one of them becomes a physical copy, and something has to perform
      those copies. <b>That something should be a DMA engine — never a scalar core.</b>
    </p>

    <h2 class="doc-h2">What the vector core should not do</h2>

    <p class="doc-p">
      Everything data-dependent or irregular, which is what a wide SIMD array is worst at:
    </p>

    <ul class="kt-text-body text-warm-700 dark:text-warm-300 leading-7 my-4 max-w-[70ch] list-disc pl-5">
      <li v-for="t in notThis" :key="t">{{ t }}</li>
    </ul>

    <Callout kind="rule" title="A real capability boundary, not a preference">
      <p>
        The budget for that work is <b>thousands of operations per token, not billions</b>. The
        moment bulk arithmetic lands there it becomes the limit. Element-dynamic behaviour —
        where <i>which</i> element is touched depends on a value — is <b>not expressible
        on-device at any cost today</b>, and that is the machine's largest single gap.
        Scalar-dynamic behaviour is expressible, at the price of one host round trip per step.
      </p>
    </Callout>

    <h2 class="doc-h2">Measured</h2>

    <SpecTable
      :cols="measured.cols"
      :rows="measured.rows"
      caption="Out-of-context synthesis on xcvu13p-fhgb2104-2L-e. One lane at 324.8 MHz says nothing about the assembled core: vec_lanes and vec_cu started at 305.1 and 229.3 MHz, and the paths that bound them were all control reaching a datapath, never the arithmetic. After the shrink the worst path is no longer in the core at all — it is inside the ALU, so the core now sits at the ALU floor"
    />

    <h2 class="doc-h2">Open, and needing measurement rather than argument</h2>

    <ul class="kt-text-body text-warm-700 dark:text-warm-300 leading-7 my-4 max-w-[70ch] list-disc pl-5">
      <li v-for="(o, i) in open" :key="i" v-html="o" />
    </ul>

    <Callout kind="trap" title="An L1 footprint band that returns wrong data">
      <p>
        A vector kernel whose buffers occupy <b>352 to 480 of the core's 512 L1 words returns
        wrong data</b>. 320 words and below is clean, and so is exactly 512. Measured,
        unexplained, and currently guarded rather than fixed.
      </p>
    </Callout>
  </DocPage>
</template>
