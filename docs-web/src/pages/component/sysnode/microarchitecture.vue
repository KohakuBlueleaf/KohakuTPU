<script setup>
// ===========================================================================
// Memory agent — microarchitecture.
//
// The companion page (/framework/sysnode) is the WHAT: the instruction set, the
// contracts, the two addon slots. This one is the HOW, read out of the RTL:
// two instruction streams inside one module, the tag that makes out-of-order
// responses legal, the slot table and the fairness rule that lost a dirty
// line, and the staging store that is deliberately not a cache.
//
// Every trap here is a failure that HAPPENED. Each one names the file.
// ===========================================================================

// ------------------------------------------------------------- the port
// Landscape. Read path on rows 0 and 5, write path on rows 10 and 15, one
// output register at the right where the two meet.
const port = {
  nodes: [
    { id: "in", x: 0, y: 5, w: 11, label: "flits in", sub: "mem_in_valid · busy" },

    { id: "rdq", x: 13, y: 0, w: 11, label: "u_rdq", sub: "MEM_RD_REQ" },
    { id: "wrq", x: 13, y: 10, w: 11, label: "u_wrq", sub: "WR_REQ + WR_DATA" },

    { id: "rs", x: 26, y: 0, w: 11, label: "rs — read engine", sub: "IDLE FILL WAIT STG", accent: true },
    { id: "slots", x: 26, y: 10, w: 11, label: "ws_* slot table", sub: "matched by src_x, src_y" },
    { id: "rdyq", x: 26, y: 15, w: 11, label: "u_rdyq", sub: "ready order, depth WR_SLOTS" },

    { id: "axir", x: 39, y: 0, w: 11, label: "AXI read channel", sub: "AR out · R back" },
    { id: "st", x: 39, y: 10, w: 11, label: "st — write + plain read", sub: "IDLE RD_DATA WR_DATA WR_ACK", accent: true },

    { id: "skid", x: 52, y: 0, w: 11, label: "u_rskid", sub: "R crossed uncut, then registered" },
    { id: "stg", x: 39, y: 5, w: 11, label: "mag_stage", sub: "aperture 0 · STAGE != 0" },
    { id: "axiw", x: 52, y: 10, w: 11, label: "AXI write channel", sub: "AW · W · B" },

    { id: "pw", x: 65, y: 0, w: 11, label: "p_w0..3", sub: "beats that ARE words" },
    { id: "ew", x: 52, y: 5, w: 11, label: "e_w0..3", sub: "emit buffer · e_tag · e_dst", accent: true },

    { id: "out", x: 65, y: 5, w: 11, label: "mem_out_data", sub: "one register · emitter wins", accent: true },
  ],
  edges: [
    { from: "in:r", to: "rdq:l", dir: "h" },
    { from: "in:r", to: "wrq:l", dir: "h" },
    { from: "rdq:r", to: "rs:l", dir: "h", accent: true },
    { from: "rs:r", to: "axir:l", dir: "h", accent: true, label: "AR" },
    { from: "axir:r", to: "skid:l", dir: "h", label: "R" },
    { from: "skid:r", to: "pw:l", dir: "h", accent: true, label: "a beat IS a word" },
    { from: "stg:r", to: "ew:l", dir: "h", dash: true, label: "staged fill" },
    { from: "pw:b", to: "ew:t", dir: "v" },
    { from: "ew:r", to: "out:l", dir: "h", accent: true },
    { from: "wrq:r", to: "slots:l", dir: "h" },
    { from: "slots:b", to: "rdyq:t", dir: "v", label: "ready" },
    { from: "rdyq:r", to: "st:l", dir: "h", label: "ws_pick" },
    { from: "st:r", to: "axiw:l", dir: "h" },
    { from: "st:t", to: "out:b", dir: "v", dash: true, label: "WR_ACK" },
  ],
}

// --------------------------------------------------------- the two machines
const stSm = {
  states: [
    { id: "S_IDLE", x: 0, y: 0, label: "IDLE", sub: "0" },
    { id: "S_RD_DATA", x: 7, y: -3.5, label: "RD_DATA", sub: "2" },
    { id: "S_WR_DATA", x: 7, y: 3.5, label: "WR_DATA", sub: "5" },
    { id: "S_WR_ACK", x: 14, y: 3.5, label: "WR_ACK", sub: "6" },
  ],
  edges: [
    { from: "S_IDLE", to: "S_RD_DATA", label: "take_rd_p" },
    { from: "S_RD_DATA", to: "S_RD_DATA", label: "beat → flit", self: true },
    { from: "S_RD_DATA", to: "S_IDLE", label: "r_last", curve: -70 },
    { from: "S_IDLE", to: "S_WR_DATA", label: "ws_issue" },
    { from: "S_WR_DATA", to: "S_WR_ACK", label: "m_wlast" },
    { from: "S_WR_ACK", to: "S_IDLE", label: "B, and st_out", curve: 100 },
  ],
}

const rsStates = [
  { id: "RS_IDLE", x: 0, y: 0, label: "IDLE" },
  { id: "RS_FILL", x: 7, y: 0, label: "FILL", sub: "R beats" },
  { id: "RS_WAIT", x: 14, y: 0, label: "WAIT", sub: "emit" },
  { id: "RS_STG", x: 7, y: 5, label: "STG", sub: "staged" },
]
const rsEdges = [
  { from: "RS_IDLE", to: "RS_FILL", label: "take_rd_e" },
  { from: "RS_FILL", to: "RS_FILL", label: "beat", self: true },
  { from: "RS_FILL", to: "RS_WAIT", label: "last beat" },
  { from: "RS_WAIT", to: "RS_FILL", label: "entry i+1", curve: -70 },
  { from: "RS_WAIT", to: "RS_IDLE", label: "run done", curve: 145 },
  { from: "RS_IDLE", to: "RS_STG", label: "staged" },
  { from: "RS_STG", to: "RS_WAIT", label: "stg_rvalid" },
]

const machineCols = [
  { key: "m", label: "Machine", mono: true },
  { key: "runs", label: "Runs" },
  { key: "own", label: "Owns" },
  { key: "shares", label: "Shares" },
]
const machineRows = [
  {
    m: "st",
    runs: "writes, and the <b>plain</b> read (<code>STREAM = 0</code>), which only benches use",
    own: "<code>rq_x/rq_y/rq_txn</code>, the AW/W/B channels, <code>wb_cnt</code>",
    shares: "the output register",
  },
  {
    m: "rs",
    runs: "every <b>entry</b> read — <code>STREAM = 1</code>, from DRAM or from staging",
    own: "<code>rd_*</code>, <code>p_w0..3</code>, <code>e_w0..3</code>, the AR/R channels",
    shares: "the output register",
    _tone: "good",
  },
]

// ------------------------------------------------ take_rd_p versus take_rd_e
const takeCols = [
  { key: "t", label: "Term", mono: true },
  { key: "when", label: "Taken when", mono: true },
  { key: "cost", label: "What it costs the write path" },
]
const takeRows = [
  {
    t: "take_rd_p",
    when: "!in_stream &amp;&amp; rd_free &amp;&amp; (st == S_IDLE)",
    cost: "<b>excludes a write for the whole burst.</b> <code>ws_issue</code> requires <code>st == S_IDLE</code>, and it also carries <code>!take_rd_p</code>, so the plain read wins the cycle it is taken and holds the machine until <code>r_last</code>",
    _tone: "bad",
  },
  {
    t: "take_rd_e",
    when: "in_stream &amp;&amp; rd_free &amp;&amp; (st != S_RD_DATA)",
    cost: "<b>nothing.</b> It runs in <code>rs</code>, never enters <code>st</code>, and writes keep being issued underneath it",
    _tone: "good",
  },
]

const plainRead = {
  rows: [
    { name: "take_rd_p", kind: "bit", values: [1, 0, 0, 0, 0, 0], mark: [0] },
    { name: "st", kind: "bus", values: ["IDLE", "RD_DATA", "RD_DATA", "RD_DATA", "RD_DATA", "IDLE"] },
    { name: "ws_has_pick", kind: "bit", values: [1, 1, 1, 1, 1, 1] },
    { name: "ws_issue", kind: "bit", values: [0, 0, 0, 0, 0, 1], mark: [5] },
    { name: "mem_out_data", kind: "bus", values: [null, "beat 0", "beat 1", "beat 2", "beat 3", null] },
    {
      name: "",
      kind: "text",
      values: ["AR out", "beat → flit", "", "", "r_last", "the write finally issues"],
    },
  ],
  notes: [
    {
      cycle: 0,
      text: "A slot has been ready since before the read arrived. It cannot be issued: ws_issue is (st == S_IDLE) && ws_has_pick && !take_rd_p, and st is not S_IDLE again until the last beat has left.",
      tone: "bad",
    },
    {
      text: "This is not a defect. memory-protocol.md §3.1 says the plain path exists for benches and bring-up, and it turns each AXI beat straight into a response flit — which is why it may take a beat only when the output register is free.",
    },
  ],
}

