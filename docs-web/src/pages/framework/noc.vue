<script setup>
/* ================================================================= router circuit */
const router = {
  nodes: [
    {
      id: "lin",
      x: 0,
      y: 6,
      w: 8,
      h: 2.6,
      label: "link in",
      sub: "data/valid/busy",
    },
    {
      id: "fifo",
      x: 10,
      y: 6,
      w: 9,
      h: 2.6,
      label: "flit FIFO",
      sub: "FIFO_DEPTH · MEMORY_TYPE",
    },
    {
      id: "route",
      x: 10,
      y: 10,
      w: 9,
      h: 2.6,
      label: "route compare",
      sub: "clamp, then XY",
    },
    {
      id: "hold",
      x: 21,
      y: 6,
      w: 9,
      h: 2.6,
      label: "holding reg",
      sub: "one per INPUT port",
      accent: true,
    },
    {
      id: "req",
      x: 21,
      y: 10,
      w: 9,
      h: 2.6,
      label: "req[4:0]",
      sub: "one-hot, KILL-masked",
    },
    {
      id: "mux",
      x: 33,
      y: 3.5,
      w: 9,
      h: 2.6,
      label: "5:1 mux",
      sub: "FLIT_WIDTH wide",
    },
    {
      id: "arb",
      x: 33,
      y: 8,
      w: 9,
      h: 2.6,
      label: "round-robin",
      sub: "pointer per output",
    },
    {
      id: "oreg",
      x: 45,
      y: 3.5,
      w: 9,
      h: 2.6,
      label: "output reg",
      accent: true,
    },
    {
      id: "lout",
      x: 57,
      y: 3.5,
      w: 8,
      h: 2.6,
      label: "link out",
      sub: "data/valid/busy",
    },
  ],
  edges: [
    { from: "lin:r", to: "fifo:l", dir: "h" },
    { from: "fifo:r", to: "hold:l", dir: "h" },
    { from: "fifo:b", to: "route:t", dir: "v" },
    { from: "route:r", to: "req:l", dir: "h" },
    { from: "hold:r", to: "mux:l", dir: "h", accent: true, label: "flit" },
    { from: "req:r", to: "arb:l", dir: "h", label: "request" },
    { from: "arb:t", to: "mux:b", dir: "v", label: "grant" },
    { from: "mux:r", to: "oreg:l", dir: "h", accent: true },
    { from: "oreg:r", to: "lout:l", dir: "h", accent: true },
    { from: "oreg:b", to: "arb:r", dir: "h", dash: true, label: "room" },
  ],
  groups: [
    { x: -1.5, y: 4.5, w: 32.5, h: 8.5, label: "noc_inport.v — one of five" },
    { x: 31.5, y: 2, w: 24.5, h: 9.5, label: "noc_outport.v — one of five" },
  ],
};

const knobs = {
  cols: [
    { key: "knob", label: "Knob", mono: true },
    { key: "what", label: "What it moves" },
  ],
  rows: [
    {
      knob: "FLIT_WIDTH",
      what: "<b>Everything.</b> Every register, mux and FIFO in the router is this wide.",
    },
    { knob: "router count", what: "Linear. Cost per router is fixed." },
    {
      knob: "MEMORY_TYPE",
      what: "Which primitive the flit buffers land in — LUT versus block RAM.",
    },
    {
      knob: "FIFO_DEPTH",
      what: "Almost nothing in LUTRAM up to a shift-register's depth; a step function in block RAM.",
    },
  ],
};

/* ================================================================= link handshake */
const hsBrokenSender = {
  rows: [
    { name: "busy", kind: "bit", values: [0, 1, 1, 0, 0] },
    { name: "valid", kind: "bit", values: [0, 1, 0, 0, 0] },
    { name: "data", kind: "bus", values: [null, "F0", null, null, null] },
    { name: "transfer", kind: "text", values: ["", "", "", "", "never"] },
  ],
  notes: [
    { cycle: 0, text: "The sender samples busy low and commits to F0." },
    {
      cycle: 1,
      text: "F0 is presented — but the receiver raised busy this cycle. valid && !busy is false, so there is no transfer.",
    },
    {
      cycle: 2,
      text: "The sender withdraws valid. F0 is destroyed. No error, no retry, no counter moves.",
      tone: "bad",
    },
    {
      cycle: 3,
      text: "busy falls, and there is nothing left to take. A dropped MEM_WR_DATA leaves its slot short forever, so the source's next descriptor opens a second slot and its data binds to the older one.",
      tone: "bad",
    },
  ],
};

const hsBrokenReceiver = {
  rows: [
    { name: "busy", kind: "bit", values: [1, 1, 1, 0, 0] },
    { name: "valid", kind: "bit", values: [1, 1, 1, 1, 0] },
    { name: "data", kind: "bus", values: ["F0", "F0", "F0", "F0", null] },
    { name: "wr_en = room", kind: "bit", values: [1, 1, 1, 1, 0] },
    {
      name: "enqueued",
      kind: "bus",
      values: ["F0", "F0", "F0", "F0", null],
      mark: [0, 1, 2],
    },
  ],
  notes: [
    {
      cycle: 0,
      text: 'busy is high, so no transfer has legally happened — but the receiver derives its write enable from "is there room", which is still true.',
      tone: "bad",
    },
    {
      cycle: 2,
      text: "The sender is correctly holding valid and data unchanged, so the same flit is written on every cycle of backpressure.",
      tone: "bad",
    },
    {
      cycle: 3,
      text: "The legal transfer finally happens. F0 is now in the queue four times. A duplicated write beat overruns its slot's expected count and the surplus beat matches nothing.",
      tone: "bad",
    },
  ],
};

const hsFixed = {
  rows: [
    { name: "busy", kind: "bit", values: [1, 1, 0, 0, 1, 0] },
    { name: "valid", kind: "bit", values: [1, 1, 1, 1, 1, 1], mark: [2, 3, 5] },
    { name: "data", kind: "bus", values: ["F0", "F0", "F0", "F1", "F2", "F2"] },
    { name: "transfer", kind: "text", values: ["", "", "F0", "F1", "", "F2"] },
  ],
  notes: [
    {
      cycle: 2,
      text: "valid && !busy. That cycle is the transfer, and it is the only definition of one.",
      tone: "good",
    },
    {
      cycle: 3,
      text: "The next flit is presented immediately. One flit per cycle is sustained because the input load term and the output room term are both true on the cycle the register is being emptied.",
      tone: "good",
    },
    {
      cycle: 4,
      text: "busy rises. The sender holds F2 unchanged and does not withdraw it.",
      tone: "good",
    },
  ],
};

/* ================================================================= flit */
const flitGeom = [
  { name: "header", bits: 32, value: "4·POS_WIDTH + 16" },
  { name: "payload", bits: 256, value: "per type", accent: true },
];

const flitHeader = [
  { name: "dst_x", bits: 4, value: "flit 287:284", accent: true },
  { name: "dst_y", bits: 4, value: "flit 283:280", accent: true },
  { name: "src_x", bits: 4, value: "flit 279:276" },
  { name: "src_y", bits: 4, value: "flit 275:272" },
  { name: "type", bits: 4, value: "flit 271:268" },
  { name: "txn", bits: 8, value: "flit 267:260" },
  { name: "last", bits: 1, value: "flit 259" },
  { name: "rsvd", bits: 3, value: "flit 258:256" },
];

const headerSpec = {
  cols: [
    { key: "f", label: "Field", mono: true },
    { key: "w", label: "Width", align: "right" },
    { key: "gen", label: "General position", mono: true },
    { key: "own", label: "Owner" },
  ],
  rows: [
    {
      f: "dst_x",
      w: "POS_WIDTH",
      gen: "[FLIT_WIDTH-1 -: POS_WIDTH]",
      own: "framework",
    },
    {
      f: "dst_y",
      w: "POS_WIDTH",
      gen: "[FLIT_WIDTH-POS_WIDTH-1 -: POS_WIDTH]",
      own: "framework",
    },
    {
      f: "src_x",
      w: "POS_WIDTH",
      gen: "[FLIT_WIDTH-2*POS_WIDTH-1 -: POS_WIDTH]",
      own: "framework",
    },
    {
      f: "src_y",
      w: "POS_WIDTH",
      gen: "[FLIT_WIDTH-3*POS_WIDTH-1 -: POS_WIDTH]",
      own: "framework",
    },
    {
      f: "type",
      w: "4",
      gen: "[FLIT_WIDTH-4*POS_WIDTH-1 -: 4]",
      own: "framework",
    },
    {
      f: "txn",
      w: "8",
      gen: "[FLIT_WIDTH-4*POS_WIDTH-5 -: 8]",
      own: "per type",
    },
    { f: "last", w: "1", gen: "[FLIT_WIDTH-4*POS_WIDTH-13]", own: "framework" },
    {
      f: "rsvd",
      w: "3",
      gen: "[FLIT_WIDTH-4*POS_WIDTH-14 -: 3]",
      own: "framework",
    },
    {
      f: "payload",
      w: "rest",
      gen: "[FLIT_WIDTH-4*POS_WIDTH-17 : 0]",
      own: "per type",
    },
  ],
};

