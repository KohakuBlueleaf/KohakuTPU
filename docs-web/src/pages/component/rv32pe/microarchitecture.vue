<script setup>
// ===========================================================================
// RV32 PE — microarchitecture.
// Drawn from the RTL under src/kohakuaccel/pe/rv32/ and from
// docs/arch/cpu/rv32-pe/microarchitecture.md.
//
// Every resource and frequency figure on this page comes from ONE run:
//   scripts/tcl/ooc_rv_pe.tcl, top rv_pe, xcvu13p-fhgb2104-2L-e,
//   Vivado 2024.2, out-of-context SYNTHESIS (not placed, not routed),
//   -flatten_hierarchy none, 2.500 ns request, shipped configuration.
// Per-unit numbers are the hierarchical LUT-SITE column of that run.
// ===========================================================================

/* --- the six register boundaries ----------------------------------------
 * A horizontal flow, so the components are tall and narrow: the stage on top,
 * the register that CLOSES it directly beneath. Five architectural stages,
 * six boundaries, and the two extra ones are the two synchronous arrays. */
const PITCH = 13;
const stages = [
  {
    s: "IF1",
    ss: "next-PC select",
    b: "imem address reg",
    bs: "boundary 1 · a synchronous array costs a cycle between address and data",
    a: true,
  },
  {
    s: "IF2",
    ss: "instruction out · decode, combinationally",
    b: "regfile address reg",
    bs: "boundary 2 · the same cost, paid a second time",
    a: true,
  },
  {
    s: "ID",
    ss: "operands out · forwarding",
    b: "x_op1 · x_op2 · x_target",
    bs: "boundary 3 · the ID → EX register",
    a: false,
  },
  {
    s: "EX",
    ss: "ALU · multiply · branch resolve · effective address",
    b: "m_val · m_addr · m_be",
    bs: "boundary 4 · the EX → MEM register",
    a: false,
  },
  {
    s: "MEM",
    ss: "array address · write enables",
    b: "w_val · w_off · w_region",
    bs: "boundary 5 · the MEM → WB register",
    a: false,
  },
  {
    s: "WB",
    ss: "array data out · commit",
    b: "rf_we · rf_wa · rf_wd",
    bs: "boundary 6 · the register-file write port",
    a: false,
  },
];

