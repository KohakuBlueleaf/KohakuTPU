<script setup>
/* Where the arithmetic width lands against shipped mobile GPUs.
 *
 * PROVENANCE. Two kinds of number appear here and they are kept apart.
 *
 *   WIDTH — fused multiply-adds issued per clock by a named configuration.
 *   Arithmetic over unit counts. Exact, and the load-bearing half of the page.
 *
 *   RATE — GFLOP/s and instructions per fragment. Width multiplied by an
 *   ASSUMED 350 MHz. No frequency figure in this project is a closed-timing
 *   result and no mesh of these PEs has been placed, so every rate is PROJECTED
 *   and labelled.
 *
 * AREA is deliberately absent. Every published LUT total for either PE predates
 * the float tier's rebuild in binary32, so no figure this project has can say
 * whether the mesh below fits. Source: docs/projects/kohakumpe/simt/comparison.md
 * and unit-counts.md.
 */
const CLK = 350;

const shipped = {
  cols: [
    { key: "p", label: "Shipped part" },
    { key: "f", label: "FMA/clk per shader core", align: "right", mono: true },
  ],
  rows: [
    {
      p: "Mali-G77, G57, G78 — Valhall gen 1–2",
      f: "<b>16</b> <span class='opacity-60'>one 16-wide execution engine</span>",
    },
    { p: "Mali-G310 — Valhall gen 3, entry", f: "16 minimum, scaling to 64" },
    {
      p: "Mali-G510, G610, G710 — Valhall gen 3",
      f: "<b>64</b> <span class='opacity-60'>two engines, dual datapath</span>",
    },
  ],
};

const perClock = `   SIMD PE at 4 float units                  4 FMA/clk
   SIMT PE at 8 float units                  8 FMA/clk

   mesh   =  8 SIMD x 4  +  4 SIMT x 8  =  32 + 32  =   64 FMA/clk
   device =  4 meshes                              =  256 FMA/clk`;

const meshFig = {
  nodes: [
    {
      id: "simd",
      x: 0,
      y: 0,
      w: 10,
      h: 6,
      label: "8 × SIMD PE",
      sub: "4 float units each → 32 FMA/clk",
    },
    {
      id: "simt",
      x: 0,
      y: 8,
      w: 10,
      h: 6,
      label: "4 × SIMT PE",
      sub: "8 float units each → 32 FMA/clk",
      accent: true,
    },
    {
      id: "sum",
      x: 16,
      y: 4,
      w: 10,
      h: 6,
      label: "one mesh",
      sub: "64 FP FMA per clock",
      accent: true,
    },
    {
      id: "g610",
      x: 32,
      y: 4,
      w: 10,
      h: 6,
      label: "one Mali-G610 or G710 shader core",
      sub: "64 FMA/clk",
    },
  ],
  edges: [
    { from: "simd:r", to: "sum:l", dir: "h" },
    { from: "simt:r", to: "sum:l", dir: "h", accent: true },
    { from: "sum:r", to: "g610:l", dir: "h", accent: true, label: "same width" },
  ],
};

const lands = {
  cols: [
    { key: "w", label: "" },
    { key: "f", label: "FMA/clk", align: "right", mono: true },
    { key: "c", label: "clock", align: "right", mono: true },
    { key: "g", label: "GFLOP/s", align: "right", mono: true },
  ],
  rows: [
    {
      w: "<b>one of these meshes — PROJECTED</b>",
      f: "64",
      c: "350 assumed",
      g: "<b>44.8</b>",
      _tone: "good",
    },
    { w: "Mali-G57 / G77 core", f: "16", c: "~950 MHz", g: "~30" },
    { w: "Mali-G310, minimum configuration", f: "16", c: "~650 MHz", g: "~21" },
    { w: "Mali-G610 / G710 core", f: "64", c: "~850 MHz", g: "~109" },
    {
      w: "<b>a four-mesh device — PROJECTED</b>",
      f: "256",
      c: "350 assumed",
      g: "<b>179.2</b>",
      _tone: "good",
    },
    { w: "Mali-G57 MC3 — Dimensity 700 class", f: "48", c: "~950 MHz", g: "~91" },
    {
      w: "Adreno 540 — Snapdragon 835, 2017",
      f: "not disclosed",
      c: "~710 MHz",
      g: "~567",
    },
    { w: "Mali-G610 MC6", f: "384", c: "~850 MHz", g: "~653" },
  ],
};

