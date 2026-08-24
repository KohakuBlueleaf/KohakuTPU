<script setup>
/* Residency and accumulators. Capacities are the built/shipping shapes on
 * xcvu13p-fhgb2104-2L-e; where a figure is a ceiling rather than a shape it says so. */

const tiers = {
  nodes: [
    {
      id: "dram",
      x: 0,
      y: 0,
      w: 42,
      h: 3.6,
      label: "DDR4 — 16 GiB on the card",
      sub: "4 GB per mesh, every master sees its own at offset 0",
    },
    {
      id: "mag",
      x: 0,
      y: 6,
      w: 26,
      h: 3.6,
      label: "system node — MAG half",
      sub: "burst engine · tagged responses. The descriptor walk and the MXFP7 quantiser slot are the node's control PE, not MAG",
      accent: true,
    },
    {
      id: "l2",
      x: 30,
      y: 6,
      w: 12,
      h: 3.6,
      label: "L2 / staging",
      sub: "NOT BUILT — an addon slot",
    },
    {
      id: "mesh",
      x: 0,
      y: 12,
      w: 42,
      h: 3.2,
      label: "mesh — one 256-bit word per cycle per port",
      accent: true,
    },
    {
      id: "l1c",
      x: 0,
      y: 18,
      w: 20,
      h: 4,
      label: "cluster L1",
      sub: "u_l1a + u_l1b · 928 bit · 512 entries/side · lat 1",
    },
    {
      id: "l1v",
      x: 22,
      y: 18,
      w: 20,
      h: 4,
      label: "vector L1",
      sub: "256 bit · 512 words · block RAM · no tags · lat 1",
    },
    {
      id: "tile",
      x: 0,
      y: 24,
      w: 20,
      h: 4,
      label: "accumulator tile",
      sub: "352 bit · 5 primitives · lat 2 · FP22",
      accent: true,
    },
    {
      id: "rf",
      x: 22,
      y: 24,
      w: 20,
      h: 4,
      label: "register file",
      sub: "3 mirrors · striped by lane · 3R1W",
    },
  ],
  edges: [
    { from: "dram:b", to: "mag:t", dir: "v" },
    { from: "mag:r", to: "l2:l", dir: "h", dash: true },
    {
      from: "mag:b",
      to: "mesh:t",
      dir: "v",
      accent: true,
      label: "quantise in transit",
    },
    { from: "mesh:b", to: "l1c:t", dir: "v", accent: true },
    { from: "mesh:b", to: "l1v:t", dir: "v", accent: true },
    {
      from: "l1c:b",
      to: "tile:t",
      dir: "v",
      accent: true,
      label: "512 MAC/cycle",
    },
    { from: "l1v:b", to: "rf:t", dir: "v", label: "convert on read" },
  ],
};

const ladder = {
  cols: [
    { key: "t", label: "tier" },
    { key: "c", label: "capacity" },
    { key: "w", label: "width", mono: true },
    { key: "p", label: "primitive · latency" },
  ],
  rows: [
    {
      t: "<b>DRAM</b>",
      c: "16 GiB on the card; <b>4 GB per mesh</b>, a hard per-shard budget",
      w: "—",
      p: "DDR4, one controller per SLR; a DDR4 interface cannot span SLRs",
    },
    {
      t: "<b>MAG</b>",
      c: "no storage — it is a walk, a burst engine and a transform slot",
      w: "—",
      p: "the quantiser lives here, not in the compute unit",
    },
    {
      t: "<b>L2 / staging</b>",
      c: "<b>not built</b>",
      w: "—",
      p: "an addon slot in the same agent; where the next structural decision about this machine gets made",
      _tone: "warn",
    },
    {
      t: "cluster L1 A and B",
      c: "512 entries per side in the shipping shape — 13 RAMB36 per port, 26 for the two",
      w: "928 bits",
      p: "named block RAM · read latency <b>1</b>",
    },
    {
      t: "<b>accumulator tile</b>",
      c: "512 sub-tiles on BRAM36, 4,096 on URAM288 — <b>5 primitives either way</b>",
      w: "352 bits",
      p: "named block RAM · read latency <b>2</b>",
      _tone: "good",
    },
    {
      t: "vector L1",
      c: "512 words, and the fill protocol's 9-bit tag cannot name an entry past 511",
      w: "256 bits",
      p: "block RAM · read latency <b>1</b>",
    },
    {
      t: "vector register file",
      c: "16 vector registers of up to 128 elements, striped across the lanes",
      w: "—",
      p: "3 mirrored memories: two block RAM, one LUTRAM",
    },
    {
      t: "vector instruction memory",
      c: "a program, loaded then entered",
      w: "32 bits",
      p: "distributed LUTRAM",
    },
  ],
};

