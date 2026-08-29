<script setup>
/**
 * /framework/estimator — a per-knob resource calculator for the two AXI
 * systems, fitted to and validated against one OOC synthesis per configuration.
 * Every coefficient and every row is transcribed in src/content/estimator.js;
 * the model is scripts/py/kx_cost.py.
 */
import {
  PART,
  TARGET_MHZ,
  KX_ROWS,
  KX_LUT,
  KX_FF,
  kxLut,
  kxFf,
  kxUram,
  kxBram,
  STN_ROWS,
  STN_PORT,
  stnEstimate,
} from "@/content/estimator";

const n = (v) => (v == null ? "—" : Math.round(v).toLocaleString());
const pct = (p) => (p == null ? "—" : `${p >= 0 ? "+" : ""}${p.toFixed(2)}%`);

/* ---------------- Kohaku Xache calculator ---------------- */
/* rp: the read engine — 0 the one-beat engine, 1 the streaming engine (RD_OUTQ 4) */
const kx = reactive({ rp: 1, M: 4, N: 4, K: 1, rsamd: 1, wsamd: 1, cdc: 4 });
const kxOut = computed(() => ({
  lut: kxLut(kx.rp, kx.M, kx.N, kx.K, kx.rsamd, kx.wsamd, kx.cdc),
  ff: kxFf(kx.rp, kx.M, kx.N, kx.K, kx.rsamd, kx.wsamd, kx.cdc),
  uram: kxUram(kx.N, kx.K),
  bram: kxBram(kx.cdc),
}));
/* the same setting under the other engine, for the side-by-side line */
const kxOther = computed(() =>
  kxLut(1 - kx.rp, kx.M, kx.N, kx.K, kx.rsamd, kx.wsamd, kx.cdc),
);

/* itemised bill: each knob's contribution at the current setting */
const kxBill = computed(() => {
  const rp = kx.rp;
  const L = (...a) => kxLut(rp, ...a);
  const ship = KX_LUT[rp].ship;
  const rows = [
    {
      k: `ship 4×4 K1 SAMD, no CDC — ${rp ? "streaming" : "one-beat"} engine`,
      v: ship,
    },
    { k: `masters M = ${kx.M}`, v: L(kx.M, 4, 1, 1, 1, 0) - ship },
    { k: `homes N = ${kx.N}`, v: L(4, kx.N, 1, 1, 1, 0) - ship },
    {
      k: "M×N crossbar interaction",
      v:
        L(kx.M, kx.N, 1, 1, 1, 0) -
        L(kx.M, 4, 1, 1, 1, 0) -
        L(4, kx.N, 1, 1, 1, 0) +
        ship,
    },
    {
      k: `cache line K = ${kx.K} × IO width`,
      v: L(4, kx.N, kx.K, 1, 1, 0) - L(4, kx.N, 1, 1, 1, 0),
    },
    {
      k: kx.rsamd ? "read SAMD" : "read SASD (shared engine)",
      v: L(kx.M, kx.N, kx.K, kx.rsamd, 1, 0) - L(kx.M, kx.N, kx.K, 1, 1, 0),
    },
    {
      k: kx.wsamd ? "write SAMD" : "write SASD (shared write path)",
      v:
        L(kx.M, kx.N, kx.K, kx.rsamd, kx.wsamd, 0) -
        L(kx.M, kx.N, kx.K, kx.rsamd, 1, 0),
    },
    {
      k: `${kx.cdc} edge crossing(s), W/R FIFOs in BRAM`,
      v: KX_LUT[rp].CDC * kx.cdc,
    },
  ];
  return rows.map((r) => ({ k: r.k, v: (r.v >= 0 ? "+" : "") + n(r.v) }));
});