const gflops = [
  { label: "Mali-G310, minimum configuration — one core", value: 21 },
  { label: "Mali-G57 / G77 — one core", value: 30 },
  {
    label: "one of these meshes — PROJECTED, 64 FMA/clk at an assumed 350 MHz",
    value: 44.8,
    tone: "accent",
  },
  { label: "Mali-G57 MC3 — Dimensity 700 class", value: 91 },
  { label: "Mali-G610 / G710 — one core", value: 109, tone: "good" },
  {
    label: "a four-mesh device — PROJECTED, 256 FMA/clk at an assumed 350 MHz",
    value: 179.2,
    tone: "accent",
  },
  { label: "Adreno 540 — Snapdragon 835, 2017", value: 567, tone: "warn" },
  { label: "Mali-G610 MC6", value: 653, tone: "warn" },
];

const issue = `   4 meshes x 4 SIMT PE x 8 threads x 350 MHz  =  44.8 G thread-instructions/s
                                                  PROJECTED`;

const frames = {
  cols: [
    { key: "t", label: "Target, at 2× overdraw" },
    { key: "f", label: "fragments/s", align: "right", mono: true },
    { key: "i", label: "instructions each", align: "right", mono: true },
    { key: "u", label: "issue used", align: "right", mono: true },
    { key: "b", label: "budget per fragment", align: "right", mono: true },
  ],
  rows: [
    {
      t: "720p, 30 fps",
      f: "55.3 M",
      i: "50",
      u: "<b>6.2 %</b>",
      b: "<b>810</b>",
      _tone: "good",
    },
    {
      t: "1080p, 60 fps",
      f: "248.8 M",
      i: "200",
      u: "<b>111 %</b>",
      b: "<b>180</b>",
      _tone: "bad",
    },
  ],
};

const missing = {
  cols: [
    { key: "w", label: "What a shipped mobile GPU has in fixed function" },
    { key: "us", label: "What this machine does instead" },
  ],
  rows: [
    {
      w: "<b>a rasteriser</b> — triangle setup, edge functions, coverage",
      us: "software, on the same PEs that shade. <b>Unmeasured, and the single largest unknown on this page by a wide margin.</b> A Mali core does this in fixed function and spends none of its 16 or 64 units on it",
      _tone: "bad",
    },
    {
      w: "<b>a texture sampler</b>",
      us: "address arithmetic is the integer path and costs nothing new; filtering is lane code at <b>12 multiply-adds per RGBA bilinear tap</b>. Both are priced — but a shipped part gets them free and this machine pays issue slots",
      _tone: "warn",
    },
    {
      w: "<b>depth and blend fixed function</b>, and <b>atomics</b>",
      us: "neither exists. The A extension's opcode major is <b>not in the legal set at all</b>, so an <code>amo*</code> word raises an illegal-instruction fault rather than being decoded into something adjacent",
      _tone: "bad",
    },
    {
      w: "<b>a driver and a shading-language path</b>",
      us: "SPIR-V to this ISA is a <b>designed route with no implementation</b> anywhere in <code>src/</code> or <code>compiler/</code>. Nothing here runs a shader written in a shading language today",
      _tone: "bad",
    },
    {
      w: "<b>a placed and routed design</b>",
      us: "every figure either PE has is out-of-context synthesis of <b>one</b> PE. The interconnect and the memory agent are budgeted for but have never been co-placed with a dozen of them, and <b>the clock is what would move</b>",
      _tone: "bad",
    },
  ],
};

