<script setup>
/* ------------------------------------------------------------------ what is on the die */
const die = {
  nodes: [
    { id: "host", x: 10, y: 0, w: 12, h: 3, label: "host", sub: "PCIe" },
    { id: "xdma", x: 3, y: 5, w: 11, h: 3, label: "XDMA", sub: "host DMA" },
    {
      id: "jtag",
      x: 18,
      y: 5,
      w: 11,
      h: 3,
      label: "JTAG-AXI",
      sub: "bring-up",
    },
    {
      id: "axi",
      x: 1,
      y: 10.5,
      w: 29,
      h: 3.6,
      label: "AXI surface  ·  arch/axi",
      sub: "address decode · clock crossing · width conversion · N-to-1",
    },
    {
      id: "ddr",
      x: 1,
      y: 17,
      w: 9,
      h: 3.4,
      label: "DDR4 × N",
      sub: "vendor controller",
    },
    {
      id: "mag",
      x: 13,
      y: 17,
      w: 16,
      h: 3.4,
      label: "system node  ·  arch/sysnode",
      sub: "ONE component — MAG + ctrl PE, neither separable",
      accent: true,
    },
    {
      id: "magmem",
      x: 13,
      y: 20.6,
      w: 7,
      h: 2.8,
      label: "MAG",
      sub: "memory · cross-mesh · staging slot",
    },
    {
      id: "magcpu",
      x: 22,
      y: 20.6,
      w: 7,
      h: 2.8,
      label: "ctrl PE (0,0)",
      sub: "RV32 · mover · transform slot",
      accent: true,
    },
    {
      id: "hub",
      x: 13,
      y: 24,
      w: 16,
      h: 2.4,
      label: "sn_hub",
      sub: "the node's ports — nothing inside owns one",
    },
    {
      id: "mesh",
      x: 1,
      y: 27.5,
      w: 28,
      h: 4,
      label: "mesh  ·  arch/noc",
      sub: "routers + edge ring · one flit per cycle per link · XY",
      accent: true,
    },
    {
      id: "l2",
      x: 1,
      y: 33.5,
      w: 13,
      h: 3.4,
      label: "L2 adapter",
      sub: "addon slot",
    },
    {
      id: "cu",
      x: 16,
      y: 33.5,
      w: 13,
      h: 3.4,
      label: "compute unit",
      sub: "YOURS — inside and out",
      accent: true,
    },
    {
      id: "ilink",
      x: 33,
      y: 27.5,
      w: 10,
      h: 3,
      label: "interlink",
      sub: "other meshes",
    },
  ],
  edges: [
    { from: "host:b", to: "xdma:t", dir: "v" },
    { from: "host:b", to: "jtag:t", dir: "v" },
    { from: "xdma:b", to: "axi:t", dir: "v" },
    { from: "jtag:b", to: "axi:t", dir: "v" },
    { from: "axi:b", to: "ddr:t", dir: "v", label: "memory window" },
    {
      from: "axi:b",
      to: "mag:t",
      dir: "v",
      accent: true,
      label: "control · dispatch",
    },
    { from: "ddr:r", to: "magmem:l", dir: "h", label: "AXI" },
    { from: "mag:b", to: "magmem:t", dir: "v" },
    { from: "mag:b", to: "magcpu:t", dir: "v" },
    { from: "magmem:b", to: "hub:t", dir: "v" },
    { from: "magcpu:b", to: "hub:t", dir: "v" },
    { from: "hub:b", to: "mesh:t", dir: "v", accent: true, label: "flits" },
    { from: "mesh:b", to: "l2:t", dir: "v" },
    { from: "mesh:b", to: "cu:t", dir: "v", accent: true },
    { from: "magmem:r", to: "ilink:l", dir: "h" },
  ],
  groups: [
    { x: 12, y: 16.6, w: 18, h: 10.2, label: "system node — ONE component" },
    { x: 0, y: 26.5, w: 30, h: 11, label: "ship — one mesh per die region" },
  ],
};

