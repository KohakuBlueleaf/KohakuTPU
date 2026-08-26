<script setup>
/* Figures cited here are measured elsewhere and linked; this page carries no
 * numbers of its own beyond the two totals. Source: docs/arch/cpu/rv32-pe/ ·
 * docs/arch/cpu/rv64-sys/. */

const contract = {
  cols: [
    { key: "g", label: "what the framework hands a core" },
    { key: "w", label: "and what that decides" },
  ],
  rows: [
    {
      g: "<b>a NoC endpoint</b> — <code>CU_INST</code>, <code>CU_DATA</code>, <code>CU_SIGNAL</code>, <code>CU_CTRL</code>",
      w: "a core is dispatched to, loaded and reported on the same way every other unit is. The hub demuxes on coordinate, so “looks exactly like a CU” is true rather than approximate",
    },
    {
      g: "<b>the 40-bit map</b> — <code>[39]</code> aperture, <code>[37:36]</code> mesh, <code>[35:0]</code> local",
      w: "an address carries its mesh wherever it is issued, so a pointer means the same thing on every unit",
    },
    {
      g: "<b>the completion contract</b>",
      w: "a completion asserts the pipeline is empty, the requestors are idle <i>and every write has been acknowledged</i>. It is the host's and the dispatcher's only sequencing point",
    },
    {
      g: "<b>the mover's descriptor format</b>, six modes, the transform slot",
      w: "bulk movement is a descriptor, not a loop. A core issues one and waits; the compiler emits them",
    },
    {
      g: "<b>the <code>buf_id</code> map</b> — 0/1 granules, 4/5 words, 3 reserved",
      w: "the loader path is shared, so an image reaches any unit the same way",
    },
  ],
};

const two = {
  cols: [
    { key: "w", label: "" },
    { key: "a", label: "RV32 PE" },
    { key: "b", label: "RV64-sys" },
  ],
  rows: [
    { w: "<b>job</b>", a: "runs one kernel and retires", b: "hosts a runtime and outlives the work it dispatches" },
    { w: "ISA", a: "RV32IM, no CSRs", b: "RV64IMA + Zicsr, traps and interrupts" },
    { w: "privilege", a: "none — there are no modes", b: "<b>M, S and U</b>, with delegation" },
    { w: "faults", a: "halt and report", b: "trap to a handler, at either level" },
    { w: "address decode", a: "four bits", b: "Sv39 over a 40-bit card, one MMU shared by fetch and data" },
    { w: "L1", a: "blocking, one outstanding miss", b: "write-back over the node's cached range" },
    { w: "divide", a: "faults deliberately", b: "present — a runtime allocates and converts time" },
    { w: "atomics", a: "none", b: "full A, and not negotiable" },
    { w: "where it sits", a: "a compute unit in the mesh", b: "the processor inside a system node" },
    {
      w: "how it reaches the fabric",
      a: "a compute-unit shell — it is dispatched to, and dispatches",
      b: "a <b>mailbox</b> in its control region — it dispatches and queues completions, and is never dispatched to",
    },
  ],
};
</script>

<template>
  <DocPage
    title="Processors on this framework"
    summary="What the framework hands any core, and why there are two of them rather than one that stretches. The RV32 PE runs kernels inside a compute unit; RV64-sys hosts a runtime inside a system node. They share no RTL, and each page below opens its own."
    domain="framework"
    status="shipped"
    source="docs/arch/cpu/rv32-pe/ · docs/arch/cpu/rv64-sys/"
  >
    <p class="doc-p">
      A processor here is not a general-purpose CPU that happens to be on a die.
      It is a unit that has to fit a contract the rest of the machine already
      depends on — how it is loaded, how it is dispatched to, what its addresses
      mean, and what it must have finished before it says so.
    </p>

    <SpecTable :cols="contract.cols" :rows="contract.rows" />

    <Callout kind="rule" title="LUT and frequency outrank latency">
      <p>
        A core here shares a die with the thing that does the actual work. A
        cycle spent is a cycle; a LUT spent is a LUT that a compute unit does not
        get. Where the two conflict, the smaller and faster structure wins and
        the cycle is paid — which is why both cores stall rather than
        speculate, and why neither has a store buffer.
      </p>
    </Callout>

    <h2 class="doc-h2">Two cores, because the jobs disagree</h2>

    <p class="doc-p">
      Every row below is a place where a compute unit and a runtime host want
      opposite things. Retrofitting one into the other drags the compute PE's
      constraints into the runtime and the runtime's costs into every compute
      PE, so there are two PEs and one contract between them.
    </p>

    <SpecTable :cols="two.cols" :rows="two.rows" />

    <Callout kind="note" title="The system node's processor is not optional">
      <p>
        There is no <code>CTRL_PE</code> parameter. The mover, the transform
        slot and the processor are parts of a node, not options on it — and the
        RV64 complex is fused through the node's memory port, the mover's config
        window, the doorbell, the hub and staging.
      </p>
      <p>
        A whole node with it measures <b>32,859 CLB LUT sites</b>, of which the
        processor is 7,244: <code>sysnode</code> as top,
        <code>CPU_RV64 = 1</code>, <code>PORTS = 2</code>, out-of-context
        synthesis on <code>xcvu13p-fhgb2104-2L-e</code> under Vivado 2024.2 at a
        3.333 ns request — <b>not placed and not routed</b>. That run
        <b>meets 300 MHz in out-of-context synthesis</b>: worst slack
        <b>+0.039 ns</b>, <b>0 failing endpoints</b>. <b>It is not closed
        timing</b>, and no frequency above 300 MHz follows from it.
      </p>
    </Callout>

    <h2 class="doc-h2">Where to go next</h2>

    <div class="grid gap-4 sm:grid-cols-2 mt-6">
      <RouterLink
        to="/component/rv32pe"
        class="card-hover p-5 no-underline block"
      >
        <div
          class="kt-text-title font-semibold text-warm-800 dark:text-warm-200 mb-1"
        >
          The RV32 PE
        </div>
        <p class="kt-text-caption text-warm-500 dark:text-warm-400 leading-6">
          The compute unit's processor: what it is, what it costs, and every
          option that was costed and declined.
        </p>
      </RouterLink>
      <RouterLink
        to="/component/rv64sys"
        class="card-hover p-5 no-underline block"
      >
        <div
          class="kt-text-title font-semibold text-warm-800 dark:text-warm-200 mb-1"
        >
          RV64-sys
        </div>
        <p class="kt-text-caption text-warm-500 dark:text-warm-400 leading-6">
          The runtime host: RV64IMA with three privilege levels, Sv39 and an
          L1, and what a whole system node costs with it inside.
        </p>
      </RouterLink>
    </div>
  </DocPage>
</template>
