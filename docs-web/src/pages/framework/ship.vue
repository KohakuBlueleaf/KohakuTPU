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
    {
      id: "clk",
      x: 22,
      y: 0,
      w: 10,
      label: "clock, reset",
      sub: "one each, every interface",
    },
    {
      id: "smem",
      x: 9,
      y: 5.5,
      w: 12,
      label: "S_AXI_MEM",
      sub: "the memory window",
    },
    {
      id: "sctrl",
      x: 33,
      y: 5.5,
      w: 12,
      label: "S_AXI_CTRL",
      sub: "control · staging · pass-through",
    },
    {
      id: "dram",
      x: 0,
      y: 11,
      w: 13,
      label: "M_AXI_DRAM",
      sub: "per requester, or one",
    },
    {
      id: "edge",
      x: 16,
      y: 11,
      w: 23,
      h: 3.6,
      label: "edge complex + memory boundary",
      accent: true,
    },
    {
      id: "link",
      x: 42,
      y: 11,
      w: 13,
      label: "M/S_AXIS_LINK0/1",
      sub: "interlink, when enabled",
    },
    {
      id: "mesh",
      x: 16,
      y: 18,
      w: 23,
      h: 3.6,
      label: "routers, and the endpoints on them",
      accent: true,
    },
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
  groups: [
    { x: 15.5, y: 9.6, w: 25.5, h: 13.6, label: "fixed at elaboration" },
  ],
};

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
};

/* ---- the mesh picture -------------------------------------------------- */
const TOKENS = [
  ["xxx", "vec", "vec", "xxx"],
  ["mag", "mat", "mat", "xxx"],
  ["mag", "mat", "mat", "xxx"],
  ["xxx", "vec", "vec", "xxx"],
];
const TOKEN_SUB = {
  xxx: "nothing here",
  nul: "tied off",
  mag: "edge complex",
  mat: "router · local",
  vec: "edge ring",
};
const G = { x0: 110, y0: 60, cw: 110, ch: 70 };
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
);
const axis = [0, 1, 2, 3];

const tokenKinds = {
  cols: [
    { key: "t", label: "Token", mono: true },
    { key: "m", label: "Meaning" },
  ],
  rows: [
    {
      t: "xxx",
      m: "nothing here. Required at the four corners, which touch no router",
    },
    {
      t: "nul",
      m: "the port exists but is tied off — how a side is left empty without changing the grid's shape",
    },
    { t: "mag", m: "one of the edge complex's fabric attachments" },
  ],
};

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
};

/* ---- what moves a ship's cost ------------------------------------------ */
const knobs = {
  cols: [
    { key: "k", label: "Knob", mono: true },
    { key: "w", label: "What it moves" },
  ],
  rows: [
    {
      k: "grid size",
      w: "<strong>Everything.</strong> Routers are the fixed overhead and endpoints are what you wanted; the ratio between them is the topology decision. A grid one row larger is <code>NX</code> more routers whether or not you fill the locals.",
    },
    {
      k: "edge slots used",
      w: "Nearly nothing. An edge endpoint costs a link, not a router — which is why capacity is <code>NX*NY + 2*(NX+NY)</code> and not <code>NX*NY</code>.",
    },
    {
      k: "<code>PORTS</code>",
      w: "One <code>mag_mem_port</code> each, plus a fabric attachment each. A port serves roughly two clusters, and <strong>capped at 4</strong> by the parameter list rather than by anything structural.",
    },
    {
      k: "<code>ILINK</code>",
      w: "Zero generates <em>none</em> of it — no switch, no links, no extra AXI master, and the remote address decode folds to a constant. Enabled, it is <code>mag_link_cdc</code> at 139 LUT per crossing.",
    },
    {
      k: "concentrated vs plain",
      w: "Where the requesters meet and where the clock crossing happens, not how much there is of it. A device-image decision.",
    },
    {
      k: "<code>MW</code>",
      w: "The memory beat. <code>mag_dram_port</code> packs <code>DATA_W → MW</code>, so nothing inside the mesh learns the memory width.",
    },
  ],
};

/* ---- interlink --------------------------------------------------------- */
const meshGrid = {
  nodes: [
    {
      id: "m0",
      x: 0,
      y: 0,
      w: 13,
      h: 3.4,
      label: "mesh 0",
      sub: "(0,0)",
      accent: true,
    },
    {
      id: "m1",
      x: 22,
      y: 0,
      w: 13,
      h: 3.4,
      label: "mesh 1",
      sub: "(1,0)",
      accent: true,
    },
    {
      id: "m2",
      x: 0,
      y: 8,
      w: 13,
      h: 3.4,
      label: "mesh 2",
      sub: "(0,1)",
      accent: true,
    },
    {
      id: "m3",
      x: 22,
      y: 8,
      w: 13,
      h: 3.4,
      label: "mesh 3",
      sub: "(1,1)",
      accent: true,
    },
  ],
  edges: [
    { from: "m0:r", to: "m1:l", label: "link0 · X", dir: "h", accent: true },
    { from: "m2:r", to: "m3:l", label: "link0 · X", dir: "h", accent: true },
    { from: "m0:b", to: "m2:t", label: "link1 · Y", dir: "v" },
    { from: "m1:b", to: "m3:t", label: "link1 · Y", dir: "v" },
  ],
};

