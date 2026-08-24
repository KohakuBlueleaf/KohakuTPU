<script setup>
// ===========================================================================
// SIMT PE — where this lands against shipped GPUs.
// Drawn from docs/projects/kohakumpe/simt/comparison.md. Every figure for this machine is
// one accelerator on one part, xcvu13p-fhgb2104-2L-e, out-of-context synthesis
// at the 2.857 ns ask (350 MHz) unless a row says otherwise.
// ===========================================================================

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

const measured = {
  cols: [
    { key: "u", label: "Unit" },
    { key: "lut", label: "LUT", align: "right", mono: true },
    { key: "ff", label: "FF", align: "right", mono: true },
    { key: "bram", label: "BRAM", align: "right", mono: true },
    { key: "dsp", label: "DSP48", align: "right", mono: true },
    { key: "f", label: "Fmax", align: "right", mono: true },
    { key: "s", label: "slack", align: "right", mono: true },
  ],
  rows: [
    {
      u: "<b>SIMT PE</b> — 8 int + 8 float lanes, RV32M",
      lut: "<b>21,586</b>",
      ff: "17,268",
      bram: "30.5",
      dsp: "<b>48</b>",
      f: "<b>365.6 MHz</b>",
      s: "+0.122",
      _tone: "good",
    },
    {
      u: "<b>SIMD PE</b> — SIMD 8 + 4 float lanes",
      lut: "<b>13,772</b>",
      ff: "10,126",
      bram: "13",
      dsp: "<b>72</b>",
      f: "353.4 MHz",
      s: "+0.027",
      _tone: "good",
    },
    {
      u: "controller PE — <code>rv_pe</code>, SIMD_EN = 0",
      lut: "2,477",
      ff: "4,140",
      bram: "5",
      dsp: "0",
      f: "377.9 MHz",
      s: "—",
    },
  ],
};

const perClock = `   SIMD PE, 4 float lanes                    4 FMA/clk
   SIMT PE, 8 float lanes                    8 FMA/clk

   mesh = 8 DSP x 4  +  4 GPU x 8   =  32 + 32  =  64 FMA/clk
   device = 4 meshes                             = 256 FMA/clk`;

const meshFig = {
  nodes: [
    {
      id: "simd",
      x: 0,
      y: 0,
      w: 14,
      label: "8 × SIMD PE",
      sub: "4 float lanes each → 32",
    },
    {
      id: "simt",
      x: 0,
      y: 5,
      w: 14,
      label: "4 × SIMT PE",
      sub: "8 float lanes each → 32",
      accent: true,
    },
    {
      id: "ctl",
      x: 0,
      y: 10,
      w: 14,
      label: "2 × controller PE",
      sub: "no float lanes",
    },
    {
      id: "sum",
      x: 20,
      y: 4.4,
      w: 14,
      label: "one mesh",
      sub: "64 FP FMA / clock",
      accent: true,
    },
    {
      id: "g610",
      x: 38,
      y: 4.4,
      w: 16,
      label: "one Mali-G610 shader core",
      sub: "64 FMA/clk — parity on width",
      accent: true,
    },
  ],
  edges: [
    { from: "dsp:r", to: "sum:l", dir: "h" },
    { from: "gpu:r", to: "sum:l", dir: "h", accent: true },
    { from: "ctl:r", to: "sum:l", dir: "h", dash: true },
    { from: "sum:r", to: "g610:l", dir: "h", accent: true, label: "=" },
  ],
};