/* ------------------------------------------------------------------ four kinds */
const kinds = {
  cols: [
    { key: "kind", label: "" },
    { key: "what", label: "What it is" },
    { key: "change", label: "Can you change it" },
  ],
  rows: [
    {
      kind: "<b>fixed protocol</b>",
      what: "flit format, the compute-unit port handshake, memory request and response encoding, credit and retry, cross-mesh encapsulation",
      change: "<b>No.</b> Change it and you are not on the framework any more.",
    },
    {
      kind: "<b>customizable addon</b>",
      what: "ships working, <i>built</i> to be swapped: the in-MAG transform stage, in-MAG staging, the NoC-endpoint L2 adapter, DRAM-port packing",
      change:
        "<b>Yes</b> — that is what the slot is for. The default is a starting point, not a decision.",
    },
    {
      kind: "<b>convention</b>",
      what: "how to design a thing well, with worked examples: L1 fill and response tagging, unit-to-unit messaging, how to spend your instruction bits",
      change:
        "<b>Your call.</b> Some are forced in practice because MAG hands you data in a shape; the rest are advice. Each one says which.",
    },
    {
      kind: "<b>yours</b>",
      what: "the datapath, the memory structure, what the instructions mean, pipeline depth",
      change: "<b>Entirely.</b>",
    },
  ],
};

/* ------------------------------------------------------------------ which framework */
const compare = {
  cols: [
    { key: "who", label: "" },
    { key: "for", label: "is a framework for" },
    { key: "serves", label: "serves people who" },
  ],
  rows: [
    {
      who: "Vitis HLS",
      for: "turning C into RTL",
      serves: "do <b>not</b> want to write RTL",
    },
    {
      who: "IP catalogues",
      for: "assembling vendor blocks",
      serves: "want someone else's datapath",
    },
    {
      who: "soft-processor overlays",
      for: "running software on fabric",
      serves: "want a CPU on an FPGA",
    },
    {
      who: "<b>KohakuAccel</b>",
      for: "<b>building an accelerator around a datapath you designed</b>",
      serves: "<b>want to write the interesting RTL and nothing else</b>",
    },
  ],
};

/* ------------------------------------------------------------------ five systems */
const systems = {
  cols: [
    { key: "sys", label: "System", mono: true },
    { key: "owns", label: "Owns" },
    { key: "stops", label: "Stops at" },
  ],
  rows: [
    {
      sys: "noc",
      owns: "the flit, the link, the router, the mesh coordinate space, and the port every endpoint attaches through",
      stops: "the meaning of what a flit carries",
    },
    {
      sys: "sysnode",
      owns: "the memory half of the instruction set, the ports that serve it, the transform stage on each memory path, and the multiplexing of every non-compute consumer onto the fabric's edge",
      stops:
        "the DRAM controller, what the transform computes, and what the bytes mean",
    },
    {
      sys: "axi",
      owns: "the boundary to everything that is not the framework: host, DRAM, debug. Arbitration, clock crossing, width conversion, burst legality",
      stops: "anything that speaks flits",
    },
    {
      sys: "ship",
      owns: "assembly: turning a mesh picture into a module, and joining several meshes into one image",
      stops: "placement of what it assembled",
    },
    {
      sys: "physical",
      owns: "die regions, pblocks, clock domains, what may and may not cross a boundary",
      stops: "logic. It constrains; it does not compute",
    },
  ],
};

/* ------------------------------------------------------------------ ownership of a flit's bits */
const owners = {
  nodes: [
    {
      id: "hdr",
      x: 0,
      y: 0,
      w: 11,
      h: 3,
      label: "routing header",
      sub: "arch/noc",
    },
    {
      id: "mem",
      x: 12,
      y: 0,
      w: 14,
      h: 3,
      label: "memory request encoding",
      sub: "arch/sysnode",
    },
    {
      id: "you",
      x: 27,
      y: 0,
      w: 11,
      h: 3,
      label: "your payload",
      sub: "you",
      accent: true,
    },
  ],
  edges: [],
};