const chain = {
  nodes: [
    { id: "t0", x: -7, y: 0.5, w: 5, h: 5, label: "tie", sub: "LINK0" },
    {
      id: "c0",
      x: 0,
      y: 0,
      w: 6,
      h: 6,
      label: "mesh_0",
      sub: "SLR0",
      accent: true,
    },
    {
      id: "c1",
      x: 11,
      y: 0,
      w: 6,
      h: 6,
      label: "mesh_1",
      sub: "SLR1",
      accent: true,
    },
    {
      id: "c3",
      x: 22,
      y: 0,
      w: 6,
      h: 6,
      label: "mesh_3",
      sub: "SLR2",
      accent: true,
    },
    {
      id: "c2",
      x: 33,
      y: 0,
      w: 6,
      h: 6,
      label: "mesh_2",
      sub: "SLR3",
      accent: true,
    },
    { id: "t1", x: 44, y: 0.5, w: 5, h: 5, label: "tie", sub: "LINK1" },
  ],
  edges: [
    { from: "t0:r", to: "c0:l", dir: "h", dash: true },
    { from: "c0:r", to: "c1:l", dir: "h", accent: true, label: "mag_link_cdc" },
    { from: "c1:r", to: "c3:l", dir: "h", accent: true, label: "mag_link_cdc" },
    { from: "c3:r", to: "c2:l", dir: "h", accent: true, label: "mag_link_cdc" },
    { from: "c2:r", to: "t1:l", dir: "h", dash: true },
  ],
};

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
};

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
};

const doorbellBroken = {
  rows: [
    {
      name: "wr → mesh B",
      kind: "bus",
      values: ["W0", "W1", "W2", null, null, null, null, null],
    },
    {
      name: "bresp",
      kind: "bus",
      values: [null, null, null, null, "B0", "B1", "B2", null],
    },
    {
      name: "doorbell",
      kind: "bit",
      values: [0, 0, 0, 1, 0, 0, 0, 0],
      mark: [3],
    },
    {
      name: "consumer",
      kind: "text",
      values: ["", "", "", "released", "reads", "", "", ""],
    },
  ],
  notes: [
    {
      cycle: 3,
      text: "The doorbell counts as soon as it is sent, so the consumer is released by data that is still in a queue. Posted writes and a doorbell are a race with no observable ordering.",
      tone: "bad",
    },
  ],
};
const doorbellFixed = {
  rows: [
    {
      name: "wr → mesh B",
      kind: "bus",
      values: ["W0", "W1", "W2", null, null, null, null, null],
    },
    {
      name: "bresp",
      kind: "bus",
      values: [null, null, null, null, "B0", "B1", "B2", null],
    },
    {
      name: "doorbell",
      kind: "bus",
      values: [null, null, null, "arrive", "wait", "wait", "wait", "count"],
      mark: [7],
    },
    {
      name: "consumer",
      kind: "text",
      values: ["", "", "", "", "", "", "", "released"],
    },
  ],
  notes: [
    {
      cycle: 7,
      text: "An inbound doorbell waits for every write ahead of it to have its write response before it counts, so a consumer released by a doorbell is released by data that is in memory rather than in a queue.",
      tone: "good",
    },
  ],
};

const crossingCost = {
  cols: [
    { key: "i", label: "what crosses a die boundary", mono: true },
    { key: "n", label: "n", mono: true, align: "right" },
    { key: "t", label: "LUT", mono: true, align: "right" },
    { key: "w", label: "wires per boundary", mono: true, align: "right" },
  ],
  rows: [
    {
      i: "mag_link_cdc — the interlink",
      n: "6",
      t: "139 each",
      w: "772 — 3.35%",
    },
    {
      i: "credited link — the station bus",
      n: "3",
      t: "1,641 total",
      w: "629 — 2.73%",
    },
    {
      i: "slr_cross — SUPERSEDED",
      n: "3",
      t: "9,795 total",
      w: "—",
      _tone: "bad",
    },
  ],
};

/* ---- address map ------------------------------------------------------- */
const outsideAddr = [
  { name: "window", bits: 3, value: "id+1", accent: true },
  { name: "ap", bits: 1 },
  { name: "rsv", bits: 1 },
  { name: "mesh", bits: 2, value: "0..3", accent: true },
  { name: "local", bits: 36, value: "64 GB per mesh" },
];
const insideAddr = [
  { name: "ap", bits: 1, accent: true },
  { name: "rsv", bits: 1 },
  { name: "mesh", bits: 2, value: "0..3", accent: true },
  { name: "local", bits: 36, value: "64 GB per mesh" },
];

const addrFields = {
  cols: [
    { key: "f", label: "Field", mono: true },
    { key: "w", label: "Width", align: "right" },
    { key: "p", label: "Position", mono: true },
    { key: "o", label: "Owner" },
  ],
  rows: [
    {
      f: "window",
      w: "3",
      p: "addr[42:40]",
      o: "<strong>the device image.</strong> Transport, not address space: the interconnect consumes it to choose a mesh's <code>S_AXI_MEM</code> port, and the mesh receives <code>addr[39:0]</code> unmodified. It does not exist inside a ship",
    },
    {
      f: "aperture",
      w: "1",
      p: "addr[39]",
      o: "framework. 1 selects a special region (staging L2, …), 0 selects DRAM",
    },
    { f: "reserved", w: "1", p: "addr[38]", o: "framework. MUST be 0" },
    {
      f: "mesh",
      w: "2",
      p: "addr[37:36]",
      o: "framework, and tested <strong>absolutely</strong> — an address carries which mesh it belongs to no matter who issued it or where it arrives. This is what makes remote entry work",
    },
    {
      f: "local",
      w: "36",
      p: "addr[35:0]",
      o: "<strong>you.</strong> 64 GB of map per mesh, of which 4 GB is behind real memory",
    },
  ],
};

