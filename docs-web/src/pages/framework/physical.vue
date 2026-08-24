<script setup>
/**
 * /framework/physical — drawn from, in full:
 *   docs/arch/physical/README.md, floorplan.md, clocking.md, device-facts.md,
 *   where-the-boundary-falls.md, measurement.md
 *   docs/arch/ship/v5-interconnect-groundtruth.md, docs/projects/kohakutpu/ship.md
 *
 * Every figure on this page is the KohakuTPU reference instance on ONE part,
 * xcvu13p-fhgb2104-2L-e, and names its build where builds differ.
 */

/* ---- the die map ------------------------------------------------------- */
const BAND_H = 96;
const GAP = 34;
const Y0 = 22;
const LANE_IL = 261; // interlink lane, through the mesh boxes
const LANE_AX = 544; // station-bus lane, one station per die

const bands = [
  {
    slr: "SLR3",
    role: "end die · one face",
    mesh: "mesh_2",
    shape: "2x2 · 7 cluster + 2 vector",
    ddr: "ddr4_0",
    stn: "station 3",
    stnSub: "end of the line",
  },
  {
    slr: "SLR2",
    role: "middle · two faces",
    mesh: "mesh_3",
    shape: "2x2 · 6 cluster + 2 vector",
    ddr: "ddr4_1",
    stn: "station 2",
    stnSub: "two neighbours",
  },
  {
    slr: "SLR1",
    role: "master · config",
    mesh: "mesh_1",
    shape: "1x2 · 4 cluster + 0 vector",
    ddr: "ddr4_3",
    stn: "station 1",
    stnSub: "all three managers",
    extra: "XDMA",
    extraSub: "JTAG-AXI · bank 65",
  },
  {
    slr: "SLR0",
    role: "end die · one face",
    mesh: "mesh_0",
    shape: "2x2 · 7 cluster + 2 vector",
    ddr: "ddr4_2",
    stn: "station 0",
    stnSub: "end of the line",
  },
].map((b, i) => ({ ...b, y: Y0 + i * (BAND_H + GAP) }));

const seams = [0, 1, 2].map((i) => {
  const y = Y0 + i * (BAND_H + GAP);
  return {
    i,
    mid: y + BAND_H + GAP / 2,
    top: y + BAND_H - 20,
    bot: y + BAND_H + GAP + 20,
  };
});

/* ---- placement --------------------------------------------------------- */
const populations = {
  cols: [
    { key: "m", label: "mesh", mono: true },
    { key: "s", label: "SLR", mono: true },
    { key: "d", label: "DRAM", mono: true },
    { key: "a", label: "as built, four-mesh v5" },
    { key: "p", label: "the four-mesh plan" },
    { key: "w", label: "why this die" },
  ],
  rows: [
    {
      m: "mesh_0",
      s: "SLR0",
      d: "ddr4_2",
      a: "2x2 · 7 + 2",
      p: "6 + 4",
      w: "empty otherwise; only <code>ddr4_2</code> is on it",
    },
    {
      m: "mesh_1",
      s: "SLR1",
      d: "ddr4_3",
      a: "1x2 · 4 + 0",
      p: "4 + 4",
      w: "XDMA is here, so it is the most crowded die",
    },
    {
      m: "mesh_2",
      s: "SLR3",
      d: "ddr4_0",
      a: "2x2 · 7 + 2",
      p: "4 + 4",
      w: "the grid diagonal of SLR1",
    },
    {
      m: "mesh_3",
      s: "SLR2",
      d: "ddr4_1",
      a: "2x2 · 6 + 2",
      p: "6 + 4",
      w: "already held a 6+4",
    },
  ],
};

/* §2.8 of docs/projects/kohakuaxi/station-bus.md. The SmartConnect column is
 * the superseded baseline and is NOT uniformly v5 — see `replacedProvenance`. */
const replaced = {
  cols: [
    { key: "d", label: "" },
    { key: "s", label: "SmartConnect tree", mono: true, align: "right" },
    { key: "l", label: "station line", mono: true, align: "right" },
    { key: "r", label: "ratio", mono: true, align: "right" },
  ],
  rows: [
    { d: "SLR0", s: "8,516", l: "3,718", r: "2.29x" },
    {
      d: "SLR1",
      s: "41,788",
      l: "8,756",
      r: "<strong>4.77x</strong>",
      _tone: "good",
    },
    { d: "SLR2", s: "13,266", l: "4,239", r: "3.13x" },
    { d: "SLR3", s: "8,516", l: "3,720", r: "2.29x" },
    { d: "between dies", s: "9,795", l: "1,641", r: "5.97x" },
    {
      d: "<strong>total</strong>",
      s: "<strong>81,881</strong>",
      l: "<strong>22,106</strong>",
      r: "<strong>3.70x</strong>",
    },
  ],
};

const replacedProvenance = {
  cols: [
    { key: "r", label: "row" },
    { key: "s", label: "where the SmartConnect figure comes from", mono: true },
  ],
  rows: [
    { r: "SLR1", s: "multimesh_v5_root_smc_0" },
    { r: "SLR2", s: "multimesh_v5_leaf_smc_2_0" },
    {
      r: "SLR0, SLR3",
      s: "multimesh_<strong>v4</strong>_leaf_smc_0_0 — no v5 run exists for either",
      _tone: "warn",
    },
    {
      r: "between dies",
      s: "3 x multimesh_<strong>v2</strong>_slr_cross_2_0",
      _tone: "warn",
    },
  ],
};

