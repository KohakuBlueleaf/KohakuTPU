<script setup>
// ---------------------------------------------------------------- overview
const overview = {
  /* Horizontal flow, so the components are VERTICAL: the eye follows mesh ->
   * hub -> clients -> the internal protocol -> the ports -> the memories, and
   * each column is read top to bottom. As a stack of full-width bars this was
   * six levels tall and taller than a screen. */
  groups: [
    {
      x: -2.5,
      y: -3.2,
      w: 100,
      h: 33,
      label: "the system node — ONE component, one per mesh",
    },
    /* Starts right of sn_hub so its label is not hidden behind the hub. */
    { x: 25.5, y: 7.8, w: 67, h: 21.1, label: "MAG — the memory gateway" },
    {
      x: 28.5,
      y: -2.2,
      w: 32,
      h: 8.6,
      label: "the control complex",
    },
  ],
  nodes: [
    {
      id: "mesh",
      x: 0,
      y: 8,
      w: 8.5,
      h: 11,
      label: "the mesh",
      sub: "compute units · routers",
      accent: true,
    },
    {
      id: "hub",
      x: 15.5,
      y: -1,
      w: 8.5,
      h: 29,
      label: "sn_hub",
      sub: "the ONLY thing that owns an attachment · demux in · steer out by row",
      accent: true,
    },
    {
      id: "cpu",
      x: 30,
      y: -1,
      w: 11,
      h: 6.4,
      label: "the processor",
      sub: "RV32 or RV64 · imem · spad · L1",
      accent: true,
    },
    {
      id: "hostm",
      x: 48,
      y: -1,
      w: 11,
      h: 6.4,
      label: "mover · transform",
      sub: "the processor's SIMD memory unit",
    },
    {
      id: "eng",
      x: 30,
      y: 9,
      w: 11,
      h: 8.4,
      label: "memory engines × PORTS",
      sub: "intake · read engine · write slots",
      accent: true,
    },
    {
      id: "edge",
      x: 30,
      y: 19.4,
      w: 11,
      h: 8.4,
      label: "control agent · interlink",
      sub: "host reach and cross-mesh",
    },
    {
      id: "hostw",
      x: 48,
      y: 9,
      w: 11,
      h: 8.4,
      label: "host memory window",
      sub: "bursty and rare, its own channel",
    },
    {
      id: "q",
      x: 48,
      y: 19.4,
      w: 11,
      h: 8.4,
      label: "internal request protocol",
      sub: "q_valid / q_ready / q_addr / q_len",
    },
    {
      id: "stagep",
      x: 66,
      y: 9,
      w: 11,
      h: 8.4,
      label: "mag_stage_port",
      sub: "ADDON · claims the aperture",
    },
    {
      id: "dramp",
      x: 66,
      y: 19.4,
      w: 11,
      h: 8.4,
      label: "mag_dram_port",
      sub: "arbitrate · pack width · cross clock",
      accent: true,
    },
    {
      id: "stage",
      x: 84,
      y: 9,
      w: 11,
      h: 8.4,
      label: "mag_stage",
      sub: "URAM · 4 banks × 16,384",
    },
    {
      id: "dram",
      x: 84,
      y: 19.4,
      w: 11,
      h: 8.4,
      label: "M_AXI_DRAM",
      sub: "the agent's ONE AXI master",
    },
  ],
  edges: [
    { from: "mesh:r", to: "hub:l", accent: true },
    { from: "hub:r", to: "cpu:l", label: "dst (0,0)", accent: true },
    { from: "hub:r", to: "eng:l", label: "memory", accent: true },
    { from: "hub:r", to: "edge:l", label: "remote" },
    { from: "cpu:r", to: "hostm:l", label: "mv.go" },
    { from: "eng:r", to: "q:l", accent: true },
    { from: "edge:r", to: "q:l", label: "interlink", dash: true },
    /* Three wires on q's top: the mover is listed first so it takes the
     * left slot and wraps round the left of the host window, the window
     * drops straight into the middle slot, and the aperture leaves from the
     * right slot up the free corridor to mag_stage_port. */
    { from: "hostm:b", to: "q:t", label: "MV" },
    { from: "hostw:b", to: "q:t" },
    /* Leaves the processor's right side so it does not run through the MAG
     * group label under the processor. */
    { from: "cpu:r", to: "q:l", label: "cp_*" },
    { from: "q:r", to: "dramp:l", accent: true },
    { from: "q:t", to: "stagep:l", label: "aperture" },
    { from: "stagep:b", to: "dramp:t", label: "not staged" },
    { from: "stagep:r", to: "stage:l" },
    { from: "dramp:r", to: "dram:l", accent: true },
  ],
};

// ------------------------------------------------------------ the port
const port = {
  nodes: [
    {
      id: "in",
      x: 0,
      y: 0,
      w: 40,
      h: 3,
      label: "flits in, at this port's coordinate",
      accent: true,
    },
    {
      id: "demux",
      x: 0,
      y: 5,
      w: 40,
      h: 3.4,
      label: "demux by type",
      sub: "mem_in_busy is this port's own occupancy, and nothing else",
    },
    {
      id: "qrd",
      x: 14,
      y: 10.5,
      w: 12,
      h: 3.4,
      label: "read queue",
      sub: "MEM_RD_REQ",
    },
    {
      id: "qwr",
      x: 28,
      y: 10.5,
      w: 12,
      h: 3.4,
      label: "write queue",
      sub: "MEM_WR_REQ + MEM_WR_DATA",
    },
    {
      id: "axir",
      x: 0,
      y: 16,
      w: 12,
      h: 3.4,
      label: "AXI read",
      sub: "address accumulated, never base + n × size",
    },
    {
      id: "eng",
      x: 14,
      y: 16,
      w: 12,
      h: 3.4,
      label: "read engine",
      sub: "own state, own return context",
      accent: true,
    },
    {
      id: "slots",
      x: 28,
      y: 16,
      w: 12,
      h: 3.4,
      label: "write slot array",
      sub: "WR_SLOTS × WBURST beats, keyed by src",
    },
    {
      id: "axiw",
      x: 28,
      y: 21.5,
      w: 12,
      h: 3.4,
      label: "AXI write",
      sub: "one burst per slot, beats contiguous",
    },
    {
      id: "emit",
      x: 14,
      y: 27,
      w: 12,
      h: 3.4,
      label: "emit buffer",
      sub: "latched, so the next AR can start",
    },
    {
      id: "out",
      x: 0,
      y: 32.5,
      w: 40,
      h: 3.4,
      label: "one output register",
      sub: "the emitter outranks the plain-read and write-ack path",
      accent: true,
    },
  ],
  edges: [
    { from: "in:b", to: "demux:t" },
    { from: "demux:b", to: "qrd:t" },
    { from: "demux:b", to: "qwr:t" },
    { from: "qrd:b", to: "eng:t" },
    { from: "qwr:b", to: "slots:t" },
    { from: "eng:l", to: "axir:r", dir: "h" },
    { from: "axir:b", to: "emit:t", label: "at line rate" },
    { from: "emit:b", to: "out:t", accent: true },
    { from: "slots:b", to: "axiw:t" },
    { from: "axiw:b", to: "out:r", label: "MEM_WR_ACK on BVALID" },
  ],
};

// ------------------------------------------------------- the edge complex
const edgeDemux = {
  nodes: [
    {
      id: "flit",
      x: 0,
      y: 0,
      w: 15,
      h: 3,
      label: "flit arrives at port N",
      accent: true,
    },
    { id: "t1", x: 0, y: 5, w: 15, h: 3, label: "rsvd[2] set?" },
    { id: "t2", x: 0, y: 10, w: 15, h: 3, label: "dst == (0,0)?" },
    { id: "t3", x: 0, y: 15, w: 15, h: 3, label: "memory type?" },
    { id: "t4", x: 0, y: 20, w: 15, h: 3, label: "otherwise" },
    {
      id: "enc",
      x: 19,
      y: 5,
      w: 20,
      h: 3.4,
      label: "the interlink encapsulator",
      sub: "asked FIRST: a memory flit may also be remote",
    },
    {
      id: "pe",
      x: 19,
      y: 10,
      w: 20,
      h: 3.4,
      label: "the control processor",
      sub: "round robin; HOLDS when full — a dropped CU_DATA beat is corruption",
      accent: true,
    },
    {
      id: "eng",
      x: 19,
      y: 15,
      w: 20,
      h: 3.4,
      label: "that port's engine",
      sub: "backpressure: the two intake queues",
      accent: true,
    },
    {
      id: "ag",
      x: 19,
      y: 20,
      w: 20,
      h: 3.4,
      label: "the control agent",
      sub: "round robin; if it cannot take the flit at all, ACCEPTED AND DROPPED",
    },
  ],
  edges: [
    { from: "flit:b", to: "t1:t" },
    { from: "t1:r", to: "enc:l", label: "yes", dir: "h" },
    { from: "t1:b", to: "t2:t", label: "no" },
    { from: "t2:r", to: "pe:l", label: "yes", dir: "h" },
    { from: "t2:b", to: "t3:t", label: "no" },
    { from: "t3:r", to: "eng:l", label: "yes", dir: "h" },
    { from: "t3:b", to: "t4:t", label: "no" },
    { from: "t4:r", to: "ag:l", dir: "h" },
  ],
};

// --------------------------------------------------------- transform slot
// ONE bank per agent, driven only by the mover. The top row is the converting
// move; the bottom row is every fetch, which is never transformed.
const xformPaths = {
  nodes: [
    {
      id: "mv",
      x: 0,
      y: 0,
      w: 13,
      h: 3,
      label: "memory mover",
      sub: "names a slot id",
    },
    { id: "src", x: 16, y: 0, w: 11, h: 3, label: "mem / L2", sub: "source" },
    {
      id: "xs",
      x: 30,
      y: 0,
      w: 12,
      h: 3,
      label: "transform",
      sub: "one shared bank",
      accent: true,
    },
    {
      id: "dst",
      x: 45,
      y: 0,
      w: 11,
      h: 3,
      label: "mem / L2",
      sub: "converted, once",
    },
    { id: "mem2", x: 16, y: 7, w: 11, h: 3, label: "mem / L2" },
    { id: "port", x: 30, y: 7, w: 12, h: 3, label: "memory port" },
    {
      id: "cu",
      x: 45,
      y: 7,
      w: 20,
      h: 3,
      label: "NoC → compute unit",
      sub: "never transformed",
      accent: true,
    },
  ],
  edges: [
    { from: "mv:r", to: "src:l", dir: "h" },
    { from: "src:r", to: "xs:l", dir: "h" },
    { from: "xs:r", to: "dst:l", dir: "h" },
    { from: "mem2:r", to: "port:l", dir: "h" },
    { from: "port:r", to: "cu:l", dir: "h" },
  ],
};