/* validation: the model against every measured row, per family */
function validate(rp) {
  const rows = KX_ROWS[rp].map(([M, N, K, r, w, c, lut, ff]) => {
    const el = kxLut(rp, M, N, K, r, w, c);
    const ef = kxFf(rp, M, N, K, r, w, c);
    const pl = (100 * (el - lut)) / lut;
    const pf = ef == null ? null : (100 * (ef - ff)) / ff;
    return {
      cfg: `M${M} N${N} K${K} ${r ? "rSAMD" : "rSASD"} ${w ? "wSAMD" : "wSASD"} cdc${c}`,
      lut: n(lut),
      elut: n(el),
      pl: pct(pl),
      ff: n(ff),
      eff: n(ef),
      pf: pct(pf),
      _pl: Math.abs(pl),
      _pf: pf == null ? 0 : Math.abs(pf),
      _tone:
        Math.abs(pl) >= 3 || (pf != null && Math.abs(pf) >= 3) ? "bad" : "good",
    };
  });
  return {
    rows,
    worstL: Math.max(...rows.map((r) => r._pl)),
    worstF: Math.max(...rows.map((r) => r._pf)),
    fitted: KX_FF[rp] != null,
  };
}
const kxValid = { 0: validate(0), 1: validate(1) };

const kxValidTable = {
  cols: [
    { key: "cfg", label: "configuration", mono: true },
    { key: "lut", label: "LUT measured", mono: true, align: "right" },
    { key: "elut", label: "LUT model", mono: true, align: "right" },
    { key: "pl", label: "err", mono: true, align: "right" },
    { key: "ff", label: "FF measured", mono: true, align: "right" },
    { key: "eff", label: "FF model", mono: true, align: "right" },
    { key: "pf", label: "err", mono: true, align: "right" },
  ],
};

/* ---------------- station bus calculator ---------------- */
const stn = reactive({
  stations: [
    { managers: 0, nsu512: 1, nsu32: 3 },
    { managers: 3, nmu512: 2, nmu32: 1, nsu512: 1, nsu32: 3 },
    { managers: 0, nsu512: 1, nsu32: 3 },
    { managers: 0, nsu512: 1, nsu32: 3 },
  ],
});
const stnOut = computed(() => stnEstimate(stn));
const stnBillTable = {
  cols: [
    { key: "label", label: "item" },
    { key: "n", label: "×", mono: true, align: "right" },
    { key: "each", label: "LUT each", mono: true, align: "right" },
    { key: "lut", label: "LUT", mono: true, align: "right" },
  ],
};
const stnBill = computed(() =>
  stnOut.value.items.map((i) => ({
    label: i.label,
    n: `${i.n}`,
    each: n(i.each),
    lut: `<b>${n(i.lut)}</b>`,
  })),
);
const stnLoops = {
  cols: [
    { key: "loop", label: "loop" },
    { key: "bus", label: "whole bus LUT", mono: true, align: "right" },
    { key: "slr1", label: "SLR1 station", mono: true, align: "right" },
    { key: "hub", label: "SLR1 hub", mono: true, align: "right" },
  ],
  rows: STN_ROWS.map((r) => ({
    loop: r.loop,
    bus: n(r.bus),
    slr1: n(r.slr1),
    hub: n(r.hub),
    _tone: r.loop.includes("design point")
      ? "good"
      : r.loop.includes("refuted")
        ? "bad"
        : undefined,
  })),
};
const shipStn = stnEstimate({ stations: stn.stations }).lut;
</script>