const entryRead = {
  rows: [
    { name: "take_rd_e", kind: "bit", values: [1, 0, 0, 0, 0, 0], mark: [0] },
    { name: "rs", kind: "bus", values: ["IDLE", "FILL", "FILL", "FILL", "WAIT", "WAIT"] },
    { name: "st", kind: "bus", values: ["IDLE", "WR_DATA", "WR_DATA", "WR_ACK", "IDLE", "WR_DATA"] },
    { name: "ws_issue", kind: "bit", values: [0, 1, 0, 0, 0, 1], mark: [1, 5] },
    { name: "", kind: "text", values: ["AR out", "a write starts", "", "B", "acked", "the next write"] },
  ],
  notes: [
    {
      cycle: 1,
      text: "st is free the whole time, because the entry read never enters it. Two independent interfaces run at the larger of their times instead of the sum.",
      tone: "good",
    },
    {
      text: "A unit that reads and writes concurrently MUST therefore use the entry form. mag_mem_port.v states the reason in the header: run a streaming fetch inside st and S_IDLE never comes round, so the slots fill, intake jams on a write descriptor nothing will accept, and the data flit behind it reports “no open write”.",
      tone: "good",
    },
  ],
}

// ----------------------------------------------------- the descriptor walk
const chip = (rs, ent, ar, anext, q, e) => [
  { k: "rs", v: rs },
  { k: "rd_ent", v: ent },
  { k: "m_araddr", v: ar },
  { k: "rd_anext", v: anext },
  { k: "q_start", v: q },
  { k: "e_act", v: e },
]

const walk = [
  {
    title: "the descriptor is at the head of u_rdq",
    active: "RS_IDLE",
    chips: chip("IDLE", "—", "—", "—", "0", "0"),
    note: "STREAM = 1, count = 2, txn = T, addr = A. rd_free is (rs == RS_IDLE) && !e_act — the emit buffer has to be empty too, because the run is about to claim it.",
  },
  {
    title: "take_rd_e — the whole run is captured in one cycle",
    active: "RS_FILL",
    chips: chip("FILL", "0", "A", "A + 128", "1", "0"),
    note: "rd_base, rd_cnt, rd_ebytes, rd_anext, rd_elast, rd_peer and rd_nd all latch here. m_arlen is entry_words − 1 — 3 by default, so a 1024-bit entry is 4 beats at DATA_W = 256.",
  },
  {
    title: "beats 0–2, pushed at line rate",
    active: "RS_FILL",
    chips: chip("FILL", "0", "—", "A + 128", "0", "0"),
    note: "r_ready is ((st == S_RD_DATA) && st_out) || (rs == RS_FILL). RS_FILL takes a beat unconditionally because it buffers into p_w0..3 and emits later, behind the emit buffer's own guard; S_RD_DATA cannot, because accepting one would overwrite a flit the mesh has not taken.",
  },
  {
    title: "beat 3 lands — and the NEXT address goes out in the same cycle",
    active: "RS_WAIT",
    chips: chip("WAIT", "0", "A + 128", "A + 256", "0", "0"),
    note: "Not after the entry has finished leaving. rd_anext is ACCUMULATED: computing base + (ent+1) × ebytes instead cost 86 MHz, because against a register that product is a real full-width multiplier sitting in the AR address path.",
  },
  {
    title: "q_done fires, and q_rdy remembers it",
    active: "RS_WAIT",
    chips: chip("WAIT", "0", "—", "A + 512", "0", "0"),
    note: "q_done is a one-cycle pulse. If the emit buffer is still busy when it fires, the entry has to be remembered — or the read engine waits forever for an edge that already happened.",
  },
  {
    title: "hand off and re-arm, in ONE cycle",
    active: "RS_FILL",
    chips: chip("FILL", "1", "—", "A + 512", "1", "1"),
    note: "e_w0..3 ← q_w0..3, e_tag ← rd_txn + rd_ent, e_act ← 1, e_dst ← 0, q_emit ← 0 — and q_start is raised again for entry 1, whose beats are already arriving. The AXI read of entry 1 and the NoC emit of entry 0 use different wires; sharing the transform's output registers is the only thing that would serialise them.",
  },
  {
    title: "word 0 out, while entry 1 is still on the bus",
    active: "RS_FILL",
    chips: chip("FILL", "1", "—", "A + 512", "0", "1"),
    note: "emit_go is e_act && out_free, and out_free covers the cycle a flit is being ACCEPTED rather than only an empty register — waiting for the register to read empty halves the response rate.",
  },
  {
    title: "words 1, 2, 3 — last on word 3, then the peers",
    active: "RS_FILL",
    chips: chip("FILL", "1", "—", "A + 512", "0", "1"),
    note: "last is (q_emit == rd_elast). If e_dst < rd_nd the emitter sets q_emit back to 0 and re-sends the SAME latched words with a different header: no second AXI read, no second pass of the transform.",
  },
  {
    title: "entry 1 emits as txn T+1, and the run ends",
    active: "RS_IDLE",
    chips: chip("IDLE", "—", "—", "—", "0", "0"),
    note: "Two entries, eight response flits, one request flit, sixteen AXI beats, and no cursor anywhere.",
  },
]

// ---------------------------------------------------------- the response tag
const respHdr = [
  { name: "dst_x", bits: 4, value: "e_dx" },
  { name: "dst_y", bits: 4, value: "e_dy" },
  { name: "src_x", bits: 4, value: "MEM_X" },
  { name: "src_y", bits: 4, value: "MEM_Y" },
  { name: "type", bits: 4, value: "0x2" },
  { name: "txn", bits: 8, value: "rd_txn + rd_ent", accent: true },
  { name: "last", bits: 1, value: "q_emit == rd_elast" },
  { name: "rsvd[2]", bits: 1, value: "0" },
  { name: "rsvd[1:0]", bits: 2, value: "q_emit", accent: true },
  { name: "payload", bits: 256, value: "e_w0 / e_w1 / e_w2 / e_w3" },
]

const outOfOrder = {
  rows: [
    { name: "flit arriving", kind: "bus", values: ["T+1 w2", "other traffic", "T+0 w3", "T+1 w0", "T+0 w1", "T+1 w3"] },
    { name: "txn − T", kind: "bus", values: ["1", "—", "0", "1", "0", "1"] },
    { name: "rsvd[1:0]", kind: "bus", values: ["2", "—", "3", "0", "1", "3"] },
    { name: "last", kind: "bit", values: [0, 0, 1, 0, 0, 1] },
    { name: "write address", kind: "bus", values: ["slot 1, w2", "—", "slot 0, w3", "slot 1, w0", "slot 0, w1", "slot 1, w3"], mark: [0, 2, 3, 4, 5] },
  ],
  notes: [
    {
      text: "Every flit names its own destination. The receiver derives the write address from the header and never from a counter, so this order and any other produce the same memory image.",
      tone: "good",
    },
    {
      text: "This arrival order is NOT what one agent produces today — one server finishes an entry's words before starting the next. That is a property of the SERVER, and memory-protocol.md §3.2.1 says a requester relying on it MUST assert it rather than assume it. Drawn here as what the tagging makes legal.",
    },
  ],
}

// -------------------------------------------------------- the write slots
const slotCols = [
  { key: "f", label: "Field", mono: true },
  { key: "w", label: "Per", mono: true, align: "center" },
  { key: "m", label: "Meaning" },
]
const slotRows = [
  { f: "ws_val", w: "slot", m: "a descriptor has been seen" },
  { f: "ws_rdy", w: "slot", m: "all <code>len + 1</code> beats have landed" },
  { f: "ws_iss", w: "slot", m: "the burst is <b>on the AXI bus</b>. Without this bit a slot on the bus reads as {val, !rdy}, indistinguishable from one still waiting for data, and the next <code>MEM_WR_DATA</code> from that source binds to the in-flight slot", _tone: "warn" },
  { f: "ws_x, ws_y", w: "slot", m: "the source coordinate — <b>the only binding</b> between a descriptor and its data" },
  { f: "ws_addr, ws_txn", w: "slot", m: "captured from the descriptor" },
  { f: "ws_stg, ws_bad", w: "slot", m: "lands in staging rather than DRAM; or names a reserved aperture and goes nowhere at all" },
  { f: "ws_len, ws_cnt", w: "slot", m: "beats expected and beats received, both <code>$clog2(WBURST) + 1</code> bits — which is where a <code>len &gt; 7</code> wraps" },
  { f: "ws_data", w: "slot × beat", m: "<code>WR_SLOTS × WBURST</code> beats of <code>DATA_W</code>. The part that grows fastest, and both factors are sized for correctness rather than tuned" },
  { f: "ws_fill_now", w: "slot", m: "“this slot's next beat is its last”, precomputed from <b>registered state only</b>, so the ready decision is a 1-bit select instead of mux → add → compare" },
]

