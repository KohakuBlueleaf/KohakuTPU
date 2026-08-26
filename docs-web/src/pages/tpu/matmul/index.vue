<script setup>
/* The matmul cluster. Resource and Fmax figures are out-of-context synthesis on
 * xcvu13p-fhgb2104-2L-e; the results page carries their conditions. */

const hierarchy = {
  cols: [
    { key: "l", label: "level", mono: true },
    { key: "s", label: "span", mono: true },
    { key: "d", label: "data" },
    { key: "m", label: "mechanism" },
    { key: "c", label: "what it costs" },
  ],
  rows: [
    {
      l: "L0",
      s: "K = 8",
      d: "int",
      m: "DSP <code>PCOUT → PCIN</code> cascade",
      c: "dedicated silicon",
      _tone: "good",
    },
    {
      l: "L1",
      s: "K = 32",
      d: "int",
      m: "CU-to-CU, over the DSP's <code>C</code> port",
      c: "one 48-bit bus",
      _tone: "good",
    },
    {
      l: "L2",
      s: "K = 32n",
      d: "FP",
      m: "accumulator CU, one adder",
      c: "one add per 32 MACs",
    },
    {
      l: "L3",
      s: "unbounded",
      d: "FP",
      m: "accumulator to accumulator",
      c: "one packet per tile",
    },
  ],
};

/* Two int7 MACs per DSP48E2, sharing one activation through the pre-adder. */
const packing = {
  nodes: [
    {
      id: "a",
      x: 0,
      y: 0,
      w: 15,
      label: "A (27) = w1 << 19",
      sub: "pure wiring, no logic",
    },
    {
      id: "d",
      x: 0,
      y: 5,
      w: 15,
      label: "D (27) = w0",
      sub: "pure wiring, no logic",
    },
    {
      id: "pre",
      x: 19,
      y: 2.5,
      w: 12,
      label: "pre-adder",
      sub: "w1·2^19 + w0",
      accent: true,
    },
    {
      id: "mul",
      x: 35,
      y: 2.5,
      w: 12,
      label: "multiply",
      sub: "27 x 18",
      accent: true,
    },
    {
      id: "b",
      x: 35,
      y: 9,
      w: 12,
      label: "B (18) = a",
      sub: "one activation, shared",
    },
    {
      id: "p",
      x: 51,
      y: 2.5,
      w: 16,
      label: "P (48)",
      sub: "two products, disjoint fields",
      accent: true,
    },
  ],
  edges: [
    { from: "a:r", to: "pre:l", dir: "h" },
    { from: "d:r", to: "pre:l", dir: "h" },
    { from: "pre:r", to: "mul:l", dir: "h", accent: true },
    { from: "b:t", to: "mul:b", dir: "v" },
    { from: "mul:r", to: "p:l", dir: "h", accent: true },
  ],
};

const pField = [
  { name: "sign extension", bits: 15, value: "" },
  {
    name: "sum of w1·a",
    bits: 14,
    value: "upper output — row i+1",
    accent: true,
  },
  {
    name: "sum of w0·a",
    bits: 19,
    value: "lower output — row i",
    accent: true,
  },
];

const sChoice = {
  cols: [
    { key: "s", label: "S", mono: true },
    { key: "f", label: "packs in 27 b?" },
    { key: "g", label: "guard", mono: true, align: "right" },
    { key: "d", label: "cascade depth", mono: true, align: "right" },
  ],
  rows: [
    {
      s: "<b>19</b>",
      f: "yes — <code>-33,554,496</code> of <code>-67,108,864</code>",
      g: "5",
      d: "<b>32</b>",
      _tone: "good",
    },
    {
      s: "20",
      f: "<b>no</b> — <code>-67,108,928</code> overflows by 64",
      g: "6",
      d: "—",
      _tone: "bad",
    },
  ],
};

/* The cluster: four tensor CUs chained into one accumulator.
 * Geometry note: the TCU gaps are 3 units wide so an edge label fits BETWEEN
 * two boxes rather than on them, and the group's top edge sits above the
 * operand row so no wire crosses its caption. */
