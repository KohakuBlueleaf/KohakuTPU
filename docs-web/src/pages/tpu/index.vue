<script setup>
/* KohakuTPU overview. Every figure here is one accelerator on one part —
 * xcvu13p-fhgb2104-2L-e — and says which build it came from. */

const whatItComputes = {
  cols: [
    { key: "k", label: "" },
    { key: "v", label: "", mono: true },
  ],
  rows: [
    {
      k: "element format",
      v: "int7 significand + E5M3 scale, one scale per 32 along K",
    },
    { k: "tensor CU", v: "4x8x4 — 64 DSP48E2, 128 MAC/cycle" },
    {
      k: "cluster",
      v: "4 tensor CUs + 1 accumulator — 4x32x4, <b>512 MAC/cycle</b>",
    },
    {
      k: "accumulate",
      v: "<code>S1 E7 M14</code> (FP22), one add per 32 multiplies",
    },
    { k: "supported shape", v: "M = 4a, N = 4b, K = 32c" },
    {
      k: "vector core",
      v: "16 lanes of E8M15, three DSPs each, four transcendentals at full rate",
    },
  ],
};

/* One mesh: what a compute unit actually attaches to. */
const meshShape = {
  nodes: [
    {
      id: "ddr",
      x: 0,
      y: 0,
      w: 13,
      label: "DDR4 channel",
      sub: "4 GB, one per SLR",
    },
    {
      id: "mag",
      x: 0,
      y: 5,
      w: 13,
      label: "memory agent",
      sub: "descriptors, tagged responses, transform slot",
      accent: true,
    },
    {
      id: "mesh",
      x: 0,
      y: 10,
      w: 30,
      h: 4,
      label: "mesh",
      sub: "routers and local ports",
      accent: true,
    },
    {
      id: "clu",
      x: 0,
      y: 20,
      w: 14,
      label: "matmul cluster",
      sub: "928-bit L1 x2 · 304 DSP · macro-op",
    },
    {
      id: "vec",
      x: 16,
      y: 20,
      w: 14,
      label: "vector core",
      sub: "256-bit L1 · 51 DSP · a program",
    },
  ],
  edges: [
    { from: "ddr:b", to: "mag:t", dir: "v" },
    {
      from: "mag:b",
      to: "mesh:t",
      dir: "v",
      accent: true,
      label: "MXFP7 quantiser",
    },
    { from: "mesh:b", to: "clu:t", dir: "v", accent: true, label: "one port" },
    { from: "mesh:b", to: "vec:t", dir: "v", accent: true, label: "one port" },
  ],
  groups: [{ x: -1.4, y: 18.2, w: 32.8, h: 5.4, label: "two units, one port" }],
};

/* The four-mesh line, as enumerated off the multi-mesh build. */
const ship = {
  nodes: [
    {
      id: "m0",
      x: 0,
      y: 0,
      w: 12,
      label: "mesh_0",
      sub: "SLR0 · ddr4_2 · 6+2",
    },
    {
      id: "m1",
      x: 15,
      y: 0,
      w: 12,
      label: "mesh_1",
      sub: "SLR1 · ddr4_3 · 6+0",
      accent: true,
    },
    {
      id: "m3",
      x: 30,
      y: 0,
      w: 12,
      label: "mesh_3",
      sub: "SLR2 · ddr4_1 · 6+2",
    },
    {
      id: "m2",
      x: 45,
      y: 0,
      w: 12,
      label: "mesh_2",
      sub: "SLR3 · ddr4_0 · 6+2",
    },
    {
      id: "xdma",
      x: 15,
      y: 7,
      w: 12,
      label: "XDMA / PCIe",
      sub: "76,319 LUT — 17.7% of an SLR",
    },
  ],
  edges: [
    { from: "m0:r", to: "m1:l", dir: "h", accent: true, label: "link" },
    { from: "m1:r", to: "m3:l", dir: "h", accent: true, label: "link" },
    { from: "m3:r", to: "m2:l", dir: "h", accent: true, label: "link" },
    { from: "m1:b", to: "xdma:t", dir: "v" },
  ],
};

