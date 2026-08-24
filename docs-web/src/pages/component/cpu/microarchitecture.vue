<script setup>
// ===========================================================================
// Controller PE — microarchitecture.
// Drawn from the RTL under src/kohakuaccel/pe/rv32/, not from the prose.
// Every LUT / Fmax figure is out-of-context synthesis on
// xcvu13p-fhgb2104-2L-e, Vivado 2024.2, synth only.
// ===========================================================================

/* --- the six register boundaries, laid out as the pipeline ---------------
 * Landscape on purpose: stage row on top, the register that CLOSES each stage
 * beneath it, and beneath that the array or escape that explains its shape. */
const P = 15.5;
const stageRow = [
  ["IF1", "next-PC select"],
  ["IF2", "instruction out · decode"],
  ["ID", "operands out · forward"],
  ["EX", "ALU · resolve · address"],
  ["MEM", "array address · we"],
  ["WB", "array data out · commit"],
];
const boundRow = [
  ["f2_pc · f2_valid · redir_q", "fetch_en = !hold"],
  ["d_pc · d_imm · d_alu · d_u1 · d_u2 …", "enabled by !d_hold"],
  ["x_op1 · x_op2 · x_rs2v · x_target", "enabled by !x_hold"],
  ["m_addr · m_be · m_sdata · m_val", "enabled by !x_hold"],
  ["w_val · w_off · w_region · w_f3", "no enable"],
  ["rf_we · rf_wa · rf_wd", "rv_wb is combinational"],
];
// The last flag is `accent`: the two synchronous arrays that cost the two
// extra boundaries, and the one value that leaves EX without a register.
const noteRow = [
  [0, "rv_imem addr reg", "READ_LAT 1 — boundary 1", true],
  [1, "rv_regfile addr reg", "READ_LAT 1 — boundary 2", true],
  [3, "ex_addr = sum", "combinational, to the pins", true],
  [5, "the write-through", "covers distance 4", false],
];

const landscape = {
  nodes: [
    ...stageRow.map(([l, s], i) => ({
      id: `s${i}`,
      x: i * P,
      y: 0,
      w: 14,
      h: 3.2,
      label: l,
      sub: s,
      accent: i === 3,
    })),
    ...boundRow.map(([l, s], i) => ({
      id: `b${i}`,
      x: i * P,
      y: 4.6,
      w: 14,
      h: 4.6,
      label: l,
      sub: s,
    })),
    ...noteRow.map(([i, l, s, a]) => ({
      id: `n${i}`,
      x: i * P,
      y: 11,
      w: 14,
      h: 3.4,
      label: l,
      sub: s,
      accent: a,
    })),
  ],
  edges: [
    ...stageRow
      .slice(1)
      .map((_, i) => ({ from: `s${i}:r`, to: `s${i + 1}:l`, dir: "h" })),
    ...stageRow.map((_, i) => ({
      from: `s${i}:b`,
      to: `b${i}:t`,
      dir: "v",
      dash: true,
    })),
    ...noteRow.map(([i]) => ({
      from: `b${i}:b`,
      to: `n${i}:t`,
      dir: "v",
      accent: true,
    })),
  ],
  groups: [
    { x: -0.6, y: 4, w: 15.2, h: 5.8, label: "rv_if" },
    { x: P - 0.6, y: 4, w: 30.7, h: 5.8, label: "rv_id" },
    { x: 3 * P - 0.6, y: 4, w: 15.2, h: 5.8, label: "rv_ex" },
    { x: 4 * P - 0.6, y: 4, w: 15.2, h: 5.8, label: "rv_mem" },
    { x: 5 * P - 0.6, y: 4, w: 15.2, h: 5.8, label: "rv_wb" },
  ],
};

/* --- EX: the ALU -------------------------------------------------------- */

