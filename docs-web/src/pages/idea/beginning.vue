<script setup>
// KohakuTPU as it sits on a mesh today: matrix clusters and vector cores on
// the endpoints, one system node with DRAM behind it, the host reaching in.
const machine = {
  groups: [{ x: 26.5, y: -3, w: 58, h: 25, label: "one mesh of four on the device" }],
  nodes: [
    { id: "host", x: 0, y: 8, w: 12, h: 5, label: "host", sub: "Python driver" },
    {
      id: "node",
      x: 27,
      y: 7,
      w: 12,
      h: 7,
      label: "system node",
      sub: "MAG · mover · transform slot",
      accent: true,
    },
    { id: "dram", x: 27, y: 16, w: 12, h: 4, label: "DRAM" },
    {
      id: "mesh",
      x: 43,
      y: 1.5,
      w: 10,
      h: 18,
      label: "mesh",
      sub: "XY routers",
      accent: true,
    },
    {
      id: "mat",
      x: 58,
      y: 1,
      w: 24,
      h: 5,
      label: "matrix cluster",
      sub: "DSP48E2 chains · int7 × E5M3",
      accent: true,
    },
    {
      id: "vec",
      x: 58,
      y: 8,
      w: 24,
      h: 5,
      label: "vector core",
      sub: "16 ALU lanes · E8M15",
      accent: true,
    },
    {
      id: "more",
      x: 58,
      y: 15,
      w: 24,
      h: 4,
      label: "… more units, same port",
    },
  ],
  edges: [
    { from: "host:r", to: "node:l", dir: "h", label: "AXI" },
    { from: "node:b", to: "dram:t", dir: "v", accent: true },
    { from: "node:r", to: "mesh:l", dir: "h", accent: true, label: "flits" },
    { from: "mesh:r", to: "mat:l", dir: "h", accent: true },
    { from: "mesh:r", to: "vec:l", dir: "h", accent: true },
    { from: "mesh:r", to: "more:l", dir: "h" },
  ],
};

// Four tensor CUs cascade through the DSP48E2's PCOUT -> PCIN chain: the
// multiply and the whole K=32 reduction happen inside the hard blocks.
const cascade = {
  nodes: [
    { id: "a", x: 0, y: 4, w: 12, h: 4.5, label: "tensor CU", sub: "K 0–7", accent: true },
    { id: "b", x: 16, y: 4, w: 12, h: 4.5, label: "tensor CU", sub: "K 8–15", accent: true },
    { id: "c", x: 32, y: 4, w: 12, h: 4.5, label: "tensor CU", sub: "K 16–23", accent: true },
    { id: "d", x: 48, y: 4, w: 12, h: 4.5, label: "tensor CU", sub: "K 24–31", accent: true },
    { id: "acc", x: 66, y: 4, w: 14, h: 4.5, label: "FP22 accumulator", sub: "no LUT MAC" },
  ],
  edges: [
    { from: "a:r", to: "b:l", dir: "h", accent: true, label: "PCOUT→PCIN" },
    { from: "b:r", to: "c:l", dir: "h", accent: true },
    { from: "c:r", to: "d:l", dir: "h", accent: true },
    { from: "d:r", to: "acc:l", dir: "h", accent: true },
  ],
};

const fmt = {
  cols: [
    { key: "f", label: "8-bit block scale" },
    { key: "waste", label: "what it costs" },
    { key: "err", label: "p50 rel. error" },
  ],
  rows: [
    {
      f: "E8M0 (power-of-two)",
      waste: "wastes up to a full bit of significand, depending where the block's peak falls",
      err: "0.54%",
    },
    {
      f: "E5M3 (KohakuTPU)",
      waste: "three mantissa bits put the block peak at 63 every time — same 8 bits, no waste",
      err: "0.38%",
    },
  ],
};

const split = {
  cols: [
    { key: "p", label: "What building KohakuTPU took" },
    { key: "kind", label: "specific to tensors?" },
  ],
  rows: [
    { p: "The DSP48E2 packing and cascade", kind: "yes — the clever part" },
    { p: "The FP22 accumulator and the number format", kind: "yes" },
    { p: "The matmul cluster and its residency", kind: "yes" },
    { p: "A network to carry operands between units", kind: "no — every accelerator needs it" },
    { p: "A memory agent that turns requests into DRAM traffic", kind: "no" },
    { p: "Dispatch, completion, and a host interface", kind: "no" },
  ],
};
</script>