/* ---- window against address -------------------------------------------- */
const winBroken = {
  rows: [
    {
      name: "AWADDR",
      kind: "bus",
      values: ["0x300_0000_0000", null, null, null, null, null],
    },
    { name: "window", kind: "text", values: ["mesh 2", "", "", "", "", ""] },
    { name: "addr[37:36]", kind: "text", values: ["0", "", "", "", "", ""] },
    { name: "mine", kind: "bit", values: [0, 0, 0, 0, 0, 0], mark: [0] },
    { name: "AWREADY", kind: "bit", values: [0, 0, 0, 0, 0, 0] },
    { name: "BVALID", kind: "bit", values: [0, 0, 0, 0, 0, 0] },
  ],
  notes: [
    {
      cycle: 0,
      text: "The window says mesh 2 and the address field says mesh 0. Both decoders are absolute and neither is wrong; they simply describe different meshes, so no requester on mesh 2's converged path claims the beat.",
      tone: "bad",
    },
    {
      cycle: 5,
      text: "mine never rises, so nothing accepts and nothing answers. There is no DECERR and no timeout: it presents as a HANG, several layers away from the driver line that set the window. A driver that sets the window and forgets [37:36] sees exactly this.",
      tone: "bad",
    },
  ],
};

const winFixed = {
  rows: [
    {
      name: "AWADDR",
      kind: "bus",
      values: ["0x320_0000_0000", null, null, null, null, null],
    },
    { name: "window", kind: "text", values: ["mesh 2", "", "", "", "", ""] },
    { name: "addr[37:36]", kind: "text", values: ["2", "", "", "", "", ""] },
    { name: "mine", kind: "bit", values: [1, 1, 0, 0, 0, 0], mark: [0] },
    { name: "AWREADY", kind: "bit", values: [0, 1, 0, 0, 0, 0] },
    { name: "BVALID", kind: "bit", values: [0, 0, 0, 0, 1, 0] },
  ],
  notes: [
    {
      cycle: 0,
      text: "The mesh id appears twice on purpose and both copies agree. That is not redundancy to be optimised away — the window chooses where a transaction ENTERS and the address chooses where it LANDS.",
      tone: "good",
    },
    {
      cycle: 4,
      text: "Deliberately disagreeing is the remote case, and it is the same mechanism: mesh 2's window carrying [37:36] = 3 is decoded as remote by awaddr[37:36] != my_mesh and forwarded over the interlink. One field would make that unrepresentable.",
      tone: "good",
    },
  ],
};

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
};

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
};

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
};

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
};

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
};

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
};

const placeOrder = {
  cols: [
    { key: "n", label: "#", mono: true, align: "center" },
    { key: "r", label: "Resolve the mesh from" },
    { key: "w", label: "Note" },
  ],
  rows: [
    {
      n: "1",
      r: "the task's own <code>mesh</code>, if it names one",
      w: "stated wins",
    },
    {
      n: "2",
      r: "otherwise the mesh its <strong>reads</strong> live on",
      w: "a unit fetches through its own memory agent, so its operands fix where it runs",
      _tone: "good",
    },
    {
      n: "3",
      r: "otherwise the mesh its <strong>writes</strong> live on",
      w: "results need not be local",
    },
    { n: "4", r: "otherwise the machine's default", w: "" },
  ],
};

/* ---- how you actually generate one -------------------------------------- */
const procedure = {
  cols: [
    { key: "n", label: "#", mono: true, align: "center" },
    { key: "s", label: "Step" },
  ],
  rows: [
    {
      n: "1",
      s: "<strong>Write the picture, not the Verilog.</strong> A grid of 3-character tokens: <code>xxx</code> at the four corners, routers in the interior, gateways on the edge rings.",
    },
    {
      n: "2",
      s: "<strong>Check it fits the capacity formula</strong> — <code>NX*NY</code> router locals plus <code>2*(NX+NY)</code> edge slots, corners excluded — and fill edge slots before growing the grid, because an edge endpoint costs a link and a grid row costs <code>NX</code> routers.",
    },
    {
      n: "3",
      s: "<strong>Place at most four <code>mag</code> tiles, and place them on different rows.</strong> More than four is refused; on one row they share a funnel instead of splitting it.",
    },
    {
      n: "4",
      s: "<strong>Choose the memory boundary form</strong> — plain if the device image merges the masters, concentrated if the ship should.",
    },
    {
      n: "5",
      s: "<strong>Decide the interlink at elaboration.</strong> Off costs literally nothing, so the only reason to leave it on is that this image has a second mesh.",
    },
    {
      n: "6",
      s: "<strong>Generate. Never hand-edit the result</strong>, and never let a second file restate what the picture already said.",
    },
    {
      n: "7",
      s: "<strong>Assign the windows at 1 TiB spacing</strong>, leaving <code>0x000_…</code> free so control stays below 4 GiB, and read the offsets back out of the block design rather than trusting that they applied.",
    },
    {
      n: "8",
      s: "<strong>Elaborate and measure out of context</strong> before believing any of it.",
    },
  ],
};

