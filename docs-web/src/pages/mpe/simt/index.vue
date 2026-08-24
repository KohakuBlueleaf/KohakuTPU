<script setup>
// ---------------------------------------------------------------------------
// One instruction stream, many lanes. The decode is shared; the register file
// is replicated. That sentence is the whole of SIMT and it is what the first
// figure has to say.
// ---------------------------------------------------------------------------
const stream = {
  nodes: [
    { id: "pc", x: 15, y: 0, w: 11, label: "nxt[wave]", sub: "ONE per WAVE" },
    {
      id: "fetch",
      x: 15,
      y: 4.6,
      w: 11,
      label: "fetch",
      sub: "f1 → f2, 3 deep",
    },
    {
      id: "dec",
      x: 15,
      y: 9.2,
      w: 11,
      label: "control word",
      sub: "predecoded, ONE, shared",
      accent: true,
    },
    {
      id: "l0",
      x: 0,
      y: 18.5,
      w: 8,
      label: "lane 0",
      sub: "x0..x31 · ALU · FMA",
    },
    {
      id: "l1",
      x: 9.2,
      y: 18.5,
      w: 8,
      label: "lane 1",
      sub: "x0..x31 · ALU · FMA",
    },
    {
      id: "l2",
      x: 18.4,
      y: 18.5,
      w: 8,
      label: "lane 2",
      sub: "x0..x31 · ALU · FMA",
    },
    { id: "ld", x: 27.6, y: 18.5, w: 4.5, label: "…" },
    {
      id: "l7",
      x: 33.6,
      y: 18.5,
      w: 8,
      label: "lane 7",
      sub: "x0..x31 · ALU · FMA",
    },
  ],
  edges: [
    { from: "pc:b", to: "fetch:t", dir: "v" },
    { from: "fetch:b", to: "dec:t", dir: "v" },
    { from: "dec:b", to: "l0:t", dir: "v", accent: true },
    { from: "dec:b", to: "l1:t", dir: "v", accent: true },
    { from: "dec:b", to: "l2:t", dir: "v", accent: true },
    { from: "dec:b", to: "l7:t", dir: "v", accent: true },
  ],
  groups: [
    {
      x: -1,
      y: 17.4,
      w: 43.6,
      h: 5.6,
      label: "replicated — x0..x31 exist per LANE, per WAVE",
    },
  ],
};

// `add x5, x3, x4` — one instruction, eight results.
const oneInstr = {
  mask: [1, 1, 1, 1, 1, 1, 1, 1],
  rows: [
    { name: "x3", values: [7, 3, 11, 0, 5, 9, 2, 14] },
    { name: "x4", values: [1, 1, 1, 1, 1, 1, 1, 1] },
    { name: "x5 ←", values: [8, 4, 12, 1, 6, 10, 3, 15] },
  ],
};

// ---------------------------------------------------------------------------
// The two halves. The base ISA slot is spent on the per-thread side.
// ---------------------------------------------------------------------------
const halves = {
  nodes: [
    {
      id: "stream",
      x: 10.5,
      y: 0,
      w: 19,
      h: 3.6,
      label: "ONE program counter per WAVE",
      sub: "ONE instruction at a time, any wave",
      accent: true,
    },
    {
      id: "sreg",
      x: 0,
      y: 8.6,
      w: 16,
      label: "s0..s31",
      sub: "per WAVE · behind custom-2 / -3",
    },
    {
      id: "sbase",
      x: 0,
      y: 12.4,
      w: 16,
      label: "base pointers, trip counts",
      sub: "SALU · rdctl · s2v",
    },
    {
      id: "sbr",
      x: 0,
      y: 16.2,
      w: 16,
      label: "sbeqz / sbnez",
      sub: "uniform branches",
    },
    {
      id: "vreg",
      x: 24,
      y: 8.6,
      w: 16,
      label: "x0..x31",
      sub: "per LANE per WAVE · ORDINARY RV32I",
      accent: true,
    },
    {
      id: "vmask",
      x: 24,
      y: 12.4,
      w: 16,
      label: "active mask + IPDOM stack",
      sub: "split · join · tmc",
      accent: true,
    },
    {
      id: "vlanes",
      x: 24,
      y: 16.2,
      w: 16,
      label: "8 int lanes + 8 float lanes",
      sub: "32 bits each, always computing",
      accent: true,
    },
    {
      id: "lsu",
      x: 24,
      y: 22,
      w: 16,
      label: "the LSU",
      sub: "one address PER LANE",
      accent: true,
    },
  ],
  edges: [
    { from: "stream:b", to: "sreg:t", dir: "v" },
    { from: "stream:b", to: "vreg:t", dir: "v", accent: true },
    { from: "vlanes:b", to: "lsu:t", dir: "v", accent: true },
    { from: "vlanes:l", to: "sbr:r", dir: "h", label: "subgroup ops" },
  ],
  groups: [
    { x: -1, y: 7.4, w: 18, h: 12.4, label: "the scalar half" },
    { x: 23, y: 7.4, w: 18, h: 12.4, label: "the per-thread half" },
  ],
};

const follows = {
  cols: [
    { key: "thing", label: "What SIMT adds" },
    { key: "how", label: "How it is built" },
    { key: "cost", label: "Measured cost", mono: true, align: "right" },
  ],
  rows: [
    {
      thing: "<b>A mask, not a predicate on the datapath</b>",
      how: "an inactive lane computes whatever it computes and its <i>write</i> is dropped — one enable per bank, nothing on the arithmetic path",
      cost: "+64 LUT<br><span class='opacity-60'>G2, 8 lanes</span>",
      _tone: "good",
    },
    {
      thing: "<b>An IPDOM stack</b>",
      how: "<code>split</code> and <code>join</code> implement structured divergence exactly, without the compiler proving anything about uniformity",
      cost: "+188 LUT<br><span class='opacity-60'>G3, 8 lanes</span>",
      _tone: "good",
    },
    {
      thing: "<b>Sixteen resident wave contexts</b>",
      how: "the register file was already a memory, so a wave id is <i>address bits</i> — but scheduling them is a front end, and that is not free",
      cost: "+0 to store<br>+1,775 to schedule",
      _tone: "good",
    },
    {
      thing: "<b>A subgroup network</b>",
      how: "one butterfly serves <code>shflxor</code> and <code>bcast</code> — <code>log2(LANES)</code> conditional swaps, not a crossbar",
      cost: "+935 LUT<br><span class='opacity-60'>G8, 8 lanes</span>",
      _tone: "good",
    },
    {
      thing: "<b>An address per lane</b>",
      how: "three addressing tiers already distinguished in the encoding, so a coalescer can replace the serial walk without the ISA moving",
      cost: "not built<br><span class='opacity-60'>G5</span>",
      _tone: "warn",
    },
  ],
};

// ---------------------------------------------------------------------------
// The float tier. Operand width is a PROPERTY OF THE INSTRUCTION. There is no
// dtype knob and no dtype name for the capability — see khs_float_lane's own
// header, quoted on the page.
// ---------------------------------------------------------------------------
const floatPath = {
  nodes: [
    { id: "a32", x: 0, y: 0, w: 10, label: "FP32 in", sub: "vreg[31:0]" },
    { id: "a16", x: 0, y: 4.4, w: 10, label: "FP16 in", sub: "vreg[15:0]" },
    { id: "cvt", x: 13, y: 2.2, w: 11, label: "vec_cvt", sub: "→ E8M15" },
    {
      id: "alu",
      x: 26,
      y: 2.2,
      w: 13,
      label: "vec_alu FMA",
      sub: "E8M15 · 15 cyc · II 1",
      accent: true,
    },
    { id: "y32", x: 41, y: 0, w: 10, label: "FP32 out", sub: "vreg[31:0]" },
    { id: "y16", x: 41, y: 4.4, w: 10, label: "FP16 out", sub: "vreg[15:0]" },
    {
      id: "half",
      x: 13,
      y: 9.8,
      w: 11,
      label: "half = f7[2]",
      sub: "per instruction",
      accent: true,
    },
    {
      id: "hp",
      x: 26,
      y: 9.8,
      w: 13,
      label: "hpipe, 15 deep",
      sub: "the width follows the result",
    },
  ],
  edges: [
    { from: "a32:r", to: "cvt:l", dir: "h" },
    { from: "a16:r", to: "cvt:l", dir: "h" },
    { from: "cvt:r", to: "alu:l", dir: "h", accent: true },
    { from: "alu:r", to: "y32:l", dir: "h" },
    { from: "alu:r", to: "y16:l", dir: "h" },
    { from: "half:t", to: "cvt:b", dir: "v", accent: true },
    { from: "half:r", to: "hp:l", dir: "h" },
    { from: "hp:r", to: "y16:b", dir: "h", label: "selects" },
  ],
};

