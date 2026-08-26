<script setup>
// ===========================================================================
// The system node, as ONE component.
//
// /framework/sysnode is the contract a compute unit builds against;
// /component/sysnode/microarchitecture is the RTL. This page answers "what is
// this block, how is it built, what does it cost, and how do I size one".
//
// EVERY FIGURE names four axes: measurement context, synthesis flow, timing
// request, and which report. Whole-node runs are `sysnode` AS TOP, out of
// context, -directive default (so hierarchical rows are a REBUILT netlist),
// one clock at 3.333 ns from scripts/xdc/ooc_sysnode.xdc, and the numbers come
// from `report_utilization` — CLB LUT *sites*, not raw LUT primitives.
// ===========================================================================

// ------------------------------------------------------ the node, whole
const whole = {
  groups: [{ x: -3, y: -3.5, w: 92, h: 31, label: "the system node — one per mesh" }],
  nodes: [
    {
      id: "mesh",
      x: 0,
      y: 6,
      w: 9,
      h: 12,
      label: "the mesh",
      sub: "routers · compute units",
      accent: true,
    },
    {
      id: "hub",
      x: 18,
      y: 0,
      w: 9,
      h: 24,
      label: "sn_hub",
      sub: "the only thing that owns an attachment",
      accent: true,
    },
    {
      id: "cpx",
      x: 36,
      y: 0,
      w: 13,
      h: 7,
      label: "control complex",
      sub: "processor · mover · slot",
      accent: true,
    },
    {
      id: "eng",
      x: 36,
      y: 9,
      w: 13,
      h: 6,
      label: "memory engines",
      sub: "one per attachment",
    },
    {
      id: "edge",
      x: 36,
      y: 17,
      w: 13,
      h: 7,
      label: "agent · interlink",
      sub: "host reach · cross-mesh",
    },
    {
      id: "conv",
      x: 58,
      y: 5,
      w: 11,
      h: 14,
      label: "converged path",
      sub: "one internal protocol for every requester",
      accent: true,
    },
    {
      id: "dram",
      x: 78,
      y: 5,
      w: 10,
      h: 14,
      label: "one AXI master",
      sub: "DRAM, and staging in front of it",
    },
  ],
  edges: [
    { from: "mesh:r", to: "hub:l", accent: true },
    { from: "hub:r", to: "cpx:l", label: "(0,0)" },
    { from: "hub:r", to: "eng:l", label: "memory", accent: true },
    { from: "hub:r", to: "edge:l", label: "remote" },
    { from: "cpx:r", to: "conv:l", label: "cp · MV" },
    { from: "eng:r", to: "conv:l", accent: true },
    { from: "edge:r", to: "conv:l", label: "inbound", dash: true },
    { from: "conv:r", to: "dram:l", accent: true },
  ],
};

// ------------------------------------------ the converged path, mechanism
// The structure that sets the cost: the per-requester latch, the single
// claimed burst in the staging port, and the DRAM port's arbiters and CDC
// FIFOs. Boxes carry the registers and queues, not just names.
const conv = {
  nodes: [
    {
      id: "reqs",
      x: 0,
      y: 0,
      w: 11,
      h: 16,
      label: "MP1 requesters",
      sub: "PORTS engines · host upload · CP · MV · +1 with the interlink",
      accent: true,
    },
    {
      id: "latch",
      x: 19,
      y: 0,
      w: 10,
      h: 16,
      label: "channel latch",
      sub: "the channel offered on the first ungranted cycle, held until q_ready",
      accent: true,
    },
    {
      id: "stgp",
      x: 37,
      y: 0,
      w: 11,
      h: 7,
      label: "mag_stage_port",
      sub: "aperture claim · ONE burst at a time · dwr/drd per requester",
    },
    {
      id: "stg",
      x: 37,
      y: 9,
      w: 11,
      h: 7,
      label: "mag_stage",
      sub: "BANKS × ENTRIES URAM · per-bank dispatch register",
    },
    {
      id: "dramp",
      x: 56,
      y: 0,
      w: 11,
      h: 16,
      label: "mag_dram_port",
      sub: "two round-robin arbiters · DATA_W→MW pack · five async FIFOs",
      accent: true,
    },
    {
      id: "axi",
      x: 75,
      y: 4,
      w: 10,
      h: 8,
      label: "M_AXI_DRAM",
      sub: "the node's one AXI master, in the DRAM clock domain",
    },
  ],
  edges: [
    { from: "reqs:r", to: "latch:l", label: "q_* w_* r_*", accent: true },
    { from: "latch:r", to: "stgp:l", accent: true },
    { from: "stgp:b", to: "stg:t", label: "port B", dir: "v" },
    { from: "stgp:r", to: "dramp:l", label: "not staged", accent: true },
    { from: "dramp:r", to: "axi:l", accent: true },
  ],
};

// ------------------------------------------------- the two configurations
const cfgCols = [
  { key: "w", label: "" },
  { key: "a", label: "CPU_RV64 = 0 — the default" },
  { key: "b", label: "CPU_RV64 = 1" },
];
const cfgRows = [
  { w: "<b>module</b>", a: "<code>rv_mag_pe</code>", b: "<code>rv64_mag_pe</code>" },
  {
    w: "<b>the processor</b>",
    a: "RV32, no control registers, a blocking L1, faults reported as halts",
    b: "RV64IMA with <b>M, S and U privilege</b>, Sv39 translation shared by fetch and data, and a write-back L1",
  },
  {
    w: "<b>how the host loads it</b>",
    a: "<code>CU_DATA</code> flits through the mesh, kicked with a <code>CU_INST</code>",
    b: "an AXI-side register window and a boot doorbell",
  },
  {
    w: "<b>on the fabric</b>",
    a: "a compute unit at <code>(0,0)</code> — enumerated, kicked, reports a completion, and <b>dispatches</b> to other units",
    b: "<b>a dispatcher at <code>(0,0)</code>, and not a compute unit.</b> A mailbox in the control region builds <code>CU_INST</code> flits and queues the <code>CU_SIGNAL</code>s that come back; nothing kicks it and it reports no completion",
    _tone: "good",
  },
  {
    w: "<b>cross-mesh doorbell</b>",
    a: "rung from the node",
    b: "<b>rung from the processor</b>, through the interlink's config window. The host wins a same-cycle collision and the processor retries; the four inbound counts read back in one word",
    _tone: "good",
  },
  {
    w: "<b>status and interrupts</b>",
    a: "mirrored into the node's one status register",
    b: "in its own host window, plus <code>pe_status</code>; <code>irq_summary</code> carries a mover fault and the host's stop request, and a queued completion raises the same line",
    _tone: "good",
  },
  {
    w: "<b>transform-slot registers</b>",
    a: "reachable through the processor's node range",
    b: "<b>not reachable.</b> The bank's <code>cfg_en</code> is tied to zero and its <code>cfg_rdata</code> and fault output are unread",
    _tone: "warn",
  },
  {
    w: "<b>the mover and the slot</b>",
    a: "instantiated, unchanged",
    b: "instantiated, unchanged",
    _tone: "good",
  },
];