const categories = {
  cols: [
    { key: "t", label: "Thing" },
    { key: "c", label: "Category" },
  ],
  rows: [
    {
      t: "the ship's boundary — clock, reset, AXI, and nothing else",
      c: "<strong>fixed protocol.</strong> It is what makes a ship droppable into a vendor block design without hand-wiring",
    },
    {
      t: "the 40-bit address and its four fields",
      c: "<strong>fixed protocol.</strong> A component that decodes differently is not on the framework",
    },
    {
      t: "the interlink's dimension-order rule and its credit classes",
      c: "<strong>fixed protocol.</strong> Changing it means redoing the deadlock argument",
    },
    {
      t: "which coordinate each endpoint occupies, and what shape the grid is",
      c: "<strong>customizable</strong> — that is what the picture is",
    },
    {
      t: "plain against concentrated memory boundary",
      c: "<strong>customizable</strong>, per device image",
    },
    {
      t: "the mesh id at runtime",
      c: "<strong>customizable</strong> — the elaboration parameter supplies only its reset value, so one generated module can occupy several positions",
    },
    {
      t: "gateways on edge rings, compute on router locals",
      c: "<strong>convention.</strong> The other way works and competes for the same router",
    },
    {
      t: "regenerating rather than hand-editing",
      c: "<strong>convention, forced in practice.</strong> An edit is lost on the next run and cannot be checked against anything",
    },
    {
      t: "what any endpoint in the picture actually computes",
      c: "<strong>yours</strong>",
    },
  ],
};