const scanCols = [
  { key: "s", label: "Scan", mono: true },
  { key: "picks", label: "Picks" },
  { key: "note", label: "Note" },
]
const scanRows = [
  {
    s: "ws_free",
    picks: "the <b>lowest</b> free slot",
    note: "one combinational loop counting downward, so the last assignment — the lowest index — wins",
  },
  {
    s: "ws_match",
    picks: "the lowest slot with <code>val &amp;&amp; !rdy &amp;&amp; !iss</code> whose <code>{x, y}</code> equals the arriving data flit's source",
    note: "<code>!ws_iss</code> is the whole reason the third bit exists",
  },
  {
    s: "ws_pick",
    picks: "the <b>head of <code>u_rdyq</code></b> — ready order, never lowest-ready",
    note: "a FIFO of slot indices, <code>WR_SLOTS</code> deep so it cannot overflow, pushed on the cycle a slot's last beat lands",
    _tone: "good",
  },
]

const starveBroken = {
  rows: [
    { name: "ws_free (lowest free)", kind: "bus", values: ["15", "0", "1", "0", "1", "0"] },
    { name: "ready set", kind: "bus", values: ["{0,15}", "{1,15}", "{0,15}", "{1,15}", "{0,15}", "{1,15}"] },
    { name: "lowest ready → issued", kind: "bus", values: ["0", "1", "0", "1", "0", "1"], mark: [0, 1, 2, 3, 4, 5] },
    { name: "slot 15", kind: "bus", values: ["ready", "ready", "ready", "ready", "ready", "ready"], mark: [5] },
    { name: "", kind: "text", values: ["", "", "", "", "", "slot 15 still holds it"] },
  ],
  notes: [
    {
      text: "Rounds, not cycles. AXI drains and the slots free from the bottom, so every later writeback is handed 0 or 1 — the lowest free, and then the lowest ready. Slot 15 is never the lowest ready again.",
      tone: "bad",
    },
    {
      text: "Measured on a 64-line flush: slot 15 took a dirty line, MAG alternated slots 0 and 1 for the other sixty-three, and that line never reached DRAM. Silently. The RTL records the same shape as an L1 writeback held 2,300+ cycles by an RMW stream.",
      tone: "bad",
    },
  ],
}

const reorderBroken = {
  rows: [
    { name: "one source sends", kind: "bus", values: ["W1 desc", "W1 data", "W2 desc", "W2 data", "—", "—"] },
    { name: "allocated slot", kind: "bus", values: ["15", "15", "3", "3", "—", "—"] },
    { name: "goes ready", kind: "bus", values: [null, "15", null, "3", null, null] },
    { name: "lowest ready issues", kind: "bus", values: [null, null, null, null, "3", "15"], mark: [4, 5] },
    { name: "memory holds", kind: "bus", values: [null, null, null, null, "W2", "W1"], mark: [5] },
  ],
  notes: [
    {
      cycle: 5,
      text: "W1 and W2 go to the SAME address. Slot 3 was freed between them, so W2 lands in a lower slot than W1 and is issued first — two writes to one address end holding the FIRST value.",
      tone: "bad",
    },
    {
      text: "Both failures are reproduced deterministically by tests/sysnode/mag_mem_port_tb.v as named checks, `starve` and `reorder`, so a fix is judged there rather than through a DRAM diff. The starvation check is a per-slot bound: a ramp slot waited out the whole stream, measured 4,600+ cycles at 300 rounds against an ACK_BOUND of 1,200.",
      tone: "bad",
    },
  ],
}

const starveFixed = {
  rows: [
    { name: "ws_free (lowest free)", kind: "bus", values: ["15", "0", "1", "0", "1", "0"] },
    { name: "rdyq_push", kind: "bus", values: ["15", "0", "1", "0", "1", "0"] },
    { name: "u_rdyq head", kind: "bus", values: ["15", "0", "1", "0", "1", "0"], mark: [0] },
    { name: "issued", kind: "bus", values: ["15", "0", "1", "0", "1", "0"], mark: [0] },
    { name: "", kind: "text", values: ["the dirty line goes out", "", "", "", "", ""] },
  ],
  notes: [
    {
      cycle: 0,
      text: "Allocation is UNCHANGED — still the lowest free slot. What changed is the pick: ws_pick is the head of u_rdyq, pushed the cycle take_wr_data lands a slot's last beat.",
      tone: "good",
    },
    {
      text: "Ready order IS per-source program order, so the reorder above cannot happen either, and the queue's head is at most WR_SLOTS-1 services from issue. Two simulation assertions guard the pair against drifting apart: a ready head that never issues, and an issued slot that is not ready.",
      tone: "good",
    },
  ],
}

// ------------------------------------------------------------ intake accept
const takeBroken = {
  rows: [
    { name: "mem_in_valid", kind: "bit", values: [1, 1, 1, 0] },
    { name: "mem_in_busy", kind: "bit", values: [1, 1, 0, 0] },
    { name: "rq_full / wq_full", kind: "bit", values: [0, 0, 0, 0] },
    { name: "enqueued", kind: "bus", values: ["WR_DATA", "WR_DATA", "WR_DATA", null], mark: [0, 1, 2] },
    { name: "", kind: "text", values: ["the sender is held off", "still held off", "accepted", ""] },
  ],
  notes: [
    {
      cycle: 0,
      text: "Enqueuing on “is there room” writes the SAME flit once per cycle of backpressure, because the link holds a flit asserted until a cycle with busy low. The duplicate overruns its slot's ws_len and the surplus matches nothing.",
      tone: "bad",
    },
    {
      text: "The mirror-image error is just as silent: a DROPPED beat leaves the slot short forever, so it never becomes ready, the source's next descriptor opens a SECOND slot, and its data matches the older one.",
      tone: "bad",
    },
  ],
}

const takeFixed = {
  rows: [
    { name: "mem_in_valid", kind: "bit", values: [1, 1, 1, 0] },
    { name: "mem_in_busy", kind: "bit", values: [1, 1, 0, 0] },
    { name: "mi_take", kind: "bit", values: [0, 0, 1, 0], mark: [2] },
    { name: "enqueued", kind: "bus", values: [null, null, "WR_DATA", null], mark: [2] },
  ],
  notes: [
    {
      text: "mi_take = mem_in_valid && !mem_in_busy — accept exactly when the sender believes we did. Both errors are silent and permanent, so the accept term and the busy term must be the same predicate.",
      tone: "good",
    },
    {
      text: "mem_in_busy is “either queue is at Q_DEPTH − Q_MARGIN, or either FIFO says almost”. It counts for itself rather than trusting the FIFO's almost flag, which is not a margin.",
      tone: "good",
    },
  ],
}

// ---------------------------------------------------------- what is sliced
const sliceCols = [
  { key: "f", label: "Field", mono: true },
  { key: "s", label: "The slice in mag_mem_port.v", mono: true },
  { key: "n", label: "Normalised on capture to" },
]
const sliceRows = [
  {
    f: "addr",
    s: "rq_flit[255 -: 40]",
    n: "<b>40 bits whatever <code>ADDR_W</code> is</b> — a flit contract, not a width. Slicing it by <code>ADDR_W</code> read <code>addr &gt;&gt; 6</code> on a 34-bit build, silently",
    _tone: "warn",
  },
  { f: "len", s: "rq_flit[215 -: 8]", n: "on a write, <code>wi_len[WBW:0] + 1</code> — truncated to 4 bits, so a descriptor with <code>len &gt; 7</code> wraps and the data buffer aliases" },
  { f: "flags", s: "rq_flit[207 -: 8]", n: "<code>[6]</code> STREAM. <code>[4]</code> and <code>[5]</code> are <b>reserved and ignored</b> — they were QUANT and BLAYOUT, and a fetch is never transformed. Bits 0–3 and 7 are read by nothing" },
  { f: "count", s: "rq_flit[199 -: 8]", n: "1 unless STREAM is set; 0 becomes 1. The run is 1–255 entries" },
  { f: "peer", s: "rq_flit[191 -: 24]", n: "three <code>{y, x}</code> bytes, selected by <code>e_dst</code> at emit time" },
  { f: "n_peer", s: "rq_flit[167 -: 2]", n: "0–3 extra destinations" },
  { f: "entry_words", s: "rq_flit[165 -: 8]", n: "<b>4</b> if it is 0 or above 4. <code>rd_elast</code> is then <code>in_ew[1:0] − 1</code>, a deliberate two-bit wrap that turns 4 words into a last index of 3" },
]

