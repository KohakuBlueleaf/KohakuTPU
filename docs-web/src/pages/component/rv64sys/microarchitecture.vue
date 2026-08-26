<script setup>
// ===========================================================================
// RV64 system core — microarchitecture.
// Presents docs/arch/cpu/rv64-sys/microarchitecture.md.
//
// Attribution rows: rv64_syscore as its own top under -flatten_hierarchy
// none, MEM_PRIM = block, xcvu13p-fhgb2104-2L-e, Vivado 2024.2,
// out-of-context SYNTHESIS (not placed, not routed), 3.333 ns request.
// In-context rows: ooc_sysnode_rv64.tcl 2, sysnode as the top, CPU_RV64 = 1,
// the default `rebuilt` flattening; build/node_sn64_p2_{util,hier,time}.rpt.
// Cycle figures: Verilator against independently written C++ models.
// ===========================================================================

/* --- five logical stages, six register boundaries ------------------------
 * A horizontal flow, so the components are tall and narrow: the stage on top,
 * the register that CLOSES it directly beneath. The sixth boundary is the
 * accented one, and it exists only because the register file is read-first. */
const PITCH = 13;
const stages = [
  {
    s: "F",
    ss: "next-PC select · the instruction memory and the predictor are addressed, through the fetch page register when translation is on",
    b: "pc",
    bs: "boundary 1 · plus d_instr_hold, the copy taken when fetch stops, and the fault flag beside it",
    a: false,
  },
  {
    s: "D",
    ss: "decode, combinational · the register file's address leaves, and so does the forward SELECT",
    b: "d_valid · d_pc",
    bs: "boundary 2 · the instruction's bits arrive from the array",
    a: false,
  },
  {
    s: "E",
    ss: "the forward mux, then ALU or muldiv or AMO or CSR · the branch resolves here too",
    b: "e_* and the forward selects",
    bs: "boundary 3 · the decoded instruction",
    a: false,
  },
  {
    s: "M",
    ss: "the data memory answers · load align and sign extend",
    b: "m_val · m_ld · m_off · m_wr",
    bs: "boundary 4 · the result, or a load's align control. FORWARD SOURCE 1",
    a: false,
  },
  {
    s: "W",
    ss: "the register file's write port",
    b: "wb_we · wb_rd · wb_val",
    bs: "boundary 5 · the value being written. FORWARD SOURCE 2",
    a: false,
  },
  {
    s: "W−1",
    ss: "the write that landed last cycle · nothing reads it but the forwarding network",
    b: "w_wr_q · w_rd_q · w_val_q",
    bs: "boundary 6 · the value written LAST cycle. FORWARD SOURCE 3",
    a: true,
  },
];

const boundaries = {
  nodes: [
    ...stages.map((t, i) => ({
      id: `s${i}`,
      x: i * PITCH,
      y: 0,
      w: 9,
      h: 11,
      label: t.s,
      sub: t.ss,
      accent: t.a,
    })),
    ...stages.map((t, i) => ({
      id: `b${i}`,
      x: i * PITCH,
      y: 15,
      w: 9,
      h: 8.5,
      label: t.b,
      sub: t.bs,
      accent: t.a,
    })),
  ],
  edges: [
    ...stages.slice(1).map((_, i) => ({
      from: `s${i}:r`,
      to: `s${i + 1}:l`,
    })),
    ...stages.map((_, i) => ({ from: `s${i}:b`, to: `b${i}:t`, dash: true })),
  ],
};

const occupancy = {
  cols: [
    { key: "c", label: "Class", mono: true },
    { key: "o", label: "Occupancy", align: "right", mono: true },
    { key: "l", label: "Latency", align: "right", mono: true },
    { key: "w", label: "Why" },
  ],
  rows: [
    {
      c: "ALU, shift, LUI, AUIPC",
      o: "1",
      l: "1",
      w: "forwarded from M",
    },
    {
      c: "load",
      o: "1",
      l: "<b>2</b>",
      w: "the data does not exist until M has run — one bubble if consumed immediately",
    },
    { c: "store", o: "1", l: "—", w: "no result" },
    {
      c: "branch, correctly predicted",
      o: "1",
      l: "—",
      w: "<b>no penalty</b>",
      _tone: "good",
    },
    {
      c: "branch or jump, predicted taken in D",
      o: "1",
      l: "—",
      w: "1 instruction killed",
    },
    {
      c: "branch or jump, mispredicted",
      o: "1",
      l: "—",
      w: "<b>2 killed</b> — the resolve is in E",
    },
    {
      c: "any CSR instruction",
      o: "<b>2</b>",
      l: "2",
      w: "the write data is registered",
    },
    { c: "lr", o: "<b>3</b>", l: "3", w: "read only" },
    { c: "amo*, sc", o: "<b>4</b>", l: "4", w: "read, modify, write" },
    {
      c: "mul, mulh, mulhsu, mulhu, mulw",
      o: "<b>8</b>",
      l: "8",
      w: "one 32×32 DSP reused four times, two-deep",
    },
    {
      c: "div, divu, rem, remu and the W forms",
      o: "<b>66</b>",
      l: "66",
      w: "restoring, one bit per cycle",
      _tone: "warn",
    },
    {
      c: "any access the wrapper stalls",
      o: "1 + the stall",
      l: "",
      w: "the request is issued in E, so <b>E is what holds</b>",
    },
  ],
};

/* --- the forwarding network ---------------------------------------------- */

const forward = {
  nodes: [
    {
      id: "m",
      x: 0,
      y: 0,
      w: 18,
      h: 4,
      label: "M — m_val",
      sub: "distance 1 · the result has not been written at all",
    },
    {
      id: "w",
      x: 0,
      y: 5.5,
      w: 18,
      h: 4,
      label: "W — w_data",
      sub: "distance 2 · the write is being presented THIS cycle",
    },
    {
      id: "wq",
      x: 0,
      y: 11,
      w: 18,
      h: 4,
      label: "W−1 — w_val_q",
      sub: "distance 3 · the write landed on the SAME EDGE as the read",
      accent: true,
    },
    {
      id: "rf",
      x: 0,
      y: 16.5,
      w: 18,
      h: 4,
      label: "rv64_regfile",
      sub: "distance 4 and beyond · the array returns it, so no fourth source",
    },
    {
      id: "mux",
      x: 27,
      y: 5.5,
      w: 14,
      h: 9.5,
      label: "the 4:1 mux",
      sub: "priority M, then W, then W−1, then the array",
      accent: true,
    },
    {
      id: "held",
      x: 48,
      y: 5.5,
      w: 14,
      h: 9.5,
      label: "op_held → op_rs1 · op_rs2",
      sub: "captured on the first stalled cycle, held for the rest of it",
      accent: true,
    },
    {
      id: "bub",
      x: 27,
      y: 18,
      w: 14,
      h: 4,
      label: "bubble",
      sub: "F and D hold; E drains into M",
      accent: true,
    },
  ],
  edges: [
    { from: "m:r", to: "mux:l" },
    { from: "w:r", to: "mux:l" },
    { from: "wq:r", to: "mux:l", accent: true },
    { from: "rf:r", to: "mux:l" },
    { from: "mux:r", to: "held:l", accent: true },
    { from: "m:r", to: "bub:l", label: "if it is a load", dash: true },
  ],
};

const distances = {
  cols: [
    { key: "d", label: "Distance", align: "center", mono: true },
    { key: "p", label: "Producer is in", align: "center" },
    { key: "s", label: "The source", mono: true },
    { key: "w", label: "Why the array cannot answer" },
  ],
  rows: [
    {
      d: "1",
      p: "M",
      s: "m_val",
      w: "the result has not been written at all — <b>and a load is excluded here, and only here</b>",
    },
    {
      d: "2",
      p: "W",
      s: "w_data",
      w: "the write is being presented <i>this</i> cycle",
    },
    {
      d: "3",
      p: "W−1",
      s: "w_val_q",
      w: "<b>the write landed on the same edge that captured the read</b>, and the array is read-first, so it returned the old value",
      _tone: "good",
    },
    {
      d: "4 and beyond",
      p: "already in the array",
      s: "—",
      w: "written an edge earlier, so the array does return it",
    },
  ],
};

const stalls = {
  cols: [
    { key: "n", label: "", mono: true },
    { key: "r", label: "Raised by" },
    { key: "f", label: "Freezes" },
    { key: "k", label: "Keeps moving" },
  ],
  rows: [
    {
      n: "<b>stall</b>",
      r: "a multiply or divide not done · an AMO not finished · a CSR write · the wrapper stalling a memory access",
      f: "F, D and E all hold; the forward selects freeze; <code>op_held</code> freezes the operands; the E→M register inserts a bubble",
      k: "<b>M and W drain.</b> <code>mcycle</code> and <code>mtime</code> keep counting",
    },
    {
      n: "<b>bubble</b>",
      r: "a load in E whose <code>rd</code> is a source of the instruction in D — <b>or <code>imem_stall</code></b>: fetch waiting for a translation, for a fence to retire, or for the privilege register to settle after a trap",
      f: "F and D hold",
      k: "<b>E drains into M</b> — which is the point: the load's data does not exist until M has run, and an instruction already in flight is not delayed by a translation it does not need",
    },
    {
      n: "d_redir",
      r: "a taken prediction in D",
      f: "—",
      k: "kills the one instruction already fetched behind the branch; <b>the branch itself continues into E to be checked</b>",
    },
    {
      n: "e_redir",
      r: "a mispredict resolved in E",
      f: "—",
      k: "kills the two behind it",
    },
    {
      n: "trap_redir",
      r: "a trap, <code>mret</code> or <code>sret</code> at an instruction boundary",
      f: "—",
      k: "kills the two behind it, and suppresses the trapping instruction's own writeback and CSR write. <b>Only the PC moves this cycle</b>: the CSR and privilege writes land the next one, and fetch is held for it",
    },
    {
      n: "halted",
      r: "a fault with no handler, or the external halt input",
      f: "<b>everything, W included</b>",
      k: "nothing",
      _tone: "bad",
    },
  ],
};

const drain = {
  cols: [
    { key: "s", label: "Stage", mono: true, align: "center" },
    { key: "e", label: "Its register's enable" },
    { key: "w", label: "What a stall does to it" },
  ],
  rows: [
    {
      s: "E",
      e: "the whole-pipeline enable",
      w: "holds — this is the instruction being stalled",
    },
    {
      s: "M",
      e: "the whole-pipeline enable, <b>plus an explicit clear on stall</b>",
      w: "takes a bubble: its write-enable and load bits are forced low",
    },
    {
      s: "W",
      e: "<b>not gated on the pipeline enable at all</b> — only on halt",
      w: "keeps advancing, every cycle",
      _tone: "good",
    },
    {
      s: "W−1",
      e: "the same as W",
      w: "keeps advancing, every cycle",
      _tone: "good",
    },
  ],
};

/* --- execute -------------------------------------------------------------- */

