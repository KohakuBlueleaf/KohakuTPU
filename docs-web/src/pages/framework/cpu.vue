<script setup>
/* Resource and frequency figures: out-of-context synthesis on
 * xcvu13p-fhgb2104-2L-e, Vivado 2024.2, synth only. Cycle figures: the PE's own
 * CTL_CYCLE / CTL_INSTRET counters on the full system — real routers, the real
 * memory agent, RAM behind it. Source: docs/arch/pe/. */

const assembly = {
  nodes: [
    { id: "fab", x: 0, y: 0, w: 12, h: 3.2, label: "the mesh", sub: "a router coordinate, a window in the map" },
    {
      id: "base",
      x: 0,
      y: 5,
      w: 12,
      h: 4,
      label: "u_base — noc_cu_base",
      sub: "flit port · instruction FIFO · receive FIFO · CU_CTRL",
      accent: true,
    },
    { id: "req", x: 0, y: 11, w: 12, h: 3.6, label: "u_req", sub: "the NoC requestor · WR_MAX = 1" },
    { id: "l1", x: 0, y: 16.5, w: 12, h: 3.6, label: "u_l1", sub: "128 lines over DRAM · 1 BRAM" },
    { id: "glue", x: 16, y: 5, w: 13, h: 4, label: "window writer + kick FSM", sub: "183 LUT · CU_DATA in, CU_SIGNAL out" },
    { id: "imem", x: 16, y: 11, w: 6, h: 3.6, label: "u_imem", sub: "2048 × 32 · 2 BRAM" },
    { id: "spad", x: 23, y: 11, w: 6, h: 3.6, label: "u_spad", sub: "2048 × 32 · 2 BRAM" },
    { id: "core", x: 16, y: 16.5, w: 13, h: 3.6, label: "u_core", sub: "the RV32I pipeline · 1,187 LUT", accent: true },
  ],
  edges: [
    { from: "fab:b", to: "base:t", dir: "v", accent: true },
    { from: "base:r", to: "glue:l", dir: "h", label: "CU_DATA" },
    { from: "glue:b", to: "imem:t", dir: "v", label: "buf_id 1" },
    { from: "glue:b", to: "spad:t", dir: "v", label: "buf_id 0" },
    { from: "imem:b", to: "core:t", dir: "v", label: "fetch" },
    { from: "spad:b", to: "core:t", dir: "v", label: "lw / sw" },
    { from: "core:l", to: "l1:r", dir: "h", label: "0x8xxx_xxxx" },
    { from: "l1:t", to: "req:b", dir: "v", label: "32-byte line" },
    { from: "req:t", to: "base:b", dir: "v", label: "one write outstanding", accent: true },
  ],
  groups: [{ x: -1.2, y: 4.4, w: 31.4, h: 17, label: "rv_pe" }],
}

const bufIds = {
  cols: [
    { key: "id", label: "buf_id", align: "center", mono: true },
    { key: "t", label: "Target" },
    { key: "g", label: "Granularity" },
  ],
  rows: [
    { id: "0", t: "scratchpad window", g: "raw 32-byte granules" },
    { id: "1", t: "instruction window", g: "raw 32-byte granules" },
    { id: "3", t: "<b>reserved to the framework</b>", g: "rejected by this unit", _tone: "bad" },
    { id: "4", t: "scratchpad window", g: "one 32-bit word, byte enabled" },
    { id: "5", t: "instruction window", g: "one 32-bit word" },
    { id: "anything else", t: "—", g: "rejected, counted out of the burst, and reported", _tone: "bad" },
  ],
}

const pipe = {
  nodes: [
    { id: "if1", x: 0, y: 0, w: 9, h: 4, label: "IF1", sub: "next-PC select" },
    { id: "if2", x: 10.5, y: 0, w: 9, h: 4, label: "IF2", sub: "instruction out, decode" },
    { id: "id", x: 21, y: 0, w: 9, h: 4, label: "ID", sub: "operands out, forwarding" },
    { id: "ex", x: 31.5, y: 0, w: 9, h: 4, label: "EX", sub: "ALU, branch resolve, address", accent: true },
    { id: "mem", x: 42, y: 0, w: 9, h: 4, label: "MEM", sub: "array address, write enables" },
    { id: "wb", x: 52.5, y: 0, w: 9, h: 4, label: "WB", sub: "array data out, commit" },
    { id: "wa", x: 0, y: 7.5, w: 9, h: 3.6, label: "window addr reg", sub: "synchronous array" },
    { id: "ra", x: 10.5, y: 7.5, w: 9, h: 3.6, label: "regfile addr reg", sub: "synchronous array" },
    { id: "ea", x: 31.5, y: 7.5, w: 9, h: 3.6, label: "effective address", sub: "the ALU's own adder" },
  ],
  edges: [
    { from: "if1:r", to: "if2:l", dir: "h" },
    { from: "if2:r", to: "id:l", dir: "h" },
    { from: "id:r", to: "ex:l", dir: "h" },
    { from: "ex:r", to: "mem:l", dir: "h" },
    { from: "mem:r", to: "wb:l", dir: "h" },
    { from: "if1:b", to: "wa:t", dir: "v", dash: true },
    { from: "if2:b", to: "ra:t", dir: "v", dash: true },
    { from: "ex:b", to: "ea:t", dir: "v", dash: true },
  ],
}

/* --- hazards ------------------------------------------------------------- */

const hazards = {
  cols: [
    { key: "src", label: "Producer is in" },
    { key: "d", label: "Distance", align: "center", mono: true },
    { key: "what", label: "What happens" },
  ],
  rows: [
    { src: "EX", d: "1", what: "forward the ALU output, or stall — see <code>FWD_X</code>" },
    { src: "MEM", d: "2", what: "forward the EX result register; <b>stall if it is a load</b>" },
    { src: "WB", d: "3", what: "forward the writeback value, loads included" },
    { src: "—", d: "4", what: "the register file's own write-through bypass" },
  ],
}

const d4rows = (sees) => [
  { name: "IF2", kind: "bus", values: ["i0", "i1", "i2", "i3", "i4", "i5"] },
  { name: "ID", kind: "bus", values: [null, "i0", "i1", "i2", "i3", "i4"] },
  { name: "EX", kind: "bus", values: [null, null, "i0", "i1", "i2", "i3"] },
  { name: "MEM", kind: "bus", values: [null, null, null, "i0", "i1", "i2"] },
  { name: "WB", kind: "bus", values: [null, null, null, null, "i0", "i1"], mark: [4] },
  { name: "x5 write enable", kind: "bit", values: [0, 0, 0, 0, 1, 0], mark: [4] },
  { name: "i4 rs1 addr captured", kind: "bit", values: [0, 0, 0, 0, 1, 0], mark: [4] },
  { name: "i4 reads", kind: "text", values: ["", "", "", "", "", sees] },
]

const loadUse = [
  { name: "IF2", kind: "bus", values: ["lw x5", "add x6", "sub x7", "sub x7", "sub x7"] },
  { name: "ID", kind: "bus", values: [null, "lw x5", "add x6", "add x6", "add x6"] },
  { name: "EX", kind: "bus", values: [null, null, "lw x5", "—", "—"] },
  { name: "MEM", kind: "bus", values: [null, null, null, "lw x5", "—"] },
  { name: "WB — x5 exists", kind: "bus", values: [null, null, null, null, "lw x5"], mark: [4] },
  { name: "stall", kind: "bit", values: [0, 0, 1, 1, 0], mark: [2, 3] },
]

const predTaken = [
  { name: "fetch addr", kind: "bus", values: ["0x40", "0x44", "0x48", "0x20", "0x24"], mark: [3] },
  { name: "BTB, same addr", kind: "text", values: ["—", "—", "HIT → 0x20", "", ""] },
  { name: "bubble", kind: "bit", values: [0, 0, 0, 0, 0] },
]

const mispredict = [
  { name: "fetch addr", kind: "bus", values: ["0x48", "0x4C", "0x50", "0x54", "0x20"], mark: [4] },
  { name: "the branch is in", kind: "text", values: ["IF2", "ID", "EX — resolves", "", ""] },
  { name: "redirect registered", kind: "bit", values: [0, 0, 0, 1, 0], mark: [3] },
  { name: "discarded", kind: "text", values: ["", "0x4C", "0x50", "0x54", ""] },
]