// ------------------------------------------------------------- no strobe
const strobeCols = [
  { key: "p", label: "Path" },
  { key: "s", label: "Byte enable" },
  { key: "c", label: "Consequence" },
]
const strobeRows = [
  {
    p: "<code>MEM_WR_DATA</code> → <code>mag_mem_port</code> → AXI",
    s: "<b>none.</b> The flit payload is 256 bits of data with no fields at all (flit-format §4.2), and <code>m_wstrb</code> is <code>{(DATA_W/8){1'b1}}</code>",
    c: "<b>every DRAM write writes every byte of the beat.</b> A partial-line store is a read-modify-write, and the requester does it",
    _tone: "bad",
  },
  {
    p: "<code>mag_dram_port</code>'s internal <code>w_strb</code>",
    s: "carried, never synthesised — “a requester doing a partial write has its untouched bytes clobbered otherwise”",
    c: "the mechanism exists on the converged path; the memory port simply hands it all ones. Strobes start <b>cleared</b> in the width packer, so a partial head and a partial tail both fall out with no special case",
  },
  {
    p: "the host memory window",
    s: "<code>sm_wstrb</code>, straight from the host AXI slave",
    c: "the host can write sub-beat; a compute unit cannot",
  },
  {
    p: "unit-to-unit <code>CU_DATA</code>",
    s: "a <code>buf_id</code> whose contract is one 32-bit word with byte enables — <code>BUF_SPAD_W</code> in the reference PE",
    c: "<b>byte granularity exists here and nowhere else.</b> It never reaches DRAM",
    _tone: "good",
  },
]

// ---------------------------------------------------------- transform slot
const xfCols = [
  { key: "c", label: "Constant", mono: true },
  { key: "v", label: "At DATA_W = 256", mono: true, align: "right" },
  { key: "w", label: "Written as" },
]
const xfRows = [
  { c: "Q_ENTRY_BITS", v: "2048", w: "a literal — one entry is 4 lanes × 32 elements as FP16" },
  { c: "P_ENTRY_BITS", v: "1024", w: "a literal — the same entry as operand words" },
  { c: "Q_ENTRY_BYTES", v: "256", w: "<code>Q_ENTRY_BITS / 8</code>" },
  { c: "P_ENTRY_BYTES", v: "128", w: "<code>P_ENTRY_BITS / 8</code>" },
  { c: "Q_ARLEN", v: "7", w: "<code>Q_ENTRY_BITS / DATA_W − 1</code>", _tone: "good" },
  { c: "P_ARLEN", v: "3", w: "<code>P_ENTRY_BITS / DATA_W − 1</code>", _tone: "good" },
]

// --------------------------------------------------------------- staging
const apAddr = [
  { name: "aperture", bits: 1, value: "1", accent: true },
  { name: "rsvd", bits: 1, value: "MUST be 0" },
  { name: "mesh", bits: 2, value: "== MESH_ID", accent: true },
  { name: "aperture id", bits: 4, value: "== AP_STAGE", accent: true },
  { name: "offset", bits: 32, value: "inside the store" },
]

const stage = {
  nodes: [
    { id: "apo", x: 0, y: 0, w: 14, label: "port A", sub: "a_req · one whole entry" },
    { id: "bpo", x: 16, y: 0, w: 14, label: "port B", sub: "b_req · one word" },
    { id: "dec", x: 0, y: 5, w: 30, h: 3.4, label: "decode: mesh FIRST, then aperture", sub: "a_mine · b_mine · a_fault", accent: true },
    { id: "arb", x: 0, y: 10, w: 30, h: 3.4, label: "arbitrate — A wins", sub: "b_go only when it wants the OTHER port" },
    { id: "bk", x: 0, y: 15, w: 14, label: "bank", sub: "the LOW address bits" },
    { id: "rw", x: 16, y: 15, w: 14, label: "row", sub: "the bits above them" },
    { id: "disp", x: 0, y: 20, w: 30, h: 3.4, label: "dispatch registers · PIPE", sub: "ONE COPY PER BANK, DONT_TOUCH", accent: true },
    { id: "banks", x: 0, y: 25, w: 30, h: 3.4, label: "BANKS × WORDS kohaku_sdpram", sub: "MEM_PRIM ultra · READ_LAT 2", accent: true },
    { id: "oreg", x: 0, y: 30, w: 30, h: 3.4, label: "per-bank output register", sub: "the only long wire left is this to the mux" },
    { id: "sr", x: 0, y: 35, w: 14, label: "bk_sr · wd_sr", sub: "RTOT deep, cannot stall" },
    { id: "mux", x: 16, y: 35, w: 14, label: "return mux", sub: "a_rdata / b_rdata", accent: true },
  ],
  edges: [
    { from: "apo:b", to: "dec:t", dir: "v" },
    { from: "bpo:b", to: "dec:t", dir: "v" },
    { from: "dec:b", to: "arb:t", dir: "v", accent: true },
    { from: "arb:b", to: "bk:t", dir: "v" },
    { from: "arb:b", to: "rw:t", dir: "v" },
    { from: "bk:b", to: "disp:t", dir: "v" },
    { from: "rw:b", to: "disp:t", dir: "v" },
    { from: "disp:b", to: "banks:t", dir: "v", accent: true },
    { from: "banks:b", to: "oreg:t", dir: "v" },
    { from: "oreg:b", to: "sr:t", dir: "v" },
    { from: "sr:r", to: "mux:l", dir: "h", accent: true },
  ],
}

const stgCols = [
  { key: "q", label: "Question" },
  { key: "a", label: "The RTL's answer" },
]
const stgRows = [
  { q: "How is it reached?", a: "<b>By address, never by an instruction.</b> Point a fetch at the aperture and it reads back; point a drain there and it stages" },
  { q: "What happens to a foreign mesh's address?", a: "It fails the mesh test <b>first</b>, so it is neither ours nor a fault and <b>passes on untouched</b> — which is what lets mesh 0 reach mesh 3's store" },
  { q: "What happens to a reserved aperture?", a: "<code>a_fault</code>, and the port <b>drops the request rather than aliasing it onto DRAM</b>. The requester then hangs loudly instead of being told a lie" },
  { q: "Does it convert anything?", a: "<b>No.</b> Staging holds operand words verbatim. Nothing on a fetch path converts — the transform slot is the mover's, and it runs before any fetch reads the result", _tone: "warn" },
  { q: "Replacement? Coherence? Write policy?", a: "<b>None of them exist.</b> It is a reserved range in the address map, so there are no tags, no associativity, no replacement, no coherence and no write policy", _tone: "good" },
  { q: "What does software owe?", a: "One thing: <b>results destined for DRAM must use DRAM addresses.</b> Nothing writes back" },
  { q: "How does the host reach it?", a: "It is in the address map, so the host DMA reaches it like any memory — through port B, one word per access" },
  { q: "How many reads outstanding?", a: "<b>One.</b> <code>mag_stage_port</code> holds a single returned word, so a second request would drop the first" },
]

const placeCols = [
  { key: "w", label: "Where the store sits", mono: true },
  { key: "who", label: "Who can be staged" },
  { key: "cost", label: "URAM" },
]
const placeRows = [
  {
    w: "STAGE_AT_PORT = 0",
    who: "one store <b>inside each</b> <code>mag_mem_port</code>, upstream of where the requesters meet — so <b>the mover and the interlink can never be staged</b>",
    cost: "<code>MEM_PORTS</code> × 64. At <code>MEM_PORTS = 4</code> that is <b>256 URAM</b>",
    _tone: "bad",
  },
  {
    w: "STAGE_AT_PORT = 1",
    who: "one store on the <b>converged</b> internal path, in <code>mag_stage_port</code>, in front of <code>mag_dram_port</code> — every requester reaches it",
    cost: "<b>64 URAM</b> per agent: 4 banks × 16,384 entries, 2 MB",
    _tone: "good",
  },
]

// ------------------------------------------------------------- the q contract
const qBroken = {
  rows: [
    { name: "q_valid", kind: "bit", values: [1, 1, 1, 1] },
    { name: "q_write", kind: "bit", values: [1, 1, 0, 0], mark: [2] },
    { name: "q_addr", kind: "bus", values: ["W", "W", "R", "R"], mark: [2] },
    { name: "arbiter's snapshot", kind: "bus", values: ["—", "write", "write", "write"] },
    { name: "captured address", kind: "bus", values: [null, null, null, "R"], mark: [3] },
    { name: "q_ready (one wire)", kind: "bit", values: [0, 0, 0, 1] },
    { name: "", kind: "text", values: ["offered as a write", "", "switched to a read", "the WRONG channel pops"] },
  ],
  notes: [
    {
      cycle: 3,
      text: "Both arbiters decide on a REGISTERED request vector and sample the bus LIVE. A presentation that switches mid-wait gets the other transaction's address captured, and the single grant wire then pops whichever channel the requester is offering now.",
      tone: "bad",
    },
    {
      text: "Measured in rv_mc4: a writeback of line 641 landed at 787 and the fill returned 641. Nothing on the AXI side is wrong — the burst is well formed, it is simply someone else's.",
      tone: "bad",
    },
  ],
}