const notOwned = {
  cols: [
    { key: "n", label: "Not owned" },
    { key: "w", label: "Who owns it" },
  ],
  rows: [
    {
      n: "the flit, the router, the link handshake",
      w: "noc. Assembly instantiates them and defines none of them",
    },
    {
      n: "what a descriptor means, and what the memory ports do with one",
      w: "sysnode",
    },
    {
      n: "the AXI surface in front of the boundary — arbitration, width, clock crossing",
      w: "axi. A ship exposes AXI; it does not convert it",
    },
    {
      n: "which die region a ship lands on, and what a link may cross",
      w: "physical. Assembly decides that a link exists, not where it goes",
    },
    {
      n: "whether a mesh's DRAM has anything in it",
      w: "the runtime. The map gives 64 GB per mesh and 4 GB is real; staying under it is a compiler invariant nothing checks",
    },
    {
      n: "how a program is split across meshes",
      w: "the compiler. The mesh axis constrains placement; it does not schedule",
    },
  ],
};

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
    {
      n: "2",
      w: "placement chosen so traffic fits the topology",
      b: "removes the crossings rung 1 did not see",
    },
    {
      n: "3",
      w: "a deliberate parallel split, as across cards",
      b: "the split a model author already knows they want",
    },
    {
      n: "4",
      w: "placement aware that meshes differ",
      b: "uses a mesh that lacks a unit type for the work it <em>can</em> do",
    },
  ],
};
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
      A ship is one complete, self-contained accelerator: a mesh of routers, the
      endpoints on them, one edge complex, the memory boundary behind it, and
      the AXI surface in front. Everything inside is fixed at elaboration.
      Everything outside is AXI.
    </p>

    <Fig
      caption="One clock and one reset serve every interface, master and slave alike, so neither carries a direction prefix, and interface-inference attributes on the port list name them all so the tool ties them up on its own."
      zoom
    >
      <BlockDiagram
        :nodes="boundary.nodes"
        :edges="boundary.edges"
        :groups="boundary.groups"
      />
    </Fig>

    <Callout kind="rule" title="Why the boundary is exactly this">
      <p>
        The shape is not an accident of convenience — it is what makes a ship
        droppable into a vendor block design without hand-wiring. It is
        <strong>fixed protocol</strong>: a ship has a name, a fixed shape, and a
        boundary consisting of clock, reset and AXI interfaces, and nothing
        else.
      </p>
    </Callout>

    <h3 class="doc-h3">Two forms of memory boundary</h3>
    <SpecTable
      :cols="memBoundary.cols"
      :rows="memBoundary.rows"
      caption="Which one to use is a device-image decision, not a mesh decision."
    />

    <Callout
      kind="note"
      title="The concentrated form is framework RTL, not a device top"
    >
      <p>
        It is <code>src/kohakuaccel/sysnode/sysnode.v</code> — assembly rather
        than a top, and nothing in it is specific to any accelerator, so a
        second project instantiates it directly instead of reaching into the
        first one's directory. It was in a project's tree once and that is the
        shape to avoid: a reusable composition parked among device tops is
        reused by reaching across a boundary that then stops meaning anything.
      </p>
    </Callout>

    <h3 class="doc-h3">What a ship costs</h3>
    <Callout kind="rule">
      <p>
        <strong
          >The cost of a ship is the sum of its parts and the wiring between
          them, and the wiring is not free.</strong
        >
        A mesh's routers are its fixed overhead; the endpoints are what you
        actually wanted. The ratio between those two is the topology decision,
        and it is the reason the fabric pages spend so much effort on what a
        router costs per port.
      </p>
    </Callout>

    <SpecTable
      :cols="knobs.cols"
      :rows="knobs.rows"
      caption="The knobs that move ship cost, in the order they matter."
    />

    <Callout
      kind="trap"
      title="Port count is capped at four by the parameter list, not by the structure"
    >
      <p>
        The edge complex's port coordinates are
        <strong>four named parameter pairs</strong> —
        <code>MEM_X/MEM_Y</code> through <code>MEM_X3/MEM_Y3</code> — and the
        generate that instantiates ports selects between them by index. A fifth
        port is not a parameter change; it is an edit to the parameter list, the
        generate and the mesh checker, which refuses a map with more than four
        <code>mag</code> tiles for exactly this reason.
      </p>
      <p>
        A packed vector was rejected, and the reason is worth keeping rather
        than fixing away: <strong>one shift misaligns a whole port at the wrong
        node, and it elaborates cleanly</strong>. The form that does not scale
        was chosen over the form that fails silently. If you raise the cap,
        raise it as more named pairs.
      </p>
    </Callout>

    <h2 class="doc-h2">The mesh is a picture</h2>
    <p class="doc-p">
      A mesh is described as a grid of fixed-width tokens. The interior is the
      router grid; the first and last row are the north and south edge rings,
      the first and last column the west and east edge rings.
    </p>

    <Fig
      caption="Routers sit at (1..NX, 1..NY); edge endpoints sit just outside, at (x,0), (x,NY+1), (0,y) and (NX+1,y) — reachable precisely because of the coordinate clamp in the fabric's routing rule. Non-square grids fall out of this, which is why the router takes GRID_X_HI and GRID_Y_HI separately rather than one square bound."
      zoom
    >
      <svg
        viewBox="40 20 760 350"
        class="dgm"
        role="img"
        aria-label="A 4 by 4 token grid describing a 2 by 2 mesh"
      >
        <text x="60" y="46" class="dgm-sub">x</text>
        <text x="60" y="99" class="dgm-sub">y</text>

        <text
          v-for="c in axis"
          :key="`cx${c}`"
          :x="G.x0 + c * G.cw + G.cw / 2"
          y="46"
          text-anchor="middle"
          class="dgm-sub"
        >
          {{ c }}
        </text>
        <text
          v-for="r in axis"
          :key="`cy${r}`"
          x="94"
          :y="G.y0 + r * G.ch + G.ch / 2 + 4"
          text-anchor="end"
          class="dgm-sub"
        >
          {{ r }}
        </text>

        <rect
          x="217"
          y="127"
          width="226"
          height="146"
          rx="10"
          fill="none"
          stroke="var(--gem-main)"
          stroke-width="1"
          stroke-dasharray="4 4"
          opacity="0.55"
        />

        <g v-for="cell in cells" :key="cell.key">
          <rect
            :x="cell.x"
            :y="cell.y"
            width="98"
            height="58"
            rx="6"
            :class="cell.router && !cell.empty ? 'dgm-box-accent' : 'dgm-box'"
            :stroke-dasharray="cell.empty ? '4 4' : undefined"
            :opacity="cell.empty ? 0.45 : 1"
          />
          <text
            :x="cell.cx"
            :y="cell.cy - 2"
            text-anchor="middle"
            class="dgm-label"
            font-weight="600"
          >
            {{ cell.t }}
          </text>
          <text
            :x="cell.cx"
            :y="cell.cy + 12"
            text-anchor="middle"
            class="dgm-sub"
          >
            {{ cell.sub }}
          </text>
        </g>

        <rect
          x="600"
          y="92"
          width="18"
          height="13"
          rx="3"
          class="dgm-box-accent"
        />
        <text x="626" y="103" class="dgm-sub">router · a local port</text>
        <rect x="600" y="120" width="18" height="13" rx="3" class="dgm-box" />
        <text x="626" y="131" class="dgm-sub">edge-ring endpoint</text>
        <rect
          x="600"
          y="148"
          width="18"
          height="13"
          rx="3"
          class="dgm-box"
          stroke-dasharray="4 4"
          opacity="0.45"
        />
        <text x="626" y="159" class="dgm-sub">xxx · nothing here</text>
        <rect
          x="600"
          y="176"
          width="18"
          height="13"
          rx="3"
          fill="none"
          stroke="var(--gem-main)"
          stroke-width="1"
          stroke-dasharray="4 4"
          opacity="0.55"
        />
        <text x="626" y="187" class="dgm-sub">the router rectangle</text>

        <text x="600" y="222" class="dgm-sub">routers at (1..NX, 1..NY)</text>
        <text x="600" y="238" class="dgm-sub">
          edge endpoints just outside:
        </text>
        <text x="600" y="254" class="dgm-sub">
          (x,0) (x,NY+1) (0,y) (NX+1,y)
        </text>
      </svg>
    </Fig>

    <p class="doc-p">
      Three token kinds are structural rather than project-specific. The rest
      name endpoint types, and those are supplied by the accelerator being
      built.
    </p>
    <SpecTable :cols="tokenKinds.cols" :rows="tokenKinds.rows" />

    <Callout
      kind="trap"
      title="The endpoint vocabulary is hardcoded, so “supplied by the accelerator” is not yet true"
    >
      <p>
        <code>scripts/py/gen_mesh.py</code> carries
        <code>KNOWN = {xxx, nul, mag, vec, mat}</code> and
        <strong>raises on any other token</strong>. Two of those five name the
        reference project's compute units, so a second accelerator cannot
        describe its own mesh without editing the generator — and the generator
        emits those module names with those parameters, which is the wrong seam
        in the same place.
      </p>
      <p>
        The right shape is a registry: the generator knows how to place a thing
        that has a fabric port and a coordinate, and each accelerator supplies
        its own list. Until then, read the token table above as three structural
        kinds plus <em>this project's</em> two, and expect to edit Python before
        your first map elaborates.
      </p>
    </Callout>

    <p class="doc-p">
      The generator emits the router instances with their per-axis grid bounds,
      the link nets between them, the endpoint instances, the edge complex with
      its port coordinates, and the AXI boundary. Two properties of the
      generated file matter more than its contents.
    </p>

    <Callout kind="trap" title="It is generated, and says so">
      <p>
        A hand-edit is lost on the next regeneration, and a topology that exists
        only as edited Verilog cannot be checked against anything.
      </p>
    </Callout>

    <Callout
      kind="rule"
      title="One picture is the single source of the topology"
    >
      <p>
        The coordinates the software stack needs — where each endpoint is, which
        rows the memory ports are on, what the grid bounds are — are the same
        coordinates synthesis consumed. Any second description of the same
        topology is a place for the two to drift.
      </p>
      <p>
        <strong>Today there are two, and the drift is unguarded.</strong> The
        mesh picture is one; the machine description the software stack reads is
        another, generated separately. They already share values that must
        agree and nothing compares them —
        <code>TILES</code>, <code>GA</code> and <code>GB</code> are frozen at
        synthesis into the generated top and repeated in the driver's machine
        description, and a top that simply omits them elaborates
        <strong>the compute unit's own defaults instead</strong>, silently
        building a smaller machine than the software believes it has. Parts of
        that description are worse: its capacity fields come from a build log
        and its address fields from a block design nothing in the tree can read,
        so they can only be verified against hardware.
      </p>
    </Callout>

    <Callout
      kind="trap"
      title="A framework assembly carrying a project's prefix is a boundary that has stopped meaning anything"
    >
      <p>
        <code>ktpu_ship_2x2_6c2v_il</code> and its siblings under
        <code>src/kohakutpu/top/generated/</code> are
        <strong>framework assemblies with a project prefix</strong>. Nothing in
        the generator is specific to that accelerator except the two hardcoded
        tokens above, so the name records who ran the generator rather than what
        the module is.
      </p>
      <p>
        It matters because it is how the dependency the framework spent effort
        removing gets rebuilt by accident: a second accelerator that wants a
        2x2 ship reaches into the first one's directory for it, and from then on
        the two projects are coupled through a path rather than through an
        interface.
      </p>
    </Callout>

    <h3 class="doc-h3">Generation is elaboration, not runtime</h3>
    <p class="doc-p">
      A generated ship has no configuration registers for its shape. Every
      coordinate, grid bound and endpoint type is a parameter resolved at
      synthesis, so the cost of an unused feature is zero rather than small.
    </p>
    <Callout kind="note" title="The clearest example is the interlink">
      <p>
        With it disabled, every one of its nets is tied to a constant, every use
        folds, and the generated top does not expose the ports at all — the
        resulting build is identical to one made before the interlink existed.
        That property is maintained deliberately: every addition sits inside a
        generate or is gated by the parameter, because “costs nothing when off”
        is only true if someone keeps checking.
      </p>
    </Callout>

    <h3 class="doc-h3">Conventions</h3>
    <SpecTable
      :cols="conventions.cols"
      :rows="conventions.rows"
      caption="One forced, three free. The fourth is free and currently violated — the drift it names is the one in the rule above."
    />

    <h2 class="doc-h2">Several ships in one image</h2>
    <p class="doc-p">
      One mesh is bounded by how much fabric a die region can hold. Past that,
      the answer is not a bigger mesh — it is several, joined at their edge
      complexes. That decision was made on measurement rather than on argument:
      a single mesh spanning several die regions was implemented, and its worst
      path was almost entirely routing with no logic in it. The measured path,
      and the die it was measured on, are on
      <RouterLink to="/framework/physical" class="doc-link"
        >Floorplan and clocks</RouterLink
      >.
    </p>

    <Callout kind="rule" title="The interlink is a second routing layer">
      <p>
        It does not inherit the fabric's deadlock proof, so it gets its own by
        the same argument: dimension-order routing on
        <strong>mesh</strong> coordinates over a rectangular grid of meshes.
        Mesh id maps to <code>(x, y) = (id[0], id[1])</code>; X first, then Y.
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
        A forwarded packet <strong>always turns X into Y</strong>, never Y into
        X. So link0's forward class feeds link1, and link1's forward class is
        provably dead — traffic there is the turn the model forbids, which makes
        it a fault to report rather than a case to handle.
      </p>
      <p>
        The channel dependency graph is X to Y and nothing else. Acyclic, hence
        deadlock-free — and only while the mesh-of-meshes stays a grid.
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
        The grid model above admits four links; the image built has three, all
        of them between SLR-adjacent dies, with the fourth pair tied off.
        Closing the ring is what forces one long link: a 4-cycle cannot embed in
        a 4-node path without one edge spanning. That long one is what a plain
        shift-register pipe exists for, legal precisely because the link
        protocol is credit-based and has no handshake to preserve —
        <strong>add stages there and nowhere else</strong>, since a pipeline
        stage anywhere with a real ready signal reintroduces the combinational
        crossing the link asserts against.
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
        Every packet has at least one beat, including the two that carry no
        data. Their beat is zero and ignored. One wasted beat on two rare packet
        kinds removes a special case from the framing, both queues, the arbiter
        and both benches.
      </p>
    </Callout>

    <h3 class="doc-h3">What crosses, and what “arrived” means</h3>
    <SpecTable :cols="crosses.cols" :rows="crosses.rows" />

    <Callout kind="rule" title="Completion means landed">
      <p>
        An inbound doorbell waits for every write ahead of it to have its write
        response before it counts, so a consumer released by a doorbell is
        released by data that is in memory rather than in a queue. Without that
        rule, posted writes and a doorbell are a race with no observable
        ordering.
      </p>
    </Callout>

    <WaveTrace
      :rows="doorbellBroken.rows"
      :notes="doorbellBroken.notes"
      variant="broken"
      label="doorbell counts on send"
    />
    <WaveTrace
      :rows="doorbellFixed.rows"
      :notes="doorbellFixed.notes"
      variant="fixed"
      label="doorbell waits for the write responses"
    />
    <p class="kt-text-caption text-warm-500 dark:text-warm-400 -mt-2 mb-6">
      Schematic. The ordering is the rule; the cycle numbers are illustration,
      not a measurement.
    </p>

    <Callout
      kind="trap"
      title="The source coordinate is preserved across a crossing"
    >
      <p>
        Rewriting it to the receiving edge's own coordinate would make two
        remote bursts arriving at one endpoint indistinguishable — and telling
        senders apart is how a receiving unit avoids merging two senders' data
        into one region. The cost is that “answer the sender” no longer
        resolves, so a remote transfer must name its acknowledgement destination
        explicitly, and a fault register reports one that does not.
      </p>
    </Callout>

    <h3 class="doc-h3">What it costs</h3>
    <Callout kind="rule">
      <p>
        <strong
          >Several ships in one image cost the interlink once per ship</strong
        >, plus the boundary crossing registers. Against a mesh, that is small.
        Against the alternative — one mesh stretched across the same area — it
        is the difference between a design that closes and one that does not.
        Disabled, it costs nothing at all.
      </p>
    </Callout>
    <SpecTable
      :cols="crossingCost.cols"
      :rows="crossingCost.rows"
      caption="CLB LUT sites on xcvu13p-fhgb2104-2L-e with Vivado 2024.2, against a 23,040-wire budget per boundary; the wire counts are post-placement and the LUT figures are the per-instance breakdown of one out-of-context synthesis. The interlink and the station bus are what cross a boundary today. slr_cross was the SmartConnect tree's register slice and is SUPERSEDED — it is shown only to price what the credited link replaced, a factor of 6.0 on the connective tissue between dies alone, and it is not in the design."
    />

    <h2 class="doc-h2">The address map</h2>
    <p class="doc-p">
      The map is a ship-level fact because it is the only place the whole
      machine is visible at once. The machine is a
      <strong>40-bit</strong> machine: every address a unit issues, every
      address in an instruction, and every address a decoder tests is 40 bits.
    </p>

    <BitField
      :fields="insideAddr"
      caption="Inside — what a unit issues and every decoder tests"
    />

    <p class="doc-p">
      A host master does not drive those 40 bits directly. It drives a
      <strong>43-bit</strong> AXI address,
      <code>outside = (mesh + 1) &lt;&lt; 40 | inside</code>. The address space
      is 40 bits; the transport is 43. A mesh cannot tell whether a request came
      from its own mover or from the host DMA engine.
    </p>

    <BitField
      :fields="outsideAddr"
      caption="Outside — what a host master drives"
    />

    <SpecTable
      :cols="addrFields.cols"
      :rows="addrFields.rows"
      caption="Inside a ship there are 40 bits and four fields; outside there are 43 and five. The window is the only one a mesh never sees."
    />

    <SpecTable
      :cols="windows.cols"
      :rows="windows.rows"
      caption="The four windows of the four-mesh v5 device image on xcvu13p-fhgb2104-2L-e. 0x000_… is left free so the control space stays below 4 GB and the host bridge's AXI-Lite can reach it. Reachable by jtag_axi_0/Data and xdma_0/M_AXI only; explicitly excluded from xdma_0/M_AXI_LITE."
    />

    <Callout kind="note" title="Why a prefix exists at all">
      <p>
        <code>S_AXI_MEM</code> declares 40-bit addressing, so its
        <code>reg0</code> segment is a fixed 1 TB and Vivado will only place it
        on a 1 TB boundary. Four of those cannot tile inside one 1 TB space.
        Assigning them at 64 GB spacing does not fail loudly — Vivado discards
        the offsets and puts <strong>all four meshes at offset 0</strong>, which
        is what <code>BD 41-1377</code> reports.
      </p>
    </Callout>

    <SpecTable
      :cols="worked.cols"
      :rows="worked.rows"
      caption="The mesh id appears TWICE — once in the window, once in [37:36]. That is not redundancy to be optimised away."
    />

    <Callout
      kind="trap"
      title="If the window and the address disagree, nothing faults"
    >
      <p>
        <code>mine</code> simply stays low in <code>mag_stage_port.v</code> and
        no requester claims the beat, so the access is never answered —
        <strong>it presents as a hang, not as an error</strong>. A driver that
        sets the window but forgets <code>[37:36]</code> sees exactly this.
      </p>
      <p>
        The same independence is what makes remote entry work: the window
        chooses where a transaction <em>enters</em> and the address chooses
        where it <em>lands</em>. A write into mesh 2's window carrying
        <code>[37:36] = 3</code> is decoded by mesh 2 as remote (<code
          >awaddr[37:36] != my_mesh</code
        >) and forwarded over the interlink.
      </p>
    </Callout>

    <WaveTrace
      :rows="winBroken.rows"
      :notes="winBroken.notes"
      variant="broken"
      label="window says mesh 2, address says mesh 0 — nothing claims the beat"
    />
    <WaveTrace
      :rows="winFixed.rows"
      :notes="winFixed.notes"
      variant="fixed"
      label="both copies agree, and disagreeing on purpose is the remote case"
    />
    <p class="kt-text-caption text-warm-500 dark:text-warm-400 -mt-2 mb-6">
      Schematic. The decode and the absence of any response are the rule; the
      cycle numbers are illustration, not a measurement.
    </p>

    <Callout kind="open" title="NOT YET TRACED">
      <p>
        That the <code>S_AXI_MEM</code> path reaches the interlink forwarder the
        way the mover's path does. The decoders are absolute, so the address is
        <em>classified</em> remote; whether the forwarding is wired for host
        traffic is unverified. Do not plan around it until someone follows the
        path.
      </p>
    </Callout>

    <Callout kind="trap" title="Capacity: the map is bigger than the memory">
      <p>
        The map gives each mesh 64 GB of local space. Each mesh has
        <strong>4 GB</strong> of DDR4 behind it, so addresses from 4 GB to 64 GB
        within a mesh decode correctly, reach <code>M_AXI_DRAM</code>, and hit
        nothing. Staying under 4 GB per mesh is a compiler invariant, not
        something the hardware checks — unlike an unimplemented aperture, which
        does fault. <em>(Four-mesh v5 image on xcvu13p-fhgb2104-2L-e.)</em>
      </p>
    </Callout>

    <h3 class="doc-h3">Control, below 4 GiB</h3>
    <p class="doc-p">
      Control decodes positionally rather than from a table of hand-assigned
      bases: the station comes off one field and the endpoint off another, and
      every endpoint gets one 64 KiB window.
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
        The mesh id is <strong>writable at runtime</strong>, with the
        elaboration parameter supplying only its reset value. So several
        instances of the same generated module can occupy different positions in
        the grid, and the instances differ by configuration rather than by being
        different modules.
      </p>
      <p>
        A flit likewise carries a spare header bit meaning “this is for another
        mesh”, which is zero on every flit a single-mesh build ever produces.
        That is what lets one compiler target both: the single-mesh case is the
        multi-mesh case with a field left at zero, rather than a different
        encoding.
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
      A second mesh is not a wider first one.
      <strong>It is a second memory</strong>, and every consequence follows from
      that. A mesh's units fetch operands through <em>their</em> memory agent,
      which serves <em>that</em> mesh's memory, so an address is not a number —
      it is a pair, <code>(mesh, offset)</code>, and the same offset on two
      meshes names two different bytes.
    </p>
    <SpecTable
      :cols="meshConsequences.cols"
      :rows="meshConsequences.rows"
      caption="A compiler that misses any of these generates programs that are wrong rather than slow."
    />

    <h3 class="doc-h3">The mesh axis constrains; it does not schedule</h3>
    <p class="doc-p">
      Placement across the units <em>within</em> a mesh is a scheduling
      decision: any unit of the right type can run the task, and choosing badly
      costs time. Which <strong>mesh</strong> a task runs on is not that. A task
      cannot run where its operands are not, whatever the load says.
    </p>
    <SpecTable
      :cols="placeOrder.cols"
      :rows="placeOrder.rows"
      caption="Place resolves the mesh before it considers a coordinate (compiler/kohakuaccel/passes/place.py)."
    />

    <Callout kind="rule" title="Reads decide before writes">
      <p>
        The two are not symmetric:
        <strong>operands must be local, results need not be.</strong> A unit
        writing to another mesh is an ordinary remote transfer; a unit reading
        operands from two meshes at once has no mesh to run on, and that is
        refused rather than guessed.
      </p>
    </Callout>

    <Callout
      kind="trap"
      title="Load must be accumulated per (mesh, coordinate)"
    >
      <p>
        Not per coordinate. The same coordinate on two meshes is two different
        units, and one counter for both balances a task against work on a mesh
        it cannot see.
      </p>
    </Callout>

    <h3 class="doc-h3">The spectrum</h3>
    <SpecTable
      :cols="rungs.cols"
      :rows="rungs.rows"
      caption="Not four implementations — one implementation with progressively more of the decisions taken from the user, which is why rung 4's scheduler improvements land in rung 1 for free."
    />
    <p class="doc-p">
      The build order that follows is worth stating, because the tempting one is
      wrong.
      <strong
        >Ship rung 1, plus the manual knob at 2, 3 and 4. Then write real
        kernels at 2, 3 and 4 and measure them against rung 1.</strong
      >
      Those hand-written kernels are the specification for the automatic
      version.
    </p>

    <Callout kind="open" title="No cost model for a collective exists">
      <p>
        There is no measured cross-mesh transfer rate this page can quote. The
        one that used to appear here was taken before the memory mover was
        rebuilt and describes an engine that no longer exists; it is
        <strong>withdrawn</strong> rather than carried forward, and nothing has
        replaced it.
      </p>
      <p>
        What survives it is structural and does not need a number: a transfer's
        cost is a property of <em>the engine driving the link</em>, not of the
        link, so a cost model keyed on topology and raw bandwidth prices the
        wrong thing. Any model built here has to be fitted against a
        re-measurement on the current mover.
      </p>
    </Callout>

    <Callout
      kind="open"
      title="Nothing checks that a machine description matches the device"
    >
      <p>
        A mesh map generates hardware and a machine description tells software
        the same thing twice, with nothing comparing them. Capacity is not
        checked either: nothing refuses an allocation that does not fit a mesh's
        memory, and it becomes a runtime failure with no diagnosis.
      </p>
    </Callout>

    <h2 class="doc-h2">Generating one</h2>
    <SpecTable :cols="procedure.cols" :rows="procedure.rows" />

    <h2 class="doc-h2">Fixed protocol, addon, convention, or yours</h2>
    <SpecTable :cols="categories.cols" :rows="categories.rows" />

    <h2 class="doc-h2">What assembly does not own</h2>
    <SpecTable :cols="notOwned.cols" :rows="notOwned.rows" />
    <p class="doc-p">
      The first row is worth stating twice. Assembly knows how to instantiate a
      router and wire a link; it knows nothing about what travels on one.
      <strong
        >That is why a mesh picture is a picture and not a program</strong
      >
      — everything it can express is placement, and everything it cannot is
      somebody else's page.
    </p>
  </DocPage>
</template>
