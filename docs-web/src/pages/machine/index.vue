<script setup>
/* The sheet: everything the project ships, on one diagram, from the card down
 * to the primitive. Every number is copied from the page that measured it and
 * the table below names that page; nothing here is measured afresh.
 *
 * Layout is four bands so the wires stay planar: the host and the station bus;
 * the image (four SLR columns, the Xache bar across them, a DDR4 channel each);
 * inside one node / one router / one hop; inside one cluster / one vector core /
 * one RV32 PE; and the primitives under the unit that is made of them. */

const X = 34; // one SLR column's pitch
const col = (i) => i * X;

const slr = [
  { i: 0, mesh: "mesh_0", ddr: "ddr4_2", pop: "8 clusters + 2 vector cores" },
  { i: 1, mesh: "mesh_1", ddr: "ddr4_3", pop: "6 clusters + 2 vector cores" },
  { i: 2, mesh: "mesh_3", ddr: "ddr4_1", pop: "8 clusters + 2 vector cores" },
  { i: 3, mesh: "mesh_2", ddr: "ddr4_0", pop: "8 clusters + 2 vector cores" },
];

const cardNodes = slr.flatMap(({ i, mesh, ddr, pop }) => [
  {
    id: `st${i}`,
    x: col(i) + 11,
    y: 9,
    w: 10,
    h: 3,
    label: `station ${i}`,
    sub: "two link ports · control window · staging aperture",
  },
  {
    id: `nd${i}`,
    x: col(i),
    y: 14,
    w: 32,
    h: 5,
    label: `system node ${i} — 32,859 LUT`,
    sub: "sn_hub · RV64 runtime host · mover · transform slot · 2 memory ports · 2 MB staging · agent · interlink",
    accent: true,
  },
  {
    id: `ms${i}`,
    x: col(i),
    y: 22,
    w: 26,
    h: 10,
    label: `${mesh} — routers + edge ring`,
    sub: `${pop} · 2 memory ports on different rows · XY routing, one 288-bit flit per cycle per link`,
    accent: true,
  },
  {
    id: `dd${i}`,
    x: col(i) + 21,
    y: 42,
    w: 11,
    h: 3.4,
    label: `DDR4 ${ddr} — 4 GB`,
    sub: "vendor controller",
  },
]);

const cardEdges = slr.flatMap(({ i }) => [
  { from: "sb:b", to: `st${i}:t` },
  { from: `st${i}:b`, to: `nd${i}:t`, label: i === 0 ? "host reach" : "" },
  {
    from: `nd${i}:b`,
    to: `ms${i}:t`,
    label: i === 0 ? "flits" : "",
    accent: true,
  },
  {
    from: `nd${i}:b`,
    to: "xb:t",
    label: i === 0 ? "AXI4 512b" : "",
  },
  { from: "xb:b", to: `dd${i}:t` },
  ...(i < 3
    ? [
        {
          from: `nd${i}:r`,
          to: `nd${i + 1}:l`,
          label: i === 0 ? "interlink" : "",
          dash: true,
          accent: true,
        },
      ]
    : []),
]);