const whyArchitecture = {
  cols: [
    { key: "r", label: "Reason" },
    { key: "d", label: "What follows from it" },
  ],
  rows: [
    {
      r: "<strong>Some structures cannot be split at all.</strong>",
      d: "Carry chains, arithmetic-block cascades and memory-block cascades do not propagate across a die boundary; the only connection between regions is the crossing register. So a datapath built on a cascade is <em>by construction</em> a unit of placement, and how you decompose your compute unit is a floorplan decision made at design time. This is a correctness rule, not an optimisation.",
      _tone: "bad",
    },
    {
      r: "<strong>The largest fixed blocks cannot move.</strong>",
      d: "A memory interface is anchored to a region by its pinout — its I/O banks and its clocking must all be in one region — and a host bridge is anchored by its transceivers. Both consume a region's budget, and they consume it unevenly: the region holding the host bridge gives up real compute to do so. Identical silicon does not mean interchangeable regions.",
    },
    {
      r: "<strong>The crossing is registered, so it costs latency.</strong>",
      d: "Any protocol that crosses a boundary must tolerate that latency, and whether it does is decided when the protocol is designed, not when it is placed. The interlink is credit-based precisely because a ready signal travelling back across a boundary is the combinational crossing all of this exists to avoid.",
    },
    {
      r: "<strong>How much fits is a per-region question.</strong>",
      d: "The resource that runs out first sets how many compute units a machine can have, and it differs by region once fixed loads are placed. A device-wide total is the wrong number to plan against.",
    },
  ],
};

const classes = {
  cols: [
    { key: "n", label: "", mono: true, align: "center" },
    { key: "c", label: "Traffic class, descending bandwidth" },
    { key: "b", label: "Can a boundary land here?" },
  ],
  rows: [
    {
      n: "1",
      c: "inside a compute unit",
      b: "<strong>No.</strong> A cascade cannot cross, and that is a correctness rule rather than advice.",
      _tone: "bad",
    },
    {
      n: "2",
      c: "compute unit to memory agent",
      b: "<strong>No.</strong> A memory channel cannot cross a boundary — which is exactly what makes a region-resident mesh able to reach its own memory without crossing.",
      _tone: "bad",
    },
    {
      n: "3",
      c: "between compute units",
      b: "<strong>Only as an explicit registered link.</strong> A fabric stretched across a boundary was implemented and rejected on measurement.",
      _tone: "warn",
    },
    {
      n: "4",
      c: "control traffic",
      b: "Yes — and it does. One credited link per boundary, carrying two streams (REQ out, RSP back) and measured at under 3% of the wire budget.",
      _tone: "good",
    },
    {
      n: "5",
      c: "host to control plane",
      b: "Yes. The cheapest class there is.",
      _tone: "good",
    },
  ],
};

/* ---- device facts ------------------------------------------------------ */
const deviceFacts = {
  cols: [
    { key: "f", label: "What has to be established" },
    { key: "v", label: "For xcvu13p-fhgb2104-2L-e" },
  ],
  rows: [
    { f: "die regions", v: "four, identical in hard-block census" },
    { f: "boundaries between them", v: "three" },
    {
      f: "asymmetries",
      v: "the two end regions have one crossing face rather than two; one region carries configuration and device identity",
    },
    {
      f: "crossing registers per boundary",
      v: "tens of thousands, <strong>shared between both directions</strong> — it is a total, not a per-direction budget",
    },
    {
      f: "crossing latency",
      v: "one cycle, transmit register to receive register, plus whatever pipelining the frequency demands",
    },
    {
      f: "memory channels",
      v: "one wired to each region — which is what makes a region-resident mesh able to reach its own memory without crossing",
    },
    {
      f: "host bridge",
      v: "in one region only, fixed by transceiver placement",
    },
  ],
};

const census = {
  cols: [
    { key: "r", label: "" },
    { key: "s", label: "per SLR", mono: true, align: "right" },
    { key: "d", label: "device", mono: true, align: "right" },
  ],
  rows: [
    { r: "CLB LUT", s: "432,000", d: "1,728,000" },
    { r: "CLB FF", s: "864,000", d: "3,456,000" },
    { r: "BRAM36", s: "672", d: "2,688" },
    { r: "URAM288", s: "320", d: "1,280" },
    { r: "DSP48E2", s: "3,072", d: "12,288" },
    { r: "clock regions", s: "32 (8 wide x 4 tall)", d: "128" },
    { r: "Laguna sites", s: "3,840 end dies, 7,680 middle", d: "23,040" },
  ],
};

const channels = {
  cols: [
    { key: "c", label: "channel", mono: true },
    { key: "s", label: "SLR", mono: true },
    { key: "n", label: "notes" },
  ],
  rows: [
    { c: "ddr4_c0", s: "SLR3", n: "" },
    { c: "ddr4_c1", s: "SLR2", n: "the single-mesh design on the card today" },
    { c: "ddr4_c2", s: "SLR0", n: "" },
    {
      c: "ddr4_c3",
      s: "SLR1",
      n: "<strong>XDMA/PCIe is also here</strong>",
      _tone: "warn",
    },
  ],
};

const crossing = {
  cols: [
    { key: "k", label: "" },
    { key: "v", label: "value" },
  ],
  rows: [
    { k: "boundaries", v: "3" },
    {
      k: "SLLs per boundary",
      v: "23,040, <strong>shared between both directions</strong>",
    },
    {
      k: "measured crossing delay",
      v: "0.755 ns &nbsp;=&nbsp; 0.096 clock-to-Q &nbsp;+&nbsp; 0.659 SLL route, speed grade -2L",
    },
    { k: "latency", v: "1 cycle, transmit register to receive register" },
    { k: "at 300 MHz", v: "the crossing alone is about 23% of the period" },
  ],
};