const conversions = {
  cols: [
    { key: "c", label: "Conversion", mono: true },
    { key: "p", label: "Property" },
  ],
  rows: [
    {
      c: "FP16 → E8M15",
      p: "<b>exact</b> — nothing is lost, and a subnormal normalises into an ordinary E8M15 value",
      _tone: "good",
    },
    {
      c: "FP32 → E8M15",
      p: "the exponent field is kept <b>verbatim</b>, so range is FP32's; mantissa below bit 8 is rounded off",
      _tone: "good",
    },
    {
      c: "E8M15 → FP16",
      p: "the one direction that is both lossy <b>and</b> range-limited — a finite overflow <b>saturates silently</b> to the largest finite FP16",
      _tone: "bad",
    },
  ],
};

const granule = `   8 lanes x 32 bit  =  256 bit  =  one native memory entry  =  one flit payload

   integer lanes  <-  the memory granule  (256-bit entry / flit)   8, FIXED
   float lanes    <-  arithmetic demand   (throughput vs LUT)      a KNOB`;

const renderMix = {
  cols: [
    { key: "stage", label: "Stage" },
    { key: "needs", label: "What it needs" },
    { key: "kind", label: "Kind", align: "right" },
  ],
  rows: [
    {
      stage: "rasterisation, edge equations",
      needs: "exact, watertight edge functions on a subpixel grid",
      kind: "<b>integer</b>",
    },
    {
      stage: "depth interpolation and buffer",
      needs: "24-bit fixed point — M15 <b>z-fights</b>",
      kind: "<b>integer / fixed</b>",
    },
    {
      stage: "texture addressing",
      needs:
        "wrap, clamp, mip select, Morton swizzle — <code>y*width+x</code> is why RV32M is built",
      kind: "<b>integer / bitwise</b>",
    },
    {
      stage: "texture filtering",
      needs: "fixed-point or E8M15 weights",
      kind: "float-ish",
    },
    {
      stage: "fragment / colour shading",
      needs: "mediump — E8M15 exceeds fp16 in range and mantissa",
      kind: "<b>float</b>",
    },
    {
      stage: "vertex transform",
      needs: "E8M15 products into an FP32 accumulator",
      kind: "<b>float</b>",
    },
  ],
};

// ---------------------------------------------------------------------------
// ISA
// ---------------------------------------------------------------------------
const rtype = [
  { name: "funct7", bits: 7, value: "operation", accent: true },
  { name: "rs2", bits: 5 },
  { name: "rs1", bits: 5 },
  { name: "funct3", bits: 3, value: "group", accent: true },
  { name: "rd", bits: 5 },
  { name: "opcode", bits: 7, value: "0x5B custom-2" },
];

const itype = [
  { name: "imm", bits: 12, value: "12-bit immediate", accent: true },
  { name: "rs1", bits: 5 },
  { name: "funct3", bits: 3, value: "the instruction", accent: true },
  { name: "rd", bits: 5 },
  { name: "opcode", bits: 7, value: "0x7B custom-3" },
];

const vmemF7 = [
  { name: "op", bits: 3, value: "0..5", accent: true },
  { name: "scale", bits: 2, value: "0..3" },
  { name: "width", bits: 2, value: "b / h / w" },
];

const fltF7 = [
  { name: "reserved", bits: 4, value: "0" },
  { name: "half", bits: 1, value: "operand width", accent: true },
  { name: "op", bits: 2, value: "fma / mul / add / sub", accent: true },
];

const groups = {
  cols: [
    { key: "g", label: "Group", mono: true },
    { key: "enc", label: "Encoding", mono: true },
    { key: "n", label: "Count", align: "right", mono: true },
    { key: "what", label: "What" },
  ],
  rows: [
    {
      g: "SALU",
      enc: "custom-2, funct3 0",
      n: "10",
      what: "scalar ALU, register-register",
    },
    {
      g: "SMOV",
      enc: "custom-2, funct3 1",
      n: "2",
      what: "<code>s2v</code>, <code>rdctl</code>",
    },
    {
      g: "DIV",
      enc: "custom-2, funct3 2",
      n: "4",
      what: "<code>split</code>, <code>join</code>, <code>tmc</code>, <code>bar</code>",
    },
    {
      g: "SUB",
      enc: "custom-2, funct3 3",
      n: "10",
      what: "subgroup: shuffle, broadcast, ballot, five reductions, <code>vreadfirst</code>, <code>vlaneid</code>",
    },
    {
      g: "VMEM",
      enc: "custom-2, funct3 4",
      n: "64",
      what: "scalar base + vector offset — six op stems × widths × four scales",
    },
    {
      g: "FLT",
      enc: "custom-2, funct3 5",
      n: "8",
      what: "<code>vfma</code>, <code>vfmul</code>, <code>vfadd</code>, <code>vfsub</code> and their <code>_h</code> forms",
      _tone: "good",
    },
    {
      g: "—",
      enc: "custom-3, funct3 0–7",
      n: "8",
      what: "scalar ALU immediate, and the two uniform branches",
    },
    {
      g: "<i>RV32M</i>",
      enc: "<i>OP major, funct7 = 0000001</i>",
      n: "<i>4</i>",
      what: "<i><code>mul</code>, <code>mulh</code>, <code>mulhsu</code>, <code>mulhu</code> — <b>not</b> in the custom table or its count</i>",
      _tone: "good",
    },
  ],
};

const fltOps = {
  cols: [
    { key: "i", label: "Instruction", mono: true },
    { key: "d", label: "Does", mono: true },
    { key: "how", label: "How the one datapath serves it" },
  ],
  rows: [
    {
      i: "vfma vd, vs1, vs2",
      d: "vd = vs1 * vs2 + vd",
      how: "<b>three reads against two ports</b> — the third address is the destination, so it needs no new instruction field, and <code>rd</code> is compared as a <b>source</b> by the hazard logic",
    },
    {
      i: "vfmul vd, vs1, vs2",
      d: "vd = vs1 * vs2",
      how: "the lane with its addend forced to 0.0",
    },
    {
      i: "vfadd vd, vs1, vs2",
      d: "vd = vs1 + vs2",
      how: "the lane with its multiplier forced to 1.0",
    },
    {
      i: "vfsub vd, vs1, vs2",
      d: "vd = vs1 - vs2",
      how: "inverts vs2's <b>sign bit</b> — bit 31 or bit 15, whichever the width bit says. A lane has no subtract and negating a float is one bit",
    },
  ],
};

const vmemOps = {
  cols: [
    { key: "op", label: "op", mono: true, align: "right" },
    { key: "stem", label: "stem", mono: true },
    { key: "what", label: "What" },
  ],
  rows: [
    { op: "0", stem: "vl", what: "load, sign-extended" },
    { op: "1", stem: "vlu", what: "load, zero-extended" },
    { op: "2", stem: "vs", what: "store" },
    { op: "3", stem: "vlin", what: "lane-linear load, signed", _tone: "good" },
    {
      op: "4",
      stem: "vlinu",
      what: "lane-linear load, unsigned",
      _tone: "good",
    },
    { op: "5", stem: "vsin", what: "lane-linear store", _tone: "good" },
  ],
};

const divOps = {
  cols: [
    { key: "i", label: "Instruction", mono: true },
    { key: "what", label: "What it does" },
  ],
  rows: [
    {
      i: "split vs1",
      what: "diverge on the per-lane predicate in <code>vs1</code> (non-zero = true). Pushes <b>two</b> entries.",
    },
    {
      i: "join",
      what: "pop: restore the saved mask, resume at the popping join's own <code>pc+4</code>. Pops <b>one</b>.",
    },
    {
      i: "tmc ss1",
      what: "the active mask ← <code>ss1[LANES-1:0]</code>; a mask of zero retires the wave",
    },
    {
      i: "bar ss1, ss2",
      what: "workgroup barrier <code>ss1</code> across <code>ss2</code> waves — workgroup scope only, because one workgroup is one PE. <b>Encodes and does not execute</b>",
      _tone: "warn",
    },
  ],
};

const toScalar = {
  cols: [
    { key: "i", label: "Instruction", mono: true },
    { key: "takes", label: "Takes" },
    { key: "why", label: "Why it is not the other two" },
  ],
  rows: [
    {
      i: "vreadfirst sd, vs1",
      takes: "the <b>lowest active</b> lane's value — one lane, selected",
      why: "what makes a <i>memory-resident uniform</i> reachable at all. Defined under a mask because it names the lowest <i>active</i> lane, never lane 0, which may be masked off — and the selection tree prefers its left subtree, so that is structural rather than ordered.",
    },
    {
      i: "ballot sd, vs1",
      takes: "one bit per lane — a predicate, across lanes",
      why: "a condition, not a value. One OR per lane, ANDed with the mask: one level deep",
    },
    {
      i: "redux* sd, vs1",
      takes: "add / max / min / and / or — a reduction, across lanes",
      why: "a pipelined <b>tree</b>, log2(LANES) deep. As a chain it held the whole core at 71.7 MHz",
    },
  ],
};