const boundaries = {
  nodes: [
    ...stages.map((t, i) => ({
      id: `s${i}`,
      x: i * PITCH,
      y: 0,
      w: 9,
      h: 10,
      label: t.s,
      sub: t.ss,
      accent: i === 3,
    })),
    ...stages.map((t, i) => ({
      id: `b${i}`,
      x: i * PITCH,
      y: 14,
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

/* --- EX: the ALU --------------------------------------------------------- */

/* Both operands feed all five units. Two sources on the same side of a column
 * cannot both reach two shared boxes without a crossing (the four wires form a
 * cycle whose two diagonals meet), so x_op2 sits in front and fans to every
 * unit, and x_op1 is drawn with its top and bottom wires only, passing above
 * and below x_op2. The two boxes share a centre line so the slot sort falls
 * back to list order: x_op1 must take the OUTER lane on add and on bw. */
const alu = {
  nodes: [
    {
      id: "op1",
      x: 0,
      y: 7.7,
      w: 10,
      h: 3.6,
      label: "x_op1",
      sub: "rs1, PC, or zero",
    },
    {
      id: "op2",
      x: 13,
      y: 7.7,
      w: 10,
      h: 3.6,
      label: "x_op2",
      sub: "rs2 or the immediate",
    },

    {
      id: "add",
      x: 27,
      y: 0,
      w: 15,
      h: 3.4,
      label: "sum = x_op1 + x_op2",
      sub: "ONE 32-bit adder",
      accent: true,
    },
    {
      id: "sub",
      x: 27,
      y: 3.9,
      w: 15,
      h: 3.4,
      label: "diff = x_op1 − x_op2",
      sub: "A_SUB only",
    },
    {
      id: "cmp",
      x: 27,
      y: 7.8,
      w: 15,
      h: 3.4,
      label: "eq · lt · ltu",
      sub: "also the branch unit's",
      accent: true,
    },
    {
      id: "shf",
      x: 27,
      y: 11.7,
      w: 15,
      h: 3.4,
      label: "one 33-bit shifter",
      sub: "SLL · SRL · SRA",
      accent: true,
    },
    {
      id: "bw",
      x: 27,
      y: 15.6,
      w: 15,
      h: 3.4,
      label: "^   |   &",
      sub: "one LUT level, no sharing",
    },

    {
      id: "mux",
      x: 46,
      y: 0,
      w: 11,
      h: 19,
      label: "alu_r",
      sub: "case (x_alu) — ten encodings, five inputs",
      accent: true,
    },
    {
      id: "out",
      x: 61,
      y: 7.8,
      w: 13,
      h: 3.4,
      label: "into the result mux",
      sub: "where the multiply joins",
      accent: true,
    },
  ],
  edges: [
    { from: "op1:t", to: "add:l" },
    { from: "op2:t", to: "add:l" },
    { from: "op2:t", to: "sub:l" },
    { from: "op2:r", to: "cmp:l" },
    { from: "op2:b", to: "bw:l" },
    { from: "op2:b", to: "shf:l" },
    { from: "op1:b", to: "bw:l" },
    { from: "add:r", to: "mux:l" },
    { from: "sub:r", to: "mux:l" },
    { from: "cmp:r", to: "mux:l" },
    { from: "shf:r", to: "mux:l" },
    { from: "bw:r", to: "mux:l" },
    { from: "mux:r", to: "out:l", accent: true },
  ],
};

const aluOps = {
  cols: [
    { key: "e", label: "x_alu", mono: true, align: "center" },
    { key: "i", label: "Instructions", mono: true },
    { key: "hw", label: "What produces it", mono: true },
    { key: "sh", label: "Shared with" },
  ],
  rows: [
    {
      e: "A_ADD",
      i: "ADD ADDI LUI AUIPC · every load, store and JALR address",
      hw: "sum",
      sh: "<b>five other consumers</b> — the diagram below",
    },
    { e: "A_SUB", i: "SUB", hw: "diff", sh: "nothing" },
    {
      e: "A_SLT",
      i: "SLT SLTI",
      hw: "{31'd0, lt}",
      sh: "<code>br_cond</code> for BLT / BGE",
    },
    {
      e: "A_SLTU",
      i: "SLTU SLTIU",
      hw: "{31'd0, ltu}",
      sh: "<code>br_cond</code> for BLTU / BGEU",
    },
    {
      e: "—",
      i: "BEQ BNE",
      hw: "eq",
      sh: "<code>br_cond</code> only — no ALU result",
    },
    {
      e: "A_SLL",
      i: "SLL SLLI",
      hw: "shift_r",
      sh: "<b>the one shifter</b>",
      _tone: "good",
    },
    {
      e: "A_SRL",
      i: "SRL SRLI",
      hw: "shift_r",
      sh: "<b>the one shifter</b>",
      _tone: "good",
    },
    {
      e: "A_SRA",
      i: "SRA SRAI",
      hw: "shift_r",
      sh: "<b>the one shifter</b>",
      _tone: "good",
    },
    { e: "A_XOR", i: "XOR XORI", hw: "x_op1 ^ x_op2", sh: "nothing" },
    { e: "A_OR", i: "OR ORI", hw: "x_op1 | x_op2", sh: "nothing" },
    { e: "A_AND", i: "AND ANDI", hw: "x_op1 &amp; x_op2", sh: "nothing" },
  ],
};

const adderFanout = {
  nodes: [
    {
      id: "a",
      x: 0,
      y: 9.4,
      w: 14,
      h: 4,
      label: "sum",
      sub: "the one 32-bit adder",
      accent: true,
    },
    {
      id: "c1",
      x: 20,
      y: 0,
      w: 19,
      h: 3.2,
      label: "alu_r default",
      sub: "ADD · ADDI · LUI · AUIPC",
    },
    {
      id: "c2",
      x: 20,
      y: 4.2,
      w: 19,
      h: 3.2,
      label: "ex_addr",
      sub: "the arrays' address pins, this cycle",
      accent: true,
    },
    {
      id: "c3",
      x: 20,
      y: 8.4,
      w: 19,
      h: 3.2,
      label: "m_addr",
      sub: "captured into the MEM register",
    },
    {
      id: "c4",
      x: 20,
      y: 12.6,
      w: 19,
      h: 3.2,
      label: "act_target",
      sub: "JALR: {sum[31:1], 1'b0}",
    },
    {
      id: "c5",
      x: 20,
      y: 16.8,
      w: 19,
      h: 3.2,
      label: "misalign",
      sub: "sum[0] and sum[1:0]",
    },
    {
      id: "c6",
      x: 20,
      y: 21,
      w: 19,
      h: 3.2,
      label: "be_n · sd_n",
      sub: "byte enables, store-data replicate",
    },
  ],
  edges: [
    { from: "a:r", to: "c1:l" },
    { from: "a:r", to: "c2:l", accent: true },
    { from: "a:r", to: "c3:l" },
    { from: "a:r", to: "c4:l" },
    { from: "a:r", to: "c5:l" },
    { from: "a:r", to: "c6:l" },
  ],
};

const shifter = {
  nodes: [
    {
      id: "w",
      x: 0,
      y: 2,
      w: 12,
      h: 3.2,
      label: "x_op1",
      sub: "the 32-bit word",
    },
    {
      id: "r1",
      x: 16,
      y: 0,
      w: 13,
      h: 3.2,
      label: "rev32( )",
      sub: "wiring · 0 LUT",
    },
    {
      id: "m1",
      x: 33,
      y: 2,
      w: 13,
      h: 3.2,
      label: "sh_left ?",
      sub: "one 32-bit 2:1 mux",
      accent: true,
    },
    {
      id: "sgn",
      x: 0,
      y: 7.5,
      w: 12,
      h: 3.6,
      label: "sh_sign",
      sub: "A_SRA && x_op1[31]",
    },
    {
      id: "cat",
      x: 33,
      y: 7.5,
      w: 13,
      h: 3.6,
      label: "{ sh_sign, word }",
      sub: "33 bits",
      accent: true,
    },
    {
      id: "shift",
      x: 33,
      y: 13,
      w: 13,
      h: 4,
      label: "$signed >>> sh",
      sub: "ONE shifter",
      accent: true,
    },
    {
      id: "sh",
      x: 50,
      y: 13,
      w: 13,
      h: 4,
      label: "sh = x_op2[4:0]",
      sub: "the shift amount",
    },
    {
      id: "r2",
      x: 16,
      y: 19,
      w: 13,
      h: 3.2,
      label: "rev32( )",
      sub: "wiring · 0 LUT",
    },
    {
      id: "m2",
      x: 33,
      y: 19,
      w: 13,
      h: 3.2,
      label: "sh_left ?",
      sub: "one 32-bit 2:1 mux",
      accent: true,
    },
    {
      id: "res",
      x: 50,
      y: 19,
      w: 13,
      h: 3.2,
      label: "shift_r",
      sub: "all three shifts",
      accent: true,
    },
  ],
  edges: [
    { from: "w:r", to: "r1:l" },
    { from: "w:r", to: "m1:l" },
    { from: "r1:r", to: "m1:l" },
    { from: "m1:b", to: "cat:t" },
    { from: "sgn:r", to: "cat:l", accent: true },
    { from: "cat:b", to: "shift:t", accent: true },
    { from: "sh:l", to: "shift:r" },
    { from: "shift:b", to: "r2:t" },
    { from: "shift:b", to: "m2:t" },
    { from: "r2:r", to: "m2:l" },
    { from: "m2:r", to: "res:l", accent: true },
  ],
};

const shiftWorked = {
  cols: [
    { key: "op", label: "x_alu", mono: true },
    { key: "l", label: "sh_left", mono: true, align: "center" },
    { key: "s", label: "sh_sign", mono: true, align: "center" },
    { key: "in", label: "sh_in — 33 bits", mono: true },
    { key: "out", label: "sh_out", mono: true },
    { key: "r", label: "shift_r", mono: true },
  ],
  rows: [
    {
      op: "A_SRL",
      l: "0",
      s: "0",
      in: "0_8000_00F0",
      out: "0_0800_000F",
      r: "<b>0800_000F</b>",
    },
    {
      op: "A_SRA",
      l: "0",
      s: "<b>1</b>",
      in: "<b>1</b>_8000_00F0",
      out: "1_F800_000F",
      r: "<b>F800_000F</b>",
      _tone: "good",
    },
    {
      op: "A_SLL",
      l: "<b>1</b>",
      s: "0",
      in: "0_<b>0F00_0001</b>",
      out: "0_00F0_0000",
      r: "<b>0000_0F00</b>",
      _tone: "good",
    },
  ],
};

const branchUnit = {
  nodes: [
    { id: "eq", x: 0, y: 0, w: 13, h: 3.2, label: "eq", sub: "x_op1 == x_op2" },
    {
      id: "lt",
      x: 0,
      y: 4,
      w: 13,
      h: 3.2,
      label: "lt",
      sub: "signed less-than",
    },
    {
      id: "ltu",
      x: 0,
      y: 8,
      w: 13,
      h: 3.2,
      label: "ltu",
      sub: "unsigned less-than",
    },
    {
      id: "f3hi",
      x: 18,
      y: -4.5,
      w: 13,
      h: 3.2,
      label: "x_f3[2:1]",
      sub: "selects one",
    },
    {
      id: "mux",
      x: 18,
      y: 4,
      w: 13,
      h: 3.2,
      label: "3:1 mux",
      sub: "00 eq · 10 lt · else ltu",
      accent: true,
    },
    {
      id: "f3lo",
      x: 18,
      y: 12,
      w: 13,
      h: 3.2,
      label: "x_f3[0]",
      sub: "BNE, BGE, BGEU invert",
    },
    {
      id: "xor",
      x: 35,
      y: 8,
      w: 11,
      h: 3.2,
      label: "^",
      sub: "one XOR gate",
      accent: true,
    },
    {
      id: "cond",
      x: 50,
      y: 8,
      w: 13,
      h: 3.2,
      label: "br_cond",
      sub: "act_taken · ex_redir",
      accent: true,
    },
  ],
  edges: [
    { from: "eq:r", to: "mux:l" },
    { from: "lt:r", to: "mux:l" },
    { from: "ltu:r", to: "mux:l" },
    { from: "f3hi:b", to: "mux:t", dash: true },
    { from: "mux:r", to: "xor:l", accent: true },
    { from: "f3lo:r", to: "xor:l" },
    { from: "xor:r", to: "cond:l", accent: true },
  ],
};

const funct3 = {
  cols: [
    { key: "i", label: "Branch", mono: true },
    { key: "f", label: "funct3", mono: true, align: "center" },
    { key: "s", label: "x_f3[2:1] selects", mono: true, align: "center" },
    { key: "x", label: "x_f3[0] inverts", mono: true, align: "center" },
  ],
  rows: [
    { i: "BEQ", f: "000", s: "eq", x: "0" },
    { i: "BNE", f: "001", s: "eq", x: "<b>1</b>" },
    {
      i: "—",
      f: "010 / 011",
      s: "<b>illegal — the decoder refuses</b>",
      x: "—",
      _tone: "bad",
    },
    { i: "BLT", f: "100", s: "lt", x: "0" },
    { i: "BGE", f: "101", s: "lt", x: "<b>1</b>" },
    { i: "BLTU", f: "110", s: "ltu", x: "0" },
    { i: "BGEU", f: "111", s: "ltu", x: "<b>1</b>" },
  ],
};

/* --- the multiplier ------------------------------------------------------ */

const resultMux = {
  nodes: [
    {
      id: "alur",
      x: 0,
      y: 0,
      w: 11,
      h: 6.5,
      label: "alu_r",
      sub: "the ten ALU encodings",
    },
    {
      id: "mulr",
      x: 0,
      y: 9,
      w: 11,
      h: 6.5,
      label: "mul_res",
      sub: "33 × 33 signed · three stages · 4 DSP",
      accent: true,
    },
    {
      id: "link",
      x: 0,
      y: 18,
      w: 11,
      h: 6.5,
      label: "x_pc + 4",
      sub: "the link value, at x_link",
    },
    {
      id: "mux",
      x: 16,
      y: 3,
      w: 10,
      h: 18.5,
      label: "the result mux",
      sub: "x_link, then x_mul, then the ALU",
      accent: true,
    },
    {
      id: "exalu",
      x: 31,
      y: 3,
      w: 10,
      h: 18.5,
      label: "ex_alu",
      sub: "the stage's combinational result",
      accent: true,
    },
    {
      id: "mval",
      x: 46,
      y: 3,
      w: 13,
      h: 6.5,
      label: "m_val",
      sub: "registered into MEM at this edge",
    },
    {
      id: "fwd",
      x: 46,
      y: 15,
      w: 13,
      h: 6.5,
      label: "fwd_x_val",
      sub: "the distance-1 forward, this cycle",
      accent: true,
    },
    {
      id: "op",
      x: 64,
      y: 15,
      w: 13,
      h: 6.5,
      label: "x_op1 · x_op2",
      sub: "the ID → EX register",
      accent: true,
    },
  ],
  edges: [
    { from: "alur:r", to: "mux:l" },
    { from: "mulr:r", to: "mux:l", accent: true },
    { from: "link:r", to: "mux:l" },
    { from: "mux:r", to: "exalu:l", accent: true },
    { from: "exalu:r", to: "mval:l" },
    { from: "exalu:r", to: "fwd:l", accent: true },
    { from: "fwd:r", to: "op:l", accent: true },
  ],
};

const mulHold = {
  rows: [
    {
      name: "in EX",
      kind: "bus",
      values: ["mul", "mul", "mul", "mul", "next"],
      mark: [3],
    },
    { name: "mc", kind: "bus", values: ["0", "1", "2", "3", "0"] },
    {
      name: "ex_mul_hold",
      kind: "bit",
      values: [1, 1, 1, 0, 0],
      mark: [3],
    },
    {
      name: "into MEM at this edge",
      kind: "bus",
      values: ["bubble", "bubble", "bubble", "mul", "next"],
      mark: [3],
    },
    {
      name: "in MEM",
      kind: "bus",
      values: ["the one before", "bubble", "bubble", "bubble", "mul"],
    },
  ],
  notes: [
    {
      text: "The hold stops EX alone and bubbles MEM. The memory stall is deliberately not this: it stops EX and MEM together, so freezing the MEM-valid bit under it is safe. Freezing it under the multiply hold instead would retire the instruction sitting in MEM once per held cycle.",
    },
    {
      cycle: 3,
      text: "The counter resets on advance. Back-to-back multiplies hold the multiply-in condition across the stage boundary, so without the reset the second would retire carrying the first one's product.",
      tone: "good",
    },
  ],
};

const mulForms = {
  cols: [
    { key: "i", label: "Instruction", mono: true },
    { key: "a", label: "rs1 extended", align: "center" },
    { key: "b", label: "rs2 extended", align: "center" },
    { key: "h", label: "Half returned", align: "center" },
  ],
  rows: [
    { i: "mul", a: "signed", b: "unsigned", h: "<b>low 32</b>" },
    { i: "mulh", a: "signed", b: "<b>signed</b>", h: "high 32" },
    { i: "mulhsu", a: "signed", b: "unsigned", h: "high 32" },
    { i: "mulhu", a: "<b>unsigned</b>", b: "unsigned", h: "high 32" },
    {
      i: "div · divu · rem · remu",
      a: "—",
      b: "—",
      h: "<b>refused by name</b>",
      _tone: "bad",
    },
  ],
};

const simtWhy = {
  cols: [
    { key: "c", label: "The cost, on this core" },
    { key: "g", label: "The same thing on the SIMT PE" },
  ],
  rows: [
    {
      c: "<b>The result mux lengthens the distance-1 forwarding path.</b> <code>ex_alu</code> feeds <code>fwd_x_val</code>, which is an input to the ID → EX operand register",
      g: "<b>That path does not exist.</b> With as many resident waves as the pipeline is deep, no two in-flight instructions share a wave, so the design carries no forwarding network and no interlock — there is nothing there to lengthen",
      _tone: "good",
    },
    {
      c: "<b>A multi-cycle result needs a new stall term</b>, and stall terms fan out across the whole front end",
      g: "<b>The flag was already there.</b> A wave with a float in flight is skipped by the scheduler through one pending bit per wave; a multiply sets and clears the <i>same</i> bit and retires through the <i>same</i> slot",
      _tone: "good",
    },
  ],
};

/* --- fetch and the predictor -------------------------------------------- */

const predOk = {
  rows: [
    {
      name: "imem_addr",
      kind: "bus",
      values: ["0x50", "0x20", "0x24", "0x28", "0x2C"],
      mark: [1],
    },
    {
      name: "f2_pc",
      kind: "bus",
      values: ["0x4C", "0x50", "0x20", "0x24", "0x28"],
      mark: [2],
    },
    {
      name: "f2_instr",
      kind: "bus",
      values: ["[0x4C]", "[0x50] beq", "[0x20]", "[0x24]", "[0x28]"],
      mark: [2],
    },
    { name: "pred_taken", kind: "bit", values: [0, 1, 0, 0, 0], mark: [1] },
    { name: "f2_valid", kind: "bit", values: [1, 1, 1, 1, 1] },
    {
      name: "",
      kind: "text",
      values: ["BTB read at 0x50", "hit → 0x20", "target's bits out", "", ""],
    },
  ],
  notes: [
    {
      text: "The predictor is read with the SAME address as the instruction window — q_addr = pc_fetch — so the answer for an instruction arrives in the cycle that instruction's bits do. It is checked against q_pc = f2_pc, the PC the answer belongs to, which is what makes a short tag safe.",
    },
    {
      cycle: 1,
      text: "0x20 goes into the window's address register in the same cycle the branch's own bits come out of it. A correctly predicted taken branch costs no bubble at all.",
      tone: "good",
    },
  ],
};

const mispredict = {
  rows: [
    {
      name: "imem_addr",
      kind: "bus",
      values: ["0x50", "0x54", "0x58", "0x5C", "0x20", "0x24", "0x28", "0x2C"],
      mark: [4],
    },
    {
      name: "f2_pc",
      kind: "bus",
      values: ["0x4C", "0x50", "0x54", "0x58", "0x5C", "0x20", "0x24", "0x28"],
      mark: [5],
    },
    {
      name: "f2_valid",
      kind: "bit",
      values: [1, 1, 1, 1, 0, 1, 1, 1],
      mark: [4],
    },
    {
      name: "ex_redir / kill",
      kind: "bit",
      values: [0, 0, 0, 1, 0, 0, 0, 0],
      mark: [3],
    },
    {
      name: "redir_q",
      kind: "bit",
      values: [0, 0, 0, 0, 1, 0, 0, 0],
      mark: [4],
    },
    {
      name: "in EX",
      kind: "text",
      values: [
        "",
        "",
        "",
        "beq — resolves",
        "bubble",
        "bubble",
        "bubble",
        "0x20",
      ],
    },
  ],
  notes: [
    {
      cycle: 3,
      text: "EX resolves every branch against the architectural answer, so the predictor has no correctness role. ex_redir also drives kill, which clears f2_valid, the decode valid bit and the EX valid bit at this same edge — the two younger instructions die together.",
    },
    {
      cycle: 4,
      text: "A redirect is registered, deliberately: steering fetch in the resolve cycle would put the ALU output into the next-PC mux. One more cycle costs a third bubble and keeps the ALU output going nowhere but a flop.",
    },
    {
      text: "Three bubbles in EX — the 3-cycle mispredict penalty, and the same cost an unpredicted taken branch pays.",
    },
  ],
};

const bpX = {
  nodes: [
    {
      id: "arr",
      x: 0,
      y: 0,
      w: 15,
      h: 3.6,
      label: "an array with no reset",
      sub: "reads back X",
      accent: true,
    },
    {
      id: "cnt",
      x: 19,
      y: 0,
      w: 15,
      h: 3.6,
      label: "n_cnt",
      sub: "the saturating update",
    },
    {
      id: "wd",
      x: 38,
      y: 0,
      w: 15,
      h: 3.6,
      label: "wr_d → the entry",
      sub: "X is written in",
    },
    {
      id: "q",
      x: 38,
      y: 6.5,
      w: 15,
      h: 3.6,
      label: "q_taken",
      sub: "and q_target",
    },
    {
      id: "pc",
      x: 19,
      y: 6.5,
      w: 15,
      h: 3.6,
      label: "pc_fetch",
      sub: "the next-PC mux",
    },
    {
      id: "dead",
      x: 0,
      y: 6.5,
      w: 15,
      h: 3.6,
      label: "an X fetch address",
      sub: "with nothing stalled",
      accent: true,
    },
  ],
  edges: [
    { from: "arr:r", to: "cnt:l" },
    { from: "cnt:r", to: "wd:l" },
    { from: "wd:b", to: "q:t" },
    { from: "q:l", to: "pc:r" },
    { from: "pc:l", to: "dead:r", accent: true },
  ],
};

/* --- the forwarding network ---------------------------------------------- */

/* stall_d belongs to EX and MEM alone, so it sits in the pocket between those
 * two rows, fed from EX's bottom and MEM's top; the four wires into fwd_pick
 * then never have to cross it. Its two left-face slots (at 1/3 and 2/3 of its
 * height) are placed EXACTLY on the two stub lanes — 14 px below EX's bottom
 * edge and 14 px above MEM's top — so each dashed wire is one L-bend and no
 * run of it lies alongside the EX → fwd_pick wire. */
const forward = {
  nodes: [
    {
      id: "ex",
      x: 0,
      y: 0,
      w: 17,
      h: 4,
      label: "EX — ex_alu",
      sub: "distance 1 · the ALU output, combinational",
    },
    {
      id: "sd",
      x: 21,
      y: 3.375,
      w: 16,
      h: 4.5,
      label: "stall_d",
      sub: "raised instead, when that producer is a load",
      accent: true,
    },
    {
      id: "mem",
      x: 0,
      y: 7.25,
      w: 17,
      h: 4,
      label: "MEM — m_val",
      sub: "distance 2 · the EX result register",
    },
    {
      id: "wb",
      x: 0,
      y: 12.75,
      w: 17,
      h: 4,
      label: "WB — wstage_val",
      sub: "distance 3 · where a load's word first exists",
    },
    {
      id: "rf",
      x: 0,
      y: 18.25,
      w: 17,
      h: 4,
      label: "rv_regfile — rd1 · rd2",
      sub: "the default · distance 4 is a write-through inside it",
    },
    {
      id: "pick",
      x: 42,
      y: 6,
      w: 14,
      h: 9.5,
      label: "fwd_pick",
      sub: "4:1 — the nearest producer wins",
      accent: true,
    },
    {
      id: "op",
      x: 62,
      y: 6,
      w: 14,
      h: 9.5,
      label: "x_op1 · x_op2",
      sub: "the ID → EX operand register",
      accent: true,
    },
  ],
  edges: [
    { from: "ex:b", to: "sd:l", dash: true },
    { from: "mem:t", to: "sd:l", dash: true },
    { from: "ex:r", to: "pick:l" },
    { from: "mem:r", to: "pick:l" },
    { from: "wb:r", to: "pick:l" },
    { from: "rf:r", to: "pick:l" },
    { from: "pick:r", to: "op:l", accent: true },
  ],
};

const distances = {
  cols: [
    { key: "d", label: "Distance", align: "center", mono: true },
    { key: "p", label: "Producer is in", align: "center" },
    { key: "s", label: "The source signal", mono: true },
    { key: "w", label: "What happens" },
  ],
  rows: [
    {
      d: "1",
      p: "EX",
      s: "ex_alu",
      w: "forwarded through <code>fwd_pick</code> at <code>FWD_X = 1</code> — <b>unless the producer is a load</b>, and then the consumer stalls",
    },
    {
      d: "2",
      p: "MEM",
      s: "m_val",
      w: "forwarded from a register — <b>unless the producer is a load</b>, and then the consumer stalls",
    },
    {
      d: "3",
      p: "WB",
      s: "wstage_val",
      w: "forwarded, loads included: this is where a load's word exists",
      _tone: "good",
    },
    {
      d: "4",
      p: "—",
      s: "byp_d",
      w: "the register file's own write-through, inside <code>rv_regfile</code>",
      _tone: "good",
    },
  ],
};

const stall = {
  nodes: [
    {
      id: "ld1",
      x: 0,
      y: 0,
      w: 14,
      h: 4.4,
      label: "hz1 && x_load",
      sub: "a load in EX — distance 1",
    },
    {
      id: "ld2",
      x: 0,
      y: 6,
      w: 14,
      h: 4.4,
      label: "hz2 && m_load",
      sub: "a load in MEM — distance 2",
    },
    {
      id: "sm",
      x: 0,
      y: 15,
      w: 14,
      h: 4.4,
      label: "stall_m",
      sub: "the memory stage is not ready",
    },
    {
      id: "mh",
      x: 0,
      y: 21,
      w: 14,
      h: 4.4,
      label: "ex_mul_hold",
      sub: "a multiply, three cycles",
    },
    {
      id: "sd",
      x: 20,
      y: 3,
      w: 12,
      h: 4.4,
      label: "stall_d",
      sub: "the one hazard rule",
      accent: true,
    },
    {
      id: "sx",
      x: 20,
      y: 18,
      w: 12,
      h: 4.4,
      label: "stall_x",
      sub: "EX may not advance",
    },
    {
      id: "hf",
      x: 38,
      y: 10.5,
      w: 14,
      h: 4.8,
      label: "hold_front",
      sub: "stall_d || stall_x",
      accent: true,
    },
    {
      id: "d1",
      x: 58,
      y: 3,
      w: 16,
      h: 4.4,
      label: "rv_if",
      sub: "the fetch hold",
    },
    {
      id: "d2",
      x: 58,
      y: 10.5,
      w: 16,
      h: 4.4,
      label: "rv_id",
      sub: "the decode register",
    },
    {
      id: "d3",
      x: 58,
      y: 18,
      w: 16,
      h: 4.4,
      label: "rv_regfile",
      sub: "ra_en, the address hold",
    },
  ],
  edges: [
    { from: "ld1:r", to: "sd:l" },
    { from: "ld2:r", to: "sd:l" },
    { from: "sm:r", to: "sx:l" },
    { from: "mh:r", to: "sx:l" },
    { from: "sd:r", to: "hf:l", accent: true },
    { from: "sx:r", to: "hf:l", accent: true },
    { from: "hf:r", to: "d1:l" },
    { from: "hf:r", to: "d2:l" },
    { from: "hf:r", to: "d3:l" },
  ],
};

const loadUse = [
  {
    name: "IF2",
    kind: "bus",
    values: ["lw x5", "add x6", "sub x7", "sub x7", "sub x7"],
  },
  {
    name: "ID",
    kind: "bus",
    values: [null, "lw x5", "add x6", "add x6", "add x6"],
  },
  { name: "EX", kind: "bus", values: [null, null, "lw x5", "—", "—"] },
  { name: "MEM", kind: "bus", values: [null, null, null, "lw x5", "—"] },
  {
    name: "WB — x5 exists",
    kind: "bus",
    values: [null, null, null, null, "lw x5"],
    mark: [4],
  },
  { name: "stall_d", kind: "bit", values: [0, 0, 1, 1, 0], mark: [2, 3] },
];

/* --- the register file --------------------------------------------------- */

/* The read address and the write port both reach both arrays. Two sources on
 * the same side of a column cannot share two boxes without a crossing, so the
 * write port sits in front, between the arrays and the live address, and the
 * live address passes above and below it: array #1 on top, the bypass compare
 * (the write port's own) in the middle, array #2 at the bottom. live and wport
 * share a centre line so the slot sort falls back to list order — live takes
 * the outer lane on both arrays. */
const regfile = {
  nodes: [
    {
      id: "addr",
      x: 0,
      y: 3,
      w: 13,
      h: 3.6,
      label: "ra1 · ra2",
      sub: "f2_instr[19:15], [24:20]",
    },
    {
      id: "en",
      x: 0,
      y: 7.5,
      w: 13,
      h: 3.6,
      label: "ra_en = !hold_front",
      sub: "re-issue the read while held",
    },
    {
      id: "live",
      x: 16,
      y: 5.2,
      w: 11,
      h: 3.6,
      label: "ra1_live",
      sub: "ra_en ? ra1 : ra1_q",
      accent: true,
    },
    {
      id: "wport",
      x: 30,
      y: 5.2,
      w: 12,
      h: 3.6,
      label: "we · wa · wd",
      sub: "from rv_wb, same edge",
    },
    {
      id: "p1",
      x: 45,
      y: 0,
      w: 15,
      h: 3.6,
      label: "kohaku_sdpram #1",
      sub: "32 × 32 · distributed · LAT 1",
    },
    {
      id: "byp",
      x: 45,
      y: 5.2,
      w: 15,
      h: 3.6,
      label: "byp1 · byp2 · byp_d",
      sub: "wa == ra_live, registered",
      accent: true,
    },
    {
      id: "p2",
      x: 45,
      y: 10.4,
      w: 15,
      h: 3.6,
      label: "kohaku_sdpram #2",
      sub: "the mirror, written identically",
    },
    {
      id: "sel",
      x: 63,
      y: 5,
      w: 14,
      h: 4,
      label: "rd1 · rd2",
      sub: "byp ? byp_d : q — and x0 forced",
      accent: true,
    },
  ],
  edges: [
    { from: "addr:r", to: "live:l" },
    { from: "en:r", to: "live:l" },
    { from: "live:t", to: "p1:l" },
    { from: "wport:t", to: "p1:l" },
    { from: "wport:r", to: "byp:l" },
    { from: "wport:b", to: "p2:l" },
    { from: "live:b", to: "p2:l" },
    { from: "p1:r", to: "sel:l", accent: true },
    { from: "p2:r", to: "sel:l", accent: true },
    { from: "byp:r", to: "sel:l" },
  ],
};

const primChoice = {
  cols: [
    { key: "p", label: "REGFILE_PRIM", mono: true },
    { key: "r", label: "Read latency", align: "center", mono: true },
    { key: "c", label: "What it costs" },
  ],
  rows: [
    {
      p: '"distributed"',
      r: "1",
      c: "<b>the shipped form.</b> 129 LUT sites of <code>u_core</code>'s 1,298, of which 40 are LUTRAM",
      _tone: "good",
    },
    {
      p: '"block"',
      r: "1",
      c: "two <code>RAMB36E2</code>, each <b>3.1 % depth-utilised</b> at 32 × 32 in a 1K × 36 aspect — the worst ratio anything in this design could post",
      _tone: "bad",
    },
  ],
};

/* --- the two L1s, inside ------------------------------------------------- */

const doorbellBroken = [
  {
    name: "fabric port (A)",
    kind: "text",
    values: ["idle", "wr spad[W] ← 0x01", "idle", "idle"],
  },
  {
    name: "core read addr (B)",
    kind: "bus",
    values: ["W", "W", null, null],
    mark: [1],
  },
  {
    name: "array out (B)",
    kind: "bus",
    values: ["0x00", "0x00", "X", "0x01"],
    mark: [2],
  },
  {
    name: "the poll sees",
    kind: "text",
    values: ["not rung", "not rung", "UNDEFINED", "rung"],
  },
];

const doorbellFixed = [
  {
    name: "fabric port (A)",
    kind: "text",
    values: ["idle", "wr spad[W] ← 0x01", "idle", "idle"],
  },
  {
    name: "core read addr (B)",
    kind: "bus",
    values: ["W", "W", null, null],
    mark: [1],
  },
  { name: "byte-wise bypass", kind: "bit", values: [0, 1, 0, 0], mark: [1] },
  {
    name: "array out (B)",
    kind: "bus",
    values: ["0x00", "0x00", "0x01", "0x01"],
    mark: [2],
  },
  {
    name: "the poll sees",
    kind: "text",
    values: ["not rung", "not rung", "rung", "rung"],
  },
];

const xport = {
  cols: [
    { key: "c", label: "Caller", mono: true },
    { key: "x", label: "XPORT_OK", align: "center", mono: true },
    { key: "a", label: "How it answers the collision" },
  ],
  rows: [
    {
      c: "rv_spad",
      x: "1",
      a: "<b>bypassed, byte by byte.</b> The core receives the bytes the fabric just wrote — correct rather than merely defined, and on a doorbell the collision <i>is</i> the common case",
      _tone: "good",
    },
    {
      c: "rv_l1",
      x: "1",
      a: "<b>discarded and re-issued.</b> A fill writes the word a stalled access is presenting; the access is decided one cycle later, never on that edge",
      _tone: "good",
    },
    {
      c: "anything else",
      x: "0",
      a: "<b>asserts the moment one happens.</b> An array that does not declare how it handles the collision is a bug waiting for a doorbell",
      _tone: "bad",
    },
  ],
};

const l1sm = {
  states: [
    { id: "IDLE", x: 0, y: 0, label: "L_IDLE" },
    { id: "EVRD", x: 8, y: -5, label: "L_EV_RD" },
    { id: "EVSEND", x: 16, y: -5, label: "L_EV_SEND" },
    { id: "FREQ", x: 24, y: 0, label: "L_F_REQ" },
    { id: "FWAIT", x: 16, y: 5, label: "L_F_WAIT" },
    { id: "FWR", x: 8, y: 5, label: "L_F_WR" },
  ],
  edges: [
    { from: "IDLE", to: "EVRD", label: "miss · dirty", curve: 25 },
    { from: "EVRD", to: "EVSEND", label: "8 words out" },
    { from: "EVSEND", to: "FREQ", label: "wb_ready", curve: 25 },
    { from: "IDLE", to: "FREQ", label: "miss · clean victim", curve: 70 },
    { from: "FREQ", to: "FWAIT", label: "fill_ready", curve: 25 },
    { from: "FWAIT", to: "FWR", label: "resp_valid" },
    { from: "FWR", to: "IDLE", label: "8 rotations", curve: 25 },
  ],
};

const l1states = {
  cols: [
    { key: "s", label: "State", mono: true },
    { key: "w", label: "What it does" },
  ],
  rows: [
    {
      s: "L_IDLE",
      w: "the only state that acts on <code>flush</code>, <code>inval</code> or a miss. A hit completes here with no state change",
    },
    {
      s: "L_EV_RD",
      w: "walk the dirty victim out of the data array, eight 32-bit words on port A",
    },
    {
      s: "L_EV_SEND",
      w: "hand the 256-bit <code>linebuf</code> to the requestor as one writeback descriptor and one beat",
    },
    { s: "L_F_REQ", w: "raise <code>fill_valid</code> with the line address" },
    {
      s: "L_F_WAIT",
      w: "wait for <code>resp_valid</code>; on it, latch the 256-bit response and write the tag <b>valid, clean</b>",
    },
    {
      s: "L_F_WR",
      w: "walk the response back into the array, eight words, rotating <code>linebuf</code> back to the order it arrived in",
    },
    {
      s: "L_S_SCAN",
      w: "flush-all: present <code>scan</code> on the tag port. Two states per line, because the tag word is only out in the next one",
    },
    {
      s: "L_S_TEST",
      w: "<code>dirty</code> alone decides — a fill leaves a line valid and clean, so dirty implies valid by construction",
    },
    {
      s: "L_S_RD / L_S_SEND",
      w: "the same eight-word walk and one-beat writeback, then rewrite the tag <b>still valid, no longer dirty</b>",
    },
    {
      s: "L_S_DRAIN",
      w: "wait for <code>wr_idle</code>. A flush is not finished until every writeback has been <b>acknowledged</b>",
      _tone: "good",
    },
    {
      s: "L_I_SCAN",
      w: "invalidate-all, and the power-on sweep the tag array's missing reset needs — one line a cycle, MEM held for all of it",
    },
    {
      s: "L_REPROBE",
      w: "one cycle before returning to <code>L_IDLE</code>. A sweep leaves the tag port on its own cursor, so deciding hit or miss on the cycle it ends would evict to the sweep's address",
      _tone: "good",
    },
  ],
};

const rotate = {
  rows: [
    {
      name: "st",
      kind: "bus",
      values: [
        "L_EV_RD",
        "L_EV_RD",
        "L_EV_RD",
        "L_EV_RD",
        "L_EV_RD",
        "L_EV_RD",
        "L_EV_RD",
        "L_EV_RD",
        "L_EV_RD",
        "L_EV_SEND",
      ],
    },
    {
      name: "a_walk",
      kind: "bit",
      values: [1, 1, 1, 1, 1, 1, 1, 1, 1, 0],
      mark: [9],
    },
    {
      name: "wcnt — addr out",
      kind: "bus",
      values: ["0", "1", "2", "3", "4", "5", "6", "7", "7", null],
    },
    {
      name: "a_rd",
      kind: "bus",
      values: [null, "w0", "w1", "w2", "w3", "w4", "w5", "w6", "w7", "w7"],
    },
    {
      name: "wvalid_d",
      kind: "bit",
      values: [0, 1, 1, 1, 1, 1, 1, 1, 1, 1],
      mark: [9],
    },
    {
      name: "rotations",
      kind: "text",
      values: [
        "",
        "1",
        "2",
        "3",
        "4",
        "5",
        "6",
        "7",
        "8 — w0 at the bottom",
        "BLOCKED",
      ],
    },
  ],
  notes: [
    {
      text: "linebuf <= {a_rd, linebuf[255:32]} and the array's write port always takes linebuf[31:0]. Eight rotations put word 0 back at the bottom, which removes an 8:1 mux and a 1:8 demux from that port and replaces both with wiring.",
    },
    {
      cycle: 9,
      text: "wvalid_d is a cycle late by construction and is still set after the walk ends. Without the && a_walk guard a ninth rotation would shift the whole writeback by one word.",
      tone: "good",
    },
  ],
};

/* --- the NoC requestor --------------------------------------------------- */

const flit = [
  { name: "dest x", bits: 4, value: "MEM_X" },
  { name: "dest y", bits: 4, value: "MEM_Y" },
  { name: "src x", bits: 4, value: "POS_X" },
  { name: "src y", bits: 4, value: "POS_Y" },
  { name: "type", bits: 4, value: "MEM_RD_REQ …" },
  { name: "txn", bits: 8, value: "the tag" },
  { name: "last", bits: 1, value: "0 / 1" },
  { name: "rsvd", bits: 3, value: "3'b000" },
  { name: "payload", bits: 256, value: "descriptor / line", accent: true },
];

const descr = [
  { name: "addr", bits: 40, value: "40, not 34", accent: true },
  { name: "len", bits: 8, value: "0" },
  { name: "flags", bits: 8, value: "0x40 STREAM" },
  { name: "count", bits: 8, value: "1" },
  { name: "0", bits: 24, value: "—" },
  { name: "0", bits: 2, value: "—" },
  { name: "entry_words", bits: 8, value: "1", accent: true },
  { name: "0", bits: 158, value: "—" },
];

const descrSpec = {
  cols: [
    { key: "f", label: "Field", mono: true },
    { key: "w", label: "Width", align: "right", mono: true },
    { key: "p", label: "Position", mono: true },
    { key: "o", label: "Owner" },
  ],
  rows: [
    {
      f: "addr",
      w: "40",
      p: "[255:216]",
      o: "<b>the requestor, and it is 40 bits and the whole of them.</b> The core's own addresses are 32; the top eight come from the window registers, which is why a descriptor is not just a sign-extended pointer",
      _tone: "warn",
    },
    {
      f: "len",
      w: "8",
      p: "[215:208]",
      o: "the requestor. Beats minus one — <b>zero means one beat</b>, and this core never issues anything else",
    },
    {
      f: "flags",
      w: "8",
      p: "[207:200]",
      o: "<b>fixed protocol.</b> Only bit 6, STREAM, is set here. Bits 4 and 5 are reserved and ignored — a requestor that sets them gets an ordinary untransformed fetch",
    },
    {
      f: "count",
      w: "8",
      p: "[199:192]",
      o: "the requestor. Entries, read only when STREAM is set",
    },
    {
      f: "peer / n_peer",
      w: "24 / 2",
      p: "[191:168] [167:166]",
      o: "<b>unused by this core and MUST be zero.</b> They exist so a memory agent can answer several listeners at once, which this requestor never asks for",
    },
    {
      f: "entry_words",
      w: "8",
      p: "[165:158]",
      o: "the requestor",
    },
    {
      f: "reserved",
      w: "158",
      p: "[157:0]",
      o: "<b>the framework. MUST be zero</b> — a future allocation will take them, and a requestor that leaves rubbish here breaks on a version bump rather than on a test",
    },
  ],
};

const knobs = {
  cols: [
    { key: "k", label: "Knob", mono: true },
    { key: "d", label: "Default", mono: true, align: "right" },
    { key: "w", label: "What it moves on this page" },
  ],
  rows: [
    {
      k: "IMEM_WORDS / SPAD_WORDS",
      d: "2048",
      w: "<b>The block RAM, and almost nothing else.</b> Both are sized to fill their tiles exactly — the point of the number is that it is the tile, not that it is a round figure.",
    },
    {
      k: "L1_LINES",
      d: "128",
      w: "128 fills the block RAM's natural depth, so <b>halving it saves almost nothing</b>. On this core per-line valid and dirty are flop arrays, which is why the line count is an expensive knob here and a cheap one on the RV64 core.",
      _tone: "warn",
    },
    {
      k: "BTB_ENTRIES",
      d: "32",
      w: "Predictor size — and <b>0 removes the predictor entirely</b>, as a generate rather than a zero-sized array. Thirty-two is right for a working set of a few backedges; a wider table buys nothing.",
    },
    {
      k: "REGFILE_PRIM",
      d: '"distributed"',
      w: "LUTRAM against a block-RAM register file. <b>Interchangeable on timing here</b> — a pure resource trade, unlike the RV64 core where the block-RAM clock-to-out becomes the binding path.",
    },
    {
      k: "FWD_X",
      d: "1",
      w: "The distance-1 bypass. <b>0 is measured worse on every axis</b> and survives only as the proof of that.",
    },
    {
      k: "WR_MAX",
      d: "1",
      w: "Un-acknowledged writes. One is what the communication model assumes, and <b>raising it buys nothing a blocking cache can use</b>.",
    },
    {
      k: "SIMD_EN",
      d: "0",
      w: "The wide-datapath seam. At 0 nothing behind it is elaborated, so the figures on this page measure exactly the core described here.",
    },
  ],
};

const threeDesc = {
  cols: [
    { key: "w", label: "What" },
    { key: "t", label: "Flit type", mono: true },
    { key: "x", label: "txn", mono: true, align: "center" },
    { key: "d", label: "Descriptor", mono: true },
    { key: "b", label: "The beat behind it" },
  ],
  rows: [
    {
      w: "<b>line fill</b>",
      t: "T_MEM_RD_REQ",
      x: "{4'd0, rd_tag}",
      d: "descr(fill_phys, 0, STREAM, 1, 1)",
      b: "none — the response is one flit, <code>last</code> set",
    },
    {
      w: "<b>dirty writeback</b>",
      t: "T_MEM_WR_REQ",
      x: "0x20",
      d: "descr(wb_phys, 0, 0, 0, 0)",
      b: "<code>T_MEM_WR_DATA</code>, <code>last</code> set, the 256-bit line",
    },
    {
      w: "<b>peer push</b>",
      t: "T_CU_DATA",
      x: "0",
      d: "cudesc(buf_id, offset)",
      b: "<code>T_CU_DATA</code>, <code>last</code> set, {sel, be, data}",
    },
  ],
};

const tagTrace = {
  rows: [
    { name: "fill_valid", kind: "bit", values: [1, 0, 0, 0, 1, 0, 0] },
    { name: "rd_pend", kind: "bit", values: [0, 1, 1, 0, 0, 1, 1] },
    {
      name: "rd_tag",
      kind: "bus",
      values: ["0x0", "0x0", "0x0", "0x1", "0x1", "0x1", "0x1"],
    },
    {
      name: "sent txn",
      kind: "text",
      values: ["0x0", "", "", "", "0x1", "", ""],
    },
    {
      name: "rx_txn[3:0]",
      kind: "bus",
      values: [null, null, "0x0", null, null, "0x0", "0x1"],
      mark: [5],
    },
    {
      name: "match?",
      kind: "text",
      values: ["", "", "yes", "", "", "NO — dropped", "yes"],
    },
  ],
  notes: [
    {
      cycle: 2,
      text: "The response carries the tag its request went out with. It matches, the 256-bit line is handed to rv_l1, and rd_tag rotates.",
      tone: "good",
    },
    {
      cycle: 5,
      text: "A late or repeated response from the previous transaction carries 0x0 while the tag is now 0x1. With one fixed tag it would have been accepted as this fill's data. It is dropped, and named: MEM_RD_RESP txn 0 with no matching outstanding fill.",
      tone: "bad",
    },
    {
      text: "Only one read is ever outstanding — take_fill requires !rd_pend — so the tag is not there to distinguish concurrent reads. It is there to distinguish this read from the last one.",
    },
  ],
};

// Sixteen agent write slots, drawn two rows of eight so the whole set fits the
// column — slot 15 is the point of the sequence and must never be off-screen.
const slotGrid = (holds, hot) => ({
  nodes: holds.map((h, i) => ({
    id: `s${i}`,
    x: (i % 8) * 5,
    y: Math.floor(i / 8) * 4.6,
    w: 4.6,
    h: 3.4,
    label: String(i),
    sub: h,
    accent: hot.includes(i),
  })),
});

const fillArr = (n, v) => Array.from({ length: n }, () => v);

const slots = [
  {
    title: "two rules, each reasonable alone",
    grid: slotGrid(fillArr(16, "free"), []),
    note: "The memory agent allocates the LOWEST FREE slot and issues the LOWEST READY one. Neither rule is wrong on its own.",
  },
  {
    title: "a flush that outruns AXI fills the slots",
    grid: slotGrid([...fillArr(15, "in use"), "PE line"], [15]),
    note: "The PE's dirty line is handed slot 15 — the lowest free one at that moment. Nothing is wrong yet.",
  },
  {
    title: "slot 15 is never the lowest ready again",
    grid: slotGrid(
      ["PE · out", "PE · out", ...fillArr(13, "free"), "STUCK"],
      [15],
    ),
    note: "AXI drains and the slots free from the bottom, so every later writeback takes 0 or 1 — the lowest free, and the lowest ready. Measured on a 64-line flush: slot 15 took a dirty line, the agent alternated slots 0 and 1 for the other sixty-three, and that line never reached DRAM. Silently.",
  },
  {
    title: "WR_MAX = 1 — bound what is outstanding",
    grid: slotGrid(["the one", ...fillArr(15, "free")], [0]),
    note: "At most one un-acknowledged write, which keeps the agent's in-use set where the two rules do compose. It is free in steady state: a blocking one-miss cache has a whole fill round trip between dirty evictions, and the previous acknowledgement always arrives inside it.",
  },
];

const pushPath = {
  nodes: [
    {
      id: "fifo",
      x: 0,
      y: 0,
      w: 15,
      h: 3.6,
      label: "completion FIFO",
      sub: "noc_cu_base · empty",
    },
    { id: "sr", x: 18, y: 0, w: 13, h: 3.6, label: "send_ready" },
    { id: "tf", x: 34, y: 0, w: 13, h: 3.6, label: "tx_free · take_push" },
    {
      id: "pq",
      x: 50,
      y: 0,
      w: 15,
      h: 3.6,
      label: "pq_valid",
      sub: "ONE ENTRY, A FLOP",
      accent: true,
    },
    {
      id: "pr",
      x: 50,
      y: 8,
      w: 15,
      h: 3.6,
      label: "push_ready = !pq_valid",
      sub: "off a flop, not a wire",
      accent: true,
    },
    {
      id: "bs",
      x: 34,
      y: 8,
      w: 13,
      h: 3.6,
      label: "base_stall",
      sub: "rv_mem",
    },
    {
      id: "sm",
      x: 18,
      y: 8,
      w: 13,
      h: 3.6,
      label: "stall_m → x_hold",
      sub: "rv_ex",
    },
    {
      id: "cnt",
      x: 0,
      y: 8,
      w: 15,
      h: 3.6,
      label: "the saturating counter",
      sub: "rv_bpred",
    },
  ],
  edges: [
    { from: "fifo:r", to: "sr:l" },
    { from: "sr:r", to: "tf:l" },
    { from: "tf:r", to: "pq:l" },
    { from: "pq:b", to: "pr:t", accent: true },
    { from: "pr:l", to: "bs:r" },
    { from: "bs:l", to: "sm:r" },
    { from: "sm:l", to: "cnt:r" },
  ],
  groups: [
    { x: -1, y: -1.2, w: 67, h: 6, label: "the fabric side" },
    { x: -1, y: 6.8, w: 67, h: 6, label: "the core side" },
  ],
};

const completion = {
  cols: [
    { key: "t", label: "Term", mono: true },
    { key: "m", label: "What it asserts" },
  ],
  rows: [
    {
      t: "core_halted",
      m: "the program raised <code>ECALL</code>, <code>EBREAK</code> or a fault",
    },
    { t: "pipe_empty", m: "nothing is in flight in IF2, ID, EX, MEM or WB" },
    {
      t: "req_idle",
      m: "the requestor is in <code>S_IDLE</code>, has nothing in <code>send_valid</code>, no read pending, <b>and nothing in the push register</b> — a push still in the register has not been sent",
    },
    {
      t: "wr_out == 0",
      m: "<b>every write the program issued has been acknowledged by memory.</b> The completion is the host's sequencing point, and it would mean nothing weaker",
      _tone: "good",
    },
  ],
};

/* --- what the stage costs ------------------------------------------------ */

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
</script>

<template>
  <DocPage
    title="RV32 PE microarchitecture"
    summary="The RTL, drawn. Six register boundaries and why there are not five; one adder, one shifter and one comparator triple; where the multiply result joins and why that is where its cost is; three forwarding sources by position and the one rule that stalls; the register file, the L1's fill machine, and the requestor's single outstanding write."
    domain="cpu"
    status="shipped"
    source="src/kohakuaccel/pe/rv32/ · docs/arch/cpu/rv32-pe/microarchitecture.md"
  >
    <p class="doc-p">
      The
      <RouterLink to="/component/rv32pe" class="doc-link">RV32 PE</RouterLink>
      page states the contract: the instruction set, the memory model, and the
      protocol by which a <b>mesh</b> — the grid of routers this machine is
      built on — starts the core and hears that it finished. This page is the
      implementation behind that contract, and why each piece has the shape it
      has.
    </p>

    <h2 class="doc-h2">What it owns</h2>
    <p class="doc-p">Four mechanisms. Every section below is one of them.</p>
    <div class="grid gap-3 sm:grid-cols-2 my-5">
      <div class="card p-4">
        <div
          class="kt-text-caption font-semibold text-warm-800 dark:text-warm-200 mb-1"
        >
          Six register boundaries
        </div>
        <p class="kt-text-caption text-warm-500 dark:text-warm-400 leading-6">
          Five stages, and two synchronous arrays that each cost a cycle between
          an address and its data.
        </p>
      </div>
      <div class="card p-4">
        <div
          class="kt-text-caption font-semibold text-warm-800 dark:text-warm-200 mb-1"
        >
          One adder and one shifter
        </div>
        <p class="kt-text-caption text-warm-500 dark:text-warm-400 leading-6">
          The execute stage, built so that every operation reuses the same two
          structures — and a result mux instead of a third.
        </p>
      </div>
      <div class="card p-4">
        <div
          class="kt-text-caption font-semibold text-warm-800 dark:text-warm-200 mb-1"
        >
          Two L1s and a scratchpad
        </div>
        <p class="kt-text-caption text-warm-500 dark:text-warm-400 leading-6">
          Separate instruction and data caches, and a data window a peer can
          write while a poll loop is reading it.
        </p>
      </div>
      <div class="card p-4">
        <div
          class="kt-text-caption font-semibold text-warm-800 dark:text-warm-200 mb-1"
        >
          The NoC requestor
        </div>
        <p class="kt-text-caption text-warm-500 dark:text-warm-400 leading-6">
          Descriptors, sixteen write slots, and the counting that has to lead
          rather than lag.
        </p>
      </div>
    </div>

    <p class="doc-p">
      The recurring theme: the core spends flip-flops and block RAM freely,
      prefers a pipeline stage over a bypass, and registers anything whose
      combinational form would fan out — because its objectives are LUT and
      frequency, never latency.
    </p>

    <p class="doc-p">
      The shape that was rejected, and it is rejected once here rather than
      argued in six places, is <b>anything that buys cycles with area</b>. No
      divider, no second issue slot, no hit-under-miss, no address-generation
      stage beyond the one that already exists. The reason is not that latency
      does not matter — it is that this core is instantiated dozens of times on
      one device, so every LUT is multiplied by the unit count while every cycle
      saved is not. A structure that pays for itself in a single-core machine
      can be the wrong answer here at exactly the same performance.
    </p>

    <Callout kind="measured" title="Where every figure on this page comes from">
      <p>
        One run.
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
        <b>−0.251 ns</b>
      </p>
      <p>
        <b>The slack is negative: the request was not met.</b> 363.5 MHz is what
        the tool reports the worst path can carry, not a frequency the design
        has closed at. Synthesis slack is also optimistic — elsewhere in this
        project a module lost 0.740 ns going from synthesis to routing — so read
        every megahertz here as an upper bound. Never quote an Fmax from this
        tree without the request beside it.
      </p>
      <p>
        <b>The per-instance figures below are all from this one run</b>, so they
        are internally consistent and they are not the whole story. The same
        configuration at a <b>3.333 ns</b> request is
        <b>2,586 LUT at the same 363.5 MHz</b>, with <b>+0.582 ns</b> of slack —
        <RouterLink to="/component/rv32pe" class="doc-link"
          >the two requests side by side</RouterLink
        >. The tighter ask bought <b>zero megahertz for 86 LUT sites</b>, which
        is what over-constraining looks like when the binding path is already as
        short as the structure allows. Every breakdown on this page inherits the
        2.500 ns run's numbers; do not mix a row from it with a total from the
        other.
      </p>
      <p class="font-mono kt-text-caption">
        worst path, same run: u_core/u_ex/m_addr_reg[29]/C →
        u_core/u_id/d_imm_reg[12]/S &nbsp;·&nbsp; 9 logic levels
      </p>
    </Callout>

    <Callout kind="trap" title="Two accountings, and they do not agree">
      <p>
        That one run emits per-instance numbers twice. One counts
        <b>LUT primitives</b> under a name prefix; the other is Vivado's
        <b>hierarchical LUT-site</b> report, which is the unit a vendor
        utilisation table uses and which packs two small LUTs into one site.
        <code>u_ex</code> is <b>589</b> in the first and <b>489</b> in the
        second; the whole unit is 2,910 and 2,672.
      </p>
      <p>
        <b>Every per-unit figure on this page is the site column.</b> The two
        are not interchangeable and a difference between them is not a
        measurement — never subtract one from the other.
      </p>
    </Callout>

    <h2 class="doc-h2">Six register boundaries for five stages</h2>

    <p class="doc-p">
      The instruction window and the register file are both synchronous arrays,
      and each costs a cycle between presenting an address and receiving data.
      Counting those honestly is what lets the fetch loop close: the address
      path in fetch is
      <code>PC → mux → RAM address register</code> and nothing else.
    </p>

    <Fig
      caption="Five architectural stages, six register boundaries. The two accented ones are the two synchronous arrays — they are the whole reason the count is six and not five."
      zoom
      wide
    >
      <BlockDiagram :nodes="boundaries.nodes" :edges="boundaries.edges" />
    </Fig>

    <Callout
      kind="rule"
      title="The two things that are combinational on purpose"
    >
      <p>
        <b>Decode is combinational on the fetched word</b>, inside IF2.
        <code>ra1</code> and <code>ra2</code> come straight off
        <code>f2_instr[19:15]</code> and <code>[24:20]</code>, so the
        register-file address leaves at the same edge as the control bits — and
        that buys the operand-fetch cycle instead of costing a seventh boundary.
        Nothing that compares the opcode may sit in front of it.
      </p>
      <p>
        <b>The effective address leaves EX combinationally</b> — it is the ALU's
        own adder output — because the data arrays register their address input.
        It must be at their pins in this cycle for the data to be out in MEM.
      </p>
    </Callout>

    <Callout kind="note" title="Two more shapes in the same budget">
      <p>
        <b>A branch or jump target is computed in ID, not EX.</b> PC and
        immediate are both registered by then, the adder is off every critical
        path in that stage, and carrying the target instead of the immediate
        keeps the EX register the same width.
      </p>
      <p>
        <b
          >The two registers in <code>rv_id</code> stop for different
          reasons.</b
        >
        A data hazard freezes decode and feeds EX a bubble; a memory stall
        freezes EX as well and nothing moves at all. One enable could not
        express both.
      </p>
    </Callout>

    <SpecTable
      :cols="knobs.cols"
      :rows="knobs.rows"
      caption="The knobs, in the order they move area. Two of them are sized to a block-RAM tile rather than to a workload, which is what makes them look like round numbers and is not why they are those numbers"
    />

    <h2 class="doc-h2">The ALU, as it is actually built</h2>

    <p class="doc-p">
      Ten ALU encodings, and five things that compute. EX carries
      <b>one 32-bit adder</b> over the operands plus one PC+4 incrementer, one
      subtractor, one comparator triple, one shifter and the bitwise gates. The
      whole stage — ALU, multiply, branch resolve, effective address,
      misalignment check, byte enables and store-data replication — is
      <b>489 LUT sites and 4 DSP</b>.
    </p>

    <Fig
      caption="Five producers, one result mux: the three shift encodings all select the same input, and the comparator triple is shared with the branch unit, so a ten-way select is a five-input mux. Every unit takes both operands; x_op1's fan-out is drawn as its top and bottom wires only."
      zoom
      wide
    >
      <BlockDiagram :nodes="alu.nodes" :edges="alu.edges" />
    </Fig>

    <SpecTable :cols="aluOps.cols" :rows="aluOps.rows" />

    <h3 class="doc-h3">The adder has six consumers</h3>

    <p class="doc-p">
      <code>sum</code> is not “the add result”. It is the effective address, the
      JALR target, the alignment check and the byte-enable decode as well, and
      only one of those six uses reaches the result mux.
    </p>

    <Fig
      caption="One adder, six consumers, and the accented one leaves the stage without a register in front of it. This is why the address path and the ALU path cannot be separated in this core: they are the same carry chain. It is also the one adder here that cannot be borrowed — anything muxed into its inputs lands in front of the arrays' address pins."
      zoom
    >
      <BlockDiagram :nodes="adderFanout.nodes" :edges="adderFanout.edges" />
    </Fig>

    <h3 class="doc-h3">One 33-bit arithmetic shifter does all three shifts</h3>

    <p class="doc-p">
      A left shift is a right shift on the reversed word, and a bit reversal is
      wiring. <code>SRA</code> is the same shifter with the sign fed in above
      the word.
    </p>

    <Fig
      caption="Why 33 bits and not 32: $signed( ) >>> replicates the TOP bit, so appending an explicit fill bit above the word makes the same arithmetic shifter serve SRL and SRA with no second shifter and no mux on the result. sh_sign is the entire difference between them. The 33rd bit matters for SLL too — the reversed word's bit 31 is x_op1[0], which a 32-bit >>> would smear into the fill."
      zoom
      wide
    >
      <BlockDiagram :nodes="shifter.nodes" :edges="shifter.edges" />
    </Fig>

    <SpecTable
      :cols="shiftWorked.cols"
      :rows="shiftWorked.rows"
      caption="x_op1 = 0x8000_00F0, sh = 4, through the one shifter. The two reversals cost no logic; the direction costs two 32-bit 2:1 muxes and the arithmetic-versus-logical distinction costs one AND gate"
    />

    <h3 class="doc-h3">The branch condition is a 3:1 mux and one XOR</h3>

    <p class="doc-p">
      <code>eq</code>, <code>lt</code> and <code>ltu</code> are computed once
      and consumed twice — by the ALU result mux for
      <code>SLT</code>/<code>SLTU</code>, and by the branch unit for all six
      conditional branches. The branch unit itself is two gates, because RV32I's
      <code>funct3</code> encoding was built for it.
    </p>

    <Fig
      caption="funct3[2:1] picks which comparison and funct3[0] is the invert bit: BNE is not-BEQ, BGE is not-BLT, BGEU is not-BLTU. One XOR does all three negations. funct3 010 and 011 are unmapped and the decoder marks them illegal, so the default arm of the mux can be ltu."
      zoom
    >
      <BlockDiagram :nodes="branchUnit.nodes" :edges="branchUnit.edges" />
    </Fig>

    <SpecTable :cols="funct3.cols" :rows="funct3.rows" />

    <Callout
      kind="note"
      title="What is structural in the source, and what is left to synthesis"
    >
      <p>
        Three of these are written as sharing and are visible in the Verilog:
        the <b>one shifter</b> between two reversals, the <b>one adder</b> with
        six consumers, and the <b>comparator triple</b> feeding both the result
        mux and the branch unit.
      </p>
      <p>
        <code>diff</code> and the three relations are written as independent
        expressions — <code>x_op1 - x_op2</code>, <code>x_op1 &lt; x_op2</code>,
        <code>$signed(x_op1) &lt; $signed(x_op2)</code> — and whether they end
        up on one carry chain is the synthesiser's decision, not the RTL's. The
        489 LUT below is the whole stage measured after that decision, not an
        accounting of the pieces.
      </p>
    </Callout>

    <h2 class="doc-h2">The multiplier</h2>

    <p class="doc-p">
      EX carries <code>mul</code>, <code>mulh</code>, <code>mulhsu</code> and
      <code>mulhu</code>. What it does not carry is a divider or any floating
      point. <b>One 33 × 33 signed product serves all four forms</b> — only the
      operand extension and which half is returned differ — in three pipeline
      stages, occupying <b>4 DSP</b> rather than fabric.
    </p>

    <SpecTable
      :cols="mulForms.cols"
      :rows="mulForms.rows"
      caption="The same product, four readings of it: two extension bits and one half-select, derived from funct3 alone. mul takes the cheaper extension on rs2 because the low 32 bits of a product do not depend on how either operand was read. div, divu, rem and remu are decoded on the same funct7 and raise an illegal-instruction halt at the offending PC — recognised and rejected, never aliased onto the multiplier"
    />

    <p class="doc-p">
      The multiplier is <b>free-running</b>: the operands are frozen for the
      whole hold, so every capture takes the same value and gating would buy
      nothing but a mux. A multiply costs <b>three stall cycles</b>.
    </p>

    <WaveTrace
      label="a multiply in EX — three stall cycles"
      :rows="mulHold.rows"
      :notes="mulHold.notes"
    />

    <h3 class="doc-h3">Why the result mux is where the cost is</h3>

    <p class="doc-p">
      The multiply does not join the pipeline at a writeback port of its own. It
      joins through <code>ex_alu</code>, the stage's one combinational result —
      and <code>ex_alu</code> has two consumers with very different deadlines.
      One is a register. The other is the distance-1 forwarding path, which has
      to reach the ID operand register in the same cycle.
    </p>

    <Fig
      caption="Three inputs, one result, two consumers. m_val is captured at the end of this cycle; fwd_x_val has to cross the forwarding mux and reach the ID → EX operand register before it. A case added to the result mux is one more LUT level in front of the shorter of the two deadlines."
      zoom
      wide
    >
      <BlockDiagram :nodes="resultMux.nodes" :edges="resultMux.edges" />
    </Fig>

    <Callout
      kind="rule"
      title="The alternative, if the mux ever measures worse"
    >
      <p>
        Retire the product through MEM on its own writeback port. That leaves
        the distance-1 forward untouched, at the price of one more stall cycle.
      </p>
      <p>
        The option <i>not</i> taken is a scoreboard letting the multiply retire
        out of order. The hazard unit is the whole of this core's complexity
        budget — three sources by position, one stall rule, nothing else — and a
        scoreboard ends that invariant for one instruction.
      </p>
    </Callout>

    <h3 class="doc-h3">What the multiplier replaces</h3>

    <Callout
      kind="measured"
      title="About 54 cycles each, and that is the easy case"
    >
      <p>
        A software multiply was measured before the unit existed, on the
        full-system bench (<code>tests/pe/tools/rv_run.py</code> — real routers,
        the real memory agent, cycle counts read from the PE's own
        <code>CTL_CYCLE</code> counter).
        <b>Both figures below describe the core without the multiplier:</b> a
        128-element int8 dot product ran <b>8,221 cycles</b>, and the same
        kernel with its multiplies costed at one instruction each is
        <b>1,297</b> — so 128 software multiplies accounted for 6,924 cycles,
        <b>about 54 cycles each</b>.
      </p>
      <p>
        That is the <i>easy</i> case: int8 operands unroll to eight shift-add
        steps, where a general 32 × 32 <code>__mulsi3</code> unrolls to far
        more. Three stall cycles against roughly 54 is the trade the unit makes.
      </p>
    </Callout>

    <h3 class="doc-h3">Why the same purchase was cheap on the SIMT PE</h3>

    <p class="doc-p">
      Worth recording because it generalises past this core. The
      <RouterLink to="/mpe/simt" class="doc-link">SIMT PE</RouterLink> built the
      same multiply and paid neither of the two costs above.
    </p>

    <SpecTable
      :cols="simtWhy.cols"
      :rows="simtWhy.rows"
      caption="There, the multiplier is built at the float tier's own latency on purpose: equal latency makes a collision between a float result and a multiply result structurally impossible rather than arbitrated, because one instruction issues per cycle and two results can only want the write port on the same cycle if they were issued on the same cycle"
    />

    <Callout kind="rule" title="The general rule">
      <p>
        <b
          >A multi-cycle unit is cheap in a machine that already has a way to
          park an instruction, and expensive in one whose whole complexity
          budget is three positional forwards and one stall rule.</b
        >
      </p>
      <p>
        Note the other half of why <code>RV32M</code> was affordable at all: it
        is <b>standard encoding space</b>, so it cost none of the four custom
        opcode majors and a compiler emits it with
        <code>-march=rv32im</code> and nothing else.
      </p>
    </Callout>

    <h3 class="doc-h3">
      Why <code>div</code> and <code>rem</code> are a different answer
    </h3>

    <p class="doc-p">
      An iterative divider is ~35 cycles and a <b>33-bit subtract per cycle</b>,
      and it cannot borrow the one in EX: <code>sum</code> is
      <code>ex_addr</code>. Muxing a divider's operands into the EX adder puts a
      mux in front of the effective address, which is the one path the whole
      six-boundary arrangement exists to keep short.
    </p>

    <p class="doc-p">
      So a divider carries its own 33-bit subtractor, its own remainder shift
      register, its own quotient register, the sign fixups
      <code>div</code>'s truncate-toward-zero needs, and the two mandated
      special cases (÷0, and −2³¹ ÷ −1). <b>ESTIMATE 200–300 LUT</b> — reasoned
      from measured neighbours on the same part, not measured — which is most of
      the EX stage again, for an instruction a controller issues approximately
      never. And it is 35 cycles against libgcc's ~60–80: a 2× on a rare
      instruction, where the multiplier is an 8–13× on a common one.
    </p>

    <Callout kind="rule" title="Iterative division is a trap at this size">
      <p>
        Not because it is hard, but because its cost is a fixed structure and
        its benefit is a small multiple on a rare event. With the multiplier
        built, divide-by-a-constant strength-reduces to <code>mulhu</code> and
        covers the case a controller actually meets: turning a linear index into
        mesh coordinates.
      </p>
    </Callout>

    <h3 class="doc-h3">Why minimal scalar float is the wrong purchase</h3>

    <p class="doc-p">
      The tempting shape is to reuse what is already measured: one
      <code>khs_f16_lane</code> — a KohakuMPE unit measured on the same part — a
      32-entry float register file in the same LUTRAM shape as the integer one,
      and the two FP32 converters, one of which is pure wiring. Roughly
      <b>900–1,100 LUT and 2 DSP, ESTIMATE</b>. Two things make it the wrong
      purchase anyway, and neither is about the LUT.
    </p>

    <Callout
      kind="rule"
      title="Fifteen cycles, and an encoding no compiler emits"
    >
      <p>
        <b>Fifteen cycles into a three-source in-order forwarding network.</b>
        <code>fadd</code> would stall EX for fifteen cycles, or need the
        scoreboard the section above refuses. The SIMD tier does not have this
        problem because an <i>accumulating</i> instruction needs only the
        accumulator's own busy shadow, where an instruction that writes a
        register has to be tracked.
      </p>
      <p>
        <b>And it would not be <code>F</code>.</b> The format that exists is 24
        bits with no subnormals, one rounding mode and a documented one-ulp
        deviation on subtractive alignment. RISC-V's <code>F</code> is IEEE-754
        binary32 with subnormals, five rounding modes and <code>fcsr</code> —
        and this core has no CSR file at all. A non-conforming float behind a
        custom major also has nowhere to live: all four custom majors are spoken
        for. The core's first design objective is that ordinary compilers work
        unmodified; a float extension a compiler cannot target defeats it.
      </p>
      <p>
        Where a kernel needing float should be is the wide classes' float tier,
        which is built and measured — the format, the lane counts and the
        accuracy are
        <RouterLink to="/mpe/simd" class="doc-link">KohakuMPE's</RouterLink> to
        state.
      </p>
    </Callout>

    <h3 class="doc-h3">A faulting instruction retires, and commits nothing</h3>

    <SpecTable
      :cols="[
        { key: 'f', label: 'EX register field', mono: true },
        { key: 'v', label: 'On a fault', mono: true },
        { key: 'w', label: 'Why' },
      ]"
      :rows="[
        {
          f: 'm_valid',
          v: 'x_valid',
          w: 'the faulting instruction <b>retires</b> — it is the one that raised the halt, so squashing it would leave nothing to report',
        },
        {
          f: 'm_wen',
          v: 'x_wen &amp;&amp; !fault',
          w: 'it must not <b>commit</b>. A faulting load would otherwise write back the address',
          _tone: 'bad',
        },
        {
          f: 'm_load / m_store',
          v: '&amp;&amp; !fault',
          w: 'the request is cancelled in the same edge, so a faulting access never reaches memory',
          _tone: 'bad',
        },
        {
          f: 'the whole register',
          v: 'not gated by <code>ex_redir</code>',
          w: 'the instruction that caused the redirect is the one in this stage',
        },
      ]"
      caption="ECALL, EBREAK, an illegal encoding, a misaligned access and an unmapped region all take one path out of EX: a halt is a redirect that also stops fetch. There are no CSRs and no trap vector"
    />

    <Callout
      kind="rule"
      title="Everything in EX is qualified by “EX is not held”"
    >
      <p>
        The resolve, the predictor update and the halt are all gated by it.
        Without that, a stalled memory stage would let the same branch resolve
        every cycle and walk its saturating counter to a value it never earned.
      </p>
    </Callout>

    <ResourceBars
      :items="coreSplit.items"
      unit="LUT sites · inside u_core, 1,298 total"
      caption="Hierarchical LUT-site accounting from the run named at the top of this page: xcvu13p-fhgb2104-2L-e, Vivado 2024.2, OOC synthesis only, 2.500 ns request. The predictor fits inside the u_if number because its entries live in LUTRAM depth rather than logic"
    />

    <h2 class="doc-h2">Fetch, and a predictor read at the same address</h2>

    <p class="doc-p">
      A small branch-target buffer plus a 2-bit saturating table, read with the
      <b>same address as the instruction window</b>, so the prediction is
      available in the cycle the instruction's bits are. Its job is to remove
      the taken-branch penalty of a loop, not to be accurate.
    </p>

    <WaveTrace
      label="predicted taken — no bubble"
      :rows="predOk.rows"
      :notes="predOk.notes"
    />

    <p class="doc-p">
      Nothing in the predictor is speculative state needing repair. EX resolves
      every branch against the architectural answer, so a wrong prediction costs
      the redirect penalty and never correctness — which is why the tag can be
      short and the table can alias.
    </p>

    <WaveTrace
      label="mispredict — three bubbles, and the redirect is registered"
      :rows="mispredict.rows"
      :notes="mispredict.notes"
    />

    <Callout kind="note" title="One expression drives three things">
      <p>
        <code>pc_fetch</code> is the window's address, the predictor's read
        address and the input to the <code>f2_pc</code> register. Because it is
        one expression, the three can never disagree about which instruction is
        being fetched. <code>BTB_ENTRIES = 0</code> removes the structure
        entirely — a generate, not a zero-sized array — and every taken branch
        then pays the redirect.
      </p>
    </Callout>

    <Callout kind="rule" title="The update lands one cycle after the resolve">
      <p>
        EX's comparator driving the counter's read-modify-write is a long path
        for something non-architectural, so the resolve is registered on the way
        in and a cycle of staleness can only cost a prediction. Every resolve
        writes the <b>whole</b> entry — <code>{valid, cnt, tag, target}</code>
        is one LUTRAM word — so nothing has to be read back and preserved, and
        the entry count buys memory depth rather than logic.
      </p>
      <p>
        A redirect is registered for the same kind of reason: steering fetch in
        the resolve cycle would put the ALU output into the next-PC mux.
      </p>
    </Callout>

    <Callout
      kind="trap"
      title="A LUTRAM array has no reset, and X propagates into the fetch address"
    >
      <p>
        The predictor reads its entry twice: once at the lookup index, and once
        at the <b>unregistered</b> resolve index so the old counter arrives in
        the same cycle as the registered resolve. That second array is a mirror
        rather than a second port, because the lookup owns the first port every
        cycle — and it is instantiated at the <b>full entry width</b> even
        though it uses only the two counter bits. A narrower mirror reads back
        undefined bits, and they do not stay local.
      </p>
    </Callout>

    <Fig
      caption="An X read out of an unreset array reaches the saturating update, then the entry, then the prediction, then the fetch address — a core frozen on an undefined PC with nothing stalled. The array is therefore swept clean at power-on before the first prediction is allowed out, and two one-shot assertions name an X on the way out of the array and an X on the way in."
      zoom
      wide
    >
      <BlockDiagram :nodes="bpX.nodes" :edges="bpX.edges" />
    </Fig>

    <h2 class="doc-h2">
      The forwarding network, and the one thing that stalls
    </h2>

    <p class="doc-p">
      The forwarding network is the whole of the complexity budget:
      <b>three sources by position, one stall rule, nothing else.</b> Nothing
      here consults an instruction's identity — only how far behind the consumer
      its producer is.
    </p>

    <Fig
      caption="Four inputs chosen by position alone: distances 1, 2 and 3 are the three stages behind the consumer, and distance 4 never leaves the register file. The two dashed paths are the ones that can fail."
      zoom
      wide
    >
      <BlockDiagram :nodes="forward.nodes" :edges="forward.edges" />
    </Fig>

    <SpecTable :cols="distances.cols" :rows="distances.rows" />

    <p class="doc-p">
      The two stall terms are one rule:
      <b>a load's value does not exist until WB.</b> Distances 1 and 2 name a
      value that has not come out of an array yet, so they stall; distance 3 is
      exactly where it exists, so it forwards. That is the load-use penalty —
      two cycles back to back, one at a spacing of one — and it is the only
      stall the hazard unit has.
    </p>

    <WaveTrace
      :rows="loadUse"
      label="load-use, back to back — two stalls"
      :notes="[
        {
          cycle: 2,
          text: 'The consumer is in ID at distance 1 from a load in EX, and a load\'s value does not exist yet. It is held in ID; fetch holds with it and EX is fed a bubble.',
        },
        {
          cycle: 4,
          text: 'The load reaches WB, the distance-3 forward hands the value straight into ID, and the stall drops. Two cycles back to back; one at a spacing of one.',
        },
      ]"
    />

    <Fig
      caption="Where a hold goes. stall_d is the hazard rule; stall_x is EX's own reason not to advance — the memory stage, or a multiply. Together they hold fetch, the decode register and the register file's address, which is why widening a stall term is expensive here and adding a mux is not."
      zoom
      wide
    >
      <BlockDiagram :nodes="stall.nodes" :edges="stall.edges" />
    </Fig>

    <Callout
      kind="measured"
      title="FWD_X = 1 is the default because the mux was never the expensive part"
    >
      <p>
        Removing the distance-1 bypass looks like it should trade a cycle for
        frequency. Measured — the same OOC synthesis flow, run with
        <code>fwd_x 0</code> — it saves about
        <b>2 LUT and loses about 5 MHz</b>, because without the bypass the stall
        term widens from “hazard at distance 1 <b>and</b> the producer is a
        load” to “hazard at distance 1”, and that term fans out across
        everything in the diagram above. The <code>0</code> form stays built and
        verified so the claim survives re-measurement.
      </p>
    </Callout>

    <Callout
      kind="note"
      title="A select that names a value which is not ready is harmless"
    >
      <p>
        <code>fwd1_sel</code> is computed from the position compares alone, with
        no knowledge of whether the producer is a load. When it names a load in
        EX, the same condition has already raised the stall, so the operand
        register never captures it. That is what keeps the priority encoder
        small.
      </p>
    </Callout>

    <h2 class="doc-h2">The register file: 32 × 32, two reads, one write</h2>

    <p class="doc-p">
      No FPGA primitive offers two independent read ports, so 2R1W is built the
      standard way — two mirrored 1W1R arrays written identically. Read latency
      is 1 in both storage variants, which is why <code>REGFILE_PRIM</code> can
      be swapped without moving a pipeline boundary.
    </p>

    <Fig
      caption="The address is captured by the array itself rather than by a flop in front of it, so the array's own output register IS the pipeline boundary. ra_en holds the address through a stall, so the read is re-issued every cycle and a write that lands mid-stall is visible one cycle later. x0 is forced at the output rather than written as zero, so nothing depends on the array having been initialised."
      zoom
      wide
    >
      <BlockDiagram :nodes="regfile.nodes" :edges="regfile.edges" />
    </Fig>

    <SpecTable
      :cols="primChoice.cols"
      :rows="primChoice.rows"
      caption="Two storage variants behind one parameter, and that is the point of the module rather than an afterthought: the register file is one of the largest single LUT items in a core this small, so whether it should be LUTRAM or block RAM is a number, not an opinion. Same run, same conditions as every other figure here"
    />

    <Callout kind="rule" title="The distance-4 write-through is not optional">
      <p>
        A write lands at the same edge that captures a read address four
        instructions behind it, and a synchronous array returns the pre-write
        value for that read. The forwarding network covers distances 1 to 3;
        <code>byp1</code>/<code>byp2</code> cover distance 4, and
        <b>without it the core is wrong for exactly that one spacing</b> — the
        kind of bug that survives a casual test suite, which is why the
        co-simulation covers every producer-to-consumer distance by construction
        rather than by someone remembering this case.
      </p>
    </Callout>

    <h2 class="doc-h2">Inside the two L1s</h2>

    <p class="doc-p">
      The split itself — external L1 versus internal L1, by <i>who writes</i>
      rather than by what is stored — is on the
      <RouterLink to="/component/rv32pe" class="doc-link">RV32 PE</RouterLink>
      page, because it is what removes coherence from the memory model. What
      follows is how each of the two arrays behaves when both of its ports want
      the same word.
    </p>

    <h3 class="doc-h3">The write both ports can make at once</h3>

    <p class="doc-p">
      A window written by the fabric and read by its owner has one hard case:
      the push lands in the very word a poll loop is reading —
      <b>and on a doorbell that is the common case</b>, because the peer pushes
      exactly the word the consumer polls. A true-dual-port array returns
      undefined data for that collision <b>in silicon</b>, and per-port
      reasoning (“neither port reads what it writes”) is true per port and false
      across them.
    </p>

    <WaveTrace
      :rows="doorbellBroken"
      variant="broken"
      label="true dual port, no bypass"
      :notes="[
        {
          cycle: 2,
          text: 'Undefined data, at the exact moment the protocol depends on. A poll that sampled the array mid-push reads garbage and goes round the loop once more — and a four-instruction poll loop is about nine cycles, so one extra iteration costs a whole loop.',
          tone: 'bad',
        },
      ]"
    />

    <WaveTrace
      :rows="doorbellFixed"
      variant="fixed"
      label="byte-wise cross-port bypass"
      :notes="[
        {
          cycle: 2,
          text: 'When the fabric port writes the word the core is reading, the core receives the written bytes — correct rather than merely defined, and byte-wise because a peer\'s sb is as legal as a local one.',
          tone: 'good',
        },
        {
          cycle: 2,
          text: 'It sits on the critical path, which is the price of the doorbell being right. The rv_spad wrapper that carries it is 38 LUT sites, and the whole scratchpad — that wrapper plus the array around its two block RAMs — is 46. A window without the bypass would be a handful of sites.',
        },
      ]"
    />

    <SpecTable
      :cols="xport.cols"
      :rows="xport.rows"
      caption="Which answer is right belongs to the caller, not the array. rv_l1 additionally asserts that a colliding word never completes an access on the cycle the fill wrote it — the promise its XPORT_OK(1) is making"
    />

    <h3 class="doc-h3">Why a line is 32 bytes</h3>

    <p class="doc-p">
      The line size is not a cache-design choice. It is the <b>flit</b>'s
      payload width — a flit being the fixed-size packet the mesh moves — and
      everything about the fill path follows from making the two equal.
    </p>

    <Fig
      caption="PAY = FLIT_WIDTH − 4 × POS_WIDTH − 16, which is 256 at the defaults: a 288-bit flit less two coordinate pairs and 16 bits of type, tag, last and reserved. The L1's line is eight 32-bit words — also 256. So a fill is ONE request and ONE response, and a writeback is ONE descriptor and ONE beat."
    >
      <BitField :fields="flit" />
    </Fig>

    <Callout kind="note" title="And why the arrays themselves are 32 bits">
      <p>
        A 256-bit array port looks natural at that line size. It is not: a
        <code>RAMB36E2</code> in true-dual-port mode is 36 bits per port, so a
        256-bit true-dual-port array is eight block RAMs whose 32-bit face is
        the only one the CPU uses — and every read then needs an 8:1 32-bit mux
        on the load path. Walking a line as eight 32-bit words costs 8 cycles
        per fill against a DRAM latency of hundreds, and zero LUT.
      </p>
      <p>
        <b
          >A block-RAM port is 36 bits wide at that aspect, and a wider array
          silently becomes something else without the tool warning about it.</b
        >
        That is a durable property of the primitive, not of this design — which
        is also why every array here names its primitive rather than leaving it
        to inference. Left to inference, both the resource and the read latency
        can move between tool versions, and read latency here is pipeline
        structure.
      </p>
    </Callout>

    <Fig
      caption="Direct mapped, blocking, one outstanding miss. A dirty victim is walked out as eight 32-bit words and sent BEFORE the fill request goes, so the writeback and the fill are two separate transactions in a fixed order — never concurrent, which is what makes one transaction tag enough. L_F_WAIT leaves on resp_valid with the whole 256-bit line, and L_F_WR walks it back in over eight rotations."
      zoom
    >
      <StateMachine :states="l1sm.states" :edges="l1sm.edges" :r="40" />
    </Fig>

    <SpecTable
      :cols="l1states.cols"
      :rows="l1states.rows"
      caption="Thirteen states, of which the miss path above is six. The reset state is L_I_SCAN, not L_IDLE: the tag array is LUTRAM and has no reset, so every line reads back undefined until it has been written once. Per-line valid and dirty ride in that same tag word rather than in flop arrays, which is what makes the 128-line capacity nearly free — and what makes invalidate-all a one-line-per-cycle sweep rather than a broadcast"
    />

    <h3 class="doc-h3">The line buffer is a rotate, not an indexed register</h3>

    <WaveTrace
      label="walking a dirty victim out — eight words, no mux"
      :rows="rotate.rows"
      :notes="rotate.notes"
    />

    <Callout kind="rule" title="The limit of the trick is worth stating">
      <p>
        A rotate needs a 2:1 mux on every bit, so it pays only where the
        register was already written word-at-a-time and that mux already
        existed. Applied to a register loaded whole, the same construction
        <i>adds</i> logic — the granule writer at the top of
        <code>rv_pe</code> loads its 256-bit buffer in one beat and is therefore
        <b>indexed, not rotated</b>.
      </p>
    </Callout>

    <h2 class="doc-h2">The NoC requestor</h2>

    <p class="doc-p">
      Transaction tags, descriptor legality, response matching, write ordering
      and backpressure — everything about the framework memory protocol that
      RV32 software must never see. <code>lw</code> and <code>sw</code> are the
      whole interface software gets.
    </p>

    <Fig
      caption="The line-fill descriptor. A fill is an ENTRY read, not a plain read: entry_words = 1 with STREAM set asks the memory agent's read engine for one 32-byte entry, where a plain read would occupy the agent's shared read/write state machine and exclude a write for its whole duration."
    >
      <BitField :fields="descr" />
    </Fig>

    <SpecTable
      :cols="descrSpec.cols"
      :rows="descrSpec.rows"
      caption="Positions are absolute within the 256-bit payload at the default FLIT_WIDTH of 288. The owner column is what a second requestor's author needs: three of these fields are this core's to fill, one is fixed protocol, and two must be zero because the framework will allocate them"
    />

    <Callout
      kind="rule"
      title="The address field is forty bits, not the thirty-four the flit table shows"
    >
      <p>
        The agent's port slices <code>[255 -: 40]</code> whatever
        <code>ADDR_W</code> is, and the six bits above 34 carry the mesh and the
        aperture. Every other field the agent reads is named; the rest is zero,
        <b>which the spec requires rather than tolerates</b>. The translation
        from the 2 GB software window to the physical base is
        <code>DRAM_BASE | addr[30:0]</code> — OR-ed, never added, because the
        base's low 31 bits are zero by construction, so it costs no logic at
        all.
      </p>
    </Callout>

    <SpecTable :cols="threeDesc.cols" :rows="threeDesc.rows" />

    <h3 class="doc-h3">The tag rotates so a stale response is detectable</h3>

    <WaveTrace
      :rows="tagTrace.rows"
      :notes="tagTrace.notes"
      label="one outstanding read, and a rotating tag"
    />

    <h3 class="doc-h3">One write outstanding</h3>

    <Callout
      kind="trap"
      title="Slot 15 took a dirty line, and that line never reached DRAM"
    >
      <p>
        The protocol already forbids two <i>open</i> writes from one source —
        agent write slots are matched by source coordinate alone, so two would
        bind data to the wrong descriptor. The PE bounds
        <b>un-acknowledged</b> writes as well, and the reason is a measurement,
        not caution.
      </p>
      <p>
        The agent allocates the <b>lowest free</b> slot and issues the
        <b>lowest ready</b> one, which is not fair: a requester that outruns AXI
        is handed a high slot that is then never the lowest ready again.
        Bounding what is outstanding keeps the agent's in-use set where the two
        rules do compose.
      </p>
    </Callout>

    <StepPlayer :steps="slots" label="the agent's sixteen write slots">
      <template #default="{ state }">
        <BlockDiagram :nodes="state.grid.nodes" />
      </template>
    </StepPlayer>

    <Callout kind="trap" title="…and the count has to lead, not lag">
      <p>
        The outstanding-write count is incremented when the write
        <b>descriptor</b> goes out, not when the data flit follows it.
        <code>rv_l1</code> releases the writeback on that same cycle, so a count
        that lagged would leave a window in which flush-all sees zero
        outstanding and <b>declares itself finished one line short</b> — the
        same class of loss, one level up.
      </p>
      <p>
        Write acknowledgements are counted and then dropped. Nothing in the
        framework consumes them and a unit that holds one wedges the mesh — but
        flush-all and the completion both need to know when a writeback is
        actually in memory, and the acknowledgement is the only thing that says
        so.
      </p>
    </Callout>

    <h3 class="doc-h3">The push handshake is a register, not a wire</h3>

    <Fig
      caption="The completion FIFO's empty flag reaches the memory stage's stall through send_ready, and that stall reaches the branch predictor's saturating counter. A one-deep holding register cuts the path there, and costs no throughput: a push holds the engine for two cycles anyway, its descriptor and its data."
      zoom
      wide
    >
      <BlockDiagram
        :nodes="pushPath.nodes"
        :edges="pushPath.edges"
        :groups="pushPath.groups"
      />
    </Fig>

    <p class="doc-p">
      That register is also why the completion's idle test has four terms rather
      than three.
    </p>

    <SpecTable
      :cols="completion.cols"
      :rows="completion.rows"
      caption="All four must hold before the unit reports completion. The inbound completion queue is 8 entries deep with a sticky overflow bit: a queue that silently dropped a completion would be indistinguishable from a unit that never finished, so the loss is made detectable instead"
    />

    <h2 class="doc-h2">Changing one</h2>

    <p class="doc-p">
      The order below is what keeps a change from being priced wrongly. It is
      short because most of this core is not parameterised, and the parts that
      are have their trade already measured in the table above.
    </p>

    <ol class="doc-p list-decimal pl-5 space-y-1">
      <li>
        <b>Multiply the cost by the unit count before deciding anything.</b>
        This is the whole difference between this core's answers and a
        single-core machine's, and it is why a divider is refused here and built
        on the RV64 core.
      </li>
      <li>
        <b>Price a multi-cycle unit by what the machine already has</b>, not by
        what the unit does. A structure is cheap in a machine that already has a
        way to park an instruction and expensive in one whose whole complexity
        budget is positional forwarding — this core is the second kind.
      </li>
      <li>
        <b>Check whether a new signal reaches the stall.</b> The stall fans out
        to every pipeline register including the predictor's counter, so a term
        added to it is a term added to the widest net in the design. The push
        handshake became a register for exactly this reason.
      </li>
      <li>
        <b>Read the site column, never the primitive column</b>, and never
        subtract one from the other. The two accountings disagree by roughly 10
        % on this unit and the difference is not a measurement.
      </li>
      <li>
        <b>Re-measure anything the change shares a path with.</b> A saving is a
        property of a path, not of a module, and it moves when the path does.
      </li>
      <li>
        <b>Then quote the request beside the frequency.</b> The shipped figure
        on this page has <i>negative</i> slack — the request was not met — so
        every megahertz here is an upper bound on an upper bound.
      </li>
    </ol>

    <Callout kind="open" title="Open questions the flow does not answer">
      <p>
        <b>There is no routed result for this core.</b> Every figure is
        out-of-context synthesis, which is optimistic — a module elsewhere in
        this tree lost 0.740 ns going from synthesis to routing — and nothing in
        the flow says which of these paths would move.
      </p>
      <p>
        <b
          >The cost of <i>attaching</i> is not separated from the cost of
          computing.</b
        >
        Choosing between many small PEs and few large ones needs that
        subtraction, and the framework measures it with a null unit rather than
        deriving it — so the answer exists for one configuration and is not a
        model.
      </p>
      <p>
        <b>And the SIMD seam is unelaborated.</b> At
        <span class="chip">SIMD_EN = 0</span> nothing behind it is built, which
        is what makes the figures on this page trustworthy and also means the
        cost of turning it on is not among them.
      </p>
    </Callout>
  </DocPage>
</template>