const sll = {
  cols: [
    { key: "b", label: "boundary", mono: true },
    { key: "s", label: "SLLs", mono: true, align: "right" },
    { key: "p", label: "% of 23,040", mono: true, align: "right" },
    { key: "o", label: "outward", mono: true, align: "right" },
    { key: "i", label: "inward", mono: true, align: "right" },
  ],
  rows: [
    { b: "SLR1 ↔ SLR0", s: "639", p: "2.77%", o: "365", i: "274" },
    { b: "SLR2 ↔ SLR1", s: "634", p: "2.75%", o: "363", i: "271" },
    { b: "SLR3 ↔ SLR2", s: "644", p: "2.80%", o: "364", i: "280" },
    { b: "interlink, for scale", s: "772", p: "3.35%", o: "—", i: "—" },
  ],
};

const occupancy = {
  cols: [
    { key: "m", label: "measurement" },
    { key: "v", label: "value", mono: true, align: "right" },
    { key: "n", label: "note" },
  ],
  rows: [
    {
      m: "SLR0, CLB occupancy",
      v: "95.49%",
      n: "the <strong>binding</strong> die",
      _tone: "bad",
    },
    {
      m: "SLR1, CLB occupancy",
      v: "88.61%",
      n: "the <strong>emptiest</strong> die",
      _tone: "good",
    },
    { m: "SLR1, DSP", v: "45%", n: "against 79.6% elsewhere", _tone: "good" },
    {
      m: "every die, CLB occupancy",
      v: "88–95%",
      n: "while using only <strong>61–69%</strong> of its LUTs — the design is packing-bound",
    },
    {
      m: "URAM288, device",
      v: "120 of 1,280 — 9.38%",
      n: "no hard block is close to binding",
    },
    {
      m: "XDMA alone",
      v: "76,319 LUT · 72,059 FF",
      n: "17.7% of one SLR, from the single-mesh design on the card",
    },
  ],
};

/* ---- clocking ---------------------------------------------------------- */
const domains = {
  cols: [
    { key: "d", label: "Domain" },
    { key: "c", label: "Carries" },
    { key: "n", label: "Notes" },
  ],
  rows: [
    {
      d: "<strong>mesh</strong>",
      c: "fabric, edge complex, compute units",
      n: "one domain for the whole mesh, by construction",
    },
    {
      d: "<strong>control</strong>",
      c: "host bridge, debug bridge, control interconnect",
      n: "<strong>fixed</strong>",
    },
    {
      d: "<strong>memory</strong>",
      c: "one per memory controller",
      n: "each on its own controller's user clock",
    },
    {
      d: "<strong>host</strong>",
      c: "the DMA engine's own interface",
      n: "vendor IP",
    },
  ],
};

const genDiagram = {
  nodes: [
    { id: "ref", x: 0, y: 4, w: 13, label: "reference clk" },
    {
      id: "fixed",
      x: 17,
      y: 0,
      w: 16,
      h: 3.6,
      label: "fixed generator",
      sub: "the control domain, and the reconfiguration port below",
    },
    {
      id: "var",
      x: 17,
      y: 8,
      w: 16,
      h: 3.6,
      label: "variable generator",
      sub: "dynamic reconfiguration",
      accent: true,
    },
    {
      id: "loads",
      x: 38,
      y: 0,
      w: 19,
      h: 3.6,
      label: "debug bridge · interconnect",
      sub: "resets",
    },
    {
      id: "meshes",
      x: 38,
      y: 8,
      w: 19,
      h: 3.6,
      label: "every mesh",
      sub: "one knob, not one per mesh",
      accent: true,
    },
  ],
  edges: [
    { from: "ref:r", to: "fixed:l", dir: "h" },
    { from: "ref:r", to: "var:l", dir: "h" },
    { from: "fixed:r", to: "loads:l", dir: "h" },
    { from: "var:r", to: "meshes:l", dir: "h", accent: true },
    {
      from: "fixed:b",
      to: "var:t",
      dir: "v",
      dash: true,
      label: "a register write",
    },
  ],
};

const retuneRules = {
  cols: [
    { key: "r", label: "Rule" },
    { key: "w", label: "Why" },
  ],
  rows: [
    {
      r: "<strong>One knob, not one per mesh.</strong>",
      w: "Anything spanning meshes — the interlink — shares a clock, so they retune together.",
    },
    {
      r: "<strong>A retune resets every mesh.</strong>",
      w: "On-chip state is lost; memory survives, since it is on its own controller and clock.",
    },
    {
      r: "<strong>Quiesce first.</strong>",
      w: "The interlink is credit-based, and retuning with packets in flight leaves credits inconsistent on both sides.",
    },
  ],
};

const seq = {
  states: [
    { id: "run", x: 0, y: 0, label: "run" },
    { id: "quiesce", x: 5, y: 0, label: "quiesce" },
    { id: "retune", x: 10, y: 0, label: "retune" },
    { id: "lock", x: 15, y: 0, label: "lock" },
    { id: "reset", x: 20, y: 0, label: "reset" },
    { id: "init", x: 25, y: 0, label: "init" },
    { id: "upload", x: 30, y: 0, label: "upload" },
  ],
  edges: [
    { from: "run", to: "quiesce" },
    { from: "quiesce", to: "retune" },
    { from: "retune", to: "lock" },
    { from: "lock", to: "reset" },
    { from: "reset", to: "init" },
    { from: "init", to: "upload" },
    { from: "upload", to: "run", curve: -100 },
  ],
};