const meshFit = {
  cols: [
    { key: "w", label: "" },
    { key: "n", label: "count", align: "right", mono: true },
    { key: "each", label: "LUT each", align: "right", mono: true },
    { key: "lut", label: "LUT", align: "right", mono: true },
    { key: "dsp", label: "DSP48", align: "right", mono: true },
    { key: "bram", label: "BRAM", align: "right", mono: true },
    { key: "fma", label: "FMA/clk", align: "right", mono: true },
  ],
  rows: [
    {
      w: "SIMD PE, SIMD 8 + 4 float",
      n: "8",
      each: "13,772",
      lut: "110,176",
      dsp: "576",
      bram: "104",
      fma: "32",
    },
    {
      w: "SIMT PE, 8 int + 8 float",
      n: "4",
      each: "21,586",
      lut: "86,344",
      dsp: "192",
      bram: "122",
      fma: "32",
    },
    {
      w: "controller PE",
      n: "2",
      each: "2,477",
      lut: "4,954",
      dsp: "0",
      bram: "10",
      fma: "—",
    },
    {
      w: "<b>mesh total</b>",
      n: "<b>14</b>",
      each: "",
      lut: "<b>201,474</b>",
      dsp: "<b>768</b>",
      bram: "<b>236</b>",
      fma: "<b>64</b>",
      _tone: "good",
    },
    {
      w: "against the budget",
      n: "",
      each: "",
      lut: "~350,000",
      dsp: "3,072",
      bram: "672",
      fma: "—",
    },
    {
      w: "<b>used</b>",
      n: "",
      each: "",
      lut: "<b>58 %</b>",
      dsp: "<b>25 %</b>",
      bram: "<b>35 %</b>",
      fma: "",
    },
  ],
};

const occupancy = [
  {
    label:
      "LUT — of the ~350,000 a mesh has once fabric and memory agent are paid for",
    value: 58,
    note: "%",
    tone: "accent",
  },
  { label: "BRAM — per SLR on this part", value: 35, note: "%", tone: "good" },
  { label: "DSP48 — per SLR on this part", value: 25, note: "%", tone: "good" },
];

