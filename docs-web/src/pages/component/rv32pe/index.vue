<script setup>
// ===========================================================================
// The RV32 PE — the contract.
// Presents docs/arch/cpu/rv32-pe/README.md and architecture.md.
//
// Every resource and frequency figure comes from ONE run:
//   scripts/tcl/ooc_rv_pe.tcl, top rv_pe, xcvu13p-fhgb2104-2L-e,
//   Vivado 2024.2, out-of-context SYNTHESIS (not placed, not routed),
//   -flatten_hierarchy none, 2.500 ns request, shipped configuration.
// Per-unit numbers are the hierarchical LUT-SITE column of that run.
// Cycle figures are the PE's own CTL_CYCLE / CTL_INSTRET counters on the
// full-system simulation driven by tests/pe/tools/rv_run.py.
// ===========================================================================

const assembly = {
  nodes: [
    {
      id: "fab",
      x: 0,
      y: 0,
      w: 12,
      h: 3.4,
      label: "the mesh",
      sub: "a router coordinate, a window in the map",
    },
    {
      id: "base",
      x: 0,
      y: 5,
      w: 12,
      h: 4.2,
      label: "u_base — noc_cu_base",
      sub: "the compute-unit port · 498 LUT · 4 BRAM",
      accent: true,
    },
    {
      id: "req",
      x: 0,
      y: 11,
      w: 12,
      h: 4,
      label: "u_req",
      sub: "the memory requestor · 274 LUT",
    },
    {
      id: "l1",
      x: 0,
      y: 16.8,
      w: 12,
      h: 4,
      label: "u_l1",
      sub: "internal L1 · 128 lines over DRAM · 365 LUT · 1 BRAM",
    },
    {
      id: "glue",
      x: 19,
      y: 5,
      w: 13,
      h: 4.2,
      label: "window writer + kick FSM",
      sub: "191 LUT · CU_DATA in, CU_SIGNAL out",
    },
    {
      id: "imem",
      x: 19,
      y: 11,
      w: 6,
      h: 4,
      label: "u_imem",
      sub: "2048 × 32 · 2 BRAM",
    },
    {
      id: "spad",
      x: 26,
      y: 11,
      w: 6,
      h: 4,
      label: "u_spad",
      sub: "2048 × 32 · 2 BRAM · 46 LUT",
    },
    {
      id: "core",
      x: 19,
      y: 16.8,
      w: 13,
      h: 4,
      label: "u_core",
      sub: "the RV32IM pipeline · 1,298 LUT · 4 DSP",
      accent: true,
    },
  ],
  edges: [
    { from: "fab:b", to: "base:t", accent: true },
    { from: "base:r", to: "glue:l", label: "CU_DATA" },
    { from: "glue:b", to: "imem:t", label: "buf_id 1" },
    { from: "glue:b", to: "spad:t", label: "buf_id 0" },
    { from: "imem:b", to: "core:t", label: "fetch" },
    { from: "spad:b", to: "core:t", label: "lw / sw" },
    { from: "core:l", to: "l1:r", label: "DRAM" },
    { from: "l1:t", to: "req:b", label: "32-byte line" },
    {
      from: "req:t",
      to: "base:b",
      label: "1 write out",
      accent: true,
    },
  ],
  groups: [{ x: -1.2, y: 4.4, w: 34.4, h: 17.2, label: "rv_pe" }],
};

const bufIds = {
  cols: [
    { key: "id", label: "buf_id", align: "center", mono: true },
    { key: "t", label: "Target" },
    { key: "g", label: "Granularity" },
  ],
  rows: [
    { id: "0", t: "scratchpad window", g: "raw 32-byte granules" },
    { id: "1", t: "instruction window", g: "raw 32-byte granules" },
    {
      id: "3",
      t: "<b>reserved to the framework</b>",
      g: "rejected by this unit",
      _tone: "bad",
    },
    { id: "4", t: "scratchpad window", g: "one 32-bit word, byte enabled" },
    { id: "5", t: "instruction window", g: "one 32-bit word" },
    {
      id: "anything else",
      t: "—",
      g: "rejected, counted out of the burst, and reported",
      _tone: "bad",
    },
  ],
};

/* --- the instruction set -------------------------------------------------- */

const isa = {
  cols: [
    { key: "f", label: "Feature" },
    { key: "s", label: "Status" },
  ],
  rows: [
    {
      f: "RV32I base integer ISA",
      s: "implemented, co-simulated instruction by instruction",
    },
    {
      f: "<b><code>mul</code>, <code>mulh</code>, <code>mulhsu</code>, <code>mulhu</code></b>",
      s: "<b>implemented.</b> One shared 33 × 33 signed multiplier serves all four forms, on the standard <code>OP</code> encoding. A multiply costs 3 stall cycles",
      _tone: "good",
    },
    {
      f: "<b><code>div</code>, <code>divu</code>, <code>rem</code>, <code>remu</code></b>",
      s: "<b>absent, and refused by name.</b> The same decode raises an illegal-instruction halt, so the encoding is recognised and rejected rather than falling through to a multiply",
      _tone: "bad",
    },
    {
      f: "CSRs, <code>Zicsr</code>",
      s: "<b>absent.</b> No CSR file exists; counters are memory-mapped",
    },
    {
      f: "interrupts, traps, trap vectors",
      s: "<b>absent.</b> The unit halts; it is never interrupted",
    },
    {
      f: "<code>FENCE</code>",
      s: "executes as NOP — one core, one memory port, already ordered",
    },
    {
      f: "<code>FENCE.I</code>",
      s: "not needed: the instruction window is not writable from the data side",
    },
    {
      f: "misaligned load/store",
      s: "<b>faults</b> (RV32I permits either fixup or fault)",
    },
    {
      f: "<code>ECALL</code>, <code>EBREAK</code>",
      s: "halt the unit, and the halt word travels in the completion",
    },
    {
      f: "<b>floating point — <code>F</code>, <code>D</code>, <code>Zfh</code></b>",
      s: "<b>absent.</b> No <code>f0..f31</code>, no <code>fcsr</code>, no rounding mode. Not “not yet”: there is no float register file to name",
      _tone: "bad",
    },
    {
      f: "atomics, <code>A</code>",
      s: "absent; exclusive access between units is ownership and push, not locks",
    },
  ],
};

const arith = {
  cols: [
    { key: "c", label: "Class" },
    { key: "m", label: "Integer multiply" },
    { key: "d", label: "Divide" },
    { key: "f", label: "Scalar float" },
  ],
  rows: [
    {
      c: "<b>this core — the RV32 PE</b>",
      m: "<b>yes</b>, RV32M's multiply half, on the scalar register file",
      d: "no — faults",
      f: "no",
      _tone: "good",
    },
    {
      c: "the SIMD PE",
      m: "its scalar half is this core, unchanged. The vector multiply is in the <i>vector</i> register file behind custom-0",
      d: "no — faults",
      f: "no — the float tier is a vector tier",
    },
    {
      c: "the SIMT PE",
      m: "RV32M on its <i>per-thread</i> register file, one product per lane. Its <i>uniform</i> ALU is the ten register-register operations RV32I has, and has no multiply",
      d: "no — faults, per-thread included",
      f: "no — the float tier is a lane tier",
    },
  ],
};

/* --- the two L1s ---------------------------------------------------------- */