const qFixed = {
  rows: [
    { name: "q_valid", kind: "bit", values: [1, 1, 1, 1] },
    { name: "sel_h (holding)", kind: "bit", values: [0, 1, 1, 1], mark: [1] },
    { name: "q_write", kind: "bit", values: [1, 1, 1, 1] },
    { name: "q_addr", kind: "bus", values: ["W", "W", "W", "W"] },
    { name: "q_ready", kind: "bit", values: [0, 0, 0, 1] },
    { name: "", kind: "text", values: ["write wins at FIRST offer", "choice held", "held", "granted — as offered"] },
  ],
  notes: [
    {
      text: "The adapter in mag.v latches which channel it offered on the first cycle the request was not granted, and holds it until q_ready. Write wins when both are offered, but only at the first offer.",
      tone: "good",
    },
    {
      text: "mag_dram_port carries the matching guard as a simulation assertion: a requester that changes {write, addr, len} while waiting is reported by name, with the old and new values, rather than silently crossing two transactions.",
      tone: "good",
    },
  ],
}

// ----------------------------------------------------------------- the mover
const mover = {
  nodes: [
    { id: "cfg", x: 0, y: 0, w: 30, h: 3.4, label: "cfg_en · cfg_addr · cfg_data", sub: "a slice of the control window, offsets preserved" },
    { id: "src", x: 0, y: 5, w: 14, label: "u_src — mx_tdesc", sub: "6 dims, bound axes" },
    { id: "dst", x: 16, y: 5, w: 14, label: "u_dst — mx_tdesc", sub: "DEFINES the iteration space", accent: true },
    { id: "lat", x: 0, y: 10, w: 30, h: 3.4, label: "the element latch", sub: "e_rd · e_wr · e_kind · e_last · e_flt", accent: true },
    { id: "issue", x: 0, y: 15, w: 30, h: 3.4, label: "the ISSUE engine", sub: "folds consecutive addresses into bursts", accent: true },
    { id: "ar", x: 0, y: 20, w: 14, label: "u_arskid → AR", sub: "MAX_OUT in flight" },
    { id: "cq", x: 16, y: 20, w: 14, label: "u_cfifo", sub: "write commands" },
    { id: "df", x: 0, y: 25, w: 14, label: "u_dfifo", sub: "512 × 256b, block" },
    { id: "wr", x: 0, y: 30, w: 30, h: 3.4, label: "the WRITE engine", sub: "IDLE · ARM · GEN · DATA", accent: true },
    { id: "axi", x: 0, y: 35, w: 30, h: 3.4, label: "its own AXI master", sub: "AW · W · B — and NO fabric endpoint" },
  ],
  edges: [
    { from: "cfg:b", to: "src:t", dir: "v" },
    { from: "cfg:b", to: "dst:t", dir: "v" },
    { from: "src:b", to: "lat:t", dir: "v" },
    { from: "dst:b", to: "lat:t", dir: "v", accent: true },
    { from: "lat:b", to: "issue:t", dir: "v", accent: true },
    { from: "issue:b", to: "ar:t", dir: "v" },
    { from: "issue:b", to: "cq:t", dir: "v" },
    { from: "ar:b", to: "df:t", dir: "v", label: "R" },
    { from: "df:b", to: "wr:t", dir: "v" },
    { from: "cq:b", to: "wr:t", dir: "v" },
    { from: "wr:b", to: "axi:t", dir: "v", accent: true },
  ],
}

const wSm = {
  states: [
    { id: "W_IDLE", x: 0, y: 0, label: "IDLE" },
    { id: "W_ARM", x: 7, y: 0, label: "ARM", sub: "wait for data" },
    { id: "W_DATA", x: 14, y: 0, label: "DATA" },
    { id: "W_GEN", x: 7, y: 5, label: "GEN", sub: "two PRNG halves" },
  ],
  edges: [
    { from: "W_IDLE", to: "W_ARM", label: "not resident" },
    { from: "W_ARM", to: "W_DATA", label: "w_ready_rd" },
    { from: "W_IDLE", to: "W_DATA", label: "w_now", curve: -78 },
    { from: "W_IDLE", to: "W_GEN", label: "K_GEN" },
    { from: "W_GEN", to: "W_ARM", label: "pr_valid ×2" },
    { from: "W_DATA", to: "W_DATA", label: "a beat, or the next burst", self: true },
    { from: "W_DATA", to: "W_IDLE", label: "w_endbst", curve: 100 },
  ],
}

const modeCols = [
  { key: "m", label: "Mode", mono: true },
  { key: "d", label: "What the engine does" },
]
const modeRows = [
  { m: "COPY", d: "both walkers step in lockstep. A source stride of <b>zero is a broadcast</b>, with no extra mode" },
  { m: "TRANSPOSE", d: "allocated and <b>faults</b> (<code>F_MODE</code>). Not implemented", _tone: "bad" },
  { m: "GATHER", d: "the whole index vector is pulled into <code>u_ixbuf</code> first, then three pipeline stages per element — index out of the buffer, multiply, base add" },
  { m: "GENERATE", d: "a counter-based PRNG keyed on the destination's <b>absolute word address</b>, so one fill and four fills of its quarters produce identical bytes" },
  { m: "FILL", d: "an immediate, splatted at the configured element width" },
]

const moverTrapCols = [
  { key: "s", label: "Symptom" },
  { key: "c", label: "Cause, and the fix that is in the source" },
]
const moverTrapRows = [
  {
    s: "one word of a burst is X, and only sometimes",
    c: "<code>fcnt</code> counts a word <b>before the FIFO presents it</b> — <code>xpm_fifo_sync</code> in first-word-fall-through deasserts <code>empty</code> some cycles after the write, measured 2 to 6 here. A pop timed on the count alone samples X, silently, and only when the write engine happens to pounce inside that window. The residency test is <code>rd_ok = !f_empty</code> <i>and</i> the count",
    _tone: "bad",
  },
  {
    s: "a chained burst repeats the previous word",
    c: "the count has to be taken <b>net of the beat leaving this cycle</b>: <code>fcnt_av = fcnt − f_rd</code>. A burst starting one word short drained the FIFO past empty",
    _tone: "bad",
  },
  {
    s: "hangs at exactly the right amount of data",
    c: "<code>&gt;</code> where <code>&gt;=</code> was meant. The test is exactly <code>fcnt_av &gt;= n</code>; <code>&gt;</code> alone hangs at exact residency",
    _tone: "bad",
  },
  {
    s: "a run is held open across a full command FIFO and deadlocks",
    c: "only if the write engine is starved of data — so <code>w_starve</code> is <b>registered</b>, and <code>rflush</code> chops the open read run. Starvation persists until data arrives, so a flush one cycle late still breaks it",
  },
  {
    s: "a top bit of the index write address disappears",
    c: "<code>IDX_WORDS</code> at 128 dropped <code>ix_wr_a</code>'s top bit silently (Vivado Synth 8-689). 256 is the fix, and a 256-bit port is 4 RAMB36 at any depth to 512, so it costs nothing",
    _tone: "bad",
  },
  {
    s: "a part-select naming neither cause nor bound",
    c: "<code>ADDR_W</code> other than 40. The map is <b>absolute</b>, so a narrower build is a different map rather than the bottom corner of this one — the module now instantiates a nonexistent module named <code>mm_mover_ADDR_W_must_be_40_the_address_map_is_absolute</code> and fails at elaboration",
    _tone: "good",
  },
]

// -------------------------------------------------------- measured decisions
const timingCols = [
  { key: "d", label: "Decision in the RTL" },
  { key: "m", label: "What it was measured against", mono: false },
  { key: "w", label: "Where" },
]
const timingRows = [
  {
    d: "take the transform out of the port entirely",
    m: "the read FIFO's BRAM output into the transform's DSP control was <b>9 LUT levels, 4.399 ns</b>, and set the WNS on <b>every</b> SLR1 probe",
    w: "<code>mag_mem_port.v</code>",
  },
  {
    d: "accumulate <code>rd_anext</code> rather than computing <code>base + n × size</code>",
    m: "<b>86 MHz</b>. Against a register the product is a real full-width multiplier in the AR address path",
    w: "<code>mag_mem_port.v</code>",
  },
  {
    d: "keep the intake FIFOs in <code>distributed</code>",
    m: "<code>block</code> measured <b>−456 LUT but 330.0 → 305.3 MHz</b>, under the 320 floor — the worst path already starts at that FIFO's output, where a BRAM's clock-to-out is far slower",
    w: "<code>mag_mem_port.v</code>",
    _tone: "warn",
  },
  {
    d: "one dispatch register <b>per bank</b> in the staging store",
    m: "as a single shared register it was the worst data path: <b>4.860 ns at 98.4% route with ZERO logic levels</b>. It carries <code>DONT_TOUCH</code> or it is merged back",
    w: "<code>mag_stage.v</code>",
  },
  {
    d: "register the staging grant",
    m: "<code>q_ready</code> reading the rotating priority scan put <b>7 levels</b> in the mover's backpressure chain, <b>−0.498 ns</b> against −0.239",
    w: "<code>mag_stage_port.v</code>",
  },
  {
    d: "keep an active-claimant one-hot instead of decoding <code>id == g</code>",
    m: "the decode sat <b>4 levels ahead</b> of the DRAM port's arbiter, at <b>−0.354 ns</b>",
    w: "<code>mag_stage_port.v</code>",
  },
  {
    d: "capture the arbiter's choice instead of using it the same cycle",
    m: "scan, mux, arithmetic and ready on one path cost <b>49 MHz</b> in a 6+2 mesh",
    w: "<code>mag_dram_port.v</code>",
  },
  {
    d: "mask-then-isolate round robin instead of a serial scan",
    m: "the serial scan was <b>N dependent levels</b> under <code>q_ready</code>: <b>1,800 paths at 13–15 levels</b> into the mover",
    w: "<code>mag_dram_rr</code>",
  },
  {
    d: "track <code>rleft == 0</code> as its own bit",
    m: "<b>215 paths sat at 11 levels</b> through a 16-bit compare",
    w: "<code>mag_dram_port.v</code>",
  },
  {
    d: "two registers before the gather multiply",
    m: "BRAM output straight into a 32×32 multiply measured <b>188 MHz</b>",
    w: "<code>mm_mover.v</code>",
  },
  {
    d: "carry <code>ra_nxt</code>/<code>wa_nxt</code> and the room counts rather than computing them",
    m: "as expressions, two adders in series on the command FIFO's write enable — <b>14 levels, −0.155 ns at 3.333 ns</b>. Registering both is worth <b>40 MHz</b>",
    w: "<code>mm_mover.v</code>",
  },
]

