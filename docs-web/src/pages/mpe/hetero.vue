<script setup>
/* Every figure on this page is out-of-context synthesis on one part,
 * xcvu13p-fhgb2104-2L-e, Vivado 2024.2, synth only. The CONSTRAINT ASKED is
 * named on every row: the controller PE was measured at 3.333 ns, the two wide
 * classes at 2.857 ns, and a tighter ask buys LUT it cannot spend on megahertz.
 * Sources are named per table. */

const lineage = {
  nodes: [
    {
      id: "core",
      x: 8,
      y: 0,
      w: 16,
      h: 4,
      label: "the base core",
      sub: "RV32I · no multiply, no float · six register boundaries",
      accent: true,
    },
    {
      id: "cpu",
      x: 0,
      y: 7,
      w: 9,
      h: 4.4,
      label: "controller PE",
      sub: "2,477 LUT · 377.9 MHz · 3.333 ns",
    },
    {
      id: "simd",
      x: 11.5,
      y: 7,
      w: 9,
      h: 4.4,
      label: "SIMD PE",
      sub: "13,772 LUT · 353.4 MHz · 2.857 ns",
    },
    {
      id: "simt",
      x: 23,
      y: 7,
      w: 9,
      h: 4.4,
      label: "SIMT PE",
      sub: "21,586 LUT · 365.6 MHz · 2.857 ns",
    },
    {
      id: "cpuw",
      x: 0,
      y: 14,
      w: 9,
      h: 4.4,
      label: "one 32-bit operation",
      sub: "no multiply, no float",
    },
    {
      id: "dspw",
      x: 11.5,
      y: 14,
      w: 9,
      h: 4.4,
      label: "8 packed lanes + 4 float",
      sub: "32 int8 ops from one instruction",
    },
    {
      id: "gpuw",
      x: 23,
      y: 14,
      w: 9,
      h: 4.4,
      label: "8 threads + 8 float",
      sub: "own path, own address, own multiply",
    },
  ],
  edges: [
    { from: "core:b", to: "cpu:t", dir: "v", label: "as it is" },
    { from: "core:b", to: "dsp:t", dir: "v", label: "SIMD_EN", accent: true },
    { from: "core:b", to: "gpu:t", dir: "v", label: "rebuild" },
    { from: "cpu:b", to: "cpuw:t", dir: "v" },
    { from: "dsp:b", to: "dspw:t", dir: "v", accent: true },
    { from: "gpu:b", to: "gpuw:t", dir: "v", accent: true },
  ],
  groups: [{ x: -1, y: 12.6, w: 34, h: 7.2, label: "what ONE instruction drives" }],
}

const classes = {
  cols: [
    { key: "cls", label: "PE class" },
    { key: "arith", label: "The arithmetic it ships with" },
    { key: "lut", label: "LUT", align: "right", mono: true },
    { key: "ff", label: "FF", align: "right", mono: true },
    { key: "bram", label: "BRAM", align: "right", mono: true },
    { key: "dsp", label: "DSP48", align: "right", mono: true },
    { key: "fmax", label: "Fmax", align: "right", mono: true },
    { key: "ask", label: "ask", align: "right", mono: true },
  ],
  rows: [
    {
      cls: "<b>controller</b> — <code>rv_pe</code>",
      arith:
        "RV32I only — <b>no multiply, no divide, no float</b>. One 32-bit scalar datapath",
      lut: "<b>2,477</b>",
      ff: "4,140",
      bram: "5",
      dsp: "0",
      fmax: "377.9 MHz",
      ask: "3.333 ns",
    },
    {
      cls: "<b>DSP</b> — <code>rv_pe</code>, <code>SIMD_EN&nbsp;=&nbsp;1</code><br>8 int + 4 float",
      arith:
        "<b>SWAR packed int8/int16/int32</b> — add, compare, shift, <code>vmul</code>, and <code>vdot</code> into the accumulators — plus a <b>float tier of 4 lanes</b>",
      lut: "<b>13,772</b>",
      ff: "10,126",
      bram: "13",
      dsp: "72",
      fmax: "353.4 MHz",
      ask: "<b>2.857 ns</b>",
    },
    {
      cls: "<b>GPU</b> — <code>kht_pe</code><br>8 int + 8 float, 16 waves",
      arith:
        "per-thread RV32I <b>+ RV32M</b> (<code>mul</code>, <code>mulh</code>, <code>mulhsu</code>, <code>mulhu</code>, one product per lane) plus a <b>float tier of 8 lanes</b>",
      lut: "<b>21,586</b>",
      ff: "17,268",
      bram: "30.5",
      dsp: "48",
      fmax: "365.6 MHz",
      ask: "<b>2.857 ns</b>",
    },
  ],
}

