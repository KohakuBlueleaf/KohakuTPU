<script setup>
/* Three processing-element classes on one mesh.
 *
 * PROVENANCE. Every resource or frequency figure names its part, its tool, its
 * requested period, its -flatten_hierarchy setting and the script that produced
 * it. The RV32 PE row is scripts/tcl/ooc_rv_pe.tcl at -flatten_hierarchy none,
 * transcribed from docs/arch/cpu/rv32-pe/performance.md. The two wide rows are
 * one frozen tree of a scripts/py/khs_sweep.py campaign at rebuilt, tabulated in
 * docs/projects/kohakumpe/unit-counts.md. The three flows differ, so the rows do
 * not subtract, and that is stated on the page rather than left to the reader.
 */

/* ------------------------------------------------------------- lineage */
const lineage = {
  nodes: [
    {
      id: "core",
      x: 12,
      y: 0,
      w: 13,
      h: 4.6,
      label: "rv_core — the framework's RV32IM controller",
      sub: "six register boundaries · one stall rule · mul, mulh, mulhsu, mulhu · NO divide, NO float",
      accent: true,
    },
    {
      id: "cpu",
      x: 0,
      y: 8.5,
      w: 11,
      h: 4.4,
      label: "RV32 PE",
      sub: "rv_pe with SIMD_EN = 0",
    },
    {
      id: "simd",
      x: 13,
      y: 8.5,
      w: 11,
      h: 4.4,
      label: "SIMD PE",
      sub: "rv_pe + khs_unit at the execute stage",
    },
    {
      id: "simt",
      x: 26,
      y: 8.5,
      w: 11,
      h: 4.4,
      label: "SIMT PE",
      sub: "kht_pe — a rebuild on the same shape, not a parameter on it",
    },
    {
      id: "cpuw",
      x: 0,
      y: 16,
      w: 11,
      h: 4.4,
      label: "one 32-bit operation",
      sub: "including one 33×33 signed product",
    },
    {
      id: "dspw",
      x: 13,
      y: 16,
      w: 11,
      h: 4.4,
      label: "one packed vector",
      sub: "8 slots of 32 bits, ONE address stream",
    },
    {
      id: "gpuw",
      x: 26,
      y: 16,
      w: 11,
      h: 4.4,
      label: "8 threads",
      sub: "own path, own address, own product",
    },
  ],
  edges: [
    { from: "core:b", to: "cpu:t", dir: "v", label: "as it is" },
    { from: "core:b", to: "simd:t", dir: "v", accent: true, label: "SIMD_EN" },
    { from: "core:b", to: "simt:t", dir: "v", label: "rebuild" },
    { from: "cpu:b", to: "cpuw:t", dir: "v" },
    { from: "simd:b", to: "dspw:t", dir: "v", accent: true },
    { from: "simt:b", to: "gpuw:t", dir: "v", accent: true },
  ],
  groups: [
    { x: -1.2, y: 14.7, w: 39.4, h: 7, label: "what ONE instruction drives" },
  ],
};

/* ------------------------------------------------------- what each ships */
const classes = {
  cols: [
    { key: "cls", label: "PE class" },
    { key: "arith", label: "The arithmetic it ships with" },
    { key: "lut", label: "LUT", align: "right", mono: true },
    { key: "ff", label: "FF", align: "right", mono: true },
    { key: "bram", label: "BRAM", align: "right", mono: true },
    { key: "dsp", label: "DSP48", align: "right", mono: true },
    { key: "fmax", label: "Fmax", align: "right", mono: true },
    { key: "fl", label: "flatten", align: "right", mono: true },
  ],
  rows: [
    {
      cls: "<b>RV32 PE</b><br><code>rv_pe</code>, <code>SIMD_EN = 0</code>",
      arith:
        "RV32<b>IM</b> — one 32-bit datapath with a multiplier. <code>mul</code>, <code>mulh</code>, <code>mulhsu</code> and <code>mulhu</code> are always built; <code>div</code>, <code>divu</code>, <code>rem</code> and <code>remu</code> are decoded and <b>refused by name</b>. No vector tier",
      lut: "<b>2,586</b>",
      ff: "3,844",
      bram: "9",
      dsp: "<b>4</b>",
      fmax: "363.5<br><span class='opacity-60'>+0.582 ns · met</span>",
      fl: "none",
      _tone: "good",
    },
    {
      cls: "<b>SIMD PE</b><br>8 slots · 8 integer lanes · 4 float units",
      arith:
        "the base core's, plus <b>packed int8 / int16 / int32</b> — add, subtract, saturating add and subtract, min, max, <code>vmul</code> — a packed shifter, a cross-lane permute, reduction trees, and an <b>IEEE binary32</b> float tier. No seeds and no accumulator in this row",
      lut: "<b>15,682</b>",
      ff: "9,836",
      bram: "13",
      dsp: "56",
      fmax: "349.3",
      fl: "rebuilt",
    },
    {
      cls: "<b>SIMT PE</b><br>8 threads · 16 waves · 8 float units",
      arith:
        "the base ISA, <b>per thread</b>: <code>add x5, x3, x4</code> is <code>x5[lane] = x3[lane] + x4[lane]</code>. Plus an active mask, a divergence stack, a subgroup butterfly, a banked shared memory, per-thread RV32M and an <b>IEEE binary32</b> float tier. No seeds in this row",
      lut: "<b>19,461</b>",
      ff: "17,268",
      bram: "30.5",
      dsp: "48",
      fmax: "361.0",
      fl: "rebuilt",
    },
  ],
};

