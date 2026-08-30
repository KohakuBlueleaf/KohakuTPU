<script setup>
/**
 * /component/pxache — the Partitioned Xache, kx_pxache: the Xache with its
 * masters and homes spread over P partitions of one clock.
 *
 * Drawn from, in full:
 *   docs/projects/kohakuaxi/pxache.md
 *   src/kohakuaxi/pxache/{kx_pxache,lane/kx_hop,lane/kx_lane}.v
 *   tests/axi/{kx_hop_tb,kx_lane_tb,kx_pxache_tb}.v · scripts/tcl/ooc_mod.tcl
 *
 * Every number is transcribed once, in src/content/estimator.js (KX_PX), and
 * read from there. One part, xcvu13p-fhgb2104-2L-e, out-of-context SYNTHESIS
 * at a 300 MHz ask — nothing is placed, and OOC does not know a die.
 */
import { PART, TARGET_MHZ, KX_PX } from "@/content/estimator";

const n = (v) => (v == null ? "—" : Math.round(v).toLocaleString());

/* ------------------------------------------------------------ structure */
/* Three partitions of one clock; master 0's request lane going up and home
   2's response lane coming down, each a chain of hops with a tap after
   every boundary. Validated against kx_pxache.v: a tap IS the home's slot
   for that master (request side) or the master's source from that home
   (response side); the local pair is wires; the head of a landing ring feeds
   the tap and the next hop's TX register with only the valids differing. */
/* Rows per partition: the master's port, then the response lane's landing
   ring and tap (its consumer is the port above it), then the request lane's
   (its consumer is the home below it), then the home — so no tap crosses the
   other lane, and the drawing has no crossing. */
const lanes = {
  groups: [
    { x: -1, y: -1, w: 29, h: 27.5, label: "partition 0 — rstn_p[0]" },
    { x: 29, y: -1, w: 29, h: 27.5, label: "partition 1 — rstn_p[1]" },
    { x: 59, y: -1, w: 29, h: 27.5, label: "partition 2 — rstn_p[2]" },
  ],
  nodes: [
    {
      id: "p0",
      x: 0,
      y: 0,
      w: 16,
      h: 4.2,
      label: "master 0's port",
      sub: "read slots + reorder ring · write slots · AR / AW-W lane TX",
      accent: true,
    },
    {
      id: "rs0",
      x: 18,
      y: 8,
      w: 9,
      h: 4,
      label: "landing ring, tap",
      sub: "home 2's R/B lane at partition 0: master 0's source",
    },
    {
      id: "h0",
      x: 0,
      y: 21,
      w: 16,
      h: 4.2,
      label: "home 0",
      sub: "kx_carray · engines · R/B lane TX",
    },
    {
      id: "p1",
      x: 30,
      y: 0,
      w: 16,
      h: 4.2,
      label: "master 1's port",
      sub: "its own lanes, not drawn",
    },
    {
      id: "rs1",
      x: 48,
      y: 8,
      w: 9,
      h: 4,
      label: "landing ring, tap",
      sub: "home 2's R/B lane at partition 1: master 1's source",
    },
    {
      id: "rq1",
      x: 32,
      y: 14,
      w: 9,
      h: 4,
      label: "landing ring, tap",
      sub: "master 0's AR / AW-W lane at partition 1",
      accent: true,
    },
    {
      id: "h1",
      x: 30,
      y: 21,
      w: 16,
      h: 4.2,
      label: "home 1",
      sub: "slot for master 0 = the tap above",
    },
    {
      id: "p2",
      x: 60,
      y: 0,
      w: 16,
      h: 4.2,
      label: "master 2's port",
      sub: "its own lanes, not drawn",
    },
    {
      id: "rq2",
      x: 62,
      y: 14,
      w: 9,
      h: 4,
      label: "landing ring, tap",
      sub: "master 0's lane at partition 2, its last tap",
      accent: true,
    },
    {
      id: "h2",
      x: 60,
      y: 21,
      w: 16,
      h: 4.2,
      label: "home 2",
      sub: "kx_carray · engines · R/B lane TX toward partition 0",
      accent: true,
    },
  ],
  edges: [
    { from: "p0:b", to: "h0:t", label: "local pair: wires", dir: "v" },
    {
      from: "h0:t",
      to: "p0:b",
      label: "local R/B: wires",
      dir: "v",
      dash: true,
    },
    { from: "p0:b", to: "rq1:l", label: "hop 0→1", accent: true },
    { from: "rq1:b", to: "h1:t", label: "take: dst = home 1", dir: "v" },
    {
      from: "rq1:r",
      to: "rq2:l",
      label: "forward: the head is next TX",
      dir: "h",
      accent: true,
    },
    { from: "rq2:b", to: "h2:t", label: "take: dst = home 2", dir: "v" },
    { from: "h2:r", to: "rs1:r", label: "hop 2→1: R/B flit" },
    { from: "rs1:t", to: "p1:b", label: "take: dst = master 1" },
    { from: "rs1:l", to: "rs0:r", label: "hop 1→0: forward", dir: "h" },
    {
      from: "rs0:l",
      to: "p0:r",
      label: "take: lands at slot, beat",
      accent: true,
    },
  ],
};

