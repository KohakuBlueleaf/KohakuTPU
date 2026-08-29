<script setup>
/**
 * /framework/axi — which AXI goes where. Three kinds of AXI carry three kinds
 * of traffic in this machine, and one boundary discipline applies to all of
 * them. The two AXI systems the framework ships are components with pages of
 * their own: /component/station-bus and /component/xache.
 *
 * Drawn from: docs/arch/axi.md · docs/address-map.md ·
 * src/kohakuaccel/axi/ · src/kohakuaccel/sysnode/core/mag_dram_port.v ·
 * src/kohakuaccel/sysnode/interlink/
 */

// ---------------------------------------------------------- the three kinds
const kinds = {
  groups: [
    {
      x: -1.5,
      y: 4.5,
      w: 69,
      h: 17.5,
      label: "three kinds of AXI — one boundary discipline",
    },
  ],
  nodes: [
    {
      id: "host",
      x: 0,
      y: 0,
      w: 14,
      h: 3,
      label: "host",
      sub: "XDMA · JTAG-AXI · main_orch",
    },
    {
      id: "sb",
      x: 0,
      y: 6,
      w: 30,
      h: 3.6,
      label: "1 · the station bus",
      sub: "AXI4 and AXI4-Lite · a line of stations, one per die · many widths, many clocks",
      accent: true,
    },
    {
      id: "ctl",
      x: 0,
      y: 12,
      w: 14,
      h: 3.6,
      label: "MAG slave port",
      sub: "control window · staging · memory window",
    },
    {
      id: "util",
      x: 16,
      y: 12,
      w: 14,
      h: 3.6,
      label: "utilities",
      sub: "clk_wiz · DDR controller register port · Lite registers",
    },
    {
      id: "mag",
      x: 34,
      y: 6,
      w: 14,
      h: 3.6,
      label: "MAG DRAM masters × M",
      sub: "mag_dram_port · one per mesh · RD_OUT bursts in flight",
    },
    {
      id: "kx",
      x: 34,
      y: 12,
      w: 14,
      h: 3.6,
      label: "2 · Kohaku Xache",
      sub: "AXI4 at 512 b at two edges only · M masters → N channels, a tagged cache fused per channel",
      accent: true,
    },
    {
      id: "ddr",
      x: 34,
      y: 18,
      w: 14,
      h: 3,
      label: "DDR4 × N",
      sub: "vendor controllers",
    },
    {
      id: "il",
      x: 52,
      y: 6,
      w: 14,
      h: 3.6,
      label: "3 · the interlink",
      sub: "AXI4-Stream · 288-bit beats, one flit each · credits, never tready",
      accent: true,
    },
    {
      id: "peer",
      x: 52,
      y: 12,
      w: 14,
      h: 3.6,
      label: "another mesh's MAG",
      sub: "remote writes · encapsulated flits · doorbells",
    },
  ],
  edges: [
    { from: "host:b", to: "sb:t", label: "AXI4 · AXI4-Lite" },
    { from: "sb:b", to: "ctl:t", label: "a write's address decides" },
    { from: "sb:b", to: "util:t" },
    {
      from: "ctl:r",
      to: "mag:l",
      label: "the memory window's upload master",
      dir: "h",
      dash: true,
    },
    { from: "mag:b", to: "kx:t", label: "AXI4 · 512 b", accent: true },
    { from: "kx:b", to: "ddr:t", label: "AXI4 per channel", accent: true },
    { from: "mag:r", to: "il:l", label: "flits", dir: "h" },
    {
      from: "il:b",
      to: "peer:t",
      label: "packets of up to 32 flits",
      accent: true,
    },
  ],
};

