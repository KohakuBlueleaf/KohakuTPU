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
// The float tier. IEEE binary32 is the ONLY compute type, so a thread is a whole
// 32-bit slot: no format bit in the encoding, no conversion at either edge, no
// reserved half of a register. The units are the SIMD tier's rv_fpu and
// khs_fp32_sfu, instantiated here and never forked.
// ---------------------------------------------------------------------------
const floatPath = {
  nodes: [
    {
      id: "src",
      x: 0,
      y: 2.2,
      w: 11,
      label: "per-thread regs",
      sub: "one binary32 element per LANE",
    },
    {
      id: "sel",
      x: 14,
      y: 2.2,
      w: 12,
      label: "operand select",
      sub: "unit u on pass p serves thread p·U + u",
      accent: true,
    },
    {
      id: "fma",
      x: 29,
      y: 0,
      w: 14,
      h: 4,
      label: "FMA × FLANES",
      sub: "6 deep · II 1 · 2 DSP48 each",
      accent: true,
    },
    {
      id: "sfu",
      x: 29,
      y: 6,
      w: 14,
      h: 4,
      label: "seed × FSFU_UNITS",
      sub: "exp2 log2 rcp rsqrt · 10 deep",
    },
    {
      id: "we",
      x: 46,
      y: 2.2,
      w: 13,
      label: "per-lane write enable",
      sub: "a decode of the retiring pass index",
      accent: true,
    },
    {
      id: "out",
      x: 62,
      y: 2.2,
      w: 11,
      label: "per-thread regs",
      sub: "written in place",
    },
  ],
  edges: [
    { from: "src:r", to: "sel:l", dir: "h" },
    { from: "sel:r", to: "fma:l", dir: "h", accent: true },
    { from: "sel:r", to: "sfu:l", dir: "h" },
    { from: "fma:r", to: "we:l", dir: "h", accent: true },
    { from: "sfu:r", to: "we:l", dir: "h" },
    { from: "we:r", to: "out:l", dir: "h", accent: true },
  ],
};

