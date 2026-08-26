<script setup>
// The system node: the hub owns every mesh attachment; MAG, the mover, the
// control agent, the interlink and the processor are all its clients. One AXI
// master to DRAM; one reach to the mesh, shared.
const node = {
  groups: [{ x: -1, y: -3, w: 62, h: 27, label: "the system node — one per mesh" }],
  nodes: [
    {
      id: "hub",
      x: 2,
      y: 2,
      w: 11,
      h: 18,
      label: "hub",
      sub: "the only thing that owns an attachment",
      accent: true,
    },
    { id: "mag", x: 20, y: -1, w: 16, h: 4.5, label: "MAG", sub: "descriptors → DRAM" },
    {
      id: "mover",
      x: 20,
      y: 5,
      w: 16,
      h: 4.5,
      label: "mover",
      sub: "6-D strided copy + transform slot",
    },
    {
      id: "agent",
      x: 20,
      y: 11,
      w: 16,
      h: 4.5,
      label: "control agent",
      sub: "host's reach into the mesh",
    },
    {
      id: "proc",
      x: 20,
      y: 17,
      w: 16,
      h: 4.5,
      label: "control processor",
      sub: "structural, never optional",
    },
    {
      id: "ilink",
      x: 44,
      y: 5,
      w: 15,
      h: 4.5,
      label: "interlink",
      sub: "to the next mesh",
      accent: true,
    },
    { id: "dram", x: 44, y: 11, w: 15, h: 4.5, label: "DRAM", sub: "one AXI master" },
  ],
  edges: [
    { from: "hub:r", to: "mag:l", dir: "h", accent: true },
    { from: "hub:r", to: "mover:l", dir: "h" },
    { from: "hub:r", to: "agent:l", dir: "h" },
    { from: "hub:r", to: "proc:l", dir: "h" },
    { from: "mag:r", to: "dram:l", dir: "h", accent: true, label: "AXI" },
    { from: "mover:r", to: "ilink:l", dir: "h" },
  ],
};

// A descriptor is a shape, not an address: one flit becomes a whole strided
// burst, and an element outside the tensor is defined rather than undefined.
const descr = {
  nodes: [
    {
      id: "flit",
      x: 0,
      y: 5,
      w: 16,
      h: 5,
      label: "one descriptor",
      sub: "base · counts · strides",
      accent: true,
    },
    {
      id: "mover",
      x: 24,
      y: 5,
      w: 14,
      h: 5,
      label: "the mover",
      sub: "walks it",
    },
    { id: "b0", x: 46, y: 0, w: 20, h: 3, label: "DRAM burst" },
    { id: "b1", x: 46, y: 4, w: 20, h: 3, label: "DRAM burst" },
    { id: "b2", x: 46, y: 8, w: 20, h: 3, label: "DRAM burst" },
    { id: "b3", x: 46, y: 12, w: 20, h: 3, label: "… bounded" },
  ],
  edges: [
    { from: "flit:r", to: "mover:l", dir: "h", accent: true },
    { from: "mover:r", to: "b0:l", dir: "h" },
    { from: "mover:r", to: "b1:l", dir: "h" },
    { from: "mover:r", to: "b2:l", dir: "h" },
    { from: "mover:r", to: "b3:l", dir: "h" },
  ],
};

// The station bus: a line of identical stations, no root, per-port cost
// independent of how many other ports exist.
const bus = {
  nodes: [
    { id: "xdma", x: 0, y: 0, w: 12, h: 4, label: "XDMA", sub: "host DMA" },
    { id: "jtag", x: 0, y: 6, w: 12, h: 4, label: "JTAG", sub: "debug" },
    { id: "s0", x: 18, y: 3, w: 11, h: 4.5, label: "station", sub: "SLR 0", accent: true },
    { id: "s1", x: 33, y: 3, w: 11, h: 4.5, label: "station", sub: "SLR 1", accent: true },
    { id: "s2", x: 48, y: 3, w: 11, h: 4.5, label: "station", sub: "SLR 2", accent: true },
    { id: "n0", x: 33, y: 11, w: 11, h: 4, label: "system node", sub: "mesh 1" },
    { id: "n2", x: 48, y: 11, w: 11, h: 4, label: "system node", sub: "mesh 2" },
  ],
  edges: [
    { from: "xdma:r", to: "s0:l", dir: "h" },
    { from: "jtag:r", to: "s0:l", dir: "h" },
    { from: "s0:r", to: "s1:l", dir: "h", accent: true, label: "link + CDC" },
    { from: "s1:r", to: "s2:l", dir: "h", accent: true },
    { from: "s1:b", to: "n0:t", dir: "v" },
    { from: "s2:b", to: "n2:t", dir: "v" },
  ],
};

const parts = {
  cols: [
    { key: "p", label: "The generic spine, shipped" },
    { key: "d", label: "what it is" },
  ],
  rows: [
    {
      p: "noc/ — the mesh",
      d: "XY routers, the orchestrator, the L2 endpoint adapter, and noc_cu_base — which every compute unit wraps, so a unit conforms by construction.",
    },
    {
      p: "sysnode/ — the system node",
      d: "MAG the memory gateway, the descriptor-driven mover with its transform slot, and the interlink that joins one mesh to the next.",
    },
    {
      p: "axi/ — the station bus",
      d: "a line of stations carrying host traffic to every die of a multi-SLR part, with per-station clocks and link CDCs.",
    },
    {
      p: "the 40-bit address map",
      d: "one global space across all meshes; a request's own mesh and its neighbours are named in the same address.",
    },
  ],
};
</script>