const kindCols = [
  { key: "k", label: "Kind" },
  { key: "p", label: "Protocol" },
  { key: "c", label: "Carries" },
  { key: "w", label: "Page" },
];
const kindRows = [
  {
    k: "<b>1 · the station bus</b>, with the MAG slave port and the utilities around it",
    p: "AXI4 and AXI4-Lite at whatever width and clock each endpoint has — 512 b at 250 MHz from XDMA, 64 b at 100 MHz from JTAG, 32-bit Lite registers",
    c: "host and control traffic: register windows, instruction staging, the memory window, the clock wizard's and DDR controller's register ports. A line of stations, one per die, no crossbar",
    w: "<RouterLink to='/component/station-bus' class='doc-link'>The station bus</RouterLink>",
    _tone: "good",
  },
  {
    k: "<b>2 · Kohaku Xache</b> — the DRAM fabric",
    p: "AXI4 at 512 bits, at exactly two places: where a MAG DRAM master attaches and where a DDR channel attaches. Nothing AXI-shaped between",
    c: "memory traffic from M meshes to N DRAM channels, a tagged cache fused per channel, a streaming read engine with a per-master read queue, channel interleaving as wires",
    w: "<RouterLink to='/component/xache' class='doc-link'>Kohaku Xache</RouterLink>",
    _tone: "good",
  },
  {
    k: "<b>3 · the interlink</b> — MAG to MAG",
    p: "AXI4-Stream: 288-bit beats, one NoC flit per beat, a 96-bit <code>TUSER</code> packet header, packets of up to 32 beats, credit per class; <code>tready</code> never crosses",
    c: "the mover's remote writes, flits bound for another mesh, doorbells — over an SLL with nothing but registers in the crossing",
    w: "<RouterLink to='/component/sysnode' class='doc-link'>The system node</RouterLink>",
    _tone: "good",
  },
];

// ------------------------------------------------------------- the surface
const surface = {
  groups: [
    {
      x: -1.5,
      y: 4.5,
      w: 85,
      h: 11,
      label: "src/kohakuaccel/axi — conversion, and only conversion",
    },
  ],
  nodes: [
    {
      id: "host",
      x: 0,
      y: 0,
      w: 14,
      h: 3,
      label: "host DMA",
      sub: "XDMA — vendor IP",
    },
    {
      id: "dbg",
      x: 17,
      y: 0,
      w: 14,
      h: 3,
      label: "debug bridge",
      sub: "JTAG-AXI — vendor IP",
    },
    {
      id: "ddr",
      x: 51,
      y: 0,
      w: 14,
      h: 3,
      label: "DDR4 controller",
      sub: "vendor IP",
    },
    {
      id: "model",
      x: 68,
      y: 0,
      w: 14,
      h: 3,
      label: "memory models",
      sub: "reference + stub",
    },
    {
      id: "slave",
      x: 0,
      y: 6,
      w: 31,
      h: 3.4,
      label: "slave surface",
      sub: "memory · control · instruction staging · pass-through",
      accent: true,
    },
    {
      id: "n1",
      x: 51,
      y: 6,
      w: 31,
      h: 3.4,
      label: "axi_n1 / mag_dram_port",
      sub: "arbitrate · round robin · 5 queues · id routing · pack width · cross clock",
      accent: true,
    },
    {
      id: "orch",
      x: 34,
      y: 0,
      w: 14,
      h: 3,
      label: "main_orch",
      sub: "host driver · WR · POLL · DONE",
    },
    {
      id: "fab",
      x: 0,
      y: 12,
      w: 48,
      h: 3.4,
      label: "the framework — flits, routers, the memory agent",
      sub: "a compute unit never sees AXI",
      accent: true,
    },
  ],
  edges: [
    { from: "host:b", to: "slave:t" },
    { from: "dbg:b", to: "slave:t" },
    { from: "orch:b", to: "slave:t", label: "host-side master" },
    { from: "slave:b", to: "fab:t", label: "control writes become flits" },
    { from: "slave:r", to: "n1:l", label: "host memory window", dir: "h" },
    { from: "fab:r", to: "n1:b", label: "one master per port", accent: true },
    { from: "n1:t", to: "ddr:b" },
    { from: "n1:t", to: "model:b", label: "in simulation", dash: true },
  ],
};