/* ------------------------------------------------------------------ how work flows */
const flowNodesBase = [
  {
    id: "mir",
    x: 0,
    y: -6,
    w: 11,
    h: 3.4,
    label: "status mirror",
    sub: "per node · global count",
  },
  { id: "host", x: 0, y: 0, w: 11, h: 3, label: "host", sub: "PCIe / JTAG" },
  {
    id: "win",
    x: 0,
    y: 6,
    w: 11,
    h: 3,
    label: "memory window",
    sub: "AXI slave + master",
  },
  { id: "dram", x: 0, y: 12, w: 11, h: 3, label: "DRAM" },
  {
    id: "ctrl",
    x: 15,
    y: 0,
    w: 13,
    h: 3.4,
    label: "control agent",
    sub: "staging RAM · PROG_KICK",
  },
  {
    id: "mesh",
    x: 15,
    y: 6,
    w: 13,
    h: 3.4,
    label: "mesh",
    sub: "XY · one flit/cycle/link",
  },
  {
    id: "port",
    x: 15,
    y: 12,
    w: 13,
    h: 3.4,
    label: "memory port",
    sub: "AXI reads · transform",
  },
  { id: "cu", x: 32, y: 6, w: 13, h: 3.4, label: "compute unit", sub: "yours" },
];

const flowEdgesBase = [
  { id: "e1", from: "host:b", to: "win:t", dir: "v", label: "operands" },
  { id: "e2", from: "win:b", to: "dram:t", dir: "v" },
  {
    id: "e3",
    from: "host:r",
    to: "ctrl:l",
    dir: "h",
    label: "program · PROG_KICK",
  },
  {
    id: "e4",
    from: "ctrl:b",
    to: "mesh:t",
    dir: "v",
    label: "CU_INST ↓   CU_SIGNAL ↑",
  },
  { id: "e5", from: "mesh:r", to: "cu:l", dir: "h" },
  {
    id: "e6",
    from: "cu:b",
    to: "port:r",
    dir: "h",
    label: "MEM_RD_REQ / MEM_WR_REQ",
  },
  { id: "e7", from: "port:l", to: "dram:r", dir: "h", label: "AXI" },
  { id: "e8", from: "port:t", to: "mesh:b", dir: "v", label: "MEM_RD_RESP" },
  { id: "e9", from: "ctrl:t", to: "mir:r", dir: "h", label: "summarise" },
  { id: "e10", from: "mir:b", to: "host:t", dir: "v" },
];