const tiers = {
  cols: [
    { key: "form", label: "Form", mono: true },
    { key: "knows", label: "What the hardware knows" },
    { key: "req", label: "Requests", align: "right", mono: true },
  ],
  rows: [
    {
      form: "lane-linear<br><span class='opacity-60'>vlin / vsin</span>",
      knows: "everything, at decode — no vector operand at all",
      req: "ALWAYS 1",
      _tone: "good",
    },
    {
      form: "uniform base<br><span class='opacity-60'>vl / vs</span>",
      knows:
        "the high bits are equal, so the compare is over <b>offset fields</b> rather than full computed addresses",
      req: "1..LANES",
    },
    {
      form: "RV32I lw / sw<br><span class='opacity-60'>per-lane base</span>",
      knows: "nothing",
      req: "1..LANES",
    },
  ],
};

// ---------------------------------------------------------------------------
// Residency
// ---------------------------------------------------------------------------
const regions = {
  nodes: [
    {
      id: "ea",
      x: 15.5,
      y: 0,
      w: 12,
      label: "ea",
      sub: "one per LANE",
      accent: true,
    },
    {
      id: "dram",
      x: 0,
      y: 5.5,
      w: 12,
      label: "R_DRAM",
      sub: "ea[31] = 1 · via rv_l1",
    },
    {
      id: "on",
      x: 14.5,
      y: 5.5,
      w: 14,
      label: "ea[31] = 0",
      sub: "ea[30:28] selects",
    },
    { id: "spad", x: 0, y: 11.5, w: 10, label: "R_SPAD", sub: "= 1" },
    { id: "ctl", x: 11, y: 11.5, w: 10, label: "R_CTL", sub: "= 2" },
    { id: "lds", x: 22, y: 11.5, w: 10, label: "R_LDS", sub: "= 4" },
    { id: "bad", x: 33, y: 11.5, w: 10, label: "R_BAD", sub: "else — FAULT" },
  ],
  edges: [
    { from: "ea:b", to: "dram:t", dir: "v" },
    { from: "ea:b", to: "on:t", dir: "v", accent: true },
    { from: "on:b", to: "spad:t", dir: "v" },
    { from: "on:b", to: "ctl:t", dir: "v" },
    { from: "on:b", to: "lds:t", dir: "v" },
    { from: "on:b", to: "bad:t", dir: "v" },
  ],
};

const residency = {
  cols: [
    { key: "where", label: "Where a value lives", mono: true },
    { key: "scope", label: "Scope" },
    { key: "who", label: "Who writes it" },
    { key: "prim", label: "Primitive", mono: true },
  ],
  rows: [
    {
      where: "x0..x31",
      scope: "per LANE, per WAVE",
      who: "this shader only",
      prim: "block RAM<br><span class='opacity-60'>2 banks per lane, 3 at HAS_FLT</span>",
    },
    {
      where: "s0..s31",
      scope: "per WAVE",
      who: "this shader only, through custom-2/-3",
      prim: "distributed<br><span class='opacity-60'>33 bits: {zero, value}</span>",
    },
    {
      where: "IPDOM pairs",
      scope: "per WAVE",
      who: "<code>split</code> writes, <code>join</code> reads",
      prim: "distributed<br><span class='opacity-60'>20 LUT at G3</span>",
    },
    {
      where: "R_LDS · R_SPAD",
      scope: "per PE",
      who: "the NoC <b>and</b> this core — it is the <i>home</i> of its addresses, never a copy",
      prim: "LANES banks<br><span class='opacity-60'>word-interleaved</span>",
    },
    {
      where: "rv_l1",
      scope: "per PE",
      who: "this core only, plus fills — never externally written",
      prim: "direct mapped<br><span class='opacity-60'>ONE outstanding miss</span>",
    },
    {
      where: "R_DRAM",
      scope: "the whole machine",
      who: "everyone, through the memory agent",
      prim: "DDR",
    },
  ],
};

// ---------------------------------------------------------------------------
// The ladder
// ---------------------------------------------------------------------------
const gates = {
  cols: [
    { key: "g", label: "Gate", mono: true },
    { key: "gen", label: "Generics", mono: true },
    { key: "adds", label: "What it adds" },
    { key: "built", label: "Built", align: "center" },
  ],
  rows: [
    {
      g: "G0",
      gen: "WAVES 1, HAS_MASK 0, HAS_IPDOM 0",
      adds: "the arithmetic substrate: lanes, register file, writeback",
      built: "yes",
      _tone: "good",
    },
    {
      g: "G1",
      gen: "WAVES 16",
      adds: "wave-indexed storage — many wave contexts",
      built: "yes",
      _tone: "good",
    },
    {
      g: "G2",
      gen: "HAS_MASK 1",
      adds: "the active mask, <code>tmc</code>, predication",
      built: "yes",
      _tone: "good",
    },
    {
      g: "G3",
      gen: "HAS_IPDOM 1",
      adds: "<code>split</code>/<code>join</code>, the bounded stack, the overflow fault",
      built: "yes",
      _tone: "good",
    },
    {
      g: "G4",
      gen: "HAS_LDSBANK",
      adds: "divergent LDS addressing, bank conflicts resolved in hardware",
      built: "yes",
      _tone: "good",
    },
    {
      g: "G5",
      gen: "—",
      adds: "the coalescer: one gather becomes one request when lanes agree",
      built: "no",
      _tone: "warn",
    },
    {
      g: "G6",
      gen: "—",
      adds: "MSHRs: more than one miss in flight",
      built: "no",
      _tone: "warn",
    },
    {
      g: "G7",
      gen: "WAVES on kht_core",
      adds: "the wave scheduler: waves genuinely issuing, not merely stored",
      built: "yes",
      _tone: "good",
    },
    {
      g: "G8",
      gen: "HAS_SHFL",
      adds: "the subgroup butterfly for <code>shflxor</code> and <code>bcast</code>",
      built: "yes",
      _tone: "good",
    },
    {
      g: "G9",
      gen: "HAS_FLT, FLANES",
      adds: "the float tier — and, riding its retire slot, RV32M integer multiply",
      built: "yes",
      _tone: "good",
    },
  ],
};

const ladder = {
  cols: [
    { key: "g", label: "Gate" },
    { key: "w", label: "WAVES", align: "right", mono: true },
    { key: "m", label: "mask", align: "right", mono: true },
    { key: "i", label: "ipdom", align: "right", mono: true },
    { key: "lut", label: "LUT", align: "right", mono: true },
    { key: "d", label: "ΔLUT", align: "right", mono: true },
    { key: "ff", label: "FF", align: "right", mono: true },
    { key: "bram", label: "BRAM", align: "right", mono: true },
    { key: "cs", label: "ctrl sets", align: "right", mono: true },
    { key: "f", label: "Fmax", align: "right", mono: true },
  ],
  rows: [
    {
      g: "G0 the arithmetic substrate",
      w: "1",
      m: "0",
      i: "0",
      lut: "2,952",
      d: "—",
      ff: "307",
      bram: "8",
      cs: "2",
      f: "324.1 MHz",
    },
    {
      g: "G1 wave-indexed storage",
      w: "16",
      m: "0",
      i: "0",
      lut: "2,952",
      d: "<b>+0</b>",
      ff: "311",
      bram: "8",
      cs: "2",
      f: "324.1 MHz",
    },
    {
      g: "G2 active masks, <code>tmc</code>",
      w: "16",
      m: "1",
      i: "0",
      lut: "3,016",
      d: "+64",
      ff: "447",
      bram: "8",
      cs: "18",
      f: "324.1 MHz",
    },
    {
      g: "G3 <code>split</code>/<code>join</code>, bounded stack",
      w: "16",
      m: "1",
      i: "1",
      lut: "3,204",
      d: "+188",
      ff: "516",
      bram: "8",
      cs: "36",
      f: "324.1 MHz",
    },
  ],
};

const deltas = [
  {
    label: "G1 — WAVES 1 → 16, sixteen wave contexts",
    value: 0,
    note: "+0 LUT, +4 FF",
    tone: "good",
  },
  {
    label: "G2 — HAS_MASK, the active mask and tmc",
    value: 64,
    note: "+64 LUT",
    tone: "good",
  },
  {
    label: "G3 — HAS_IPDOM, split/join and the bounded stack",
    value: 188,
    note: "+188 LUT",
    tone: "good",
  },
  {
    label: "every SIMT gate on this sweep, G3 − G0",
    value: 252,
    note: "on a substrate of 2,952",
    tone: "accent",
  },
];