// ------------------------------------------------------------ bit fields
const flitOwners = [
  { name: "dst_x", bits: 4, value: "noc" },
  { name: "dst_y", bits: 4, value: "noc" },
  { name: "src_x", bits: 4, value: "noc" },
  { name: "src_y", bits: 4, value: "noc" },
  { name: "type", bits: 4, value: "noc" },
  { name: "txn", bits: 8, value: "per type", accent: true },
  { name: "last", bits: 1, value: "noc" },
  { name: "rsvd", bits: 3, value: "framework" },
  {
    name: "payload",
    bits: 256,
    value: "sysnode on a descriptor · YOURS on CU_INST",
    accent: true,
  },
];

const descriptor = [
  { name: "addr", bits: 40, value: "byte address", accent: true },
  { name: "len", bits: 8, value: "beats − 1" },
  { name: "flags", bits: 8, value: "see below", accent: true },
  { name: "count", bits: 8, value: "entries" },
  { name: "peer", bits: 24, value: "3 × {y,x}" },
  { name: "n_peer", bits: 2 },
  { name: "entry_words", bits: 8, value: "0 or above 4 means 4" },
  { name: "reserved", bits: 158, value: "MUST be 0" },
];

const flagBits = [
  { name: "—", bits: 1, value: "MUST be 0" },
  { name: "STREAM", bits: 1, value: "count entries", accent: true },
  { name: "—", bits: 1, value: "reserved · was BLAYOUT" },
  { name: "—", bits: 1, value: "reserved · was QUANT" },
  { name: "—", bits: 1, value: "MUST be 0" },
  { name: "flush", bits: 1, value: "no RTL reads it" },
  { name: "invalidate", bits: 1, value: "no RTL reads it" },
  { name: "cacheable", bits: 1, value: "no RTL reads it" },
];

// field · width · position · owner — the columns that tell a reader which bits
// are theirs. Positions are the general expression the RTL computes.
const descSpec = {
  cols: [
    { key: "f", label: "Field", mono: true },
    { key: "w", label: "Width", align: "right" },
    { key: "p", label: "Position in the payload", mono: true },
    { key: "o", label: "Owner" },
  ],
  rows: [
    {
      f: "addr",
      w: "40",
      p: "flit[255 -: 40]",
      o: "<b>framework</b> — and it is 40 bits <i>whatever</i> <code>ADDR_W</code> is",
      _tone: "warn",
    },
    {
      f: "len",
      w: "8",
      p: "flit[215 -: 8]",
      o: "framework — beats − 1, and MUST be ≤ 7",
    },
    {
      f: "flags",
      w: "8",
      p: "flit[207 -: 8]",
      o: "framework; bits 4–5 reserved to the addon",
    },
    {
      f: "count",
      w: "8",
      p: "flit[199 -: 8]",
      o: "framework — entries in the run, 0 means 1",
    },
    {
      f: "peer",
      w: "24",
      p: "flit[191 -: 24]",
      o: "framework — three {y,x} bytes",
    },
    {
      f: "n_peer",
      w: "2",
      p: "flit[167 -: 2]",
      o: "framework — 0–3 extra destinations",
    },
    {
      f: "entry_words",
      w: "8",
      p: "flit[165 -: 8]",
      o: "framework — 0 or above 4 means 4",
    },
    {
      f: "reserved",
      w: "158",
      p: "the rest",
      o: "<b>framework.</b> MUST be zero — a future allocation will take it",
    },
  ],
};

const modes = {
  cols: [
    { key: "m", label: "Mode", mono: true },
    { key: "d", label: "What the engine does" },
    { key: "s", label: "Status" },
  ],
  rows: [
    {
      m: "COPY",
      d: "both walkers step in lockstep; a source stride of <b>zero is a broadcast</b>, with no extra mode",
      s: "built",
    },
    {
      m: "TRANSPOSE",
      d: "—",
      s: "<b>allocated and unimplemented.</b> The mover raises <code>F_MODE</code> rather than moving the wrong bytes",
      _tone: "bad",
    },
    {
      m: "GATHER",
      d: "the index vector is pulled into a buffer first, then three pipeline stages per element",
      s: "built",
    },
    {
      m: "GENERATE",
      d: "a counter-based PRNG keyed on the destination's <b>absolute word address</b>, so one fill and four fills of its quarters produce identical bytes",
      s: "built",
    },
    {
      m: "FILL",
      d: "an immediate, splatted at the configured element width",
      s: "built",
    },
    {
      m: "TRANSFORM",
      d: "mode 5 — the source walk feeds the transform slot and the destination counts <b>entries</b>",
      s: "built",
      _tone: "good",
    },
  ],
};

const insideAddr = [
  { name: "aperture", bits: 1, value: "1 = staging L2", accent: true },
  { name: "rsvd", bits: 1, value: "MUST be 0" },
  { name: "mesh", bits: 2, value: "0..3", accent: true },
  { name: "local", bits: 36, value: "64 GB" },
];

// ----------------------------------------------------- the descriptor walk
const readStates = [
  { id: "idle", x: 0, y: 3, label: "idle" },
  { id: "issue", x: 6, y: 0, label: "issue", sub: "AR" },
  { id: "fill", x: 13, y: 0, label: "fill", sub: "R beats" },
  { id: "latch", x: 17, y: 6, label: "latch", sub: "emit buf" },
  { id: "emit", x: 7, y: 7, label: "emit", sub: "per dest" },
];
const readEdges = [
  { from: "idle", to: "issue", label: "MEM_RD_REQ" },
  { from: "issue", to: "fill", label: "AR accepted" },
  { from: "fill", to: "fill", label: "beat", self: true },
  { from: "fill", to: "latch", label: "last beat" },
  { from: "fill", to: "issue", label: "next AR", curve: -70 },
  { from: "latch", to: "emit", label: "words stable" },
  { from: "emit", to: "issue", label: "entry i+1" },
  { from: "emit", to: "idle", label: "run done" },
];

const c = (entry, beat, addr, buf, flit) => [
  { k: "entry", v: entry },
  { k: "beat", v: beat },
  { k: "AR", v: addr },
  { k: "emit buf", v: buf },
  { k: "flit out", v: flit },
];

const readWalk = [
  {
    title: "The descriptor lands in the read queue",
    active: "idle",
    chips: c("—", "—", "—", "—", "—"),
    note: "STREAM = 1, count = 2, txn = 0x20, addr = A. One flit has just named two entries and several hundred cycles of traffic. It does NOT name a transform: a fetch is never transformed, so the operand at A is already in its final format.",
  },
  {
    title: "AR for entry 0",
    active: "issue",
    chips: c("0", "—", "A", "—", "—"),
    note: "An entry is entry_words words — 4 unless the request says otherwise, so 4 AXI beats at DATA_W = 256.",
  },
  {
    title: "Beats 0–2 arrive",
    active: "fill",
    chips: c("0", "0..2", "—", "—", "—"),
    note: "Each beat IS an operand word. They are captured here rather than written straight into the emit buffer, so the fetch never waits for the previous entry to finish leaving.",
  },
  {
    title: "Beat 3 lands — and the next address goes out in the same cycle",
    active: "issue",
    chips: c("0", "3", "A + 128", "—", "—"),
    note: "The next entry's address is issued the moment the current entry's last beat lands, not after that entry has finished leaving. That overlaps the address-to-first-beat latency, which would otherwise be paid once per entry.",
  },
  {
    title: "The entry is complete",
    active: "latch",
    chips: c("0", "—", "A + 128", "word0..3", "—"),
    note: "The four words move to the emit buffer in one cycle, which frees the capture registers for entry 1 immediately.",
  },
  {
    title: "Word 0 out, while entry 1 is still on the AXI bus",
    active: "emit",
    chips: c("0", "1 filling", "—", "word0..3", "txn 0x20 · rsvd 0"),
    note: "Without the emit buffer the capture registers ARE the emit source, so fetch and emit exclude each other and two independent interfaces run at the sum of their times instead of the larger.",
  },
  {
    title: "Words 1, 2, 3 — last set on word 3",
    active: "emit",
    chips: c("0", "1 filling", "—", "word0..3", "txn 0x20 · rsvd 3 · last"),
    note: "last is set on the final word of each entry, not only of the run.",
  },
  {
    title: "Entry 1 latched and emitted",
    active: "emit",
    chips: c("1", "—", "—", "word0..3", "txn 0x21 · rsvd 0..3"),
    note: "txn is the requester's own tag plus this entry's index within the run. A run of entries therefore lands in a run of slots and the receiver needs no arithmetic at all.",
  },
  {
    title: "Run complete",
    active: "idle",
    chips: c("—", "—", "—", "—", "—"),
    note: "Two entries, eight response flits, one request flit, and no cursor anywhere.",
  },
];

// ------------------------------------------------------------- wave traces
const intakeBroken = {
  rows: [
    {
      name: "queue head",
      kind: "bus",
      values: ["RD_REQ", "RD_REQ", "RD_REQ", "RD_REQ", "RD_REQ", "RD_REQ"],
      mark: [1, 2, 3, 4, 5],
    },
    {
      name: "behind it",
      kind: "bus",
      values: [
        "WR_DATA",
        "WR_DATA",
        "WR_DATA",
        "WR_DATA",
        "WR_DATA",
        "WR_DATA",
      ],
    },
    { name: "drain can finish", kind: "bit", values: [0, 0, 0, 0, 0, 0] },
    { name: "read servable", kind: "bit", values: [0, 0, 0, 0, 0, 0] },
    {
      name: "state",
      kind: "text",
      values: ["", "blocked", "blocked", "blocked", "blocked", "forever"],
    },
  ],
  notes: [
    {
      cycle: 1,
      text: "A read request at the head that cannot be served blocks the write data behind it — and that data is what lets a drain finish and frees the resource the read was waiting for.",
      tone: "bad",
    },
  ],
};