const alu = {
  nodes: [
    {
      id: "op1",
      x: 0,
      y: 6.5,
      w: 11,
      h: 3.2,
      label: "x_op1",
      sub: "rs1, PC, or zero",
    },
    {
      id: "op2",
      x: 0,
      y: 10.5,
      w: 11,
      h: 3.2,
      label: "x_op2",
      sub: "rs2 or the immediate",
    },

    {
      id: "add",
      x: 15,
      y: 0,
      w: 15,
      h: 3.2,
      label: "sum = x_op1 + x_op2",
      sub: "ONE 32-bit adder",
      accent: true,
    },
    {
      id: "sub",
      x: 15,
      y: 3.7,
      w: 15,
      h: 3.2,
      label: "diff = x_op1 − x_op2",
      sub: "A_SUB only",
    },
    {
      id: "cmp",
      x: 15,
      y: 7.4,
      w: 15,
      h: 3.2,
      label: "eq · lt · ltu",
      sub: "also the branch unit's",
      accent: true,
    },
    {
      id: "shf",
      x: 15,
      y: 11.1,
      w: 15,
      h: 3.2,
      label: "one 33-bit shifter",
      sub: "SLL · SRL · SRA",
      accent: true,
    },
    {
      id: "bw",
      x: 15,
      y: 14.8,
      w: 15,
      h: 3.2,
      label: "^   |   &",
      sub: "one LUT level, no sharing",
    },

    {
      id: "mux",
      x: 34,
      y: 0,
      w: 11,
      h: 18,
      label: "alu_r",
      sub: "case (x_alu) — 10 encodings, 5 inputs",
      accent: true,
    },
    {
      id: "out",
      x: 49,
      y: 6.4,
      w: 13,
      h: 3.2,
      label: "ex_alu",
      sub: "to MEM, and forwarded",
      accent: true,
    },
    {
      id: "lnk",
      x: 49,
      y: 11,
      w: 13,
      h: 3.2,
      label: "x_pc + 4",
      sub: "the link value, at x_link",
    },
  ],
  edges: [
    { from: "op1:r", to: "add:l", dir: "h" },
    { from: "op1:r", to: "sub:l", dir: "h" },
    { from: "op1:r", to: "cmp:l", dir: "h" },
    { from: "op1:r", to: "shf:l", dir: "h" },
    { from: "op1:r", to: "bw:l", dir: "h" },
    { from: "op2:r", to: "add:l", dir: "h" },
    { from: "op2:r", to: "sub:l", dir: "h" },
    { from: "op2:r", to: "cmp:l", dir: "h" },
    { from: "op2:r", to: "shf:l", dir: "h" },
    { from: "op2:r", to: "bw:l", dir: "h" },
    { from: "add:r", to: "mux:l", dir: "h" },
    { from: "sub:r", to: "mux:l", dir: "h" },
    { from: "cmp:r", to: "mux:l", dir: "h" },
    { from: "shf:r", to: "mux:l", dir: "h" },
    { from: "bw:r", to: "mux:l", dir: "h" },
    { from: "mux:r", to: "out:l", dir: "h", accent: true },
    { from: "lnk:t", to: "out:b", dir: "v" },
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
    { from: "a:r", to: "c1:l", dir: "h" },
    { from: "a:r", to: "c2:l", dir: "h", accent: true },
    { from: "a:r", to: "c3:l", dir: "h" },
    { from: "a:r", to: "c4:l", dir: "h" },
    { from: "a:r", to: "c5:l", dir: "h" },
    { from: "a:r", to: "c6:l", dir: "h" },
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
    { from: "w:r", to: "r1:l", dir: "h" },
    { from: "w:r", to: "m1:l", dir: "h" },
    { from: "r1:r", to: "m1:l", dir: "h" },
    { from: "m1:b", to: "cat:t", dir: "v" },
    { from: "sgn:r", to: "cat:l", dir: "h", accent: true },
    { from: "cat:b", to: "shift:t", dir: "v", accent: true },
    { from: "sh:l", to: "shift:r", dir: "h" },
    { from: "shift:b", to: "r2:t", dir: "v" },
    { from: "shift:b", to: "m2:t", dir: "v" },
    { from: "r2:r", to: "m2:l", dir: "h" },
    { from: "m2:r", to: "res:l", dir: "h", accent: true },
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
    { from: "eq:r", to: "mux:l", dir: "h" },
    { from: "lt:r", to: "mux:l", dir: "h" },
    { from: "ltu:r", to: "mux:l", dir: "h" },
    { from: "f3hi:b", to: "mux:t", dir: "v", dash: true },
    { from: "mux:r", to: "xor:l", dir: "h", accent: true },
    { from: "f3lo:r", to: "xor:l", dir: "h" },
    { from: "xor:r", to: "cond:l", dir: "h", accent: true },
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

const exCost = {
  items: [
    {
      label: "EX — ALU, resolve, address, byte enables",
      value: 418,
      tone: "accent",
    },
    { label: "ID — operands, forwarding, branch-target adder", value: 234 },
    { label: "MEM — the region decode and the extract", value: 181 },
    { label: "IF, with the whole predictor", value: 135 },
    { label: "rv_regfile", value: 129 },
    { label: "hazard unit, run/halt, counters", value: 70 },
    { label: "WB", value: 20 },
  ],
};

const faultRetire = {
  cols: [
    { key: "f", label: "EX register field", mono: true },
    { key: "v", label: "On a fault", mono: true },
    { key: "w", label: "Why" },
  ],
  rows: [
    {
      f: "m_valid",
      v: "x_valid",
      w: "the faulting instruction <b>retires</b> — it is the one that raised the halt, so squashing it would leave nothing to report",
    },
    {
      f: "m_wen",
      v: "x_wen &amp;&amp; !fault",
      w: "it must not <b>commit</b>. A faulting load would otherwise write back the address",
      _tone: "bad",
    },
    {
      f: "m_load / m_store",
      v: "&amp;&amp; !fault",
      w: "the request is cancelled in the same edge, so a faulting access never reaches memory",
      _tone: "bad",
    },
    {
      f: "the whole register",
      v: "not gated by <code>ex_redir</code>",
      w: "the instruction that caused the redirect is the one in this stage",
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
      text: "EX resolves every branch against the architectural answer, so the predictor has no correctness role. ex_redir also drives kill, which clears f2_valid, d_v and x_valid at this same edge — the two younger instructions die together.",
    },
    {
      cycle: 4,
      text: "A redirect is registered, deliberately: steering fetch in the resolve cycle would put the ALU output into the next-PC mux; one more cycle costs a third bubble and keeps the ALU output going nowhere but a flop.",
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
      h: 3.4,
      label: "a narrower mirror",
      sub: "reads back X",
    },
    {
      id: "cnt",
      x: 19,
      y: 0,
      w: 15,
      h: 3.4,
      label: "n_cnt",
      sub: "the saturating update",
    },
    {
      id: "wd",
      x: 38,
      y: 0,
      w: 15,
      h: 3.4,
      label: "wr_d → the entry",
      sub: "X is written in",
    },
    {
      id: "q",
      x: 38,
      y: 6,
      w: 15,
      h: 3.4,
      label: "q_taken",
      sub: "and q_target",
    },
    {
      id: "pc",
      x: 19,
      y: 6,
      w: 15,
      h: 3.4,
      label: "pc_fetch",
      sub: "the next-PC mux",
    },
    {
      id: "dead",
      x: 0,
      y: 6,
      w: 15,
      h: 3.4,
      label: "frozen on an X PC",
      sub: "with nothing stalled",
      accent: true,
    },
  ],
  edges: [
    { from: "arr:r", to: "cnt:l", dir: "h" },
    { from: "cnt:r", to: "wd:l", dir: "h" },
    { from: "wd:b", to: "q:t", dir: "v" },
    { from: "q:l", to: "pc:r", dir: "h" },
    { from: "pc:l", to: "dead:r", dir: "h", accent: true },
  ],
};

/* --- hazards ------------------------------------------------------------ */

const forward = {
  nodes: [
    {
      id: "xrd",
      x: 0,
      y: 0,
      w: 14,
      h: 3.2,
      label: "EX — ex_alu",
      sub: "the combinational ALU output",
    },
    {
      id: "mrd",
      x: 0,
      y: 4,
      w: 14,
      h: 3.2,
      label: "MEM — m_val",
      sub: "the EX result register",
    },
    {
      id: "wrd",
      x: 0,
      y: 8,
      w: 14,
      h: 3.2,
      label: "WB — wstage_val",
      sub: "the writeback value",
    },
    {
      id: "rf",
      x: 0,
      y: 12,
      w: 14,
      h: 3.2,
      label: "rd1 · rd2",
      sub: "the register file — default",
    },
    {
      id: "mux",
      x: 20,
      y: 4,
      w: 14,
      h: 7.2,
      label: "fwd_pick",
      sub: "4:1 — nearest producer wins",
      accent: true,
    },
    {
      id: "op",
      x: 39,
      y: 4,
      w: 14,
      h: 7.2,
      label: "x_op1 · x_op2",
      sub: "the ID → EX register",
      accent: true,
    },
  ],
  edges: [
    { from: "xrd:r", to: "mux:l", dir: "h" },
    { from: "mrd:r", to: "mux:l", dir: "h" },
    { from: "wrd:r", to: "mux:l", dir: "h" },
    { from: "rf:r", to: "mux:l", dir: "h" },
    { from: "mux:r", to: "op:l", dir: "h", accent: true },
  ],
};

const stall = {
  nodes: [
    {
      id: "ld1",
      x: 0,
      y: 0,
      w: 14,
      h: 3.2,
      label: "hz1 && x_load",
      sub: "a load in EX — distance 1",
    },
    {
      id: "ld2",
      x: 0,
      y: 4,
      w: 14,
      h: 3.2,
      label: "hz2 && m_load",
      sub: "a load in MEM — distance 2",
    },
    {
      id: "sd",
      x: 20,
      y: 2,
      w: 14,
      h: 3.2,
      label: "stall_d",
      sub: "the only stall rule",
      accent: true,
    },
    {
      id: "hf",
      x: 39,
      y: 2,
      w: 14,
      h: 3.2,
      label: "hold_front",
      sub: "= stall_d || stall_m",
    },
    { id: "d1", x: 58, y: -2, w: 15, h: 3.2, label: "rv_if.hold" },
    { id: "d2", x: 58, y: 2, w: 15, h: 3.2, label: "rv_id.d_hold" },
    { id: "d3", x: 58, y: 6, w: 15, h: 3.2, label: "rv_regfile.ra_en" },
    {
      id: "bub",
      x: 39,
      y: 8.5,
      w: 14,
      h: 3.2,
      label: "bubble into EX",
      sub: "x_valid is cleared",
      accent: true,
    },
  ],
  edges: [
    { from: "ld1:r", to: "sd:l", dir: "h" },
    { from: "ld2:r", to: "sd:l", dir: "h" },
    { from: "sd:r", to: "hf:l", dir: "h", accent: true },
    { from: "hf:r", to: "d1:l", dir: "h" },
    { from: "hf:r", to: "d2:l", dir: "h" },
    { from: "hf:r", to: "d3:l", dir: "h" },
    { from: "sd:b", to: "bub:t", dir: "v", accent: true },
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
      w: "forwarded through <code>fwd_pick</code> at <code>FWD_X = 1</code> — <b>unless the producer is a load</b>",
    },
    {
      d: "2",
      p: "MEM",
      s: "m_val",
      w: "forwarded from a register — <b>unless the producer is a load</b>",
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

/* --- the register file --------------------------------------------------- */

const regfile = {
  nodes: [
    {
      id: "addr",
      x: 0,
      y: 3,
      w: 14,
      h: 3.2,
      label: "ra1 · ra2",
      sub: "f2_instr[19:15], [24:20]",
    },
    {
      id: "en",
      x: 0,
      y: 7.5,
      w: 14,
      h: 3.2,
      label: "ra_en = !hold_front",
      sub: "re-issue the read while held",
    },
    {
      id: "live",
      x: 18,
      y: 5,
      w: 13,
      h: 3.2,
      label: "ra1_live",
      sub: "ra_en ? ra1 : ra1_q",
      accent: true,
    },
    {
      id: "p1",
      x: 35,
      y: 0,
      w: 15,
      h: 3.6,
      label: "kohaku_sdpram #1",
      sub: "32 × 32 · distributed · LAT 1",
    },
    {
      id: "p2",
      x: 35,
      y: 4.5,
      w: 15,
      h: 3.6,
      label: "kohaku_sdpram #2",
      sub: "the mirror, written identically",
    },
    {
      id: "wport",
      x: 18,
      y: 10.5,
      w: 13,
      h: 3.2,
      label: "we · wa · wd",
      sub: "from rv_wb, same edge",
    },
    {
      id: "byp",
      x: 35,
      y: 10.5,
      w: 15,
      h: 3.6,
      label: "byp1 · byp2 · byp_d",
      sub: "wa == ra_live, registered",
      accent: true,
    },
    {
      id: "sel",
      x: 54,
      y: 4.5,
      w: 14,
      h: 4,
      label: "rd1 · rd2",
      sub: "byp ? byp_d : q — and x0 forced",
      accent: true,
    },
  ],
  edges: [
    { from: "addr:r", to: "live:l", dir: "h" },
    { from: "en:r", to: "live:l", dir: "h" },
    { from: "live:r", to: "p1:l", dir: "h" },
    { from: "live:r", to: "p2:l", dir: "h" },
    { from: "wport:r", to: "p1:l", dir: "h" },
    { from: "wport:r", to: "p2:l", dir: "h" },
    { from: "wport:r", to: "byp:l", dir: "h" },
    { from: "p1:r", to: "sel:l", dir: "h", accent: true },
    { from: "p2:r", to: "sel:l", dir: "h", accent: true },
    { from: "byp:r", to: "sel:l", dir: "h" },
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
      c: "<b>the shipped form.</b> 129 LUT of <code>u_core</code>'s 1,187. <code>u_core</code> carries 72 LUTRAM sites in total, shared with the predictor's entry arrays",
      _tone: "good",
    },
    {
      p: '"block"',
      r: "1",
      c: "two <code>RAMB36E2</code>, each <b>3.1 % depth-utilized</b> at 32 × 32 in a 1K × 36 aspect — the worst ratio anything in this design could post",
      _tone: "bad",
    },
  ],
};

/* --- the internal L1 ----------------------------------------------------- */

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
      a: "<b>bypassed, byte by byte.</b> The core receives the bytes the NoC just wrote — correct rather than merely defined, and on a doorbell the collision <i>is</i> the common case",
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

/* --- the NoC requestor --------------------------------------------------- */

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

const fill = (n, v) => Array.from({ length: n }, () => v);

const slots = [
  {
    title: "two rules, each reasonable alone",
    grid: slotGrid(fill(16, "free"), []),
    note: "The memory agent allocates the LOWEST FREE slot and issues the LOWEST READY one. Neither rule is wrong on its own.",
  },
  {
    title: "a flush that outruns AXI fills the slots",
    grid: slotGrid([...fill(15, "in use"), "PE line"], [15]),
    note: "The PE's dirty line is handed slot 15 — the lowest free one at that moment. Nothing is wrong yet.",
  },
  {
    title: "slot 15 is never the lowest ready again",
    grid: slotGrid(
      ["PE · out", "PE · out", ...fill(13, "free"), "STUCK"],
      [15],
    ),
    note: "AXI drains and the slots free from the bottom, so every later writeback takes 0 or 1 — the lowest free, and the lowest ready. Measured on a 64-line flush: slot 15 took a dirty line, MAG alternated slots 0 and 1 for the other sixty-three, and that line never reached DRAM. Silently.",
  },
  {
    title: "WR_MAX = 1 — bound what is outstanding",
    grid: slotGrid(["the one", ...fill(15, "free")], [0]),
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
      h: 3.4,
      label: "completion FIFO",
      sub: "noc_cu_base · empty",
    },
    { id: "sr", x: 18, y: 0, w: 13, h: 3.4, label: "send_ready" },
    { id: "tf", x: 34, y: 0, w: 13, h: 3.4, label: "tx_free · take_push" },
    {
      id: "pq",
      x: 50,
      y: 0,
      w: 15,
      h: 3.4,
      label: "pq_valid",
      sub: "ONE ENTRY, A FLOP",
      accent: true,
    },
    {
      id: "pr",
      x: 50,
      y: 8,
      w: 15,
      h: 3.4,
      label: "push_ready = !pq_valid",
      sub: "off a flop, not a wire",
      accent: true,
    },
    {
      id: "bs",
      x: 34,
      y: 8,
      w: 13,
      h: 3.4,
      label: "base_stall",
      sub: "rv_mem",
    },
    {
      id: "sm",
      x: 18,
      y: 8,
      w: 13,
      h: 3.4,
      label: "stall_m → x_hold",
      sub: "rv_ex",
    },
    {
      id: "cnt",
      x: 0,
      y: 8,
      w: 15,
      h: 3.4,
      label: "the saturating counter",
      sub: "rv_bpred",
    },
  ],
  edges: [
    { from: "fifo:r", to: "sr:l", dir: "h" },
    { from: "sr:r", to: "tf:l", dir: "h" },
    { from: "tf:r", to: "pq:l", dir: "h" },
    { from: "pq:b", to: "pr:t", dir: "v", accent: true },
    { from: "pr:l", to: "bs:r", dir: "h" },
    { from: "bs:l", to: "sm:r", dir: "h" },
    { from: "sm:l", to: "cnt:r", dir: "h" },
  ],
  groups: [
    { x: -1, y: -1.2, w: 67, h: 5.8, label: "the fabric side" },
    { x: -1, y: 6.8, w: 67, h: 5.8, label: "the core side" },
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
</script>

<template>
  <DocPage
    title="Controller PE microarchitecture"
    summary="The RTL, drawn. How the ALU is built out of one adder, one shifter and one comparator triple; the six register boundaries and what each holds; fetch and the predictor including a mispredict; the forwarding network and the one case that stalls; the register file, the L1's fill FSM, and the requestor's WR_MAX = 1."
    domain="cpu"
    status="shipped"
    source="src/kohakuaccel/pe/rv32/ · docs/arch/pe/microarchitecture.md"
  >
    <p class="doc-p">
      The
      <RouterLink to="/framework/cpu" class="doc-link"
        >Controller PE page</RouterLink
      >
      says what the core is and what it costs. This page opens it: what is
      physically in each stage, which wires are registered and which are not,
      and where the RTL's own comments record a failure. Every LUT and Fmax
      figure here is out-of-context synthesis on
      <code>xcvu13p-fhgb2104-2L-e</code>, Vivado 2024.2, synth only.
    </p>

    <h2 class="doc-h2">Six register boundaries for five stages</h2>

    <p class="doc-p">
      The instruction window and the register file are both synchronous arrays,
      and each costs a cycle between presenting an address and receiving data.
      Counting those honestly is what lets the fetch loop close: the address
      path in fetch is
      <code>PC → mux → RAM address register</code> and nothing else.
    </p>

    <Fig
      caption="Two extra boundaries, one per synchronous array, and two escapes that deliberately are not boundaries. Note where the enables differ: rv_id carries two registers because they stop for different reasons — a data hazard freezes decode and feeds EX a bubble, while a memory stall freezes EX as well and nothing moves at all."
      zoom
      wide
    >
      <BlockDiagram
        :nodes="landscape.nodes"
        :edges="landscape.edges"
        :groups="landscape.groups"
      />
    </Fig>

    <Callout
      kind="rule"
      title="The two things that are combinational on purpose"
    >
      <p>
        <b>Decode is combinational on the fetched word</b>, inside IF2.
        <code>ra1</code> and <code>ra2</code> come straight off
        <code>f2_instr[19:15]</code> and <code>[24:20]</code>, so the
        register-file address leaves at the same edge as the control bits — that
        buys the operand-fetch cycle instead of costing a seventh boundary. The
        RTL is explicit about what used to sit there:
        <i
          >“the ECALL opcode compare that sat here was most of the iteration-3
          binding path.”</i
        >
      </p>
      <p>
        <b>The effective address leaves EX combinationally</b> — it is the ALU's
        own adder output — because the data arrays register their address input.
        It must be at their pins in this cycle for the data to be out in MEM.
      </p>
    </Callout>

    <h2 class="doc-h2">The ALU, as it is actually built</h2>

    <p class="doc-p">
      Ten ALU encodings, and five things that compute. EX carries
      <b>one 32-bit adder</b> over the operands plus one PC+4 incrementer; the
      branch and jump target adder lives in ID, where PC and immediate are both
      registered by then and the adder is off every critical path in that stage.
      The whole stage — ALU, branch resolve, effective address, misalignment
      check, byte enables and store-data replication — is <b>418 LUT</b>.
    </p>

    <Fig
      caption="Five producers, one result mux: the three shift encodings all select the same input, and the comparator triple is shared with the branch unit, so a 10-way select is a 5-input mux."
      zoom
      wide
    >
      <BlockDiagram :nodes="alu.nodes" :edges="alu.edges" />
    </Fig>

    <SpecTable :cols="aluOps.cols" :rows="aluOps.rows" />

    <Callout
      kind="rule"
      title="Ten encodings, and no eleventh: the machine's multiplier is on the SIMT PE"
    >
      <p>
        There is no multiplier, no divider and no float in this stage.
        <code>mul</code> faults — <code>rv_id</code> accepts
        <code>funct7</code> of <code>0000000</code> and <code>0100000</code> on
        the register-register group and nothing else, so <code>RV32M</code>'s
        <code>0000001</code> raises an illegal-instruction halt at the offending
        PC.
      </p>
      <p>
        <b>The SIMT PE decodes that same <code>funct7</code> and builds it</b> —
        <code>mul</code>, <code>mulh</code>, <code>mulhsu</code>,
        <code>mulhu</code>, one product per lane on its per-thread register
        file. It was cheap there for reasons that are visible on this page: the
        two things that make a multiply expensive <i>here</i> are the result mux
        on <code>ex_alu</code>, which feeds <code>fwd_x_val</code> and therefore
        <code>x_op1_reg</code>, and a widened stall term in
        <code>stall_d</code>. The SIMT PE has neither — barrel scheduling
        deleted the forwarding network entirely, and a multi-cycle result reuses
        the per-wave pending flag that already existed for its float lanes. The
        costing, both ways, is on the
        <RouterLink to="/framework/cpu" class="doc-link"
          >controller PE</RouterLink
        >
        page.
      </p>
    </Callout>

    <h3 class="doc-h3">The adder has six consumers</h3>

    <p class="doc-p">
      <code>sum</code> is not “the add result”. It is the effective address, the
      JALR target, the alignment check and the byte-enable decode as well, and
      only one of those six uses reaches the result mux.
    </p>

    <Fig
      caption="One adder, six consumers, and the accented one leaves the stage without a register in front of it. This is why the address path and the ALU path cannot be separated in this core: they are the same carry chain."
      zoom
    >
      <BlockDiagram :nodes="adderFanout.nodes" :edges="adderFanout.edges" />
    </Fig>

    <h3 class="doc-h3">One 33-bit arithmetic shifter does all three shifts</h3>

    <p class="doc-p">
      A left shift is a right shift on the reversed word, and a bit reversal is
      wiring. The RTL says it in one line:
      <i
        >“a left shift is a right shift between two bit reversals, which are
        wiring, and SRA is the same shifter with the sign fed in above the
        word.”</i
      >
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
        the
        <b>one shifter</b> between two reversals, the <b>one adder</b> with six
        consumers, and the <b>comparator triple</b> feeding both the result mux
        and the branch unit.
      </p>
      <p>
        <code>diff</code> and the three relations are written as independent
        expressions — <code>x_op1 - x_op2</code>, <code>x_op1 &lt; x_op2</code>,
        <code>$signed(x_op1) &lt; $signed(x_op2)</code> — and whether they end
        up on one carry chain is the synthesiser's decision, not the RTL's. The
        418 LUT below is the whole stage measured after that decision, not an
        accounting of the pieces.
      </p>
    </Callout>

    <ResourceBars
      :items="exCost.items"
      unit="LUT · inside u_core, 1,187 total"
      caption="Hierarchical site accounting at a 2.5 ns request on xcvu13p-fhgb2104-2L-e, Vivado 2024.2, OOC synthesis only"
    />

    <h3 class="doc-h3">A faulting instruction retires, and commits nothing</h3>

    <SpecTable
      :cols="faultRetire.cols"
      :rows="faultRetire.rows"
      caption="ECALL, EBREAK, an illegal encoding, a misaligned access and an unmapped region all take one path out of EX: a halt is a redirect that also stops fetch. There are no CSRs and no trap vector"
    />

    <Callout kind="rule" title="Everything in EX is qualified by !x_hold">
      <p>
        <code>live = x_valid &amp;&amp; !x_hold</code> gates the resolve, the
        predictor update and the halt. The RTL says why:
        <i
          >“Without that, a stalled memory stage would let the same branch
          resolve every cycle and walk its saturating counter to a value it
          never earned.”</i
        >
      </p>
    </Callout>

    <h2 class="doc-h2">Fetch, and a predictor read at the same address</h2>

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

    <Callout
      kind="trap"
      title="The mirror array is full width, and it was not always"
    >
      <p>
        The predictor reads its entry twice: once at the lookup index, and once
        at the
        <b>unregistered</b> resolve index so the old counter arrives in the same
        cycle as the registered resolve. That second array is a mirror rather
        than a second port, because the lookup owns the first port every cycle —
        and it is instantiated at the <b>full entry width</b> even though it
        only uses the two counter bits.
      </p>
      <p>
        The RTL records what happened when it was not: a narrower array read
        back <b>X</b>, which reached the saturating update, then the entry, then
        <code>q_taken</code>, then the PC — a core frozen on an X fetch address
        with nothing stalled, and only the multi-core ping-pong caught it.
      </p>
    </Callout>

    <Fig
      caption="BROKEN — the propagation. The array has no reset at all, so a power-on sweep writes every entry clean before the first prediction is allowed out, and two one-shot assertions now name an X on the way out of the array and an X on the way in."
      zoom
    >
      <BlockDiagram :nodes="bpX.nodes" :edges="bpX.edges" />
    </Fig>

    <Callout kind="rule" title="The update lands one cycle after the resolve">
      <p>
        EX's comparator reaching the counter's read-modify-write was the binding
        path once the memory stalls were out of the way, so the resolve is
        registered on the way in. Nothing here is architectural, so a cycle of
        staleness can only cost a prediction. Every resolve writes the
        <b>whole</b> entry — <code>{valid, cnt, tag, target}</code> is one
        LUTRAM word — so nothing has to be read back and preserved, and the
        entry count buys memory depth rather than logic.
      </p>
    </Callout>

    <h2 class="doc-h2">
      The forwarding network, and the one thing that stalls
    </h2>

    <Fig
      caption="Three forwarding sources selected by position, plus the register file's own read as the default. The priority takes the nearest producer, and the select is computed from the position compares alone."
      zoom
      wide
    >
      <BlockDiagram :nodes="forward.nodes" :edges="forward.edges" />
    </Fig>

    <SpecTable :cols="distances.cols" :rows="distances.rows" />

    <Fig
      caption="hold_front is the term the FWD_X = 0 experiment widens — from hz1 && x_load to hz1 — and it reaches rv_if's fetch enable, rv_id's decode register and the register file's address hold. That fan-out is why removing the distance-1 bypass saves about 2 LUT and loses 5 MHz."
      zoom
      wide
    >
      <BlockDiagram :nodes="stall.nodes" :edges="stall.edges" />
    </Fig>

    <p class="doc-p">
      The two stall terms are one rule:
      <b>a load's value does not exist until WB.</b> Distances 1 and 2 name a
      value that has not come out of an array yet, so they stall; distance 3 is
      exactly where it exists, so it forwards. That is the load-use penalty —
      two cycles back to back, one at a spacing of one — and it is the only
      stall the hazard unit has.
    </p>

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
      caption="Two storage variants behind one parameter, and that is the point of the module rather than an afterthought: the register file is the largest single LUT item in a core this small, so whether it should be LUTRAM or block RAM is a number, not an opinion. xcvu13p-fhgb2104-2L-e, OOC synthesis"
    />

    <Callout kind="rule" title="The distance-4 write-through is not optional">
      <p>
        A write lands at the same edge that captures a read address four
        instructions behind it, and a synchronous array returns the pre-write
        value for that read. The forwarding network in
        <code>rv_id</code> covers distances 1 to 3; <code>byp1</code>/<code
          >byp2</code
        >
        cover distance 4, and
        <b>without it the core is wrong only for that one spacing</b> — which is
        why the co-simulation covers every producer-to-consumer distance by
        construction rather than by someone remembering this case.
      </p>
    </Callout>

    <h2 class="doc-h2">The internal L1: why a line is 32 bytes</h2>

    <p class="doc-p">
      The line size is not a cache-design choice. It is the flit's payload
      width, and everything about the fill path follows from making them equal.
    </p>

    <Fig
      caption="PAY = FLIT_WIDTH − 4 × POS_WIDTH − 16, which is 256 at the defaults: a 288-bit flit less two coordinate pairs and 16 bits of type, tag, last and reserved. The L1's line is eight 32-bit words — also 256. So a fill is ONE request and ONE response, and a writeback is ONE descriptor and ONE beat."
    >
      <BitField :fields="flit" />
    </Fig>

    <p class="doc-p">
      That is the protocol-adaptation half of the cache's job and it holds
      whatever the hit rate turns out to be. Ordinary
      <code>lb</code>/<code>lh</code>/<code>lw</code> and
      <code>sb</code>/<code>sh</code>/<code>sw</code> are presented to software
      while the upstream protocol stays line-oriented.
    </p>

    <Fig
      caption="Direct mapped, blocking, one outstanding miss. A dirty victim is walked out as eight 32-bit words and sent BEFORE the fill request goes, so the writeback and the fill are two separate transactions in a fixed order — never concurrent, which is what makes one transaction tag enough. L_F_WAIT leaves on resp_valid with the whole 256-bit line, and L_F_WR walks it back in over eight rotations."
      zoom
    >
      <StateMachine :states="l1sm.states" :edges="l1sm.edges" :r="40" />
    </Fig>

    <SpecTable
      :cols="l1states.cols"
      :rows="l1states.rows"
      caption="Thirteen states, of which the miss path above is six. The reset state is L_I_SCAN, not L_IDLE: the tag array is LUTRAM and has no reset, so every line reads back undefined until it has been written once"
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
        <i>adds</i> logic — the CU_DATA granule writer in
        <code>rv_pe</code> loads its 256-bit buffer in one beat and is therefore
        <b>indexed, not rotated</b>, measured at +178 LUT the other way.
      </p>
    </Callout>

    <h3 class="doc-h3">
      Every array declares how it handles a cross-port collision
    </h3>

    <p class="doc-p">
      A true dual-port collision returns undefined data <b>in silicon</b>, not
      just in the model, and per-port reasoning (“neither port reads what it
      writes”) is true per port and false across them.
      <code>rv_ram_be</code> makes the answer a parameter.
    </p>

    <SpecTable
      :cols="xport.cols"
      :rows="xport.rows"
      caption="Which answer is right belongs to the caller, not the array. rv_l1 additionally asserts that a colliding word never completes an access on the cycle the fill wrote it — the promise its XPORT_OK(1) is making"
    />

    <h2 class="doc-h2">The NoC requestor</h2>

    <p class="doc-p">
      Transaction tags, descriptor legality, response matching, write ordering
      and backpressure — everything about the framework memory protocol that
      RV32 software must never see.
      <code>lw</code> and <code>sw</code> are the whole interface software gets.
    </p>

    <Fig
      caption="The line-fill descriptor. A fill is an ENTRY read, not a plain read: entry_words = 1 with STREAM set asks the agent's read engine for one 32-byte entry, where a plain read would occupy the agent's shared read/write FSM and exclude a write for its whole duration."
    >
      <BitField :fields="descr" />
    </Fig>

    <Callout
      kind="rule"
      title="The address field is forty bits, not the thirty-four the flit table shows"
    >
      <p>
        <code>mag_mem_port</code> slices <code>[255 -: 40]</code> whatever
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

    <h3 class="doc-h3">WR_MAX = 1</h3>

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
        <b
          >Measured on a 64-line flush — slot 15 took a dirty line, MAG
          alternated slots 0 and 1 for the other sixty-three, and that line
          never reached DRAM, silently.</b
        >
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
        <code>n_out</code> is incremented when the write <b>descriptor</b> goes
        out, not when the data flit follows it. <code>rv_l1</code> releases the
        writeback on that same cycle, so a count that lagged would leave a
        window in which flush-all sees zero outstanding and
        <b>declares itself finished one line short</b> — the same class of loss,
        one level up.
      </p>
    </Callout>

    <h3 class="doc-h3">The push handshake is a register, not a wire</h3>

    <Fig
      caption="Driven from take_push, push_ready was the front of the binding path: the completion FIFO's empty flag reached the MEM stage's stall through send_ready, and that stall reached the branch predictor's saturating counter — 12 levels across five modules. One entry costs no throughput, because a push holds the engine for two cycles anyway, its descriptor and its data."
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
      caption="All four must hold before the kick FSM leaves K_RUN. Write acknowledgements are counted and then dropped — nothing in the framework consumes them and a unit that holds one wedges the mesh — but flush-all and the completion both need to know when a writeback is actually in memory, and the acknowledgement is the only thing that says so"
    />
  </DocPage>
</template>
