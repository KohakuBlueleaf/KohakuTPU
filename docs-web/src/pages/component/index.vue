<script setup>
/* Figures on this page are out-of-context synthesis on xcvu13p-fhgb2104-2L-e,
 * Vivado 2024.2, synth only, and the constraint asked is named per table. The
 * system-node rows are at 3.333 ns; the PE rows carry their own ask. */

const parts = {
  cols: [
    { key: "part", label: "Component" },
    { key: "what", label: "What it is" },
    { key: "swap", label: "Replacing it means" },
  ],
  rows: [
    {
      part: "<b>CPU PE</b> — <code>rv_pe</code>",
      what: "an RV32I core that is a compute unit: one local port, one instruction FIFO, four <code>CU_CTRL</code> registers. A <code>CU_INST</code> is a kick and the unit retires when the program halts",
      swap: "writing your own unit. The core is an example of the convention, not a fixture",
    },
    {
      part: "<b>System node</b> — <code>sysnode</code>",
      what: "the memory agent, the mover and the control processor as one block. Every mesh has exactly one",
      swap: "not a thing you replace — you configure it. Port count, staging, interlink and the processor are parameters",
      _tone: "good",
    },
    {
      part: "<b>SIMD PE</b> — <code>rv_pe</code>, <code>SIMD_EN&nbsp;=&nbsp;1</code>",
      what: "the same core widened: eight packed integer lanes and a float tier, one instruction driving all of them",
      swap: "a build parameter. A machine with no use for it sets <code>SIMD_EN&nbsp;=&nbsp;0</code> and pays nothing",
    },
    {
      part: "<b>Transform slot</b> — <code>xform_bank</code>",
      what: "one conversion stage ON the mover's read return, reached only through descriptor mode 5, occupant selected by an id",
      swap: "writing one module. The framework names <code>xform_bank</code> and nothing inside it",
      _tone: "good",
    },
    {
      part: "<b>Staging</b> — <code>mag_stage</code>",
      what: "on-chip memory at a reserved aperture, reached by ordinary addresses",
      swap: "a parameter — banks and entries. Its address decode is fixed",
    },
    {
      part: "<b>Endpoint adapter</b> — <code>noc_l2_adapter</code>",
      what: "a module between a router local port and the unit on it, same six signals on both faces",
      swap: "dropping yours in. <code>PASS&nbsp;=&nbsp;1</code> makes it a wire",
    },
  ],
}

const obligations = {
  cols: [
    { key: "o", label: "Obligation" },
    { key: "why", label: "What it buys" },
    { key: "ex", label: "The transform slot's answer" },
  ],
  rows: [
    {
      o: "<b>port contract</b>",
      why: "signals, directions and handshake, so the host module's control does not change when the occupant does",
      ex: "<code>start</code> / <code>id</code> / <code>mode</code> / <code>beat</code> in, <code>done</code> / <code>word0..3</code> out",
    },
    {
      o: "<b>geometry contract</b>",
      why: "the host has arithmetic that depends on the occupant's shape and needs it <i>before</i> the occupant runs",
      ex: "<code>IN_BITS</code>, <code>OUT_WORDS</code> — 2048 in, 4 words out for the reference quantiser",
    },
    {
      o: "<b>selection</b>",
      why: "a request says <i>use this one</i> without the framework knowing what it is",
      ex: "an <b>id</b>, not a bit per transform: 0 is bypass, <i>k</i> routes to occupant <i>k</i>",
      _tone: "good",
    },
    {
      o: "<b>default occupant</b>",
      why: "a project with no use for the slot pays nothing and still elaborates",
      ex: "id 0 copies beats through and raises <code>done</code> after the last",
    },
  ],
}
</script>

<template>
  <DocPage
    title="The parts the framework ships"
    summary="A component is a part that ships working and expects to be replaced or configured. What each one is, what replacing it actually costs, and the four obligations a slot has to meet before calling itself one."
    domain="cpu"
    status="measured"
    source="src/kohakuaccel/ · docs/integrate/what-you-own.md"
  >
    <p class="doc-p">
      The framework is the connection problem — how to be a node, how to ask for memory, how to be
      dispatched to, how to report completion. <b>A component is something it ships on top of that
      anyway</b>, because a mesh with nothing in it is not testable and a convention with no worked
      example is prose.
    </p>

    <p class="doc-p">
      That is the whole reason this section exists separately from
      <RouterLink to="/framework" class="doc-link">Framework</RouterLink>: those pages are contracts
      you cannot change and remain on the framework. These are parts you are expected to change, and
      KohakuAccel builds a working multi-processor mesh out of them without any project on top —
      <b>the framework is its own first example</b>.
    </p>

    <SpecTable
      :cols="parts.cols"
      :rows="parts.rows"
      caption="Every row here is instantiated and simulated by the framework's own benches. The system node row is the odd one: it is not replaceable, because replacing it means replacing the memory protocol, and at that point you are no longer on the framework"
    />

    <h2 class="doc-h2">What makes a slot a slot</h2>

    <p class="doc-p">
      Four obligations. Miss any one and what you have is a hook — a place where a module happens to
      be instantiated — rather than something a second project can fill.
    </p>

    <SpecTable :cols="obligations.cols" :rows="obligations.rows" />

    <Callout kind="trap" title="A slot that advertises replaceability and requires bug-compatibility is worse than no slot">
      <p>
        The transform stage used to name its occupant directly in two framework modules, hardcode its
        2:1 compression ratio in the memory agent's address arithmetic, name its selection bits after
        one project's number format, and — the one that mattered — <b>depend on an undocumented
        internal priority of the plug-in</b>, worked around identically at two call sites with
        nothing telling a replacement's author so.
      </p>
      <p>
        It meets all four obligations now, and the framework names exactly one module:
        <code>xform_bank</code>. Renaming the reference quantiser breaks one project file and nothing
        in the framework.
      </p>
    </Callout>

    <h2 class="doc-h2">Where to go next</h2>

    <div class="grid gap-4 sm:grid-cols-3 mt-6">
      <RouterLink to="/component/sysnode/microarchitecture" class="card-hover p-5 no-underline block">
        <div class="kt-text-title font-semibold text-warm-800 dark:text-warm-200 mb-1">
          Sysnode micro
        </div>
        <p class="kt-text-caption text-warm-500 dark:text-warm-400 leading-6">
          The memory ports, the converged requester path, the write slots and what the mover actually
          walks.
        </p>
      </RouterLink>
      <RouterLink to="/component/cpu/microarchitecture" class="card-hover p-5 no-underline block">
        <div class="kt-text-title font-semibold text-warm-800 dark:text-warm-200 mb-1">
          CPU PE micro
        </div>
        <p class="kt-text-caption text-warm-500 dark:text-warm-400 leading-6">
          Six register boundaries, one stall rule, two L1s split by who writes, and the 38 LUT that
          make a doorbell correct.
        </p>
      </RouterLink>
      <RouterLink to="/component/caching" class="card-hover p-5 no-underline block">
        <div class="kt-text-title font-semibold text-warm-800 dark:text-warm-200 mb-1">Caching</div>
        <p class="kt-text-caption text-warm-500 dark:text-warm-400 leading-6">
          Staging, the transform slot and the tagged L2 that is designed and not built — three
          answers to reuse, only one of which is a cache.
        </p>
      </RouterLink>
    </div>
  </DocPage>
</template>
