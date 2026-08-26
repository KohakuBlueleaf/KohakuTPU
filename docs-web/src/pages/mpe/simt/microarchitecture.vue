<script setup>
// ===========================================================================
// SIMT PE — microarchitecture.
// One mechanism, one diagram, and every failure drawn as a concrete broken
// trace beside the working one. Every trap below is a failure that HAPPENED,
// not one that was anticipated.
// ===========================================================================

// --------------------------------------------------------------- whole unit
const unit = {
  nodes: [
    {
      id: "kick",
      x: 0,
      y: 0,
      w: 10,
      label: "kick",
      sub: "CU_INST · op = wave count",
    },
    { id: "port", x: 0, y: 6, w: 10, label: "fabric port", sub: "CU_DATA" },
    {
      id: "lds",
      x: 0,
      y: 11,
      w: 10,
      label: "kht_lds",
      sub: "LANES banks, interleaved",
    },

    {
      id: "pcq",
      x: 12.5,
      y: 0,
      w: 10,
      label: "nxt[wave]",
      sub: "round-robin over rdy",
    },
    {
      id: "imem",
      x: 12.5,
      y: 6,
      w: 10,
      label: "imem window",
      sub: "instruction",
    },
    {
      id: "ictl",
      x: 12.5,
      y: 11,
      w: 10,
      label: "ictl window",
      sub: "60b control, PREDECODED",
      accent: true,
    },

    {
      id: "fetch",
      x: 25,
      y: 0,
      w: 11,
      label: "fetch",
      sub: "f1 → f2, three deep",
    },
    {
      id: "ex",
      x: 25,
      y: 6,
      w: 11,
      label: "EX",
      sub: "no decode stage",
      accent: true,
    },
    {
      id: "sfile",
      x: 25,
      y: 11,
      w: 11,
      label: "scalar file + SALU",
      sub: "s0..s31 per wave",
    },

    {
      id: "vreg",
      x: 38.5,
      y: 6,
      w: 10,
      label: "kht_vregfile",
      sub: "x0..x31 per LANE",
      accent: true,
    },
    {
      id: "mask",
      x: 38.5,
      y: 11,
      w: 10,
      label: "mask + IPDOM",
      sub: "split · join · tmc",
      accent: true,
    },
    {
      id: "valu",
      x: 38.5,
      y: 16,
      w: 10,
      label: "kht_valu",
      sub: "the lane array",
      accent: true,
    },
    {
      id: "fpu",
      x: 38.5,
      y: 21,
      w: 10,
      label: "kht_fpu · kht_imul",
      sub: "15 cyc, one retire slot",
      accent: true,
    },

    { id: "lsu", x: 51, y: 0, w: 10, label: "LSU", sub: "walks lanes" },

    {
      id: "comp",
      x: 63.5,
      y: 0,
      w: 10,
      label: "completion",
      sub: "the host's ONLY sequencing point",
    },
    {
      id: "l1",
      x: 63.5,
      y: 6,
      w: 10,
      label: "rv_l1",
      sub: "write-back, ONE miss",
    },
    { id: "mag", x: 63.5, y: 11, w: 10, label: "fabric → MAG", sub: "DRAM" },
  ],
  edges: [
    { from: "kick:r", to: "pcq:l", dir: "h", accent: true },
    { from: "port:r", to: "imem:l", dir: "h" },
    { from: "port:r", to: "ictl:l", dir: "h", label: "kht_predec" },
    { from: "port:b", to: "lds:t", dir: "v" },
    { from: "pcq:r", to: "fetch:l", dir: "h" },
    { from: "imem:r", to: "fetch:l", dir: "h" },
    { from: "ictl:r", to: "ex:l", dir: "h", accent: true },
    { from: "fetch:b", to: "ex:t", dir: "v", accent: true },
    { from: "ex:b", to: "sfile:t", dir: "v" },
    { from: "ex:r", to: "vreg:l", dir: "h", accent: true },
    { from: "ex:t", to: "lsu:b", dir: "v", label: "the memory op" },
    { from: "vreg:b", to: "mask:t", dir: "v", accent: true },
    { from: "mask:b", to: "valu:t", dir: "v", accent: true },
    { from: "valu:b", to: "fpu:t", dir: "v", accent: true },
    { from: "valu:l", to: "sfile:r", dir: "h" },
    { from: "lsu:l", to: "lds:r", dir: "h" },
    { from: "lsu:r", to: "l1:l", dir: "h" },
    { from: "l1:b", to: "mag:t", dir: "v" },
    {
      from: "l1:t",
      to: "comp:b",
      dir: "v",
      dash: true,
      label: "flush, then done",
    },
  ],
  groups: [{ x: 37.5, y: 5.9, w: 12, h: 18.5, label: "kht_unit" }],
};

// ------------------------------------------------------- decode at image load
const predec = {
  nodes: [
    {
      id: "cud",
      x: 0,
      y: 3,
      w: 12,
      label: "CU_DATA word",
      sub: "once per shader",
    },
    {
      id: "pd",
      x: 15,
      y: 6.5,
      w: 12,
      label: "kht_predec",
      sub: "combinational",
      accent: true,
    },
    { id: "im", x: 31, y: 0, w: 11, label: "imem", sub: "32b · READ_LAT 1" },
    {
      id: "ic",
      x: 31,
      y: 6.5,
      w: 11,
      label: "ictl",
      sub: "60b · READ_LAT 1",
      accent: true,
    },
    {
      id: "addr",
      x: 15,
      y: 13.5,
      w: 12,
      label: "imem_addr",
      sub: "one address, both arrays",
    },
    {
      id: "out",
      x: 46,
      y: 3,
      w: 12,
      label: "instr + ctrl",
      sub: "arrive TOGETHER, in EX",
      accent: true,
    },
  ],
  edges: [
    { from: "cud:r", to: "im:l", dir: "h" },
    { from: "cud:r", to: "pd:l", dir: "h" },
    { from: "pd:r", to: "ic:l", dir: "h", accent: true },
    { from: "addr:r", to: "ic:b", dir: "v" },
    { from: "addr:r", to: "im:b", dir: "v" },
    { from: "im:r", to: "out:l", dir: "h" },
    { from: "ic:r", to: "out:l", dir: "h", accent: true },
  ],
  groups: [
    { x: 30, y: -1, w: 13, h: 11.6, label: "the instruction window, twice" },
  ],
};

const budgetLevels = `   3.333 ns  the 300 MHz period
  -0.909     RAMB36E2 clock-to-out                     <- not negotiable
  -0.318     the first net (fanout 373 on x_rs2)
  -0.056     clock skew
  -0.035     clock uncertainty
  -0.050     setup at the destination
  =========
   1.965 ns  for LOGIC LEVELS AND THEIR ROUTE

   one level, unplaced:  ~0.04 logic + ~0.20 route = 0.24 ns
   =>  about NINE levels, and route is 63% of every one of them`;

// ------------------------------------------------------------------- fetch
const fetchOk = {
  rows: [
    {
      name: "imem_addr",
      kind: "bus",
      values: ["0x04", "0x08", "0x0C", "0x10"],
    },
    { name: "f1_pc", kind: "bus", values: ["0x04", "0x08", "0x0C", "0x10"] },
    {
      name: "imem_data",
      kind: "bus",
      values: [null, "[0x04]", "[0x08]", "[0x0C]"],
    },
    {
      name: "f2_pc",
      kind: "bus",
      values: [null, null, "0x04", "0x08"],
      mark: [2, 3],
    },
    {
      name: "i2 (instr)",
      kind: "bus",
      values: [null, null, "[0x04]", "[0x08]"],
      mark: [2, 3],
    },
    {
      name: "",
      kind: "text",
      values: [
        "address issued",
        "one cycle of block RAM",
        "ONE FABRIC FLOP",
        "",
      ],
    },
  ],
  notes: [
    {
      text: "Three stages, not two: f1 is the address issued into the window, and f2 is the instruction REGISTERED IN FABRIC beside the PC and the wave it belongs to. f2_pc and i2 name the SAME instruction.",
    },
    {
      cycle: 2,
      text: "The third stage is worth 0.83 ns. Executing straight off the window makes every downstream cone start at a RAMB36E2, whose clock-to-out is 0.909 ns — 36% of a 2.5 ns period, spent before any logic runs. Off a flip-flop the same cone starts at 0.077.",
      tone: "good",
    },
  ],
};

const trap1 = {
  rows: [
    {
      name: "pc_q (architectural)",
      kind: "bus",
      values: ["0x04", "0x04", "0x08", "0x08"],
    },
    {
      name: "imem_addr",
      kind: "bus",
      values: ["0x04", "0x04", "0x08", "0x08"],
    },
    {
      name: "instr",
      kind: "bus",
      values: ["[0x04]", "[0x04]", "[0x08]", "[0x08]"],
      mark: [1, 3],
    },
  ],
  notes: [
    {
      cycle: 1,
      text: "EVERY INSTRUCTION EXECUTES TWICE. An architectural PC only advances when an instruction RETIRES, which is a cycle after the fetch should already have moved on.",
      tone: "bad",
    },
  ],
};

const trap2Broken = {
  rows: [
    { name: "hold", kind: "bit", values: [0, 1, 1, 0] },
    {
      name: "imem_addr",
      kind: "bus",
      values: ["0x08", "0x0C", "0x10", "0x14"],
    },
    {
      name: "f1_pc",
      kind: "bus",
      values: ["0x08", "0x08", "0x08", "0x08"],
      mark: [1, 2],
    },
    {
      name: "imem_data",
      kind: "bus",
      values: ["[0x04]", "[0x08]", "[0x0C]", "[0x10]"],
      mark: [3],
    },
    {
      name: "",
      kind: "text",
      values: ["", "f1 FROZEN", "address ran on", "i2 <= [0x10]"],
    },
  ],
  notes: [
    {
      cycle: 1,
      text: "f1_pc is frozen by the same hold, but the address is not — so the window keeps fetching words f1 no longer names.",
      tone: "bad",
    },
    {
      cycle: 3,
      text: "On resume i2 <= [0x10] while f2_pc <= 0x08. 0x08 NEVER EXECUTES. No error, no trap.",
      tone: "bad",
    },
  ],
};

const trap2Fixed = {
  rows: [
    { name: "hold", kind: "bit", values: [0, 1, 1, 0] },
    {
      name: "imem_addr",
      kind: "bus",
      values: ["0x08", "0x08", "0x08", "0x0C"],
      mark: [1, 2],
    },
    { name: "f1_pc", kind: "bus", values: ["0x08", "0x08", "0x08", "0x08"] },
    {
      name: "imem_data",
      kind: "bus",
      values: ["[0x04]", "[0x08]", "[0x08]", "[0x08]"],
      mark: [3],
    },
    {
      name: "",
      kind: "text",
      values: ["", "re-presented", "re-presented", "i2 <= [0x08]"],
    },
  ],
  notes: [
    {
      text: "imem_addr = hold ? f1_pc : nxt[cur] — held, the window must RE-PRESENT the address already in flight.",
      tone: "good",
    },
    { cycle: 3, text: "0x08 executes.", tone: "good" },
  ],
};

const interleave = {
  rows: [
    { name: "cur", kind: "bus", values: ["w0", "w1", "w2", "w0", "w1"] },
    {
      name: "fetch",
      kind: "bus",
      values: ["nxt[0]", "nxt[1]", "nxt[2]", "nxt[0]", "nxt[1]"],
    },
    { name: "f1", kind: "bus", values: [null, "w0", "w1", "w2", "w0"] },
    {
      name: "f2 (EX)",
      kind: "bus",
      values: [null, null, "w0", "w1", "w2"],
      mark: [2, 3, 4],
    },
    {
      name: "MEM",
      kind: "bus",
      values: [null, null, null, "w0", "w1"],
      mark: [3, 4],
    },
  ],
  notes: [
    {
      cycle: 3,
      text: "EX and MEM hold DIFFERENT waves, so the hazard between them cannot exist. Round-robin picks a live wave every cycle — ready-wave interleaving, not a compulsory barrel period: with four live waves it issues from four, not from WAVES with WAVES−4 bubbles.",
      tone: "good",
    },
    {
      text: "Interleaving is also what makes the third fetch stage cheap. Two fetches are wrong-path on a redirect — the one in f1 and the one being issued — and each only if it belongs to the redirecting wave, so under interleaving both usually belong to OTHER waves and survive.",
    },
  ],
};