<template>
  <DocPage
    title="Resource estimator"
    summary="A per-knob LUT/FF/URAM/BRAM bill for Kohaku Xache and the station bus, fitted to one out-of-context synthesis per configuration and validated against every one of them."
    domain="framework"
    status="measured"
    :source="`scripts/py/kx_cost.py · src/content/estimator.js · scripts/tcl/ooc_kx.tcl · ${PART} · ${TARGET_MHZ} MHz target · synthesis only`"
  >
    <Callout kind="rule" title="A total is not a knob's cost">
      <p>
        Every figure below is <b>ship + Σ (per-knob measured delta × count)</b>.
        A knob's cost was measured by changing that knob alone and synthesising
        once; a whole configuration's total is never quoted as a knob's price.
        Where two knobs interact — the crossbar scales as M×N, not M+N — the
        interaction is its own measured term. The model is delivered only while
        its error against every measured row is under
        <b>3% for both LUT and FF</b>; the validation table at the end is that
        check, recomputed live.
      </p>
    </Callout>

    <h2 class="doc-h2">Kohaku Xache</h2>
    <p class="doc-p">
      M AXI masters in, N DRAM homes out, one 2 MB cache per home (64 URAM), no
      AXI inside. Knobs: the read engine (one-beat, or streaming with a read
      queue), width (512 here), read and write SASD/SAMD independently, cache
      line = K × IO width, and per-port clock crossings (a crossing only where a
      port's clock differs from the fabric's). Each engine is its own family of
      measured rows and its own fit; <code>RD_OUTQ</code> is not a term — the
      ship at 1 / 2 / 4 / 8 outstanding is within 90 LUT.
    </p>

    <div class="my-5 doc-w card p-4 grid grid-cols-2 md:grid-cols-3 gap-3">
      <label class="kt-text-caption"
        >read engine
        <select v-model.number="kx.rp" class="doc-input">
          <option :value="1">streaming, RD_OUTQ 4 (RD_PIPE=1)</option>
          <option :value="0">one-beat (RD_PIPE=0)</option>
        </select></label
      >
      <label class="kt-text-caption"
        >masters M
        <input
          v-model.number="kx.M"
          type="number"
          min="1"
          max="16"
          class="doc-input"
      /></label>
      <label class="kt-text-caption"
        >homes N
        <input
          v-model.number="kx.N"
          type="number"
          min="1"
          max="16"
          class="doc-input"
      /></label>
      <label class="kt-text-caption"
        >line K (× IO)
        <select v-model.number="kx.K" class="doc-input">
          <option :value="1">1</option>
          <option :value="2">2</option>
          <option :value="4">4</option>
        </select></label
      >
      <label class="kt-text-caption"
        >read datapath
        <select v-model.number="kx.rsamd" class="doc-input">
          <option :value="1">SAMD (per home)</option>
          <option :value="0">SASD (shared)</option>
        </select></label
      >
      <label class="kt-text-caption"
        >write datapath
        <select v-model.number="kx.wsamd" class="doc-input">
          <option :value="1">SAMD (per home)</option>
          <option :value="0">SASD (shared)</option>
        </select></label
      >
      <label class="kt-text-caption"
        >edge crossings (ports on another clock)
        <input
          v-model.number="kx.cdc"
          type="number"
          min="0"
          max="32"
          class="doc-input"
      /></label>
    </div>

    <ResourceBars
      :items="[
        {
          label: 'LUT',
          value: kxOut.lut,
          max: 40000,
          tone: kxOut.lut <= 12000 ? 'good' : 'warn',
        },
        {
          label: 'FF',
          value: kxOut.ff ?? 0,
          max: 40000,
          note: kxOut.ff == null ? 'fit pending' : undefined,
        },
        {
          label: 'URAM',
          value: kxOut.uram,
          max: 1280,
          note: 'of 1,280',
          tone: 'accent',
        },
        {
          label: 'BRAM',
          value: kxOut.bram,
          max: 2688,
          note: 'of 2,688',
          tone: 'accent',
        },
      ]"
      unit="estimated, out-of-context synthesis"
      :caption="`${kx.rp ? 'streaming engine' : 'one-beat engine'} · M${kx.M} N${kx.N} K${kx.K} · ${kx.rsamd ? 'rSAMD' : 'rSASD'} ${kx.wsamd ? 'wSAMD' : 'wSASD'} · ${kx.cdc} crossings · the ${kx.rp ? 'one-beat' : 'streaming'} engine at this setting: ${n(kxOther)} LUT`"
    />

    <SpecTable
      :cols="[
        { key: 'k', label: 'term' },
        { key: 'v', label: 'LUT', mono: true, align: 'right' },
      ]"
      :rows="kxBill"
      caption="The itemised bill for the setting above. Each line is a measured delta from the family's ship row, at the measured step nearest the setting; between measured steps the delta is interpolated."
    />

    <Callout
      kind="measured"
      title="What the table found that a linear model could not"
    >
      <p>
        LUT is <b>convex in M and in K</b>: with the one-beat engine 2 → 4 → 8
        masters cost 5,287 → 8,408 → 14,242 (each doubling costs more than the
        last, because each home's M:1 mux grows), and K 1 → 2 → 4 costs +4,119
        then +8,896 per four homes. FF is linear in the same knobs to within
        1.3%. So LUT is a step table of measured deltas and FF is a
        least-squares fit — the two resources need different models.
      </p>
      <p>
        <b>Read-SASD and write-SASD do not add.</b> On the current array
        read-SASD alone <i>costs</i> 130 LUT (one engine's arbiter over all M×N
        slots replaces N four-way ones), write-SASD alone saves 1,652, and both
        together save 2,052 — the shared read engine only pays once the write
        side is shared too. The model keeps the three measured points as three
        cases and scales each with the M·N growth measured at 8×8.
      </p>
    </Callout>

    <h3 class="doc-h3">
      Validation — every measured row, the streaming engine
    </h3>
    <SpecTable
      :cols="kxValidTable.cols"
      :rows="kxValid[1].rows"
      :caption="
        kxValid[1].fitted
          ? `max |error|: LUT ${kxValid[1].worstL.toFixed(2)}% · FF ${kxValid[1].worstF.toFixed(2)}% · target < 3% on both · one ooc_kx.tcl synthesis per row · RD_PIPE=1 RD_OUTQ=4`
          : `LUT max |error| ${kxValid[1].worstL.toFixed(2)}% over the rows landed; the FF fit needs the family's full grid`
      "
    />
    <h3 class="doc-h3">Validation — every measured row, the one-beat engine</h3>
    <SpecTable
      :cols="kxValidTable.cols"
      :rows="kxValid[0].rows"
      :caption="`max |error|: LUT ${kxValid[0].worstL.toFixed(2)}% · FF ${kxValid[0].worstF.toFixed(2)}% · target < 3% on both · one ooc_kx.tcl synthesis per row · RD_PIPE=0`"
    />

    <h2 class="doc-h2">Station bus</h2>
    <p class="doc-p">
      Stations are isolated, so the bus is a sum: each station's hub set plus
      its manager (NMU) and subordinate (NSU) ports, plus one link pair per
      boundary. Each port's cost is its own instance in the ship's hierarchy
      report at the design point (loop 3).
    </p>
    <SpecTable
      :cols="stnBillTable.cols"
      :rows="stnBill"
      :caption="`the ship line, itemised · total ${n(stnOut.lut)} LUT · measured whole bus 23,053 (the remainder is per-station reset sync and tie-offs)`"
    />
    <SpecTable
      :cols="stnLoops.cols"
      :rows="stnLoops.rows"
      caption="The four station loops. Two levers refuted by measurement are kept in the table: a total that looked like a shrink and was a barrel shifter, and a synthesis attribute that added a stage."
    />
    <Callout
      kind="measured"
      title="A narrow port is cheaper only where it can be single-beat"
    >
      A full-AXI4 32-bit subordinate costs 808 LUT against a 512-bit one's 760:
      its control (the burst split that lets it accept an 8 KB flit burst) does
      not shrink with width. A port proven single-transaction — a config
      register block — takes <code>SINGLE_BEAT</code> and drops to about
      {{ n(STN_PORT.nsu_32_single_beat) }}: its three channel queues become
      one-entry skids. The ship's 32-bit subordinates take bursts, so the ship
      cannot use it; the knob is verified by the station bench.
    </Callout>
  </DocPage>
</template>

<style scoped>
.doc-input {
  display: block;
  width: 100%;
  margin-top: 0.25rem;
  padding: 0.35rem 0.5rem;
  border: 1px solid var(--kt-border, #ccc);
  border-radius: 0.375rem;
  background: transparent;
  font-family: ui-monospace, monospace;
}
</style>