const intakeFixed = {
  rows: [
    {
      name: "read queue",
      kind: "bus",
      values: ["RD_REQ", "RD_REQ", "RD_REQ", "RD_REQ", "served", "—"],
    },
    {
      name: "write queue",
      kind: "bus",
      values: ["WR_DATA", "WR_DATA", "WR_DATA", "drained", "—", "—"],
    },
    {
      name: "drain can finish",
      kind: "bit",
      values: [0, 0, 1, 1, 1, 1],
      mark: [2],
    },
    {
      name: "read servable",
      kind: "bit",
      values: [0, 0, 0, 1, 1, 1],
      mark: [3],
    },
    { name: "mem_in_busy", kind: "bit", values: [0, 0, 0, 0, 0, 0] },
  ],
  notes: [
    {
      cycle: 2,
      text: "Two queues behind one busy signal, demultiplexed by type. Busy is still “is there room in both”, which is still local state, so the hazard above does not come back.",
      tone: "good",
    },
  ],
};

const wrBroken = {
  rows: [
    {
      name: "flit in",
      kind: "bus",
      values: [
        "WR_REQ A",
        "WR_REQ B",
        "DATA A0",
        "DATA B0",
        "DATA A1",
        "DATA B1",
      ],
    },
    { name: "open write", kind: "bus", values: ["A", "A", "A", "A", "A", "A"] },
    {
      name: "stored as",
      kind: "bus",
      values: ["—", "—", "A[0]", "A[1]", "A[2]", "A[3]"],
      mark: [3, 5],
    },
    {
      name: "B's burst",
      kind: "text",
      values: ["", "", "", "lost", "", "lost"],
    },
  ],
  notes: [
    {
      cycle: 3,
      text: "Collecting “the next flit” into the open write is wrong the moment two units write at once. B's beats land inside A's burst and A's land one slot late.",
      tone: "bad",
    },
    {
      text: "Nothing reports it. A MEM_WR_DATA with no matching open write is dropped, and a simulation $display is the only record.",
      tone: "bad",
    },
  ],
};

const wrFixed = {
  rows: [
    {
      name: "flit in",
      kind: "bus",
      values: [
        "WR_REQ A",
        "WR_REQ B",
        "DATA A0",
        "DATA B0",
        "DATA A1",
        "DATA B1",
      ],
    },
    {
      name: "slot for A",
      kind: "bus",
      values: ["val", "val", "A[0]", "A[0]", "A[0..1]", "A[0..1]"],
      mark: [2, 4],
    },
    {
      name: "slot for B",
      kind: "bus",
      values: ["—", "val", "val", "B[0]", "B[0]", "B[0..1]"],
      mark: [3, 5],
    },
    {
      name: "matched by",
      kind: "text",
      values: ["src", "src", "src", "src", "src", "src"],
    },
  ],
  notes: [
    {
      text: "Each source gets a slot, matched by source coordinate. A slot holds a whole burst, because one burst's beats must be contiguous on AXI while the mesh interleaves data flits freely.",
      tone: "good",
    },
    {
      text: "src_x and src_y on the data flits MUST match the descriptor's. This is the only binding; a wrong source coordinate stores the bytes into another node's write.",
      tone: "good",
    },
  ],
};

const multicast = {
  rows: [
    {
      name: "destination",
      kind: "bus",
      values: [
        "requester",
        "requester",
        "requester",
        "requester",
        "peer 0",
        "peer 0",
        "peer 0",
        "peer 0",
      ],
    },
    {
      name: "txn",
      kind: "bus",
      values: ["0x20", "0x20", "0x20", "0x20", "0x20", "0x20", "0x20", "0x20"],
    },
    {
      name: "rsvd[1:0]",
      kind: "bus",
      values: ["0", "1", "2", "3", "0", "1", "2", "3"],
    },
    {
      name: "last",
      kind: "bit",
      values: [0, 0, 0, 1, 0, 0, 0, 1],
      mark: [3, 7],
    },
    {
      name: "AXI reads",
      kind: "text",
      values: ["", "", "", "", "none", "", "", ""],
    },
    {
      name: "transform runs",
      kind: "text",
      values: ["", "", "", "", "none", "", "", ""],
    },
  ],
  notes: [
    {
      text: "Destinations are served one entry at a time: all of entry i's words to destination 0, then all of entry i's words to destination 1, and so on.",
      tone: "good",
    },
    {
      text: "The entry is read once and converted once; the same latched words are re-sent with a different header per destination. Served separately, that is one DRAM read and one conversion pass per consumer for a bit-identical result.",
      tone: "good",
    },
  ],
};

// ---------------------------------------------------------------- states
const slotStates = [
  { id: "free", x: 0, y: 0, label: "free" },
  { id: "val", x: 10, y: 0, label: "val", sub: "seen" },
  { id: "rdy", x: 10, y: 6, label: "rdy", sub: "data in" },
  { id: "iss", x: 0, y: 6, label: "iss", sub: "on the bus" },
];
const slotEdges = [
  { from: "free", to: "val", label: "descriptor arrives" },
  { from: "val", to: "rdy", label: "len+1 beats in" },
  { from: "rdy", to: "iss", label: "issued on AXI" },
  { from: "iss", to: "free", label: "ack sent, freed" },
];

// ---------------------------------------------------------------- tables
const expressCols = [
  { key: "what", label: "Message" },
  { key: "names", label: "What it can name" },
  { key: "bound", label: "Where it stops" },
];
const expressRows = [
  {
    what: "<b>A read</b>",
    names:
      "a byte address and an entry geometry, and optionally a run of consecutive entries, a transform, and up to a few extra destinations",
    bound:
      "entries <b>MUST</b> be contiguous — the engine accumulates the address and offers no stride. <code>count</code> is 8 bits, 0 means 1, the maximum run is 255 entries",
  },
  {
    what: "<b>A write</b>",
    names: "a descriptor followed by data flits, acknowledged fire-and-forget",
    bound:
      "<code>len</code> <b>MUST</b> be at most 7 — <code>WBURST</code> is a fixed constant of 8, not a parameter",
  },
  {
    what: "<b>A mover command</b>",
    names:
      "a source and a destination as N-dimensional strided descriptors with bound axes, plus a mode: copy, transpose, gather, generate, fill, <b>transform</b>",
    bound:
      "reads memory and writes memory, and never talks to a compute unit — it has its own AXI master and <b>no fabric endpoint</b>",
  },
];

// The geometry is the OCCUPANT's, declared as parameters, because the mover has
// to size a converting move before the transform has run.
const geomCols = [
  { key: "q", label: "Path", mono: false },
  { key: "size", label: "Source per entry", mono: true },
  { key: "beats", label: "AXI beats read", mono: true, align: "right" },
  { key: "flits", label: "Words written", mono: true, align: "right" },
];
const geomRows = [
  {
    q: "fetch — never transformed",
    size: "entry_words × DATA_W/8 bytes",
    beats: "entry_words",
    flits: "entry_words",
  },
  {
    q: "converting move — mover mode 5",
    size: "IN_BITS, declared by the occupant",
    beats: "IN_BITS / DATA_W",
    flits: "OUT_WORDS, at most 4",
    _tone: "good",
  },
];

const slotNameCols = [
  { key: "f", label: "Header field", mono: true },
  { key: "carries", label: "Carries" },
];
const slotNameRows = [
  {
    f: "txn",
    carries:
      "The requester's own <code>txn</code>, <b>plus this entry's index within the run</b>, as an 8-bit sum.",
  },
  { f: "rsvd[1:0]", carries: "The word's index within the entry, 0–3." },
  {
    f: "last",
    carries: "Set on the final word <b>of each entry</b>, not only of the run.",
  },
];

const shapeCols = [
  { key: "agent", label: "The agent will…" },
  { key: "you", label: "So a receiver should…" },
];
const shapeRows = [
  {
    agent: "deliver in <b>whole entries</b>, never partial ones",
    you: "make the entry a natural write unit — an integer number of entries per buffer slot, not a fractional one",
  },
  {
    agent:
      "deliver one entry as <b>entry_words consecutive flits</b>, <code>last</code> on the final word",
    you: "assemble by word index rather than by counting arrivals",
  },
  {
    agent:
      "put the <b>destination slot in the header</b> — <code>txn</code> plus the entry index, and <code>rsvd[1:0]</code> for the word",
    you: "derive the write address from the flit, not from a cursor. A cursor is correct only for as long as there is exactly one server",
  },
  {
    agent: "<b>interleave</b> other traffic between an entry's words",
    you: "frame by type and tolerate gaps",
  },
  {
    agent:
      "deliver <b>the same words to every peer destination</b> of a multicast",
    you: "not assume it is the only recipient, and not assume a per-destination transform",
  },
];

const fixedCols = [
  { key: "thing", label: "Fixed by the framework" },
  { key: "why", label: "Why" },
];
const fixedRows = [
  {
    thing:
      "its position — on the <b>mover's read-return path</b>, between R and the mover's staging FIFO",
    why: "one instance per agent rather than one per port or one per compute unit, and the mover's own walker feeds it, so a strided source needs no gather pass",
  },
  {
    thing:
      "<b>selection is an id</b>, named per move on the source walker's header",
    why: "occupants are all resident in fabric, so the id routes one request to one of them. A bit per transform would encode a mask nobody can act on",
  },
  {
    thing:
      "its handshake — start, a stream of accepted beats, done, a fixed number of output words",
    why: "so the mover's control does not change when the transform does",
  },
  {
    thing:
      "that it may change the byte count, and <b>declares the ratio</b> as IN_BITS / OUT_WORDS",
    why: "the mover sizes both walks before the transform has run. OUT_WORDS is at most 4, because the bank presents four word outputs",
  },
  {
    thing: "that it is <b>entry-granular</b>",
    why: "a transform with a cross-element dependency cannot emit until the whole entry has arrived. The engine is written for that case, so a streaming transform is also fine",
  },
  {
    thing:
      "an <b>occupant register space</b>, reached by the control processor and indexed by id",
    why: "configuration a per-move mode field is too narrow to carry — a palette, a coefficient table — and the bank's own status",
  },
];