const cluster = {
  nodes: [
    { id: "o0", x: 0, y: 0, w: 11, h: 2.6, label: "A0 B0", sub: "K 0..7 · delay 0" },
    { id: "o1", x: 14, y: 0, w: 11, h: 2.6, label: "A1 B1", sub: "K 8..15 · delay 2" },
    { id: "o2", x: 28, y: 0, w: 11, h: 2.6, label: "A2 B2", sub: "K 16..23 · delay 4" },
    { id: "o3", x: 42, y: 0, w: 11, h: 2.6, label: "A3 B3", sub: "K 24..31 · delay 6" },
    {
      id: "t0",
      x: 0,
      y: 6,
      w: 11,
      h: 4,
      label: "TCU 0",
      sub: "4x8x4 · 64 DSP · W = 0",
    },
    {
      id: "t1",
      x: 14,
      y: 6,
      w: 11,
      h: 4,
      label: "TCU 1",
      sub: "4x8x4 · 64 DSP · W = C",
    },
    {
      id: "t2",
      x: 28,
      y: 6,
      w: 11,
      h: 4,
      label: "TCU 2",
      sub: "4x8x4 · 64 DSP · W = C",
    },
    {
      id: "t3",
      x: 42,
      y: 6,
      w: 11,
      h: 4,
      label: "TCU 3",
      sub: "4x8x4 · 64 DSP · W = C",
    },
    {
      id: "acu",
      x: 57,
      y: 6,
      w: 13,
      h: 4,
      label: "accumulator",
      sub: "FP22 · resident tile",
      accent: true,
    },
    {
      id: "mesh",
      x: 57,
      y: 13,
      w: 13,
      label: "mesh port",
      sub: "the only one in the cluster",
      accent: true,
    },
  ],
  edges: [
    { from: "o0:b", to: "t0:t", dir: "v" },
    { from: "o1:b", to: "t1:t", dir: "v" },
    { from: "o2:b", to: "t2:t", dir: "v" },
    { from: "o3:b", to: "t3:t", dir: "v" },
    { from: "t0:r", to: "t1:l", dir: "h", accent: true, label: "→ W" },
    { from: "t1:r", to: "t2:l", dir: "h", accent: true, label: "→ W" },
    { from: "t2:r", to: "t3:l", dir: "h", accent: true, label: "→ W" },
    {
      from: "t3:r",
      to: "acu:l",
      dir: "h",
      accent: true,
      label: "16 × int",
    },
    { from: "acu:b", to: "mesh:t", dir: "v", accent: true },
  ],
  groups: [
    {
      x: -1.2,
      y: -1.2,
      w: 55.4,
      h: 12.6,
      label:
        "mx_cluster_core — 256 DSP, zero fabric adders, and no port to the mesh at all",
    },
  ],
};

/* Operand skew, as mx_tcu.v builds it: stage 0 takes the operands undelayed and
 * stage k reads a k-deep shift register. One register per STAGE carries the
 * whole 56-bit bundle, not 32 separate 7-bit lines. */
const CYC = 12;
const TILES = 4;
const skewRows = Array.from({ length: 8 }, (_, k) => ({
  name: k === 0 ? "k=0 · no delay" : `k=${k} · ${k}-deep sr`,
  kind: "bus",
  values: Array.from({ length: CYC }, (_, c) =>
    c >= k && c < k + TILES ? `T${c - k}` : undefined,
  ),
}));
skewRows.push({
  name: "P out of k=7",
  kind: "bus",
  values: Array.from({ length: CYC }, (_, c) =>
    c >= 8 && c < 8 + TILES ? `sum T${c - 8}` : undefined,
  ),
  mark: [8],
});

const skewNotes = [
  {
    cycle: 0,
    text: "Stage k's operands must arrive k cycles after stage 0's, because the cascade adds one pipeline stage per DSP. The delay is explicit: stage 0 reads the operand bundle directly and stage k reads the end of a k-deep shift register.",
  },
  {
    cycle: 8,
    text: "One shift register serves a whole STAGE — four rows of A and four columns of B at that k, 56 bits — rather than 32 narrow 7-bit lines. That is what makes the delay lines eight objects per tensor CU instead of 256.",
  },
  {
    cycle: 11,
    text: "Stage k's P lands at t + k + 4, so stage 7 completes at t + 11. Four tiles are in flight down the chain at once, which is the only condition under which the per-stage skew is exercised at all.",
    tone: "good",
  },
];

/* Where the skew is HELD is a separate decision from how deep it is. */
const skewStore = {
  cols: [
    { key: "s", label: "held in", mono: true },
    { key: "c", label: "what a depth-k line costs" },
    { key: "b", label: "what the shipping RTL does" },
  ],
  rows: [
    {
      s: "SRL16E / SRL32",
      c: "<b>one LUT per bit at ANY depth.</b> These lines are 2 to 7 deep inside a tensor CU and 2, 4 and 6 deep between them, so an SRL16E pays a whole LUT to use at most 7 of its 16 stages",
      b: "not used",
      _tone: "warn",
    },
    {
      s: "<b>flip-flops</b>",
      c: "one FF per bit per stage — more registers, and no LUT at all",
      b: "<code>mx_tcu.v</code> and <code>mx_cluster_core.v</code> both carry <code>(* shreg_extract = &quot;no&quot; *)</code> on the delay arrays, so synthesis is forbidden to infer an SRL",
      _tone: "good",
    },
  ],
};