/* --- the two L1s --------------------------------------------------------- */

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
    { k: "<b>who writes it</b>", ext: "the NoC, and this core", int: "this core only, plus fills" },
    { k: "<b>tags</b>", ext: "none — the address-region decode <b>is</b> the lookup", int: "yes, direct mapped" },
    {
      k: "<b>holds</b>",
      ext: "program text (<code>rv_imem</code>) and data (<code>rv_spad</code>)",
      int: "copies of DRAM lines",
    },
    {
      k: "<b>coherence case</b>",
      ext: "none: it is the <b>home</b> of its addresses, never a copy",
      int: "none: <b>never externally written</b>",
      _tone: "good",
    },
  ],
}

const doorbellBroken = [
  { name: "NoC port (A)", kind: "text", values: ["idle", "wr spad[W] ← 0x01", "idle", "idle"] },
  { name: "core read addr (B)", kind: "bus", values: ["W", "W", null, null], mark: [1] },
  { name: "array out (B)", kind: "bus", values: ["0x00", "0x00", "X", "0x01"], mark: [2] },
  { name: "the poll sees", kind: "text", values: ["not rung", "not rung", "UNDEFINED", "rung"] },
]

const doorbellFixed = [
  { name: "NoC port (A)", kind: "text", values: ["idle", "wr spad[W] ← 0x01", "idle", "idle"] },
  { name: "core read addr (B)", kind: "bus", values: ["W", "W", null, null], mark: [1] },
  { name: "byte-wise bypass", kind: "bit", values: [0, 1, 0, 0], mark: [1] },
  { name: "array out (B)", kind: "bus", values: ["0x00", "0x00", "0x01", "0x01"], mark: [2] },
  { name: "the poll sees", kind: "text", values: ["not rung", "not rung", "rung", "rung"] },
]

/* --- the map ------------------------------------------------------------- */

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
      sem: "read/write. Writable by this core and by the NoC; a NoC write landing in a word being read returns the <b>new</b> bytes",
      cost: "1 cycle, always",
    },
    { addr: "0x2xxx_xxxx", region: "local control", sem: "word registers; some reads, some stores with side effects", cost: "1 cycle" },
    {
      addr: "0x3xxx_xxxx",
      region: "peer windows",
      sem: "<b>store only</b> — a store becomes a push to another unit's window; a load faults",
      cost: "1 cycle + hold",
    },
    { addr: "0x8xxx_xxxx ↑", region: "global DRAM", sem: "cached read/write through the internal L1", cost: "hit 1 cycle, miss a fill round trip" },
    { addr: "anything else", region: "—", sem: "faults", cost: "—", _tone: "bad" },
  ],
}

const peerAddr = [
  { name: "0x3", bits: 4, value: "the region", accent: true },
  { name: "dest x", bits: 4 },
  { name: "dest y", bits: 4 },
  { name: "window", bits: 1, value: "0 spad / 1 imem" },
  { name: "granule index", bits: 14, value: "32-byte granules" },
  { name: "word", bits: 3, value: "within the granule" },
  { name: "byte", bits: 2, value: "the store's byte enables" },
]

const ctl = {
  cols: [
    { key: "off", label: "Offset", mono: true },
    { key: "name", label: "Name", mono: true },
    { key: "acc", label: "Access" },
    { key: "mean", label: "Meaning" },
  ],
  rows: [
    { off: "0x00", name: "CTL_STATUS", acc: "read", mean: "bit 0 <code>flush_busy</code>, bit 1 writes outstanding" },
    { off: "0x04", name: "CTL_FLUSH", acc: "<b>store</b>", mean: "write back every dirty line — <b>blocking</b>" },
    { off: "0x08", name: "CTL_INVAL", acc: "<b>store</b>", mean: "drop every line, dirty included — blocking, one cycle per line" },
    { off: "0x0C", name: "CTL_CAUSE", acc: "read", mean: "halt cause of the last halt" },
    { off: "0x10", name: "CTL_COREID", acc: "read", mean: "<code>{y, x}</code>, this PE's mesh coordinate" },
    { off: "0x14", name: "CTL_ARG", acc: "read", mean: "the word the kick carried" },
    { off: "0x18", name: "CTL_CYCLE", acc: "read", mean: "cycles since the kick" },
    { off: "0x1C", name: "CTL_INSTRET", acc: "read", mean: "instructions retired since the kick" },
    { off: "0x20", name: "CTL_WROUT", acc: "read", mean: "writes not yet acknowledged" },
  ],
}

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
}

const handoff = [
  {
    title: "writer: ... stores ...",
    code: ["sw   t0, 0(a1)", "sw   t1, 4(a1)", "..."],
    where: "the writer's internal L1 — dirty",
    note: "Nothing outside this core can see any of it yet. The lines are dirty in a write-back cache.",
  },
  {
    title: "writer: sw x0, 0(CTL_FLUSH)",
    code: ["sw   x0, 0(CTL_FLUSH)   -- blocking"],
    where: "DRAM, acknowledged",
    note: "A store to CTL_FLUSH completes only after every dirty line is written back and acknowledged, so the instruction after it cannot overtake the data. ~12 cycles per dirty line against a prompt agent.",
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
    where: "an ordinary load — 1 cycle, zero NoC traffic",
    note: "A push landing in the very word being polled returns the pushed bytes. That is what the scratchpad's cross-port bypass buys.",
  },
  {
    title: "reader: sw x0, 0(CTL_INVAL)",
    code: ["sw   x0, 0(CTL_INVAL)   -- blocking, 1 cycle per line"],
    where: "the reader's L1 — empty",
    note: "A sweep, one line per cycle, nothing on the wire. Without it the reader may answer from a line it filled before the writer ran.",
  },
  {
    title: "reader: ... loads ...",
    code: ["lw   a0, 0(a2)", "..."],
    where: "filled from DRAM — the writer's data",
    note: "Both controls exist for this sequence. Peer windows carry control; data of any size goes through DRAM.",
  },
]

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
    { cause: "3", by: "illegal encoding, misaligned access, or an unmapped region", word: "the offending PC", code: "0x04" },
  ],
}

/* --- performance --------------------------------------------------------- */

const units = {
  cols: [
    { key: "u", label: "Unit" },
    { key: "lut", label: "Total LUT", align: "right", mono: true },
    { key: "logic", label: "Logic LUT", align: "right", mono: true },
    { key: "lram", label: "LUTRAM", align: "right", mono: true },
    { key: "ff", label: "FF", align: "right", mono: true },
    { key: "bram", label: "BRAM", align: "right", mono: true },
  ],
  rows: [
    { u: "<b>whole PE</b>", lut: "<b>2,583</b>", logic: "2,215", lram: "368", ff: "<b>4,140</b>", bram: "<b>5</b>" },
    { u: "top glue: window writer, kick FSM", lut: "183", logic: "183", lram: "0", ff: "413", bram: "0" },
    { u: "<code>u_base</code> — the framework attach", lut: "657", logic: "409", lram: "248", ff: "1,381", bram: "0" },
    { u: "<code>u_core</code> — the RV32I pipeline", lut: "1,187", logic: "1,115", lram: "72", ff: "1,013", bram: "0" },
    { u: "<code>u_l1</code> — internal L1, 128 lines", lut: "364", logic: "316", lram: "48", ff: "413", bram: "1" },
    { u: "<code>u_req</code> — NoC requestor", lut: "147", logic: "147", lram: "0", ff: "883", bram: "0" },
    { u: "<code>u_imem</code> — instruction window", lut: "0", logic: "0", lram: "0", ff: "0", bram: "2" },
    { u: "<code>u_spad</code> — scratchpad", lut: "45", logic: "45", lram: "0", ff: "37", bram: "2" },
  ],
}

const coreSplit = {
  items: [
    { label: "EX — one adder, one subtractor, one shifter. No multiplier", value: 418 },
    { label: "ID — operands, forwarding, branch target", value: 234 },
    { label: "MEM", value: 181 },
    { label: "IF, with the whole predictor", value: 135, note: "entries live in LUTRAM depth" },
    { label: "register file — LUTRAM", value: 129 },
    { label: "hazard / run / counters", value: 70 },
    { label: "WB", value: 20 },
  ],
}

