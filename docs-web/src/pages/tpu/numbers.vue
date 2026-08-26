<script setup>
/* MXFP7 and the dtype ladder. Accuracy figures are measured on
 * xcvu13p-fhgb2104-2L-e or in the bench that names its two ground truths. */

const scaleField = [
  { name: "E", bits: 5, value: "exponent, SBIAS = 20", accent: true },
  { name: "M", bits: 3, value: "mantissa, scale = 2^(E-20) · (1 + M/8)" },
];

const payload = [
  { name: "32 x int7", bits: 224, value: "element (i,k), i = 0..3, k = 0..7" },
  { name: "4 x E5M3", bits: 32, value: "one scale per row i", accent: true },
];

/* The ladder a value walks along. A pipeline drawn top-to-bottom fights its own
 * subject, so this runs left to right; the table below carries the detail. */
const ladder = {
  nodes: [
    {
      id: "a",
      x: 0,
      y: 0,
      w: 7,
      h: 7.5,
      label: "DRAM / mesh",
      sub: "FP16 — software-visible",
    },
    {
      id: "b",
      x: 11,
      y: 0,
      w: 7,
      h: 7.5,
      label: "L1 · tensor CU",
      sub: "int7 + E5M3 · 928 bits per entry",
      accent: true,
    },
    {
      id: "c",
      x: 22,
      y: 0,
      w: 7,
      h: 7.5,
      label: "cluster output",
      sub: "int19 + scale · one exact K=32 block",
      accent: true,
    },
    {
      id: "d",
      x: 33,
      y: 0,
      w: 7,
      h: 7.5,
      label: "accumulator",
      sub: "FP22 S1E7M14 · one add per 32 MACs",
      accent: true,
    },
    {
      id: "e",
      x: 44,
      y: 0,
      w: 7,
      h: 7.5,
      label: "mesh / DRAM",
      sub: "FP16 — software-visible again",
    },
    {
      id: "v",
      x: 44,
      y: 11,
      w: 7,
      h: 7.5,
      label: "vector core",
      sub: "E8M15 inside · FP32 or FP16 in memory",
    },
  ],
  edges: [
    { from: "a:r", to: "b:l", dir: "h", accent: true },
    { from: "b:r", to: "c:l", dir: "h", accent: true },
    { from: "c:r", to: "d:l", dir: "h", accent: true },
    { from: "d:r", to: "e:l", dir: "h", accent: true },
    { from: "e:b", to: "v:t", dir: "v", dash: true, label: "epilogues" },
  ],
};

const dtypes = {
  cols: [
    { key: "f", label: "format", mono: true },
    { key: "b", label: "bits", mono: true, align: "right" },
    { key: "w", label: "where it lives" },
    { key: "y", label: "why this width" },
  ],
  rows: [
    {
      f: "FP16",
      b: "16",
      w: "DRAM, the mesh, the driver",
      y: "what software puts in and gets out; the format never leaves this at the boundary",
    },
    {
      f: "<b>int7 + E5M3</b>",
      b: "<b>7</b> + 8 per 32",
      w: "cluster L1 and the DSP operands",
      y: "<code>32 x 7 = 224</code> plus <code>4 x 8 = 32</code> fills a 256-bit flit exactly — and the guard budget independently gives cascade depth 32",
      _tone: "good",
    },
    {
      f: "int19 + scale",
      b: "19",
      w: "the cluster chain's output",
      y: "a K=32 sum reaches ±131,072, which is 18 bits and a sign",
    },
    {
      f: "<b>FP22</b>  S1E7M14",
      b: "22",
      w: "the accumulator tile",
      y: "E7 is required by the sum of two E5M3 exponents, not chosen; MW=14 sits at the accuracy cliff with less area and more slack",
    },
    {
      f: "<b>E8M15</b>",
      b: "24",
      w: "inside a vector ALU",
      y: "16 significand bits + 32 product bits = 48, the DSP's C port exactly",
    },
    {
      f: "FP32",
      b: "32",
      w: "accepted and emitted by the vector core",
      y: "E8 = E8, so the exponent needs no change at all in either direction",
    },
  ],
};

