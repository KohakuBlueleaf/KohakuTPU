<script setup>
/* KohakuMPE — a project whose compute units are processors.
 *
 * PROVENANCE RULE FOR THIS FILE. Every resource or frequency figure names its
 * part, its tool, its flow, its requested period and the script that produced
 * it, and no figure appears without all five. Where a published total describes
 * a configuration the RTL can no longer build, it is named as such and NOT
 * carried into arithmetic.
 *
 * Sources: docs/projects/kohakumpe/README.md, unit-counts.md,
 * configurable-widths.md; docs/arch/cpu/rv32-pe/performance.md;
 * src/kohakumpe/**, src/kohakuaccel/pe/rv32/**.
 */

/* ---------------------------------------------------------------- the PE */
const pe = {
  nodes: [
    {
      id: "win",
      x: 0,
      y: 0,
      w: 9,
      h: 7,
      label: "instruction window",
      sub: "IMEM_WORDS × 32 b · block RAM · written by the fabric",
    },
    {
      id: "fe",
      x: 12.5,
      y: 0,
      w: 9,
      h: 7,
      label: "front end",
      sub: "fetch · decode · one PC per instruction stream",
    },
    {
      id: "srf",
      x: 25,
      y: 0,
      w: 9,
      h: 7,
      label: "scalar registers",
      sub: "x0..x31 · 32 b · LUTRAM, two mirrored arrays",
    },
    {
      id: "alu",
      x: 37.5,
      y: 0,
      w: 9,
      h: 7,
      label: "scalar IM unit",
      sub: "add · compare · bitwise · multiply — 4 DSP48",
    },
    {
      id: "arr",
      x: 50,
      y: 0,
      w: 9,
      h: 7,
      label: "the unit array",
      sub: "the replicated arithmetic — this is the width",
      accent: true,
    },
    {
      id: "res",
      x: 62.5,
      y: 0,
      w: 9,
      h: 7,
      label: "result mux",
      sub: "one select arm per feature; a missing arm is a silent wrong answer",
      accent: true,
    },

    {
      id: "port",
      x: 12.5,
      y: 11.5,
      w: 9,
      h: 6,
      label: "compute-unit port",
      sub: "noc_cu_base — kick in, completion out",
    },
    {
      id: "spad",
      x: 25,
      y: 11.5,
      w: 9,
      h: 6,
      label: "scratchpad",
      sub: "SPAD_WORDS × 32 b · block RAM",
    },
    {
      id: "l1",
      x: 37.5,
      y: 11.5,
      w: 9,
      h: 6,
      label: "internal L1",
      sub: "L1_LINES × 32 B · one outstanding miss",
    },
    {
      id: "wrf",
      x: 50,
      y: 11.5,
      w: 9,
      h: 6,
      label: "wide register file",
      sub: "the replicated state — the widest path in the unit",
      accent: true,
    },
  ],
  edges: [
    { from: "win:r", to: "fe:l", dir: "h" },
    { from: "fe:r", to: "srf:l", dir: "h" },
    { from: "srf:r", to: "alu:l", dir: "h" },
    { from: "alu:r", to: "arr:l", dir: "h", accent: true, label: "addr" },
    { from: "arr:r", to: "res:l", dir: "h", accent: true },
    { from: "res:b", to: "wrf:r", accent: true, label: "write" },
    { from: "wrf:t", to: "arr:b", dir: "v", accent: true, label: "read" },
    { from: "port:t", to: "fe:b", dir: "v" },
    { from: "spad:t", to: "srf:b", dir: "v" },
    { from: "l1:t", to: "alu:b", dir: "v" },
    { from: "port:l", to: "win:b" },
  ],
  groups: [
    {
      x: 11.3,
      y: -1.3,
      w: 36.4,
      h: 9.5,
      label:
        "the framework's RV32IM controller core, unchanged in both classes",
    },
    {
      x: 48.8,
      y: -1.3,
      w: 24,
      h: 20,
      label: "the wide datapath — khs_unit (SIMD) or kht_unit (SIMT)",
    },
  ],
};

/* ---------------------------------------------------------------- knobs */
const knobs = {
  cols: [
    { key: "k", label: "Knob", mono: true },
    { key: "on", label: "On", mono: true },
    { key: "w", label: "What it moves" },
  ],
  rows: [
    {
      k: "SIMD / LANES",
      on: "both",
      w: "<b>Everything.</b> The register file, the scratchpad row, the cross-lane network and the reduction trees move together, and so does the memory granule. It is <b>not a tuning knob</b>: 8 × 32 bit is one flit payload and one memory-agent entry, and narrowing it turns every coalesced load into two or more requests permanently.",
    },
    {
      k: "FLOAT_LANES / FLANES",
      on: "both",
      w: "The largest single block in either PE. Marginally <b>1,003–1,095 LUT</b> per unit on SIMD and <b>789–1,104</b> on SIMT, plus 2 DSP48 each. Zero elaborates no float tier at all and every float encoding faults.",
    },
    {
      k: "FSFU_UNITS",
      on: "both",
      w: "How many of those float units carry the four seeds. <b>Not monotonic in LUT</b> — see the trap below — and a nonzero count deepens the whole tier from 6 cycles to 10, on both cores.",
    },
    {
      k: "PERM_UNITS / SHFL_UNITS",
      on: "both",
      w: "The cross-lane network. Pays <b>only at one or two units</b>: a narrow build is a direct select rather than a narrowed network, so one output lane is a <code>LANES</code>-to-1 32-bit mux either way.",
    },
    {
      k: "LDS_BANKS",
      on: "SIMT",
      w: "Banks in the shared memory. Its conflict resolver is a <code>LANES × LANES</code> comparison, so its LUT goes as the <b>square</b> of the lane count while its flops and BRAM stay linear.",
    },
    {
      k: "WAVES",
      on: "SIMT",
      w: "Resident wave contexts. <b>+0 LUT to store</b> — the register file is already a memory, so a wave id is address bits — <b>+124</b> once the per-wave mask and stack arrays are on, and <b>+1,775</b> to schedule.",
    },
    {
      k: "ILANES · SHIFT_UNITS · RED_UNITS",
      on: "SIMD",
      w: "The integer tier's widths. <code>ILANES</code> narrows the <b>ALU and not the multipliers</b> — the DSP column is flat at 8, 4 and 2 — because fabric adders and DSP columns are two budgets and one knob must not span both.",
    },
    {
      k: "VREG_PRIM",
      on: "both",
      w: "Which primitive the wide register file lands in. On SIMT, <code>distributed</code> is <b>+6,226 LUT</b> — more than twice the whole SIMT substrate — for 8 BRAM and megahertz already met. On SIMD it is available in the other direction and lands on the binding path.",
    },
    {
      k: "VREGS · IPDOM_D · NACC",
      on: "—",
      w: "<b>Not levers.</b> A distributed-RAM primitive is 32 entries deep, so a file of 8 and a file of 32 cost the same LUTs; a divergence stack is small storage. Measured at −35 and −8 LUT respectively.",
    },
  ],
};

