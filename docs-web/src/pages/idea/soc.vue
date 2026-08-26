<script setup>
// A processor is just another compute unit on the port. The RV32 controller
// PE, and KohakuMPE's SIMD and SIMT PEs, all hang off the mesh the same way a
// matrix cluster does.
const procs = {
  groups: [{ x: 40, y: -2.5, w: 43, h: 21, label: "compute units that are processors" }],
  nodes: [
    {
      id: "mesh",
      x: 0,
      y: 5,
      w: 12,
      h: 6,
      label: "mesh",
      sub: "same port for all",
      accent: true,
    },
    { id: "mat", x: 20, y: 0, w: 16, h: 4, label: "matrix cluster", sub: "fixed datapath" },
    {
      id: "rv32",
      x: 43,
      y: -1,
      w: 18,
      h: 4.5,
      label: "RV32 controller PE",
      sub: "a CU that is a CPU",
      accent: true,
    },
    {
      id: "simd",
      x: 43,
      y: 5.5,
      w: 18,
      h: 4.5,
      label: "SIMD PE",
      sub: "8 int lanes + float tier",
      accent: true,
    },
    {
      id: "simt",
      x: 43,
      y: 12,
      w: 18,
      h: 4.5,
      label: "SIMT PE",
      sub: "threads may diverge · binary32",
      accent: true,
    },
  ],
  edges: [
    { from: "mesh:r", to: "mat:l", dir: "h" },
    { from: "mesh:r", to: "rv32:l", dir: "h", accent: true },
    { from: "mesh:r", to: "simd:l", dir: "h", accent: true },
    { from: "mesh:r", to: "simt:l", dir: "h", accent: true },
  ],
};

// The control processor is structural in the system node; CPU_RV64 chooses
// which one. RV32 answers on the mesh at (0,0); RV64 has no mesh presence and
// is loaded through a host window.
const control = {
  groups: [{ x: -1, y: -2.5, w: 62, h: 17, label: "the system node's control processor — never optional" }],
  nodes: [
    {
      id: "mag",
      x: 2,
      y: 4,
      w: 14,
      h: 5,
      label: "MAG + mover",
      sub: "the execution unit",
      accent: true,
    },
    {
      id: "rv32",
      x: 24,
      y: -0.5,
      w: 18,
      h: 5,
      label: "RV32 complex",
      sub: "CPU_RV64 = 0 · ships · mesh (0,0)",
      accent: true,
    },
    {
      id: "rv64",
      x: 24,
      y: 7.5,
      w: 18,
      h: 5,
      label: "RV64 complex",
      sub: "CPU_RV64 = 1 · built, not finished",
    },
    { id: "os", x: 47, y: 7.5, w: 12, h: 5, label: "an OS", sub: "Sv39 · M/S/U" },
  ],
  edges: [
    { from: "rv32:l", to: "mag:r", dir: "h", accent: true, label: "commands" },
    { from: "rv64:l", to: "mag:r", dir: "h", dash: true },
    { from: "rv64:r", to: "os:l", dir: "h", dash: true, label: "someday hosts" },
  ],
};

const simdt = {
  cols: [
    { key: "q", label: "" },
    { key: "simd", label: "SIMD PE" },
    { key: "simt", label: "SIMT PE" },
  ],
  rows: [
    {
      q: "One instruction drives",
      simd: "8 int lanes + a float tier, one address stream",
      simt: "many threads, each its own address",
    },
    {
      q: "May the lanes disagree?",
      simd: "no — same address, same branch",
      simt: "yes — a mask, a divergence stack, a lane-serialising load/store",
    },
    {
      q: "You pick it when",
      simd: "every element is treated the same",
      simt: "lane 3 takes the if and lane 4 the else",
    },
    { q: "Float", simd: "IEEE binary32", simt: "IEEE binary32 — same units, verbatim" },
  ],
};

const ships = {
  cols: [
    { key: "s", label: "State" },
    { key: "w", label: "What" },
  ],
  rows: [
    {
      s: "On silicon",
      w: "the matrix clusters, the vector cores, the NoC mesh and routers, the system node with its mover and transform slot, the interlink joining four meshes, and 40-bit global addressing.",
    },
    {
      s: "Synthesised, not yet on silicon",
      w: "L2 staging and the endpoint adapter, per-component clock control, the double-pumped matrix core, and the per-domain reset architecture.",
    },
    {
      s: "Built, not finished",
      w: "the RV64 system processor — it elaborates, simulates and runs programs, but in the node its dispatch port and doorbell are still tied off, so it cannot yet drive a compute unit. That is the frontier this direction is being built toward.",
    },
    {
      s: "Software",
      w: "a working driver and compiler; kernels compile to cluster and vector programs; flash attention runs; tinygrad is an optional frontend.",
    },
  ],
};
</script>