const scaleChoice = {
  cols: [
    { key: "s", label: "scale field" },
    { key: "p", label: "p50 relative error", mono: true, align: "right" },
    { key: "q", label: "p99", mono: true, align: "right" },
  ],
  rows: [
    { s: "power-of-two scale (E8M0, the OCP shape)", p: "0.54%", q: "48%" },
    { s: "<b>E5M3</b>", p: "<b>0.38%</b>", q: "<b>23%</b>", _tone: "good" },
  ],
};

const conversionSite = {
  cols: [
    { key: "w", label: "" },
    { key: "l", label: "where" },
    { key: "s", label: "source in memory", mono: true },
    { key: "c", label: "cost" },
  ],
  rows: [
    {
      w: "<s>online</s>",
      l: "<s>on the read path, per fetch</s> — <b>retired</b>",
      s: "—",
      c: "would have been once <b>per read</b>",
      _tone: "bad",
    },
    {
      w: "<b>pre-converted</b>",
      l: "a mover pass: mem/L2 → slot → mem/L2",
      s: "int7+E5M3, 128 B/entry",
      c: "once <b>per tensor</b>",
      _tone: "good",
    },
  ],
};

const errLadder = {
  cols: [
    { key: "f", label: "format" },
    { key: "e", label: "relative error, ½ ulp", mono: true, align: "right" },
    { key: "n", label: "note" },
  ],
  rows: [
    { f: "FP16", e: "4.9e-4", n: "one FP16 ULP is 9.77e-4" },
    {
      f: "<b>E8M15</b>",
      e: "<b>1.5e-5</b>",
      n: "32x better than FP16, 256x worse than FP32 — this is not an FP32 core",
      _tone: "good",
    },
    {
      f: "E8M16 — 17 significand bits",
      e: "7.6e-6",
      n: "2x better, and free on the DSP's 18-bit <i>signed</i> B port, which stops at 17. It still overflows the addend's 48-bit window by 4 bits, so the alignment shifter leaves the DSP",
      _tone: "warn",
    },
    {
      f: "E8M17 — 18 significand bits",
      e: "3.8e-6",
      n: "4x better, bought at the one place in the datapath that is already the critical path: an 18-bit unsigned significand reads negative on a signed port, and the window overflows by 7",
      _tone: "warn",
    },
    {
      f: "FP32",
      e: "6.0e-8",
      n: "reachable only through the extended mode, which is designed and not built",
    },
  ],
};

const mw = {
  cols: [
    { key: "m", label: "MW", mono: true, align: "right" },
    { key: "w", label: "width", mono: true },
    { key: "e", label: "worst rel. error", mono: true, align: "right" },
    { key: "u", label: "in FP16 ULP", mono: true, align: "right" },
    { key: "v", label: "verdict" },
  ],
  rows: [
    {
      m: "<b>16</b>",
      w: "FP24",
      e: "3.34e-4",
      u: "<b>0.34</b>",
      v: "pass",
      _tone: "good",
    },
    {
      m: "<b>14</b>",
      w: "FP22",
      e: "3.37e-4",
      u: "<b>0.35</b>",
      v: "pass — the operating point",
      _tone: "good",
    },
    {
      m: "12",
      w: "FP20",
      e: "4.27e-3",
      u: "4.4",
      v: "marginal",
      _tone: "warn",
    },
    {
      m: "11",
      w: "FP19",
      e: "2.04e-3",
      u: "2.1",
      v: "marginal",
      _tone: "warn",
    },
    { m: "10", w: "FP18", e: "5.40e-3", u: "5.5", v: "fails", _tone: "bad" },
  ],
};

