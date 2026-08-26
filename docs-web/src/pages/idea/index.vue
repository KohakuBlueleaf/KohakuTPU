<script setup>
// The shape: one compute unit on a mesh, a system node holding everything
// outside it, the host reaching in through the station bus.
const shape = {
  groups: [
    { x: 20.5, y: -3, w: 45, h: 24, label: "one mesh — one clock domain" },
  ],
  nodes: [
    { id: "host", x: 0, y: 7.5, w: 13, h: 5, label: "host", sub: "PCIe / JTAG" },
    {
      id: "bus",
      x: 20.5,
      y: 7.5,
      w: 11,
      h: 5,
      label: "station bus",
      sub: "AXI, across the card",
    },
    {
      id: "node",
      x: 36,
      y: 6.5,
      w: 12,
      h: 7,
      label: "system node",
      sub: "MAG · mover · processor · hub",
      accent: true,
    },
    { id: "dram", x: 36, y: 15.5, w: 12, h: 4, label: "DRAM" },
    {
      id: "mesh",
      x: 52,
      y: 1.5,
      w: 11,
      h: 17,
      label: "mesh",
      sub: "XY routers + endpoints",
      accent: true,
    },
    {
      id: "cu",
      x: 70,
      y: 1.5,
      w: 13,
      h: 6,
      label: "YOUR compute unit",
      sub: "datapath + noc_cu_base",
      accent: true,
    },
    {
      id: "cu2",
      x: 70,
      y: 12,
      w: 13,
      h: 6,
      label: "another compute unit",
      sub: "agrees on nothing but the port",
    },
  ],
  edges: [
    { from: "host:r", to: "bus:l", dir: "h" },
    { from: "bus:r", to: "node:l", dir: "h", label: "host reach" },
    { from: "node:b", to: "dram:t", dir: "v", accent: true, label: "AXI master" },
    { from: "node:r", to: "mesh:l", dir: "h", accent: true, label: "flits" },
    { from: "mesh:r", to: "cu:l", dir: "h", accent: true, label: "port" },
    { from: "mesh:r", to: "cu2:l", dir: "h" },
  ],
};

const kinds = {
  cols: [
    { key: "k", label: "The four kinds" },
    { key: "ex", label: "examples" },
    { key: "ch", label: "may you change it" },
  ],
  rows: [
    {
      k: "Fixed protocol",
      ex: "flit format, the port handshake, memory encoding, credits, completion",
      ch: "No — change it and you are off the framework",
    },
    {
      k: "Customizable addon",
      ex: "the mover's transform slot, L2 staging, the endpoint adapter",
      ch: "Yes — that is what the slot is for",
    },
    {
      k: "Convention",
      ex: "how a well-behaved unit is shaped, each marked forced or free",
      ch: "Follow or don't — but know which is which",
    },
    {
      k: "Yours",
      ex: "the datapath, the memories, the instruction meanings, the pipeline",
      ch: "Entirely — the framework has no opinion",
    },
  ],
};

const stages = [
  {
    n: 1,
    to: "/idea/beginning",
    title: "It began as one accelerator",
    line: "KohakuTPU: an int7 tensor engine whose MACs cost zero LUTs. Building it revealed that most of the work was not about tensors at all.",
  },
  {
    n: 2,
    to: "/idea/platform",
    title: "A memory unit and a NoC make a platform",
    line: "The generic part, named and shipped: the mesh, the system node with MAG and the mover, the interlink, the station bus. A shape that hosts anything.",
  },
  {
    n: 3,
    to: "/idea/framework",
    title: "From a platform to a framework",
    line: "Fix the port, ship the parts, and the only thing left to write is a compute unit and an ISA. saxpy proves it: a second accelerator in ~60 lines.",
  },
  {
    n: 4,
    to: "/idea/soc",
    title: "From accelerator to a general SoC platform",
    line: "A processor is a compute unit too — KohakuMPE's SIMD and SIMT PEs, and a full RV64 core growing into the node's runtime host.",
  },
];
</script>