const twoL1 = {
  nodes: [
    {
      id: "fab",
      x: 0,
      y: 0,
      w: 12,
      h: 7,
      label: "the fabric",
      sub: "CU_DATA bursts, and a peer unit's push",
    },
    {
      id: "core",
      x: 0,
      y: 10,
      w: 12,
      h: 7,
      label: "this core",
      sub: "fetch · lw · sw",
    },
    {
      id: "ext",
      x: 20,
      y: 0,
      w: 12,
      h: 7,
      label: "external L1",
      sub: "the two windows · real SRAM · no tags · the HOME of its addresses",
      accent: true,
    },
    {
      id: "int",
      x: 20,
      y: 10,
      w: 12,
      h: 7,
      label: "internal L1",
      sub: "a tagged cache · direct mapped · 128 lines",
      accent: true,
    },
    {
      id: "dram",
      x: 40,
      y: 10,
      w: 12,
      h: 7,
      label: "global DRAM",
      sub: "through the memory agent",
    },
  ],
  edges: [
    { from: "fab:r", to: "ext:l", label: "writes" },
    { from: "core:r", to: "ext:l", label: "reads and writes" },
    { from: "core:r", to: "int:l", label: "lw / sw only" },
    { from: "int:r", to: "dram:l", label: "fills, writebacks" },
  ],
};

const l1s = {
  cols: [
    { key: "k", label: "" },
    { key: "ext", label: "external L1" },
    { key: "int", label: "internal L1" },
  ],
  rows: [
    {
      k: "<b>what it is</b>",
      ext: "real SRAM windows mapped into the global address space",
      int: "a tagged cache over global DRAM",
    },
    {
      k: "<b>who writes it</b>",
      ext: "the fabric, and this core",
      int: "this core only, plus fills",
    },
    {
      k: "<b>tags</b>",
      ext: "none — the address-region decode <b>is</b> the lookup",
      int: "yes, direct mapped",
    },
    {
      k: "<b>holds</b>",
      ext: "program text and data",
      int: "copies of DRAM lines",
    },
    {
      k: "<b>coherence case</b>",
      ext: "none: it is the <b>home</b> of its addresses, never a copy",
      int: "none: <b>never externally written</b>",
      _tone: "good",
    },
  ],
};

/* --- the map -------------------------------------------------------------- */

const regions = {
  cols: [
    { key: "addr", label: "Software address", mono: true },
    { key: "region", label: "Region" },
    { key: "sem", label: "Semantics — contract" },
    { key: "cost", label: "Cost" },
  ],
  rows: [
    {
      addr: "0x0xxx_xxxx",
      region: "instruction window",
      sem: "<b>not addressable from the data side</b> — self-modifying code is a fault, not a race",
      cost: "faults",
      _tone: "bad",
    },
    {
      addr: "0x1xxx_xxxx",
      region: "scratchpad",
      sem: "read/write. Writable by this core and by the fabric; a fabric write landing in a word being read returns the <b>new</b> bytes",
      cost: "1 cycle, always",
    },
    {
      addr: "0x2xxx_xxxx",
      region: "local control",
      sem: "word registers; some reads, some stores with side effects",
      cost: "1 cycle",
    },
    {
      addr: "0x3xxx_xxxx",
      region: "peer windows",
      sem: "<b>store only</b> — a store becomes a push into another unit's window; a load faults",
      cost: "1 cycle + hold",
    },
    {
      addr: "—",
      region: "dispatch",
      sem: "<b>store only</b> — three stores construct and fire a <code>CU_INST</code> at another compute unit; a load faults",
      cost: "1 cycle each, the third holds",
    },
    {
      addr: "—",
      region: "vector scratchpad",
      sem: "<b>store only, and present only when the SIMD extension is built.</b> Without it the region is unmapped and both loads and stores fault",
      cost: "1 cycle",
    },
    {
      addr: "0x8xxx_xxxx ↑",
      region: "global DRAM",
      sem: "cached read/write through the internal L1",
      cost: "hit 1 cycle, miss a fill round trip",
    },
    {
      addr: "anything else",
      region: "—",
      sem: "faults",
      cost: "—",
      _tone: "bad",
    },
  ],
};

const peerAddr = [
  { name: "0x3", bits: 4, value: "the region", accent: true },
  { name: "dest x", bits: 4 },
  { name: "dest y", bits: 4 },
  { name: "window", bits: 1, value: "0 spad / 1 imem" },
  { name: "granule index", bits: 14, value: "32-byte granules" },
  { name: "word", bits: 3, value: "within the granule" },
  { name: "byte", bits: 2, value: "the store's byte enables" },
];

const peerSpec = {
  cols: [
    { key: "f", label: "Field", mono: true },
    { key: "w", label: "Width", align: "right", mono: true },
    { key: "p", label: "Position", mono: true },
    { key: "o", label: "Owner" },
  ],
  rows: [
    {
      f: "region",
      w: "4",
      p: "[31:28]",
      o: "<b>the decode.</b> <code>0x3</code> is what separates a push from a DRAM store, and it is why a push is never cached",
    },
    {
      f: "dest x / dest y",
      w: "4 each",
      p: "[27:24] [23:20]",
      o: "<b>the program</b> — this is the routing. The requestor puts them straight into the flit header's destination field",
    },
    {
      f: "window",
      w: "1",
      p: "[19]",
      o: "you. 0 selects the peer's scratchpad, 1 its instruction window — which is how one PE loads another's program",
    },
    {
      f: "granule index",
      w: "14",
      p: "[18:5]",
      o: "you, in 32-byte granules. <b>The receiving window bounds-checks it</b>; an out-of-range push is dropped",
    },
    {
      f: "word",
      w: "3",
      p: "[4:2]",
      o: "you — which 32-bit word of the granule the store lands in",
    },
    {
      f: "byte",
      w: "2",
      p: "[1:0]",
      o: "<b>the store instruction.</b> <code>sb</code> and <code>sh</code> work: the byte enables travel with the push and the receiving window applies them",
    },
  ],
};

/* --- push ordering -------------------------------------------------------- */

const pushCachedBroken = {
  rows: [
    {
      name: "program",
      kind: "bus",
      values: ["sw payload", "sw doorbell", "—", "—", "—"],
    },
    {
      name: "line state",
      kind: "text",
      values: ["dirty", "dirty", "evict flag", "", "evict payload"],
    },
    {
      name: "on the wire",
      kind: "bus",
      values: [null, null, "doorbell", null, "payload"],
      mark: [2],
    },
    {
      name: "consumer polls",
      kind: "text",
      values: ["", "", "", "sees the flag", "reads stale"],
    },
  ],
  notes: [
    {
      cycle: 1,
      text: "Both stores hit in the cache and both lines are dirty. Program order has been recorded, and nothing has left the unit.",
      tone: "bad",
    },
    {
      cycle: 2,
      text: "A dirty line leaves whenever the cache chooses — on a conflict miss, on a sweep, on whatever the replacement policy does next. Here the doorbell's line is evicted first, because nothing ties eviction order to store order.",
      tone: "bad",
    },
    {
      cycle: 3,
      text: "The consumer's poll succeeds against a payload that has not arrived. It reads whatever was in its scratchpad before, and the handshake has inverted: the flag now means LESS than nothing, because it actively asserts something false.",
      tone: "bad",
    },
  ],
};

const pushHoldBroken = {
  rows: [
    {
      name: "program",
      kind: "bus",
      values: ["sw payload", "sw doorbell", "—", "—", "—"],
    },
    {
      name: "M stage",
      kind: "text",
      values: ["retires", "retires", "", "", ""],
      mark: [1],
    },
    {
      name: "requestor slot",
      kind: "bus",
      values: [null, "payload", "doorbell", "—", "—"],
      mark: [2],
    },
    {
      name: "on the wire",
      kind: "bus",
      values: [null, null, null, "doorbell", null],
    },
    {
      name: "the payload",
      kind: "text",
      values: ["", "", "overwritten", "", "never sent"],
    },
  ],
  notes: [
    {
      cycle: 1,
      text: "The region is uncached, so the ordering problem above cannot happen — but the store retires when it is ISSUED rather than when the requestor accepted it.",
      tone: "bad",
    },
    {
      cycle: 2,
      text: "The next store arrives at a requestor whose single holding slot is still full. The payload has not gone out and is replaced by the doorbell that was meant to announce it.",
      tone: "bad",
    },
    {
      cycle: 4,
      text: "One flit leaves where two were issued. The consumer sees the flag and the payload never existed — the same end state as the cached case, reached without a cache being involved, which is why the region decode alone is not the whole fix.",
      tone: "bad",
    },
  ],
};