const waveCost = {
  cols: [
    { key: "k", label: "Sixteen wave contexts cost" },
    { key: "v", label: "LUT", align: "right", mono: true },
    { key: "why", label: "Why" },
  ],
  rows: [
    {
      k: "in <b>storage</b>",
      v: "+0",
      why: "the register file was already a memory, so a wave id is <b>address bits</b> — 32 registers × 16 waves is 512 entries, exactly one RAMB18E2 in simple-dual-port",
      _tone: "good",
    },
    {
      k: "with <b>mask + IPDOM</b>",
      v: "+124",
      why: "those two are per-wave <i>arrays</i>, and arrays do scale",
    },
    {
      k: "<b>scheduled</b>",
      v: "+1,775",
      why: "the front end — measured as <code>WAVES</code> on <code>kht_core</code> minus the same sweep on <code>kht_unit</code>",
      _tone: "warn",
    },
  ],
};

const laneScale = [
  {
    label: "LANES = 4",
    value: 1659,
    note: "415 LUT/lane · 36 ctrl sets · 324.1 MHz",
  },
  {
    label: "LANES = 8",
    value: 3204,
    note: "401 LUT/lane · 36 ctrl sets · 324.1 MHz",
    tone: "accent",
  },
  {
    label: "LANES = 16",
    value: 6355,
    note: "397 LUT/lane · 36 ctrl sets · 324.0 MHz",
  },
  {
    label: "LANES = 32",
    value: 12478,
    note: "390 LUT/lane · 36 ctrl sets · 324.1 MHz",
  },
];

const vregPrim = {
  cols: [
    { key: "p", label: "VREG_PRIM", mono: true },
    { key: "lut", label: "LUT", align: "right", mono: true },
    { key: "lram", label: "of which LUTRAM", align: "right", mono: true },
    { key: "ff", label: "FF", align: "right", mono: true },
    { key: "bram", label: "BRAM", align: "right", mono: true },
    { key: "cs", label: "ctrl sets", align: "right", mono: true },
    { key: "f", label: "Fmax", align: "right", mono: true },
  ],
  rows: [
    {
      p: "block <span class='opacity-60'>(default)</span>",
      lut: "3,204",
      lram: "20",
      ff: "516",
      bram: "8",
      cs: "36",
      f: "324.1 MHz",
    },
    {
      p: "distributed",
      lut: "9,430",
      lram: "5,140",
      ff: "1,028",
      bram: "<b>0</b>",
      cs: "165",
      f: "475.3 MHz",
    },
    {
      p: "<b>Δ</b>",
      lut: "<b>+6,226</b>",
      lram: "+5,120",
      ff: "+512",
      bram: "−8",
      cs: "+129",
      f: "<b>+151.2 MHz</b>",
      _tone: "bad",
    },
  ],
};

const crossLane = {
  cols: [
    { key: "l", label: "LANES", align: "right", mono: true },
    {
      key: "bfly",
      label: "G8 butterfly · N log N",
      align: "right",
      mono: true,
    },
    { key: "res", label: "G4 resolver · N²", align: "right", mono: true },
    { key: "r", label: "ratio", align: "right", mono: true },
    { key: "f4", label: "G4 Fmax", align: "right", mono: true },
  ],
  rows: [
    { l: "4", bfly: "+268", res: "509", r: "1.9×", f4: "643.1 MHz" },
    { l: "8", bfly: "+935", res: "1,633", r: "1.7×", f4: "514.9 MHz" },
    {
      l: "16",
      bfly: "+3,019",
      res: "6,194",
      r: "2.1×",
      f4: "<b>339.2 MHz</b>",
      _tone: "warn",
    },
    {
      l: "32",
      bfly: "+6,000",
      res: "25,961",
      r: "<b>4.3×</b>",
      f4: "<b>317.7 MHz</b>",
      _tone: "bad",
    },
  ],
};

// ---------------------------------------------------------------------------
// Budget — every row measured, and every row on the same part.
// ---------------------------------------------------------------------------
const budget = [
  {
    label: "controller PE — rv_pe, SIMD_EN = 0",
    value: 2477,
    note: "measured · 2.857 ns",
  },
  {
    label: "kht_unit at G3 — the SIMT unit alone, integer lanes",
    value: 3204,
    note: "measured · 3.333 ns",
  },
  {
    label: "kht_core — the pipeline, kht_unit inside it",
    value: 9653,
    note: "measured · 3.333 ns",
  },
  {
    label: "SIMD PE — SIMD 8 + 4 float lanes",
    value: 13772,
    note: "measured · 2.857 ns",
  },
  {
    label: "SIMT PE, integer only — the campaign's baseline",
    value: 15794,
    note: "measured · 2.500 ns",
    tone: "warn",
  },
  {
    label: "SIMT PE, 8 int + 8 float lanes, RV32M — OF RECORD",
    value: 21586,
    note: "measured · 2.857 ns",
    tone: "accent",
  },
  {
    label: "target band, upper edge",
    value: 25000,
    note: "TARGET · the band is 20–25k",
    tone: "good",
  },
  { label: "review line", value: 30000, note: "TARGET", tone: "warn" },
  { label: "ceiling", value: 35000, note: "TARGET", tone: "bad" },
];

const record = {
  cols: [
    {
      key: "k",
      label:
        "kht_pe · 8 lanes / 16 waves · xcvu13p-fhgb2104-2L-e · OOC synth at 2.857 ns",
    },
    { key: "v", label: "", mono: true, align: "right" },
  ],
  rows: [
    { k: "LUT", v: "<b>21,586</b>", _tone: "good" },
    { k: "FF", v: "17,268" },
    { k: "BRAM", v: "30.5" },
    {
      k: "DSP48",
      v: "<b>48</b> <span class='opacity-60'>— 2/lane float, 4/lane multiply</span>",
    },
    { k: "control sets", v: "201" },
    { k: "Fmax", v: "<b>365.6 MHz</b>", _tone: "good" },
    { k: "slack", v: "<b>+0.122 ns</b>", _tone: "good" },
  ],
};

// ---------------------------------------------------------------------------
// Status
// ---------------------------------------------------------------------------
const shaders = {
  cols: [
    { key: "s", label: "Shader", mono: true },
    { key: "ex", label: "Exercises" },
    { key: "r", label: "Result", align: "right" },
  ],
  rows: [
    {
      s: "gpu_smoke.s",
      ex: "kick argument, <code>vlaneid</code>, RV32I per lane, lane-linear store",
      r: "<b>PASS</b>",
      _tone: "good",
    },
    {
      s: "gpu_diverge.s",
      ex: "<code>split</code>/<code>join</code>: odd and even lanes take different paths and reconverge",
      r: "<b>PASS</b>",
      _tone: "good",
    },
    {
      s: "gpu_nested.s",
      ex: "a split inside a split — two stack pairs, the phase bit toggling at both levels",
      r: "<b>PASS</b>",
      _tone: "good",
    },
    {
      s: "gpu_gather.s",
      ex: "a <b>real gather</b>: per-lane base <code>lw</code> across five 32-byte lines, six fills",
      r: "<b>PASS</b>",
      _tone: "good",
    },
    {
      s: "gpu_isa.s",
      ex: "execution coverage: 29 instruction results, every scalar ALU form, every subgroup path, reductions under a non-trivial mask and over negative data",
      r: "<b>PASS</b>",
      _tone: "good",
    },
    {
      s: "gpu_lds.s",
      ex: "<b>G4</b>: the banked LDS at both ends of its range — conflict-free, reversed, and every lane on one bank. Run with the gate <b>off and on</b>",
      r: "<b>PASS</b>",
      _tone: "good",
    },
    {
      s: "gpu_shfl.s",
      ex: "<b>G8</b>: the butterfly — every stage in turn, full reversal, <code>bcast</code>, and a lane whose source is masked off",
      r: "<b>PASS</b>",
      _tone: "good",
    },
    {
      s: "gpu_waves.s",
      ex: "<b>G7</b>: a real dispatch — every wave writes its own slice, 1 to 16 waves",
      r: "<b>PASS</b>",
      _tone: "good",
    },
    {
      s: "gpu_chain.s",
      ex: "<b>G7's witness</b>: a 20-deep dependency chain, where interleaving actually pays",
      r: "<b>PASS</b>",
      _tone: "good",
    },
    {
      s: "gpu_fault.s",
      ex: "the <b>region fault</b>: a per-lane access to an unmapped region halts with cause 3 at the faulting PC",
      r: "<b>PASS</b>",
      _tone: "good",
    },
    {
      s: "gpu_float.s",
      ex: "<b>G9</b>: the float tier on narrow operands, <code>vfma</code> chain included, at 1 wave <b>and</b> at 16 — one wave is the <i>worst</i> case, because with nothing else runnable the 15-cycle latency is exposed rather than hidden",
      r: "<b>PASS</b>",
      _tone: "good",
    },
    {
      s: "gpu_f32.s",
      ex: "the two <b>wide-operand format witnesses</b>, at 1 wave and at 16",
      r: "<b>PASS</b>",
      _tone: "good",
    },
    {
      s: "gpu_mul.s",
      ex: "<b>RV32M</b>: the sign corners — <code>mulh</code>, <code>mulhu</code> and <code>mulhsu</code> are three different high halves of the same two bit patterns",
      r: "<b>PASS</b>",
      _tone: "good",
    },
  ],
};