const growth = {
  cols: [
    { key: "s", label: "operand statistics" },
    { key: "g", label: "growth of |c|", mono: true },
    { key: "a", label: "K=256", mono: true, align: "right" },
    { key: "b", label: "K=1024", mono: true, align: "right" },
    { key: "c", label: "K=2048", mono: true, align: "right" },
  ],
  rows: [
    {
      s: "zero-mean, std σ",
      g: "~4.2 √K σa σb",
      a: "67σ²",
      b: "134σ²",
      c: "190σ²",
    },
    {
      s: "<b>mean μ ≠ 0</b>",
      g: "<b>~K μa μb</b>",
      a: "<b>256μ²</b>",
      b: "<b>1024μ²</b>",
      c: "<b>2048μ²</b>",
      _tone: "bad",
    },
  ],
};

const card = {
  cols: [
    { key: "w", label: "" },
    { key: "p", label: "rel p50", mono: true, align: "right" },
    { key: "q", label: "rel p90", mono: true, align: "right" },
    { key: "o", label: "over 10%", mono: true, align: "right" },
  ],
  rows: [
    {
      w: "software <code>int7 + E8M0</code>",
      p: "2.26%",
      q: "14.16%",
      o: "14.0%",
    },
    {
      w: "software <code>int7 + E4M3</code>",
      p: "4.06%",
      q: "26.84%",
      o: "25.3%",
    },
    {
      w: "<b>the card</b>, blown elements excluded",
      p: "<b>1.64%</b>",
      q: "<b>10.82%</b>",
      o: "<b>10.8%</b>",
      _tone: "good",
    },
  ],
};

const notCovered = [
  "<b>Range is FP16's, not FP32's.</b> The scale is E5M3, spanning FP16's ~30 binades, so quantising an FP32 tensor is bounded by FP16's range. In practice that costs nothing, since data outside FP16's range could not have been an FP16 tensor either — but it is why the format's value type is FP16.",
  "<b>FP32 operands are not supported by the converter.</b> The honest route if they are ever wanted is teaching the occupant an FP32 mode: the block-peak reduction works unchanged, because FP32 is sign-magnitude with the exponent above the mantissa and the peak is still a plain unsigned max. The cost is halved read bandwidth on the converting move, 8 elements per beat instead of 16.",
  "<b>Feeding FP32 into a matmul is precision theatre regardless.</b> Everything below ~7 bits plus a block scale is discarded before the first multiply, so a tensor destined for a matmul should be stored FP16 by whatever produced it.",
  "<b>Results saturate on the way out.</b> The accumulator's own range is far wider than FP16's, and the conversion at <code>EMIT</code> clamps at 65,504 silently.",
];
</script>