const pushFixed = {
  rows: [
    {
      name: "program",
      kind: "bus",
      values: ["sw payload", "sw doorbell", "—", "—", "—"],
    },
    { name: "M holds", kind: "bit", values: [1, 0, 1, 0, 0], mark: [0, 2] },
    {
      name: "requestor took it",
      kind: "bit",
      values: [0, 1, 0, 1, 0],
    },
    {
      name: "on the wire",
      kind: "bus",
      values: [null, "payload", null, "doorbell", null],
    },
    {
      name: "consumer polls",
      kind: "text",
      values: ["", "", "", "", "flag, then data"],
    },
  ],
  notes: [
    {
      cycle: 0,
      text: "The memory stage holds the store until the requestor has TAKEN the push, not until it has been offered one. That single property is what makes program order equal arrival order.",
      tone: "good",
    },
    {
      cycle: 3,
      text: "The doorbell can only be issued after the payload is on the wire, and the mesh delivers in order per source-destination pair — so it can only ARRIVE after it too.",
      tone: "good",
    },
    {
      text: "Ordering holds per destination. Pushes to two different PEs have no order between them, so a doorbell in one peer's window says nothing about a payload written into another's.",
      tone: "good",
    },
  ],
};

const ctl = {
  cols: [
    { key: "off", label: "Offset", mono: true },
    { key: "name", label: "Name", mono: true },
    { key: "acc", label: "Access" },
    { key: "mean", label: "Meaning" },
  ],
  rows: [
    {
      off: "0x00",
      name: "CTL_STATUS",
      acc: "read",
      mean: "bit 0 <code>flush_busy</code>, bit 1 writes outstanding",
    },
    {
      off: "0x04",
      name: "CTL_FLUSH",
      acc: "<b>store</b>",
      mean: "write back every dirty line — <b>blocking</b>",
    },
    {
      off: "0x08",
      name: "CTL_INVAL",
      acc: "<b>store</b>",
      mean: "drop every line, dirty included — blocking, one cycle per line",
    },
    {
      off: "0x0C",
      name: "CTL_CAUSE",
      acc: "read",
      mean: "halt cause of the last halt",
    },
    {
      off: "0x10",
      name: "CTL_COREID",
      acc: "read",
      mean: "<code>{y, x}</code>, this PE's mesh coordinate",
    },
    {
      off: "0x14",
      name: "CTL_ARG",
      acc: "read",
      mean: "the word the kick carried",
    },
    {
      off: "0x18",
      name: "CTL_CYCLE",
      acc: "read",
      mean: "cycles since the kick",
    },
    {
      off: "0x1C",
      name: "CTL_INSTRET",
      acc: "read",
      mean: "instructions retired since the kick",
    },
    {
      off: "0x20",
      name: "CTL_WROUT",
      acc: "read",
      mean: "writes not yet acknowledged",
    },
  ],
};

const ordering = {
  cols: [
    { key: "n", label: "#", align: "center", mono: true },
    { key: "rule", label: "The rule" },
    { key: "why", label: "What rests on it" },
  ],
  rows: [
    {
      n: "1",
      rule: "<b>Program order is arrival order, per destination.</b> The requestor emits one burst at a time and the mesh preserves order between one source and one destination.",
      why: "push-and-doorbell. No rule relates pushes to <i>different</i> destinations.",
    },
    {
      n: "2",
      rule: "<b>A write is in memory when it is acknowledged, not when it leaves.</b>",
      why: "completion, flush, and any dependence on “the data is there”",
    },
    {
      n: "3",
      rule: "<b>A store to <code>CTL_FLUSH</code> completes only after every dirty line is written back and acknowledged.</b>",
      why: "flush-then-doorbell needs no barrier machinery",
    },
    {
      n: "4",
      rule: "<b>A store to <code>CTL_INVAL</code> completes only after every line is dropped.</b>",
      why: "a load after it cannot hit a stale line",
    },
  ],
};

const handoff = [
  {
    title: "writer: … stores …",
    code: ["sw   t0, 0(a1)", "sw   t1, 4(a1)", "..."],
    where: "the writer's internal L1 — dirty",
    note: "Nothing outside this core can see any of it yet. The lines are dirty in a write-back cache.",
  },
  {
    title: "writer: sw x0, 0(CTL_FLUSH)",
    code: ["sw   x0, 0(CTL_FLUSH)   -- blocking"],
    where: "DRAM, acknowledged",
    note: "A store to CTL_FLUSH completes only after every dirty line is written back and acknowledged, so the instruction after it cannot overtake the data. About 12 cycles per dirty line against a prompt agent.",
  },
  {
    title: "writer: sw doorbell → the reader's window",
    code: ["sw   s0, 0(DOORBELL)    -- the LAST store"],
    where: "in flight to the reader's scratchpad",
    note: "The doorbell is the last store. Everything it announces precedes it, by rule 1 — program order is arrival order, per destination.",
  },
  {
    title: "reader: poll its own scratchpad",
    code: ["poll:", "  lw   t0, 0(s1)", "  beq  t0, zero, poll"],
    where: "an ordinary load — 1 cycle, no fabric traffic",
    note: "A push landing in the very word being polled returns the pushed bytes. That is what the scratchpad's cross-port bypass buys.",
  },
  {
    title: "reader: sw x0, 0(CTL_INVAL)",
    code: ["sw   x0, 0(CTL_INVAL)   -- blocking, 1 cycle per line"],
    where: "the reader's L1 — empty",
    note: "A sweep, one line per cycle, nothing on the wire. Without it the reader may answer from a line it filled before the writer ran.",
  },
  {
    title: "reader: … loads …",
    code: ["lw   a0, 0(a2)", "..."],
    where: "filled from DRAM — the writer's data",
    note: "Both controls exist for this sequence. Peer windows carry control; data of any size goes through DRAM.",
  },
];

/* --- halting and the unit protocol --------------------------------------- */

const halts = {
  cols: [
    { key: "cause", label: "Cause", align: "center", mono: true },
    { key: "by", label: "Raised by" },
    { key: "word", label: "Halt word", mono: true },
    { key: "code", label: "Completion code", mono: true },
  ],
  rows: [
    { cause: "1", by: "<code>ECALL</code>", word: "a0", code: "0x00" },
    { cause: "2", by: "<code>EBREAK</code>", word: "a0", code: "0x04" },
    {
      cause: "3",
      by: "illegal encoding, misaligned access, or an unmapped region",
      word: "the offending PC",
      code: "0x04",
    },
  ],
};

/* --- what it costs -------------------------------------------------------- */