const budget = {
  items: [
    { label: "controller PE — assembled, 3.333 ns ask", value: 2477, note: "377.9 MHz" },
    { label: "SIMD PE — 8 int, no float tier, 3.333 ns ask", value: 10343, note: "357.1 MHz" },
    {
      label: "SIMD PE — 8 int + 4 float, THE DSP REFERENCE, 2.857 ns ask",
      value: 13772,
      note: "353.4 MHz · measured",
    },
    {
      label: "SIMT PE — 8 int + 8 float + RV32M, THE GPU REFERENCE, 2.857 ns ask",
      value: 21586,
      note: "365.6 MHz · measured",
    },
    {
      label: "SIMD PE — 8 int + 16 float, one lane per element, 3.333 ns ask",
      value: 22743,
      note: "346.5 MHz",
    },
  ],
}

const granule = `   ELEMENTS   how many fit a 256-bit register at the operand width
              the INSTRUCTION names          8 at 32 bit, 16 at 16 bit
   LANES      how many are built             a BUILD PARAMETER
   INTERVAL   elements / lanes

   8 lanes x 32 bit  =  256 bit  =  one native entry  =  one flit

   integer lanes  <-  the memory granule  (coalescing)      8, FIXED
   float lanes    <-  arithmetic demand   (throughput/LUT)  a KNOB`

/* ONE dtype configuration, machine-wide. Operand width is a per-instruction
 * property; the internal format is always E8M15 and there is no build in which
 * it is anything else. The classes differ in float PRESENCE and WIDTH only. */
const dtype = {
  cols: [
    { key: "p", label: "Legitimately a parameter" },
    { key: "n", label: "NOT a parameter" },
  ],
  rows: [
    {
      p: "whether a float tier exists at all",
      n: "which format the datapath computes in",
      _tone: "good",
    },
    {
      p: "how many float lanes, and the issue interval that follows",
      n: "whether an operand is FP16 or FP32",
      _tone: "good",
    },
  ],
}

const mesh = {
  cols: [
    { key: "what", label: "" },
    { key: "lut", label: "LUT", align: "right", mono: true },
    { key: "dsp", label: "DSP48", align: "right", mono: true },
    { key: "bram", label: "BRAM tiles", align: "right", mono: true },
    { key: "ff", label: "FF", align: "right", mono: true },
    { key: "fma", label: "FP FMA / clk", align: "right", mono: true },
  ],
  rows: [
    { what: "8 × SIMD PE — 4 float lanes each", lut: "110,176", dsp: "576", bram: "104", ff: "81,008", fma: "32" },
    { what: "4 × SIMT PE — 8 float lanes each", lut: "86,344", dsp: "192", bram: "122", ff: "69,072", fma: "32" },
    { what: "2 × controller PE", lut: "4,954", dsp: "0", bram: "10", ff: "8,280", fma: "0" },
    {
      what: "<b>one mesh</b>",
      lut: "<b>201,474</b>",
      dsp: "<b>768</b>",
      bram: "<b>236</b>",
      ff: "<b>158,360</b>",
      fma: "<b>64</b>",
      _tone: "good",
    },
    { what: "available", lut: "~350,000", dsp: "3,072", bram: "—", ff: "—", fma: "—" },
  ],
}