const freq = {
  cols: [
    { key: "ask", label: "Constraint asked", mono: true },
    { key: "fmax", label: "Fmax", align: "right", mono: true },
    { key: "lut", label: "LUT", align: "right", mono: true },
    { key: "verdict", label: "" },
  ],
  rows: [
    { ask: "3.333 ns", fmax: "<b>410.8 MHz</b>", lut: "<b>2,491</b>", verdict: "<b>constrain it here</b> — 0.899 ns to spare", _tone: "good" },
    { ask: "2.5 ns", fmax: "410.8 MHz", lut: "2,583", verdict: "~90 extra sites for zero megahertz", _tone: "warn" },
    { ask: "2.2 – 2.0 ns", fmax: "410.8 MHz", lut: "+350 – 400", verdict: "13–15 % of the unit for zero megahertz", _tone: "bad" },
  ],
}

const bram = {
  cols: [
    { key: "arr", label: "Array" },
    { key: "shape", label: "Words × width", mono: true },
    { key: "tiles", label: "Tiles", align: "right", mono: true },
    { key: "depth", label: "Depth used", align: "right", mono: true },
  ],
  rows: [
    { arr: "instruction window", shape: "2048 × 32", tiles: "2", depth: "<b>100 %</b>" },
    { arr: "scratchpad", shape: "2048 × 32", tiles: "2", depth: "<b>100 %</b>" },
    { arr: "internal L1 data", shape: "1024 × 32", tiles: "1", depth: "<b>100 %</b>" },
  ],
}

const itiming = {
  cols: [
    { key: "ev", label: "Event" },
    { key: "cost", label: "Cost" },
  ],
  rows: [
    { ev: "most instructions", cost: "1 cycle" },
    { ev: "load-use, back to back", cost: "2 stall cycles" },
    { ev: "load-use at a spacing of one", cost: "1 stall cycle" },
    { ev: "taken branch, predicted", cost: "0", _tone: "good" },
    { ev: "mispredict or unpredicted taken branch", cost: "3 cycles" },
    { ev: "peer-window push", cost: "1 cycle + hold until the requestor accepts" },
    { ev: "<code>ECALL</code> / <code>EBREAK</code>", cost: "halts; the completion carries the word" },
    { ev: "scratchpad or control access", cost: "1 cycle, always" },
    { ev: "DRAM hit / miss", cost: "1 cycle / a fill round trip — hundreds of cycles, dominated by the agent and DRAM" },
    { ev: "steady-state evict-and-refill pair", cost: "~30 cycles against the real agent" },
    { ev: "flush-all, prompt acknowledgements", cost: "~12 cycles per dirty line — 197 for 16" },
    { ev: "flush-all, slow agent", cost: "each line pays the acknowledgement latency — 677 for the same 16", _tone: "warn" },
    { ev: "invalidate-all", cost: "one cycle per line, pipeline held, nothing on the wire" },
  ],
}

const scaling = {
  cols: [
    { key: "what", label: "The same program, kick to halt" },
    { key: "one", label: "1 PE", align: "right", mono: true },
    { key: "two", label: "2 PEs", align: "right", mono: true },
    { key: "four", label: "4 PEs", align: "right", mono: true },
  ],
  rows: [{ what: "cycles — identical instruction stream at every count", one: "7,418", two: "7,778 <span class='opacity-60'>(+4.9 %)</span>", four: "8,431 <span class='opacity-60'>(+13.7 %)</span>" }],
}

const params = {
  cols: [
    { key: "p", label: "Parameter", mono: true },
    { key: "d", label: "Default", align: "center", mono: true },
    { key: "w", label: "What it decides" },
  ],
  rows: [
    { p: "BTB_ENTRIES", d: "32", w: "predictor size; 0 removes the predictor entirely (a generate)" },
    { p: "FWD_X", d: "1", w: "the distance-1 bypass. 0 is measured worse on every axis and remains only as the proof" },
    { p: "L1_LINES", d: "128", w: "internal L1 lines. 128 fills the BRAM's natural depth; halving saves almost nothing" },
    { p: "REGFILE_PRIM", d: "\"distributed\"", w: "LUTRAM vs block-RAM register file. Interchangeable timing; a resource trade" },
    { p: "IMEM_WORDS / SPAD_WORDS", d: "2048", w: "the two windows, sized to fill their BRAM tiles exactly" },
    { p: "WR_MAX", d: "1", w: "un-acknowledged writes. 1 is what the communication model assumes; raising it buys nothing a blocking cache can use" },
  ],
}

/* --- the arithmetic EX does not have ------------------------------------- */
/* The cost rows below are ESTIMATE — reasoned from measured neighbours on
 * xcvu13p-fhgb2104-2L-e, not synthesised. Nothing in them has been built.
 * Source: docs/arch/pe/microarchitecture.md, docs/arch/pe/simd/performance.md. */

const absent = {
  cols: [
    { key: "f", label: "Feature" },
    { key: "s", label: "In this core" },
    { key: "w", label: "Where the machine's version of it actually lives" },
  ],
  rows: [
    {
      f: "<b><code>mul</code></b>, <code>mulh</code>, <code>mulhsu</code>, <code>mulhu</code>",
      s: "<b>absent; the encodings fault</b>",
      w: "the SIMD PE's <code>vmul</code> / <code>vdot</code>, in the <i>vector</i> register file behind custom-0 — and, since the SIMT PE was built, <b>RV32M itself on the GPU's per-thread register file</b>, one product per lane, on the standard <code>OP</code> encoding",
      _tone: "bad",
    },
    {
      f: "<b><code>div</code></b>, <code>rem</code>",
      s: "<b>absent; the encodings fault</b>",
      w: "<b>nowhere.</b> No class in this machine divides — the SIMT PE decodes <code>funct3</code> 100–111 under the same <code>funct7</code> and refuses it by name",
      _tone: "bad",
    },
    {
      f: "<b>floating point</b> — <code>F</code>, <code>D</code>, <code>Zfh</code>",
      s: "<b>absent.</b> No <code>f0..f31</code>, no <code>fcsr</code>, no rounding mode — there is no CSR file to hold one",
      w: "both wide classes' <b>E8M15 float lanes</b>, one fused multiply-add per element: a tier of <b>4 lanes on the SIMD PE</b> and <b>8 on the SIMT PE</b>, both measured. There is one dtype configuration in this machine — FP32 or FP16 operands in, E8M15 compute, FP32 or FP16 out — so the two classes differ in width, not in format. None of it is reachable from a scalar register",
      _tone: "bad",
    },
    {
      f: "atomics, <code>A</code>",
      s: "absent",
      w: "ownership and push, not locks — the communication model above",
    },
    {
      f: "CSRs, <code>Zicsr</code>, interrupts, traps",
      s: "absent",
      w: "memory-mapped counters in the local control region; the unit halts, it is never interrupted",
    },
  ],
}

const mulCost = {
  cols: [
    { key: "s", label: "Shape" },
    { key: "d", label: "DSP", align: "right", mono: true },
    { key: "l", label: "LUT", align: "right", mono: true },
    { key: "i", label: "Issue", mono: true },
    { key: "t", label: "Latency", align: "right", mono: true },
  ],
  rows: [
    { s: "<code>mul</code> alone, pipelined", d: "3", l: "~20 + registers", i: "1", t: "3–4" },
    {
      s: "<b>the whole family</b> — <code>mul</code>, <code>mulh</code>, <code>mulhsu</code>, <code>mulhu</code>",
      d: "<b>4</b>",
      l: "<b>~200</b>",
      i: "1",
      t: "3–4",
      _tone: "good",
    },
    { s: "one DSP, multi-cycle over four passes", d: "1", l: "~150", i: "1 per 4–5 cycles", t: "4–5", _tone: "warn" },
    {
      s: "<code>div</code> / <code>rem</code>, iterative — <b>its own 33-bit subtractor</b>",
      d: "0",
      l: "<b>200–300</b>",
      i: "1 per ~35 cycles",
      t: "~35",
      _tone: "bad",
    },
    {
      s: "scalar float, reusing the measured E8M15 lane",
      d: "2",
      l: "<b>900–1,100</b>",
      i: "1 per 15 cycles",
      t: "15",
      _tone: "bad",
    },
  ],
}