const types = {
  cols: [
    { key: "code", label: "Code", mono: true },
    { key: "name", label: "Name", mono: true },
    { key: "by", label: "May be sent by" },
    { key: "to", label: "Consumed by" },
  ],
  rows: [
    {
      code: "0x0",
      name: "MEM_RD_REQ",
      by: "any endpoint",
      to: "the memory agent",
    },
    {
      code: "0x1",
      name: "MEM_WR_REQ",
      by: "any endpoint",
      to: "the memory agent",
    },
    {
      code: "0x2",
      name: "MEM_RD_RESP",
      by: "the memory agent",
      to: "the requester, or a listed peer",
    },
    {
      code: "0x3",
      name: "MEM_WR_ACK",
      by: "the memory agent",
      to: "<b>nobody</b>",
      _tone: "warn",
    },
    {
      code: "0x4",
      name: "MEM_WR_DATA",
      by: "any endpoint",
      to: "the memory agent",
    },
    {
      code: "0x5",
      name: "CU_INST",
      by: "the orchestrator",
      to: "a compute unit's instruction FIFO",
    },
    {
      code: "0x6",
      name: "CU_SIGNAL",
      by: "a compute unit",
      to: "the orchestrator's status mirror",
    },
    {
      code: "0x7",
      name: "CU_CTRL",
      by: "any controller",
      to: "answered inside noc_cu_base",
    },
    {
      code: "0x8",
      name: "CU_DATA",
      by: "any endpoint",
      to: "a compute unit's receive path",
    },
    {
      code: "0x9–0xE",
      name: "unallocated",
      by: "—",
      to: "reserved to the framework",
    },
    {
      code: "0xF",
      name: "ERROR",
      by: "—",
      to: "declared and unimplemented",
      _tone: "bad",
    },
  ],
};

const rsvd = {
  cols: [
    { key: "bit", label: "Bit", mono: true },
    { key: "meaning", label: "Meaning" },
    { key: "by", label: "Set by" },
  ],
  rows: [
    {
      bit: "rsvd[2]",
      meaning:
        "<b>Remote-mesh marker.</b> The memory agent's inbound demux hands this flit to the interlink encapsulator instead of to the agent. Zero on every flit a single-mesh build produces.",
      by: "a unit sending across a mesh boundary",
    },
    {
      bit: "rsvd[1:0]<br>when rsvd[2] set",
      meaning: "The destination <b>mesh id</b>, 0–3.",
      by: "the same sender",
    },
    {
      bit: "rsvd[1:0]<br>on MEM_RD_RESP",
      meaning:
        "<b>Word index within the entry</b>, 0–3. Combined with txn, this tells the receiver exactly which slot the word belongs in, so arrival order stops being load-bearing.",
      by: "the memory agent",
    },
    {
      bit: "rsvd[1:0]<br>otherwise",
      meaning: "Reserved. MUST be zero.",
      by: "—",
    },
  ],
};

/* ---------------------------------------------------------------- payload layouts */
const plMemReq = [
  {
    name: "addr",
    bits: 40,
    value: "[39] aperture · [38] rsvd · [37:36] mesh · [35:0] local",
    accent: true,
  },
  { name: "len", bits: 8, value: "beats − 1" },
  { name: "flags", bits: 8 },
  { name: "count", bits: 8, value: "entries" },
  { name: "peer", bits: 24, value: "3 × {y,x}" },
  { name: "n_peer", bits: 2 },
  { name: "entry_words", bits: 8 },
  { name: "reserved", bits: 158, value: "MUST be 0" },
];

const plCuData = [
  { name: "buf_id", bits: 8, value: "framework namespace", accent: true },
  { name: "offset", bits: 16, value: "32-byte granules" },
  { name: "len", bits: 8, value: "data flits − 1" },
  { name: "flags", bits: 8, value: "bit 0 only" },
  { name: "ack_y", bits: 4 },
  { name: "ack_x", bits: 4 },
  { name: "reserved", bits: 208, value: "MUST be 0" },
];

const plSignal = [
  { name: "code", bits: 8, value: "framework below 0x40", accent: true },
  { name: "arg", bits: 32, value: "always unit-defined" },
  { name: "reserved", bits: 216, value: "MUST be 0" },
];

const plCtrlReply = [
  { name: "op", bits: 8, value: "always 0x02" },
  { name: "index", bits: 8, value: "echoed" },
  { name: "value", bits: 64, value: "the register", accent: true },
  { name: "reserved", bits: 176, value: "zero" },
];

const flags = {
  cols: [
    { key: "bit", label: "Bit", mono: true },
    { key: "name", label: "Name", mono: true },
    { key: "status", label: "Status" },
  ],
  rows: [
    {
      bit: "0",
      name: "cacheable",
      status: "Declared in noc_pkt.vh. <b>No RTL reads it.</b>",
      _tone: "warn",
    },
    {
      bit: "1",
      name: "invalidate",
      status: "Declared. No RTL reads it.",
      _tone: "warn",
    },
    {
      bit: "2",
      name: "flush",
      status: "Declared. No RTL reads it.",
      _tone: "warn",
    },
    { bit: "3", name: "—", status: "Unallocated. MUST be 0." },
    {
      bit: "4",
      name: "—",
      status:
        "<b>Reserved and ignored.</b> Was QUANT. A fetch is never transformed — the transform slot belongs to the memory mover — so a requester that sets this gets an ordinary untransformed read.",
      _tone: "warn",
    },
    {
      bit: "5",
      name: "—",
      status:
        "<b>Reserved and ignored.</b> Was BLAYOUT, the packing select for that transform.",
      _tone: "warn",
    },
    {
      bit: "6",
      name: "STREAM",
      status:
        "This descriptor covers <i>count</i> consecutive entries, not one fetch.",
    },
    { bit: "7", name: "—", status: "Unallocated. MUST be 0." },
  ],
};

const bufIds = {
  cols: [
    { key: "id", label: "buf_id", mono: true },
    { key: "alloc", label: "Allocation" },
    { key: "kind", label: "Kind" },
  ],
  rows: [
    {
      id: "0",
      alloc: "First operand buffer, by convention.",
      kind: "Convention",
    },
    {
      id: "1",
      alloc: "Second operand buffer, by convention.",
      kind: "Convention",
    },
    {
      id: "2",
      alloc:
        "Accumulator / result buffer, in the unit's <i>internal</i> accumulation format, by convention.",
      kind: "Convention",
    },
    {
      id: "3",
      alloc: "<b>Reserved: the staging adapter.</b> A unit MUST NOT claim it.",
      kind: "<b>Fixed</b>",
      _tone: "warn",
    },
    {
      id: "4–255",
      alloc:
        "Unallocated. A unit MAY use one, but MUST publish what it means, and MUST expect a future framework allocation to take it.",
      kind: "<b>Fixed</b>",
    },
  ],
};

const signals = {
  cols: [
    { key: "code", label: "Code", mono: true },
    { key: "name", label: "Name", mono: true },
    { key: "by", label: "Emitted by" },
    { key: "arg", label: "arg", mono: true },
  ],
  rows: [
    {
      code: "0x00",
      name: "INST_COMPLETE",
      by: "the framework, on retirement",
      arg: "exec_result",
    },
    {
      code: "0x01",
      name: "BATCH_COMPLETE",
      by: "the framework, on retiring an instruction with last set",
      arg: "{24'd0, txn}",
    },
    {
      code: "0x02",
      name: "BARRIER_REACHED",
      by: "<b>nothing.</b> Allocated, unimplemented.",
      arg: "barrier id",
      _tone: "warn",
    },
    {
      code: "0x03",
      name: "DATA_RECEIVED",
      by: "the unit, on completing a CU_DATA burst whose flags[0] was set",
      arg: "{24'd0, buf_id}",
    },
    {
      code: "0x04",
      name: "FAULT",
      by: "the framework, when exec_fault is set at exec_done",
      arg: "exec_result",
    },
    {
      code: "0x05–0x3F",
      name: "reserved",
      by: "reserved to the framework",
      arg: "—",
    },
    {
      code: "0x40–0xFF",
      name: "<b>unit-defined</b>",
      by: "the unit",
      arg: "unit-defined",
    },
  ],
};

/* ================================================================= mesh map */
const meshMap = {
  nodes: [
    {
      id: "c00",
      x: 0,
      y: 0,
      w: 5.5,
      h: 3,
      label: "xxx",
      sub: "corner · must be empty",
    },
    { id: "c10", x: 7, y: 0, w: 5.5, h: 3, label: "mat", sub: "1,0 · edge" },
    { id: "c20", x: 14, y: 0, w: 5.5, h: 3, label: "vec", sub: "2,0 · edge" },
    {
      id: "c30",
      x: 21,
      y: 0,
      w: 5.5,
      h: 3,
      label: "xxx",
      sub: "corner · must be empty",
    },

    { id: "c01", x: 0, y: 4.5, w: 5.5, h: 3, label: "mag", sub: "0,1 · edge" },
    {
      id: "c11",
      x: 7,
      y: 4.5,
      w: 5.5,
      h: 3,
      label: "R + mat",
      sub: "1,1 · local",
      accent: true,
    },
    {
      id: "c21",
      x: 14,
      y: 4.5,
      w: 5.5,
      h: 3,
      label: "R + mat",
      sub: "2,1 · local",
      accent: true,
    },
    {
      id: "c31",
      x: 21,
      y: 4.5,
      w: 5.5,
      h: 3,
      label: "xxx",
      sub: "3,1 · nothing",
    },

    { id: "c02", x: 0, y: 9, w: 5.5, h: 3, label: "mag", sub: "0,2 · edge" },
    {
      id: "c12",
      x: 7,
      y: 9,
      w: 5.5,
      h: 3,
      label: "R + mat",
      sub: "1,2 · local",
      accent: true,
    },
    {
      id: "c22",
      x: 14,
      y: 9,
      w: 5.5,
      h: 3,
      label: "R + mat",
      sub: "2,2 · local",
      accent: true,
    },
    {
      id: "c32",
      x: 21,
      y: 9,
      w: 5.5,
      h: 3,
      label: "xxx",
      sub: "3,2 · nothing",
    },

    {
      id: "c03",
      x: 0,
      y: 13.5,
      w: 5.5,
      h: 3,
      label: "xxx",
      sub: "corner · must be empty",
    },
    { id: "c13", x: 7, y: 13.5, w: 5.5, h: 3, label: "mat", sub: "1,3 · edge" },
    {
      id: "c23",
      x: 14,
      y: 13.5,
      w: 5.5,
      h: 3,
      label: "vec",
      sub: "2,3 · edge",
    },
    {
      id: "c33",
      x: 21,
      y: 13.5,
      w: 5.5,
      h: 3,
      label: "xxx",
      sub: "corner · must be empty",
    },
  ],
  edges: [
    { from: "c11:r", to: "c21:l", dir: "h" },
    { from: "c12:r", to: "c22:l", dir: "h" },
    { from: "c11:b", to: "c12:t", dir: "v" },
    { from: "c21:b", to: "c22:t", dir: "v" },
    { from: "c10:b", to: "c11:t", dir: "v" },
    { from: "c20:b", to: "c21:t", dir: "v" },
    { from: "c01:r", to: "c11:l", dir: "h" },
    { from: "c02:r", to: "c12:l", dir: "h" },
    { from: "c13:t", to: "c12:b", dir: "v" },
    { from: "c23:t", to: "c22:b", dir: "v" },
  ],
  groups: [
    {
      x: 6,
      y: 3.5,
      w: 14.5,
      h: 10,
      label: "router grid — one endpoint per local port",
    },
  ],
};