/* The K sweep: one step per K block, gm=8 gn=8 nk=4 (32x128x32). */
const sweep = [
  {
    title: "K block 0 — the tile opens",
    kb: 0,
    depth: 1,
    cmd: "LOAD",
    issued: 64,
    note: "The acc bit is 0, so the 64 issues with kb = 0 are LOAD: they overwrite the resident tile rather than adding into it. Operands are L1A[g·4 + 0] and L1B[h·4 + 0] — one A entry and one B entry read in the same cycle, which is why A and B are separate 928-bit RAMs.",
  },
  {
    title: "K block 1 — ADD",
    kb: 1,
    depth: 2,
    cmd: "ADD",
    issued: 128,
    note: "New operands, same 64 tile addresses. K is the OUTERMOST loop, so a tile address recurs every gm·gn = 64 cycles rather than every cycle — which is what lets the accumulator be a plain memory with a synchronous read.",
  },
  {
    title: "K block 2 — ADD",
    kb: 2,
    depth: 3,
    cmd: "ADD",
    issued: 192,
    note: "Nothing has left the cluster. Output traffic so far: zero bytes. The tile is working storage, not a staging pipe.",
  },
  {
    title: "K block 3 — ADD, and emit",
    kb: 3,
    depth: 4,
    cmd: "ADD_EMIT",
    issued: 256,
    note: "A sub-tile's LAST accumulation already computes its finished value at stage 5. GEMM.emit fuses the drain into it: the same command writes the tile back AND hands the value out, no re-read, no extra command slot — and a separate drain could never overlap a sweep, because a sweep issues a command every cycle and has none spare.",
  },
  {
    title: "DRAIN fuse — these already left",
    kb: null,
    depth: 4,
    cmd: "—",
    issued: 256,
    drained: true,
    note: "DRAIN.fuse means “these already left; wait for them”, and it does not wait for the sweep — so one tile's results can still be draining while the next tile's sweep runs. 64 emits collect into 8 bursts of 8: 72 flits, not 128.",
  },
];

const elementWidth = {
  cols: [
    { key: "e", label: "element", mono: true },
    { key: "p", label: "product", mono: true, align: "right" },
    { key: "k", label: "packs", mono: true, align: "right" },
    { key: "s", label: "S", mono: true, align: "right" },
    { key: "g", label: "guard", mono: true, align: "right" },
    { key: "d", label: "depth", mono: true, align: "right" },
    { key: "m", label: "MAC/cycle at 12,288 DSP", mono: true, align: "right" },
  ],
  rows: [
    { e: "int8", p: "16 b", k: "2", s: "19", g: "3", d: "8", m: "24,576" },
    {
      e: "<b>int7</b>",
      p: "<b>14 b</b>",
      k: "<b>2</b>",
      s: "<b>19</b>",
      g: "<b>5</b>",
      d: "<b>32</b>",
      m: "<b>24,576</b>",
      _tone: "good",
    },
    { e: "int6", p: "12 b", k: "2", s: "19", g: "7", d: "128", m: "24,576" },
    {
      e: "int4",
      p: "8 b",
      k: "3",
      s: "11",
      g: "3",
      d: "8",
      m: "<b>36,864</b>",
      _tone: "warn",
    },
  ],
};

const memories = {
  cols: [
    { key: "m", label: "memory" },
    { key: "w", label: "width", mono: true },
    { key: "p", label: "primitive" },
    { key: "l", label: "read latency", mono: true, align: "right" },
  ],
  rows: [
    {
      m: "<code>u_l1a</code> — the A operand",
      w: "<b>928 bits</b>",
      p: "named, not inferred",
      l: "1",
    },
    {
      m: "<code>u_l1b</code> — the B operand",
      w: "<b>928 bits</b>",
      p: "named, not inferred",
      l: "1",
    },
    {
      m: "the accumulator tile, one per node",
      w: "352 bits at <code>ACC_MW = 14</code>",
      p: "named, not inferred",
      l: "<b>2</b>",
    },
  ],
};

const opcodes = {
  cols: [
    { key: "o", label: "opcode", mono: true },
    { key: "f", label: "fields", mono: true },
    { key: "d", label: "does" },
  ],
  rows: [
    {
      o: "<b>FILL</b>",
      f: "addr, n, sel",
      d: "load n L1 entries from memory — <b>one</b> memory flit carrying {base, count}, not n of them; the agent walks the address sequence itself and returns count x 4 responses each tagged {entry, word}, so arrival order carries no meaning",
    },
    {
      o: "<b>GEMM</b>",
      f: "gm, gn, nk, anchor, acc",
      d: "sweep gm x gn output sub-tiles over nk K blocks",
    },
    {
      o: "<b>DRAIN</b>",
      f: "addr, n, fuse, last",
      d: "get n resident sub-tiles out — to memory, to another mesh node, or to another mesh entirely",
    },
  ],
};

const acuCommands = {
  cols: [
    { key: "v", label: "value", mono: true, align: "right" },
    { key: "n", label: "name", mono: true },
    { key: "b", label: "built" },
    { key: "e", label: "effect", mono: true },
  ],
  rows: [
    { v: "0", n: "NOP", b: "yes", e: "nothing" },
    { v: "1", n: "LOAD", b: "yes", e: "tile[addr] = chain" },
    { v: "2", n: "ADD", b: "yes", e: "tile[addr] += chain" },
    { v: "3", n: "ADD_PEER", b: "yes", e: "tile[addr] += peer_in" },
    { v: "4", n: "SEND", b: "yes", e: "peer_out = tile[addr]" },
    { v: "5", n: "EMIT", b: "yes", e: "emit_out = fp16(tile[addr])" },
    { v: "6", n: "FWD", b: "<b>no</b>", e: "peer_out = chain", _tone: "bad" },
    {
      v: "7",
      n: "ADD_EMIT",
      b: "yes",
      e: "tile[addr] += chain, <b>and</b> emit the result",
      _tone: "good",
    },
  ],
};

