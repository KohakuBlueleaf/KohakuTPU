<script setup>
/* The three answers to reuse this machine has, and the one it has not built.
 * LUT and DSP figures are out-of-context synthesis of `sysnode` on
 * xcvu13p-fhgb2104-2L-e, Vivado 2024.2, 3.333 ns ask, synth only. */

const tiers = {
  cols: [
    { key: "t", label: "Tier" },
    { key: "what", label: "What it is" },
    { key: "reach", label: "Reached by" },
    { key: "state", label: "State" },
  ],
  rows: [
    {
      t: "<b>DRAM</b>",
      what: "off-chip, behind one AXI master per mesh",
      reach: "an ordinary address",
      state: "shipping",
    },
    {
      t: "<b>Staging</b> — <code>mag_stage</code>",
      what: "on-chip memory at a reserved aperture inside the system node. 2 MB at 4 banks",
      reach: "an ordinary address in the aperture — <b>no tags, no lookup, never a miss</b>",
      state: "shipping",
      _tone: "good",
    },
    {
      t: "<b>Endpoint adapter</b> — <code>noc_l2_adapter</code>",
      what: "a store spliced into one unit's local link, so a unit's traffic can be observed or served without touching the router or the unit",
      reach: "a <code>CU_CTRL</code> instruction",
      state: "proof of concept",
    },
    {
      t: "<b>Tagged L2</b>",
      what: "an actual cache — tags, hits, misses, a coherence story",
      reach: "an address, with the tier deciding whether it goes further",
      state: "<b>designed, not built</b>",
      _tone: "bad",
    },
  ],
}

const shared = {
  cols: [
    { key: "q", label: "What a shared cache would give" },
    { key: "a", label: "What this machine does instead" },
  ],
  rows: [
    {
      q: "<b>one fetch serving many consumers</b> — the broadcast a shared L2 exists to provide",
      a: "<b>shared fetch.</b> One instruction names the set of units consuming the same operand, the lowest-numbered issues a single descriptor, and the memory agent delivers to all of them. Compiler knowledge instead of arbitration and coherence",
      _tone: "good",
    },
    {
      q: "<b>a working set that survives across passes</b>",
      a: "<b>staging.</b> A reserved aperture, addressed directly. It never misses, so nothing has to decide what to evict",
    },
    {
      q: "<b>a format the consumer wants rather than the one memory holds</b>",
      a: "<b>the transform slot.</b> The mover converts once per tensor into staging or back into memory; a fetch is never transformed",
    },
  ],
}

const xform = {
  cols: [
    { key: "c", label: "Config" },
    { key: "lut", label: "LUT", align: "right", mono: true },
    { key: "dsp", label: "DSP48", align: "right", mono: true },
    { key: "wns", label: "WNS", align: "right", mono: true },
  ],
  rows: [
    { c: "1 port, transform in the memory port", lut: "25,773", dsp: "67", wns: "+0.096" },
    { c: "1 port, one shared slot", lut: "<b>22,091</b>", dsp: "<b>35</b>", wns: "+0.096", _tone: "good" },
    { c: "2 ports, transform in the memory port", lut: "36,733", dsp: "99", wns: "+0.088" },
    { c: "2 ports, one shared slot", lut: "<b>28,243</b>", dsp: "<b>35</b>", wns: "+0.096", _tone: "good" },
    {
      c: "2 ports + control processor, transform in the memory port",
      lut: "39,886",
      dsp: "99",
      wns: "<b>−0.372</b>",
      _tone: "bad",
    },
    {
      c: "2 ports + control processor, one shared slot",
      lut: "<b>31,277</b>",
      dsp: "<b>35</b>",
      wns: "<b>+0.096</b>",
      _tone: "good",
    },
  ],
}
</script>