const slotPortCols = [
  { key: "port", label: "Port", mono: true },
  { key: "dir", label: "Dir", mono: true },
  { key: "w", label: "Width", mono: true },
  { key: "contract", label: "Contract" },
];
const slotPortRows = [
  {
    port: "clk, rst",
    dir: "in",
    w: "1",
    contract:
      "the agent's clock; <code>rst</code> active-high, used synchronously — never a foreign domain",
  },
  {
    port: "start",
    dir: "in",
    w: "1",
    contract:
      "one-cycle pulse opening an entry; <code>id</code> and <code>mode</code> are valid during it. It <b>leads the first beat</b> — a beat presented with start is dropped",
    _tone: "warn",
  },
  {
    port: "id",
    dir: "in",
    w: "ID_W",
    contract: "which occupant; <code>0</code> is bypass",
  },
  {
    port: "mode",
    dir: "in",
    w: "MODE_W",
    contract:
      "<b>opaque</b> per-move configuration, captured at <code>start</code>; the framework carries it and never interprets it",
  },
  {
    port: "beat",
    dir: "in",
    w: "DATA_W",
    contract: "one source beat, <b>already registered by the agent</b>",
  },
  {
    port: "beat_valid",
    dir: "in",
    w: "1",
    contract:
      "qualifies <code>beat</code>; beats are <b>pushed at line rate</b>, never handshaken",
  },
  {
    port: "need_beat",
    dir: "out",
    w: "1",
    contract:
      "for an occupant that cannot take line rate; the agent ignores it, so tie it high or drive it truthfully",
    _tone: "warn",
  },
  {
    port: "done",
    dir: "out",
    w: "1",
    contract: "one-cycle pulse: the entry's outputs are final",
  },
  {
    port: "word0..word3",
    dir: "out",
    w: "DATA_W ea.",
    contract:
      "the transformed entry, <b>stable from done until the next start</b>. Four is the ceiling on OUT_WORDS",
  },
  {
    port: "cfg_en, cfg_id, cfg_addr, cfg_data",
    dir: "in",
    w: "1, ID_W, 8, 32",
    contract: "the occupant register space; a write is <code>cfg_en</code>",
  },
  {
    port: "cfg_rdata",
    dir: "out",
    w: "32",
    contract:
      "<b>combinational</b> read of <code>cfg_addr</code>, which is why there is no write-enable",
  },
  {
    port: "fault",
    dir: "out",
    w: "4",
    contract: "sticky, cleared by any write to register <code>0x00</code>",
  },
];

const candidateCols = [
  { key: "what", label: "Candidate" },
  { key: "where", label: "Where" },
  { key: "risk", label: "Risk" },
  { key: "status", label: "Status" },
];
const candidateRows = [
  {
    what: "address translation + cache on AXI",
    where: "in front of the DDR4 controller",
    risk: "low, but wrong layer for operands",
    status: "not built",
  },
  {
    what: "<b>memory-agent staging</b> — reserved address range backed by URAM",
    where: "inside the memory agent",
    risk: "low",
    status: "<b>built and shipping</b> — <code>mag_stage.v</code>",
    _tone: "good",
  },
  {
    what: "<b>mesh staging</b> — URAM node on a local link",
    where: "a mesh endpoint",
    risk: "low, reuses everything",
    status: "<b>built and shipping</b> — <code>noc_l2_adapter.v</code>, form 2",
    _tone: "good",
  },
  {
    what: "routers snoop and cache in flight",
    where: "inside the router",
    risk: "<b>high</b> — the only option that risks deadlock",
    status: "research",
    _tone: "bad",
  },
];

const costCols = [
  { key: "item", label: "Per memory port" },
  { key: "note", label: "Note" },
];
const costRows = [
  {
    item: "two flit-wide intake FIFOs",
    note: "primitive choice is a genuine trade: block RAM saves LUTs and costs frequency, because the worst path already starts at that FIFO's output and a block RAM's clock-to-out is far slower than a LUTRAM's",
  },
  {
    item: "the write slot array",
    note: "a small register file of per-slot state indexed by source, plus a data array of <code>WR_SLOTS × WBURST</code> beats — <b>the part that grows fastest</b>, since it is <code>WR_SLOTS × WBURST × DATA_W</code> bits and both factors are sized for correctness rather than tuned",
  },
  { item: "the read engine's emit buffer", note: "a few beat-wide registers" },
  {
    item: "one AXI master channel",
    note: "converged onto the agent's single AXI master at the boundary",
  },
  {
    item: "whatever the transform costs, once",
    note: "the contention that transform instance creates is the reason ports exist",
  },
];

const convCols = [
  { key: "conv", label: "Convention" },
  { key: "force", label: "Force" },
  { key: "why", label: "Because" },
];
const convRows = [
  {
    conv: "<b>Accept words in the shape they arrive.</b>",
    force: "Forced",
    why: "a response is N words per entry, each carrying its entry's index within the run and its word index within the entry. You are free to store it however you like — the arrival shape is not yours to choose",
    _tone: "warn",
  },
  {
    conv: "<b>Bin by tag; do not build a cursor.</b>",
    force: "Forced",
    why: "a receiver written against arrival order works until the first time two runs overlap",
    _tone: "warn",
  },
  {
    conv: "<b>Fetches are entry-granular.</b>",
    force: "Forced",
    why: "a run is consecutive entries at a fixed stride. Set the entry-words field or rearrange the region with the mover — but do not expect the memory agent to slice differently per request",
    _tone: "warn",
  },
  {
    conv: "<b>Discard write acks.</b>",
    force: "Forced",
    why: "slot sizing assumes you do not wait. A unit that waits <i>and</i> a slot count sized for a unit that does not is how you get a deadlock",
    _tone: "warn",
  },
  {
    conv: "<b>Store operands so that a pass is one contiguous run.</b>",
    force: "Free",
    why: "scattered entries turn one streaming request into many single-entry ones, and the per-request overhead is then paid per entry",
  },
  {
    conv: "<b>Name extra destinations rather than issuing identical requests.</b>",
    force: "Free",
    why: "the fetch happens once instead of once per consumer, and the waste scales with unit count",
  },
  {
    conv: "<b>Put format conversion in the transform slot, not in your unit.</b>",
    force: "Free",
    why: "one instance per memory agent rather than one per compute unit, and converting BEFORE the fetch divides the cost by the number of later reads. A unit that converts internally works; it pays for it once per unit and once per pass",
  },
  {
    conv: "<b>Schedule a converting move; do not ask a fetch to convert.</b>",
    force: "Forced",
    why: "a fetch is never transformed. A single-use operand therefore costs an explicit mover pass and pays MORE for it — 512 bytes of traffic per entry against 256 — which is the price of paying the conversion once per buffer instead of once per read",
    _tone: "warn",
  },
];

const catCols = [
  { key: "thing", label: "Thing" },
  { key: "cat", label: "Category" },
];
const catRows = [
  {
    thing: "memory request and response encoding, tags, acks",
    cat: "<b>fixed protocol</b>",
  },
  {
    thing: "the mover's command set and descriptor form",
    cat: "<b>fixed protocol</b>",
  },
  {
    thing:
      "the transform stage's position, selection and handshake — <b>the slot</b>",
    cat: "<b>fixed protocol</b>",
  },
  {
    thing: "control-agent register map and dispatch mechanism",
    cat: "<b>fixed protocol</b>",
  },
  {
    thing: "<b>what plugs into the transform slot</b>",
    cat: "customizable <b>addon</b> — the framework ships an identity bank and names no transform",
    _tone: "good",
  },
  {
    thing: "<b>staging of fetched lines inside the memory agent</b>",
    cat: "customizable <b>addon</b> — whether, how much, and with what behaviour",
    _tone: "good",
  },
  {
    thing: "<b>DRAM-port beat packing</b> at the memory boundary",
    cat: "customizable <b>addon</b>",
    _tone: "good",
  },
  {
    thing:
      "port count, port coordinates, slot count, queue depths, storage primitives",
    cat: "customizable — sized for correctness first",
  },
  {
    thing: "what the bytes mean: layout, tiling, tensor semantics",
    cat: "<b>yours</b>",
  },
  {
    thing: "your unit's own memories and how it stores what arrives",
    cat: "<b>yours</b>, entirely",
  },
];
</script>