// ---------------------------------------------------------------- knobs
const knobCols = [
  { key: "k", label: "Knob", mono: true },
  { key: "w", label: "What it moves" },
];
const knobRows = [
  {
    k: "PORTS",
    w: "<b>The one shape knob.</b> Each attachment adds a whole memory server behind it — two flit-wide intake FIFOs, a read engine with its emit buffer, a <code>WR_SLOTS × WBURST</code> write-slot array, and a requester on the converged path. <b>2,063 and 2,031 LUT plus 4 RAMB36 each</b> for the two instances of a two-port node, since the slot array moved into block RAM.",
  },
  {
    k: "STAGE_AT_PORT",
    w: "<b>Not a tuning knob.</b> It decides <i>who can reach the store</i> and <i>how wide one access is</i> — see the trap below. Not a size, not a speed.",
    _tone: "warn",
  },
  {
    k: "CPU_RV64",
    w: "Which control complex. <code>rv64_mag_pe</code> measures <b>16,010 LUT</b> in the run below, of which the processor is 7,244 and the mover and the transform slot — identical in both complexes — are the other 8,766. <b>The RV32 complex has not been re-measured since</b>, so no difference between the two is quoted here.",
  },
  {
    k: "ILINK",
    w: "Cross-mesh. At <b>0</b> every one of its nets ties to a constant, every use folds, and the generated top does not expose the ports — so a build without it is identical to one made before it existed. Enabled it is <b>3,729 LUT</b> in the run below: the switch at 2,435 and the encapsulator with its doorbell registers at 1,294.",
  },
  {
    k: "STAGE_BANKS × STAGE_ENTRIES",
    w: "The store's size, and it is <b>URAM, not LUT</b>. 4 × 16,384 × 1,024 bits is 2 MB and 64 URAM.",
  },
  {
    k: "WR_SLOTS",
    w: "<b>A correctness parameter, not a performance one.</b> Under-sizing does not corrupt anything — it deadlocks. Two per node that can have a write in flight, not one.",
    _tone: "warn",
  },
];

// ---------------------------------------------- the converged-path protocol
const qBroken = {
  rows: [
    { name: "q_valid", kind: "bit", values: [1, 1, 1, 1] },
    { name: "q_write", kind: "bit", values: [1, 1, 0, 0], mark: [2] },
    { name: "q_addr", kind: "bus", values: ["W", "W", "R", "R"], mark: [2] },
    {
      name: "arbiter's snapshot",
      kind: "bus",
      values: ["—", "write", "write", "write"],
    },
    { name: "captured addr", kind: "bus", values: [null, null, null, "R"], mark: [3] },
    { name: "q_ready", kind: "bit", values: [0, 0, 0, 1] },
    {
      name: "",
      kind: "text",
      values: ["offered as a write", "", "switched to a read", "the WRONG channel pops"],
    },
  ],
  notes: [
    {
      cycle: 3,
      text: "Both arbiters decide on a REGISTERED request vector and sample the bus LIVE, and the grant is a single wire. A presentation that switches mid-wait gets the other transaction's address captured, and the grant then pops whichever channel the requester is offering now.",
      tone: "bad",
    },
    {
      text: "Nothing on the AXI side is malformed. The burst is well formed — it is simply someone else's. Measured symptom: a cache line written back to one address while the fill that displaced it returned a different one.",
      tone: "bad",
    },
  ],
};

const qFixed = {
  rows: [
    { name: "q_valid", kind: "bit", values: [1, 1, 1, 1] },
    { name: "sel_h (holding)", kind: "bit", values: [0, 1, 1, 1], mark: [1] },
    { name: "q_write", kind: "bit", values: [1, 1, 1, 1] },
    { name: "q_addr", kind: "bus", values: ["W", "W", "W", "W"] },
    { name: "q_ready", kind: "bit", values: [0, 0, 0, 1] },
    {
      name: "",
      kind: "text",
      values: ["write wins at FIRST offer", "choice held", "held", "granted — as offered"],
    },
  ],
  notes: [
    {
      text: "The adapter latches which channel it offered on the first cycle the request was not granted, and holds it until q_ready. Write wins when both are offered, but only at the first offer.",
      tone: "good",
    },
    {
      text: "The matching guard is a simulation assertion in mag_dram_port: a requester that changes {write, addr, len} while waiting is reported BY NAME, with the old and new values, rather than silently crossing two transactions.",
      tone: "good",
    },
  ],
};

// -------------------------------------------------------- staging, both ways
const placeCols = [
  { key: "w", label: "", mono: false },
  { key: "a", label: "STAGE_AT_PORT = 0" },
  { key: "b", label: "STAGE_AT_PORT = 1 — what ships" },
];
const placeRows = [
  {
    w: "<b>where the store sits</b>",
    a: "one inside <b>every</b> memory port, upstream of where the requesters meet",
    b: "one on the <b>converged</b> path, in front of the DRAM port",
  },
  {
    w: "<b>who can reach it</b>",
    a: "that port's flit traffic only — <b>the mover and the interlink never can</b>",
    b: "every requester: the engines, the host upload, the processor, the mover, inbound remote writes",
    _tone: "good",
  },
  {
    w: "<b>URAM</b>",
    a: "<code>PORTS</code> × 64. At two ports, <b>4 MB spent to obtain 2 MB of reachable store</b>; at four, 256 URAM",
    b: "<b>64</b>, once",
    _tone: "good",
  },
  {
    w: "<b>which store port it uses</b>",
    a: "<b>port A</b> — <code>WORDS = 4</code>, so a read moves a whole <b>1,024-bit entry</b> per access",
    b: "<b>port B only.</b> <code>mag_stage_port</code> ties <code>a_req</code>, <code>a_we</code>, <code>a_addr</code> and <code>a_wdata</code> to zero and leaves every A output unconnected",
    _tone: "warn",
  },
  {
    w: "<b>access width and depth</b>",
    a: "one entry per access, entry-granular fill",
    b: "<b>one 256-bit word per access, one read outstanding.</b> The port holds a single returned word, so a second request would drop the first",
    _tone: "warn",
  },
];