const routing = {
  cols: [
    { key: "work", label: "The work" },
    { key: "pe", label: "Goes to" },
    { key: "why", label: "Because" },
  ],
  rows: [
    {
      work: "deciding what to do next — sequencing kernels, walking descriptors, reacting to completions",
      pe: "<b>controller PE</b>",
      why: "a hand-written state machine does it until the policy changes, and then it does it wrong",
    },
    {
      work: "vertex transform, blending, colour-space conversion — one address stream, every element treated the same",
      pe: "<b>SIMD PE</b>",
      why: "widening the datapath and letting one instruction drive eight of them; 24.9× on an int8 dot against a scalar core that has a multiplier",
    },
    {
      work: "a fragment shader where lane 3 takes the <code>if</code> and lane 4 takes the <code>else</code>; a texture fetch whose address is per-lane",
      pe: "<b>SIMT PE</b>",
      why: "an active mask, an IPDOM stack and an address per lane — none of which the SIMD tier anticipates",
    },
    {
      work: "a fixed dataflow with operands resident across many passes and no control flow at all",
      pe: "not a PE at all",
      why: "that is a systolic array, and a PE is scalar control, uniform width, or divergence — an array is none of the three",
    },
  ],
}

const majors = {
  cols: [
    { key: "major", label: "major", mono: true },
    { key: "op", label: "opcode", mono: true },
    { key: "owner", label: "owner" },
    { key: "carries", label: "carries" },
  ],
  rows: [
    {
      major: "custom-0",
      op: "0x0B",
      owner: "<b>SIMD PE</b>",
      carries:
        "the packed-integer tier: memory, packed ALU, bitwise, shifts, dot and accumulators, moves, permute",
    },
    {
      major: "custom-1",
      op: "0x2B",
      owner: "<b>SIMD PE</b>",
      carries:
        "the float tier. A build without E8M15 leaves the major unmapped, so a float instruction faults rather than landing in a decode case",
    },
    {
      major: "custom-2",
      op: "0x5B",
      owner: "<b>SIMT PE</b>",
      carries:
        "the R-type groups: scalar ALU, scalar/vector moves, divergence, subgroup, the uniform-base and lane-linear memory forms",
    },
    {
      major: "custom-3",
      op: "0x7B",
      owner: "<b>SIMT PE</b>",
      carries: "the I-type groups: scalar immediate ALU, scalar shifts, the two uniform branches",
    },
    {
      major: "OP",
      op: "0x33",
      owner: "<b>standard — not in the pool</b>",
      carries:
        "<code>funct7 = 0000001</code> is <code>RV32M</code>: <code>mul</code>, <code>mulh</code>, <code>mulhsu</code>, <code>mulhu</code> on the SIMT PE's per-thread file. <code>funct3</code> 100–111 is <code>div</code>/<code>rem</code> and is decoded and <b>refused</b>",
      _tone: "good",
    },
  ],
}

const rtype = [
  { name: "funct7", bits: 7, value: "operation" },
  { name: "rs2", bits: 5 },
  { name: "rs1", bits: 5 },
  { name: "funct3", bits: 3, value: "group" },
  { name: "rd", bits: 5 },
  { name: "opcode", bits: 7, value: "custom-N", accent: true },
]

const mulWhere = {
  cols: [
    { key: "c", label: "The cost, on the controller PE" },
    { key: "g", label: "The same thing on the SIMT PE" },
  ],
  rows: [
    {
      c: "<b>The result mux lengthens the distance-1 forwarding path.</b> <code>ex_alu</code> feeds <code>fwd_x_val</code>, which is an input to <code>x_op1_reg</code> — the register the critical path already ends at",
      g: "<b>That path does not exist.</b> Barrel scheduling deleted it: with as many resident waves as the pipeline is deep, no two in-flight instructions share a wave, so there is no forwarding network and no interlock to lengthen",
      _tone: "good",
    },
    {
      c: "<b>A multi-cycle result needs a new stall term</b>, and stall terms fan out — the <code>FWD_X</code> experiment priced widening one at <b>5 MHz</b>",
      g: "<b>The flag was already there.</b> A wave with a float in flight is skipped by the scheduler via one pending bit per wave; a multiply sets and clears the <i>same</i> bit and retires through the <i>same</i> slot",
      _tone: "good",
    },
  ],
}