/* ------------------------------------------------------------------ hop */
const hop = {
  groups: [
    { x: -1, y: -1, w: 24, h: 12.5, label: "sender's partition — s_rstn" },
    { x: 27, y: -1, w: 41, h: 12.5, label: "receiver's partition — m_rstn" },
  ],
  nodes: [
    {
      id: "src",
      x: 0,
      y: 0,
      w: 8,
      h: 3.2,
      label: "s_valid / s_data",
      sub: "the lane head",
    },
    {
      id: "tx",
      x: 12,
      y: 0,
      w: 10,
      h: 3.6,
      label: "TX register",
      sub: "tx_v, tx_d: its only load is the wire",
      accent: true,
    },
    {
      id: "cred",
      x: 0,
      y: 6,
      w: 10,
      h: 3.6,
      label: "credit counter",
      sub: "DEPTH at reset; −1 per send, +1 per cr",
    },
    {
      id: "cr",
      x: 13,
      y: 6,
      w: 8,
      h: 3.2,
      label: "cr, fok regs",
      sub: "landing the pulses",
    },
    {
      id: "ram",
      x: 28,
      y: 0,
      w: 14,
      h: 3.6,
      label: "landing ring — the RAM",
      sub: "write port registers WE/ADDR/DIN: the wire lands here. dst + kind bits in LUTRAM",
      accent: true,
    },
    {
      id: "rd",
      x: 46,
      y: 0,
      w: 12,
      h: 3.6,
      label: "read stage",
      sub: "issue when the output is free or taken; rd_data holds",
    },
    {
      id: "dst",
      x: 61,
      y: 0,
      w: 6,
      h: 3.6,
      label: "the head",
      sub: "tap, next TX",
    },
    {
      id: "pp",
      x: 28,
      y: 6,
      w: 14,
      h: 3.6,
      label: "pp, fok_rx registers",
      sub: "a pop as a pulse; ring out of reset",
    },
  ],
  edges: [
    {
      from: "src:r",
      to: "tx:l",
      label: "send: valid & credit & fok",
      dir: "h",
    },
    {
      from: "tx:r",
      to: "ram:l",
      label: "boundary wire",
      dir: "h",
      accent: true,
    },
    { from: "ram:r", to: "rd:l", label: "1 read stage", dir: "h" },
    { from: "rd:r", to: "dst:l", label: "m_valid", dir: "h" },
    { from: "rd:b", to: "pp:t", label: "pop", dir: "v" },
    {
      from: "pp:l",
      to: "cr:r",
      label: "boundary wire, back",
      dir: "h",
      accent: true,
    },
    { from: "cr:l", to: "cred:r", label: "+1", dir: "h" },
    { from: "cred:t", to: "tx:b", label: "credit ≠ 0", dir: "v" },
  ],
};

