<script setup>
/**
 * /component/caching — drawn from, in full:
 *   docs/notes/cache/{README,mag-staging,noc-staging,endpoint-tagged,axi-tlb}.md
 *   docs/spec/transform-slot.md · docs/address-map.md
 * and verified against the RTL, which wins wherever the notes disagree:
 *   src/kohakuaccel/noc/endpoint/noc_l2_adapter.v
 *   src/kohakuaccel/sysnode/core/{mag_stage,mag_stage_port,mag_xform,mag}.v
 *   src/kohakuaccel/sysnode/cpu/rv64_mag_pe.v · scripts/py/gen_mesh.py
 *
 * LUT and DSP figures are out-of-context SYNTHESIS of `sysnode` on
 * xcvu13p-fhgb2104-2L-e, Vivado 2024.2, 3.333 ns ask. Nothing is placed.
 */

/* ================================================== where a read is answered */
const tiersDia = {
  nodes: [
    {
      id: "cu",
      x: 0,
      y: 9,
      w: 9,
      h: 5.5,
      label: "compute unit",
      sub: "emits MEM_RD_REQ",
      accent: true,
    },
    {
      id: "ad",
      x: 13,
      y: 9,
      w: 9.5,
      h: 5.5,
      label: "noc_l2_adapter",
      sub: "in the local link · 8 URAM · 256 KB",
      accent: true,
    },
    {
      id: "rt",
      x: 28,
      y: 9,
      w: 8,
      h: 5.5,
      label: "router",
      sub: "XY · one flit per cycle",
    },
    {
      id: "mp",
      x: 40,
      y: 9,
      w: 9.5,
      h: 5.5,
      label: "memory port",
      sub: "walks the descriptor",
    },
    {
      id: "sp",
      x: 53.5,
      y: 0,
      w: 10,
      h: 5.5,
      label: "mag_stage",
      sub: "4 banks · 64 URAM · 2 MB",
      accent: true,
    },
    {
      id: "dp",
      x: 53.5,
      y: 18,
      w: 10,
      h: 5.5,
      label: "mag_dram_port",
      sub: "concentrate · cross clock",
    },
    {
      id: "dr",
      x: 67,
      y: 18,
      w: 8,
      h: 5.5,
      label: "DDR4",
      sub: "4 GB per mesh",
    },
  ],
  edges: [
    { from: "cu:r", to: "ad:l", dir: "h", accent: true },
    { from: "ad:r", to: "rt:l", dir: "h", label: "not ours" },
    { from: "rt:r", to: "mp:l", dir: "h" },
    { from: "mp:t", to: "sp:b", dir: "v", accent: true, label: "aperture" },
    { from: "mp:b", to: "dp:t", dir: "v", label: "DRAM" },
    { from: "dp:r", to: "dr:l", dir: "h" },
  ],
};

/* ================================================== inside the adapter */
const adapter = {
  nodes: [
    {
      id: "eu",
      x: 0,
      y: 6,
      w: 8,
      h: 5,
      label: "endpoint out",
      sub: "data/valid/busy",
    },
    {
      id: "dec",
      x: 11,
      y: 6,
      w: 10,
      h: 5,
      label: "window decode",
      sub: "base compare · three carries",
    },
    {
      id: "skid",
      x: 24,
      y: 6,
      w: 9,
      h: 5,
      label: "skid",
      sub: "one flit, plus the verdict",
      accent: true,
    },
    {
      id: "rd",
      x: 36,
      y: 0,
      w: 9,
      h: 5,
      label: "read engine",
      sub: "cur · left · tag · word",
    },
    {
      id: "wt",
      x: 36,
      y: 12,
      w: 9,
      h: 5,
      label: "write engine",
      sub: "one open write",
    },
    {
      id: "ram",
      x: 48,
      y: 6,
      w: 10,
      h: 5,
      label: "kohaku_sdpram",
      sub: "256b × DEPTH · ultra · READ_LAT 2",
      accent: true,
    },
    {
      id: "q",
      x: 61,
      y: 6,
      w: 9,
      h: 5,
      label: "sync_fifo",
      sub: "QD = 16 · distributed",
      accent: true,
    },
    {
      id: "ep",
      x: 73,
      y: 6,
      w: 8,
      h: 5,
      label: "endpoint in",
      sub: "response, or the router's flit",
    },
    {
      id: "rt",
      x: 11,
      y: 19,
      w: 10,
      h: 5,
      label: "router out",
      sub: "toward the endpoint",
    },
    {
      id: "ctl",
      x: 24,
      y: 19,
      w: 9,
      h: 5,
      label: "CU_CTRL",
      sub: "0xE0–0xE7 · terminated here",
      accent: true,
    },
    {
      id: "ru",
      x: 48,
      y: 19,
      w: 10,
      h: 5,
      label: "router in",
      sub: "a forward, or a reply",
    },
  ],
  edges: [
    { from: "eu:r", to: "dec:l", dir: "h" },
    { from: "dec:r", to: "skid:l", dir: "h", label: "verdict" },
    { from: "skid:r", to: "rd:l", dir: "h", accent: true },
    { from: "skid:r", to: "wt:l", dir: "h" },
    { from: "rd:r", to: "ram:l", dir: "h", accent: true, label: "line" },
    { from: "wt:r", to: "ram:l", dir: "h", label: "line + data" },
    { from: "ram:r", to: "q:l", dir: "h", accent: true },
    { from: "q:r", to: "ep:l", dir: "h", accent: true },
    { from: "skid:b", to: "ru:t", dir: "v", label: "forwarded whole" },
    { from: "rt:r", to: "ctl:l", dir: "h" },
    { from: "ctl:r", to: "ru:l", dir: "h", label: "reply" },
  ],
};

const adapterKnobs = {
  cols: [
    { key: "knob", label: "Knob", mono: true },
    { key: "what", label: "What it moves" },
  ],
  rows: [
    {
      knob: "PASS",
      what: "<b>Everything, at once.</b> 1 generates a straight wire on both faces and the whole module folds away. A slot whose empty state costs nothing is a slot people leave in.",
    },
    {
      knob: "DEPTH",
      what: "The store, linearly: <b>DEPTH × 256 bits</b>. 8,192 lines is 256 KB and 8 URAM — 4 URAM per 4,096 lines at 256 bits.",
    },
    {
      knob: "L2_BITS",
      what: "<b>Not free.</b> It MUST be <span class='chip'>5 + $clog2(DEPTH)</span> or the window and the store are different sizes. See the trap below.",
      _tone: "warn",
    },
    {
      knob: "QD",
      what: "The response queue, in flits. 16 is XPM's floor and buys 13 reads in flight; below that the read engine idles waiting on credit.",
    },
    {
      knob: "FLIT_WIDTH",
      what: "The skid, the forward path and the queue — every one of them is one flit wide. The store is not: its line is fixed at 256 bits.",
    },
  ],
};

const magKnobs = {
  cols: [
    { key: "knob", label: "Knob", mono: true },
    { key: "what", label: "What it moves" },
  ],
  rows: [
    {
      knob: "STAGE",
      what: "<b>Everything, at once.</b> 0 generates none of it — no store, no aperture claim, and the port is a straight wire.",
    },
    {
      knob: "STAGE_AT_PORT",
      what: "<b>Not a performance knob.</b> 1 puts one store on the converged path; 0 puts one in every port, at the same address, unreachable by the mover. See the trap below.",
      _tone: "bad",
    },
    {
      knob: "ENTRIES",
      what: "The store, linearly: <b>ENTRIES × 4 × 256 bits</b>. 16,384 is 2 MB and 64 URAM.",
    },
    {
      knob: "BANKS",
      what: "Almost no capacity — a bank shallower than 4,096 wastes URAM depth, so banking <i>multiplies</i> capacity rather than dividing it. What it moves is <b>placeability</b>: four banks fit around a die's URAM columns where one block does not.",
    },
    {
      knob: "PIPE",
      what: "One cycle of latency, and the worst data path in the module. 1 registers the dispatch per bank and each bank's output; as a single shared register that broadcast measured <b>4.860 ns at 98.4% route and ZERO logic levels</b>, so it carries a dont-touch and one copy per bank.",
      _tone: "warn",
    },
    {
      knob: "WORDS",
      what: "The line, and therefore the entry alignment. 4 words is a 1,024-bit line and a 128-byte entry — but only port A can read one whole.",
    },
  ],
};