/* --------------------------------------------------- the measured record */
const measured = {
  cols: [
    { key: "w", label: "Configuration" },
    { key: "lut", label: "LUT", align: "right", mono: true },
    { key: "ff", label: "FF", align: "right", mono: true },
    { key: "bram", label: "BRAM", align: "right", mono: true },
    { key: "dsp", label: "DSP", align: "right", mono: true },
    { key: "f", label: "Fmax", align: "right", mono: true },
    { key: "fl", label: "flatten", align: "right", mono: true },
  ],
  rows: [
    {
      w: "<b>RV32 PE</b>, shipped configuration — the base both classes are built on. <code>ooc_rv_pe.tcl</code>, top <code>rv_pe</code>",
      lut: "<b>2,586</b>",
      ff: "3,844",
      bram: "9",
      dsp: "<b>4</b>",
      f: "363.5<br><span class='opacity-60'>+0.582 ns</span>",
      fl: "none",
      _tone: "good",
    },
    {
      w: "<b>SIMD PE</b> reference row — 8 slots, 8 integer lanes, 4 float units, no seeds, no accumulator",
      lut: "<b>15,682</b>",
      ff: "9,836",
      bram: "13",
      dsp: "56",
      f: "349.3",
      fl: "rebuilt",
    },
    {
      w: "<b>SIMT PE</b> reference row — 8 threads, 16 waves, 8 float units, no seeds, every gate on",
      lut: "<b>19,461</b>",
      ff: "17,268",
      bram: "30.5",
      dsp: "48",
      f: "361.0",
      fl: "rebuilt",
    },
    {
      w: "the same SIMD PE with <code>SIMD_EN = 0</code> — the extension gone, a <code>generate</code> rather than a zero width",
      lut: "2,661",
      ff: "4,140",
      bram: "5",
      dsp: "0",
      f: "396.5",
      fl: "rebuilt",
      _tone: "warn",
    },
  ],
};

const parity = {
  cols: [
    { key: "b", label: "Block" },
    { key: "t", label: "SIMT", align: "right", mono: true },
    { key: "d", label: "SIMD", align: "right", mono: true },
  ],
  rows: [
    { b: "PE with no float tier", t: "<b>10,852</b>", d: "<b>10,309</b>" },
    { b: "marginal FP FMA unit", t: "789 – 1,104", d: "1,003 – 1,095" },
    { b: "the active mask and the IPDOM stack", t: "681", d: "—" },
    { b: "the subgroup butterfly", t: "1,224", d: "—" },
    { b: "the banked LDS and its resolver", t: "1,948", d: "—" },
    {
      b: "the packed shifter",
      t: "— <span class='opacity-60'>RV32I's shifter is inside the thread's own ALU</span>",
      d: "1,088",
    },
    { b: "the cross-lane permute — slide, pack, unpack", t: "—", d: "1,884" },
    {
      b: "<b>stripped: both at 8 FMA and 8 multiply units, every optional block off</b>",
      t: "<b>16,118</b>",
      d: "<b>16,775</b>",
      _tone: "warn",
    },
  ],
};

/* ------------------------------------------------------------- the walk */
const walkBroken = {
  rows: [
    { name: "MEM", kind: "bus", values: ["vfmul", "next", "next"] },
    { name: "pass", kind: "bus", values: ["0", "—", "—"] },
    { name: "threads served", kind: "bus", values: ["3..0", "—", "—"] },
    {
      name: "threads 7..4",
      kind: "text",
      values: ["0x0000_0000", "", ""],
      mark: [0],
    },
    { name: "retires", kind: "bit", values: [1, 0, 0] },
  ],
  notes: [
    {
      cycle: 0,
      text: "Four units, eight threads, and the four threads above the unit count are tied to a constant instead of being sequenced. One pass issues and the instruction retires on it.",
      tone: "bad",
    },
    {
      cycle: 1,
      text: "Half the register is correct and half is zero. Nothing faults, because a build that cannot do something has to REFUSE it — and this one did not refuse, it answered. Zero is a value a float kernel meets constantly, so nothing downstream trips on it either.",
      tone: "bad",
    },
  ],
};

