<script setup>
// The port contract: a compute unit wraps noc_cu_base, which handles framing,
// discovery, completion and credits — so the unit conforms by construction.
const port = {
  groups: [{ x: 41, y: -2.5, w: 42, h: 20, label: "YOURS — the framework has no opinion" }],
  nodes: [
    {
      id: "mesh",
      x: 0,
      y: 5,
      w: 12,
      h: 5,
      label: "mesh port",
      sub: "one link",
      accent: true,
    },
    {
      id: "base",
      x: 20,
      y: 1,
      w: 16,
      h: 13,
      label: "noc_cu_base",
      sub: "framing · discovery · completion · credits",
      accent: true,
    },
    { id: "dp", x: 43, y: -1, w: 18, h: 4.5, label: "your datapath" },
    { id: "mem", x: 43, y: 5, w: 18, h: 4.5, label: "your memories" },
    { id: "isa", x: 43, y: 11, w: 18, h: 4.5, label: "your instruction meanings" },
  ],
  edges: [
    { from: "mesh:r", to: "base:l", dir: "h", accent: true, label: "flits" },
    { from: "base:r", to: "dp:l", dir: "h", label: "accept / retire" },
    { from: "base:r", to: "mem:l", dir: "h" },
    { from: "base:r", to: "isa:l", dir: "h" },
  ],
};

// The compiler's three-level IR. The middle level is machine-determined and
// identical for every workload.
const ir = {
  nodes: [
    {
      id: "g",
      x: 0,
      y: 4,
      w: 18,
      h: 5,
      label: "graph",
      sub: "what to compute",
      accent: true,
    },
    {
      id: "s",
      x: 26,
      y: 4,
      w: 20,
      h: 5,
      label: "schedule",
      sub: "placement · packing · coalescing · completion",
      accent: true,
    },
    {
      id: "p",
      x: 54,
      y: 4,
      w: 18,
      h: 5,
      label: "program",
      sub: "machine code",
      accent: true,
    },
  ],
  edges: [
    { from: "g:r", to: "s:l", dir: "h", accent: true },
    { from: "s:r", to: "p:l", dir: "h", accent: true, label: "machine-determined" },
  ],
};

// saxpy: five files become a real mesh with a router, the memory agent, the
// orchestrator and two saxpy units — driven the way a host drives the card.
const saxpy = {
  groups: [{ x: 25, y: -2.5, w: 46, h: 20, label: "a real mesh, generated" }],
  nodes: [
    { id: "cu", x: 0, y: 0, w: 20, h: 3.4, label: "saxpy_cu.v", sub: "from the CU template" },
    { id: "isa", x: 0, y: 5, w: 20, h: 3.4, label: "isa.py + unit.py", sub: "~60 lines" },
    { id: "map", x: 0, y: 10, w: 20, h: 3.4, label: "tokens + .map", sub: "3-line table" },
    { id: "orch", x: 28, y: -1, w: 16, h: 4, label: "orchestrator", accent: true },
    { id: "agent", x: 28, y: 5, w: 16, h: 4, label: "memory agent", accent: true },
    { id: "u0", x: 50, y: 1.5, w: 16, h: 4, label: "saxpy unit", accent: true },
    { id: "u1", x: 50, y: 9, w: 16, h: 4, label: "saxpy unit", accent: true },
  ],
  edges: [
    { from: "cu:r", to: "orch:l", dir: "h", label: "generate" },
    { from: "map:r", to: "agent:l", dir: "h" },
    { from: "orch:r", to: "u0:l", dir: "h", accent: true, label: "dispatch" },
    { from: "agent:r", to: "u1:l", dir: "h" },
  ],
};

const files = {
  cols: [
    { key: "f", label: "For a project NAME, these files are yours" },
    { key: "d", label: "and nothing else is" },
  ],
  rows: [
    { f: "src/examples/NAME/NAME_cu.v", d: "your unit: the datapath, wrapped in noc_cu_base" },
    { f: "…/tokens_NAME.py", d: "a token → instance-text map, for the mesh generator" },
    { f: "…/NAME.map", d: "the mesh picture — a grid of tokens" },
    { f: "driver/examples/NAME/isa.py", d: "how a tensor shape becomes instruction words" },
    { f: "…/unit.py", d: "type registration and the simulation model" },
  ],
};
</script>