<template>
  <DocPage
    title="The system node"
    summary="Descriptors in, DRAM traffic out, operand streams back — the memory half of the instruction set, and the two slots you plug into."
    domain="framework"
    status="shipped"
    source="src/kohakuaccel/sysnode/ · docs/arch/sysnode/ · docs/spec/memory-protocol.md · docs/spec/transform-slot.md"
  >
    <p class="doc-p">
      The <b>system node</b> is the single point where one mesh touches
      everything outside it. It is three things in one enclosure: <b>MAG</b>,
      the memory gateway — the memory instruction set, the service behind those
      instructions, and the edge complex that lets three consumers share one set
      of attachments; the <b>mover</b>, with the transform slot on its read
      return; and the <b>control processor</b>, which commands the mover.
    </p>
    <p class="doc-p">
      Call it the system node, never a "node" — a fabric endpoint is a node, and
      every compute unit sits on one. MAG is the gateway inside it, and keeps
      that narrower name. This page is the <b>contract</b> a compute unit builds
      against;
      <RouterLink to="/component/sysnode" class="doc-link"
        >the component page</RouterLink
      >
      is what the block is and what it costs, and
      <RouterLink to="/component/sysnode/microarchitecture" class="doc-link"
        >the microarchitecture page</RouterLink
      >
      is the RTL.
    </p>

    <Fig
      caption="ONE component. sn_hub owns every attachment and nothing below it owns one — the engines, the agent, the interlink and the processor are all its clients, told apart by what the flit is and where it is addressed. MAG and the processor are drawn as separate boxes because they are separate CONCERNS, not separate modules: neither ships without the other, and there is no parameter that removes the processor. Inside, every requester speaks one internal protocol and AXI appears once, at the boundary. The control agent has no arrow into that path — it never fetches from memory, so it needs no AXI master at all."
      zoom
      wide
    >
      <BlockDiagram
        :nodes="overview.nodes"
        :edges="overview.edges"
        :groups="overview.groups"
      />
    </Fig>

    <h2 class="doc-h2">What it owns</h2>
    <p class="doc-p">Four things, and nothing else.</p>
    <div class="grid gap-3 sm:grid-cols-2 my-5">
      <div class="card p-4">
        <div
          class="kt-text-caption font-semibold text-warm-800 dark:text-warm-200 mb-1"
        >
          The memory instruction set
        </div>
        <p class="kt-text-caption text-warm-500 dark:text-warm-400 leading-6">
          What a read, a write and a mover command can <i>express</i>. Your
          compiler emits these; it does not define them.
        </p>
      </div>
      <div class="card p-4">
        <div
          class="kt-text-caption font-semibold text-warm-800 dark:text-warm-200 mb-1"
        >
          The service behind it
        </div>
        <p class="kt-text-caption text-warm-500 dark:text-warm-400 leading-6">
          One memory port per attachment: intake queues, a read engine, write
          slots matched by source, and one AXI channel.
        </p>
      </div>
      <div class="card p-4">
        <div
          class="kt-text-caption font-semibold text-warm-800 dark:text-warm-200 mb-1"
        >
          Two addon slots
        </div>
        <p class="kt-text-caption text-warm-500 dark:text-warm-400 leading-6">
          The <span class="chip">transform slot</span> on the mover's read
          return, and <span class="chip">staging</span> in the address map. Both
          ship working and both are built to be replaced.
        </p>
      </div>
      <div class="card p-4">
        <div
          class="kt-text-caption font-semibold text-warm-800 dark:text-warm-200 mb-1"
        >
          The edge
        </div>
        <p class="kt-text-caption text-warm-500 dark:text-warm-400 leading-6">
          The host's reach into the mesh, the link to other meshes, and the hub
          that puts four clients on one set of attachments.
        </p>
      </div>
    </div>

    <p class="doc-p">
      The rejected alternative is the one most accelerators take:
      <b>let every compute unit carry its own memory system.</b> Then burst
      generation, 4 KB boundary handling, out-of-order write reassembly and a
      request encoding are copied into every unit — each copy a place to get it
      wrong, and each unit's author solving a problem that has nothing to do
      with their datapath. The other rejected alternative is subtler:
      <b
        >give the memory service, the control plane, the inter-mesh link and the
        processor an attachment each.</b
      >
      A mesh has very few attachments to give away, three of those four are
      nearly idle, and paying four times over for three idle consumers is how a
      fabric runs out of ports before it runs out of bandwidth.
    </p>

    <Callout kind="rule" title="The split this system draws">
      <p>
        A compute unit should not contain a memory system. The split is between
        <b>naming</b> memory and <b>serving</b> it. A unit names what it wants
        ahead of time, because that is the assumption the whole framework rests
        on: addresses are computable, not discovered by chasing pointers.
        Serving it is here.
      </p>
    </Callout>

    <h2 class="doc-h2">The instruction set you inherit</h2>
    <p class="doc-p">
      An instruction reaching a compute unit is one flit, and that flit's bits
      belong to three different owners. So the machine already has an
      instruction set before your compute unit exists.
    </p>

    <BitField
      :fields="flitOwners"
      caption="Who owns which bits, at FLIT_WIDTH = 288 and POS_WIDTH = 4. The routing header is the NoC's; descriptors, entry geometry, transform selection and mover commands are this system's; the instruction payload a compute unit executes is yours entirely. txn's meaning is per type — on MEM_RD_REQ it is the requester's tag, and the agent echoes it plus the entry index; rsvd is framework-owned on every type, and the agent writes the word index into its low two bits on a response"
    />

    <Callout kind="note" title="Two practical consequences">
      <p>
        <b>Instruction bits are a shared budget.</b> The header takes its fixed
        slice before you see the flit.
        <b>Your compiler emits memory instructions it did not define</b> — the
        back end you write is responsible for <i>scheduling</i> fetches and
        writes, not for inventing their encoding.
      </p>
    </Callout>

    <h3 class="doc-h3">The descriptor</h3>
    <BitField
      :fields="descriptor"
      caption="MEM_RD_REQ (0x0) and MEM_WR_REQ (0x1) payload. On a write only addr and len are read. The descriptor is 98 bits inside a 256-bit payload — the rest MUST be zero"
    />
    <BitField
      :fields="flagBits"
      caption="flags, at descriptor bits 207 down to 200. Bits 0–2 are declared in noc_pkt.vh and no RTL reads them; bits 4 and 5 select the transform addon and are reserved here rather than given a meaning here"
    />

    <Callout
      kind="trap"
      title="The address field is 40 bits whatever ADDR_W is"
    >
      <p>
        <code>mag_mem_port.v</code> states it as a rule with the failure
        attached: “<code>NOC_MEM_ADDR</code> is 40 bits WHATEVER
        <code>ADDR_W</code> is — a flit contract, not a width. Slicing it by
        <code>ADDR_W</code> read <code>addr &gt;&gt; 6</code> on a 34-bit build,
        <b>silently</b>.”
      </p>
      <p>
        The spec tree described a 34-bit <code>addr</code> with a 6-bit
        <code>addr_spare</code> beside it and the mesh id at
        <code>addr[33:32]</code>, in <code>flit-format.md</code> §4.1,
        <code>parameters.md</code> §1, <code>memory-protocol.md</code> §8 and
        <code>spec/README.md</code>'s own worked example of the bit notation.
        <b>All four said the same wrong thing</b>, which is what a normative
        tree does when one page is copied into the next.
        <code>docs/address-map.md</code> was right the whole time and disagreed
        with all of them.
      </p>
      <p>
        They now read one 40-bit field with <code>[39]</code> the aperture bit,
        <code>[38]</code> reserved and <code>[37:36]</code> the mesh — which is
        what the RTL does and what <code>MachineSpec.global_addr</code> builds.
        A sender that had followed the old text would have placed every request
        <b>64× too high</b>, and nothing on the path would have reported it.
      </p>
    </Callout>

    <BitField
      :fields="insideAddr"
      caption="The address every unit issues, every instruction carries and every decoder tests. mag_stage.v, mag_stage_port.v and mag_mem_port.v all test it absolutely — an address carries which mesh it belongs to, no matter who issued it or where it arrives"
    />

    <SpecTable
      :cols="descSpec.cols"
      :rows="descSpec.rows"
      caption="The descriptor, field by field. The owner column is the one that matters: every field here is the framework's, so a compute unit's own instruction bits are entirely in the CU_INST payload and nowhere in a memory descriptor"
    />

    <SpecTable
      :cols="expressCols"
      :rows="expressRows"
      caption="The shape of what is expressible, because that shape is what constrains your compiler. Field positions are in docs/spec/memory-protocol.md"
    />

    <SpecTable
      :cols="modes.cols"
      :rows="modes.rows"
      caption="The mover's six modes, including the one that is allocated and unimplemented. A mode code the engine cannot execute raises a fault rather than doing something plausible — which is the only safe behaviour when the alternative is moving the wrong bytes and reporting success"
    />

    <Callout kind="note" title="Padding is not a special case">
      <p>
        Because the mover's descriptors have bound axes, an element outside the
        tensor is
        <i>padding</i> rather than a special case — the source's low
        <code>valid</code> injects a constant and the destination's low
        <code>valid</code> suppresses a write, so a padded traversal needs no
        border handling anywhere else in the machine. The walker underneath is a
        general affine address generator <b>with no multipliers</b>: each
        dimension carries its own partial sum, incremented on step and zeroed on
        wrap, so the address is an adder tree rather than a product.
      </p>
    </Callout>

    <h2 class="doc-h2">A port is the unit the machine grows by</h2>
    <p class="doc-p">
      The read engine fetches one entry at a time. With a single engine, every
      compute unit in the mesh queues behind one state machine and one emit
      buffer — and the reference machine stopped scaling while nothing was
      saturated, which is the diagnostic: the limit was the <i>server</i>, not
      the bandwidth.
    </p>

    <Fig
      caption="One memory port is a whole server. PORTS of them are instantiated, and adding one adds all of it. Nothing is shared between ports except the address space on the far side of AXI."
      zoom
    >
      <BlockDiagram :nodes="port.nodes" :edges="port.edges" />
    </Fig>

    <Callout kind="rule" title="Ports MUST be placed at different mesh nodes">
      <p>
        That is not a placement preference. Routing is XY on clamped
        coordinates, so a port at <code>(0, y)</code> draws traffic to router
        <code>(GRID_LO, y)</code> and to no other.
        <b
          >Two ports on one router split the server without splitting the
          funnel</b
        >
        — the link into that router stays exactly as narrow as before.
      </p>
    </Callout>

    <h2 class="doc-h2">Intake: backpressure must not depend on content</h2>
    <p class="doc-p">
      <code>mem_in_busy</code> is computed from this port's own queue occupancy
      and nothing else. It never depends on what the arriving flit is — because
      the mesh is in-order behind a busy signal, so a port that decides “busy”
      from <i>this particular</i> flit stops everything behind it too, including
      the flit that would have freed the resource.
    </p>

    <WaveTrace
      v-bind="intakeBroken"
      variant="broken"
      label="one intake queue"
    />
    <WaveTrace
      v-bind="intakeFixed"
      variant="fixed"
      label="two queues, demultiplexed by type"
    />

    <Callout kind="rule" title="The margin is explicit">
      <p>
        <code>mem_in_busy</code> is “either queue is near full”, at
        <code>Q_MARGIN</code> entries of <code>Q_DEPTH</code> (4 of 64 at the
        default build). The port counts for itself rather than relying on the
        FIFO's <code>almost</code> flag, which is not a margin.
      </p>
    </Callout>

    <h2 class="doc-h2">Reads: the response says where it belongs</h2>
    <p class="doc-p">
      Every response flit is self-describing, so the receiver needs no cursor
      and arrival order stops being load-bearing.
      <b>That is what makes a streaming fetch possible at all</b> — one request,
      hundreds of cycles of traffic, and a receiver that can bin every flit it
      gets without tracking where it is.
    </p>

    <StepPlayer :steps="readWalk" label="A streaming fetch of two entries">
      <template #default="{ state }">
        <div class="flex flex-wrap gap-1.5 mb-4">
          <span v-for="ch in state.chips" :key="ch.k" class="chip">
            <span class="opacity-60 mr-1">{{ ch.k }}</span
            >{{ ch.v }}
          </span>
        </div>
        <StateMachine
          :states="readStates"
          :edges="readEdges"
          :active="state.active"
        />
      </template>
    </StepPlayer>

    <p class="doc-p kt-text-caption">
      State names on that diagram are this page's; the transitions and their
      reasons are the ones <code>docs/arch/sysnode/memory-port.md</code> and
      <code>docs/spec/memory-protocol.md</code> §3.2 specify. Two edges are
      drawn short and mean more than they say: <code>next AR</code> fires on the
      current entry's <i>last beat</i>, not after the transform has finished
      with it; and <b>emit</b> repeats once per word and then once more per
      extra destination, re-sending the same latched words with a different
      header.
    </p>

    <SpecTable
      :cols="geomCols"
      :rows="geomRows"
      caption="An entry is the unit both paths work in. A fetch's size is stated by the request; a converting move's is stated by the OCCUPANT, as IN_BITS/OUT_WORDS, because the mover must size the move before the transform has run"
    />

    <SpecTable
      :cols="slotNameCols"
      :rows="slotNameRows"
      caption="How a response names its slot — the mechanism that makes streaming possible, and one a requester MUST use rather than keeping a cursor"
    />

    <Callout kind="rule" title="Size your own tag space">
      <p>
        A requester <b>MUST</b> size its own tag space so that
        <code>txn + count − 1</code> does not exceed 255. The addition is 8-bit
        and wraps silently; a run that wraps aliases two entries onto one slot.
      </p>
    </Callout>

    <Callout
      kind="trap"
      title="Assembling an entry in one register is a bet on the server"
    >
      <p>
        A requester that assembles an entry from consecutive flits into one
        register is relying on a property of the <i>server</i> — that one agent
        finishes an entry's words before starting the next — and
        <b>MUST assert it rather than assume it</b>.
      </p>
      <p>
        A second server, a reordering fetch engine, or two senders into one
        receiver would interleave two entries into one and produce
        <b>a plausible wrong result</b>. The idiom is cheap and both reference
        units use it; it is correct only because there is exactly one server
        today.
      </p>
    </Callout>

    <SpecTable
      :cols="shapeCols"
      :rows="shapeRows"
      caption="Convention, but binding in practice. Nothing checks any of this — the agent will deliver in this shape whether or not your unit was designed for it, and conversion at the receiver is the thing entry tagging exists to avoid"
    />

    <h3 class="doc-h3">Extra destinations</h3>
    <p class="doc-p">
      A read request may name up to three extra destinations in
      <code>peer[23:0]</code>, one <code>{y, x}</code> byte each. They exist
      because a set of units frequently sweeps the same operand.
    </p>

    <WaveTrace v-bind="multicast" label="one entry, requester plus one peer" />

    <Callout
      kind="open"
      title="Who issues the request is not the framework's decision"
    >
      <p>
        A sharing set must arrive at one issuer by some rule its own driver
        enforces; the framework <b>neither elects one nor detects two</b>. As of
        the cache notes the mechanism is decoded by the hardware and the driver
        does not set it, because a follower cannot yet tell which fill an
        arriving entry belongs to — so the traffic reduction is one rendezvous
        away from being usable, not being measured today.
      </p>
    </Callout>

    <h2 class="doc-h2">
      Writes: slots matched by source, not by arrival order
    </h2>
    <p class="doc-p">
      A write is a descriptor flit and then data flits, and the mesh may put
      another node's flit between them.
    </p>

    <WaveTrace
      v-bind="wrBroken"
      variant="broken"
      label="collect “the next flit” into the open write"
    />
    <WaveTrace
      v-bind="wrFixed"
      variant="fixed"
      label="a slot per source, matched by coordinate"
    />

    <Fig
      caption="A slot walks val → rdy → iss → free, and all three bits are needed. free → val is a descriptor matched by source coordinate; val → rdy is len + 1 data flits collected; rdy → iss is the burst issued on AXI; iss → free is BVALID, which is also when the MEM_WR_ACK goes out. ws_val, ws_rdy and ws_iss are the RTL's own names."
    >
      <StateMachine :states="slotStates" :edges="slotEdges" />
    </Fig>

    <Callout kind="trap" title="Two bits are not enough">
      <p>
        With only <code>val</code> and <code>rdy</code>, a slot whose write is
        on the bus is indistinguishable from one still waiting for its data, and
        <b>the next data flit from that source binds to the in-flight slot.</b>
      </p>
    </Callout>

    <Callout
      kind="trap"
      title="Under-sizing does not corrupt anything. It deadlocks."
    >
      <p>
        <code>WR_SLOTS</code> <b>MUST</b> be at least
        <b>two per node that can have a write in flight</b>, not one. A compute
        unit discards its <code>MEM_WR_ACK</code> — which it should, they are
        fire-and-forget — so its next descriptor arrives while the previous
        burst is still on the AXI bus and the previous slot is still allocated.
        With one slot per node the second descriptor finds nothing free, is
        never popped, and blocks the data flits behind it — which are what would
        have freed one.
      </p>
    </Callout>

    <Callout kind="trap" title="A miscount leaves a slot that is never freed">
      <p>
        A source <b>MUST</b> send exactly <code>len + 1</code> data flits per
        descriptor. The burst ends on the agent's <b>beat counter</b>, not on
        the flit stream, so a requester that miscounts its own data does not
        desynchronise the response —
        <b
          >but it does leave a slot that never completes, and that slot is never
          freed.</b
        >
      </p>
      <p>
        A source also <b>MUST NOT</b> have more than one write open at a time.
        Slots are matched by source coordinate alone, so two open writes from
        one source cannot be told apart, and which one the data binds to is
        undefined.
      </p>
    </Callout>

    <Callout kind="rule" title="A write ack carries no status">
      <p>
        <code>MEM_WR_ACK</code> is a single flit, <code>txn</code> echoed,
        <code>last</code> set, payload all zero. Success and failure are
        indistinguishable on the mesh, and an AXI slave error response is
        ignored entirely. <b>Nothing consumes it</b> — every compute unit in the
        tree drops it, and a unit that does not drop it wedges.
      </p>
      <p>
        A program that must read what it wrote therefore
        <b>MUST NOT</b> sequence on the ack. It sequences at an instruction
        boundary the host can observe: the writing instruction's completion,
        seen through the orchestrator's status mirror.
      </p>
    </Callout>

    <h3 class="doc-h3">Reads and writes run alongside each other</h3>
    <p class="doc-p">
      A streaming fetch occupies the read path for its entire run. Inside one
      state machine that machine never returns to idle, so no write slot can be
      issued: the slots fill, intake jams on a write descriptor nothing will
      accept, and the data flit behind it reports “no open write”. The read
      engine therefore has its own state and its own return context, and shares
      only the single output register — where the emitter wins, and cannot
      starve the write path because a few response flits per entry against a
      fetch of several beats leaves most cycles free.
    </p>

    <h2 class="doc-h2">Addon slot 1: the transform stage</h2>
    <p class="doc-p">
      <b>The slot is fixed protocol; what plugs into it is an addon.</b> The
      framework fixes where the stage sits, how it is selected and how it is
      driven. What it <i>does</i> is a property of the accelerator you are
      building.
    </p>
    <p class="doc-p">
      <b>One bank per agent, and only the memory mover can reach it.</b> A
      compute unit's fetch is never transformed — it reads operands already in
      their final format, written that way by the host or converted in place by
      the mover. Selection is an <b>id</b>, never a bit per transform:
      <code>0</code> is bypass, <code>1</code> is slot 1, and a design with
      several transforms picks one rather than encoding a mask.
    </p>
    <p class="doc-p">
      Why one is enough is <i>structural</i>, not a workload measurement. A
      per-port transform is fed from that port's AXI R channel;
      <code>sysnode</code> converges every port master onto <b>one</b> DRAM
      master; and a staged read never transforms, because staging holds operand
      words verbatim. So every transformed byte comes from a single converged
      master, and N transforms could consume one beat per cycle between them —
      N−1 idle by construction.
    </p>
    <p class="doc-p kt-text-caption">
      One instance measured <b>4,499 LUT and 32 DSP</b> — out-of-context
      synthesis on <code>xcvu13p-fhgb2104-2L-e</code>, Vivado 2024.2, at 3.333
      ns, <code>sysnode</code> whole at <code>PORTS = 2</code>, by
      <code>scripts/tcl/ooc_sysnode.tcl</code>. That is the framework's
      arbiter plus <i>this project's</i> bank and occupant, so it is what one
      accelerator's transform costs rather than what a slot costs.
    </p>
    <p class="doc-p">
      And why the mover rather than the requester: a transform on the fetch path
      is paid
      <b>once per read</b>; on the mover path it is paid <b>once per buffer</b>.
      Intermediate results are written back by the units and then re-read, so
      converting on every read is the expensive arrangement. The cost of the
      choice is that a single-use operand needs an explicit mover pass the
      compiler must schedule, and pays MORE for it — source read, converted copy
      written, copy read again.
    </p>

    <Fig
      caption="ONE bank per agent, reachable only by the memory mover, and sitting ON its read-return path. Top: the converting move, mem/L2 → slot → mem/L2, mover mode 5, paid once per buffer. Bottom: every fetch, which is never transformed. Three earlier arrangements are retired — one instance per memory port, a second on the host upload window, and a separate engine muxed onto the mover's AXI channel."
      zoom
      wide
    >
      <BlockDiagram :nodes="xformPaths.nodes" :edges="xformPaths.edges" />
    </Fig>

    <SpecTable :cols="fixedCols" :rows="fixedRows" />

    <Callout
      kind="rule"
      title="A converting move is an ordinary descriptor in mode 5"
    >
      <p>
        There is no second engine and no second command set. The occupant is
        named on the
        <b>source walker's header</b>, in bits it already left free, because a
        transform applies to the read side. The two walkers then count different
        things: the <b>source counts source words</b> and defines the iteration
        space, and the <b>destination counts entries</b>.
      </p>
      <p>
        That is what deletes the gather pass. The walker issues an entry's reads
        wherever they live and the in-order returns stream into the occupant — a
        source strided
        <i>within</i> an entry costs nothing extra, where a walker-less engine
        had to assemble it in staging first.
      </p>
      <p>
        <b>The reservation is unchanged in kind.</b> <code>m_rready</code> is
        tied high and space is reserved before the AR goes out; folded, the rule
        is “do not issue an entry's reads without room for its
        <code>OUT_WORDS</code>” — still a static count, still known in advance.
      </p>
    </Callout>

    <Callout
      kind="trap"
      title="A bound axis is not available in a transform move"
    >
      <p>
        A padded element issues no read, and the occupant is fed a fixed beat
        count off the read return — so a bound axis would leave an entry one
        beat short <b>forever</b>. The mover raises fault 7 rather than
        converting the wrong bytes. Padding remains available on every other
        mode, where a low <code>valid</code> injects the immediate.
      </p>
    </Callout>

    <Callout kind="note" title="Two claims justify the stage existing at all">
      <p>
        <b
          >Converting before the fetch divides the cost by the number of later
          reads</b
        >
        — a buffer converted once and read many times pays the transform once,
        not once per pass.
        <b>Converting in the agent means one instance per agent</b>, not one per
        compute unit — and the agent count is one per mesh, while the unit count
        is set by the die.
      </p>
      <p>
        Both are independent of what the transform is. There is
        <b>no postprocess hook on the write path</b>: a drain is written to
        memory verbatim, and an accelerator that needs one is adding a framework
        feature rather than configuring an existing one.
      </p>
    </Callout>

    <SpecTable
      :cols="slotPortCols"
      :rows="slotPortRows"
      caption="The interface an occupant must present — docs/spec/transform-slot.md. The identity bank in src/templates/transform/ is a working implementation of all of it"
    />

    <Callout kind="rule" title="The three hard rules">
      <p>
        <b>1. Fixed output shape, and four is the ceiling.</b> An entry yields
        <code>OUT_WORDS</code> words whatever the source length, and the bank
        presents exactly <code>word0..word3</code> — so an expanding transform
        shrinks its entry rather than growing its output. The bypass occupant
        obeys the same rule, so a requester naming id 0 gets the same shape as
        any other.
      </p>
      <p>
        <b>2. The whole entry may be needed before anything can be emitted.</b>
        An occupant whose output depends on the whole entry — a block-shared
        scale, a running maximum — may raise <code>done</code> any number of
        cycles after the last beat. The engine is written for that case, which
        is why a streaming transform is also fine.
      </p>
      <p>
        <b>3. Input is push-only.</b> Beats are pushed at line rate and never
        handshaken; a transform that needs backpressure must buffer internally.
      </p>
    </Callout>

    <Callout kind="measured" title="Register the beat before the transform">
      <p>
        The port registers the beat before the transform. In
        <code>mag_mem_port.v</code> the read FIFO's BRAM output into the
        occupant's DSP control was <b>9 LUT levels, 4.399 ns</b>, and set the
        WNS on <b>every SLR1 probe</b> until it was registered. An occupant gets
        a registered input and should keep its first level shallow.
      </p>
      <p class="kt-text-caption">
        Timing figures are from that module on the SLR1 probe vehicle,
        <code>xcvu13p</code>.
      </p>
    </Callout>

    <Callout
      kind="note"
      title="The framework names one module and no transform"
    >
      <p>
        <code>mag_xform</code> instantiates <code>xform_bank</code>, and that is
        the only module name fixed here. A bank holds a design's occupants and
        demuxes the id internally, so nothing in the protocol, the mover or the
        agent is named after an arithmetic. A design with a transform writes its
        own bank and changes nothing else.
      </p>
      <p>
        A design with <b>no</b> transform compiles
        <code>src/templates/transform/xform_bank.v</code>, where every id is
        bypass — there is no flag to leave unset, because selection is an id.
        Sharing the module name is what lets the framework elaborate on its own:
        <code>tests/sysnode/xform_identity_tb.v</code> builds the mover, the
        slot and that bank with <b>no design sources at all</b>, and would fail
        to compile if the only bank in the tree belonged to one.
      </p>
    </Callout>

    <h2 class="doc-h2">Addon slot 2: staging</h2>
    <p class="doc-p">
      Fetched lines can be held on the memory-agent side so that several units
      asking for the same region do not each reach memory for it. What is fixed
      is the surrounding shape: requests arrive as flits, responses leave as
      tagged flits, and the intake and emit paths are unchanged whether or not
      anything is staged in between. Whether to stage at all, how much, with
      what replacement behaviour and in which storage primitive, is the addon's
      business.
    </p>

    <Callout kind="rule" title="Prefer explicit staging over caching">
      <p>
        The access pattern is not something to discover at runtime: a blocked
        sweep walks nested loops over addresses the compiler already computed.
        <b
          >A cache spends tags and comparators rediscovering what was written
          down.</b
        >
      </p>
      <p>
        Staging is a reserved range in the existing address map, so it needs no
        new instruction, no tags, no associativity, no replacement, no coherence
        and no write policy. Host access is free — it is in the address map, so
        the host DMA reaches it like any memory. The one software obligation:
        results destined for DRAM must use DRAM addresses.
      </p>
    </Callout>

    <SpecTable
      :cols="candidateCols"
      :rows="candidateRows"
      caption="The four candidates for what sits between DRAM and the compute units. Two are built and in the ship tops, selected independently with gen_mesh.py --l2-mag / --l2-cu / --l2-vec. No software targets either yet"
    />

    <Callout kind="measured" title="The deciding factor is reach, not capacity">
      <p>
        DDR4 is ~30–40 ns away (ASSUMED, user-supplied) — 9–12 cycles at 300 MHz
        — while URAM is 2 cycles and <b>9.38% used</b> (MEASURED: 120 of 1,280,
        placed multi-mesh run of 2026-08-12). An L2 wants a few hundred KB per
        pass and even a conservative budget gives several MB per SLR.
      </p>
      <p>
        What is scarce is the ability of one centralised block to reach URAM
        columns spread across the die, with the most crowded SLR at
        <b>95.80% CLB</b> (MEASURED — the cache notes' own figure; the placed v5
        design separately reports SLR0 at 95.49%). There is no room to route
        around it. Mesh staging sidesteps this by distributing: each node is a
        local port, an address decoder and its URAM bank, so each can sit where
        its memory is.
      </p>
      <p class="kt-text-caption">
        The shipping agent-side configuration is
        <b>4 banks × 16,384 entries = 64 URAM per agent</b>, with 8 URAM per CU
        adapter in the four-mesh design. The line is 1,024 bits, not the 936 the
        note's arithmetic assumed; that arithmetic is marked SUPERSEDED in
        <code>docs/notes/cache/mag-staging.md</code> §3 and has not been
        recomputed.
      </p>
    </Callout>

    <Callout
      kind="note"
      title="What mesh staging costs that agent staging does not"
    >
      <p>
        <b>Hops</b> — a request travels to the staging node and the response
        travels back; 1–2 hops each way on a 2×2 mesh, still far inside the DRAM
        round trip, but it consumes link bandwidth agent traffic would otherwise
        have. <b>A bandwidth ceiling</b> — a local port is one flit per cycle,
        so a 288-bit flit at 300 MHz is ~10.8 GB/s per node, against ~77 GB/s of
        DDR4. A centralised L2 reading many URAMs in parallel inside the agent
        is not limited this way.
      </p>
      <p class="kt-text-caption">
        300 MHz here is
        <b>an assumed rate for the arithmetic, not a measured one</b>. No Fmax
        in this tree is a closed-timing figure; every clock result is
        out-of-context synthesis, and synthesis slack is optimistic.
      </p>
      <p>
        <b
          >That is the real trade: mesh staging buys placement freedom and pays
          in per-node bandwidth.</b
        >
        A staging node on an otherwise-unused local costs one endpoint and zero
        routers, against <b>3,281 LUT per router</b> (MEASURED, placed
        hierarchy) for the alternative of adding locals.
      </p>
    </Callout>

    <Callout
      kind="open"
      title="Any caching proposal must say what it adds beyond shared fetch"
    >
      <p>
        A fill descriptor already names up to three other compute units sharing
        one operand, the lowest-numbered one issues a single descriptor, and the
        memory agent multicasts the result to all of them.
        <b
          >That is precisely the broadcast a shared cache would exist to
          provide</b
        >, done with compiler knowledge and without arbitration or coherence.
      </p>
    </Callout>

    <h2 class="doc-h2">The edge complex</h2>
    <p class="doc-p">
      A mesh has a small number of attachments to give away. Memory traffic, the
      control plane, the inter-mesh link and the processor all need one, and
      giving each its own would cost four times the ports for three consumers
      that are nearly idle. So <b>nothing inside the node owns a port</b>: four
      kinds of client on one set of attachments, told apart by what the flit is
      and where it is addressed rather than by which port it arrived on.
    </p>

    <Fig
      caption="Inbound classification at a memory port. The orchestrator has no endpoint of its own; it answers at port 0's coordinate — which is what lets a compute unit reply to whoever sent its instruction without being configured."
      zoom
    >
      <BlockDiagram :nodes="edgeDemux.nodes" :edges="edgeDemux.edges" />
    </Fig>

    <Callout
      kind="trap"
      title="Control-plane traffic at a memory port is best-effort"
    >
      <p>
        <b>A flit the agent cannot accept is discarded rather than held.</b>
        This is the most important line in the module, and it is a deliberate
        loss of data.
      </p>
      <p>
        The agent raises busy when its receive mailbox is full, and a host that
        never drains the mailbox leaves it full <b>indefinitely</b>. Holding the
        port for that would stall the <i>memory</i> flits behind it on the same
        link, for good, because nothing clears the condition. The framework
        trades a loss on a path nothing uses in steady state — completions
        bypass the mailbox entirely, and the driver does not read it — for
        removing an unbounded stall on the path everything uses.
      </p>
      <p>
        Waiting one's <i>turn</i> is different, does hold the port, and is
        bounded by the port count.
      </p>
    </Callout>

    <Callout
      kind="rule"
      title="Outbound priority: agent, processor, interlink, engine"
    >
      <p>
        The agent wins because its traffic is a handful of control flits against
        a stream of operand words; engine priority would let a busy port starve
        dispatch exactly when the machine is busiest. The processor is next,
        because a stalled dispatch stalls the whole graph, and the interlink
        sits below both because its burst is already bounded by credit the far
        end granted. Within the engine the read-response emitter outranks the
        plain-read and write-ack path, so a <code>MEM_WR_ACK</code> can be
        delayed for the whole duration of a streaming fetch.
        <b
          >It cannot be starved indefinitely — but no latency bound is
          offered.</b
        >
      </p>
      <p>
        Inbound, the ports round-robin into each single-input client, and the
        pointer moves only on an accepted flit; moving it every cycle would let
        a port lose its turn to one that had nothing to send.
        <b>The three arbiters are separate</b> — sharing one would let a stalled
        interlink hold up dispatch, or a busy processor hold up the agent.
      </p>
      <p>
        <b
          >Inbound order matters too, because one flit can satisfy two tests.</b
        >
        A memory flit may also be marked remote, and the engine is not the
        consumer of one that is leaving this mesh — so remote is asked first,
        then the processor's coordinate, then the type.
      </p>
    </Callout>

    <h3 class="doc-h3">The control agent</h3>
    <p class="doc-p">
      The host's reach into the mesh: an AXI slave on one side and a fabric
      endpoint on the other, offering a raw flit mailbox, instruction dispatch,
      and a status mirror. Dispatch stalls on credit and never on the network,
      and it needs no AXI master because it never fetches from memory — it only
      forwards what the host already placed in the staging RAM, rewriting the
      header with the destination from a register and the source stamped with
      its own coordinates.
    </p>

    <Callout
      kind="trap"
      title="The status mirror stores a count, not a sticky flag"
    >
      <p>
        Completion signals are summarised into a per-node status word and a
        global count, and
        <b>the flit itself is dropped rather than queued</b>. Queued, unread
        signals fill a FIFO, raise busy, and stop the agent accepting anything —
        including the very signals that return dispatch credits. A host that
        never reads would wedge the control plane after a FIFO's worth of
        completions.
      </p>
      <p>
        A count rather than a flag means a host polling slower than events
        arrive can tell how many it missed. The global count exists because “is
        everyone finished” against a per-node mirror would otherwise cost one
        poll per node and grow the host program with the machine.
      </p>
    </Callout>

    <Callout kind="note" title="Why a raw flit mailbox exists">
      <p>
        Inject and receive any flit, malformed ones included. An address-mapped
        bridge could only ever emit memory requests — never an instruction,
        never a deliberately bad header — so bring-up and fault injection would
        have no mechanism.
      </p>
      <p>
        Both of the agent's RAMs are LUTRAM for structural reasons rather than
        preference. The staging RAM's read destination is a variable part-select
        and block RAM read data has to land in a plain register; a
        <code>ram_style</code> attribute asking for block is rejected as
        infeasible and <b>silently downgraded</b> — and an ignored attribute
        reads exactly like a guarantee, so none is written. The status mirror
        does a read-modify-write of one address in one cycle, which block RAM
        cannot do.
      </p>
    </Callout>

    <Callout
      kind="rule"
      title="Command a submodule through a slice of the control window"
    >
      <p>
        The mover's command path is a slice of the control window, not a set of
        boundary ports. That is a design rule with a scar behind it:
        <b
          >loose sideband ports never get wired up in a block design, and a
          shipped engine that nothing could command is worse than no engine.</b
        >
        The window forwards writes verbatim with the offset preserved, so the
        client keeps its own register offsets.
      </p>
    </Callout>

    <h2 class="doc-h2">What a port costs</h2>
    <SpecTable :cols="costCols" :rows="costRows" />

    <Callout
      kind="note"
      title="Two structural choices keep the slot logic cheap"
    >
      <p>
        A per-slot “the next beat is the last” term is precomputed from
        registered state only, so the ready decision is a 1-bit select rather
        than mux-then-add-then-compare; and free / match / pick are three
        separate priority scans over the slot array rather than one scan with
        conditions, so none of them lands in the other's path.
      </p>
      <p>
        Disabled, every one of the interlink's nets is tied to a constant, every
        use folds, and the generated top does not expose the ports at all — so a
        build without it is identical to one made before it existed. That is
        maintained deliberately: every addition sits inside a generate or is
        gated by the parameter, because
        <b>“costs nothing when off” is only true if someone keeps checking</b>.
      </p>
    </Callout>

    <h2 class="doc-h2">Two properties that hold by construction</h2>
    <p class="doc-p">
      Both are arguments rather than test results, which means you can check
      them rather than trust them — and both are load-bearing for code you will
      write.
    </p>

    <Callout
      kind="rule"
      title="Arrival order is not load-bearing, and that is a proof"
    >
      <p>
        Every response flit carries its own destination: <code>txn</code> is the
        requester's tag <b>plus this entry's index in the run</b>, and
        <code>rsvd[1:0]</code> is the word's index <b>within the entry</b>. A
        receiver therefore computes its write address from the header alone.
      </p>
      <p>
        So for any interleaving of any number of flits from any number of runs,
        each flit's landing place is a function of that flit and nothing else —
        <b>there is no receiver state for an interleaving to corrupt</b>. That
        is what makes a streaming fetch possible at all: one request, hundreds
        of cycles of traffic, and a receiver that needs no cursor.
      </p>
      <p>
        <b>The proof has one precondition and it is yours to hold:</b> the
        addition is 8-bit and wraps silently, so a requester MUST size its tag
        space such that <code>txn + count − 1 ≤ 255</code>. A run that wraps
        aliases two entries onto one slot, and the property fails with nothing
        reported.
      </p>
    </Callout>

    <Callout
      kind="rule"
      title="Content-independent backpressure is self-clearing, and that is a proof"
    >
      <p>
        <code>mem_in_busy</code> is a function of this port's own queue
        occupancy and nothing else. It never depends on what the arriving flit
        is.
      </p>
      <p>
        The argument: the mesh is in-order behind a busy signal. If a port
        raised busy because it could not accept <i>this particular</i> flit,
        then everything behind that flit stops too — including, in the general
        case, the flit that would have freed the resource the port is waiting
        for. That is a cycle, and no buffer depth removes it. Deciding from
        <b>local state only</b> breaks the cycle by construction: the condition
        that raised busy is cleared by the port's own draining, which does not
        depend on any inbound flit.
      </p>
      <p>
        <b>The same argument forces the two intake queues.</b> One queue would
        put a read request at the head in front of the write data behind it —
        and that data is exactly what lets a drain finish. Busy is still “is
        there room in both”, which is still local state, so splitting the queue
        does not reintroduce the hazard it removes.
      </p>
    </Callout>

    <h2 class="doc-h2">Building a unit against it</h2>
    <h3 class="doc-h3">Sizing what you have to hold</h3>
    <p class="doc-p">
      Three numbers fall out of the protocol and you need all three before
      writing a fill path. A response is
      <code>entry_words</code> flits per entry and <code>count</code> entries
      per run, so
      <b
        >one request produces <code>entry_words × count</code> response flits</b
      >
      — up to <code>4 × 255</code> = 1,020 at the defaults, arriving over as
      many cycles as the fabric takes. Your unit must be able to absorb every
      one of them: <b>credits are your obligation, not the fabric's</b>, and
      issuing a request whose response you cannot absorb is how a fabric
      deadlocks. On the write side a burst is <code>len + 1</code> beats with
      <code>len ≤ 7</code>, so a store larger than 8 beats is several
      descriptors, and <b>a source MUST NOT have two writes open at once</b>
      because slots are matched by source coordinate alone.
    </p>

    <h3 class="doc-h3">A procedure</h3>
    <ol class="doc-p list-decimal pl-5 space-y-1">
      <li>
        <b>Decide the shape your unit wants operands in</b>, then set
        <code>entry_words</code> to match it — or accept 4 and rearrange the
        region with the mover instead. Do not expect the agent to slice
        differently per request.
      </li>
      <li>
        <b>Write the fill path against the header, never a counter.</b> Derive
        the write address from <code>txn</code> and <code>rsvd[1:0]</code>. A
        cursor is correct only for as long as there is exactly one server, and
        nothing tells you when that stops being true.
      </li>
      <li><b>Size your tag space</b> so a run cannot wrap the 8-bit sum.</li>
      <li>
        <b>Drop your write acks.</b> Slot sizing assumes you do not wait on
        them, and a unit that waits <i>and</i> a slot count sized for a unit
        that does not is a deadlock.
      </li>
      <li>
        <b>Lay operands out so a pass is one contiguous run.</b> Scattered
        entries turn one streaming request into many single-entry ones and the
        per-request overhead is then paid per entry.
      </li>
      <li>
        <b>Name extra destinations</b> when several units sweep the same
        operand, rather than issuing identical requests.
      </li>
      <li>
        <b>Put format conversion in the transform slot</b>, scheduled as a mover
        pass — not in your unit, and not on the fetch. A fetch is never
        transformed.
      </li>
    </ol>

    <Callout kind="open" title="Open questions the flow does not answer">
      <p>
        <b>Nothing elects an issuer for a shared fetch.</b> A read may name up
        to three extra destinations, but a sharing set has to arrive at one
        issuer by a rule its own driver enforces — the framework
        <b>neither elects one nor detects two</b>. The mechanism is decoded by
        the hardware and the reference driver does not set it, because a
        follower cannot yet tell which fill an arriving entry belongs to. The
        traffic reduction is one rendezvous away from being usable.
      </p>
      <p>
        <b>Nothing checks a slot count against a mesh.</b>
        <code>WR_SLOTS</code> must be at least two per node that can have a
        write in flight, and that is arithmetic nobody performs — an under-sized
        array does not corrupt anything, it deadlocks, and the symptom appears
        at a node that did nothing wrong.
      </p>
      <p>
        <b>No latency bound is offered on a write ack.</b> The read-response
        emitter outranks the ack path, so an ack can be delayed for the whole
        duration of a streaming fetch. It cannot be starved indefinitely — but
        if you need a bound, there is not one.
      </p>
    </Callout>

    <h2 class="doc-h2">Conventions</h2>
    <p class="doc-p">
      This is the system that forces the most on a compute unit, because
      <b
        >the memory agent hands you data in a shape whether you like it or
        not.</b
      >
      Four of these are not really optional; the rest are advice with a reason.
    </p>
    <SpecTable :cols="convCols" :rows="convRows" />

    <h2 class="doc-h2">Fixed protocol, addon, convention, or yours</h2>
    <SpecTable :cols="catCols" :rows="catRows" />

    <Callout kind="note" title="One boundary is a claim, not a description">
      <p>
        <b>The control agent is a separate system that MAG hosts.</b> It shares
        the memory ports because attachments are scarce, not because dispatch is
        a memory concern. Everything about it — staging, credits, the status
        mirror, the mailbox — would be unchanged if the memory ports were
        replaced wholesale. In the source that shows as packaging rather than
        design: <code>noc_orchestrator.v</code> sits with the router and is
        instantiated by exactly one module, the memory gateway.
      </p>
      <p>
        The same is true of the interlink. Five modules —
        <code>mag_link</code>, <code>mag_link_pipe</code>,
        <code>mag_switch</code>, <code>mag_ilink</code> and
        <code>il_pkt_arb</code> — implement a second routing layer with its own
        topology, its own deadlock argument and its own credit protocol, and
        they live inside the gateway because the gateway hosts the endpoint. The
        package boundary belongs with the ship.
      </p>
    </Callout>
  </DocPage>
</template>