// --------------------------------------------------------------- hold graph
const holds = {
  nodes: [
    { id: "l1s", x: 0, y: 0, w: 11, label: "l1_stall", sub: "from registers" },
    { id: "lbusy", x: 0, y: 4, w: 11, label: "lsu_busy", sub: "a REGISTER" },
    {
      id: "vt",
      x: 0,
      y: 9,
      w: 11,
      label: "vt_stall",
      sub: "the vector hazard",
    },
    {
      id: "warm",
      x: 0,
      y: 13,
      w: 11,
      label: "warm_stall",
      sub: "the read has not caught up",
    },
    {
      id: "want",
      x: 0,
      y: 17,
      w: 11,
      label: "lsu_want",
      sub: "a walk is owed",
    },
    {
      id: "shz",
      x: 0,
      y: 21,
      w: 11,
      label: "s_hz",
      sub: "the scalar interlock",
    },
    {
      id: "fsoon",
      x: 0,
      y: 25,
      w: 11,
      label: "f_soon",
      sub: "a float lands in 2",
    },
    {
      id: "need",
      x: 0,
      y: 29,
      w: 11,
      label: "lsu_need",
      sub: "the walk, gated",
    },

    {
      id: "base",
      x: 15,
      y: 2,
      w: 12,
      label: "base_hold",
      sub: "from registers",
      accent: true,
    },
    {
      id: "hold",
      x: 15,
      y: 14.5,
      w: 12,
      label: "hold",
      sub: "mixed",
      accent: true,
    },

    {
      id: "xhold",
      x: 31,
      y: 2,
      w: 13,
      label: "kht_unit.x_hold",
      sub: "FREEZE the MEM register",
    },
    {
      id: "fetchgo",
      x: 31,
      y: 14.5,
      w: 13,
      label: "fetch · nxt[] · go",
      sub: "DO NOT RETIRE",
    },
    {
      id: "xdefer",
      x: 31,
      y: 27,
      w: 13,
      label: "kht_unit.x_defer",
      sub: "READ it, do NOT COMMIT it",
    },
  ],
  edges: [
    { from: "l1s:r", to: "base:l", dir: "h" },
    { from: "lbusy:r", to: "base:l", dir: "h" },
    { from: "base:r", to: "xhold:l", dir: "h", accent: true },
    { from: "base:b", to: "hold:t", dir: "v" },
    { from: "vt:r", to: "hold:l", dir: "h" },
    { from: "warm:r", to: "hold:l", dir: "h" },
    { from: "want:r", to: "hold:l", dir: "h" },
    { from: "shz:r", to: "hold:l", dir: "h" },
    { from: "fsoon:r", to: "hold:l", dir: "h" },
    { from: "hold:r", to: "fetchgo:l", dir: "h", accent: true },
    { from: "need:r", to: "xdefer:l", dir: "h", accent: true },
    { from: "shz:r", to: "xdefer:l", dir: "h", accent: true },
    { from: "fsoon:r", to: "xdefer:l", dir: "h", accent: true },
  ],
};

const holdTable = {
  cols: [
    { key: "s", label: "Signal", mono: true },
    { key: "reg", label: "Registered?" },
    { key: "to", label: "Fed to", mono: true },
    { key: "means", label: "Means" },
  ],
  rows: [
    {
      s: "base_hold",
      reg: "from registers",
      to: "x_hold",
      means: "<b>freeze</b> the MEM register",
    },
    {
      s: "hold",
      reg: "mixed",
      to: "fetch, nxt[], go, s_wen",
      means: "do not <b>retire</b> this instruction",
    },
    {
      s: "x_defer",
      reg: "<b>combinational</b>",
      to: "kht_unit",
      means:
        "do not <b>commit</b> it yet — <code>lsu_need || s_hz || f_soon</code>",
    },
    {
      s: "warm_stall",
      reg: "<b>combinational</b>",
      to: "hold, x_split, lsu_need",
      means:
        "the vector read has not caught up, or a reduction's tree has not drained",
    },
    {
      s: "s_hz",
      reg: "from registers",
      to: "hold, x_defer, x_tmc",
      means: "the scalar half's distance-1 interlock",
    },
    {
      s: "f_soon",
      reg: "from registers",
      to: "hold, x_defer",
      means: "a float or multiply result wants the write port in two cycles",
    },
  ],
};

const trap3 = {
  nodes: [
    {
      id: "x",
      x: 0,
      y: 0,
      w: 11,
      label: "x_hold = 1",
      sub: "= base_hold || vt_stall",
    },
    {
      id: "mem",
      x: 15,
      y: 0,
      w: 13,
      label: "MEM register FREEZES",
      sub: "nothing advances",
    },
    {
      id: "rd",
      x: 32,
      y: 0,
      w: 11,
      label: "m_rd stays live",
      sub: "the destination",
    },
    {
      id: "hz",
      x: 32,
      y: 6,
      w: 11,
      label: "hz_raw stays 1",
      sub: "the hazard never clears",
    },
    {
      id: "vt",
      x: 15,
      y: 6,
      w: 13,
      label: "vt_stall stays 1",
      sub: "wedged",
      accent: true,
    },
  ],
  edges: [
    { from: "x:r", to: "mem:l", dir: "h" },
    { from: "mem:r", to: "rd:l", dir: "h" },
    { from: "rd:b", to: "hz:t", dir: "v" },
    { from: "hz:l", to: "vt:r", dir: "h" },
    {
      from: "vt:l",
      to: "x:b",
      dir: "h",
      accent: true,
      label: "an input to itself",
    },
  ],
};

const trap4 = {
  rows: [
    { name: "per_lane", kind: "bit", values: [1, 1] },
    { name: "lsu_busy", kind: "bit", values: [0, 1], mark: [0] },
    { name: "hold", kind: "bit", values: [0, 1] },
    { name: "go", kind: "bit", values: [1, 0], mark: [0] },
    { name: "lsu_run", kind: "bit", values: [0, 1] },
    {
      name: "",
      kind: "text",
      values: ["RETIRES — fetch advances", "walk reads the NEXT instruction"],
    },
  ],
  notes: [
    {
      cycle: 0,
      text: "lsu_busy is a REGISTER, so on the cycle a walk is decided it is still low. go fires, the instruction retires, and the walk then reads width/scale/base off the NEXT instruction.",
      tone: "bad",
    },
    {
      text: "Symptom: ea = garbage, rgn = BAD, l1_req suppressed — the store vanishes and three of four checks still pass.",
      tone: "bad",
    },
  ],
};

const trap5 = {
  nodes: [
    {
      id: "need",
      x: 0,
      y: 0,
      w: 11,
      label: "lsu_need = 1",
      sub: "in base_hold",
    },
    { id: "base", x: 15, y: 0, w: 11, label: "base_hold = 1" },
    { id: "mem", x: 30, y: 0, w: 13, label: "MEM register FREEZES" },
    {
      id: "hz",
      x: 30,
      y: 5.5,
      w: 13,
      label: "the address hazard",
      sub: "never clears",
    },
    { id: "vt", x: 15, y: 5.5, w: 11, label: "vt_stall = 1" },
    {
      id: "walk",
      x: 0,
      y: 5.5,
      w: 11,
      label: "the walk never STARTS",
      sub: "DEADLOCK",
      accent: true,
    },
  ],
  edges: [
    { from: "need:r", to: "base:l", dir: "h" },
    { from: "base:r", to: "mem:l", dir: "h" },
    { from: "mem:b", to: "hz:t", dir: "v" },
    { from: "hz:l", to: "vt:r", dir: "h" },
    { from: "vt:l", to: "walk:r", dir: "h", accent: true },
  ],
};

const deferFixed = {
  rows: [
    { name: "lsu_need", kind: "bit", values: [1, 1, 1, 1, 0] },
    { name: "base_hold", kind: "bit", values: [0, 1, 1, 1, 0], mark: [0] },
    { name: "x_hold", kind: "bit", values: [0, 1, 1, 1, 0], mark: [0] },
    { name: "x_defer", kind: "bit", values: [1, 1, 1, 1, 0], mark: [0] },
    { name: "vt_stall", kind: "bit", values: [1, 0, 0, 0, 0] },
    { name: "lsu_busy", kind: "bit", values: [0, 1, 1, 1, 0] },
    { name: "lsu_done", kind: "bit", values: [0, 0, 0, 0, 1] },
    { name: "hold", kind: "bit", values: [1, 1, 1, 1, 0] },
    {
      name: "",
      kind: "text",
      values: [
        "MEM ADVANCES → bubble, hazard clears",
        "walk STARTS",
        "…walk…",
        "…walk…",
        "capture with ld_buf complete — RETIRE",
      ],
    },
  ],
  notes: [
    {
      cycle: 0,
      text: "lsu_need is NOT in base_hold. x_defer suppresses the MEM CAPTURE without freezing the register: kht_unit sees !x_hold, so the MEM register advances and inserts a bubble — and the hazard clears.",
      tone: "good",
    },
    {
      cycle: 4,
      text: "lsu_done → lsu_need = 0 → x_defer = 0, hold = 0. kht_unit captures with ld_buf complete and the instruction retires.",
      tone: "good",
    },
  ],
};

// ------------------------------------------------------- trap 6, warm_stall
const v1Consumers = {
  cols: [
    { key: "c", label: "Consumer", mono: true },
    { key: "st", label: "Stage", mono: true, align: "center" },
    { key: "v", label: "v1_rd is…" },
    { key: "owe", label: "Cycles owed", mono: true, align: "right" },
  ],
  rows: [
    {
      c: "kht_valu (ordinary RV32I)",
      st: "MEM",
      v: "correct, by construction",
      owe: "0",
      _tone: "good",
    },
    {
      c: "split's predicate",
      st: "EX",
      v: "the <b>PREVIOUS</b> instruction's rs1",
      owe: "1",
      _tone: "bad",
    },
    {
      c: "ballot / vreadfirst",
      st: "EX",
      v: "the <b>PREVIOUS</b> instruction's rs1",
      owe: "1",
      _tone: "bad",
    },
    {
      c: "redux*",
      st: "EX",
      v: "the same, plus a pipelined tree that has not drained",
      owe: "LNW + 2",
      _tone: "bad",
    },
    {
      c: "a per-lane access",
      st: "EX",
      v: "the same, plus <code>ea_all_q</code> registered behind it",
      owe: "2, or <b>4</b> with the banked LDS",
      _tone: "bad",
    },
  ],
};

const warmNoHz = {
  rows: [
    {
      name: "instr in EX",
      kind: "bus",
      values: ["split x6", "split x6", "split x6"],
    },
    { name: "hz", kind: "bit", values: [0, 0, 0] },
    { name: "warm_stall", kind: "bit", values: [1, 0, 0] },
    {
      name: "v1_rd",
      kind: "bus",
      values: ["x[prev rs1]", "x6", "x6"],
      mark: [0],
    },
    {
      name: "",
      kind: "text",
      values: [
        "the PREVIOUS instr's rs1",
        "v1_rd = x6 — correct",
        "split COMMITS",
      ],
    },
  ],
  notes: [
    {
      cycle: 1,
      text: "No hazard — x6 was written long ago, so ONE cycle of hold is enough.",
      tone: "good",
    },
  ],
};

const warmHzBroken = {
  rows: [
    {
      name: "instr in EX",
      kind: "bus",
      values: ["split x6", "split x6", "split x6"],
    },
    { name: "hz", kind: "bit", values: [1, 0, 0] },
    { name: "warm cycle counted", kind: "bit", values: [1, 1, 1], mark: [0] },
    {
      name: "v1_rd",
      kind: "bus",
      values: ["—", "OLD x6", "NEW x6"],
      mark: [1],
    },
    {
      name: "",
      kind: "text",
      values: ["andi x6 writes at THIS edge", "split COMMITS — OLD x6", ""],
    },
  ],
  notes: [
    {
      cycle: 1,
      text: "andi x6 ; split x6 — the write of x6 lands at the cycle-1 edge, so the read issued then is read-first and still returns the OLD x6. Counting the hazard cycle as a warm cycle releases the split one cycle early, and it diverges on a stale predicate.",
      tone: "bad",
    },
  ],
};

const warmHzFixed = {
  rows: [
    {
      name: "instr in EX",
      kind: "bus",
      values: ["split x6", "split x6", "split x6"],
    },
    { name: "hz", kind: "bit", values: [1, 0, 0] },
    { name: "warm cycle counted", kind: "bit", values: [0, 1, 1], mark: [0] },
    {
      name: "v1_rd",
      kind: "bus",
      values: ["—", "OLD x6", "NEW x6"],
      mark: [2],
    },
    {
      name: "",
      kind: "text",
      values: [
        "hazard stands — NOT counted",
        "one warm cycle",
        "split COMMITS — NEW x6",
      ],
    },
  ],
  notes: [
    {
      text: "One rule covers every class — do not count the cycles the hazard stands:  else if (f2_valid && !vt_stall && warm_q != 4'hF) warm_q <= warm_q + 1.",
      tone: "good",
    },
    {
      text: "warm_q is a COUNTER, not a bit, because the classes owe different numbers — and the three counts are constants, so each test is a pure decode of a register and only a 3:1 select is left downstream of the instruction. Compare first, select after.",
      tone: "good",
    },
  ],
};

const staleMask = [
  {
    title: "split x6 — the predicate reads STALE",
    mask: [0, 0, 0, 0, 0, 0, 0, 0],
    note: "v1_rd read as zero, so t_set was empty. mask ← t_set = 0000_0000 — kht_unit reported an ALL-ZERO ACTIVE MASK, twice. The TRUE body writes nothing.",
  },
  {
    title: "join — and the else-branch takes everything",
    mask: [1, 1, 1, 1, 1, 1, 1, 1],
    note: "mask ← f_set = 1111_1111. EVERY lane takes the else-branch. The assertion kht_unit carries for an all-zero mask is what named this in one line — it was written because “the scheduler cannot issue such a wave” was an argument rather than a check.",
  },
];