/* ================================================================= XY route */
const xy = {
  nodes: [
    {
      id: "D",
      x: 16,
      y: 0,
      w: 6,
      h: 3,
      label: "D",
      sub: "2,0 · edge endpoint",
      accent: true,
    },
    { id: "r11", x: 8, y: 5, w: 6, h: 3, label: "R", sub: "1,1" },
    {
      id: "r21",
      x: 16,
      y: 5,
      w: 6,
      h: 3,
      label: "R",
      sub: "2,1 · clamped dst",
    },
    { id: "r31", x: 24, y: 5, w: 6, h: 3, label: "R", sub: "3,1" },
    { id: "r12", x: 8, y: 10, w: 6, h: 3, label: "R", sub: "1,2" },
    { id: "r22", x: 16, y: 10, w: 6, h: 3, label: "R", sub: "2,2" },
    { id: "r32", x: 24, y: 10, w: 6, h: 3, label: "R", sub: "3,2" },
    {
      id: "S",
      x: 0,
      y: 15,
      w: 6,
      h: 3,
      label: "S",
      sub: "0,3 · edge endpoint",
      accent: true,
    },
    { id: "r13", x: 8, y: 15, w: 6, h: 3, label: "R", sub: "1,3" },
    { id: "r23", x: 16, y: 15, w: 6, h: 3, label: "R", sub: "2,3" },
    { id: "r33", x: 24, y: 15, w: 6, h: 3, label: "R", sub: "3,3" },
  ],
  edges: [
    { from: "r11:r", to: "r21:l", dir: "h" },
    { from: "r21:r", to: "r31:l", dir: "h" },
    { from: "r12:r", to: "r22:l", dir: "h" },
    { from: "r22:r", to: "r32:l", dir: "h" },
    { from: "r23:r", to: "r33:l", dir: "h" },
    { from: "r11:b", to: "r12:t", dir: "v" },
    { from: "r12:b", to: "r13:t", dir: "v" },
    { from: "r31:b", to: "r32:t", dir: "v" },
    { from: "r32:b", to: "r33:t", dir: "v" },

    { from: "S:r", to: "r13:l", dir: "h", accent: true, label: "inward" },
    { from: "r13:r", to: "r23:l", dir: "h", accent: true, label: "X" },
    { from: "r23:t", to: "r22:b", dir: "v", accent: true, label: "Y" },
    { from: "r22:t", to: "r21:b", dir: "v", accent: true, label: "Y" },
    { from: "r21:t", to: "D:b", dir: "v", accent: true, label: "outward" },
  ],
  groups: [{ x: 7, y: 4, w: 24, h: 15, label: "router rectangle" }],
};

/* ================================================================= framing */
const framingBroken = {
  rows: [
    {
      name: "inbound",
      kind: "bus",
      values: ["desc A", "data A0", "data B0", "data A1", "data A2"],
    },
    { name: "type", kind: "text", values: ["0x8", "0x8", "0x8", "0x8", "0x8"] },
    { name: "src", kind: "text", values: ["A", "A", "B", "A", "A"] },
    {
      name: "stored",
      kind: "bus",
      values: ["open", "w0=A0", "w1=B0", "w2=A1", "w3=A2"],
      mark: [2],
    },
  ],
  notes: [
    {
      cycle: 2,
      text: "The receiver collected \"the next flit\" into the open message. B's data flit is now word 1 of A's burst.",
      tone: "bad",
    },
    {
      cycle: 4,
      text: "The burst completes with the right count and the wrong bytes. Nothing reports it, and the mesh interleaves by design — there is no mechanism that prevents another node's flit from landing here.",
      tone: "bad",
    },
  ],
};

const framingFixed = {
  rows: [
    {
      name: "inbound",
      kind: "bus",
      values: ["desc A", "data A0", "data B0", "data A1", "data A2"],
    },
    { name: "type", kind: "text", values: ["0x8", "0x8", "0x8", "0x8", "0x8"] },
    { name: "src", kind: "text", values: ["A", "A", "B", "A", "A"] },
    {
      name: "stored",
      kind: "bus",
      values: ["open", "w0=A0", "not A", "w1=A1", "w2=A2"],
      mark: [2],
    },
    { name: "last vs count", kind: "text", values: ["", "", "", "", "agree"] },
  ],
  notes: [
    {
      cycle: 2,
      text: "The source coordinate does not match the open stream's, so the flit is not part of this burst. CU_DATA is the exception that proves the framing rule: descriptor and data share the type code, so a receiver frames by count and disambiguates senders by source.",
      tone: "good",
    },
    {
      cycle: 4,
      text: "last agrees with the descriptor's count. They disagree exactly when two senders have interleaved into one receiver, which is otherwise indistinguishable from data corruption.",
      tone: "good",
    },
  ],
};

/* ================================================================= cu port retire */
const retireBroken = {
  rows: [
    { name: "inst_valid", kind: "bit", values: [1, 1, 0, 0, 0, 1, 0, 0, 0] },
    {
      name: "inst_ready",
      kind: "bit",
      values: [0, 1, 0, 0, 0, 1, 0, 0, 0],
      mark: [5],
    },
    {
      name: "exec_done",
      kind: "bit",
      values: [0, 0, 0, 0, 1, 1, 0, 1, 0],
      mark: [5],
    },
    { name: "in_flight", kind: "bit", values: [0, 0, 1, 1, 1, 0, 0, 0, 0] },
    {
      name: "signal q",
      kind: "text",
      values: ["", "", "", "", "A", "", "", "dropped", ""],
    },
  ],
  notes: [
    {
      cycle: 1,
      text: "A is accepted. inst_ready is high for exactly one cycle.",
    },
    {
      cycle: 4,
      text: "A retires and its completion is queued. in_flight clears for the next cycle.",
    },
    {
      cycle: 5,
      text: "The unit's retire pulse is still high when B is accepted. Both arms fire, the retire arm wins, and in_flight never rises for B.",
      tone: "bad",
    },
    {
      cycle: 6,
      text: "B is executing, but in_flight is low. The base does not know an instruction is outstanding.",
      tone: "bad",
    },
    {
      cycle: 7,
      text: "B raises exec_done with no instruction in flight, so it is discarded: no CU_SIGNAL, no credit. The dispatcher stalls forever and nothing says why.",
      tone: "bad",
    },
  ],
};

const retireFixed = {
  rows: [
    { name: "inst_valid", kind: "bit", values: [1, 1, 0, 0, 0, 1, 0, 0, 0] },
    {
      name: "inst_ready",
      kind: "bit",
      values: [0, 1, 0, 0, 0, 1, 0, 0, 0],
      mark: [5],
    },
    {
      name: "exec_done",
      kind: "bit",
      values: [0, 0, 0, 0, 1, 0, 0, 0, 0],
      mark: [4],
    },
    { name: "in_flight", kind: "bit", values: [0, 0, 1, 1, 1, 0, 1, 1, 1] },
    {
      name: "signal q",
      kind: "text",
      values: ["", "", "", "", "A", "", "", "", ""],
    },
  ],
  notes: [
    {
      cycle: 4,
      text: "A retires on a one-cycle pulse. Its completion is queued and its credit is on the way back.",
      tone: "good",
    },
    {
      cycle: 5,
      text: "in_flight is clear, so the base offers B and the unit accepts. exec_done is low this cycle — that is the whole rule.",
      tone: "good",
    },
    {
      text: "Every unit in the reference project leaves a cycle between the two arms, and the component bench counts accepted instructions against completions to keep it that way.",
      tone: "good",
    },
  ],
};

/* ================================================================= completions */
const compBroken = {
  rows: [
    { name: "exec_done", kind: "bit", values: [0, 1, 0, 1, 0, 0, 0, 0] },
    { name: "noc_out_busy", kind: "bit", values: [1, 1, 1, 1, 1, 0, 0, 0] },
    {
      name: "holding reg",
      kind: "bus",
      values: [null, "A", "A", "B", "B", "B", null, null],
      mark: [3],
    },
    { name: "sent", kind: "text", values: ["", "", "", "", "", "B", "", ""] },
    {
      name: "credits back",
      kind: "bus",
      values: ["0", "0", "0", "0", "0", "1", "1", "1"],
    },
  ],
  notes: [
    {
      cycle: 3,
      text: "The second completion overwrites the first before the link ever drained. A's credit is never returned.",
      tone: "bad",
    },
    {
      cycle: 7,
      text: "Two instructions retired; one credit came back. The dispatcher holds one fewer credit forever, and there is no counter that says so.",
      tone: "bad",
    },
  ],
};

