<script setup>
import { SECTIONS } from "@/site";
import { gemVars } from "@/utils/colors";

const die = {
  nodes: [
    { id: "host", x: 0, y: 0, w: 11, label: "host", sub: "PCIe" },
    { id: "xdma", x: 0, y: 5, w: 11, label: "XDMA" },
    { id: "jtag", x: 15, y: 5, w: 11, label: "JTAG-AXI", sub: "debug" },
    {
      id: "axi",
      x: 0,
      y: 10,
      w: 26,
      label: "AXI fabric",
      sub: "kohakuaxi — station bus",
    },
    { id: "ddr", x: 0, y: 15, w: 11, label: "DDR4 x N" },
    {
      id: "mag",
      x: 15,
      y: 15,
      w: 11,
      label: "system node",
      sub: "MAG + ctrl PE, one component",
      accent: true,
    },
    {
      id: "mesh",
      x: 0,
      y: 21,
      w: 26,
      h: 4,
      label: "mesh",
      sub: "kohakunoc — routers + local ports",
      accent: true,
    },
    { id: "cu", x: 0, y: 27, w: 12.5, label: "compute unit", sub: "yours" },
    { id: "l2", x: 13.5, y: 27, w: 12.5, label: "L2 adapter", sub: "addon" },
  ],
  edges: [
    { from: "host:b", to: "xdma:t", dir: "v" },
    { from: "xdma:b", to: "axi:t", dir: "v" },
    { from: "jtag:b", to: "axi:t", dir: "v" },
    { from: "axi:b", to: "ddr:t", dir: "v" },
    { from: "axi:b", to: "mag:t", dir: "v", accent: true },
    { from: "ddr:b", to: "mag:l", dir: "h" },
    { from: "mag:b", to: "mesh:t", dir: "v", accent: true },
    { from: "mesh:b", to: "cu:t", dir: "v", accent: true },
    { from: "mesh:b", to: "l2:t", dir: "v" },
  ],
};
</script>

<template>
  <div class="container-page pb-24">
    <section class="pt-8 pb-10">
      <p
        class="kt-text-caption uppercase tracking-widest text-warm-400 dark:text-warm-600 font-semibold mb-3"
      >
        Architecture reference
      </p>
      <h1
        class="text-4xl sm:text-5xl font-semibold tracking-tight text-warm-900 dark:text-warm-100 leading-tight"
      >
        KohakuAccel
      </h1>
      <p
        class="kt-text-emphasis text-warm-600 dark:text-warm-400 mt-4 max-w-[68ch] leading-7"
      >
        A framework for building FPGA accelerators around a compute unit you
        design. You write the datapath; the framework is DRAM and its
        controllers, a memory agent that turns descriptors into transfers, an
        on-chip network, the host interface, floorplanning, clock domains, and
        the flow that closes timing.
      </p>
      <p
        class="kt-text-caption text-warm-500 dark:text-warm-400 mt-4 max-w-[68ch] leading-6"
      >
        These pages are the visual companion to
        <span class="chip">docs/</span> — the same content, drawn. Every
        mechanism gets a diagram; every number carries where it was measured.
      </p>
    </section>

    <Fig
      caption="What is actually on the die. A ship is one complete assembly of this, floorplanned for a specific device; an image may hold several meshes, one per SLR, joined by the interlink."
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