const exec = {
  nodes: [
    {
      id: "held",
      x: 0,
      y: 4.2,
      w: 16,
      h: 8,
      label: "op_rs1 · op_rs2",
      sub: "out of op_held",
      accent: true,
    },
    {
      id: "alu",
      x: 24,
      y: 0,
      w: 20,
      h: 3.4,
      label: "rv64_alu",
      sub: "1 cycle · one adder, one shifter",
    },
    {
      id: "md",
      x: 24,
      y: 4.2,
      w: 20,
      h: 3.4,
      label: "rv64_muldiv",
      sub: "8 cycles, or 66 · 4 DSP",
    },
    {
      id: "amo",
      x: 24,
      y: 8.4,
      w: 20,
      h: 3.4,
      label: "the AMO sequencer",
      sub: "3 cycles, or 4",
    },
    {
      id: "csr",
      x: 24,
      y: 12.6,
      w: 20,
      h: 3.4,
      label: "rv64_csr",
      sub: "2 cycles",
    },
    {
      id: "link",
      x: 24,
      y: 16.8,
      w: 20,
      h: 3.4,
      label: "e_pc + 4",
      sub: "the jal / jalr link value",
    },
    {
      id: "res",
      x: 52,
      y: 4.2,
      w: 16,
      h: 8,
      label: "e_result → m_val",
      sub: "one result, into the M register",
      accent: true,
    },
  ],
  edges: [
    { from: "held:r", to: "alu:l" },
    { from: "held:r", to: "md:l" },
    { from: "held:r", to: "amo:l" },
    { from: "held:r", to: "csr:l" },
    { from: "held:r", to: "link:l" },
    { from: "alu:r", to: "res:l" },
    { from: "md:r", to: "res:l" },
    { from: "amo:r", to: "res:l" },
    { from: "csr:r", to: "res:l" },
    { from: "link:r", to: "res:l" },
  ],
};

const addrFanout = {
  nodes: [
    {
      id: "ea",
      x: 0,
      y: 8.4,
      w: 15,
      h: 4.4,
      label: "ea = op_rs1 + e_imm",
      sub: "the 64-bit adder — about eight logic levels on its own",
      accent: true,
    },
    {
      id: "c1",
      x: 22,
      y: 0,
      w: 21,
      h: 3.6,
      label: "dmem_addr",
      sub: "NOT registered — a read must be issued now to be answered next cycle",
      accent: true,
    },
    {
      id: "c2",
      x: 22,
      y: 4.4,
      w: 21,
      h: 3.6,
      label: "misalign → the trap",
      sub: "registered",
    },
    {
      id: "c3",
      x: 22,
      y: 8.8,
      w: 21,
      h: 3.6,
      label: "strb → dmem_wstrb",
      sub: "registered",
    },
    {
      id: "c4",
      x: 22,
      y: 13.2,
      w: 21,
      h: 3.6,
      label: "m_off",
      sub: "the load-align shift amount, captured in E",
    },
    {
      id: "c5",
      x: 22,
      y: 17.6,
      w: 21,
      h: 3.6,
      label: "eff_q → mtval",
      sub: "registered",
    },
  ],
  edges: [
    { from: "ea:r", to: "c1:l", accent: true },
    { from: "ea:r", to: "c2:l" },
    { from: "ea:r", to: "c3:l" },
    { from: "ea:r", to: "c4:l" },
    { from: "ea:r", to: "c5:l" },
  ],
};

const mdFsm = {
  states: [
    { id: "IDLE", x: 0, y: 0, label: "S_IDLE" },
    { id: "MUL", x: 14, y: -7, label: "S_MUL" },
    { id: "DIV", x: 14, y: 7, label: "S_DIV" },
    { id: "FIN", x: 29, y: 0, label: "S_FIN" },
  ],
  edges: [
    { from: "IDLE", to: "MUL", label: "multiply", curve: 25 },
    { from: "MUL", to: "FIN", label: "cnt 5", curve: 25 },
    { from: "IDLE", to: "DIV", label: "divide", curve: -25 },
    { from: "DIV", to: "FIN", label: "cnt 63", curve: -25 },
    { from: "FIN", to: "IDLE", label: "release", curve: 120 },
  ],
};

const amoFsm = {
  states: [
    { id: "IDLE", x: 0, y: 0, label: "A_IDLE" },
    { id: "RD", x: 13, y: 0, label: "A_RD" },
    { id: "WR", x: 26, y: 0, label: "A_WR" },
    { id: "FIN", x: 39, y: 0, label: "A_FIN" },
  ],
  edges: [
    { from: "IDLE", to: "RD", label: "read + latch" },
    { from: "RD", to: "WR", label: "AMO, or SC ok" },
    { from: "WR", to: "FIN", label: "" },
    { from: "RD", to: "FIN", label: "lr, or SC fails", curve: 75 },
    { from: "FIN", to: "IDLE", label: "the result", curve: 120 },
  ],
};

/* --- the knobs ------------------------------------------------------------ */

const knobs = {
  cols: [
    { key: "k", label: "Knob", mono: true },
    { key: "d", label: "Default", mono: true, align: "right" },
    { key: "w", label: "What it moves" },
  ],
  rows: [
    {
      k: "HAS_ATOMIC",
      d: "1",
      w: "The AMO sequencer, the reservation register and three states inside execute. Dropping it measured <b>−776 LUT, 13.3 % of the core</b>, at essentially no change in frequency — <b>by far the largest single knob here</b>, and the only one that changes the instruction set.",
      _tone: "warn",
    },
    {
      k: "MEM_PRIM",
      d: '"distributed"',
      w: "The register file's primitive, and it is a measured trade rather than a preference — see the table below. <b>Not a free choice at <code>block</code>:</b> the array's clock-to-out becomes the binding path and no logic restructuring moves it.",
    },
    {
      k: "TAG_W",
      d: "11",
      w: "The BTB entry width, which is <code>1 + TAG_W + 38</code>. <b>This is the knob that can silently take the BTB out of block RAM</b> — at 34 the entry reaches 73 bits and the array becomes LUTs with no warning.",
      _tone: "warn",
    },
    {
      k: "RAS_DEPTH",
      d: "16",
      w: "<b>The one predictor knob that costs real LUT.</b> The top of stack is a flop so a prediction costs no array read; the rest is LUTRAM because a 16:1 mux on 64 bits would be roughly 320 LUT.",
    },
    {
      k: "BTB_ENTRIES",
      d: "256",
      w: "Depth in block RAM, not logic. Sized for a wide branch footprint rather than one loop.",
    },
    {
      k: "PHT_ENTRIES",
      d: "1024",
      w: "Depth in block RAM, not logic — and it buys <i>two</i> arrays, because the update reads the old counter through a mirror rather than stealing the lookup port.",
    },
    {
      k: "HIST_W",
      d: "8",
      w: "How many bits of global history are XORed into the direction index. Flops, and few of them.",
    },
    {
      k: "RESET_PC",
      d: "0",
      w: "Where fetch begins. No cost.",
    },
  ],
};

/* --- the hold ------------------------------------------------------------- */

const holdBroken = {
  rows: [
    { name: "hold", kind: "bit", values: [0, 1, 1, 0] },
    { name: "pc", kind: "bus", values: ["→C", "→C", "→C", "→C"] },
    { name: "imem_data", kind: "bus", values: ["B", "C", "C", "C"] },
    {
      name: "what D decodes",
      kind: "bus",
      values: ["B", "C", "C", "C"],
      mark: [1],
    },
  ],
  notes: [
    {
      cycle: 0,
      text: "B is in decode. The array is addressed by pc, which is already pointing past B — D always holds the word fetched from the PREVIOUS pc.",
    },
    {
      cycle: 1,
      text: "The hold freezes pc, and one cycle later the array is answering that frozen pc. B's word is gone from the array's output and nothing kept a copy.",
      tone: "bad",
    },
    {
      cycle: 2,
      text: "Decode is now working on C while every valid bit in the pipeline still says B is the instruction in D. B is never executed and C is executed twice, with no signal anywhere saying so.",
      tone: "bad",
    },
  ],
};

const holdFixed = {
  rows: [
    { name: "hold", kind: "bit", values: [0, 1, 1, 0] },
    { name: "imem_data", kind: "bus", values: ["B", "C", "C", "C"] },
    { name: "capture", kind: "bit", values: [0, 1, 0, 0], mark: [1] },
    {
      name: "d_instr_hold",
      kind: "bus",
      values: [null, "B", "B", "B"],
    },
    { name: "what D decodes", kind: "bus", values: ["B", "B", "B", "B"] },
  ],
  notes: [
    {
      cycle: 1,
      text: "The first cycle of the hold — and only the first, because a flag marks it taken — copies the array's output into a holding register.",
      tone: "good",
    },
    {
      cycle: 2,
      text: "Decode reads the copy for the rest of the hold. Both the register and the flag clear when fetch moves again.",
      tone: "good",
    },
    {
      text: "A flop-input stage holds by not clocking. An array-input stage keeps being handed new data whether it wants it or not, so it has to capture — which is why a hold is never simply a clock enable.",
      tone: "good",
    },
  ],
};

/* --- the stall, and its two widths ---------------------------------------- */

const drainBroken = {
  rows: [
    { name: "stall", kind: "bit", values: [0, 1, 1, 0] },
    { name: "E", kind: "bus", values: ["I3", "I3", "I3", "I3"] },
    {
      name: "M",
      kind: "bus",
      values: ["I2", "bubble", "bubble", "bubble"],
      mark: [1],
    },
    {
      name: "W, gated on the stall",
      kind: "bus",
      values: ["I1", "I1", "I1", "I1"],
      mark: [1],
    },
    {
      name: "registers written",
      kind: "text",
      values: ["I1", "", "", ""],
    },
  ],
  notes: [
    {
      cycle: 0,
      text: "I3 stalls in execute. I2 is in memory and I1 is in writeback — neither of them is the instruction being held.",
    },
    {
      cycle: 1,
      text: "The stall gates W too, so W does not capture I2. M is cleared to a bubble in the same cycle, which is exactly where the bubble belongs.",
      tone: "bad",
    },
    {
      cycle: 1,
      text: "I2's result existed in one place and it has just been overwritten by the bubble. Gating W did not DELAY the writeback — it threw it away, because M has already advanced past it and there is nothing left to re-present.",
      tone: "bad",
    },
    {
      cycle: 3,
      text: "The stall releases. I2 never wrote its register, and the next reader of it gets whatever was there before — a right value that disappeared.",
      tone: "bad",
    },
  ],
};

const operandBroken = {
  rows: [
    { name: "stall", kind: "bit", values: [0, 1, 1, 0] },
    {
      name: "select, frozen in D",
      kind: "bus",
      values: ["take M", "take M", "take M", "take M"],
    },
    {
      name: "M holds",
      kind: "bus",
      values: ["I2's result", "bubble", "bubble", "bubble"],
      mark: [1],
    },
    {
      name: "operand off the mux",
      kind: "bus",
      values: ["I2's result", "bubble", "bubble", "bubble"],
      mark: [1],
    },
    {
      name: "effective address",
      kind: "text",
      values: ["correct", "garbage", "garbage", "garbage"],
    },
  ],
  notes: [
    {
      cycle: 0,
      text: "The select was computed in decode and captured with the instruction. On this cycle it is right: the producer really is in M.",
    },
    {
      cycle: 1,
      text: "M drains — correctly; that is the rule the previous trace establishes. The frozen select now points at a stage holding something else entirely.",
      tone: "bad",
    },
    {
      cycle: 1,
      text: "Everything derived from the operand moves with it: the effective address, the branch comparison, the store data, the multiplier's inputs. A stalled load recomputes its address from a network that has moved, and the symptom is a spurious misalignment fault on an access that was aligned when it was issued — a wrong value that appeared.",
      tone: "bad",
    },
  ],
};