const compFixed = {
  rows: [
    { name: "exec_done", kind: "bit", values: [0, 1, 0, 1, 0, 0, 0, 0] },
    { name: "noc_out_busy", kind: "bit", values: [1, 1, 1, 1, 1, 0, 0, 0] },
    {
      name: "signal FIFO",
      kind: "bus",
      values: [null, "A", "A", "A B", "A B", "B", null, null],
    },
    { name: "sent", kind: "text", values: ["", "", "", "", "", "A", "B", ""] },
    {
      name: "credits back",
      kind: "bus",
      values: ["0", "0", "0", "0", "0", "1", "2", "2"],
    },
  ],
  notes: [
    {
      cycle: 3,
      text: "Completions are queued, not held. The queue is 16 deep, so a datapath that retires faster than a congested link drains loses nothing.",
      tone: "good",
    },
    {
      text: "Instruction issue also stops when the signal queue is full: an instruction that executed but cannot be reported is worse than one that never issued.",
      tone: "good",
    },
  ],
};

/* ================================================================= flow control */
const fc = {
  nodes: [
    {
      id: "ep0",
      x: 0,
      y: 3.4,
      w: 11,
      h: 4.2,
      label: "requester",
      sub: "holds credits",
      accent: true,
    },
    { id: "r0", x: 14, y: 4, w: 8, h: 3, label: "router", sub: "no counter" },
    { id: "r1", x: 25, y: 4, w: 8, h: 3, label: "router", sub: "no counter" },
    {
      id: "ep1",
      x: 36,
      y: 3.4,
      w: 11,
      h: 4.2,
      label: "responder",
      sub: "bounded queue",
      accent: true,
    },
  ],
  edges: [
    { from: "ep0:r", to: "r0:l", dir: "h", label: "valid/busy" },
    { from: "r0:r", to: "r1:l", dir: "h", label: "valid/busy" },
    { from: "r1:r", to: "ep1:l", dir: "h", label: "valid/busy" },
    {
      from: "ep0:t",
      to: "ep1:t",
      dir: "h",
      dash: true,
      accent: true,
      label: "end-to-end credit",
    },
  ],
};

/* ================================================================= cu port signals */
const portSignals = {
  cols: [
    { key: "sig", label: "Signal", mono: true },
    { key: "dir", label: "Dir" },
    { key: "w", label: "Width", mono: true },
    { key: "meaning", label: "Meaning" },
  ],
  rows: [
    {
      sig: "inst_flit",
      dir: "in",
      w: "FLIT_WIDTH",
      meaning: "The whole CU_INST flit at the head of the instruction FIFO.",
    },
    {
      sig: "inst_valid",
      dir: "in",
      w: "1",
      meaning: "An instruction is available and may be accepted.",
    },
    {
      sig: "inst_ready",
      dir: "out",
      w: "1",
      meaning:
        "The unit accepts it this cycle. High for <b>exactly one cycle</b> per instruction.",
    },
    {
      sig: "exec_done",
      dir: "out",
      w: "1",
      meaning: "One-cycle pulse: the accepted instruction has retired.",
    },
    {
      sig: "exec_result",
      dir: "out",
      w: "32",
      meaning: "Sampled on exec_done. Becomes the CU_SIGNAL argument.",
    },
    {
      sig: "exec_fault",
      dir: "out",
      w: "1",
      meaning: "Sampled on exec_done. Turns the completion into SIG_FAULT.",
    },
    {
      sig: "dbg_ctr",
      dir: "out",
      w: "64",
      meaning:
        "Free-running unit-defined counters, published as CU_CTRL index 3.",
    },
    {
      sig: "send_flit / send_valid / send_ready",
      dir: "out / out / in",
      w: "FLIT_WIDTH",
      meaning:
        "A flit the unit transmits, header included. The base sends it <b>verbatim</b> and stamps nothing.",
    },
    {
      sig: "recv_flit / recv_valid / recv_ready",
      dir: "in / in / out",
      w: "FLIT_WIDTH",
      meaning:
        "The head of the receive queue. Pops on recv_valid &amp;&amp; recv_ready.",
    },
    {
      sig: "inst_space",
      dir: "out of base",
      w: "16",
      meaning:
        "Free entries in the instruction FIFO. A unit MAY leave it unconnected.",
    },
    {
      sig: "busy",
      dir: "out of base",
      w: "1",
      meaning: "in_flight, or instructions queued, or completions unsent.",
    },
  ],
};

const inbound = {
  cols: [
    { key: "t", label: "Type", mono: true },
    { key: "d", label: "Destination" },
  ],
  rows: [
    { t: "CU_INST (0x5)", d: "The instruction FIFO, depth INST_DEPTH." },
    {
      t: "CU_CTRL (0x7)",
      d: "Answered by the base. <b>It never reaches the unit.</b>",
    },
    {
      t: "everything else",
      d: "The receive FIFO, depth RECV_DEPTH, presented as recv_*.",
    },
  ],
};