// ----------------------------------------------------------------- cost
const nodeCost = {
  cols: [
    { key: "w", label: "The whole node" },
    { key: "lut", label: "LUT", mono: true, align: "right" },
    { key: "ff", label: "FF", mono: true, align: "right" },
    { key: "bram", label: "BRAM", mono: true, align: "right" },
    { key: "uram", label: "URAM", mono: true, align: "right" },
    { key: "dsp", label: "DSP", mono: true, align: "right" },
    { key: "wns", label: "WNS", mono: true, align: "right" },
  ],
  rows: [
    {
      w: "<b>RV32 complex — the default</b>, staging inside each port. <b>Not re-run since</b>",
      lut: "31,220",
      ff: "52,481",
      bram: "41.5",
      uram: "128",
      dsp: "39",
      wns: "+0.096",
      _tone: "warn",
    },
    {
      w: "<b>RV64 complex</b>, staging on the converged path — the run of 2026-08-26",
      lut: "<b>32,859</b>",
      ff: "46,436",
      bram: "57.5",
      uram: "65",
      dsp: "<b>47</b>",
      wns: "<b>+0.039</b>",
      _tone: "good",
    },
  ],
};

const bought = {
  cols: [
    { key: "c", label: "The change, in the order it was applied" },
    { key: "l", label: "LUT", mono: true, align: "right" },
    { key: "w", label: "WNS", mono: true, align: "right" },
  ],
  rows: [
    { c: "before this pass — privilege, Sv39, the doorbell, the mailbox and fetch translation all built", l: "36,279", w: "−1.371" },
    {
      c: "the MMU's control inputs taken from <b>decode</b> rather than from the byte strobes, which carry the address adder",
      l: "36,279",
      w: "−1.371",
    },
    {
      c: "an installed-vector flag instead of a 64-bit compare in the trap decision; the fence no longer qualified by <i>and it did not trap</i>; reset trimmed to control",
      l: "36,502",
      w: "−0.519",
    },
    {
      c: "<b>WARL masks on the sparse CSRs</b>; the instruction boundary ANDed in once at the end; a registered fence; the PRNG's constant multiplies onto DSP",
      l: "35,531",
      w: "−0.160",
    },
    {
      c: "<b>the trap's state writes one cycle after the redirect</b>, with a one-cycle fetch hold; <b>the write-slot data array into block RAM</b>",
      l: "<b>32,613</b>",
      w: "−0.147",
      _tone: "good",
    },
    {
      c: "the CSR kill term narrowed; a registered retire; the shared-MMU ownership rules; the instruction page fault delivered",
      l: "32,796",
      w: "−0.081",
    },
    {
      c: "<b>the doorbell address fix, byte strobes through staging, and the remote-write landing rule</b> — the abilities the two-node bench exercises",
      l: "32,860",
      w: "−0.081",
    },
    {
      c: "<b>the mover's <code>fifo_room</code> limit registered against a config-time constant</b> — <b>the last cone</b>, and the add-and-compare leaves the command FIFO's admission path with it",
      l: "<b>32,859</b>",
      w: "<b>+0.039</b>",
      _tone: "good",
    },
  ],
};

const split = {
  items: [
    { label: "mag — engines, agent, interlink, DRAM and staging paths", value: 16335, max: 32859 },
    { label: "rv64_mag_pe — processor, mover, transform slot", value: 16010, max: 32859 },
    { label: "sn_hub", value: 514, max: 32859 },
  ],
};

const insideCols = [
  { key: "i", label: "Instance", mono: true },
  { key: "l", label: "LUT", mono: true, align: "right" },
  { key: "b", label: "Belongs to" },
];
const insideRows = [
  {
    i: "rv64_syscore",
    l: "7,244",
    b: "<b>the processor</b> — of which the core is 6,169, so the wrapper, the L1, the MMU, the node port and the dispatch mailbox are 1,075 between them. <b>The node's worst path is now inside it</b>, and it passes",
  },
  {
    i: "mm_mover",
    l: "4,226",
    b: "<b>the node.</b> Remove the processor and this does not go away — it moves back to the memory gateway",
  },
  {
    i: "mag_xform",
    l: "4,540",
    b: "<b>the node</b>, and this one is a project's occupant inside a framework arbiter",
  },
];

// ------------------------------------------------------------- deployment
const sizeCols = [
  { key: "n", label: "What lives in staging" },
  { key: "s", label: "Floor" },
  { key: "y", label: "Why not elsewhere" },
];
const sizeRows = [
  {
    n: "page tables, a runtime plus a handful of programs",
    s: "~256 KB",
    y: "too large for a scratchpad, and DRAM is cached and non-coherent — page tables in a write-back L1 are a correctness problem and every walk would be a fill",
  },
  {
    n: "the cross-node mailbox, three peer meshes at 64 KB",
    s: "~192 KB",
    y: "the scratchpad's second port already has two writers with an assertion when they collide; staging is multi-writer and single-reader, which is the mailbox shape exactly",
  },
  {
    n: "allocator metadata — a bitmap over 4 GB at 4 KB granularity",
    s: "~128 KB",
    y: "in DRAM it would be cached, so every reclaim would interact with the invalidate sweep",
  },
  { n: "runtime spill and working set", s: "~128 KB", y: "—" },
  {
    n: "<b>floor</b>",
    s: "<b>~704 KB</b>",
    y: "<b>so the requirement is ≥ 1 MiB, and the 2 MB that exists is comfortable</b>",
    _tone: "good",
  },
];

// -------------------------------------------------------------- not owned
const notCols = [
  { key: "n", label: "Not owned" },
  { key: "w", label: "Whose it is" },
];
const notRows = [
  { n: "routing, links, arbitration between endpoints", w: "the mesh" },
  { n: "the DRAM controller, and clock crossing to it", w: "vendor IP, behind the AXI boundary" },
  { n: "what the transform computes", w: "the occupant's author; the node owns the slot" },
  { n: "what the bytes mean — layout, tiling, tensor semantics", w: "you, and your compiler" },
  { n: "your unit's memory system", w: "<b>the compute unit's author, entirely</b>", _tone: "good" },
  { n: "how many memory ports exist and where they attach", w: "the ship" },
  { n: "carrying traffic between meshes", w: "the interlink, described with the ship" },
];