const refill = {
  cols: [
    { key: "t", label: "output tile", mono: true },
    { key: "r", label: "refill", mono: true, align: "right" },
    { key: "i", label: "implication" },
  ],
  rows: [
    {
      t: "4 x 4",
      r: "256 el/cyc",
      i: "many ports, unaffordable",
      _tone: "bad",
    },
    { t: "16 x 16", r: "64 el/cyc", i: "workable" },
    {
      t: "64 x 64",
      r: "16 el/cyc",
      i: "about half of one port",
      _tone: "good",
    },
  ],
};

const demand = {
  cols: [
    { key: "t", label: "tile (Gm x Gn)", mono: true },
    { key: "w", label: "words/cycle needed", mono: true, align: "right" },
    { key: "p", label: "port supplies", mono: true, align: "right" },
    { key: "m", label: "margin", mono: true, align: "right" },
  ],
  rows: [
    { t: "8 x 8", w: "<b>1.000</b>", p: "1.0", m: "<b>none</b>", _tone: "bad" },
    { t: "16 x 16", w: "0.500", p: "1.0", m: "2.0x" },
    {
      t: "<b>16 x 32</b> (designed)",
      w: "<b>0.375</b>",
      p: "1.0",
      m: "<b>2.7x</b>",
      _tone: "good",
    },
    { t: "32 x 32", w: "0.250", p: "1.0", m: "4.0x" },
  ],
};

const primitives = {
  cols: [
    { key: "p", label: "primitive", mono: true },
    { key: "n", label: "primitives", mono: true, align: "right" },
    {
      key: "d",
      label: "depth that comes with them",
      mono: true,
      align: "right",
    },
    { key: "t", label: "best power-of-two tile", mono: true, align: "right" },
    { key: "i", label: "intensity", mono: true, align: "right" },
  ],
  rows: [
    { p: "BRAM36", n: "5", d: "512 sub-tiles", t: "16 x 32", i: "21.3" },
    {
      p: "URAM288",
      n: "5",
      d: "4,096 sub-tiles",
      t: "64 x 64",
      i: "64.0",
      _tone: "good",
    },
  ],
};

const fp22 = [
  { name: "S", bits: 1, value: "sign" },
  { name: "E", bits: 7, value: "bias 63", accent: true },
  { name: "M", bits: 14, value: "ACC_MW, implicit leading 1" },
];

const pipeline = {
  cols: [
    { key: "s", label: "stage", mono: true },
    { key: "d", label: "does" },
  ],
  rows: [
    {
      s: "1",
      d: "extract the two packed fields per chain, apply the scale product",
    },
    { s: "2a", d: "leading-one search → one-hot 2^k (log depth)" },
    { s: "2a2", d: "the normalising shift, as 2 DSP multiplies per lane" },
    { s: "2b", d: "round and assemble → accumulator float" },
    {
      s: "3",
      d: "<b>read the tile</b>, compare exponents, align",
      _tone: "warn",
    },
    { s: "4", d: "add, leading-one search, shift" },
    { s: "5", d: "round, assemble, <b>write back</b>", _tone: "warn" },
    { s: "6", d: "(<code>EMIT</code> only) convert to FP16" },
  ],
};