<template>
  <div class="container-page pb-24">
    <section class="pt-8 pb-6">
      <p
        class="kt-text-caption uppercase tracking-widest text-warm-400 dark:text-warm-600 font-semibold mb-3"
      >
        The idea · Stage 2 of 4
      </p>
      <h1
        class="text-3xl sm:text-4xl font-semibold tracking-tight text-warm-900 dark:text-warm-100 leading-tight"
      >
        A memory unit and a NoC make a platform.
      </h1>
      <p
        class="kt-text-emphasis text-warm-600 dark:text-warm-400 mt-4 max-w-[72ch] leading-7"
      >
        The generic part — the majority of the work behind KohakuTPU — got named,
        separated and shipped. Two pieces are the whole foundation: a
        <b>memory unit</b> and an <b>on-chip network</b>. Together they are a shape
        anything can sit on.
      </p>
    </section>

    <h2 class="doc-h2">The mesh, and the one node that owns the outside</h2>
    <p class="doc-p">
      A <b>mesh</b> is one grid of XY routers, the endpoints hanging off them, and
      exactly <b>one system node</b> with its own DRAM behind it — all in one
      clock domain, with the fabric ending at the mesh edge so a router never
      knows another mesh exists. Put a compute unit on a router's local port and
      it inherits messaging and addressing by coordinate. It never contains a
      memory system: it <i>names</i> what it wants, and the node serves it.
    </p>
    <Fig
      caption="Inside the system node, the hub is the only thing that owns a mesh attachment — MAG, the mover, the control agent, the interlink and the control processor are all its clients, so a stalled one cannot hold up the others. MAG owns the single AXI master to DRAM; the interlink is the only path to another mesh. The node is the single point where a mesh touches anything outside it."
      zoom
      wide
    >
      <BlockDiagram :nodes="node.nodes" :edges="node.edges" :groups="node.groups" />
    </Fig>

    <h2 class="doc-h2">Memory is a service, addressed by descriptor</h2>
    <p class="doc-p">
      <b>MAG</b> — the memory access gateway — turns a <b>descriptor</b> into DRAM
      traffic. A descriptor is a shape, not a single address: base, counts and
      strides, so one flit produces a whole burst instead of one request per word,
      and an address is computable ahead of time rather than discovered by
      chasing a pointer. The <b>mover</b> walks the richest ones — strided,
      six-dimensional, and bounded, so an element outside the tensor is defined
      rather than undefined — which is why a transpose or a gather is a
      <i>descriptor</i>, not missing hardware.
    </p>
    <Fig
      caption="One descriptor, walked by the mover, becomes a bounded sequence of DRAM bursts. This is why a compute unit asks for a tensor shape and gets it, never issuing an address itself."
      wide
    >
      <BlockDiagram :nodes="descr.nodes" :edges="descr.edges" />
    </Fig>

    <h2 class="doc-h2">The host reaches in over a line, not a crossbar</h2>
    <p class="doc-p">
      Outside the mesh, host traffic crosses the card on the <b>station bus</b>: a
      line of identical stations, each with any number of local masters and
      slaves and exactly two neighbours — <b>there is no root.</b> It replaces a
      crossbar, whose cost grows with masters times slaves, with a structure
      whose per-port cost does not depend on the port count. Each station carries
      its own clock and the link between two carries a CDC, so a design spread
      across a stack of dice stays a line.
    </p>
    <Fig
      caption="The station bus as deployed: XDMA and JTAG enter at one end, and each mesh's system node hangs off the station on its own die. Width, clock and protocol differences are resolved once per port at its shim — never pairwise between ports."
      wide
    >
      <BlockDiagram :nodes="bus.nodes" :edges="bus.edges" />
    </Fig>

    <h2 class="doc-h2">One shape, many machines</h2>
    <p class="doc-p">
      A memory gateway and a NoC are not specific to any accelerator — together
      they are a <b>common platform</b>, and the network is what lets many
      different things live in the same macro shape. Two compute units on one
      mesh need agree on nothing but the port they attach through. The interlink
      then joins meshes edge to edge — cross-mesh traffic is write-only, released
      by a doorbell whose arrival means the data has landed in memory — so the
      shape scales past a single die without changing.
    </p>
    <SpecTable :cols="parts.cols" :rows="parts.rows" />

    <Callout kind="measured" title="Reuse stopped being a bonus">
      <p>
        Once the substrate was named, reuse became the entire premise. Build the
        platform once; every new unit — a matrix cluster, a vector core, later a
        processor — is just another thing you attach to it. What was left was to
        turn a platform you rebuild per project into a framework you build
        <i>against</i>.
      </p>
    </Callout>

    <div class="mt-8 pt-5 border-t border-warm-200 dark:border-warm-700 flex justify-between gap-4">
      <RouterLink
        to="/idea/beginning"
        class="inline-flex items-center gap-1.5 kt-text-body text-warm-500 dark:text-warm-400 no-underline hover:text-warm-800 dark:hover:text-warm-200"
      >
        <span class="i-carbon-arrow-left" />
        One accelerator
      </RouterLink>
      <RouterLink
        to="/idea/framework"
        class="inline-flex items-center gap-1.5 kt-text-body font-medium text-gem no-underline hover:gap-2.5 transition-all text-right"
      >
        Next — from a platform to a framework
        <span class="i-carbon-arrow-right" />
      </RouterLink>
    </div>
  </div>
</template>