// ------------------------------------------------------- mask = write enable
const maskWe = {
  nodes: [
    { id: "v0", x: 0, y: 0, w: 9.5, label: "kht_valu", sub: "lane 0" },
    { id: "v1", x: 10.5, y: 0, w: 9.5, label: "kht_valu", sub: "lane 1" },
    { id: "v2", x: 21, y: 0, w: 9.5, label: "kht_valu", sub: "lane 2" },
    { id: "v3", x: 31.5, y: 0, w: 9.5, label: "kht_valu", sub: "lane 3" },
    {
      id: "w0",
      x: 0,
      y: 6,
      w: 9.5,
      label: "we = 1",
      sub: "w_mask[0]",
      accent: true,
    },
    {
      id: "w1",
      x: 10.5,
      y: 6,
      w: 9.5,
      label: "we = 1",
      sub: "w_mask[1]",
      accent: true,
    },
    { id: "w2", x: 21, y: 6, w: 9.5, label: "we = 0", sub: "w_mask[2]" },
    { id: "w3", x: 31.5, y: 6, w: 9.5, label: "we = 0", sub: "w_mask[3]" },
    { id: "b0", x: 0, y: 12, w: 9.5, label: "x[rd] bank", sub: "written" },
    { id: "b1", x: 10.5, y: 12, w: 9.5, label: "x[rd] bank", sub: "written" },
    { id: "d2", x: 21, y: 12, w: 9.5, label: "dropped", sub: "no write" },
    { id: "d3", x: 31.5, y: 12, w: 9.5, label: "dropped", sub: "no write" },
  ],
  edges: [
    { from: "v0:b", to: "w0:t", dir: "v" },
    { from: "v1:b", to: "w1:t", dir: "v" },
    { from: "v2:b", to: "w2:t", dir: "v" },
    { from: "v3:b", to: "w3:t", dir: "v" },
    { from: "w0:b", to: "b0:t", dir: "v", accent: true },
    { from: "w1:b", to: "b1:t", dir: "v", accent: true },
    { from: "w2:b", to: "d2:t", dir: "v", dash: true },
    { from: "w3:b", to: "d3:t", dir: "v", dash: true },
  ],
  groups: [
    {
      x: -0.8,
      y: -1,
      w: 42.6,
      h: 5.2,
      label: "ALWAYS computes — every lane, masked or not",
    },
  ],
};

// ------------------------------------------------------------- IPDOM stack
const pairWord = [
  {
    name: "outer mask",
    bits: 8,
    value: "taken by the 2nd join · phase 1",
    accent: true,
  },
  {
    name: "false-lane mask",
    bits: 8,
    value: "taken by the 1st join · phase 0",
  },
];

const stackMem = {
  nodes: [
    { id: "sp", x: 0, y: 0, w: 11, label: "sp − 1", sub: "rd_a = base + sp−1" },
    { id: "spw", x: 0, y: 5, w: 11, label: "sp", sub: "wr_a = base + sp" },
    {
      id: "base",
      x: 0,
      y: 10,
      w: 11,
      label: "base = wave × PAIRS",
      sub: "one region per wave",
    },
    {
      id: "mem",
      x: 14,
      y: 2.5,
      w: 15,
      h: 7,
      label: "kohaku_sdpram",
      sub: "distributed · READ_LAT 0 · DEPTH = WAVES × PAIRS",
      accent: true,
    },
    {
      id: "phase",
      x: 14,
      y: 12,
      w: 15,
      label: "phase bit per wave",
      sub: "which half the next join takes",
    },
    {
      id: "join",
      x: 32,
      y: 2.5,
      w: 11,
      label: "a join costs NO cycle",
      sub: "combinational read",
      accent: true,
    },
  ],
  edges: [
    { from: "sp:r", to: "mem:l", dir: "h", label: "read" },
    { from: "spw:r", to: "mem:l", dir: "h", label: "write" },
    { from: "base:r", to: "mem:l", dir: "h" },
    { from: "mem:r", to: "join:l", dir: "h", accent: true },
    { from: "phase:t", to: "mem:b", dir: "v" },
  ],
};

const OUTER = [1, 1, 1, 1, 1, 1, 1, 1];
const TRUE_SET = [0, 1, 0, 1, 0, 1, 0, 1];
const FALSE_SET = [1, 0, 1, 0, 1, 0, 1, 0];

const PAIR0 = [{ i: "pair 0", outer: "11111111", fals: "10101010" }];
const WRITES = ["✓", "✓", "✓", "✓", "✓", "✓", "✓", "✓"];

const ipdom = [
  {
    title: "before the split — reconverged",
    instr: "add x5, x3, x4",
    note: "Ordinary RV32I, and every lane retires its write. sp = 0, phase = 0, the stack is empty. This is the state a wave starts in and the state it must come back to.",
    mask: OUTER,
    pred: null,
    active: 8,
    stack: [],
    sp: "0",
    phase: "0",
  },
  {
    title: "split — the predicate is read per lane",
    instr: "split x6",
    note: "The per-lane predicate in x6: odd lanes true, even lanes false. Nothing has been pushed yet — this is the value the split is about to act on, and reading it one cycle too early is Trap 6 above.",
    mask: OUTER,
    pred: [0, 1, 0, 1, 0, 1, 0, 1],
    active: 8,
    stack: [],
    sp: "0",
    phase: "0",
  },
  {
    title: "split — ONE write pushes the PAIR",
    instr: "split x6",
    note: "pair0 ← { outer = 11111111, false = 10101010 }. Two pushes are one write, so a single write port suffices. mask ← t_set, and utilisation drops to 4/8 — half the lanes are struck through: they still compute, their writes are dropped.",
    mask: TRUE_SET,
    pred: null,
    active: 4,
    stack: PAIR0,
    sp: "1",
    phase: "0",
  },
  {
    title: "the IF body runs on the odd lanes",
    instr: "sw x5, 0(x7)   ← the IF body",
    note: "The even lanes compute whatever they compute and their writes are dropped. Nothing about the stack moves while a body runs.",
    mask: TRUE_SET,
    pred: null,
    active: 4,
    stack: PAIR0,
    sp: "1",
    phase: "0",
  },
  {
    title: "join — phase 0 takes the FALSE half",
    instr: "join",
    note: "mask ← pair0.false = 10101010. sp stays at 1 and phase becomes 1: the pair still has one half left, so nothing is popped yet. This is why a split costs two entries and a join pops one.",
    mask: FALSE_SET,
    pred: null,
    active: 4,
    stack: PAIR0,
    sp: "1",
    phase: "1",
  },
  {
    title: "the ELSE body runs on the even lanes",
    instr: "sw x9, 0(x7)   ← the ELSE body",
    note: "Same instruction stream, the complementary lanes. The two halves of the branch are serialised — which is the whole cost of divergence, and it is throughput, never correctness.",
    mask: FALSE_SET,
    pred: null,
    active: 4,
    stack: PAIR0,
    sp: "1",
    phase: "1",
  },
  {
    title: "join — phase 1 takes the OUTER mask, and pops",
    instr: "join",
    note: "mask ← pair0.outer = 11111111. sp = 0, phase = 0. Reconverged, and the pointer is back where it started — which is exactly what simt_nested.s proves, at two levels.",
    mask: OUTER,
    pred: null,
    active: 8,
    stack: [],
    sp: "0",
    phase: "0",
  },
];

const stackCols = [
  { key: "i", label: "entry", mono: true },
  { key: "outer", label: "outer mask", mono: true },
  { key: "fals", label: "false-lane mask", mono: true },
];

// ---------------------------------------------------------------- hazards
const hzTrace = {
  rows: [
    { name: "EX", kind: "bus", values: ["B", "·", "·"] },
    { name: "MEM", kind: "bus", values: ["M", "B", "·"] },
    { name: "WB", kind: "bus", values: ["W", "M", "B"] },
    {
      name: "",
      kind: "text",
      values: [
        "B reads the array",
        "M writes at END of this cycle",
        "W wrote at END of cycle 0",
      ],
    },
  ],
  notes: [
    {
      text: "TWO distances, not one, because the lane ALU has a writeback stage: a write lands two cycles after its read was issued, so an EX read misses BOTH the write happening at the end of this cycle and the one a cycle behind it. B stalls against M (distance 1) AND against W (distance 2).",
    },
    {
      text: "A stalled cycle inserts a BUBBLE rather than freezing the stage, which is what lets the hazard clear — and is exactly what Trap 3 and Trap 5 both violate.",
      tone: "good",
    },
  ],
};

// ------------------------------------------------------- register-in-front
const flopFront = {
  cols: [
    { key: "w", label: "What", mono: true },
    { key: "was", label: "Started at" },
    { key: "cost", label: "Clock-to-out", mono: true, align: "right" },
    { key: "now", label: "Now starts at" },
    { key: "won", label: "Worth", mono: true, align: "right" },
  ],
  rows: [
    {
      w: "decode",
      was: "the window, between it and the control registers",
      cost: "—",
      now: "the memory's <b>write</b> path (kht_predec)",
      won: "+55 MHz",
      _tone: "good",
    },
    {
      w: "the instruction",
      was: "a RAMB36E2 output",
      cost: "0.909 ns",
      now: "a fabric register (<code>i2</code>/<code>c2</code>)",
      won: "+22 MHz",
      _tone: "good",
    },
    {
      w: "the lane ALU",
      was: "kht_vregfile, a RAMB18E2",
      cost: "0.845 ns",
      now: "<code>w1_q</code>/<code>w2_q</code>",
      won: "+6 MHz",
      _tone: "good",
    },
    {
      w: "the scalar ALU",
      was: "a 512-entry distributed RAM read, out of a block RAM's output",
      cost: "2.15 ns",
      now: "<code>a1_q</code>/<code>a2_q</code> — the register moved <b>in front</b>",
      won: "—",
      _tone: "good",
    },
    {
      w: "every lane's address",
      was: "indexed off the vector file per lane",
      cost: "0.845 ns",
      now: "computed once into <code>ea_all_q</code>",
      won: "+28 MHz",
      _tone: "good",
    },
    {
      w: "<code>lane_on</code>",
      was: "a 16:1 mux into l1_req → l1_stall → hold",
      cost: "—",
      now: "registered in the walk's phase 0",
      won: "<b>+50 MHz</b>",
      _tone: "good",
    },
    {
      w: "the round-robin pick",
      was: "5 LUT levels into the window's address",
      cost: "—",
      now: "a register (<code>cur_q</code>)",
      won: "+4 MHz",
      _tone: "good",
    },
    {
      w: "the branch's zero test",
      was: "a 32-bit reduce on the file read",
      cost: "—",
      now: "a <b>stored bit</b> — the file is <code>reg [32:0]</code>",
      won: "—",
      _tone: "good",
    },
  ],
};

const salu = {
  nodes: [
    {
      id: "in",
      x: 0,
      y: 6,
      w: 12,
      label: "a1_q, a2_q",
      sub: "REGISTERED operands",
    },
    { id: "add", x: 16, y: 0, w: 12, label: "s_add", sub: "CARRY8 ×4" },
    { id: "cmp", x: 16, y: 4.5, w: 12, label: "s_lt / s_ltu", sub: "compare" },
    { id: "shf", x: 16, y: 9, w: 12, label: "s_shf", sub: "barrel, a_sh" },
    { id: "log", x: 16, y: 13.5, w: 12, label: "s_log", sub: "xor / or / and" },
    {
      id: "mux",
      x: 32,
      y: 6,
      w: 10,
      label: "5-way, 2 steps",
      sub: "LUT6 + MUXF7",
      accent: true,
    },
    {
      id: "out",
      x: 45,
      y: 6,
      w: 12,
      label: "sfile[aad_q]",
      sub: "{zero, result}",
      accent: true,
    },
  ],
  edges: [
    { from: "in:r", to: "add:l", dir: "h" },
    { from: "in:r", to: "cmp:l", dir: "h" },
    { from: "in:r", to: "shf:l", dir: "h" },
    { from: "in:r", to: "log:l", dir: "h" },
    { from: "add:r", to: "mux:l", dir: "h" },
    { from: "cmp:r", to: "mux:l", dir: "h" },
    { from: "shf:r", to: "mux:l", dir: "h" },
    { from: "log:r", to: "mux:l", dir: "h" },
    { from: "mux:r", to: "out:l", dir: "h", accent: true },
  ],
};

const zeroFlag = {
  cols: [
    { key: "w", label: "Where the zero test was tried" },
    { key: "r", label: "Result", align: "right" },
  ],
  rows: [
    {
      w: "a separate <code>reg [WAVES*32-1:0]</code> shadow array",
      r: "+702 LUT, +512 FF, <b>−3.7 MHz</b> — the indexed-flop-array anti-pattern, for the fourth time",
      _tone: "bad",
    },
    {
      w: "<code>sres == 0</code> <b>after</b> the ALU, with the ALU still behind the file read",
      r: "one endpoint, 17 levels, <b>−23 MHz</b>",
      _tone: "bad",
    },
    {
      w: "<code>sres == 0</code> after the ALU, with the ALU <b>in front</b> of the file read",
      r: "<b>built</b> — the cone starts at <code>a1_q</code>/<code>a2_q</code>, so ALU + compare + write fits",
      _tone: "good",
    },
  ],
};

// -------------------------------------------------------------------- LSU
const lsuSm = {
  states: [
    { id: "IDLE", x: 0, y: 0, label: "IDLE" },
    { id: "RUN", x: 7, y: -2.6, label: "RUN" },
    { id: "LDSW", x: 7, y: 2.6, label: "LDS", sub: "wait" },
    { id: "DRAIN", x: 14, y: 0, label: "DRAIN" },
    { id: "DONE", x: 21, y: 0, label: "DONE" },
  ],
  edges: [
    { from: "IDLE", to: "RUN", label: "!all_lds_q" },
    { from: "IDLE", to: "LDSW", label: "all_lds_q" },
    { from: "RUN", to: "RUN", label: "l1_stall", self: true },
    { from: "RUN", to: "DRAIN", label: "lane_last" },
    { from: "LDSW", to: "DRAIN", label: "lds_done" },
    { from: "DRAIN", to: "DONE" },
    { from: "DONE", to: "IDLE", label: "go — retires", curve: -110 },
  ],
};