/* The pacing contract: K inner, then K outer. */
const paceBroken = {
  rows: [
    {
      name: "tile address",
      kind: "bus",
      values: ["t0", "t0", "t0", "t0", "t0", "t0"],
      mark: [1, 2, 3, 4, 5],
    },
    {
      name: "K block",
      kind: "bus",
      values: ["k0", "k1", "k2", "k3", "k4", "k5"],
    },
    {
      name: "cycles since same address",
      kind: "bus",
      values: ["—", "1", "1", "1", "1", "1"],
    },
    { name: "REUSE_MIN = 5 met", kind: "bit", values: [1, 0, 0, 0, 0, 0] },
  ],
  notes: [
    {
      cycle: 1,
      text: "A pipelined adder cannot close a single-cycle accumulate loop: the result of cycle N is not available to cycle N+1.",
      tone: "bad",
    },
    {
      cycle: 3,
      text: "Surviving that recurrence cost three rotating banks, a two-step fold on EMIT, and a per-address zero mask so LOAD could clear the other banks in time. All of it was machinery to survive a loop order.",
      tone: "bad",
    },
  ],
};

const paceFixed = {
  rows: [
    {
      name: "tile address",
      kind: "bus",
      values: ["t0", "t1", "t2", "…", "t63", "t0"],
      mark: [0, 5],
    },
    {
      name: "K block",
      kind: "bus",
      values: ["k0", "k0", "k0", "k0", "k0", "k1"],
    },
    {
      name: "cycles since same address",
      kind: "bus",
      values: ["—", "—", "—", "—", "—", "64"],
    },
    { name: "REUSE_MIN = 5 met", kind: "bit", values: [1, 1, 1, 1, 1, 1] },
  ],
  notes: [
    {
      cycle: 5,
      text: "K outermost makes an address recur every Gm·Gn cycles — 64 at the smallest useful tiling and 512 at the balanced one. There is no tight recurrence left, so read latency is free and one bank suffices: 3 banks → 1 (4x less tile memory), the EMIT fold deleted, the zero mask deleted.",
      tone: "good",
    },
  ],
};

/* Peer reduction topologies. */
const peer = {
  nodes: [
    { id: "c0", x: 0, y: 0, w: 10, h: 2.6, label: "ACC0" },
    { id: "c1", x: 13, y: 0, w: 10, h: 2.6, label: "ACC1" },
    { id: "c2", x: 26, y: 0, w: 10, h: 2.6, label: "ACC2" },
    { id: "c3", x: 39, y: 0, w: 10, h: 2.6, label: "ACC3" },
    { id: "cr", x: 52, y: 0, w: 11, h: 2.6, label: "result", accent: true },
    { id: "t0", x: 0, y: 7.5, w: 10, h: 2.6, label: "ACC0" },
    { id: "t1", x: 0, y: 10.5, w: 10, h: 2.6, label: "ACC1" },
    { id: "t2", x: 0, y: 13.5, w: 10, h: 2.6, label: "ACC2" },
    { id: "t3", x: 0, y: 16.5, w: 10, h: 2.6, label: "ACC3" },
    { id: "u0", x: 22, y: 9, w: 10, h: 2.6, label: "ACC01" },
    { id: "u1", x: 22, y: 15, w: 10, h: 2.6, label: "ACC23" },
    { id: "tr", x: 44, y: 12, w: 11, h: 2.6, label: "result", accent: true },
  ],
  edges: [
    { from: "c0:r", to: "c1:l", dir: "h", accent: true },
    { from: "c1:r", to: "c2:l", dir: "h", accent: true },
    { from: "c2:r", to: "c3:l", dir: "h", accent: true },
    { from: "c3:r", to: "cr:l", dir: "h", accent: true },
    { from: "t0:r", to: "u0:l", dir: "h" },
    { from: "t1:r", to: "u0:l", dir: "h" },
    { from: "t2:r", to: "u1:l", dir: "h" },
    { from: "t3:r", to: "u1:l", dir: "h" },
    { from: "u0:r", to: "tr:l", dir: "h" },
    { from: "u1:r", to: "tr:l", dir: "h" },
  ],
  groups: [
    {
      x: -1.4,
      y: -1.2,
      w: 66,
      h: 5.0,
      label:
        "CHAIN — N-1 hops, one port pair per node, maps onto neighbour routing",
    },
    {
      x: -1.4,
      y: 6.3,
      w: 58,
      h: 13.4,
      label:
        "TREE — log2(N) hops, lower latency, worth it when many clusters split one K",
    },
  ],
};