/* ================================================== the straddle */
const straddleBroken = {
  rows: [
    {
      name: "descriptor",
      kind: "bus",
      values: ["addr=…FFF80", "len=3", null, null, null, null],
    },
    {
      name: "base compare",
      kind: "text",
      values: ["in window", "", "", "", "", ""],
    },
    {
      name: "line index",
      kind: "bus",
      values: [null, "8189", "8190", "8191", "0", "1"],
      mark: [4, 5],
    },
    {
      name: "served from",
      kind: "text",
      values: ["", "store", "store", "store", "store", "store"],
    },
    {
      name: "bytes returned",
      kind: "text",
      values: ["", "right", "right", "right", "WRONG", "WRONG"],
    },
  ],
  notes: [
    {
      cycle: 0,
      text: "The decode tests only that the start address is inside the window. It is.",
    },
    {
      cycle: 4,
      text: "The line index wrapped. Words 4 and 5 of this run are read out of lines 0 and 1 — whatever the last program left there. The response flits carry the right tags and the right count, so the unit reassembles a complete entry out of two other tensors.",
      tone: "bad",
    },
    {
      cycle: 5,
      text: "Nothing faults. The burst completes, the credit returns, and the only evidence is the arithmetic being wrong later.",
      tone: "bad",
    },
  ],
};

const straddleFixed = {
  rows: [
    {
      name: "descriptor",
      kind: "bus",
      values: ["addr=…FFF80", "len=3", null, null, null, null],
    },
    {
      name: "base compare",
      kind: "text",
      values: ["in window", "", "", "", "", ""],
    },
    {
      name: "end carry",
      kind: "bit",
      values: [1, 0, 0, 0, 0, 0],
      mark: [0],
    },
    {
      name: "i_fits",
      kind: "bit",
      values: [0, 0, 0, 0, 0, 0],
    },
    {
      name: "served from",
      kind: "text",
      values: ["forwarded", "", "", "", "", ""],
    },
  ],
  notes: [
    {
      cycle: 0,
      text: "The decode adds the run length to the line index and looks at the carry OUT of the index. It is set, so the run leaves the window and the whole descriptor is forwarded to the memory agent — unsplit.",
      tone: "good",
    },
    {
      cycle: 1,
      text: "Forwarded whole is the deliberate answer, and it is not free: the part of the run that WAS staged is now read from DRAM instead, so a straddling descriptor is correct and slow. The adapter says so on a simulation line; on hardware it is silent, which is why a compiler should tile to the window.",
      tone: "good",
    },
  ],
};

/* ================================================== the transform grant */
const grantBroken = {
  rows: [
    { name: "req", kind: "bit", values: [0, 1, 1, 1, 1, 1] },
    { name: "gnt", kind: "bit", values: [0, 0, 1, 1, 1, 1], mark: [2] },
    { name: "AR issued", kind: "bit", values: [1, 0, 0, 0, 0, 0], mark: [0] },
    {
      name: "R beats",
      kind: "bus",
      values: [null, "b0", "b1", "b2", "b3", null],
    },
    {
      name: "into the bank",
      kind: "text",
      values: ["", "DROPPED", "b1", "b2", "b3", ""],
      mark: [1],
    },
  ],
  notes: [
    {
      cycle: 0,
      text: "The requester issues its AR before it holds a grant. Read return is in order and pushed at line rate — the occupant's input is never handshaken — so the first beat arrives with nowhere to go.",
      tone: "bad",
    },
    {
      cycle: 1,
      text: "b0 is dropped. There is a simulation $display and no hardware signal: the occupant sees three beats where its IN_BITS says four, so it either never raises done — a hung move — or emits an entry built from the wrong window of the source.",
      tone: "bad",
    },
  ],
};

const grantFixed = {
  rows: [
    { name: "req", kind: "bit", values: [1, 1, 1, 1, 1, 0] },
    { name: "gnt", kind: "bit", values: [0, 1, 1, 1, 1, 1], mark: [1] },
    { name: "AR issued", kind: "bit", values: [0, 1, 0, 0, 0, 0], mark: [1] },
    {
      name: "R beats",
      kind: "bus",
      values: [null, null, "b0", "b1", "b2", "b3"],
    },
    {
      name: "into the bank",
      kind: "text",
      values: ["", "start", "b0", "b1", "b2", "b3"],
    },
  ],
  notes: [
    {
      cycle: 1,
      text: "The grant lands first, and the AR goes out under it. Grant is held for the WHOLE run, not per entry — a requester issues the next entry's read while the current entry is still inside the occupant, so its beats can land before it could re-acquire.",
      tone: "good",
    },
    {
      cycle: 5,
      text: "req falls at the end of the run and only then is the grant released. Reconfiguring an occupant mid-run is therefore unrepresentable, which is why an occupant may latch its registers at start and needs no further guard.",
      tone: "good",
    },
  ],
};

/* ================================================== bit layouts */
const addrFields = [
  { name: "aperture", bits: 1, value: "[39] · 1 = not DRAM", accent: true },
  { name: "rsvd", bits: 1, value: "[38] · MUST be 0" },
  { name: "mesh", bits: 2, value: "[37:36] · 0–3", accent: true },
  { name: "aperture id", bits: 4, value: "[35:32] · which one", accent: true },
  { name: "offset", bits: 32, value: "[31:0]" },
];

const stagedAddr = {
  cols: [
    { key: "f", label: "Field", mono: true },
    { key: "w", label: "Width", align: "right" },
    { key: "p", label: "Position", mono: true },
    { key: "o", label: "Owner" },
  ],
  rows: [
    {
      f: "aperture",
      w: "1",
      p: "addr[39]",
      o: "framework — <span class='chip'>ship</span> fixes it",
    },
    { f: "reserved", w: "1", p: "addr[38]", o: "framework" },
    {
      f: "mesh",
      w: "2",
      p: "addr[37:36]",
      o: "framework. Tested <b>first</b>, before the aperture id",
    },
    {
      f: "aperture id",
      w: "4",
      p: "addr[35:32]",
      o: "framework — <span class='chip'>AP_STAGE</span> is 0",
    },
    {
      f: "entry index",
      w: "rest",
      p: "addr[31:7]",
      o: "you. One entry is 128 bytes, so <b>addr[6:0] MUST be zero</b>",
    },
    {
      f: "bank select",
      w: "2",
      p: "addr[8:7]",
      o: "framework — <b>inside</b> the entry index, so a sequential fill spreads across banks rather than loading one",
    },
  ],
};

const apertures = {
  cols: [
    { key: "id", label: "addr[35:32]", mono: true },
    { key: "n", label: "Name" },
    { key: "s", label: "Status" },
  ],
  rows: [
    {
      id: "0x0",
      n: "<b>staging</b>",
      s: "Built. <span class='chip'>mag_stage</span> claims it, and it is the only one a memory port serves.",
      _tone: "good",
    },
    {
      id: "0x1, 0x2",
      n: "architecturally defined",
      s: "In <span class='chip'>AP_IMPL</span>, so an access does <b>not</b> fault — and no memory port answers one, so it hangs instead.",
      _tone: "warn",
    },
    {
      id: "0x3",
      n: "—",
      s: "Not in <span class='chip'>AP_IMPL</span>. <b>Faults</b>, rather than aliasing onto DRAM.",
    },
    {
      id: "0x4, 0x5",
      n: "architecturally defined",
      s: "In <span class='chip'>AP_IMPL</span>. Same as 0x1 and 0x2.",
      _tone: "warn",
    },
    {
      id: "0x6–0xF",
      n: "unallocated",
      s: "<b>Faults.</b> <span class='chip'>AP_IMPL</span> is <span class='chip'>16'h0037</span> and that is the whole of it.",
    },
  ],
};

const windowFields = [
  { name: "window tag", bits: 22, value: "[39:18] · compared", accent: true },
  { name: "line index", bits: 13, value: "[17:5] · 8,192 lines" },
  { name: "byte in line", bits: 5, value: "[4:0] · MUST be 0" },
];

const windowSpec = {
  cols: [
    { key: "f", label: "Field", mono: true },
    { key: "w", label: "Width", align: "right" },
    { key: "p", label: "Position", mono: true },
    { key: "o", label: "Owner" },
  ],
  rows: [
    {
      f: "window tag",
      w: "40 − L2_BITS",
      p: "addr[39 -: 40-L2_BITS]",
      o: "software, over CU_CTRL index 1. Carries the mesh id, which is why the adapter never decodes a mesh field of its own",
    },
    {
      f: "line index",
      w: "$clog2(DEPTH)",
      p: "addr[5 +: $clog2(DEPTH)]",
      o: "the adapter",
    },
    {
      f: "byte in line",
      w: "5",
      p: "addr[4:0]",
      o: "nobody. A line is 32 bytes and the flit payload is 32 bytes",
    },
  ],
};