/* ------------------------------------------------------------ the widths */
const granule = `   SLOTS / THREADS   32-bit elements the machine is built around    8, FIXED
   UNITS             how many are BUILT for a feature               0, 1, 2, 4, 8 or -1
   PASSES            slots / units, one issued per cycle            derived

   8 x 32 bit  =  256 bit  =  one native memory entry  =  one flit payload

   integer width  <-  the memory granule (coalescing)       FIXED by the granule
   float width    <-  arithmetic demand (throughput vs LUT)  A KNOB`;

/* --------------------------------------------------------- the multiply */
const holdBroken = {
  rows: [
    { name: "EX", kind: "bus", values: ["mul", "mul", "mul", "next"] },
    { name: "hold EX", kind: "bit", values: [1, 1, 1, 0] },
    { name: "MEM", kind: "bus", values: ["prev", "prev", "prev", "mul"] },
    { name: "MEM valid", kind: "bit", values: [1, 1, 1, 1], mark: [1, 2] },
    {
      name: "retires",
      kind: "text",
      values: ["prev", "prev", "prev", "mul"],
      mark: [1, 2],
    },
  ],
  notes: [
    {
      cycle: 0,
      text: "The multiply holds EX for three cycles. The MEMORY stall is safe to freeze the MEM-valid bit under, because it stops EX and MEM together — so the same treatment looks safe here.",
    },
    {
      cycle: 1,
      text: "It is not. This hold stops EX alone, so MEM keeps advancing — and with its valid bit frozen the instruction sitting in it retires again, with the same correct answer, once per held cycle.",
      tone: "bad",
    },
    {
      cycle: 3,
      text: "Every answer written is right and the retired-instruction count has run away. There is no wrong value anywhere to trip an assertion on, which is why this presents as a counter that disagrees with the program rather than as a failure.",
      tone: "bad",
    },
  ],
};

const holdFixed = {
  rows: [
    { name: "EX", kind: "bus", values: ["mul", "mul", "mul", "next"] },
    { name: "hold EX", kind: "bit", values: [1, 1, 1, 0] },
    { name: "MEM", kind: "bus", values: ["prev", null, null, "mul"] },
    { name: "MEM valid", kind: "bit", values: [1, 0, 0, 1], mark: [3] },
    { name: "retires", kind: "text", values: ["prev", "", "", "mul"] },
  ],
  notes: [
    {
      cycle: 1,
      text: "A hold that stops only the upper stage MUST send the lower stage a bubble. MEM advances into an invalid slot and retires nothing.",
      tone: "good",
    },
    {
      cycle: 3,
      text: "Three stall cycles for the multiply. Against them: a software multiply on the EASY case — int8 operands, which unroll to eight shift-add steps — measured about 54 cycles each on the full-system bench, and a general 32×32 unrolls to far more.",
      tone: "good",
    },
  ],
};

/* -------------------------------------------------------------- RV32M */
const mEnc = [
  { name: "funct7", bits: 7, value: "0000001", accent: true },
  { name: "rs2", bits: 5 },
  { name: "rs1", bits: 5 },
  { name: "funct3", bits: 3, value: "which of eight", accent: true },
  { name: "rd", bits: 5 },
  { name: "opcode", bits: 7, value: "0110011 — OP" },
];