<template>
  <div class="container-page pb-24">
    <section class="pt-8 pb-6">
      <p
        class="kt-text-caption uppercase tracking-widest text-warm-400 dark:text-warm-600 font-semibold mb-3"
      >
        The idea · Stage 1 of 4
      </p>
      <h1
        class="text-3xl sm:text-4xl font-semibold tracking-tight text-warm-900 dark:text-warm-100 leading-tight"
      >
        It began as one accelerator.
      </h1>
      <p
        class="kt-text-emphasis text-warm-600 dark:text-warm-400 mt-4 max-w-[72ch] leading-7"
      >
        There was no framework at the start, and no plan for one. There was
        <b>KohakuTPU</b>: a low-precision tensor engine, and one question — can it
        run fast on an FPGA? It is still the flagship, and it is what everything
        else grew out of.
      </p>
    </section>

    <h2 class="doc-h2">What KohakuTPU is</h2>
    <p class="doc-p">
      Matrix clusters and vector cores sitting on a mesh — four meshes on one
      device — programmed from Python. You write a kernel as a function, call it
      like one, and the compiler places it; the only line that crosses the link
      is the one that reads the answer back. Underneath, three things make it
      worth building, and all three are about squeezing arithmetic onto an FPGA's
      hard blocks.
    </p>

    <Fig
      caption="KohakuTPU on one of its four meshes. The matrix cluster and the vector core are just compute units on endpoints; the system node holds the memory agent and the mover and owns DRAM; the host drives it all from Python over AXI. Nothing on this picture is specific to matmul except the two accented units on the right."
      zoom
      wide
    >
      <BlockDiagram :nodes="machine.nodes" :edges="machine.edges" :groups="machine.groups" />
    </Fig>

    <h2 class="doc-h2">MACs that cost zero LUTs</h2>
    <p class="doc-p">
      Four tensor compute units chain through the DSP48E2's <code>PCOUT →
      PCIN</code> cascade. The multiply <i>and</i> the entire <code>K=32</code>
      reduction happen inside the DSPs — the fabric holds control, not
      arithmetic, so the multiply-accumulate array spends no general logic at
      all. That is the property the whole machine is organised around.
    </p>
    <Fig
      caption="The cascade: each tensor CU covers eight steps of the K reduction and passes its running sum to the next through the dedicated PCOUT→PCIN wire. The sum never leaves the DSP column until it lands in the FP22 accumulator."
      wide
    >
      <BlockDiagram :nodes="cascade.nodes" :edges="cascade.edges" />
    </Fig>

    <h2 class="doc-h2">A number format built for the DSP</h2>
    <p class="doc-p">
      Elements are <b>int7</b> with an <b>E5M3</b> scale shared by a block of 32.
      It is a microscaling format, but the scale is deliberately <i>not</i> a
      power of two. A power-of-two (E8M0) scale wastes up to a whole bit of
      significand depending on where a block's peak falls in its binade; three
      mantissa bits pin that peak at 63 every time. The field is still 8 bits —
      the accuracy is free.
    </p>
    <SpecTable
      :cols="fmt.cols"
      :rows="fmt.rows"
      caption="Measured per element on correlated operands. The gain is a better use of the same 8 bits, not a bigger field."
    />

    <h2 class="doc-h2">The clue that started everything</h2>
    <p class="doc-p">
      There is a fourth property, and it is an arithmetic one: a cluster needs
      only <b>two mesh ports, not five</b>. The DSP chain eats eight operand
      words per cycle and a port delivers one, so more ports never close that
      gap — holding a large output tile resident does. A <code>Gm × Gn</code>
      block needs <code>4(Gm+Gn)/(Gm·Gn)</code> words per cycle, which is 0.375 at
      16×32. Every one of these decisions is about the datapath. And that is
      exactly the point that mattered in hindsight.
    </p>
    <SpecTable
      :cols="split.cols"
      :rows="split.rows"
      caption="By the time the engine ran, the clever KohakuTPU-specific work was the minority of what had been built. The majority was generic — and a completely different accelerator would have needed it, built the same way."
    />

    <Callout kind="measured" title="The first clue">
      <p>
        The datapath was the small part. The system around it — the network, the
        memory agent, the dispatch, the host reach — was the large part, and the
        large part was <b>generic</b>. That single observation is what the rest
        of this story is built on: if the scaffolding is the same every time,
        build it once and share it. Do not rebuild it for every accelerator.
      </p>
    </Callout>

    <div class="mt-8 pt-5 border-t border-warm-200 dark:border-warm-700 flex justify-between gap-4">
      <RouterLink
        to="/idea"
        class="inline-flex items-center gap-1.5 kt-text-body text-warm-500 dark:text-warm-400 no-underline hover:text-warm-800 dark:hover:text-warm-200"
      >
        <span class="i-carbon-arrow-left" />
        The idea
      </RouterLink>
      <RouterLink
        to="/idea/platform"
        class="inline-flex items-center gap-1.5 kt-text-body font-medium text-gem no-underline hover:gap-2.5 transition-all text-right"
      >
        Next — a memory unit and a NoC make a platform
        <span class="i-carbon-arrow-right" />
      </RouterLink>
    </div>
  </div>
</template>