<template>
  <div class="container-page pb-24">
    <section class="pt-8 pb-6">
      <p
        class="kt-text-caption uppercase tracking-widest text-warm-400 dark:text-warm-600 font-semibold mb-3"
      >
        The idea
      </p>
      <h1
        class="text-3xl sm:text-4xl font-semibold tracking-tight text-warm-900 dark:text-warm-100 leading-tight"
      >
        Accelerator research has no standard codebase. This is one.
      </h1>
      <p
        class="kt-text-emphasis text-warm-600 dark:text-warm-400 mt-4 max-w-[72ch] leading-7"
      >
        A field moves fast when there is a codebase to fork. Machine-learning
        research has BasicSR and taming-transformers; you clone one, change the
        part you care about, and run. Accelerator research has nothing like it —
        <b>every new machine rebuilds the same transport, dispatch and memory
        plumbing before its first interesting instruction runs.</b> KohakuAccel
        is built to be the thing you fork instead.
      </p>
    </section>

    <h2 class="doc-h2">The one problem it removes</h2>
    <p class="doc-p">
      The framework does not remove the design work — it removes <b>the
      connection problem.</b> You still design the whole compute unit: the
      datapath, the memories, the pipeline, what the instructions mean. The
      framework has no opinion about any of that. What it fully defines is how
      you <i>receive</i> and how you <i>send</i>: the port, the flit format,
      dispatch, credits, completion, faults, discovery, memory requests,
      unit-to-unit transfer, and cross-mesh addressing. That work is unglamorous,
      it is where the silent failures live, and you do not have to work it out.
    </p>

    <Fig
      caption="The shape everything is built into. You write one compute unit and wrap noc_cu_base; it hangs off a mesh endpoint. Exactly one system node per mesh is the single point where the mesh touches anything outside it — it holds MAG (the memory gateway), the mover, a control processor and the hub they share, and it owns the one AXI master to DRAM. The host reaches in over the station bus. Two units on the same mesh agree on nothing but the port."
      zoom
      wide
    >
      <BlockDiagram :nodes="shape.nodes" :edges="shape.edges" :groups="shape.groups" />
    </Fig>

    <p class="doc-p">
      Ownership has four categories, not two — and knowing which a part is, is
      the whole skill of building on the framework. Mistake a convention for a
      contract and you waste effort obeying a suggestion; mistake a contract for
      a convention and you produce traffic that routes plausibly and means
      something else.
    </p>
    <SpecTable :cols="kinds.cols" :rows="kinds.rows" />

    <h2 class="doc-h2">How the idea grew</h2>
    <p class="doc-p">
      The framework has levels, and it earned them one at a time — it did not
      start as a framework at all. Each step keeps the one before it and adds
      generality on top. This section is that story, in order, told through what
      is in the tree today.
    </p>
    <div class="grid gap-3 my-5">
      <RouterLink
        v-for="s in stages"
        :key="s.n"
        :to="s.to"
        class="card-hover p-5 no-underline flex gap-4 group"
      >
        <div
          class="shrink-0 w-8 h-8 rounded-full bg-gem/10 text-gem flex items-center justify-center font-semibold kt-text-body"
        >
          {{ s.n }}
        </div>
        <div class="min-w-0">
          <div
            class="kt-text-title font-semibold text-warm-800 dark:text-warm-200 flex items-center gap-1.5"
          >
            {{ s.title }}
            <span
              class="i-carbon-arrow-right text-gem opacity-0 group-hover:opacity-100 transition-opacity"
            />
          </div>
          <p class="kt-text-body text-warm-600 dark:text-warm-400 leading-6 mt-1">
            {{ s.line }}
          </p>
        </div>
      </RouterLink>
    </div>

    <Callout kind="measured" title="The proof is in the tree: examples/saxpy">
      <p>
        Claims about frameworks are cheap, so the platform carries an acceptance
        test — a <b>second, unrelated accelerator built from the framework
        alone.</b> Its whole software half is one instruction, <code>y = a·x +
        y</code> over float32, in about 60 lines of ISA and unit model. Its
        hardware half is one file built from the compute-unit template. A
        three-line token table generates a real mesh — a router, the memory
        agent, the orchestrator, two saxpy units — and the bench drives it the
        way a host drives the card: uploads operands over AXI, dispatches the
        program, reads the results back <b>bit-exact</b>. When it is green, "a
        new accelerator is a new compute unit plus a new ISA" is demonstrated,
        not claimed.
      </p>
    </Callout>

    <Callout kind="rule" title="What the numbers here are, and are not">
      <p>
        This is a work in progress, built more for learning than for production.
        Every resource and frequency figure in these docs is
        <b>out-of-context synthesis</b> on an <code>xcvu13p</code> under Vivado
        2024.2 — no place, no route. <b>No frequency anywhere is a closed-timing
        result</b>, and synthesis slack is optimistic, so read one as an upper
        bound on the logic, not a speed the assembled machine runs at. Where a
        part is built but not finished — the RV64 runtime host is the honest
        example — the page that covers it says so.
      </p>
    </Callout>

    <RouterLink
      to="/idea/beginning"
      class="inline-flex items-center gap-1.5 mt-6 kt-text-body font-medium text-gem no-underline hover:gap-2.5 transition-all"
    >
      Start the story — it began as one accelerator
      <span class="i-carbon-arrow-right" />
    </RouterLink>
  </div>
</template>