const mFields = {
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
      p: "[6:0] = 0110011",
      o: "<b>RISC-V.</b> The standard OP major, <b>not</b> one of the four custom ones — which is why RV32M costs none of the pool and a compiler emits it with <code>-march=rv32im</code> and nothing else",
      _tone: "good",
    },
    {
      f: "funct7",
      w: "7",
      p: "[31:25] = 0000001",
      o: "<b>RISC-V.</b> The M extension's selector inside the register-register group that already existed",
    },
    {
      f: "funct3[2]",
      w: "1",
      p: "[14]",
      o: "<b>this implementation's refusal.</b> Set is exactly <code>div</code> / <code>divu</code> / <code>rem</code> / <code>remu</code>. <code>rv_id.v</code> makes it a term of <i>illegal</i> and never a term of the multiply enable, so the four divides fault <b>by name</b> rather than by falling off the end of a case",
      _tone: "warn",
    },
    {
      f: "funct3[1:0]",
      w: "2",
      p: "[13:12]",
      o: "<b>RISC-V.</b> 0 <code>mul</code> · 1 <code>mulh</code> · 2 <code>mulhsu</code> · 3 <code>mulhu</code>. <code>rv_ex.v</code> reads these two bits three ways and builds nothing else from them",
    },
    {
      f: "rd · rs1 · rs2",
      w: "5 each",
      p: "[11:7] · [19:15] · [24:20]",
      o: "the instruction. On the SIMT PE these name the <b>per-thread</b> file, so one <code>mul</code> is <code>LANES</code> products",
    },
  ],
};

const mDecode = {
  cols: [
    { key: "i", label: "Instruction", mono: true },
    { key: "f", label: "funct3[1:0]", mono: true, align: "right" },
    { key: "a", label: "a_sgn — rs1", mono: true, align: "right" },
    { key: "b", label: "b_sgn — rs2", mono: true, align: "right" },
    { key: "h", label: "hi_sel", mono: true, align: "right" },
  ],
  rows: [
    {
      i: "mul",
      f: "00",
      a: "1",
      b: "<b>0</b>",
      h: "0",
      _tone: "warn",
    },
    { i: "mulh", f: "01", a: "1", b: "<b>1</b>", h: "1", _tone: "good" },
    { i: "mulhsu", f: "10", a: "1", b: "0", h: "1" },
    { i: "mulhu", f: "11", a: "0", b: "0", h: "1" },
  ],
};

/* ------------------------------------------------------------- routing */
const routing = {
  cols: [
    { key: "work", label: "The work" },
    { key: "pe", label: "Goes to" },
    { key: "why", label: "Because" },
  ],
  rows: [
    {
      work: "deciding what happens next — sequencing kernels, walking descriptors, reacting to completions",
      pe: "<b>RV32 PE</b>",
      why: "one 32-bit datapath and a program counter is the whole requirement, and a hand-written state machine does the job until the policy changes",
    },
    {
      work: "vertex transform, blending, colour-space conversion — one address stream, every element treated the same, arithmetic dense",
      pe: "<b>SIMD PE</b>",
      why: "one instruction drives eight 32-bit slots, and the packed element types put four int8 pairs through one native carry chain per slot. There is nothing to diverge and nothing to serialise",
    },
    {
      work: "a fragment shader where lane 3 takes the <code>if</code> and lane 4 takes the <code>else</code>; a texture fetch whose address is per lane",
      pe: "<b>SIMT PE</b>",
      why: "an active mask, a divergence stack and an address per lane — none of which the SIMD tier anticipates, and adding them there would cost every uniform kernel",
    },
    {
      work: "a fixed dataflow with operands resident across many passes and no control flow at all",
      pe: "<b>not a PE at all</b>",
      why: "that is a systolic array. A PE is scalar control, uniform width, or divergence, and an array is none of the three",
    },
  ],
};

/* ---------------------------------------------------------- opcode map */
const majors = {
  cols: [
    { key: "major", label: "Major", mono: true },
    { key: "op", label: "Opcode", mono: true },
    { key: "owner", label: "Owner" },
    { key: "carries", label: "Carries" },
  ],
  rows: [
    {
      major: "custom-0",
      op: "0x0B",
      owner: "<b>SIMD PE</b>",
      carries:
        "the packed-integer tier: vector load and store, packed arithmetic, bitwise, immediate shifts, moves and reductions, and the cross-lane permute",
    },
    {
      major: "custom-0<br><code>funct3 = 5</code>",
      op: "0x0B",
      owner: "<b>SIMD PE</b>",
      carries:
        "<b>reserved and unmapped.</b> The retired integer dot unit and its accumulator lived here — <code>vdot</code>, <code>vdotn</code>, <code>vaccz</code>, <code>vaccrd</code>, <code>vaccwr</code>, and the <code>MULS</code> and <code>DOT_DSP</code> knobs that sized them. Keeping the group unmapped is what makes a binary built for them <b>fault</b>",
      _tone: "warn",
    },
    {
      major: "custom-1",
      op: "0x2B",
      owner: "<b>SIMD PE</b>",
      carries:
        "the float tier: the accumulator group, the converters, the elementwise base and the four seeds. <code>FLOAT_LANES = 0</code> leaves the major unmapped, so a float instruction faults at the opcode",
    },
    {
      major: "custom-1<br><code>funct3 = 1</code>",
      op: "0x2B",
      owner: "<b>SIMD PE</b>",
      carries:
        "<b>declared and not built.</b> <code>vfredsum.f32</code> encodes and <b>faults</b>. The golden model implements it, so the model is ahead of the RTL here — and returning slot 0 alone would be the plausible wrong answer a refusal exists to prevent",
      _tone: "bad",
    },
    {
      major: "custom-2",
      op: "0x5B",
      owner: "<b>SIMT PE</b>",
      carries:
        "the R-type groups: scalar ALU, scalar/vector moves, divergence, subgroup, the uniform-base and lane-linear memory forms, and the float group",
    },
    {
      major: "custom-2<br><code>funct3 = 6, 7</code>",
      op: "0x5B",
      owner: "<b>SIMT PE</b>",
      carries: "<b>unallocated.</b> Reserved, and they fault",
    },
    {
      major: "custom-3",
      op: "0x7B",
      owner: "<b>SIMT PE</b>",
      carries:
        "the I-type groups: scalar immediate ALU and the two uniform branches. Its <code>funct3</code> space is deliberately <b>not</b> filled",
    },
    {
      major: "OP",
      op: "0x33",
      owner: "<b>RISC-V — not in the pool</b>",
      carries:
        "<code>funct7 = 0000001</code> is RV32M, on every RV32 core in the tree. The divides are decoded and refused",
      _tone: "good",
    },
    {
      major: "AMO",
      op: "0x2F",
      owner: "<b>nobody</b>",
      carries:
        "<b>not in the legal opcode set at all.</b> An <code>amo*</code> word raises an illegal-instruction fault rather than being decoded into something adjacent — there are no atomics on either class",
      _tone: "bad",
    },
  ],
};