const sheet = {
  groups: [
    {
      x: -2,
      y: 7,
      w: 138,
      h: 41,
      label:
        "one image — xcvu13p-fhgb2104-2L-e, four SLRs, four meshes on a line (multimesh_v7, 2026-08-23: 30 clusters + 8 vector cores)",
    },
    ...slr.map(({ i }) => ({
      x: col(i) - 0.5,
      y: 8,
      w: 33,
      h: 38.5,
      label: `SLR${i}${i === 1 ? " — also the host's die: XDMA 76,319 LUT" : ""}`,
    })),
    {
      x: -2,
      y: 51,
      w: 48,
      h: 19,
      label:
        "inside one system node — one per mesh, the only thing that touches anything outside it",
    },
    {
      x: 49,
      y: 51,
      w: 47,
      h: 19,
      label: "inside one router — noc_inport × 5, noc_outport × 5",
    },
    {
      x: 99,
      y: 51,
      w: 37,
      h: 19,
      label: "inside one hop — kx_hop, one per lane per die boundary, 3 cycles",
    },
    {
      x: -2,
      y: 73,
      w: 56,
      h: 19,
      label:
        "inside one matmul cluster — mx_cluster_cu, 16,390 LUT · 304 DSP · 512 MAC/cycle",
    },
    {
      x: 57,
      y: 73,
      w: 45,
      h: 19,
      label: "inside one vector core — vec_cu, 35,629 LUT · 51 DSP · 16 lanes",
    },
    {
      x: 105,
      y: 73,
      w: 37,
      h: 19,
      label:
        "inside one RV32 PE — rv_pe, 2,672 LUT · 4 DSP; measured, the image carries none",
    },
    {
      x: -2,
      y: 95,
      w: 32,
      h: 13,
      label:
        "one DSP48E2 of the 64 in a TCU — two int7 MACs: 0 LUT, 0 FF, 1 DSP",
    },
    {
      x: 57,
      y: 95,
      w: 45,
      h: 13,
      label:
        "one ALU lane of the 16 — vec_alu: E8M15 FMA, latency 14, II 1, 1,249 LUT",
    },
    {
      x: 105,
      y: 95,
      w: 37,
      h: 13,
      label: "one flit — what every link, FIFO and port carries: 288 bits",
    },
  ],
  nodes: [
    /* ---- the host and its bus ---- */
    {
      id: "host",
      x: 0,
      y: 0,
      w: 14,
      h: 4,
      label: "host",
      sub: "PCIe · XDMA 512b @ 250 MHz · JTAG-AXI 64b @ 100 MHz",
    },
    {
      id: "sb",
      x: 20,
      y: 0,
      w: 116,
      h: 4,
      label:
        "station bus — sb_bd_line4: one module, four pblocks, a station per die",
      sub: "AXI4 + AXI4-Lite to endpoints of any width and clock · credits in flits, never a ready across a die · 23,053 LUT where the vendor tree took 81,881",
      accent: true,
    },
    /* ---- the image ---- */
    ...cardNodes,
    {
      id: "xb",
      x: 0,
      y: 35,
      w: 134,
      h: 4,
      label:
        "Kohaku Xache — 4 DRAM masters → 4 channels, a tagged 2 MB cache fused per channel, partitioned per die (kx_pxache)",
      sub: "AXI4 512b at the two edges only · kx_carray 64 URAM per home · a lane of credited hops per source across every boundary · 9,642 LUT at the ship point, +966 for four partitions",
      accent: true,
    },
    /* ---- inside one system node ---- */
    {
      id: "hub",
      x: 0,
      y: 54,
      w: 7,
      h: 12.5,
      label: "sn_hub",
      sub: "514 LUT · owns every attachment · the mesh side",
      accent: true,
    },
    {
      id: "cpx",
      x: 11,
      y: 54,
      w: 12,
      h: 3.6,
      label: "control complex — 16,010 LUT",
      sub: "rv64_syscore 7,244 · mm_mover 4,226 · xform slot 4,540 (MXFP7 quantiser, id 1)",
      accent: true,
    },
    {
      id: "eng",
      x: 11,
      y: 58.5,
      w: 12,
      h: 3.6,
      label: "memory engines × 2",
      sub: "mag_mem_port: intake FIFOs, read engine, write slots in block RAM — 2,063 LUT each",
    },
    {
      id: "agt",
      x: 11,
      y: 63,
      w: 12,
      h: 3.6,
      label: "agent · interlink",
      sub: "host window, staging aperture · switch 2,435 + encapsulator 1,294 LUT",
    },
    {
      id: "conv",
      x: 25,
      y: 56,
      w: 12,
      h: 8,
      label: "converged path",
      sub: "channel latch → mag_stage_port → mag_stage (64 URAM, 2 MB) → mag_dram_port: two arbiters, DATA_W→MW, five async FIFOs",
      accent: true,
    },
    {
      id: "maxi",
      x: 39,
      y: 57.5,
      w: 6,
      h: 5,
      label: "M_AXI_DRAM",
      sub: "to the Xache",
    },
    /* ---- inside one router ---- */
    {
      id: "lin",
      x: 50,
      y: 55,
      w: 7,
      h: 3,
      label: "link in",
      sub: "data/valid/busy",
    },
    {
      id: "fifo",
      x: 59,
      y: 55,
      w: 8,
      h: 3,
      label: "flit FIFO",
      sub: "FIFO_DEPTH × 288b",
    },
    {
      id: "hold",
      x: 69,
      y: 55,
      w: 7,
      h: 3,
      label: "holding reg",
      sub: "one per input",
      accent: true,
    },
    { id: "mux", x: 78, y: 55, w: 7, h: 3, label: "5:1 mux", sub: "288b wide" },
    {
      id: "oreg",
      x: 87,
      y: 55,
      w: 7,
      h: 3,
      label: "output reg",
      sub: "→ link out",
      accent: true,
    },
    {
      id: "route",
      x: 59,
      y: 61,
      w: 8,
      h: 3,
      label: "route compare",
      sub: "clamp, then XY",
    },
    { id: "req", x: 69, y: 61, w: 7, h: 3, label: "req[4:0]", sub: "one-hot" },
    {
      id: "arb",
      x: 78,
      y: 61,
      w: 7,
      h: 3,
      label: "round robin",
      sub: "pointer per output",
    },
    /* ---- inside one hop ---- */
    {
      id: "htx",
      x: 100,
      y: 55,
      w: 8,
      h: 3.6,
      label: "TX register",
      sub: "the lane head; one load, the wire",
    },
    {
      id: "hring",
      x: 110,
      y: 55,
      w: 12,
      h: 3.6,
      label: "landing ring RAM",
      sub: "16 entries; the write port IS the landing register",
      accent: true,
    },
    {
      id: "hhead",
      x: 124,
      y: 55,
      w: 11,
      h: 3.6,
      label: "read stage → head",
      sub: "dst + kind out of distributed RAM",
    },
    {
      id: "hcr",
      x: 100,
      y: 62,
      w: 8,
      h: 3.6,
      label: "credit counter",
      sub: "16 at reset · −1 send · +1 cr",
    },
    {
      id: "hpp",
      x: 110,
      y: 62,
      w: 12,
      h: 3.6,
      label: "pp / fok registers",
      sub: "a pop as a pulse; ring out of reset",
    },
    /* ---- inside one matmul cluster ---- */
    {
      id: "cport",
      x: 0,
      y: 76,
      w: 8,
      h: 3.6,
      label: "mesh port",
      sub: "noc_cu_base — identical for both units",
    },
    {
      id: "cmgr",
      x: 10,
      y: 76,
      w: 10,
      h: 3.6,
      label: "manager · sequencer",
      sub: "one GEMM flit → 256 accumulator commands",
    },
    {
      id: "cl1",
      x: 22,
      y: 76,
      w: 10,
      h: 3.6,
      label: "L1A · L1B",
      sub: "928 b × 2, named block RAM, READ_LAT 1",
    },
    {
      id: "t0",
      x: 0,
      y: 83,
      w: 8,
      h: 4,
      label: "TCU 0",
      sub: "4×8×4 · 64 DSP48E2",
    },
    { id: "t1", x: 10, y: 83, w: 8, h: 4, label: "TCU 1", sub: "delay 2" },
    { id: "t2", x: 20, y: 83, w: 8, h: 4, label: "TCU 2", sub: "delay 4" },
    { id: "t3", x: 30, y: 83, w: 8, h: 4, label: "TCU 3", sub: "delay 6" },
    {
      id: "acu",
      x: 40,
      y: 83,
      w: 12,
      h: 4,
      label: "accumulator",
      sub: "FP22 resident tile · 5 BRAM · READ_LAT 2 · emit → drain → port",
      accent: true,
    },
    /* ---- inside one vector core ---- */
    {
      id: "vport",
      x: 58,
      y: 76,
      w: 8,
      h: 3.6,
      label: "mesh port",
      sub: "the same six signals",
    },
    {
      id: "vl1",
      x: 68,
      y: 76,
      w: 10,
      h: 3.6,
      label: "L1 scratchpad",
      sub: "512 × 256 b · block RAM · no tags",
    },
    {
      id: "vim",
      x: 68,
      y: 83,
      w: 10,
      h: 3.6,
      label: "instruction memory",
      sub: "32-bit words · LUTRAM",
    },
    {
      id: "vrf",
      x: 80,
      y: 76,
      w: 10,
      h: 3.6,
      label: "register file",
      sub: "3 mirrors · 3R1W per lane · striped",
    },
    {
      id: "vagu",
      x: 80,
      y: 83,
      w: 10,
      h: 3.6,
      label: "address generator",
      sub: "base + 4 (stride, bound)",
    },
    {
      id: "valu",
      x: 92,
      y: 78,
      w: 9,
      h: 6.5,
      label: "16 ALU lanes",
      sub: "3 DSP each · FLAT, D2, D4, TREE",
      accent: true,
    },
    /* ---- inside one RV32 PE ---- */
    {
      id: "pbase",
      x: 106,
      y: 76,
      w: 7,
      h: 3.6,
      label: "noc_cu_base",
      sub: "498 LUT · 4 BRAM",
    },
    {
      id: "pglue",
      x: 115,
      y: 76,
      w: 9,
      h: 3.6,
      label: "window writer · kick FSM",
      sub: "191 LUT",
    },
    {
      id: "pimem",
      x: 126,
      y: 76,
      w: 6,
      h: 3.6,
      label: "imem",
      sub: "2048 × 32",
    },
    {
      id: "pspad",
      x: 134,
      y: 76,
      w: 7,
      h: 3.6,
      label: "spad",
      sub: "2048 × 32",
    },
    {
      id: "pl1",
      x: 106,
      y: 83,
      w: 7,
      h: 4,
      label: "u_l1 · u_req",
      sub: "128 lines · 365 + 274 LUT",
    },
    {
      id: "pcore",
      x: 115,
      y: 83,
      w: 12,
      h: 4,
      label: "rv_core — RV32IM, 5 stages",
      sub: "1,298 LUT · 4 DSP · BTB 32",
      accent: true,
    },
    {
      id: "pslot",
      x: 129,
      y: 83,
      w: 12,
      h: 4,
      label: "SIMD_EN slot",
      sub: "khs_unit (SIMD, 15,682 LUT) or kht_unit (SIMT, 19,461 LUT) — measured OOC",
      accent: true,
    },
    /* ---- one DSP48E2 ---- */
    {
      id: "da",
      x: 0,
      y: 98,
      w: 9,
      h: 3.2,
      label: "A (27b) = w1 << 19",
      sub: "wiring",
    },
    {
      id: "dd",
      x: 0,
      y: 102.5,
      w: 9,
      h: 3.2,
      label: "D (27b) = w0",
      sub: "wiring",
    },
    {
      id: "dpre",
      x: 11,
      y: 100,
      w: 8,
      h: 3.4,
      label: "pre-adder",
      sub: "w1·2^19 + w0",
      accent: true,
    },
    {
      id: "dmul",
      x: 21,
      y: 100,
      w: 8,
      h: 3.4,
      label: "27 × 18 multiply",
      sub: "× a on B · P holds two products",
      accent: true,
    },
    /* ---- one ALU lane ---- */
    {
      id: "le",
      x: 58,
      y: 99,
      w: 8,
      h: 3.6,
      label: "DSP-E",
      sub: "exponent sum, alignment shift",
    },
    {
      id: "lm",
      x: 68,
      y: 99,
      w: 8,
      h: 3.6,
      label: "DSP-M",
      sub: "sig_a·sig_b + aligned c on the 48-bit C port",
      accent: true,
    },
    {
      id: "lp",
      x: 78,
      y: 99,
      w: 8,
      h: 3.6,
      label: "DSP-P",
      sub: "Horner stage 1 · FP32 high half",
    },
    {
      id: "ltab",
      x: 88,
      y: 99,
      w: 13,
      h: 3.6,
      label: "seed tables",
      sub: "exp2 · log2 · inv · rsqrt — 32 segments, quadratic",
    },
    /* ---- one flit ---- */
    {
      id: "fhdr",
      x: 106,
      y: 99,
      w: 11,
      h: 3.6,
      label: "header — 32 b",
      sub: "dst_x/y · src_x/y · type · txn · last",
    },
    {
      id: "fpay",
      x: 119,
      y: 99,
      w: 22,
      h: 3.6,
      label: "payload — 256 b",
      sub: "a memory descriptor (98 b + 158 b yours) · a granule · eight instruction words · a cluster opcode",
      accent: true,
    },
  ],
  edges: [
    { from: "host:r", to: "sb:l", label: "AXI" },
    ...cardEdges,
    { from: "xb:b", to: "htx:t", label: "one boundary", dash: true },
    /* node */
    { from: "hub:r", to: "cpx:l", label: "(0,0)" },
    { from: "hub:r", to: "eng:l", label: "memory", accent: true },
    { from: "hub:r", to: "agt:l", label: "remote" },
    { from: "cpx:r", to: "conv:l", label: "cp · MV" },
    { from: "eng:r", to: "conv:l", accent: true },
    { from: "agt:r", to: "conv:l", label: "inbound", dash: true },
    { from: "conv:r", to: "maxi:l", accent: true },
    /* router */
    { from: "lin:r", to: "fifo:l", dir: "h" },
    { from: "fifo:r", to: "hold:l", dir: "h" },
    { from: "fifo:b", to: "route:t", dir: "v" },
    { from: "route:r", to: "req:l", dir: "h" },
    { from: "hold:r", to: "mux:l", dir: "h", accent: true, label: "flit" },
    { from: "req:r", to: "arb:l", dir: "h", label: "request" },
    { from: "arb:t", to: "mux:b", dir: "v", label: "grant" },
    { from: "mux:r", to: "oreg:l", dir: "h", accent: true },
    { from: "oreg:b", to: "arb:r", dash: true, label: "room" },
    /* hop */
    {
      from: "htx:r",
      to: "hring:l",
      label: "boundary wire",
      accent: true,
      dir: "h",
    },
    { from: "hring:r", to: "hhead:l", label: "1 read stage", dir: "h" },
    { from: "hhead:b", to: "hpp:t", label: "pop" },
    { from: "hpp:l", to: "hcr:r", label: "wire, back", dir: "h", accent: true },
    { from: "hcr:t", to: "htx:b", label: "credit ≠ 0", dir: "v" },
    /* cluster */
    { from: "cport:r", to: "cmgr:l", dir: "h" },
    { from: "cmgr:r", to: "cl1:l", dir: "h", label: "FILL" },
    { from: "cmgr:b", to: "t1:t", dir: "v", label: "commands" },
    { from: "cl1:b", to: "t2:t", dir: "v", label: "operands, skewed" },
    { from: "t0:r", to: "t1:l", dir: "h", accent: true, label: "→ W" },
    { from: "t1:r", to: "t2:l", dir: "h", accent: true },
    { from: "t2:r", to: "t3:l", dir: "h", accent: true },
    { from: "t3:r", to: "acu:l", dir: "h", accent: true, label: "16 × int" },
    { from: "t0:b", to: "da:t", dash: true, label: "one of 64" },
    /* vector */
    { from: "vport:r", to: "vl1:l", dir: "h", label: "fill / drain" },
    { from: "vport:r", to: "vim:l", label: "load and run" },
    {
      from: "vl1:r",
      to: "vrf:l",
      dir: "h",
      accent: true,
      label: "convert on read",
    },
    { from: "vim:r", to: "vagu:l", dir: "h" },
    { from: "vrf:r", to: "valu:l", dir: "h", accent: true },
    { from: "vagu:r", to: "valu:l", dir: "h" },
    { from: "valu:b", to: "ltab:t", dash: true, label: "one of 16" },
    /* PE */
    { from: "pbase:r", to: "pglue:l", dir: "h", label: "CU_DATA" },
    { from: "pglue:r", to: "pimem:l", dir: "h", label: "buf_id 1" },
    { from: "pimem:r", to: "pspad:l", dir: "h", label: "buf_id 0" },
    { from: "pimem:b", to: "pcore:t", dir: "v", label: "fetch" },
    { from: "pcore:l", to: "pl1:r", dir: "h", label: "DRAM" },
    { from: "pl1:t", to: "pbase:b", dir: "v", label: "lines" },
    { from: "pcore:r", to: "pslot:l", dir: "h", label: "EX stage" },
    /* DSP */
    { from: "da:r", to: "dpre:l", dir: "h" },
    { from: "dd:r", to: "dpre:l", dir: "h" },
    { from: "dpre:r", to: "dmul:l", dir: "h", accent: true },
    /* lane */
    { from: "le:r", to: "lm:l", dir: "h" },
    { from: "lp:l", to: "lm:r", dir: "h", label: "Horner 1 → 2" },
    { from: "ltab:l", to: "lp:r", dir: "h" },
  ],
};