const walkFixed = {
  rows: [
    { name: "MEM", kind: "bus", values: ["vfmul", "vfmul", "next"] },
    { name: "pass", kind: "bus", values: ["0", "1", "—"] },
    { name: "threads served", kind: "bus", values: ["3..0", "7..4", "—"] },
    { name: "MEM held", kind: "bit", values: [1, 0, 0] },
    { name: "retires", kind: "bit", values: [0, 1, 0], mark: [1] },
  ],
  notes: [
    {
      cycle: 0,
      text: "Unit u on pass p serves element p·U + u. One pass issues per cycle and the memory stage is held until the last of them has gone.",
      tone: "good",
    },
    {
      cycle: 1,
      text: "The instruction retires exactly ONCE, at every unit count. The vector file is written once, the scalar writeback fires once, and nothing in the program can see the passes — which is what makes the same binary and the same golden memory image run at every width.",
      tone: "good",
    },
  ],
};

/* ----------------------------------------------------------- the encoding */
const rtype = [
  { name: "funct7", bits: 7, value: "the operation", accent: true },
  { name: "rs2", bits: 5 },
  { name: "rs1", bits: 5 },
  { name: "funct3", bits: 3, value: "the group", accent: true },
  { name: "rd", bits: 5 },
  { name: "opcode", bits: 7, value: "a custom major" },
];

const fields = {
  cols: [
    { key: "f", label: "Field", mono: true },
    { key: "w", label: "Width", align: "right", mono: true },
    { key: "p", label: "Position", mono: true },
    { key: "o", label: "Owner" },
  ],
  rows: [
    {
      f: "opcode",
      w: "7",
      p: "[6:0]",
      o: "<b>RISC-V.</b> One of the four custom majors, or the standard OP major for RV32M",
    },
    {
      f: "rd",
      w: "5",
      p: "[11:7]",
      o: "the instruction. On a vector store it carries the <b>data</b> register, because a vector store's data comes from the vector file and RV32's S-format constraint therefore does not apply",
    },
    {
      f: "funct3",
      w: "3",
      p: "[14:12]",
      o: "<b>the PE class</b> — it names the group, and the allocation is per class",
    },
    { f: "rs1", w: "5", p: "[19:15]", o: "the instruction" },
    { f: "rs2", w: "5", p: "[24:20]", o: "the instruction" },
    {
      f: "funct7",
      w: "7",
      p: "[31:25]",
      o: "<b>the group.</b> Its internal layout is the group's, not the class's, and the datapath <b>slices</b> it rather than comparing against one constant per encoding",
    },
    {
      f: "funct7[1:0]<br>on a typed SIMD group",
      w: "2",
      p: "[26:25]",
      o: "<b>fixed:</b> 0 = int8, 1 = int16, 2 = int32. Read straight off the instruction word rather than out of a decode case, so two adjacent instructions may use different widths with no state to change",
      _tone: "warn",
    },
    {
      f: "funct7[1:0]<br>on the SIMD float major",
      w: "2",
      p: "[26:25]",
      o: "<b>fixed:</b> <code>f32</code> is the only value a build accepts. Every other value is an unmapped encoding rather than a silent reinterpretation",
      _tone: "warn",
    },
    {
      f: "funct7[2]<br>on the SIMT float group",
      w: "1",
      p: "[27]",
      o: "<b>fixed:</b> selects the seed half. <code>funct7[1:0]</code> then names the operation within it",
    },
    {
      f: "funct7[6:4] · [3:2] · [1:0]<br>on the SIMT memory group",
      w: "3 · 2 · 2",
      p: "[31:29] · [28:27] · [26:25]",
      o: "<b>fixed:</b> op, scale, width. <code>op &gt;= 3</code> is exactly the lane-linear predicate, which is one comparator",
    },
  ],
};

const majors = {
  cols: [
    { key: "m", label: "Major", mono: true },
    { key: "o", label: "Opcode", mono: true },
    { key: "own", label: "Owner" },
    { key: "c", label: "Carries" },
  ],
  rows: [
    {
      m: "custom-0",
      o: "0x0B",
      own: "<b>SIMD PE</b>",
      c: "the packed-integer tier: <code>VLD</code>, <code>VST</code>, <code>VINT</code>, <code>VBIT</code>, <code>VSHI</code>, <code>VMOV</code>, <code>VPRM</code>",
    },
    {
      m: "custom-0<br><code>funct3 = 5</code>",
      o: "0x0B",
      own: "<b>SIMD PE</b>",
      c: "<b><code>VMAC</code> — reserved and unmapped.</b> The retired integer dot unit and its accumulator lived here. Leaving the group unmapped is what makes a binary built for them <b>fault</b> rather than decode as something adjacent",
      _tone: "warn",
    },
    {
      m: "custom-1",
      o: "0x2B",
      own: "<b>SIMD PE</b>",
      c: "the float tier: <code>FMAC</code>, <code>FCVT</code>, <code>FALU</code>, <code>FSFU</code>. <code>FLOAT_LANES = 0</code> leaves the major unmapped",
    },
    {
      m: "custom-1<br><code>funct3 = 1</code>",
      o: "0x2B",
      own: "<b>SIMD PE</b>",
      c: "<b><code>FRED</code> — declared and not built.</b> <code>vfredsum.f32</code> encodes and <b>faults</b>. Returning slot 0 alone would be a plausible wrong answer, which is the one thing a refusal exists to prevent",
      _tone: "bad",
    },
    {
      m: "custom-2",
      o: "0x5B",
      own: "<b>SIMT PE</b>",
      c: "the R-type groups: scalar ALU, moves, divergence, subgroup, the memory forms, and the float group at <code>funct3 = 5</code>",
    },
    {
      m: "custom-2<br><code>funct3 = 6, 7</code>",
      o: "0x5B",
      own: "<b>SIMT PE</b>",
      c: "<b>unallocated.</b> Reserved and they fault",
    },
    {
      m: "custom-3",
      o: "0x7B",
      own: "<b>SIMT PE</b>",
      c: "the I-type groups: scalar immediate ALU and the two uniform branches. An I-type layout has no <code>funct7</code>, so a group here holds exactly <b>one</b> instruction",
    },
    {
      m: "OP",
      o: "0x33",
      own: "<b>RISC-V — not in the pool</b>",
      c: "<code>funct7 = 0000001</code> is RV32M. <code>mul</code>, <code>mulh</code>, <code>mulhsu</code>, <code>mulhu</code> are built on every RV32 core in the tree; <code>funct3</code> 100–111 is <code>div</code>/<code>divu</code>/<code>rem</code>/<code>remu</code> and is <b>decoded and refused</b>",
      _tone: "good",
    },
  ],
};