const flowSteps = [
  {
    title: "1 · The host places operands in DRAM",
    hot: ["host", "win", "dram"],
    lit: ["e1", "e2"],
    note: "Ordinary AXI writes into the edge complex's memory window. The window is an AXI slave with its own master behind it, so a long upload is one burst on the host side and whatever the memory wants on the other. A declared inbound transform runs here, once per byte written, rather than once per read.",
  },
  {
    title: "2 · The host stages a program and kicks it",
    hot: ["host", "ctrl"],
    lit: ["e3"],
    note: "Instructions are written into a staging RAM inside the control agent, again as ordinary AXI writes. The host names a destination coordinate and writes PROG_KICK. The agent reads the staging RAM, stamps each instruction's routing header — destination from PROG_DST, source with its own coordinates — and pushes the flit into the fabric.",
  },
  {
    title: "3 · The instruction arrives at a compute unit",
    hot: ["ctrl", "mesh", "cu"],
    lit: ["e4", "e5"],
    note: "It lands in that unit's instruction FIFO inside noc_cu_base, which hands the datapath one instruction at a time on an inst_flit / inst_valid / inst_ready handshake. The framework remembers who sent it, so the unit never needs to be told where its controller is.",
  },
  {
    title: "4 · The compute unit asks for operands",
    hot: ["cu", "mesh", "port"],
    lit: ["e6"],
    note: "It emits a MEM_RD_REQ naming a byte address, a length, and — if it wants a run of consecutive entries — a count. The flit is routed to whichever memory port serves its row.",
  },
  {
    title: "5 · The memory port fetches and streams back",
    hot: ["port", "dram", "mesh", "cu"],
    lit: ["e7", "e8", "e5"],
    note: "It issues AXI reads, runs the fetch-path transform if the request asked for one, and emits response flits each of which says where it belongs: the requester's own transaction tag plus this entry's position in the run, plus the word index within the entry. The receiver needs no cursor, and arrival order stops being load-bearing. A request may name extra destinations, so one fetch and one transform can serve several consumers.",
  },
  {
    title: "6 · The unit computes, then writes results",
    hot: ["cu", "port"],
    lit: ["e6"],
    note: "A write is a descriptor flit followed by data flits. The memory port matches data to descriptor by source coordinate rather than by arrival order, because the mesh may put another node's flit between them. The write ack is fire-and-forget: the unit does not wait for it.",
  },
  {
    title: "7 · The unit retires the instruction",
    hot: ["cu", "mesh", "ctrl"],
    lit: ["e4"],
    note: "It raises exec_done, and noc_cu_base queues a CU_SIGNAL back to whoever sent the instruction. Completions are queued rather than held in a register, because a datapath can retire faster than a congested link drains and one register would let each completion overwrite the last — silently losing the credits they carry.",
  },
  {
    title: "8 · The host sees it",
    hot: ["ctrl", "mir", "host"],
    lit: ["e9", "e10"],
    note: "The agent does not queue completion signals for the host to read; it summarises them into a per-node status mirror and a global count. A host that never reads a mailbox therefore cannot wedge the control plane, which is exactly what a queued-and-undrained mailbox would do.",
  },
];

const flowNodes = (s) =>
  flowNodesBase.map((n) => ({
    ...n,
    accent: s ? s.hot.includes(n.id) : false,
  }));
const flowEdges = (s) =>
  flowEdgesBase.map((e) => ({
    ...e,
    accent: s ? s.lit.includes(e.id) : false,
  }));

/* Two conforming units, nothing shared. Deliberately labelled by SHAPE, not by
 * name: the point is that two units on one port need not agree about anything
 * below it, and naming a project's modules here would suggest the framework
 * knows about them. */
const units = {
  cols: [
    { key: "k", label: "" },
    { key: "mat", label: "a wide-operand matrix unit" },
    { key: "vec", label: "a vector core" },
  ],
  rows: [
    {
      k: "operand memory",
      mat: "two RAMs, <b>928 bits</b> wide — one per operand",
      vec: "one RAM, <b>256 bits</b> wide",
    },
    {
      k: "L1 count",
      mat: "4 core + 1 accumulator tile",
      vec: "1, plus a separate instruction memory",
    },
    {
      k: "read latency",
      mat: "1 for L1, 2 for the accumulator",
      vec: "1, or 2 on the deeper primitive",
    },
    {
      k: "register file",
      mat: "none",
      vec: "three mirrored RAMs, to synthesise three read ports",
    },
    {
      k: "other memories",
      mat: "a separate accumulator tile RAM at read latency 2",
      vec: "a 32-bit instruction memory in distributed LUTRAM",
    },
  ],
};

