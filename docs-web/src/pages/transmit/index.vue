<script setup>
/* Every figure on this page: xcvu13p-fhgb2104-2L-e, Vivado 2024.2,
 * out-of-context synthesis at a 3.333 ns ask, one run per configuration,
 * 2026-08-31. Fmax is 1000 / (period - WNS), the synthesis estimate. */

const layers = {
  cols: [
    { key: "layer", label: "Layer" },
    { key: "mods", label: "Modules" },
    { key: "what", label: "What it adds" },
  ],
  rows: [
    {
      layer: "<b>surface</b>",
      mods: "<code>kts_tx</code>, <code>kts_rx</code>",
      what: "the credited wire: a forward wire of flits, a backward wire of credit counts, no <code>ready</code> on either; virtual channels; credits issued by the receiver once its buffers leave reset; batching",
      _tone: "good",
    },
    {
      layer: "<b>carriers</b>",
      mods: "<code>kts_pipe</code>, <code>kts_cdc</code>, <code>kts_wconv</code>, <code>kts_over_axis</code>, <code>kts_over_axi4</code>, <code>kts_over_serial</code>",
      what: "the wire's length: register stages at zero LUT; a clock crossing; a width change; the surface tunnelled through an AXI4-Stream path, an AXI4 interconnect as posted writes, or a word stream with framing, sequence numbers and go-back-N replay",
    },
    {
      layer: "<b>packets</b>",
      mods: "<code>kts_pkt.vh</code>, <code>kts_switch</code>",
      what: "a header flit — kind, destination, source, byte length, tag; a K-port switch routing by destination range, packet-atomic per output and VC, reading registers",
    },
    {
      layer: "<b>bridges</b>",
      mods: "<code>kts_axi4_in/out</code>, <code>kts_axis_in/out</code>",
      what: "AXI4 and AXI4-Stream carried <i>over</i> a surface: an existing endpoint reaches across any wire; the far end's AXI ID is the packet's tag",
    },
    {
      layer: "<b>primitives</b>",
      mods: "<code>kts_fifo</code>, <code>kts_afifo</code>, <code>kts_ram</code>",
      what: "the project's own named memories; it imports nothing",
    },
  ],
};

const stage = {
  cols: [
    { key: "what", label: "One register stage on the wire, W = 288" },
    { key: "lut", label: "LUT" },
    { key: "ff", label: "FF" },
  ],
  rows: [
    { what: "<b><code>kts_pipe</code>, N = 1</b>", lut: "<b>0</b>", ff: "291", _tone: "good" },
    { what: "<code>kts_ref_skid</code> — one valid/ready stage", lut: "152", ff: "581" },
    { what: "<code>axis_register_slice</code>, fully registered (vendor)", lut: "300", ff: "584" },
    { what: "<code>axis_register_slice</code>, SLR-crossing mode (vendor)", lut: "590", ff: "887" },
  ],
};

const ends = {
  cols: [
    { key: "m", label: "Module" },
    { key: "w", label: "W" },
    { key: "lut", label: "LUT" },
    { key: "ff", label: "FF" },
    { key: "f", label: "est. MHz" },
  ],
  rows: [
    { m: "<code>kts_tx</code>, VC 2", w: "64 / 288 / 512 / 1024", lut: "68 / 178 / 290 / 546", ff: "82 / 306 / 530 / 1,042", f: "782 / 629 / 624 / 618" },
    { m: "<code>kts_rx</code>, VC 2, D 32, LUTRAM", w: "64 / 288 / 512 / 1024", lut: "190 / 446 / 702 / 1,288", ff: "420 / 1,540 / 2,660 / 5,220", f: "576" },
    { m: "<b><code>kts_rx</code>, VC 2, D 128, block RAM</b>", w: "288", lut: "<b>189</b> + 9 BRAM", ff: "404", f: "820", _tone: "good" },
    { m: "<code>kts_cdc</code>, both directions", w: "64 / 288 / 512 / 1024", lut: "333 / 589 / 845 / 1,432", ff: "554 / 1,226 / 1,898 / 3,434", f: "667" },
    { m: "<code>kts_wconv</code> 288 → 144", w: "", lut: "699", ff: "1,704", f: "562" },
    { m: "<code>kts_switch</code>, K 3, two-entry head queues", w: "64 / 288 / 512 / 1024", lut: "1,495 / 6,891 / 7,318 / 15,416", ff: "1,578 / 5,610 / 9,648 / 18,876", f: "447 / 423 / 434 / 390" },
    { m: "<code>kts_axi4_in</code> / <code>kts_axi4_out</code>, 256-bit data", w: "288", lut: "627 / 434", ff: "1,347 / 1,295", f: "610 / 572" },
    { m: "<code>kts_over_axis</code> / <code>kts_over_axi4</code>", w: "288", lut: "415 / 451", ff: "926 / 1,253", f: "676 / 672" },
    { m: "<code>kts_over_serial</code>, RELIABLE 0 / 1", w: "288", lut: "1,137 / 1,305", ff: "2,049 / 2,431", f: "578 / 480" },
  ],
};