// KohakuMPE is GPU/GPGPU. Workload discussion on this page is RENDERING; any AI
// workload belongs to a project page under projects/, never here and never on a
// framework page.
</script>

<template>
  <DocPage
    title="SIMD, SIMT and what is not a PE"
    summary="Three kinds of processing element share one mesh: the controller PE, the SIMD PE and the SIMT PE. What arithmetic each ships, what each costs, which work routes to which, and where the room in the opcode map actually is."
    domain="simt"
    status="measured"
    source="src/kohakuaccel/pe/rv32/ · docs/arch/pe/"
  >
    <p class="doc-p">
      Every class here is a compute unit first: one local port, one instruction FIFO, the four
      <code>CU_CTRL</code> registers. A driver enumerates one without knowing it is a processor. The
      only thing different is what it does with an instruction — a <code>CU_INST</code> is a
      <b>kick</b>, and the unit retires when the program halts.
    </p>

    <Fig
      caption="The lineage. The SIMD PE is a parameter on the base core; the SIMT PE is a rebuild on its shape rather than a parameter on it. All three figures are OOC synthesis on xcvu13p-fhgb2104-2L-e, Vivado 2024.2 — the controller at a 3.333 ns ask, the two wide classes at 2.857 ns."
      zoom
    >
      <BlockDiagram :nodes="lineage.nodes" :edges="lineage.edges" :groups="lineage.groups" />
    </Fig>

    <h2 class="doc-h2">What each class actually ships</h2>

    <p class="doc-p">
      Naming the LUT without naming the arithmetic is what let “the machine has float” and “the
      machine has no multiply” both circulate as true. The arithmetic is the first column for that
      reason.
    </p>

    <SpecTable
      :cols="classes.cols"
      :rows="classes.rows"
      caption="xcvu13p-fhgb2104-2L-e, Vivado 2024.2, out-of-context synthesis only — pre-placement, so a routed result will be somewhat worse. Resource figures are CLB LUT sites. Both wide classes report 48/48 component tests passing on the build these figures come from. div and rem fault on every class, per-thread included. Sources: docs/arch/pe/performance.md, docs/arch/pe/simd/, docs/projects/kohakumpe/simt/status.md."
    />

    <Callout kind="trap" title="Read the ask column before subtracting two rows">
      <p>
        The controller row is at the older <b>3.333 ns</b> request and the two wide rows at
        <b>2.857 ns</b>. On this core a tighter ask buys LUT and no megahertz at all — ~90 extra
        sites at 2.5 ns, 350–400 at 2.0 ns — so a controller figure lifted from a 2.857 ns run would
        be larger and no faster. A difference taken across two asks measures the constraint as much
        as the design.
      </p>
      <p>
        <b>Two synthesis flows are in that table too.</b> The controller and DSP rows are
        <code>-flatten_hierarchy none</code>; the GPU row is <code>rebuilt</code>. Run both ways on
        the same GPU design at a 2.500 ns ask, <code>none</code> costs <b>+720 LUT and −4.1 MHz</b>
        — real, but small enough that the rows compare once it is named. The controller PE also has
        a fully flattened figure, <b>2,491 LUT at 410.8 MHz</b>, 33 MHz of which is the flow rather
        than the design.
      </p>
    </Callout>

    <Callout kind="measured" title="The DSP48 counts are not on the same axis as the LUT counts">
      <p>
        The SIMD PE's <b>72</b> includes <code>SIMD_DOTDSP&nbsp;=&nbsp;1</code>, which keeps the
        <code>vdot</code> sum inside the DSP48 column and <i>buys LUT back</i>: 32 more DSP for a
        measured <b>256 LUT and 32 CARRY8</b> per eight lanes. DSP is the cheap resource on this
        device and LUT is the binding one, so the two wide classes are spending on the same axis in
        opposite directions — and the LUT totals still compare.
      </p>
    </Callout>

    <h2 class="doc-h2">The reference configurations, and why they differ</h2>

    <Callout kind="rule" title="8 int + 4 float is the DSP reference; 8 int + 8 float is the GPU reference">
      <p>
        <b>Both are buildable, both are measured, and float is not optional in either — rendering
        needs it.</b> The integer number is the same on both classes and the float number is not,
        and neither of those facts is arbitrary.
      </p>
      <p>
        <b>Lane count and element count are separate numbers, and only one of them is a build
        parameter.</b> How many <i>elements</i> a 256-bit register holds follows from the operand
        width the instruction names — 8 at 32 bits, 16 at 16 — and the <i>lanes</i> are the
        parameter. The issue interval is <code>elements / lanes</code>: four float lanes over a
        sixteen-element vector is one vector every four cycles, which is not a narrower machine but
        a slower one.
      </p>
      <div
        class="font-mono kt-text-caption whitespace-pre text-warm-700 dark:text-warm-300 leading-6 overflow-x-auto my-3"
      >{{ granule }}</div>
      <p>
        <b>The integer lanes are the address path.</b> A contiguous 32-bit load by eight threads is
        exactly one <code>MEM_RD_REQ</code>, one native memory entry, one flit. Narrowing them
        breaks single-request coalescing permanently — every coalesced load becomes two or more
        requests, for every kernel, forever. Float carries no such constraint, which is why it is
        the knob and the integer side is not. That asymmetry is the whole justification for
        <code>int 8 / float 4</code> over <code>int 4 / float 4</code>, and for rejecting
        <code>int &lt; float</code> outright.
      </p>
      <p>
        The two classes land on different float counts because a lane costs LUT and each class had a
        different amount to spend. The <b>width</b> differs; nothing else about the float does.
      </p>
    </Callout>

    <h3 class="doc-h3">There is one dtype configuration, and it is the whole design</h3>

    <div
      class="font-mono kt-text-caption whitespace-pre text-warm-700 dark:text-warm-300 leading-6 overflow-x-auto my-3"
    >   FP32 or FP16 operands in  -&gt;  E8M15 compute  -&gt;  FP32 or FP16 out</div>

    <p class="doc-p">
      <b>Operand width is a per-instruction property, not a build option</b>, and the internal
      format is <b>always E8M15</b> — an 8-bit exponent, a 15-bit mantissa. There is no build in
      which the datapath computes in anything else, and there is no per-class float format to
      compare. Two PE classes carrying float carry the <i>same</i> float, so a result from one is
      comparable to a result from the other by construction rather than by intent.
    </p>

    <SpecTable
      :cols="dtype.cols"
      :rows="dtype.rows"
      caption="So: the controller PE has no float tier; the SIMD PE has one at four lanes; the SIMT PE has one at eight. All three statements are about presence and width — none of them is about format"
    />

    <Callout kind="measured" title="Precision is not the deficit it sounds like">
      <p>
        E8M15 is a <b>1.5e-5</b> relative error — <b>32× better than the fp16</b> that mobile
        fragment shaders run at — with no subnormals, one rounding mode and a documented one-ulp
        deviation on subtractive alignment. E8 is FP32's exponent field, which is what lets one
        internal format serve both operand widths without a second datapath.
      </p>
    </Callout>

    <h2 class="doc-h2">What a mesh of these is</h2>

    <p class="doc-p">
      The mesh population is <b>8 SIMD PEs, 4 SIMT PEs and 2 controller PEs</b>, and its float width
      is chosen rather than fallen into: <code>8 × 4 + 4 × 8 = 64</code> FP FMA per clock, which is
      <b>one Mali-G610 shader core's width, exactly</b>.
    </p>

    <SpecTable
      :cols="mesh.cols"
      :rows="mesh.rows"
      caption="Multiples of the measured per-PE figures above — the DSP and GPU rows at a 2.857 ns ask, the controller row at 3.333 ns. LUT is the binding resource and DSP is not, 768 of 3,072, which is what makes SIMD_DOTDSP and the DSP48-backed float lanes the right trade rather than a convenience"
    />

    <Callout kind="note" title="PE count per mesh is not decided by LUT alone">
      <p>
        At 2,477 LUT each, a mesh's ~350,000 usable LUT would hold over a hundred controller PEs —
        and that is not the limit. Each unit lives entirely on <code>noc_clk</code> and its requestor
        speaks to the memory agent over the fabric, so PE count is bounded by the agent's capacity:
        <b>four PEs per NoC/MAG pair is the measured ceiling</b>, and sharing one agent between four
        costs +13.7 % on a fixed compute-bound program —
        <RouterLink to="/framework/cpu" class="doc-link">controller PE</RouterLink>.
      </p>
    </Callout>

    <Callout kind="measured" title="A quarter of a controller PE is not a processor">
      <p>
        <code>u_base</code> — the framework attach every compute unit on this fabric carries,
        processor or not — is <b>657 LUT and 1,381 FF</b>. On the controller PE that is a quarter of
        the unit and the marginal cost of <i>this unit being a processor</i> is nearer
        <b>1,900 LUT</b>; on the SIMT PE the same attach is <b>3.0 %</b>. It is the one number in
        this whole comparison that is identical in every row.
      </p>
    </Callout>

    <h2 class="doc-h2">Where the multiply is, and where it is not</h2>

    <p class="doc-p">
      A <b>scalar</b> register-register multiply exists nowhere in this machine. The controller PE
      is RV32I, so <code>mul</code> faults; the SIMD PE's multiply lives in the <i>vector</i> register
      file behind custom-0, reached by putting operands in <code>v0..v7</code>; the SIMT PE's
      <i>uniform</i> ALU on custom-2 is the same ten register-register operations RV32I has.
    </p>

    <Callout kind="rule" title="But the SIMT PE multiplies, per thread, with a standard opcode">
      <p>
        <code>mul</code>, <code>mulh</code>, <code>mulhsu</code> and <code>mulhu</code> execute on
        the SIMT PE's <b>per-thread</b> register file, one product per lane, on the standard
        <code>OP</code> encoding. A shader that writes <code>a * b</code> gets one instruction. On
        the controller PE and the SIMD PE's scalar half the same expression still calls libgcc —
        about <b>54 cycles</b>, measured. <code>div</code> and <code>rem</code> fault everywhere,
        per-thread included.
      </p>
    </Callout>

    <p class="doc-p">
      <b>That the multiplier landed there rather than on the controller is the finding, not an
      accident.</b> The controller PE's costing named two risks, and the SIMT PE has neither of them.
    </p>

    <SpecTable
      :cols="mulWhere.cols"
      :rows="mulWhere.rows"
      caption="The multiplier is built at the float tier's own latency on purpose: equal latency makes a collision between a float result and a multiply result structurally impossible rather than arbitrated, because one instruction issues per cycle. It is an increment on machinery that already existed for float, not a second mechanism — and it ships with that tier rather than separately from it. Source: docs/arch/pe/microarchitecture.md"
    />

    <Callout kind="note" title="The general rule, with a built example behind it">
      <p>
        A multi-cycle unit is <b>cheap in a machine that already has a way to park an
        instruction</b>, and <b>expensive in one whose whole complexity budget is three positional
        forwards and one stall rule</b>. What a multiplier, a divider or scalar float would each
        cost on the controller PE is still costed on the
        <RouterLink to="/framework/cpu" class="doc-link">controller PE</RouterLink> page — and the answer
        there is still <code>mul</code> yes, <code>div</code> no, scalar float no.
      </p>
    </Callout>

    <h2 class="doc-h2">What routes to which</h2>

    <SpecTable :cols="routing.cols" :rows="routing.rows" />

    <Callout kind="rule" title="The SIMD tier's own boundary, quoted">
      <p>
        “per-lane branching, per-lane addresses, masks and predication — a SIMT core's. Nothing here
        anticipates them, and adding them here would cost every uniform kernel.”
      </p>
      <p>
        This design goes wide on work that is <i>uniform</i>. When lanes need to take different
        paths, or fetch from different addresses, the answer is a different machine and not a wider
        one.
      </p>
    </Callout>

    <h2 class="doc-h2">The opcode map</h2>

    <p class="doc-p">
      RISC-V reserves <b>four</b> custom opcode majors and no more. Every PE class draws from that
      one pool <b>for the instructions RISC-V has not already standardised</b>, so the allocation is
      recorded outside both tiers' ISA modules — a table that lives in one tier's source is not an
      authority the other tier can check itself against.
    </p>

    <BitField
      :fields="rtype"
      caption="An R-type custom instruction. funct3 selects the group, funct7 the operation inside it, and the low two bits of funct7 are the element type on every typed DSP group"
    />

    <SpecTable
      :cols="majors.cols"
      :rows="majors.rows"
      caption="All four custom majors are spoken for. The last row is not one of them — it is the standard OP major, and it is there because anyone counting remaining encoding room must not charge RV32M against the pool. Verified in src/kohakumpe/simt/kht_predec.v, which decodes funct7 0000001 into is_imul and is_mdiv and makes is_mdiv a term of illegal"
    />

    <Callout kind="rule" title="Standard encoding space is worth more than its size suggests">
      <p>
        <code>RV32M</code> cost <b>none</b> of the four custom majors, and a compiler emits it with
        <code>-march=rv32im</code> and nothing else. That is the property no custom-major extension
        in this machine has — and it is exactly why a non-conforming scalar float behind a custom
        major was refused: the core's first design objective is that ordinary compilers work
        unmodified, and a float extension a compiler cannot target defeats it.
      </p>
    </Callout>

    <Callout kind="rule" title="This is not the constraint it looks like">
      <p>
        <b>The scarcity is in format-distinct instructions, not in instructions.</b> An R-type group
        has a 7-bit <code>funct7</code>, so it holds <b>128</b> operations. The extensions
        deliberately deferred today — atomics, texture, ray tracing — are all R-type shaped, and
        each fits in <code>funct7</code> room inside an existing group without touching the major
        allocation at all.
      </p>
      <p>
        <code>custom-3</code>'s <code>funct3</code> space is deliberately <b>not</b> filled. Leave
        it that way: it is the only I-type room the machine has left, and an I-type instruction is
        the one shape that cannot be squeezed in anywhere else — an I-type layout has no
        <code>funct7</code>, so an I-type group holds exactly one instruction.
      </p>
    </Callout>

    <Callout kind="note" title="Why the DSP majors were not reused">
      <p>
        A GPU build carries no SIMD tier, so <code>custom-0</code> and <code>custom-1</code> would be
        free in it. They are left alone anyway: if the GPU ever exposes the DSP's packed integer
        operations, it should reuse <b>the DSP's own encodings unchanged</b>, so the assembler, the
        golden model and the disassembler share code instead of forking a second packed-integer
        table.
      </p>
    </Callout>

    <h2 class="doc-h2">Where to go next</h2>

    <div class="grid gap-4 sm:grid-cols-3 mt-6">
      <RouterLink to="/framework/cpu" class="card-hover p-5 no-underline block">
        <div class="kt-text-title font-semibold text-warm-800 dark:text-warm-200 mb-1">
          CPU PE
        </div>
        <p class="kt-text-caption text-warm-500 dark:text-warm-400 leading-6">
          Six register boundaries, one stall rule, two L1s split by who writes, and the 38 LUT that
          make a doorbell correct.
        </p>
      </RouterLink>
      <RouterLink to="/component/simd" class="card-hover p-5 no-underline block">
        <div class="kt-text-title font-semibold text-warm-800 dark:text-warm-200 mb-1">SIMD PE</div>
        <p class="kt-text-caption text-warm-500 dark:text-warm-400 leading-6">
          Eight lanes, four int8 elements sharing one carry chain, an accumulator that issues one
          dot per cycle, and a float tier of rotating partials.
        </p>
      </RouterLink>
      <RouterLink to="/mpe/simt" class="card-hover p-5 no-underline block">
        <div class="kt-text-title font-semibold text-warm-800 dark:text-warm-200 mb-1">SIMT PE</div>
        <p class="kt-text-caption text-warm-500 dark:text-warm-400 leading-6">
          The SIMT class: a mask that is a write enable, an IPDOM stack that is a memory, per-thread
          RV32M, and eight float lanes.
        </p>
      </RouterLink>
    </div>
  </DocPage>
</template>