// ------------------------------------------------------------ concentrator
const conc = {
  nodes: [
    {
      id: "reqs",
      x: 0,
      y: 0,
      w: 46,
      h: 3.4,
      label: "N requesters, requester clock",
      sub: "the requester index is prepended to AWID / ARID",
      accent: true,
    },
    { id: "rr", x: 0, y: 6, w: 14, h: 3.4, label: "round robin", sub: "AW" },
    {
      id: "wm",
      x: 16,
      y: 6,
      w: 14,
      h: 3.4,
      label: "W mux",
      sub: "head until wlast",
    },
    { id: "rr2", x: 32, y: 6, w: 14, h: 3.4, label: "round robin", sub: "AR" },
    {
      id: "fifos",
      x: 0,
      y: 12,
      w: 46,
      h: 3.4,
      label: "five asynchronous FIFOs",
      sub: "awq · wq · arq · bq · rq — five in total, not five per requester",
      accent: true,
    },
    {
      id: "demux",
      x: 52,
      y: 12,
      w: 18,
      h: 3.4,
      label: "demux by id",
      sub: "none is kept, none is sized",
    },
    {
      id: "slave",
      x: 0,
      y: 18,
      w: 46,
      h: 3.4,
      label: "memory domain — one slave",
      sub: "the internal beat is packed to the memory beat here, and nowhere else",
      accent: true,
    },
  ],
  edges: [
    { from: "reqs:b", to: "rr:t" },
    { from: "reqs:b", to: "wm:t" },
    { from: "reqs:b", to: "rr2:t" },
    { from: "rr:b", to: "fifos:t" },
    { from: "wm:b", to: "fifos:t" },
    { from: "rr2:b", to: "fifos:t" },
    { from: "fifos:b", to: "slave:t", accent: true },
    { from: "fifos:r", to: "demux:l", label: "B / R", dir: "h" },
    { from: "demux:t", to: "reqs:r", label: "BID / RID route it" },
  ],
};

// ------------------------------------------------------------ wave traces
const lastBroken = {
  rows: [
    { name: "WVALID", kind: "bit", values: [1, 1, 1, 1, 1, 1] },
    {
      name: "WDATA",
      kind: "bus",
      values: ["d0", "d1", "d2", "d3", "e0", "e1"],
    },
    { name: "WLAST", kind: "bit", values: [0, 0, 1, 0, 0, 0], mark: [2] },
    { name: "burst ends", kind: "text", values: ["", "", "here", "", "", ""] },
    { name: "BVALID", kind: "bit", values: [0, 0, 0, 1, 0, 0] },
    {
      name: "d3 read as",
      kind: "text",
      values: ["", "", "", "beat 0 of the next", "", ""],
    },
  ],
  notes: [
    {
      cycle: 2,
      text: "The requester miscounts its own data and asserts WLAST one beat early. The response fires early, d3 is consumed as the first beat of the next burst, and every later burst is one beat out.",
      tone: "bad",
    },
  ],
};

const lastFixed = {
  rows: [
    { name: "WVALID", kind: "bit", values: [1, 1, 1, 1, 1, 1] },
    {
      name: "WDATA",
      kind: "bus",
      values: ["d0", "d1", "d2", "d3", "e0", "e1"],
    },
    { name: "WLAST", kind: "bit", values: [0, 0, 1, 0, 0, 0] },
    {
      name: "beat counter",
      kind: "bus",
      values: ["0", "1", "2", "3", "0", "1"],
      mark: [3],
    },
    { name: "burst ends", kind: "text", values: ["", "", "", "here", "", ""] },
    { name: "BVALID", kind: "bit", values: [0, 0, 0, 0, 1, 0] },
  ],
  notes: [
    {
      cycle: 3,
      text: "A burst ends because a counter says so, not because WLAST arrived. The early WLAST is ignored and the response stays aligned.",
      tone: "good",
    },
    {
      text: "The requester's error is contained, not repaired. In the memory agent the same rule leaves a write slot that never completes — see the memory agent page.",
      tone: "good",
    },
  ],
};

// ------------------------------------------------------------------ tables
const roleCols = [
  { key: "role", label: "Role" },
  { key: "shape", label: "Shape" },
  { key: "where", label: "Where" },
];
const roleRows = [
  {
    role: "<b>slave</b>",
    shape: "host writes registers, staging, memory",
    where: "the control agent, the memory window",
  },
  {
    role: "<b>master</b>",
    shape: "the framework reads and writes memory",
    where: "one per memory port, plus upload, mover, interlink landing",
  },
  {
    role: "<b>model</b>",
    shape: "a slave that behaves like memory, for simulation",
    where: "<code>axi4_ram.v</code>, <code>axi_ram.v</code>",
  },
];