const mulWhere = {
  cols: [
    { key: "c", label: "What a multi-cycle unit costs a short in-order core" },
    { key: "g", label: "The same thing on a barrel-scheduled core" },
  ],
  rows: [
    {
      c: "<b>The result mux lengthens the distance-1 forwarding path.</b> The multiply result joins through the same wire that feeds both the memory-stage value and the forwarding network, and that network ends at the register the core's critical path once terminated at — so adding a case to that mux was one more level in exactly the wrong place",
      g: "<b>That path does not exist.</b> With as many resident wave contexts as the pipeline is deep, no two in-flight instructions share a wave, so there is no forwarding network and no distance-1 interlock to lengthen",
      _tone: "good",
    },
    {
      c: "<b>A multi-cycle result needs a stall term, and stall terms fan out across the whole front end.</b> That geometry is visible in the shipped core from a different source: an address decode reaching a stall term that reaches the front end's pipeline registers",
      g: "<b>The flag was already there.</b> A wave with a float in flight is skipped by the scheduler through one pending bit per wave; a multiply sets and clears the <i>same</i> bit and retires through the <i>same</i> write port, padded to the float tier's exact latency so the two can never want it on one cycle",
      _tone: "good",
    },
    {
      c: "<b>The option not taken is a scoreboard</b> letting the multiply retire out of order. The hazard unit is the whole of this core's complexity budget — three sources by position, one stall rule, nothing else — and a scoreboard ends that invariant for one instruction",
      g: "<b>Also not taken, for the opposite reason.</b> There is no per-register scoreboard because there does not need to be one: a per-wave pending bit restores the barrel-scheduling invariant that a multi-cycle unit breaks",
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
      t: "the compute-unit port every class presents, and the kick and completion across it",
      c: "<b>fixed protocol</b>, and the framework's",
    },
    {
      t: "which custom major belongs to which class",
      c: "<b>fixed protocol.</b> The allocation is recorded outside both classes' ISA modules, because a table one tier owns is not an authority the other can check itself against",
    },
    {
      t: "RV32M at its standard encoding, and the four divides faulting",
      c: "<b>fixed protocol</b> of the base core. There is no <code>HAS_M</code>, no RV32I build, and nothing to configure",
    },
    {
      t: "a feature at zero faults rather than computing something plausible",
      c: "<b>fixed protocol</b> of both wide classes",
    },
    {
      t: "<code>khs_unit</code> and <code>khs_scalar_decode</code> behind the framework's <code>SIMD_EN</code>",
      c: "<b>customizable addon</b> — a slot the framework names behind a parameter that is 0 by default",
    },
    {
      t: "every unit count on either wide class",
      c: "<b>yours.</b> Range and meaning are set by this project's own specification, not by the framework's parameter rule",
    },
    {
      t: "which class a piece of work goes to",
      c: "<b>convention.</b> The table above is how the shipped parts happen to agree; nothing enforces it",
    },
  ],
};