const lsuPhases = {
  rows: [
    { name: "ph_q", kind: "bus", values: ["0", "1", "2", "0", "1", "2"] },
    { name: "ln_q", kind: "bus", values: ["2", "2", "2", "3", "3", "3"] },
    {
      name: "l1_probe",
      kind: "bus",
      values: ["ea(2)", null, null, "ea(3)", null, null],
    },
    {
      name: "l1_addr",
      kind: "bus",
      values: [null, "ea_r = a2", null, null, "ea_r = a3", null],
    },
    { name: "l1_req", kind: "bit", values: [0, 1, 0, 0, 1, 0] },
    {
      name: "l1_rdata",
      kind: "bus",
      values: [null, null, "d2", null, null, "d3"],
    },
    {
      name: "capture",
      kind: "bus",
      values: [null, null, "ld_buf[2]", null, null, "ld_buf[3]"],
      mark: [2, 5],
    },
    {
      name: "",
      kind: "text",
      values: [
        "register the address",
        "issue; a miss HOLDS here",
        "take the word",
        "",
        "",
        "",
      ],
    },
  ],
  notes: [
    {
      cycle: 0,
      text: "Phase 0 exists for TIMING, and it is the assembled PE's binding path that bought it: ea combinational into l1_addr put the 32-bit adder, rv_l1's tag compare and stall in ONE cycle — and stall feeds hold, which gates every register in the core.",
      tone: "good",
    },
    {
      cycle: 2,
      text: "Phase 2 exists for CORRECTNESS: l1_rdata is the data for the access one cycle earlier, and rv_l1 drops it the moment a NEW request misses. So the word is taken on a cycle with no request in flight.",
      tone: "good",
    },
    {
      text: "A STORE takes two phases, not three: it has nothing to collect, so phase 1 retires the lane directly.",
    },
  ],
};

const lsuBroken = {
  rows: [
    { name: "ln_q", kind: "bus", values: ["2", "3", "3"] },
    { name: "address", kind: "bus", values: ["a2", "a3 (MISS)", "a3"] },
    { name: "l1_rdata", kind: "bus", values: ["d1", "0000", "…"], mark: [1] },
    {
      name: "capture",
      kind: "bus",
      values: ["d1 → buf[1]", "0000 → buf[2]", "…"],
      mark: [1],
    },
  ],
  notes: [
    {
      cycle: 1,
      text: "ONE CYCLE PER LANE: the cycle in which lane n's word is readable is the same cycle lane n+1's address goes out, so a miss there destroys the word before it is captured. Lane 2 silently reads ZERO.",
      tone: "bad",
    },
    {
      text: "The observed signature was exactly that — only the lanes that immediately PRECEDED a miss came back zero, 2, 4 and 7 of eight, which reads like random data corruption until the lines are lined up against the misses.",
      tone: "bad",
    },
  ],
};

const l1Rules = {
  cols: [
    { key: "n", label: "#", mono: true, align: "right" },
    {
      key: "r",
      label:
        "rv_l1's read interface — three properties that only matter together",
    },
  ],
  rows: [
    {
      n: "1",
      r: "<code>l1_rdata(k)</code> is the data for the access at <code>k−1</code> — a registered read",
    },
    {
      n: "2",
      r: "a <b>MISS on a new address zeroes <code>l1_rdata</code> immediately</b>",
    },
    {
      n: "3",
      r: "a miss completes <b>only while <code>l1_req</code> stands</b> — a held handshake",
    },
  ],
};

const ADDRS = [
  "0x0040",
  "0x0044",
  "0x0100",
  "0x0048",
  "0x0104",
  "0x004C",
  "0x0240",
  "0x0108",
];
const LINES = [
  "0x040",
  "0x040",
  "0x100",
  "0x040",
  "0x100",
  "0x040",
  "0x240",
  "0x100",
];

const coalesce = [
  {
    title: "the request set — eight lanes, eight addresses",
    note: "Every address is 0x8000_0000 + the offset shown, because the base is scalar — and that is exactly what the uniform-base form buys: the compare is over OFFSET FIELDS and the high bits cannot differ. It is narrower, never skipped. A uniform base does not imply contiguous offsets, so this is still a scatter. A line is 32 bytes, which is one NoC/MAG payload.",
    mask: [1, 1, 1, 1, 1, 1, 1, 1],
    pending: [1, 1, 1, 1, 1, 1, 1, 1],
    role: ["—", "—", "—", "—", "—", "—", "—", "—"],
    req: 0,
  },
  {
    title: "pass 1 — the lowest pending lane is the LEADER",
    note: "Lane 0 leads with line 0x040. Lanes 1, 3 and 5 match it and become followers: four lanes, ONE request.",
    mask: [1, 1, 1, 1, 1, 1, 1, 1],
    pending: [0, 0, 1, 0, 1, 0, 1, 1],
    role: [
      "LEADER",
      "follow",
      "wait",
      "follow",
      "wait",
      "follow",
      "wait",
      "wait",
    ],
    req: 1,
  },
  {
    title: "pass 2 — lane 2 leads what is left",
    note: "Line 0x100. Lanes 4 and 7 follow. Three lanes, one request.",
    mask: [1, 1, 1, 1, 1, 1, 1, 1],
    pending: [0, 0, 0, 0, 0, 0, 1, 0],
    role: [
      "done",
      "done",
      "LEADER",
      "done",
      "follow",
      "done",
      "wait",
      "follow",
    ],
    req: 2,
  },
  {
    title: "pass 3 — lane 6 is alone",
    note: "Line 0x240, no followers. Eight lanes have been served by THREE requests instead of eight. Today the same access takes eight walks of one lane each: 8 requests over 1 gather, which is LANES by construction.",
    mask: [1, 1, 1, 1, 1, 1, 1, 1],
    pending: [0, 0, 0, 0, 0, 0, 0, 0],
    role: ["done", "done", "done", "done", "done", "done", "LEADER", "done"],
    req: 3,
  },
];

const storeData = {
  cols: [
    { key: "k", label: "" },
    { key: "rv", label: "RV32I   sw rs2, imm(rs1)", mono: true },
    { key: "vm", label: "vmem   vsinw2 x7, s1", mono: true },
  ],
  rows: [
    { k: "base", rv: "x[rs1] <b>per lane</b>", vm: "s[ss1] — <b>SCALAR</b>" },
    { k: "data", rv: "x[rs2] per lane", vm: "x[rd] — <b>the rd FIELD</b>" },
    {
      k: "offset",
      rv: "the immediate",
      vm: "lane <span class='opacity-60'>(lane-linear)</span>",
    },
  ],
};

const counters = {
  cols: [
    { key: "when", label: "" },
    { key: "r", label: "req_ctr / gather_ctr", mono: true, align: "right" },
    { key: "what", label: "" },
  ],
  rows: [
    {
      when: "today, on hardware",
      r: "8 / 1",
      what: "= LANES — the serial walk",
      _tone: "warn",
    },
    {
      when: "with G5",
      r: "1 / 1",
      what: "lanes agree → one request · <b>PROJECTED, G5 is not built</b>",
      _tone: "warn",
    },
  ],
};

// -------------------------------------------------------------- reductions
const redux = {
  cols: [
    { key: "i", label: "Shape", mono: true },
    { key: "d", label: "Depth" },
    { key: "n", label: "Registered?" },
  ],
  rows: [
    {
      i: "ballot",
      d: "one OR per lane, then AND with the mask — <b>one level</b>",
      n: "no",
    },
    {
      i: "vreadfirst",
      d: "a tree of 32-bit <b>muxes</b>, log2(LANES) deep, left subtree preferred so the lowest ACTIVE lane wins by construction",
      n: "no",
    },
    {
      i: "redux*",
      d: "a tree of 32-bit ADD / MIN / MAX / AND / OR, log2(LANES) deep",
      n: "<b>yes — every level, and the leaves</b>",
      _tone: "good",
    },
  ],
};

const reduxHist = {
  cols: [
    { key: "f", label: "Form", mono: true },
    { key: "lv", label: "Levels", mono: true, align: "right" },
    { key: "c", label: "CARRY8", mono: true, align: "right" },
    { key: "lut", label: "kht_core LUT", mono: true, align: "right" },
    { key: "f2", label: "kht_core Fmax", mono: true, align: "right" },
  ],
  rows: [
    {
      f: "a sequential loop — a CHAIN",
      lv: "44",
      c: "9",
      lut: "7,899",
      f2: "<b>71.7 MHz</b>",
      _tone: "bad",
    },
    {
      f: "a balanced tree",
      lv: "21",
      c: "6",
      lut: "8,498",
      f2: "154.1 MHz",
      _tone: "warn",
    },
    {
      f: "a <b>pipelined</b> tree — built",
      lv: "—",
      c: "—",
      lut: "<b>7,754</b>",
      f2: "<b>277.9 MHz</b>",
      _tone: "good",
    },
  ],
};

const identities = {
  cols: [
    { key: "op", label: "Operation", mono: true },
    { key: "id", label: "An inactive lane contributes", mono: true },
  ],
  rows: [
    { op: "reduxadd / reduxor", id: "0x0000_0000" },
    { op: "reduxand", id: "0xFFFF_FFFF" },
    {
      op: "reduxmin",
      id: "0x7FFF_FFFF <span class='opacity-60'>— the largest signed</span>",
      _tone: "warn",
    },
    {
      op: "reduxmax",
      id: "0x8000_0000 <span class='opacity-60'>— the smallest signed</span>",
      _tone: "warn",
    },
  ],
};

// ------------------------------------------------------------- banked LDS
const ldsBanks = {
  nodes: [
    { id: "w", x: 0, y: 0, w: 13, label: "addr", sub: "linear word address" },
    {
      id: "bk",
      x: 16,
      y: 0,
      w: 13,
      label: "addr[LNW-1:0]",
      sub: "is the BANK",
      accent: true,
    },
    {
      id: "row",
      x: 16,
      y: 4.5,
      w: 13,
      label: "addr >> LNW",
      sub: "is the ROW within it",
    },
    { id: "b0", x: 33, y: -1.5, w: 7, label: "b0", sub: "rv_spad" },
    { id: "b1", x: 41, y: -1.5, w: 7, label: "b1", sub: "rv_spad" },
    { id: "bd", x: 49, y: -1.5, w: 5, label: "…" },
    { id: "b7", x: 55, y: -1.5, w: 7, label: "b7", sub: "rv_spad" },
    {
      id: "res",
      x: 33,
      y: 5.5,
      w: 15,
      label: "the resolver",
      sub: "LANES × LANES, lowest lane wins",
      accent: true,
    },
    {
      id: "xb",
      x: 50,
      y: 5.5,
      w: 12,
      label: "return crossbar",
      sub: "bank k → the lane that asked",
    },
  ],
  edges: [
    { from: "w:r", to: "bk:l", dir: "h", accent: true },
    { from: "w:r", to: "row:l", dir: "h" },
    { from: "bk:r", to: "b0:l", dir: "h" },
    { from: "bk:r", to: "b1:l", dir: "h" },
    { from: "bk:r", to: "b7:l", dir: "h" },
    { from: "res:r", to: "xb:l", dir: "h", accent: true },
  ],
  groups: [{ x: 32, y: -2.6, w: 30, h: 5.4, label: "WORDS/LANES deep each" }],
};

const ldsPasses = {
  cols: [
    { key: "a", label: "Access", mono: true },
    { key: "b", label: "Banks touched" },
    { key: "p", label: "Passes", align: "right", mono: true },
  ],
  rows: [
    {
      a: "lane i → word i",
      b: "8 distinct — conflict-free",
      p: "<b>1</b>",
      _tone: "good",
    },
    {
      a: "lane i → word 7−i",
      b: "8 distinct — reversed",
      p: "<b>1</b>",
      _tone: "good",
    },
    {
      a: "lane i → word 8i",
      b: "all bank 0 — the worst case",
      p: "<b>8</b>",
      _tone: "bad",
    },
  ],
};

// -------------------------------------------------------------- butterfly
const butterfly = {
  rows: [
    {
      name: "stage 0 · dist 1 · ctl[0]=1",
      kind: "text",
      values: ["0↔1", "2↔3", "4↔5", "6↔7"],
    },
    {
      name: "stage 1 · dist 2 · ctl[1]=0",
      kind: "text",
      values: ["—", "—", "—", "—"],
    },
    {
      name: "stage 2 · dist 4 · ctl[2]=1",
      kind: "text",
      values: ["0↔4", "1↔5", "2↔6", "3↔7"],
    },
  ],
  notes: [
    {
      text: "LANES = 8, ctl = 5 (binary 101). Stage k conditionally swaps across distance 2^k under bit k of that lane's control, so lane 0 ends holding vs1[5] = 0 ^ 5. That is O(LANES · log LANES) where an all-to-all select is O(LANES²) — the shape kht_lds' resolver pays for.",
    },
    {
      text: "ONE network serves both instructions. Lane i must end up holding vs1[src], so the per-lane control is src ^ i either way: shflxor gives src = i ^ m, and bcast gives src = L.",
      tone: "good",
    },
  ],
};