const units = {
  cols: [
    { key: "u", label: "Instance" },
    { key: "lut", label: "Total LUT", align: "right", mono: true },
    { key: "logic", label: "Logic LUT", align: "right", mono: true },
    { key: "lram", label: "LUTRAM", align: "right", mono: true },
    { key: "ff", label: "FF", align: "right", mono: true },
    { key: "bram", label: "BRAM", align: "right", mono: true },
    { key: "dsp", label: "DSP", align: "right", mono: true },
  ],
  rows: [
    {
      u: "<b>rv_pe — the whole unit</b>",
      lut: "<b>2,672</b>",
      logic: "2,436",
      lram: "236",
      ff: "<b>3,844</b>",
      bram: "<b>9</b>",
      dsp: "<b>4</b>",
    },
    {
      u: "<code>(rv_pe)</code> — the top level itself: window writer, kick FSM",
      lut: "191",
      logic: "191",
      lram: "0",
      ff: "477",
      bram: "0",
      dsp: "0",
    },
    {
      u: "<code>u_base</code> — the compute-unit port",
      lut: "498",
      logic: "410",
      lram: "88",
      ff: "840",
      bram: "4",
      dsp: "0",
    },
    {
      u: "<code>u_core</code> — the RV32IM pipeline",
      lut: "1,298",
      logic: "1,226",
      lram: "72",
      ff: "1,085",
      bram: "0",
      dsp: "4",
    },
    {
      u: "<code>u_l1</code> — internal L1, 128 lines",
      lut: "365",
      logic: "317",
      lram: "48",
      ff: "413",
      bram: "1",
      dsp: "0",
    },
    {
      u: "<code>u_req</code> — the memory requestor",
      lut: "274",
      logic: "246",
      lram: "28",
      ff: "992",
      bram: "0",
      dsp: "0",
    },
    {
      u: "<code>u_imem</code> — instruction window",
      lut: "0",
      logic: "0",
      lram: "0",
      ff: "0",
      bram: "2",
      dsp: "0",
    },
    {
      u: "<code>u_spad</code> — scratchpad",
      lut: "46",
      logic: "46",
      lram: "0",
      ff: "37",
      bram: "2",
      dsp: "0",
    },
  ],
};

const coreSplit = {
  items: [
    {
      label: "u_ex — ALU, multiply, resolve, address, byte enables (+ 4 DSP)",
      value: 489,
      tone: "accent",
    },
    { label: "u_id — operands, forwarding, branch-target adder", value: 248 },
    { label: "u_mem — the region decode and the sub-word extract", value: 206 },
    { label: "u_if — fetch, with the whole predictor", value: 135 },
    { label: "u_rf — the register file", value: 129 },
    { label: "rv_core itself — hazards, run/halt, counters", value: 71 },
    { label: "u_wb", value: 20 },
  ],
};

const asks = {
  cols: [
    { key: "r", label: "Request", mono: true },
    { key: "l", label: "LUT", align: "right", mono: true },
    { key: "f", label: "FF", align: "right", mono: true },
    { key: "b", label: "BRAM", align: "right", mono: true },
    { key: "d", label: "DSP", align: "right", mono: true },
    { key: "x", label: "Fmax", align: "right", mono: true },
    { key: "s", label: "Slack", align: "right", mono: true },
    { key: "v", label: "" },
  ],
  rows: [
    {
      r: "3.333 ns · a 300 MHz ask",
      l: "<b>2,586</b>",
      f: "3,844",
      b: "9",
      d: "4",
      x: "<b>363.5</b>",
      s: "<b>+0.582</b>",
      v: "<b>constrain it here</b>",
      _tone: "good",
    },
    {
      r: "2.500 ns · a 400 MHz ask",
      l: "2,672",
      f: "3,844",
      b: "9",
      d: "4",
      x: "363.5",
      s: "<b>−0.251</b>",
      v: "86 extra LUT sites for zero megahertz",
      _tone: "warn",
    },
  ],
};

const bram = {
  cols: [
    { key: "arr", label: "Array" },
    { key: "shape", label: "Words × width", mono: true },
    { key: "tiles", label: "Tiles", align: "right", mono: true },
    { key: "depth", label: "Depth used", align: "right", mono: true },
  ],
  rows: [
    {
      arr: "instruction window",
      shape: "2048 × 32",
      tiles: "2",
      depth: "<b>100 %</b>",
    },
    {
      arr: "scratchpad",
      shape: "2048 × 32",
      tiles: "2",
      depth: "<b>100 %</b>",
    },
    {
      arr: "internal L1 data",
      shape: "1024 × 32",
      tiles: "1",
      depth: "<b>100 %</b>",
    },
    {
      arr: "the compute-unit port's receive FIFO",
      shape: "—",
      tiles: "4",
      depth: "not reported by the script",
    },
  ],
};

const itiming = {
  cols: [
    { key: "ev", label: "Event" },
    { key: "cost", label: "Cost" },
  ],
  rows: [
    { ev: "most instructions", cost: "1 cycle" },
    {
      ev: "<code>mul</code>, <code>mulh</code>, <code>mulhsu</code>, <code>mulhu</code>",
      cost: "3 stall cycles",
    },
    {
      ev: "<code>div</code>, <code>rem</code>",
      cost: "<b>fault</b> — not built on any class",
      _tone: "bad",
    },
    { ev: "load-use, back to back", cost: "2 stall cycles" },
    { ev: "load-use at a spacing of one", cost: "1 stall cycle" },
    { ev: "taken branch, predicted", cost: "0", _tone: "good" },
    { ev: "mispredict or unpredicted taken branch", cost: "3 cycles" },
    {
      ev: "peer-window push",
      cost: "1 cycle + hold until the requestor accepts",
    },
    {
      ev: "dispatch: the argument and PC stores",
      cost: "1 cycle each — they write requestor registers and always accept",
    },
    {
      ev: "dispatch: the opcode store",
      cost: "1 cycle + hold until the requestor accepts; it is the doorbell",
    },
    { ev: "scratchpad or control access", cost: "1 cycle, always" },
    {
      ev: "DRAM hit / miss",
      cost: "1 cycle / a fill round trip — hundreds of cycles, dominated by the agent and DRAM",
    },
    {
      ev: "steady-state evict-and-refill pair",
      cost: "~30 cycles against the real agent",
    },
    {
      ev: "flush-all, prompt acknowledgements",
      cost: "~12 cycles per dirty line — 197 for 16",
    },
    {
      ev: "flush-all, slow agent",
      cost: "each line pays the acknowledgement latency — 677 for the same 16",
      _tone: "warn",
    },
    {
      ev: "invalidate-all",
      cost: "one cycle per line, pipeline held, nothing on the wire",
    },
  ],
};

const scaling = {
  cols: [
    { key: "what", label: "The same program, kick to halt" },
    { key: "one", label: "1 PE", align: "right", mono: true },
    { key: "two", label: "2 PEs", align: "right", mono: true },
    { key: "four", label: "4 PEs", align: "right", mono: true },
  ],
  rows: [
    {
      what: "cycles — identical instruction stream at every count, so the whole difference is memory",
      one: "7,418",
      two: "7,778 <span class='opacity-60'>(+4.9 %)</span>",
      four: "8,431 <span class='opacity-60'>(+13.7 %)</span>",
    },
  ],
};

const params = {
  cols: [
    { key: "p", label: "Parameter", mono: true },
    { key: "d", label: "Default", align: "center", mono: true },
    { key: "w", label: "What it decides" },
  ],
  rows: [
    {
      p: "BTB_ENTRIES",
      d: "32",
      w: "predictor size; 0 removes the predictor entirely (a generate, not a zero-sized array)",
    },
    {
      p: "FWD_X",
      d: "1",
      w: "the distance-1 bypass. 0 is measured worse on every axis and remains only as the proof",
    },
    {
      p: "L1_LINES",
      d: "128",
      w: "internal L1 lines. 128 fills the block RAM's natural depth; halving saves almost nothing",
    },
    {
      p: "REGFILE_PRIM",
      d: '"distributed"',
      w: "LUTRAM against block-RAM register file. Interchangeable timing; a resource trade",
    },
    {
      p: "IMEM_WORDS / SPAD_WORDS",
      d: "2048",
      w: "the two windows, sized to fill their block-RAM tiles exactly",
    },
    {
      p: "WR_MAX",
      d: "1",
      w: "un-acknowledged writes. 1 is what the communication model assumes; raising it buys nothing a blocking cache can use",
    },
    {
      p: "SIMD_EN",
      d: "0",
      w: "the wide-datapath seam. At 0 nothing behind it is elaborated and the core is exactly what the figures here measure",
    },
  ],
};