const next = {
  cols: [
    { key: "n", label: "#", mono: true, align: "right" },
    { key: "w", label: "What to measure, in order" },
  ],
  rows: [
    {
      n: "1",
      w: "<b>Software rasterisation cost</b>, in instructions per triangle and per fragment. Until this exists the frame budgets describe <b>shading only</b>, and the honest headline is “the shading is affordable”, not “the frame is”",
      _tone: "bad",
    },
    {
      n: "2",
      w: "<b>A real shader corpus through the issue model</b>, so 50 and 200 instructions per fragment stop being placeholders. <b>810 and 180 per fragment are what such a corpus would be judged against</b>",
      _tone: "warn",
    },
    {
      n: "3",
      w: "<b>Re-measure both PEs against the current float tier.</b> No area figure this project has describes a PE the RTL can build today, so nothing on this page can yet say whether the mesh in the diagram fits",
      _tone: "warn",
    },
    {
      n: "4",
      w: "<b>Place and route one mesh.</b> Every rate here is computed on an assumed clock until it does",
      _tone: "warn",
    },
  ],
};

const kinds = {
  cols: [
    { key: "t", label: "Thing" },
    { key: "c", label: "Category" },
  ],
  rows: [
    {
      t: "the 64 FMA/clk mesh target itself",
      c: "<b>yours.</b> It is the one figure on this page that is a design decision rather than a measurement or an assumption — and it is what the SIMT PE's float unit count was chosen against",
    },
    {
      t: "the float unit counts behind it",
      c: "<b>yours</b> — independent widths on both classes, set per instance",
    },
    {
      t: "IEEE binary32 as the compute format",
      c: "<b>not a parameter at all</b>, on either class",
    },
    {
      t: "comparing on FMA per clock rather than on GFLOP/s",
      c: "<b>convention.</b> Nothing enforces it, and it is worth copying: GFLOPS across vendors and clock domains hides where the difference comes from, which for an FPGA is always the clock",
    },
  ],
};
</script>