const notOwned = {
  cols: [
    { key: "n", label: "Not settled here" },
    { key: "w", label: "Where it is" },
  ],
  rows: [
    {
      n: "how the base core is built — the six register boundaries, the hazard rules, the predictor, the two L1s, the requestor",
      w: "the framework's RV32 controller PE. Both wide classes inherit it and neither may change it",
    },
    {
      n: "what a SIMD width costs, and what a SIMT gate costs",
      w: "each class's own pages, where the campaign and the tree behind each row are named",
    },
    {
      n: "whether a mesh of these fits, and at what clock",
      w: "<b>nowhere yet.</b> Every figure either wide class has is out-of-context synthesis of one PE, and no mesh of them has been placed",
    },
    {
      n: "how many PEs one system node carries",
      w: "the framework's system node. The measured shape is that one memory agent serves up to four PEs, and sharing it costs <b>+13.7%</b> on a fixed compute-bound program",
    },
  ],
};
</script>

<template>
  <DocPage
    title="SIMD, SIMT and what is not a PE"
    summary="Three classes of processing element share one mesh and one base core. What arithmetic each ships, which work routes to which, where the room in the opcode map actually is, and why the multiplier is nearly free on one of them and was expensive on another."
    domain="simt"
    status="measured"
    source="src/kohakuaccel/pe/rv32/ · src/kohakumpe/ · docs/arch/cpu/rv32-pe/ · docs/projects/kohakumpe/configurable-widths.md"
  >
    <h2 class="doc-h2">What this page settles</h2>
    <p class="doc-p">
      Three things. <b>Which class a piece of work belongs on</b>, which is a
      question about the shape of the work and never about which is faster.
      <b>Where the multiply is</b>, which is on all three and is not a knob on
      any of them. And <b>who owns which opcode major</b>, which is the one
      allocation that has to be settled centrally because four is all RISC-V
      reserves.
    </p>
    <p class="doc-p">
      The alternative that was rejected is a single configurable wide class that
      stretches from uniform to divergent. It fails on cost rather than on
      taste: an active mask, a divergence stack and a per-lane address path are
      hardware that a uniform kernel cannot use and would still pay for, and the
      measurement below says the base machines differ by 543 LUT in the
      <i>other</i> direction. Two classes and one shared base core is what buys
      the uniform case its cheapness without giving up the divergent one.
    </p>

    <Fig
      caption="The lineage. The SIMD PE is a parameter on the base core — SIMD_EN elaborates the extension or it does not, a generate rather than a zero width. The SIMT PE is a rebuild on the base core's shape rather than a parameter on it: it keeps the six register boundaries and the hazard style because those are what close at this clock, and changes which register file an ordinary RV32I opcode addresses. RV32IM is the base ISA on all three, per-thread included."
      zoom
    >
      <BlockDiagram
        :nodes="lineage.nodes"
        :edges="lineage.edges"
        :groups="lineage.groups"
      />
    </Fig>

    <p class="doc-p">
      Every class here is a compute unit first: one local port on the mesh, one
      instruction FIFO, the same control registers a driver enumerates any unit
      through. A driver reads one without knowing it is a processor. The only
      thing different is what it does with an instruction — an instruction flit
      is a <b>kick</b> that starts a program, and the unit retires when the
      program halts.
    </p>

    <h2 class="doc-h2">What each class ships, and what it costs</h2>

    <p class="doc-p">
      A LUT total without the arithmetic beside it says nothing: two of these
      three carry a float tier, all three carry an integer multiplier, and none
      of that is visible in a number. The arithmetic is the first column for
      that reason.
    </p>

    <SpecTable
      :cols="classes.cols"
      :rows="classes.rows"
      caption="Three whole assembled PEs on xcvu13p-fhgb2104-2L-e with Vivado 2024.2, out-of-context SYNTHESIS at a 3.333 ns request, -directive default. Nothing placed, nothing routed. Figures are CLB LUT sites. The RV32 PE row is scripts/tcl/ooc_rv_pe.tcl, top rv_pe, at -flatten_hierarchy none. The two wide rows are the reference rows of one frozen tree of a scripts/py/khs_sweep.py campaign at rebuilt, tabulated in docs/projects/kohakumpe/unit-counts.md."
    />

    <Callout
      kind="trap"
      title="These three rows line up in columns and do not subtract"
    >
      <p>
        <b>Two different flatten settings.</b> The RV32 row is
        <span class="chip">-flatten_hierarchy none</span>, which is what its
        script asks for, and the two wide rows are
        <span class="chip">rebuilt</span>, which is what the ship synthesises at
        because nothing in the build scripts sets the setting on the ship's run.
        On this same RV32 PE the two settings differ by
        <b>+720 LUT and −4.1 MHz</b> for <span class="chip">none</span>. On the
        assembled SIMT PE at one target, <span class="chip">none</span> read
        <b>636 LUT high</b>. And the gap is
        <b>configuration-dependent</b> — on the SIMD PE it measured 647 LUT at
        one setting of one knob and 243 at another, because at the first setting
        all of the difference was the tool inferring DSP48 post-adders the RTL
        placed explicitly at the second. It cannot be applied as a correction.
      </p>
      <p>
        <b>Two different source trees.</b> The wide rows predate the float
        tier's rebuild in binary32 and the removal of the integer dot unit;
        <b>no absolute total for either wide class describes a PE that can be
        built from the RTL as it now stands.</b> What survives is the shape —
        what a marginal unit costs, and which knobs are not levers — and those
        are on each class's own pages.
      </p>
      <p>
        <b>The symptom of ignoring this is a delta that reads as a design
        change.</b> "The extension costs 13,096 LUT" is the RV32 row subtracted
        from the SIMD row, and it is two flows, two trees and two configurations
        differenced into one number.
      </p>
    </Callout>

    <h2 class="doc-h2">Every compute feature is a width</h2>

    <Callout kind="rule" title="A count, never a boolean, and 0 means not built">
      <p>
        There is no boolean "has a float tier" and no boolean "has a
        multiplier". Each feature that has a width is a <b>unit count</b> with
        legal values 0, 1, 2, 4, 8 and −1 for full rate, and the count sets how
        many cycles an operation takes, <b>never what the instruction set
        contains</b>. The same binary and the same golden memory image run at
        every width. A count of <span class="chip">0</span> means the hardware
        is not instantiated and every encoding needing it <b>faults at
        decode</b>, because a feature that decodes with no datapath returns a
        plausible wrong answer.
      </p>
      <p>
        Two consequences follow that a reader will not guess.
        <b>The integer ALU is one IM unit</b> — add, subtract, compare, bitwise
        and multiply through one operand path and one result path, rather than
        an ALU beside a multiplier array with a dispatch mux between them —
        because multiplexers are the expensive primitive on this fabric and two
        units serving one issue port need a mux in front and a mux behind, both
        wider than the logic they arbitrate. So the multiply count is
        <b>not a knob on either wide class</b>: it follows
        <span class="chip">ILANES</span> on SIMD and
        <span class="chip">LANES</span> on SIMT. And
        <b>there is no dot-product unit</b>: a dot product is a packed multiply
        followed by a reduction, and a part that needs a high rate carries
        matrix units instead.
      </p>
    </Callout>

    <div
      class="font-mono kt-text-caption whitespace-pre text-warm-700 dark:text-warm-300 leading-6 overflow-x-auto my-3"
    >
      {{ granule }}
    </div>

    <Callout
      kind="rule"
      title="The integer width is the address path; the float width is not"
    >
      <p>
        A contiguous 32-bit load by eight slots or eight threads is exactly one
        memory read request, one native memory entry, one flit payload — the
        strongest machine-level alignment in either design. Narrow the integer
        side and every coalesced load becomes two or more requests,
        <b>for every kernel, permanently</b>. That is why the vector is 256 bits
        in every configuration, and why fewer integer lanes than float units is
        rejected outright: it starves addressing to feed arithmetic, so every
        memory operation serialises while the float units wait.
      </p>
      <p>
        Float has no such tie. A unit count below the element count costs an
        <b>issue interval</b> of <span class="chip">elements / units</span> and
        nothing else — four float units over eight slots is one vector every two
        cycles, which is a slower machine and not a narrower one. Latency is the
        cheapest thing to trade in a datapath that is already six cycles deep.
        <b
          >The one exception is the SIMD float accumulator, where the unit count
          is architectural</b
        >: an element's accumulate chain becomes a shorter strided subset of the
        partials, and float addition does not associate, so a build with a
        different count computes different answers on the same program.
      </p>
    </Callout>

    <h2 class="doc-h2">Where the multiply is</h2>

    <p class="doc-p">
      <b>Every RV32 core in this tree is RV32IM.</b>
      <span class="chip">mul</span>, <span class="chip">mulh</span>,
      <span class="chip">mulhsu</span> and <span class="chip">mulhu</span> are
      always built — on the RV32 PE, on the SIMD PE's scalar half, and on the
      SIMT PE's per-thread file alike — and the four DSP48 in the RV32 PE's row
      above <i>are</i> that multiplier. There is no
      <span class="chip">HAS_M</span> and nothing to configure. Address
      arithmetic is what wants it: a control processor computing descriptor
      addresses, strides and mesh coordinates was the core most obviously
      missing it, and on the SIMT side a pixel index is
      <span class="chip">y * width + x</span> and a mip or Morton address is a
      multiply, each of which would otherwise be a software shift-add chain
      running on every lane of every fragment.
    </p>

    <BitField
      :fields="mEnc"
      caption="RV32M sits at its standard RISC-V encoding inside the register-register group that already existed. No new opcode major was spent on it, on either class"
    />

    <SpecTable
      :cols="mFields.cols"
      :rows="mFields.rows"
      caption="Read out of src/kohakuaccel/pe/rv32/core/rv_id.v and rv_ex.v. The owner column is what says which bits are RISC-V's and which are this implementation's."
    />

    <SpecTable
      :cols="mDecode.cols"
      :rows="mDecode.rows"
      caption="One 33×33 signed product serves all four forms, in three registered stages, and only the operand extension and which half is returned differ. rv_ex.v builds exactly three wires from funct3[1:0]: a_sgn = (f3[1:0] != 3), b_sgn = (f3[1:0] == 1), hi_sel = (f3[1:0] != 0)."
    />

    <Callout kind="trap" title="Only mulh sign-extends rs2">
      <p>
        <span class="chip">b_sgn</span> is
        <span class="chip">(x_f3[1:0] == 1)</span>, so
        <span class="chip">mulh</span> is the <b>only</b> form that sign-extends
        the second operand. <span class="chip">mul</span> reads
        <span class="chip">rs2</span> <b>unsigned</b> — and that is correct
        rather than a bug, because the low 32 bits of a product do not depend on
        how either operand was extended. The three sign variants are
        <b>three different readings of one product</b>, not three multipliers.
      </p>
      <p>
        The symptom of getting it backwards is narrow and easy to miss: every
        <span class="chip">mul</span> is still right, and only
        <span class="chip">mulhsu</span> and <span class="chip">mulhu</span>
        return a wrong high half — on operands whose top bit is set, which a
        kernel using the multiplier for small indices never produces. It is a
        corner a workload cannot reach, so it has to be pinned by a test written
        against the specification.
      </p>
    </Callout>

    <Callout
      kind="rule"
      title="A multiply hold stops EX alone, so it MUST bubble the stage below"
    >
      <p>
        The multiplier is <b>free-running</b>: the operands are frozen for the
        whole hold, so every capture takes the same value and gating would buy
        nothing but a mux. What the hold must not do is stop the memory stage as
        well. A stall that stops two adjacent stages together may safely freeze
        the register between them — that is what the <i>memory</i> stall does —
        and a hold that stops only the upper stage must not, because the lower
        stage continues to advance.
      </p>
      <p>
        <b>The counter also resets on advance.</b> Back-to-back multiplies hold
        the multiply-in condition across the stage boundary, so without an
        explicit reset the second would retire carrying the first one's product.
      </p>
    </Callout>

    <WaveTrace
      :rows="holdBroken.rows"
      :notes="holdBroken.notes"
      variant="broken"
      label="MEM-valid frozen under the hold — one retirement per held cycle, every answer correct"
    />
    <WaveTrace
      :rows="holdFixed.rows"
      :notes="holdFixed.notes"
      variant="fixed"
      label="A bubble into MEM — three stall cycles, one retirement"
    />

    <Callout
      kind="rule"
      title="div and rem are refused, and the refusal has arithmetic behind it"
    >
      <p>
        An iterative divider is priced at about <b>35 cycles</b> against a
        software routine's 60–80 — a 2× on a rare instruction — where
        <span class="chip">mul</span> replaces roughly
        <b>54 cycles</b> of shift-add on the easy case, measured on the
        full-system bench against a build without the multiplier. Once
        <span class="chip">mul</span> exists, divide-by-a-constant
        strength-reduces to <span class="chip">mulhu</span>, which is the case
        software actually meets. A long sequential unit also does not suit a
        barrel-scheduled pipeline whose whole invariant is a fixed latency.
      </p>
      <p>
        The refusal is doubled deliberately: the assembler refuses the mnemonics
        outright <i>and</i> the decoder faults the encodings, so a divide cannot
        be assembled or executed by accident.
      </p>
    </Callout>

    <SpecTable
      :cols="mulWhere.cols"
      :rows="mulWhere.rows"
      caption="The same arithmetic in two pipelines. Neither column is an argument that one pipeline is better — they are the reason the same unit is a three-stall-cycle hold with a stall term on one core and rides machinery that already existed on the other."
    />

    <Callout
      kind="open"
      title="One frequency figure the RTL records has no run behind it"
    >
      <p>
        <span class="chip">rv_ex.v</span> records the multiplier's form as
        measured at <b>365.6 MHz</b>, and
        <b>no report in this tree backs that figure</b> — nothing measures the
        multiplier in isolation. Treat it as unverified. What <i>is</i> measured
        is the assembled unit with the multiplier built, which is the RV32 PE
        row above.
      </p>
    </Callout>

    <h2 class="doc-h2">What routes to which</h2>

    <SpecTable :cols="routing.cols" :rows="routing.rows" />

    <Callout kind="rule" title="Going wide is for work that is uniform">
      <p>
        The distinction between the two wide classes is <b>not lane count</b>.
        It is <b>whether the lanes may disagree</b> — on an address, or on which
        side of a branch they are executing. SIMD says no and is cheaper for it;
        SIMT says yes and pays for an active mask, a divergence stack and a
        lane-serialising load/store unit.
      </p>
      <p>
        So when lanes need different paths or different addresses, the answer is
        a <b>different machine, not a wider one</b>. That is the whole reason
        there are two wide classes rather than one configurable one, and the
        measurement that makes it a design rather than an assertion is that
        SIMD's base PE is <b>543 LUT cheaper</b> than SIMT's at matched widths —
        10,309 against 10,852 — even though SIMD's base carries the shifter, the
        permute network and thirty-two multipliers and SIMT's carries no
        multiplier inside the lane array at all.
      </p>
    </Callout>

    <h2 class="doc-h2">The opcode map</h2>

    <p class="doc-p">
      RISC-V reserves <b>four</b> custom opcode majors and no more. Every class
      draws from that one pool for the instructions RISC-V has not already
      standardised, so the allocation is recorded outside both classes' ISA
      modules.
    </p>

    <SpecTable
      :cols="majors.cols"
      :rows="majors.rows"
      caption="All four custom majors are spoken for. The last two rows are not part of the pool and are here because anyone counting remaining encoding room must not charge RV32M against it, and must not assume the atomics major is available to be claimed."
    />

    <Callout
      kind="rule"
      title="The scarcity is in format-distinct instructions, not in instructions"
    >
      <p>
        An R-type group has a 7-bit <span class="chip">funct7</span>, so it
        holds <b>128</b> operations. The extensions deliberately deferred today
        — atomics, texture, ray tracing — are all R-type shaped, and each fits
        in <span class="chip">funct7</span> room inside an existing group
        without touching the major allocation at all.
      </p>
      <p>
        <span class="chip">custom-3</span>'s
        <span class="chip">funct3</span> space is deliberately
        <b>not</b> filled. Leave it that way: it is the only I-type room the
        machine has left, and an I-type layout has no
        <span class="chip">funct7</span>, so an I-type group holds exactly one
        instruction. The SIMD tier hit that wall and spent a whole
        <span class="chip">funct3</span> on its vector load and another on its
        vector store.
      </p>
    </Callout>

    <Callout
      kind="note"
      title="Why the SIMD majors were not reused on the SIMT side"
    >
      <p>
        A SIMT build carries no SIMD tier, so
        <span class="chip">custom-0</span> and
        <span class="chip">custom-1</span> would be free in it. They are left
        alone anyway, for two reasons. A hypothetical PE carrying both classes
        would never have to renumber either set. And if the SIMT side ever
        exposes the packed-integer operations, it should reuse
        <b>the SIMD tier's own encodings unchanged</b>, so the assembler, the
        golden model and the disassembler share code instead of forking a second
        packed-integer table.
      </p>
    </Callout>

    <h2 class="doc-h2">Fixed protocol, addon, convention, or yours</h2>
    <SpecTable :cols="kinds.cols" :rows="kinds.rows" />

    <h2 class="doc-h2">What this page does not settle</h2>
    <SpecTable :cols="notOwned.cols" :rows="notOwned.rows" />

    <div class="grid gap-4 sm:grid-cols-3 mt-6">
      <RouterLink to="/framework/cpu" class="card-hover p-5 no-underline block">
        <div
          class="kt-text-title font-semibold text-warm-800 dark:text-warm-200 mb-1"
        >
          Processors on this framework
        </div>
        <p class="kt-text-caption text-warm-500 dark:text-warm-400 leading-6">
          The contract every core answers, and why there are two of them rather
          than one that stretches.
        </p>
      </RouterLink>
      <RouterLink to="/mpe/simd" class="card-hover p-5 no-underline block">
        <div
          class="kt-text-title font-semibold text-warm-800 dark:text-warm-200 mb-1"
        >
          SIMD PE
        </div>
        <p class="kt-text-caption text-warm-500 dark:text-warm-400 leading-6">
          Eight 32-bit slots, four int8 elements sharing one native carry chain,
          a cross-lane permute, and a binary32 float tier whose width is a knob.
        </p>
      </RouterLink>
      <RouterLink to="/mpe/simt" class="card-hover p-5 no-underline block">
        <div
          class="kt-text-title font-semibold text-warm-800 dark:text-warm-200 mb-1"
        >
          SIMT PE
        </div>
        <p class="kt-text-caption text-warm-500 dark:text-warm-400 leading-6">
          A mask that is a write enable, a divergence stack that is a memory,
          per-thread RV32M, and an address per lane.
        </p>
      </RouterLink>
    </div>
  </DocPage>
</template>