/* ----------------------------------------------------------- master port */
const port = {
  groups: [
    {
      x: -1,
      y: -1,
      w: 70,
      h: 22,
      label: "one master's port, in its partition",
    },
  ],
  nodes: [
    {
      id: "ar",
      x: 0,
      y: 0,
      w: 8,
      h: 3.2,
      label: "AR",
      sub: "from the master's edge",
    },
    {
      id: "slot",
      x: 12,
      y: 0,
      w: 14,
      h: 4,
      label: "read slot",
      sub: "rq_wp: {home, id, beats}; a 4 KB page of the ring is its",
      accent: true,
    },
    {
      id: "arf",
      x: 32,
      y: 0,
      w: 13,
      h: 3.6,
      label: "AR flit, the slot in it",
      sub: "{slot, id, addr, len, size} → local slot or a lane",
    },
    {
      id: "srcs",
      x: 0,
      y: 8,
      w: 11,
      h: 4.4,
      label: "R sources",
      sub: "local engines' r_val · the taps of every remote home's lane",
    },
    {
      id: "pick",
      x: 15,
      y: 8,
      w: 11,
      h: 4.4,
      label: "pick",
      sub: "combinational: the drain's home first, else lowest valid",
      accent: true,
    },
    {
      id: "ring",
      x: 32,
      y: 8,
      w: 14,
      h: 4.4,
      label: "reorder ring — BRAM",
      sub: "RD_OUTQ pages × 64 beats; a beat lands the cycle it is offered",
      accent: true,
    },
    {
      id: "drain",
      x: 52,
      y: 8,
      w: 12,
      h: 4.4,
      label: "drain",
      sub: "the oldest slot, beat by beat as its beats land; id travels with the beat",
    },
    { id: "r", x: 66, y: 8, w: 3, h: 4.4, label: "R", sub: "" },
    { id: "aw", x: 0, y: 16, w: 8, h: 3.2, label: "AW · W", sub: "" },
    {
      id: "wslot",
      x: 12,
      y: 16,
      w: 14,
      h: 4,
      label: "write slot",
      sub: "{home, id}; W beats to the AW's home, next AW after the last beat",
    },
    {
      id: "bfill",
      x: 32,
      y: 16,
      w: 14,
      h: 4,
      label: "B lands at once",
      sub: "completes the oldest open slot bound for its home",
    },
    {
      id: "b",
      x: 52,
      y: 16,
      w: 12,
      h: 4,
      label: "B in issue order",
      sub: "wq_dp",
    },
  ],
  edges: [
    { from: "ar:r", to: "slot:l", label: "rd_ok", dir: "h" },
    { from: "slot:r", to: "arf:l", dir: "h" },
    { from: "srcs:r", to: "pick:l", label: "rs_v", dir: "h" },
    {
      from: "pick:r",
      to: "ring:l",
      label: "{slot, beat}",
      dir: "h",
      accent: true,
    },
    {
      from: "ring:r",
      to: "drain:l",
      label: "{dslot, rc}",
      dir: "h",
      accent: true,
    },
    { from: "drain:r", to: "r:l", dir: "h" },
    {
      from: "slot:b",
      to: "ring:t",
      label: "home, beats of the slot",
      dir: "v",
      dash: true,
    },
    { from: "aw:r", to: "wslot:l", label: "wr_ok", dir: "h" },
    { from: "wslot:r", to: "bfill:l", label: "AW/W flit", dir: "h" },
    { from: "bfill:r", to: "b:l", dir: "h" },
  ],
};

/* ---------------------------------------------------------------- tables */
const pxCols = [
  { key: "c", label: "" },
  { key: "p", label: "P", mono: true, align: "right" },
  { key: "lut", label: "LUT", mono: true, align: "right" },
  { key: "ff", label: "FF", mono: true, align: "right" },
  { key: "u", label: "URAM", mono: true, align: "right" },
  { key: "b", label: "BRAM", mono: true, align: "right" },
  { key: "w", label: "WNS ns", mono: true, align: "right" },
  { key: "f", label: "Fmax MHz", mono: true, align: "right" },
];
const pxRows = KX_PX.map((r, i) => ({
  c: r[0],
  p: r[1] == null ? "—" : `${r[1]}`,
  lut: i === 1 || i === 2 ? `<b>${n(r[2])}</b>` : n(r[2]),
  ff: n(r[3]),
  u: n(r[4]),
  b: `${r[5]}`,
  w: `+${r[6].toFixed(3)}`,
  f: r[7].toFixed(0),
  _tone: i === 1 || i === 2 ? "good" : i === 4 ? "warn" : undefined,
}));