// ------------------------------------------------ float tier + multiplier
const shadow = {
  nodes: [
    { id: "w1", x: 0, y: 0, w: 10, label: "w1_q", sub: "vs1" },
    { id: "w2", x: 0, y: 4, w: 10, label: "w2_q", sub: "vs2" },
    { id: "w3", x: 0, y: 8, w: 10, label: "w3_q", sub: "vd — the addend" },
    {
      id: "fpu",
      x: 14,
      y: 1,
      w: 14,
      label: "kht_fpu",
      sub: "FLANES × rv_fpu · FSFU_UNITS of them with a khs_fp32_sfu beside",
      accent: true,
    },
    {
      id: "imul",
      x: 14,
      y: 7,
      w: 14,
      label: "kht_imul",
      sub: "8 × 33×33 signed",
      accent: true,
    },
    {
      id: "sh",
      x: 14,
      y: 13.5,
      w: 14,
      label: "fsh_* shadow pipe",
      sub: "valid · wa · mask · wave",
    },
    {
      id: "sel",
      x: 32,
      y: 4,
      w: 12,
      label: "fsh_mul[FLAT]",
      sub: "which unit retires",
      accent: true,
    },
    {
      id: "port",
      x: 48,
      y: 4,
      w: 13,
      label: "the VRF write port",
      sub: "a MUX, never an arbitration",
      accent: true,
    },
    {
      id: "soon",
      x: 32,
      y: 13.5,
      w: 12,
      label: "f_soon = fsh_v[FLAT-2]",
      sub: "two cycles of warning",
    },
  ],
  edges: [
    { from: "w1:r", to: "fpu:l", dir: "h" },
    { from: "w2:r", to: "fpu:l", dir: "h" },
    { from: "w3:r", to: "fpu:l", dir: "h" },
    { from: "w1:r", to: "imul:l", dir: "h" },
    { from: "w2:r", to: "imul:l", dir: "h" },
    { from: "fpu:r", to: "sel:l", dir: "h" },
    { from: "imul:r", to: "sel:l", dir: "h" },
    { from: "sh:r", to: "sel:l", dir: "h" },
    { from: "sel:r", to: "port:l", dir: "h", accent: true },
    { from: "sh:r", to: "soon:l", dir: "h" },
    { from: "soon:t", to: "sel:b", dir: "v", dash: true },
  ],
};

const fpendTrace = {
  rows: [
    {
      name: "instr in EX",
      kind: "bus",
      values: ["vfma w0", "…", "…", "…", "vfma w0"],
    },
    { name: "fpend[w0]", kind: "bit", values: [1, 1, 1, 1, 0], mark: [0] },
    { name: "rdy[w0]", kind: "bit", values: [0, 0, 0, 0, 1] },
    { name: "redirect", kind: "bit", values: [1, 0, 0, 0, 0], mark: [0] },
    {
      name: "nxt[w0]",
      kind: "bus",
      values: ["pc+4", "pc+4", "pc+4", "pc+4", "pc+4"],
    },
    { name: "fwb_v", kind: "bit", values: [0, 0, 0, 1, 0], mark: [3] },
    {
      name: "",
      kind: "text",
      values: [
        "set at ISSUE",
        "other waves issue",
        "…",
        "result retires",
        "w0 runnable again",
      ],
    },
  ],
  notes: [
    {
      cycle: 0,
      text: "fpend is set at ISSUE, not at launch: EX is two stages ahead of the launch, and the wave has to be blocked by then. The scheduler reads fpend and skips those waves — rdy = live & ~fpend.",
      tone: "good",
    },
    {
      cycle: 0,
      text: "And the float REDIRECTS ITS OWN WAVE to pc+4. fpend blocks FETCH, but the front end is three deep, so two instructions of that wave are already in flight when the float issues — measured as three dependent vfma launching in consecutive cycles with stale addends. Treating the float as a redirect kills exactly those two.",
      tone: "good",
    },
    {
      text: "The cycle count here is illustrative: FLAT is the tier's own latency — 6 with no seed units built and 10 with them — drawn short so the mechanism is visible in one screen.",
    },
  ],
};

const notScoreboard = {
  cols: [
    { key: "k", label: "" },
    { key: "v", label: "" },
  ],
  rows: [
    {
      k: "<b>The invariant the machine is built on</b>",
      v: "with <code>WAVES ≥ pipeline depth</code>, no two in-flight instructions share a wave — so forwarding and interlocks are <b>deleted, not optimised</b>",
    },
    {
      k: "<b>What a 15-cycle unit does to it</b>",
      v: "breaks the precondition: a wave's next instruction can now reach EX while its own float is still in flight",
    },
    {
      k: "<b>What restores it</b>",
      v: "<b>one bit per wave</b>. Not a scoreboard — no per-register tracking, no out-of-order retire, both of which are refused elsewhere in this machine",
      _tone: "good",
    },
    {
      k: "<b>And it is occupancy-dependent</b>",
      v: "sixteen runnable waves give a 16-cycle round trip against 15 of latency; at four the round trip is 4 and a dependent float waits out the remaining ~11. It degrades into the simple-stall case — but a low-occupancy kernel <i>looks</i> like a bug unless this is read first",
      _tone: "warn",
    },
  ],
};

const measuredLatency = {
  cols: [
    { key: "l", label: "simt_float.s launched", align: "right", mono: true },
    { key: "c", label: "cycles", align: "right", mono: true },
    { key: "w", label: "work", align: "right", mono: true },
  ],
  rows: [
    { l: "1", c: "1,274", w: "1×" },
    { l: "16", c: "3,731", w: "<b>16×</b>", _tone: "good" },
  ],
};

// ------------------------------------------------------------ kick / halt
const kick = {
  nodes: [
    {
      id: "inst",
      x: 0,
      y: 0,
      w: 13,
      label: "CU_INST",
      sub: "the instruction FIFO",
    },
    {
      id: "data",
      x: 0,
      y: 5.5,
      w: 13,
      label: "CU_DATA",
      sub: "the receive queue",
    },
    {
      id: "kfsm",
      x: 17,
      y: 0,
      w: 13,
      label: "the kick FSM",
      sub: "K_IDLE waits",
      accent: true,
    },
    {
      id: "gw",
      x: 17,
      y: 5.5,
      w: 13,
      label: "the granule walk",
      sub: "8 words per flit",
    },
    {
      id: "quiet",
      x: 34,
      y: 3,
      w: 13,
      label: "rx_quiet",
      sub: "cleared by OUR OWN progress",
      accent: true,
    },
    {
      id: "boot",
      x: 51,
      y: 0,
      w: 12,
      label: "boot_v",
      sub: "op = the wave count",
      accent: true,
    },
  ],
  edges: [
    { from: "inst:r", to: "kfsm:l", dir: "h" },
    { from: "data:r", to: "gw:l", dir: "h" },
    { from: "gw:r", to: "quiet:l", dir: "h" },
    { from: "quiet:t", to: "kfsm:b", dir: "v", accent: true },
    { from: "kfsm:r", to: "boot:l", dir: "h", accent: true },
  ],
};

const haltSm = {
  states: [
    { id: "H_RUN", x: 0, y: 0, label: "H_RUN" },
    { id: "H_FLUSH", x: 7, y: 0, label: "H_FLUSH" },
    { id: "H_RISE", x: 14, y: 0, label: "H_RISE" },
    { id: "H_FALL", x: 21, y: 0, label: "H_FALL" },
    { id: "H_DONE", x: 28, y: 0, label: "H_DONE" },
  ],
  edges: [
    { from: "H_RUN", to: "H_FLUSH", label: "ecall / fault" },
    { from: "H_FLUSH", to: "H_RISE", label: "l1_flush PULSE" },
    { from: "H_RISE", to: "H_FALL", label: "busy RISES" },
    { from: "H_FALL", to: "H_DONE", label: "busy FALLS" },
  ],
};

const haltTrace = {
  rows: [
    {
      name: "hst",
      kind: "bus",
      values: ["H_RUN", "H_FLUSH", "H_RISE", "H_FALL", "H_FALL", "H_DONE"],
    },
    { name: "live", kind: "bit", values: [1, 0, 0, 0, 0, 0] },
    { name: "l1_flush", kind: "bit", values: [0, 1, 0, 0, 0, 0], mark: [1] },
    {
      name: "l1_flush_busy",
      kind: "bit",
      values: [0, 0, 1, 1, 0, 0],
      mark: [1],
    },
    { name: "halted", kind: "bit", values: [0, 0, 0, 0, 0, 1] },
    { name: "go", kind: "bit", values: [1, 0, 0, 0, 0, 0] },
  ],
  notes: [
    {
      cycle: 1,
      text: "rv_l1 acts on `flush` in L_IDLE only and ASSERTS if it is raised later, so the request must be a PULSE — and busy does not rise in the pulse cycle, which is why the wait is split in two.",
    },
    {
      text: "go is gated on hst == H_RUN, because the instruction already in the window behind an ecall would otherwise retire too and overwrite the cause and the halt word with its own.",
    },
    {
      text: "This trace is the documented state sequence drawn out, not a captured waveform — the source states the rule and the state list, and the number of cycles busy stays high is the L1's, not fixed.",
    },
  ],
};

const completion = {
  cols: [
    { key: "t", label: "The completion means", mono: true },
    { key: "w", label: "Why that term is there" },
  ],
  rows: [
    {
      t: "core_halted",
      w: "the last live wave has retired, and the L1 flush has finished",
    },
    {
      t: "pipe_empty",
      w: "<code>!f1_valid && !f2_valid && !lsu_busy</code> — <b>f1 too</b>: with two fetches in flight, a completion built while f1 still carries one reports a unit that has not finished executing",
    },
    { t: "req_idle", w: "the requestor has nothing outstanding" },
    {
      t: "wr_out == 0",
      w: "every write this shader issued has been <b>ACKNOWLEDGED</b>, not merely sent. The completion is the host's only sequencing point",
      _tone: "good",
    },
  ],
};

const haltWord = {
  nodes: [
    {
      id: "wb",
      x: 0,
      y: 0,
      w: 11,
      label: "the VRF write port",
      sub: "not the MEM stage's intent",
    },
    { id: "isa0", x: 13.5, y: 0, w: 10, label: "rd == x10 ?", sub: "a0" },
    {
      id: "lane0",
      x: 26,
      y: 0,
      w: 10,
      label: "mask[0] ?",
      sub: "lane 0 active",
    },
    {
      id: "a0",
      x: 38.5,
      y: 0,
      w: 10,
      label: "a0_q",
      sub: "the halt word",
      accent: true,
    },
  ],
  edges: [
    { from: "wb:r", to: "isa0:l", dir: "h" },
    { from: "isa0:r", to: "lane0:l", dir: "h" },
    { from: "lane0:r", to: "a0:l", dir: "h", accent: true },
  ],
};

// ----------------------------------------------------------------- gates
const noDatapath = {
  cols: [
    { key: "i", label: "Encoded", mono: true },
    { key: "needs", label: "Needs" },
    { key: "does", label: "What a build without it does" },
  ],
  rows: [
    {
      i: "shflxor · bcast",
      needs: "the subgroup butterfly — <code>HAS_SHFL</code>",
      does: "<b>illegal → FAULT (cause 3)</b>",
      _tone: "bad",
    },
    {
      i: "the FLT encodings",
      needs: "float units — <code>FLANES &gt; 0</code>",
      does: "<b>illegal → FAULT (cause 3)</b>",
      _tone: "bad",
    },
    {
      i: "the four seed encodings",
      needs: "seed units — <code>FSFU_UNITS &gt; 0</code>, a subset of the float units",
      does: "<b>illegal → FAULT (cause 3)</b>",
      _tone: "bad",
    },
    {
      i: "bar",
      needs: "a workgroup barrier",
      does: "<b>decoded, read by NOTHING → retires as a NO-OP</b>",
      _tone: "warn",
    },
  ],
};