// ---------------------------------------------------------------- divergence
const divCols = [
  { key: "d", label: "Divergence" },
  { key: "t", label: "Detail" },
]
const divRows = [
  {
    d: "<b><code>RD_OUT</code> is a 3.25× throughput knob that ships OFF.</b>",
    t: "<code>mag_dram_port.v</code> states it plainly: 4 outstanding reads per requester measures <b>2,744 → 8,917 MB/s</b> on 20-word bursts, and <b>it corrupts memory in <code>mover_chain1</code></b>. The default is 1, which keeps the pending arrays legal and never uses them. The performance is real and so is the corruption; nothing on this site attributes the first without the second.",
    _tone: "bad",
  },
  {
    d: "<b>The mover reaches into a project package.</b>",
    t: "<code>mm_mover.v</code> instantiates <code>mx_tdesc</code>, which lives with the matmul project. It is a general N-dimensional affine address generator with bound axes and nothing matmul-specific in it — the more reusable of the two, in the wrong place.",
  },
  {
    d: "<b><code>mag_1m.v</code>'s header describes a shim its body no longer has.</b>",
    t: "The per-master AXI-to-request conversion moved inside <code>mag.v</code>, which now instantiates <code>mag_stage_port</code> and then <code>mag_dram_port</code> on the converged path. <code>mag_1m</code> is a pass-through, and the <code>x_*</code> wires it still declares for that shim are unread.",
  },
  {
    d: "<b>Descriptor field positions exist only as literals.</b>",
    t: "<code>count</code>, <code>peer</code>, <code>n_peer</code> and <code>entry_words</code> have no macro anywhere; they are literal part-selects in <code>mag_mem_port.v</code> and in each compute unit. <code>noc_pkt.vh</code> is included by no module, so a divergence between two of them is silent — and one has already happened.",
  },
]
</script>