const ctrlIdx = {
  cols: [
    { key: "i", label: "index", mono: true },
    { key: "r", label: "Read", mono: true },
    { key: "w", label: "Write" },
  ],
  rows: [
    {
      i: "0xE0",
      r: "{16'h0002, 8'd1, L2_BITS, DEPTH}",
      w: "ignored — capability, so a driver discovers the geometry rather than being told it",
    },
    {
      i: "0xE1",
      r: "{24'd0, l2_base}",
      w: "<b>sets the window base.</b> The mesh id rides in it, so a store is programmed rather than compiled in",
      _tone: "good",
    },
    {
      i: "0xE2",
      r: "{63'd0, l2_en}",
      w: "<b>enable.</b> Zero at reset — an adapter nobody configured claims no address",
      _tone: "good",
    },
    {
      i: "0xE3",
      r: "{n_serv, n_fwd}",
      w: "ignored. Two 32-bit counters: responses served here, and requests in range that were forwarded anyway",
    },
    {
      i: "0xE4–0xE7",
      r: "<b>zero</b>",
      w: "ignored. Allocated to the slot and unimplemented — the window is 8 indices wide because it is 8-aligned, not because 8 are used",
      _tone: "warn",
    },
  ],
};

/* ================================================== transform */
const xfPos = {
  nodes: [
    {
      id: "mem",
      x: 0,
      y: 3,
      w: 9,
      h: 14,
      label: "memory",
      sub: "DRAM, or staging",
      accent: true,
    },
    {
      id: "r",
      x: 12,
      y: 3,
      w: 8.5,
      h: 5.5,
      label: "R channel",
      sub: "in order, line rate",
    },
    {
      id: "b1",
      x: 23.5,
      y: 3,
      w: 8.5,
      h: 5.5,
      label: "beat register",
      sub: "the agent's, always",
    },
    {
      id: "bank",
      x: 35,
      y: 3,
      w: 10,
      h: 5.5,
      label: "xform_bank",
      sub: "IN_BITS in · OUT_WORDS out",
      accent: true,
    },
    {
      id: "b2",
      x: 48,
      y: 3,
      w: 8.5,
      h: 5.5,
      label: "mux register",
      sub: "one cycle per ENTRY",
    },
    {
      id: "fifo",
      x: 59.5,
      y: 3,
      w: 9,
      h: 5.5,
      label: "mover FIFO",
      sub: "holds converted words",
      accent: true,
    },
    {
      id: "wb",
      x: 71.5,
      y: 3,
      w: 9,
      h: 5.5,
      label: "AW / W",
      sub: "back to memory",
    },
    {
      id: "mp",
      x: 23.5,
      y: 12,
      w: 9,
      h: 5,
      label: "memory port",
      sub: "no slot here",
    },
    {
      id: "mesh",
      x: 36,
      y: 12,
      w: 8,
      h: 5,
      label: "mesh",
    },
    {
      id: "cu",
      x: 47,
      y: 12,
      w: 10,
      h: 5,
      label: "compute unit",
      sub: "final format already",
      accent: true,
    },
  ],
  edges: [
    { from: "mem:r", to: "r:l", dir: "h", accent: true, label: "mode 5" },
    { from: "r:r", to: "b1:l", dir: "h", accent: true },
    { from: "b1:r", to: "bank:l", dir: "h", accent: true },
    { from: "bank:r", to: "b2:l", dir: "h", accent: true },
    { from: "b2:r", to: "fifo:l", dir: "h", accent: true },
    { from: "fifo:r", to: "wb:l", dir: "h", accent: true },
    { from: "mem:r", to: "mp:l", dir: "h", label: "a unit's fetch" },
    { from: "mp:r", to: "mesh:l", dir: "h" },
    { from: "mesh:r", to: "cu:l", dir: "h" },
  ],
};

const xfSel = [
  { name: "rsvd", bits: 5, value: "[63:59]" },
  { name: "XFORM_MODE", bits: 4, value: "[58:55] · occupant only", accent: true },
  { name: "rsvd", bits: 4, value: "[54:51]" },
  { name: "XFORM_ID", bits: 4, value: "[50:47] · the agent", accent: true },
  { name: "walker header", bits: 47, value: "[46:0] · addressing" },
];

const xfFields = {
  cols: [
    { key: "f", label: "Field", mono: true },
    { key: "w", label: "Width", align: "right" },
    { key: "p", label: "Position", mono: true },
    { key: "o", label: "Owner" },
  ],
  rows: [
    {
      f: "XFORM_ID",
      w: "ID_W",
      p: "reg 0x10 [50:47]",
      o: "<b>the agent</b> — it demuxes to one occupant. 0 is bypass",
    },
    {
      f: "XFORM_MODE",
      w: "MODE_W",
      p: "reg 0x10 [58:55]",
      o: "<b>the occupant only.</b> The framework carries it and never reads it, which is why nothing here is named after a number format",
    },
    {
      f: "IN_BITS",
      w: "parameter",
      p: "elaboration",
      o: "the occupant declares; <b>the agent's address arithmetic consumes it</b> before the occupant runs",
    },
    {
      f: "OUT_WORDS",
      w: "parameter",
      p: "elaboration",
      o: "the occupant declares, <b>at most 4</b> — the port list is word0..word3 and that is the ceiling",
    },
  ],
};

const xfIds = {
  cols: [
    { key: "v", label: "XFORM_ID", mono: true },
    { key: "m", label: "Occupant" },
    { key: "s", label: "Status" },
  ],
  rows: [
    {
      v: "0",
      m: "bypass",
      s: "Built, and it obeys the same shape rule — four beats in, four words out — so naming 0 gets the same geometry as naming anything else.",
      _tone: "good",
    },
    {
      v: "1",
      m: "the project's occupant",
      s: "Built. <span class='chip'>IN_BITS = 2048</span>, <span class='chip'>OUT_WORDS = 4</span> — eight source beats in, four words out, the 2:1 ratio the mover sizes a converting move from.",
      _tone: "good",
    },
    {
      v: "2 and up",
      m: "unallocated",
      s: "<span class='chip'>SLOTS</span> is 1 in every build in the tree. An id past the last occupant is <b>answered by the bypass path</b> and raises the bank's <span class='chip'>fault[0]</span>.",
      _tone: "warn",
    },
  ],
};

const bankRegs = {
  cols: [
    { key: "o", label: "offset", mono: true },
    { key: "r", label: "Read", mono: true },
    { key: "w", label: "Write" },
  ],
  rows: [
    {
      o: "0x00",
      r: "{28'd0, fault}",
      w: "any write clears the sticky fault",
    },
    {
      o: "0x04",
      r: "{8'd0, OUT_WORDS, IN_BITS} of cfg_id",
      w: "—. Zero if that id names no occupant, which is how a driver discovers a slot rather than being told",
    },
    {
      o: "0x08 and up",
      r: "the occupant's own",
      w: "the occupant's own. <b>The shipping occupant has none</b> — its mode picks its packing and its scale is derived per entry, and that a complete occupant needs zero registers is what keeps them optional",
    },
  ],
};

/* ================================================== cost */
const xform = {
  cols: [
    { key: "c", label: "Config" },
    { key: "lut", label: "LUT", align: "right", mono: true },
    { key: "dsp", label: "DSP48", align: "right", mono: true },
    { key: "wns", label: "WNS", align: "right", mono: true },
  ],
  rows: [
    {
      c: "1 port, transform in the memory port",
      lut: "25,773",
      dsp: "67",
      wns: "+0.096",
    },
    {
      c: "1 port, one shared slot",
      lut: "<b>22,091</b>",
      dsp: "<b>35</b>",
      wns: "+0.096",
      _tone: "good",
    },
    {
      c: "2 ports, transform in the memory port",
      lut: "36,733",
      dsp: "99",
      wns: "+0.088",
    },
    {
      c: "2 ports, one shared slot",
      lut: "<b>28,243</b>",
      dsp: "<b>35</b>",
      wns: "+0.096",
      _tone: "good",
    },
    {
      c: "2 ports + control processor, transform in the memory port",
      lut: "39,886",
      dsp: "99",
      wns: "<b>−0.372</b>",
      _tone: "bad",
    },
    {
      c: "2 ports + control processor, one shared slot",
      lut: "<b>31,277</b>",
      dsp: "<b>35</b>",
      wns: "<b>+0.096</b>",
      _tone: "good",
    },
  ],
};