/* ------------------------------------------------------------------ ship */
const ship = {
  nodes: [
    {
      id: "hostx",
      x: 5,
      y: 0,
      w: 12,
      h: 3,
      label: "host · XDMA · AXI",
      sub: "one die pays for it",
    },
    { id: "ddr0", x: 0, y: 6, w: 9, h: 2.8, label: "DDR4 ch 0" },
    {
      id: "mag0",
      x: 10,
      y: 6,
      w: 10,
      h: 2.8,
      label: "system node 0",
      sub: "MAG + ctrl PE",
      accent: true,
    },
    {
      id: "mag1",
      x: 24,
      y: 6,
      w: 10,
      h: 2.8,
      label: "system node 1",
      sub: "MAG + ctrl PE",
      accent: true,
    },
    { id: "ddr1", x: 35, y: 6, w: 9, h: 2.8, label: "DDR4 ch 1" },
    {
      id: "mesh0",
      x: 0,
      y: 10.5,
      w: 20,
      h: 3.2,
      label: "mesh 0",
      sub: "units + memory ports",
    },
    {
      id: "mesh1",
      x: 24,
      y: 10.5,
      w: 20,
      h: 3.2,
      label: "mesh 1",
      sub: "units + memory ports",
    },
  ],
  edges: [
    { from: "hostx:b", to: "mag0:t", dir: "v", label: "AXI" },
    { from: "ddr0:r", to: "mag0:l", dir: "h" },
    { from: "ddr1:l", to: "mag1:r", dir: "h" },
    { from: "mag0:b", to: "mesh0:t", dir: "v" },
    { from: "mag1:b", to: "mesh1:t", dir: "v" },
    {
      from: "mag0:r",
      to: "mag1:l",
      dir: "h",
      accent: true,
      dash: true,
      label: "interlink",
    },
  ],
  groups: [
    { x: -1, y: 5, w: 22, h: 9.5, label: "SLR 0" },
    { x: 23, y: 5, w: 22, h: 9.5, label: "SLR 1" },
  ],
};

/* ------------------------------------------------------------------ fit */
const fit = {
  cols: [
    { key: "v", label: "" },
    { key: "t", label: "The framework assumes a shape" },
  ],
  rows: [
    {
      v: "fits",
      t: "Work decomposes into units that stream operands in, compute, and stream results out.",
      _tone: "good",
    },
    {
      v: "fits",
      t: "A unit's working set fits in on-chip memory for the duration of a step.",
      _tone: "good",
    },
    {
      v: "fits",
      t: "Addresses are known ahead of time — expressible as descriptors, not discovered by following pointers.",
      _tone: "good",
    },
    {
      v: "fits",
      t: "Units are independent within a step; they synchronise between steps, not inside one.",
      _tone: "good",
    },
    {
      v: "does not",
      t: "Pointer chasing or data-dependent addressing.",
      _tone: "bad",
    },
    {
      v: "does not",
      t: "Tight low-latency coupling <i>between</i> units — write one larger unit instead.",
      _tone: "bad",
    },
    { v: "does not", t: "Cache coherence between units.", _tone: "bad" },
    {
      v: "does not",
      t: "Kernels small enough that dispatch dominates the work.",
      _tone: "bad",
    },
  ],
};

/* ------------------------------------------------------------------ three protocols */
const protocols = {
  cols: [
    { key: "p", label: "Protocol" },
    { key: "between", label: "Between" },
    { key: "unit", label: "Unit" },
    { key: "fc", label: "Flow control", mono: true },
  ],
  rows: [
    {
      p: "<b>AXI4</b>",
      between: "outside world and the framework",
      unit: "burst",
      fc: "VALID/READY, per channel",
    },
    {
      p: "<b>flit</b>",
      between: "endpoints on one fabric",
      unit: "288-bit flit",
      fc: "busy/valid <b>with retry</b>, plus end-to-end credits",
    },
    {
      p: "<b>descriptor</b>",
      between: "a compute unit and memory",
      unit: "one entry, or a run of them",
      fc: "credits held by the requester",
    },
  ],
};
</script>

