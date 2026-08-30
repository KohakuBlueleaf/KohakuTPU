<script setup>
import { SECTIONS } from "@/site";
import { gemVars } from "@/utils/colors";

const die = {
  nodes: [
    { id: "host", x: 0, y: 7, w: 8, h: 5, label: "host", sub: "PCIe" },
    { id: "xdma", x: 11, y: 2.5, w: 8, h: 5, label: "XDMA" },
    {
      id: "jtag",
      x: 11,
      y: 11.5,
      w: 8,
      h: 5,
      label: "JTAG-AXI",
      sub: "debug",
    },
    {
      id: "axi",
      x: 22,
      y: 2,
      w: 8.5,
      h: 15,
      label: "AXI fabric",
      sub: "kohakuaxi — station bus",
    },
    { id: "ddr", x: 33.5, y: 0, w: 8.5, h: 5, label: "DDR4 × N" },
    {
      id: "mag",
      x: 33.5,
      y: 7,
      w: 8.5,
      h: 9,
      label: "system node",
      sub: "MAG + RV64 runtime host, one component",
      accent: true,
    },
    {
      id: "mesh",
      x: 45,
      y: 4,
      w: 8.5,
      h: 11,
      label: "mesh",
      sub: "kohakunoc — routers + local ports",
      accent: true,
    },
    {
      id: "cu",
      x: 56.5,
      y: 0,
      w: 8.5,
      h: 5,
      label: "compute unit",
      sub: "yours",
      accent: true,
    },
    {
      id: "l2",
      x: 56.5,
      y: 7,
      w: 8.5,
      h: 5,
      label: "L2 adapter",
      sub: "addon",
    },
    {
      id: "rv32",
      x: 56.5,
      y: 14,
      w: 8.5,
      h: 5,
      label: "RV32 PE",
      sub: "a unit that is a CPU",
    },
  ],
  edges: [
    { from: "host:r", to: "xdma:l", dir: "h" },
    { from: "host:r", to: "jtag:l", dir: "h" },
    { from: "xdma:r", to: "axi:l", dir: "h" },
    { from: "jtag:r", to: "axi:l", dir: "h" },
    { from: "axi:r", to: "ddr:l", dir: "h", dash: true, label: "control" },
    { from: "axi:r", to: "mag:l", dir: "h", accent: true, label: "data" },
    { from: "mag:t", to: "ddr:b", dir: "v", accent: true, label: "memory" },
    { from: "mag:r", to: "mesh:l", dir: "h", accent: true },
    { from: "mesh:r", to: "cu:l", dir: "h", accent: true },
    { from: "mesh:r", to: "l2:l", dir: "h" },
    { from: "mesh:r", to: "rv32:l", dir: "h" },
  ],
};
</script>

<template>
  <div class="container-page pb-24">
    <section class="pt-8 pb-10">
      <p
        class="kt-text-caption uppercase tracking-widest text-warm-400 dark:text-warm-600 font-semibold mb-3"
      >
        A framework for FPGA accelerators
      </p>
      <h1
        class="text-4xl sm:text-5xl font-semibold tracking-tight text-warm-900 dark:text-warm-100 leading-tight"
      >
        KohakuAccel
      </h1>
      <p
        class="kt-text-emphasis text-warm-600 dark:text-warm-400 mt-4 max-w-[68ch] leading-7"
      >
        You write a compute datapath. The framework brings everything around it:
        DRAM and its controllers, a memory agent that turns descriptors into
        transfers, an on-chip network, the host interface, a programmable
        runtime host, floorplanning, and the flow that closes timing.
      </p>

      <div
        class="mt-6 rounded-xl border border-warm-200 dark:border-warm-700 bg-warm-100/60 dark:bg-warm-900/40 p-5 max-w-[70ch]"
      >
        <p
          class="kt-text-caption uppercase tracking-widest text-warm-400 dark:text-warm-600 font-semibold mb-2"
        >
          The stand
        </p>
        <p class="kt-text-body text-warm-700 dark:text-warm-300 leading-7">
          One opinion runs through all of it:
          <b>build hardware the way frontier software is built.</b> A general
          substrate you program, a fast iteration loop, reuse over re-spin — and
          generality treated as cheap enough to pay for, not a luxury to avoid.
          The system node is the clearest case: there is a cheaper
          fixed-function controller, and we build a full OS-capable RV64
          processor instead, so the hard, changing work becomes a program on the
          card rather than a hardware re-spin.
        </p>
        <RouterLink
          to="/machine"
          class="inline-flex items-center gap-1.5 mt-3 kt-text-body font-medium text-gem no-underline hover:gap-2.5 transition-all"
        >
          Everything we ship — on one sheet
          <span class="i-carbon-arrow-right" />
        </RouterLink>
      </div>

      <p
        class="kt-text-caption text-warm-500 dark:text-warm-400 mt-5 max-w-[68ch] leading-6"
      >
        These pages are the visual companion to
        <span class="chip">docs/</span> — the same content, drawn. Every
        mechanism gets a diagram; every number carries where it was measured.
      </p>
    </section>

    <Fig
      caption="What is actually on the die. DRAM is reached only through the system node: the host and debug write to the AXI surface (data), the node's MAG turns descriptors into DRAM transfers (memory), and the dashed AXI→DDR link is the controller's configuration, not a data path — there is no direct host access to DRAM. A ship is one complete assembly of this, floorplanned for a specific device; an image may hold several meshes, one per die region, joined by the interlink. Two processors are on it and they are not variants of each other: an RV32 PE is an ordinary compute unit that happens to run programs — kicked, it runs to completion and reports one word — while the system node's RV64 host runs the runtime that does the kicking."
      zoom
    >
      <BlockDiagram :nodes="die.nodes" :edges="die.edges" />
    </Fig>

    <h2 class="doc-h2">The tree</h2>
    <div class="grid gap-4 sm:grid-cols-3 mt-6">
      <RouterLink
        v-for="s in SECTIONS"
        :key="s.key"
        :to="s.pages[0].path"
        :style="gemVars(s.domain)"
        class="card-hover p-5 no-underline block group"
      >
        <div class="flex items-center gap-2.5 mb-2">
          <div :class="s.icon" class="text-gem text-lg" />
          <span
            class="kt-text-title font-semibold text-warm-800 dark:text-warm-200"
          >
            {{ s.title }}
          </span>
        </div>
        <p class="kt-text-caption text-warm-500 dark:text-warm-400 leading-6">
          {{ s.blurb }}
        </p>
        <div class="mt-3 flex flex-wrap gap-1.5">
          <span
            v-for="p in s.pages.slice(1)"
            :key="p.path"
            class="gem-badge bg-warm-100 dark:bg-warm-800 text-warm-500 dark:text-warm-400"
          >
            {{ p.short }}
          </span>
        </div>
      </RouterLink>
    </div>

    <Callout kind="rule" title="House rule">
      <p>
        If a page says “comprehensive”, “powerful”, or “seamless”, it is out of
        date. Say what it does, what it costs, and where it stops.
      </p>
    </Callout>
  </div>
</template>