const gates = {
  cols: [
    { key: "g", label: "Level", mono: true },
    { key: "w", label: "What it proves" },
  ],
  rows: [
    {
      g: "--gate 1",
      w: "the core against a Python RV32I golden model, comparing PC, destination and value for <b>every committed instruction</b> — and it runs the <i>configured</i> shape",
    },
    { g: "--gate 2", w: "the memory frontend against the protocol" },
    {
      g: "--gate 3",
      w: "real software on the real memory substrate: boots through the window-write path, checks halt word, completion code and a DRAM checksum",
    },
    {
      g: "--gate 4",
      w: "one, two and four PEs sharing a mesh and a memory agent — where the communication protocol is proven between running cores rather than against a bench",
    },
  ],
};

const notDone = {
  cols: [
    { key: "n", label: "Not present" },
    { key: "w", label: "Why, and what stands in its place" },
  ],
  rows: [
    {
      n: "divide and remainder",
      w: "a fixed ~35-cycle structure with its own subtractor, bought for an instruction a controller issues approximately never. With <code>mul</code> built, divide-by-a-constant strength-reduces to <code>mulhu</code>",
    },
    {
      n: "scalar floating point",
      w: "fifteen cycles into a three-source in-order forwarding network, and it would not be <code>F</code>, so no compiler would emit it. The float arithmetic in this machine is in the wide datapaths",
    },
    {
      n: "CSRs, privilege, interrupts",
      w: "the unit halts and reports; there is no supervisor to trap to. A core that boots once and runs forever is a different processor",
    },
    {
      n: "an MMU",
      w: "the PE's addresses are its windows plus a DRAM aperture fixed at elaboration",
    },
    {
      n: "cache coherence",
      w: "removed by construction: nothing cached is externally written, and the externally written memory is the home of its own addresses",
    },
    {
      n: "multiple outstanding misses",
      w: "the internal L1 is blocking with one outstanding miss; latency tolerance comes from having many independent units",
    },
    {
      n: "a loader",
      w: "an image arrives on the same write path as any other data, so there is no second memory-write protocol to go wrong",
    },
    {
      n: "a second clock domain",
      w: "the whole unit is on the fabric clock, so there is no clock-domain crossing anywhere in it",
    },
  ],
};
</script>