const rate = {
  cols: [
    { key: "n", label: "Register stages on the wire" },
    { key: "m", label: "Measured flits / cycle" },
    { key: "b", label: "Bound min(1, VC·D / RTT)" },
  ],
  rows: [
    { n: "0", m: "<b>1.000</b>", b: "1.000" },
    { n: "4", m: "<b>1.000</b>", b: "1.000" },
    { n: "32", m: "<b>0.420</b>", b: "0.450 (RTT 71)" },
  ],
};
</script>

<template>
  <DocPage
    title="Kohaku Transmit Surface"
    summary="A credit-based, latency-insensitive transport for FPGA links — on a die, across dies, across chips — with no ready on the wire, any width, and one register-to-register path per stage. A standalone project that imports nothing."
    domain="framework"
    status="measured"
    source="src/kohakutransmit/ · docs/projects/kohakutransmit/"
  >
    <p class="doc-p">
      A <b>surface</b> is one direction of a link: a forward wire carrying
      flits and a backward wire carrying credits, and nothing else. The sender
      emits a flit only while it holds a credit for that flit's virtual
      channel; the receiver's buffer is exactly as deep as the credits it
      issued; a credit goes back for every flit the consumer takes out. There
      is no <code>ready</code> on either wire, so the sender never waits on the
      receiver in the same cycle — and the wire between them may be nothing, a
      dozen register stages, a die boundary, a clock crossing or a serial
      transceiver to another chip. The only thing that changes is the round
      trip the credits have to cover.
    </p>

    <SpecTable
      :cols="layers.cols"
      :rows="layers.rows"
      caption="Two directions of bridging are both present: a bridge carries AXI over KTS; a carrier kts_over_* carries KTS over AXI4-Stream, AXI4 or a serial stream, and the same surface comes out the far end"
    />

    <h2 class="doc-h2">What a stage costs, and what it replaces</h2>

    <p class="doc-p">
      Against a valid/ready channel the difference is structural:
      <code>ready</code> travels against the data in the same cycle, so every
      register stage on such a channel is a skid buffer with a combinational
      path through it, every clock crossing is a FIFO per channel, and a die
      crossing that must be flop-to-flop with nothing in between cannot carry
      <code>ready</code> at all. A surface has no such signal to carry.
    </p>

    <SpecTable
      :cols="stage.cols"
      :rows="stage.rows"
      caption="The pipe stage is the number the project is built around: a stage costs registers only, so the length of a wire is a question of latency and buffer depth, never of logic or of a path"
    />

    <h2 class="doc-h2">The ends, the carriers, the switch, the bridges</h2>

    <SpecTable
      :cols="ends.cols"
      :rows="ends.rows"
      caption="A receiving end that covers a 128-cycle round trip in block RAM costs fewer LUTs than one covering 32 in LUTRAM. The switch's cost is its K:1 return select of W bits per output per VC; its arbiters read registers and every width closes with margin"
    />

    <h2 class="doc-h2">Throughput against the credit bound</h2>

    <p class="doc-p">
      Three copies of one link on wires of 0, 4 and 32 register stages, two
      VCs of 16 credits each, every VC offering and popping every cycle for
      4,000 cycles. Every flit in order, none lost, none duplicated; the
      measured rate is the formula.
    </p>

    <SpecTable :cols="rate.cols" :rows="rate.rows" />

    <Callout kind="trap" title="Credits start at zero, and the receiver says how many">
      <p>
        Every credit the sender will ever hold comes over the backward wire:
        the receiver issues its depth per VC once its buffers are out of
        reset, then one per flit its consumer removes. A sender never needs to
        know the receiver's depth, two ends configured differently still
        interoperate, and no flit is ever sent into a buffer that is not yet
        listening. A carrier that must buffer flits sizes that buffer at the
        credits in flight and cannot overflow; a carrier that must buffer
        credits merges them, because they are counts.
      </p>
    </Callout>

    <h2 class="doc-h2">The reference implementations</h2>

    <p class="doc-p">
      Nine benches, each the smallest design showing one property, all in the
      standard check: a link across a wire of any length; a surface tunnelled
      through four AXI4-Stream register slices with random ready; a surface
      tunnelled through an AXI4 interconnect model as posted writes; a surface
      over a word stream that drops one word in forty, with go-back-N replay
      against a lossless control; AXI4 across a surface with several
      transactions outstanding; AXI4-Stream across a surface; a 3:1 and a 1:3
      clock crossing; a 96 → 32 → 96 width conversion; a three-port line
      switch. The specification, the usage manual and every number are
      <code>docs/projects/kohakutransmit/</code>.
    </p>
  </DocPage>
</template>