/* ================================================================= conventions */
const conventions = {
  cols: [
    { key: "c", label: "Convention" },
    { key: "f", label: "Forced?" },
    { key: "why", label: "What breaks if you deviate" },
  ],
  rows: [
    {
      c: "<b>Hold credits per destination, and stall locally.</b>",
      f: "<b>Forced</b>",
      why: "The protocol requires that a requester never issue a request whose response it cannot absorb. How many credits you hold is yours; that you hold them is not. Deviating deadlocks the fabric under load, and the symptom appears at a node that did nothing wrong.",
      _tone: "warn",
    },
    {
      c: "<b>Take your flit type codes from noc_pkt.vh</b>, never from a neighbouring module.",
      f: "Free — and the one most worth obeying",
      why: "The type field is fixed protocol, but nothing in the build enforces it, so every module that restates a code is a chance to restate it wrongly. That has now happened twice in this tree.",
    },
    {
      c: "<b>Let unit-to-unit payloads be <i>(which buffer, where in it, how much)</i>.</b>",
      f: "Free",
      why: "The envelope is fixed; the meaning of a buffer index is yours and the network never interprets it. This shape holds for a unit with four operand buffers and for one with two. Publish what your indices mean as part of your unit's contract.",
    },
    {
      c: "<b>Signal codes below the central threshold are allocated; yours start above it.</b>",
      f: "Half forced",
      why: "The allocation is protocol so a controller can act on any unit's completion without knowing the unit. What you attach to the event is free.",
    },
    {
      c: "<b>Start from a shipping unit's port wiring</b>, not from the instrument and not from the spec.",
      f: "Free",
      why: "noc_cu_base already implements the retire-cycle rule and the hold-until-not-busy rule. Starting from the spec means rediscovering them, and both fail silently.",
    },
    {
      c: "<b>Keep one instruction in one flit if you can.</b>",
      f: "Free",
      why: "A single-flit instruction makes dispatch accounting exactly one credit per instruction, which is the case every tool in the tree assumes when reading counters. Continuation flits are supported and nothing in the reference project has needed them.",
    },
    {
      c: "<b>Put your opcode where the framework's demultiplex does not look.</b>",
      f: "Free, but narrow",
      why: "The type field routes your flit to the instruction queue; your payload is then untouched. Reusing header bits for your own meaning works right up until a framework version starts reading them.",
    },
    {
      c: "<b>Report dbg_ctr, even if the count is arbitrary.</b>",
      f: "Free",
      why: "The one 64-bit word you supply is the only unit-defined observability the control plane has. Tying it to zero is a decision to be blind during bring-up.",
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
      thing:
        "the compute-unit port: its signals, its obligations, its handshake rules",
      cat: "<b>fixed protocol</b>",
    },
    {
      thing: "the flit header and the message classes",
      cat: "<b>fixed protocol</b>",
    },
    {
      thing: "the control-register block every unit answers",
      cat: "<b>fixed protocol</b>",
    },
    {
      thing: "the link handshake and its retry rule",
      cat: "<b>fixed protocol.</b> Not negotiable at any level",
    },
    {
      thing: "XY routing, and the acyclic argument behind it",
      cat: "<b>fixed protocol.</b> Changing it is designing a different fabric",
    },
    {
      thing:
        "<b>the endpoint-side L2 adapter</b>, between a router's local link and an endpoint",
      cat: "<b>customizable addon</b> — it presents the same six signals on both faces, so a pass-through is a straight wire and a staging or caching version drops into the same place",
    },
    {
      thing: "FLIT_WIDTH, FIFO_DEPTH, MEMORY_TYPE, INST_DEPTH, RECV_MEM",
      cat: "<b>customizable</b> — sized per instance",
    },
    {
      thing: "the flit layout <i>as currently enforced</i>",
      cat: "<b>fixed protocol, held by convention.</b> noc_pkt.vh is the protocol; nothing includes it, so agreement is by hand",
      _tone: "bad",
    },
    { thing: "what an instruction <i>means</i>", cat: "<b>yours</b>" },
    {
      thing:
        "your unit's memories: count, width, depth, read latency, primitive",
      cat: "<b>yours</b>, entirely",
    },
    {
      thing: "how many credits your endpoint holds, and its reassembly buffer",
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
    { n: "what an instruction means", w: "you, the compute-unit author" },
    { n: "descriptors, addresses, memory semantics", w: "sysnode" },
    { n: "DRAM, host DMA, anything AXI", w: "axi" },
    {
      n: "how many credits an endpoint holds, and its reassembly buffer",
      w: "the endpoint. The fabric defines that credits are required, not how many",
    },
    {
      n: "clock domain crossing on a <b>local</b> link",
      w: "the endpoint. <b>Router-to-router is one clock</b> — which is what the acyclic argument rests on — but a local link may cross domains through <span class='chip'>noc_local_cdc</span>, one async FIFO per direction. The generator uses it to run compute units at their own rate",
    },
    { n: "which coordinate a given endpoint occupies", w: "ship" },
    { n: "where a router is placed, and what a link may cross", w: "physical" },
    {
      n: "carrying flits between meshes",
      w: "ship, through the interlink. The fabric ends at the mesh edge",
    },
  ],
};

/* ================================================================= mesh capacity */
const capacity = {
  cols: [
    { key: "c", label: "Constraint" },
    { key: "d", label: "Detail" },
  ],
  rows: [
    {
      c: "<b>The four corners must be empty.</b>",
      d: 'A corner touches no router, so an endpoint there would have nowhere to attach. This has a protocol consequence: because (0,0) can never hold an endpoint, <b>zero is a safe sentinel</b> in any field that names a node — several message classes use it to mean "reply to the sender" rather than "reply to node zero".',
    },
    {
      c: "<b>The memory agent occupies at least one slot and at most four.</b>",
      d: "Port count is a bandwidth decision, and it is the main one you make here: each port has its own read path and therefore its own instance of the read-path transform. Every unit bound to a port contends for that one transform.",
    },
    {
      c: "<b>The control plane occupies none.</b>",
      d: "The dispatch orchestrator has no node of its own — it lives behind the memory agent's ports, and each port's inbound flits are demultiplexed by type. Do not budget an endpoint for control.",
    },
  ],
};
</script>

<template>
  <DocPage
    title="Mesh and routers"
    summary="The flit, the link, the router, and the port every compute unit attaches through. The fabric moves messages between endpoints; it does not know what any of them mean."
    domain="framework"
    status="shipped"
    source="src/kohakuaccel/noc/ · docs/arch/noc/ · docs/spec/flit-format.md · docs/spec/compute-unit-port.md"
  >
    <h2 class="doc-h2">What it owns</h2>
    <p class="doc-p">Four things, and nothing else.</p>
    <div class="grid gap-3 sm:grid-cols-2 my-5">
      <div class="card p-4">
        <div
          class="kt-text-caption font-semibold text-warm-800 dark:text-warm-200 mb-1"
        >
          The flit
        </div>
        <p class="kt-text-caption text-warm-500 dark:text-warm-400 leading-6">
          One fixed-width message, one cycle on a link. A routing header and a
          payload the network never reads.
        </p>
      </div>
      <div class="card p-4">
        <div
          class="kt-text-caption font-semibold text-warm-800 dark:text-warm-200 mb-1"
        >
          The link
        </div>
        <p class="kt-text-caption text-warm-500 dark:text-warm-400 leading-6">
          A <span class="chip">data</span> / <span class="chip">valid</span> /
          <span class="chip">busy</span> triple with a retry rule.
        </p>
      </div>
      <div class="card p-4">
        <div
          class="kt-text-caption font-semibold text-warm-800 dark:text-warm-200 mb-1"
        >
          The router
        </div>
        <p class="kt-text-caption text-warm-500 dark:text-warm-400 leading-6">
          Five ports, dimension-order routing, round-robin arbitration.
        </p>
      </div>
      <div class="card p-4">
        <div
          class="kt-text-caption font-semibold text-warm-800 dark:text-warm-200 mb-1"
        >
          The compute-unit port
        </div>
        <p class="kt-text-caption text-warm-500 dark:text-warm-400 leading-6">
          <span class="chip">noc_cu_base</span> — the module that makes an
          endpoint a legal node without its author writing any network logic.
        </p>
      </div>
    </div>

    <p class="doc-p">
      A machine with tens of compute units needs a way to get instructions to
      them and operands in and out, and AXI is the wrong shape twice over.
      AXI4-Full wide enough to feed tens of units is a crossbar whose cost grows
      with masters times slaves, and it carries machinery this kind of machine
      never uses: out-of-order completion by ID, burst reordering, exclusive
      access, four independent channel handshakes per transaction. AXI4-Lite
      drops all of that and drops the bandwidth with it. The cost of building
      the narrower thing is that nothing off the shelf speaks it, so every
      bridge to the outside world is written by hand.
    </p>

    <h2 class="doc-h2">What a router costs</h2>
    <p class="doc-p">
      A router is not a crossbar of storage. It is five input ports and five
      output ports, and the flip-flop count is set by how many
      <span class="chip">FLIT_WIDTH</span>-wide registers exist per router — the
      number the design has been repeatedly shaped to reduce.
    </p>
    <Fig
      caption="One input port and one output port of five. Routing is computed on the FIFO output, one cycle before the flit is offered, so it is off the arbitration path entirely."
      zoom
      wide
    >
      <BlockDiagram
        :nodes="router.nodes"
        :edges="router.edges"
        :groups="router.groups"
      />
    </Fig>

    <Callout
      kind="trap"
      title="The holding slot is one per input port, not one per output direction"
    >
      <p>
        Two flits bound for the same output were already serialised, so
        per-direction slots only helped when successive flits diverged — and
        they cost <b>twenty-five</b> <span class="chip">FLIT_WIDTH</span> buses
        instead of five. The trade is head-of-line blocking on a congested
        direction against two thirds of the router's flip-flops, taken
        deliberately. Virtual channels are the standard remedy if profiling ever
        shows it costs more than it saves.
      </p>
      <p>
        The slot is kept rather than removed, and that is the other half of the
        trade. Feeding the FIFO output straight to the arbiter saves more flops
        but puts FIFO read, route computation, arbitration and a 5:1
        <span class="chip">FLIT_WIDTH</span> mux into one combinational path.
      </p>
    </Callout>

    <Callout kind="note" title="One flit per cycle, not one every two">
      <p>
        Grants are withheld while the output register holds a flit the receiver
        has not taken, so nothing is popped on top of something that has not
        left. Both the input load term and the output
        <span class="chip">room</span> term are true on the cycle the register
        is being emptied, which is what sustains full rate.
      </p>
    </Callout>

    <SpecTable
      :cols="knobs.cols"
      :rows="knobs.rows"
      caption="The knobs that move fabric cost, in the order they matter."
    />

    <Callout
      kind="note"
      title="MEMORY_TYPE is a per-instance decision, not a global one"
    >
      <p>
        A flit buffer is wide and shallow, which is the shape distributed RAM is
        good at and the shape block RAM wastes: a block RAM's widest port is far
        narrower than a flit, so the primitive count is set by width and the
        depth then comes free. In a LUT-bound design with block RAM sitting near
        empty,
        <b
          >deliberately wasting block RAM to buy LUTs back is the correct
          trade</b
        >
        rather than an argument against it.
      </p>
      <p>
        The same reasoning appears inside <span class="chip">noc_cu_base</span>:
        its receive queue is flit-wide and dominates the module's LUTs, while
        its completion queue is narrow enough that block RAM loses outright — so
        the receive queue gets its own <span class="chip">RECV_MEM</span> knob
        rather than sharing one parameter that would force the wrong answer on
        one of them.
      </p>
    </Callout>

    <h2 class="doc-h2">
      The link handshake, and why both halves are mandatory
    </h2>
    <Callout kind="rule" title="Hold until taken">
      <p>
        A sender <b>MUST</b> hold <span class="chip">valid</span> high and
        <b>MUST</b> hold <span class="chip">data</span> unchanged until it
        observes a cycle in which the receiver's
        <span class="chip">busy</span> is low. It MUST NOT withdraw a flit it
        has offered. A receiver <b>MUST</b> accept a flit in exactly the cycles
        where <span class="chip">valid &amp;&amp; !busy</span>, and
        <b>MUST NOT</b> accept it in any other cycle.
      </p>
    </Callout>

    <WaveTrace
      :rows="hsBrokenSender.rows"
      :notes="hsBrokenSender.notes"
      variant="broken"
      label="Sender gives up — the flit is destroyed"
    />

    <WaveTrace
      :rows="hsBrokenReceiver.rows"
      :notes="hsBrokenReceiver.notes"
      variant="broken"
      label="Receiver accepts on “is there room” — the flit is duplicated"
    />

    <WaveTrace
      :rows="hsFixed.rows"
      :notes="hsFixed.notes"
      variant="fixed"
      label="Both halves — one flit per cycle, nothing lost, nothing duplicated"
    />

    <Callout
      kind="trap"
      title="Both faults are silent, and both land several modules away"
    >
      <p>
        In a memory system either error is permanent. A duplicated write beat
        overruns its slot's expected count; a dropped one leaves the slot short
        forever, so the source's next descriptor opens a second slot and its
        data binds to the older one. The symptom appears as a short burst or a
        wrong tile.
      </p>
      <p>
        This is why <span class="chip">sync_fifo</span>'s
        <span class="chip">wr_almost</span> output being no margin at all is
        survivable: <span class="chip">USE_ADV_FEATURES</span> is zero, so it
        reduces to plain <span class="chip">full</span>.
        <b>What makes plain full safe is the retry, not a margin.</b> Anything
        that needs a real margin counts for itself —
        <span class="chip">mag_mem_port</span> does, with
        <span class="chip">Q_MARGIN</span>.
      </p>
    </Callout>

    <Callout kind="rule" title="Three further rules on the mesh-facing port">
      <p>
        <span class="chip">noc_in_busy</span> <b>MUST</b> be a function of the
        endpoint's own state only. It MUST NOT depend on
        <span class="chip">noc_in_valid</span> or on any field of
        <span class="chip">noc_in_data</span> — deciding backpressure from the
        incoming flit's type means a flit the endpoint cannot classify right now
        blocks the port, and the mesh being in-order behind it, blocks the flit
        that would free the resource.
      </p>
      <p>
        An endpoint <b>MUST NOT</b> hold
        <span class="chip">noc_in_busy</span> high indefinitely: every condition
        that raises it MUST be cleared by something other than an inbound flit.
        An endpoint <b>MAY</b> assert
        <span class="chip">noc_out_valid</span> and
        <span class="chip">noc_in_busy</span> in the same cycle — the link is
        full duplex.
      </p>
    </Callout>

    <h2 class="doc-h2">The flit</h2>
    <p class="doc-p">
      One indivisible unit of transfer. There is no sub-flit granularity and no
      flit spans two cycles. The router reads
      <span class="chip">dst_x</span> and <span class="chip">dst_y</span> and
      nothing else — every other bit is opaque to the fabric, which is what
      keeps the router small and lets the message set change without touching
      the routing logic.
    </p>

    <BitField
      :fields="flitGeom"
      caption="Geometry at the defaults: FLIT_WIDTH = 288, POS_WIDTH = 4. The header is the top 4·POS_WIDTH + 16 bits; the payload is everything below it"
    />

    <BitField
      :fields="flitHeader"
      caption="The header, expanded. The numbers on the bar are offsets within the 32-bit header; the flit-absolute slice at the defaults is printed under each field name"
    />

    <SpecTable
      :cols="headerSpec.cols"
      :rows="headerSpec.rows"
      caption="The general expression is what the RTL computes, and it is the one to build against. noc_pkt.vh writes these as literals (287:284, …), correct only at FLIT_WIDTH = 288 and POS_WIDTH = 4 — so including it as it stands would silently pin a mesh to one flit width. POS_WIDTH = 4 caps a mesh at 16×16 coordinates, edge endpoints included, so the usable router grid is at most 14×14."
    />

    <Callout kind="trap" title="src_* MUST be the sender's own coordinates">
      <p>
        Three mechanisms depend on it and all three fail silently if it is
        wrong.
      </p>
      <p>
        1. <span class="chip">noc_cu_base</span> addresses an instruction's
        completion to the <span class="chip">src</span> of that instruction's
        flit. A wrong source sends the completion somewhere else, and the
        dispatch credit is never returned.
      </p>
      <p>
        2. <span class="chip">mag_mem_port</span> matches a write's data flits
        to its descriptor <b>by source coordinate alone</b>. A wrong source
        binds data to another node's write.
      </p>
      <p>
        3. A multi-flit receiver distinguishes two interleaved senders by
        source. A wrong source is indistinguishable from stream corruption.
      </p>
      <p>
        The interlink preserves <span class="chip">src_*</span> across a mesh
        boundary, so an "answer the sender" sentinel is meaningless on a remote
        burst.
      </p>
    </Callout>

    <h3 class="doc-h3">Message types</h3>
    <SpecTable :cols="types.cols" :rows="types.rows" />

    <Callout kind="trap" title="CU_DATA is 0x8, not 0x4">
      <p>
        <span class="chip">0x4</span> is <span class="chip">MEM_WR_DATA</span>.
        The two collided in an earlier revision, and a
        <span class="chip">CU_DATA</span> flit reaching the memory agent entered
        its write queue as data — <b>a silent wrong-bytes store</b>. Bit 3 no
        longer partitions memory traffic from unit traffic: five memory messages
        do not fit in four codes. <span class="chip">NOC_T_IS_MEM(t)</span> is
        <span class="chip">t &lt;= 4'h4</span>.
      </p>
      <p>
        <span class="chip">ERROR</span> (<span class="chip">0xF</span>) is
        declared and unimplemented. No module produces it and no module consumes
        it. A unit MUST NOT send it and MUST NOT expect one. Codes
        <span class="chip">0x9</span>–<span class="chip">0xE</span>
        are reserved to the framework: a unit that needs a private message class
        MUST use
        <span class="chip">CU_DATA</span> with a unit-defined
        <span class="chip">buf_id</span>, not an unallocated type code.
      </p>
      <p>
        <b>Take the codes from the header, never from a neighbouring module.</b>
        <span class="chip">noc_pkt.vh</span> is the protocol and it is correct,
        but <span class="chip">`include</span> appears nowhere in
        <span class="chip">src/</span> for it — every module restates the codes
        as local parameters, so a divergence between any two is silent. It has
        already happened twice. Once in shipping RTL, which is the collision
        above. And once still present:
        <span class="chip">noc_cu_null.v:49</span> declares
        <span class="chip">T_CU_DATA = 4'h4</span> and builds flits with it,
        which <span class="chip">NOC_T_IS_MEM</span> would classify as memory
        traffic.
      </p>
    </Callout>

    <h3 class="doc-h3">rsvd</h3>
    <SpecTable
      :cols="rsvd.cols"
      :rows="rsvd.rows"
      caption="Three bits, all framework-owned. A unit MUST transmit 3'b000 unless a rule above says otherwise. The two uses of rsvd[1:0] never collide: a MEM_RD_RESP never sets rsvd[2], and a remote flit is CU_DATA or MEM_WR_*. None of these three meanings is declared in noc_pkt.vh — they exist only as part-selects in the modules that use them, so this table is the declaration."
    />

    <h3 class="doc-h3">Payload layouts</h3>
    <p class="doc-p">
      All positions are absolute within the default 256-bit payload. A build
      with a different
      <span class="chip">FLIT_WIDTH</span> or
      <span class="chip">POS_WIDTH</span> changes the payload width, and these
      do not carry over unchanged. Several of these fields —
      <span class="chip">count</span>, <span class="chip">peer</span>,
      <span class="chip">n_peer</span> and
      <span class="chip">entry_words</span> on a read descriptor,
      <span class="chip">ack_y</span>/<span class="chip">ack_x</span> on
      <span class="chip">CU_DATA</span> — have no macro anywhere and exist only
      as literal part-selects in the modules that read them. These layouts are
      their specification.
    </p>

    <BitField
      :fields="plMemReq"
      caption="MEM_RD_REQ (0x0) / MEM_WR_REQ (0x1) descriptor. On a MEM_WR_REQ only addr and len are read; flags, count, peer, n_peer and entry_words are read on MEM_RD_REQ only. addr is 40 bits and the whole of it: [39] selects a command aperture rather than DRAM, [38] is reserved, and [37:36] names the mesh a request is aimed at"
    />
    <SpecTable
      :cols="flags.cols"
      :rows="flags.rows"
      caption="MEM_RD_REQ flags, payload bits 207:200. Bits 4 and 5 used to select a format conversion on the fetch; both are now RESERVED AND IGNORED. A fetch is never transformed — operands arrive in their final format, converted beforehand by the memory mover through the transform slot — so a requester that still sets them gets an untransformed fetch, which is the correct answer"
    />

    <BitField
      :fields="plCuData"
      caption="CU_DATA (0x8) descriptor. A burst is one descriptor flit followed by len + 1 pure data flits — 1 to 256 of them — and the data flits are 256 bits of payload each. offset advances by one granule per data flit"
    />

    <BitField
      :fields="plSignal"
      caption="CU_SIGNAL (0x6). arg is always unit-defined content, whatever the code"
    />

    <BitField
      :fields="plCtrlReply"
      caption="CU_CTRL (0x7) reply. The request carries op (MUST be 0; noc_cu_base does not read it) and index, and nothing else"
    />

    <Callout
      kind="trap"
      title="{ack_y, ack_x} == 0 means “send the completion to the descriptor's source”"
    >
      <p>
        <span class="chip">(0,0)</span> is a safe sentinel because it is a mesh
        corner, which touches no router and can hold no endpoint — the mesh
        generator rejects a map that puts anything there. But a completion
        addressed at the sender is
        <b>useless when the sender is another compute unit</b>: nothing there
        consumes it, so nothing can sequence a reader behind a writer. A
        unit-to-unit transfer <b>SHOULD</b> point
        <span class="chip">ack</span> at the orchestrator instead, and a burst
        that crosses a mesh boundary <b>MUST</b> name an explicit ack, because
        the source coordinate is preserved and the sentinel would resolve to a
        node in the wrong mesh.
      </p>
      <p>
        <span class="chip">flags[0]</span> set makes the receiver emit
        <span class="chip">SIG_DATA_RECEIVED</span> when the burst completes.
        Without it a unit-to-unit transfer is unobservable: the framework
        signals on instruction retirement and a burst is not an instruction, so
        a sender that waits would wait forever.
      </p>
      <p>
        A receiver <b>MUST</b> range-check
        <span class="chip">offset + len</span> against the named buffer and
        <b>MUST NOT</b> wrap. A rejected burst <b>MUST</b> still be counted out
        to its <span class="chip">last</span> flit — otherwise the next data
        flit is read as a descriptor and the damage spreads — and
        <b>SHOULD</b> still be acknowledged, or the sender waits forever.
      </p>
    </Callout>

    <h3 class="doc-h3">buf_id is a framework namespace</h3>
    <p class="doc-p">
      Not a free field: a unit does not pick its own numbering. The routers
      never interpret it, but a sender naming a destination buffer has to mean
      the same thing the receiver does, and framework services need indices they
      can rely on across unit types. This table is the allocation — in the
      source it survives only as
      <span class="chip">localparam</span>s inside one accelerator's cluster and
      as a bare rejection inside another's, neither of which a second
      accelerator's author would think to read.
    </p>
    <SpecTable :cols="bufIds.cols" :rows="bufIds.rows" />
    <Callout kind="rule" title="Two absolute rules follow">
      <p>
        A unit with fewer buffers than the table has entries <b>MUST</b> map its
        buffers onto the low indices in order and <b>MUST</b> reject every other
        index, rather than aliasing an unallocated index onto something it does
        have. A unit with one flat buffer answers at
        <span class="chip">0</span> and faults on everything else.
      </p>
      <p>
        A unit <b>MUST</b> publish, in its own documentation, which indices it
        accepts and what each holds. Index <span class="chip">2</span> in
        particular carries the unit's <i>internal</i> accumulation format, which
        differs between units by construction — one field names it, and there is
        deliberately no second bit that could disagree.
      </p>
    </Callout>

    <h3 class="doc-h3">CU_SIGNAL code allocation</h3>
    <SpecTable
      :cols="signals.cols"
      :rows="signals.rows"
      caption="Codes below 0x40 are centrally allocated so a controller can act on any unit's signals without knowing what that unit is. A unit MUST NOT emit 0x00, 0x01 or 0x04 — the framework emits those, and a duplicate returns a dispatch credit that was never spent."
    />

    <h2 class="doc-h2">Routing and the coordinate space</h2>
    <p class="doc-p">
      Positions are <span class="chip">(x, y)</span> in a
      <span class="chip">2^POS_WIDTH</span> square. Routers occupy an inner
      rectangle, and the two upper bounds are per-axis, so a mesh need not be
      square. Endpoints sit in one of two places: on a router's
      <b>local</b> port, or just outside the router rectangle on the
      <b>edge ring</b>, hanging off a router's otherwise unused directional
      port.
    </p>

    <Fig
      caption="A mesh is described as a picture, one three-character token per position, and a generator turns it into a top module. The interior is the router grid; the first and last rows are the north and south edges, the first and last columns the west and east edges. xxx is nothing; nul is a port that exists but is tied off — which is how you leave a side empty without changing the grid's shape."
      zoom
    >
      <BlockDiagram
        :nodes="meshMap.nodes"
        :edges="meshMap.edges"
        :groups="meshMap.groups"
      />
    </Fig>

    <p class="doc-p">
      The edge ring is where gateways go, and that is why it exists: a memory
      port at
      <span class="chip">(0, y)</span> draws traffic to router
      <span class="chip">(GRID_LO, y)</span> and to no other, so several
      gateways on different rows genuinely split the load instead of splitting
      one funnel. An edge endpoint costs a link, not a router — the lever that
      lets a grid carry considerably more endpoints than it has routers.
      Capacity of an <span class="chip">NX × NY</span> grid is
      <span class="chip">NX*NY</span> router locals plus
      <span class="chip">2*(NX+NY)</span>
      edge slots.
    </p>

    <SpecTable
      :cols="capacity.cols"
      :rows="capacity.rows"
      caption="Three constraints on filling it."
    />

    <h3 class="doc-h3">The clamp</h3>
    <p class="doc-p">
      An edge endpoint cannot literally finish X routing, because X terminates
      at a position that is not a router.
      <span class="chip">noc_inport.v</span> resolves this by routing toward the
      <b>clamped</b> destination — the router adjacent to the target — and
      taking the outward hop only on arrival.
    </p>

    <pre
      class="card p-4 overflow-x-auto font-mono kt-text-caption leading-6 text-warm-700 dark:text-warm-300"
    >
wire [POS_WIDTH-1:0] r_pos_x = (pos_x &lt; LO) ? LO : (pos_x &gt; XHI) ? XHI : pos_x;
wire [POS_WIDTH-1:0] r_pos_y = (pos_y &lt; LO) ? LO : (pos_y &gt; YHI) ? YHI : pos_y;</pre>

    <Fig
      caption="The dashed box is the router rectangle: GRID_LO to GRID_X_HI in x, GRID_LO to GRID_Y_HI in y. Route X until the column matches, then Y, then at most one outward hop. S and D are edge endpoints outside it, so the flit routes toward the clamped destination — the router adjacent to the target — and takes the outward hop only on arrival. The alternative, routing Y-first for edge destinations, would mix XY and YX in one network and put back the cycles XY exists to prevent."
      zoom
    >
      <BlockDiagram :nodes="xy.nodes" :edges="xy.edges" :groups="xy.groups" />
    </Fig>

    <Callout
      kind="rule"
      title="XY routing is a proof rather than a test result"
    >
      <p>
        The Y-to-X turn never happens, so the channel dependency graph is
        acyclic, so the fabric cannot deadlock on buffer dependencies.
        <b
          >That property does not come from buffer depth. Deeper FIFOs make a
          deadlock rarer and harder to find; they do not remove one.</b
        >
      </p>
      <p>Two consequences follow, and both are load-bearing elsewhere:</p>
      <p>
        <b>Delivery is in order per source-destination pair.</b> The path is
        deterministic. This is what lets the memory system reassemble by
        counting instead of by sequence number, and what lets a multi-flit
        message be a descriptor followed by data.
      </p>
      <p>
        <b>Buffers only have to cover the backpressure round trip.</b> They are
        not sized against deadlock, because nothing about deadlock depends on
        them.
      </p>
    </Callout>

    <Callout kind="note" title="The turn masks are derived, not passed in">
      <p>
        <span class="chip">noc_router.v</span> turns the acyclic argument into
        logic: each input port's request vector is masked by a
        <span class="chip">*_KILL</span> constant derived from the clamp bounds,
        killing the turns XY can never ask for. The masks are computed from
        <span class="chip">GRID_LO</span> /
        <span class="chip">GRID_X_HI</span> /
        <span class="chip">GRID_Y_HI</span> rather than passed in as a second
        parameter,
        <b
          >because a separately supplied parameter is a second place for the
          topology to be wrong</b
        >. Outside synthesis the router also reports a masked request that was
        actually made — a wrong mask presents as a hang several modules away, so
        it is named where it happens.
      </p>
    </Callout>

    <Callout
      kind="note"
      title="What the generator does that you would otherwise do by hand"
    >
      <p>
        It instantiates the routers with the right per-axis clamps, wires every
        link with exactly one driver per direction, instantiates each endpoint
        with its coordinates, and
        <b>ties off every link nothing claimed</b>. The fourth is the one that
        catches fire when done manually: an unclaimed link has an undriven
        direction — data, valid, and the busy coming back at it all float — and
        which direction that is depends on which side of the router it sits on.
      </p>
      <p>
        It also assigns each endpoint its memory agent port, by nearest clamped
        Manhattan distance. A unit is therefore
        <b>bound to one memory port at elaboration</b>, not at runtime.
      </p>
      <p>
        <b>It supplies both reset polarities, which a hand-written top must do
        for itself.</b> One mesh carries two conventions:
        <span class="chip">noc_cu_base</span>,
        <span class="chip">mag</span> and
        <span class="chip">noc_orchestrator</span> take
        <span class="chip">resetn</span>, active low and synchronous, while
        <span class="chip">noc_router</span>,
        <span class="chip">noc_inport</span> and
        <span class="chip">noc_outport</span> take
        <span class="chip">rst</span>, active high — and the two port modules use
        it <b>asynchronously</b>. A mesh top MUST derive both from one release,
        or half the fabric leaves reset on a different cycle from the other half.
      </p>
    </Callout>

    <h2 class="doc-h2">Multi-flit framing</h2>
    <Callout kind="rule" title="Frame by type, never by position">
      <p>
        A multi-flit message is a descriptor flit followed by pure data flits.
        Data flits
        <b>MUST</b> be identifiable
        <b>by their <span class="chip">type</span> code</b>, never by their
        position after a descriptor.
      </p>
      <p>
        The mesh interleaves. Another node's flit can land between a descriptor
        and its data at any point, and there is no mechanism that prevents it.
        The cost is one type code per multi-flit class —
        <span class="chip">MEM_WR_REQ</span>/<span class="chip"
          >MEM_WR_DATA</span
        >
        is the pair — in exchange for framing that cannot be broken by
        arbitration.
      </p>
    </Callout>

    <WaveTrace
      :rows="framingBroken.rows"
      :notes="framingBroken.notes"
      variant="broken"
      label="Two CU_DATA senders, framed by position"
    />
    <WaveTrace
      :rows="framingFixed.rows"
      :notes="framingFixed.notes"
      variant="fixed"
      label="Framed by count, disambiguated by source coordinate"
    />

    <h2 class="doc-h2">The compute-unit port</h2>
    <p class="doc-p">
      This is where the framework earns its keep. Everything else in this system
      is infrastructure that exists so
      <span class="chip">src/kohakuaccel/noc/endpoint/noc_cu_base.v</span> can
      offer a small, stable handshake — and so that you never have to work out
      how to connect to the fabric. You still write the whole unit; you do not
      write the connection.
    </p>
    <SpecTable
      :cols="portSignals.cols"
      :rows="portSignals.rows"
      caption="The datapath handshake. A unit MAY implement the mesh-facing port directly and skip noc_cu_base; it then owes every obligation itself, including CU_CTRL replies, completion signalling and reply addressing. No unit in the tree does it."
    />

    <SpecTable
      :cols="inbound.cols"
      :rows="inbound.rows"
      caption="Every inbound flit is classified by its type field and goes to exactly one place. noc_in_busy is asserted when EITHER FIFO is full, not the one the arriving flit would enter — because busy has to be meaningful in cycles when noc_in_valid is low, and the type field is only trustworthy alongside a valid flit. Consequence a unit MUST plan for: a receive queue the unit stops draining will stall the instruction stream as well."
    />

    <h3 class="doc-h3">
      Never raise exec_done in the same cycle as inst_ready
    </h3>
    <Callout kind="rule" title="Leave at least one cycle between them">
      <p>
        The unit <b>MUST</b> assert <span class="chip">exec_done</span> exactly
        once for each accepted instruction — never twice, never zero times — and
        <b>MUST NOT</b> assert it in the same cycle as
        <span class="chip">inst_ready</span>. The base clears
        <span class="chip">in_flight</span> on that arm, so the newly accepted
        instruction's own completion would find
        <span class="chip">in_flight</span> low and never be queued: a
        permanently lost dispatch credit. An
        <span class="chip">exec_done</span> asserted while no instruction is in
        flight is <b>discarded</b> — no <span class="chip">CU_SIGNAL</span> and
        no credit.
      </p>
      <p>
        The unit also <b>MUST NOT</b> accept an instruction it cannot retire in
        bounded time without further external input that is not guaranteed to
        arrive.
      </p>
    </Callout>
    <WaveTrace
      :rows="retireBroken.rows"
      :notes="retireBroken.notes"
      variant="broken"
      label="Both arms in one cycle — one dispatch credit lost permanently"
    />
    <WaveTrace
      :rows="retireFixed.rows"
      :notes="retireFixed.notes"
      variant="fixed"
      label="One cycle between them"
    />

    <h3 class="doc-h3">Completions are queued, not held</h3>
    <WaveTrace
      :rows="compBroken.rows"
      :notes="compBroken.notes"
      variant="broken"
      label="One holding register — each completion overwrites the last"
    />
    <WaveTrace
      :rows="compFixed.rows"
      :notes="compFixed.notes"
      variant="fixed"
      label="A 16-deep completion queue"
    />

    <Callout kind="rule" title="What the base does, so the unit MUST NOT">
      <p>
        <b>The reply address comes from the instruction.</b>
        <span class="chip">noc_cu_base</span> latches
        <span class="chip">src_x</span> / <span class="chip">src_y</span> /
        <span class="chip">txn</span> / <span class="chip">last</span> from the
        <span class="chip">CU_INST</span> flit it issues, and answers there. A
        unit is never configured with its controller's coordinate, so moving the
        controller is not a rebuild of every unit.
      </p>
      <p>
        <b>Control-register reads are answered here, not in the datapath.</b>
        That is what lets a controller enumerate a unit it has never heard of:
        capabilities, status with live instruction-queue space, a busy-cycle and
        retired-instruction pair counted identically for every unit type, and
        one 64-bit word the datapath supplies. Counting cycles in the framework
        rather than in each unit is deliberate — wall clock cannot substitute
        when a single debug read costs milliseconds against microseconds of
        compute.
      </p>
      <p>
        <b
          >Transmit arbitration has a fixed priority: signals, then control
          replies, then the datapath.</b
        >
        Signals win because they return dispatch credits, so starving them
        stalls the controller. The <span class="chip">tx_free</span> term covers
        the output register being emptied <i>this</i> cycle rather than merely
        idle, because deciding from
        <span class="chip">!noc_out_busy</span> alone pops a signal against a
        link that may be busy by the time the flit is presented — and a lost
        signal never returns its credit.
      </p>
    </Callout>

    <Callout kind="trap" title="The receive path may block, but only boundedly">
      <p>
        Every condition that holds <span class="chip">recv_ready</span> low
        <b>MUST</b> be clearable by the send path, a timer, or unit-internal
        progress. It <b>MUST NOT</b> require another inbound flit. A unit whose
        receive path waits on an inbound flit deadlocks the mesh, not just
        itself: <span class="chip">noc_in_busy</span> goes high, the link
        stalls, and in-order delivery means everything behind it on that link
        stalls too.
      </p>
      <p>
        A flit of a type the unit does not understand <b>MUST</b> be accepted
        and dropped, not held. Held, it sits at the head of the receive FIFO,
        raises <span class="chip">noc_in_busy</span> for good, and wedges the
        instruction stream behind it. <span class="chip">MEM_WR_ACK</span> is
        the specific case every writing unit hits — nothing consumes it, and a
        unit that issues writes MUST dispose of the acks.
      </p>
    </Callout>

    <h3 class="doc-h3">The measurement instrument</h3>
    <p class="doc-p">
      <span class="chip">src/kohakuaccel/noc/endpoint/noc_cu_null.v</span>
      attaches to the fabric and computes nothing. Its job is to isolate what
      being <i>connected</i> costs before any arithmetic exists — the number
      that decides between many small units and few large ones. Subtract it from
      a real unit and the remainder is genuinely compute; that subtraction is
      the input to choosing a mesh shape. It is written to defeat synthesis
      pruning, which is the whole reason it can be trusted: every bit of both
      flits folds into an output, and traffic originates from external inputs so
      the mesh cannot be proven idle and constant-folded.
    </p>
    <Callout kind="trap" title="It is an instrument, and MUST NOT be copied as a skeleton">
      <p>
        Two independent reasons, and the second is not cosmetic.
        <span class="chip">noc_cu_null.v:49</span> carries the mistyped
        <span class="chip">T_CU_DATA = 4'h4</span> described above, so an author
        who copied it would ship a flit that a memory port classifies as write
        data — discovered on their first unit-to-unit message. And it raises
        <span class="chip">inst_ready</span> and
        <span class="chip">exec_done</span> in the same cycle, which is exactly
        what the retire rule below forbids: the base clears
        <span class="chip">in_flight</span> on that arm, so it accepts one
        instruction and then never accepts another.
      </p>
      <p>
        Neither invalidates any measurement. The module is instantiated only by
        measurement tops with no memory agent present, so nothing ever
        classifies a flit it emits and nothing ever dispatches a second
        instruction to it. Start from a shipping unit's port wiring instead.
      </p>
    </Callout>

    <h2 class="doc-h2">
      Two kinds of flow control, for two different failures
    </h2>
    <p class="doc-p">
      <b>Hop-by-hop</b> is the link handshake. It stops buffer overflow.
      <b>End-to-end credit</b> stops <i>protocol</i> deadlock, which hop-by-hop
      cannot touch: if a node's input fills with requests and it cannot inject
      the response that would drain them, the fabric locks — and that is a
      dependency between message classes, which routing does not address.
    </p>
    <Fig
      caption="A credit is one response the requester can absorb, or one instruction the target's queue can still hold. Credits live at the endpoints, not in the router: the router contains no counter and no notion of message class. This is the single most important thing to understand about the fabric's cost — making it deadlock-free cost logic at the edges and nothing in the middle."
      zoom
      wide
    >
      <BlockDiagram :nodes="fc.nodes" :edges="fc.edges" />
    </Fig>
    <Callout kind="rule" title="The rule">
      <p>
        A requester may not issue a request whose response it cannot absorb, and
        a dispatcher may not send an instruction the target's queue cannot hold.
      </p>
    </Callout>

    <h2 class="doc-h2">Choosing a mesh</h2>
    <h3 class="doc-h3">One port or two</h3>
    <p class="doc-p">
      A unit gets one local port unless you deliberately give it two, and the
      default is right far more often than it looks. The reference instance's
      largest unit was originally two endpoints on adjacent routers — a manager
      and an accumulator — and was merged into one.
      <b
        >A second port buys no bandwidth if both endpoints load the same
        direction of the link</b
      >: the link is full duplex, and the manager's fetches and the
      accumulator's drains were opposite directions of one link, so one port
      carried both at no contention. A second port also costs a router local,
      and locals are the scarce resource. Take a second port when the two
      directions genuinely conflict — a unit that both streams operands in
      <i>and</i> streams results out continuously, at rates that together exceed
      one link.
    </p>

    <h3 class="doc-h3">What an endpoint costs</h3>
    <p class="doc-p">
      Two costs scale differently, and conflating them gives the wrong answer.
      <b>The endpoint framework is cheap</b> —
      <span class="chip">noc_cu_base</span> is three flit queues and a little
      logic, and attaching one more unit to an existing router is nearly free.
      <b>Routers are not, and router count follows endpoint count.</b> The real
      cost of "one more endpoint" is mostly the fraction of a router it forces.
      So prefer coarse units: three small units behind one port, one framework
      instance and one local memory cost far less than three endpoints.
      Granularity and specialisation are different axes — choosing "one unit
      does one thing" does not oblige you to choose "many small units".
    </p>

    <h3 class="doc-h3">A procedure</h3>
    <ol class="doc-p list-decimal pl-5 space-y-1">
      <li>
        <b>Count endpoints.</b> Your units, plus one to four memory ports. Not
        the control plane.
      </li>
      <li>
        <b>Merge until coupling is loose.</b> Anything two endpoints must do
        together within a few cycles should be one endpoint.
      </li>
      <li>
        <b
          >Pick the smallest grid whose capacity —
          <span class="chip">NX*NY + 2*(NX+NY)</span>, corners excluded — holds
          them</b
        >, filling edge slots before growing the grid.
      </li>
      <li>
        <b>Place memory ports near the traffic</b>, remembering each unit is
        bound to its nearest one at elaboration.
      </li>
      <li>
        <b>Check it fits one die</b>, with room for the host infrastructure —
        DMA, AXI interconnect and a memory controller are not free, and one of
        your dies also pays for the host interface.
      </li>
      <li><b>If it does not fit, add a mesh, not rows.</b></li>
      <li>
        <b>Generate, elaborate, and measure out of context</b> before believing
        any of it.
      </li>
    </ol>

    <Callout kind="open" title="Open questions the flow does not answer">
      <p>
        The map vocabulary is not extensible without editing the generator —
        today
        <span class="chip">scripts/py/gen_mesh.py</span> knows
        <span class="chip">mag</span>, <span class="chip">vec</span> and
        <span class="chip">mat</span>, and emits those module names with those
        parameters, which is the wrong seam: the vocabulary should come from the
        project.
      </p>
      <p>
        Nothing checks a map against the device it is meant for — endpoint
        count, grid size and die capacity are related by arithmetic nobody
        performs until synthesis fails. The memory port assignment is
        nearest-by-hops and nothing else, so a map whose traffic is skewed
        toward one port gets no warning. And endpoint placement within a die is
        unmodelled: a within-die layout change has been observed to cost real
        performance through routing alone, and no tool in the flow predicts that
        from a map.
      </p>
    </Callout>

    <h2 class="doc-h2">Conventions</h2>
    <SpecTable :cols="conventions.cols" :rows="conventions.rows" />

    <h2 class="doc-h2">Fixed protocol, addon, convention, or yours</h2>
    <SpecTable :cols="categories.cols" :rows="categories.rows" />

    <h2 class="doc-h2">What this system does not own</h2>
    <SpecTable :cols="notOwned.cols" :rows="notOwned.rows" />
    <p class="doc-p">
      The last one is worth stating twice. A flit for another mesh is recognised
      at the edge complex by a marker bit and handed to the interlink;
      <b>the router never knows another mesh exists</b>. Extending the fabric
      across a die boundary was tried and rejected on measurement.
    </p>
  </DocPage>
</template>