const twoUnits = {
  cols: [
    { key: "k", label: "" },
    { key: "c", label: "matmul cluster" },
    { key: "v", label: "vector core" },
  ],
  rows: [
    { k: "operand memory width", c: "<b>928 bits</b>", v: "<b>256 bits</b>" },
    {
      k: "operand memories",
      c: "<b>two</b> — <code>u_l1a</code>, <code>u_l1b</code>; A and B are separate RAMs",
      v: "<b>one</b> flat scratchpad",
    },
    {
      k: "memories in the unit",
      c: "<b>five</b> L1-class RAMs: two per manager plus each node's own accumulator tile",
      v: "operand L1, an instruction memory in distributed LUTRAM, and a register file mirrored three times to synthesise three read ports",
    },
    {
      k: "read latency",
      c: "1 on L1, <b>2</b> on the accumulator tile",
      v: "1, and the walk derives from the primitive rather than assuming it",
    },
    {
      k: "what an instruction is",
      c: "a macro-op — one flit becomes hundreds of internal commands",
      v: "a program — words loaded into instruction memory, then entered",
    },
    { k: "element format", c: "int7 with a shared block scale", v: "E8M15" },
    {
      k: "mesh port",
      c: "<b>one, identical</b>",
      v: "<b>one, identical</b>",
      _tone: "good",
    },
  ],
};

const exercises = {
  cols: [
    { key: "f", label: "framework" },
    { key: "u", label: "how KohakuTPU uses it" },
  ],
  rows: [
    {
      f: "compute-unit port",
      u: "two unit types on the same contract — a cluster and a vector core, one with a macro-op and one with a program",
    },
    {
      f: "instruction payload",
      u: "three cluster opcodes in a 256-bit payload; 32-bit vector words, eight per payload, inside a load-and-run envelope",
    },
    {
      f: "flit format",
      u: "operand words sized so 32 int7 elements plus 4 scales fill the payload exactly",
    },
    {
      f: "memory protocol",
      u: "streaming descriptors, out-of-order tagged responses, burst writes, and a per-request quantise flag",
    },
    {
      f: "memory agent",
      u: "KohakuTPU's own quantiser occupies the framework's transform slot at id 1, reachable only by the memory mover",
    },
    {
      f: "mesh",
      u: "unit-to-unit bulk transfer, used for peer accumulation at full accumulator width",
    },
    {
      f: "ship assembly",
      u: "four independent meshes, one per SLR, joined by the interlink",
    },
    { f: "measurement flow", u: "every figure on the results page" },
  ],
};

const categories = {
  cols: [
    { key: "c", label: "category" },
    { key: "m", label: "meaning" },
    { key: "w", label: "what falls here" },
  ],
  rows: [
    {
      c: "<b>fixed protocol</b>",
      m: "cannot be changed by a project",
      w: "the mesh port's six signals and its retry flow control; the flit header; how an instruction arrives and a completion returns; the memory request/response protocol",
    },
    {
      c: "<b>customizable addon</b>",
      m: "ships working, meant to be swapped",
      w: "<b>the MXFP7 quantiser</b> — KohakuTPU's number format plugged into the memory agent's transform stage; staging or an L2 in the same agent, if it is ever built",
    },
    {
      c: "<b>convention</b>",
      m: "how to design a thing — some forced by the agent's design, some free",
      w: "operands stored tile-major so a fill is one instruction; K swept outermost inside a sweep and innermost across chunks; naming a memory primitive rather than inferring it",
    },
    {
      c: "<b>yours</b>",
      m: "the project's own, top to bottom",
      w: "<b>almost everything else</b>: the number format, the DSP packing, the cascade, the accumulator and its tile, the vector ALU, both instruction sets, the compiler, the mesh populations",
      _tone: "good",
    },
  ],
};