const ruleCols = [
  { key: "n", label: "#", align: "right" },
  { key: "rule", label: "Rule" },
  { key: "fail", label: "The failure behind it" },
];
const ruleRows = [
  {
    n: "1",
    rule: "<b>VALID is never a function of READY.</b>",
    fail: "the reverse is a combinational loop between two compliant devices, and the AXI specification forbids it for exactly that reason",
  },
  {
    n: "2",
    rule: "<b>A burst ends because a counter says so, not because WLAST arrived.</b>",
    fail: "a requester that miscounts its own data must not be able to desynchronise the response",
  },
  {
    n: "3",
    rule: "<b>BID / RID echo AWID / ARID.</b>",
    fail: "AXI4 requires it, and this layer depends on it structurally — response routing is the ID, not a table",
  },
  {
    n: "4",
    rule: "<b>A burst must not cross a 4 KB boundary</b>, and AxLEN maxes at 255.",
    fail: "an interconnect is permitted to do arbitrary things if you break it; some split, some stall, some corrupt",
  },
];

const winCols = [
  { key: "mesh", label: "Mesh", mono: true, align: "right" },
  { key: "base", label: "Window base", mono: true },
];
const winRows = [
  { mesh: "0", base: "0x100_0000_0000" },
  { mesh: "1", base: "0x200_0000_0000" },
  { mesh: "2", base: "0x300_0000_0000" },
  { mesh: "3", base: "0x400_0000_0000" },
];

const opCols = [
  { key: "op", label: "Opcode", mono: true },
  { key: "args", label: "Arguments", mono: true },
  { key: "does", label: "Does" },
];
const opRows = [
  { op: "WR", args: "addr, data", does: "issue an AXI write" },
  {
    op: "POLL",
    args: "addr, mask, want",
    does: "read <code>addr</code> until <code>(data &amp; mask) == want</code>",
  },
  { op: "DONE", args: "code", does: "stop, latch code, raise the done flag" },
];

const ilCols = [
  { key: "p", label: "Property" },
  { key: "v", label: "The interlink" },
  { key: "w", label: "Why" },
];
const ilRows = [
  {
    p: "beat",
    v: "288 bits = one NoC flit; <code>LINK_W</code> is width-agnostic but every stage on a link must agree",
    w: "256 payload bits per beat is 9.6 GB/s at 300 MHz, which is what one NoC port produces — a flit crosses verbatim, nothing packed, padded or reconstructed",
  },
  {
    p: "packet",
    v: "up to <code>MAX_BEATS = 32</code> flits behind one 96-bit <code>TUSER</code> header; a whole burst is one packet",
    w: "the header is amortised over the burst even though each beat holds one flit; DOORBELL and CREDIT packets carry one ignored beat so nothing frames a zero-beat case",
  },
  {
    p: "flow control",
    v: "credits, per class — “stops at the peer” and “the peer forwards” are two counters; <code>RX_BEATS = 64</code> per class is the initial credit, returned <code>CRED_BATCH = 8</code> at a time",
    w: "one shared pool would let a stalled forward path stop traffic that was going to terminate anyway; credit packets are absorbed into an adder and never enter a FIFO",
    _tone: "good",
  },
  {
    p: "<code>tready</code>",
    v: "never crosses. The receiver is always ready because credit reserved the space; <code>m_axis_tready</code> reaches only a simulation assertion",
    w: "a Laguna site is a flip-flop, so a crossing is usable only as flop → SLL → flop; one gate in it forfeits the site (an implemented mesh once measured 4.6 ns at 98% routing with zero logic levels)",
    _tone: "good",
  },
  {
    p: "clocks",
    v: "<code>mag_link_cdc</code>, a plain asynchronous FIFO deeper than both classes' credit, when the meshes run different clocks",
    w: "a plain FIFO is enough only because nothing hands back a ready; its depth must exceed the sender's total credit or a beat is discarded, which is why the depth is a parameter with a stated bound",
  },
  {
    p: "routing",
    v: "<code>mag_switch</code>: three ports (link 0, link 1, local), one comparison per hop along mesh 0 – 1 – 3 – 2",
    w: "a second routing layer with its own deadlock proof: position is monotone along a path, the channel dependency graph is two disjoint chains, and a ring would close it",
  },
];
</script>