/* ---------------------------------------------------------- four kinds */
const kinds = {
  cols: [
    { key: "t", label: "Thing" },
    { key: "c", label: "Category" },
  ],
  rows: [
    {
      t: "the compute-unit port each class presents — its signals, its kick, its completion",
      c: "<b>fixed protocol</b>, and the framework's rather than this project's",
    },
    {
      t: "the vector scratchpad's store-only rule from the scalar side, and the vector alignment contract",
      c: "<b>fixed protocol</b> of the SIMD PE",
    },
    {
      t: "the accumulation order of the SIMD float accumulator — <code>NPART</code> and the unit count both change the answers",
      c: "<b>fixed protocol</b> of the SIMD PE. Float addition does not associate, so this is arithmetic and not an implementation detail",
    },
    {
      t: "a <code>split</code> pushes two entries and a <code>join</code> pops one; a depth of D permits D/2 nested levels; overflow is a fault",
      c: "<b>fixed protocol</b> of the SIMT PE",
    },
    {
      t: "a feature at zero <b>faults</b> rather than computing something plausible",
      c: "<b>fixed protocol</b> of both. A plausible wrong number is worse than a halt, and knowing is the point of building a narrow configuration",
    },
    {
      t: "<code>khs_unit</code> and <code>khs_scalar_decode</code> in the framework's <code>SIMD_EN</code> slot",
      c: "<b>customizable addon</b> — the slot is named behind a parameter that is 0 by default, so a framework-only build never elaborates either and the names need not resolve",
    },
    {
      t: "every width: <code>SIMD</code>, <code>ILANES</code>, <code>FLOAT_LANES</code>, <code>FSFU_UNITS</code>, <code>PERM_UNITS</code>, <code>SHIFT_UNITS</code>, <code>RED_UNITS</code>, <code>FCVT_UNITS</code>, <code>LANES</code>, <code>WAVES</code>, <code>FLANES</code>, <code>SHFL_UNITS</code>, <code>LDS_BANKS</code>",
      c: "<b>yours.</b> These are this project's parameters on this project's units, so their range and meaning are set here rather than by the framework",
    },
    {
      t: "the marginal method — two synthesised rows differing in exactly one count",
      c: "<b>convention.</b> Worth copying, and nothing enforces it",
    },
    { t: "what a kernel or a shader computes", c: "<b>yours</b>" },
  ],
};

const notOwned = {
  cols: [
    { key: "n", label: "Not owned here" },
    { key: "w", label: "Who owns it" },
  ],
  rows: [
    {
      n: "the flit, the router, the compute-unit port, the credit rule",
      w: "the framework's mesh",
    },
    {
      n: "descriptors, addresses, how operands reach a window",
      w: "the framework's system node",
    },
    {
      n: "the base RV32IM pipeline, its regions, its halt model, its multiplier",
      w: "the framework's RV32 controller PE. Both classes inherit it, and neither may change it",
    },
    {
      n: "the fused multiply-add itself",
      w: "<code>rv_fpu.v</code>, in the <b>framework</b>, because RV32F is a standard extension over IEEE binary32 and binary32 is nobody's private format",
    },
    {
      n: "E8M15, and anything that computes in it",
      w: "KohakuTPU's vector core. <b>There is no E8M15 anywhere in KohakuMPE</b>, and a precision figure quoted from one project says nothing about the other",
    },
    {
      n: "a systolic dataflow with operands resident across many passes",
      w: "a matrix unit. Neither class pretends to be one, and a part that needs high-rate dot products carries one",
    },
    {
      n: "which custom major belongs to which class",
      w: "the opcode map, which lives outside both classes' ISA modules — a table one tier owns is not an authority the other can check itself against",
    },
    {
      n: "where a PE lands on the die and at what clock",
      w: "the framework's physical layer. No PE here has been placed or routed",
    },
  ],
};
</script>