const status = {
  cols: [
    { key: "p", label: "part" },
    { key: "s", label: "state" },
  ],
  rows: [
    {
      p: "matmul datapath",
      s: "<b>built and verified</b> against both a behavioural model and a real DSP48E2",
      _tone: "good",
    },
    {
      p: "accumulator",
      s: "<b>built</b>, FP22, resident tile, peer transfer reachable",
      _tone: "good",
    },
    {
      p: "cluster as an endpoint",
      s: "<b>built</b>, one mesh port, closes with margin",
      _tone: "good",
    },
    {
      p: "quantiser",
      s: "<b>built</b>, as the transform slot's occupant at id 1; a fetch is never transformed",
      _tone: "good",
    },
    {
      p: "vector ALU",
      s: "<b>built and measured</b> — FMA within one ulp (correctly rounded outside one stated subtractive corner), faithful seeds",
      _tone: "good",
    },
    {
      p: "vector core around it",
      s: "<b>built</b>, and its instruction set partly so",
    },
    {
      p: "driver and hand-built encoders",
      s: "<b>run on the card</b>",
      _tone: "good",
    },
    {
      p: "compiler path",
      s: "one path, <code>kohakutpu.lang</code> to <code>kohakutpu.isa</code>; cluster <b>and</b> vector ops emit",
    },
    {
      p: "tinygrad frontend",
      s: "built on 0.13 — matmul, epilogues and elementwise chains lower and run",
    },
    {
      p: "tensor-descriptor ISA",
      s: "designed, walker built and validated, <b>not wired in</b>",
      _tone: "warn",
    },
    {
      p: "chain bypass, <code>FWD</code>",
      s: "<b>not built</b>",
      _tone: "warn",
    },
    {
      p: "split-K epilogue on a vector core",
      s: "designed, not built",
      _tone: "warn",
    },
    {
      p: "place-and-route on a populated die",
      s: "<b>not done</b> for any cluster-count configuration",
      _tone: "bad",
    },
  ],
};

const layering = {
  cols: [
    { key: "o", label: "one of these" },
    { key: "b", label: "becomes" },
  ],
  rows: [
    { o: "one <code>GEMM</code> flit", b: "256 accumulator commands" },
    { o: "one <code>FILL</code> flit", b: "128 response flits" },
    { o: "four flits", b: "a whole 32x128x32 matmul" },
  ],
};
</script>