<template>
  <DocPage
    title="Staging, the transform slot and the tagged L2"
    summary="Three answers to reuse, only one of which is a cache — and that one is not built. Why a per-port transform could never have been parallel, what moving it to the memory path measured, and what a tagged tier would have to add beyond shared fetch to be worth building."
    domain="simd"
    status="measured"
    source="src/kohakuaccel/sysnode/ · docs/notes/cache/ · docs/spec/transform-slot.md"
  >
    <p class="doc-p">
      This machine has <b>no cache in the datapath</b>. It has on-chip memory you address, a
      conversion stage the mover drives, and a compiler that knows which units want the same bytes.
      That is a deliberate position, and the honest version of it is that
      <b>any caching proposal here has to say what it adds beyond shared fetch</b>.
    </p>

    <SpecTable :cols="tiers.cols" :rows="tiers.rows" />

    <h2 class="doc-h2">What a shared cache would have been for</h2>

    <SpecTable :cols="shared.cols" :rows="shared.rows" />

    <Callout kind="rule" title="Staging is not a cache and calling it one costs you the design">
      <p>
        A staged read has <b>no tag, no lookup and no miss path</b>: the address decodes into the
        aperture and the store answers. That is why it can serve a 1,024-bit line in one port-A read
        with no burst, and why a staged fetch never transforms — <b>staging holds operand words
        verbatim</b>, so there is nothing to convert on the way out.
      </p>
    </Callout>

    <h2 class="doc-h2">The transform slot, and why one is enough</h2>

    <p class="doc-p">
      The slot converts between the format memory holds and the format a unit wants. It used to sit
      in <b>every memory port</b>, on the theory that a mesh buys conversion throughput by having
      more ports. That theory was wrong, and structurally so rather than by workload.
    </p>

    <Callout kind="trap" title="N transforms could only ever consume one beat per cycle between them">
      <p>
        A per-port transform is fed from <b>that port's AXI R channel</b>. The sysnode's
        single-master concentrator converges <b>every port master onto one DRAM master</b>.
        And a staged read never transforms, because staging holds operand words verbatim.
        So every transformed byte came from one converged master, and <b>N−1 instances were idle by
        construction</b> — not because the workload failed to exercise them.
      </p>
      <p>
        Each instance measured <b>4,490 LUT and 32 DSP</b>. The reason no bench caught it is worth
        keeping too: the mesh bench set up a transformed fetch and then only checked
        <i>“FILL completed”</i>, so it passed unchanged either side of the removal.
      </p>
    </Callout>

    <SpecTable
      :cols="xform.cols"
      :rows="xform.rows"
      caption="Out-of-context synthesis of sysnode on xcvu13p-fhgb2104-2L-e, Vivado 2024.2, 3.333 ns ask. Marginal port cost falls from 10,960 LUT + 32 DSP to 6,152 LUT and no DSP, which is what makes a sysnode with more than two ports affordable"
    />

    <Callout kind="measured" title="The transform was also the path setting WNS">
      <p>
        The memory port recorded it against itself: the read FIFO's BRAM output reached the
        quantiser's DSP control through <b>9 LUT levels, 4.399 ns</b>, and set the worst slack on
        every SLR1 probe. Taking it out of the port recovered <b>0.468 ns</b> — the shipping
        configuration went from <b>−0.372 to +0.096</b>, and the control processor's slack cost went
        from 0.46 ns to <b>zero</b>.
      </p>
      <p>
        The general lesson is in the two one-port rows: <b>a per-port cost measured at one port is
        not a per-port cost</b>, and slack in particular does not extrapolate. At one port the
        processor was free; at two it was not.
      </p>
    </Callout>

    <h2 class="doc-h2">The tagged L2 that is not built</h2>

    <p class="doc-p">
      The tier that does not exist is a real cache attached to a compute unit — tags, hits, misses —
      and its interesting property is not caching at all. It is that
      <b>a tagged tier can carry two address classes</b>: <i>memory</i>, which is cacheable and
      tagged, and <i>communication</i>, which is never tagged and always misses, so a miss
      <i>is</i> the packet.
    </p>

    <Callout kind="note" title="Why that matters more than hit rate">
      <p>
        It is what lets a core that <b>cannot bend to this model</b> — a foreign RISC-V, an A53 —
        join the mesh at all. Such a core cannot emit a <code>MEM_RD_REQ</code>; it emits loads and
        stores. A tier that turns an address in one class into a flit gives it the protocol without
        the core knowing there is one.
      </p>
      <p>
        The unresolved part is blocking behaviour: <i>everything as memory</i> means a communication
        access stalls the core the way a load does, and real CPUs separate that with memory
        attributes — ARM Device versus Normal, x86 UC versus WB, RISC-V PMA. That is a design
        question this machine has not had to answer yet, because its own units speak the protocol
        directly.
      </p>
    </Callout>
  </DocPage>
</template>