<template>
  <DocPage
    title="MXFP7 and the dtype ladder"
    summary="An int7 significand with an E5M3 scale shared by 32 elements — why this format, what the anchor is for, every format a value passes through on its way across the machine, and the one place precision actually dies."
    domain="tpu"
    status="shipped"
    source="xcvu13p-fhgb2104-2L-e, Vivado 2024.2 · docs/projects/kohakutpu/number-format.md · results.md §6"
  >
    <p class="doc-p">
      The element format KohakuTPU's tensor core multiplies in. A
      <b>microscaling</b> format in the OCP style — one scale shared by a block
      of elements — with two deliberate departures: the block is
      <b>32 along the reduction axis only</b>, and the scale is
      <b>E5M3</b> rather than E8M0. <code>SBIAS = 20</code>,
      <code>KBLOCK = 32</code> and <code>ANCHOR = 40</code>
      are the constants, and they are what the RTL is written against.
    </p>

    <BitField
      :fields="scaleField"
      caption="The scale field for one block. It stays 8 bits — the same width an E8M0 field would be — so nothing about the flit format, the mesh or L1 changes; only the interpretation does"
    />

    <Callout kind="rule" title="Software never sees this">
      <p>
        The host uploads FP16, results come back FP16, and throughout these
        pages
        <b
          >“int7” means the 7-bit significand field as it appears inside the DSP
          packing</b
        >
        — never a type a program has. The driver never constructs int7+E5M3
        itself: it marks the upload and the hardware converts, so the format
        stays entirely inside the machine and the software model exists only as
        a golden reference for the bench.
      </p>
    </Callout>

    <h2 class="doc-h2">Why a block-scaled integer format at all</h2>

    <p class="doc-p">
      The previous design multiplied FP8 elements and summed them in a
      floating-point adder tree.
      <b
        >That tree was 84% of the tensor core: 10,656 of 12,731 LUTs for 128
        MACs.</b
      >
      Every node in it aligned, added and normalised, because every product
      arrived with its own exponent. Factor the exponent out of a whole block
      and that stops being true: with the scale shared <b>along K only</b>,
    </p>

    <p class="doc-p">
      <code
        >C[i][j] = ( Σ_k a_int[i][k] · b_int[k][j] ) · 2^(sA[i] + sB[j])</code
      >
      — the left half exact integer, the right half constant across the entire
      block.
    </p>

    <p class="doc-p">
      The scale factor is constant across the block, so every product entering
      the sum has the same weight.
      <b>No alignment is needed and the sum is exact.</b> That is the property
      the whole cluster is built around, and it is why the adder tree disappears
      rather than getting cheaper: there is nothing left for it to do. The
      reason the block is shared along K and not along a tile is the same
      property — a scale shared across rows would not cancel out of the
      reduction.
    </p>

    <Callout kind="note" title="The block size is not an independent parameter">
      <p>
        <code>KBLOCK = 32</code> is the same number as the DSP cascade depth,
        and that is not a coincidence —
        <b>it is one parameter wearing two names.</b> A smaller block, say K=8
        and one per tensor CU, would force a rescale between every CU in the
        chain and collapse the exact integer level back into floating point; a
        larger one would need guard bits the packing does not have.
        <b
          >So the block size was not chosen for numerical reasons and then
          implemented. It fell out of the DSP arithmetic, and the numerics were
          checked against it afterwards.</b
        >
      </p>
    </Callout>

    <h2 class="doc-h2">Why E5M3 and not E8M0</h2>

    <p class="doc-p">
      <b
        >Three mantissa bits, because a power of two wastes up to a full bit of
        the significand.</b
      >
      With <code>scale = 2^e</code> the best available scale can only land a
      block's peak somewhere in <code>[32, 64)</code> of the int7 range — where
      inside that binade depends on where the peak happens to fall, so the loss
      is between zero and one whole bit and varies block to block. Three
      mantissa bits put the peak in <code>[56, 63]</code> every time.
    </p>

    <SpecTable
      :cols="scaleChoice.cols"
      :rows="scaleChoice.rows"
      caption="Measured per element on correlated operands. Same 8-bit field, so nothing about the flit format, the mesh or L1 changed"
    />

    <p class="doc-p">
      <b>Five exponent bits, because the output is FP16.</b> FP16's normal range
      spans 30 binades; E5 covers 31, so it just fits, and E4 covers 16 and does
      not. The three extra exponent bits an E8M0 field would spend buy range
      this datapath cannot express anyway. The mantissa costs one multiply at
      each end and neither is on a critical path: quantising divides by the
      scale using an eight-entry reciprocal table — the divisor has exactly
      eight possible values, so a divider was never needed — and accumulating
      multiplies the integer partial by <code>m8a·m8b</code>, once per 32 MACs.
    </p>

    <Callout
      kind="rule"
      title="The significand rounds to nearest, the scale rounds up"
    >
      <p>
        The scale rounds <b>up</b> to the smallest representable value with
        <code>peak/scale ≤ 63</code>. Rounding it down would put the block's
        peak past 63 and clip it — damaging the largest element in the block,
        which is the one that matters most. Rounding up costs at most a little
        of the range below the peak.
      </p>
      <p>
        Two edge cases are handled rather than ignored.
        <b>A block whose peak is itself subnormal</b> wants a scale below E5's
        range; it clamps, which degrades the block where letting the exponent
        wrap would corrupt it outright. And
        <b>subnormal elements are decoded properly, not flushed</b> — flushing
        would zero most of any block whose peak is below ~2e-3, which would read
        as the format being poor on small tensors rather than as a dropped case.
      </p>
    </Callout>

    <h2 class="doc-h2">The operand payload, and why the element is 7 bits</h2>

    <BitField
      :fields="payload"
      caption="A tensor CU consumes a K=8 slice of a 4-row operand per cycle: 32 elements plus the block's four scales riding along with them. The same 256 bits reads as 16 x FP16 when the buffer holds float data"
    />

    <p class="doc-p">
      <b>int7 is the width that fills the payload exactly</b>, and that is one
      of two independent reasons the element is seven bits rather than eight;
      the other is the guard-bit budget on the matmul page, which arrives at the
      same answer from the DSP side. The four scales are identical across the
      four K-slices of a block, and repeating them costs 12.5% of the payload
      and makes <b>every flit self-contained</b> — a response word says what it
      is without reference to any other, which is what lets responses arrive out
      of order.
    </p>

    <h2 class="doc-h2">The anchor</h2>

    <p class="doc-p">
      The block scales are stored with their exponents biased by
      <code>SBIAS = 20</code>, so a stored field of <code>E</code> means
      <code>2^(E-20)</code>. When two of them meet in the accumulator, both
      biases have to come off:
      <code>exp = ea[i] + eb[j] - anchor - 6</code> with
      <code>ANCHOR = 2 · SBIAS = 40</code>, and
      <code>val = part · (m8a · m8b)</code> with <code>m8 = 8 + M</code>.
      <b>The exponent halves add; the mantissas multiply.</b> <code>(1 + Ma/8)(1 + Mb/8)</code> is <code>(m8a · m8b) / 64</code> with
      the product in <code>[64, 225]</code>, so the partial sum is multiplied by
      an 8-bit integer and the <code>/64</code> comes off the exponent as the
      <code>-6</code>. That is <b>exact</b>: no shifter, no rounding, and no
      precision lost. Converting each scale to a float and multiplying would
      have cost both.
    </p>

    <Callout
      kind="trap"
      title="One field on DRAIN is dead, and it is decoded and discarded"
    >
      <p>
        The anchor is a <b>constant of the format, not a tunable</b> — every
        <code>GEMM</code> carries it, and it is a field at all only because the
        accumulator has no other way to be told which bias convention its
        operands were stored under. <code>DRAIN</code> carries the same field
        and it is dead: during a drain the cluster forces <code>anchor</code>,
        <code>sa</code> and <code>sb</code> to zero, because
        <code>EMIT</code> reads the tile and converts without applying any
        scale.
      </p>
    </Callout>

    <h2 class="doc-h2">Where the conversion happens</h2>

    <p class="doc-p">
      <b>Not in the compute unit, and not on a fetch.</b> The quantiser occupies
      the memory agent's <i>transform slot</i> at id 1 — one shared bank sitting
      <b>on the memory mover's read-return path</b>, between R and its staging
      FIFO, so nothing but the mover can reach it. A conversion is a move,
      mem/L2 → slot → mem/L2, run as descriptor <b>mode 5</b>, and it happens
      before any fetch reads the result. A compute unit's fetch is never
      transformed: it reads operands already in their final format.
      <b>The transform itself is KohakuTPU's</b> — a different project plugs
      something else into the same slot, or the identity bank, and reads its
      operands through untouched.
    </p>

    <p class="doc-p">
      One converted entry is 4 lanes × 32 K elements:
      <b>8 beats of 256 bits in, 4 words of 256 bits out</b> — the
      <code>IN_BITS</code>/<code>OUT_WORDS</code> ratio the occupant declares,
      because the mover has to size the move before the transform has run. The
      entry is 928 bits in L1 against 2,048 bits of FP16 in memory — the 2.2×
      density that is the whole reason the encoding exists between memory and
      the MAC array. The block scale is shared along K, so
      <b>nothing can be emitted until the whole entry has arrived</b>, which is
      why the occupant buffers an entry rather than streaming it.
    </p>

    <p class="doc-p">
      The request no longer carries a flag for any of this.
      <code>flags[4]</code> and <code>flags[5]</code> — <code>QUANT</code> and
      <code>BLAYOUT</code> — are reserved and ignored, and so is a cluster's
      <code>preq</code>.
      <b
        >The driver states the format by scheduling the conversion, not by
        flagging the read.</b
      >
    </p>

    <SpecTable
      :cols="conversionSite.cols"
      :rows="conversionSite.rows"
      caption="An operand is read once per output tile it participates in, so converting once is the online cost divided by the number of passes, and it halves the bytes the fetch path moves for good. That is why the online arrangement was removed rather than kept as an option: there is no shape at which paying per read is the cheaper of the two. The memory system still holds no map of which addresses are which format, and still must not learn one"
    />

    <p class="doc-p">
      Element slot assignment is the only difference between an A operand and a
      B operand, and that is where the transpose happens:
      <code>lane*8 + (k % 8)</code> for A, <code>(k % 8)*4 + lane</code> for B.
      One circuit serves both, so the driver stores both operands in the same
      shape.
    </p>

    <h2 class="doc-h2">The dtype ladder</h2>

    <Fig
      caption="This is a ladder of FORMATS, not a dataflow: the first arrow is the quantiser — a max-tree to an E5M3 scale, then a shift-and-round to int7 — and it does NOT happen on the way from DRAM to L1. It is a separate mover pass run beforehand, so what actually crosses that arrow at run time is already int7 + E5M3. The other three do sit on the datapath: the exact integer accumulation over K=32, a single normalise into the accumulator, and the conversion at EMIT, which is the step that saturates, silently, at 65,504. The machine as a whole is AMP FP16-MXFP7 — operands and results in memory are FP16, the multiply is MXFP7, the accumulate is FP22 — so the throughput unit is FLOPS, not IOPS, and one MAC counts as 2 FLOPs."
      zoom
    >
      <BlockDiagram :nodes="ladder.nodes" :edges="ladder.edges" />
    </Fig>

    <SpecTable :cols="dtypes.cols" :rows="dtypes.rows" />

    <SpecTable
      :cols="errLadder.cols"
      :rows="errLadder.rows"
      caption="The vector core's internal ladder. FP16 round-trips through E8M15 exactly, so a kernel that reads FP16, computes, and writes FP16 loses precision only to the arithmetic and never to the format"
    />

    <h2 class="doc-h2">Where precision does not die</h2>

    <p class="doc-p">
      Two candidates turn out not to be the limit, and both were measured rather
      than argued.
    </p>

    <SpecTable
      :cols="mw.cols"
      :rows="mw.rows"
      caption="384 checks per width, on a 32-block K sweep accumulated into one resident sub-tile — 32 roundings deep, which is what a real K=1024 matmul does. There is a cliff between 22 and 20 bits, not between 24 and 20; the ordering among MW=10/11/12 is not meaningful, and the signal is the ~13x step between 14 and 12"
    />

    <Callout
      kind="measured"
      title="Two ground truths, and the bench checks they agree first"
    >
      <p>
        Every figure above comes from the same bench at different widths,
        checked against an exact integer model <b>and</b> an FP64 model, with
        the bench asserting that those two agree before either is trusted. That
        is what makes the reported error attributable to the accumulator rather
        than to quantisation or to a drifting model.
      </p>
      <p>
        Narrowing also exposed a real bug the wide case hides: in the
        rounding-carry path the fraction was taken one bit too wide, which
        overflows the output concatenation and pushes the sign bit out. At MW=16
        nothing in the suite rounds up far enough to reach that path.
        <b>Sweeping a parameter is a test in its own right.</b>
      </p>
    </Callout>

    <h2 class="doc-h2">Where precision dies</h2>

    <p class="doc-p">
      In the conversion to FP16 on the way out, which saturates at 65,504
      silently. The growth of a dot product is what makes this ordinary rather
      than exotic.
    </p>

    <SpecTable
      :cols="growth.cols"
      :rows="growth.rows"
      caption="The second row is the one real workloads sit in — every post-ReLU activation is non-negative — so K = 2048 overflows FP16 once μa·μb > 32. And the first row implies something in the other direction: for zero-mean data, K=256 → 2048 costs only √8 = 2.8x of headroom, so a shape that overflows at K=2048 was already within 3x of overflowing at K=256"
    />

    <Callout
      kind="trap"
      title="A K sweep does not gradually erode headroom; a biased operand distribution destroys it outright"
    >
      <p>
        Splitting K does not fix it — the final sum is the same number however K
        is partitioned. What splitting K enables is a different place to finish,
        in a format with FP32's exponent range, converting once on the store.
        <b>That path is not built</b>, and until then silent FP16 saturation is
        an open defect rather than a documented limit.
      </p>
    </Callout>

    <h2 class="doc-h2">What the format actually costs, on the card</h2>

    <SpecTable
      :cols="card.cols"
      :rows="card.rows"
      caption="Measured against FP64 on a real ViT-B/16 projection with normalised activations, 128x256x256 on xcvu13p-fhgb2104-2L-e. Quote p50 / p90 / >10%; the max is a defect, not a property of the format"
    />

    <Callout
      kind="measured"
      title="The software model is PESSIMISTIC about this silicon by ~1.4x"
    >
      <p>
        Anyone using <code>int7 + E8M0</code> in software to argue a number
        format should say so; the hardware quantiser is better than the model of
        it, consistently across four operand distributions.
      </p>
      <p>
        <b
          >The operand distribution decides the answer, so a figure without one
          is meaningless</b
        >: the same card measures 0.39% p50 on <code>lowrank</code> operands and
        1.64% on real weights. <code>lowrank</code> is optimistic — A and B
        share a basis there, which real weights and activations do not — so iid
        <code>normal</code> is the better proxy for a linear layer.
      </p>
    </Callout>

    <Callout kind="trap" title="Two card observations, one closed and one open">
      <p>
        <b>The blown elements are OPERAND RANGE, and that one is closed.</b>
        Their count follows operand magnitude and nothing else — scaling an
        operand by an exact power of two, which changes no mantissa, takes 75
        blown at a true peak of 5.79 to <b>195</b> at 11.57 and to
        <b>zero</b> at 1.45. Keep the contraction in range, and do not read a
        saturated element as evidence about the machine.
      </p>
      <p>
        <b>The flickering is separate and still open.</b> Repeating one matmul
        with fixed operands leaves ~0.6% of elements differing between runs, and
        the bad elements sit one per 4x4 sub-tile — sixteen of those share one
        32-byte word, so no skew or dropped beat can produce it.
        <b
          >In simulation two runs of the same shape are bit-identical, verified
          by hash; on the card they are not</b
        >, so expect ~0.5% disagreement before concluding anything from a
        difference between two hardware runs.
      </p>
    </Callout>

    <h2 class="doc-h2">What the format does not cover</h2>

    <ul
      class="kt-text-body text-warm-700 dark:text-warm-300 leading-7 my-4 max-w-[70ch] list-disc pl-5"
    >
      <li v-for="(t, i) in notCovered" :key="i" v-html="t" class="my-2" />
    </ul>
  </DocPage>
</template>