<template>
  <DocPage
    title="A mesh of processors"
    summary="KohakuMPE is what the framework builds when the compute units on a mesh are processors rather than a fixed datapath. Two processing-element classes, what selects which, and the one idea both are configured by: every compute feature is a unit count."
    domain="simt"
    status="measured"
    source="src/kohakumpe/ · src/kohakuaccel/pe/rv32/ · docs/projects/kohakumpe/"
  >
    <h2 class="doc-h2">What this project owns</h2>
    <p class="doc-p">Four things, and nothing else.</p>
    <div class="grid gap-3 sm:grid-cols-2 my-5">
      <div class="card p-4">
        <div
          class="kt-text-caption font-semibold text-warm-800 dark:text-warm-200 mb-1"
        >
          The SIMD PE
        </div>
        <p class="kt-text-caption text-warm-500 dark:text-warm-400 leading-6">
          One instruction over eight 32-bit slots and a separately-sized float
          tier. One program counter, one address stream, and no way for the
          lanes to disagree.
        </p>
      </div>
      <div class="card p-4">
        <div
          class="kt-text-caption font-semibold text-warm-800 dark:text-warm-200 mb-1"
        >
          The SIMT PE
        </div>
        <p class="kt-text-caption text-warm-500 dark:text-warm-400 leading-6">
          One instruction stream over many <b>threads</b>, each with its own
          register file, its own address, and its own side of a branch.
        </p>
      </div>
      <div class="card p-4">
        <div
          class="kt-text-caption font-semibold text-warm-800 dark:text-warm-200 mb-1"
        >
          The width mechanism
        </div>
        <p class="kt-text-caption text-warm-500 dark:text-warm-400 leading-6">
          Every compute feature on both classes is an independent
          <b>unit count</b>. A narrower count costs cycles, never encodings, and
          never changes an answer.
        </p>
      </div>
      <div class="card p-4">
        <div
          class="kt-text-caption font-semibold text-warm-800 dark:text-warm-200 mb-1"
        >
          A framework slot occupant
        </div>
        <p class="kt-text-caption text-warm-500 dark:text-warm-400 leading-6">
          <span class="chip">khs_unit</span> fills the framework's
          <span class="chip">SIMD_EN</span> slot, which makes it a
          <b>customizable addon</b> occupant as well as a compute unit.
        </p>
      </div>
    </div>

    <p class="doc-p">
      Three terms before anything else. A <b>compute unit</b> is the block a
      project writes and the framework attaches, through one port whose signals
      and protocol the framework fixes. A <b>PE</b> — processing element — is a
      compute unit that fetches and executes instructions; every PE here is the
      framework's RV32IM controller core with a wide datapath bolted to its
      execute stage. A <b>lane</b> or <b>unit</b> is one copy of the arithmetic,
      and a datapath <i>W</i> units wide serves <i>N</i> elements in
      <span class="chip">N/W</span> <b>passes</b>, one issued per cycle, that no
      instruction can see.
    </p>

    <p class="doc-p">
      The alternative that was rejected is a fixed datapath on the same port,
      which is what the reference accelerator does and does well. It loses when
      the work has control flow in it: a matrix cluster or a vector core is
      configured by a descriptor, and everything it can do has to be expressible
      as one. A processor on the port carries a program counter instead, so the
      decision about what happens next is made on the unit rather than sent to
      it — at the cost of an instruction window, a fetch path and a front end
      that the fixed datapath does not pay for. Both are first-class citizens of
      one mesh, and that is the framework's actual claim rather than either
      project's.
    </p>

    <h2 class="doc-h2">What a PE costs</h2>
    <p class="doc-p">
      Both classes are the same picture with a different array in the middle.
      The cost is set by what is <i>replicated</i> — the wide register file and
      the unit array — and by the memories, which are fixed regardless of width.
    </p>

    <Fig
      caption="One PE, both classes. The left three quarters are the framework's controller core and are identical in a SIMD PE, a SIMT PE and a bare RV32 PE. The accented ring is what a width moves: the wide register file out, through the unit array, through the result mux, back into the write port — and it is also the loop that sets the frequency on the SIMD PE. The fabric writes the instruction window and the scratchpads directly through the port; it never reaches a register file."
      zoom
      wide
    >
      <BlockDiagram :nodes="pe.nodes" :edges="pe.edges" :groups="pe.groups" />
    </Fig>

    <Callout
      kind="rule"
      title="Every figure on these pages is out-of-context synthesis, and five things have to be named before one means anything"
    >
      <p>
        The <b>part</b> — <span class="chip">xcvu13p-fhgb2104-2L-e</span> unless
        stated otherwise. The <b>tool</b> — Vivado 2024.2. The
        <b>requested period</b>, because LUT is not independent of it: synthesis
        spends area chasing timing it cannot reach. The <b>flow</b> —
        <span class="chip">-flatten_hierarchy</span> and the directive. And the
        <b>script</b> that produced it.
      </p>
      <p>
        <b>No frequency figure in this project is a closed-timing result.</b>
        They are synthesis estimates, they are the optimistic end — this
        repository has measured a module lose <b>0.740&nbsp;ns</b> between
        synthesis and routing — and they move by tens of megahertz between rows
        that differ in nothing that should matter. Treat them as a screen for a
        structural problem, not as a result.
        <b>Nothing here is placed or routed</b>, and no mesh of these PEs has
        been assembled.
      </p>
    </Callout>

    <Callout
      kind="trap"
      title="A LUT count varies along four axes, and a row that names fewer than four is not comparable with anything"
    >
      <p>
        <b>1. Measurement context.</b> Standalone-as-top against sub-hierarchy
        of a larger synthesis. <b>2. Flow.</b>
        <span class="chip">-flatten_hierarchy none</span> preserves module
        boundaries and is the <i>diagnostic</i> setting;
        <span class="chip">rebuilt</span> is Vivado's default and is what the
        ship synthesises at, because nothing in the build scripts sets the
        setting on the ship's run. On the assembled SIMT PE at one target,
        <span class="chip">none</span> read <b>636 LUT high</b>; on the RV32 PE
        the two differ by <b>+720 LUT and −4.1 MHz</b> for
        <span class="chip">none</span>. The gap is
        <b>configuration-dependent</b> — on the SIMD PE it has measured 647 LUT
        at one setting of one knob and 243 at another — so it cannot be carried
        between rows as a correction either.
      </p>
      <p>
        <b>3. The timing request.</b> Rows taken at different periods are never
        subtracted. <b>4. Which report you read.</b>
        <span class="chip">report_utilization</span> counts CLB LUT
        <i>sites</i>; a primitive census counts raw LUT <i>primitives</i>. For
        one module in this repository those read <b>6,360 and 6,739</b>; for the
        RV32 PE they read <b>2,586 and 2,910</b>. Neither is the other plus an
        error term — a CLB site can host two smaller primitives, and LUTRAM
        occupies a site without being a LUT primitive. Quote whichever answers
        your question, and say which it is.
      </p>
      <p>
        <b
          >The symptom of getting this wrong is a delta that looks like a design
          change.</b
        >
        The RV32 PE row below is a <span class="chip">none</span> figure and the
        two wide rows are <span class="chip">rebuilt</span>; the columns line up
        and the rows do not subtract.
      </p>
    </Callout>

    <SpecTable
      :cols="knobs.cols"
      :rows="knobs.rows"
      caption="The knobs that move cost, in the order they matter. Every per-unit figure is MARGINAL — the difference between two synthesised rows one step apart, divided by the change in the count. Dividing a tier's total by its unit count charges the tier's fixed overhead (the extra register-file read port the fused multiply-add's addend needs, the retire path, the scoreboard, the pass sequencer) to the units, and manufactures a defect that is not there."
    />

    <Callout
      kind="trap"
      title="A fractional rate is worst in the middle, on both cores"
    >
      <p>
        The seed count is <b>not monotonic in LUT</b>, and the reason is
        structural rather than an artefact of one campaign. On the SIMT PE, four
        seed units of eight measured <b>cheaper than two</b>. Splitting each row
        into seed hardware at 276 LUT a unit leaves a residual — the walk — of
        185, 674, −78 and 0 at one, two, four and eight units, and that residual
        is the sum of two terms that move in <b>opposite directions</b> with the
        count. The <b>placement mux</b> — thread <i>i</i>'s source is unit
        <span class="chip">i mod U</span> — does not exist at one unit and grows
        with <i>U</i>. The <b>pass decode</b> has
        <span class="chip">LANES/U</span> values and shrinks with <i>U</i>. At
        full rate both index arms are the same expression and the mux folds
        entirely.
      </p>
      <p>
        A second, independent measurement on the <i>other</i> core gives the
        same shape, which is what makes it a property rather than one campaign's
        oddity: full rate measured <b>66 LUT below</b> one unit, for four times
        the rate.
        <b
          >Take the seed count equal to the float count and spend the DSP and
          BRAM, or take one unit.</b
        >
        Quarter rate — the ratio every desktop GPU provisions transcendentals at
        — saves three quarters of the BRAM and DSP and only 44% of the LUT.
      </p>
    </Callout>

    <h2 class="doc-h2">Every compute feature is a width</h2>

    <Callout kind="rule" title="A width costs cycles, never encodings">
      <p>
        A feature with <span class="chip">U</span> units serving
        <span class="chip">N</span> elements issues
        <span class="chip">N/U</span> passes, one per cycle, sequenced by
        hardware. <b>The instruction set carries no count</b>: the same
        instruction, the same binary and the same golden memory image run at
        every width, and the only difference is cycles.
      </p>
      <p>
        <b>A width at zero means the feature is not built</b>, and every
        encoding that would need it <b>MUST</b> fault at decode.
        <b>A width at full costs nothing</b> — at
        <span class="chip">U == N</span> the hardware is the plain un-walked
        array, because the sequencing logic exists only in the narrow
        elaboration branch. And a feature that can only be present or absent is
        still spelled as a <b>count</b> with the values 0 and 1: one vocabulary,
        one way to say none.
      </p>
    </Callout>

    <WaveTrace
      :rows="walkBroken.rows"
      :notes="walkBroken.notes"
      variant="broken"
      label="Threads above the unit count tied to a constant — a silently wrong half-register"
    />
    <WaveTrace
      :rows="walkFixed.rows"
      :notes="walkFixed.notes"
      variant="fixed"
      label="The pass walk — the memory stage held, the instruction retired once"
    />

    <Callout
      kind="trap"
      title="A width that does not divide the element count elaborates cleanly, synthesises, and reports a plausible frequency"
    >
      <p>
        That is the whole reason the rule is enforced at
        <b>elaboration</b> rather than left to a bench. A non-dividing count
        truncates the pass count, so the walk covers <i>some</i> of the elements
        rather than all of them — and the build is otherwise entirely
        well-formed. It fails only in a component bench, on a workload, or on
        silicon.
      </p>
      <p>
        The refusal is written as an instantiation of a module that does not
        exist, so the error names the rule that broke rather than a line number:
        <span class="chip"
          >Module &lt;khs_unit_requires_PERM_UNITS_to_divide_SIMD&gt; not
          found</span
        >. The rules, on both cores: every width is 0, −1, or divides the
        element count and does not exceed it; a seed unit is a float unit, so
        <span class="chip">FSFU_UNITS &lt;= FLOAT_LANES</span>; a float
        <i>group</i> with no units is refused; and the tier's declared latency
        must equal the depth its array builds.
      </p>
    </Callout>

    <h2 class="doc-h2">What the two classes measure</h2>

    <SpecTable
      :cols="measured.cols"
      :rows="measured.rows"
      caption="xcvu13p-fhgb2104-2L-e, Vivado 2024.2, out-of-context SYNTHESIS at a 3.333 ns request, -directive default. Nothing placed, nothing routed. The RV32 PE row is scripts/tcl/ooc_rv_pe.tcl at -flatten_hierarchy none; the three wide rows are one frozen tree of a scripts/py/khs_sweep.py campaign at rebuilt, and are tabulated in docs/projects/kohakumpe/unit-counts.md, which names the tree per table. The four DSP48 on the RV32 row ARE the multiplier — an earlier measurement of the same unit reported 0 DSP48 and is superseded, because it predated it."
    />

    <Callout
      kind="trap"
      title="Every published absolute total for either wide PE describes a configuration the RTL can no longer build"
    >
      <p>
        The float tier was rebuilt from an E8M15 datapath with two operand
        formats into a binary32-only one, which deleted both operand converters;
        the integer dot unit, its accumulator, the
        <span class="chip">MULS</span> multiplier-depth knob and the
        <span class="chip">DOT_DSP</span> mapping knob were removed; the
        <span class="chip">HAS_SHIFT</span> /
        <span class="chip">HAS_PERM</span> /
        <span class="chip">HAS_FLOAT</span> booleans went in favour of the
        counts alone; and the converter group gained the datapath it had been
        missing.
        <b
          >Re-measurement against the current parameter set has not been
          published.</b
        >
      </p>
      <p>
        What survives is the <b>shape</b>, and the shapes are the findings: what
        a marginal unit costs, where a width pays and where it does not, and
        which knobs are not levers. So the rows above are kept and
        <b>are not multiplied into a mesh total</b>. Any page that prices an
        array by multiplying one of them by a PE count is pricing a machine that
        cannot be built.
      </p>
    </Callout>

    <h3 class="doc-h3">Parity is not redundancy</h3>

    <SpecTable
      :cols="parity.cols"
      :rows="parity.rows"
      caption="One earlier campaign than the reference rows above, internally comparable and never subtracted against them. Its rows were taken before the integer dot unit was removed."
    />

    <Callout
      kind="trap"
      title="SIMD does not beat SIMT on LUT at matched features"
    >
      <p>
        With the mask, the divergence stack, the shuffle and the banked shared
        memory all off, at 8 fused multiply-adds and 8 multiply units, the SIMT
        PE measures <b>16,118</b>. The comparable SIMD figure — its own
        reference less the packed shifter and the permute — is <b>16,775</b>,
        which is <b>657 LUT, 4.1%, dearer.</b> The two land within 1% of each
        other at matched widths, and the price list says why that is not a
        redundancy to remove.
      </p>
      <p>
        SIMD's base PE is <b>543 LUT cheaper</b> than SIMT's — 10,309 against
        10,852 — even though SIMD's base carries the shifter, the permute
        network and thirty-two multipliers and SIMT's carries no multiplier at
        all inside the lane array. So the divergence hardware is real and SIMD
        does not pay for it. But SIMD is <b>not a subset of SIMT</b>: it carries
        packed int8/int16/int32 lanes, a cross-lane permute network, a vector
        scratchpad and optionally a rotating float accumulator.
        <b
          >"SIMD must be much cheaper at the same features" is not reachable by
          removing redundancy; it is a decision about which SIMD features to
          drop</b
        >, and every one of them is priced.
      </p>
    </Callout>

    <h2 class="doc-h2">One float format, and it is not a setting</h2>

    <div
      class="font-mono kt-text-body whitespace-pre text-warm-700 dark:text-warm-300 leading-7 overflow-x-auto my-3"
    >
      IEEE binary32 in -&gt; binary32 compute -&gt; binary32 out
    </div>

    <p class="doc-p">
      Both classes compute in binary32 and nothing else. There is no second
      format, no conversion at the operand edge, and no parameter in either PE
      that selects one. <b>Denormals flush to sign-preserved zero</b> on input
      and output, which is D3D11's functional requirement rather than a
      shortcut. Both instantiate the same units —
      <span class="chip">rv_fpu</span> for the fused multiply-add and
      <span class="chip">khs_fp32_sfu</span> for the four seeds — so a SIMD
      float result and a SIMT float result agree element for element, and only
      the addressing differs. That single-sourcing is what makes the per-unit
      DSP and BRAM costs identical on the two cores by construction: a float
      unit is 2 DSP48, and the seed capability adds 1 DSP48 and 1.5 BRAM to a
      unit that carries it.
    </p>

    <Callout
      kind="open"
      title="The float tier has no kernel evidence on either class"
    >
      <p>
        Nothing under <span class="chip">compiler/</span> references either PE,
        and the SIMD PE's only kernel library —
        <span class="chip">tests/pe/tools/rv_simd_kernels.py</span> — contains
        <b>zero float instructions</b>. The integer features each have a paired
        kernel representing real work; the float tier has a component bench and
        a golden model and <b>no workload</b>. The SIMT side runs float shaders,
        which exercise the datapath and the pass walk, but a shader written to
        exercise the RTL is not a workload either.
      </p>
      <p>
        So any statement that the float tier is validated means
        <i>validated against a model</i>, and a page presenting it as validated
        by use is overclaiming. Whether either PE has a float workload at all is
        a <b>compiler</b> question and not an RTL one.
      </p>
    </Callout>

    <h2 class="doc-h2">The instruction encoding</h2>
    <p class="doc-p">
      RISC-V reserves four custom opcode majors and no more. Both classes draw
      from that one pool, for the instructions RISC-V has not already
      standardised — which is why RV32M costs none of them.
    </p>

    <BitField
      :fields="rtype"
      caption="The R-type shape both classes use. funct3 names the group and funct7 the operation inside it, so an R-type group holds up to 128 operations — the scarcity is in format-distinct instructions, not in instructions"
    />

    <SpecTable
      :cols="fields.cols"
      :rows="fields.rows"
      caption="The owner column is what tells a reader which bits are theirs. Anything marked fixed is protocol between the assembler, the golden model, the RTL decode header and — on the SIMD side — the C intrinsic header, all four of which are generated from or checked against one field table."
    />

    <SpecTable
      :cols="majors.cols"
      :rows="majors.rows"
      caption="Every major, including the unallocated slots and the declared-but-unimplemented ones. custom-3's funct3 space is deliberately not filled: it is the only I-type room the machine has left, and an I-type instruction is the one shape that cannot be squeezed in anywhere else."
    />

    <Callout
      kind="trap"
      title="An encoding test proves nothing about execution"
    >
      <p>
        An instruction can round-trip perfectly through the assembler, the
        model, the decode header and the intrinsic header, set a write enable,
        and have <b>no datapath behind it</b> — the fault checks wired, a
        parameter in a cost report, a register written, and none of it touching
        the result. Two instances have been fixed on the SIMD PE: a converter
        group whose registered decode signals had no branch in the
        <b>result mux</b>, so its instructions wrote the integer lane's output;
        and an accumulator whose float units were instantiated without
        connecting their operation port, so the tool tied it to opcode zero — a
        pass-through — and the tier neither multiplied nor accumulated.
      </p>
      <p>
        <b>No bench built from the decode can catch it.</b> The generator, the
        golden model and the RTL are written from one instruction table, so a
        feature missing from the <i>datapath</i> is missing from all three
        consistently and they agree with each other about nothing being wrong.
        Three checks find the whole class: follow the decode register to the
        <b>result</b> and count its reads; grep the synthesis log for
        <span class="chip">[Synth 8-7071]</span> and keep only the
        <b>inputs</b>, because an unconnected input is tied to zero and silently
        becomes a legal-looking value; and <b>read the area column</b> — those
        accumulator units synthesised at roughly a fifth of what a working unit
        costs, and a full multiply-add cannot be that small.
      </p>
    </Callout>

    <h2 class="doc-h2">Choosing a configuration</h2>

    <p class="doc-p">
      The widths are independent, so a part is configured by workload rather
      than by picking a profile. Two properties guide it, and both are measured
      rather than assumed. <b>Feature count dominates unit count:</b> each PE
      pays a fixed base cost once — the core, the windows, the L1, the
      requestor, the fabric port — so meeting a throughput target with fewer,
      wider elements is cheaper than with more, narrower ones.
      <b>A feature at zero is still available:</b> a width of 0 removes hardware
      from one build and the knob remains, so no configuration is a deletion.
      And because every feature is a parameter,
      <b>the PEs of one mesh need not be the same build</b> — giving the seed
      units only to the PEs that will run transcendental-heavy work buys more
      total float width per LUT than widening every PE equally.
    </p>

    <ol class="doc-p list-decimal pl-5 space-y-1">
      <li>
        <b>Pick the class from the shape of the work</b>, not from a rate. The
        distinction is not lane count — it is
        <b>whether the lanes may disagree</b>, on an address or on which side of
        a branch they are executing.
      </li>
      <li>
        <b>Set the integer width to the memory granule and leave it there.</b>
        Eight 32-bit slots is one flit payload and one memory-agent entry, and
        that alignment is the strongest machine-level property in either design.
      </li>
      <li>
        <b>Set the float count from arithmetic demand.</b> It costs an issue
        interval of <span class="chip">elements / units</span> and nothing else
        — except on the SIMD accumulator, where it also changes the answers.
      </li>
      <li>
        <b>Set the seed count to the float count, or to one.</b> The middle of
        the range is the one place not to sit.
      </li>
      <li>
        <b>Price the configuration from the marginal table</b> before
        synthesising it, and read the estimate as a <b>ceiling</b>. The model is
        additive by construction and cannot see two features that share control
        logic, so a stripped build comes in <i>cheaper</i> than it predicts,
        never dearer. On rows that moved seven knobs at once the error has
        reached 44%.
      </li>
      <li>
        <b
          >Synthesise it and read the configuration line back off the
          <span class="chip">synth_design</span> command in the run log.</b
        >
        A knob that is parsed but not applied produces a row that varies in its
        tag and not in its netlist.
      </li>
      <li>
        <b>Do not add rows together across tops.</b> A core contains a unit and
        a PE contains both, so a column of separate out-of-context runs cannot
        be summed — and a ladder whose top is one submodule
        <b>cannot see a path that leaves it</b>.
      </li>
    </ol>

    <Callout kind="open" title="What the flow does not answer">
      <p>
        <b>Whether a mesh of these PEs fits, and at what clock.</b> Every figure
        either class has is out-of-context synthesis of <i>one</i> PE. The
        interconnect and the system node are budgeted for but have never been
        co-placed with a dozen PEs, and the clock is what would move. No mesh of
        these PEs has been placed.
      </p>
      <p>
        <b>How many PEs one system node will carry.</b> The measured shape is
        that one memory agent serves up to four PEs, and that sharing it costs
        <b>+13.7%</b> on a fixed compute-bound program — measured while the
        three neighbours run the heaviest memory work in the suite, including a
        copy whose source and destination sit exactly one cache size apart so
        every access conflict-misses. That is a bound on the agent, not on
        fabric.
      </p>
      <p>
        <b>Whether the SIMD/SIMT frequency asymmetry is real.</b> The SIMD rows
        span 316.8–367.8 MHz and the SIMT rows 343.1–385.2, and a single row is
        not a property. Settling it needs a placed run, which is outside an
        out-of-context campaign.
      </p>
    </Callout>

    <h2 class="doc-h2">Fixed protocol, addon, convention, or yours</h2>
    <SpecTable :cols="kinds.cols" :rows="kinds.rows" />

    <h2 class="doc-h2">What this project does not own</h2>
    <SpecTable :cols="notOwned.cols" :rows="notOwned.rows" />
  </DocPage>
</template>