/* Where the machine's multiplier actually went, and why not here.
 * Source: docs/arch/pe/microarchitecture.md, src/kohakumpe/simt/. */
const mulWhere = {
  cols: [
    { key: "c", label: "The cost, on this core" },
    { key: "g", label: "The same thing on the SIMT PE" },
  ],
  rows: [
    {
      c: "<b>The result mux lengthens the distance-1 forwarding path.</b> <code>ex_alu</code> feeds <code>fwd_x_val</code>, which is an input to <code>x_op1_reg</code> — the register the critical path already ends at",
      g: "<b>That path does not exist.</b> Barrel scheduling deleted it: with as many resident waves as the pipeline is deep, no two in-flight instructions share a wave, so the design carries no forwarding network and no interlock — there is nothing there to lengthen",
      _tone: "good",
    },
    {
      c: "<b>A multi-cycle result needs a new stall term</b>, and stall terms fan out — <code>FWD_X</code> priced widening one at <b>5 MHz</b>",
      g: "<b>The flag was already there.</b> A wave with a float in flight is skipped by the scheduler via one pending bit per wave; a multiply sets and clears the <i>same</i> bit and retires through the <i>same</i> slot",
      _tone: "good",
    },
  ],
}

const changeMind = {
  cols: [
    { key: "i", label: "If" },
    { key: "t", label: "Then" },
  ],
  rows: [
    {
      i: "a controller kernel profile shows &gt;5 % of cycles in <code>__divsi3</code> — not <code>__mulsi3</code>",
      t: "<code>div</code>/<code>rem</code> earns its ~250 LUT. Measure before building",
    },
    {
      i: "the EX result mux measures worse than −15 MHz with the multiply input",
      t: "keep <code>mul</code> but retire it through MEM on its own writeback port, not through <code>ex_alu</code> — one more stall, the distance-1 forward untouched",
    },
    {
      i: "<b>this</b> core gains a way to park an instruction — a pending flag, a scoreboard, anything",
      t: "every multi-cycle unit reprices at once, <code>mul</code> first. This is what happened on the SIMT PE, and it is why the machine's multiply is there and not here",
      _tone: "good",
    },
    {
      i: "something needs a scalar <b>transcendental</b> (rendering will)",
      t: "the answer is still not scalar float. Untie <code>op</code> in the float lane, which already carries the seeds in its source: <b>ESTIMATE</b> +640 LUT and +1 DSP <b>per lane</b>, or +420 LUT, +1 DSP and +1 BRAM with the coefficient ROMs in block RAM — for <code>exp2</code>, <code>log2</code>, <code>inv</code> and <code>rsqrt</code> at II = 1. The lane count is a build parameter, so that cost scales with it",
    },
  ],
}

const gates = {
  cols: [
    { key: "g", label: "Level", mono: true },
    { key: "w", label: "What it proves" },
  ],
  rows: [
    { g: "--gate 1", w: "the core against a Python RV32I golden model, comparing PC, destination and value for <b>every committed instruction</b> — and it runs the <i>configured</i> shape" },
    { g: "--gate 2", w: "the memory frontend against the protocol" },
    { g: "--gate 3", w: "real software on the real memory substrate: boots through the window-write path, checks halt word, completion code and a DRAM checksum" },
    { g: "--gate 4", w: "one, two and four PEs sharing a NoC and a memory agent — where the communication protocol is proven between running cores rather than against a bench" },
  ],
}
</script>

