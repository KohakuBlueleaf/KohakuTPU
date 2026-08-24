<script setup>
/**
 * /framework/ship — drawn from, in full:
 *   docs/arch/ship/README.md, what-is-a-ship.md, generation.md, interlink.md,
 *   docs/arch/ship/v5-interconnect-groundtruth.md, docs/address-map.md,
 *   docs/integrate/multi-mesh.md
 *
 * Every figure below is the KohakuTPU reference instance on ONE part,
 * xcvu13p-fhgb2104-2L-e, and says so where it appears.
 */

/* ---- the boundary shape ------------------------------------------------ */
const boundary = {
  nodes: [
    { id: "clk", x: 22, y: 0, w: 10, label: "clock, reset", sub: "one each, every interface" },
    { id: "smem", x: 9, y: 5.5, w: 12, label: "S_AXI_MEM", sub: "the memory window" },
    { id: "sctrl", x: 33, y: 5.5, w: 12, label: "S_AXI_CTRL", sub: "control · staging · pass-through" },
    { id: "dram", x: 0, y: 11, w: 13, label: "M_AXI_DRAM", sub: "per requester, or one" },
    { id: "edge", x: 16, y: 11, w: 23, h: 3.6, label: "edge complex + memory boundary", accent: true },
    { id: "link", x: 42, y: 11, w: 13, label: "M/S_AXIS_LINK0/1", sub: "interlink, when enabled" },
    { id: "mesh", x: 16, y: 18, w: 23, h: 3.6, label: "routers, and the endpoints on them", accent: true },
  ],
  edges: [
    { from: "clk:b", to: "edge:t", dir: "v" },
    { from: "smem:b", to: "edge:t", dir: "v" },
    { from: "sctrl:b", to: "edge:t", dir: "v" },
    { from: "edge:l", to: "dram:r", dir: "h" },
    { from: "edge:r", to: "link:l", dir: "h" },
    { from: "edge:b", to: "mesh:t", dir: "v", accent: true },
  ],
  // Left edge sits right of the S_AXI_MEM drop at x=240 so the group's label,
  // which the component renders at (x*unit + 8), is not crossed by that wire.
  groups: [{ x: 15.5, y: 9.6, w: 25.5, h: 13.6, label: "fixed at elaboration" }],
}

const memBoundary = {
  cols: [
    { key: "f", label: "Form" },
    { key: "e", label: "What it exposes" },
    { key: "m", label: "Where the merge happens" },
    { key: "c", label: "Clock" },
  ],
  rows: [
    {
      f: "<strong>plain</strong>",
      e: "one AXI master per internal requester",
      m: "in the device image",
      c: "the mesh's",
    },
    {
      f: "<strong>concentrated</strong>",
      e: "one wider master",
      m: "inside the ship",
      c: "already crossed into the memory's",
    },
  ],
}

/* ---- the mesh picture -------------------------------------------------- */
const TOKENS = [
  ["xxx", "vec", "vec", "xxx"],
  ["mag", "mat", "mat", "xxx"],
  ["mag", "mat", "mat", "xxx"],
  ["xxx", "vec", "vec", "xxx"],
]
const TOKEN_SUB = {
  xxx: "nothing here",
  nul: "tied off",
  mag: "edge complex",
  mat: "router · local",
  vec: "edge ring",
}
const G = { x0: 110, y0: 60, cw: 110, ch: 70 }
const cells = TOKENS.flatMap((row, r) =>
  row.map((t, c) => ({
    key: `${r}-${c}`,
    r,
    c,
    t,
    sub: TOKEN_SUB[t],
    x: G.x0 + c * G.cw + 6,
    y: G.y0 + r * G.ch + 6,
    cx: G.x0 + c * G.cw + G.cw / 2,
    cy: G.y0 + r * G.ch + G.ch / 2,
    router: r >= 1 && r <= 2 && c >= 1 && c <= 2,
    empty: t === "xxx",
  })),
)
const axis = [0, 1, 2, 3]

const tokenKinds = {
  cols: [
    { key: "t", label: "Token", mono: true },
    { key: "m", label: "Meaning" },
  ],
  rows: [
    { t: "xxx", m: "nothing here. Required at the four corners, which touch no router" },
    {
      t: "nul",
      m: "the port exists but is tied off — how a side is left empty without changing the grid's shape",
    },
    { t: "mag", m: "one of the edge complex's fabric attachments" },
  ],
}

const conventions = {
  cols: [
    { key: "c", label: "Convention" },
    { key: "s", label: "Standing" },
    { key: "w", label: "What it buys" },
  ],
  rows: [
    {
      c: "Regenerate; never hand-edit a generated ship",
      s: "<strong>forced</strong>",
      w: "An edit is lost on the next regeneration, and a topology that exists only as edited Verilog cannot be checked against the picture, the driver, or anything else.",
      _tone: "warn",
    },
    {
      c: "Put gateways on edge rings and compute on router locals",
      s: "free",
      w: "A port at <code>(0, y)</code> draws traffic to exactly one router, so gateways on different rows genuinely split the load. Gateways on locals work; they just compete with the compute unit for the same router.",
    },
    {
      c: "Spread an endpoint's ports across adjacent routers rather than stacking them on one",
      s: "free",
      w: "Two attachments to the same router share that router's capacity, so a multi-port endpoint gains ports without gaining path.",
    },
    {
      c: "Let one description produce both the hardware and the software's view of the topology",
      s: "free, and currently violated",
      w: "Coordinates that the driver believes and coordinates that synthesis consumed should not be two artefacts, because nothing detects the moment they stop matching.",
      _tone: "bad",
    },
  ],
}