<template>
  <DocPage
    title="Where this lands against shipped GPUs"
    summary="What the arithmetic width is worth in industry terms, which mobile parts it corresponds to, what class of rendering that makes plausible — and why arithmetic is not this machine's limit."
    domain="simt"
    status="projected"
    source="docs/projects/kohakumpe/simt/comparison.md · docs/projects/kohakumpe/unit-counts.md"
  >
    <h2 class="doc-h2">What this page settles, and what it cannot</h2>
    <p class="doc-p">
      It settles <b>width</b>: how many fused multiply-adds a named
      configuration issues per clock, and which shipped parts that corresponds
      to. Width is arithmetic over unit counts and is exact.
    </p>
    <p class="doc-p">
      It cannot settle <b>area</b> or <b>clock</b>. Every published LUT total
      for either PE class predates the float tier's rebuild in binary32, so no
      figure this project has describes a PE the RTL can build today — which
      means <b>nothing here says whether the mesh below fits</b>. And no
      frequency figure in this project is a closed-timing result: they are
      out-of-context synthesis estimates of <i>one</i> PE at a time, they are
      the optimistic end, and this repository has measured a module lose
      <b>0.740&nbsp;ns</b> between synthesis and routing. Every rate below is
      therefore computed at an <b>assumed 350 MHz</b> and labelled PROJECTED. It
      is not a measured frame rate and it is not a closed clock.
    </p>

    <Callout
      kind="rule"
      title="Read the width comparisons as load-bearing and the rates as arithmetic on top of an assumption"
    >
      <p>
        Peak floating-point rate is
        <span class="chip">units × 2 × clock</span>, because a fused
        multiply-add is two operations. The <b>units</b> half is measured. The
        <b>clock</b> half is assumed, and a 10% change in it moves the last
        section of this page across its own threshold. Where a reading depends
        on the clock, this page says so.
      </p>
    </Callout>

    <h2 class="doc-h2">The unit of comparison is FMA per clock</h2>

    <SpecTable
      :cols="shipped.cols"
      :rows="shipped.rows"
      caption="ARM documents Mali per shader core, so the comparison is exact. Qualcomm does not disclose an FP32 ALU count for Adreno, so that one is compared on quoted throughput only. Sanity check on the older figure: a G77 MP11 at ~850 MHz gives 11 × 16 × 2 × 0.85 GHz ≈ 299 GFLOP/s, which matches published MP11 numbers."
    />

    <h2 class="doc-h2">What this configuration has</h2>

    <p class="doc-p">
      Both PE classes compute in IEEE binary32 with the same units — one
      <span class="chip">rv_fpu</span> per unit, never forked between the two —
      so their multiply-adds are directly commensurable and a mesh total is a
      plain sum rather than a conversion.
    </p>

    <div
      class="font-mono kt-text-caption whitespace-pre text-warm-700 dark:text-warm-300 leading-6 overflow-x-auto my-3"
    >
      {{ perClock }}
    </div>

    <Fig
      caption="One mesh is 64 FP FMA per clock, which is one Mali-G610 or G710 shader core exactly — and it is reached by the two PE classes contributing half each rather than by either being widened past its own sensible point. At four float units on the SIMT side the same mesh is 48 and short. This is the one figure on the page that is a design decision rather than a measurement or an assumption, and it is the target the SIMT PE's float count was chosen against."
      zoom
      wide
    >
      <BlockDiagram :nodes="meshFig.nodes" :edges="meshFig.edges" />
    </Fig>

    <Callout
      kind="trap"
      title="An array throughput is a configuration, not a property of the design"
    >
      <p>
        The float count is a <b>knob</b> on both classes, with legal values 0,
        1, 2, 4, 8 and −1 for full rate, and a narrower one costs an issue
        interval rather than an instruction. Quoting an array throughput without
        naming the unit counts behind it describes a configuration and calls it
        a machine — the same twelve PEs at eight float units each would be
        <b>96</b> FMA per clock, and at two they would be 24.
      </p>
      <p>
        The count above is the one that lands on a shader core's width. It is
        not the widest the parts will build, and it is not a menu price.
      </p>
    </Callout>

    <Callout
      kind="trap"
      title="This page makes no claim about whether that mesh fits on a die"
    >
      <p>
        It is the obvious next question and it cannot be answered from anything
        this project has measured. Every absolute LUT total for either class was
        taken before the float tier was rebuilt from an E8M15 datapath with two
        operand formats into a binary32-only one, before the integer dot unit
        and its accumulator were removed, and before the converter group gained
        its datapath. <b>Multiplying one of those totals by a PE count prices a
        machine that cannot be built</b>, and the answer would be wrong in an
        unknown direction rather than merely stale.
      </p>
      <p>
        The symptom of doing it anyway is a page that reports a die-occupancy
        percentage to two significant figures on top of a withdrawn number. Item
        3 below is what would fix it.
      </p>
    </Callout>

    <h2 class="doc-h2">Where that lands</h2>

    <SpecTable
      :cols="lands.cols"
      :rows="lands.rows"
      caption="PROJECTED for this machine, at an ASSUMED 350 MHz — not a measured or a closed clock. The shipped parts are published unit counts at typical clocks."
    />

    <ResourceBars
      :items="gflops"
      unit="GFLOP/s"
      caption="The two accented rows are PROJECTED: a measured unit count multiplied by an assumed clock. The shipped parts are published counts at typical clocks; Adreno 540's ALU count is not disclosed, so its bar is quoted throughput only"
    />

    <Callout kind="note" title="Three readings, and two of them are about width">
      <p>
        <b>One mesh is four G57 cores wide</b> and about 1.5× one in projected
        throughput. Once the width ratio reaches 4:1 the clock deficit stops
        deciding the outcome — which is the general shape of what an FPGA can
        and cannot buy back.
      </p>
      <p>
        <b>One mesh matches one G610 or G710 core in width exactly</b>, and
        would reach about <b>41%</b> of it in throughput at the assumed clock.
        The remaining gap is <b>entirely clock</b> — 350 MHz against ~850 — and
        not units.
      </p>
      <p>
        <b>The device would sit between a 2020 mid-range mobile GPU and a 2017
        flagship</b> — roughly 2× a Mali-G57 MC3, and about a third of an Adreno
        540. A full G610 MC6 is <b>not reachable</b>: it is 1.5× the width
        <i>and</i> 2.4× the clock, and neither half of that is closeable here.
      </p>
    </Callout>

    <h2 class="doc-h2">What that makes plausible</h2>

    <p class="doc-p">
      The useful question is not GFLOPS, it is whether the machine can issue
      enough instructions per pixel. Issue capacity is one instruction per cycle
      per PE across eight threads:
    </p>

    <div
      class="font-mono kt-text-caption whitespace-pre text-warm-700 dark:text-warm-300 leading-6 overflow-x-auto my-3"
    >
      {{ issue }}
    </div>

    <SpecTable
      :cols="frames.cols"
      :rows="frames.rows"
      caption="Two frame budgets against that issue capacity. 50 and 200 instructions per fragment are PLACEHOLDERS rather than a measured shader corpus."
    />

    <Callout
      kind="trap"
      title="Read the instruction budget, not the percentage"
    >
      <p>
        The percentage moves with the assumed clock and the budget does not, so
        state it as <b>810 thread-instructions per fragment at 720p30</b> and
        <b>180 at 1080p60</b>, both at 2× overdraw.
        <b>1080p60 with 200-instruction shaders sits just past the ceiling
        rather than just under it</b>, and a 10% change in the assumed clock
        moves it across — which is exactly the kind of conclusion a percentage
        hides and a budget does not.
      </p>
      <p>
        The supporting traffic is <b>not</b> the constraint, and it does not
        move with the clock either. A framebuffer at 720p30 is ~221 MB/s written
        and read; one RGBA bilinear tap per fragment is 12 multiply-adds and a
        32-byte entry read, so ~1.8 GB/s at 720p30 — against four DRAM channels.
      </p>
      <p>
        <b
          >So a small real-time renderer at 720p30 is comfortably inside the
          arithmetic, and 1080p60 with rich shaders is at or slightly over the
          ceiling</b
        >
        — as an arithmetic bound, not as a frame rate.
      </p>
    </Callout>

    <h2 class="doc-h2">What these numbers do not say</h2>
    <p class="doc-p">
      Everything above prices <b>arithmetic</b>. Rendering is not
      arithmetic-bound on this machine, and the gap is fixed function.
    </p>

    <SpecTable :cols="missing.cols" :rows="missing.rows" />

    <Callout kind="note" title="Precision is not on that list">
      <p>
        Both PE classes compute in <b>IEEE binary32</b> throughout, which is the
        format desktop shading uses and a wider one than the fp16 mobile
        fragment shaders actually run at. For colour, filter weights and
        interpolation this machine is at the bar rather than below it.
      </p>
      <p>
        The only mobile advantage here is a <i>rate</i> one: those parts run
        fp16 at double their FP32 rate, which this machine does not — one
        element per 32-bit word and one compute format means there is no such
        trade to make, and buying it would mean a second datapath rather than a
        mode.
      </p>
      <p>
        <b>Integer multiply is not on the list either.</b> RV32M is built on
        both classes, one 33×33 signed product per lane, so a pixel index or a
        Morton address is one instruction rather than a shift-add chain. Divide
        and remainder fault, deliberately — divide-by-a-constant
        strength-reduces to <code>mulhu</code>.
      </p>
    </Callout>

    <h2 class="doc-h2">What to measure next, in order</h2>

    <SpecTable :cols="next.cols" :rows="next.rows" />

    <Callout
      kind="open"
      title="Until item 1 lands, the frame budgets are an upper bound on what the arithmetic permits, not a frame rate"
    >
      <p>
        Nothing on this page prices triangle setup, coverage, or the driver that
        would have to produce the shader in the first place.
      </p>
    </Callout>

    <h2 class="doc-h2">Fixed protocol, addon, convention, or yours</h2>
    <SpecTable :cols="kinds.cols" :rows="kinds.rows" />
  </DocPage>
</template>