const seqSteps = [
  {
    active: "run",
    title: "Running",
    note: "The mesh runs on the variable generator. The fixed generator carries the control plane — including the reconfiguration port of the generator below it, because the control plane must never stand on the clock it is changing.",
  },
  {
    active: "quiesce",
    title: "Quiesce",
    note: "The interlink is credit-based, and retuning with packets in flight leaves credits inconsistent on both sides.",
  },
  {
    active: "retune",
    title: "Retune",
    note: "Changing the frequency is a register write, over a narrow bus clocked from the fixed domain.",
  },
  { active: "lock", title: "Wait for lock", note: "" },
  {
    active: "reset",
    title: "Reset",
    note: "A retune resets every mesh. On-chip state is lost; memory survives, since it is on its own controller and clock.",
  },
  { active: "init", title: "Re-initialise", note: "" },
  {
    active: "upload",
    title: "Re-upload anything that lived on chip",
    note: "",
  },
];

const asBuiltClocks = {
  cols: [
    { key: "c", label: "clock", mono: true },
    { key: "r", label: "request", mono: true, align: "right" },
    { key: "f", label: "Fmax", mono: true, align: "right" },
    { key: "s", label: "slack", mono: true, align: "right" },
  ],
  rows: [
    {
      c: "bus_clk1",
      r: "200 MHz",
      f: "357.9 MHz",
      s: "+2.206 ns",
      _tone: "warn",
    },
    { c: "bus_clk2", r: "200 MHz", f: "392.6 MHz", s: "+2.453 ns" },
    { c: "bus_clk0, bus_clk3", r: "200 MHz", f: "428.1 MHz", s: "+2.664 ns" },
    { c: "clk_ctrl", r: "100 MHz", f: "395.9 MHz", s: "+7.474 ns" },
    { c: "clk_xdma", r: "250 MHz", f: "396.8 MHz", s: "+1.480 ns" },
    {
      c: "clk_s0, clk_s2, clk_s3",
      r: "178–237 MHz",
      f: "502.5 MHz",
      s: "+2.2 to +3.6 ns",
    },
    { c: "clk_s1, clk_ddr", r: "300 MHz", f: "520.3 MHz", s: "+1.411 ns" },
  ],
};
</script>