<template>
  <DocPage
    title="The accelerator"
    summary="An MXFP7 tensor accelerator on xcvu13p-fhgb2104-2L-e: two compute units that share nothing but a mesh port, an instruction set spent on the datapath, and a compiler that plans against the machine's own capacities."
    domain="tpu"
    status="measured"
    source="xcvu13p-fhgb2104-2L-e · docs/projects/kohakutpu/README.md · ship.md"
  >
    <p class="doc-p">
      <code>C = A · B</code> in MXFP7: a 7-bit signed significand with an E5M3
      scale shared by a block of 32, multiplied inside DSP48E2s and reduced as
      exact integers, with floating point reached once per 32 MACs. Operands and
      results in memory are FP16; the internal format is never visible to
      software.
    </p>

    <SpecTable
      :cols="whatItComputes.cols"
      :rows="whatItComputes.rows"
      caption="One accelerator on xcvu13p-fhgb2104-2L-e. The whole machine is AMP FP16-MXFP7, so the throughput unit is FLOPS rather than IOPS: the integer datapath is an implementation of a floating-point multiply whose exponent has been factored out of the block."
    />

    <h2 class="doc-h2">Two units, one port — and nothing else in common</h2>

    <p class="doc-p">
      This is the project's strongest single piece of evidence about the
      framework, and it is an argument about <i>flexibility</i> rather than
      about fit. KohakuTPU contains two compute units. They are the same shape
      at exactly one place: the mesh port.
    </p>

    <Fig
      caption="What one mesh holds. The framework fixed how a unit receives work and returns results and nothing else; everything behind that boundary diverged completely, down to the number of memories, their widths, their primitives and their latencies."
      zoom
    >
      <BlockDiagram
        :nodes="meshShape.nodes"
        :edges="meshShape.edges"
        :groups="meshShape.groups"
      />
    </Fig>

    <SpecTable :cols="twoUnits.cols" :rows="twoUnits.rows" />

    <Callout kind="rule" title="Two answers, not one pattern">
      <p>
        None of the structure on these pages should be read as “the way a
        compute unit is built”. A 928-bit L1 with a separate A and B RAM is what
        a DSP cascade eating eight operand words per cycle needs; a 256-bit flat
        scratchpad is what a 16-lane SIMD core needs. The fact that both answers
        reached the mesh through the same six signals is what the framework is
        claiming.
      </p>
    </Callout>

    <h2 class="doc-h2">The ship shape</h2>

    <p class="doc-p">
      Two facts about <code>xcvu13p-fhgb2104-2L-e</code> decide the whole
      assembly. <b>DSP48E2 rather than DSP58</b>, so there is no native INT8
      SIMD and the packing on the matmul page exists to build one. And
      <b>four SLRs</b>, so the machine is four machines: exactly one DDR4
      controller per SLR, and a DDR4 interface cannot span SLRs.
    </p>

    <p class="doc-p">
      The obvious arrangement is one large mesh spanning the die. It was
      implemented, and rejected on measurement: its worst path was
      <b>4.6 ns at 98.3% routing with zero logic levels</b>. A path that is
      almost entirely route and has no logic in it cannot be fixed by pipelining
      the logic, because there is none.
    </p>

    <Fig
      caption="The four meshes are a line, not a grid — an SLL joins only adjacent SLRs, so the buildable fabric is the SLR stack in order and the mesh ids are not in SLR order. Populations shown are read off the multi-mesh build's own control-plane enumeration; the four-mesh plan in ship.md names 6+4 / 4+4 / 4+4 / 6+4 instead. mesh_0 to mesh_2 is three hops — the diameter, and precisely the pair a 2x2 grid would have made adjacent."
      zoom
      wide
    >
      <BlockDiagram :nodes="ship.nodes" :edges="ship.edges" />
    </Fig>

    <Callout
      kind="trap"
      title="A population is a property of a build, never of “the ship”"
    >
      <p>
        The four-mesh plan is 6+4 / 4+4 / 4+4 / 6+4. A placed run measured
        meshes of 6+2 and 6+0 from the placed hierarchy. Both are true of their
        own build. Check which build a figure came from before carrying it — and
        read unit populations off the card's control plane rather than trusting
        a document.
      </p>
    </Callout>

    <p class="doc-p">
      <b>mesh_1 has no vector core.</b> SLR1 holds the host interface, so it is
      the most crowded die and its mesh gave up the vector cores rather than the
      clusters. The consequence is not “mesh_1 is a spare”: it is six perfectly
      good matmul clusters that can run any stage needing no epilogue, and
      nothing else.
    </p>

    <h2 class="doc-h2">What it demonstrates</h2>

    <p class="doc-p">
      <b>That a compute unit can be almost all datapath.</b> Every MAC is 0 LUT,
      0 FF, 1 DSP — the multiply and the whole K=32 reduction happen inside the
      DSPs. Against the FP8 design it replaced, the same 128 MACs cost 1,188 LUT
      instead of 12,731, and essentially all of the difference is accumulation
      leaving the fabric.
    </p>

    <p class="doc-p">
      <b>That the framework's port contract is enough to feed one.</b> A cluster
      attaches with a single mesh port. It works because the resident output
      tile creates enough operand reuse to bring the demand under one word per
      cycle — which is an arithmetic property of the datapath, not a concession
      the framework made.
    </p>

    <p class="doc-p"><b>That the layering pays for itself.</b></p>

    <SpecTable
      :cols="layering.cols"
      :rows="layering.rows"
      caption="Every level exists to stop the level above it from having to say the same thing 256 times. Counts are from the worked pass in isa.md §8: C[32,32] = A[32,128] @ B.T[32,128] on one cluster."
    />

    <p class="doc-p">
      <b
        >That the thing that limits a dataflow machine is usually not what it
        looks like.</b
      >
      Three separate times a measurement pointed at bandwidth and was wrong, and
      the machine went from 6.8% of its own datapath peak to 87.6% without
      widening a single bus.
    </p>

    <h2 class="doc-h2">Which framework features it exercises</h2>

    <SpecTable :cols="exercises.cols" :rows="exercises.rows" />

    <h3 class="doc-h3">Which category is which</h3>

    <p class="doc-p">
      The tree distinguishes four kinds of thing, and a project page is only
      useful if it says which kind each of its subjects is.
    </p>

    <SpecTable
      :cols="categories.cols"
      :rows="categories.rows"
      caption="Most of KohakuTPU is in the last row, and saying so is what makes the framework claim credible. A framework that had dictated the datapath would not have needed a project to prove anything."
    />

    <h2 class="doc-h2">Status</h2>

    <SpecTable :cols="status.cols" :rows="status.rows" />

    <Callout kind="trap" title="Two open defects, recorded rather than hidden">
      <p>
        <b>Silent FP16 saturation</b> on the way out of the accumulator — the
        value survives the entire reduction intact and is destroyed at the
        conversion, which clamps at 65,504 with no signal.
      </p>
      <p>
        <b>An L1 footprint band that returns wrong data</b> — a vector kernel
        whose buffers occupy 352 to 480 of the core's 512 L1 words returns wrong
        data, currently guarded rather than fixed.
      </p>
    </Callout>

    <h2 class="doc-h2">How to read the rest</h2>

    <p class="doc-p">
      The order below is the order the decisions were forced, and each page
      assumes the one before it.
    </p>

    <div class="grid gap-3 sm:grid-cols-2 mt-4">
      <RouterLink to="/tpu/numbers" class="card-hover p-4 no-underline block">
        <div
          class="kt-text-caption font-semibold text-warm-800 dark:text-warm-200"
        >
          1 · MXFP7 and the dtype ladder
        </div>
        <p
          class="kt-text-caption text-warm-500 dark:text-warm-400 leading-6 mt-1"
        >
          The format sets the operand width, which sets the packing, which sets
          the cascade depth, which sets the block size. Start here or nothing
          else will look motivated.
        </p>
      </RouterLink>
      <RouterLink to="/tpu/matmul" class="card-hover p-4 no-underline block">
        <div
          class="kt-text-caption font-semibold text-warm-800 dark:text-warm-200"
        >
          2 · The matmul cluster
        </div>
        <p
          class="kt-text-caption text-warm-500 dark:text-warm-400 leading-6 mt-1"
        >
          Two int7 MACs per DSP sharing an activation through the pre-adder, the
          packing offset, the guard-bit budget, and the cascade that reduces
          K=32 without touching the fabric.
        </p>
      </RouterLink>
      <RouterLink to="/tpu/memory" class="card-hover p-4 no-underline block">
        <div
          class="kt-text-caption font-semibold text-warm-800 dark:text-warm-200"
        >
          3 · Residency and accumulators
        </div>
        <p
          class="kt-text-caption text-warm-500 dark:text-warm-400 leading-6 mt-1"
        >
          The resident output tile, why its size decides the port count, the
          reuse contract, and where floating point starts.
        </p>
      </RouterLink>
      <RouterLink to="/tpu/vector" class="card-hover p-4 no-underline block">
        <div
          class="kt-text-caption font-semibold text-warm-800 dark:text-warm-200"
        >
          4 · The vector core
        </div>
        <p
          class="kt-text-caption text-warm-500 dark:text-warm-400 leading-6 mt-1"
        >
          The second unit: E8M15 chosen so an FMA fits one DSP exactly, four
          base-2 seeds at full rate, and what it deliberately does not do.
        </p>
      </RouterLink>
      <RouterLink
        to="/tpu/results"
        class="card-hover p-4 no-underline block sm:col-span-2"
      >
        <div
          class="kt-text-caption font-semibold text-warm-800 dark:text-warm-200"
        >
          5 · What was measured
        </div>
        <p
          class="kt-text-caption text-warm-500 dark:text-warm-400 leading-6 mt-1"
        >
          Every measured number with its conditions — resources, Fmax by block,
          per-SLR capacity, accuracy, throughput, and what closed and what did
          not.
        </p>
      </RouterLink>
    </div>
  </DocPage>
</template>