<template>
  <DocPage
    title="What is on the die"
    summary="A framework for building FPGA accelerators around a compute unit you design. This page says what exists, who owns each piece, and what a unit of work does from the moment a host asks for it to the moment the answer is back in DRAM."
    domain="framework"
    status="shipped"
    source="docs/README.md · docs/arch/README.md"
  >
    <h2 class="doc-h2">Which framework this is</h2>
    <p class="doc-p">
      The word is load-bearing, so be precise about which one. If you want to
      avoid writing RTL, use HLS. KohakuAccel assumes the datapath is the part
      you care about, and that writing a DDR controller, a DMA engine, an
      on-chip network, a dispatch mechanism and a driver for the fifth time is
      not.
    </p>
    <SpecTable :cols="compare.cols" :rows="compare.rows" />

    <h2 class="doc-h2">What is on the die</h2>
    <Fig
      caption="One ship. A device image may hold several, one per die region, joined by the interlink. The system node is ONE component: MAG and the control processor are drawn separately because they are separate concerns, but neither is a module you can instantiate alone and neither owns a fabric port — sn_hub does, and everything inside is its client. The compute unit is the only block you have to write."
      zoom
    >
      <BlockDiagram
        :nodes="die.nodes"
        :edges="die.edges"
        :groups="die.groups"
      />
    </Fig>

    <h2 class="doc-h2">Four kinds of thing</h2>
    <p class="doc-p">
      "You supply this, we supply that" is too coarse to build against.
      Everything in KohakuAccel is one of four kinds, and every page says which.
    </p>
    <SpecTable :cols="kinds.cols" :rows="kinds.rows" />

    <Callout
      kind="trap"
      title="A convention is not a specification and not a default implementation"
    >
      <p>
        It is "here is how we did it, here is why, here is what breaks if you
        deviate." Mistaking a convention for a contract wastes effort obeying a
        suggestion; mistaking a contract for a convention produces traffic that
        <b>routes plausibly and means something else</b>. The normative form of
        row one is <span class="chip">docs/spec/</span>; row three lives in
        <span class="chip">docs/integrate/</span> and never in the spec tree.
      </p>
    </Callout>

    <h2 class="doc-h2">The five systems</h2>
    <p class="doc-p">
      Each system is defined by what it owns, not by which directory it
      currently lives in. The framework is legible exactly to the extent that
      these boundaries hold — which is why every page states what its system
      does <i>not</i> own and which neighbour takes over.
    </p>
    <SpecTable
      :cols="systems.cols"
      :rows="systems.rows"
      caption='noc is a historical name; read it as the fabric if that helps. The system node is MAG plus the mover, the control processor and the interlink — never a plain "node", which is what a fabric endpoint is.'
    />

    <Callout
      kind="rule"
      title="When these pages name a project, it is for one of two reasons"
    >
      <p>
        A framework page describes a mechanism and nothing about any accelerator
        built on it, so a project's module name appearing here is deliberate and
        means one of exactly two things. Either it is <b>provenance</b> — a
        measured figure has to say which design was measured, and every one of
        them here is the reference instance on one part. Or it is
        <b>evidence for a defect of this framework</b>: a namespace the
        framework owns but only declares inside a project, a reusable
        composition parked in a project's directory. Those read as accusations,
        not as documentation, and each is on its page's warts list.
      </p>
      <p>
        <b>The largest one is closed.</b> The SIMD unit instantiated arithmetic
        that exists only under <span class="chip">src/kohakutpu/</span>, so the
        framework did not build without that project. The whole unit is
        <span class="chip">src/kohakumpe/simd/</span> now — project to project,
        the allowed direction — reached through the
        <span class="chip">SIMD_EN</span> slot.
        <span class="chip">scripts/py/deps.py</span> reads every instantiation
        under <span class="chip">src/kohakuaccel/</span> and fails the run on a
        new edge; it reports 0 project dependencies over 252 framework files.
      </p>
    </Callout>

    <h2 class="doc-h2">What the framework actually removes</h2>
    <p class="doc-p">
      Not the design work. The <b>connection</b> problem. Writing a compute unit
      has always come with a second, larger job attached: work out how this
      thing reaches memory, how instructions get to it, how results get back,
      how it does not deadlock the machine, and how a host ever sees any of it.
      That job is the same every time, it is where the subtle failures live, and
      it has nothing to do with what you were trying to build.
    </p>
    <p class="doc-p">
      <b>You still write a whole compute unit</b> — the datapath, its memory
      system, its pipeline, its instruction semantics. What you never have to
      work out is how it connects.
    </p>

    <h3 class="doc-h3">You inherit a way of asking, not a memory system</h3>
    <Fig
      caption="An instruction flit's bits have three owners, and only the last is yours. The machine already knows how to say fetch this region, in these entries, transformed this way, delivered to these nodes; how to say write this back; and how to say rearrange one region into another."
    >
      <BlockDiagram :nodes="owners.nodes" :edges="owners.edges" />
    </Fig>
    <p class="doc-p">
      This is about <b>encoding and transport only</b>. It says nothing about
      what your unit does with the data once it arrives — how many memories it
      has, how wide they are, what their read latency is, how they are banked.
      That is your design, and the framework has no opinion on it.
    </p>

    <h2 class="doc-h2">How work flows</h2>
    <p class="doc-p">
      One step of work, end to end. Nothing here is specific to what the compute
      unit computes.
    </p>
    <StepPlayer :steps="flowSteps" label="Host to unit and back">
      <template #default="{ state }">
        <BlockDiagram :nodes="flowNodes(state)" :edges="flowEdges(state)" />
      </template>
    </StepPlayer>

    <Callout
      kind="rule"
      title="The dispatcher stalls on credit, never on the network"
    >
      <p>
        A credit is one instruction the target's queue can still hold. This is
        not throughput smoothing: backpressuring an instruction into the mesh
        blocks whatever is behind it, which may be the completion that would
        have freed the resource the instruction is waiting for.
        <b>Stalling locally is safe; stalling the network is not.</b>
      </p>
    </Callout>

    <h2 class="doc-h2">The three protocols, and where each stops</h2>
    <p class="doc-p">
      The framework is three protocols with hard edges between them. Most
      confusion about "where does this belong" resolves by asking which protocol
      the thing speaks.
    </p>
    <SpecTable :cols="protocols.cols" :rows="protocols.rows" />

    <Callout
      kind="trap"
      title="The flit link handshake is not AXI's, and the difference is not cosmetic"
    >
      <p>
        A sender asserts <span class="chip">valid</span> and holds both
        <span class="chip">valid</span> and <span class="chip">data</span> until
        a cycle in which <span class="chip">busy</span> is low; a receiver
        accepts if and only if <span class="chip">valid &amp;&amp; !busy</span>.
        Both halves are required. A sender that gives up loses a flit; a
        receiver that accepts unconditionally duplicates one. Either failure is
        silent and lands several modules away from its cause — the traces are on
        <RouterLink to="/framework/noc" class="doc-link"
          >Mesh and routers</RouterLink
        >.
      </p>
    </Callout>

    <h2 class="doc-h2">What a compute unit is</h2>
    <p class="doc-p">
      The framework's whole shape follows from what it assumes a compute unit to
      be, and the assumption is deliberately thin. A compute unit is anything
      that attaches to one fabric port and accepts instructions one at a time;
      names the memory it wants ahead of time, as an address and a length; and
      signals retirement, and can be asked its capabilities and its counters.
    </p>
    <p class="doc-p">
      It is not assumed to be arithmetic. Nothing in
      <span class="chip">noc_cu_base</span>,
      <span class="chip">mag_mem_port</span> or the router knows whether the
      datapath multiplies, sorts, or hashes.
    </p>
    <SpecTable
      :cols="units.cols"
      :rows="units.rows"
      caption="Two compute units on one mesh, sharing the port and nothing else. Every figure is read out of a real project's RTL and none of it is a framework fixture — the framework does not know either column exists."
    />

    <Callout
      kind="trap"
      title="There is no framework-mandated L1, because there could not be one"
    >
      <p>
        928 bits against 256; five memories against two; different read
        latencies and different storage primitives — in one project, on one
        mesh, through one port. Both are ordinary conforming nodes.
        <b
          >If a page anywhere in this tree reads as though the framework
          supplies your L1, it is wrong.</b
        >
      </p>
    </Callout>

    <h2 class="doc-h2">What a ship is</h2>
    <p class="doc-p">
      A ship is one complete assembly of the picture above, elaborated for a
      specific mesh shape and device region. On a multi-die FPGA the topology
      question is not "what shape mesh" — it is "what fits on one die".
    </p>
    <Fig
      caption="One mesh per die, each with its own DRAM channel, joined memory-agent to memory-agent by an explicit registered link. Flow control across that boundary is credit-based, with no ready signal travelling back — a backwards-travelling ready is exactly the combinational crossing the registered link exists to avoid. The mesh does not learn that other meshes exist: a remote transfer is addressed to the local memory agent's port and the real destination rides in header fields the message class does not otherwise use, on every flit of a burst rather than just the first, because the encapsulator at the far end is stateless."
      zoom
      wide
    >
      <BlockDiagram
        :nodes="ship.nodes"
        :edges="ship.edges"
        :groups="ship.groups"
      />
    </Fig>

    <Callout
      kind="trap"
      title="A single mesh spanning dies was built, measured, and rejected"
    >
      <p>
        Its worst path was almost entirely routing delay with no logic levels in
        it. The wire budget for a crossing was never the binding constraint;
        <b>latency and routing were</b>. Two more constraints are hardware rules
        rather than preferences: a compute unit cannot span an SLR boundary,
        because carry chains and DSP/BRAM/URAM cascades do not propagate across
        one; and a DRAM channel cannot either, because all the I/O banks an
        interface uses, and its clocking, must be on one die.
      </p>
      <p>
        So: size a mesh to one die, and reach for more dies by adding meshes,
        not by growing one.
      </p>
    </Callout>

    <h2 class="doc-h2">Coordinates</h2>
    <p class="doc-p">
      One numbering runs through every system. A fabric position is
      <span class="chip">(x, y)</span> in a
      <span class="chip">2^POS_WIDTH</span> square. Routers occupy an inner
      rectangle bounded by <span class="chip">GRID_LO</span> on both axes and
      <span class="chip">GRID_X_HI</span> /
      <span class="chip">GRID_Y_HI</span> per axis. Endpoints sit either on a
      router's local port or just outside the router rectangle, on the edge
      ring.
    </p>

    <Callout
      kind="trap"
      title="Packed coordinates are {y, x}, with y in the high half"
    >
      <p>
        <span class="chip">PROG_DST</span>, the status mirror index, a request's
        extra-destination list — the packing is the same everywhere, and
        mismatching it is the kind of error that presents as
        <b>traffic arriving at a plausible wrong node</b>.
      </p>
    </Callout>

    <h2 class="doc-h2">Does your workload fit</h2>
    <SpecTable
      :cols="fit.cols"
      :rows="fit.rows"
      caption="Saying no here is cheaper than finding out after floorplanning."
    />

    <h2 class="doc-h2">Numbers</h2>
    <Callout
      kind="rule"
      title="Measurements live with the project that produced them"
    >
      <p>
        Framework pages carry no Fmax, LUT, FF, BRAM or utilisation figures. Any
        such figure describes <b>one accelerator on one part</b> — for the
        reference instance, <span class="chip">xcvu13p-fhgb2104-2L-e</span>.
        Those numbers are evidence the framework closes on real silicon. They
        are not specifications of it, and a framework doc that quotes them as if
        they were is wrong.
      </p>
      <p>
        Device facts — how many die regions a part has, how many hard memory
        controllers, what a cascade may not cross — are not measurements and do
        appear.
      </p>
    </Callout>

    <h2 class="doc-h2">House rule</h2>
    <Callout kind="note">
      <p>
        If a page says "comprehensive", "powerful", or "seamless", it is out of
        date. Say what it does, what it costs, and where it stops.
      </p>
    </Callout>
  </DocPage>
</template>