<template>
  <DocPage
    title="Memory agent — microarchitecture"
    summary="The RTL, drawn. Two instruction streams inside one module, the {entry, word} tag that makes out-of-order responses legal, the slot table and the fairness rule that lost a dirty line, the staging store that is deliberately not a cache, and the mover's own engine."
    domain="framework"
    status="shipped"
    source="src/kohakuaccel/sysnode/core/ · src/kohakuaccel/sysnode/mover/ · docs/arch/sysnode/ · docs/spec/memory-protocol.md"
  >
    <p class="doc-p">
      <RouterLink to="/framework/sysnode" class="doc-link">The memory agent page</RouterLink>
      is what the agent promises. This one is how it is built, and it starts from the
      thing that is easiest to miss: <b>the memory agent has an instruction space, so
      it is a machine that executes rather than a pipe that moves bytes.</b> One flit
      is one instruction. It names an address, an entry geometry, a run length, a
      transform and up to three extra destinations, and a single memory port is
      running <b>two</b> of those instruction streams at once.
    </p>

    <h2 class="doc-h2">One module, two machines</h2>

    <Fig
      caption="mag_mem_port, at signal level. The read path runs across the top: u_rdq, the rs engine, the AR/R channels, the skid and its beat register, the transform, the emit buffer. The write path runs across the bottom: u_wrq, the slot table, the ready-order queue, the st machine, the AW/W/B channels. They meet at exactly one place — mem_out_data, one register, where the emitter outranks the write-ack path. Everything here is per-port state; nothing is shared with another port except the address space on the far side of AXI."
      zoom
      wide
    >
      <BlockDiagram :nodes="port.nodes" :edges="port.edges" />
    </Fig>

    <SpecTable :cols="machineCols" :rows="machineRows" />

    <Fig caption="st — the write machine, plus the single-shot read that only benches use. A slot is released on ws_done at the ack, not at the issue, so a source cannot reuse it before its data is safe." zoom>
      <StateMachine :states="stSm.states" :edges="stSm.edges" />
    </Fig>

    <Callout kind="rule" title="The state encodings have gaps, and the gaps are load-bearing">
      <p>
        <code>S_IDLE</code> is 0, <code>S_RD_DATA</code> is 2, <code>S_WR_DATA</code> is
        5, <code>S_WR_ACK</code> is 6 — the numbers under each state above. They are not
        contiguous because <b>benches watch <code>st</code> by number</b>, so a state is
        retired by leaving a hole rather than by renumbering the ones after it.
      </p>
    </Callout>

    <Fig caption="rs — the read engine, with its own state and its own return context. RS_STG is the staged path: one entry per port-A read, no AR, no beats, no transform." zoom>
      <StateMachine :states="rsStates" :edges="rsEdges" />
    </Fig>

    <h3 class="doc-h3">take_rd_p versus take_rd_e</h3>
    <p class="doc-p">
      The two read forms are taken by <b>different terms with different conditions</b>,
      and the difference is the whole reason a streaming fetch and a write can overlap.
    </p>

    <SpecTable :cols="takeCols" :rows="takeRows" />

    <WaveTrace
      v-bind="plainRead"
      variant="broken"
      label="a plain read — the write path is excluded for the whole burst"
    />
    <WaveTrace
      v-bind="entryRead"
      variant="fixed"
      label="an entry read — writes keep issuing underneath it"
    />

    <h2 class="doc-h2">The descriptor engine, one entry at a time</h2>
    <p class="doc-p">
      Scrub through a streaming fetch of two entries. The two things to watch
      are <b>when the next address goes out</b> and <b>when the emit buffer is handed
      over</b> — both happen a cycle earlier than the obvious ordering would put them,
      and that is the entire throughput argument for the engine.
    </p>

    <StepPlayer :steps="walk" label="STREAM = 1, count = 2">
      <template #default="{ state }">
        <div class="flex flex-wrap gap-1.5 mb-4">
          <span v-for="ch in state.chips" :key="ch.k" class="chip">
            <span class="opacity-60 mr-1">{{ ch.k }}</span>{{ ch.v }}
          </span>
        </div>
        <StateMachine :states="rsStates" :edges="rsEdges" :active="state.active" />
      </template>
    </StepPlayer>

    <Callout kind="measured" title="Why the transform left this port">
      <p>
        The port used to hold one, and the register in front of it existed for a single
        measurement: the read FIFO's block-RAM output reached the transform's DSP control
        through <b>9 LUT levels, 4.399 ns</b>, and set the WNS on <b>every</b> SLR1 probe.
        Moving the transform out removes that path from the port entirely. The shared slot
        it moved to registers the beat before the bank for exactly the same reason.
      </p>
      <p class="kt-text-caption">
        Measured on the SLR1 probe vehicle, <code>xcvu13p-fhgb2104-2L-e</code>.
      </p>
    </Callout>

    <Callout kind="trap" title="q_done is a pulse, so q_rdy has to remember it">
      <p>
        The transform raises <code>done</code> for one cycle. If the emit buffer is
        still handing out the previous entry when it fires, the engine would wait
        forever for an edge that <b>already happened</b> — so <code>q_rdy</code> latches
        it and <code>RS_WAIT</code> tests <code>q_rdy &amp;&amp; !e_act</code>.
      </p>
    </Callout>

    <h2 class="doc-h2">Every response names its own slot</h2>
    <p class="doc-p">
      This is the mechanism the whole read path exists to deliver, and it is three
      header fields.
    </p>

    <BitField
      :fields="respHdr"
      caption="MEM_RD_RESP as mag_mem_port writes it. txn is the requester's own tag plus this entry's index in the run; rsvd[1:0] is the word index inside the entry; last is set on the final word OF EACH ENTRY, not only of the run. Nothing about placement is in the payload — the payload is the data"
    />

    <p class="doc-p">
      Because the destination slot is in the header, <b>arrival order stops being load
      bearing</b> and the receiver needs no per-entry state. That is what makes a
      streaming fetch possible at all: one request, hundreds of cycles of traffic, and a
      receiver that can bin every flit it gets without tracking where it is.
    </p>

    <WaveTrace v-bind="outOfOrder" label="what the tag makes legal" />

    <Callout kind="rule" title="The tag arithmetic is 8-bit and wraps silently">
      <p>
        <code>e_tag &lt;= rd_txn + rd_ent</code>. A requester <b>MUST</b> size its own
        tag space so that <code>txn + count − 1</code> does not exceed 255; a run that
        wraps aliases two entries onto one slot, and nothing reports it.
      </p>
    </Callout>

    <h3 class="doc-h3">Extra destinations cost one register, not one fetch</h3>
    <p class="doc-p">
      <code>e_dst</code> walks 0 to <code>rd_nd</code>. Destination 0 is the requester;
      the rest are bytes of <code>rd_peer</code>, unpacked as <code>{y, x}</code> with
      <code>y</code> in the high nibble. When the last word of an entry has gone out and
      a peer remains, <code>q_emit</code> returns to 0 and the <b>same latched
      <code>e_w0..3</code></b> are re-sent with a different header. No second AXI read,
      no second pass of the transform.
    </p>

    <h3 class="doc-h3">What the RTL actually slices</h3>
    <SpecTable
      :cols="sliceCols"
      :rows="sliceRows"
      caption="Descriptor fields as literal part-selects. noc_pkt.vh has no macro for count, peer, n_peer or entry_words, and is included by no module, so these positions exist here and in each compute unit independently"
    />

    <h2 class="doc-h2">Intake: accept exactly when the sender believes you did</h2>
    <p class="doc-p">
      <code>mem_in_busy</code> and the accept term have to be <b>the same
      predicate</b>. Deriving one from queue occupancy and the other from “is there
      room” is a one-line difference with two permanent, silent failure modes.
    </p>

    <WaveTrace v-bind="takeBroken" variant="broken" label="enqueue on “is there room”" />
    <WaveTrace v-bind="takeFixed" variant="fixed" label="mi_take = mem_in_valid &amp;&amp; !mem_in_busy" />

    <Callout kind="note" title="The drop is named at the moment it happens">
      <p>
        The port carries a simulation <code>$display</code> on
        <code>(mi_rd &amp;&amp; rq_full) || (mi_wr &amp;&amp; wq_full)</code> — an input
        flit dropped because backpressure was too late. Without it the first symptom is
        <i>“write data with no open write”</i>, hundreds of cycles later and several
        modules away.
      </p>
    </Callout>

    <h2 class="doc-h2">The write path: allocation and issue are different rules</h2>

    <SpecTable
      :cols="slotCols"
      :rows="slotRows"
      caption="The slot table. Every field is per slot except the data array, which is WR_SLOTS × WBURST beats — and WBURST is a fixed constant of 8, not a parameter"
    />

    <SpecTable
      :cols="scanCols"
      :rows="scanRows"
      caption="Three separate priority scans over the slot array rather than one scan with conditions, so none of them lands in the other's path"
    />

    <Callout kind="trap" title="Lowest-free allocation and lowest-ready issue do not compose">
      <p>
        Neither rule is wrong on its own. Together they are: <b>a requester that outruns
        AXI is handed a high slot that is then never the lowest ready again.</b> The
        freed low slots recycle faster than the engine drains, and with reads winning
        <code>S_IDLE</code> the refill goes ready before the next pick.
      </p>
      <p>
        It has two faces, and both are silent. One starves a slot; the other lands one
        source's same-address writes out of program order.
      </p>
    </Callout>

    <WaveTrace v-bind="starveBroken" variant="broken" label="broken — starvation" />
    <WaveTrace v-bind="reorderBroken" variant="broken" label="broken — reorder" />
    <WaveTrace v-bind="starveFixed" variant="fixed" label="fixed — u_rdyq, a FIFO of slot indices" />

    <Callout kind="rule" title="The requester's half of the same fix is WR_MAX">
      <p>
        Bounding what a requester leaves outstanding keeps the agent's in-use set in the
        region where the two rules <i>do</i> compose. That is why the reference PE ships
        with <code>WR_MAX = 1</code> — one un-acknowledged write — and the comment beside
        it names <code>mag_mem_port.v</code> as the reason. The requester side of this
        story is on
        <RouterLink to="/mpe/cpu/microarchitecture" class="doc-link">the CPU
        microarchitecture page</RouterLink>.
      </p>
    </Callout>

    <Callout kind="trap" title="A picked slot must stop being pickable immediately">
      <p>
        Not at its write ack. Releasing at the ack leaves the slot ready for the cycle
        the machine spends re-entering <code>S_IDLE</code>, so <b>the same write is
        issued twice</b> and the next write's turn never comes.
      </p>
      <p>
        <code>ws_issue</code> clears <code>ws_rdy</code> and sets <code>ws_iss</code> on
        the spot; <code>ws_done</code> at the ack clears <code>ws_val</code> and
        <code>ws_iss</code> together, so the source cannot reuse the slot before its data
        is safe.
      </p>
    </Callout>

    <Callout kind="trap" title="A missed B never comes again">
      <p>
        <code>m_bready</code> is tied high, so the slave's write response is consumed the
        cycle it appears — whether or not <code>S_WR_ACK</code> can act on it. Often it
        cannot, because the read emitter owns the output register. <code>wr_b</code> is a
        one-bit latch that catches the response the cycle it arrives; the state that
        clears it runs later in the same always block, so an arrive-and-consume in one
        cycle ends correctly at zero.
      </p>
    </Callout>

    <h2 class="doc-h2">There is no partial-line DRAM write</h2>
    <p class="doc-p">
      <code>MEM_WR_DATA</code>'s payload is 256 bits of data and <b>nothing else</b> —
      no fields, no strobe — and the port drives <code>m_wstrb</code> to all ones
      unconditionally. Both halves of that are deliberate, and together they set a rule
      a compute unit has to design around.
    </p>

    <SpecTable
      :cols="strobeCols"
      :rows="strobeRows"
      caption="Byte enables exist on three of the agent's four write paths. The one they do not exist on is the one every compute unit uses"
    />

    <Callout kind="rule" title="If your line is dirty in part, you merge it">
      <p>
        The agent will not. A store narrower than <code>DATA_W</code> is a
        <b>read-modify-write in the requester</b>, and the fetch, the merge and the
        write-back are all the unit's own instructions.
      </p>
      <p>
        This costs the reference PE nothing, because its L1 is write-back with a
        <b>256-bit line</b> — exactly one beat — so every writeback is a whole line and
        the missing strobe is never reached for. A unit whose natural store is narrower
        pays for the difference itself.
      </p>
    </Callout>

    <h2 class="doc-h2">The transform slot, as the port drives it</h2>
    <p class="doc-p">
      The slot is fixed protocol and the occupant is an addon; the framework side of
      that split is on
      <RouterLink to="/framework/sysnode" class="doc-link">the memory agent page</RouterLink>.
      What is worth reading in the RTL is how little of the geometry is written down as
      a number.
    </p>

    <SpecTable
      :cols="xfCols"
      :rows="xfRows"
      caption="One entry is 4 lanes × 32 elements whatever the bus is wide. The burst lengths are DERIVED, not written as 7 and 3 — hardcoding them would make a wider bus a silent correctness change rather than a parameter"
    />

    <Callout kind="rule" title="The three hard rules a replacement must hold">
      <p>
        <b>1. Fixed output shape.</b> A transformed fetch yields exactly four operand
        words per entry, whatever the source length. Only a NON-transforming fetch may
        choose its own words-per-entry.
      </p>
      <p>
        <b>2. The whole entry may be needed before anything can be emitted.</b> The port
        is built for that: <code>done</code> may come any number of cycles after the last
        beat.
      </p>
      <p>
        <b>3. Input is push-only.</b> The port drives <code>beat_valid</code> from its
        own read state machine; a transform that needs backpressure must buffer
        internally. <code>need_beat</code> is reserved and <b>the port ignores it
        today</b>, so a compliant module ties it high or drives it truthfully.
      </p>
    </Callout>

    <Callout kind="note" title="A beat IS an operand word, and it still goes through the buffer">
      <p>
        Nothing on this path converts, so the beats <i>are</i> the operand words, and they
        land in <code>p_w0..3</code> rather than going to the emit buffer directly —
        because the emitter may still be handing out the previous entry. The same
        double-buffer serves both paths, which is why the port's control does not change
        when the transform does.
      </p>
    </Callout>

    <h2 class="doc-h2">Addon slot 2: staging is an address range, not a cache</h2>

    <BitField
      :fields="apAddr"
      caption="The address a staged access carries. With bit 39 clear this is an ordinary DRAM address and the low 36 bits are one flat local space; with it set, the four bits above the offset name which aperture. mag_stage, mag_stage_port and mag_mem_port all decode it independently and identically"
    />

    <Callout kind="rule" title="Mesh first, then aperture">
      <p>
        The order is the whole reason a packet <i>transiting</i> this mesh is safe. It
        fails the mesh test, so it is <b>neither ours nor a fault</b> and passes on
        untouched — which is what lets mesh 0 reach mesh 3's store.
      </p>
      <p>
        Get the order the other way round and a foreign address either faults or, worse,
        is claimed.
      </p>
    </Callout>

    <Fig
      caption="mag_stage. Banks are interleaved on the LOW address bits, so a sequential fill spreads across them instead of loading one. The wide path has ONE driver and only the narrow address and control fan out per bank. The return is a fixed-latency shift register that cannot stall: the caller leads and takes rvalid when it comes."
      zoom
    >
      <BlockDiagram :nodes="stage.nodes" :edges="stage.edges" />
    </Fig>

    <Callout kind="measured" title="One dispatch register per bank, and it carries DONT_TOUCH">
      <p>
        As a single shared register the dispatch was <b>the worst data path in the
        design: 4.860 ns at 98.4% route, with ZERO logic levels.</b> It is pure wire —
        one register reaching URAM columns spread across the die. Replicating it per bank
        fixes that, and the replicas are only stable because the attribute stops the tool
        merging them back.
      </p>
      <p>
        The <code>PIPE</code> registers are <b>generated, not muxed</b>: a ternary on a
        parameter still infers them, and 1,024 dead flip-flops is not a nothing.
      </p>
      <p class="kt-text-caption">
        <code>xcvu13p-fhgb2104-2L-e</code>. The shipped store is 4 banks × 16,384 entries
        = 64 URAM, 2 MB per agent.
      </p>
    </Callout>

    <SpecTable
      :cols="stgCols"
      :rows="stgRows"
      caption="Everything staging does not have to decide, because it is a reserved range in an address map the compiler already computed"
    />

    <Callout kind="rule" title="Where explicit staging stops">
      <p>
        A GEMM sweep walks <code>for kb: for g: for h</code> over addresses the compiler
        already computed, so <b>a cache would spend tags and comparators rediscovering
        what was written down.</b> That argument is exactly as strong as its premise.
      </p>
      <p>
        It stops the moment an address is <i>not</i> knowable ahead of time. Nothing here
        discovers, evicts, or writes back: a fetch that misses the aperture simply goes
        to DRAM, and a result left in the aperture stays there. Staging converts nothing
        either: it holds operand words verbatim, which is also why a staged read never
        needed a transform in front of it.
      </p>
    </Callout>

    <h3 class="doc-h3">Where the store sits changes who can use it</h3>
    <SpecTable :cols="placeCols" :rows="placeRows" />

    <Callout kind="note" title="The module default and the shipped setting differ">
      <p>
        <code>mag.v</code> defaults <code>STAGE_AT_PORT</code> to <b>0</b>, the per-port
        form. Every generated ship top that enables staging passes
        <code>STAGE(1)</code> and <code>STAGE_AT_PORT(1)</code> — the converged-path form
        — so the default is the older shape kept for comparison, not the shipping one.
        Selection is <code>gen_mesh.py --l2-mag</code>, independent of the mesh-side
        adapter. <b>No software targets either yet.</b>
      </p>
    </Callout>

    <h2 class="doc-h2">One AXI master, and the contract in front of it</h2>
    <p class="doc-p">
      Every requester inside the agent — the memory ports, the host window, the mover,
      and the interlink's inbound writes — speaks one internal protocol:
      <code>q_valid / q_ready / q_addr / q_len / q_write</code>, plus the
      <code>w_*</code> and <code>r_*</code> streams. <code>mag_stage_port</code> claims
      staged traffic off that converged path, and <code>mag_dram_port</code> is the
      single converter behind it: two round-robin arbiters, a width pack from
      <code>DATA_W</code> to the memory's beat, and five asynchronous FIFOs that carry
      the whole thing into the DRAM clock domain. <b>AXI is heavy, so it appears once.</b>
    </p>

    <Callout kind="rule" title="A requester's presentation HOLDS until q_ready">
      <p>
        <code>{valid, write, addr, len}</code> may not change while a request waits.
        Both arbiters decide on a <b>registered</b> request vector and sample the bus
        <b>live</b>, and the grant is a single wire.
      </p>
    </Callout>

    <WaveTrace v-bind="qBroken" variant="broken" label="a presentation that switches mid-wait" />
    <WaveTrace v-bind="qFixed" variant="fixed" label="the choice latched at first offer" />

    <Callout kind="note" title="The head and tail of a packed burst are over-fetched, then discarded">
      <p>
        A burst's start sub-beat is its <code>phase</code>, and the memory address is
        that address aligned down to a memory beat. Each memory beat becomes up to
        <code>MW/SW</code> internal ones and the extra head and tail are <b>discarded
        rather than avoided</b> — the alternative is a narrower burst and a special case
        at both ends.
      </p>
      <p>
        On the write side the strobes start <b>cleared</b> and only written lanes set
        them, so a partial head and a partial tail fall out with no special case at all.
      </p>
    </Callout>

    <h2 class="doc-h2">The mover is a second machine, not a third read path</h2>
    <p class="doc-p">
      It reads memory and writes memory and <b>never talks to a compute unit</b>. It has
      its own AXI master, no fabric endpoint, and its command path is a slice of the
      control window rather than a set of boundary ports — a design rule with a scar
      behind it: <i>loose sideband ports never get wired up in a block design, and a
      shipped engine that nothing could command is worse than no engine.</i>
    </p>

    <Fig
      caption="mm_mover. Two mx_tdesc walkers stepped in lockstep, with the DESTINATION defining the iteration space — which is what makes a source stride of zero a broadcast with no extra mode. src_valid low injects the immediate, which is how padding works; dst_valid low suppresses the write. The element latch classifies each destination element as K_RD, K_FILL, K_GEN or K_SKIP, and the issue engine sends a read for the first and a command for the first three; K_SKIP is a suppressed write that breaks both runs. A burst's FIFO space is reserved before its AR goes out."
      zoom
    >
      <BlockDiagram :nodes="mover.nodes" :edges="mover.edges" />
    </Fig>

    <SpecTable :cols="modeCols" :rows="modeRows" />

    <Fig caption="The write engine. W_ARM exists only for the case where the data has not arrived: when it has, w_now goes straight to W_DATA, because stopping in W_ARM would cost a third cycle on every single-beat write. A command also loads straight off the last beat of the burst before it, so back-to-back bursts cost n+1 cycles rather than n+2." zoom>
      <StateMachine :states="wSm.states" :edges="wSm.edges" />
    </Fig>

    <Callout kind="rule" title="The whole burst is resident before AW">
      <p>
        A granted write then streams at one beat per cycle and <b>never parks the DRAM
        port's write mux</b> — which matters because AXI4 forbids W interleaving, so W
        follows AW order and a stalled writer blocks everyone behind it.
      </p>
      <p>
        The read side reserves symmetrically: FIFO space for a whole burst is taken
        before its AR is issued, so the return can never be refused and never backs up
        into the shared path.
      </p>
    </Callout>

    <SpecTable
      :cols="moverTrapCols"
      :rows="moverTrapRows"
      caption="Every row is a failure that happened, and every one of them is a residency or a width question rather than an algorithm question"
    />

    <Callout kind="open" title="This section is the compressed version of a page">
      <p>
        The mover is not a variant of the read engine. It has its own instruction set —
        five modes, six-dimensional descriptors with bound axes, a PRNG whose output is a
        pure function of the destination address — its own two-engine pipeline, its own
        burst-coalescing rules, and its own fault register. It is the only engine here
        that is <b>commanded rather than requested</b>.
      </p>
      <p>
        It should have its own page. What is above is the shape and the traps; the mode
        semantics, the descriptor form and the register map deserve the same treatment
        the read engine gets here.
      </p>
    </Callout>

    <h2 class="doc-h2">Where the timing actually went</h2>
    <p class="doc-p">
      Almost none of the structure above is there for elegance. These are the decisions
      that have a measurement attached, in the order the data flows.
    </p>

    <SpecTable
      :cols="timingCols"
      :rows="timingRows"
      caption="Every figure is out-of-context synthesis or a placed run on xcvu13p-fhgb2104-2L-e, at the clock the source states beside it. Where a source names a vehicle — the SLR1 probe, a 6+2 mesh, ktpu_ship_2x2_6c2v_il_pump, mm_mesh — that vehicle is what was measured, and the number does not transfer to another one"
    />

    <h2 class="doc-h2">Where today's source disagrees</h2>
    <SpecTable :cols="divCols" :rows="divRows" />

    <Callout kind="note" title="The other divergences are on the parent page">
      <p>
        The 34-bit-versus-40-bit address field, the control agent's packaging, the
        interlink living inside the memory agent and the moved source directory are all
        recorded on
        <RouterLink to="/framework/sysnode" class="doc-link">the memory agent page</RouterLink>,
        together with the protocol contracts they belong to.
      </p>
    </Callout>
  </DocPage>
</template>