const granules = {
  cols: [
    { key: "w", label: "" },
    { key: "s", label: "shape", mono: true },
    { key: "b", label: "bytes", mono: true, align: "right" },
    { key: "n", label: "words", mono: true, align: "right" },
  ],
  rows: [
    {
      w: "write — a <code>DRAIN</code>",
      s: "4x4 fp16 sub-tile",
      b: "32",
      n: "<b>1</b>",
      _tone: "good",
    },
    {
      w: "read — a <code>FILL</code>",
      s: "4 lanes x 32 K entry",
      b: "256",
      n: "8",
    },
  ],
};

const band = {
  cols: [
    { key: "w", label: "L1 words used", mono: true },
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

const missing = [
  "<b>Nothing bounds a MODEL, only a call.</b> <code>footprint</code> prices one kernel; a transformer block is tens, and the peak is across them. The thing to build is lifetimes over a step list rather than over one kernel's stages.",
  "<b>Weights are not distinguished from activations.</b> Pinning weights at the bottom in declaration order means adding a layer moves no weight already uploaded, and only activations are packed. At 16 GiB with SDXL's weights resident, that distinction is most of the budget.",
  "<b>No cross-call reuse.</b> Every call allocates and frees its own temps; two kernels in a row re-pay. A scratch arena owned by the runtime and handed to successive calls would remove it.",
  "<b><code>fragmentation</code> is measured but never acted on.</b> <code>trim()</code> only reclaims at the top. With an epoch bump the arena can be reset wholesale between steps, which is the cheap defragmentation a forward pass can actually use.",
  "<b>No test at 16 GiB.</b> Every test runs on an arena of megabytes. A model-sized placement — weights plus a block's activations — has never been built, and the failure it would find is a peak, not a leak.",
];
</script>

<template>
  <DocPage
    title="Residency and accumulators"
    summary="The tiers a value passes through and what each holds — and the one number that is not about arithmetic at all: the size of the tile a cluster holds resident decides how many mesh ports it needs."
    domain="tpu"
    status="building"
    source="xcvu13p-fhgb2104-2L-e · docs/projects/kohakutpu/accumulator.md · memory.md · results.md §2, §9"
  >
    <Fig
      caption="The tiers, and where each capacity comes from. The quantiser sits on the memory-agent side rather than in the compute unit: putting it in the CU would put a 32-element max-tree and a shift/round per element in 32 places instead of one, and would put FP16 on the mesh, throwing away the 2.2x density the format was chosen for."
      zoom
    >
      <BlockDiagram :nodes="tiers.nodes" :edges="tiers.edges" />
    </Fig>

    <SpecTable :cols="ladder.cols" :rows="ladder.rows" />

    <h2 class="doc-h2">
      Why the buffer is working storage, not a staging pipe
    </h2>

    <p class="doc-p">
      The arithmetic intensity of an output tile is
      <code>M·N·K / (M·K + K·N) = M·N / (M + N)</code>.
      <b>K cancels.</b> Chaining more compute units raises MACs and operand
      demand together, so the cluster chain buys DSP density and shared control
      — it buys no bandwidth at all. Only M and N do. For a cluster running at
      512 MAC/cycle, the refill is <code>512 · (1/M + 1/N)</code> elements per
      cycle.
    </p>

    <SpecTable
      :cols="refill.cols"
      :rows="refill.rows"
      caption="So the accumulator's capacity is not a convenience: it is the knob that sets the cluster's port count, and on-chip memory is far cheaper than mesh endpoints"
    />

    <p class="doc-p">
      In the instruction's own terms — <code>Gm</code> row groups by
      <code>Gn</code> column groups of 4x4 sub-tiles — the demand is
      <code>4(Gm + Gn) / (Gm·Gn)</code> words per cycle.
    </p>

    <SpecTable :cols="demand.cols" :rows="demand.rows" />

    <Callout kind="trap" title="The machine ran at break-even for a long time">
      <p>
        A port supplies one word per cycle, so
        <b>8x8 is exactly break-even</b> and every cycle lost to latency or
        arbitration comes straight off the result. That is most of why it
        measured 6–7% of its own datapath peak: the 6.6% was a break-even
        configuration plus overhead, not a machine at a fundamental roofline.
      </p>
    </Callout>

    <h3 class="doc-h3">Depth is free until the primitive runs out</h3>

    <p class="doc-p">
      A sub-tile is 16 values of <code>ACC_MW + 8</code> bits —
      <b>352 bits</b> at the default <code>ACC_MW = 14</code>. Against a 72-bit
      memory port that is <code>ceil(352/72) = 5</code> primitives,
      <b>whatever the depth</b>, because width sets the primitive count.
    </p>

    <SpecTable
      :cols="primitives.cols"
      :rows="primitives.rows"
      caption="Measured on the 6-cluster mesh: moving the tile to URAM read 30 URAM in and BRAM down 254 → 224 — a 1:1 exchange, because five primitives is five primitives and only the depth behind them changes. The resident output block went from 64x128 to 256x256 for no net memory"
    />

    <Callout
      kind="note"
      title="URAM costs no pipeline stage here, and that is the whole reason it is free"
    >
      <p>
        <code>READ_LAT = 2</code> is already the operating point, so the align
        stage already begins from a register and URAM's extra beat is a beat
        that was already being taken. The vector core's L1 is the opposite case
        and pays for it: it runs <code>READ_LAT = 1</code>, and URAM cannot do
        <code>READ_LAT = 1</code> at all.
      </p>
    </Callout>

    <Callout kind="rule" title="A ceiling is not a shape">
      <p>
        <code>TILES = 4096</code> <i>permits</i> a 256x256 output block; it does
        not impose one. The compiler's <code>choose_tile</code> ranks candidates
        by intensity <b>discounted by padding</b>, so a small problem still
        picks a small tile out of a deep accumulator, while a shallow
        accumulator cannot offer a large tile to a problem that wants one. This
        was rejected once on the grounds that it would pad every dimension up to
        256, and that objection was right about the wrong layer.
      </p>
    </Callout>

    <h2 class="doc-h2">
      The accumulator's format, and where floating point starts
    </h2>

    <BitField
      :fields="fp22"
      caption="S1 E7 M14 — the RTL parameter is ACC_MW, the built default is 14, and these pages call the result FP22. All-zero is the zero encoding. The compiler's type system names a dtype ACC24 for S1E7M16, the MW=16 variant; older pages say FP24 for the same thing"
    />

    <p class="doc-p">
      <b>E7 is not the tunable.</b> The accumulator holds
      <code>int × scaleA × scaleB</code> where the scales are E5M3 block scales;
      for FP16 sources that exponent sum spans roughly −48…+30, so an E5 field
      overflows on ordinary data rather than on edge cases. E7 is required, not
      chosen. <b>The mantissa is the tunable</b>, and results leave as FP16 — 11
      significand bits — so anything the accumulator carries beyond what
      survives that conversion only has to keep rounding from compounding across
      the K sweep. Measured across widths on a 32-block K sweep there is
      <b>a cliff between 22 and 20 bits, not between 24 and 20</b>: MW=14 is
      indistinguishable from MW=16, both landing at about a third of an FP16
      ULP.
    </p>

    <p class="doc-p">
      The block scale is applied here, and the way it is split is what keeps the
      operation exact: <code>val = part · (m8a · m8b)</code> with
      <code>m8 = 8 + M</code>, and
      <code>exp = ea[i] + eb[j] - anchor - 6</code>.
      <b>The exponent halves add; the mantissas multiply.</b> No shifter, no
      rounding, no precision lost. The magnitude is taken <i>inside</i> that
      multiply rather than after it — taken after, it put a 30-bit two's
      complement carry chain between the DSP's output register and the
      leading-one search: 0.952 ns of a 3.401 ns path, and four of its twelve
      logic levels.
    </p>

    <SpecTable
      :cols="pipeline.cols"
      :rows="pipeline.rows"
      caption="Seven stages, one accumulate per cycle sustained. The tile address is presented at stage 2a2 because READ_LAT = 2 means data lands two cycles later, during stage 3 — and REUSE_MIN did not move when stage 2a2 was added, because it counts the tile read to the tile write and the new stage sits ahead of the read"
    />

    <Callout
      kind="rule"
      title="Two things here are deliberately not constants anyone has to keep in sync"
    >
      <p>
        <b>The command rides a FIFO, not a matched delay.</b> The cascade is ~19
        cycles deep and that depth depends on the CU count and the skew SRLs, so
        the manager pushes one <code>{op, addr, sa, sb, anchor}</code> per issue
        and pops one per valid partial. Order is preserved by construction, so
        alignment survives any change to the chain.
      </p>
      <p>
        <b>Nothing counts cycles waiting for <code>emit_valid</code>.</b> The CU
        previously waited a fixed 10, which silently became too short when the
        pipeline deepened, and the system wrote a zero result while every unit
        test still passed.
      </p>
    </Callout>

    <h2 class="doc-h2">One bank, and the contract that replaced three</h2>

    <WaveTrace
      :rows="paceBroken.rows"
      :notes="paceBroken.notes"
      variant="broken"
      label="K innermost — an address recurs every cycle"
    />

    <WaveTrace
      :rows="paceFixed.rows"
      :notes="paceFixed.notes"
      variant="fixed"
      label="K outermost — an address recurs every Gm·Gn cycles"
    />

    <p class="doc-p">
      What replaces the banks is a contract:
      <b
        >consecutive commands to the same tile address must be at least
        <code>REUSE_MIN = 5</code> cycles apart.</b
      >
      Removing the banks turned a structural guarantee into a requirement on the
      caller, so it is checked in simulation rather than left implicit — a
      caller that sweeps K on the inside fails loudly instead of quietly
      accumulating into stale data. The check caught a real violation the moment
      it existed: the older single-port CU emitted a tile 2–3 cycles after the
      last accumulate into it. The manager guarantees it by construction,
      inserting idle cycles only below <code>Gm·Gn = 5</code>, where the cost is
      nothing at any tiling worth running.
    </p>

    <Callout
      kind="trap"
      title="busy has to mean more than “a command is in the pipeline”"
    >
      <p>
        It must mean “not safe to take the control mux yet”, and taking the mux
        means issuing an
        <code>EMIT</code> that reads an address an in-flight command may be
        about to write. So <code>busy</code> covers the write <i>and</i> the
        <code>REUSE_MIN</code> gap after it. A pipeline-only version reads
        correct and fails only when the whole <code>GEMM</code> is short enough
        that its tail has not cleared —
        <b
          >every sub-tile then drains as zero, which looks exactly like a
          compute bug.</b
        >
      </p>
    </Callout>

    <h2 class="doc-h2">Peer transfer: a matmul that spans clusters</h2>

    <Fig
      caption="Both are just sequences of SEND and ADD_PEER, so the topology is a scheduling decision, not a hardware one. The value transferred is the accumulator's own float, two 256-bit granules per sub-tile, so the sub-tile keeps full accumulator precision across the transfer — a K-split through memory would round to FP16 in between, and a drain-and-refill round trip would cost the write, the read and a quantiser pass."
      zoom
      wide
    >
      <BlockDiagram
        :nodes="peer.nodes"
        :edges="peer.edges"
        :groups="peer.groups"
      />
    </Fig>

    <p class="doc-p">
      <b>Split M/N should still be the default</b>, because it costs nothing —
      each cluster owns a disjoint output tile and writes it directly. Split K
      exists so that a tall-thin or short-wide matmul can still fill the
      machine, and the peer network is what makes it cheap when it is needed.
      The direct accumulator-to-accumulator wire does <b>not</b> exist:
      <code>peer_out</code> is left open and <code>FWD</code> has no command
      source, so a send leaves through the drain queue as two granules rather
      than on a dedicated wire.
    </p>

    <h2 class="doc-h2">The two granules that bind every span</h2>

    <SpecTable
      :cols="granules.cols"
      :rows="granules.rows"
      caption="Both fall out of WORD_BYTES = 32, LANES = 4, KBLOCK = 32. A drain burst is 256 bytes — WBURST = 8 granules of a 256-bit word — and a DRAIN moves whole bursts whatever the sub-tile count says, so every span starts on one and is rounded up to one, or a legal drain writes into the buffer after it and reports success"
    />

    <Callout kind="rule" title="A 4x4 sub-tile is EXACTLY one 256-bit word">
      <p>
        That is why the drained order looks strange and is nonetheless the fast
        one for fp16 @ fp16 → fp16: row-major would spread those sixteen values
        across four rows as four
        <b>partial</b> words, and a partial word is a read-modify-write. The
        sub-tile order is not a quirk of the drain —
        <b>it is the only shape whose write granule is the memory granule.</b>
      </p>
      <p>
        Which makes a fused epilogue a <b>layout</b> win, not only a latency
        one: the tile crosses the mesh into a vector core's L1 still in sub-tile
        order and is computed there, so it never lands in memory in a shape
        something later has to convert.
      </p>
    </Callout>

    <h2 class="doc-h2">The offsets are a ceiling independent of capacity</h2>

    <p class="doc-p">
      <code>aoff</code> and <code>boff</code> say where a sweep reads;
      <code>eoff</code> says where a fill lands. They buy double buffering —
      consecutive K chunks go to alternate halves of L1 A, so the fill for chunk
      <i>i+1</i> runs while chunk <i>i</i> is still being swept — and residency,
      since B does not change across the m loop and re-filling it per m-tile was
      a quarter of all memory traffic at the 256-cube: 4,096 beats of 16,384.
    </p>

    <Callout kind="trap" title="8-bit offsets, two banks of 256">
      <p>
        L1 is two banks of 256 rather than one flat 512; a bank bit picks the
        half and the address is bank concatenated with offset.
        <b>So a chunk may not exceed 256 entries whatever L1 holds</b> — and
        this is the ceiling the driver got wrong for a session, planning
        288-entry B chunks whose last offset wrapped onto entry 0, so every
        sub-tile past the wrap multiplied <i>another K block's</i> B. Measured
        on the card as a worst element of <b>8.23e+02</b> where the shape one
        step smaller was exactly right.
      </p>
      <p>
        On the compiler's planner the bank fields are still written as zero,
        <b
          >so every chunk lives in bank 0 and half of a 512-entry L1 is
          unreachable</b
        >. Wiring them would restore <code>gn = 32</code> at
        <code>nk ≥ 9</code>.
      </p>
    </Callout>

    <h2 class="doc-h2">The one real range limit</h2>

    <p class="doc-p">
      The accumulator's own range is enormous — E7 with bias 63 reaches roughly
      2^64 — and the value survives the entire reduction intact.
      <b>It is destroyed on the way out</b>, in the conversion to FP16 at stage
      6, which saturates at 65,504 silently. This matters more than it looks,
      because for a dot product over operands with a non-zero mean the growth is
      <code>~K·μa·μb</code> — linear, not square-root — and every post-ReLU
      activation is non-negative, so <code>K = 2048</code> overflows FP16 once
      <code>μa·μb &gt; 32</code>.
      <b
        >A K sweep does not gradually erode headroom; a biased operand
        distribution destroys it outright.</b
      >
    </p>

    <p class="doc-p">
      Splitting K does not fix this — the final sum is the same number however K
      is partitioned. What splitting K enables is
      <b>a different place to finish</b>: each cluster emits a partial at
      accumulator width, and a vector core sums them in a format with FP32's
      exponent range before converting once, on the store. That path is
      <b>not built</b>: the conversion field exists in the vector ISA, but
      nothing emits accumulator-width words into a vector core's L1 yet. Until
      then, silent FP16 saturation is an open defect rather than a documented
      limit, and nothing in the compiler mitigates it.
    </p>

    <h2 class="doc-h2">A feature that was built, measured, and cancelled</h2>

    <p class="doc-p">
      Attention wants <code>s = (q @ k.T) · scale</code> and then
      <code>· log2(e)</code> — two full vector passes over a score tile to
      multiply by two compile-time constants. The accumulator can absorb both:
      the scale product reaches the stage-1 DSP on an 18-bit B port using only 8
      bits, so a 9-bit constant mantissa folds into the same multiply exactly.
      It worked, bit-identity was verified two ways, and it cost
      <b>+852 LUT and −12.7 MHz</b> with no extra DSP.
    </p>

    <p class="doc-p">
      <b>It was cancelled anyway, because the host can do it for nothing.</b>
      Scaling <code>Q</code> by the constant before upload gives identical
      scores — <code>(C·Q) @ K.T = C·(Q@K.T)</code> — and costs no accuracy at
      all: MXFP7 is block-scaled, so a uniform factor is absorbed entirely into
      each block's exponent and never touches an int7 significand. Zero LUT,
      zero DSP, no instruction change, no quantisation contract.
    </p>

    <Callout kind="rule" title="Two things worth keeping from it">
      <p>
        <b>A width cannot be switched off at run time.</b> The first version
        made the scale always present with 1.0 as its neutral value. That is
        bit-identical and <i>not</i> cost-identical: it measured
        <b>10,297 LUT and 330.7 MHz</b> with the scale at 1.0, because widening
        the block-scale product widens the normaliser datapath from 30 to 39
        bits whatever the value in it.
        <b
          >A feature that changes a width has to be a compile-time parameter,
          not a neutral run-time value</b
        >, or the build that does not use it still pays.
      </p>
      <p>
        <b>Ask where else the identity holds before spending fabric on it.</b> A
        constant factor on a matmul output can be folded into either operand,
        and operands are uploaded once while outputs are produced every cycle.
        The accumulator is the wrong end of that trade.
      </p>
    </Callout>

    <h2 class="doc-h2">The L1 footprint band</h2>

    <SpecTable
      :cols="band.cols"
      :rows="band.rows"
      caption="Measured, unexplained, guarded. The corruption is 16 L1 words wide. The cost is a capability limit rather than a wrong answer, because the driver caps the footprint below the band: at a channel count of 320 with 32 groups a normalisation group is 10·hw elements, so the spatial extent is capped at hw ≤ 128 — an 8x16 tile works and a 12x16 does not"
    />

    <h2 class="doc-h2">Three failures worth keeping</h2>

    <Callout kind="trap" title="From a lifetime planner that no longer exists">
      <p>
        <b
          >A plan whose DECLARATION order differs from the runner's ISSUE order
          verifies clean and reads an address the allocator has given away.</b
        >
        Measured: a per-head transformer block scored <b>1.38e-01</b> instead of
        1.07e-03, with <code>verify()</code> passing.
      </p>
      <p>
        <b
          >Reuse aliasing is the one failure mode of a lifetime allocator, and
          it is completely silent on the machine.</b
        >
      </p>
      <p>
        <b
          >An unwritten line on this ECC DRAM is an uncorrectable error, not
          zeros.</b
        >
      </p>
    </Callout>

    <h2 class="doc-h2">What is missing, and how to get it</h2>

    <ol
      class="kt-text-body text-warm-700 dark:text-warm-300 leading-7 my-4 max-w-[70ch] list-decimal pl-5"
    >
      <li v-for="(m, i) in missing" :key="i" v-html="m" class="my-2" />
    </ol>

    <Callout kind="open" title="What is NOT modelled: DRAM row locality">
      <p>
        Nothing here knows a page size or a bank, so two buffers read together
        may land in the same bank and serialise. Against the measured cross-mesh
        rates — the mover's
        <b>0.098 GB/s</b>, a remote drain's <b>1.253 GB/s</b> sustained, and a
        <b>3.20 GB/s</b> link ceiling at 100.09 MHz — DRAM is not currently the
        bottleneck, so this is correctly unbuilt rather than forgotten. Revisit
        when a kernel is DRAM-bound.
      </p>
    </Callout>
  </DocPage>
</template>