<template>
  <DocPage
    title="The CPU PE"
    summary="A small RV32I processor whose memory interface was designed around the framework from the start. Six register boundaries, one stall rule, two L1s split by who writes them, the 38 LUT that make a doorbell correct — and no multiplier, no divider and no float, with each option costed."
    domain="cpu"
    status="shipped"
    source="src/kohakuaccel/pe/rv32/ · docs/arch/pe/"
  >
    <p class="doc-p">
      Every accelerator on this framework needs something that decides <i>what to do next</i> —
      sequencing kernels, walking descriptors, reacting to completions. Hand-written state machines
      do it until the day the policy changes, and then they do it wrong. The design objectives, in
      order: RV32I compatibility so ordinary compilers work; <b>very low LUT</b>, so dozens of PEs
      on one device are realistic; <b>high Fmax</b>, so scalar control is never the slow component;
      and a memory frontend that is part of the core rather than a generic CPU bus with an adapter
      bolted on.
    </p>

    <Callout kind="rule" title="LUT and frequency outrank latency everywhere">
      <p>
        The core spends FF and BRAM freely, prefers a pipeline stage over a bypass, and registers
        anything whose combinational form would fan out. Every shape on this page follows from
        that, and where it does not, the exception is measured.
      </p>
    </Callout>

    <h2 class="doc-h2">The attach</h2>

    <p class="doc-p">
      One local port, one clock, no AXI, no sideband, no second clock domain. A mesh gains a PE the
      way it gains any unit — a router coordinate and a window in the address map. The layout is the
      architecture: <code>core/</code> is the pipeline, <code>mem/</code> the two L1s,
      <code>noc/</code> the fabric attach, and <code>rv_pe.v</code> assembles them and holds nothing
      else.
    </p>

    <Fig
      caption="rv_pe — instantiation and wiring only. Boot is not a mechanism: a program image is an ordinary CU_DATA burst into the instruction window, arguments another into the scratchpad, then the standard kick. LUT figures from the hierarchical run at a 2.5 ns request on xcvu13p-fhgb2104-2L-e."
      zoom
    >
      <BlockDiagram :nodes="assembly.nodes" :edges="assembly.edges" :groups="assembly.groups" />
    </Fig>

    <SpecTable
      :cols="bufIds.cols"
      :rows="bufIds.rows"
      caption="Where a CU_DATA burst lands. A granule descriptor's offset and len are both in granules, and offset + len is range-checked against the named window — rejected, never wrapped. A granule is written as eight 32-bit words, so the unit stops accepting for eight cycles per data flit; that backpressure is bounded by the unit's own progress and never by another inbound flit"
    />

    <Callout kind="rule" title="What a completion asserts">
      <p>
        Completion is <code>CU_SIGNAL</code> to whoever kicked, carrying the halt word. It asserts
        three things at once: the pipeline is empty, the requestor is idle, and <b>every write the
        program issued has been acknowledged by memory</b>. A host that reads DRAM on seeing the
        completion finds the program's results there — the completion is the host's sequencing
        point, and it would mean nothing weaker.
      </p>
      <p>
        A kick never overtakes the data it announces: the unit holds a kick until its receive path
        is quiet, so an image still being written when the kick arrives is finished before fetch
        begins. The hold clears by the unit's own progress and cannot deadlock.
      </p>
    </Callout>

    <h2 class="doc-h2">Five stages, six register boundaries</h2>

    <p class="doc-p">
      The extra boundary exists because the instruction window and the register file are
      synchronous arrays: each costs a cycle between presenting an address and receiving data, and
      counting those honestly is what lets the fetch loop close. The address path in fetch is
      <code>PC → mux → RAM address register</code> and nothing else.
    </p>

    <Fig
      caption="Two consequences are structure, not detail. Decode is combinational on the fetched word, inside IF2 — the register-file address leaves at the same edge as the control bits, buying the operand-fetch cycle instead of costing a seventh boundary. And the effective address leaves EX combinationally, because the data arrays register their address input."
      zoom
      wide
    >
      <BlockDiagram :nodes="pipe.nodes" :edges="pipe.edges" />
    </Fig>

    <Callout kind="note" title="Two more shapes in the same budget">
      <p>
        A branch or jump target is computed <b>in ID rather than EX</b>: PC and immediate are both
        registered by then, the adder is off every critical path in that stage, and carrying the
        target instead of the immediate keeps the EX register the same width.
      </p>
      <p>
        EX carries <b>one adder, one subtractor and one shifter</b>, not three of each.
        <code>SUB</code>, <code>SLT</code>/<code>SLTU</code> and every branch comparison come off the
        one subtractor; <code>SLL</code>, <code>SRL</code> and <code>SRA</code> share a single 33-bit
        arithmetic right shifter between two bit reversals — a left shift is a right shift on the
        reversed word, and reversals are wiring.
      </p>
      <p>
        <b>The adder is not shared with the subtractor, and that is structural.</b>
        <code>sum = op1 + op2</code> is also <code>ex_addr</code>, which leaves the stage
        combinationally to the data arrays' address pins — so anything muxed into its inputs lands
        in front of that path. It is the one adder in this core that cannot be borrowed, which is
        what decides the divider below.
      </p>
    </Callout>

    <h2 class="doc-h2">Hazards: three forwards, one stall rule</h2>

    <SpecTable :cols="hazards.cols" :rows="hazards.rows" />

    <p class="doc-p">
      A load's data does not exist until WB, which is why distances 1 and 2 stall on a load and
      distance 3 does not. That is the load-use penalty: two cycles back to back, one at a spacing
      of one.
    </p>

    <WaveTrace
      :rows="loadUse"
      label="load-use, back to back — two stalls"
      :notes="[
        { cycle: 2, text: 'The consumer is in ID at distance 1 from a load in EX, and a load\'s value does not exist yet. It is held in ID; fetch holds with it.' },
        { cycle: 4, text: 'The load reaches WB, the distance-3 forward hands the value straight into ID, and the stall drops. Two cycles back to back; one at a spacing of one.' },
      ]"
    />

    <h3 class="doc-h3">The distance-4 write-through is not optional</h3>

    <p class="doc-p">
      A write lands at the same edge that captures a read address four instructions behind it, and
      a synchronous array returns the pre-write value for that read.
    </p>

    <WaveTrace
      :rows="d4rows('OLD x5')"
      variant="broken"
      label="no write-through — wrong at exactly one spacing"
      :notes="[
        { cycle: 4, text: 'i0 writes x5 in WB at the same edge that captures i4\'s register-file read address in IF2.', tone: 'bad' },
        { cycle: 5, text: 'The array returns the pre-write value. The forwarding network covers distances 1, 2 and 3 and does not see this one. The kind of bug that survives a casual test suite.', tone: 'bad' },
      ]"
    />

    <WaveTrace
      :rows="d4rows('NEW x5')"
      variant="fixed"
      label="the register file's own write-through bypass"
      :notes="[
        { cycle: 5, text: 'The co-simulation covers every producer-to-consumer distance by construction, which is what turns this from a case someone remembered into a case that cannot be missed.', tone: 'good' },
      ]"
    />

    <Callout kind="measured" title="FWD_X = 1 is the default because the mux was never the expensive part">
      <p>
        Removing the distance-1 bypass looks like it should trade a cycle for frequency; measured,
        it saves about <b>2 LUT and loses 5 MHz</b>, because without the bypass the stall term
        widens from <code>hz1 &amp;&amp; x_load</code> to <code>hz1</code> and that term fans out
        across the whole front end. The 0 form stays built and verified so the claim survives
        re-measurement.
      </p>
    </Callout>

    <h2 class="doc-h2">Branch prediction</h2>

    <p class="doc-p">
      A small BTB plus a 2-bit saturating table, read with the <b>same address as the instruction
      window</b>. Its job is to remove the taken-branch penalty of a loop, not to be accurate:
      nothing in it is speculative state needing repair, because EX resolves every branch against
      the architectural answer. A wrong prediction costs the redirect penalty and never
      correctness — which is why the tag can be short and the table can alias.
    </p>

    <WaveTrace :rows="predTaken" label="predicted taken — no bubble" :notes="[
      { cycle: 2, text: 'Tag, target, valid and counter all ride in one LUTRAM entry, so the entry count buys memory depth rather than logic. The prediction is available in the cycle the instruction\'s bits are.' },
    ]" />

    <WaveTrace :rows="mispredict" label="mispredict — three cycles" :notes="[
      { cycle: 3, text: 'A redirect is registered. Steering fetch in the resolve cycle would put the ALU output into the next-PC mux; one more cycle costs a third bubble and keeps the ALU output going nowhere but a flop.' },
      { text: 'The predictor update lands one cycle after the resolve too — EX\'s comparator driving a read-modify-write is a long path for something non-architectural, and a cycle of staleness can only cost a prediction.' },
    ]" />

    <h2 class="doc-h2">The two L1s</h2>

    <p class="doc-p">
      The I/D split is recast as <b>external L1 and internal L1</b>, split by <i>who writes</i>, not
      by what is stored. This is the single idea that removes coherence from the design.
    </p>

    <SpecTable :cols="l1s.cols" :rows="l1s.rows" />

    <p class="doc-p">
      There is no external-write-versus-dirty-line case anywhere in this PE because there is no way
      to construct one. The instruction window is not reachable from the data side, which keeps the
      fetch port exclusive — fetch never contends with a load.
    </p>

    <h3 class="doc-h3">The write both ports can make at once</h3>

    <p class="doc-p">
      A window written by the NoC and read by its owner has one hard case: the push lands in the
      very word a poll loop is reading — <b>and on a doorbell that is the common case</b>, because
      the peer pushes exactly the word the consumer polls. A true-dual-port array returns undefined
      data for that collision in silicon, and per-port reasoning (“neither port reads what it
      writes”) is true per port and false across them.
    </p>

    <WaveTrace
      :rows="doorbellBroken"
      variant="broken"
      label="true dual port, no bypass"
      :notes="[
        { cycle: 2, text: 'Undefined data, at the exact moment the protocol depends on. A poll that sampled the array mid-push reads garbage and goes round the loop once more — and a four-instruction poll loop is ~9 cycles, so one extra iteration costs a whole loop.', tone: 'bad' },
      ]"
    />

    <WaveTrace
      :rows="doorbellFixed"
      variant="fixed"
      label="byte-wise cross-port bypass — 38 LUT"
      :notes="[
        { cycle: 2, text: 'When the NoC port writes the word the core is reading, the core receives the written bytes — correct rather than merely defined, and byte-wise because a peer\'s sb is as legal as a local one.', tone: 'good' },
        { cycle: 2, text: 'It costs 38 LUT and sits on the critical path, which is the price of the doorbell being right. The scratchpad is 45 LUT rather than the ~7 of a plain array for exactly this reason.' },
      ]"
    />

    <Callout kind="rule" title="The other array answers differently, and says so">
      <p>
        The internal L1's fill collides too — a fill writes the word a stalled access is presenting
        — and answers the opposite way: the colliding read is <b>discarded and re-issued</b> after
        the fill. Which answer is right belongs to the caller, not the array. The RAM wrapper
        (<code>rv_ram_be</code>) makes the choice explicit: <b>an array that does not declare how it
        handles the collision asserts the moment one happens.</b>
      </p>
    </Callout>

    <h3 class="doc-h3">The cache exists even if it never hits</h3>

    <p class="doc-p">
      Two jobs, and the second does not depend on hit rate: <b>protocol adaptation</b>. A 32-byte
      line is exactly one NoC/MAG payload, so a fill is one request and one response and a
      writeback is one descriptor and one beat. Ordinary <code>lb</code>/<code>lh</code>/<code>lw</code>
      and <code>sb</code>/<code>sh</code>/<code>sw</code> are presented to software while the
      upstream protocol stays line-oriented.
    </p>

    <p class="doc-p">
      It is direct mapped and blocking, with <b>one outstanding miss</b>. No MSHRs, no
      hit-under-miss, no load/store queue: latency tolerance in this machine comes from having many
      independent PEs, and whether per-core miss concurrency beats instantiating more cores is a
      later measurement, not an assumption.
    </p>

    <Callout kind="note" title="Why the arrays are 32 bits, and what the rotate costs">
      <p>
        A flit carries 256 bits, so a 256-bit array port looks natural. It is not: a
        <code>RAMB36E2</code> in true-dual-port mode is 36 bits per port, so a 256-bit TDP array is
        eight BRAMs whose 32-bit face is the only one the CPU uses — and every read needs an 8:1
        32-bit mux on the load path. Walking a line as eight 32-bit words costs 8 cycles per fill
        against a DRAM latency of hundreds, and zero LUT.
      </p>
      <p>
        The 256-bit line buffer between array and fabric is a <b>rotate</b>, not an indexed
        register. <b>The limit of the trick is worth stating:</b> a rotate needs a 2:1 mux on every
        bit, so it pays only where the register was already written word-at-a-time and that mux
        already existed — applied to a register loaded whole, the same construction <i>adds</i>
        logic.
      </p>
      <p>
        Per-line <code>valid</code> and <code>dirty</code> ride in the tag LUTRAM beside the tag
        rather than in flop arrays — indexed flop arrays cost LUT twice, once as flops and once as
        the read mux in front of the tag compare. The consequence is that invalidate-all is a
        one-line-per-cycle sweep rather than a broadcast, which is why <code>CTL_INVAL</code>
        blocks.
      </p>
    </Callout>

    <Callout kind="rule" title="Primitives are named, never inferred">
      <p>
        Left to inference, both the resource and the read latency can move between tool versions,
        and read latency here is pipeline structure.
      </p>
    </Callout>

    <h2 class="doc-h2">The NoC requestor</h2>

    <p class="doc-p">
      Everything about the framework memory protocol that RV32 software must never see: transaction
      tags, descriptor legality, response matching, write ordering, backpressure.
      <code>lw</code> and <code>sw</code> are the whole interface software gets. Three properties
      are contracts rather than conveniences.
    </p>

    <SpecTable
      :cols="[
        { key: 'p', label: 'Property' },
        { key: 'w', label: 'Why it is a contract' },
      ]"
      :rows="[
        {
          p: '<b>A fill is an entry read, not a plain read.</b> <code>entry_words = 1</code> with <code>STREAM</code> set.',
          w: 'a plain read would occupy the memory agent\'s shared read/write FSM and exclude a write for its whole duration',
        },
        {
          p: '<b>One write outstanding, acknowledged before the next</b> (<code>WR_MAX = 1</code>).',
          w: 'the protocol already forbids two <i>open</i> writes from one source; bounding un-acknowledged writes too is what gives ordering rules 2 and 3 their force. Free in steady state — a blocking cache never asks for a second writeback while one is open',
        },
        {
          p: '<b>The push handshake is a register, not a wire.</b>',
          w: 'the completion FIFO\'s state reaching the MEM stall combinationally would tie the front end\'s timing to the fabric\'s; a one-deep holding register cuts that path for one cycle of push latency',
        },
      ]"
    />

    <Callout kind="trap" title="The same trap, one level down, with a number on it">
      <p>
        The SIMD PE's vector window write is the same shape: letting the NoC's write enable —
        combinational from the receive FIFO's empty flag — reach the MEM stage's stall, the fetch
        hold and the instruction window's address was measured at
        <b>93.6 MHz of the assembled PE's clock</b>. The framework's own requestor registers its
        push handshake for exactly this reason —
        <RouterLink to="/component/simd" class="doc-link">SIMD PE</RouterLink>.
      </p>
    </Callout>

    <h2 class="doc-h2">The memory map</h2>

    <p class="doc-p">
      One decoder, deciding on the <b>top four address bits and nothing else</b>.
      <code>0x8xxx_xxxx</code> upward is a 2 GB software window onto physical DRAM; the translation
      is <code>DRAM_BASE | addr[30:0]</code> — OR-ed, never added, because the base's low 31 bits
      are zero by construction, so it costs no logic.
    </p>

    <SpecTable :cols="regions.cols" :rows="regions.rows" />

    <h3 class="doc-h3">Peer windows: the address is the routing</h3>

    <BitField
      :fields="peerAddr"
      caption="Word 0 of granule 0 of the scratchpad of the PE at (2,2) is a store to 0x3220_0000. sb and sh work: the byte enables travel with the push and the receiving window applies them"
    />

    <Callout kind="rule" title="Reads of a peer window do not exist">
      <p>
        The model is push-only, and a load here faults. A consumer reads its <i>own</i> scratchpad
        with an ordinary load: one cycle, zero NoC traffic. For bulk data, go through DRAM or the
        mover rather than pushing word by word; a peer push is one word per store.
      </p>
    </Callout>

    <h3 class="doc-h3">Local control</h3>

    <p class="doc-p">
      Word-addressed from <code>0x2000_0000</code>. Only <code>addr[7:2]</code> decodes, so the
      region aliases every 256 bytes; name the words by their symbol. Counters that would be CSRs
      elsewhere live here — there is no CSR file, no <code>Zicsr</code>, no interrupts and no trap
      vectors anywhere in this core.
    </p>

    <SpecTable :cols="ctl.cols" :rows="ctl.rows" />

    <h2 class="doc-h2">Ordering, and the idioms that rest on it</h2>

    <SpecTable :cols="ordering.cols" :rows="ordering.rows" />

    <Callout kind="rule" title="The corollaries a program must respect">
      <p>
        The doorbell is the <b>last</b> store. The flag must be a <b>different word</b> from the
        payload it announces, or a poll cannot tell a half-written entry from a finished one. A ring
        beats a single slot: advance a producer index last, never rewrite an entry a consumer may
        already have passed, and no handshake back is needed. Ordering holds
        <b>per destination</b> — pushes to two different PEs have no order between them.
      </p>
    </Callout>

    <StepPlayer :steps="handoff" label="DRAM hand-off between units">
      <template #default="{ state }">
        <div class="font-mono kt-text-caption whitespace-pre text-warm-700 dark:text-warm-300 leading-6 overflow-x-auto">{{ state.code.join("\n") }}</div>
        <div class="mt-3 flex items-center gap-2">
          <span class="kt-text-micro uppercase tracking-wider text-warm-400 dark:text-warm-600">the data is in</span>
          <span class="chip">{{ state.where }}</span>
        </div>
      </template>
    </StepPlayer>

    <Callout kind="note" title="What is deliberately absent">
      <p>
        No coherence, by construction rather than omission: the only externally writable memory is
        the home of its own addresses (never a copy), and the only cached memory is never externally
        written. No atomics: exclusive access between cores is done by ownership and push, not by
        locks. <code>FENCE</code> executes as a NOP — one core, one memory port, already ordered —
        and <code>FENCE.I</code> is not needed, because the instruction window is not writable from
        the data side.
      </p>
    </Callout>

    <h2 class="doc-h2">Halting</h2>

    <p class="doc-p">
      A halt is a redirect that also stops fetch. The halting instruction retires — it is the one
      that raised the halt — but does not commit architectural state beyond that.
      <code>a0</code> in the halt word is the committed value: a halt redirects, so nothing younger
      than the halting instruction commits, and everything older already has. Misaligned load and
      store <b>fault</b>, which RV32I permits.
    </p>

    <SpecTable :cols="halts.cols" :rows="halts.rows" caption="The cause and word are readable at CTL_CAUSE after the halt, and travel in the completion signal" />

    <h2 class="doc-h2">What it costs</h2>

    <p class="doc-p">
      The shipped configuration: 128-line L1, 2048-word windows, 32-entry BTB,
      <code>FWD_X</code> 1, LUTRAM register file, <code>WR_MAX</code> 1.
      <b>2,491 LUT, ~4,140 FF, 5 BRAM at 410.8 MHz</b>, at a 3.333 ns ask on
      <code>xcvu13p-fhgb2104-2L-e</code>.
    </p>

    <Callout kind="trap" title="Two numbers for the same core, and both are right">
      <p>
        This PE is <b>2,491 LUT at 410.8 MHz</b> in a flattened run and <b>2,477 LUT at
        377.9 MHz</b> in a hierarchy-preserved one — both at the same 3.333 ns ask, both the same
        RTL. The second is the flow the SIMD PE is measured in, so it is the one to hold beside a DSP
        figure; 33 MHz of that spread is the flow, not the design.
        <b>Compare within a flow, never across one.</b>
      </p>
    </Callout>

    <SpecTable
      :cols="units.cols"
      :rows="units.rows"
      caption="Hierarchical site accounting at a tighter 2.5 ns request, where the whole PE is 2,583 — the split is the information; it moves only a few sites across requests. xcvu13p-fhgb2104-2L-e, Vivado 2024.2, OOC synthesis only"
    />

    <Callout kind="measured" title="Two of those rows are design outcomes, not accounting">
      <p>
        The <b>scratchpad is 45 LUT</b> rather than the ~7 of a plain array because 38 of them are
        the cross-port bypass that makes the doorbell correct — a priced correctness cost, sitting
        on the critical path.
      </p>
      <p>
        And <b>a quarter of the unit is the framework attach</b>: <code>u_base</code> is 657 LUT and
        1,381 FF of port logic that every compute unit on this fabric carries, processor or not —
        the marginal cost of <i>this unit being a processor</i> is nearer <b>1,900 LUT</b>.
      </p>
    </Callout>

    <ResourceBars
      :items="coreSplit.items"
      unit="LUT · inside u_core, 1,187 total"
      caption="The predictor fits inside the IF number because its entries live in LUTRAM depth rather than logic"
    />

    <h3 class="doc-h3">Frequency, and the constraint to ask</h3>

    <p class="doc-p">
      The ceiling is a block RAM's clock-to-out, and the design reaches it at a 3.333 ns request.
      Below that the constraint buys nothing: synthesis answers an impossible request by spending
      LUT on a path whose length it cannot change.
    </p>

    <SpecTable :cols="freq.cols" :rows="freq.rows" caption="xcvu13p-fhgb2104-2L-e, Vivado 2024.2, OOC synthesis only. At the mesh ship clock — noc_clk at 300 MHz — the PE carries better than 30 % timing margin" />

    <Callout kind="measured" title="The critical path is the load-data return">
      <p class="font-mono kt-text-caption">
        u_spad/.../mem_reg_0/CLKBWRCLK → u_core/u_id/x_op1_reg[16]/D &nbsp;·&nbsp; 6 levels
      </p>
      <p>
        A data array's clock-to-out, the cross-port bypass, the sub-word extract, the forwarding
        network, the ID operand register. It is the <b>distance-3 forward of a load result</b> — the
        one hazard distance resolved by forwarding because a load's data exists no earlier — and the
        array at its head is the scratchpad, whose cross-port bypass put a mux between the array and
        the extract. A scalar core with honest synchronous memories ends at the speed a memory hands
        a word back, and this one does.
      </p>
    </Callout>

    <SpecTable :cols="bram.cols" :rows="bram.rows" caption="Every array that earns a tile fills the tile's natural depth at its aspect, 1K × 36 for a 32-bit port. Width is 88.9 % everywhere — a 36-bit face carrying 32 data bits — which is the primitive, not a choice. The tag array is far too shallow to earn a tile and stays LUTRAM, which is what makes the 128-line capacity nearly free on one tile" />

    <Callout kind="note" title="The register file is LUTRAM, and that is the same argument">
      <p>
        A block-RAM form exists behind <code>REGFILE_PRIM</code> with identical timing, but a
        32 × 32 register file leaves a 1K × 36 <code>RAMB36E2</code> <b>3.1 % depth-utilized</b> —
        the worst ratio anything in this design could post — so the LUTRAM form ships.
      </p>
    </Callout>

    <h2 class="doc-h2">Timing, in cycles</h2>

    <SpecTable :cols="itiming.cols" :rows="itiming.rows" caption="Read from the PE's own CTL_CYCLE / CTL_INSTRET counters on the full system: real routers, the real memory agent, RAM behind it" />

    <h3 class="doc-h3">Communication</h3>

    <p class="doc-p">
      <b>A push-and-doorbell round trip between two running cores is 49 cycles</b> — two window
      pushes, two hops each way, and two poll loops. The number is quantised by the poll: a
      four-instruction poll loop is ~9 cycles, and a push is observable only when the loop next
      comes round, so one extra iteration costs a whole loop. That quantisation is what the
      scratchpad's cross-port bypass buys.
    </p>

    <p class="doc-p">
      <b>Two concurrent pairs cost exactly what one costs</b> — identical to the cycle, on
      link-disjoint routes — so pairwise communication scales until routes share a link. All-to-one
      aggregation, with workers pushing value-then-flag and the leader polling flags only, costs the
      leader <b>380 cycles and 10 instructions</b> for three workers over one: the leader reads a
      value beside a flag it has seen with no handshake back, which is the per-destination ordering
      rule doing the work.
    </p>

    <h3 class="doc-h3">Multi-core scaling</h3>

    <SpecTable :cols="scaling.cols" :rows="scaling.rows" caption="One memory agent serves up to four PEs. The +13.7 % is measured while the three neighbours run the heaviest memory work in the suite" />

    <Callout kind="trap" title="Lay buffers out so they do not conflict-miss">
      <p>
        That worst case is deliberate: a copy whose source and destination sit exactly one
        cache-size apart, so every access conflict-misses — <b>26.6 cycles per instruction</b>, the
        hardest load one PE can put on the agent with no wasted instructions. A stride that maps
        source and destination to the same sets is what a 4 KB direct-mapped cache punishes.
      </p>
      <p>
        Two DRAM hand-offs running concurrently through one agent cost the second pair
        <b>+159 cycles on the write side and +175 on the read side</b> over the first — two blocking
        flush-alls sharing the agent's write slots.
      </p>
    </Callout>

    <h2 class="doc-h2">Configuring one</h2>

    <p class="doc-p">
      Every knob is a parameter of one design — no forked files. <b>The defaults below are the
      shipped configuration</b>, and they are what the numbers on this page characterise; a changed
      setting is verified as itself, never assumed from the default build.
    </p>

    <SpecTable :cols="params.cols" :rows="params.rows" />

    <SpecTable
      :cols="gates.cols"
      :rows="gates.rows"
      caption="python tests/pe/tools/rv_run.py --gate N. Every run is bounded — per-case cycle ceilings, a watchdog, spin caps — so a hanging core fails rather than hanging the bench"
    />

    <Callout kind="rule" title="One definition reaches both the bench and synthesis">
      <p>
        Configuration knobs flow to the benches and to synthesis from the same definition, so what
        levels 1–4 verified is what the synthesis measured — a knob that reached only one of the two
        would silently decouple them.
      </p>
    </Callout>

    <h2 class="doc-h2">The arithmetic EX does not have</h2>

    <p class="doc-p">
      <b>There is no multiplier, no divider and no floating point in this core.</b>
      <code>mul</code> faults: the decoder accepts <code>funct7</code> of <code>0000000</code> and
      <code>0100000</code> on the register-register group and nothing else, so RV32M's
      <code>0000001</code> raises an illegal-instruction halt at the offending PC.
    </p>

    <SpecTable :cols="absent.cols" :rows="absent.rows" />

    <Callout kind="trap" title="No SCALAR datapath in this machine has a multiplier — but the SIMT PE does">
      <p>
        It is easy to read “the machine has float” and conclude the wrong thing about this core, and
        just as easy to read “this core has no multiply” and conclude the wrong thing about the
        machine. The SIMD PE's multiply and its float tier live in the <i>vector</i> register file
        behind custom-0 and custom-1, and reaching them means putting operands in
        <code>v0..v7</code>. The SIMT PE's <i>uniform</i> ALU is <b>ten</b> register-register
        operations — the same ten RV32I has.
      </p>
      <p>
        <b>The SIMT PE's per-thread datapath, however, has <code>RV32M</code>.</b>
        <code>mul</code>, <code>mulh</code>, <code>mulhsu</code> and <code>mulhu</code> run on the
        per-thread register file, one product per lane, on the standard <code>OP</code> encoding —
        so a shader that writes <code>a * b</code> gets one instruction. <b>On this core and on the
        SIMD PE's scalar half it still calls libgcc.</b> The DSP page prices that: the base core's
        128-element int8 dot is 8,221 cycles and the same kernel with its multiply costed at one
        instruction is 1,297, so 128 software multiplies account for 6,924 cycles —
        <b>about 54 cycles each</b>, and that is the easy case, because int8 operands unroll to
        eight shift-add steps where a general 32×32 unrolls to far more.
      </p>
      <p>
        <code>div</code> and <code>rem</code> fault on every class, per-thread included.
      </p>
    </Callout>

    <SpecTable
      :cols="mulCost.cols"
      :rows="mulCost.rows"
      caption="ESTIMATE — reasoned from measured neighbours on xcvu13p-fhgb2104-2L-e, not synthesised; nothing in this table has been built. A DSP48E2 is 27×18 signed, so a 32×32 product is four 17-bit partial products and a small carry network; mul needs only three, because the high-times-high term contributes nothing below bit 34. The LUT is the merge network and the operand registers, not the multiply"
    />

    <Callout kind="measured" title="Take the pipelined multiplier, not the multi-cycle one">
      <p>
        The multi-cycle form saves three DSP columns, and on this device
        <b>a DSP column is worth about 230 LUT</b> — so it trades ~690 LUT of nominal value for real
        sequencing logic on a resource the design is explicitly <i>not</i> short of. LUT is the
        binding resource here and DSP is not.
      </p>
    </Callout>

    <Callout kind="trap" title="The cost is not the multiplier — it is where its result joins">
      <p>
        <b>The result mux is on the distance-1 forwarding path.</b> <code>ex_alu</code> feeds both
        <code>m_val</code> and <code>fwd_x_val</code>, and <code>fwd_x_val</code> is an input to
        <code>x_op1_reg</code> — the register the critical path above already ends at. Adding a case
        to that mux is one more LUT level in exactly the wrong place. The SIMD PE measures this shape
        one level up: its binding path ends at a result mux feeding a write port, and removing one
        block from that mux buys <b>33.9 MHz</b>.
      </p>
      <p>
        <b>And the stall term widens.</b> A 3-cycle multiply in an in-order core with three
        positional forwarding sources must stall EX — a new term in <code>stall_d</code>. The
        <code>FWD_X</code> result above is the warning: widening a stall term that was already there
        cost <b>5 MHz</b> and saved 2 LUT.
      </p>
      <p>
        The alternative is a scoreboard, letting the multiply retire out of order. <b>Do not.</b>
        The hazard unit is the whole of this core's complexity budget — three sources by position,
        one stall rule, nothing else — and a scoreboard ends that invariant for one instruction.
        <b>ESTIMATE −5 to −15 MHz</b> on a 410.8 MHz core, and the mux is what to measure first.
      </p>
    </Callout>

    <h3 class="doc-h3">Why the GPU was the cheap place for a multiplier, and this core is not</h3>

    <p class="doc-p">
      The costing above is still the costing for <b>this</b> core. What has happened since is that
      the machine got its multiplier somewhere else — and the reason is exactly the two risks just
      named, because the SIMT PE has neither of them.
    </p>

    <SpecTable
      :cols="mulWhere.cols"
      :rows="mulWhere.rows"
      caption="The multiplier is built at the float tier's own latency on purpose: equal latency makes a collision between a float result and a multiply result structurally impossible rather than arbitrated, because one instruction issues per cycle and two results can only want the write port on the same cycle if they were issued on the same cycle. It is an increment on machinery that already existed for float, not a second mechanism — and it ships with that tier rather than separately from it"
    />

    <Callout kind="rule" title="The general rule, now with a built example behind it">
      <p>
        <b>A multi-cycle unit is cheap in a machine that already has a way to park an instruction,
        and expensive in one whose whole complexity budget is three positional forwards and one
        stall rule.</b> That is what this core's costing was pointing at all along; the SIMT PE is
        what it looks like when the machinery is already there.
      </p>
      <p>
        Note the other half of why it was cheap: <code>RV32M</code> is <b>standard encoding
        space</b>, so it cost none of the four custom opcode majors and a compiler emits it with
        <code>-march=rv32im</code> and nothing else.
      </p>
    </Callout>

    <Callout kind="rule" title="Iterative division is a trap at this size, and the mechanism is specific">
      <p>
        An iterative divider is ~35 cycles and a <b>33-bit subtract per cycle</b>, and it cannot
        borrow the one in EX: <code>sum</code> is <code>ex_addr</code>. Muxing a divider's operands
        into that adder puts a mux in front of the effective address — the one path the whole
        six-boundary arrangement exists to keep short, because the address leaves EX combinationally
        precisely so the data arrays can register it.
      </p>
      <p>
        So a divider carries its own subtractor, its own remainder shift register, its own quotient
        register, the sign fixups <code>div</code>'s truncate-toward-zero needs, and the two mandated
        special cases (÷0, and −2³¹ ÷ −1). <b>200–300 LUT ESTIMATE</b> — most of the EX stage's own
        418 again — for a <b>2×</b> against libgcc on an instruction a controller issues
        approximately never, where <code>mul</code> is an <b>8–13×</b> on a common one. If
        <code>mul</code> is built, divide-by-a-constant strength-reduces to <code>mulhu</code> and
        covers the case a controller actually meets.
      </p>
    </Callout>

    <Callout kind="rule" title="Minimal scalar float is the wrong purchase, and not because of the LUT">
      <p>
        The tempting shape is to reuse what is already measured:
        <code>khs_f16_lane</code> is <b>609 LUT and 2 DSP</b>, fifteen cycles deep, II = 1, its FMA
        verified bit-for-bit against a golden model. One of those, a 32-entry float register file,
        and the two FP32 converters — one of which is pure wiring, because E8 <i>is</i> FP32's
        exponent field — is roughly <b>900–1,100 LUT and 2 DSP, ESTIMATE</b>.
      </p>
      <p>
        <b>Fifteen cycles into a three-source in-order forwarding network.</b> <code>fadd</code>
        would stall EX for fifteen cycles or need the scoreboard just refused. The SIMD tier does not
        have this problem because an <i>accumulating</i> instruction needs only the accumulator's own
        busy shadow, where an instruction that writes a register has to be tracked.
      </p>
      <p>
        <b>And it would not be <code>F</code>, so no compiler would emit it.</b> E8M15 is 24 bits
        with no subnormals, one rounding mode, and a documented one-ulp deviation on subtractive
        alignment; RISC-V's <code>F</code> is IEEE-754 binary32 with subnormals, five rounding modes
        and <code>fcsr</code>, and this core has no CSR file at all. A non-conforming float behind a
        custom major also has nowhere to live — <b>all four custom majors are spoken for</b>. The
        core's first design objective is that ordinary compilers work unmodified.
      </p>
    </Callout>

    <Callout kind="open" title="The recommendation: build mul/mulh. Not div/rem. Not scalar float.">
      <p>
        <code>mul</code> is the only one of the three that is <b>standard encoding space</b>, that
        GCC emits with nothing more than <code>-march=rv32im</code>, and that removes a measured
        54-cycle tax from every index computation — and from every benchmark baseline this project
        publishes, which today has to carry a twin whose multiply is costed by hand to stay honest.
        Four DSP columns on a device where DSP is not the binding resource is the cheapest thing this
        core could spend.
      </p>
      <p>
        The other two belong where the arithmetic already is. Float on this machine is built,
        measured and accurate to <b>1.5e-5</b> — 32× better than the fp16 mobile fragment shaders
        run at — and it now exists in <b>both</b> wide classes: <b>4 E8M15 FMA lanes on the DSP
        PE</b> and <b>8 on the SIMT PE</b>, both measured, float not optional in either because
        rendering needs it. That is where a kernel needing float should be —
        <RouterLink to="/component/simd" class="doc-link">SIMD PE</RouterLink>,
        <RouterLink to="/mpe/simt" class="doc-link">SIMT PE</RouterLink>.
      </p>
    </Callout>

    <SpecTable :cols="changeMind.cols" :rows="changeMind.rows" caption="What would change that answer" />

    <h2 class="doc-h2">Where extensions attach</h2>

    <p class="doc-p">
      Nothing enters this baseline because it is normally found in a CPU. Each of
      <code>RV32M</code>, <code>Zbb</code>, a larger predictor, a larger L1, multiple outstanding
      misses, atomics, DSP or SIMD lanes is a separate experiment: add one feature, measure
      resources and frequency, measure workload benefit.
    </p>

    <SpecTable
      :cols="[
        { key: 's', label: 'Seam' },
        { key: 'w', label: 'Where' },
      ]"
      :rows="[
        { s: 'new functional units', w: 'the EX stage\'s ALU select, widened; the decoder already routes <code>funct3</code>/<code>funct7</code>' },
        { s: 'wide operands', w: 'a second register file beside <code>rv_regfile</code>, on the same two-boundary read timing' },
        { s: 'wide memory', w: 'the internal L1\'s line is already 256 bits at the fill boundary; only the CPU-side port is 32' },
        { s: 'bulk peer transfer', w: 'a write-combining buffer in front of the requestor\'s push path' },
        { s: 'more miss concurrency', w: 'the requestor\'s single tag becomes a small MSHR table; <code>rv_l1</code>\'s blocking FSM is what changes' },
      ]"
      caption="The seams an extension would use, named but not designed. The SIMD PE is what happens when the first two are taken"
    />
  </DocPage>
</template>