const perfCols = [
  { key: "s", label: "scenario" },
  { key: "x", label: "kx_xache", mono: true, align: "right" },
  { key: "a", label: "kx_pxache P=1", mono: true, align: "right" },
  { key: "b", label: "P=4, one per partition", mono: true, align: "right" },
];
const perfRows = [
  {
    s: "1 master reads 64 KB, hits, 4 outstanding",
    x: "1,044 · 18.8 GB/s",
    a: "<b>1,038</b> · 18.9",
    b: "<b>1,036</b> · 19.0",
    _tone: "good",
  },
  {
    s: "4 masters read 16 KB each, hits",
    x: "465 · 42.3",
    a: "<b>467</b> · 42.1",
    b: "<b>473</b> · 41.6",
    _tone: "good",
  },
  {
    s: "4 masters read 16 KB each under one ID",
    x: "—",
    a: "467 · 42.1",
    b: "473 · 41.6",
  },
  {
    s: "1 master writes 64 KB",
    x: "1,104 · 17.8",
    a: "1,120 · 17.6",
    b: "1,264 · 15.6 (the bench waits for each remote B)",
  },
  {
    s: "4 masters write 16 KB each",
    x: "477 · 41.2",
    a: "481 · 40.9",
    b: "508 · 38.7",
  },
  { s: "2 KB read, hits", x: "37", a: "39", b: "39 local" },
  {
    s: "32-beat hit, master 0 to the home in partition 0 / 1 / 2 / 3",
    x: "39",
    a: "39 / 39 / 39 / 39",
    b: "<b>39 / 45 / 51 / 57</b>",
    _tone: "good",
  },
];

const knobCols = [
  { key: "p", label: "parameter", mono: true },
  { key: "v", label: "measured at", mono: true },
  { key: "m", label: "meaning" },
];
const knobRows = [
  { p: "P", v: "1, 4", m: "partitions of one clock. P = 1 generates no lane" },
  {
    p: "MP[m] · HP[h]",
    v: "{3,2,1,0}",
    m: "the partition of each master and home, packed PW bits each; the lane count and every TAKE table follow from them at elaboration",
    _tone: "good",
  },
  {
    p: "rstn_p[P]",
    v: "together, or 3 cycles apart",
    m: "one reset per partition; a hop's halves take the two they sit in, and <code>fok</code> tells the sender when the far ring is out of reset",
  },
  {
    p: "RD_OUTQ · WR_OUTQ",
    v: "4 · 4",
    m: "read slots (a page of the reorder ring each) and write slots per master",
  },
  {
    p: "HOP_DEPTH",
    v: "16",
    m: "landing ring entries; ≥ 4 streams a beat per cycle",
  },
  {
    p: "HOP_BUF",
    v: "lean",
    m: "the ring, or <code>xpm</code> — one cycle slower (4 against 3)",
  },
  {
    p: "HOP_RXREG",
    v: "0",
    m: "1: a register in front of every landing RAM, for a placement that wants a flop at both ends of a die crossing — +1 cycle and 590 FF per hop",
    _tone: "warn",
  },
  {
    p: "the Xache's",
    v: "the ship's",
    m: "<code>M</code>, <code>N_HOME</code>, <code>K</code>, <code>W</code>, <code>SETS</code>, <code>MCDC</code>, <code>HCDC</code>, <code>NSWAP</code> — unchanged in meaning and in cost; engines are one per home on both sides, so no <code>RSAMD</code>/<code>WSAMD</code>",
  },
];

const loopCols = [
  { key: "d", label: "design" },
  { key: "a", label: "P=1 LUT", mono: true, align: "right" },
  { key: "b", label: "P=4 LUT", mono: true, align: "right" },
  { key: "w", label: "P=4 WNS", mono: true, align: "right" },
  { key: "why", label: "what sent it back to impl" },
];
const loopRows = [
  {
    d: "per-ID ordering tables (one home in flight per ID)",
    a: "10,809",
    b: "11,373",
    w: "−0.394",
    why: "+815 LUT of tables at P=1; a runtime-indexed ready path from one master's table through another partition's engine into a third master's counter; a one-ID stream stalls at every home switch",
    _tone: "bad",
  },
  {
    d: "reorder ring with ring-address allocation",
    a: "11,146",
    b: "13,396",
    w: "−0.139",
    why: "820 LUT of per-slot address and count arithmetic and a 512-beat space check; the AW/W flit built as a 590-bit mux; the tap's kind decoded off block-RAM output (0.83 ns clock-to-out)",
    _tone: "bad",
  },
  {
    d: "pages, explicit flits, fast bits, a register before every landing RAM",
    a: "9,972",
    b: "10,528",
    w: "+0.775",
    why: "four cycles per hop against the three asked for: the register duplicates the RAM's own input register",
    _tone: "warn",
  },
  {
    d: "<b>the same with the wire landing in the RAM — ships</b>",
    a: "<b>9,972</b>",
    b: "<b>10,960</b>",
    w: "<b>+0.569</b>",
    why: "—",
    _tone: "good",
  },
];