const stallFixed = {
  rows: [
    { name: "stall", kind: "bit", values: [0, 1, 1, 0] },
    {
      name: "M",
      kind: "bus",
      values: ["I2", "bubble", "bubble", "bubble"],
    },
    {
      name: "W, never gated",
      kind: "bus",
      values: ["I1", "I2", "bubble", "bubble"],
      mark: [1],
    },
    { name: "op_held capture", kind: "bit", values: [1, 0, 0, 0], mark: [0] },
    {
      name: "operand used",
      kind: "bus",
      values: ["I2's result", "the capture", "the capture", "the capture"],
    },
    { name: "registers written", kind: "text", values: ["I1", "I2", "", ""] },
  ],
  notes: [
    {
      cycle: 0,
      text: "Two things happen on the FIRST stalled cycle and both have to happen on this one. W captures I2 — the drain — and op_held captures both operands, because the forward mux is still correct in this cycle and the drain happens at the end of it.",
      tone: "good",
    },
    {
      cycle: 1,
      text: "I2 writes its register on schedule. Every later cycle of the stall reads the operand capture instead of the mux, and the capture releases when the stall does.",
      tone: "good",
    },
    {
      text: "The forwarding network is what makes a wrong value appear; the drain is what makes a right value disappear. If a value goes missing, check the drain before checking the forwarding.",
      tone: "good",
    },
  ],
};

/* --- start exactly once --------------------------------------------------- */

const startBroken = {
  rows: [
    { name: "a divide in E", kind: "bit", values: [1, 1, 1, 1, 1] },
    { name: "start", kind: "bit", values: [1, 1, 1, 1, 1], mark: [1, 2, 3, 4] },
    { name: "cnt", kind: "bus", values: ["0", "0", "0", "0", "0"], mark: [4] },
    { name: "done", kind: "bit", values: [0, 0, 0, 0, 0] },
  ],
  notes: [
    {
      cycle: 0,
      text: "The divide launches and the counter starts.",
    },
    {
      cycle: 1,
      text: "The instruction is still in E — it has to be, it is being executed — so the start condition, which is a LEVEL and not an edge, is still true. The unit restarts.",
      tone: "bad",
    },
    {
      cycle: 4,
      text: "The counter never leaves zero and done never asserts. The core stalls in execute forever, with a divider that looks busy and a program that has stopped.",
      tone: "bad",
    },
  ],
};

const startFixed = {
  rows: [
    { name: "a divide in E", kind: "bit", values: [1, 1, 1, 1, 1] },
    { name: "already fired", kind: "bit", values: [0, 1, 1, 1, 1], mark: [1] },
    { name: "start", kind: "bit", values: [1, 0, 0, 0, 0], mark: [0] },
    { name: "cnt", kind: "bus", values: ["0", "1", "2", "3", "…63"] },
    { name: "done", kind: "text", values: ["", "", "", "", "at 66"] },
  ],
  notes: [
    {
      cycle: 0,
      text: "start is the level ANDed with “not already fired”, so it is true for exactly one cycle.",
      tone: "good",
    },
    {
      cycle: 1,
      text: "The flag holds for the whole operation and clears when the instruction finally leaves E.",
      tone: "good",
    },
    {
      text: "Latch the operands on entry, and start exactly once. Both rules exist for the same reason: an instruction that holds E sees a pipeline that keeps moving underneath it, so neither its inputs nor its start condition can be re-read.",
      tone: "good",
    },
  ],
};

/* --- the predictor -------------------------------------------------------- */

const btbEntry = [
  { name: "valid", bits: 1, value: "1" },
  { name: "tag", bits: 11, value: "tag[10:0]" },
  { name: "target", bits: 38, value: "target[38:1] — Sv39", accent: true },
];

const btbWide = [
  { name: "valid", bits: 1, value: "1" },
  { name: "tag", bits: 11, value: "tag[10:0]" },
  { name: "target", bits: 64, value: "a full 64-bit target", accent: true },
];

const btbSpec = {
  cols: [
    { key: "f", label: "Field", mono: true },
    { key: "w", label: "Width", align: "right", mono: true },
    { key: "p", label: "Position", mono: true },
    { key: "o", label: "Owner" },
  ],
  rows: [
    {
      f: "valid",
      w: "1",
      p: "[49]",
      o: "the power-on sweep clears it; a resolve in E sets it. <b>Neither array has a reset</b>, which is why the sweep exists at all",
    },
    {
      f: "tag",
      w: "TAG_W",
      p: "[48:38]",
      o: "the resolve, from the branch's own PC above the index bits. It MAY alias — the tag is short on purpose",
    },
    {
      f: "target",
      w: "38",
      p: "[37:0]",
      o: "the resolve, as <code>target[38:1]</code>. <b>Bit 0 is not stored because an instruction address is even</b>, and bits above 38 are not stored because Sv39 is the real address space",
    },
  ],
};

const predStructures = {
  cols: [
    { key: "s", label: "Structure" },
    { key: "z", label: "Size", mono: true },
    { key: "p", label: "Primitive" },
    { key: "w", label: "Why" },
  ],
  rows: [
    {
      s: "BTB",
      z: "256 entries",
      p: "block RAM",
      w: "a wide branch footprint, not one loop",
    },
    {
      s: "gshare PHT",
      z: "1024 × 2-bit",
      p: "block RAM",
      w: "direction depending on data, not position",
    },
    {
      s: "PHT mirror",
      z: "1024 × 2-bit",
      p: "block RAM",
      w: "so the update reads the old counter without stealing the lookup port",
    },
    {
      s: "global history",
      z: "8 bits",
      p: "flops",
      w: "XORed into the PHT index",
    },
    {
      s: "RAS",
      z: "16 entries",
      p: "top of stack a flop, the rest LUTRAM",
      w: "a BTB predicts returns badly — a function called from N sites has N return targets and one entry thrashes between them",
    },
  ],
};

const predPop = {
  cols: [
    { key: "k", label: "" },
    { key: "a", label: "the RV32 PE's branches" },
    { key: "b", label: "this core's branches" },
  ],
  rows: [
    {
      k: "what runs",
      a: "one hot loop inside a compute unit",
      b: "a runtime: schedulers, allocators, drivers",
    },
    {
      k: "the footprint",
      a: "narrow — a handful of backedges, hit constantly",
      b: "<b>wide</b> — many branches, each hit rarely",
    },
    {
      k: "the directions",
      a: "positional, and a backedge is taken almost always",
      b: "<b>data-dependent</b>, so position predicts poorly",
    },
    {
      k: "the calls",
      a: "few, and often inlined",
      b: "<b>dense</b>, and a function is called from many sites",
    },
  ],
};

const predBuys = {
  cols: [
    { key: "p", label: "Program", mono: true },
    { key: "n", label: "No predictor", align: "right", mono: true },
    { key: "y", label: "With predictor", align: "right", mono: true },
    { key: "d", label: "", align: "right", mono: true },
  ],
  rows: [
    { p: "atomics", n: "8,415", y: "<b>7,507</b>", d: "−10.8 %" },
    { p: "hello_im", n: "666,352", y: "<b>645,778</b>", d: "−3.1 %" },
    {
      p: "dhry",
      n: "956,572",
      y: "<b>855,429</b>",
      d: "<b>−10.6 %</b>",
      _tone: "good",
    },
  ],
};

/* --- area ----------------------------------------------------------------- */

const peSplit = {
  cols: [
    { key: "i", label: "Instance", mono: true },
    { key: "l", label: "LUT", align: "right", mono: true },
    { key: "f", label: "FF", align: "right", mono: true },
    { key: "w", label: "What it is" },
  ],
  rows: [
    {
      i: "(rv64_syscore)",
      l: "464",
      f: "889",
      w: "the wrapper — the range decode, the two-phase handshake, the control region, the fetch page register",
    },
    { i: "u_core", l: "<b>5,944</b>", f: "3,218", w: "the pipeline" },
    { i: "u_l1", l: "349", f: "499", w: "the write-back L1's control; the arrays are block RAM and LUTRAM" },
    { i: "u_np", l: "288", f: "725", w: "the node port — a priority mux and one set of 256-bit AXI registers" },
    { i: "u_mmu", l: "151", f: "217", w: "the TLB's control and the walker; the array is one block RAM" },
    { i: "u_mbox", l: "138", f: "308", w: "the dispatch mailbox and its 16-deep completion queue" },
    { i: "<b>total</b>", l: "<b>7,334</b>", f: "<b>5,856</b>", w: "" },
  ],
};

const coreSplit = {
  items: [
    {
      label: "core glue — forwarding muxes, hazard logic, load align, the trap path",
      value: 2201,
    },
    { label: "u_md — multiply and divide, plus 4 DSP", value: 1366 },
    { label: "u_csr — the CSR file, trap state and privilege", value: 1304 },
    { label: "u_alu — one adder, one shared shifter", value: 539 },
    { label: "u_rf — the register file, in LUTRAM", value: 211 },
    { label: "u_bp — the predictor", value: 205 },
    { label: "u_dec — the decoder", value: 118 },
  ],
};

const rfContexts = {
  cols: [
    { key: "c", label: "How it was measured" },
    { key: "l", label: "LUT", align: "right", mono: true },
    { key: "w", label: "What that number is" },
  ],
  rows: [
    {
      c: "the <code>u_rf</code> row under <code>-flatten_hierarchy none</code>",
      l: "<b>211</b>",
      w: "<b>the array.</b> 31 × 64 bits mirrored — 4 Kbit of storage — and the little that surrounds it",
      _tone: "good",
    },
    {
      c: "<code>rv64_regfile</code> synthesised as its own top, <code>distributed</code>",
      l: "147",
      w: "the same array with no consumers at all, at an earlier RTL vintage",
    },
    {
      c: "the same module as its own top, <code>block</code>",
      l: "67 + 2 RAMB18",
      w: "the same array in the other primitive",
    },
    {
      c: "the <code>u_rf</code> row inside the assembled node, <i>rebuilt</i>",
      l: "<b>1,555</b>",
      w: "the array <b>plus the operand path re-parented onto it</b> — seven times the figure above, for the same storage",
      _tone: "warn",
    },
  ],
};