const granule = `   8 lanes x 32 bit  =  256 bit  =  one native memory entry  =  one flit payload

   integer lanes  <-  the memory granule  (256-bit entry / flit)   8, FIXED
   float units    <-  arithmetic demand   (throughput vs LUT)      A KNOB`;

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
      needs: "fixed-point or float weights",
      kind: "float-ish",
    },
    {
      stage: "fragment / colour shading",
      needs: "mediump at least — binary32 exceeds it in range and mantissa",
      kind: "<b>float</b>",
    },
    {
      stage: "vertex transform",
      needs: "binary32 products into a binary32 accumulator",
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

/* funct7[2] selects the SEED half; funct7[1:0] names the operation within it.
 * There is no operand-width bit and there never was one: binary32 is the only
 * compute type, so a thread is a whole 32-bit slot. Read out of the field table
 * tests/pe/tools/rv_simt_isa.py. */
const fltF7 = [
  { name: "reserved", bits: 4, value: "0" },
  { name: "seed", bits: 1, value: "0 = arithmetic, 1 = seed", accent: true },
  { name: "op", bits: 2, value: "which of the four", accent: true },
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
      g: "<b>FLT</b>",
      enc: "custom-2, funct3 5",
      n: "<b>8</b>",
      what: "<code>vfma</code>, <code>vfmul</code>, <code>vfadd</code>, <code>vfsub</code>, and the four base-2 seeds <code>vfexp2</code>, <code>vflog2</code>, <code>vfrcp</code>, <code>vfrsqrt</code>. <b>One operand width</b> — binary32 — so there are no narrow forms. A build with <code>FLANES = 0</code> faults on all eight; one with <code>FSFU_UNITS = 0</code> faults on the four seeds and keeps the other four",
      _tone: "good",
    },
    {
      g: "—",
      enc: "custom-2, funct3 6–7",
      n: "0",
      what: "<b>unallocated.</b> Reserved, and they fault",
      _tone: "warn",
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
      how: "inverts vs2's <b>sign bit</b>. A unit has no subtract and negating a float is one bit",
    },
    {
      i: "vfexp2 · vflog2<br>vfrcp · vfrsqrt",
      d: "one operand each",
      how: "<b>the seed half</b>, on the <code>FSFU_UNITS</code> of the float units that carry a <code>khs_fp32_sfu</code> beside their multiply-add. Newton refinement is an instruction sequence deliberately: <code>1/a</code> is <code>y' = y(2−ay)</code>, two multiply-adds, and <code>rsqrt</code> is <code>y' = y(1.5−0.5ay²)</code>, three",
      _tone: "warn",
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
      prim: "block RAM<br><span class='opacity-60'>2 banks per lane, 3 where float units are built — vfma's addend needs a third read port</span>",
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
      gen: "FLANES, FSFU_UNITS",
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
/* Every bar is `-flatten_hierarchy none`, from the ladder's own suites, at
 * 3.333 ns, on an INTEGER-ONLY lane array. They are internally comparable and
 * they MUST NOT be added: kht_core contains a kht_unit and kht_pe contains
 * both. `none` is NOT the ship. */
const budget = [
  {
    label: "kht_unit at G3 — the unit alone, shuffle off",
    value: 3204,
    note: "measured · none · integer-only",
  },
  {
    label: "kht_unit at G3 + G8 — the butterfly built",
    value: 4139,
    note: "measured · none · integer-only",
  },
  {
    label: "kht_core — the pipeline, with a kht_unit INSIDE it",
    value: 9653,
    note: "measured · none · integer-only",
  },
  {
    label: "kht_pe — the assembled PE, and the only row that is a PE",
    value: 16115,
    note: "measured · none · integer-only · 182.0 MHz",
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
        "kht_pe · 8 threads / 16 waves / 8 float units / no seed units · xcvu13p-fhgb2104-2L-e · Vivado 2024.2 · OOC synthesis · -flatten_hierarchy rebuilt · 3.333 ns",
    },
    { key: "v", label: "", mono: true, align: "right" },
  ],
  rows: [
    { k: "LUT", v: "<b>19,461</b>", _tone: "good" },
    { k: "FF", v: "17,268" },
    { k: "BRAM", v: "30.5" },
    {
      k: "DSP48",
      v: "<b>48</b> <span class='opacity-60'>— 2 × FLANES + 4 × LANES, exact on every measured row, including the two where LANES itself moves</span>",
    },
    { k: "control sets", v: "202" },
    { k: "requested", v: "<b>3.333 ns</b> — 300.0 MHz" },
    {
      k: "achieved",
      v: "<b>361.0 MHz</b> <span class='opacity-60'>— a synthesis estimate and a screen, not a closed clock</span>",
    },
    {
      k: "the tree",
      v: "one frozen source tree of a khs_sweep campaign, and <b>NOT the RTL as it now stands</b>",
      _tone: "warn",
    },
  ],
};

/* Every row is a DELTA against the reference row above with ONE knob moved, on
 * that same frozen tree, at rebuilt and 3.333 ns. Transcribed from
 * docs/projects/kohakumpe/unit-counts.md, which names the tree per table. */
const simtWidths = {
  cols: [
    { key: "f", label: "knob", mono: true },
    { key: "u", label: "value", mono: true, align: "right" },
    { key: "lut", label: "LUT", mono: true, align: "right" },
    { key: "d", label: "ΔLUT", mono: true, align: "right" },
    { key: "ff", label: "FF", mono: true, align: "right" },
    { key: "bram", label: "BRAM", mono: true, align: "right" },
    { key: "dsp", label: "DSP", mono: true, align: "right" },
    { key: "fx", label: "Fmax", mono: true, align: "right" },
  ],
  rows: [
    {
      f: "—",
      u: "the reference row",
      lut: "<b>19,461</b>",
      d: "—",
      ff: "17,268",
      bram: "30.5",
      dsp: "48",
      fx: "361.0",
      _tone: "good",
    },
    {
      f: "FLANES",
      u: "4",
      lut: "16,307",
      d: "<b>−3,154</b>",
      ff: "13,917",
      bram: "30.5",
      dsp: "40",
      fx: "334.7",
    },
    {
      f: "",
      u: "2",
      lut: "14,100",
      d: "<b>−5,361</b>",
      ff: "12,233",
      bram: "30.5",
      dsp: "36",
      fx: "378.9",
    },
    {
      f: "FSFU_UNITS",
      u: "2",
      lut: "20,841",
      d: "<b>+1,380</b>",
      ff: "17,954",
      bram: "33.5",
      dsp: "50",
      fx: "346.4",
    },
    {
      f: "",
      u: "8 — full rate",
      lut: "22,084",
      d: "<b>+2,623</b>",
      ff: "20,065",
      bram: "42.5",
      dsp: "56",
      fx: "363.9",
      _tone: "warn",
    },
    {
      f: "SHFL_UNITS",
      u: "4",
      lut: "19,488",
      d: "<b>+27</b>",
      ff: "17,264",
      bram: "30.5",
      dsp: "48",
      fx: "377.9",
      _tone: "warn",
    },
    {
      f: "",
      u: "2",
      lut: "19,318",
      d: "−143",
      ff: "17,261",
      bram: "30.5",
      dsp: "48",
      fx: "379.4",
    },
    {
      f: "",
      u: "1",
      lut: "18,944",
      d: "<b>−517</b>",
      ff: "17,270",
      bram: "30.5",
      dsp: "48",
      fx: "343.1",
    },
    {
      f: "the shuffle GATE",
      u: "0 — it <b>faults</b>",
      lut: "18,581",
      d: "−880",
      ff: "17,267",
      bram: "30.5",
      dsp: "48",
      fx: "383.0",
    },
    {
      f: "LDS_BANKS",
      u: "4",
      lut: "18,847",
      d: "<b>−614</b>",
      ff: "17,267",
      bram: "26.5",
      dsp: "48",
      fx: "405.2",
    },
    {
      f: "",
      u: "1",
      lut: "17,899",
      d: "<b>−1,562</b>",
      ff: "17,264",
      bram: "24.5",
      dsp: "48",
      fx: "361.0",
    },
    {
      f: "the LDS GATE",
      u: "0 — no shared memory",
      lut: "17,656",
      d: "−1,805",
      ff: "16,930",
      bram: "24.5",
      dsp: "48",
      fx: "376.4",
    },
    {
      f: "WAVES",
      u: "8",
      lut: "18,802",
      d: "<b>−659</b>",
      ff: "16,756",
      bram: "30.5",
      dsp: "48",
      fx: "380.4",
    },
    {
      f: "",
      u: "4",
      lut: "18,414",
      d: "<b>−1,047</b>",
      ff: "16,497",
      bram: "30.5",
      dsp: "48",
      fx: "352.6",
    },
    {
      f: "IPDOM_D",
      u: "4 — half the stack",
      lut: "19,453",
      d: "<b>−8</b>",
      ff: "17,247",
      bram: "30.5",
      dsp: "48",
      fx: "360.2",
    },
    {
      f: "HAS_MASK + HAS_IPDOM",
      u: "0 — the gate",
      lut: "19,264",
      d: "−197",
      ff: "17,028",
      bram: "30.5",
      dsp: "48",
      fx: "380.5",
    },
    {
      f: "LANES",
      u: "4, with FLANES 4",
      lut: "11,369",
      d: "−8,092",
      ff: "10,712",
      bram: "20.5",
      dsp: "24",
      fx: "383.4",
    },
  ],
};

const gatesCost = {
  cols: [
    { key: "g", label: "gate", mono: true },
    { key: "l", label: "LUT", mono: true, align: "right" },
    { key: "b", label: "BRAM", mono: true, align: "right" },
    { key: "c", label: "ctrl sets", mono: true, align: "right" },
  ],
  rows: [
    {
      g: "HAS_MASK + HAS_IPDOM — the mask array and the divergence stack",
      l: "<b>681</b>",
      b: "0",
      c: "−37",
    },
    {
      g: "HAS_SHFL — the subgroup butterfly",
      l: "<b>1,224</b>",
      b: "0",
      c: "0",
    },
    {
      g: "HAS_LDSBANK — the banked LDS and its address resolver",
      l: "<b>1,948</b>",
      b: "6",
      c: "−16",
    },
    {
      g: "<b>the three together</b>",
      l: "<b>3,853</b>",
      b: "6",
      c: "—",
      _tone: "good",
    },
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
      s: "simt_smoke.s",
      ex: "kick argument, <code>vlaneid</code>, RV32I per lane, lane-linear store",
      r: "<b>PASS</b>",
      _tone: "good",
    },
    {
      s: "simt_diverge.s",
      ex: "<code>split</code>/<code>join</code>: odd and even lanes take different paths and reconverge",
      r: "<b>PASS</b>",
      _tone: "good",
    },
    {
      s: "simt_nested.s",
      ex: "a split inside a split — two stack pairs, the phase bit toggling at both levels",
      r: "<b>PASS</b>",
      _tone: "good",
    },
    {
      s: "simt_gather.s",
      ex: "a <b>real gather</b>: per-lane base <code>lw</code> across five 32-byte lines, six fills",
      r: "<b>PASS</b>",
      _tone: "good",
    },
    {
      s: "simt_isa.s",
      ex: "execution coverage — one result per instruction form, every scalar ALU form, every subgroup path, reductions under a non-trivial mask and over negative data",
      r: "<b>PASS</b>",
      _tone: "good",
    },
    {
      s: "simt_lds.s",
      ex: "<b>G4</b>: the banked shared memory at both ends of its range — conflict-free, reversed, and every lane on one bank. Run at <b>four bank counts including none</b>",
      r: "<b>PASS</b>",
      _tone: "good",
    },
    {
      s: "simt_shfl.s",
      ex: "<b>G8</b>: the butterfly — every stage in turn, full reversal, <code>bcast</code>, a lane whose source is masked off, and <b>four shuffle widths</b>",
      r: "<b>PASS</b>",
      _tone: "good",
    },
    {
      s: "simt_waves.s",
      ex: "<b>G7</b>: a real dispatch — every wave writes its own slice, 1 to 16 waves",
      r: "<b>PASS</b>",
      _tone: "good",
    },
    {
      s: "simt_chain.s",
      ex: "<b>G7's witness</b>: a 20-deep dependency chain, at 1 wave and 2. It stalls every instruction with one wave and none with two — <b>1.90 cycles per instruction against 0.90</b>, a 2.1× on the dependent section against a theoretical 2.0× for removing a one-cycle distance-1 hazard",
      r: "<b>PASS</b>",
      _tone: "good",
    },
    {
      s: "simt_fault.s",
      ex: "the <b>region fault</b>: a per-lane access to an unmapped region halts with cause 3 at the faulting PC",
      r: "<b>PASS</b>",
      _tone: "good",
    },
    {
      s: "simt_valu.s",
      ex: "<b>the per-thread ALU itself.</b> Every other shader reaches only a handful of its operations, so without this the lane datapaths are nine tenths untested",
      r: "<b>PASS</b>",
      _tone: "good",
    },
    {
      s: "simt_f32.s",
      ex: "the format witnesses — an operand only binary32's exponent range holds, and a mantissa bit only its significand keeps",
      r: "<b>PASS</b>",
      _tone: "good",
    },
    {
      s: "simt_fwalk.s",
      ex: "<b>the pass walk.</b> Per-lane <i>distinct</i> float operands, so a build whose units serve the wrong threads is a wrong word rather than a slow pass — every other float shader has uniform operands and would pass a crossed placement. <b>One wave is the WORST case for the float tier</b>, not the easy one: with nothing else runnable the tier's latency is exposed rather than hidden, so a dependent chain that is right at one wave is right at any occupancy",
      r: "<b>PASS</b> at four unit counts, and at 1 wave and 16",
      _tone: "good",
    },
    {
      s: "simt_mul.s",
      ex: "<b>RV32M</b>: the sign corners — <code>mulh</code>, <code>mulhu</code> and <code>mulhsu</code> are three different high halves of the same two bit patterns — and one row with <b>no float tier at all</b>, because <code>mul</code> does not depend on one",
      r: "<b>PASS</b>",
      _tone: "good",
    },
  ],
};