// ------------------------------------------------------------------- traps
const trapTable = {
  cols: [
    { key: "sym", label: "Symptom" },
    { key: "cause", label: "Cause" },
  ],
  rows: [
    {
      sym: "every PC executes twice",
      cause:
        "the window was driven from the architectural PC, which lags the fetch",
    },
    {
      sym: "one instruction silently never happens",
      cause:
        "<code>imem_addr</code> ran on while <code>f1</code> was held, so the resumed <code>f2 &lt;= f1</code> no longer named the word in flight",
    },
    {
      sym: "wedges on the first back-to-back dependency",
      cause:
        "<code>kht_unit</code> was fed the <b>combined</b> hold, so its own stall was an input to itself",
    },
    {
      sym: "a store lands at a nonsense address, <code>rgn</code> is BAD",
      cause:
        "the walk started in the same cycle the instruction retired, and decoded the next one",
    },
    {
      sym: "deadlock on the first store that uses the previous result",
      cause:
        "<code>lsu_need</code> was in <code>base_hold</code>, freezing the MEM register so the hazard never cleared",
    },
    {
      sym: "a load never retires, the PC never advances",
      cause:
        "no <code>lsu_done</code>: the finished walk dropped <code>hold</code> and immediately restarted",
    },
    {
      sym: "every lane takes the else-branch; “all-zero active mask” is reported",
      cause:
        "<code>split</code> read its predicate from the <b>registered</b> vector port in EX, so it got the previous instruction's operand",
    },
    {
      sym: "only the lanes that preceded a cache miss read zero",
      cause:
        "a one-cycle walk captured lane <i>n</i>'s word on the cycle lane <i>n+1</i>'s miss zeroed <code>l1_rdata</code>",
    },
    {
      sym: "a load returns zero and the fill never completes",
      cause:
        "<code>l1_req</code> was dropped while waiting for the word; <code>rv_l1</code> completes a miss only while the request stands",
    },
    {
      sym: "the unit closes at 324 MHz and the core containing it at 72",
      cause:
        "the cross-lane reduction was a <b>serial chain</b> of LANES 32-bit adds, and it lives in <code>kht_core</code> where the unit-only ladder never looked",
    },
    {
      sym: "<code>reduxmax</code> over 1..8 passes while being wrong",
      cause:
        "min and max started at zero, which clamps every result against 0 — invisible whenever the data straddles it",
    },
    {
      sym: "three dependent <code>vfma</code> launch in consecutive cycles with stale addends",
      cause:
        "<code>fpend</code> blocks <b>fetch</b>, but the front end is three deep — the float must also redirect its own wave to <code>pc+4</code>",
    },
    {
      sym: "<code>mul x10, x6, x8</code> jumps forty bytes and skips nine instructions",
      cause:
        "<code>is_imul</code> was added to <code>br_take</code> and not to <code>redir_pc</code>, so a multiply took <code>f2_pc + imm_i</code> — and an R-type's imm field is <code>funct7|rs2</code>",
    },
    {
      sym: "a wave comes back one instruction late for every cycle it waited on a float",
      cause:
        "the per-wave PC increment was gated on the fetch, not on <code>rdy[cur]</code>. Invisible before G9: the only unready wave used to be a <b>dead</b> one",
    },
    {
      sym: "the machine wedges with sixteen waves runnable, on a <code>vfmul</code>",
      cause:
        "a held instruction is <code>go</code> on every cycle of the hold, so the float re-launched the lane array every cycle and <code>f_soon</code> never cleared",
    },
    {
      sym: "a <code>join</code> underflows and faults in perfectly balanced code",
      cause:
        "the stack committed on <code>go</code> rather than <code>go_c</code>, so a join sitting under another wave's <code>f_soon</code> popped once per cycle",
    },
    {
      sym: "a <code>vfma</code> reads its addend from before the instruction that set it",
      cause:
        "<code>vd</code> is a <b>source</b> for <code>vfma</code> and was not compared as one — seen as <code>c = 0x0400</code> where the shader had just built <code>0x4000</code>",
    },
    {
      sym: "the halt word is whatever <code>a0</code> held before the multiply that computed it",
      cause:
        "the a0 snoop watched the MEM stage's write intent; a float or multiply retires through its own slot, so the probe must be the register file's <b>write port</b>",
    },
    {
      sym: "the shader runs a half-written image",
      cause:
        "CU_INST and CU_DATA are different queues, so a kick can reach the head while the last granule is still being walked in. The kick waits on <code>rx_quiet</code>",
    },
    {
      sym: "halt word is X, or is the wrong instruction's",
      cause:
        "<code>a0</code> read from the <code>ecall</code>'s <code>rs1</code>, or latched before the writeback committed",
    },
    {
      sym: "shader passes every check, DRAM is unchanged",
      cause: "no flush: the stores were sitting in dirty L1 lines",
    },
    {
      sym: "a hierarchical reference simulates and will not synthesise",
      cause:
        "<code>u_vt.v1_rd</code> — read ports must be <b>ports</b> (<code>rd1_o</code>/<code>rd2_o</code>)",
    },
    {
      sym: "a Vivado run reports clean and every LUT figure is unconstrained",
      cause:
        "the OOC <code>create_clock</code> was guarded by a <code>get_ports</code> test that evaluates before the design exists. The XDC is now unconditional and the script <b>errors</b> if <code>get_clocks</code> is empty",
    },
  ],
};
</script>