const disagrees = {
  cols: [
    { key: "w", label: "Where today's source disagrees" },
    { key: "d", label: "Detail" },
  ],
  rows: [
    {
      w: "The generator has a hardcoded endpoint vocabulary",
      d: "<code>scripts/py/gen_mesh.py</code> knows <code>mat</code> and <code>vec</code> by name and rejects anything else. Those are two of the reference project's compute units. The right shape is a registry: the generator knows how to place a thing with a fabric port and a coordinate, and each accelerator supplies its own list.",
    },
    {
      w: "Ship modules are named after the reference project",
      d: "<code>ktpu_ship_2x2_6c2v_il</code> and its siblings are framework assemblies carrying a project prefix, in a directory whose name (<code>synth_top</code>) describes how they are used rather than what they are.",
    },
    {
      w: "Topology is described twice",
      d: "The mesh picture is one description; the board description consumed by the software stack is another, generated separately. Its capacity fields come from a build log and its address fields from a block design that nothing in the tree can read — so parts of it can only be verified against hardware.",
    },
    {
      w: "The edge complex's port coordinates are four named parameter pairs",
      d: "The generate that instantiates ports selects between them by index, so the port count is capped at four by the parameter list rather than by anything structural. A packed vector was rejected — reasonably, since one shift misaligns a whole port and still elaborates — but the resulting form does not scale.",
    },
    {
      w: "A reusable composition sits inside a project, in a directory of device tops",
      d: "The concentrated memory boundary is <code>src/kohakutpu/top/mag_1m.v</code>: it is assembly rather than a top, and nothing in it is specific to that project, so it belongs in the framework. As it stands a second project reuses it by reaching into the first one.",
    },
  ],
}

/* ---- interlink --------------------------------------------------------- */
const meshGrid = {
  nodes: [
    { id: "m0", x: 0, y: 0, w: 13, h: 3.4, label: "mesh 0", sub: "(0,0)", accent: true },
    { id: "m1", x: 22, y: 0, w: 13, h: 3.4, label: "mesh 1", sub: "(1,0)", accent: true },
    { id: "m2", x: 0, y: 8, w: 13, h: 3.4, label: "mesh 2", sub: "(0,1)", accent: true },
    { id: "m3", x: 22, y: 8, w: 13, h: 3.4, label: "mesh 3", sub: "(1,1)", accent: true },
  ],
  edges: [
    { from: "m0:r", to: "m1:l", label: "link0 · X", dir: "h", accent: true },
    { from: "m2:r", to: "m3:l", label: "link0 · X", dir: "h", accent: true },
    { from: "m0:b", to: "m2:t", label: "link1 · Y", dir: "v" },
    { from: "m1:b", to: "m3:t", label: "link1 · Y", dir: "v" },
  ],
}

const chain = {
  nodes: [
    { id: "t0", x: -8, y: 0, w: 6, label: "tie", sub: "LINK0" },
    { id: "c0", x: 0, y: 0, w: 11, label: "mesh_0", sub: "SLR0", accent: true },
    { id: "c1", x: 13, y: 0, w: 11, label: "mesh_1", sub: "SLR1", accent: true },
    { id: "c3", x: 26, y: 0, w: 11, label: "mesh_3", sub: "SLR2", accent: true },
    { id: "c2", x: 39, y: 0, w: 11, label: "mesh_2", sub: "SLR3", accent: true },
    { id: "t1", x: 52, y: 0, w: 6, label: "tie", sub: "LINK1" },
  ],
  edges: [
    { from: "t0:r", to: "c0:l", dir: "h", dash: true },
    { from: "c0:r", to: "c1:l", dir: "h", accent: true, label: "mag_link_cdc" },
    { from: "c1:r", to: "c3:l", dir: "h", accent: true, label: "mag_link_cdc" },
    { from: "c3:r", to: "c2:l", dir: "h", accent: true, label: "mag_link_cdc" },
    { from: "c2:r", to: "t1:l", dir: "h", dash: true },
  ],
}

const linkRules = {
  cols: [
    { key: "p", label: "Property" },
    { key: "r", label: "The rule" },
    { key: "w", label: "The reason" },
  ],
  rows: [
    {
      p: "Nothing combinational crosses",
      r: "Every output is a register and every input is registered before use.",
      w: "A die-boundary crossing register <em>is</em> a flip-flop, so the tool can only use one when the path is flop to flop; one gate anywhere in the crossing forfeits it and the path becomes ordinary interconnect.",
    },
    {
      p: "<code>TREADY</code> does not cross",
      r: "The sending end never reads it. The receiver is always ready because credit reserved the space before the beat was sent.",
      w: "Wiring a real slave at the far end would put a combinational path back across the boundary, which is the thing the whole arrangement exists to avoid — so a simulation assertion watches for it.",
    },
    {
      p: "Credit is per class",
      r: "Does this packet stop at the peer, or does the peer forward it.",
      w: "One shared pool would let a stalled forward path stop traffic that was going to terminate anyway. Credit returns are absorbed into a counter on arrival and never enter a queue, so no credit return waits on the space it is about to release.",
    },
  ],
}