/* --------------------------------------------- the interlink config window */
const ilRegs = {
  cols: [
    { key: "o", label: "Control offset", mono: true, align: "center" },
    { key: "i", label: "Interlink register", mono: true, align: "center" },
    { key: "f", label: "Fields", mono: true },
    { key: "m", label: "Meaning" },
  ],
  rows: [
    {
      o: "0xC0",
      i: "0x80",
      f: "[0] enable · [1] clear counts · [2] clear faults",
      m: "Enabled at reset. <b>The three share one write</b> — see the trap below",
      _tone: "warn",
    },
    {
      o: "0xC8",
      i: "0x88",
      f: "[1:0] mesh id",
      m: "Defaults to the node's <code>MESH_ID</code>; a build rarely writes it",
    },
    {
      o: "0xD0",
      i: "0x90",
      f: "[1:0] destination mesh · [15:8] tag",
      m: "<b>Writing this rings that mesh.</b> The tag is carried to the far side and is the sender's to define",
      _tone: "good",
    },
    {
      o: "0x28",
      i: "—",
      f: "four 16-bit lanes",
      m: "<b>Read-only:</b> inbound rings by source mesh — mesh 0 in [15:0], mesh 3 in [63:48]",
    },
  ],
};

const crossRules = {
  cols: [
    { key: "w", label: "What is written" },
    { key: "l", label: "Where it lands on the far side" },
  ],
  rows: [
    {
      w: "<b>a special address — bit 39 set</b>, naming another mesh's staging aperture",
      l: "<b>that mesh's staging, at the full 40-bit address.</b> The inbound handler keeps every bit and the staging port claims it by bit 39 and the mesh field",
      _tone: "good",
    },
    {
      w: "a DRAM address naming another mesh",
      l: "that mesh's DRAM, <b>by its low 32 bits</b> — local DRAM starts at zero, so the mesh field would put the write 4 GB out",
    },
    {
      w: "<b>any read</b>",
      l: "<b>nowhere. Reads never cross.</b> A mover source must be in this mesh, and the processor's own port is local in both directions",
      _tone: "bad",
    },
  ],
};

const catCols = [
  { key: "t", label: "Thing" },
  { key: "c", label: "Category" },
];
const catRows = [
  { t: "memory request and response encoding, tags, acks", c: "<b>fixed protocol</b>" },
  { t: "the mover's command set and descriptor form", c: "<b>fixed protocol</b>" },
  { t: "the transform slot's position, selection and handshake", c: "<b>fixed protocol</b>" },
  { t: "the internal requester protocol — <code>q_*</code>, <code>w_*</code>, <code>r_*</code>", c: "<b>fixed protocol</b>, inside the node" },
  {
    t: "<b>what plugs into the transform slot</b>",
    c: "customizable <b>addon</b> — the framework ships an identity bank and names no transform",
    _tone: "good",
  },
  {
    t: "<b>whether, how much and where staging sits</b>",
    c: "customizable <b>addon</b> — but read the trap: one of the two placements is not a tuning choice",
    _tone: "good",
  },
  { t: "<b>DRAM-port beat packing</b>", c: "customizable <b>addon</b>", _tone: "good" },
  { t: "which processor", c: "customizable — <code>CPU_RV64</code>" },
  { t: "<b>that there is a processor at all</b>", c: "<b>not a parameter.</b> There is no build without one" },
  { t: "port count, slot count, queue depths, storage primitives", c: "customizable — sized for correctness first" },
  { t: "your unit's own memories and how it stores what arrives", c: "<b>yours</b>, entirely" },
];
</script>

