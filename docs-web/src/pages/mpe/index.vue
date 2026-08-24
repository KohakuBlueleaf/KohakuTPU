<script setup>
/* KohakuMPE: a mesh whose compute units are processors. Figures are
 * out-of-context synthesis on xcvu13p-fhgb2104-2L-e, Vivado 2024.2, synth only;
 * the ask is named per row because a tighter one buys LUT and no megahertz. */

const population = {
  cols: [
    { key: "what", label: "" },
    { key: "lut", label: "LUT", align: "right", mono: true },
    { key: "dsp", label: "DSP48", align: "right", mono: true },
    { key: "bram", label: "BRAM", align: "right", mono: true },
    { key: "fma", label: "FP FMA / clk", align: "right", mono: true },
  ],
  rows: [
    { what: "8 × SIMD PE — 4 float lanes each", lut: "110,176", dsp: "576", bram: "104", fma: "32" },
    { what: "4 × SIMT PE — 8 float lanes each", lut: "86,344", dsp: "192", bram: "122", fma: "32" },
    { what: "2 × CPU PE", lut: "4,954", dsp: "0", bram: "10", fma: "0" },
    {
      what: "<b>one mesh of processors</b>",
      lut: "<b>201,474</b>",
      dsp: "<b>768</b>",
      bram: "<b>236</b>",
      fma: "<b>64</b>",
      _tone: "good",
    },
    { what: "available", lut: "~350,000", dsp: "3,072", bram: "—", fma: "—" },
  ],
}

const shape = {
  cols: [
    { key: "q", label: "The property" },
    { key: "a", label: "Which unit it selects" },
  ],
  rows: [
    {
      q: "every element treated the same, one address stream, arithmetic dense — vertex transform, blending, colour conversion",
      a: "<b>SIMD PE</b> — one instruction over eight lanes; 24.9× on an int8 dot against a scalar core that has a multiplier",
    },
    {
      q: "a fragment shader where lane 3 takes the <code>if</code> and lane 4 the <code>else</code>; a texture fetch addressed per lane",
      a: "<b>SIMT PE</b> — an active mask, an IPDOM stack, an address per lane",
    },
    {
      q: "a fixed dataflow with operands resident across many passes and no control flow",
      a: "<b>not a PE at all</b> — that is a systolic array, and it does not pretend to be one",
    },
    {
      q: "elementwise over a long vector, reductions, format conversion",
      a: "<b>a vector unit</b> — schedule-bound rather than capacity-bound",
    },
    {
      q: "deciding what happens next",
      a: "<b>CPU PE</b>, or the system node's control processor if it is dispatch rather than compute",
      _tone: "good",
    },
  ],
}
</script>

<template>
  <DocPage
    title="A mesh of processors"
    summary="KohakuMPE is what KohakuAccel builds when the compute units are processors rather than an accelerator. What a mesh of them costs, what work selects which class, and why the framework shipping a working machine on its own is the point rather than a demo."
    domain="simt"
    status="measured"
    source="src/kohakumpe/ · src/kohakuaccel/pe/rv32/ · docs/arch/pe/"
  >
    <p class="doc-p">
      <b>KohakuAccel can build a working machine with no project on top of it.</b> Take the mesh, the
      system node and the CPU PE the framework already ships, populate the router locals with
      processors instead of an accelerator, and what comes out is a multi-processor mesh that a
      driver enumerates, dispatches to and collects completions from without knowing the units are
      processors.
    </p>

    <p class="doc-p">
      That is why this section exists next to
      <RouterLink to="/tpu" class="doc-link">KohakuTPU</RouterLink> rather than inside it. That is
      one project's answer, aimed at one workload. This is another, aimed at graphics and
      general-purpose compute and built from
      <RouterLink to="/component" class="doc-link">the same components</RouterLink> — and the fact
      that both are first-class citizens of one mesh is the framework's actual claim.
    </p>

    <SpecTable
      :cols="population.cols"
      :rows="population.rows"
      caption="Multiples of the measured per-PE figures — the wide classes at a 2.857 ns ask, the CPU PE at 3.333 ns. The float width is chosen rather than fallen into: 8 × 4 + 4 × 8 = 64 FP FMA per clock, one Mali-G610 shader core's width exactly. LUT is the binding resource here and DSP is not, at 768 of 3,072"
    />

    <Callout kind="note" title="PE count is bounded by the memory agent, not by LUT">
      <p>
        At 2,477 LUT each, a mesh's ~350,000 usable LUT would hold over a hundred CPU PEs. That is
        not the limit. Every unit lives on <code>noc_clk</code> and reaches memory over the fabric,
        so the bound is the agent's capacity: <b>four PEs per NoC/system-node pair</b> is the
        measured ceiling, and sharing one agent between four costs <b>+13.7 %</b> on a fixed
        compute-bound program.
      </p>
    </Callout>

    <h2 class="doc-h2">What selects which unit</h2>

    <p class="doc-p">
      The question is never “which is faster”. It is <b>what shape the work has</b> — whether lanes
      agree, whether addresses are contiguous, whether operands stay resident.
    </p>

    <SpecTable :cols="shape.cols" :rows="shape.rows" />

    <Callout kind="rule" title="Going wide is for work that is uniform">
      <p>
        The SIMD tier draws its own boundary: “per-lane branching, per-lane addresses, masks and
        predication — a SIMT core's. Nothing here anticipates them, and adding them here would cost
        every uniform kernel.”
      </p>
      <p>
        When lanes need different paths or different addresses, the answer is a
        <b>different machine, not a wider one</b>. That is the whole reason there are two wide
        classes rather than one configurable one.
      </p>
    </Callout>

    <h2 class="doc-h2">Where to go next</h2>

    <div class="grid gap-4 sm:grid-cols-3 mt-6">
      <RouterLink to="/mpe/hetero" class="card-hover p-5 no-underline block">
        <div class="kt-text-title font-semibold text-warm-800 dark:text-warm-200 mb-1">
          Heterogeneity
        </div>
        <p class="kt-text-caption text-warm-500 dark:text-warm-400 leading-6">
          All four unit kinds side by side: arithmetic, cost, what routes where, and the opcode map
          they share.
        </p>
      </RouterLink>
      <RouterLink to="/mpe/simt" class="card-hover p-5 no-underline block">
        <div class="kt-text-title font-semibold text-warm-800 dark:text-warm-200 mb-1">SIMT PE</div>
        <p class="kt-text-caption text-warm-500 dark:text-warm-400 leading-6">
          A mask that is a write enable, an IPDOM stack that is a memory, per-thread RV32M, and eight
          float lanes.
        </p>
      </RouterLink>
      <RouterLink to="/mpe/simt/comparison" class="card-hover p-5 no-underline block">
        <div class="kt-text-title font-semibold text-warm-800 dark:text-warm-200 mb-1">
          SIMT vs industry
        </div>
        <p class="kt-text-caption text-warm-500 dark:text-warm-400 leading-6">
          Where one mesh lands against shipped mobile GPUs, and which comparisons are fair.
        </p>
      </RouterLink>
    </div>
  </DocPage>
</template>