<template>
  <DocPage
    title="The RV32 PE"
    summary="A small in-order RISC-V core packaged as an ordinary compute unit: someone kicks it, it runs to completion, it reports a word. RV32I plus the multiply half of M, a memory frontend designed around the fabric rather than bolted to it, and two L1s split by who writes them — which is what removes coherence from the design."
    domain="cpu"
    status="shipped"
    source="src/kohakuaccel/pe/rv32/ · docs/arch/cpu/rv32-pe/architecture.md"
  >
    <p class="doc-p">
      The RV32 PE hangs off one mesh router's local port, accepts one
      instruction at a time, and reports a 32-bit word when it finishes. This
      page is the part of it that software and the surrounding system may rely
      on; an implementation is free to change anything else, and nothing here,
      without a spec change. How it is built is on the
      <RouterLink to="/component/rv32pe/microarchitecture" class="doc-link"
        >microarchitecture</RouterLink
      >
      page.
    </p>

    <p class="doc-p">
      Where it sits: below it is one router's local port and, through that, the
      memory agent that serves its row; above it is whoever started it — a host
      through the control agent, or another PE.
      <b>Nothing in the fabric knows it is a processor.</b>
    </p>

    <h2 class="doc-h2">What it owns</h2>
    <p class="doc-p">Four things are contract. Everything else is free to change.</p>
    <div class="grid gap-3 sm:grid-cols-2 my-5">
      <div class="card p-4">
        <div
          class="kt-text-caption font-semibold text-warm-800 dark:text-warm-200 mb-1"
        >
          The instruction set
        </div>
        <p class="kt-text-caption text-warm-500 dark:text-warm-400 leading-6">
          RV32I plus multiply, with the memory frontend reached through stores
          rather than through opcodes.
        </p>
      </div>
      <div class="card p-4">
        <div
          class="kt-text-caption font-semibold text-warm-800 dark:text-warm-200 mb-1"
        >
          The attach
        </div>
        <p class="kt-text-caption text-warm-500 dark:text-warm-400 leading-6">
          One fabric port through the framework's shell: kicked by a flit,
          answering with one word.
        </p>
      </div>
      <div class="card p-4">
        <div
          class="kt-text-caption font-semibold text-warm-800 dark:text-warm-200 mb-1"
        >
          The memory model
        </div>
        <p class="kt-text-caption text-warm-500 dark:text-warm-400 leading-6">
          Two L1s, a scratchpad that peers can write, and the ordering rules the
          push-and-doorbell idiom rests on.
        </p>
      </div>
      <div class="card p-4">
        <div
          class="kt-text-caption font-semibold text-warm-800 dark:text-warm-200 mb-1"
        >
          Halting
        </div>
        <p class="kt-text-caption text-warm-500 dark:text-warm-400 leading-6">
          How a run ends, what the completion word carries, and what a fault
          reports.
        </p>
      </div>
    </div>

    <p class="doc-p">
      The alternative that was rejected is the one every accelerator starts
      with: <b>a hand-written state machine per unit.</b> It works, it is small,
      and it does the job until the day the policy changes — and then it does
      the job wrong, in RTL, on a device that has to be rebuilt to fix it. A
      processor moves that policy into software at the cost of a fetch, a decode
      and a register file. What makes the trade affordable here is that the
      alternative was never one state machine but dozens, and what makes it
      survivable is that this core is small enough for dozens of copies. That
      constraint is why the pages below argue about LUT rather than about IPC.
    </p>

    <Callout kind="note" title="The words this page is built out of">
      <p>
        <b>Mesh</b> — the grid of routers this machine is built on; every unit
        on it has an (x, y) coordinate.
        <b>Flit</b> — the fixed-size packet the mesh moves; here 288 bits, of
        which 256 are payload.
        <b>Compute unit</b> — anything that attaches to one fabric port, takes
        instructions one at a time, names the memory it wants as an address and
        a length, and signals retirement. The framework has no opinion about
        whether a unit multiplies, sorts or executes a program.
      </p>
      <p>
        <b>Kick</b> — a compute unit's start signal, delivered as a
        <code>CU_INST</code> flit; for this unit it means <i>begin executing at
        this PC</i>.
        <b>Completion</b> — the <code>CU_SIGNAL</code> flit the unit sends back
        when it retires, carrying one word.
        <b>Memory agent</b> — the block that owns a row's path to DRAM and
        serves read and write descriptors to the units on it.
      </p>
      <p>
        <b>Scratchpad</b> — this unit's data window: ordinary memory to its own
        core, and a writable mailbox to everything else on the mesh.
        <b>Push</b> — a store that becomes a write into another unit's window.
        <b>Doorbell</b> — the last push of a sequence, the one a consumer polls
        for; it is what turns ordering into a handshake.
      </p>
    </Callout>

    <p class="doc-p">
      Every accelerator on this framework needs something that decides
      <i>what to do next</i> — sequencing kernels, walking descriptors, reacting
      to completions. Hand-written state machines do it until the day the policy
      changes, and then they do it wrong. The design objectives, in order: RV32I
      compatibility so ordinary compilers work; <b>low LUT</b>, so dozens of
      units on one device are realistic; <b>high frequency</b>, so scalar
      control is never the slow component; and a memory frontend that is part of
      the core rather than a generic CPU bus with an adapter bolted on.
    </p>

    <Callout kind="rule" title="LUT and frequency outrank latency everywhere">
      <p>
        The core spends flip-flops and block RAM freely, prefers a pipeline
        stage over a bypass, and registers anything whose combinational form
        would fan out. Every shape on these two pages follows from that, and
        where it does not, the exception is measured.
      </p>
    </Callout>

    <h2 class="doc-h2">The attach</h2>

    <p class="doc-p">
      One local port, one clock, no AXI, no sideband, no second clock domain. A
      mesh gains a PE the way it gains any unit — a router coordinate and a
      window in the address map. The directory layout is the architecture:
      <code>core/</code> is the pipeline, <code>mem/</code> the two L1s,
      <code>noc/</code> the fabric attach, and <code>rv_pe.v</code> assembles
      them and holds nothing else.
    </p>

    <Fig
      caption="rv_pe — instantiation and wiring only. Boot is not a mechanism: a program image is an ordinary CU_DATA burst into the instruction window, arguments another into the scratchpad, then the standard kick. LUT figures are hierarchical LUT sites from the OOC synthesis run named under “What it costs” below."
      zoom
    >
      <BlockDiagram
        :nodes="assembly.nodes"
        :edges="assembly.edges"
        :groups="assembly.groups"
      />
    </Fig>

    <SpecTable
      :cols="bufIds.cols"
      :rows="bufIds.rows"
      caption="Where a CU_DATA burst lands. A granule descriptor's offset and len are both in granules, and offset + len is range-checked against the named window — rejected, never wrapped. A granule is written as eight 32-bit words, so the unit stops accepting for eight cycles per data flit; that backpressure is bounded by the unit's own progress and never by another inbound flit"
    />

    <Callout kind="rule" title="What a completion asserts">
      <p>
        Completion is <code>CU_SIGNAL</code> to whoever kicked, carrying the
        halt word. It asserts three things at once: the pipeline is empty, the
        requestor is idle, and
        <b>every write the program issued has been acknowledged by memory</b>. A
        host that reads DRAM on seeing the completion finds the program's
        results there — the completion is the host's sequencing point, and it
        would mean nothing weaker.
      </p>
      <p>
        A kick never overtakes the data it announces: the unit holds a kick
        until its receive path is quiet, so an image still being written when
        the kick arrives is finished before fetch begins. The hold clears by the
        unit's own progress and cannot deadlock.
      </p>
    </Callout>

    <h2 class="doc-h2">The instruction set</h2>

    <p class="doc-p">
      <b>RV32I plus the multiply half of <code>M</code></b>, unprivileged,
      executed by an in-order single-issue core. Ordinary compilers and
      hand-written assembly work unmodified, at
      <code>-march=rv32im</code> and nothing else. The deviations from a
      full-featured hart are themselves architectural.
    </p>

    <SpecTable
      :cols="isa.cols"
      :rows="isa.rows"
      caption="Counters that would be CSRs elsewhere — cycle, instructions retired, core id — are words in the local control region"
    />

    <Callout
      kind="trap"
      title="Where the arithmetic in this machine actually is"
    >
      <p>
        Two readings of the table above are each wrong about a different class.
        It is easy to read “the machine has float” and conclude the wrong thing
        about this core, and just as easy to read “a scalar core has no
        multiply” and conclude the wrong thing about the machine.
      </p>
    </Callout>

    <SpecTable
      :cols="arith.cols"
      :rows="arith.rows"
      caption="RV32M cost none of the four custom opcode majors — it went into the standard OP major where RISC-V already put it. Every non-standard encoding any class uses comes out of those four, and there are no more than four"
    />

    <h2 class="doc-h2">The memory model</h2>

    <p class="doc-p">
      Software sees ordinary RV32 addresses.
      <b>One decoder, deciding on the top four address bits and nothing else</b>,
      and each region's semantics are fixed. Underneath that map sit two
      caches — except that only one of them is a cache, and the split between
      them is by <i>who writes</i>, not by what is stored.
    </p>

    <Fig
      caption="The external L1 has two writers and no tags: it is the home of its addresses, never a copy of anything. The internal L1 has exactly one writer — this core — and holds copies of DRAM lines. So there is no external-write-versus-dirty-line case anywhere in this PE, because there is no way to construct one."
      zoom
      wide
    >
      <BlockDiagram :nodes="twoL1.nodes" :edges="twoL1.edges" />
    </Fig>

    <SpecTable :cols="l1s.cols" :rows="l1s.rows" />

    <p class="doc-p">
      The instruction window is not reachable from the data side, which also
      keeps the fetch port exclusive — fetch never contends with a load, and
      <code>FENCE.I</code> has nothing to do.
    </p>

    <SpecTable :cols="regions.cols" :rows="regions.rows" />

    <Callout kind="rule" title="A push is never cached, and the store MUST hold until the requestor takes it">
      <p>
        The peer and dispatch regions are decoded separately from DRAM rather
        than being “DRAM that happens to live elsewhere”: the doorbell protocol
        needs stores on the wire in program order, and a dirty cache line leaves
        whenever the cache chooses. That is the first half. The second is that
        the memory stage <b>MUST</b> hold until the requestor has
        <i>taken</i> the push, not until it has been offered one — which is what
        makes program order equal arrival order.
      </p>
      <p>
        Both halves are required, and dropping either produces the same visible
        failure.
      </p>
    </Callout>

    <WaveTrace
      :rows="pushCachedBroken.rows"
      :notes="pushCachedBroken.notes"
      variant="broken"
      label="A cached push — the doorbell's line is evicted before the payload's"
    />

    <WaveTrace
      :rows="pushHoldBroken.rows"
      :notes="pushHoldBroken.notes"
      variant="broken"
      label="Uncached, but retiring on issue — the doorbell overwrites the payload in the slot"
    />

    <WaveTrace
      :rows="pushFixed.rows"
      :notes="pushFixed.notes"
      variant="fixed"
      label="Uncached and held until taken — program order becomes arrival order"
    />

    <Callout
      kind="trap"
      title="A doorbell that arrives early is worse than one that never arrives"
    >
      <p>
        A missing doorbell hangs the consumer, which is obvious and gets found.
        An early one lets the consumer proceed on data that is not there, so the
        error surfaces as a wrong result in whatever the consumer computed —
        arbitrarily far from the push, in a different unit, with nothing
        connecting the two.
      </p>
      <p>
        The producer sees nothing either way: <b>a push is fire-and-forget and
        there is no acknowledgement</b>. This is the reason the region decode is
        a separate address range rather than a cache attribute — an attribute
        can be got wrong per page, and a range cannot be got wrong at all once
        the linker script is right.
      </p>
    </Callout>

    <h3 class="doc-h3">Peer windows: the address is the routing</h3>

    <BitField
      :fields="peerAddr"
      caption="Word 0 of granule 0 of the scratchpad of the PE at (2,2) is a store to 0x3220_0000. sb and sh work: the byte enables travel with the push and the receiving window applies them"
    />

    <SpecTable
      :cols="peerSpec.cols"
      :rows="peerSpec.rows"
      caption="The owner column is unusually simple here and that is the design: four of the six fields are the program's, and the address IS the routing. There is no descriptor to build, no destination register to load and no unit-id table to keep — a peer store compiles to one instruction"
    />

    <Callout kind="rule" title="Reads of a peer window do not exist">
      <p>
        The model is push-only, and a load here faults. A consumer reads its
        <i>own</i> scratchpad with an ordinary load: one cycle, no fabric
        traffic at all. For bulk data, go through DRAM rather than pushing word
        by word; a peer push is one word per store.
      </p>
    </Callout>

    <h3 class="doc-h3">Local control</h3>

    <p class="doc-p">
      Word-addressed from <code>0x2000_0000</code>. Only
      <code>addr[7:2]</code> decodes, so the region aliases every 256 bytes;
      name the words by their symbol. Counters that would be CSRs elsewhere live
      here — there is no CSR file, no <code>Zicsr</code>, no interrupts and no
      trap vectors anywhere in this core.
    </p>

    <SpecTable :cols="ctl.cols" :rows="ctl.rows" />

    <h2 class="doc-h2">Ordering, and the idioms that rest on it</h2>

    <p class="doc-p">
      Four rules, and every communication idiom in this machine reduces to them.
      Loads and stores inside one core are always self-consistent — the
      scratchpad bypass, the cache and the forwarding network exist so that a
      program reading its own writes never observes anything but program order.
    </p>

    <SpecTable :cols="ordering.cols" :rows="ordering.rows" />

    <Callout kind="rule" title="The corollaries a program must respect">
      <p>
        The doorbell is the <b>last</b> store. The flag must be a
        <b>different word</b> from the payload it announces, or a poll cannot
        tell a half-written entry from a finished one. A ring beats a single
        slot: advance a producer index last, never rewrite an entry a consumer
        may already have passed, and no handshake back is needed. Ordering holds
        <b>per destination</b> — pushes to two different PEs have no order
        between them.
      </p>
    </Callout>

    <StepPlayer :steps="handoff" label="DRAM hand-off between units">
      <template #default="{ state }">
        <div
          class="font-mono kt-text-caption whitespace-pre text-warm-700 dark:text-warm-300 leading-6 overflow-x-auto"
        >
          {{ state.code.join("\n") }}
        </div>
        <div class="mt-3 flex items-center gap-2">
          <span
            class="kt-text-micro uppercase tracking-wider text-warm-400 dark:text-warm-600"
            >the data is in</span
          >
          <span class="chip">{{ state.where }}</span>
        </div>
      </template>
    </StepPlayer>

    <Callout kind="note" title="What is deliberately absent from the memory model">
      <p>
        No coherence, by construction rather than omission. No atomics:
        exclusive access between units is done by ownership and push, not by
        locks. No ordering between a peer push and a DRAM write except through
        rule 3 — which is exactly what the hand-off above exercises.
      </p>
    </Callout>

    <h2 class="doc-h2">Halting</h2>

    <p class="doc-p">
      A halt is a redirect that also stops fetch. The halting instruction
      retires — it is the one that raised the halt — but does not commit
      architectural state beyond that.
      <code>a0</code> in the halt word is the committed value: a halt redirects,
      so nothing younger than the halting instruction commits, and everything
      older already has. Software's convention for what
      <code>a0</code> carries — a result, an error code — is its own.
    </p>

    <SpecTable
      :cols="halts.cols"
      :rows="halts.rows"
      caption="Every fault this core has is raised in the EX stage and takes one path, including the ones the memory stage's address decoder finds — which is why an unmapped region halts at the offending PC rather than in a stage the redirect path cannot reach. The cause and word are readable at CTL_CAUSE after the halt, and travel in the completion signal"
    />

    <h2 class="doc-h2">The unit protocol, and the PE as a dispatcher</h2>

    <p class="doc-p">
      The RV32 PE is a compute unit first. Its externally visible life is the
      same as every unit on this fabric: windows are written from outside as
      <code>CU_DATA</code>, a kick starts it, and a completion reports the halt
      word. Boot is not a mechanism — it is those ordinary writes plus a kick.
    </p>

    <p class="doc-p">
      The same unit can be on the <i>sending</i> side of that protocol, and two
      facilities make it a controller rather than only a worker. Both are
      contract.
    </p>

    <SpecTable
      :cols="[
        { key: 'f', label: 'Facility' },
        { key: 'w', label: 'What it is' },
      ]"
      :rows="[
        {
          f: '<b>Dispatch</b>',
          w: 'Three stores into the dispatch region write an argument word, a start PC, and then an opcode word that fires a <code>CU_INST</code> at a named destination coordinate. <b>The opcode store is the doorbell</b> and is the only one of the three that can stall; the other two write requestor registers and always accept. The transaction field is a program id locally, and — when the flit is marked remote — the final coordinate in the target mesh, so software owns which it is',
        },
        {
          f: '<b>Completions in</b>',
          w: '<code>CU_SIGNAL</code> flits addressed to this PE land in an <b>8-entry completion queue</b> readable through the control region: a count, a sticky overflow bit, the head\'s code and id, the head\'s argument word, and a store that retires the head. The queue is bounded, so a lost completion is <b>detectable</b> — the overflow bit says so — rather than silently absorbed',
        },
      ]"
      caption="A mesh can therefore execute a dependency graph with the host out of the loop: each PE kicks its successors and waits on their completions"
    />

    <h2 class="doc-h2">What it costs</h2>

    <Callout kind="measured" title="One run, and the condition on every figure below">
      <p>
        <b>Out-of-context synthesis — not placed and not routed</b> — of top
        <code>rv_pe</code> on <code>xcvu13p-fhgb2104-2L-e</code> under Vivado
        2024.2, by <code>scripts/tcl/ooc_rv_pe.tcl</code> with
        <code>-flatten_hierarchy none</code>, in the shipped configuration
        (<code>L1_LINES</code> 128, <code>REGFILE_PRIM</code> distributed,
        <code>BTB_ENTRIES</code> 32, both windows 2048 words) at a
        <b>2.500 ns request</b>.
      </p>
      <p class="font-mono kt-text-caption">
        2,672 LUT · 3,844 FF · 9 BRAM · 4 DSP &nbsp;·&nbsp; 363.5 MHz, slack
        <b>−0.251 ns</b> &nbsp;·&nbsp; 88 control sets
      </p>
      <p>
        <b>The slack is negative: the request was not met.</b> Asked for
        400 MHz, this configuration lands at 363.5. That number is what the tool
        reports the worst path can carry, not a frequency the design has closed
        at — and synthesis slack is optimistic, so a routed figure would be
        worse. <b>Never quote an Fmax from this tree without the request beside
        it.</b>
      </p>
      <p class="font-mono kt-text-caption">
        worst path, same run: u_core/u_ex/m_addr_reg[29]/C →
        u_core/u_id/d_imm_reg[12]/S &nbsp;·&nbsp; 9 logic levels
      </p>
    </Callout>

    <SpecTable
      :cols="units.cols"
      :rows="units.rows"
      caption="Hierarchical LUT-site accounting, the unit a vendor utilisation table uses. The six children and the top level's own 191 sites sum to the whole unit's 2,672 exactly, and the same holds for the FF, BRAM and DSP columns"
    />

    <Callout kind="trap" title="Two accountings, and they do not agree">
      <p>
        That one run emits per-instance numbers twice. One counts
        <b>LUT primitives</b> under a name prefix; the other is the hierarchical
        <b>LUT-site</b> report above, which packs two small LUTs into one site.
        <code>u_ex</code> is <b>589</b> in the first and <b>489</b> in the
        second; the whole unit is 2,910 and 2,672. The two are not
        interchangeable and a difference between them is not a measurement —
        <b>never subtract one from the other.</b>
      </p>
      <p>
        The same warning applies to <code>u_base</code> across runs.
        <b>498 LUT is that shell inside this PE, at this request, in this
        configuration.</b> Figures of 657 and 756 exist for the same shell
        elsewhere, at a different configuration and a different request; a shell
        figure is meaningless without both attached, and the three should not be
        differenced.
      </p>
    </Callout>

    <ResourceBars
      :items="coreSplit.items"
      unit="LUT sites · inside u_core, 1,298 total"
      caption="The predictor fits inside the u_if number because its entries live in LUTRAM depth rather than logic. The 4 DSP are all in u_ex, and they are the multiplier"
    />

    <Callout kind="measured" title="Two of those rows are design outcomes, not accounting">
      <p>
        The <b>scratchpad is 46 LUT</b> rather than the handful a plain array
        would take, because 38 of them are the cross-port bypass that makes a
        doorbell correct — a priced correctness cost, sitting on the critical
        path.
      </p>
      <p>
        And <b>the compute-unit port is about a fifth of the unit</b>:
        <code>u_base</code> is 498 LUT and 840 FF of port logic that every
        compute unit on this fabric carries, processor or not. Everything that
        makes this unit a <i>processor</i> rather than a port is the rest —
        <b>2,174 sites</b>, that total less <code>u_base</code>, an arithmetic
        that holds only because both figures are the same site accounting from
        the same run.
      </p>
      <p>
        The attach does not grow with the datapath behind it, which is the
        property being claimed: on the widest class measured so far the same
        shell is 3.0 % of the unit — a
        <RouterLink to="/mpe" class="doc-link">KohakuMPE</RouterLink>
        measurement, reported there with its own conditions.
      </p>
    </Callout>

    <SpecTable
      :cols="bram.cols"
      :rows="bram.rows"
      caption="Every array that earns a tile fills the tile's natural depth at its aspect, 1K × 36 for a 32-bit port. Width is 88.9 % everywhere — a 36-bit face carrying 32 data bits — which is the primitive, not a choice. The L1's tag array is far too shallow to earn a tile and stays LUTRAM, which is what makes the 128-line capacity nearly free on one tile"
    />

    <h2 class="doc-h2">Timing, in cycles</h2>

    <SpecTable
      :cols="itiming.cols"
      :rows="itiming.rows"
      caption="Read from the PE's own CTL_CYCLE / CTL_INSTRET counters on the full-system simulation driven by tests/pe/tools/rv_run.py: real routers, the real memory agent, RAM behind it"
    />

    <h3 class="doc-h3">Communication</h3>

    <p class="doc-p">
      <b
        >A push-and-doorbell round trip between two running cores is 49
        cycles</b
      >
      — two window pushes, two hops each way, and two poll loops. The number is
      quantised by the poll: a four-instruction poll loop is about nine cycles,
      and a push is observable only when the loop next comes round, so one extra
      iteration costs a whole loop. That quantisation is what the scratchpad's
      cross-port bypass buys — a poll that sampled the array mid-push would read
      undefined data and go round once more.
    </p>

    <p class="doc-p">
      <b>Two concurrent pairs cost exactly what one costs</b> — identical to the
      cycle, on link-disjoint routes — so pairwise communication scales until
      routes share a link. All-to-one aggregation, with workers pushing
      value-then-flag and the leader polling flags only, costs the leader
      <b>380 cycles and 10 instructions</b> for three workers over one: the
      leader reads a value beside a flag it has seen with no handshake back,
      which is the per-destination ordering rule doing the work.
    </p>

    <h3 class="doc-h3">Multi-core scaling</h3>

    <SpecTable
      :cols="scaling.cols"
      :rows="scaling.rows"
      caption="One memory agent serves up to four PEs. The +13.7 % is measured while the three neighbours run the heaviest memory work in the suite"
    />

    <Callout kind="trap" title="Lay buffers out so they do not conflict-miss">
      <p>
        That worst case is deliberate: a copy whose source and destination sit
        exactly one cache-size apart, so every access conflict-misses —
        <b>26.6 cycles per instruction</b>, the hardest load one PE can put on
        the agent with no wasted instructions. A stride that maps source and
        destination to the same sets is what a 4 KB direct-mapped cache
        punishes.
      </p>
      <p>
        Two DRAM hand-offs running concurrently through one agent cost the
        second pair
        <b>+159 cycles on the write side and +175 on the read side</b> over the
        first — two blocking flush-alls sharing the agent's write slots.
      </p>
    </Callout>

    <h2 class="doc-h2">Configuring one</h2>

    <p class="doc-p">
      Every knob is a parameter of one design — no forked files.
      <b>The defaults below are the shipped configuration</b>, and they are what
      the numbers on this page characterise; a changed setting is verified as
      itself, never assumed from the default build.
    </p>

    <SpecTable :cols="params.cols" :rows="params.rows" />

    <SpecTable
      :cols="gates.cols"
      :rows="gates.rows"
      caption="python tests/pe/tools/rv_run.py --gate N. Every run is bounded — per-case cycle ceilings, a watchdog, spin caps — so a hanging core fails rather than hanging the bench. Configuration knobs reach the benches and synthesis from the same definition, so what levels 1–4 verified is what the synthesis measured"
    />

    <h3 class="doc-h3">A procedure</h3>
    <ol class="doc-p list-decimal pl-5 space-y-1">
      <li>
        <b>Count the units before sizing any one of them.</b> This core exists
        to be instantiated dozens of times, so its cost is
        <span class="chip">per-unit LUT × units</span> and the interesting
        question is always the product. A knob that adds 10 % to one PE adds
        10 % to all of them.
      </li>
      <li>
        <b>Size the scratchpad against the working set and the peer traffic
        together</b>, because it is both — ordinary memory to its own core and a
        writable mailbox to everything else on the mesh. A ring that peers push
        into has to fit beside the data the program is working on.
      </li>
      <li>
        <b>Lay the buffers out so they do not conflict-miss.</b> The caches are
        direct-mapped; two hot arrays that share an index line thrash against
        each other and the effect is a multiple, not a percentage.
      </li>
      <li>
        <b>Link with the map that matches the wrapper.</b>
        <code>.rodata</code> is read with loads, so it belongs where loads
        reach.
      </li>
      <li>
        <b>Run the gate levels, then synthesise.</b> Configuration knobs reach
        the benches and synthesis from the same definition, so what the gates
        verified is what the synthesis measured — but only if you ran the gates
        at the configuration you are about to build.
      </li>
      <li>
        <b>Verify a changed setting as itself.</b> The numbers on this page
        characterise the defaults, and nothing about a knob's cost is safe to
        interpolate.
      </li>
    </ol>

    <Callout kind="open" title="Open questions the flow does not answer">
      <p>
        <b>Nothing checks a peer push against the map it is aimed at.</b> The
        destination coordinate is four bits of an address, so a store to a
        coordinate that holds no PE — or holds one with a smaller scratchpad —
        is a legal instruction. The receiving window bounds-checks the granule
        index and drops what does not fit; nothing reports the drop to the
        sender, because a push is fire-and-forget by construction.
      </p>
      <p>
        <b>Nothing derives the link script from the parameters.</b> The window
        sizes and the linker's view of them are kept in step by hand, and the
        failure mode is an image truncated at a bounds check rather than an
        error.
      </p>
      <p>
        <b>And there is no per-unit cost model for the mesh.</b> Deciding
        between many small PEs and few large ones needs the cost of
        <i>attaching</i> one separated from the cost of computing, and that
        subtraction is a measurement rather than something the flow predicts.
      </p>
    </Callout>

    <h2 class="doc-h2">What this core deliberately does not do</h2>

    <p class="doc-p">
      Stated once, so a reader is not left inferring absence from silence. Each
      of these is a decision with a cost attached rather than an unfinished
      edge, and the reasoning for the first two is on the
      <RouterLink to="/component/rv32pe/microarchitecture" class="doc-link"
        >microarchitecture</RouterLink
      >
      page.
    </p>

    <SpecTable :cols="notDone.cols" :rows="notDone.rows" />

    <Callout kind="note" title="What this PE does not own">
      <p>
        The flit, the link, the router and the port handshake belong to the
        <RouterLink to="/framework/noc" class="doc-link">mesh</RouterLink>;
        descriptor encoding, write slots and response tagging to the
        <RouterLink to="/framework/sysnode" class="doc-link">system node</RouterLink>;
        where the PE lands on the die and at what clock to the
        <RouterLink to="/framework/physical" class="doc-link">floorplan</RouterLink>.
        The meaning of an address past its own DRAM window is not this unit's to
        state.
      </p>
      <p>
        <b><code>SIMD_EN</code> is a slot.</b> A wide datapath attaches behind
        the same six pipeline boundaries; what either wide class costs, how many
        lanes it carries and what format it computes in are
        <RouterLink to="/mpe" class="doc-link">KohakuMPE's</RouterLink> numbers,
        not this framework's. Unfilled, this core is exactly what the figures
        above measure.
      </p>
    </Callout>
  </DocPage>
</template>