const crosses = {
  cols: [
    { key: "k", label: "Kind" },
    { key: "d", label: "What it is" },
  ],
  rows: [
    {
      k: "<strong>Memory writes to another mesh's memory</strong>",
      d: "Split out by address. Answered locally and at once — a posted write is the entire point, since waiting for a far memory would put a boundary round trip inside a per-word loop.",
    },
    {
      k: "<strong>Fabric flits marked for another mesh</strong>",
      d: "Encapsulated at the sending edge and injected into the receiving mesh's fabric.",
    },
    {
      k: "<strong>A doorbell</strong>",
      d: "The synchronisation primitive between meshes.",
    },
    {
      k: "<strong>A memory <em>read</em> naming another mesh</strong>",
      d: "<strong>Not forwarded.</strong> It aliases to local memory with the mesh bits ignored, exactly as it would in a single-mesh build, and a fault register records that a program did something the compiler should have caught. That is a scope decision, not a limitation of the transport: remote reads would need a return path with its own credit class.",
      _tone: "bad",
    },
  ],
}

const doorbellBroken = {
  rows: [
    { name: "wr → mesh B", kind: "bus", values: ["W0", "W1", "W2", null, null, null, null, null] },
    { name: "bresp", kind: "bus", values: [null, null, null, null, "B0", "B1", "B2", null] },
    { name: "doorbell", kind: "bit", values: [0, 0, 0, 1, 0, 0, 0, 0], mark: [3] },
    { name: "consumer", kind: "text", values: ["", "", "", "released", "reads", "", "", ""] },
  ],
  notes: [
    {
      cycle: 3,
      text: "The doorbell counts as soon as it is sent, so the consumer is released by data that is still in a queue. Posted writes and a doorbell are a race with no observable ordering.",
      tone: "bad",
    },
  ],
}
const doorbellFixed = {
  rows: [
    { name: "wr → mesh B", kind: "bus", values: ["W0", "W1", "W2", null, null, null, null, null] },
    { name: "bresp", kind: "bus", values: [null, null, null, null, "B0", "B1", "B2", null] },
    { name: "doorbell", kind: "bus", values: [null, null, null, "arrive", "wait", "wait", "wait", "count"], mark: [7] },
    { name: "consumer", kind: "text", values: ["", "", "", "", "", "", "", "released"] },
  ],
  notes: [
    {
      cycle: 7,
      text: "An inbound doorbell waits for every write ahead of it to have its write response before it counts, so a consumer released by a doorbell is released by data that is in memory rather than in a queue.",
      tone: "good",
    },
  ],
}

const crossingCost = {
  cols: [
    { key: "i", label: "what crosses a die boundary", mono: true },
    { key: "n", label: "n", mono: true, align: "right" },
    { key: "t", label: "LUT", mono: true, align: "right" },
    { key: "w", label: "wires per boundary", mono: true, align: "right" },
  ],
  rows: [
    { i: "mag_link_cdc — the interlink", n: "6", t: "139 each", w: "772 — 3.35%" },
    { i: "credited link — the station bus", n: "3", t: "1,641 total", w: "629 — 2.73%" },
    {
      i: "slr_cross — SUPERSEDED",
      n: "3",
      t: "9,795 total",
      w: "—",
      _tone: "bad",
    },
  ],
}

/* ---- address map ------------------------------------------------------- */
const outsideAddr = [
  { name: "window", bits: 3, value: "id+1", accent: true },
  { name: "ap", bits: 1 },
  { name: "rsv", bits: 1 },
  { name: "mesh", bits: 2, value: "0..3", accent: true },
  { name: "local", bits: 36, value: "64 GB per mesh" },
]
const insideAddr = [
  { name: "ap", bits: 1, accent: true },
  { name: "rsv", bits: 1 },
  { name: "mesh", bits: 2, value: "0..3", accent: true },
  { name: "local", bits: 36, value: "64 GB per mesh" },
]

const addrFields = {
  cols: [
    { key: "b", label: "Bits", mono: true },
    { key: "n", label: "Field" },
    { key: "m", label: "Meaning" },
  ],
  rows: [
    {
      b: "[42:40]",
      n: "window",
      m: "<strong>Transport, not address space.</strong> The interconnect consumes it to choose a mesh's <code>S_AXI_MEM</code> port; the mesh receives <code>addr[39:0]</code> unmodified.",
    },
    { b: "[39]", n: "aperture", m: "1 = special (staging L2, …), 0 = DRAM" },
    { b: "[38]", n: "reserved", m: "must be 0" },
    {
      b: "[37:36]",
      n: "mesh",
      m: "0..3. Tested <strong>absolutely</strong> — an address carries which mesh it belongs to, no matter who issued it or where it arrives.",
    },
    { b: "[35:0]", n: "local", m: "64 GB of map per mesh" },
  ],
}

const windows = {
  cols: [
    { key: "m", label: "mesh", mono: true },
    { key: "b", label: "window base", mono: true },
    { key: "r", label: "range" },
  ],
  rows: [
    { m: "mesh_0", b: "0x100_0000_0000", r: "1 TiB" },
    { m: "mesh_1", b: "0x200_0000_0000", r: "1 TiB" },
    { m: "mesh_2", b: "0x300_0000_0000", r: "1 TiB" },
    { m: "mesh_3", b: "0x400_0000_0000", r: "1 TiB" },
  ],
}