<template>
  <DocPage
    title="Floorplan and clocks"
    summary="The die is not flat, its resources are not uniformly reachable, and several of its constraints are correctness rules rather than performance advice. This is the part of the architecture that lives in geometry."
    domain="framework"
    status="measured"
    source="docs/arch/physical/ · constraints and block design · every figure is one accelerator on xcvu13p-fhgb2104-2L-e"
  >
    <h2 class="doc-h2">Why placement is architecture and not a build step</h2>
    <p class="doc-p">
      A framework that treated placement as something the tool does afterwards
      would be wrong four times over.
    </p>
    <SpecTable :cols="whyArchitecture.cols" :rows="whyArchitecture.rows" />

    <Callout kind="rule" title="The consequence that matters most">
      <p>
        Put together:
        <strong
          >the number of compute units in a machine is set by geometry, not by a
          throughput knee.</strong
        >
        That is why unit count appears in the architecture rather than in a
        tuning guide.
      </p>
    </Callout>

    <h2 class="doc-h2">The die</h2>
    <p class="doc-p">
      Four die regions in a line, one memory channel wired to each, one mesh
      resident in each. SLR1 is the master region on this part — the
      configuration bank, the JTAG/BSCAN logic and the tandem-capable PCIe
      blocks are all there, so the host bridge and the debug bridge are anchored
      to it and cannot move.
    </p>

    <Fig
      caption="ONE accelerator on ONE part, xcvu13p-fhgb2104-2L-e. Two structures cross every boundary and neither is an AXI port. The accent lane on the left is the interlink between meshes, one registered credited link per boundary. The plain lane on the right is the station bus: one station per die on a line, no root, every station the same module with exactly two neighbours, and all three managers on station 1 because that is where the host bridge is anchored."
      zoom
    >
      <svg
        viewBox="0 0 780 524"
        class="dgm"
        role="img"
        aria-label="Four SLRs in a line with one mesh, one DDR4 channel and one interconnect node each"
      >
        <text :x="LANE_IL" y="14" text-anchor="middle" class="dgm-sub">
          interlink
        </text>
        <text :x="LANE_AX" y="14" text-anchor="middle" class="dgm-sub">
          station bus
        </text>

        <g v-for="b in bands" :key="b.slr">
          <rect
            x="30"
            :y="b.y"
            width="720"
            :height="BAND_H"
            rx="8"
            class="dgm-box"
            opacity="0.75"
          />

          <text x="44" :y="b.y + 44" class="dgm-label" font-weight="600">
            {{ b.slr }}
          </text>
          <text x="44" :y="b.y + 58" class="dgm-sub">{{ b.role }}</text>

          <rect
            x="172"
            :y="b.y + 20"
            width="178"
            height="56"
            rx="6"
            class="dgm-box-accent"
          />
          <text
            x="261"
            :y="b.y + 44"
            text-anchor="middle"
            class="dgm-label"
            font-weight="600"
          >
            {{ b.mesh }}
          </text>
          <text x="261" :y="b.y + 58" text-anchor="middle" class="dgm-sub">
            {{ b.shape }}
          </text>

          <rect
            x="364"
            :y="b.y + 20"
            width="100"
            height="56"
            rx="6"
            class="dgm-box"
          />
          <text
            x="414"
            :y="b.y + 44"
            text-anchor="middle"
            class="dgm-label"
            font-weight="600"
          >
            {{ b.ddr }}
          </text>
          <text x="414" :y="b.y + 58" text-anchor="middle" class="dgm-sub">
            4 GB
          </text>

          <rect
            x="478"
            :y="b.y + 20"
            width="132"
            height="56"
            rx="6"
            class="dgm-box"
          />
          <text
            x="544"
            :y="b.y + 44"
            text-anchor="middle"
            class="dgm-label"
            font-weight="600"
          >
            {{ b.stn }}
          </text>
          <text x="544" :y="b.y + 58" text-anchor="middle" class="dgm-sub">
            {{ b.stnSub }}
          </text>

          <template v-if="b.extra">
            <rect
              x="624"
              :y="b.y + 20"
              width="112"
              height="56"
              rx="6"
              class="dgm-box"
            />
            <text
              x="680"
              :y="b.y + 44"
              text-anchor="middle"
              class="dgm-label"
              font-weight="600"
            >
              {{ b.extra }}
            </text>
            <text x="680" :y="b.y + 58" text-anchor="middle" class="dgm-sub">
              {{ b.extraSub }}
            </text>
          </template>
        </g>

        <g v-for="s in seams" :key="`seam${s.i}`">
          <path
            :d="`M30,${s.mid} H750`"
            class="dgm-edge"
            stroke-dasharray="5 5"
            opacity="0.5"
          />
          <text x="36" :y="s.mid - 7" class="dgm-sub">
            boundary · 23,040 SLL, shared
          </text>
          <path :d="`M${LANE_IL},${s.top} V${s.bot}`" class="dgm-edge-accent" />
          <path :d="`M${LANE_AX},${s.top} V${s.bot}`" class="dgm-edge" />
          <rect
            :x="LANE_IL - 9"
            :y="s.mid - 7"
            width="18"
            height="14"
            rx="3"
            class="dgm-box-accent"
          />
          <rect
            :x="LANE_AX - 9"
            :y="s.mid - 7"
            width="18"
            height="14"
            rx="3"
            class="dgm-box"
          />
        </g>
      </svg>
    </Fig>

    <Callout kind="trap" title="Mesh-to-SLR is deliberately not identity">
      <p>
        A station is indexed by <strong>SLR</strong>, not by mesh — station 2
        lives in SLR2 and serves <code>mesh_3</code>. This trips everyone once.
      </p>
      <p>
        The station bus is a <strong>line, not a tree</strong>: there is no
        root, every station is the same module, and each has exactly two
        neighbours except the two ends. A flit carries <code>dst_stn</code> and
        each hop makes one comparison — equal ejects, less passes left, greater
        passes right. Every AXI manager lands on station 1, alongside XDMA.
      </p>
    </Callout>

    <h3 class="doc-h3">What that lane replaced</h3>
    <p class="doc-p">
      The right-hand lane used to be a SmartConnect tree — a root on SLR1 with a
      leaf on each other die, reaching them through an AXI master port on the
      parent, an AXI slave port on the child and a register slice between. A
      line reaches a die through a link, which is not an AXI port and appears in
      neither station's port count. For four dies that is six full-width AXI
      shims the line never instantiates.
    </p>
    <SpecTable
      :cols="replaced.cols"
      :rows="replaced.rows"
      caption="CLB LUTs, both sides at the same endpoint set, on xcvu13p-fhgb2104-2L-e. The SmartConnect column is the SUPERSEDED baseline — it is not what is on the die. The station column is the per-instance breakdown of one synthesis of the deployed configuration, FW=256, AW=43, BALANCED, no block RAM."
    />

    <SpecTable
      :cols="replacedProvenance.cols"
      :rows="replacedProvenance.rows"
      caption="Provenance of the SmartConnect column, which is not uniformly v5: those IPs were never re-synthesised for it. Quote the total as an order-of-magnitude replacement, not as a v5 measurement."
    />

    <Callout
      kind="trap"
      title="The saving is real and lands in the wrong place"
    >
      <p>
        The biggest ratio is SLR1's, at 4.77x, because that die carried the
        root. The instinct is that this is where the device needed it — and it
        is not. SLR1 is the
        <strong>emptiest</strong> die, so the largest saving lands where there
        was already the most room. The binding die is SLR0, which the change
        barely touches.
      </p>
    </Callout>

    <SpecTable
      :cols="populations.cols"
      :rows="populations.rows"
      caption="SLR1 carries the smallest mesh because it also carries XDMA, the station every manager attaches to, jtag_axi, the control clock and one DDR4 controller. xcvu13p-fhgb2104-2L-e."
    />

    <Callout kind="trap" title="A population is a property of a named build">
      <p>
        Populations move between generations and the two columns above disagree
        on purpose. Both are true of their own build.
        <strong
          >Treat a population as a property of a named build, never as a
          property of “the ship”</strong
        >, and check which build a figure came from before carrying it.
      </p>
    </Callout>

    <h2 class="doc-h2">Where the boundary should fall</h2>
    <p class="doc-p">
      The traffic classes in a machine of this shape are very unequal, so a
      boundary should land on the cheapest one.
    </p>
    <SpecTable :cols="classes.cols" :rows="classes.rows" />

    <Callout
      kind="measured"
      title="The fabric spanning regions was implemented, and rejected"
    >
      <p>
        The obvious arrangement is one large mesh spanning the die. Its worst
        path measured
        <strong>4.6 ns at 98.3% routing with zero logic levels</strong>. A path
        that is almost entirely route and has no logic in it cannot be fixed by
        pipelining the logic, because there is none.
      </p>
      <p>
        It was not the wire count that killed it — a full-width fabric link is a
        small fraction of one boundary's crossing registers. It was that a
        fabric whose whole premise is locality stops having any.
        <em>(One accelerator on xcvu13p-fhgb2104-2L-e.)</em>
      </p>
    </Callout>

    <Callout
      kind="rule"
      title="Three constraints, all hard rather than preferential"
    >
      <p>
        A datapath on a cascade cannot cross a boundary. A memory channel cannot
        cross a boundary. Every crossing signal is flop to flop, one cycle plus
        pipelining.
      </p>
      <p>
        What is left standing is the arrangement on
        <RouterLink to="/framework/ship" class="doc-link"
          >Ship assembly</RouterLink
        >:
        <strong
          >one mesh per region, each with its own memory channel, joined edge to
          edge by an explicit registered link.</strong
        >
      </p>
    </Callout>

    <h2 class="doc-h2">Device facts, and how to establish them</h2>
    <p class="doc-p">
      These are properties of the part, not measurements of any accelerator.
      They are given for the part the reference instance targets as an example
      of the <em>kind</em> of fact that has to be nailed down before a floorplan
      exists.
    </p>
    <SpecTable :cols="deviceFacts.cols" :rows="deviceFacts.rows" />

    <SpecTable
      :cols="census.cols"
      :rows="census.rows"
      caption="Site census of xcvu13p-fhgb2104-2L-e. The four SLRs are identical in hard IP, with two asymmetries only: the end dies have one Laguna face rather than two, and SLR1 is the master, so configuration and the device-DNA and user-eFUSE primitives live there."
    />

    <Callout kind="note" title="No hard DDR controller, and no HBM">
      <p>
        That primitive is Versal-only on this family. The XIPHY is hard and the
        controller is soft RTL — about 11.9k LUT / 13.5k FF / 25.5 BRAM36,
        roughly 2.8% of one SLR's LUTs on xcvu13p-fhgb2104-2L-e. A DDR4
        interface <strong>cannot span SLRs</strong>, which is what makes the
        channel map a constraint rather than a preference. There is no fallback
        if the DDR4 channels are not enough.
      </p>
    </Callout>

    <h3 class="doc-h3">Verify the mapping; do not infer it</h3>
    <SpecTable
      :cols="channels.cols"
      :rows="channels.rows"
      caption="Exactly one DDR4 controller per SLR, and the channel numbering does not match the die numbering. xcvu13p-fhgb2104-2L-e."
    />

    <Callout
      kind="rule"
      title="Establish the channel-to-region map from three independent witnesses"
    >
      <p>
        An I/O bank to region query, a placed clock buffer's coordinate in an
        implemented design, and the board's own pinout document.
        <strong>Agreement between three is the evidence.</strong> The numbering
        is not the mapping, and a design built on the guess crosses a boundary
        for its own memory — and nothing announces it.
      </p>
      <p>
        Distinguish <em>this design places nothing there</em> from
        <em>nothing can be placed there</em>. An implemented design that leaves
        a region empty is a property of that design, not of the board.
      </p>
    </Callout>

    <h2 class="doc-h2">The cross-boundary wire budget</h2>
    <SpecTable
      :cols="crossing.cols"
      :rows="crossing.rows"
      caption="As recorded for xcvu13p-fhgb2104-2L-e. Assume more pipeline stages than the raw crossing delay suggests: for wide buses at the frequencies this kind of machine targets, vendor guidance asks for several stages, and its own worst case needs more than that."
    />

    <Callout kind="rule" title="flop → SLL → flop, with nothing in between">
      <p>
        A Laguna site <em>is</em> a flip-flop, and a single combinational gate
        on the path — an AND with a valid, a mux on a ready — forfeits it and
        turns the crossing into ordinary interconnect. SLLs are the only data
        connection between dies, so
        <strong>every cluster must be SLR-resident</strong>; a DSP cascade is a
        physical object that cannot be cut.
      </p>
    </Callout>

    <SpecTable
      :cols="sll.cols"
      :rows="sll.rows"
      caption="Post-placement SLL usage per boundary on xcvu13p-fhgb2104-2L-e, against 629 derived for this configuration. The model is good to about 2%, and the asymmetry is the expected one — requests leave the manager station, responses return. A boundary carries exactly two streams, one REQ and one RSP, because a master both reads and writes across it: that is the minimum, not an economy."
    />

    <Callout kind="measured" title="SLL is not the constraint">
      <p>
        Every crossing structure on this device together uses well under a tenth
        of one boundary's budget. That is worth stating plainly because it is
        the opposite of the intuition that kills a spanning fabric: the fabric
        that was rejected did not run out of wires, it ran out of locality.
        <strong>Wire count was never the binding term.</strong>
      </p>
    </Callout>

    <Callout
      kind="open"
      title="Nobody has published where the ceiling really is"
    >
      <p>
        <strong
          >No published guideline exists for how many crossing registers may be
          used before routing becomes critical.</strong
        >
        Vendor documentation says only to check that usage matches the design's
        expectations. Treat the site count as a hard ceiling and do not plan to
        approach it.
      </p>
      <p>
        <strong
          >No published figure exists for how much logic headroom to reserve for
          crossing routing</strong
        >
        either. The nearest available guidance is a general “keep any one
        resource well below saturation in a single region”.
      </p>
    </Callout>

    <h3 class="doc-h3">What actually ran out first</h3>
    <SpecTable
      :cols="occupancy.cols"
      :rows="occupancy.rows"
      caption="The placed v5 design on xcvu13p-fhgb2104-2L-e, except the XDMA row, which is the single-mesh design on the card. No hard block is close to binding; the machine is bound by fabric and by packing."
    />

    <Callout
      kind="trap"
      title="The die carrying the interconnect is the emptiest one, not the fullest"
    >
      <p>
        The intuition is that the die holding the host bridge and the
        interconnect must be the tightest, and it is wrong here:
        <strong>SLR1 is the least utilised of the four</strong> — 88.61% CLB
        against SLR0's 95.49%, and 45% DSP against 79.6% — because it carries
        the interconnect but the <em>smallest share of mesh</em>. A LUT saving
        on the interconnect therefore lands where there was already the most
        room, and does not relieve the binding constraint, which is SLR0.
      </p>
      <p>
        Second half of the same trap: every die sits at 88–95% CLB occupancy
        while using only 61–69% of its LUTs. The design is
        <strong>packing-bound</strong>, so a LUT saving converts into placeable
        sites only as well as the placer packs what remains. Freeing LUTs on the
        wrong die, in a design that is not LUT-bound, buys nothing.
      </p>
    </Callout>

    <h2 class="doc-h2">
      Floorplan: pblocks, and what is deliberately not constrained
    </h2>
    <p class="doc-p">
      A region assignment is expressed as a placement constraint covering that
      region's clock-region rows, with the assembly's cell added to it. Two
      properties of that constraint are the whole technique.
    </p>

    <Callout kind="rule" title="Pin placement, not routing">
      <p>
        The purpose is locality — keep an assembly's cells together and in the
        right region — not to build a wall. A constraint that contained routing
        would also pin the paths that are
        <em>meant</em> to leave, including the boundary crossings, which is the
        opposite of what is wanted.
      </p>
    </Callout>

    <Callout kind="trap" title="State the floorplan; do not let it fall out">
      <p>
        Left unpinned, two meshes will happily land on each other's region and
        cross a boundary for their own memory —
        <strong
          >a design that meets timing, works, and is slower than it should be
          for a reason no report names.</strong
        >
        The floorplan is an input.
      </p>
    </Callout>

    <Callout kind="note" title="The crossing pipeline is the exception">
      <p>
        Boundary-crossing pipeline registers are given a stage count and left
        for the tool to size and place. Pinning them would pin the very path
        they exist to relax. The placed build is one pblock per SLR with
        <strong>the links deliberately unpinned</strong>, and the station part
        of the design places where it was told to.
      </p>
    </Callout>

    <Callout
      kind="trap"
      title="A crossing pipeline will infer an SRL and collapse into one site"
    >
      <p>
        The crossing flops are the die-crossing pipeline itself.
        <code>srl_style = "register"</code> keeps them as flops; without it the
        shift chain infers a shift register and
        <strong>every stage lands in one site</strong> — which is the one place
        the pipeline must not be, since the whole point is a flop on each side
        of the boundary.
      </p>
    </Callout>

    <h2 class="doc-h2">Clock domains</h2>
    <SpecTable
      :cols="domains.cols"
      :rows="domains.rows"
      caption="A machine of this shape has four kinds of clock, and they are mutually asynchronous."
    />

    <Callout kind="rule" title="The fabric has no clock crossing in it">
      <p>
        That is a property worth protecting: the crossings are at the memory
        boundary — asynchronous FIFOs carrying their own scoped constraints —
        and inside the interconnect that already spans domains for the host,
        which on this build means inside each station-to-station link.
        <strong
          >Adding a third place is a change to the architecture, not a wiring
          detail.</strong
        >
      </p>
      <p>
        Asynchronous domains must be declared as such. Otherwise the tool times
        crossings that were never meant to be timed and spends its effort on
        paths that do not exist.
      </p>
    </Callout>

    <Callout kind="trap" title="A constraint file is not a script">
      <p>
        Constraint parsing runs in a restricted mode that rejects control flow —
        and rejects it as a <strong>warning rather than an error</strong>, so
        the block is silently skipped and every crossing gets timed anyway.
        Write constraints flat. This one costs hours and leaves no obvious
        symptom.
      </p>
    </Callout>

    <SpecTable
      :cols="asBuiltClocks.cols"
      :rows="asBuiltClocks.rows"
      caption="Eleven clocks, all constrained and all met, on xcvu13p-fhgb2104-2L-e. Out-of-context synthesis of the deployed station-bus line. bus_clk1 binds, at 1.79x its 200 MHz target — it is the station carrying the three managers, so its switch arbitrates three injectors while the others arbitrate one. Listing every clock with its period is the check that the run was timed at all."
    />

    <Callout kind="measured" title="Per-station clock domains are free">
      <p>
        Collapsing the line onto one clock does not save anything — it
        <em>costs</em> 328 LUTs and saves 670 flip-flops, a wash either way at
        1% of the design. Four asynchronous domains, with the crossings inside
        credited links, are had for nothing measurable.
      </p>
      <p>
        That is the one place a crossbar cannot follow. The same change measured
        on vendor
        <code>axi_interconnect</code> costs
        <strong>9,833 LUTs at max-performance and 5,597 at minimum-area</strong>
        — 2.47x and 2.26x — against <strong>−328</strong> here. Its equivalent
        is a per-port clock converter.
      </p>
    </Callout>

    <Callout
      kind="trap"
      title="Do not bind the host bridge to a shared primary clock"
    >
      <p>
        The tree this replaced ran its whole crossbar on one primary clock, so
        the host bridge had to be given its own: bind XDMA to the primary and
        <strong>JTAG freezes whenever PCIe is unplugged</strong>. A line has no
        shared primary — each station runs its own fabric clock and the crossing
        lives inside the link — but the lesson survives the structure that
        taught it.
      </p>
      <p>
        A second constraint shares the same region: only one IO-driven BUFG may
        feed the MMCMs, and it needs
        <code>CLOCK_DEDICATED_ROUTE ANY_CMT_COLUMN</code> — five MMCMs across
        four SLRs exceeds <code>rule_bufg_mmcm_3loads</code> otherwise.
      </p>
    </Callout>

    <h2 class="doc-h2">A retunable mesh clock</h2>
    <p class="doc-p">
      If the mesh frequency is baked in at build time, then “it did not close
      timing” costs a full rebuild to try a lower number — the wrong unit of
      iteration for a value nobody can predict in advance. Worse, the frequency
      at which the silicon actually stops computing correctly is never measured:
      static timing analysis is a verdict at worst-case process, voltage and
      temperature, and the gap between that and reality is unknown and
      unknowable from the reports.
    </p>

    <Fig
      caption="Two clock generators, because the control plane must never stand on the clock it is changing. The variable one is an ordinary clocking primitive with dynamic reconfiguration enabled, driven over a narrow bus clocked from the fixed domain."
      zoom
    >
      <BlockDiagram :nodes="genDiagram.nodes" :edges="genDiagram.edges" />
    </Fig>

    <SpecTable
      :cols="retuneRules.cols"
      :rows="retuneRules.rows"
      caption="Three rules come with it."
    />

    <StepPlayer :steps="seqSteps" label="The retune sequence">
      <template #default="{ state }">
        <StateMachine
          :states="seq.states"
          :edges="seq.edges"
          :active="state.active"
          :r="30"
        />
      </template>
    </StepPlayer>

    <Callout
      kind="trap"
      title="A low-resolution configuration measures the clock generator, not the design"
    >
      <p>
        The arithmetic to get right is which multiplier step the output moves
        by, and the temptation to push the phase-detector frequency low for
        finer steps should be resisted twice:
        <strong>the multiplier field saturates</strong>, truncating exactly the
        top of the range being hunted, and
        <strong>jitter rises as the phase-detector frequency falls</strong>.
        Jitter is clock uncertainty on real silicon; it eats setup margin the
        same way a slow path does. For finer steps near a chosen frequency, use
        fractional multiplication at a high phase-detector frequency.
      </p>
    </Callout>

    <Callout
      kind="rule"
      title="Timing analysis only verifies up to the built-in frequency"
    >
      <p>
        The tool constrains the mesh clock from the generator's build-time
        configuration, so that frequency is the
        <strong>verified ceiling</strong>: at or below it the design is
        analysed; above it is a deliberately unmeasured sweep. Both are useful;
        they are not the same claim, and a page that conflates them is wrong.
      </p>
    </Callout>

    <h2 class="doc-h2">The measurement discipline</h2>
    <p class="doc-p">
      Placement decisions need numbers, and numbers need instruments.
    </p>

    <Callout
      kind="rule"
      title="Out-of-context synthesis is the unit of iteration"
    >
      <p>
        A block synthesised on its own gives frequency and resource figures fast
        enough to make a design loop, which a full implementation does not. What
        it does not give is placement or routing, so
        <strong
          >an out-of-context frequency is an upper bound and must be labelled as
          one</strong
        >. A page that quotes an out-of-context number as if a placed design
        achieved it is making a claim nobody checked.
      </p>
    </Callout>

    <Callout kind="rule" title="Name the instrument on every number">
      <p>
        Which top, which parameters, which part, which speed grade, and whether
        it was placed. And report where a block's cells actually landed: region
        spread per top-level block turns “the floorplan is what I asked for”
        from an assumption into a report line.
      </p>
    </Callout>

    <Callout
      kind="trap"
      title="Almost every figure this project quotes is out of context"
    >
      <p>
        Utilisation is reliable and every Fmax is an
        <strong>upper bound</strong> — it answers “is the logic deep enough to
        fail?”, not “will it place”. Placed data exists and it is thinner: a
        multi-mesh design has been placed and gives the URAM, CLB and SLL
        occupancy figures above; a single-mesh design is the one on the card and
        is where the host-IP costs were measured.
        <strong
          >No cluster-count scaling figure in this project is a placed
          result.</strong
        >
        Where a page multiplies one cluster by 32 or 45, that is arithmetic and
        is labelled as such.
      </p>
    </Callout>

    <Callout
      kind="trap"
      title="One inconsistency, recorded rather than resolved"
    >
      <p>
        The constraints file names the part without the <code>L</code> suffix
        while everything else says <code>-2L-e</code>, and all the measurements
        were taken on <code>-2L-e</code>. Speed grade changes timing, so a
        figure taken against the wrong part number would be wrong in a way
        nothing else would catch.
      </p>
    </Callout>

    <Callout kind="open" title="No part of the software stack models locality">
      <p>
        Two compute units are treated as interchangeable, which stops being true
        the moment one of them is across a boundary from the operand it needs.
        That is the same shape as the residency constraints the compiler already
        carries, and it is not there yet.
      </p>
    </Callout>
  </DocPage>
</template>