/* Tags for the viewer's toggles: which system a box belongs to. */
const tags = [
  { key: "axi", label: "host & AXI" },
  { key: "image", label: "the image" },
  { key: "node", label: "system node" },
  { key: "mesh", label: "mesh & flit" },
  { key: "tpu", label: "KohakuTPU units" },
  { key: "pe", label: "RV32 PE" },
];
const ntag = (id) => {
  if (/^(host|sb|xb|h(tx|ring|head|cr|pp))$/.test(id)) return "axi";
  if (/^(st|nd|ms|dd)\d$/.test(id)) return "image";
  if (/^(hub|cpx|eng|agt|conv|maxi)$/.test(id)) return "node";
  if (/^(lin|fifo|hold|mux|oreg|route|req|arb|fhdr|fpay)$/.test(id))
    return "mesh";
  if (/^p/.test(id)) return "pe";
  return "tpu";
};
const gtag = (label) => {
  if (/image|^SLR/.test(label)) return "image";
  if (/system node/.test(label)) return "node";
  if (/router|flit/.test(label)) return "mesh";
  if (/hop/.test(label)) return "axi";
  if (/RV32/.test(label)) return "pe";
  return "tpu";
};
const tagged = {
  groups: sheet.groups.map((g) => ({ ...g, tag: gtag(g.label) })),
  nodes: sheet.nodes.map((n) => ({ ...n, tag: ntag(n.id) })),
  edges: sheet.edges,
};