<template>
  <div class="container-page pb-24">
    <section class="pt-8 pb-6">
      <p
        class="kt-text-caption uppercase tracking-widest text-warm-400 dark:text-warm-600 font-semibold mb-3"
      >
        The idea · Stage 3 of 4
      </p>
      <h1
        class="text-3xl sm:text-4xl font-semibold tracking-tight text-warm-900 dark:text-warm-100 leading-tight"
      >
        From a platform to a framework.
      </h1>
      <p
        class="kt-text-emphasis text-warm-600 dark:text-warm-400 mt-4 max-w-[72ch] leading-7"
      >
        A platform you rebuild for each project is still a project. A framework is
        a platform with the edges nailed down: a fixed port, shipped parts, and
        one small thing left for you to write. The repository was reorganised to
        make that split real — and enforced.
      </p>
    </section>

    <h2 class="doc-h2">The contract: conform by construction</h2>
    <p class="doc-p">
      Every compute unit wraps one module, <code>noc_cu_base</code>. It handles
      framing, discovery, completion and credits, so a unit is a legal citizen of
      the mesh <b>by construction</b> rather than by careful hand-wiring. Behind
      it, everything is yours — the datapath, the memories, the pipeline depth,
      what the instructions mean — and the framework has no template and no
      opinion. A bench-mounted checker, <code>kh_port_check</code>, makes the
      port conventions executable, so a protocol violation is caught at the port
      instead of six modules downstream.
    </p>
    <Fig
      caption="The port is the entire boundary. noc_cu_base absorbs the framework mechanism; your datapath, memories and instruction semantics sit behind it and are seen by nothing else. Two units on one mesh agree on nothing but this port."
      zoom
      wide
    >
      <BlockDiagram :nodes="port.nodes" :edges="port.edges" :groups="port.groups" />
    </Fig>

    <h2 class="doc-h2">Four kinds of thing, and the split is enforced</h2>
    <p class="doc-p">
      What ships is the spine every accelerator reuses — the station bus, the
      system node, the mesh, the two processing elements — under
      <code>src/kohakuaccel/</code>. What a project adds lives under its own tree.
      This is not a guideline: <b>the framework imports nothing from any
      project, and a test fails the moment it does.</b> A framework module may
      reach a project one only through a named slot, and a dependency checker in
      the standard suite holds the line. That is what keeps the reusable part
      reusable.
    </p>

    <h2 class="doc-h2">The software half shares the split</h2>
    <p class="doc-p">
      The driver owns transports, dispatch, completion and device discovery. The
      compiler ships a <b>three-level IR</b> — graph, schedule, program — and the
      middle level does the placement, packing, coalescing and completion
      accounting. That middle level is <b>machine-determined and identical for
      every workload</b>: you do not schedule the mesh by hand. A declarative ISA
      toolkit turns a field table into an encoder, a decoder, a validator and a
      disassembler, so a new instruction set is a table, not four hand-written
      passes.
    </p>
    <Fig
      caption="The compiler's three levels. Only adjacent levels appear in one piece of code, and the schedule level — the hard part — is the same machinery for every kernel and every project."
      wide
    >
      <BlockDiagram :nodes="ir.nodes" :edges="ir.edges" />
    </Fig>

    <h2 class="doc-h2">The proof: a second accelerator in five files</h2>
    <p class="doc-p">
      Claims about frameworks are cheap, so the tree carries an acceptance test.
      <b>saxpy</b> is a complete second accelerator — <code>y = a·x + y</code>
      over float32 — built from the framework alone. Five files are the whole
      project; everything else is reused unchanged. A three-line token table and
      a map picture generate a real mesh, and its bench drives it the way a host
      drives the card: it uploads operands over AXI, dispatches the program
      through the orchestrator, and reads the results back <b>bit-exact.</b>
    </p>
    <Fig
      caption="saxpy composed: the five project files on the left generate a mesh with a router, the memory agent, the orchestrator and two saxpy units. When the bench is green, 'a new accelerator is a new compute unit plus a new ISA' is demonstrated rather than asserted."
      zoom
      wide
    >
      <BlockDiagram :nodes="saxpy.nodes" :edges="saxpy.edges" :groups="saxpy.groups" />
    </Fig>
    <SpecTable :cols="files.cols" :rows="files.rows" />

    <Callout kind="rule" title="You build on it, not from it">
      <p>
        This is the difference between a codebase you copy and a framework you
        build against. A new accelerator is no longer a new machine — it is a new
        compute unit plus a new ISA, attached to parts that already work, already
        close timing, and already know how to reach memory and the host.
        KohakuTPU became <b>one project on the framework</b>, and it is
        deliberately not the only one in the tree.
      </p>
    </Callout>

    <div class="mt-8 pt-5 border-t border-warm-200 dark:border-warm-700 flex justify-between gap-4">
      <RouterLink
        to="/idea/platform"
        class="inline-flex items-center gap-1.5 kt-text-body text-warm-500 dark:text-warm-400 no-underline hover:text-warm-800 dark:hover:text-warm-200"
      >
        <span class="i-carbon-arrow-left" />
        The platform
      </RouterLink>
      <RouterLink
        to="/idea/soc"
        class="inline-flex items-center gap-1.5 kt-text-body font-medium text-gem no-underline hover:gap-2.5 transition-all text-right"
      >
        Next — a general SoC design framework
        <span class="i-carbon-arrow-right" />
      </RouterLink>
    </div>
  </div>
</template>