/* Reporting a sweep done at the wrong moment: broken, then fixed. */
const doneBroken = {
  rows: [
    {
      name: "sweep counters",
      kind: "bus",
      values: ["g7 h5", "g7 h6", "g7 h7", "wrapped", "—", "—", "—"],
    },
    {
      name: "results still in the cascade",
      kind: "bus",
      values: ["19", "19", "19", "19", "18", "17", "16"],
    },
    {
      name: "done reported",
      kind: "bit",
      values: [0, 0, 0, 1, 1, 1, 1],
      mark: [3],
    },
    {
      name: "DRAIN holds the control mux",
      kind: "bit",
      values: [0, 0, 0, 0, 1, 1, 1],
    },
    {
      name: "tile",
      kind: "text",
      values: [
        "write",
        "write",
        "write",
        "write",
        "cut off",
        "cut off",
        "cut off",
      ],
    },
  ],
  notes: [
    {
      cycle: 3,
      text: "Not “the last tile has been issued”. The cascade is ~19 cycles deep, so when the counters finish there are still that many results in flight.",
      tone: "bad",
    },
    {
      cycle: 4,
      text: "DRAIN takes the accumulator's control port the cycle it starts — it is an explicit mux, not an arbiter — and cuts the tail off. The tail sub-tiles came back as zeros, which looks exactly like a compute bug.",
      tone: "bad",
    },
  ],
};

const doneFixed = {
  rows: [
    {
      name: "commands issued",
      kind: "bus",
      values: ["254", "255", "256", "256", "256", "256", "256"],
    },
    {
      name: "results retired",
      kind: "bus",
      values: ["236", "237", "238", "…", "255", "256", "256"],
    },
    {
      name: "busy = issued ≠ retired",
      kind: "bit",
      values: [1, 1, 1, 1, 1, 0, 0],
      mark: [5],
    },
    {
      name: "DRAIN holds the control mux",
      kind: "bit",
      values: [0, 0, 0, 0, 0, 1, 1],
    },
    {
      name: "tile",
      kind: "text",
      values: ["write", "write", "write", "write", "write", "read", "read"],
    },
  ],
  notes: [
    {
      cycle: 5,
      text: "Counting issued-minus-retired is exact and owes nothing to FIFO timing.",
      tone: "good",
    },
    {
      cycle: 5,
      text: "busy also has to cover the REUSE_MIN gap after the write, not only the pipeline: taking the mux means issuing an EMIT that reads an address an in-flight command may be about to write.",
      tone: "good",
    },
  ],
};

const notBuilt = {
  cols: [
    { key: "t", label: "not built" },
    { key: "w", label: "what it would be" },
  ],
  rows: [
    {
      t: "<b>chain bypass</b>",
      w: "a mux on the chain input, letting one cluster act either as a fused 4x32x4 unit or as four independent 4x8x4 units, so the chain is not dead silicon when a workload does not want K=32. Cheap to build in, and worth it; nothing depends on it today.",
      _tone: "warn",
    },
    {
      t: "<b><code>FWD</code></b>",
      w: "the accumulator op that would pass a chain result straight to a peer without accumulating locally. It has no command source. Peer transfer works, but through the drain queue rather than on a direct accumulator-to-accumulator wire.",
      _tone: "warn",
    },
  ],
};
</script>