<template>
  <DocPage
    title="The system node"
    summary="One block per mesh, and the only place a mesh touches anything outside it: the memory instruction set and the service behind it, a control processor with the memory mover as its SIMD unit, and the hub that puts all of them on one set of attachments."
    domain="cpu"
    status="measured"
    source="src/kohakuaccel/sysnode/ · src/kohakuaccel/pe/rv64-sys/ · docs/arch/sysnode/"
  >
    <h2 class="doc-h2">What it owns</h2>
    <p class="doc-p">Four things, and nothing else.</p>
    <div class="grid gap-3 sm:grid-cols-2 my-5">
      <div class="card p-4">
        <div class="kt-text-caption font-semibold text-warm-800 dark:text-warm-200 mb-1">
          The memory instruction set
        </div>
        <p class="kt-text-caption text-warm-500 dark:text-warm-400 leading-6">
          A compute unit does not design a way of <i>asking</i> for memory; it
          inherits one. Descriptors, entry geometry, streaming runs,
          multi-destination delivery.
        </p>
      </div>
      <div class="card p-4">
        <div class="kt-text-caption font-semibold text-warm-800 dark:text-warm-200 mb-1">
          The service behind it
        </div>
        <p class="kt-text-caption text-warm-500 dark:text-warm-400 leading-6">
          Issuing the AXI bursts, streaming responses back as flits that say
          where they belong, and reassembling write bursts the mesh delivered
          out of order.
        </p>
      </div>
      <div class="card p-4">
        <div class="kt-text-caption font-semibold text-warm-800 dark:text-warm-200 mb-1">
          The control complex
        </div>
        <p class="kt-text-caption text-warm-500 dark:text-warm-400 leading-6">
          A processor, the <span class="chip">memory mover</span> as its SIMD
          memory unit, and the <span class="chip">transform slot</span> as that
          unit's extension.
        </p>
      </div>
      <div class="card p-4">
        <div class="kt-text-caption font-semibold text-warm-800 dark:text-warm-200 mb-1">
          The hub, and every attachment
        </div>
        <p class="kt-text-caption text-warm-500 dark:text-warm-400 leading-6">
          <span class="chip">sn_hub</span> — four kinds of client on one set of
          ports. <b>Nothing inside the node owns one.</b>
        </p>
      </div>
    </div>

    <p class="doc-p">
      The alternative was to give each of those its own attachment and let every
      compute unit carry its own memory system, and both halves of that were
      rejected for the same reason: <b>a mesh has very few attachments to give
      away</b>, and three of the four clients are nearly idle. Four attachments
      for three idle consumers is the cost, and a memory system replicated per
      unit is burst generation, 4 KB boundary handling and out-of-order
      reassembly copied into every unit — each copy a place to get it wrong.
      What survives is the split between <b>naming</b> memory and
      <b>serving</b> it: a unit names what it wants ahead of time, because the
      whole framework rests on addresses being computable rather than discovered
      by chasing pointers, and serving it is the node's.
    </p>

    <Fig
      caption="One component, not an assembly. The four kinds of client are told apart by what the flit is and where it is addressed, and each has its own arbiter so a stalled one cannot hold up the others. Inside, every requester speaks one internal protocol and AXI appears once, at the far boundary."
      zoom
      wide
    >
      <BlockDiagram :nodes="whole.nodes" :edges="whole.edges" :groups="whole.groups" />
    </Fig>

    <h2 class="doc-h2">What it costs</h2>
    <p class="doc-p">
      The cost is set by what is on the converged path, not by the block
      boundaries above. Every requester inside the node speaks one internal
      protocol — <span class="chip">q_valid / q_ready / q_addr / q_len /
      q_write</span> plus the <span class="chip">w_*</span> and
      <span class="chip">r_*</span> streams — and
      <b>AXI is heavy, so it appears once, at the boundary.</b>
    </p>

    <Fig
      caption="The structure that sets the cost. MP1 is PORTS + 3, plus one more with the interlink: the memory engines, the host upload window, the processor's node port, the mover's master, and the channel inbound remote writes land through. There is no configuration in which that count is smaller, because the processor is not optional."
      zoom
      wide
    >
      <BlockDiagram :nodes="conv.nodes" :edges="conv.edges" />
    </Fig>

    <SpecTable
      :cols="knobCols"
      :rows="knobRows"
      caption="The knobs that move node cost, in the order they matter. LUT figures are out-of-context synthesis of sysnode AS TOP on xcvu13p-fhgb2104-2L-e, Vivado 2024.2, one clock at 3.333 ns, PORTS = 2, from report_utilization"
    />

    <Callout kind="rule" title="A requester's presentation MUST hold until q_ready">
      <p>
        <span class="chip">{valid, write, addr, len}</span> may not change while
        a request waits. Both arbiters decide on a <b>registered</b> request
        vector and sample the bus <b>live</b>, and the grant is a single wire —
        so the two halves can disagree about which transaction is being granted.
      </p>
    </Callout>

    <WaveTrace
      :rows="qBroken.rows"
      :notes="qBroken.notes"
      variant="broken"
      label="A presentation that switches mid-wait — the wrong channel is granted"
    />
    <WaveTrace
      :rows="qFixed.rows"
      :notes="qFixed.notes"
      variant="fixed"
      label="The choice latched at the first ungranted offer"
    />

    <h3 class="doc-h3">Measured</h3>
    <SpecTable
      :cols="nodeCost.cols"
      :rows="nodeCost.rows"
      caption="sysnode synthesised AS TOP, out of context, on xcvu13p-fhgb2104-2L-e with Vivado 2024.2, -directive default (so hierarchical rows are a REBUILT netlist), one clock request at 3.333 ns, PORTS = 2 — from report_utilization, which counts CLB LUT sites. Top row scripts/tcl/ooc_sysnode.tcl, bottom scripts/tcl/ooc_sysnode_rv64.tcl 2. BRAM is in tiles"
    />

    <Callout kind="measured" title="300 MHz is met in out-of-context synthesis, with nothing failing">
      <p>
        <b>The RV64 run's worst slack is +0.039 ns at a 3.333 ns request, with
        0 failing endpoints</b> and a total violation of zero. That is
        <b>300 MHz met in synthesis</b>, and it is the whole of the claim:
        <b>this design has not been placed or routed</b>, so it is not closed
        timing and no higher frequency follows from it. Synthesis slack is
        optimistic — this tree has a recorded <b>0.740 ns</b> loss on one module
        going from synthesis to routing.
      </p>
      <p>
        This run <b>includes</b> the cross-mesh abilities below: the fixed
        doorbell window, byte strobes through staging, and the remote-write
        landing rule. Its staging banks are the byte-enable RAM, which is how
        you can tell from the report itself.
      </p>
      <p>
        <b>The two rows differ in more than the processor</b> and were taken at
        different RTL vintages. The RV32 row has not been re-run since; it is
        kept for shape, not for comparison. The RV64 one moves staging onto the
        converged path and enlarges the processor's instruction memory,
        scratchpad and L1 — which is why URAM falls from 128 to 65 while BRAM
        rises. <b>Do not subtract one row from the other.</b>
      </p>
    </Callout>

    <Callout kind="measured" title="What closed the last cone: registering the mover's room limit">
      <p>
        Until this run the binding path ran from the mover's mode register,
        through the command FIFO's <code>fifo_room</code> calculation — <b>an
        add and a compare</b> — into that FIFO's write enable: 12 levels,
        −0.081 ns, and all 123 failing endpoints were that one cone.
        <b>It was the last failing path in the node.</b>
      </p>
      <p>
        <b>Registering the room limit takes the arithmetic out of the admission
        path, and the cone closes with it.</b> The node now
        <b>meets 300 MHz in out-of-context synthesis at +0.039 ns with 0 failing
        endpoints</b>, and its worst path is inside the processor — the
        writeback register into the core's halt-cause register, 12 levels —
        where it passes.
      </p>
      <p class="font-mono kt-text-caption">
        g_rv64.u_pe/u_cpu/u_core/wb_val_reg[1]/C →<br />
        g_rv64.u_pe/u_cpu/u_core/halt_cause_reg[1]/D &nbsp; +0.039 ns
      </p>
      <p>
        <b>One endpoint rarely names a critical region</b>, and the mover cone
        was the proof: fixing the endpoint the tool named would have moved it,
        while the root was the add-and-compare six gates upstream.
      </p>
    </Callout>

    <h3 class="doc-h3">What each change bought</h3>

    <SpecTable
      :cols="bought.cols"
      :rows="bought.rows"
      caption="Same script, same part, same tool, same request, each row a re-synthesis of the whole node with one group of changes applied. The budget was 35,000 LUT with a hard ceiling of 38,000, so the final row is 2,141 under target and meets the request. The write-slot array move alone is −2,918 LUT for +8 BRAM tiles, and the PRNG's DSP attribute is −817 LUT for +8 DSP"
    />

    <h3 class="doc-h3">Where the node's area is</h3>
    <ResourceBars
      :items="split.items"
      unit="LUT"
      :max="32859"
      caption="The RV64 node's 32,859 LUT. Rows are a REBUILT netlist, so a leaf may be charged to the instance it was re-parented into — but these three sum to 32,859 exactly, which is the check to apply before trusting any breakdown"
    />

    <SpecTable
      :cols="insideCols"
      :rows="insideRows"
      caption="Inside the control complex; these three sum to 16,010 exactly. Any area argument that starts with the integration is looking in the wrong place — the core is 85% of the processor, and two of the three instances are not the processor at all"
    />

    <Callout kind="trap" title="Subtracting the complex from the node does not price a processor">
      <p>
        It subtracts <b>the mover with it</b>, and the mover does not disappear
        — it belongs to the node whatever processor sits in it. The arithmetic
        appears to work only because both complexes happen to contain the mover,
        which is luck rather than method.
      </p>
      <p>
        That is why the gate is a <b>whole-node run</b> rather than a projection.
        The like-for-like figure is complex against complex — and
        <b>the RV32 complex has not been re-measured since this work</b>, so no
        difference is quoted. What the RV64 complex costs is 16,010 LUT, of
        which 7,244 is the processor: a 64-bit datapath, hardware divide, the
        full <code>A</code> extension, three privilege levels with delegation,
        Sv39 with a hardware walker shared by fetch and data, a 256-entry branch
        target buffer with gshare and a return-address stack, a write-back L1,
        and the dispatch mailbox.
      </p>
    </Callout>

    <Callout kind="measured" title="DSP is 47 in this configuration, and 39 of them do not move with PORTS">
      <p>
        <b>32</b> for one transform bank, <b>3</b> for the mover, <b>4</b> for
        the processor's multiplier, and <b>8</b> for the mover's pseudo-random
        generator — whose four constant multiplies now carry a
        <span class="chip">use_dsp</span> attribute and are 8 DSP48 instead of
        roughly a thousand LUT of shift-and-add.
      </p>
      <p>
        <b>A figure that does not scale with the port count is what “one
        transform bank per node” means as a measurement rather than a claim.</b>
        The older rule <i>DSP is 39 in both complexes</i> is now false for the
        RV64 one, and only because of those eight — the transform bank, the
        mover and the multiplier are unchanged.
      </p>
    </Callout>

    <h2 class="doc-h2">Which processor, and what each one connects</h2>
    <Callout kind="rule" title="The processor is not a parameter. Which processor is.">
      <p>
        <span class="chip">sysnode.v</span> instantiates a processor
        unconditionally: there is no parameter that removes it and no empty slot
        where one might go. The node cannot be built as memory service alone,
        because the memory gateway on its own cannot start work without a host
        round trip and the processor on its own cannot reach memory or another
        mesh. <b>That is the structural claim.</b>
      </p>
      <p>
        <span class="chip">CPU_RV64</span> chooses <b>which</b>, and its default
        is <code>0</code> — the RV32 complex. The mover and the transform slot
        are instantiated unchanged in both.
      </p>
    </Callout>

    <SpecTable
      :cols="cfgCols"
      :rows="cfgRows"
      caption="Neither is described here as replacing the other. The RV32 one is the default and is complete; the RV64 one is a measured configuration whose memory, host, fabric, doorbell and interrupt paths are all connected, and whose remaining gap is the transform slot's own registers"
    />

    <Callout kind="rule" title="The RV64 complex dispatches through a mailbox, not through a shell">
      <p>
        It is a <b>client of the hub</b> at <code>(0,0)</code>, the corner the
        hub already decodes the processor at — but it is not a compute unit.
        Nothing kicks it, it reports no completion, and it is not enumerable.
        What it has instead is seven registers in its control region: software
        writes a destination and two payload words,
        <b>hardware builds the <code>CU_INST</code> flit</b>, and the
        <code>CU_SIGNAL</code>s that come back land in a 16-deep queue that
        raises the external interrupt line when it is non-empty.
      </p>
      <p>
        <b>Its inbound busy line is tied low and never rises.</b> A completion
        the queue cannot take is accepted and dropped, and a sticky bit records
        it — because the unit that arbitrates the fabric must not be
        flow-controlled by the fabric: held, that flit would stall the link that
        delivers the traffic which drains the queue. <b>The default RV32
        configuration dispatches too</b>, through a compute-unit shell, and
        pays the shell's lifecycle for it.
      </p>
    </Callout>

    <Callout
      kind="trap"
      title="The ship generator cannot select the RV64 complex at all"
    >
      <p>
        <span class="chip">sysnode.v</span> takes
        <span class="chip">CPU_RV64</span>, but the mesh generator emits no
        value for it — so every generated ship top elaborates the default RV32
        branch, and <b>there is no way to build a ship with the RV64 complex
        without editing the generator</b>. Every RV64 figure on this page comes
        from a standalone <span class="chip">sysnode</span> synthesis that sets
        the parameter directly.
      </p>
      <p>
        <b>The symptom is a silent substitution:</b> a build asked for the RV64
        node produces the RV32 one, elaborates cleanly, and differs only in a LUT
        count nobody compares.
      </p>
    </Callout>

    <Callout kind="trap" title="A FILL or GATHER move cannot be fully programmed from the RV64 processor">
      <p>
        The mover decodes nine registers, at config offsets
        <code>0x00, 0x10, 0x18, 0x20, 0x28, 0x30, 0x38, 0x40, 0x50</code>. The
        RV64 control region maps its own <code>0x80</code>–<code>0xBF</code>
        onto mover offsets <code>0x00</code>–<code>0x3F</code>, so
        <code>0x40</code> — the fill immediate — and <code>0x50</code> — the
        gather pitch and word count — land <b>outside that window, in the
        doorbell sub-range</b>.
      </p>
      <p>
        <b>The symptom is a move that runs with a stale immediate or a stale
        gather geometry</b> and reports success. Both registers stay reachable
        from the host's config window, and from the RV32 processor, whose
        descriptor form replays an arbitrary
        <code>{offset, value}</code> list and can therefore name every offset.
        The two windows were sized independently; nothing about the address map
        makes the overlap deliberate.
      </p>
    </Callout>

    <h2 class="doc-h2">Where the staging store sits</h2>
    <p class="doc-p">
      <b>Staging</b> is an on-chip store with a reserved range in the address
      map — reached by ordinary addresses, never by an instruction, holding
      operand words verbatim. It is not a cache: no tags, no associativity, no
      replacement, no coherence, no write policy. Where it sits is
      <span class="chip">STAGE_AT_PORT</span>, and that parameter is the one on
      this page most likely to be mistaken for a tuning choice.
    </p>

    <SpecTable :cols="placeCols" :rows="placeRows" />

    <Callout kind="trap" title="STAGE_AT_PORT is not a tuning knob, and the trade runs both ways">
      <p>
        <b>Against 0:</b> a store half the machine cannot address is not a
        shared store, and duplicating it per port does not make it one. The
        per-port copies sit upstream of where the requesters meet, so the mover
        and the interlink can never reach them — and everything a runtime keeps
        in staging, its page tables, its cross-node mailbox and its allocator
        bitmap, is reached over the converged path. It is not merely twice the
        URAM; <b>it is the wrong store</b>.
      </p>
      <p>
        <b>Against 1, and this half is real:</b> the converged form runs on
        <b>port B alone</b>. <span class="chip">mag_stage_port</span> ties port
        A off entirely, and port A is the one that moves a whole 1,024-bit entry
        per access. So the shared store is reached
        <b>one 256-bit word at a time, with one read outstanding</b> — a
        quarter of the per-access width and no read pipelining. The
        entry-granular argument belongs to <code>STAGE_AT_PORT = 0</code> and
        does not describe what ships.
      </p>
      <p>
        <b>Reachability wins</b>, because a store the mover cannot reach cannot
        hold a runtime's working set at all, and a narrow reachable store still
        beats a wide unreachable one. But quote the width honestly: staging is
        <b>L2 latency at one word per access</b>, not a wide fill path.
      </p>
      <p>
        The reason the per-port form survived as long as it did is worth
        keeping: <b>URAM is plentiful on this device</b>, so a doubled
        megabyte-scale array does not announce itself in a LUT count.
        <b>Read the memory columns of every synthesis report, not only the logic
        ones.</b>
      </p>
    </Callout>

    <Callout kind="trap" title="Staging serves one claimed burst at a time, across every requester">
      <p>
        Round-robin on a single id. A processor's page-table walks and its
        mailbox polling therefore interleave with whatever the mover is driving
        into staging, <b>at burst granularity</b>. Fine for a dispatcher — and a
        reason a hot processor loop should not keep its working set in staging
        while the mover is driving staging hard.
      </p>
      <p>
        <b>The symptom is latency, not corruption:</b> a poll loop that is fast
        when the machine is idle and several times slower under a large move,
        with nothing in any status register to say why.
      </p>
    </Callout>

    <Callout kind="rule" title="Staging honours byte strobes, and page tables depend on it">
      <p>
        The store's write port takes the AXI strobes and its banks are the
        byte-enable RAM, so a processor's <b>8-byte store writes eight bytes</b>
        of the 32-byte word and leaves the rest alone.
      </p>
      <p>
        Without them a 64-bit store wrote <b>all four lanes with the same
        value</b>. Everything a runtime keeps in staging is exactly the shape
        that breaks on: three of every four page-table entries, and every
        mailbox word that shares a 32-byte line with another. <b>The symptom is
        a translation that walks to the wrong page</b> rather than a fault, on
        tables the program is certain it wrote correctly.
      </p>
    </Callout>

    <h2 class="doc-h2">Handing work to another mesh</h2>

    <p class="doc-p">
      Two nodes reach each other over the <b>interlink</b>, and only three
      things cross it: <b>mover writes</b>, compute-unit flits addressed to a
      remote memory node, and <b>doorbells</b>. A processor's own loads and
      stores do not — its port is local — so cross-mesh data movement is always
      a descriptor, never a pointer dereference.
    </p>

    <SpecTable
      :cols="crossRules.cols"
      :rows="crossRules.rows"
      caption="Where a remote write lands, by the shape of its address. The truncation in the middle row is deliberate and the exemption in the top row is what makes another mesh's staging usable at all — without it a copy aimed at the far mesh's aperture landed in its DRAM at the aperture offset, silently"
    />

    <SpecTable
      :cols="ilRegs.cols"
      :rows="ilRegs.rows"
      caption="The processor's interlink window, at control offset 0xC0. The control region's low six bits are the interlink register's, offset by 0x80 — so 0xC0, 0xC8 and 0xD0 are the interlink's 0x80, 0x88 and 0x90"
    />

    <Callout kind="rule" title="An inbound doorbell is a level, and the handler clears the counts">
      <p>
        Each inbound ring increments the count for its <b>source mesh</b>, and
        the node raises the processor's <b>external interrupt</b> while any
        count is non-zero. A level, not an edge: a ring that arrives while
        another is being serviced is not lost.
      </p>
      <p>
        The handler reads the counts at <code>0x28</code>, then clears them, and
        the interrupt line drops with them.
      </p>
    </Callout>

    <Callout kind="trap" title="Clearing the counts also writes the enable bit">
      <p>
        <code>0xC0</code> is one register with three fields, and a write lands
        all three: <b>enable is taken from bit 0 every time</b>. So a handler
        that clears the counts with a bare <code>1 &lt;&lt; 1</code>
        <b>disables the interlink</b> in the same store, and the node stops
        carrying anything at all.
      </p>
      <p>
        The clear is <code>0b11</code> — enable and clear together — which is
        what the shipped handler writes. <b>The symptom is a machine that works
        for exactly one ring</b> and then goes quiet, with no fault bit set,
        because nothing about disabling a link is an error.
      </p>
    </Callout>

    <Callout kind="rule" title="Write, wait for the mover to report idle, then ring">
      <p>
        The pattern for handing work to another mesh is three steps and
        <b>the middle one is not optional</b>: write the data into the far
        mesh's staging with the mover, <b>poll the mover's busy bit until it
        clears</b>, then ring.
      </p>
      <p>
        The ordering rests on two facts and needs both. <b>The mover reports
        idle only once every write packet has been accepted onto the link</b>,
        and the link delivers in order. <b>The receiving interlink then holds an
        inbound doorbell until every write that arrived ahead of it has been
        acknowledged by its memory</b>, so the ring the far handler sees is
        backed by data that is in memory rather than in a queue.
      </p>
    </Callout>

    <Callout kind="trap" title="The ring is not a release fence on its own">
      <p>
        The second fact orders the ring against writes <i>already ahead of it on
        the link</i>. It says nothing about writes still leaving this node —
        <b>the sending arbiter rotates between writes, flits and doorbells</b>,
        so a ring issued while a burst is still going out can be handed to the
        link ahead of the rest of that burst and arrive first.
      </p>
      <p>
        <b>The symptom is a receiver that reads a partly written buffer</b> and
        passes on short runs, at whatever fraction of the burst the arbiter
        happened to have sent. It is timing-dependent, so it survives a small
        test and fails on a large one. Waiting for the busy bit to clear is what
        closes it, and there is no barrier instruction that substitutes.
      </p>
    </Callout>

    <Callout kind="measured" title="The two-node bench is the evidence for all of it">
      <p>
        <span class="chip">rv64_node_pair</span> is <b>two complete nodes on one
        interlink</b>, each with its own DRAM model and its own program, cross
        connected link to link. Mesh 0 writes sixteen 64-bit words into its own
        staging <i>with byte strobes</i>, has the mover copy four 32-byte words
        into <b>mesh 1's</b> staging across the link, and rings mesh 1. Mesh 1
        takes the ring <b>as an external interrupt</b>, clears the counts in its
        handler, checks the words, and rings back; mesh 0 polls its count for
        the reply.
      </p>
      <p>
        Both programs exit zero. That single run is what stands behind the
        strobes, the landing rule, the doorbell window and the interrupt level —
        <b>four abilities that each look fine in isolation and only compose in a
        two-node system.</b> Under Verilator, so it is a cycle-level result and
        not a timing one.
      </p>
    </Callout>

    <h2 class="doc-h2">Sizing a node</h2>
    <h3 class="doc-h3">How many memory ports</h3>
    <p class="doc-p">
      A port is the unit the machine grows by, and one port serves roughly two
      compute clusters. The constraint that forces more of them is not
      bandwidth: the reference machine
      <b>stopped scaling while nothing was saturated</b>, which is the
      diagnostic — the limit was the <i>server</i>, one read engine and one emit
      buffer, not the link. Ports must land on
      <b>different mesh rows</b>, because routing is X-then-Y on clamped
      coordinates: a port at <code>(0, y)</code> draws traffic to router
      <code>(GRID_LO, y)</code> and no other, so two ports on one router split
      the server without splitting the funnel.
    </p>

    <h3 class="doc-h3">How much staging</h3>
    <SpecTable
      :cols="sizeCols"
      :rows="sizeRows"
      caption="ESTIMATE throughout, and a floor rather than a recommendation. Below 1 MiB the allocator granularity has to coarsen or the mailbox has to shrink; below about 512 KiB the arrangement does not fit and page tables move to DRAM"
    />
    <p class="doc-p">
      In primitives: a URAM288 used 64 bits wide is
      <code>4096 × 64</code> = <b>32 KiB</b>, so <b>1 MiB is exactly 32 URAM</b>
      and the shipped 2 MB is 64. The full 72-bit width would give 36 KiB and
      put the figure at 29, but data words are 64 bits and the remaining 8 go
      unused — so 32 is the number to plan with. Four meshes at 64 URAM is 256
      of the part's 1,280, about 20%.
    </p>

    <h3 class="doc-h3">A procedure</h3>
    <ol class="doc-p list-decimal pl-5 space-y-1">
      <li>
        <b>Count the compute units on the mesh</b>, and divide by two for a
        first port count. Two is the production width; one understates the node.
      </li>
      <li>
        <b>Place the ports on different rows</b> of the edge ring, and check the
        generator agrees — each unit is bound to its nearest port at
        elaboration, not at runtime.
      </li>
      <li>
        <b>Decide whether you need staging at all.</b> If a runtime is going to
        live on the node's processor, you do — its page tables have nowhere else
        to go. Then take <code>STAGE_AT_PORT = 1</code>; the other value is not
        the same feature.
      </li>
      <li>
        <b>Size the store from the table above</b>, not from a bandwidth target.
        Staging is one word per access; it buys latency and reach, not width.
      </li>
      <li>
        <b>Enable the interlink only if a second mesh exists.</b> At 0 it costs
        exactly nothing, and that is maintained deliberately — every addition
        sits inside a generate or is gated by the parameter, because “costs
        nothing when off” is only true if someone keeps checking.
      </li>
        <li>
        <b>Choose the processor.</b> A kernel dispatcher wants the RV32 complex,
        which is the default. A runtime host — one that outlives the work it
        starts, needs privilege separation or wants an address space — wants the
        RV64 one; both dispatch, and choosing it today means editing the
        generator to pass <code>CPU_RV64</code>.
      </li>
      <li>
        <b>Synthesise the node out of context before believing any of it</b>,
        and <b>read the URAM and BRAM columns</b>, not only the LUT one.
      </li>
    </ol>

    <Callout kind="open" title="Open questions the flow does not answer">
      <p>
        <b>The dispatch half has never met a real compute unit.</b> Two whole
        nodes now run together in
        <span class="chip">rv64_node_pair</span> — boot, mover, interlink,
        doorbell and interrupt, end to end — but the mailbox is proven against a
        modelled unit rather than against a mesh of them, so credit accounting
        under a real dispatch load is untested.
      </p>
      <p>
        <b>Nothing ties the completion queue's depth to the dispatch rate.</b>
        Sixteen is a constant; how many instructions a dispatcher may leave
        outstanding is a software decision made somewhere else, and the only
        feedback when the two disagree is a sticky overflow bit nobody is
        obliged to read.
      </p>
      <p>
        <b>Nothing checks a staging size against what a runtime needs.</b> The
        floor above is arithmetic nobody performs in the flow; a node generated
        with a store too small for its page tables reports nothing and fails at
        run time.
      </p>
      <p>
        <b>The per-access width of the converged store is not modelled
        anywhere.</b> One word per access with one outstanding read is a
        throughput property that no tool in the flow predicts from a
        configuration, and it does not appear in any capacity figure.
      </p>
    </Callout>

    <h2 class="doc-h2">Fixed protocol, addon, convention, or yours</h2>
    <SpecTable :cols="catCols" :rows="catRows" />

    <h2 class="doc-h2">What this component does not own</h2>
    <SpecTable :cols="notCols" :rows="notRows" />
    <Callout kind="note" title="These are divisions of design, not of component">
      <p>
        The memory gateway, the control agent, the interlink and the processor
        are separate concerns and are described separately, but
        <b>the node is one module and none of them is separable from it</b>.
        They are clients of one hub because attachments are scarce, not because
        dispatch is a memory concern.
      </p>
      <p>
        Two boundaries are packaging rather than design.
        <span class="chip">noc_orchestrator.v</span> — the control agent — lives
        with the router and is instantiated by exactly one module, the memory
        gateway. The interlink's five modules implement a second routing layer
        with its own topology, deadlock argument and credit protocol, and live
        inside the gateway because the gateway hosts the endpoint; the package
        boundary should be with the ship.
      </p>
    </Callout>
  </DocPage>
</template>