/* The banked shared memory, measured ON HARDWARE by simt_lds.s. */
const runOut = {
  cols: [
    { key: "a", label: "access", mono: true },
    { key: "b", label: "banks touched", align: "right" },
    { key: "p", label: "passes", align: "right", mono: true },
  ],
  rows: [
    {
      a: "lane <i>i</i> → word <i>i</i> — conflict-free",
      b: "8 distinct",
      p: "<b>1</b>",
      _tone: "good",
    },
    {
      a: "lane <i>i</i> → word 7−<i>i</i> — reversed",
      b: "8 distinct",
      p: "<b>1</b>",
      _tone: "good",
    },
    {
      a: "lane <i>i</i> → word 8<i>i</i> — the worst case",
      b: "all one bank",
      p: "<b>8</b>",
      _tone: "warn",
    },
  ],
};

const ldsWitness = {
  cols: [
    { key: "g", label: "the banked path", mono: true },
    { key: "r", label: "requests", align: "right", mono: true },
    { key: "res", label: "the answer", align: "right" },
  ],
  rows: [
    {
      g: "off <span class='opacity-60'>— the serial walk</span>",
      r: "<b>48</b> <span class='opacity-60'>= 8 × 6</span>",
      res: "identical",
    },
    {
      g: "on <span class='opacity-60'>— the resolver</span>",
      r: "<b>34</b> <span class='opacity-60'>= 1 + 1 + 8 + 8 + 8 + 8</span>",
      res: "identical",
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

    <Callout
      kind="trap"
      title="SIMD does not beat SIMT on LUT at matched features"
    >
      <p>
        It is the obvious expectation and the measurement says otherwise. With
        the mask, the divergence stack, the shuffle and the banked shared memory
        all off, at 8 fused multiply-adds and 8 multiply units, this PE measures
        <b>16,118</b>. The comparable SIMD figure — its own reference less the
        packed shifter and the permute — is <b>16,775</b>, which is
        <b>657 LUT, 4.1%, dearer.</b>
      </p>
      <p>
        Both halves of that are worth keeping. <b>SIMD's base PE is 543 LUT
        cheaper</b> than this one — 10,309 against 10,852 — even though it
        carries the shifter, the permute network and thirty-two multipliers and
        this one carries no multiplier inside the lane array at all. So the
        divergence hardware is real and the SIMD tier does not pay for it. And
        <b>SIMD is not a subset of SIMT</b>: it carries packed
        int8/int16/int32 lanes, a cross-lane permute, a vector scratchpad and
        optionally a rotating float accumulator. "SIMD must be much cheaper at
        the same features" is not reachable by removing redundancy; it is a
        decision about which SIMD features to drop.
      </p>
    </Callout>

    <h2 class="doc-h2">The arithmetic: 8 threads, 8 float units</h2>

    <Callout
      kind="rule"
      title="The reference row is one named generic set, not a maximum and not a menu price"
    >
      <p>
        A baseline nobody names gets silently assumed to be something else, so
        the configuration is stated with the number:
        <b>8 threads, 16 waves, 8 binary32 FMA units, no seed units</b>, with
        the mask, the divergence stack, the subgroup butterfly and the banked
        shared memory all built. Every per-feature figure below is a
        <b>delta</b> against that build with one knob moved.
      </p>
      <p>
        <b>The thread ALU width is not configurable.</b> A SIMT processor is its
        threads: <code>LANES</code> threads means <code>LANES</code> integer-and-
        multiply units, and the multiply count follows — it is the one width on
        either core set by definition rather than by measurement.
      </p>
    </Callout>

    <Callout
      kind="trap"
      title="This total describes a configuration the RTL can no longer build"
    >
      <p>
        The float tier was rebuilt from an E8M15 datapath with two operand
        formats into a binary32-only one, and the separate float gate and the
        per-thread multiplier count were removed.
        <b>Re-measurement against the current parameter set has not been
        published</b>, so no absolute total on this page is a figure for a PE
        the RTL can build today.
      </p>
      <p>
        The rows are kept because <b>the shapes are the findings</b> — what a
        marginal unit costs, where a width pays, which knobs are not levers. The
        symptom of ignoring this is a mesh total: one of these figures
        multiplied by a PE count, pricing a machine that cannot be built, wrong
        in an unknown direction rather than merely stale.
      </p>
    </Callout>

    <SpecTable
      :cols="record.cols"
      :rows="record.rows"
      caption="The whole assembled PE — SIMT core, windows, banked shared memory, L1, requestor, fabric port. xcvu13p-fhgb2104-2L-e, Vivado 2024.2, OUT-OF-CONTEXT SYNTHESIS ONLY at a 3.333 ns request, -flatten_hierarchy rebuilt, -directive default. Nothing is placed and nothing is routed: this project has measured a module lose 0.740 ns between synthesis and routing, so the frequency is a screen rather than a result. Transcribed from docs/projects/kohakumpe/unit-counts.md, which names the frozen tree behind each of its tables."
    />

    <SpecTable
      :cols="simtWidths.cols"
      :rows="simtWidths.rows"
      caption="One knob moved at a time, on the same frozen tree as the reference row, at -flatten_hierarchy rebuilt and a 3.333 ns request. Every cell is measured; a knob point that was not synthesised is absent rather than inferred. DSP and BRAM follow the closed forms exactly on every row; LUT does not, which is why the counts are tabulated rather than fitted to a slope. Fmax in MHz, and it is a screen: it moves by tens of megahertz between rows that differ in nothing that should matter, so no decision recorded here was made on it."
    />

    <Callout
      kind="trap"
      title="Four shuffle units of eight COST 27 LUT rather than saving any"
    >
      <p>
        A cross-lane width pays at one or two units and nowhere else, and the
        curve above says so directly: 8 → 4 is <b>+27</b>, 4 → 2 is −170, and
        2 → 1 is another −374. The mechanism is that a narrow build is a
        <b>direct select</b>, not a narrowed network — a butterfly routes every
        lane at once and cannot be sliced, so one output lane is a
        <code>LANES</code>-to-1 32-bit mux either way. That mux is what the
        width pays for, and it is why one unit recovers <b>59%</b> of what
        deleting the shuffle entirely saves rather than all of it.
      </p>
      <p>
        <b>The default costs exactly zero.</b> At full width the original
        full-width form is kept in its own elaboration branch and the walk
        exists only in the narrow one, so the knob is byte-identical to the
        reference in every column until it is used. The same shape appears on
        the SIMD PE's permute, which is the second measurement that makes this a
        property rather than one campaign's oddity.
      </p>
    </Callout>

    <SpecTable
      :cols="gatesCost.cols"
      :rows="gatesCost.rows"
      caption="The three blocks that make this core SIMT rather than SIMD, from one earlier campaign than the table above — internally comparable, and never subtracted against it. Its rows were taken before the integer dot unit was removed from the neighbouring core."
    />

    <h3 class="doc-h3">
      Why the two widths are equal here, and what pins the integer one
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
      pipelined at II = 1, and sixteen resident wave contexts hide its latency
      rather than stalling on it.
      <b>Eight threads is a constraint; eight float units is a choice</b> — the
      count is a knob with legal values 0, 1, 2, 4 and 8, and a narrower one
      costs an issue interval rather than an instruction.
    </p>

    <Callout
      kind="rule"
      title="A float count below the thread count walks, and the walk is a write enable"
    >
      <p>
        A unit count <code>U</code> below <code>LANES</code> issues
        <code>LANES / U</code> passes, one per cycle. SIMT places a pass with
        the register file's <b>per-lane write enable</b>: thread <i>i</i> is
        served by unit <code>i mod U</code>, a compile-time constant, and the
        enable is a decode of the retiring pass index. There is
        <b>no staging register and no runtime unit select</b>, which is what
        makes a fractional rate cheaper here than on the SIMD PE.
      </p>
      <p>
        <b>Zero is a plausible float answer, which is why the walk exists at
        all.</b> Tying every thread above <code>FLANES</code> to a constant
        instead of sequencing them gives a reduced build a silently wrong result
        rather than a fault — and zero is a number a float kernel meets
        constantly, so nothing downstream trips on it either. The walk
        is built, and a count that does not divide <code>LANES</code> is
        <b>refused at elaboration</b> by a module that does not exist.
      </p>
    </Callout>

    <h3 class="doc-h3">
      One operand width, so there is nothing to select
    </h3>

    <Callout
      kind="rule"
      title="The compute format is IEEE binary32 and there is no knob for it"
    >
      <p>
        A 32-bit word holds exactly one element, so the float tier's slot count
        <i>is</i> the thread count and <b>nothing converts at either edge</b>.
        There is no narrow form, no width bit to delay alongside a result, and
        no second datapath. What <i>is</i> a parameter is how many float units
        are built (<code>FLANES</code>) and how many of them are seed-capable
        (<code>FSFU_UNITS</code>). Neither selects a format.
      </p>
      <p>
        <b>KohakuMPE holds no E8M15.</b> The float lane that converts FP16 and
        FP32 operands into a 24-bit internal format is KohakuTPU's vector core:
        a different project, with its own modules, none of it on this path. A
        precision figure quoted from one says nothing about the other, and a
        total that contains an E8M15 datapath is not a stale measurement of this
        machine — it is a measurement of a different one.
      </p>
    </Callout>

    <Fig
      caption="The float array and its placement. Where a SIMD unit stages a pass into a register and writes once at the end, SIMT writes straight into the register file with a per-lane enable and a constant source — the difference is why the same fractional rate costs less here."
      zoom
      wide
    >
      <BlockDiagram :nodes="floatPath.nodes" :edges="floatPath.edges" />
    </Fig>

    <Callout
      kind="note"
      title="Where the arithmetic comes from, and why it is not forked"
    >
      <p>
        Both PEs instantiate <b>the same float unit</b>, so the per-unit DSP and
        BRAM costs are identical on the two cores by construction rather than by
        intent: a float unit is 2 DSP, and the seed capability adds 1 DSP and
        1.5 BRAM to a unit that has it.
      </p>
      <p>
        The LUT costs are <i>not</i> identical, and the honest form of that
        statement is a <b>pair of marginals rather than one number per core</b>.
        Measured as the difference between two synthesised rows one step apart:
        this PE's unit is <b>789 and 1,104 LUT</b> at the two steps where it was
        taken, and the SIMD PE's is <b>1,095 and 1,003</b>. The two ranges
        <b>bracket each other</b> — this PE's unit is cheaper at the wide end
        and dearer at the narrow one — so there is no single figure, and any
        page that reports one has averaged.
      </p>
      <p>
        <b>An average from a tier total is not a worse estimate; it is a
        measurement of a different thing.</b> A float tier is
        <code>units × cost(unit)</code> <i>plus</i> a fixed overhead that does
        not scale with units at all — the third register-file read port
        <code>vfma</code>'s addend needs, the retire path and the pass
        sequencer — paid once at any nonzero count. Dividing charges the units
        for it, and every average ever computed that way on either core has
        concluded that one core's unit is far dearer than the other's.
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
      caption="The FLT funct7. One bit picks the seed half and two pick the operation within it, so the datapath slices the field instead of comparing against one constant per encoding — the same trick the vmem group uses. There is no operand-width bit: binary32 is the only compute type, so a thread is a whole 32-bit slot and there is nothing to select."
    >
      <BitField :fields="fltF7" />
    </Fig>

    <SpecTable
      :cols="fltOps.cols"
      :rows="fltOps.rows"
      caption="One datapath serves the first four: vfadd is the unit with its multiplier forced to 1.0 and vfmul is the unit with its addend forced to 0.0, so there is never a second adder or a second multiplier. The units are the SIMD tier's rv_fpu and khs_fp32_sfu, instantiated here and never forked, which is what makes a SIMT float result comparable to a SIMD one element for element."
    />

    <Callout
      kind="rule"
      title="The tier has ONE latency, and a nonzero seed count is what sets it"
    >
      <p>
        A fused multiply-add on this device is <b>6 cycles</b> deep and a seed
        is <b>10</b>, so a tier that can issue a seed pads the multiply-add path
        by four to match. The tier's latency —
        <code>ALAT</code> — is therefore <b>6 with
        <code>FSFU_UNITS = 0</code> and 10 otherwise</b>, on both cores, and
        both modules check the depth they were told against the depth they
        built at elaboration.
      </p>
      <p>
        <b>RV32M is padded to exactly that same latency</b>, so the two retire
        through <b>one</b> write port with no arbitration: two results can only
        want the port on the same cycle if they were issued on the same cycle,
        which cannot happen because one instruction issues per cycle. A per-wave
        pending bit blocks the issuing wave for both, so the multiplier needed
        no second mechanism at all.
      </p>
    </Callout>

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
        guarantees by naming a merge block for every selection and loop.
        <b>The two ways of counting differ by a factor of two and both look
        reasonable</b>, so the depth rule is stated wherever the depth is — in
        the encoding table, in the generated header, in the golden model and in
        the RTL.
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
      caption="The external/internal L1 split is the base RV32 core's and is inherited unchanged: split by WHO WRITES, not by what is stored, which is what removes coherence from the design."
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
      caption="Every non-TARGET bar is out-of-context synthesis on xcvu13p-fhgb2104-2L-e, Vivado 2024.2, at a 3.333 ns request, -flatten_hierarchy none, on an INTEGER-ONLY lane array — so none of them is comparable with the arithmetic-tier rows above, and `none` is not what the ship synthesises at. The bars cannot show the one thing that matters most: kht_core contains a kht_unit and kht_pe contains both, so THE ROWS MUST NEVER BE ADDED UP, and only the last one is a PE"
    />

    <Callout
      kind="trap"
      title="Only the last bar is a PE, and it carries no float units and no multiplier"
    >
      <p>
        The area lands where the design wanted it — <b>16,115 LUT</b> against a
        20–25k target, with room — and it must not be read against a budget that
        assumes arithmetic. <code>kht_valu</code> is <code>LANES</code> copies of
        the base RV32I ALU with <b>no multiplier and no float inside that
        module</b>: the float units and the multiplier arrive as
        <i>sibling</i> modules beside it, which is exactly why the ladder never
        sees them and why the ladder's totals must never be quoted as the PE.
      </p>
      <p>
        <b>These rows are also a starting point rather than a result.</b> They
        predate the frequency work described on the
        <RouterLink to="/mpe/simt/microarchitecture" class="doc-link"
          >microarchitecture page</RouterLink
        >, which took the same shape from <b>182 to 394 MHz on 321 LUT
        fewer</b>, and they predate the arithmetic tier entirely.
      </p>
    </Callout>

    <Callout
      kind="rule"
      title="Five reporting rules, and breaking any of them produces a number that looks authoritative"
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
        <b>Name the arithmetic tier.</b> Today's G0 is
        <code>G0(int)</code> — an integer-only lane array, and it stays that way
        on purpose, because the float lane is the SIMD tier's and pricing SIMT
        around a lane this project did not design would answer a different
        question. An integer-only figure and a float-capable figure are
        different machines, and so are a PE with and without the multiplier.
        <b>A G8 total quoted without naming its lane array is not a result</b>,
        and neither is a total that does not say whether the multiplier is in
        it.
      </p>
      <p>
        <b>Name the flatten.</b>
        <code>-flatten_hierarchy none</code> is <b>not the ship</b>: nothing in
        the build scripts sets the setting on the ship's synthesis run, so it
        takes Vivado's default, <code>rebuilt</code>. Measured on the assembled
        PE at the same request, <code>none</code> read
        <b>636 LUT high</b> — 22,257 against 21,621 — because a preserved
        boundary cannot trim an unread output port or fold a constant across a
        module edge. <code>none</code> is what makes a per-block row
        attributable and it stays the diagnostic;
        <b>a row quoted against a budget must be <code>rebuilt</code></b>. The
        ladder and the budget bars on this page are <code>none</code>; the
        arithmetic-tier tables are <code>rebuilt</code>; the two sets do not
        subtract.
      </p>
      <p>
        <b>Synth is not route.</b> Every figure on this page is out-of-context
        synthesis. This project has measured a module lose 0.740 ns between
        synthesis and routing; a small negative slack at synth is not something
        placement absorbs. These numbers size the design and rank the gates.
        They do not close timing, and nothing here claims a closed clock.
      </p>
      <p>
        <b>And read the configuration back off the run log.</b> Every generic
        must be visible on the <code>synth_design</code> command line: a knob
        that is parsed but not applied produces a row that varies in its
        <i>tag</i> and not in its netlist. Two guards in this path failed
        silently once — a timing query returns nothing rather than failing, so
        an unconstrained run reports no Fmax line while every LUT figure is the
        unconstrained one and the whole thing reads exactly like a clean design.
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
      caption="Fourteen shaders, run in 34 CASES: several run more than once, at a different width or wave count, because that is what makes “the ISA knows no unit count” a test rather than a claim — one image, one golden memory, only the generic changes. The whole set runs from one command, python tests/pe/tools/rv_simt_suite.py --gates, and it is SERIAL by construction: the simulator names its build directory after the bench, so two concurrent runs destroy each other's work area and surface as a random shader failing rather than as a collision."
    />

    <h3 class="doc-h3">The banked shared memory, on hardware</h3>

    <SpecTable
      :cols="runOut.cols"
      :rows="runOut.rows"
      caption="Measured on hardware by simt_lds.s. Consecutive words are different banks, which is why stride 1 is conflict-free and stride LANES is the worst case — the same trade every GPU makes. The reversed case is the one that proves the RETURN CROSSBAR: bank 7's word has to reach lane 0, rather than lane 0 always taking bank 0."
    />

    <SpecTable
      :cols="ldsWitness.cols"
      :rows="ldsWitness.rows"
      caption="The same shader, the same correct answer, one parameter changed, and only the request count moves. That is the witness as a CONTROLLED DIFFERENCE — one parameter, one shader, one number — rather than an argument about what a resolver ought to do. Fewer banks is a width like any other: more conflicts, more passes, the same answer, and forward progress unchanged because the resolver still serves the lowest outstanding lane."
    />

    <Callout
      kind="measured"
      title="The request counter is reported NOW, before the coalescer exists"
    >
      <p>
        The load/store unit serialises lanes today, so the request-to-gather
        ratio is <code>LANES</code> <b>by construction</b>. It is counted before
        the optimisation exists precisely so the improvement will be a measured
        change rather than a number that appears from nothing —
        <b>a witness that only appears once the optimisation lands cannot show
        the optimisation working.</b>
      </p>
      <p>
        <code>simt_gather.s</code> is the case it will be judged on: a per-lane
        base load across <b>five</b> distinct 32-byte lines, so a working
        coalescer is visible as the request count falling toward the line count.
        The three addressing tiers are already distinguished in the encoding, so
        the coalescer replaces the walk <b>without the ISA moving</b>.
      </p>
    </Callout>

    <h3 class="doc-h3">Not built yet</h3>
    <p class="doc-p">
      <b>G5</b>, the coalescer, and <b>G6</b>, MSHRs. G7 interleaves
      <i>issue</i>, but one instruction is in flight at a time and a miss holds
      the whole front end, so until G6 lets a stalled wave step aside, more
      waves buy hazard-free issue and nothing else.
    </p>

    <Callout kind="open" title="One thing verified on SIMD and NOT on SIMT">
      <p>
        The seed units are the same modules on both PEs, and the seed
        <i>datapath</i> and the seed <b>walk</b> are both exercised — on the
        SIMD PE, at every unit count, and they pass. What is untested is
        <b>the SIMT side's own operand routing and placement for a seed</b>,
        because no SIMT shader can issue one today and the reference seed model
        is a float64 approximation rather than a bit-exact one. A bench that
        compares an exact memory checksum cannot grade an approximation, so
        closing this needs a tolerance-comparing SIMT bench that does not exist.
      </p>
    </Callout>

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