const runOut = {
  cols: [
    {
      key: "k",
      label:
        "One shader through SIMT PE + router + MAG + RAM, 8 lanes × 16 waves",
    },
    { key: "v", label: "", mono: true, align: "right" },
  ],
  rows: [
    {
      k: "halt word",
      v: "00000055 <span class='opacity-60'>(model 00000055)</span>",
      _tone: "good",
    },
    {
      k: "halt cause",
      v: "1 <span class='opacity-60'>(model 1)</span>",
      _tone: "good",
    },
    { k: "kick to done", v: "475 cycles" },
    { k: "memory", v: "8 request(s) over 1 gather(s)", _tone: "warn" },
  ],
};

const ldsWitness = {
  cols: [
    { key: "g", label: "HAS_LDSBANK", mono: true, align: "right" },
    { key: "r", label: "requests", align: "right", mono: true },
    { key: "ga", label: "gathers", align: "right", mono: true },
    { key: "res", label: "result", align: "right" },
  ],
  rows: [
    {
      g: "0 <span class='opacity-60'>serial walk</span>",
      r: "<b>48</b>",
      ga: "6",
      res: "PASS",
    },
    {
      g: "1 <span class='opacity-60'>banked</span>",
      r: "<b>34</b>",
      ga: "6",
      res: "PASS",
      _tone: "good",
    },
  ],
};
</script>