/* ------------------------------------------------------------ the inventory */
const invCols = [
  { key: "l", label: "Level" },
  { key: "w", label: "What ships", mono: false },
  { key: "m", label: "Measured", mono: true },
  { key: "p", label: "Page" },
];
const invRows = [
  {
    l: "<b>image</b>",
    w: "<code>multimesh_v7</code>: four meshes on a line, one per SLR, four DDR4 channels, the host on SLR1",
    m: "30 clusters + 8 vector cores enumerated off the card, 2026-08-23 · XDMA 76,319 LUT",
    p: "<RouterLink to='/tpu' class='doc-link'>The accelerator</RouterLink> · <RouterLink to='/framework/ship' class='doc-link'>Ship</RouterLink>",
  },
  {
    l: "<b>host AXI</b>",
    w: "the station bus — <code>sb_*</code>, one module, four pblocks, a station per die, credits in flits",
    m: "23,053 LUT against 81,881 for the vendor tree",
    p: "<RouterLink to='/component/station-bus' class='doc-link'>The station bus</RouterLink>",
  },
  {
    l: "<b>DRAM AXI</b>",
    w: "Kohaku Xache — <code>kx_xache</code>: M masters → N channels, a tagged 2 MB cache fused per channel; <code>kx_pxache</code> partitions it per die with a lane of hops per source",
    m: "9,642 LUT at the ship point (vendor path 38,975) · P=4: +966 LUT, 3 cycles per hop, the crossbar's bandwidth at every boundary",
    p: "<RouterLink to='/component/xache' class='doc-link'>Kohaku Xache</RouterLink> · <RouterLink to='/component/pxache' class='doc-link'>Partitioned Xache</RouterLink>",
  },
  {
    l: "<b>mesh</b>",
    w: "routers, links, the edge ring, the compute-unit port — <code>noc_*</code>",
    m: "one 288-bit flit per cycle per link · XY on clamped coordinates",
    p: "<RouterLink to='/framework/noc' class='doc-link'>NoC</RouterLink>",
  },
  {
    l: "<b>system node</b>",
    w: "<code>sysnode</code>: hub, control complex (RV64 host, mover, transform slot), memory engines, staging, agent, interlink",
    m: "32,859 LUT · 46,436 FF · 57.5 BRAM · 65 URAM · 47 DSP · +0.039 ns at 300 MHz, OOC synthesis",
    p: "<RouterLink to='/component/sysnode' class='doc-link'>The system node</RouterLink> · <RouterLink to='/component/sysnode/microarchitecture' class='doc-link'>micro</RouterLink>",
  },
  {
    l: "<b>node's processor</b>",
    w: "<code>rv64_syscore</code>: RV64IMA + Zicsr, M/S/U, Sv39, traps, L1, dispatch mailbox",
    m: "7,244 LUT inside the node · 1.331 DMIPS/MHz",
    p: "<RouterLink to='/component/rv64sys' class='doc-link'>RV64-sys</RouterLink>",
  },
  {
    l: "<b>compute unit</b>",
    w: "the matmul cluster — <code>mx_cluster_cu</code>: four 4×8×4 TCUs chained into an FP22 accumulator, one port",
    m: "16,390 LUT · 18,404 FF · 35 BRAM · 304 DSP · 512 MAC/cycle · 310 MHz met OOC",
    p: "<RouterLink to='/tpu/matmul' class='doc-link'>Matmul cluster</RouterLink>",
  },
  {
    l: "<b>compute unit</b>",
    w: "the vector core — <code>vec_cu</code>: 16 E8M15 lanes, four seeds at full rate, a strided address generator",
    m: "35,629 LUT · 22,145 FF · 44 BRAM · 51 DSP · 358.4 MHz OOC",
    p: "<RouterLink to='/tpu/vector' class='doc-link'>Vector core</RouterLink>",
  },
  {
    l: "<b>compute unit</b>",
    w: "the RV32 PE — <code>rv_pe</code>: RV32I + multiply, two L1s, a scratchpad peers can write",
    m: "2,672 LUT · 3,844 FF · 9 BRAM · 4 DSP · 363.5 MHz OOC — measured; the image carries none",
    p: "<RouterLink to='/component/rv32pe' class='doc-link'>RV32 PE</RouterLink>",
  },
  {
    l: "<b>slot occupants</b>",
    w: "<code>khs_unit</code> (SIMD PE) and <code>kht_unit</code> (SIMT PE) in the PE's <code>SIMD_EN</code> slot; the MXFP7 quantiser in the node's transform slot",
    m: "15,682 and 19,461 LUT on frozen trees, OOC · quantiser 4,267 LUT, 32 DSP",
    p: "<RouterLink to='/mpe/simd' class='doc-link'>SIMD PE</RouterLink> · <RouterLink to='/mpe/simt' class='doc-link'>SIMT PE</RouterLink> · <RouterLink to='/tpu/numbers' class='doc-link'>MXFP7</RouterLink>",
  },
  {
    l: "<b>primitive</b>",
    w: "one DSP48E2 as two int7 MACs; one <code>vec_alu</code> lane; one <code>kx_hop</code>; one flit",
    m: "0 LUT 0 FF 1 DSP · 1,249 LUT 3 DSP at 324.8 MHz · a lane 79 LUT at 666 MHz · 288 bits",
    p: "<RouterLink to='/tpu/matmul' class='doc-link'>Matmul</RouterLink> · <RouterLink to='/tpu/vector/microarchitecture' class='doc-link'>Vector micro</RouterLink> · <RouterLink to='/component/pxache' class='doc-link'>Partitioned Xache</RouterLink> · <RouterLink to='/framework/noc' class='doc-link'>NoC</RouterLink>",
  },
  {
    l: "<b>software</b>",
    w: "the driver and hand-built encoders, <code>kohakutpu.lang</code> → <code>kohakutpu.isa</code>, a tinygrad frontend, the bench harnesses and the OOC measurement flow",
    m: "runs on the card · 6.8 % → 87.6 % of datapath peak on a 256-cube in simulation",
    p: "<RouterLink to='/tpu/results' class='doc-link'>What was measured</RouterLink> · <RouterLink to='/framework/measurements' class='doc-link'>Measurements</RouterLink>",
  },
];
</script>