const worked = {
  cols: [
    { key: "w", label: "What" },
    { key: "a", label: "Address", mono: true },
  ],
  rows: [
    {
      w: "mesh 2's DRAM at local offset <code>L</code>",
      a: "0x300_0000_0000 &nbsp;+&nbsp; 0x020_0000_0000 &nbsp;+&nbsp; L<br><span class='opacity-60'>window (mesh + 1) · the [37:36] mesh field · the offset</span>",
    },
    {
      w: "mesh 2's staging L2, aperture <code>[39] = 1</code>",
      a: "0x300_0000_0000 + 0x080_0000_0000 + 0x020_0000_0000<br>= 0x3A0_0000_0000",
    },
  ],
}

const ctrlRegion = {
  cols: [
    { key: "f", label: "field", mono: true },
    { key: "m", label: "meaning" },
  ],
  rows: [
    { f: "bit AW-4", m: "which <strong>station</strong> — one per die" },
    { f: "bit 16", m: "which <strong>endpoint</strong> on that station" },
    { f: "below bit 16", m: "one 64 KiB window per endpoint" },
  ],
}

const ctrlEndpoints = {
  cols: [
    { key: "e", label: "endpoint, four per station", mono: true },
    { key: "w", label: "port width", mono: true },
  ],
  rows: [
    { e: "mesh S_AXI_MEM", w: "256-bit" },
    { e: "mesh S_AXI_CTRL", w: "32-bit" },
    { e: "DDR4 controller registers", w: "32-bit" },
    { e: "clk_wiz", w: "32-bit" },
  ],
}

const exposes = {
  cols: [
    { key: "i", label: "interface", mono: true },
    { key: "d", label: "direction" },
    { key: "w", label: "width" },
    { key: "c", label: "domain", mono: true },
  ],
  rows: [
    { i: "S_AXI_MEM", d: "slave", w: "40-bit address, DW 256", c: "axi_aclk" },
    { i: "S_AXI_CTRL", d: "slave", w: "control window", c: "axi_aclk" },
    { i: "M_AXI_DRAM", d: "master", w: "to its own DDR4", c: "dram_aclk" },
    {
      i: "M_AXIS_LINK0/1<br>S_AXIS_LINK0/1",
      d: "stream",
      w: "<code>LKW 288</code> + <code>LKU 96</code> TUSER",
      c: "axi_aclk",
    },
  ],
}

/* ---- multi-mesh software ----------------------------------------------- */
const meshConsequences = {
  cols: [
    { key: "c", label: "Consequence" },
    { key: "d", label: "Why" },
  ],
  rows: [
    {
      c: "<strong>A value is not shared by being addressed.</strong>",
      d: "Two meshes needing the same weights need two copies, uploaded separately. There is no view, no mapping and no coherence.",
    },
    {
      c: "<strong>Two regions on different meshes never alias.</strong>",
      d: "A dependency inferred from overlapping byte ranges must compare the mesh first, or every mesh serialises behind every other for no reason.",
    },
    {
      c: "<strong>A fetch cannot be shared across meshes.</strong>",
      d: "Coalescing gives one read request extra destinations, and a destination is a coordinate — which identifies a unit only within one mesh.",
    },
  ],
}

const placeOrder = {
  cols: [
    { key: "n", label: "#", mono: true, align: "center" },
    { key: "r", label: "Resolve the mesh from" },
    { key: "w", label: "Note" },
  ],
  rows: [
    { n: "1", r: "the task's own <code>mesh</code>, if it names one", w: "stated wins" },
    {
      n: "2",
      r: "otherwise the mesh its <strong>reads</strong> live on",
      w: "a unit fetches through its own memory agent, so its operands fix where it runs",
      _tone: "good",
    },
    { n: "3", r: "otherwise the mesh its <strong>writes</strong> live on", w: "results need not be local" },
    { n: "4", r: "otherwise the machine's default", w: "" },
  ],
}

const rungs = {
  cols: [
    { key: "n", label: "rung", mono: true, align: "center" },
    { key: "w", label: "who decides the split" },
    { key: "b", label: "what it buys" },
  ],
  rows: [
    {
      n: "1",
      w: "nobody — the whole machine is one device, the compiler places",
      b: "correct by default; slow where it crosses a link",
    },
    { n: "2", w: "placement chosen so traffic fits the topology", b: "removes the crossings rung 1 did not see" },
    { n: "3", w: "a deliberate parallel split, as across cards", b: "the split a model author already knows they want" },
    {
      n: "4",
      w: "placement aware that meshes differ",
      b: "uses a mesh that lacks a unit type for the work it <em>can</em> do",
    },
  ],
}
</script>