<template>
  <DocPage
    title="AXI in this machine"
    summary="Three kinds of AXI carry three kinds of traffic: the station bus for the host and control, Kohaku Xache for the DRAM fabric, and the interlink's AXI-Stream between meshes. This page says which is which, and the one discipline all of them keep."
    domain="framework"
    status="measured"
    source="src/kohakuaccel/axi/ · src/kohakuaccel/sysnode/interlink/ · docs/arch/axi.md · docs/address-map.md"
  >
    <p class="doc-p">
      Nothing outside the framework speaks flits. A host DMA engine, a memory
      controller, a debug bridge and a vendor interconnect all speak AXI — and
      AXI is substantially larger than what this machine uses: out-of-order
      completion by ID, burst reordering, exclusive access, narrow transfers,
      cache and protection attributes. So AXI appears in three places, each a
      different dialect for a different job, and none of them imports the whole
      protocol.
    </p>

    <Fig
      caption="Where each kind of AXI lives. The station bus carries the host's control and staging traffic across the dies to MAG's slave port and the utility registers; Kohaku Xache carries the meshes' memory traffic from their DRAM masters into the channels; the interlink carries flits between meshes as AXI-Stream packets. The dashed edge is the host's memory window: a host write into DRAM enters through the station bus, lands in MAG, and rides MAG's own upload master through the Xache like any other requester."
      zoom
      wide
    >
      <BlockDiagram
        :nodes="kinds.nodes"
        :edges="kinds.edges"
        :groups="kinds.groups"
      />
    </Fig>

    <SpecTable
      :cols="kindCols"
      :rows="kindRows"
      caption="The three kinds. The first two are framework components with their own pages — they ship working and are configured per deployment; the third is part of the system node. They share no module, and a number from one says nothing about another"
    />

    <Callout
      kind="note"
      title="Two failure modes if this boundary is left implicit"
    >
      <p>
        The first is <b>importing AXI's generality inwards</b>, so the fabric
        grows machinery to satisfy a bus nobody asked for. The second is
        <b>exporting the fabric's assumptions outwards</b>, so a vendor
        interconnect meets an interface that is <i>nearly</i> AXI and does
        something arbitrary about the difference. This layer exists so that the
        conversion happens once, in modules whose job is only conversion.
      </p>
    </Callout>

    <Fig
      caption="A compute unit never sees AXI. It emits memory requests as flits and the memory agent deals with bursts, boundaries and widths. The concentrator is the whole master boundary: one AXI master per memory port, plus upload, mover and the interlink landing, converge on it. The memory models are ours, not vendor IP — they stand in for the controller in simulation."
      zoom
      wide
    >
      <BlockDiagram
        :nodes="surface.nodes"
        :edges="surface.edges"
        :groups="surface.groups"
      />
    </Fig>

    <SpecTable
      :cols="roleCols"
      :rows="roleRows"
      caption="The three AXI roles"
    />

    <h2 class="doc-h2">The discipline they share</h2>
    <SpecTable
      :cols="ruleCols"
      :rows="ruleRows"
      caption="Four rules, on every AXI interface in the source tree. There is no longer one module that encodes them for everyone: the reference master that used to is retired to src/attic/legacy-axi/, superseded by orchestrator dispatch, so every master that issues AXI today — the memory agent's DRAM port, each station manager shim — carries rules 2 through 4 for itself"
    />

    <WaveTrace
      v-bind="lastBroken"
      variant="broken"
      label="a burst ends when WLAST arrives"
    />
    <WaveTrace
      v-bind="lastFixed"
      variant="fixed"
      label="a burst ends when the counter says so"
    />

    <Callout
      kind="trap"
      title="Single-outstanding is what a reference master is for, and what a real one must not be"
    >
      <p>
        The retired reference master kept <b>one burst outstanding</b>, which is
        the right choice for a module whose job is to be readable and checkable
        — and the wrong one for anything on a datapath, because with real memory
        latency single-outstanding leaves most of the bandwidth unused.
      </p>
      <p>
        Two measurements on the component pages say how much: on the station
        bus,
        <b
          >at one outstanding burst roughly half the elapsed time is
          turnaround</b
        >; through the Xache, a master that issues its next
        <code>AR</code> while the previous burst still drains reads at 18.3 GB/s
        where a one-burst-at-a-time master reads at 12.7. Copying a reference
        master's acceptance policy into a production one is the single easiest
        way to halve a link and see nothing wrong in simulation.
      </p>
    </Callout>

    <h2 class="doc-h2">
      Kind 1 — the control side: one decode is the whole control plane
    </h2>
    <p class="doc-p">
      An AXI write's <b>address</b> decides what it is: memory, control
      register, instruction staging, or a raw flit to inject. That is the reason
      there is no separate control fabric, and the reason a debug bridge can
      inject mesh traffic with an ordinary AXI write and nothing else. The
      station bus is what carries that write across the dies to the right MAG;
      its own structure, costs and knobs are on
      <RouterLink to="/component/station-bus" class="doc-link"
        >The station bus</RouterLink
      >.
    </p>

    <Callout
      kind="rule"
      title="Two windows are wide rather than deep, and both are deliberate"
    >
      <p>
        The <b>staging window</b> is sized from the number of instruction slots
        rather than fixed at one page — a fixed page silently decodes the tail
        of a long program as register writes, and the symptom is a program that
        stops early with no error. The <b>pass-through window</b> forwards
        writes verbatim with the offset preserved, so a client behind it keeps
        its own register offsets rather than having them renumbered by an index.
      </p>
    </Callout>

    <h3 class="doc-h3">The address map: outside versus inside</h3>
    <p class="doc-p">
      The machine is a 40-bit machine. A host master does not drive those 40
      bits directly — it drives a 43-bit AXI address whose top 3 bits are
      <b>not address space</b>. They are a routing prefix: the interconnect
      consumes them to choose a mesh's <code>S_AXI_MEM</code> port, and the mesh
      receives <code>addr[39:0]</code> unmodified.
    </p>

    <BitField
      :fields="[
        { name: 'window', bits: 3, value: 'mesh + 1', accent: true },
        { name: 'aperture', bits: 1, value: '1 = staging L2', accent: true },
        { name: 'rsvd', bits: 1, value: 'MUST be 0' },
        { name: 'mesh', bits: 2, value: '0..3', accent: true },
        { name: 'local', bits: 36, value: '64 GB' },
      ]"
      caption="outside = (mesh + 1) &lt;&lt; 40 | inside. A mesh cannot tell whether a request came from its own mover or from XDMA"
    />

    <SpecTable
      :cols="winCols"
      :rows="winRows"
      caption="0x000_... is left free so the control space stays below 4 GB and XDMA's AXI-Lite can reach it"
    />

    <Callout
      kind="trap"
      title="Vivado discards 64 GB offsets and stacks all four meshes at zero"
    >
      <p>
        <code>S_AXI_MEM</code> declares 40-bit addressing, so its
        <code>reg0</code> segment is a fixed <b>1 TB</b> and Vivado will only
        place it on a 1 TB boundary. Four of those cannot tile inside one 1 TB
        space. Assigning them at 64 GB spacing <b>does not fail loudly</b>
        — Vivado discards the offsets and puts all four meshes at offset 0,
        which is what
        <code>BD 41-1377</code> reports. That is why the prefix exists at all.
      </p>
    </Callout>

    <Callout
      kind="trap"
      title="If the window and the address disagree, nothing faults"
    >
      <p>
        The window chooses where a transaction <i>enters</i> and the address
        chooses where it <i>lands</i>, and they are independent. When they
        disagree, <code>mine</code> simply stays low in
        <code>mag_stage_port.v:87</code> and no requester claims the beat, so
        the access is never answered —
        <b>it presents as a hang, not as an error</b>. A driver that sets the
        window but forgets the mesh field sees exactly this.
      </p>
      <p>
        Capacity has the same shape. Each mesh gets 64 GB of local space and has
        4 GB of DDR4 behind it, so addresses from 4 GB to 64 GB decode
        correctly, reach
        <code>M_AXI_DRAM</code>, and hit nothing. Staying under 4 GB per mesh is
        a compiler invariant, not something the hardware checks — unlike an
        unimplemented aperture, which does fault.
      </p>
    </Callout>

    <Callout kind="open" title="NOT YET TRACED">
      <p>
        A write into mesh 2's window whose <code>mesh</code> field says 3 is
        <i>classified</i> remote by mesh 2's decoders, which are absolute.
        Whether the <code>S_AXI_MEM</code> path reaches the ilink forwarder the
        way the mover's path does is <b>unverified</b>. Do not plan around it
        until someone follows the path.
      </p>
    </Callout>

    <h3 class="doc-h3">The host-side control-program engine</h3>
    <p class="doc-p">
      <code>main_orch.v</code> is an AXI slave so the host can load a program,
      and an AXI master so it can execute one. The host writes a list of
      <code>WR</code> / <code>POLL</code> / <code>DONE</code> commands and then
      writes <code>GO</code>, so a whole run becomes
      <b>one host transaction</b> — the host is not in the loop per poll, and
      the same program drives real hardware over JTAG and over PCIe. It is
      synthesisable RTL, not a testbench part; it lives in
      <code>src/kohakuaccel/verif/</code> because that is where it is
      <i>used</i> — for bring-up and for scripting the host side of a simulation
      — not because it is simulation-only.
    </p>
    <p class="doc-p">
      What it is <b>not</b> is the machine's control plane. On-card
      orchestration — dispatch, completion handling, the memory choreography —
      lives in the
      <RouterLink to="/component/rv64sys" class="doc-link"
        >RV64 runtime host</RouterLink
      >
      inside the system node, which outlives the work it launches and needs no
      host in the loop at all. <code>main_orch</code> is the <i>host's</i> reach
      into the same control surface: it issues AXI writes into a MAG control
      window, exactly as the debug bridge or the DMA engine would, and MAG turns
      those into flits.
    </p>
    <SpecTable
      :cols="opCols"
      :rows="opRows"
      caption="Three opcodes is enough because the machine's entire control surface is memory-mapped; branches or arithmetic here would duplicate the host to no purpose"
    />

    <h2 class="doc-h2">
      Kind 2 — the memory side: concentrate first, then cross
    </h2>
    <p class="doc-p">
      Several internal requesters have to reach one memory. The naive structure
      crosses each requester into the memory's clock domain and then arbitrates
      there, which needs five asynchronous FIFOs <i>per requester</i>.
      Arbitrating first and crossing once needs five in total, whatever the
      requester count is. The result is the one AXI master each mesh presents to
      DRAM — and that master is what
      <RouterLink to="/component/xache" class="doc-link"
        >Kohaku Xache</RouterLink
      >
      takes in, M of them, and serves from N cached channels.
    </p>

    <Fig
      caption="There is no crossbar in the concentrator, because there is one slave: arbitration is a mux and response routing is a decode of bits that are already in flight — BID and RID say where each response goes, so no scoreboard is kept and none has to be sized. The crossbar between meshes and channels is the Xache's, on the other side of this master."
      zoom
      wide
    >
      <BlockDiagram :nodes="conc.nodes" :edges="conc.edges" />
    </Fig>

    <Callout kind="rule" title="Response routing is the ID, not a table">
      <p>
        The requester index is prepended to <code>AWID</code> /
        <code>ARID</code>, so <code>BID</code> / <code>RID</code> say where the
        response goes. The cost is that the slave's ID width must be wide enough
        to carry the index, which is why the module
        <b>derives it rather than taking it as a parameter</b>. The Xache does
        the same one level up: the master index is prepended to the ID a channel
        sees, and the channel's <code>RID</code> routes the beat back.
      </p>
      <p>
        What this deliberately is <i>not</i>: address decode, protocol
        conversion, or arbitrary topology. Optional AXI signals — lock, cache,
        prot, QoS, region — are not carried, because no master in the design
        drives them and every slave takes its defaults.
      </p>
    </Callout>

    <Callout
      kind="trap"
      title="This concentrator is built twice, and the second copy is not on this page"
    >
      <p>
        <code>axi_n1.v</code> here and <code>mag_dram_port.v</code> in the
        memory agent solve the same problem with the same structure — round
        robin, five queues, index-in-ID response routing, one asynchronous
        crossing. <code>mag_dram_port</code> additionally packs the internal
        beat up to the memory beat, carries byte strobes, and keeps
        <code>RD_OUT</code> read bursts in flight per requester — the depth the
        Xache's read queue needs a master to have.
      </p>
      <p>
        It matters to a reader rather than only to a maintainer:
        <b>a fix to one does not reach the other</b>, and the diagram above
        describes both, so a bug found here has a second home nobody will think
        to check. If you are extending concentration, extend both or merge them
        with the packing ratio as a parameter.
      </p>
    </Callout>

    <Callout kind="note" title="Width and clocks belong at the boundary">
      <p>
        The mesh's internal beat matches the flit payload, so nothing in the
        fabric or the memory agent ever gears between two widths. The packing
        therefore happens in the same module as the concentration and the clock
        crossing — which is what lets a device image change its memory width
        without any module inside the mesh knowing.
      </p>
      <p>
        A domain boundary exists in four places: <b>memory</b>, in the
        concentrator, through asynchronous FIFOs; <b>the host</b>, in whichever
        vendor interconnect merges the debug bridge and the DMA engine onto the
        control path — which is already multi-clock and is the right place to
        leave it; <b>a station-bus manager port</b>, whose shim crosses into the
        bus clock with no parameter describing the relationship; and
        <b>a Xache port that declares itself off the fabric clock</b>, per port,
        at its edge — a port on the fabric clock has no crossing at all.
      </p>
      <p>
        The queue depths split by job: address queues only have to cover the
        crossing latency, so they are small and want distributed RAM; write and
        read data queues are sized for burst throughput, so they are deep and
        wide and want block RAM. The parameters are separate for that reason.
      </p>
    </Callout>

    <h2 class="doc-h2">Kind 3 — between meshes: AXI-Stream with credits</h2>
    <p class="doc-p">
      The interlink is not AXI4 at all but AXI4-Stream, and it uses the stream's
      signals for their shape — <code>TDATA</code>, <code>TUSER</code>,
      <code>TLAST</code>, <code>TVALID</code> — while replacing its flow
      control. A flit that must reach another mesh is addressed to the local
      memory agent's port, and the real destination rides in header fields the
      message class does not otherwise use; <code>mag_ilink</code> packs a burst
      of them into one packet, <code>mag_switch</code> routes it one position
      along the line of meshes, and <code>mag_link</code> puts it on the SLL
      with registers on both sides and nothing between.
    </p>
    <SpecTable
      :cols="ilCols"
      :rows="ilRows"
      caption="src/kohakuaccel/sysnode/interlink/ — mag_ilink, mag_switch, mag_link, mag_link_pipe, mag_link_cdc, il_pkt_arb. The parameters and their bounds are in docs/spec/parameters.md §7; the encapsulation and doorbell semantics are on the system node's pages"
    />
    <Callout
      kind="trap"
      title="A credited link that ties tready high can overflow — and this one says so"
    >
      <p>
        The station bus sizes its receive FIFO from its credit count, so
        overflow there is impossible by construction. The interlink's clock
        crossing, <code>mag_link_cdc</code>, is a plain asynchronous FIFO with
        <code>tready</code> tied high, so its depth
        <b>must exceed the sender's total credit</b> — both classes together —
        or <code>xpm_fifo_async</code> drops a beat with no flag. The parameter
        carries that bound, the far end must be a <code>mag_link</code>, and a
        sticky fault bit plus a software poll are what stands between a
        misconfiguration and silence.
      </p>
    </Callout>

    <h2 class="doc-h2">Conventions</h2>
    <Callout
      kind="rule"
      title="Command a submodule through a slice of the control window, never through loose sideband ports"
    >
      <p>
        <i>(Forced, by the build flow rather than by logic.)</i> A block design
        carries clock, reset and AXI across a module boundary and nothing else.
        Sideband ports do not get wired, and the failure is that
        <b>a shipped engine is commandable by nothing</b> — which is exactly
        what happened to the memory mover before its command path moved into the
        window.
      </p>
    </Callout>
    <Callout
      kind="rule"
      title="An unconnected output is harmless; an undriven input is the fault"
    >
      <p>
        <i>(Free.)</i> Swapping vendor IP for RTL moves the wiring from a block
        design's inference to your port list. Keep the interface-inference
        attributes on the ports so the tool still ties clocks, resets and
        interfaces up on its own.
      </p>
    </Callout>
  </DocPage>
</template>