const lands = {
  cols: [
    { key: "w", label: "" },
    { key: "f", label: "FMA/clk", align: "right", mono: true },
    { key: "c", label: "clock", align: "right", mono: true },
    { key: "g", label: "GFLOP/s", align: "right", mono: true },
  ],
  rows: [
    {
      w: "<b>one of our meshes</b>",
      f: "64",
      c: "350 MHz",
      g: "<b>44.8</b>",
      _tone: "good",
    },
    { w: "Mali-G57 / G77 core", f: "16", c: "~950 MHz", g: "~30" },
    { w: "Mali-G310, minimum config", f: "16", c: "~650 MHz", g: "~21" },
    { w: "Mali-G610 / G710 core", f: "64", c: "~850 MHz", g: "~109" },
    {
      w: "<b>our whole device</b>",
      f: "256",
      c: "350 MHz",
      g: "<b>179.2</b>",
      _tone: "good",
    },
    {
      w: "Mali-G57 MC3 — Dimensity 700 class",
      f: "48",
      c: "~950 MHz",
      g: "~91",
    },
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
  { label: "Mali-G310, minimum config — one core", value: 21 },
  { label: "Mali-G57 / G77 — one core", value: 30 },
  {
    label: "one of our meshes — 64 FMA/clk at 350 MHz",
    value: 44.8,
    tone: "accent",
  },
  { label: "Mali-G57 MC3 — Dimensity 700 class", value: 91 },
  { label: "Mali-G610 / G710 — one core", value: 109, tone: "good" },
  { label: "our whole device — 4 meshes", value: 179.2, tone: "accent" },
  { label: "Adreno 540 — Snapdragon 835, 2017", value: 567, tone: "warn" },
  { label: "Mali-G610 MC6", value: 653, tone: "warn" },
];

const issue = `   4 meshes x 4 SIMT PE x 8 threads x 350 MHz  =  44.8 G thread-instructions/s`;

const frames = {
  cols: [
    { key: "t", label: "Target" },
    { key: "f", label: "fragments/s", align: "right", mono: true },
    { key: "i", label: "instructions each", align: "right", mono: true },
    { key: "u", label: "issue used", align: "right", mono: true },
  ],
  rows: [
    {
      t: "720p, 30 fps, 2× overdraw",
      f: "55.3 M",
      i: "50",
      u: "<b>6.2 %</b>",
      _tone: "good",
    },
    {
      t: "1080p, 60 fps, 2× overdraw",
      f: "248.8 M",
      i: "200",
      u: "<b>111 %</b>",
      _tone: "bad",
    },
  ],
};

const budgets = `   720p30, 2x overdraw    810 thread-instructions per fragment
   1080p60, 2x overdraw   180 thread-instructions per fragment`;

const missing = {
  cols: [
    { key: "w", label: "What a shipped mobile GPU has in fixed function" },
    { key: "us", label: "What this machine does instead" },
  ],
  rows: [
    {
      w: "<b>a rasteriser</b> — triangle setup, edge functions, coverage",
      us: "software, on the same PEs that shade. <b>Unmeasured, and the single largest unknown on this page.</b> A Mali core spends none of its 16 or 64 lanes on it",
      _tone: "bad",
    },
    {
      w: "<b>a texture sampler</b>",
      us: "address math is the integer path and costs nothing new; filtering is lane code at <b>12 FMAs per RGBA bilinear tap</b>. Both are priced — but a shipped part gets them free and we pay issue slots",
      _tone: "warn",
    },
    {
      w: "<b>depth and blend fixed function</b>, and <b>atomics</b>",
      us: "neither exists. The A extension's major is not in <code>kht_predec</code>'s legal set, so an <code>amo*</code> opcode raises an illegal-instruction fault rather than being quietly ignored",
      _tone: "bad",
    },
    {
      w: "<b>a driver and API stack</b>",
      us: "SPIR-V → NIR → this ISA is a <b>designed path with no implementation</b> anywhere in <code>src/</code> or <code>compiler/</code>. Nothing here runs a shader written in GLSL today",
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
      w: "<b>Software rasterisation cost</b>, in instructions per triangle and per fragment. Until this exists, the frame budgets above describe <b>shading only</b>, and the honest headline is “the shading is affordable”, not “the frame is”",
      _tone: "bad",
    },
    {
      n: "2",
      w: "<b>A real shader corpus through the issue model</b>, so 50 and 200 instructions per fragment stop being placeholders — 810 and 180 per fragment are what such a corpus would be judged against",
      _tone: "warn",
    },
    {
      n: "3",
      w: "<b>Place and route one mesh.</b> Every figure here is out-of-context synthesis at 58 % LUT occupancy; the interconnect and the memory agent are budgeted for but not co-placed with twelve PEs, and the clock is what would move",
      _tone: "warn",
    },
  ],
};
</script>

<template>
  <DocPage
    title="Where this lands against shipped GPUs"
    summary="What the measured numbers are worth in industry terms, which mobile parts they correspond to, and what class of rendering workload that makes plausible. Arithmetic is not this machine's limit; fixed function and the software stack are."
    domain="simt"
    status="measured"
    source="docs/projects/kohakumpe/simt/comparison.md · xcvu13p-fhgb2104-2L-e, OOC synth at 2.857 ns"
  >
    <p class="doc-p">
      A FLOPS number is only useful if it tells you what to attempt. This page
      establishes the comparison honestly, then spends most of its length on the
      part that actually decides feasibility, which is not arithmetic.
    </p>

    <h2 class="doc-h2">The unit of comparison is FMA per clock</h2>
    <p class="doc-p">
      Peak floating-point rate is <code>lanes × 2 × clock</code>, because a
      fused multiply-add is two operations. Comparing lane counts across vendors
      is meaningful; comparing GFLOPS across vendors and clock domains hides
      where the difference comes from, which for an FPGA is always the clock.
    </p>

    <SpecTable
      :cols="shipped.cols"
      :rows="shipped.rows"
      caption="ARM documents Mali per shader core, so the comparison is exact. Qualcomm does not disclose an FP32 ALU count for Adreno, so that one is compared on quoted throughput only. Sanity check on the older figure: G77 MP11 at ~850 MHz gives 11 × 16 × 2 × 0.85 GHz ≈ 299 GFLOP/s, which matches published MP11 numbers."
    />

    <h2 class="doc-h2">What this machine has</h2>

    <SpecTable
      :cols="measured.cols"
      :rows="measured.rows"
      caption="Both PE classes at the configuration of record, at the 2.857 ns ask, on xcvu13p-fhgb2104-2L-e. MEASURED — nothing here is an estimate. The SIMT PE figure is the whole unit: SIMT core, windows, banked LDS, L1, requestor, fabric port, with the float tier and the integer multiplier both built. Sources: build/sweep/g-350-pad/run.log and build/sweep/d-350/run.log"
    />

    <Callout
      kind="trap"
      title="Superseded figures that appear in older revisions"
    >
      <p>
        All at the <b>3.333 or 2.500 ns</b> ask and none of them this machine:
        <code>kht_core</code> at 9,653 LUT / 279.5 MHz; the integer-only
        assembled PE at 15,638 LUT / 392.8 MHz; the float-without-multiplier
        build at 19,215 LUT / 405.2 MHz; and the ~22k LUT <i>estimate</i> for a
        float-capable PE that this measurement replaced.
        <b>Quote none of them without its ask.</b>
      </p>
    </Callout>

    <Callout
      kind="note"
      title="The float tier takes both operand widths, and that is not a build option"
    >
      <p>
        Nothing in the table above is a dtype configuration. Operand width is
        selected
        <b>per instruction</b>, by a funct7 bit that reaches the lane as a port
        rather than a parameter — so there is no build of this PE that has the
        float tier and refuses one of the two widths. See
        <RouterLink to="/mpe/simt" class="doc-link">the SIMT PE page</RouterLink
        >.
      </p>
    </Callout>

    <div
      class="font-mono kt-text-caption whitespace-pre text-warm-700 dark:text-warm-300 leading-6 overflow-x-auto my-3"
    >
      {{ perClock }}
    </div>

    <Fig
      caption="The target is met exactly on width. One mesh is 64 FP FMA per clock, which is one Mali-G610/G710 shader core — and it is reached by the two PE classes contributing half each, rather than by either one being widened past its own sensible point."
      zoom
      wide
    >
      <BlockDiagram :nodes="meshFig.nodes" :edges="meshFig.edges" />
    </Fig>

    <h3 class="doc-h3">The mesh fits, with room</h3>

    <SpecTable
      :cols="meshFit.cols"
      :rows="meshFit.rows"
      caption="The LUT budget is the ~350,000 a mesh has once the fabric and the memory agent are paid for; the DSP48 and BRAM denominators are PER SLR on this part, which is the right denominator because a mesh is placed in one."
    />

    <ResourceBars
      :items="occupancy"
      unit="% of the mesh budget"
      :max="100"
      caption="MEASURED occupancy from the table above. The arithmetic target is not what constrains the mesh: at 58 % of the LUT and a quarter of the DSP48 the width could be bought again — which is the finding, because for eighteen months the assumption was that reaching a shader core's width would exhaust the die"
    />

    <Callout
      kind="rule"
      title="Every rate below is computed at 350 MHz, the ask — not at either Fmax"
    >
      <p>
        Both PE classes close <b>above</b> the 350 MHz ask in synthesis — 365.6
        and 353.4 — so the mesh clock is set by the slower of the two rather
        than by either missing. But out-of-context synthesis is not routing, and
        this project has measured a module lose <b>0.740 ns</b> between the two.
      </p>
    </Callout>

    <h2 class="doc-h2">Where that lands</h2>

    <SpecTable
      :cols="lands.cols"
      :rows="lands.rows"
      caption="All rows at 350 MHz for this machine."
    />

    <ResourceBars
      :items="gflops"
      unit="GFLOP/s"
      caption="Our two rows are MEASURED width at the 350 MHz ask; the shipped parts are published lane counts at typical clocks. Adreno 540's ALU count is not disclosed, so its bar is quoted throughput only"
    />

    <Callout kind="measured" title="Three readings">
      <p>
        <b>One mesh is four G57 cores wide and about 1.5× one in throughput.</b>
        Once the width ratio reaches 4:1 the clock deficit stops deciding the
        outcome.
      </p>
      <p>
        <b>One mesh matches one G610/G710 core in width exactly</b>, and reaches
        <b>41 %</b> of it in throughput. That is the standing target, and the
        remaining gap is entirely clock — 350 MHz against ~850 — not lanes.
      </p>
      <p>
        <b
          >The device sits between a 2020 mid-range mobile GPU and a 2017
          flagship</b
        >
        — almost exactly 2× a Mali-G57 MC3, and 32 % of an Adreno 540. A full
        G610 MC6 is not reachable: it is 1.5× the width <i>and</i> 2.4× the
        clock.
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
      caption="Two frame budgets at 2× overdraw, against that issue capacity."
    />

    <p class="doc-p">
      Read as an instruction budget rather than as a percentage, the same two
      rows say:
    </p>

    <div
      class="font-mono kt-text-caption whitespace-pre text-warm-700 dark:text-warm-300 leading-6 overflow-x-auto my-3"
    >
      {{ budgets }}
    </div>

    <Callout
      kind="measured"
      title="1080p60 with 200-instruction shaders is now just PAST the ceiling rather than just under it"
    >
      <p>
        111 % of issue, where the 380 MHz working assumption this page
        previously carried put it at 102 %. The measured clock moved the
        crossing point, and the honest form of the claim is the instruction
        budget: <b>180 per fragment at 1080p60, 810 at 720p30</b>.
      </p>
      <p>
        The supporting traffic is not the constraint either, and it does not
        move with the clock. A framebuffer at 720p30 is ~221 MB/s written and
        read; one RGBA bilinear tap per fragment is 12 FMAs and a 32-byte entry
        read, so ~1.8 GB/s at 720p30 — against four DRAM channels.
      </p>
      <p>
        <b
          >So a small real-time renderer at 720p30 is comfortably inside the
          arithmetic, and 1080p60 with rich shaders is at or slightly over the
          ceiling.</b
        >
      </p>
    </Callout>

    <h2 class="doc-h2">What these numbers do not say</h2>
    <p class="doc-p">
      Everything above prices <b>arithmetic</b>. Rendering is not
      arithmetic-bound on this machine, and the gap is fixed function. Every
      item below was re-checked against the tree for this revision and every one
      is still true.
    </p>

    <SpecTable :cols="missing.cols" :rows="missing.rows" />

    <Callout kind="note" title="Precision is NOT on that list">
      <p>
        E8M15 carries FP32's full 8-bit exponent, so range is FP32-equivalent
        and only the significand is short — <b>1.5e-5 relative error</b>, which
        is 32× better than the fp16 that mobile fragment shaders actually run
        at, and more range than the FP24 that shipped in DX9-era hardware. For
        colour, filter weights and interpolation this is above the bar rather
        than below it.
      </p>
      <p>
        The only mobile advantage is a <i>rate</i> one: those parts run fp16 at
        double their FP32 rate, bought by dropping to E5M10.
      </p>
      <p>
        <b>Integer multiply is no longer on the list either.</b> RV32M
        <code>mul</code>/<code>mulh</code>/<code>mulhsu</code>/<code
          >mulhu</code
        >
        are built, one 33×33 signed product per lane, so
        <code>y * width + x</code> is one instruction rather than a shift-add
        chain. Divide and remainder still fault, deliberately —
        divide-by-a-constant strength-reduces to <code>mulhu</code>.
      </p>
    </Callout>

    <h2 class="doc-h2">What to measure next, in order</h2>

    <SpecTable
      :cols="next.cols"
      :rows="next.rows"
      caption="Item 1 of the previous revision — “the eight-lane float build, which settles the Fmax derate and turns ~22k LUT from ESTIMATE into a number” — is DONE, and the measured table above is that number."
    />

    <Callout
      kind="open"
      title="Until item 1 lands, treat the frame budgets as an upper bound on what the arithmetic permits, not a frame rate"
    >
      <p>
        Nothing on this page prices triangle setup, coverage, or the driver that
        would have to produce the shader in the first place.
      </p>
    </Callout>
  </DocPage>
</template>