const catCols = [
  { key: "t", label: "Thing" },
  { key: "c", label: "Category" },
];
const catRows = [
  {
    t: "AXI4 at both edges; a burst never crossing 4 KB, which is what makes a slot a page",
    c: "<b>fixed protocol</b>, and not ours",
  },
  {
    t: "<code>P</code>, <code>MP</code>, <code>HP</code>, the resets — which partition is which die, and the reset tree",
    c: "<b>yours</b>, per deployment, in the block design; nothing under <code>src/</code> names a device",
  },
  {
    t: "that a boundary is crossed by one hop and nothing else, that nothing downstream of an engine waits, that every ready is gathered per partition at elaboration",
    c: "<b>convention with teeth</b>: it is where the bandwidth and the deadlock-freedom come from",
  },
  {
    t: "<code>HOP_RXREG</code>, <code>HOP_BUF</code>, <code>HOP_DEPTH</code>, the slot counts",
    c: "<b>customizable</b>; each is measured",
  },
];
</script>

<template>
  <DocPage
    title="Partitioned Xache"
    summary="The Kohaku Xache with its masters and homes spread over P partitions of one clock — dies of a part, regions of a floorplan. Every boundary is crossed by exactly one registered, credited hop; every (master, home) pair has its own lane, so a boundary carries the crossbar's bandwidth; a reorder ring per master lets every response land the cycle it is offered, so nothing anywhere waits on a turn. P = 1 is kx_xache at the same LUT, latency and bandwidth."
    domain="framework"
    status="measured"
    :source="`src/kohakuaxi/pxache/ · docs/projects/kohakuaxi/pxache.md · scripts/tcl/ooc_mod.tcl · ${PART} · ${TARGET_MHZ} MHz ask · synthesis only`"
  >
    <p class="doc-p">
      The
      <RouterLink to="/component/xache" class="doc-link">Xache</RouterLink>
      is a single-partition fabric: every path in it is register-to-register
      inside one region. <code>kx_pxache</code> is the same system — the same
      arrays, engines and edges, unchanged — with its <code>M</code> masters and
      <code>N</code> homes assigned to <code>P</code> partitions, so that a
      master on one die reaches a home on another through one registered,
      credited hop per boundary and nothing else. What is new is how a pair that
      sits in two partitions meets, and how a master takes its responses back.
    </p>

    <Callout
      kind="rule"
      title="Three things a partition boundary must not cost"
    >
      <p>
        <b>Bandwidth</b>: a boundary carries as many streams as the crossbar
        would, because every source has its own lane and nothing is muxed in
        transit. <b>Correctness</b>: nothing downstream of an engine ever waits,
        because a response's landing place is reserved before its request leaves
        — the Xache's own ordering, which holds a burst at its home until it is
        the master's oldest, deadlocks once latencies differ. <b>Structure</b>:
        no unregistered path leaves a partition; every ready and accept is
        gathered at elaboration over that partition's homes or masters only,
        from the constant <code>MP</code>/<code>HP</code> maps.
      </p>
    </Callout>

    <h2 class="doc-h2">Lanes, hops and taps</h2>
    <Fig
      caption="Three partitions; master 0's request lane going up and home 2's response lane coming back down. A lane is one source's stream in one direction: a chain of hops with a tap after each boundary. The landing ring's head is examined once at every tap — TAKE[t][dst], a constant — and feeds both the tap and the next hop's TX register with only the valids differing, so nothing is muxed in transit and a lane is in order by construction. The tap at home 1's partition IS home 1's request slot for master 0; the tap at partition 0 IS master 0's response source from home 2. A pair in one partition is wires, exactly as in the Xache."
      zoom
      wide
    >
      <BlockDiagram
        :nodes="lanes.nodes"
        :edges="lanes.edges"
        :groups="lanes.groups"
      />
    </Fig>
    <p class="doc-p">
      Every master has an <b>AR lane</b> and an <b>AW/W lane</b> in each
      direction it needs, every home an <b>R/B lane</b> in each direction, so
      the ship's four partitions with one master and one home each are 36 hops.
      W beats follow their AW on the same lane, so a W never waits for its AW at
      a tap. The AW/W flit is <code>{kind, W beat}</code> with the AW header
      riding in the beat's low bits, and the R/B flit
      <code>{kind, slot, id, resp, last, word}</code> with the word on every
      flit and a B ignoring it — so only a header's width is ever muxed, not 590
      bits per lane.
    </p>

    <h3 class="doc-h3">One hop</h3>
    <Fig
      caption="Nothing combinational in either direction of the crossing. The sender's TX register has one load, the wire; the wire lands in the receiver's ring RAM, whose write port registers WE, ADDR and DIN at the clock edge — the RAM is the landing register; a pop comes back as a registered pulse the sender counts as credit, and a fok pulse says the far ring is out of reset, so partitions may leave reset in any order. Three cycles accept-to-deliver, a flit per cycle, credit round trip 3 against a 16-deep ring (kx_hop_tb). The head's destination and kind bits come out of distributed RAM so no decode waits on block RAM's 0.83 ns clock-to-out: that alone took a lane from 469 to 666 MHz."
      zoom
      wide
    >
      <BlockDiagram
        :nodes="hop.nodes"
        :edges="hop.edges"
        :groups="hop.groups"
      />
    </Fig>
    <Callout
      kind="trap"
      title="A register in front of a RAM's write port is a duplicate"
    >
      <p>
        The reflex for a wire arriving from another die is a landing flop. In
        front of a RAM it is a second register stage: the first build had one
        and measured 4 cycles per hop, 590 FF per hop more, and 432 LUT fewer
        under synthesis — the amber row below. Whether the far end of a die
        crossing wants a fabric flop for its own timing is a question only a
        placed run can answer, so <code>HOP_RXREG</code> keeps it as a
        parameter, not the default.
      </p>
    </Callout>

    <h2 class="doc-h2">Ordering without waiting: the master's port</h2>
    <Fig
      caption="A read takes a slot and a 4 KB page of the master's reorder ring at the AR — AXI never lets a burst cross 4 KB, so no burst is longer — and the slot number rides to the home in the AR flit and comes back in every R flit. A beat from any source lands at {slot, beat} the cycle it is offered, whatever order the homes answer in; the drain reads the oldest slot beat by beat as its beats land and presents them in issue order, so the R channel is AXI-ordered with nothing held anywhere. The pick is combinational and prefers the home of the slot being drained. A write takes a slot for its B — a B completes the oldest open slot bound for its home, slots drain in issue order — and sends every beat before the next AW is taken."
      zoom
      wide
    >
      <BlockDiagram
        :nodes="port.nodes"
        :edges="port.edges"
        :groups="port.groups"
      />
    </Fig>
    <Callout
      kind="rule"
      title="Why the Xache's ordering cannot cross a boundary"
    >
      <p>
        <code>kx_xache</code> orders one master's reads with a sequence number
        and a turn: a home holds a completed burst until it is that master's
        oldest. In one partition every home sees requests in issue order and no
        cycle forms. Once the latency from a master to each home differs:
        <code>m</code> in partition 0 and <code>m′</code> in partition 3, homes
        <code>A</code> in 3 and <code>C</code> in 0; <code>m</code> issues seq0
        → A (far) then seq1 → C (near), <code>m′</code> issues seq0 → C (far)
        then seq1 → A (near). A sees m′.seq1 first, completes it, holds it — m′
        is waiting on C; C sees m.seq1 first, completes it, holds it — m is
        waiting on A. Neither home can serve the older burst while holding. Two
        masters and two homes with unequal latencies are enough, and the same
        hold at a lane tap builds it through the tap. So the partitioned fabric
        holds nothing: every response has its landing place before its request
        leaves.
      </p>
    </Callout>
    <Callout
      kind="trap"
      title="Two picks that measured wrong before the right one"
    >
      <p>
        A pick registered a cycle ahead — the Xache's habit for a wide mux —
        starved the engines: their lookahead is
        <code>room = accept || !r_val</code>, and a ready one cycle late ran
        them at a beat per three cycles (hit-32 measured 102 against 39), and a
        write on the stale pick re-landed each word twice. A plain lowest-valid
        combinational pick let a nearer home land ahead of the slot being
        drained, which idled and then ran a 180-cycle tail on a single-master
        stream (1,224 cycles against 1,044). With the drain's home first, every
        row is at the Xache's figures. The data mux feeds a BRAM, which absorbs
        the combinational select for nothing.
      </p>
    </Callout>

    <h2 class="doc-h2">Knobs</h2>
    <SpecTable
      :cols="knobCols"
      :rows="knobRows"
      caption="A partition here is a parameter; which partition is which die, and the reset tree that releases each, belong to the block design under xilinx-fpga/, never to src/"
    />

    <h2 class="doc-h2">What a partition costs</h2>
    <SpecTable
      :cols="pxCols"
      :rows="pxRows"
      :caption="`The ship shape — 4×4 K1, 64 URAM per home, the 16 KB rotation, four DRAM-side crossings unless noted — at P partitions, one ooc_mod.tcl synthesis each on ${PART} at ${TARGET_MHZ} MHz, beside the kx_xache baseline. P=1 is the Xache within 22 LUT; the 30 BRAM are four reorder rings at the width floor and the 500 FF their bookkeeping. Four partitions are 36 hops for 966 LUT over the Xache and 14,900 FF (the hops' TX registers on 590-bit lanes), BRAM at the width floor: 24 wide hops at 8.5, 12 narrow, 30 rings, 64 edges. The amber row is the same design with a register before every landing RAM, one cycle more per hop`"
    />
    <SpecTable
      :cols="loopCols"
      :rows="loopRows"
      caption="The design loop, kept as measurements: each row is one whole design taken through lint, bench, gate and synthesis before the numbers sent it back"
    />

    <h2 class="doc-h2">Performance</h2>
    <SpecTable
      :cols="perfCols"
      :rows="perfRows"
      caption="kx_pxache_tb with TB_PERF beside kx_xache_tb: 4×4 K1, block-RAM arrays, the 4 KB interleave, a 24-cycle DRAM, 64-beat bursts, four outstanding, cycles on the fabric clock, GB/s at 300 MHz. Reads across four partitions are within 1.3% of one partition; +6 cycles per boundary on a round trip — a 3-cycle hop each way — is the whole latency cost, identical on every shape; the write rows at P=4 are the bench's, which waits for each burst's B before the next AW, so a remote B round trip is paid per burst; the single-ID row is the case the Xache's bench never ran, at the same rate because the ring orders by slot, never by ID"
    />

    <h2 class="doc-h2">Verification</h2>
    <p class="doc-p">
      Three benches under xsim, the gate of record.
      <code>tests/axi/kx_hop_tb.v</code>: a hop at W = 590 and 60, lean and xpm
      — credits never above <code>DEPTH</code>, no ready while the receiver is
      in reset, the empty-hop latency, a streaming soak, a source reset with
      words in flight. <code>kx_lane_tb.v</code>: three taps and one — staggered
      tap releases, a random soak over every destination, a head-of-line stall
      on one tap while the others drain, a source reset, per-tap scoreboards.
      <code>kx_pxache_tb.v</code>: the Xache's bench with a partition per index
      and staggered resets, a collector that matches beats by ID against
      per-(master, ID) queues, every master to every home at once with both lane
      directions live, streaming soaks both ways, and the hit-32 latency probe.
      The Xache's fourteen shapes at P = 1 and at P = 4 plus the staggered-reset
      case and the three lanes: 32 builds, 2,877–9,192 checks each, 0 errors,
      every loop of the design before its synthesis. Verilator 5.020 cannot run
      <code>kx_pxache_tb</code> — it overflows its stack at the fork inside the
      bench's streaming task — while it runs <code>kx_xache_tb</code> in 0.2 s.
    </p>

    <h2 class="doc-h2">What it deliberately does not do</h2>
    <Callout kind="note" title="Not built, and not an omission">
      <p>
        <b>No 2-cycle hop</b> — the landing buffer is block RAM and is not
        bypassed; a bypass is a 590-bit 2:1 per hop.
        <b>No per-destination credits</b> — a taken flit its consumer cannot yet
        accept holds the lane behind it for the length of that wait, bounded
        because nothing downstream of an engine waits.
        <b>No engine shared across partitions</b>, so no
        <code>RSAMD</code>/<code>WSAMD</code>.
        <b>One write burst's beats before the next AW</b> — an AW ahead of the
        beats of the one before it on the same lane would be held at a tap by
        the engine's busy slot with those beats wedged behind it; the cost is
        the AW's accept cycle per burst. <b>A burst is at most a page</b> — 64
        beats at 512 bits, which AXI's 4 KB rule already says for full-width
        beats; the bench reports a longer one, the RTL does not check it.
        <b>Nothing places anything.</b>
      </p>
    </Callout>

    <h2 class="doc-h2">Fixed protocol, addon, convention, or yours</h2>
    <SpecTable :cols="catCols" :rows="catRows" />
  </DocPage>
</template>