<template>
  <div class="container-page pb-24">
    <section class="pt-8 pb-6">
      <p
        class="kt-text-caption uppercase tracking-widest text-warm-400 dark:text-warm-600 font-semibold mb-3"
      >
        The idea · Stage 4 of 4
      </p>
      <h1
        class="text-3xl sm:text-4xl font-semibold tracking-tight text-warm-900 dark:text-warm-100 leading-tight"
      >
        A general SoC design framework.
      </h1>
      <p
        class="kt-text-emphasis text-warm-600 dark:text-warm-400 mt-4 max-w-[72ch] leading-7"
      >
        The last generalisation changed the <i>kind</i> of thing you can build. A
        compute unit does not have to be a fixed datapath — a <b>processor is a
        compute unit too</b> — and once that was true, KohakuAccel stopped being
        only an accelerator framework.
      </p>
    </section>

    <h2 class="doc-h2">Processors on the port</h2>
    <p class="doc-p">
      The framework already ships one: the <b>RV32 controller PE</b>, a compute
      unit that happens to be a processor — kicked with one instruction, it runs
      a program to completion and retires like any other unit. And it has a slot,
      <code>SIMD_EN</code>, for a wide datapath it does not own. That slot is where
      a second project plugs in.
    </p>
    <Fig
      caption="A matrix cluster, an RV32 controller PE, a SIMD PE and a SIMT PE all hang off the same mesh port. To the network, a processor is not special — it accepts a kick and returns a completion exactly as a fixed datapath does. That is what makes 'a mesh of processors' a configuration, not a new machine."
      zoom
      wide
    >
      <BlockDiagram :nodes="procs.nodes" :edges="procs.edges" :groups="procs.groups" />
    </Fig>

    <h2 class="doc-h2">KohakuMPE: we started thinking GPU</h2>
    <p class="doc-p">
      <b>KohakuMPE</b> is the project where the compute units are processors. It
      ships two classes, each the RV32 controller core with a wide datapath bolted
      to its execute stage. The distinction between them is not lane count — it is
      <b>whether the lanes may disagree</b>, on an address or on which side of a
      branch they run. SIMD says no and is cheaper for it; SIMT says yes, pays for
      it, and is a GPU-shaped machine: many threads, per-lane addressing, IEEE
      binary32 with denormals flushed the way D3D11 requires.
    </p>
    <SpecTable :cols="simdt.cols" :rows="simdt.rows" />

    <h2 class="doc-h2">Control becomes a program</h2>
    <p class="doc-p">
      The system node's control processor is <b>structural — there is no parameter
      that removes it</b> — and the mover is that processor's execution unit
      rather than a peer with a command window. What <i>is</i> a parameter is
      <code>CPU_RV64</code>, and it chooses which processor runs the node. Today it
      defaults to 0, so the RV32 complex ships and answers on the mesh at
      <code>(0,0)</code>. The other choice is the ambition: a full <b>RV64IMA +
      Zicsr</b> core with M/S/U privilege, an Sv39 page-table walker, a write-back
      L1 and a machine-mode trap model — a core large enough to host an operating
      system, turning dispatch and scheduling into software on the card.
    </p>
    <Fig
      caption="CPU_RV64 selects the node's processor. The RV32 complex ships today and drives the mover from the mesh. The RV64 complex is built and runs programs, but its integration as the node's dispatching runtime host is not finished — drawn dashed because it is the direction, not the current default."
      zoom
      wide
    >
      <BlockDiagram :nodes="control.nodes" :edges="control.edges" :groups="control.groups" />
    </Fig>

    <h2 class="doc-h2">What that makes it — and what is actually built</h2>
    <p class="doc-p">
      With a programmable processor structural at the node's center, KohakuAccel
      is no longer a way to build one kind of thing. Build a tensor engine, build
      a GPU-shaped mesh of processors, or build a whole programmable
      system-on-chip — on the same parts. That is the platform the idea arrived
      at. It is also, honestly, a work in progress, so here is the line between
      what runs and what is still ahead.
    </p>
    <SpecTable :cols="ships.cols" :rows="ships.rows" />

    <Callout kind="rule" title="The honest trade">
      <p>
        You pay area and some frequency for generality, and a hand-tuned
        specialist will always beat a generalist on its one workload. The bet is
        that <b>a standard codebase is worth more than the last gate</b> — that a
        machine you fork, re-target and reprogram beats one you re-spin from
        scratch. Where that bet is wrong, the framework says so and lets you
        replace the part. Everything measured here is out-of-context synthesis,
        and no frequency is a closed-timing result.
      </p>
    </Callout>

    <div class="mt-8 pt-5 border-t border-warm-200 dark:border-warm-700 flex justify-between gap-4">
      <RouterLink
        to="/idea/framework"
        class="inline-flex items-center gap-1.5 kt-text-body text-warm-500 dark:text-warm-400 no-underline hover:text-warm-800 dark:hover:text-warm-200"
      >
        <span class="i-carbon-arrow-left" />
        The framework
      </RouterLink>
      <RouterLink
        to="/framework"
        class="inline-flex items-center gap-1.5 kt-text-body font-medium text-gem no-underline hover:gap-2.5 transition-all text-right"
      >
        See it in detail — what is on the die
        <span class="i-carbon-arrow-right" />
      </RouterLink>
    </div>
  </div>
</template>