<template>
  <DocPage
    title="The matmul cluster"
    summary="Two int7 MACs per DSP48E2 sharing an activation through the pre-adder, a cascade that reduces K=32 without touching the fabric, and a K sweep that reads one pair of operands and spends it on 32 multiply-accumulates."
    domain="tpu"
    status="shipped"
    source="xcvu13p-fhgb2104-2L-e, Vivado 2024.2, out-of-context synthesis · docs/projects/kohakutpu/matmul.md · isa.md"
  >
    <p class="doc-p">
      A <b>cluster</b> is four tensor CUs chained into an accumulator; a tensor
      CU is a 4x8x4 block of 64 DSP48E2s. Per cycle a cluster computes
      <code>4 x 32 x 4</code> — 512 MACs, 1,024 FLOP. The device is UltraScale+,
      so <b>DSP48E2</b> — not DSP58, which has native INT8 SIMD this part does
      not have. Everything below is arithmetic arranged to fit a primitive that
      was not designed for it.
    </p>

    <h2 class="doc-h2">Systolic in the small, mesh in the large</h2>

    <p class="doc-p">
      A systolic array is very good at one thing: making accumulation free.
      Partial sums move through dedicated links and are added where they land,
      so there is no adder tree and no accumulator storage in the hot path. It
      is bad at everything else — rigid shape, long fill and drain, poor
      utilisation on small or irregular matrices. A mesh is the opposite:
      flexible composition, independent nodes, graceful scaling, at the cost of
      packet overhead per hop. This design takes the accumulation mechanism from
      the first and the composition model from the second, and puts the boundary
      <b>where a dedicated wire stops being cheaper than a packet</b>. That
      boundary sits at K = 32 — the same number as the block size of the number
      format, which is one parameter wearing two names.
    </p>

    <SpecTable
      :cols="hierarchy.cols"
      :rows="hierarchy.rows"
      caption="L0 and L1 are exact integer — no alignment, no normalisation, no rounding — because every product inside a K=32 block carries the same block scale. L2 is where floating point first appears, reached once per 32 MACs, so a comparatively expensive adder is amortised into irrelevance."
    />

    <h2 class="doc-h2">Two MACs per DSP, and the packing offset</h2>

    <p class="doc-p">
      A DSP48E2 computes <code>(A + D) * B + {C | PCIN}</code>. The pre-adder is
      the lever: two int7 weights are laid at different bit positions in the
      same 27-bit operand, and one shared activation multiplies both at once.
    </p>

    <Fig
      caption="No fabric adder forms the packed operand: the pre-adder does it, and both port loads are wiring. Measured per mx_mac: 0 LUT, 0 FF, 1 DSP."
      zoom
      wide
    >
      <BlockDiagram :nodes="packing.nodes" :edges="packing.edges" />
    </Fig>

    <p class="doc-p">
      int7 is signed, so elements are <code>[-64, +63]</code>, and two
      constraints fight each other. The packed operand must fit 27 bits signed —
      worst case <code>w1 = w0 = -64</code> — and the lower product field needs
      guard bits above the 14-bit product width so many products can be summed
      into it before it overflows into its neighbour. Fields do not overlap for
      <code>S ≥ 14</code>, and the accumulation depth is
      <code>guard = S - 14</code>.
    </p>

    <SpecTable
      :cols="sChoice.cols"
      :rows="sChoice.rows"
      caption="S = 20 does not work: -64 · 2^20 = -2^26 exactly consumes the range, and the second weight's -64 pushes it over. S = 19 is therefore the maximum, and it gives exactly the depth needed — cascade depth 32 is exactly one K=32 block, so nothing has to be extracted mid-cluster."
    />

    <BitField
      :fields="pField"
      caption="The 48-bit P field map. Sum of 32 products lands in [-129,024, +131,072] — 18 bits and a sign — against the lower field's ±262,144, so about one bit of margin remains"
    />

    <p class="doc-p">
      Extraction happens once per K=32 block, per chain:
      <code>lower = signed(P[18:0])</code>, which is wiring, and
      <code>upper = P[47:19] + P[18]</code>, a 29-bit increment. The
      <code>+ P[18]</code> is the borrow correction — the whole 48-bit word is
      one two's complement accumulation, so a negative lower sum borrows from
      the upper field, and adding back its sign bit undoes that. It is the only
      arithmetic in the extract path, and the bench forces the lower field
      negative on all eight chains specifically to exercise it.
    </p>

    <Callout
      kind="note"
      title="The guard-bit budget agrees with the payload width"
    >
      <p>
        Seven bits rather than eight is settled twice over from opposite
        directions: the guard budget here, and the operand payload on the
        numbers page, where
        <code>32 x 7 = 224</code> plus <code>4 x 8 = 32</code> fills a 256-bit
        flit exactly.
      </p>
    </Callout>

    <h2 class="doc-h2">The cluster, and crossing CU boundaries for free</h2>

    <p class="doc-p">
      The cascade <code>PCOUT → PCIN</code> only reaches a physically adjacent
      DSP in the same column, so a 32-deep chain spanning four CUs would force
      all four to be adjacent — a floorplanning constraint worth avoiding. It is
      not needed, because <b>the <code>C</code> port is unused</b>: integer
      elements have no implied leading 1, so none of the
      <code>(1+Ma)(1+Mb)</code> correction an FP8 design needs exists here, and
      <code>C</code>
      is free to carry the upstream partial.
    </p>

    <Callout
      kind="trap"
      title="The upstream partial enters the LAST stage on W, not CU entry on Z"
    >
      <p>
        Both choices reach the same claim — zero fabric adders across the whole
        K = 32 — and they cost completely different amounts of skew, so the
        stage is the decision and the port is only its consequence.
      </p>
      <p>
        <b>On <code>Z</code> at stage 0</b>, the upstream CU's result would have
        to be ready <i>before this CU starts</i>, which is
        <b>eight cycles of operand delay per tensor CU</b> — the whole depth of
        the chain, four times over.
        <b>On <code>W</code> at the last stage</b> the two results are
        contemporary and need only the alignment the <code>C</code> register
        itself imposes: <b>two cycles per CU</b>, so the operand delays are 0,
        2, 4, 6 and the core is <code>11 + 2·(NTCU−1) = 17</code> cycles deep.
      </p>
      <p>
        Assuming one cycle instead of two is the symptom to know:
        <b>half of every tile's products vanish</b> and the other half are
        summed against the previous tile, because the downstream stage samples
        its neighbour a cycle early — when the neighbour still holds the
        previous tile's value, or zero on the first tile.
        <b>Nothing errors</b>, and with stable operands nothing is even wrong.
        The circuit is on
        <RouterLink to="/tpu/matmul/microarchitecture" class="doc-link"
          >the microarchitecture page</RouterLink
        >.
      </p>
    </Callout>

    <Fig
      caption="Inside a tensor CU the reduction is Z = PCIN. The upstream CU's partial enters on W, at the LAST stage of the chain — not at CU entry. OPMODE selects Z from {0, PCIN, P, C, …}, W from {0, C, …} and X/Y from the multiplier, so M + PCIN + C is one DSP operation: the entire K=32 accumulation across all four CUs costs zero fabric adders, and each CU stays an independent 8-deep cascade that can be placed on its own."
      zoom
      wide
    >
      <BlockDiagram
        :nodes="cluster.nodes"
        :edges="cluster.edges"
        :groups="cluster.groups"
      />
    </Fig>

    <p class="doc-p">
      Because all four CUs share one block scale, the chain input and output are
      plain integers — a CU adds its 16 partials to the 16 arriving from
      upstream and passes them on. No scale travels with them. The supported
      shape is exactly what that structure implies:
      <code>M = 4a</code>, <code>N = 4b</code>, <code>K = 32c</code>. And
      <b>the chain is only 4 deep</b>, so fill and drain are 4 cycles rather
      than the hundreds a monolithic systolic array would need — which is what
      keeps the cluster usable on small matrices, and the concrete payoff of
      putting the boundary at K=32 rather than at the whole problem.
    </p>

    <h3 class="doc-h3">Operand skew, and what holds it</h3>

    <p class="doc-p">
      A cascade adds one pipeline stage per DSP, so the operand for stage
      <code>k</code> must arrive <code>k</code> cycles after stage 0 — eight
      stages of skew on 7-bit operands, and another two per tensor CU on top of
      that.
    </p>

    <WaveTrace
      :rows="skewRows"
      :notes="skewNotes"
      label="operand skew down one 8-deep chain, four tiles streaming"
    />

    <SpecTable
      :cols="skewStore.cols"
      :rows="skewStore.rows"
      caption="A shift register is not a memory — no address, no read port — so unlike a BRAM or URAM its latency is not a tool heuristic and there is no correctness reason to name a primitive. The reason to name one anyway is area, and it points the other way from the usual advice: the shallow, wide lines a DSP cascade needs are exactly the shape an SRL is bad at."
    />

    <Callout kind="measured" title="How much of a cluster the skew is">
      <p>
        Reading the earlier, SRL-built cluster's own breakdown: 224 SRL of TCU
        0's 336 LUT, 280 each in TCUs 1, 2 and 3, and 394 of the top-level
        operand delay's 450 — <b>1,458 LUT of shift register</b> in a cluster
        measuring 4,751. That is the size of the prize the flop swap plays for,
        and it is <b>31% of that cluster's LUTs</b> spent on nothing but making
        operands arrive on the right cycle.
      </p>
      <p>
        Both numbers are out-of-context synthesis of <code>mx_cluster</code> on
        <code>xcvu13p-fhgb2104-2L-e</code>, Vivado 2024.2, against a 300 MHz
        target. The per-component rows sum to the parent exactly — 336 + 448 +
        476 + 476 + 450 + 2,565 = 4,751 LUT, and 256 DSP — which is what makes
        them a breakdown rather than rows from different runs.
        <b
          >The flop-built cluster has not been re-measured as a matched pair,
          so no LUT figure for the swap is quoted here.</b
        >
      </p>
    </Callout>

    <h2 class="doc-h2">
      The K sweep: one operand read, 32 multiply-accumulates
    </h2>

    <p class="doc-p">
      The arithmetic intensity of an output tile is
      <code>M·N·K / (M·K + K·N) = M·N / (M + N)</code>.
      <b>K cancels.</b> Chaining more compute units raises MACs and operand
      demand together, so the cluster chain buys DSP density and shared control
      and buys no bandwidth at all — only M and N do. That is why the output
      tile stays resident while K is swept past it, and it is why one pair of
      operand reads is worth K multiply-accumulates rather than one.
    </p>

    <StepPlayer
      :steps="sweep"
      label="GEMM gm=8 gn=8 nk=4 — C[32,32] = A[32,128] @ B.T[32,128]"
    >
      <template #default="{ state }">
        <div class="grid gap-5 sm:grid-cols-[auto_1fr] items-start">
          <div>
            <div
              class="kt-text-micro uppercase tracking-wider text-warm-400 dark:text-warm-600 mb-2"
            >
              resident output tile — 64 sub-tiles
            </div>
            <div class="grid grid-cols-8 gap-1 w-max">
              <div
                v-for="c in 64"
                :key="c"
                class="w-4 h-4 rounded-sm bg-gem"
                :style="{ opacity: 0.14 + 0.86 * (state.depth / 4) }"
              />
            </div>
            <div
              class="kt-text-micro font-mono text-warm-400 dark:text-warm-600 mt-2"
            >
              {{
                state.drained
                  ? "drained · never re-read"
                  : `${state.depth} of 4 K blocks accumulated`
              }}
            </div>
          </div>

          <div class="min-w-0">
            <div
              class="kt-text-micro uppercase tracking-wider text-warm-400 dark:text-warm-600 mb-2"
            >
              L1 entries this block
            </div>
            <div class="flex flex-wrap gap-1.5 mb-4">
              <span
                v-for="b in 4"
                :key="b"
                class="gem-badge font-mono"
                :class="
                  state.kb === b - 1
                    ? 'bg-gem text-white'
                    : 'bg-warm-100 dark:bg-warm-800 text-warm-400 dark:text-warm-600'
                "
              >
                K{{ b - 1 }}
              </span>
            </div>

            <dl
              class="grid grid-cols-2 gap-x-4 gap-y-2 kt-text-caption max-w-[36rem]"
            >
              <dt class="text-warm-500 dark:text-warm-400">
                accumulator command
              </dt>
              <dd class="font-mono text-warm-800 dark:text-warm-200">
                {{ state.cmd }}
              </dd>
              <dt class="text-warm-500 dark:text-warm-400">
                commands issued, cumulative
              </dt>
              <dd class="font-mono text-warm-800 dark:text-warm-200">
                {{ state.issued }} of 256
              </dd>
              <dt class="text-warm-500 dark:text-warm-400">
                operand words read
              </dt>
              <dd class="font-mono text-warm-800 dark:text-warm-200">
                {{ state.kb === null ? "0" : "8 A + 8 B entries" }}
              </dd>
              <dt class="text-warm-500 dark:text-warm-400">
                C bytes leaving the cluster
              </dt>
              <dd class="font-mono text-warm-800 dark:text-warm-200">
                {{ state.depth === 4 ? "2,048 — once" : "0" }}
              </dd>
            </dl>
          </div>
        </div>
      </template>
    </StepPlayer>

    <Callout
      kind="rule"
      title="Two different loops both mention K and they nest the opposite way"
    >
      <p>
        <b>Across chunks</b> the driver puts K innermost, so the output tile
        stays resident. <b>Within one <code>GEMM</code></b> the K-block sweep is
        outermost over sub-tiles, so a tile address recurs every
        <code>gm·gn</code> cycles. They are different levels and both are
        load-bearing.
      </p>
    </Callout>

    <p class="doc-p">
      The <code>acc</code> bit is what makes the first K block of a sweep an
      <code>ADD</code> rather than a <code>LOAD</code>. Without it every
      <code>GEMM</code> starts by overwriting the resident tile, so an output
      tile could only ever be produced by one instruction, and a K longer than
      L1 could not be expressed at all. With it, K is split into chunks that
      chain into the same resident tile, and the tile is written to memory once
      rather than once per chunk — the difference between <code>M·N</code> of
      result traffic and <code>M·N·(K/Kc)</code> in both directions, which at
      K=4096 with a 128-element chunk is 32x.
    </p>

    <h2 class="doc-h2">Three opcodes, one payload</h2>

    <SpecTable :cols="opcodes.cols" :rows="opcodes.rows" />

    <p class="doc-p">
      One 256-bit payload each, with the opcode in <code>[255:252]</code>. Two
      things about the layout are worth more than the field list.
      <b>No field's meaning depends on the opcode</b> — the payload has 87 spare
      bits and no reason to overlap anything. And
      <b>field widths matter more than they look</b>.
    </p>

    <Callout kind="trap" title="Two ways this payload has already gone wrong">
      <p>
        When the entry count had to grow from 8 bits to 16 — because the
        resident tile reached 512 sub-tiles and a <code>DRAIN</code> naming 512
        wrapped to 0, silently draining the beginning of the tile a second time
        — every field below it moved <b>down</b> rather than sharing bits with
        <code>gm</code>/<code>gn</code> on the grounds that a
        <code>FILL</code> never uses those. A field whose meaning depends on the
        opcode is how a decode bug survives review.
      </p>
      <p>
        And an unsized value in the wrong place shifts every field below it and
        elaborates cleanly: an expression written straight into a concatenation
        contributed 32 bits rather than the 34 the address field is, the whole
        payload came out four bits short, and the cluster read nonsense and
        executed nothing — no memory traffic, no error anywhere.
      </p>
    </Callout>

    <h3 class="doc-h3">And underneath it, three bits</h3>

    <SpecTable
      :cols="acuCommands.cols"
      :rows="acuCommands.rows"
      caption="The accumulator's command set: three bits and a tile address, presented with a valid strobe. It never appears on the mesh — the manager issues it once per cycle of a sweep, and one GEMM flit expands into hundreds of these without carrying any of them"
    />

    <p class="doc-p">
      <code>ADD_PEER</code> and <code>SEND</code> were dead code that compiled
      for a long time: the accumulator implemented them and the node above tied
      <code>peer_in</code> to zero and left <code>peer_out</code> open, so
      <code>SEND</code> drove nothing and <code>ADD_PEER</code> added zero.
      <code>ADD_EMIT</code> removes the need for a command slot rather than
      finding one, and in the pipeline it is <code>ADD</code>'s operand selects
      with <code>EMIT</code>'s output — one term in three expressions and
      nothing at all in stage 3, the path the cluster binds on. The explicit
      <code>EMIT</code> stays, because an output tile drained without a
      completing sweep still needs it.
    </p>

    <h2 class="doc-h2">When is a sweep finished?</h2>

    <p class="doc-p">
      The instruction is done with the sequencer the moment the manager takes it
      — the sweep runs on for <code>gm·gn·nk</code> cycles afterwards and needs
      nothing more from the CU, so holding the instruction there only stopped
      the CU from filling the other half of L1. Measured, <code>FILL</code> was
      22.3% of the machine's time with the array idle through every cycle of it.
      What <i>is</i> finished is a separate question, and getting it wrong was
      expensive twice.
    </p>

    <WaveTrace
      :rows="doneBroken.rows"
      :notes="doneBroken.notes"
      variant="broken"
      label="done when the counters finish"
    />

    <WaveTrace
      :rows="doneFixed.rows"
      :notes="doneFixed.notes"
      variant="fixed"
      label="done when issued equals retired"
    />

    <Callout
      kind="trap"
      title="The other wrong answer: the command FIFO's empty flag"
    >
      <p>
        That flag deasserts two cycles after a push, so there is a hole where
        the block reads idle with commands still queued. Only a tiling short
        enough to finish inside that hole can hit it, which is why
        <b
          >every bench down to 3 sub-tiles passed and a 2-sub-tile one did
          not.</b
        >
      </p>
    </Callout>

    <h2 class="doc-h2">
      Five memories, and none of their shapes came from the framework
    </h2>

    <SpecTable
      :cols="memories.cols"
      :rows="memories.rows"
      caption="928 bits is a consequence of the format and the tile geometry: one entry is 4 lanes x 32 K elements at 7 bits, which is 896, plus four 8-bit block scales. Two separate RAMs rather than one, because a sweep reads an A entry and a B entry in the same cycle"
    />

    <Callout kind="rule" title="Every memory names its primitive">
      <p>
        The one thing here that <i>is</i> a convention rather than a free
        choice. It is not aesthetic: the accumulator's tile cost
        <b>22,845 LUT and missed timing at 287.3 MHz</b> while it was inferred
        LUTRAM, and the same array as a named block RAM with its output register
        enabled is <b>5 primitives at 349.4 MHz</b>.
      </p>
    </Callout>

    <h2 class="doc-h2">Element width is the throughput lever</h2>

    <SpecTable
      :cols="elementWidth.cols"
      :rows="elementWidth.rows"
      caption="Packing density is set by product width against the 27-bit A port and the 48-bit accumulator. The MAC/cycle column is arithmetic against the device's 12,288 DSP48E2, not a built configuration"
    />

    <p class="doc-p">
      <b>int8 buys nothing.</b> Same two packs per DSP as int7, but only depth 8
      — so a K=32 block would need draining four times, adding fabric work for
      one extra bit of precision. <b>int4 is 1.5x the throughput</b> at three
      packs per DSP, but depth 8 means extraction every K=8, and three rows per
      DSP maps awkwardly onto a 4-row tile: it wants an 8-row tile. That is a
      genuine option and it changes the tile geometry, not just a parameter, so
      it is a different machine rather than a setting.
    </p>

    <h2 class="doc-h2">Where the LUTs are not</h2>

    <p class="doc-p">
      The design's central claim was that accumulation would leave the fabric
      entirely. Measured, per <code>mx_mac</code>: <b>0 LUT, 0 FF, 1 DSP</b>.
      The multiply <i>and</i> the whole K=32 reduction happen inside the DSPs —
      the cascade for K=8, the <code>C</code>
      port across CUs — and the claim holds exactly.
    </p>

    <Callout
      kind="measured"
      title="What that did not predict is where the LUTs went instead"
    >
      <p>
        The datapath budget written before building came to
        <b>~4,200 LUT per cluster</b> (ESTIMATE); the built endpoint, including
        its manager, L1, sequencer and the framework's compute-unit port,
        measures roughly <b>four times that</b>, and essentially all of the
        difference is outside the datapath. The direction of the argument
        survives and the estimate does not — which is why the budget is not
        quoted as a utilisation figure.
      </p>
    </Callout>

    <h2 class="doc-h2">What is not built</h2>

    <SpecTable :cols="notBuilt.cols" :rows="notBuilt.rows" />
  </DocPage>
</template>