<template>
  <DocPage
    title="Everything we ship"
    summary="One sheet. The card at the top, the primitive at the bottom, and every level between: what the host reaches through, what each die holds, what a node and a mesh are made of, what a unit is made of, what a lane and a MAC and a flit are. Every number on it is copied from the page that measured it."
    domain="framework"
    status="measured"
    source="multimesh_v7 · src/kohakuaccel/ · src/kohakuaxi/ · src/kohakutpu/ · src/kohakumpe/"
  >
    <p class="doc-p">
      Read it top down. The <b>host</b> reaches the card over the
      <b>station bus</b>, a line of four stations, one per die. Each die holds
      one <b>system node</b> — the only thing on a mesh that touches anything
      outside it — and one <b>mesh</b> of routers with the compute units on
      them; the nodes are chained by the <b>interlink</b>, and every node's DRAM
      master enters the <b>Kohaku Xache</b>, which serves the four DDR4 channels
      through a cache per channel and is partitioned across the dies with a lane
      of credited hops per source. The three bands below the image open one of
      each thing: a node, a router, a hop; a matmul cluster, a vector core, an
      RV32 PE; and under those, one DSP48E2, one ALU lane, one flit. Zoom it —
      it is drawn at one scale on purpose.
    </p>

    <Fig
      caption="The whole project on one sheet, at one scale. Boxes are modules or families of modules, wires are the port groups between them, dashed wires mean “one of these is opened below”. The image band is what a card runs today (multimesh_v7); the RV32 PE and its SIMD/SIMT slot occupants are shipped and measured out of context but no image carries one. LUT figures are out-of-context synthesis on xcvu13p-fhgb2104-2L-e with Vivado 2024.2, quoted from the page each line names in the table."
      zoom
      sheet
    >
      <BlockDiagram
        :nodes="tagged.nodes"
        :edges="tagged.edges"
        :groups="tagged.groups"
        :tags="tags"
      />
    </Fig>

    <h2 class="doc-h2">Every level, and where it is measured</h2>
    <SpecTable
      :cols="invCols"
      :rows="invRows"
      caption="One row per level of the sheet. A figure here is the figure on the named page, under that page's conditions; the levels do not add — a node contains a processor, a cluster contains its TCUs, and the image contains all of them"
    />

    <Callout kind="rule" title="What the sheet does not say">
      <p>
        No frequency on it is a closed-timing figure: every one is
        out-of-context synthesis, and this tree has measured a module lose 0.740
        ns between synthesis and routing. No mesh of PEs has been placed. The
        populations are what the card's own control plane enumerated on
        2026-08-23, not what a plan says.
      </p>
    </Callout>
  </DocPage>
</template>