<template>
  <DocPage
    title="SIMT PE — microarchitecture"
    summary="How it is built, in diagrams — the three-deep fetch, the hold signals and the loop between them, the lane-serialising LSU, the IPDOM pair-stack, the shadow pipe both multi-cycle units retire through, and the halt-and-flush. Every trap here is a failure that happened."
    domain="simt"
    status="measured"
    source="src/kohakumpe/simt/ · docs/projects/kohakumpe/simt/microarchitecture.md"
  >
    <p class="doc-p">
      <code>kht_core</code> is a <b>rebuild</b> on the
      <RouterLink to="/framework/cpu" class="doc-link">base core</RouterLink>'s
      shape, not an extension of it. The base core's six register boundaries and
      its hazard style are kept because they are what close at this clock. What
      changes is which register file an ordinary RV32I opcode addresses.
    </p>

    <h2 class="doc-h2">The whole unit</h2>

    <Fig
      caption="Left to right: the fabric port fills the two windows — the instruction and, beside it, the control word kht_predec computed as the image landed — the kick starts the per-wave PCs, and fetch drives EX's three consumers: the scalar file, kht_unit, and the LSU. kht_unit is the SIMT half, drawn as the vertical stack, and is where the measurement ladder lives as parameters. The register-class rule is visible in the accented path: an ordinary RV32I opcode drives kht_unit, and the scalar file is reached only through custom-2/-3. The arrow back from the lane array to the scalar file is the subgroup ops."
      zoom
      wide
    >
      <BlockDiagram
        :nodes="unit.nodes"
        :edges="unit.edges"
        :groups="unit.groups"
      />
    </Fig>

    <h2 class="doc-h2">
      Decode does not happen at fetch — it happens at image load
    </h2>
    <p class="doc-p">
      The base core registers its decoded outputs into EX and closes at 410 MHz.
      <code>kht_core</code> had <b>no decode stage at all</b>: decode, operand
      read, address generation and the PC update all in one cycle. That is the
      whole of why it started at 182.
    </p>

    <Callout
      kind="rule"
      title="Adding a decode stage costs a cycle of branch latency. Predecoding does not."
    >
      <p>
        Every decode signal is a pure function of the instruction word, so it is
        computed <b>once on the write path</b> as the shader image lands and
        stored in a second memory beside the instruction. Both arrays are
        <code>READ_LAT 1</code> at the same address, so the instruction and its
        control word arrive together.
        <b>+55 MHz on the assembled PE, and zero added latency.</b>
      </p>
      <p>
        <code>kht_ctrl.vh</code> is the bit layout and is included by
        <b>both</b> the producer and the consumer, so the two cannot disagree —
        and <code>kht_core</code> checks the literal width in its ports against
        <code>KHT_CW</code> at elaboration, because widening the word and
        leaving a port behind has already cost one synthesis run to an
        out-of-range part-select. It is <b>60 bits</b>; it was 50 before the
        float tier and RV32M each claimed some.
      </p>
    </Callout>

    <Fig
      caption="Measured: four of the thirteen levels between the window and the per-wave PC's clock enable were spent producing mem_store alone. The cost is one memory the width of the control word, which the tool maps to the same BRAM class as the instruction window."
      zoom
    >
      <BlockDiagram
        :nodes="predec.nodes"
        :edges="predec.edges"
        :groups="predec.groups"
      />
    </Fig>

    <h3 class="doc-h3">
      The timing budget is about nine levels, and that is arithmetic
    </h3>
    <div
      class="font-mono kt-text-caption whitespace-pre text-warm-700 dark:text-warm-300 leading-6 overflow-x-auto my-3"
    >
      {{ budgetLevels }}
    </div>

    <p class="doc-p">
      So a cone at ten-plus levels is a defect, a cone at nine is at the line,
      and
      <b
        >shortening a level is worth 0.24 ns while shaving a LUT off one is
        worth 0.04.</b
      >
      That is why every fix below removes levels or fanout, and why “add a
      pipeline stage” has been the last resort rather than the first.
    </p>

    <h2 class="doc-h2">Fetch: three cycles, and the third is worth 0.83 ns</h2>
    <p class="doc-p">
      The instruction window is block RAM, so there are three stages:
      <code>f1</code> is the address issued into it, and <code>f2</code> is the
      instruction <b>registered in fabric</b> beside the PC and the wave it
      belongs to. Getting the relationship between them wrong has broken this
      core twice, in two different ways, and neither raised an error.
    </p>

    <WaveTrace
      label="working — f1 leads f2 by one cycle of block RAM"
      :rows="fetchOk.rows"
      :notes="fetchOk.notes"
    />

    <Callout
      kind="note"
      title="The flop has a second effect that is not about the start point"
    >
      <p>
        <b
          ><code>max_fanout</code> works on a register and cannot work on a
          block RAM.</b
        >
        <code>rs2</code> was asked to replicate and stayed at fanout 359 with
        0.413 ns of route, because the tool had no driver it was allowed to
        duplicate. Behind the flop it does. The price is one more cycle of
        redirect latency — which interleaving makes cheap.
      </p>
    </Callout>

    <Callout
      kind="trap"
      title="Trap 1 — driving the window from the architectural PC"
    >
      <p>
        An architectural PC only advances when an instruction <b>retires</b>,
        which is a cycle after the fetch should already have moved on. Drive
        <code>imem_addr</code> from it and every instruction is fetched, decoded
        and committed twice.
      </p>
    </Callout>

    <WaveTrace variant="broken" :rows="trap1.rows" :notes="trap1.notes" />

    <Callout
      kind="trap"
      title="Trap 2 — letting the address advance while held"
    >
      <p>
        <code>f1</code> is frozen by the same <code>hold</code> as
        <code>f2</code>, so letting the address advance fetches a word the
        resumed <code>f2 &lt;= f1</code> no longer names. The instruction that
        gets skipped leaves no trace at all.
      </p>
    </Callout>

    <WaveTrace
      variant="broken"
      label="broken — imem_addr = nxt[cur]"
      :rows="trap2Broken.rows"
      :notes="trap2Broken.notes"
    />
    <WaveTrace
      variant="fixed"
      label="fixed — imem_addr = hold ? f1_pc : nxt[cur]"
      :rows="trap2Fixed.rows"
      :notes="trap2Fixed.notes"
    />

    <h3 class="doc-h3">One pointer per wave, and the kill is two conditions</h3>
    <p class="doc-p">
      <code>nxt[WAVES]</code> is the fetch pointer <b>and</b> the architectural
      PC — a separate committed copy bought nothing, because a redirect rewrites
      this array directly.
    </p>

    <WaveTrace
      label="round-robin over the ready set"
      :rows="interleave.rows"
      :notes="interleave.notes"
    />

    <Callout
      kind="rule"
      title="A single-wave front end can kill unconditionally. This one cannot."
    >
      <p class="font-mono kt-text-caption">
        kill_ev = go_nh &amp;&amp; (br_take || wave_ends);<br />
        kill_f1 = kill_ev &amp;&amp; (f1_wave == f2_wave);<br />
        kill_new = kill_ev &amp;&amp; (cur == f2_wave);
      </p>
      <p>
        Under interleaving the instruction behind a branch usually belongs to a
        <i>different</i> wave and must survive, so an unconditional kill would
        silently drop it.
      </p>
      <p>
        <b
          ><code>hold</code> belongs at the clock enable, not inside the
          decision.</b
        >
        Everything the front end writes is already under
        <code>if (!hold)</code>, so carrying <code>!hold</code> inside
        <code>go</code> put it in series with the redirect decision instead of
        beside it — nine levels where six would do. Measured:
        <b>1,633 failing endpoints → 70</b>, and the whole front-end cone family
        disappeared in one change.
      </p>
    </Callout>

    <h2 class="doc-h2">The hold signals</h2>
    <p class="doc-p">
      This is the most delicate part of the core and it has failed in
      <b>both</b>
      directions. They are not interchangeable.
    </p>

    <Fig
      caption="Three destinations, three meanings: freeze the MEM register, do not retire, do not commit. A signal on the wrong wire either wedges the core or loses a store. The walk's own gate is lsu_want — lsu_need's shallow half — because hold ORs warm_stall and s_hz anyway, so A || (B && !A) collapses and the gate stays off that cone."
      zoom
    >
      <BlockDiagram :nodes="holds.nodes" :edges="holds.edges" />
    </Fig>

    <SpecTable :cols="holdTable.cols" :rows="holdTable.rows" />

    <Callout
      kind="rule"
      title="EVERY core-level hold that is not base_hold must defer the commit"
    >
      <p>
        <code>kht_unit</code>'s internal <code>go</code> deliberately excludes
        this core's <code>hold</code>, so a held instruction is
        <code>go</code> on <b>every cycle of the hold</b>. That is idempotent
        for an integer register write and <b>catastrophic for a float</b>: a
        held <code>vfmul</code> re-launched the lane array every cycle,
        <code>f_soon</code> never cleared, and the whole machine wedged with
        sixteen waves runnable. <code>s2v</code> and <code>shflxor</code> are
        the same class one step milder — they would write from the stale read
        the stall exists to avoid.
      </p>
      <p>
        The stack has its own version of the same rule. The mask and the IPDOM
        stack commit on <code>go_c = go &amp;&amp; !x_defer</code>, because a
        stack is not idempotent:
        <b
          >a <code>join</code> sitting under another wave's
          <code>f_soon</code> popped once per cycle until the stack underflowed
          and faulted.</b
        >
      </p>
    </Callout>

    <Callout kind="trap" title="Trap 3 — feeding a unit its own stall">
      <p>
        With <code>x_hold = base_hold || vt_stall</code>, the stall becomes an
        input to itself: the MEM register freezes, its destination stays live,
        the hazard against it never clears, and the core wedges on the first
        back-to-back dependency.
        <b
          ><code>x_hold</code> must be the rest of the machine's stall, never
          the unit's own.</b
        >
      </p>
    </Callout>

    <Fig
      caption="BROKEN — the loop that wedges the core on the first back-to-back dependency. kht_unit's header carries the warning; the DSP unit carries the same one for the same reason."
      zoom
    >
      <BlockDiagram :nodes="trap3.nodes" :edges="trap3.edges" />
    </Fig>

    <h3 class="doc-h3">
      Traps 4 and 5 — the walk's gate on the wrong side of the line
    </h3>
    <p class="doc-p">
      <code>lsu_busy</code> is a <b>register</b>, so on the cycle a walk is
      decided it is still low. Leave the walk out of <code>hold</code> and the
      instruction retires under it; put it inside <code>base_hold</code> and the
      walk can never start.
    </p>

    <WaveTrace
      variant="broken"
      label="broken — the walk's gate OUT of hold"
      :rows="trap4.rows"
      :notes="trap4.notes"
    />

    <Callout
      kind="trap"
      title="Trap 4 was quiet because R_BAD suppresses the request"
    >
      <p>
        The walk computed a garbage <code>ea</code>, it landed outside every
        region, so <code>R_BAD</code> suppressed <code>l1_req</code> and nothing
        was issued — and the shader still reported the right halt word and the
        right cause. Three of four checks passed. The region decode is on
        <RouterLink to="/mpe/simt" class="doc-link">the SIMT PE page</RouterLink
        >.
      </p>
    </Callout>

    <Fig
      caption="BROKEN — the walk's gate inside base_hold. The MEM register freezes, so the hazard on the address operand never clears, so vt_stall never drops, so the walk can never START."
      zoom
    >
      <BlockDiagram :nodes="trap5.nodes" :edges="trap5.edges" />
    </Fig>

    <WaveTrace
      variant="fixed"
      label="fixed — x_defer suppresses the MEM capture without freezing the register"
      :rows="deferFixed.rows"
      :notes="deferFixed.notes"
    />

    <Callout
      kind="trap"
      title="…and lsu_done is what stops the walk restarting"
    >
      <p>
        Without it the finished walk drops <code>hold</code>, the same
        instruction is still in <code>f2</code>, <code>per_lane</code> is still
        true, and it <b>starts again</b> — a load that never retires and a PC
        that never advances.
      </p>
    </Callout>

    <h2 class="doc-h2">
      Trap 6 — the vector read is registered, and EX consumers forget it
    </h2>
    <p class="doc-p">
      <code>kht_vregfile</code> in <code>block</code> mode has a
      <b>registered</b> read port, so <code>v1_rd</code> in the EX stage belongs
      to the <b>MEM-stage</b> instruction. That is correct for the ALU, which
      consumes it a stage later — and wrong for everything that consumes it
      <i>in</i> EX.
    </p>

    <SpecTable
      :cols="v1Consumers.cols"
      :rows="v1Consumers.rows"
      caption="A per-lane access owes it too, gate or no gate: the serial walk used to be safe for free — lsu_run is a register and bought the cycle — but ea_all_q is a register BEHIND the vector read, so the walk must wait for that as well. One cycle per memory instruction, not per lane. With the banked LDS it is four, because deciding LDS-versus-DRAM is every lane's address and region."
    />

    <WaveTrace
      variant="fixed"
      label="no hazard — ONE cycle of hold is enough"
      :rows="warmNoHz.rows"
      :notes="warmNoHz.notes"
      :start="1"
    />
    <WaveTrace
      variant="broken"
      label="hazard (andi x6 ; split x6) — the hazard cycle counted as a warm cycle"
      :rows="warmHzBroken.rows"
      :notes="warmHzBroken.notes"
      :start="1"
    />
    <WaveTrace
      variant="fixed"
      label="hazard — do not count the cycles the hazard stands"
      :rows="warmHzFixed.rows"
      :notes="warmHzFixed.notes"
      :start="1"
    />

    <p class="doc-p">
      The symptom, before the fix, was a divergent shader in which
      <i>every</i> lane took the else-branch and <code>kht_unit</code> reported
      an <b>all-zero active mask</b> twice.
    </p>

    <StepPlayer :steps="staleMask" label="the stale-predicate symptom">
      <template #default="{ state }">
        <LaneGrid :lanes="8" :mask="state.mask" />
      </template>
    </StepPlayer>

    <h2 class="doc-h2">The mask is a write enable, not a datapath input</h2>

    <Fig
      caption="An inactive lane computes whatever it computes and its WRITE is dropped. Masking costs one enable per bank and nothing on the arithmetic path — which is why the mask gate is +64 LUT, why control sets are 36 at four lanes and 36 at thirty-two, and why Fmax does not move."
      zoom
    >
      <BlockDiagram
        :nodes="maskWe.nodes"
        :edges="maskWe.edges"
        :groups="maskWe.groups"
      />
    </Fig>

    <h2 class="doc-h2">
      The IPDOM stack is a memory, not an indexed flop array
    </h2>
    <p class="doc-p">
      One word is one pair, which is exactly what one <code>split</code> pushes.
      Two pushes are <b>one write</b>, so a single write port suffices, and the
      depth is entries/2 while <i>“a split costs two entries”</i> stays true.
    </p>

    <Fig
      caption="One stack word at LANES = 8. WIDTH = 2 × LANES. A phase bit per wave says which half the next join takes."
    >
      <BitField :fields="pairWord" />
    </Fig>

    <Fig
      caption="READ_LAT 0 is what keeps a join combinational, so the stack costs no cycle. Rebuilt from an indexed flop array into this shape, the whole G3 gate is +188 LUT, of which 20 LUT is the distributed RAM — the stack itself."
      zoom
    >
      <BlockDiagram :nodes="stackMem.nodes" :edges="stackMem.edges" />
    </Fig>

    <h3 class="doc-h3">A divergent if/else, step by step</h3>
    <p class="doc-p">
      Scrub through it. The point to watch is the <b>utilisation</b> row: inside
      the divergent region only four of eight lanes retire a write. Divergence
      costs throughput — never correctness.
    </p>

    <StepPlayer :steps="ipdom" label="split / join at LANES = 8">
      <template #default="{ state }">
        <LaneGrid
          :lanes="8"
          :mask="state.mask"
          :rows="
            state.pred
              ? [
                  { name: 'pred', values: state.pred },
                  { name: 'x[rd] write', values: WRITES },
                ]
              : [{ name: 'x[rd] write', values: WRITES }]
          "
        />
        <div class="flex flex-wrap gap-2 mt-3">
          <span class="chip">{{ state.instr }}</span>
          <span class="chip">sp = {{ state.sp }}</span>
          <span class="chip">phase = {{ state.phase }}</span>
          <span class="chip">active = {{ state.active }}/8</span>
          <span class="chip"
            >utilisation = {{ Math.round((state.active / 8) * 100) }}%</span
          >
        </div>
        <SpecTable
          v-if="state.stack.length"
          :cols="stackCols"
          :rows="state.stack"
          caption="the IPDOM stack — one word, one pair"
        />
        <p
          v-else
          class="kt-text-caption text-warm-400 dark:text-warm-600 mt-3 font-mono"
        >
          the IPDOM stack is empty
        </p>
      </template>
    </StepPlayer>

    <Callout
      kind="trap"
      title="A split while a wave is half-unwound would overwrite the pair it is still reading"
    >
      <p>
        Balanced code cannot do it — every split has two joins — but
        <i>“cannot happen”</i> is what this project has been wrong about before,
        so it is a <b>simulation assertion</b> rather than an argument. The flop
        version had no such window to be wrong in, and that is the honest cost
        of the rebuild.
      </p>
      <p>
        Overflow is a <b>fault</b> — not a wrap, not a mask merge, not a
        truncation. A masked-off lane that silently reactivates is a wrong
        answer with no witness. Underflow is the same fault.
      </p>
    </Callout>

    <h2 class="doc-h2">Hazards: stall at distance 1 and 2, no forwarding</h2>

    <WaveTrace
      label="two distances, because the lane ALU has a writeback stage"
      :rows="hzTrace.rows"
      :notes="hzTrace.notes"
    />

    <Callout kind="rule" title="Compare first, select after">
      <p>
        A <code>32×LANES</code>-wide bypass mux is the widest path in the unit,
        so a dependency <b>stalls</b> rather than forwarding — one cycle on a
        dependency a shader compiler can usually schedule around.
      </p>
      <p>
        <code>x_rs1</code> was <code>mem_store ? rd : rs1</code>, which put a
        decode term and a mux <i>in series</i> ahead of the comparator, four
        levels from the instruction word. The unit now takes both candidates and
        the select as three separate ports, so every comparator starts at a raw
        instruction field. <b>A <code>vfma</code> reads its destination</b>, so
        <code>rd</code> is compared as a source too — the same comparator under
        a second condition rather than a fourth one.
      </p>
    </Callout>

    <h2 class="doc-h2">Every fix that mattered was the same fix</h2>
    <p class="doc-p">
      A cone that starts at a block RAM begins 0.85–0.91 ns in debt on a budget
      of 2.5 ns. So the work is to make cones start at flip-flops, and to stop
      putting
      <code>hold</code> in series with decisions it only needs to gate.
    </p>

    <SpecTable
      :cols="flopFront.cols"
      :rows="flopFront.rows"
      caption="Three memories each cost the same 0.85–0.91 ns and each needed the same answer: the instruction window, the scalar file, and the vector register file. The largest single win — 50 MHz — was one register on a ONE-BIT signal, because mask reached rv_l1's stall and stall reaches every clock enable in the core. 182.0 → 394.3 MHz over nineteen rows, for 321 LUT LESS than the baseline."
    />

    <h3 class="doc-h3">The scalar half: one ALU, four parallel classes</h3>
    <p class="doc-p">
      custom-2 and custom-3 used to build a case statement each and mux between
      them — two adders, two shifters, and a mux level after the slowest thing
      in the cone.
      <code>kht_predec</code> maps both encodings onto one 4-bit operation, so
      there is one datapath with a muxed operand. Writing <i>that</i> as a
      single case statement then cost what merging it saved: the tool folded the
      shifter into the adder's carry chain, so the shift amount sat in front of
      all 32 bits of carry it has nothing to do with.
    </p>

    <Fig
      caption="Split into four parallel cones with one mux at the end, the shifter and the adder no longer share a chain. FIVE WAYS IN TWO STEPS, not four and not one: collapsing SLT/SLTU to make it four, and folding the fast path in to make one five-way, each LOST 9.4 MHz — Vivado maps this form as LUT6 + MUXF7, and a MUXF7 is dedicated silicon at 0.067 ns where another routed LUT level is 0.22."
      zoom
    >
      <BlockDiagram :nodes="salu.nodes" :edges="salu.edges" />
    </Fig>

    <Callout
      kind="rule"
      title="A stored flag only pays when the thing it is computed from starts at a register"
    >
      <p>
        The scalar file is <code>reg [32:0]</code> —
        <code>{zero, value}</code> — so the branch's zero test is
        <b>stored, not computed</b>: as a 32-bit reduce in front of the per-wave
        PC's clock enable it was the 1,024-endpoint cone that held the PE at 182
        MHz. Where the compare itself sits was tried three times.
      </p>
    </Callout>

    <SpecTable
      :cols="zeroFlag.cols"
      :rows="zeroFlag.rows"
      caption="The built RTL is sfile[aad_q] <= {(sres == 32'd0), sres} — which LOOKS like the row that lost 23 MHz and is not, because the operand register moved in front of the ALU underneath it. Forwarding is gone with it: at distance 1 the result does not exist yet in either arrangement, so the scalar half interlocks, and distance 2 needs nothing because the file was written at the end of the previous cycle."
    />

    <h2 class="doc-h2">The LSU serialises lanes</h2>
    <Callout kind="note" title="A staging decision, not a design">
      <p>
        Until the coalescer exists, a per-lane access walks its active lanes one
        at a time through the existing single-miss L1: correct, slow, and it
        makes “requests per gather” a number that <b>improves</b> when the
        coalescer lands rather than one that appears from nothing.
      </p>
    </Callout>

    <Fig
      caption="The banked LDS is one branch of the same walk, not a second one: when every active lane's address decodes to R_LDS — which all_lds_q says, from a REGISTERED decision — the access hands over to kht_lds and comes back in as many passes as the banks need. Both arrivals meet at DRAIN, so req_ctr is req_q + lds_passes and the witness stays one number whichever path served the instruction."
      zoom
    >
      <StateMachine :states="lsuSm.states" :edges="lsuSm.edges" />
    </Fig>

    <h3 class="doc-h3">
      Three phases per lane, and each one was bought separately
    </h3>

    <WaveTrace
      label="a load, two lanes"
      :rows="lsuPhases.rows"
      :notes="lsuPhases.notes"
    />

    <SpecTable :cols="l1Rules.cols" :rows="l1Rules.rows" />

    <p class="doc-p">
      A one-cycle-per-lane walk cannot survive (1) and (2) at once.
    </p>

    <WaveTrace
      variant="broken"
      label="broken — one cycle per lane"
      :rows="lsuBroken.rows"
      :notes="lsuBroken.notes"
    />

    <Callout
      kind="trap"
      title="Dropping l1_req while waiting for the word was tried, and is wrong"
    >
      <p>
        <code>rv_l1</code> abandons the fill, <code>l1_stall</code> goes low
        because nothing is asking, and the capture reads zero. The trace said it
        in one line — <code>ph 1 … req 0 stl 0 rdata 00000000</code>. Phase 1 is
        where a miss is held, and the request stands for the whole of it.
      </p>
    </Callout>

    <h3 class="doc-h3">Where a store's data comes from</h3>
    <SpecTable
      :cols="storeData.cols"
      :rows="storeData.rows"
      caption="There is no fourth register field for a vmem store's data, so read port 1 serves it. The SIMD tier's vst does exactly the same thing for exactly the same reason."
    />

    <h3 class="doc-h3">The counters are the coalescer witness</h3>
    <SpecTable
      :cols="counters.cols"
      :rows="counters.rows"
      caption="Counted NOW, before a coalescer exists. A witness that only appears once the optimisation lands cannot show the optimisation working. The three addressing tiers are already distinguished in the encoding, so the coalescer replaces the walk WITHOUT the ISA moving."
    />

    <Callout kind="open" title="The leader/follower passes below are PROJECTED">
      <p>
        G5 is <b>not built and not measured</b>. What follows is the algorithm
        the encoding was shaped for, drawn over a scattered address set so the
        shape of the work is concrete — it is not a result, and no cost is
        attributed to it anywhere on this site.
      </p>
    </Callout>

    <StepPlayer
      :steps="coalesce"
      label="coalescer leader/follower passes — PROJECTED (G5)"
    >
      <template #default="{ state }">
        <LaneGrid
          :lanes="8"
          :mask="state.mask"
          :rows="[
            { name: 'address', values: ADDRS },
            { name: 'line', values: LINES },
            { name: 'pending', values: state.pending },
            { name: 'role', values: state.role },
          ]"
        />
        <div class="flex flex-wrap gap-2 mt-3">
          <span class="chip">requests issued = {{ state.req }}</span>
          <span class="chip">serial walk today = 8</span>
        </div>
      </template>
    </StepPlayer>

    <h2 class="doc-h2">The cross-lane reductions</h2>
    <SpecTable :cols="redux.cols" :rows="redux.rows" />

    <p class="doc-p">
      <b>The arithmetic tree was the whole machine's binding path, twice.</b>
      Written as a sequential loop it is <code>LANES</code>
      <i>chained</i> 32-bit operations — and the unit-only ladder could never
      see it, because the reductions live in <code>kht_core</code>.
    </p>

    <SpecTable
      :cols="reduxHist.cols"
      :rows="reduxHist.rows"
      caption="The pipelined form is CHEAPER IN LUT than the chain it replaced — registering each level breaks the long combinational cone, so the tool packs simpler logic — at +229 FF and 3.9× the clock. A redux is held log2(LANES) extra cycles on top of the one it already owes for its operand, which is architecturally free: it is a rare instruction, it already stalls, and its operands are held stable so the pipeline simply fills. The leaves are registered too, for the same reason the levels are: they take vt_rd1 straight off the vector file's block RAM."
    />

    <SpecTable
      :cols="identities.cols"
      :rows="identities.rows"
      caption="An inactive lane contributes the identity, which is what makes a tree agree with a masked sequential reduction. Starting min and max at zero clamps every result against zero — invisible whenever the data straddles it, which is why reduxmax over 1..8 passed while being wrong."
    />

    <h2 class="doc-h2">The banked LDS</h2>

    <Fig
      caption="The interleave is why stride 1 is conflict-free and stride LANES is the worst case — the same trade every GPU makes. The resolver lives inside kht_lds rather than in the core, so the gate is one parameter and the LANES × LANES comparison is measured where it is spent. FORWARD PROGRESS, which the sequencer rests on: every pass serves the lowest outstanding lane, because that lane is by construction the lowest lane on its own bank — so a sequence ends in at most LANES passes and cannot stall. The block asserts that rather than trusting the argument."
      zoom
      wide
    >
      <BlockDiagram
        :nodes="ldsBanks.nodes"
        :edges="ldsBanks.edges"
        :groups="ldsBanks.groups"
      />
    </Fig>

    <SpecTable
      :cols="ldsPasses.cols"
      :rows="ldsPasses.rows"
      caption="Measured on hardware by simt_lds.s. The reversed case is the one that proves the RETURN CROSSBAR: bank 7's word has to reach lane 0, rather than lane 0 always taking bank 0. Two cycles a pass, because the banks register their address — drive, then take — and `done` is a cycle after 'no lanes left', or the caller reads lrdata one pass stale."
    />

    <h2 class="doc-h2">The subgroup butterfly</h2>

    <WaveTrace
      label="LANES = 8, ctl = 5 (binary 101) — lane 0 ends holding vs1[5]"
      :rows="butterfly.rows"
      :notes="butterfly.notes"
    />

    <Callout
      kind="rule"
      title="The masked case is resolved BEFORE the network, not inside it"
    >
      <p>
        The ISA fixes that a lane whose source is inactive
        <b>reads its own value</b>. That cannot be decided inside the butterfly
        — once data has moved one stage, “was my source active” is no longer a
        question the intermediate lanes can answer. So the control is zeroed up
        front: <code>ctl[i] = mask[src[i]] ? (i ^ src[i]) : 0</code>.
      </p>
      <p>
        The control is <b>WB-stage</b>, because the network consumes
        <code>w1_q</code>: the registered read port made EX-stage control one
        cycle early, and the writeback stage that put the lane ALU behind a flop
        makes it two. This is Trap 6 again, avoided rather than repeated.
      </p>
    </Callout>

    <h2 class="doc-h2">
      The float tier and the multiplier: one shadow pipe, two producers
    </h2>
    <p class="doc-p">
      Both retire at the tier's latency <code>ALAT</code>, one instruction per
      cycle — <b>6 cycles with no seed units built and 10 with them</b>, because
      a seed is four stages deeper and the multiply-add path pads to match so
      the tier has one latency and one retire shadow. That the two match is not
      a coincidence — giving the
      multiplier the float tier's <i>exact</i> latency makes a write-port
      collision <b>structurally impossible</b> instead of arbitrated: two
      results can only want the port on one cycle if they were issued on one
      cycle, and exactly one instruction issues per cycle.
    </p>

    <Fig
      caption="Every float unit is one rv_fpu — the FRAMEWORK's, because RV32F is a standard extension over IEEE binary32 — and a seed unit carries a khs_fp32_sfu beside it. Neither is forked from the SIMD tier, which is what makes a SIMT float result comparable to a SIMD one element for element. fsh_* is a shadow shift register exactly FLAT deep, carrying the valid bit, the destination address, the write mask, the wave and the pass index — everything the lane array does not carry. It MUST match the array's own depth, because if the two disagree a result lands on the wrong register with no witness, and both modules check the depth they were told against the depth they built at elaboration. It is free-running, like the lane array it shadows: the array has no clock enable, so gating this would desynchronise the two."
      zoom
      wide
    >
      <BlockDiagram :nodes="shadow.nodes" :edges="shadow.edges" />
    </Fig>

    <Callout
      kind="rule"
      title="This is not a scoreboard, and that is the point"
    >
      <p>
        A per-register scoreboard and out-of-order retire are both refused
        elsewhere in this machine.
      </p>
    </Callout>

    <SpecTable :cols="notScoreboard.cols" :rows="notScoreboard.rows" />

    <SpecTable
      :cols="measuredLatency.cols"
      :rows="measuredLatency.rows"
      caption="Same shader, same answer: 16× the work for 2.9× the cycles. One wave is the WORST case for the float tier, not the easy one — with nothing else runnable the 15-cycle latency is exposed rather than hidden, so a dependent chain that is right there is right at any occupancy."
    />

    <h3 class="doc-h3">The float redirects its own wave</h3>

    <WaveTrace
      label="fpend, and the redirect that makes it work"
      :rows="fpendTrace.rows"
      :notes="fpendTrace.notes"
    />

    <Callout
      kind="trap"
      title="A multi-cycle unit redirects to the NEXT instruction, not to a branch target"
    >
      <p>
        Adding <code>is_imul</code> to <code>br_take</code> without adding it to
        <code>redir_pc</code> sent every multiply to
        <code>f2_pc + imm_i</code> — and an R-type's immediate field <i>is</i>
        <code>funct7|rs2</code>, so <code>mul x10, x6, x8</code> jumped forty
        bytes and skipped nine instructions.
      </p>
      <p>
        And <code>rdy[cur]</code> gates the <b>PC increment</b>, not just the
        fetch: advancing a pointer on a cycle that issued no fetch skips an
        instruction. That was invisible before the float tier, because until
        then the only unready wave was a <b>dead</b> one, whose pointer nobody
        reads again.
      </p>
    </Callout>

    <Callout
      kind="note"
      title="The multiplier's pad is FLIP-FLOPS, and the RTL says so explicitly"
    >
      <p>
        <code>(* srl_style = "register" *)</code> refuses the SRL16E the shape
        would otherwise map to. An SRL16E is
        <b>one LUT per bit at any depth</b>, and this PE is LUT-bound while the
        flop half of the CLB is idle: the change measured
        <b>−256 LUT for +3,329 FF at an unchanged Fmax</b> on a matched pair. At
        a much tighter target the same change read as −15.6 MHz — an artifact of
        asking for timing the design was not going to meet, not a property of
        the pad.
      </p>
      <p>
        One 33×33 signed multiply serves all four RV32M forms: only the
        extension bits differ, and <code>mul</code>'s low half does not depend
        on them at all.
      </p>
    </Callout>

    <h2 class="doc-h2">
      The kick must not overtake the data it is the doorbell for
    </h2>
    <p class="doc-p">
      Boot is not a mechanism here. A shader image arrives as a
      <code>CU_DATA</code> burst into the instruction window and its constants
      as another into the scratchpad, then the standard kick — the same write
      path every unit has, which is why there is no loader to go wrong. But the
      two arrive on <b>different queues</b>, and the framework preserves order
      on the wire and not across them.
    </p>

    <Fig
      caption="So the kick can reach the head of its FIFO while the last granule of the shader is still being walked into the window. K_IDLE waits on rx_quiet before accepting it — and that cannot deadlock, because rx_quiet is cleared by this unit's OWN progress rather than by another flit arriving. The kick's op field is the wave count, clamped to what the build carries: op = 1 is exactly the single-wave case it always was, so no existing caller changes."
      zoom
    >
      <BlockDiagram :nodes="kick.nodes" :edges="kick.edges" />
    </Fig>

    <SpecTable
      :cols="completion.cols"
      :rows="completion.rows"
      caption="“Halted” is not enough. The completion is the host's only sequencing point, so all four terms are required before the unit reports done."
    />

    <h2 class="doc-h2">A halt flushes before it completes</h2>
    <p class="doc-p">
      A write-back L1 holds a shader's stores in dirty lines that nobody else
      will ever push out. Without the flush: the shader retires, the host reads
      DRAM, and finds it unchanged.
    </p>

    <Fig
      caption="The flush is not finished until every writeback has been ACKNOWLEDGED, which is what makes the completion mean the data is in memory rather than merely issued. A fault kills the whole unit; an ecall or ebreak retires ONE WAVE — a shader is finished when its last wave is, and a fault is a property of the program rather than of the wave that happened to hit it."
      zoom
    >
      <StateMachine :states="haltSm.states" :edges="haltSm.edges" />
    </Fig>

    <WaveTrace
      label="halt and flush — why the wait is split in two"
      :rows="haltTrace.rows"
      :notes="haltTrace.notes"
    />

    <h3 class="doc-h3">The halt word</h3>
    <p class="doc-p">
      <code>ecall</code> has <b>no source operand</b>. <code>a0</code> is a
      value the program <i>left behind</i>, so it is sampled from the writeback
      — and from the register file's <b>write port</b>, not from the MEM stage's
      intent, because a float or multiply result arrives through its own retire
      slot and would otherwise be invisible to the snoop.
    </p>

    <Fig
      caption="Lane 0 only, and only when lane 0 was active, because that is exactly what the golden model records. Reading it from the ecall's own rs1 reports x0; latching it at the halt captures a0 BEFORE it exists, because the instruction before an ecall writes a0 one cycle AFTER it retires."
      zoom
    >
      <BlockDiagram :nodes="haltWord.nodes" :edges="haltWord.edges" />
    </Fig>

    <h2 class="doc-h2">What is encoded but has no datapath</h2>
    <SpecTable
      :cols="noDatapath.cols"
      :rows="noDatapath.rows"
      caption="A build that cannot do something FAULTS rather than returning a plausible wrong answer — running simt_shfl.s against a HAS_SHFL = 0 build halts with cause 3, which is the fault working, not a regression. bar is the one exception and it should not be: with one wave per workgroup a no-op happens to be correct, and with more than one it is a race with no witness."
    />

    <Callout
      kind="trap"
      title="And one place where a gate that is off answers plausibly instead of faulting"
    >
      <p>
        <code>kht_fpu</code>'s lanes above <code>FLANES</code> return
        <b>zero</b>, because <code>FLANES &lt; LANES</code> has no walk
        sequencer to feed them — and zero is a plausible float answer, so a
        shader run on a reduced build gets silently wrong upper lanes and no
        fault. It is guarded by convention only: <code>FLANES</code> must equal
        <code>LANES</code> in any build that runs a shader.
      </p>
    </Callout>

    <h2 class="doc-h2">Traps, collected</h2>
    <p class="doc-p">
      Every row is a failure that <b>happened</b>, not one that was anticipated.
      They are listed because each looks like something else while you are in
      it.
    </p>

    <SpecTable :cols="trapTable.cols" :rows="trapTable.rows" />

    <Callout kind="rule" title="Probe the state; do not re-read the source">
      <p>
        Trap 4 cost two rounds of arithmetic guessing about which operand was
        wrong. What actually solved it was adding five fields to one trace line:
      </p>
      <p class="font-mono kt-text-caption">
        TR &lt;t&gt; LSU ln 0 ea 80000000 rgn 5 <b>vmem 1</b> lin 1 sc 2 sv1
        80000000 off 0 wd 1
      </p>
      <p>
        <code>vmem 0</code> on a <code>vsinw2</code> was the entire answer. The
        bench's <code>KHT_TRACE</code> block is bounded so a wedged run cannot
        fill the log.
      </p>
    </Callout>

    <Callout kind="note" title="Where the numbers are">
      <p>
        Every cost quoted on this page is out-of-context synthesis on
        <code>xcvu13p-fhgb2104-2L-e</code>, and each names its top and its ask
        because both moved during this work. The method, the full tables and the
        reporting rules are on
        <RouterLink to="/mpe/simt" class="doc-link">the SIMT PE page</RouterLink
        >; what the numbers are <i>worth</i> is on
        <RouterLink to="/mpe/simt/comparison" class="doc-link"
          >the comparison page</RouterLink
        >.
      </p>
    </Callout>
  </DocPage>
</template>