<template>
  <DocPage
    title="Ship assembly"
    summary="One complete accelerator whose boundary is clock, reset and AXI — how a mesh picture becomes that module, how several become one device image, and how a host addresses all of them."
    domain="framework"
    status="shipped"
    source="src/kohakutpu/top/generated/ · scripts/py/gen_mesh.py · docs/arch/ship/ · docs/address-map.md"
  >
    <h2 class="doc-h2">The boundary is clock, reset and AXI</h2>
    <p class="doc-p">
      A ship is one complete, self-contained accelerator: a mesh of routers, the endpoints on
      them, one edge complex, the memory boundary behind it, and the AXI surface in front.
      Everything inside is fixed at elaboration. Everything outside is AXI.
    </p>

    <Fig
      caption="One clock and one reset serve every interface, master and slave alike, so neither carries a direction prefix, and interface-inference attributes on the port list name them all so the tool ties them up on its own."
      zoom
    >
      <BlockDiagram :nodes="boundary.nodes" :edges="boundary.edges" :groups="boundary.groups" />
    </Fig>

    <Callout kind="rule" title="Why the boundary is exactly this">
      <p>
        The shape is not an accident of convenience — it is what makes a ship droppable into a
        vendor block design without hand-wiring. It is <strong>fixed protocol</strong>: a ship has
        a name, a fixed shape, and a boundary consisting of clock, reset and AXI interfaces, and
        nothing else.
      </p>
    </Callout>

    <h3 class="doc-h3">Two forms of memory boundary</h3>
    <SpecTable
      :cols="memBoundary.cols"
      :rows="memBoundary.rows"
      caption="Which one to use is a device-image decision, not a mesh decision."
    />

    <Callout kind="open" title="Where today's source disagrees">
      <p>
        <code>src/kohakutpu/top/mag_1m.v</code> is a reusable composition in a directory of device
        tops. It is the concentrated memory boundary above, and it is assembly, not a top.
      </p>
    </Callout>

    <h3 class="doc-h3">What a ship costs</h3>
    <Callout kind="rule">
      <p>
        <strong
          >The cost of a ship is the sum of its parts and the wiring between them, and the wiring
          is not free.</strong
        >
        A mesh's routers are its fixed overhead; the endpoints are what you actually wanted. The
        ratio between those two is the topology decision, and it is the reason the fabric pages
        spend so much effort on what a router costs per port.
      </p>
    </Callout>

    <h2 class="doc-h2">The mesh is a picture</h2>
    <p class="doc-p">
      A mesh is described as a grid of fixed-width tokens. The interior is the router grid; the
      first and last row are the north and south edge rings, the first and last column the west
      and east edge rings.
    </p>

    <Fig
      caption="Routers sit at (1..NX, 1..NY); edge endpoints sit just outside, at (x,0), (x,NY+1), (0,y) and (NX+1,y) — reachable precisely because of the coordinate clamp in the fabric's routing rule. Non-square grids fall out of this, which is why the router takes GRID_X_HI and GRID_Y_HI separately rather than one square bound."
      zoom
    >
      <svg viewBox="40 20 760 350" class="dgm" role="img" aria-label="A 4 by 4 token grid describing a 2 by 2 mesh">
        <text x="60" y="46" class="dgm-sub">x</text>
        <text x="60" y="99" class="dgm-sub">y</text>

        <text v-for="c in axis" :key="`cx${c}`" :x="G.x0 + c * G.cw + G.cw / 2" y="46"
              text-anchor="middle" class="dgm-sub">{{ c }}</text>
        <text v-for="r in axis" :key="`cy${r}`" x="94" :y="G.y0 + r * G.ch + G.ch / 2 + 4"
              text-anchor="end" class="dgm-sub">{{ r }}</text>

        <rect x="217" y="127" width="226" height="146" rx="10" fill="none"
              stroke="var(--gem-main)" stroke-width="1" stroke-dasharray="4 4" opacity="0.55" />

        <g v-for="cell in cells" :key="cell.key">
          <rect
            :x="cell.x" :y="cell.y" width="98" height="58" rx="6"
            :class="cell.router && !cell.empty ? 'dgm-box-accent' : 'dgm-box'"
            :stroke-dasharray="cell.empty ? '4 4' : undefined"
            :opacity="cell.empty ? 0.45 : 1"
          />
          <text :x="cell.cx" :y="cell.cy - 2" text-anchor="middle" class="dgm-label" font-weight="600">
            {{ cell.t }}
          </text>
          <text :x="cell.cx" :y="cell.cy + 12" text-anchor="middle" class="dgm-sub">{{ cell.sub }}</text>
        </g>

        <rect x="600" y="92" width="18" height="13" rx="3" class="dgm-box-accent" />
        <text x="626" y="103" class="dgm-sub">router · a local port</text>
        <rect x="600" y="120" width="18" height="13" rx="3" class="dgm-box" />
        <text x="626" y="131" class="dgm-sub">edge-ring endpoint</text>
        <rect x="600" y="148" width="18" height="13" rx="3" class="dgm-box"
              stroke-dasharray="4 4" opacity="0.45" />
        <text x="626" y="159" class="dgm-sub">xxx · nothing here</text>
        <rect x="600" y="176" width="18" height="13" rx="3" fill="none"
              stroke="var(--gem-main)" stroke-width="1" stroke-dasharray="4 4" opacity="0.55" />
        <text x="626" y="187" class="dgm-sub">the router rectangle</text>

        <text x="600" y="222" class="dgm-sub">routers at (1..NX, 1..NY)</text>
        <text x="600" y="238" class="dgm-sub">edge endpoints just outside:</text>
        <text x="600" y="254" class="dgm-sub">(x,0) (x,NY+1) (0,y) (NX+1,y)</text>
      </svg>
    </Fig>

    <p class="doc-p">
      Three token kinds are structural rather than project-specific. The rest name endpoint types,
      and those are supplied by the accelerator being built.
    </p>
    <SpecTable :cols="tokenKinds.cols" :rows="tokenKinds.rows" />

    <p class="doc-p">
      The generator emits the router instances with their per-axis grid bounds, the link nets
      between them, the endpoint instances, the edge complex with its port coordinates, and the
      AXI boundary. Two properties of the generated file matter more than its contents.
    </p>

    <Callout kind="trap" title="It is generated, and says so">
      <p>
        A hand-edit is lost on the next regeneration, and a topology that exists only as edited
        Verilog cannot be checked against anything.
      </p>
    </Callout>

    <Callout kind="rule" title="One picture is the single source of the topology">
      <p>
        The coordinates the software stack needs — where each endpoint is, which rows the memory
        ports are on, what the grid bounds are — are the same coordinates synthesis consumed. Any
        second description of the same topology is a place for the two to drift.
      </p>
    </Callout>

    <h3 class="doc-h3">Generation is elaboration, not runtime</h3>
    <p class="doc-p">
      A generated ship has no configuration registers for its shape. Every coordinate, grid bound
      and endpoint type is a parameter resolved at synthesis, so the cost of an unused feature is
      zero rather than small.
    </p>
    <Callout kind="note" title="The clearest example is the interlink">
      <p>
        With it disabled, every one of its nets is tied to a constant, every use folds, and the
        generated top does not expose the ports at all — the resulting build is identical to one
        made before the interlink existed. That property is maintained deliberately: every
        addition sits inside a generate or is gated by the parameter, because “costs nothing when
        off” is only true if someone keeps checking.
      </p>
    </Callout>

    <h3 class="doc-h3">Conventions</h3>
    <SpecTable :cols="conventions.cols" :rows="conventions.rows" caption="One forced, three free." />

    <SpecTable :cols="disagrees.cols" :rows="disagrees.rows" />

    <h2 class="doc-h2">Several ships in one image</h2>
    <p class="doc-p">
      One mesh is bounded by how much fabric a die region can hold. Past that, the answer is not a
      bigger mesh — it is several, joined at their edge complexes. That decision was made on
      measurement rather than on argument: a single mesh spanning several die regions was
      implemented, and its worst path was almost entirely routing with no logic in it. The
      measured path, and the die it was measured on, are on
      <RouterLink to="/framework/physical" class="doc-link">Floorplan and clocks</RouterLink>.
    </p>

    <Callout kind="rule" title="The interlink is a second routing layer">
      <p>
        It does not inherit the fabric's deadlock proof, so it gets its own by the same argument:
        dimension-order routing on <strong>mesh</strong> coordinates over a rectangular grid of
        meshes. Mesh id maps to <code>(x, y) = (id[0], id[1])</code>; X first, then Y.
      </p>
    </Callout>

    <Fig
      caption="Two links per mesh, not N. The mesh id is a fixed narrow field, so adding a fifth mesh is a change to the message format rather than a parameter change, and a port count that cannot vary should not be written as though it can."
      zoom
    >
      <BlockDiagram :nodes="meshGrid.nodes" :edges="meshGrid.edges" />
    </Fig>

    <Callout kind="trap" title="link1's forward class is provably dead">
      <p>
        A forwarded packet <strong>always turns X into Y</strong>, never Y into X. So link0's
        forward class feeds link1, and link1's forward class is provably dead — traffic there is
        the turn the model forbids, which makes it a fault to report rather than a case to handle.
      </p>
      <p>
        The channel dependency graph is X to Y and nothing else. Acyclic, hence deadlock-free —
        and only while the mesh-of-meshes stays a grid.
      </p>
    </Callout>

    <h3 class="doc-h3">Four meshes, as actually built</h3>
    <Fig
      caption="As built: the four-mesh v5 device image on xcvu13p-fhgb2104-2L-e. Six mag_link_cdc instances (xpm_fifo_async, independent wr_clk/rd_clk) on an open chain; the two ends, mesh_0/S_AXIS_LINK0 and mesh_2/S_AXIS_LINK1, are tied off and report as BD 41-759 on every build. The mesh-to-mesh links do not go through the AXI interconnect at all."
      zoom
    >
      <BlockDiagram :nodes="chain.nodes" :edges="chain.edges" />
    </Fig>

    <Callout kind="note" title="A line, not a ring">
      <p>
        The grid model above admits four links; the image built has three, all of them between
        SLR-adjacent dies, with the fourth pair tied off. Closing the ring is what forces one long
        link: a 4-cycle cannot embed in a 4-node path without one edge spanning. That long one is
        what a plain shift-register pipe exists for, legal precisely because the link protocol is
        credit-based and has no handshake to preserve —
        <strong>add stages there and nowhere else</strong>, since a pipeline stage anywhere with a
        real ready signal reintroduces the combinational crossing the link asserts against.
      </p>
    </Callout>

    <h3 class="doc-h3">Three structural properties of a link</h3>
    <SpecTable
      :cols="linkRules.cols"
      :rows="linkRules.rows"
      caption="Each is a rule rather than a preference, and each has a physical reason behind it that belongs to the physical layer but shows up here as protocol."
    />

    <Callout kind="note" title="One small uniformity worth copying">
      <p>
        Every packet has at least one beat, including the two that carry no data. Their beat is
        zero and ignored. One wasted beat on two rare packet kinds removes a special case from the
        framing, both queues, the arbiter and both benches.
      </p>
    </Callout>

    <h3 class="doc-h3">What crosses, and what “arrived” means</h3>
    <SpecTable :cols="crosses.cols" :rows="crosses.rows" />

    <Callout kind="rule" title="Completion means landed">
      <p>
        An inbound doorbell waits for every write ahead of it to have its write response before it
        counts, so a consumer released by a doorbell is released by data that is in memory rather
        than in a queue. Without that rule, posted writes and a doorbell are a race with no
        observable ordering.
      </p>
    </Callout>

    <WaveTrace :rows="doorbellBroken.rows" :notes="doorbellBroken.notes" variant="broken" label="doorbell counts on send" />
    <WaveTrace :rows="doorbellFixed.rows" :notes="doorbellFixed.notes" variant="fixed" label="doorbell waits for the write responses" />
    <p class="kt-text-caption text-warm-500 dark:text-warm-400 -mt-2 mb-6">
      Schematic. The ordering is the rule; the cycle numbers are illustration, not a measurement.
    </p>

    <Callout kind="trap" title="The source coordinate is preserved across a crossing">
      <p>
        Rewriting it to the receiving edge's own coordinate would make two remote bursts arriving
        at one endpoint indistinguishable — and telling senders apart is how a receiving unit
        avoids merging two senders' data into one region. The cost is that “answer the sender” no
        longer resolves, so a remote transfer must name its acknowledgement destination
        explicitly, and a fault register reports one that does not.
      </p>
    </Callout>

    <h3 class="doc-h3">What it costs</h3>
    <Callout kind="rule">
      <p>
        <strong>Several ships in one image cost the interlink once per ship</strong>, plus the
        boundary crossing registers. Against a mesh, that is small. Against the alternative — one
        mesh stretched across the same area — it is the difference between a design that closes
        and one that does not. Disabled, it costs nothing at all.
      </p>
    </Callout>
    <SpecTable
      :cols="crossingCost.cols"
      :rows="crossingCost.rows"
      caption="MEASURED on xcvu13p-fhgb2104-2L-e, against a 23,040-wire budget per boundary. The interlink and the station bus are what cross a boundary today. slr_cross was the SmartConnect tree's register slice and is superseded — it is shown only to price what the credited link replaced, a factor of 6.0 on the connective tissue between dies alone."
    />

    <h2 class="doc-h2">The address map</h2>
    <p class="doc-p">
      The map is a ship-level fact because it is the only place the whole machine is visible at
      once. The machine is a <strong>40-bit</strong> machine: every address a unit issues, every
      address in an instruction, and every address a decoder tests is 40 bits.
    </p>

    <BitField :fields="insideAddr" caption="Inside — what a unit issues and every decoder tests" />

    <p class="doc-p">
      A host master does not drive those 40 bits directly. It drives a <strong>43-bit</strong> AXI
      address, <code>outside = (mesh + 1) &lt;&lt; 40 | inside</code>. The address space is 40
      bits; the transport is 43. A mesh cannot tell whether a request came from its own mover or
      from the host DMA engine.
    </p>

    <BitField :fields="outsideAddr" caption="Outside — what a host master drives" />

    <SpecTable :cols="addrFields.cols" :rows="addrFields.rows" />

    <SpecTable
      :cols="windows.cols"
      :rows="windows.rows"
      caption="The four windows of the four-mesh v5 device image on xcvu13p-fhgb2104-2L-e. 0x000_… is left free so the control space stays below 4 GB and the host bridge's AXI-Lite can reach it. Reachable by jtag_axi_0/Data and xdma_0/M_AXI only; explicitly excluded from xdma_0/M_AXI_LITE."
    />

    <Callout kind="note" title="Why a prefix exists at all">
      <p>
        <code>S_AXI_MEM</code> declares 40-bit addressing, so its <code>reg0</code> segment is a
        fixed 1 TB and Vivado will only place it on a 1 TB boundary. Four of those cannot tile
        inside one 1 TB space. Assigning them at 64 GB spacing does not fail loudly — Vivado
        discards the offsets and puts <strong>all four meshes at offset 0</strong>, which is what
        <code>BD 41-1377</code> reports.
      </p>
    </Callout>

    <SpecTable
      :cols="worked.cols"
      :rows="worked.rows"
      caption="The mesh id appears TWICE — once in the window, once in [37:36]. That is not redundancy to be optimised away."
    />

    <Callout kind="trap" title="If the window and the address disagree, nothing faults">
      <p>
        <code>mine</code> simply stays low in <code>mag_stage_port.v</code> and no requester claims
        the beat, so the access is never answered — <strong>it presents as a hang, not as an
        error</strong>. A driver that sets the window but forgets <code>[37:36]</code> sees exactly
        this.
      </p>
      <p>
        The same independence is what makes remote entry work: the window chooses where a
        transaction <em>enters</em> and the address chooses where it <em>lands</em>. A write into
        mesh 2's window carrying <code>[37:36] = 3</code> is decoded by mesh 2 as remote
        (<code>awaddr[37:36] != my_mesh</code>) and forwarded over the interlink.
      </p>
    </Callout>

    <Callout kind="open" title="NOT YET TRACED">
      <p>
        That the <code>S_AXI_MEM</code> path reaches the interlink forwarder the way the mover's
        path does. The decoders are absolute, so the address is <em>classified</em> remote;
        whether the forwarding is wired for host traffic is unverified. Do not plan around it
        until someone follows the path.
      </p>
    </Callout>

    <Callout kind="trap" title="Capacity: the map is bigger than the memory">
      <p>
        The map gives each mesh 64 GB of local space. Each mesh has <strong>4 GB</strong> of DDR4
        behind it, so addresses from 4 GB to 64 GB within a mesh decode correctly, reach
        <code>M_AXI_DRAM</code>, and hit nothing. Staying under 4 GB per mesh is a compiler
        invariant, not something the hardware checks — unlike an unimplemented aperture, which
        does fault. <em>(Four-mesh v5 image on xcvu13p-fhgb2104-2L-e.)</em>
      </p>
    </Callout>

    <h3 class="doc-h3">Control, below 4 GiB</h3>
    <p class="doc-p">
      Control decodes positionally rather than from a table of hand-assigned bases: the station
      comes off one field and the endpoint off another, and every endpoint gets one 64 KiB window.
    </p>
    <SpecTable
      :cols="ctrlRegion.cols"
      :rows="ctrlRegion.rows"
      caption="Reachable by all three managers. Control must end below 4 GiB because the host bridge's AXI-Lite is 32-bit and cannot reach higher — which is also why the mesh memory windows start at 1 TiB and 0x000_… is left free."
    />
    <SpecTable
      :cols="ctrlEndpoints.cols"
      :rows="ctrlEndpoints.rows"
      caption="The deployed shape on xcvu13p-fhgb2104-2L-e. There is no GPIO endpoint: it existed to expose interlink status, and that port is not needed."
    />

    <h3 class="doc-h3">One module, several positions</h3>
    <Callout kind="rule">
      <p>
        The mesh id is <strong>writable at runtime</strong>, with the elaboration parameter
        supplying only its reset value. So several instances of the same generated module can
        occupy different positions in the grid, and the instances differ by configuration rather
        than by being different modules.
      </p>
      <p>
        A flit likewise carries a spare header bit meaning “this is for another mesh”, which is
        zero on every flit a single-mesh build ever produces. That is what lets one compiler
        target both: the single-mesh case is the multi-mesh case with a field left at zero, rather
        than a different encoding.
      </p>
    </Callout>

    <h3 class="doc-h3">What each mesh exposes, as built</h3>
    <SpecTable
      :cols="exposes.cols"
      :rows="exposes.rows"
      caption="Four-mesh v5 device image on xcvu13p-fhgb2104-2L-e. S_AXI_MEM omits awsize/awburst/awlock/awcache/awprot/awqos and their AR equivalents; awlen IS present, so bursts work — the slave is full-width INCR only, by design."
    />

    <h2 class="doc-h2">Programs across several meshes</h2>
    <p class="doc-p">
      A second mesh is not a wider first one. <strong>It is a second memory</strong>, and every
      consequence follows from that. A mesh's units fetch operands through <em>their</em> memory
      agent, which serves <em>that</em> mesh's memory, so an address is not a number — it is a
      pair, <code>(mesh, offset)</code>, and the same offset on two meshes names two different
      bytes.
    </p>
    <SpecTable
      :cols="meshConsequences.cols"
      :rows="meshConsequences.rows"
      caption="A compiler that misses any of these generates programs that are wrong rather than slow."
    />

    <h3 class="doc-h3">The mesh axis constrains; it does not schedule</h3>
    <p class="doc-p">
      Placement across the units <em>within</em> a mesh is a scheduling decision: any unit of the
      right type can run the task, and choosing badly costs time. Which <strong>mesh</strong> a
      task runs on is not that. A task cannot run where its operands are not, whatever the load
      says.
    </p>
    <SpecTable
      :cols="placeOrder.cols"
      :rows="placeOrder.rows"
      caption="Place resolves the mesh before it considers a coordinate (compiler/kohakuaccel/passes/place.py)."
    />

    <Callout kind="rule" title="Reads decide before writes">
      <p>
        The two are not symmetric: <strong>operands must be local, results need not be.</strong> A
        unit writing to another mesh is an ordinary remote transfer; a unit reading operands from
        two meshes at once has no mesh to run on, and that is refused rather than guessed.
      </p>
    </Callout>

    <Callout kind="trap" title="Load must be accumulated per (mesh, coordinate)">
      <p>
        Not per coordinate. The same coordinate on two meshes is two different units, and one
        counter for both balances a task against work on a mesh it cannot see.
      </p>
    </Callout>

    <h3 class="doc-h3">The spectrum</h3>
    <SpecTable
      :cols="rungs.cols"
      :rows="rungs.rows"
      caption="Not four implementations — one implementation with progressively more of the decisions taken from the user, which is why rung 4's scheduler improvements land in rung 1 for free."
    />
    <p class="doc-p">
      The build order that follows is worth stating, because the tempting one is wrong.
      <strong>Ship rung 1, plus the manual knob at 2, 3 and 4. Then write real kernels at 2, 3 and
      4 and measure them against rung 1.</strong> Those hand-written kernels are the specification
      for the automatic version.
    </p>

    <Callout kind="open" title="No cost model for a collective exists">
      <p>
        One link has been timed on the reference machine, and the result argues against modelling
        the <em>link</em> at all: the measured rate was 3% of the fabric's ceiling, so what a
        transfer costs is a property of the engine driving it, not of the interconnect. A cost
        model keyed on topology and bandwidth would have been wrong by a factor of 33. Recorded as
        an open question in <code>docs/integrate/multi-mesh.md</code> §8 — treat the ratio as
        belonging to the build that produced it.
      </p>
    </Callout>

    <Callout kind="open" title="Nothing checks that a machine description matches the device">
      <p>
        A mesh map generates hardware and a machine description tells software the same thing
        twice, with nothing comparing them. Capacity is not checked either: nothing refuses an
        allocation that does not fit a mesh's memory, and it becomes a runtime failure with no
        diagnosis.
      </p>
    </Callout>
  </DocPage>
</template>