const capacity = {
  cols: [
    { key: "w", label: "What" },
    { key: "f", label: "Arithmetic", mono: true },
    { key: "v", label: "At the shipped values" },
  ],
  rows: [
    {
      w: "adapter store",
      f: "DEPTH × 256 b",
      v: "8,192 × 256 b = <b>256 KB</b>",
    },
    {
      w: "adapter URAM",
      f: "ceil(DEPTH × 256 / 294,912)",
      v: "<b>8 URAM288</b> — 4 per 4,096 lines at 256 bits",
    },
    {
      w: "adapter window",
      f: "2^L2_BITS, L2_BITS = 5 + log2(DEPTH)",
      v: "2^18 = <b>256 KB</b> — the same number, and it MUST be",
    },
    {
      w: "agent store",
      f: "ENTRIES × WORDS × 256 b",
      v: "16,384 × 4 × 256 b = <b>2 MB</b>",
    },
    {
      w: "agent URAM",
      f: "BANKS × WORDS × ceil(256/72) × (ROWS/4096)",
      v: "4 × 4 × 4 × 1 = <b>64 URAM288</b>",
    },
    {
      w: "agent entry",
      f: "WORDS × 256 b",
      v: "<b>128 bytes</b>, and an access MUST be aligned to it",
    },
    {
      w: "agent URAM, <i>per-port</i> form",
      f: "PORTS × the row above",
      v: "128 at two ports, 256 at four — <b>all of it for one 2 MB address range</b>",
      _tone: "bad",
    },
  ],
};

const tiers = {
  cols: [
    { key: "t", label: "Tier" },
    { key: "what", label: "What it is" },
    { key: "reach", label: "Reached by" },
    { key: "state", label: "State" },
  ],
  rows: [
    {
      t: "<b>DRAM</b>",
      what: "off-chip, behind one AXI master per mesh",
      reach: "an ordinary address",
      state: "shipping",
    },
    {
      t: "<b>Staging</b> — <span class='chip'>mag_stage</span>",
      what: "on-chip memory at a reserved aperture inside the system node. 2 MB at 4 banks",
      reach:
        "an ordinary address in the aperture — <b>no tags, no lookup, never a miss</b>",
      state: "shipping",
      _tone: "good",
    },
    {
      t: "<b>Endpoint adapter</b> — <span class='chip'>noc_l2_adapter</span>",
      what: "a store spliced into one unit's local link, so a unit's traffic can be served without touching the router or the unit",
      reach:
        "an address in a window a <span class='chip'>CU_CTRL</span> write programmed",
      state: "shipping, control plane verified on silicon",
    },
    {
      t: "<b>Transform slot</b> — <span class='chip'>mag_xform</span>",
      what: "a conversion stage on the memory mover's read-return path, one bank per agent",
      reach: "mover descriptor mode 5, occupant chosen by an id",
      state: "shipping",
    },
    {
      t: "<b>Tagged L2</b>",
      what: "an actual cache — tags, hits, misses, a coherence story",
      reach: "an address, with the tier deciding whether it goes further",
      state: "<b>designed, not built</b>",
      _tone: "bad",
    },
  ],
};

const shared = {
  cols: [
    { key: "q", label: "What a shared cache would give" },
    { key: "a", label: "What this machine does instead" },
  ],
  rows: [
    {
      q: "<b>one fetch serving many consumers</b> — the broadcast a shared L2 exists to provide",
      a: "<b>shared fetch.</b> One descriptor names up to three other units consuming the same operand, the lowest-numbered issues it, and the memory agent delivers to all of them. Compiler knowledge instead of arbitration and coherence",
      _tone: "good",
    },
    {
      q: "<b>a working set that survives across passes</b>",
      a: "<b>staging.</b> A reserved aperture, addressed directly. It never misses, so nothing has to decide what to evict",
    },
    {
      q: "<b>a format the consumer wants rather than the one memory holds</b>",
      a: "<b>the transform slot.</b> The mover converts once per tensor into staging or back into memory; a fetch is never transformed",
    },
  ],
};

const procedure = {
  cols: [
    { key: "n", label: "#", mono: true, align: "center" },
    { key: "s", label: "Step" },
  ],
  rows: [
    {
      n: "1",
      s: "<b>Decide reach before capacity.</b> A pass's working set is a few hundred kilobytes and the part has URAM at 9.38% used, so it fits. What is scarce is one central block's ability to reach URAM columns spread across a die whose worst region is at 95.49% CLB.",
    },
    {
      n: "2",
      s: "<b>Pick the store by who needs it.</b> One unit reusing its own operands wants an adapter in its link. Anything the mover or the interlink must reach wants the agent's store, because an adapter can only answer the node behind it.",
    },
    {
      n: "3",
      s: "<b>Size DEPTH, then derive L2_BITS.</b> <span class='chip'>5 + $clog2(DEPTH)</span>, never chosen independently.",
    },
    {
      n: "4",
      s: "<b>Give each adapter its own base.</b> Two adapters on one window is two different stores answering one address.",
    },
    {
      n: "5",
      s: "<b>Generate with the flags you actually want</b> — <span class='chip'>--l2-mag</span>, <span class='chip'>--l2-cu</span>, <span class='chip'>--l2-vec</span> are independent and none implies another, because the agent's store is reached by address and the adapters by instruction.",
    },
    {
      n: "6",
      s: "<b>Write the base, then the enable, then anything else.</b> An adapter is disabled at reset and claims nothing until both are written.",
    },
    {
      n: "7",
      s: "<b>Read index 0xE3 after a run.</b> <span class='chip'>n_serv</span> against <span class='chip'>n_fwd</span> is the only witness that the store was reached at all.",
    },
  ],
};

const conventions = {
  cols: [
    { key: "c", label: "Convention" },
    { key: "f", label: "Forced?" },
    { key: "w", label: "What breaks if you deviate" },
  ],
  rows: [
    {
      c: "<b>Tile a descriptor to the staging window.</b>",
      f: "<b>Forced in effect</b>",
      w: "A run that leaves the window is forwarded whole and read from DRAM, including the part that was staged. It is correct and it is slower than not staging at all, and on hardware nothing says so.",
      _tone: "warn",
    },
    {
      c: "<b>Convert once per tensor, never once per read.</b>",
      f: "<b>Forced</b>",
      w: "There is no transform on the fetch path to deviate onto. A requester that still sets the retired request flags gets an untransformed fetch, which is the correct answer.",
    },
    {
      c: "<b>Read the bank's geometry register before sizing a move.</b>",
      f: "Free, and the one most worth obeying",
      w: "The mover's address arithmetic needs IN_BITS and OUT_WORDS before the occupant runs. Assuming a ratio that the resident occupant does not have sizes the read wrong, and a wrongly sized converting move is a partially written destination.",
    },
    {
      c: "<b>Give an occupant no registers if you can.</b>",
      f: "Free",
      w: "Nothing, on the RV32 control complex. On the RV64 one the register port is tied off, so an occupant that needs configuration cannot be driven there at all.",
      _tone: "warn",
    },
    {
      c: "<b>Keep the empty state free.</b>",
      f: "Free",
      w: "<span class='chip'>PASS=1</span> on the adapter and the template bank on the slot both generate a pass-through. A slot whose empty state costs nothing is a slot people leave in; one that costs something is one they delete, and then it is not a slot.",
    },
  ],
};

const categories = {
  cols: [
    { key: "thing", label: "Thing" },
    { key: "cat", label: "Category" },
  ],
  rows: [
    {
      thing: "the aperture bit, the mesh field and the aperture id",
      cat: "<b>fixed protocol.</b> A component that decodes differently is not on the framework",
    },
    {
      thing: "where the transform slot sits, how an occupant is selected, its port list and the three hard rules",
      cat: "<b>fixed protocol</b>",
    },
    {
      thing: "the adapter's six-signal faces, and that a CU_CTRL flit in its window is terminated there",
      cat: "<b>fixed protocol</b>",
    },
    {
      thing: "<b>what the occupant computes</b>",
      cat: "<b>customizable addon.</b> The framework carries its mode bits without reading them",
    },
    {
      thing: "<b>the adapter itself</b> — present, absent, or present and disabled",
      cat: "<b>customizable addon</b> — the same six signals on both faces, so a pass-through is a straight wire",
    },
    {
      thing: "DEPTH, QD, BANKS, ENTRIES, PIPE, and where the window base points",
      cat: "<b>customizable</b> — sized per instance",
    },
    {
      thing: "which addresses you stage, and when",
      cat: "<b>yours</b>, entirely — this is a compiler decision and the hardware has no opinion",
    },
    {
      thing: "what a staged region means",
      cat: "<b>yours</b>",
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
      n: "the flit types a staged request arrives as, and the tags a response carries",
      w: "noc. The adapter reuses them and adds nothing",
    },
    {
      n: "the descriptor walk, the write slots, the AXI bursts",
      w: "sysnode",
    },
    {
      n: "the address map the aperture bit lives in",
      w: "ship",
    },
    {
      n: "which URAM columns a store lands on",
      w: "physical. That it can be placed near them is the whole argument for the adapter",
    },
    {
      n: "the number format an occupant converts between",
      w: "the project. Nothing in the framework is named after one",
    },
    {
      n: "deciding what to evict",
      w: "<b>nobody. There is nothing to evict</b> — none of the three built tiers has a replacement policy, because none of them has tags",
    },
  ],
};
</script>