const structures = {
  cols: [
    { key: "c", label: "The change" },
    { key: "a", label: "Area" },
    { key: "f", label: "Frequency" },
  ],
  rows: [
    {
      c: "the branch predictor, added",
      a: "<b>+362 LUT, +2 BRAM</b>",
      f: "no change",
    },
    {
      c: "atomics — <code>HAS_ATOMIC</code> 1 against 0",
      a: "<b>+776 LUT</b>, 13.3 % of the core (5,824 against 5,048)",
      f: "none: 330.3 against 329.8",
    },
    {
      c: "the CSR file with traps and interrupts, added",
      a: "<b>+1,120 LUT, +724 FF</b>",
      f: "no change — it is not on the ALU path",
    },
    {
      c: "the register file at <code>block</code> instead of <code>distributed</code>",
      a: "−5 LUT, +2 BRAM",
      f: "<b>−59.6 MHz</b> (264.1 against 323.7)",
      _tone: "bad",
    },
    {
      c: "one shared shifter instead of three barrel shifters",
      a: "<b>−499 LUT</b> in <code>rv64_alu</code> (1,038 → 539)",
      f: "—",
      _tone: "good",
    },
    {
      c: "registering the DSP instead of driving it combinationally",
      a: "LUT <b>fell</b>",
      f: "<b>+47 MHz</b>",
      _tone: "good",
    },
    {
      c: "store replication instead of a barrel shift",
      a: "−235 LUT on <code>rv64_sys_pe</code>, −107 on <code>rv64_syscore</code>",
      f: "<b>+25.8</b> and <b>+15.9 MHz</b>",
      _tone: "good",
    },
    {
      c: "registering every consumer of the effective address",
      a: "<b>−227 LUT</b>",
      f: "<b>+13.8 MHz</b>, 17 failing paths to 0",
      _tone: "good",
    },
    {
      c: "<b>WARL masks on the sparse CSRs</b> — not storing a bit that is not implemented",
      a: "<code>u_csr</code> <b>1,476 → 1,267 LUT</b> and <b>1,222 → 849 FF</b>",
      f: "—",
      _tone: "good",
    },
    {
      c: "an installed-vector flag instead of a 64-bit <code>mtvec != 0</code> compare inside the trap decision",
      a: "—",
      f: "<b>node WNS −1.371 → −0.519</b>",
      _tone: "good",
    },
    {
      c: "the trap's state writes one cycle after the redirect, with a one-cycle fetch hold",
      a: "no LUT change of its own",
      f: "<b>a timing change</b> — it takes the address adder out of ~200 CSR clock enables",
      _tone: "good",
    },
  ],
};

const absent = {
  cols: [
    { key: "n", label: "Not built" },
    { key: "w", label: "Consequence" },
  ],
  rows: [
    {
      n: "speculative branch history, and any repair for it",
      w: "the predictor's history is stale by a resolve, which costs accuracy and never correctness — the tables can alias freely",
    },
    {
      n: "dual issue, register renaming, wide fetch",
      w: "one instruction word per cycle, one instruction in E, and the whole forwarding network assumes it",
    },
    {
      n: "a second write port on the register file",
      w: "write-after-write and write-after-read hazards cannot be constructed, so nothing checks for them",
    },
    {
      n: "a floating-point unit or a float register file",
      w: "there is no float register file to name",
    },
    {
      n: "an inferred memory primitive anywhere",
      w: "every array names its primitive, because read latency here is pipeline structure and inference can move it between tool versions",
    },
    {
      n: "a second clock domain",
      w: "the whole core is on one clock; crossing to the host or to DRAM is the AXI surface's job",
    },
  ],
};
</script>