<template>
  <DocPage
    title="The SIMT PE"
    summary="One instruction stream, many threads — each with its own register file, its own address, and its own path through a branch. What that capability costs, measured on one fabric against a lane array that is otherwise identical."
    domain="simt"
    status="measured"
    source="src/kohakumpe/simt/ · docs/projects/kohakumpe/simt/"
  >
    <h2 class="doc-h2">One instruction, many threads</h2>
    <p class="doc-p">
      A wave is <b>one instruction stream driving N lanes</b>. There is one
      program counter per wave, one fetch, one control word — and N copies of
      the register file, N ALUs, N addresses. Everything expensive about
      instruction supply is paid once; the thing that is replicated is the
      arithmetic and the state it works on.
    </p>

    <Fig
      caption="The control word is shared, the register file is replicated. LANES is a parameter — 8 is the configuration of record and every assembled-PE figure on this page uses it; 4, 16 and 32 are also measured on the SIMT unit."
      zoom
    >
      <BlockDiagram
        :nodes="stream.nodes"
        :edges="stream.edges"
        :groups="stream.groups"
      />
    </Fig>

    <p class="doc-p">
      So one <code>add</code> is eight adds. There is no vector register and no
      vector length: <code>x3</code> simply <i>is</i> a different value in every
      lane.
    </p>

    <Fig
      caption="add x5, x3, x4 — for every active lane, x5[lane] = x3[lane] + x4[lane]."
    >
      <LaneGrid :lanes="8" :mask="oneInstr.mask" :rows="oneInstr.rows" />
    </Fig>

    <h2 class="doc-h2">The register-class rule</h2>

    <Callout kind="rule" title="The whole design, in two lines">
      <p>
        RV32I opcode space addresses the <b>PER-THREAD</b> (vector) file.<br />
        The scalar file and all control flow live in the custom space.
      </p>
    </Callout>

    <p class="doc-p">
      This keeps shader code in ordinary encodings and preserves the pure-SIMT
      property: <code>x1</code>–<code>x31</code> <i>are</i> the per-lane
      registers. No new register class exists in the base ISA, so no compiler
      fork is needed to allocate one.
    </p>

    <Fig
      caption="AMD GCN's SALU/VALU split with the polarity inverted. On GCN the vector side is the addition; here the base ISA slot is spent on the per-thread file, so the scalar side is what the custom space adds — because a shader is mostly per-thread work, and the per-thread half should be the cheap encoding. The one path back from the threads to the scalar side is the subgroup ops: ballot, the five redux*, and vreadfirst."
      zoom
    >
      <BlockDiagram
        :nodes="halves.nodes"
        :edges="halves.edges"
        :groups="halves.groups"
      />
    </Fig>

    <h2 class="doc-h2">What SIMT costs, and what it bought</h2>
    <p class="doc-p">
      These are the things this PE has that the
      <RouterLink to="/mpe/simd" class="doc-link">SIMD tier</RouterLink> does
      not. The SIMD PE goes wide on work that is <i>uniform</i>; this one exists
      for the case where lanes need to <b>disagree</b>.
    </p>

    <SpecTable
      :cols="follows.cols"
      :rows="follows.rows"
      caption="Costs are out-of-context synthesis at LANES = 8 on xcvu13p-fhgb2104-2L-e at the 3.333 ns ask, synth only. The mask, stack and butterfly rows are kht_unit; the scheduling row is kht_core minus kht_unit. Not placed and routed."
    />

    <h2 class="doc-h2">The arithmetic: 8 integer lanes, 8 float lanes</h2>

    <Callout
      kind="measured"
      title="The configuration of record, and all of it is built"
    >
      <p>
        <b>8 integer lanes and 8 float lanes, with RV32M integer multiply.</b>
        It is stated here because a baseline nobody names gets silently assumed
        to be something else — and an earlier revision of this page said the
        reference was 8 int + 4 float and that neither number was built. Both
        halves of that are false now.
      </p>
    </Callout>

    <SpecTable
      :cols="record.cols"
      :rows="record.rows"
      caption="The whole unit — SIMT core, windows, banked LDS, L1, requestor, fabric port — with the float tier and the multiplier both in it. Source: build/sweep/g-350-pad/run.log. MEASURED, synth only: this project has measured a module lose 0.740 ns between synthesis and routing."
    />

    <h3 class="doc-h3">
      Why the two numbers are equal now, and what still pins the integer one
    </h3>

    <div
      class="font-mono kt-text-caption whitespace-pre text-warm-700 dark:text-warm-300 leading-6 overflow-x-auto my-3"
    >
      {{ granule }}
    </div>

    <p class="doc-p">
      <b>The integer lanes are the address path.</b> A contiguous 32-bit load by
      eight threads is exactly one <code>MEM_RD_REQ</code>, and that is the
      strongest machine-level alignment in the design — narrow the integer side
      and every coalesced load becomes two or more requests, for every kernel,
      permanently. Float has no such constraint: it is pure arithmetic, deeply
      pipelined at II = 1, and sixteen resident wave contexts hide its 15-cycle
      latency rather than stalling on it.
      <b>Eight integer lanes is a constraint; eight float lanes is a choice</b>,
      and what the knob was turned <i>to</i> is set by the mesh:
      <code>8×4 + 4×8 = 64</code> FP FMA per clock is one Mali-G610 shader core,
      and at four float lanes the same mesh is 48 and short.
    </p>

    <Callout
      kind="trap"
      title="FLANES < LANES returns ZERO in the upper lanes, and zero is a plausible float answer"
    >
      <p>
        <code>kht_fpu</code>'s <code>g_nolane</code> assigns
        <code>32'd0</code> to every lane above <code>FLANES</code>, because
        there is no walk sequencer to feed them. A shader run on a reduced build
        gets a <b>silently wrong result rather than a fault</b> — and zero is a
        number a float kernel meets constantly, so nothing downstream trips on
        it either.
      </p>
      <p>
        This is the one place in this PE that breaks its own rule that a build
        which cannot do something faults instead of answering plausibly, and it
        is guarded by <b>convention only</b>: <code>FLANES</code> must equal
        <code>LANES</code>
        in any build that runs a shader, the configuration of record is
        <code>FLANES = 8</code>, and every shader in the suite runs against
        that. Treat a reduced build as an area measurement and never dispatch to
        one.
      </p>
    </Callout>

    <h3 class="doc-h3">
      The float tier takes both operand widths, and that is not a knob
    </h3>

    <Callout
      kind="rule"
      title="Operand width is a property of the INSTRUCTION, not of the build"
    >
      <p>
        The funct7 bit that distinguishes <code>vfma</code> from
        <code>vfma_h</code> drives <code>half</code> in <code>kht_fpu</code>,
        which drives <code>wide(!half)</code> into the lane.
        <code>wide</code> is a <b>port</b> on <code>khs_float_lane</code>, not a
        parameter, and that lane's own header states the contract:
      </p>
      <p class="font-mono kt-text-caption">
        BOTH INPUT FORMATS AND THE ONE COMPUTE FORMAT ARE THE CONTRACT, not
        options: there is no parameter here that removes either edge.
      </p>
      <p>
        So there is no build of this PE that has the float tier and refuses one
        of the two widths. What <i>is</i> a parameter is whether a float tier
        exists at all (<code>HAS_FLT</code>) and how many lanes it has
        (<code>FLANES</code>). Neither selects a format.
      </p>
    </Callout>

    <Fig
      caption="One datapath, two operand edges. The width bit cannot be read at the result: y_e8 emerges 15 cycles after launch, by which time op belongs to whatever the scheduler picked next — so kht_fpu delays the bit through hpipe and the format follows its own result. Selecting from the live op passes at one wave and fails at sixteen."
      zoom
    >
      <BlockDiagram :nodes="floatPath.nodes" :edges="floatPath.edges" />
    </Fig>

    <SpecTable
      :cols="conversions.cols"
      :rows="conversions.rows"
      caption="The three conversions are not symmetric, and that asymmetry is why the wide form is the DEFAULT encoding and the narrow one carries the _h suffix: the format that can only lose precision is the safer default, and the one that can lose magnitude is the one a shader asks for on purpose."
    />

    <Callout
      kind="note"
      title="Where the arithmetic comes from, and why it is not forked"
    >
      <p>
        Every float lane is one <code>khs_float_lane</code> — the
        <RouterLink to="/mpe/simd" class="doc-link">SIMD tier</RouterLink>'s,
        verbatim. <code>kht_fpu</code> selects operands, drives the width bit
        and converts the result back; <b>it does not compute</b>. That is what
        makes a GPU float number comparable to a DSP float number, and what
        keeps <code>cost(SIMT) = G8 − G0</code> meaning anything at all. The
        SIMD tier instantiates the same lane with <code>.wide(1'b0)</code> and
        never raises it, because its float unit is an <i>accumulator</i> and the
        operand width changes FSLOTS, the partial count and the fold order —
        architecture, not a port widening.
      </p>
    </Callout>

    <h3 class="doc-h3">Rendering is genuinely mixed</h3>
    <p class="doc-p">
      Float is <b>not optional</b> for this PE — rendering needs it. But the
      integer half is not a leftover from a compute machine either:
      rasterisation and depth are integer <b>because float gets them wrong</b>.
      Those are exactness requirements, not performance ones.
    </p>

    <SpecTable
      :cols="renderMix.cols"
      :rows="renderMix.rows"
      caption="Two of the integer rows are why RV32M is built: a pixel index is y*width+x and a mip or Morton address is a multiply, and until the multiplier landed each was a software shift-add chain running on every lane of every fragment. Source: docs/projects/kohakumpe/simt/README.md"
    />

    <h2 class="doc-h2">The instruction set</h2>
    <p class="doc-p">
      Two custom majors, six groups. <code>custom-2</code> (0x5B) carries the
      R-type groups, where <code>funct3</code> names the group and
      <code>funct7</code> the operation; <code>custom-3</code> (0x7B) carries
      the I-type ones, where <code>funct3</code> names one instruction and the
      immediate is 12 bits.
    </p>

    <Fig
      caption="custom-2, R-type. funct3 names the group; funct7 names the operation inside it — eight groups of up to 128."
    >
      <BitField :fields="rtype" />
    </Fig>

    <Fig
      caption="custom-3, I-type. There is no funct7, so an I-type group holds exactly ONE instruction — the SIMD tier hit this wall and spent a whole funct3 on vld and another on vst. Splitting R from I across two majors buys eight of each instead of eight in total."
    >
      <BitField :fields="itype" />
    </Fig>

    <SpecTable
      :cols="groups.cols"
      :rows="groups.rows"
      caption="106 instructions in the custom space: 98 on custom-2 and 8 on custom-3. RV32M is listed for completeness and is NOT in that count — it sits at its standard RISC-V encoding inside the register-register group that already existed, so no new opcode major was spent on it. The field table tests/pe/tools/rv_simt_isa.py is the authority; kht_isa.vh, the assembler, the disassembler and the golden model are generated from or checked against it."
    />

    <h3 class="doc-h3">The float group</h3>

    <Fig
      caption="The FLT funct7. One bit picks the operand width and two pick the operation, so the datapath slices the field instead of comparing against one constant per encoding — the same trick the vmem group uses."
    >
      <BitField :fields="fltF7" />
    </Fig>

    <SpecTable
      :cols="fltOps.cols"
      :rows="fltOps.rows"
      caption="One datapath serves all four: there is never a second adder or a second multiplier. Latency is 15 cycles at II = 1 — vec_alu's own depth — and RV32M is padded to exactly that, so both retire through ONE write port with no arbitration."
    />

    <h3 class="doc-h3">Divergence</h3>

    <SpecTable :cols="divOps.cols" :rows="divOps.rows" />

    <Callout
      kind="rule"
      title="A depth of D permits D/2 nested levels, not D−1"
    >
      <p>
        A <code>split</code> pushes <b>two</b> entries and a
        <code>join</code> pops one. The resume PC is always the popping join's
        own <code>pc+4</code>, so a stack entry is <code>LANES</code> bits and
        carries no PC — exact for structured control flow, which SPIR-V
        guarantees by naming a merge block for every selection and loop. An
        earlier plan said D−1 and was wrong; the statement now appears in the
        table, in the generated header, in the model and in the RTL.
      </p>
      <p>
        <b>Overflow is a fault</b> — not a wrap, not a mask merge, not a
        truncation. A masked-off lane that silently reactivates is a wrong
        answer with no witness. Underflow, a <code>join</code> on an empty
        stack, is the same fault.
      </p>
    </Callout>

    <Callout
      kind="trap"
      title="bar encodes, does not execute, and does not fault either"
    >
      <p>
        <code>kht_predec</code> sets <code>C_BAR</code>, and nothing in
        <code>kht_core</code> or in the golden model reads it — so a workgroup
        barrier <b>retires as a no-op</b>. Every other unbuilt thing in this ISA
        raises a fault, which makes this the single exception to the rule and
        the one that can produce a wrong answer with no witness. With one wave
        per workgroup the no-op happens to be correct; with more than one it is
        a race.
        <b>Do not write a shader that relies on it.</b>
      </p>
    </Callout>

    <h3 class="doc-h3">What has no encoding, deliberately</h3>
    <p class="doc-p">
      Each of these is an absence with a reason, not an omission.
    </p>

    <Callout
      kind="rule"
      title="RV32I conditional branches are reserved and illegal in shader code"
    >
      <p>
        A branch reading a masked per-thread condition into a single PC is
        undefined.
        <i>“Legal only when the compiler proved it uniform”</i> is an
        unfalsifiable contract across four consumers, so
        <b>the encoding refuses instead</b> and the hardware raises an
        illegal-instruction fault. Uniform control uses
        <code>sbeqz</code>/<code>sbnez</code>; divergent control uses
        <code>split</code>/<code>join</code>. <code>jal</code> and
        <code>jalr</code>
        stay ordinary RV32I and are wave-wide: a call is uniform by
        construction.
      </p>
    </Callout>

    <Callout
      kind="note"
      title="No divide, no atomics, no gather opcode, no tex"
    >
      <p>
        <b>Divide and remainder</b> stay illegal — <code>funct3</code> 100–111
        in the RV32M group fault. A long sequential unit does not suit a
        barrel-scheduled pipeline whose whole invariant is a fixed latency, and
        divide-by-a-constant strength-reduces to <code>mulhu</code>, which now
        exists.
      </p>
      <p>
        <b>Atomics</b> are not in the legal opcode set at all, so an
        <code>amo*</code> word raises an illegal-instruction fault rather than
        being decoded into something adjacent.
      </p>
      <p>
        An ordinary RV32I <code>lw</code> whose base register differs per lane
        <b>is</b> a gather; the coalescer sits under the ordinary load path. The
        <code>vmem</code> group is not a gather — it is the
        <i>uniform-base</i> case, handed to the coalescer instead of
        rediscovered by it. And there is no <code>tex</code> because address
        math is integer, the fetch is an ordinary load and filtering is FMAs.
      </p>
    </Callout>

    <h3 class="doc-h3">Getting a value to the scalar side</h3>
    <SpecTable :cols="toScalar.cols" :rows="toScalar.rows" />

    <Callout kind="rule" title="An all-zero active mask is not a defined case">
      <p>
        The scheduler must never issue a wave whose mask is zero.
        <code>kht_unit</code> <b>asserts</b> on it rather than reasoning that it
        cannot happen — and that assertion is what named a real bug in one line.
        See
        <RouterLink to="/mpe/simt/microarchitecture" class="doc-link"
          >Trap 6</RouterLink
        >.
      </p>
    </Callout>

    <h2 class="doc-h2">Addressing: three tiers</h2>
    <SpecTable :cols="tiers.cols" :rows="tiers.rows" />

    <Fig
      caption="The vmem funct7, packed rather than enumerated: funct7 = op&lt;&lt;4 | scale&lt;&lt;2 | width. The datapath SLICES it instead of comparing against one constant per encoding, and op &gt;= 3 is exactly the lane-linear predicate — one comparator."
    >
      <BitField :fields="vmemF7" />
    </Fig>

    <SpecTable
      :cols="vmemOps.cols"
      :rows="vmemOps.rows"
      caption="Widths are b / h / w; scales are 0–3. vlu and vlinu at word width are not encoded — a full word has nothing to extend."
    />

    <Callout
      kind="note"
      title="A uniform base does not imply contiguous offsets"
    >
      <p>
        <code>s[ss1] + (v[vs2] &lt;&lt; scale)</code> with arbitrary per-lane
        offsets is still a scatter and still needs the full leader/follower
        pass. What the uniform-base form buys is narrower and real: the
        coalescer compares <b>offset fields</b> rather than full computed
        addresses, and it knows the high bits cannot differ. The compare is
        narrower, <b>never skipped</b> — including when <code>ss1</code> is
        <code>s0</code>, which is legal and degenerates to a pure vector
        address.
      </p>
      <p>
        Two semantics pinned so the RTL and the model cannot drift: offsets are
        <b>signed</b>, and <code>s + (v &lt;&lt; scale)</code>
        <b>wraps at 32 bits</b>, defined rather than undefined.
      </p>
    </Callout>

    <h2 class="doc-h2">Where a value lives</h2>
    <p class="doc-p">
      Every lane produces its own effective address, and the region it lands in
      is decided by a decode on the address itself — there is no tag lookup on
      the way to a window, because the window <i>is</i> the home of those
      addresses.
    </p>

    <Fig
      caption="The region decode. R_BAD issues nothing and faults — which is exactly what made one of the LSU traps so quiet: a garbage address landed outside every region, issued no request, and the shader still reported the right halt word and the right cause. The fault is registered and sticky until retire, which also makes it stricter than the combinational form it replaced."
      zoom
    >
      <BlockDiagram :nodes="regions.nodes" :edges="regions.edges" />
    </Fig>

    <SpecTable
      :cols="residency.cols"
      :rows="residency.rows"
      caption="The external/internal L1 split is the base controller PE's and is inherited unchanged: split by WHO WRITES, not by what is stored, which is what removes coherence from the design."
    />

    <h2 class="doc-h2">The measurement ladder</h2>
    <p class="doc-p">
      The SIMT PE exists to answer one question:
      <b>what does SIMT cost on this fabric?</b> A number produced by building a
      SIMT core and comparing it to somebody else's SIMD core answers nothing,
      because the two differ in the lane count, the memory system, the ALU, the
      process and the tool version at the same time. So the design is arranged
      so the answer is an identity.
    </p>

    <Callout kind="rule" title="cost(SIMT) = G8 − G0">
      <p>
        On one fabric, one lane lineage, one memory system. G0 and G8 are the
        same module with different generics; everything between them is a
        <b>controlled difference</b> — one gate turns one parameter, and its
        delta is attributable to that parameter and nothing else.
      </p>
      <p>
        <b>The ladder is parameters, not branches.</b> A gate that is not built
        must elaborate <i>none</i> of its logic. If
        <code>HAS_IPDOM = 0</code> left a stack in the netlist with its enables
        tied low, the tool would trim some of it, keep some of it, and the G3
        delta would be the cost of the parts it happened to keep. That is what
        makes <i>“the total is the sum of the measured deltas”</i>
        true rather than intended — and it is why there is one synthesis script
        for the whole ladder.
      </p>
      <p>
        <b>It leaks if you are not watching.</b> G8's first measurement had
        <code>HAS_SHFL = 0</code> at 3,232 LUT against 3,204: the network was
        correctly inside a generate, but its <i>writeback mux input</i> was not,
        so 28 LUT of a switched-off gate survived. The fix is to make the select
        constant-false at elaboration. Worth 28 LUT? No. Worth the property? Yes
        — a rule that holds “mostly” is not one you can add up.
      </p>
    </Callout>

    <SpecTable
      :cols="gates.cols"
      :rows="gates.rows"
      caption="G9 is a gate on kht_pe rather than on kht_unit, and it is the one gate with no row in the ladder table below: the arithmetic it adds is inherited from the SIMD tier, so measuring it on kht_unit would price a lane array this project deliberately does not own. That is the same reason cost(SIMT) = G8 − G0 stops at G8."
    />

    <h3 class="doc-h3">What has been measured</h3>

    <SpecTable
      :cols="ladder.cols"
      :rows="ladder.rows"
      caption="Top kht_unit, LANES = 8, VREG_PRIM = block, HAS_SHFL = 0, xcvu13p-fhgb2104-2L-e, out-of-context synthesis at 3.333 ns, synth only. Source: build/sweep_gpu-ladder.md via `python scripts/py/ooc_sweep.py gpu-ladder`, measured 2026-08-22."
    />

    <ResourceBars
      :items="deltas"
      unit="Δ LUT"
      :max="260"
      caption="G0 through G3 cost 252 LUT in total, on eight lanes, against a substrate of 2,952. MEASURED: OOC synth, kht_unit, xcvu13p-fhgb2104-2L-e, 3.333 ns, 2026-08-22"
    />

    <SpecTable
      :cols="waveCost.cols"
      :rows="waveCost.rows"
      caption="“Sixteen waves are free” is true only of the first line, which is exactly why the other two are measured separately. G1 measured WAVES with the mask and stack OFF; the middle row is the same sweep with them on. Sources: build/sweep_gpu-ladder.md, build/sweep_gpu-waves.md, build/sweep_gpu-sched.md."
    />

    <Callout
      kind="measured"
      title="G1 is free, and it is the most load-bearing measurement here"
    >
      <p>
        <b
          >Sixteen wave contexts cost +0 LUT, +0 BRAM, +0 control sets and +4
          FF.</b
        >
        Not “almost nothing” — nothing, to within four flops of the wave-id
        register. A RAMB18E2 in simple-dual-port is 512 × 36, so 32 registers ×
        16 waves fills one exactly at 32 of 36 useful bits: up to sixteen waves
        the marginal BRAM cost of a wave is <b>zero</b>, and the count buys
        latency tolerance rather than storage.
      </p>
      <p>
        That matters because “many resident contexts to hide memory latency” is
        the part of SIMT that sounds expensive and is the whole reason the model
        tolerates a cache miss.
      </p>
    </Callout>

    <Callout kind="measured" title="Fmax never moves across G0–G3">
      <p>
        Every gate lands at <b>324.1 MHz</b>, identically. The SIMT machinery —
        the mask, the stack, the wave indexing — is not on the binding path at
        eight lanes; the binding path is in the lane array, where it was at G0.
        The usual objection to SIMT is that divergence tracking is on the
        critical path. At this width, on this fabric, it measurably is not.
      </p>
    </Callout>

    <Callout kind="trap" title="G3 was over its bracket, and why it is not now">
      <p>
        G3 was first built with the IPDOM stack as an <b>indexed flop array</b>:
        <code>WAVES × IPDOM_D</code> entries behind a wide read mux and an
        equally wide write decoder. It came in far over its
        <code>&lt;+1k</code> bracket — a shape this project has been billed for
        twice before, at 29,409 LUT and at 701, both fixed the same way. Rebuilt
        as a <code>kohaku_sdpram</code> in <code>distributed</code> mode with
        <code>READ_LAT 0</code>, the whole gate is <b>+188 LUT</b>, and the
        sweep shows where it went: G3 is the only row with
        <code>lut_mem</code> non-zero, at <b>20 LUT</b> of distributed RAM — the
        stack itself. The rest is pointer, phase and fault logic, visible as
        control sets going 18 → 36.
      </p>
    </Callout>

    <h3 class="doc-h3">Lane scaling</h3>
    <p class="doc-p">
      Not a ladder gate — the lane count is the design's biggest free variable,
      so it is measured rather than extrapolated from the 8-lane row.
    </p>

    <ResourceBars
      :items="laneScale"
      unit="LUT"
      caption="kht_unit, full gate set, 16 waves, VREG_PRIM = block, HAS_SHFL = 0. LUT = 112 + 386.4 × LANES (exact at 4, 8 and 32; +61 at 16). FF = 116 + 50 × LANES, exact at all four points. BRAM = 1 × LANES. MEASURED: build/sweep_gpu-lanes.md, OOC synth at 3.333 ns"
    />

    <p class="doc-p">
      Three readings. <b>Control sets do not move</b> — 36 at four lanes, 36 at
      thirty-two: the active mask is a write enable per bank and the divergence
      state is per wave, not per lane, so widening the array adds datapath and
      adds no control. <b>The intercept is ~112 LUT</b>: everything that is not
      a lane is about a hundred LUT, so at any useful width this unit
      <i>is</i> its lane array. And <b>Fmax does not move either</b> — but that
      one came with an expiry date, and it has been collected on.
    </p>

    <h3 class="doc-h3">
      The two cross-lane networks, and why complexity class is the argument
    </h3>

    <SpecTable
      :cols="crossLane.cols"
      :rows="crossLane.rows"
      caption="G8's butterfly is log2(LANES) conditional swaps — O(N log N). G4's conflict resolver picks the lowest outstanding lane for each of LANES banks — O(N²), and its own Fmax column is what happens as a result. Sources: build/sweep_gpu-shfl.md and build/sweep_gpu-lds.md, OOC synth at 3.333 ns."
    />

    <Callout
      kind="measured"
      title="The flat 324 MHz did not survive a cross-lane network, exactly as the caveat said"
    >
      <p>
        Lane scaling is flat at 324 MHz from 4 lanes to 32
        <b>because there is no cross-lane network in <code>kht_unit</code></b> —
        lanes are independent, so a wider array is wider and not deeper. G4 is
        the first cross-lane network, and at 32 lanes it runs at
        <b>317.7 MHz, below the rest of the unit</b>: the resolver becomes the
        binding path. G8 is the second, costing 20–39 MHz at every width but
        <i>not</i> getting worse with width, because its depth grows as log
        while everything it competes with grows faster.
      </p>
      <p>
        <b>32 lanes is out.</b> <code>kht_unit</code> and
        <code>kht_lds</code> alone are 38,439 LUT there, past the 35k ceiling
        before the core, the L1, the requestor or the port is counted — and they
        do it while missing the clock. If 16 lanes is ever wanted the answer is
        a <b>cheaper resolver</b>, not a bigger budget.
      </p>
    </Callout>

    <h3 class="doc-h3">The register-file primitive</h3>
    <p class="doc-p">
      Not a ladder gate either — a configuration choice, measured because it is
      the largest single lever in the unit.
    </p>

    <SpecTable
      :cols="vregPrim.cols"
      :rows="vregPrim.rows"
      caption="8 lanes, 16 waves, full gate set. Source: build/sweep_gpu-vregprim.md. +6,226 LUT is more than twice the entire G0 substrate, spent to trade 8 BRAM for megahertz that are not needed — the clock is already met at 324.1. The vp-block row is bit-identical to g3-ipdom in the ladder table, which is the cross-check that the two sweeps measured the same design."
    />

    <h2 class="doc-h2">Budget</h2>

    <ResourceBars
      :items="budget"
      unit="LUT on xcvu13p-fhgb2104-2L-e"
      :max="35000"
      caption="Every non-TARGET row is a measurement, and each names its ask because the ask moved three times during this work (3.333 → 2.500 → 2.857 ns). The bars cannot show one thing that matters: only the last two GPU rows are a PE, and the rows above them must not be added up — kht_core already contains a kht_unit"
    />

    <Callout
      kind="rule"
      title="Three reporting rules, all of which were learned the expensive way"
    >
      <p>
        <b>Name the top.</b> A ladder whose top is one submodule
        <i>cannot see a path that leaves it</i>. Synthesising
        <code>kht_core</code> for the first time found it at
        <b>71.7 MHz</b> while the <code>kht_unit</code> inside it closed at 324
        — the cross-lane reduction was a serial chain, 44 logic levels, and it
        lives in <code>kht_core</code> where no ladder row ever looked.
        Synthesising the assembled <code>kht_pe</code> for the first time found
        <b>182 MHz</b>. A <code>kht_unit</code> figure is not a frequency claim
        for this PE, including the reassuring ones.
      </p>
      <p>
        <b>Name which G0.</b> Today's G0 is <code>G0(int)</code> — an
        integer-only lane array. An earlier revision claimed G0 was measured
        against a float lane tier; it was not, and that sentence is what let an
        integer-only figure be read against a float-capable budget.
        <b>A G8 total quoted without naming its lane array is not a result.</b>
      </p>
      <p>
        <b>Synth is not route.</b> Every figure on this page is out-of-context
        synthesis. This project has measured a module lose 0.740 ns between
        synthesis and routing; a small negative slack at synth is not something
        placement absorbs. These numbers size the design and rank the gates.
        They do not close timing.
      </p>
    </Callout>

    <h2 class="doc-h2">Status</h2>
    <p class="doc-p">
      The SIMT PE <b>runs shaders end to end on the machine bench</b> — through
      the real L1, the real memory agent and an AXI RAM — and the resulting DRAM
      matches the golden model exactly. <b>Divergence works on hardware</b>, not
      only in the model.
    </p>

    <SpecTable
      :cols="shaders.cols"
      :rows="shaders.rows"
      caption="Four checks each. The whole set runs from one command — python tests/pe/tools/rv_simt_suite.py --gates — as 19 cases, and it is SERIAL by construction: xsim names its build directory after the bench, so two concurrent runs destroy each other's work area and surface as a random shader failing rather than as a collision."
    />

    <SpecTable
      :cols="runOut.cols"
      :rows="runOut.rows"
      caption="PASS — 4 checks, 0 errors. The ladder measures what the IPDOM stack costs; only gpu_nested.s proves the split pushed a pair, the first join took the false half, the second took the outer mask, and the pointer came back to zero."
    />

    <Callout
      kind="measured"
      title="8 requests over 1 gather is the witness reading its pre-coalescer value"
    >
      <p>
        The LSU serialises lanes today, so the ratio is <code>LANES</code> by
        construction. It is counted <b>now</b>, before a coalescer exists, so
        the improvement will be a measured change rather than a number that
        appears from nothing — a witness that only appears once the optimisation
        lands cannot show the optimisation working. <code>gpu_gather.s</code> is
        the case it will be judged on: lane <i>i</i> reads word 5<i>i</i>, so
        eight lanes fall on <b>five</b> distinct 32-byte lines and
        <code>16 request(s) over 2 gather(s)</code> should become
        <code>6 over 2</code>.
      </p>
    </Callout>

    <SpecTable
      :cols="ldsWitness.cols"
      :rows="ldsWitness.rows"
      caption="G4's witness is the same shader, the same correct answer, one parameter changed: 34 is 1 + 1 + 8 + 8 + 8 + 8 exactly, and 48 is 8 × 6. A controlled difference rather than an argument about what a resolver ought to do — and the reversed case is the one that proves the return crossbar, because lane 0 must take bank 7's word."
    />

    <h3 class="doc-h3">Not built yet</h3>
    <p class="doc-p">
      <b>G5</b>, the coalescer, and <b>G6</b>, MSHRs. G7 interleaves
      <i>issue</i>, but one instruction is in flight at a time and a miss holds
      the whole front end, so until G6 lets a stalled wave step aside, more
      waves buy hazard-free issue and nothing else. Also outstanding: the
      lane/interval walk sequencer that <code>FLANES &lt; LANES</code> and
      <code>ILANES ≤ LANES</code> both need, which is ruled to the DSP realm and
      instantiated here rather than forked.
    </p>

    <Callout kind="open" title="G5 needs a memory path that does not exist yet">
      <p>
        The model, this ISA and the bench witness all define coalescing at
        <b>32-byte line</b> granularity — but <code>rv_l1</code>'s CPU-side read
        port is <b>32 bits</b>, and that is a deliberate decision in a shared
        component. Against a 32-bit port a line-granular coalescer buys
        <b>nothing</b>: eight lanes on five lines still need eight word reads,
        because the cache already coalesces the <i>fills</i> by itself.
        Delivering the promised 8 → 5 needs a wide read path contained to this
        PE plus a per-lane word crossbar — and that crossbar <b>is</b> G5's
        headline number, which is precisely why it must stay out of the shared
        L1.
      </p>
    </Callout>

    <Callout kind="note" title="Where to go next">
      <p>
        If you are changing the RTL, read
        <RouterLink to="/mpe/simt/microarchitecture" class="doc-link"
          >the microarchitecture page</RouterLink
        >
        first — several of its shapes exist to avoid a specific failure that has
        already happened once. If you want to know what these numbers are
        <i>worth</i>, see
        <RouterLink to="/mpe/simt/comparison" class="doc-link"
          >where this lands against shipped GPUs</RouterLink
        >.
      </p>
    </Callout>
  </DocPage>
</template>