<template>
  <DocPage
    title="Staging, the transform slot and the tagged L2"
    summary="Three answers to reuse, none of which is a cache — and the one that is a cache is not built. Where each store sits, how it decodes an address, what it costs, and what a tagged tier would have to add beyond shared fetch to be worth building."
    domain="cpu"
    status="measured"
    source="src/kohakuaccel/noc/endpoint/noc_l2_adapter.v · src/kohakuaccel/sysnode/core/ · docs/notes/cache/ · docs/spec/transform-slot.md"
  >
    <h2 class="doc-h2">What it owns</h2>
    <p class="doc-p">Three built things, and one that is not.</p>
    <div class="grid gap-3 sm:grid-cols-2 my-5">
      <div class="card p-4">
        <div
          class="kt-text-caption font-semibold text-warm-800 dark:text-warm-200 mb-1"
        >
          The staging aperture
        </div>
        <p class="kt-text-caption text-warm-500 dark:text-warm-400 leading-6">
          On-chip memory inside the system node, reached by
          <b>an ordinary address</b> with one bit set. No tags, no lookup, no
          miss path.
        </p>
      </div>
      <div class="card p-4">
        <div
          class="kt-text-caption font-semibold text-warm-800 dark:text-warm-200 mb-1"
        >
          The endpoint adapter
        </div>
        <p class="kt-text-caption text-warm-500 dark:text-warm-400 leading-6">
          The same idea spliced into one unit's local link, with a window
          software programs. It serves the node behind it and nothing else.
        </p>
      </div>
      <div class="card p-4">
        <div
          class="kt-text-caption font-semibold text-warm-800 dark:text-warm-200 mb-1"
        >
          The transform slot
        </div>
        <p class="kt-text-caption text-warm-500 dark:text-warm-400 leading-6">
          One conversion bank per agent, on the memory mover's read-return path.
          A fixed interface with an occupant you write.
        </p>
      </div>
      <div class="card p-4">
        <div
          class="kt-text-caption font-semibold text-warm-800 dark:text-warm-200 mb-1"
        >
          Shared fetch
        </div>
        <p class="kt-text-caption text-warm-500 dark:text-warm-400 leading-6">
          Not a store at all: one descriptor naming several consumers, so one
          read serves all of them. It is what a cache's broadcast is for.
        </p>
      </div>
    </div>

    <p class="doc-p">
      The rejected alternative is the obvious one. A cache spends tags and
      comparators discovering an access pattern at run time — and a matrix sweep
      walks a nest of loops over addresses the compiler already computed, so
      what a cache would discover is written down before the machine starts. The
      cost of refusing it is that <b>reuse becomes a compiler obligation</b>:
      nothing in this machine will notice that you fetched the same tile twice,
      and nothing will fix it.
    </p>

    <Callout
      kind="rule"
      title="Any caching proposal has to say what it adds beyond shared fetch"
    >
      <p>
        That is the bar, and the interesting part is that one candidate clears
        it and the rest do not. Bandwidth is not an answer, because shared fetch
        already delivers one read to several consumers. The answer that works is
        on the last section of this page, and it is not about hit rate.
      </p>
      <p>
        One caveat on "already true": the shared-fetch mechanism is decoded by
        the hardware and <b>the driver does not set it</b>, because a follower
        cannot yet tell which fill an arriving entry belongs to. The mechanism
        exists and is one rendezvous away from being usable; the traffic
        reduction is not being measured today.
      </p>
    </Callout>

    <SpecTable :cols="tiers.cols" :rows="tiers.rows" />

    <h2 class="doc-h2">Where a read is answered</h2>
    <Fig
      caption="One request, and the three places it can stop. The adapter decides first, because it is in the link before the router; the memory port decides second, on the aperture bit. Responses travel the same wires backwards and carry their own {entry, word} tags, so nothing here has to preserve order — which is exactly what lets a store serve some words while others are still coming back from DRAM."
      zoom
      wide
    >
      <BlockDiagram :nodes="tiersDia.nodes" :edges="tiersDia.edges" />
    </Fig>

    <SpecTable
      :cols="magKnobs.cols"
      :rows="magKnobs.rows"
      caption="The knobs that move the agent store, in the order they matter. The second one is not like the others."
    />

    <Callout
      kind="trap"
      title="The store the shipped flag builds reads ONE 256-bit word at a time, not one entry"
    >
      <p>
        <span class="chip">mag_stage</span> has two ports. Port A is entry-wide
        — a whole 1,024-bit line per access, which is the entire argument for
        making the line that wide. Port B is one word per access.
        <b
          >In the configuration <span class="chip">--l2-mag</span> generates,
          port A is tied to zero.</b
        >
        <span class="chip">mag_stage_port</span> drives
        <span class="chip">a_req(1'b0)</span>, leaves every port-A output
        unconnected, and reaches the store only through port B — with
        <b>one read outstanding</b>, because it holds one word and a second
        request would drop the first.
      </p>
      <p>
        <b>Say the cost out loud, because it is real.</b> One 256-bit word per
        access against a 1,024-bit line is a fourfold narrowing of the read
        path, and one outstanding read on top of it means the store's fixed
        return latency is not hidden by anything. Believing the wide figure
        gives a bandwidth estimate four times too high, and it does not fail —
        a staged pass is simply slower than the arithmetic said, with nothing
        reporting why.
      </p>
      <p>
        What it buys is the other half, and it is why the trade was taken:
        <b>reach.</b> On the converged path the store sits where every requester
        has already met, so the memory mover and the interlink can reach it —
        and the URAM bill is 64 rather than <span class="chip">PORTS</span> ×
        64. The wide port is not lost so much as parked: it belongs to the
        per-port placement, which is the one nothing else can address. Read the
        width argument in <span class="chip">mag-staging.md</span> as describing
        the configuration that is not selected.
      </p>
    </Callout>

    <Callout
      kind="trap"
      title="STAGE_AT_PORT is not a tuning knob — at 0, every port owns a store at the SAME address"
    >
      <p>
        The aperture decode inside a memory port is mesh plus
        <span class="chip">addr[35:32] == 0</span> and
        <b>carries no port index</b>. So with a store at each port, every port
        claims the same 2 MB window and answers it from its own URAM. A tensor
        staged through one port is <b>invisible through any other</b>, and the
        unit that reads it back gets whatever its own port's store happens to
        hold — the same failure as two endpoint adapters sharing one window
        base, one level up.
      </p>
      <p>
        The arithmetic makes it worse rather than better. At the shipping two
        ports that is <b>128 URAM — 4 MB of storage buying one 2 MB address
        range</b>, and at four ports 256 URAM buying the same 2 MB. Then the
        RTL's own comment names the rest: those per-port copies are
        <b>unreachable by the mover and the interlink</b>, which are the two
        things that put bytes into staging in the first place.
      </p>
      <p>
        So this is a parameter with one defensible value.
        <span class="chip">--l2-mag</span> sets it to 1, and the reason to know
        the other setting exists is that the width argument in the notes was
        written against it.
      </p>
    </Callout>

    <Callout kind="trap" title="The host cannot reach staging, and gets no error">
      <p>
        A host master's access with <span class="chip">addr[39]</span> set does
        not reach the staging store: the host arrives as its own requester on
        the agent's converged path, and that path forwards the full 40-bit
        address to <span class="chip">M_AXI_DRAM</span>. The read lands above 4
        GB of DDR4, where nothing answers, and
        <b>presents as a hang rather than a fault</b>. A host that wants bytes
        in staging asks the control processor to move them.
      </p>
      <p>
        The same asymmetry covers the mesh field. Both are the mover's to issue,
        and <span class="chip">docs/notes/cache/mag-staging.md</span> §1's claim
        that "host access is free" describes the intent rather than the wiring.
      </p>
    </Callout>

    <h2 class="doc-h2">What the adapter costs</h2>
    <p class="doc-p">
      It is a store, a skid, a queue and a decode. The registers below are the
      cost: everything past the skid runs off registered values, which is what
      lets the module sit in a link without a path running input to output
      through it.
    </p>

    <Fig
      caption="One local link, cut in half. The endpoint face is registered and the window is decoded AT CAPTURE, so the skid holds a flit and the verdict about it together — measured at 284 MHz. The store is a simple dual-port with READ_LAT 2, and with the launch register that is three stages between deciding to read a line and having its data beside its header, which is what the queue credit has to cover."
      zoom
      wide
    >
      <BlockDiagram :nodes="adapter.nodes" :edges="adapter.edges" />
    </Fig>

    <SpecTable
      :cols="adapterKnobs.cols"
      :rows="adapterKnobs.rows"
      caption="The knobs that move adapter cost, in the order they matter."
    />

    <Callout
      kind="trap"
      title="L2_BITS is derived, not chosen — 5 + $clog2(DEPTH), always"
    >
      <p>
        The window compare is
        <span class="chip">addr[39:L2_BITS] == base[39:L2_BITS]</span> and the
        line index is <span class="chip">addr[5 +: $clog2(DEPTH)]</span>. Those
        two are the same split of the same address, so if
        <span class="chip">L2_BITS</span> is larger than the store, part of the
        window aliases onto lines that already hold something else; if it is
        smaller, the top of the store is unaddressable. Neither elaborates
        differently and neither faults.
      </p>
      <p>
        The related decision is that <b>DEPTH must be a power of two</b>, and it
        is load-bearing rather than tidy: leaving the window is then exactly the
        carry out of the line index, which is one bit rather than a comparison.
      </p>
    </Callout>

    <Callout kind="note" title="Why the line is 256 bits here and 1,024 in the agent">
      <p>
        A <span class="chip">MEM_RD_RESP</span> payload <i>is</i> 256 bits and a
        local port sends one flit per cycle, so a wider line in the adapter adds
        a mux and buys nothing — there is nowhere for the extra bits to go. The
        agent's store is on a fill path that is not limited to one flit per
        cycle, so one read yielding one whole entry is the natural unit there.
      </p>
      <p>
        <b>Wide only pays where the consumer is wide.</b> The width argument
        exists on one side of the machine and not the other, and copying the
        agent's answer into the adapter would be a mux nobody can use.
      </p>
    </Callout>

    <h2 class="doc-h2">The window, and what a run that leaves it does</h2>
    <Callout kind="rule" title="A run is served only if the WHOLE run fits">
      <p>
        A staged request is claimed only when the base is in the window
        <b>and</b> the run's last line is still inside it. A run that starts
        inside and ends outside <b>MUST</b> be forwarded whole — not split, not
        clamped, not wrapped. A read asking for extra multicast destinations
        <b>MUST</b> also be forwarded, because an adapter can only answer the
        node behind it and the other destinations are elsewhere on the mesh.
      </p>
    </Callout>

    <WaveTrace
      :rows="straddleBroken.rows"
      :notes="straddleBroken.notes"
      variant="broken"
      label="Decode the base only — the line index wraps and the bytes are someone else's"
    />
    <WaveTrace
      :rows="straddleFixed.rows"
      :notes="straddleFixed.notes"
      variant="fixed"
      label="Test the end as well — the carry out of the line index is the whole test"
    />

    <Callout
      kind="trap"
      title="Three carries in parallel, because forming cnt × ew first costs 11 logic levels"
    >
      <p>
        A streaming fetch covers
        <span class="chip">count × entry_words</span> lines, and the obvious
        implementation multiplies and then adds. That puts a mux
        <i>in front of</i> the adder:
        <b>11 logic levels and −1.627 ns</b>. What is built computes
        <span class="chip">×1</span>, <span class="chip">×2</span> and
        <span class="chip">×4</span> as three independent adds and muxes the
        carries at the end — the mux is then behind the adder, on one bit each.
      </p>
      <p>
        Two deliberate imprecisions ride along.
        <span class="chip">entry_words = 3</span> is priced at 4, so a 3-word
        run near the top of the window is forwarded when it would have fitted —
        conservative by one entry, never wrong. And the top line is a guard that
        is never served, because <span class="chip">end &lt; DEPTH</span> is one
        carry bit where <span class="chip">end &lt;= DEPTH</span> needs an
        OR-reduce after it.
      </p>
    </Callout>

    <h2 class="doc-h2">The bit-exact layouts</h2>
    <h3 class="doc-h3">A staged address, in the agent</h3>
    <BitField
      :fields="addrFields"
      caption="The 40-bit address a unit issues. Aperture 0 is staging; the aperture id is a field of its own, so an unimplemented aperture is a fault rather than an alias onto DRAM"
    />
    <SpecTable :cols="stagedAddr.cols" :rows="stagedAddr.rows" />

    <Callout kind="trap" title="Mesh first, then aperture — and the order is what makes transit safe">
      <p>
        The decode tests the mesh field <b>before</b> the aperture. A packet
        merely transiting this mesh fails the mesh test, so it is neither ours
        nor a fault and passes on untouched. Testing the aperture first would
        make another mesh's staged address fault here — a correct program
        killed by a node it was only passing.
      </p>
      <p>
        An unaligned entry access is the fault this decode
        <i>cannot</i> catch. An entry is 128 bytes and
        <span class="chip">addr[6:0]</span> must be zero; a misaligned access
        reads a line the address does not name, and
        <b>the only symptom is that the data is someone else's</b>. There is a
        simulation check and no hardware one.
      </p>
    </Callout>

    <SpecTable
      :cols="apertures.cols"
      :rows="apertures.rows"
      caption="AP_IMPL is what the ARCHITECTURE defines, not what any port serves — a memory port serves aperture 0 alone. So the middle rows are the dangerous ones: defined, therefore not faulted, and answered by nothing."
    />

    <h3 class="doc-h3">A staged address, in an adapter</h3>
    <BitField
      :fields="windowFields"
      caption="The same 40 bits, split at L2_BITS. At the shipped DEPTH of 8,192 the split is [39:18] against [17:5] — and the mesh id sits inside the compared tag, which is why the adapter has no mesh decode of its own"
    />
    <SpecTable :cols="windowSpec.cols" :rows="windowSpec.rows" />

    <SpecTable
      :cols="ctrlIdx.cols"
      :rows="ctrlIdx.rows"
      caption="The adapter answers CU_CTRL in an 8-aligned index window and the reply is field for field the one noc_cu_base would have sent, so a controller cannot tell an addon's register from a unit's. The flit is TERMINATED here — dropping a claimed CU_CTRL is the termination mechanism, and no new type was spent on it."
    />

    <Callout
      kind="trap"
      title="Two adapters MUST NOT share a window base"
    >
      <p>
        An adapter snoops only the outbound flits of the endpoint behind it, so
        two adapters programmed to the same base are
        <b>two different stores answering one address</b> with different
        contents — whichever unit issues the read gets its own store's copy, and
        a value written through one is invisible through the other. The
        generator gives the vector adapter
        <span class="chip">0x00_F100_0000</span> against the cluster's
        <span class="chip">0x00_F000_0000</span> for that reason.
      </p>
      <p>
        This is also the limit of the in-link form, and it is a property of the
        position rather than a defect to be fixed:
        <b>an adapter cannot move data between compute units</b>, which is what
        a shared store would be for. Anything the mover or the interlink has to
        reach belongs in the agent's store instead.
      </p>
    </Callout>

    <h2 class="doc-h2">The transform slot</h2>
    <p class="doc-p">
      The slot converts between the format memory holds and the format a unit
      wants. It used to sit in <b>every memory port</b>, on the theory that a
      mesh buys conversion throughput by having more ports. That theory was
      wrong, and structurally so rather than by workload.
    </p>

    <Fig
      caption="One bank per agent, on the read-return path between R and the mover's FIFO — so the FIFO holds converted words rather than source ones. The lower lane is a compute unit's fetch, which is never transformed: operands arrive already in their final format, whether the host wrote them that way or the mover converted them in place. The two register stages are part of the contract, not an implementation choice."
      zoom
      wide
    >
      <BlockDiagram :nodes="xfPos.nodes" :edges="xfPos.edges" />
    </Fig>

    <Callout
      kind="trap"
      title="N transforms could only ever consume one beat per cycle between them"
    >
      <p>
        A per-port transform is fed from <b>that port's AXI R channel</b>. The
        system node's single-master concentrator converges
        <b>every port master onto one DRAM master</b>. And a staged read never
        transforms, because staging holds operand words verbatim. So every
        transformed byte came from one converged master, and
        <b>N−1 instances were idle by construction</b> — not because the
        workload failed to exercise them.
      </p>
      <p>Each instance measured <b>4,490 LUT and 32 DSP</b>.</p>
    </Callout>

    <Callout
      kind="rule"
      title="A bench that asserts completion cannot see hardware that was never reached"
    >
      <p>
        The mesh bench for this path set up a transformed fetch and then checked
        <i>"the fill completed"</i>. It passes identically with N transform
        instances and with one, because a completion says the request was
        served, not <b>which hardware served it</b>. Anything whose failure mode
        is "a second copy is idle" needs a witness that counts the copies — a
        per-instance activity counter, or a build that removes them and is
        expected to be bit-identical.
      </p>
    </Callout>

    <SpecTable
      :cols="xform.cols"
      :rows="xform.rows"
      caption="Out-of-context synthesis of sysnode on xcvu13p-fhgb2104-2L-e, Vivado 2024.2, 3.333 ns ask. Marginal port cost falls from 10,960 LUT + 32 DSP to 6,152 LUT and no DSP, which is what makes a sysnode with more than two ports affordable"
    />

    <Callout kind="measured" title="The transform was also the path setting WNS">
      <p>
        The memory port recorded it against itself: the read FIFO's BRAM output
        reached the quantiser's DSP control through
        <b>9 LUT levels, 4.399 ns</b>, and set the worst slack on every SLR1
        probe. Taking it out of the port recovered <b>0.468 ns</b> — the
        shipping configuration went from <b>−0.372 to +0.096</b>, and the
        control processor's slack cost went from 0.46 ns to <b>zero</b>.
      </p>
      <p>
        The general lesson is in the two one-port rows:
        <b>a per-port cost measured at one port is not a per-port cost</b>, and
        slack in particular does not extrapolate. At one port the processor was
        free; at two it was not.
      </p>
      <p class="kt-text-caption">
        Out-of-context <b>synthesis</b> of <span class="chip">sysnode</span> on
        <span class="chip">xcvu13p-fhgb2104-2L-e</span>, Vivado 2024.2, at a
        3.333 ns target. Nothing is placed and nothing is routed, so a slack
        here is an upper bound — one module in this tree lost 0.740 ns from
        synthesis to routing.
      </p>
    </Callout>

    <h3 class="doc-h3">The grant discipline</h3>
    <Callout kind="rule" title="Hold the grant before you issue the read">
      <p>
        A requester <b>MUST NOT</b> issue its AXI read until it holds a grant on
        the bank, and the grant is held for a <b>whole run</b> rather than per
        entry. Input to an occupant is <b>push-only</b>: beats are presented at
        line rate and never handshaken, so a beat arriving without a grant has
        nowhere to go and is dropped.
      </p>
      <p>
        Per-entry grant would be finer-grained and is unsafe — a requester
        issues the next entry's read while the current entry is still inside the
        occupant, so its beats can land before it could re-acquire.
      </p>
    </Callout>

    <WaveTrace
      :rows="grantBroken.rows"
      :notes="grantBroken.notes"
      variant="broken"
      label="AR before grant — the first beat is dropped, and only simulation says so"
    />
    <WaveTrace
      :rows="grantFixed.rows"
      :notes="grantFixed.notes"
      variant="fixed"
      label="Grant first, held for the run"
    />

    <Callout kind="note" title="The arbiter is built and never arbitrates">
      <p>
        <span class="chip">mag_xform</span>'s
        <span class="chip">NREQ</span> defaults to 2 and both instantiations in
        the tree pass 1: the memory mover is the only thing that drives the
        slot. An occupant author gains nothing from that — the contract above is
        what the port list is written against, and a second requester may be
        added without the occupant changing — but a reader comparing this page
        against a netlist should expect to find the arbitration folded away.
      </p>
    </Callout>

    <h3 class="doc-h3">Selection, and the geometry contract</h3>
    <BitField
      :fields="xfSel"
      caption="Mover register 0x10, the source walker's header. Both fields ride in bits the header already left free, so no register was spent on them — and both are written with the source select, because a transform applies to the read side of a move and is ignored on the destination's header"
    />
    <SpecTable :cols="xfFields.cols" :rows="xfFields.rows" />
    <SpecTable :cols="xfIds.cols" :rows="xfIds.rows" />

    <Callout
      kind="trap"
      title="An id that names no occupant is answered by BYPASS — the move succeeds and the bytes are unconverted"
    >
      <p>
        The demux has no other answer to give, so a mistyped id produces a move
        that completes, reports success, and delivers an operand in the source
        format. <span class="chip">fault[0]</span> on the bank is the one fault
        a bank can detect by itself and it exists for exactly this.
      </p>
      <p>
        <b>And it is unreadable in the RV64 configuration.</b>
        <span class="chip">rv64_mag_pe</span> instantiates the bank with
        <span class="chip">cfg_en</span> tied to zero and
        <span class="chip">cfg_rdata</span> unconnected, and the RV64 control
        region carries no occupant window — so the fault register, the geometry
        register and any occupant register are all unreachable there. The fault
        is sticky and cleared only by a write to
        <span class="chip">0x00</span>, so in that configuration it is also
        <b>unclearable</b>. An occupant with no registers of its own is
        unaffected, which is the case the shipping bank is in.
      </p>
    </Callout>

    <SpecTable
      :cols="bankRegs.cols"
      :rows="bankRegs.rows"
      caption="Reached by ordinary load and store from the control processor's node range, 0xF001_0000 | (id << 8) | reg. The host has no path to them; the host talks to the processor for work."
    />

    <Callout kind="rule" title="The three hard rules an occupant obeys">
      <p>
        <b>1. Fixed output shape, and four is the ceiling.</b> An entry yields
        <span class="chip">OUT_WORDS</span> words whatever the source length,
        and the bank presents exactly
        <span class="chip">word0..word3</span>. An expanding transform shrinks
        its entry rather than growing its output: a 1:2 expansion is
        <span class="chip">IN_BITS 512 / OUT_WORDS 4</span>, not
        <span class="chip">1024 / 8</span>.
      </p>
      <p>
        <b>2. The whole entry may be needed before anything is emitted.</b>
        <span class="chip">done</span> may come any number of cycles after the
        last beat — a block scale shared along an axis is not known until the
        block is.
      </p>
      <p>
        <b>3. Input is push-only.</b> An occupant needing backpressure buffers
        internally. <span class="chip">need_beat</span> exists on the port list
        and the agent ignores it today, so tie it high or drive it truthfully.
      </p>
      <p>
        A fourth follows from the first: an occupant that adds combinational
        depth in front of its own first register is extending a path the agent
        has already broken twice, and gets no third stage.
      </p>
    </Callout>

    <Callout kind="note" title="A fault aborts the run, and the run still completes">
      <p>
        An occupant raising <span class="chip">fault</span> stops the move: no
        further reads are issued, <span class="chip">busy</span> falls normally,
        and the mover reports a fault code meaning <i>the occupant faulted</i>.
        Nothing above has to learn a new wait.
      </p>
      <p>
        The destination is left <b>partially written</b>, which is the
        deliberate trade: a destination that is definitely incomplete is safer
        than one that is plausibly wrong. A bound axis is likewise refused
        outright — a padded element issues no read, and the occupant is fed a
        fixed <span class="chip">IN_BITS</span> off the read return, so a bound
        axis would leave an entry a beat short forever.
      </p>
    </Callout>

    <Callout
      kind="rule"
      title="Convert once per tensor, and know what a single-use operand costs"
    >
      <p>
        A transform on the fetch path is paid <b>once per read</b>; a transform
        on the mover path is paid <b>once per tensor</b>. Hidden state is
        written back by the units and then re-read, so converting on every read
        is the expensive arrangement.
      </p>
      <p>
        The cost of the choice is that a <b>single-use</b> operand pays more,
        not the same. Converting on the fetch reads it once; converting on the
        mover reads the source, writes the converted copy, and the fetch then
        reads that — at a 2:1 geometry, <b>256 + 128 + 128 bytes against 256</b>
        plus a pass of latency. The ratio follows from
        <span class="chip">IN_BITS</span> /
        <span class="chip">OUT_WORDS</span> and is whatever the occupant
        declares. An operand read more than once wins immediately, and an
        operand read once is the case the compiler should not be generating.
      </p>
    </Callout>

    <h2 class="doc-h2">Why the adapter cannot deadlock the link it sits in</h2>
    <Callout kind="rule" title="A local link is a proof, not a test result">
      <p>
        Putting a store inside the routing fabric turns a pure forwarder into a
        source, and the mesh's deadlock-freedom argument has to be redone from
        scratch. That is the reason the router is the wrong place for this, and
        it is the reason the one candidate that does put caching in the router
        is the only one flagged as risking deadlock.
      </p>
      <p>
        <b
          >A local link has exactly one producer and one consumer and takes no
          part in routing.</b
        >
        Injecting a response there is indistinguishable, from the mesh's point
        of view, from the endpoint having produced it — which endpoints do
        constantly. So the obligation reduces to a local property provable by
        inspection: never hold <span class="chip">busy</span> on either face
        forever.
      </p>
      <p>Three terms discharge it, and each is bounded by something that must finish:</p>
      <p>
        <b>The skid.</b> Backpressure toward the endpoint is
        <span class="chip">s_val &amp;&amp; !s_go</span> — one flit held, and it
        moves as soon as it is swallowed or the router face is free.
      </p>
      <p>
        <b>The held read.</b> A second staged read while one is in flight is
        held rather than forwarded, because forwarding it would read DRAM where
        the staged copy lives. It is bounded by the run in flight, which
        terminates: <span class="chip">left</span> counts down and nothing can
        extend it.
      </p>
      <p>
        <b>The control reply.</b> A pending reply owns the router face, so a
        flit waiting to be forwarded waits behind it. There is only ever one
        pending reply, and it retires the first cycle the router is not busy.
      </p>
      <p>
        The queue is the fourth term and it is not a bound, it is an
        impossibility: launches are gated on
        <span class="chip">q_used &lt; QD − 3</span>, covering the three
        pipeline stages in flight plus the write-ack's slot, so a QD-deep queue
        cannot overrun. The credit is <b>counted</b>, not read off the FIFO's
        almost-full output, which XPM ties to plain full.
      </p>
    </Callout>

    <h2 class="doc-h2">Deploying one</h2>
    <SpecTable
      :cols="capacity.cols"
      :rows="capacity.rows"
      caption="A URAM288 is 288 Kb, natively 4,096 × 72 b, and width is built by paralleling them. Every row is arithmetic on the primitive and the shipped parameters, not a placed result."
    />
    <SpecTable :cols="procedure.cols" :rows="procedure.rows" />

    <Callout kind="measured" title="Reach is the constraint, not capacity">
      <p>
        A pass's working set is a few hundred kilobytes. Even a conservative
        budget of free URAM gives several megabytes per die region, and this
        part has four of them — so the on-chip store available is in the
        mid-teens of megabytes. <b>"It does not fit" is almost always a tiling
        question rather than a capacity one.</b>
      </p>
      <p>
        What is scarce is the ability of one centralised block to reach URAM
        columns spread across a die whose worst region is at
        <b>95.49% CLB</b> occupancy. A distributed adapter is placed next to the
        columns it owns; a central store cannot be. That is the whole argument
        for the in-link form, and it is why the agent's store is
        <b>banked</b> — four banks are easier to place than one block, and the
        bank select sits inside the entry index so a sequential fill spreads
        across them rather than loading one.
      </p>
      <p class="kt-text-caption">
        URAM occupancy from one placed multi-mesh image of the reference
        accelerator on <span class="chip">xcvu13p-fhgb2104-2L-e</span>: 120 of
        1,280, 9.38%.
      </p>
    </Callout>

    <Callout kind="open" title="The data path has never run on silicon">
      <p>
        On the current image, all ten mesh-endpoint adapters on one mesh answer
        their capability, base, enable and counter registers and take a written
        base. The store is disabled at reset, so an adapter nobody configured
        claims no address. <b>No compute unit has issued a staging address on
        the card.</b> The control plane is verified and the data path is not.
      </p>
      <p>
        Four things are unmeasured and would each change a sizing decision: does
        a bank at the built width close at the mesh frequency, with many URAMs
        in parallel and an optional output register; what the port-B arbitration
        costs when host and operand traffic overlap; what the address walker and
        mux cost in fabric on a region already at 95%; and whether a staging
        node can serve the shared-fetch multicast or whether that has to stay
        with the agent.
      </p>
    </Callout>

    <h2 class="doc-h2">What a shared cache would have been for</h2>
    <SpecTable :cols="shared.cols" :rows="shared.rows" />

    <Callout
      kind="rule"
      title="Staging is not a cache and calling it one costs you the design"
    >
      <p>
        A staged read has <b>no tag, no lookup and no miss path</b>: the address
        decodes into the aperture and the store answers. That is why it can
        serve a whole line in one port-A read with no burst, and why a staged
        fetch never transforms —
        <b>staging holds operand words verbatim</b>, so there is nothing to
        convert on the way out.
      </p>
      <p>
        It also means the failure modes are not a cache's. There is no
        thrashing, no capacity miss, no coherence question and nothing to flush.
        In exchange there is no safety net at all: a wrong address is answered
        confidently with the wrong bytes, exactly as a wrong DRAM address would
        be.
      </p>
    </Callout>

    <h2 class="doc-h2">The tagged L2 that is not built</h2>
    <p class="doc-p">
      The tier that does not exist is a real cache attached to a compute unit —
      tags, hits, misses — and its interesting property is not caching at all.
      It is that
      <b>a tagged tier can carry two address classes</b>: <i>memory</i>, which
      is cacheable and tagged, and <i>communication</i>, which is never tagged
      and always misses, so a miss <i>is</i> the packet.
    </p>

    <Callout kind="note" title="Why that matters more than hit rate">
      <p>
        It is what lets a core that <b>cannot bend to this model</b> — a foreign
        RISC-V, an A53 — join the mesh at all. Such a core cannot emit a
        <span class="chip">MEM_RD_REQ</span>; it emits loads and stores. Every
        unit in this tree reimplements the request protocol for itself, and the
        RV32 PE's version of it is 453 lines whose header states the job
        plainly: everything about the framework memory protocol that software
        must never see. An application core cannot be taught that, and cannot be
        modified into it without owning its RTL.
      </p>
      <p>
        So the memory model is not a more convenient IO model. It is the only
        model under which third-party silicon can join a mesh, and
        <b>integration reach</b> — not bandwidth — is what this candidate adds
        beyond shared fetch.
      </p>
      <p>
        The pattern is already built one level down: the RV32 PE's L1 plus its
        request block is exactly this design, private to one core, and it passes
        its component checks. The work is not inventing a mechanism — it is
        lifting that pair out of the PE, making it a block any endpoint can sit
        behind, and sizing it to L2, with the fill targeting the explicit
        staging that already exists rather than DRAM.
      </p>
      <p>
        The unresolved part is blocking behaviour:
        <i>everything as memory</i> means a communication access stalls the core
        the way a load does, and real CPUs separate that with memory attributes
        — ARM Device versus Normal, x86 UC versus WB, RISC-V PMA. That is a
        design question this machine has not had to answer, because its own
        units speak the protocol directly.
      </p>
    </Callout>

    <Callout kind="open" title="What is missing is the tag array, and nothing else">
      <p>
        The endpoint slot it would occupy is <b>proven</b>: the adapter sits at
        a unit's port with URAM behind it, is selectable per endpoint, and is in
        the ship tops at 8 URAM each. The port contract, the placement and the
        URAM budget all exist. No tag array, fill or evict logic exists anywhere
        in the tree, and <b>no figure for its cost has been measured</b> —
        nothing on this page should be quoted as one until an out-of-context run
        exists.
      </p>
      <p>
        Four questions are open and three have a defensible default. Coherence:
        almost certainly none — a single writer per line, software ordering, an
        explicit flush, which is the contract the PE's L1 already has. Line
        size: 32 bytes, matching the mover's word, the flit payload and the
        agent's internal beat; anything else introduces a fragmentation this
        machine has so far avoided. Fill target: staging, if the mesh has it,
        because the hierarchy only pays for itself if a tagged miss lands in an
        explicit layer rather than crossing to DDR4. Cost is the one with no
        default.
      </p>
    </Callout>

    <Callout
      kind="note"
      title="And one alternative that should be measured before any of it is written"
    >
      <p>
        A vendor AXI L2 cache in front of the memory controller is a few hours
        of work, and instantiating it answers "does caching DRAM help this
        workload at all?" without committing to a design. If a general cache
        moves nothing, a bespoke one is unlikely to.
      </p>
      <p>
        Two structural objections stand regardless of its size, and both are
        arguments for putting a store <i>where the intent is</i> rather than
        against the vendor part. It sits <b>after</b> arbitration, so it sees
        one request and cannot tell that shared fetch served four consumers. And
        it sees the beats a descriptor produced rather than the descriptor, so
        it cannot prefetch a run the agent already knows is coming. Host and
        control traffic have neither property, and are exactly the workload a
        general cache suits.
      </p>
    </Callout>

    <h2 class="doc-h2">Conventions</h2>
    <SpecTable :cols="conventions.cols" :rows="conventions.rows" />

    <h2 class="doc-h2">Fixed protocol, addon, convention, or yours</h2>
    <SpecTable :cols="categories.cols" :rows="categories.rows" />

    <h2 class="doc-h2">What this does not own</h2>
    <SpecTable :cols="notOwned.cols" :rows="notOwned.rows" />
  </DocPage>
</template>