<template>
  <DocPage
    title="RV64 system core microarchitecture"
    summary="Six register boundaries for five logical stages, and the sixth exists only because a read-first array returns the pre-write value on a same-edge read. Three forward sources counted from the memory primitive rather than from the stage diagram; three sequencers inside execute that all obey the same two rules; a trap that redirects in one cycle and writes its state in the next; and a predictor shaped for a runtime's branches rather than a loop's."
    domain="cpu"
    status="shipped"
    source="src/kohakuaccel/pe/rv64-sys/ · docs/arch/cpu/rv64-sys/microarchitecture.md"
  >
    <h2 class="doc-h2">What it owns</h2>
    <p class="doc-p">Four mechanisms, and every other section is one of them seen from a different angle.</p>
    <div class="grid gap-3 sm:grid-cols-2 my-5">
      <div class="card p-4">
        <div
          class="kt-text-caption font-semibold text-warm-800 dark:text-warm-200 mb-1"
        >
          Six register boundaries
        </div>
        <p class="kt-text-caption text-warm-500 dark:text-warm-400 leading-6">
          Five logical stages, and a sixth boundary that exists only because a
          read-first array answers a same-edge read with the old value.
        </p>
      </div>
      <div class="card p-4">
        <div
          class="kt-text-caption font-semibold text-warm-800 dark:text-warm-200 mb-1"
        >
          The forwarding network
        </div>
        <p class="kt-text-caption text-warm-500 dark:text-warm-400 leading-6">
          Three sources selected by <i>position</i>, with the select computed a
          stage early — and one latch pair that freezes them.
        </p>
      </div>
      <div class="card p-4">
        <div
          class="kt-text-caption font-semibold text-warm-800 dark:text-warm-200 mb-1"
        >
          Two stalls and three flushes
        </div>
        <p class="kt-text-caption text-warm-500 dark:text-warm-400 leading-6">
          Each one freezes a different set of stages, and the differences are
          the whole of the hazard design.
        </p>
      </div>
      <div class="card p-4">
        <div
          class="kt-text-caption font-semibold text-warm-800 dark:text-warm-200 mb-1"
        >
          Three sequencers inside execute
        </div>
        <p class="kt-text-caption text-warm-500 dark:text-warm-400 leading-6">
          Multiply-divide, atomics and the CSR write — all obeying the same two
          rules, for the same reason.
        </p>
      </div>
    </div>

    <p class="doc-p">
      How <code>rv64_core</code> is built, cycle by cycle. The
      <RouterLink to="/component/rv64sys" class="doc-link">RV64 system core</RouterLink>
      page is the contract; this page is the machine that keeps it, and
      everything on it is free to change. The core exists to host a runtime,
      which is what makes a return-address stack and a divider worth their area
      and makes a floating-point unit not.
    </p>

    <p class="doc-p">
      The shape that was rejected is <b>out-of-order retire</b> — a scoreboard
      or a pending bit, so that a 66-cycle divide occupies one instruction
      rather than the whole machine. It is refused, and not on area.
      <b>The hazard unit is the entire complexity budget of this core</b>, and
      every one of its three forward sources is <i>positional</i>: the
      distance-1 source is “whatever is in M”, not “the producer of this
      register”. Out-of-order retire ends that — a forward source stops being a
      stage and becomes a search, the operand latch stops being a single pair,
      and the select precomputed in decode stops being computable there at all,
      because decode would no longer know which stage its producer will be in.
      Every economy on this page is downstream of that one refusal.
    </p>

    <Callout kind="trap" title="“Five-stage” is the wrong summary">
      <p>
        Five is the logical decomposition. An instruction crosses <b>six</b>
        register boundaries, three of the stages contain their own
        sub-pipelines, and the deepest path through execute is
        <b>66 cycles</b>. Anything that models this as a uniform five-stage
        machine will mispredict both its frequency and its cycle count.
      </p>
    </Callout>

    <Callout kind="measured" title="Where the figures on this page come from">
      <p>
        <b>Out-of-context synthesis — not placed and not routed</b> — on
        <code>xcvu13p-fhgb2104-2L-e</code> under Vivado 2024.2, by
        <code>scripts/tcl/ooc_syscore.tcl</code> with
        <code>-flatten_hierarchy rebuilt</code>, at a
        <b>3.333 ns request</b>. Cycle figures come from Verilator 5.020 driving
        a C++ harness, against independently written models.
      </p>
      <p>
        Synthesis slack is optimistic and there is no routed result for anything
        here. <b>Never quote a LUT or an Fmax from a simulator, and never quote a
        cycle count from synthesis.</b>
      </p>
    </Callout>

    <h2 class="doc-h2">Five stages, six register boundaries</h2>

    <p class="doc-p">
      The sixth boundary, W−1, is not architectural — <b>nothing reads it but the
      forwarding network</b>. It exists because of what a synchronous array does
      with a write and a read on the same edge, and the forwarding network below
      is about exactly that.
    </p>

    <Fig
      caption="Five logical stages, six register boundaries. The accented column is the extra one: a copy of the value written last cycle, kept solely so a consumer three instructions behind its producer can be answered."
      zoom
      wide
    >
      <BlockDiagram :nodes="boundaries.nodes" :edges="boundaries.edges" />
    </Fig>

    <SpecTable
      :cols="occupancy.cols"
      :rows="occupancy.rows"
      caption="Occupancy is how many cycles an instruction holds E, which is what blocks everything behind it. Latency is how many cycles until a consumer can use the result"
    />

    <SpecTable
      :cols="knobs.cols"
      :rows="knobs.rows"
      caption="The knobs, in the order they matter. Two of them are worth real area and two of them can silently move an array out of block RAM; the rest buy depth in a primitive the design has spare"
    />

    <h2 class="doc-h2">Fetch, and the word that has to be captured</h2>

    <p class="doc-p">
      The whole address path in fetch is
      <code>pc → 2:1 mux → the arrays' address pins</code>, and nothing else.
      That is deliberate: the instruction memory and the predictor register their
      own address input, so any logic in front of the mux is logic in front of a
      memory, and the fetch loop closes only because there is none. The
      predictor's lookup goes out with the <b>fetch</b> address and its answer
      arrives with the instruction it describes.
    </p>

    <Callout kind="rule" title="Translation joins fetch as a register, not as a lookup">
      <p>
        With Sv39 on, the instruction array is addressed by the
        <b>translated</b> PC — and the translation comes from
        <b>one page register in the wrapper</b>, not from an instruction TLB.
        Consecutive fetches share a page, so one entry covers about a thousand
        instructions; the page number is a register read and the array's address
        path is unchanged.
      </p>
      <p>
        What that costs is a fifth reason for fetch to stall, and it joins the
        <b>bubble</b> rather than the stall: F and D hold while E drains, so no
        instruction enters execute until fetch can name one, and nothing already
        in flight waits for a translation it does not need. Fetch holds while a
        page is being resolved, for the cycle after a translation lands (the
        array is still answering the old address), for the cycle after
        <code>sfence.vma</code> retires, and for the cycle after a trap while the
        privilege register settles — because whether the new PC is translated
        depends on a privilege level that is one cycle stale. That last one is
        the
        <RouterLink to="/component/rv64sys" class="doc-link"
          >trap timing contract</RouterLink
        >.
      </p>
      <p>
        <b>The hold is also why the word D captures is not captured under it.</b>
        Under a translation stall the address on the array's pins is not yet a
        physical one, so the word on the bus belongs to nowhere; capturing it
        would pin that garbage in decode for the rest of the stall.
      </p>
    </Callout>

    <Callout kind="trap" title="A faulted fetch decodes as a NOP, and traps in execute">
      <p>
        A fetch whose page faulted still delivers a word — the wrapper installs
        the faulting page as a <i>poisoned</i> entry and lets the fetch through,
        because retrying the walk instead hangs the machine: fetch is stalled, so
        the core never reaches an instruction boundary, so it never takes the
        trap that would fix the mapping.
      </p>
      <p>
        The word that arrives is whatever the untranslated array held.
        <b>It is replaced with a NOP encoding before the decoder sees it</b>, and
        a fault flag travels beside it through the same holding register the
        instruction word uses. Decoded for real, it could issue a load or
        redirect fetch <i>before</i> the trap lands. The NOP reaches execute,
        traps there with cause 12, and <code>tval</code> is the PC that could not
        be fetched.
      </p>
    </Callout>

    <Callout kind="trap" title="Freezing the PC does not freeze the word decode is looking at">
      <p>
        The array is addressed by <code>pc</code>, and D holds the instruction
        fetched from the <i>previous</i> <code>pc</code>. One cycle after a hold
        the array is answering the frozen <code>pc</code>, and D's own word is
        gone.
      </p>
      <p>
        A holding register solves it: on the <b>first</b> cycle of a hold — and
        only the first, because a flag marks it taken — the instruction word is
        copied out of the array's output, and decode reads that copy for the rest
        of the hold.
      </p>
      <p>
        <b>That is the general shape of holding a stage whose input is a
        synchronous array, and it is why a hold is never simply a clock
        enable.</b> A flop-input stage holds by not clocking; an array-input
        stage keeps being handed new data whether it wants it or not, so it has
        to capture. The same trap is one register-file read away in any design
        that stalls a stage fed by block RAM.
      </p>
    </Callout>

    <Callout kind="rule" title="A stage fed by an array MUST capture its input on the first held cycle">
      <p>
        Holding <span class="chip">pc</span> is necessary and it is not
        sufficient. The stage <b>MUST</b> copy the array's output on the
        <b>first</b> cycle of the hold and <b>MUST NOT</b> copy it again on any
        later cycle — a second copy takes the wrong word, because by then the
        array is answering the frozen address.
      </p>
    </Callout>

    <WaveTrace
      :rows="holdBroken.rows"
      :notes="holdBroken.notes"
      variant="broken"
      label="Holding the PC alone — decode silently changes instruction under the hold"
    />

    <WaveTrace
      :rows="holdFixed.rows"
      :notes="holdFixed.notes"
      variant="fixed"
      label="One holding register, written once"
    />

    <Callout
      kind="trap"
      title="The symptom is an instruction that never executed, not a stall that went wrong"
    >
      <p>
        Nothing about the hold looks broken: the stall counts are right, the
        pipeline resumes, the program continues. One instruction has been
        skipped and its successor executed twice, which surfaces as a wrong
        result far from any memory or hazard logic — and only on inputs that
        happen to stall at that instruction.
      </p>
    </Callout>

    <h2 class="doc-h2">Why a read-first register file needs three forward sources</h2>

    <p class="doc-p">
      In-order, single issue, one write port. That settles most of the hazard
      question before it is asked: write-after-write and write-after-read cannot
      occur, and structural hazards are designed out rather than arbitrated — the
      register file is 2R1W as two mirrored single-read arrays, and instruction
      and data memories are separate ports. What is left is <b>read-after-write</b>,
      <b>control</b>, and <b>multi-cycle occupancy</b>.
    </p>

    <Fig
      caption="Four sources, selected by position and mutually exclusive by priority. The accented one is the third, and it is the one a textbook diagram does not have. The dashed path is the single exclusion in the whole network: a load's data does not exist at M's input, so a consumer one instruction behind a load takes a bubble instead."
      zoom
      wide
    >
      <BlockDiagram :nodes="forward.nodes" :edges="forward.edges" />
    </Fig>

    <SpecTable :cols="distances.cols" :rows="distances.rows" />

    <Callout kind="rule" title="Count your forward sources from the memory primitive, not from the stage diagram">
      <p>
        Work the third one through. Instruction <i>I</i> is in E at cycle
        <i>T</i>; its operand address left D during <i>T−1</i>, and the array at
        read latency 1 returns the data at <i>T</i>.
        <i>I−3</i> wrote the register file at the edge ending <i>T−1</i> —
        <b>the same edge that captured I's read</b>. The array is
        <b>read-first</b>: a write and a read of one address on one edge return
        the <i>old</i> value. <i>I−4</i> wrote one edge earlier, so the array does
        return it, and no fourth source is needed.
      </p>
      <p>
        <b>A write-first array needs two sources, a read-first array needs three,
        and a flop file with a write-through port needs two — and nothing in the
        pipeline drawing tells you which you are looking at.</b> Get it wrong in
        the cheap direction and the core is incorrect for exactly one
        producer-to-consumer spacing, which is the kind of bug a casual test
        suite does not reach. The co-simulation behind this core covers every
        spacing by construction for that reason.
      </p>
      <p>
        That is the price of putting the register file in a memory primitive
        rather than in flops, and it is paid <b>once in a mux</b> rather than
        continuously in LUTRAM.
      </p>
    </Callout>

    <Callout kind="note" title="The select is computed in D, and it is exact">
      <p>
        The three selects compare register numbers <b>in decode</b>, against E, M
        and W <i>as they are in that cycle</i> — not against where the
        instruction's producers will be. That is exact rather than approximate
        because <b>the pipeline shifts exactly one stage per cycle</b>: “compare
        against E, M and W now” and “compare against M, W and W−1 when I get to
        E” are the same comparison, one cycle apart.
      </p>
      <p>
        The motivation is timing. Comparing in E put the comparator, the 4:1 mux
        and the ALU in one cycle and made the comparator the binding path. Moving
        it to D leaves E with a mux and measured <b>−156 LUT and +12.1 MHz</b>,
        with byte-identical cycle counts on all three test programs — which is
        the correctness argument a pure timing transform owes.
      </p>
    </Callout>

    <Callout kind="rule" title="A forwarding network describes the pipeline for exactly one cycle">
      <p>
        M and W keep draining while E is held, so all three sources move on
        underneath frozen selects. Reading the mux on a later cycle returns
        another instruction's result. The answer is one latch pair,
        <code>op_held</code>, in front of everything E does with its operands: on
        the <b>first</b> stalled cycle the mux is still correct — the drain
        happens at the <i>end</i> of that cycle — so that is the cycle to
        capture.
      </p>
      <p>
        Everything downstream inherits the fix at once: the effective address,
        the branch comparison, the store data, the AMO's address and source, the
        CSR write data, and the multiplier's operands. Without it, a stalled load
        recomputes its address from a network that has moved, and the symptom is
        a spurious misalignment fault on an access that was aligned when it was
        issued.
      </p>
      <p>
        <b>Any structure that holds an instruction longer than one cycle must
        take a copy on entry</b>, and doing it once structurally is cheaper and
        safer than each unit remembering to.
      </p>
    </Callout>

    <h2 class="doc-h2">Two stalls, three flushes, and what each one freezes</h2>

    <SpecTable
      :cols="stalls.cols"
      :rows="stalls.rows"
      caption="Two enables carry all of it: one gates the whole pipeline and is low when the core is halted or stalled; the other gates fetch and decode alone and is additionally low on a bubble. Every register in the core takes one of the two. Priority is trap, then mispredict, then prediction"
    />

    <Callout kind="rule" title="The kill and the redirect are deliberately different signals">
      <p>
        The E stage's own valid bit is cleared by the trap-or-mispredict pair
        only, <i>not</i> by the redirect. That asymmetry is what lets a D-stage
        prediction steer fetch while the branch that caused it still travels into
        E to be checked against what the predictor said.
        <b>Gate the valid bit on the redirect instead and a correctly predicted
        branch kills itself.</b>
      </p>
    </Callout>

    <h3 class="doc-h3">Why M and W drain through a stall</h3>

    <SpecTable :cols="drain.cols" :rows="drain.rows" />

    <Callout kind="rule" title="A stall belongs to the stage that raised it">
      <p>
        The instruction already in M is not the one being held. Gating W on the
        pipeline enable does not <i>delay</i> its writeback — it
        <b>throws the writeback away</b>, because M has already advanced past it
        and there is nothing left to re-present. The same mistake one stage
        further down, on the W−1 copy, is equally invisible and moves nothing.
      </p>
      <p>
        <b>Every stage downstream of the one that stalled keeps draining.</b> A
        pipeline where a stall freezes everything is only correct if nothing
        downstream holds state the frozen stage will not re-present, and a
        registered writeback is exactly such state.
      </p>
      <p>
        The debugging corollary is worth as much as the rule: <b>if a value goes
        missing, check the drain before checking the forwarding.</b> The
        forwarding network is what makes a wrong value appear; the drain is what
        makes a right value disappear.
      </p>
    </Callout>

    <h3 class="doc-h3">Getting the width of a stall wrong, in both directions</h3>

    <p class="doc-p">
      Those last two sentences are two different bugs, and a stall applied at the
      wrong width produces one or the other. Freeze too much and a value that
      was correct is destroyed. Freeze too little — the stage but not its
      operands — and a value that was never correct is used. Both traces below
      hold the same instruction for the same two cycles.
    </p>

    <WaveTrace
      :rows="drainBroken.rows"
      :notes="drainBroken.notes"
      variant="broken"
      label="Freezing W as well — I2's writeback is destroyed, not delayed"
    />

    <WaveTrace
      :rows="operandBroken.rows"
      :notes="operandBroken.notes"
      variant="broken"
      label="Freezing the stage but not the operands — the mux moves under a frozen select"
    />

    <WaveTrace
      :rows="stallFixed.rows"
      :notes="stallFixed.notes"
      variant="fixed"
      label="W drains and the operands are captured — both on the first stalled cycle"
    />

    <Callout
      kind="trap"
      title="The first stalled cycle is the only cycle either fix can be applied on"
    >
      <p>
        Both corrections land in the same cycle and for the same reason: it is
        the last cycle in which the pipeline still describes the instruction
        being held. W has to capture M before the bubble replaces it, and the
        operand latch has to read the forward mux before the drain moves it.
        <b>A cycle later, neither the value nor the network that produced it
        still exists.</b>
      </p>
      <p>
        Which is why <span class="chip">op_held</span> is structural rather than
        per-unit, and the two forms coexist: the atomics sequencer still latches
        its own address and source operand, because it needs them across state
        transitions, but it no longer <i>has</i> to. The structural latch is
        what a stallable memory forces — once the wrapper can hold any access,
        <i>every</i> memory instruction is multi-cycle, and a plain load has no
        per-unit place to put a latch of its own.
      </p>
    </Callout>

    <Callout kind="measured" title="The one data stall: load-use">
      <p>
        The interlock fires only when the instruction in E is a load writing a
        real register and the instruction in D reads that register. Forwarding a
        load from M means putting the byte align, the sign extension and the
        forward mux in front of the ALU in one cycle — measured at
        <b>121 failing paths at 20 logic levels</b>. One cycle later, from W, it
        is an ordinary registered value.
      </p>
      <p>
        <b>Cost: +4.6 % cycles on Dhrystone</b>, because a scheduling compiler
        fills most load delay slots. One cycle of IPC to keep the memory
        alignment network off the ALU path — and the predictor then more than
        repaid it.
      </p>
    </Callout>

    <h2 class="doc-h2">What execute has to fit into one cycle</h2>

    <Fig
      caption="One instruction, five things it might feed, one result. The three that take more than a cycle all live inside E and all obey the same two rules: latch the operands on entry, and start exactly once."
      zoom
      wide
    >
      <BlockDiagram :nodes="exec.nodes" :edges="exec.edges" />
    </Fig>

    <Callout kind="rule" title="The ALU is one adder and one shifter">
      <p>
        <b>Subtract is add-with-inverted-operand, and the carry out of that same
        adder <i>is</i> the unsigned compare.</b> With subtract asserted the
        adder computes <code>a - b</code> and its carry is set exactly when
        <code>a &gt;= b</code>, so <code>SLTU</code> is the inverse of a wire
        rather than a second 64-bit comparator. <code>SLT</code> differs from it
        only by the sign correction.
      </p>
      <p>
        <b>One arithmetic right shifter covers <code>SLL</code>,
        <code>SRL</code>, <code>SRA</code> and all three <code>W</code>
        forms.</b> A left shift is a right shift between two bit reversals, and a
        reversal is wiring. Written as three separate expressions — <code>&lt;&lt;</code>,
        <code>&gt;&gt;</code>, <code>&gt;&gt;&gt;</code> — synthesis builds
        <b>three 64-bit barrel shifters</b>: the shared form measured
        <b>539 LUT against 1,038</b> for the three, on the same exhaustively
        verified behaviour.
      </p>
      <p>
        The <code>W</code> forms are the same hardware, and <b>every</b>
        <code>W</code> result is sign-extended from bit 31 once, at the output —
        including <code>SLTU</code>'s, which is zero or one and extends to
        itself. Stating it once is cheaper and safer than deciding per operation.
      </p>
    </Callout>

    <h3 class="doc-h3">The effective address, and the one consumer that is not registered</h3>

    <Fig
      caption="The address adder is roughly eight logic levels on its own against a budget of about eleven for a whole path, so anything it feeds combinationally starts two thirds spent. Only the read address escapes registration, because a read has to be issued in the first cycle to be answered in the second."
      zoom
    >
      <BlockDiagram :nodes="addrFanout.nodes" :edges="addrFanout.edges" />
    </Fig>

    <Callout kind="rule" title="Nothing address-derived may reach the stall">
      <p>
        <code>stall</code> gates every pipeline register's enable and fans out
        across the whole front end, including the predictor's stack pointer. The
        core therefore exports a <b>decode-only</b> memory request: a single bit
        meaning <i>the instruction in E is a load, a store or an atomic</i>,
        derived from the E-stage control bits and nothing else. No address, no
        misalignment test, no range decode.
      </p>
      <p>
        The visible consequence: <b>a misaligned access issues its transaction
        and then traps.</b> That is harmless here — a misaligned store already
        emits no byte strobes, and a read has no side effect on this fabric — and
        it is what makes a misaligned access trap <i>once</i>, after the
        transaction retires, rather than on every cycle it is held.
      </p>
    </Callout>

    <Callout kind="rule" title="The store path replicates, it does not shift">
      <p>
        The byte strobes already select which lanes are written, so a sub-word
        datum only has to be <b>present</b> in the lane it lands in — it does not
        have to be moved there. A byte is replicated eight times, a halfword
        four, a word twice.
      </p>
      <p>
        The obvious alternative — shift the datum left by the address's low bits
        — is a <b>64-bit barrel shifter fed by the forward mux</b>, sitting on
        the path from the writeback register to a memory's data pins.
        Replication is <b>wiring</b>: a fan-out, not a mux. It measured
        <b>−235 LUT and +25.8 MHz</b> on the mesh compute unit and
        <b>−107 LUT and +15.9 MHz</b> on the node processor — one expression,
        both units, area <i>and</i> frequency.
      </p>
      <p>
        <b>The general move: when a datum has to reach a position, ask whether
        anything downstream already selects position.</b> If it does, put the
        datum everywhere and let the existing selector do the work. It is worth
        nothing where you would have to build the selector to use it.
      </p>
    </Callout>

    <h2 class="doc-h2">Three sequencers, and the two rules they all obey</h2>

    <p class="doc-p">
      Multiply-divide, atomics and the CSR write all take more than a cycle, all
      live inside E, and all obey the same two rules: <b>latch the operands on
      entry</b>, and <b>start exactly once</b>. Both exist for the same
      underlying reason — an instruction that holds E sees a pipeline that keeps
      moving underneath it, so neither its inputs nor its start condition can be
      re-read.
    </p>

    <Fig
      caption="A multiply issues four operand pairs on counts zero through three and runs to a count of five, because the last two cycles drain the DSP's own pipeline. Each product's range and validity travel alongside it, two deep, so the accumulator knows which quarter of the 128-bit result an arriving product belongs to without recomputing it from a counter that has already moved on."
      zoom
    >
      <StateMachine :states="mdFsm.states" :edges="mdFsm.edges" :r="40" />
    </Fig>

    <Callout kind="rule" title="Why the multiplier is 4 DSP and not 9 to 16">
      <p>
        A flat 64×64 product wants 9 to 16 DSP48s. This issues <b>four 32×32
        partial products through one multiplier over four cycles</b> and
        accumulates them by range, which measures <b>4 DSP</b> — the number that
        keeps the whole system node inside its 48-DSP ceiling with 35 already
        spent elsewhere.
      </p>
      <p>
        <b>A DSP48E2 is a pipelined primitive, and using it combinationally
        forfeits frequency for no area gain.</b> Driven combinationally, the path
        <code>count → operand mux → DSP → accumulator add</code> was
        <b>23 logic levels and 11 CARRY8 in one cycle</b> and held the whole core
        to 216.5 MHz. Registering it — two stages, which is the shape the
        primitive wants — bought <b>+47 MHz, and the LUT count fell</b>.
      </p>
      <p>
        <b>The registers you put around a hard multiplier are the ones already
        inside it.</b> Vivado absorbs them; you pay flip-flops the device has in
        abundance and get the primitive's rated frequency. The same is true of a
        block RAM's output register.
      </p>
      <p>
        Two economies inside the accumulate: <b>each partial lands in its own
        range</b>, so the adder is as wide as the range rather than as wide as
        the product; and <b>the signed correction is 64 bits wide, not 128</b>,
        because both subtrahends are shifted left by 64 and cannot borrow into
        the low half.
      </p>
    </Callout>

    <Callout kind="rule" title="A sequencer MUST start on an edge it manufactures, never on the condition that selected it">
      <p>
        The instruction sits in E for the whole operation, so the condition that
        launched it is still true on every cycle of it. The unit
        <b>MUST</b> qualify its start with a one-bit
        <i>already fired</i> flag, and that flag <b>MUST</b> clear only when the
        instruction leaves E — not when the unit finishes, and not on a stall.
      </p>
    </Callout>

    <WaveTrace
      :rows="startBroken.rows"
      :notes="startBroken.notes"
      variant="broken"
      label="Starting on the level — the divide restarts every cycle and never finishes"
    />

    <WaveTrace
      :rows="startFixed.rows"
      :notes="startFixed.notes"
      variant="fixed"
      label="One already-fired flag — the level becomes an edge"
    />

    <Callout kind="trap" title="A relaunching sequencer presents as a hang, and a relaunching atomic presents as thousands of writes">
      <p>
        For a divide the failure is silent and total: the counter never leaves
        zero, <span class="chip">done</span> never asserts, and the core stalls
        in execute forever with a divider that looks busy. Nothing is illegal,
        so nothing asserts.
      </p>
      <p>
        The atomics sequencer has the same rule and a louder symptom. Its state
        is reset <b>only when the AMO is gone</b>, never merely because memory
        is slow — reset it on a stalled cycle and the sequence restarts,
        re-issuing every phase it had already completed, so a single
        <span class="chip">amoadd</span> becomes thousands of writes. The
        general form of that one is worth carrying away on its own:
        <b>narrowing an <span class="chip">if</span> changes its
        <span class="chip">else</span>.</b>
      </p>
      <p>
        Divide is restoring, one bit per cycle, on magnitudes with the signs
        reapplied at the end, and it handles the two cases the specification
        mandates: divide by zero, and −2⁶³ ÷ −1. <code>DIVW</code> sign-extends
        its operands where <code>DIVUW</code> zero-extends them; reversing that
        is the classic RV64M bug and it shows only on negative inputs.
      </p>
    </Callout>

    <h3 class="doc-h3">Why the divider is built here and refused on the RV32 PE</h3>

    <p class="doc-p">
      The
      <RouterLink to="/component/rv32pe/microarchitecture" class="doc-link"
        >RV32 PE</RouterLink
      >
      costs a divider at 200–300 LUT and turns it down. The reasoning is sound
      and this core reaches the opposite answer, which is worth understanding
      because <b>neither answer is about the divider</b>.
    </p>

    <SpecTable
      :cols="[
        { key: 'k', label: '' },
        { key: 'a', label: 'RV32 controller PE' },
        { key: 'b', label: 'RV64 system core' },
      ]"
      :rows="[
        {
          k: 'what runs on it',
          a: 'one kernel, chosen and scheduled ahead of time',
          b: 'a runtime, executing whatever it is given',
        },
        {
          k: 'where a divide appears',
          a: 'in a kernel the author can restructure',
          b: 'in allocation, time conversion, and code the author of this core never sees',
        },
        {
          k: 'the option to not have it',
          a: 'real — strength-reduce it away at compile time',
          b: '<b>absent.</b> A runtime cannot decline to execute an instruction its compiler emitted',
        },
        {
          k: 'the marginal cost',
          a: 'its own subtractor, its own remainder and quotient registers, its own sign fixups',
          b: '<b>near zero</b> — it shares the multiplier\'s sequencer, its counter, its operand latches and its finish state',
          _tone: 'good',
        },
      ]"
      caption="Once a core has committed to a multi-cycle unit in E with a sequencer, an operand latch and a start-once flag, the divider is an extra state and a subtractor rather than a new structure. The general form: a multi-cycle unit is priced by what the machine already has, not by what the unit does"
    />

    <h3 class="doc-h3">Why an atomic holds execute for three or four cycles</h3>

    <Fig
      caption="A_IDLE issues the read and latches the address and the source operand, because both come through the forwarding network and it is valid only in that cycle. The sequencer advances only when memory is not stalling, and its state is reset only when the AMO is gone — never merely because memory is slow."
      zoom
    >
      <StateMachine :states="amoFsm.states" :edges="amoFsm.edges" :r="40" />
    </Fig>

    <Callout kind="trap" title="Narrowing an if changes its else">
      <p>
        Resetting the sequencer on a stalled cycle restarts the sequence and
        re-issues every phase it had already completed — a single
        <code>amoadd</code> becoming thousands of writes. That is the general
        form of the trap, and it is worth carrying past this unit.
      </p>
      <p>
        <code>LR</code>/<code>SC</code> carry a <b>single reservation</b>: this
        is one hart, so the only ways to lose it are another <code>LR</code> or
        any <code>SC</code>, which is exactly what the specification allows.
      </p>
    </Callout>

    <Callout kind="rule" title="Why a CSR instruction costs two cycles">
      <p>
        Fan-out, and nothing else. Driven combinationally, the write data would
        run <code>writeback value → forward mux → operand → the
        read-modify-write → every CSR register's data pins</code> — one path into
        thirteen 64-bit registers. So the instruction stalls E for one cycle
        while its write data is captured. <b>The read is unaffected</b>: the read
        data is combinational on the registered address, so a CSR read still
        returns the pre-write value, which is what the instruction owes.
      </p>
      <p>
        The write enable is deliberately <b>narrower</b> than “not stalled and
        not trapping”. Both of those carry the misalignment test, which carries
        the forward mux and the address adder — and putting the adder on a CSR
        register's clock enable measured 17 logic levels. A CSR instruction is
        never a load, a store or an atomic, so <b>the only things that can
        legitimately kill its write are an illegal CSR address and a pending
        interrupt</b>, and those are the only two the enable tests.
      </p>
      <p>
        <b>When a guard is too wide, the fix is to enumerate what can actually
        fire rather than to reuse a convenient aggregate.</b> The aggregate drags
        in every term its other users needed.
      </p>
    </Callout>

    <h2 class="doc-h2">Why the predictor is bigger than the RV32 PE's, and differently shaped</h2>

    <p class="doc-p">
      Not “the same thing, larger”. The two cores' predictors answer different
      branch populations, and each of this one's three additions is a response to
      a specific property of runtime code that a kernel loop does not have.
    </p>

    <SpecTable :cols="predPop.cols" :rows="predPop.rows" />

    <SpecTable
      :cols="predStructures.cols"
      :rows="predStructures.rows"
      caption="Note which of the three additions is free. The BTB and the direction table are block RAM, so entry count buys depth rather than logic; the stack is the one that costs real LUT, and it is the one whose contribution is currently unmeasured"
    />

    <h3 class="doc-h3">The target is 39 bits, and that is a block-RAM decision</h3>

    <Fig
      caption="What is built: 50 bits, and it maps to a block RAM. Sv39 makes 39 bits the real address space and an instruction address is even, so target[38:1] loses nothing — the answer is reassembled as {25'd0, stored, 1'b0}. The module's own header comment says 51; the expression beside it is 1 + TAG_W + 38, and the expression is the one that synthesises."
    >
      <BitField :fields="btbEntry" />
    </Fig>

    <Fig
      caption="What a full 64-bit target would make: 76 bits. A block-RAM port is 72 bits at its widest, so this array would silently become LUTs — no error, no warning, and a structure that should have cost zero logic costing hundreds."
    >
      <BitField :fields="btbWide" />
    </Fig>

    <SpecTable
      :cols="btbSpec.cols"
      :rows="btbSpec.rows"
      caption="Positions are at the defaults, TAG_W = 11. Every field is written by the resolve in execute and by the power-on sweep, and read only by the prediction path — which is the reason none of it has to be correct. E resolves every branch against the real answer, so an aliased tag or a stale counter costs a redirect and never a wrong result"
    />

    <p class="doc-p">
      The 22 bits between 50 and 72 are the design margin, and the knob that
      spends them is <span class="chip">TAG_W</span>. There is no other: the
      target field is fixed by the address space and the valid bit is one bit.
      At <span class="chip">TAG_W = 34</span> the entry reaches 73 and the array
      becomes LUTs — which nothing in the build will tell you.
    </p>

    <Callout kind="rule" title="A block-RAM port is 72 bits at its widest, and an array one bit over becomes LUTs silently">
      <p>
        It is a property of the device rather than of this design. It decides the
        shape of this entry, it decides the shape of the
        <RouterLink to="/component/rv64sys/memory-system" class="doc-link"
          >TLB entry</RouterLink
        >, and it is why every array in this core names its primitive instead of
        being inferred.
      </p>
      <p>
        The practical form: <b>check that the synthesis report says block RAM
        where you expected block RAM.</b> A 74-bit ROM elsewhere in this tree came
        back as 2,798 LUT and zero block RAM for exactly this reason.
      </p>
    </Callout>

    <h3 class="doc-h3">Nothing here is architectural</h3>

    <p class="doc-p">
      E resolves every branch against the real answer, so a wrong prediction
      costs the redirect penalty and never correctness. Three consequences
      follow, and all three are why the structure is as cheap as it is: the tag
      can be short and the tables can alias; <b>history updates on the resolve,
      not on the prediction</b>, so there is no speculative state to repair; and
      <b>the resolve is registered on the way in</b>, because E's comparator
      driving a read-modify-write of a saturating counter is a long path for
      something non-architectural.
    </p>

    <p class="doc-p">
      Neither array has a reset, so a <b>power-on sweep</b> writes every entry
      before a prediction is allowed out: the BTB to all-zero, the PHT to
      <code>01</code> — weakly not-taken. A jump forces its counter to
      <code>11</code> rather than incrementing it; a conditional branch saturates
      up or down by one.
    </p>

    <SpecTable
      :cols="predBuys.cols"
      :rows="predBuys.rows"
      caption="For +362 LUT and 2 BRAM. This is what the BTB and the direction table deliver together; the return-address stack's contribution is not separated out"
    />

    <Callout kind="trap" title="The return stack is complete, and its answer reaches the wrong instruction">
      <p>
        The stack itself works: a call pushes the link address, a return pops it,
        and the top of stack is a flop precisely so a prediction costs no array
        read. <b>The answer is a two-way mux — the stack, or the BTB — and the two
        inputs to that mux describe different instructions.</b> The select and
        the stack data are <b>registered one cycle</b>, so they describe the
        instruction that was in decode <i>last</i> cycle; the BTB's hit test is
        computed from the decode-stage PC and is not registered, so it describes
        the instruction in decode <i>now</i>.
      </p>
      <p>
        Work a return through. It is in decode at cycle <i>T</i>: the stack pops
        at <i>T</i>, and the answer offered at <i>T</i> is the BTB's. The stack's
        answer is offered at <i>T+1</i>, to the return's fall-through, which is
        not usually a control instruction at all. <b>As built, a return is
        predicted from its own BTB entry</b> — the case a stack exists to avoid.
      </p>
      <p>
        This is an accuracy question and not a correctness one, for the reason
        above. It is also why the stack's share of the −10.6 % is expected to be
        near zero as built, and why no separate measurement of it exists.
      </p>
    </Callout>

    <h2 class="doc-h2">Why the register file is LUTRAM and not block RAM</h2>

    <p class="doc-p">
      31 × 64, two reads and one write, as <b>two mirrored single-read arrays
      written identically</b> — a simple dual-port RAM has one read port, so two
      reads means two copies. The storage doubles; the LUT count does not.
      <code>x0</code> is not stored: refusing the write costs one AND gate and
      removes the case where a stale <code>x0</code> can exist at all.
    </p>

    <Callout kind="measured" title="Block-RAM clock-to-out is slow, and no logic restructuring moves it">
      <p>
        At <code>block</code> the binding path is the array's own clock-to-out,
        and it stayed the top path through two rounds of optimisation — first
        into the forward mux, then into the <code>jalr</code> adder — before the
        primitive changed. <code>distributed</code> takes the array out of the
        path for <b>5 LUT</b> and <b>+59.6 MHz</b> (323.7 against 264.1), and
        takes 67 failing paths to zero.
      </p>
      <p>
        <b>That 5 LUT is not a stable price.</b> The same swap cost 89 LUT before
        the forward select moved to D. Removing logic from a path changes what
        the next change to that path is worth, and the two measurements are not
        comparable — the later one is the real price.
      </p>
    </Callout>

    <h2 class="doc-h2">Where the area is</h2>

    <SpecTable
      :cols="peSplit.cols"
      :rows="peSplit.rows"
      caption="rv64_syscore as its own top under -flatten_hierarchy none, where a row is a true attribution rather than a re-parenting. The breakdown sums to its own total, which is the check to apply before trusting any breakdown at all — if it does not sum, rows from two different runs have been mixed"
    />

    <ResourceBars
      :items="coreSplit.items"
      unit="LUT · inside u_core, 5,944 total"
      caption="The same run, and these seven sum to 5,944 exactly. Read it for its shape: the forwarding-and-hazard glue, multiply-and-divide and the CSR file are each over a thousand LUT, and the decoder and the register file together are under four hundred"
    />

    <Callout kind="trap" title="The same register file is 211 LUT and 1,555 LUT, in two reports of the same design">
      <p>
        The array is <b>4 Kbit of storage</b> — 31 usable entries of 64 bits,
        mirrored — and nothing about 4 Kbit costs fifteen hundred LUTs. Under
        <code>none</code> it reports 211. Inside the assembled node, where
        synthesis runs with the default <b>rebuilt</b> flattening, the same
        instance reports 1,555.
      </p>
      <p>
        What happens is <b>re-parenting</b>: synthesis dissolves module
        boundaries to optimise across them and attributes the resulting leaves
        to whichever boundary survives nearest. <code>u_rf</code> sits between
        the register file read and the E stage that consumes it, which is
        exactly where the forwarding muxes and the operand select live.
      </p>
      <p>
        <b>The evidence is in the same report rather than in the argument:</b>
        under <code>rebuilt</code>, <code>u_alu</code> and <code>u_dec</code>
        have <b>no row at all</b>, while under <code>none</code> they have 539
        and 118. Their logic did not disappear; it was absorbed into neighbours,
        and <code>u_rf</code> is one of the neighbours.
      </p>
      <p>
        A second symptom of the same thing: under <code>rebuilt</code> the
        children of <code>u_core</code> sum to <b>6,179</b> against a parent
        row of <b>6,169</b>. A LUT shared between two children is charged to
        both and counted once above them, so a rebuilt breakdown can over-count
        by exactly the shared logic. <b>Totals reconcile; attributions do not
        have to.</b>
      </p>
    </Callout>

    <SpecTable
      :cols="rfContexts.cols"
      :rows="rfContexts.rows"
      caption="Four correct numbers for one module. A row in a hierarchical report is an attribution, not a measurement, and under a flattening flow the attribution is approximate by construction. When the question is what does this module cost, synthesise it as its own top or read a `none` row; when it is what does this design cost, read the total"
    />

    <h3 class="doc-h3">What each structure costs</h3>

    <SpecTable
      :cols="structures.cols"
      :rows="structures.rows"
      caption="Each row is a measured difference between two synthesis runs of the same design with one thing changed. They were taken at different absolute baselines, so the differences are the result and the absolutes are not comparable across rows"
    />

    <Callout kind="rule" title="Two of those rows are the ones to carry away">
      <p>
        <b>Architecturally visible state is the expensive part of a core.</b>
        Two dozen 64-bit CSRs is fifteen hundred flip-flops before any logic,
        and <code>mcycle</code>, <code>mtime</code> and <code>minstret</code>
        are three 64-bit increments on top. So the file implements the set the
        runtime uses rather than the set the specification permits — and where a
        register is implemented, <b>the bits that are not are not stored</b>,
        which is the WARL row above and is worth two hundred LUT and four
        hundred flops on its own.
      </p>
      <p>
        <b>Area and frequency moving the same way is the signature of removing
        logic</b> rather than trading it. The last three rows all do it, and all
        three are the same shape of change: something late in the datapath had
        been let into a path it did not belong in.
      </p>
    </Callout>

    <h3 class="doc-h3">Where the frequency goes</h3>

    <p class="doc-p">
      The binding path has moved repeatedly, and each move is a general lesson
      rather than a fix: a <b>pipelined primitive used combinationally</b>; a
      <b>block RAM's clock-to-out</b>, which no restructuring moves; a
      <b>comparator in the same cycle as the mux it selects and the ALU behind
      it</b>; and, repeatedly and with different endpoints,
      <b>the forward mux reaching through the 64-bit address adder into something
      late</b> — a range decode, a byte-write enable, a CSR write enable, the
      predictor's stack pointer, the fetch page register's clock enable.
    </p>

    <p class="doc-p">
      Privilege and translation added three more of the same shape, and all
      three were fixed the same way — <b>by narrowing what the adder is allowed
      to reach</b>, never by re-timing the endpoint the tool named.
      <span class="chip">medeleg</span> indexed by the cause itself was a 64:1
      mux downstream of the adder, <b>28 levels and −3.496 ns</b>; the core now
      selects among bits the CSR file has already indexed with constants. The
      trap decision as the enable of every CSR register was <b>18 levels</b>;
      the writes moved a cycle later. And the MMU's <span class="chip">busy</span>
      driven from the raw TLB hit landed on every CSR write enable at
      <b>25 levels and −3.842 ns</b>; it is register-derived now, at the price of
      one cycle per translated access.
    </p>

    <Callout kind="measured" title="A design has a critical region, and one endpoint rarely names it">
      <p>
        Both synthesis scripts therefore report <b>every</b> negative-slack path,
        collapsed to <code>startpoint → endpoint</code> and counted per group,
        worst group first. At one point that report read
      </p>
      <p class="font-mono kt-text-caption">
        @@@FAILN 68<br />
        @@@GROUP 64 paths worst −0.076 lvl 11 wb_val_reg → u_bp/ras_tos_reg<br />
        @@@GROUP 4 paths worst −0.076 lvl 11 wb_val_reg → u_bp/ras_sp_reg
      </p>
      <p>
        — 68 failures and <b>one</b> region, ending in the predictor. The root
        was six gates upstream in the misalignment test, the last
        address-derived term in <code>stall</code>, and removing that one term
        killed all 68 at once and bought <b>+37.8 MHz</b>. Fixing the endpoint
        the tool named would have moved it somewhere else, which is what had
        already happened four times.
      </p>
      <p>
        <b>Logic levels are the screen, not slack.</b> Anything above 11 levels
        is bad whatever the slack says: a path at −0.017 ns and 14 levels is not
        “nearly passing”, it is fragile. The 64-bit address adder alone is about
        eight of the eleven.
      </p>
    </Callout>

    <Callout kind="measured" title="The binding path today, in both contexts">
      <p>
        <b>The processor alone</b>, as its own top under
        <code>-flatten_hierarchy none</code> at the 3.333 ns request:
        <b>−0.465 ns at 14 logic levels</b>.
      </p>
      <p class="font-mono kt-text-caption">
        u_core/u_rf/z1 → forward mux → address adder → misalign<br />
        → the trap decision → the vector select → pc
      </p>
      <p>
        That is the path the design already names, and it is structural: there
        is <b>no address-generation stage between E and M</b>, so the adder and
        the trap decision share a cycle. Every economy in the trap path — the
        installed-vector flags instead of a 64-bit compare, delegation selected
        from pre-indexed constants, the state writes moved a cycle later — was
        made against this chain.
      </p>
      <p>
        <b>Inside the assembled node the same chain is not the binding path</b>,
        and the node <b>meets its 3.333 ns request</b>: worst slack
        <b>+0.039 ns with 0 failing endpoints</b>. The path that gets closest is
        the processor's — the writeback register into the core's halt-cause
        register, 12 levels — and it passes.
      </p>
      <p class="font-mono kt-text-caption">
        u_cpu/u_core/wb_val_reg[1]/C → u_cpu/u_core/halt_cause_reg[1]/D
      </p>
      <p>
        Until the last run the node was bound elsewhere entirely: the memory
        mover's mode register through its <code>fifo_room</code> calculation
        into the command FIFO's write enable, at −0.081 ns and 12 levels, with
        all 123 failing endpoints in that one cone.
        <b>Registering the room limit took the add-and-compare out of the
        admission path and closed it</b> — that was the last failing path in the
        node, which is why the figure above is a positive one.
      </p>
      <p>
        <b>Both figures are synthesis.</b> Meeting the request here means
        <i>300 MHz in out-of-context synthesis</i> and nothing more — the design
        has not been placed or routed, and synthesis slack is optimistic. It is
        not closed timing, and no higher frequency follows from it.
      </p>
    </Callout>

    <h2 class="doc-h2">Three things refused, and why</h2>

    <Callout kind="rule" title="No address-generation stage — and it costs a cycle on every access">
      <p>
        The RV32 PE computes the effective address in EX and accesses the array
        in MEM, so the address arrives a whole stage early and everything
        downstream of it has a registered address to work from. This core
        computes the address in E and consumes the data in M with nothing
        between, so <b>every consumer of the address is one adder-delay behind
        the signal it has to drive</b>.
      </p>
      <p>
        Adding the stage costs a seventh register boundary, a stage deeper for
        every branch and trap redirect to kill, and a re-derivation of the whole
        three-source forwarding network. Registering each consumer individually
        costs <b>one extra cycle on every access, the local scratchpad
        included</b>. The second is what is built.
      </p>
      <p>
        <b>A pipeline that computes an address and consumes it in the very next
        stage has no slack anywhere downstream of the adder, and pays for that in
        either a boundary or a cycle.</b> Which is cheaper depends on how much the
        redirect path is already carrying — here the predictor and the trap logic
        both kill two instructions, and a seventh boundary would make that three.
      </p>
    </Callout>

    <Callout kind="rule" title="No scoreboard — a multi-cycle unit stalls E">
      <p>
        Which is why a divide costs 66 cycles of the whole machine rather than 66
        cycles of one instruction. It is refused, and not on area:
        <b>the hazard unit is the whole of this core's complexity budget</b> —
        three forward sources selected by position, two stall rules, nothing else
        — and every one of those sources is <i>positional</i>. The distance-1
        source is “whatever is in M”, not “the producer of this register”.
      </p>
      <p>
        Out-of-order retire ends that. A forward source stops being a stage and
        starts being a search, <code>op_held</code> stops being a single latch
        pair, and the select precomputed in D stops being computable there at
        all, because D would no longer know which stage its producer will be in.
      </p>
      <p>
        <b>A multi-cycle unit is cheap in a machine that already has a way to
        park an instruction and expensive in one whose whole complexity budget is
        positional forwarding.</b> The SIMT PE is the contrast that proves it: it
        carries multi-cycle units cheaply because barrel scheduling had already
        given it one pending bit per wave before any multi-cycle unit was
        proposed.
      </p>
    </Callout>

    <Callout kind="open" title="No hit-under-miss — and it is one change that reprices three modules">
      <p>
        The core stalls in E for the whole of a memory access, and that single
        property is load-bearing three modules away: it is what lets the
        node-port arbiter be a priority mux rather than a queue, because
        <b>at most one client can ever be active</b>.
      </p>
      <p>
        A non-blocking L1 is the single largest lever on the fabric-latency
        numbers — each unit of fabric latency currently costs each access one
        core cycle, with nothing overlapped. But it does not arrive alone. It
        needs a miss-status file, it needs the arbiter to become real arbitration
        with per-client response routing, and it needs the core to be able to
        park an instruction, which is the section above.
      </p>
    </Callout>

    <h2 class="doc-h2">Changing one</h2>

    <p class="doc-p">
      Most of this core is not parameterised, and that is honest rather than
      lazy: the three forward sources, the two enables and the six boundaries
      are load-bearing for each other, and a knob over any of them would be a
      knob that has to be verified in both positions. What follows is the order
      to work in when you do change something: each step depends on the answer
      to the one above it, and getting them out of order costs a re-measurement
      rather than a correction.
    </p>

    <ol class="doc-p list-decimal pl-5 space-y-1">
      <li>
        <b>Decide first whether the change ends positional forwarding.</b> If an
        instruction can retire out of order, or a producer can be in a stage
        decode cannot name, then the three-source network, the precomputed
        select and the single operand latch all stop working together — and you
        are designing a different core, not tuning this one.
      </li>
      <li>
        <b>Count your forward sources from the memory primitive, not from the
        stage diagram.</b> A write-first array needs two, a read-first array
        needs three, and a flop file with a write-through port needs two.
        Nothing in the pipeline drawing tells you which you are looking at, and
        getting it wrong in the cheap direction is incorrect for exactly one
        producer-to-consumer spacing.
      </li>
      <li>
        <b>For every new structure that can hold an instruction longer than one
        cycle, ask what it re-reads.</b> Its operands must be captured on entry
        and its start must be an edge you manufacture. Both failures are shown
        as traces above, and both are silent in simulation until the exact
        producer-to-consumer spacing that exposes them.
      </li>
      <li>
        <b>For every new stall, name the stage that raised it</b> and confirm
        everything downstream still drains. Then check whether the stage it
        freezes is fed by an array, because an array-fed stage has to capture as
        well as hold.
      </li>
      <li>
        <b>Recompute any array entry's width and compare it against 72</b>
        before synthesising. Then read the primitive column of the report, not
        the LUT column.
      </li>
      <li>
        <b>For anything the trap decision enables, ask how wide the enable
        is.</b> That decision carries the address adder through the misalignment
        test, so it may be the enable of a flag and not of a register file: the
        state a trap writes is registered a cycle behind it, delegation is
        selected from bits indexed by constants, and <i>is a handler installed</i>
        is a flag set by the CSR write rather than a compare done at the trap.
        Each of those was a 15-to-28-level path before it was narrowed.
      </li>
      <li>
        <b>Measure the change out of context, and re-measure anything you
        measured before it.</b> Removing logic from a path changes what the next
        change to that path is worth — the register-file primitive swap cost
        89 LUT before the forward select moved to decode and 5 LUT after, and
        only the later figure is the real price.
      </li>
      <li>
        <b>Check cycle counts on all three test programs, byte-identical</b>,
        for anything claimed to be a pure timing transform. That is the
        correctness argument such a change owes, and it is cheap to produce.
      </li>
    </ol>

    <Callout kind="open" title="Open questions the flow does not answer">
      <p>
        <b>There is no routed result for anything on this page.</b> Every
        frequency here is out-of-context synthesis, which is optimistic, and the
        ranking of two options can survive routing while the numbers do not.
        Nothing in the flow tells you which of these figures would move.
      </p>
      <p>
        <b>The return-address stack's contribution is unmeasured.</b> It is the
        one predictor structure that costs real LUT, it is
        <i>currently connected to the wrong instruction</i>, and the reported
        predictor gain is what the BTB and the direction table deliver together.
        So the stack cannot be priced against what it buys, in either its
        current state or a fixed one.
      </p>
      <p>
        <b>Nothing checks an array entry against the primitive it names.</b> The
        arithmetic in step 5 is done by hand on every array in this core, and
        the failure it prevents is invisible in simulation and silent in
        synthesis.
      </p>
      <p>
        <b>And the hierarchical utilisation report cannot answer “what does this
        module cost”.</b> Under a flattening flow a row is an attribution rather
        than a measurement — <span class="chip">u_rf</span> reports over 2,000
        LUT and is not the register file, while
        <span class="chip">u_alu</span> and
        <span class="chip">u_dec</span> have no row at all. The only way to ask
        the question is to synthesise the module as its own top, and the flow
        does not do that for you.
      </p>
    </Callout>

    <h2 class="doc-h2">What this microarchitecture deliberately does not have</h2>

    <SpecTable :cols="absent.cols" :rows="absent.rows" />
  </DocPage>
</template>
