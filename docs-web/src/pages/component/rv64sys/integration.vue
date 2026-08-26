<script setup>
// ===========================================================================
// RV64 system core — integrating one.
// Presents docs/arch/cpu/rv64-sys/integration.md.
//
// The node-wiring section is read from src/kohakuaccel/sysnode/sysnode.v,
// where CPU_RV64 defaults to 0 and the g_rv64 branch connects the hub's
// processor client, the interlink doorbell, irq_summary and pe_status. The
// mailbox register map is src/kohakuaccel/pe/rv64-sys/rv64_noc_mbox.v and
// the control-region decode in rv64_syscore.v.
// ===========================================================================

/* --- the dispatch mailbox ------------------------------------------------- */

const mboxRegs = {
  cols: [
    { key: "o", label: "Offset", mono: true, align: "center" },
    { key: "n", label: "Register", mono: true },
    { key: "w", label: "Write" },
    { key: "r", label: "Read" },
  ],
  rows: [
    {
      o: "0x40",
      n: "DST",
      w: "the destination: <code>x</code> in bits 3:0, <code>y</code> in bits 11:8",
      r: "the same, in the same places",
    },
    { o: "0x48", n: "ARG0", w: "payload word 0", r: "it back" },
    { o: "0x50", n: "ARG1", w: "payload word 1", r: "it back" },
    {
      o: "0x58",
      n: "<b>GO</b>",
      w: "<b>any value.</b> Builds the <code>CU_INST</code> flit from DST, ARG0 and ARG1 and offers it. Ignored while one is still unsent",
      r: "0",
      _tone: "good",
    },
    {
      o: "0x60",
      n: "STAT",
      w: "—",
      r: "<code>{ovf@31, tx_pending@15, used@4:0}</code>",
    },
    {
      o: "0x68",
      n: "HEAD",
      w: "—",
      r: "the completion at the head of the queue, or <b>0 when the queue is empty</b>",
    },
    {
      o: "0x70",
      n: "<b>POP</b>",
      w: "<b>any value.</b> Advances the read pointer by one, if the queue is non-empty",
      r: "0",
      _tone: "good",
    },
  ],
};

const headBits = [
  { name: "rsvd", bits: 8, value: "63:56 — reads 0" },
  { name: "src_y", bits: 4, value: "55:52", accent: true },
  { name: "src_x", bits: 4, value: "51:48", accent: true },
  { name: "code", bits: 8, value: "47:40", accent: true },
  { name: "arg", bits: 32, value: "39:8", accent: true },
  { name: "rsvd", bits: 8, value: "7:0 — reads 0" },
];

const statBits = [
  { name: "rsvd", bits: 32, value: "63:32" },
  { name: "ovf", bits: 1, value: "31 — sticky", accent: true },
  { name: "rsvd", bits: 15, value: "30:16" },
  { name: "tx", bits: 1, value: "15 — a flit is still unsent", accent: true },
  { name: "rsvd", bits: 10, value: "14:5" },
  { name: "used", bits: 5, value: "4:0 — queued completions", accent: true },
];

const mboxSpec = {
  cols: [
    { key: "f", label: "Field", mono: true },
    { key: "w", label: "Width", align: "right", mono: true },
    { key: "p", label: "Position", mono: true },
    { key: "o", label: "Owner" },
  ],
  rows: [
    {
      f: "dst_x, dst_y",
      w: "POS_WIDTH each",
      p: "DST [3:0], [11:8]",
      o: "<b>software.</b> The coordinate of the compute unit being dispatched to. Held until overwritten, so a run of dispatches to one unit writes it once",
    },
    {
      f: "arg0, arg1",
      w: "64 each",
      p: "ARG0, ARG1",
      o: "<b>software, and the receiving unit.</b> They become the low 128 bits of the flit's payload verbatim; the mailbox never interprets them",
    },
    {
      f: "txn",
      w: "8",
      p: "the flit header",
      o: "<b>hardware.</b> Incremented on every accepted GO. Software cannot read or write it — the completion carries it back only if the unit puts it in <code>arg</code>",
      _tone: "warn",
    },
    {
      f: "src_x, src_y",
      w: "POS_WIDTH each",
      p: "the flit header",
      o: "<b>hardware</b>, from the complex's own coordinate, which is <code>(0,0)</code>. A unit answers there",
    },
    {
      f: "last",
      w: "1",
      p: "the flit header",
      o: "hardware, always 1 — one instruction is one flit",
    },
    {
      f: "src_y, src_x",
      w: "4 each",
      p: "HEAD [55:52], [51:48]",
      o: "<b>hardware</b>, copied from the completion flit's source. This is how software tells which unit finished",
    },
    {
      f: "code",
      w: "8",
      p: "HEAD [47:40]",
      o: "<b>the framework below 0x40, the unit above it.</b> 0x00 instruction complete, 0x01 batch complete, 0x04 fault",
    },
    {
      f: "arg",
      w: "32",
      p: "HEAD [39:8]",
      o: "<b>the unit.</b> Unit-defined content whatever the code",
    },
    {
      f: "ovf",
      w: "1",
      p: "STAT [31]",
      o: "<b>hardware, and sticky.</b> Set when a completion arrives at a full queue and dropped. It is never cleared — <b>a dropped completion and a unit that never finished look identical from software</b>, and this bit is the only thing that tells them apart",
      _tone: "warn",
    },
    {
      f: "used",
      w: "5",
      p: "STAT [4:0]",
      o: "hardware. Queued completions, 0 to 16",
    },
    {
      f: "tx",
      w: "1",
      p: "STAT [15]",
      o: "hardware. A dispatch flit is built and has not yet been taken by the link. <b>A GO written while this is set is ignored</b>",
      _tone: "warn",
    },
  ],
};

const mboxRules = {
  cols: [
    { key: "r", label: "The rule" },
    { key: "w", label: "Why it is that way" },
  ],
  rows: [
    {
      r: "<b>Software writes a dispatch, not a flit.</b>",
      w: "A flit is 288 bits against a 64-bit store port. Composing one in software is five stores with a tearing window in the middle, and nothing downstream could tell a torn flit from a valid one",
      _tone: "good",
    },
    {
      r: "<b>Popping is a write, never a side effect of the read.</b>",
      w: "The control region answers reads from a register a cycle later, so a read-triggered pop would have to guess which cycle the read really happened on. Read HEAD, act on it, then write POP",
      _tone: "good",
    },
    {
      r: "<b>A completion the queue cannot take is accepted and dropped, never held.</b>",
      w: "Held, it sits at the head of the hub's queue and stalls the link for everything behind it — <i>including the traffic that would drain us</i>. The mailbox's busy line is tied low for exactly this reason, and the sticky overflow bit is what replaces backpressure",
      _tone: "good",
    },
    {
      r: "<b>A dispatch flit is held until the link takes it.</b>",
      w: "Withdrawing an offered flit destroys it, and the loss is silent at every point downstream. GO is ignored rather than queued while one is outstanding, so a dispatcher polls STAT bit 15 or paces itself",
    },
    {
      r: "<b>A non-empty queue raises the external interrupt.</b>",
      w: "Beside the node's own summary line. A completion waiting is exactly the condition a scheduler must not have to poll for",
      _tone: "good",
    },
  ],
};

const tops = {
  cols: [
    { key: "t", label: "Top", mono: true },
    { key: "w", label: "What it is" },
    { key: "a", label: "Attaches to" },
  ],
  rows: [
    {
      t: "rv64_sys_pe",
      w: "the core as a <b>compute unit</b> the framework recognises",
      a: "one fabric port",
    },
    {
      t: "rv64_syscore",
      w: "the core as the <b>system node's processor</b>",
      a: "an AXI slave window from the host, and one AXI master onto MAG",
    },
    {
      t: "rv64_mag_pe",
      w: "<code>rv64_syscore</code> <b>plus the node's mover and transform slot</b>",
      a: "the same, plus the mover's own master",
    },
  ],
};

const deadlock = {
  nodes: [
    {
      id: "blk",
      x: 0,
      y: 0,
      w: 17,
      h: 4.6,
      label: "the processor blocks",
      sub: "sending a dispatch",
      accent: true,
    },
    {
      id: "drain",
      x: 23,
      y: 0,
      w: 17,
      h: 4.6,
      label: "it stops draining",
      sub: "its own receive queue",
    },
    {
      id: "full",
      x: 46,
      y: 0,
      w: 17,
      h: 4.6,
      label: "the queue fills",
      sub: "finite, and behind the shell",
    },
    {
      id: "busy",
      x: 46,
      y: 9,
      w: 17,
      h: 4.6,
      label: "noc_in_busy asserts",
      sub: "the shell refuses inbound flits",
    },
    {
      id: "land",
      x: 23,
      y: 9,
      w: 17,
      h: 4.6,
      label: "the completions cannot land",
      sub: "and they are what it needs to make progress",
      accent: true,
    },
  ],
  edges: [
    { from: "blk:r", to: "drain:l" },
    { from: "drain:r", to: "full:l" },
    { from: "full:b", to: "busy:t" },
    { from: "busy:l", to: "land:r" },
    { from: "land:l", to: "blk:b", accent: true },
  ],
};

const replaced = {
  cols: [
    { key: "s", label: "The shell provided" },
    { key: "a", label: "The node processor's answer" },
  ],
  rows: [
    {
      s: "the fabric port and the instruction queue",
      a: "a <b>mailbox</b> in the control region — seven registers, a 16-deep completion queue, and no flow control on the inbound side. It is a client of the node's hub rather than an endpoint of its own",
      _tone: "good",
    },
    {
      s: "the image loader",
      a: "the host writes the memories through an AXI slave window",
    },
    { s: "the kick", a: "a boot register in that window" },
    {
      s: "the completion",
      a: "a status register the host polls, plus the exit word",
    },
    {
      s: "<b>“every write is visible when the completion arrives”</b>",
      a: "<b>not discharged for the cached range.</b> That last row is a real obligation taken on, not a free removal",
      _tone: "bad",
    },
  ],
};

/* --- rv64_sys_pe ---------------------------------------------------------- */

const peWrap = {
  nodes: [
    {
      id: "fab",
      x: 0,
      y: 5,
      w: 13,
      h: 6,
      label: "the fabric",
      sub: "CU_DATA · CU_INST · CU_SIGNAL",
    },
    {
      id: "base",
      x: 18,
      y: 5,
      w: 14,
      h: 6,
      label: "noc_cu_base",
      sub: "the port, the two queues, the CU_CTRL registers",
      accent: true,
    },
    {
      id: "kick",
      x: 37,
      y: 0,
      w: 14,
      h: 4.6,
      label: "the kick machine",
      sub: "waits for receive-quiet",
    },
    {
      id: "load",
      x: 37,
      y: 6,
      w: 14,
      h: 4.6,
      label: "the loader",
      sub: "buf_id chooses what the payload means",
    },
    {
      id: "core",
      x: 56,
      y: 0,
      w: 14,
      h: 4.6,
      label: "rv64_core",
      sub: "held in reset until the boot pulse",
      accent: true,
    },
    {
      id: "mem",
      x: 56,
      y: 6,
      w: 14,
      h: 4.6,
      label: "instruction window · scratchpad",
      sub: "16 KB each by default",
    },
  ],
  edges: [
    { from: "fab:r", to: "base:l" },
    { from: "base:r", to: "kick:l", label: "CU_INST" },
    { from: "base:r", to: "load:l", label: "CU_DATA" },
    { from: "kick:r", to: "core:l", label: "boot" },
    { from: "load:r", to: "mem:l" },
    { from: "core:b", to: "base:b", label: "CU_SIGNAL", accent: true },
  ],
};

const bufIds = {
  cols: [
    { key: "b", label: "buf_id", align: "center", mono: true },
    { key: "p", label: "Payload" },
    { key: "w", label: "One flit writes" },
  ],
  rows: [
    { b: "0", p: "scratchpad <b>granule</b>", w: "4 × 64-bit words" },
    { b: "1", p: "instruction window <b>granule</b>", w: "8 × 32-bit words" },
    { b: "4", p: "scratchpad <b>word</b>", w: "one 64-bit word" },
    { b: "5", p: "instruction window <b>word</b>", w: "one 32-bit word" },
    { b: "3", p: "reserved", w: "rejected", _tone: "bad" },
    { b: "anything else", p: "—", w: "rejected", _tone: "bad" },
  ],
};

const kickFsm = {
  states: [
    { id: "IDLE", x: 0, y: 0, label: "K_IDLE" },
    { id: "START", x: 14, y: 0, label: "K_START" },
    { id: "RUN", x: 28, y: 0, label: "K_RUN" },
    { id: "DONE", x: 42, y: 0, label: "K_DONE" },
  ],
  edges: [
    { from: "IDLE", to: "START", label: "receive-quiet" },
    { from: "START", to: "RUN", label: "opcode 1" },
    { from: "START", to: "DONE", label: "any other opcode", curve: 80 },
    { from: "RUN", to: "DONE", label: "halt" },
    { from: "DONE", to: "IDLE", label: "CU_SIGNAL", curve: 125 },
  ],
};

const peCtrl = {
  cols: [
    { key: "o", label: "Offset", mono: true, align: "center" },
    { key: "w", label: "Write" },
    { key: "r", label: "Read" },
  ],
  rows: [
    {
      o: "0x00",
      w: "<b>exit</b> — end the run; the low 32 bits become the completion's argument",
      r: "the doorbell bit",
    },
    { o: "0x08", w: "one console byte, observation only", r: "the doorbell bit" },
    {
      o: "0x10",
      w: "the doorbell; bit 0 reaches the core's software interrupt line",
      r: "the doorbell bit",
    },
  ],
};

const peAbsent = {
  cols: [
    { key: "n", label: "Not present" },
    { key: "w", label: "Consequence" },
  ],
  rows: [
    {
      n: "a fabric memory requestor",
      w: "no fill, no writeback, no push, no dispatch. <b>A load outside the scratchpad goes nowhere</b> — it aliases onto the scratchpad",
      _tone: "bad",
    },
    {
      n: "an MMU and an L1",
      w: "neither is instantiated; the configuration pays for neither",
    },
    {
      n: "a <code>CU_CTRL</code> emitter and an inbound control class",
      w: "a dropped <code>CU_CTRL</code> reply is the current behaviour, and it is silent",
      _tone: "bad",
    },
    {
      n: "anything remote that can ring the doorbell",
      w: "the register exists and reaches the interrupt line; no flit reaches the register",
      _tone: "bad",
    },
  ],
};

/* --- rv64_syscore --------------------------------------------------------- */

const scWrap = {
  nodes: [
    {
      id: "host",
      x: 0,
      y: 4,
      w: 13,
      h: 6,
      label: "host AXI",
      sub: "the slave window · always ready",
    },
    {
      id: "sel",
      x: 18,
      y: 4,
      w: 13,
      h: 6,
      label: "hs_addr[31:28]",
      sub: "0 imem · 1 scratchpad · 2 the control registers",
      accent: true,
    },
    {
      id: "imem",
      x: 36,
      y: 0,
      w: 14,
      h: 4.2,
      label: "instruction window",
      sub: "write only, 32-bit words",
    },
    {
      id: "spad",
      x: 36,
      y: 5.2,
      w: 14,
      h: 4.2,
      label: "scratchpad",
      sub: "write only, 64-bit with byte strobes",
    },
    {
      id: "regs",
      x: 36,
      y: 10.4,
      w: 14,
      h: 4.2,
      label: "control registers",
      sub: "read and write",
      accent: true,
    },
  ],
  edges: [
    { from: "host:r", to: "sel:l" },
    { from: "sel:r", to: "imem:l" },
    { from: "sel:r", to: "spad:l" },
    { from: "sel:r", to: "regs:l" },
  ],
};

/* --- the kick / loader race ----------------------------------------------- */

const kickRaceBroken = {
  rows: [
    {
      name: "recv queue (CU_DATA)",
      kind: "bus",
      values: ["img 6,7", "img 7", "—", "—"],
    },
    {
      name: "inst queue (CU_INST)",
      kind: "bus",
      values: ["kick", "kick", "—", "—"],
    },
    { name: "kick accepted", kind: "bit", values: [1, 0, 0, 0], mark: [0] },
    { name: "core in reset", kind: "bit", values: [1, 0, 0, 0] },
    {
      name: "image words written",
      kind: "text",
      values: ["", "6 of 8", "7 of 8", "8 of 8"],
    },
  ],
  notes: [
    {
      cycle: 0,
      text: "CU_INST and CU_DATA arrive on TWO queues. The kick is at the head of its own queue with nothing in front of it, so a machine that only looks at the instruction queue accepts it here.",
      tone: "bad",
    },
    {
      cycle: 1,
      text: "The kick is the doorbell for an image that is still arriving. The core comes out of reset and starts fetching from a window two flits short of complete.",
      tone: "bad",
    },
    {
      cycle: 3,
      text: "The rest of the image lands underneath a running program. What executes depends on how far the core got, so the failure is not reproducible between runs of the same image.",
      tone: "bad",
    },
  ],
};

const kickSpoolBroken = {
  rows: [
    {
      name: "recv queue (CU_DATA)",
      kind: "bus",
      values: ["img 8", "—", "—", "—"],
    },
    { name: "queue empty", kind: "bit", values: [0, 1, 1, 1], mark: [1] },
    { name: "loader spooling", kind: "bit", values: [1, 1, 1, 0] },
    { name: "kick accepted", kind: "bit", values: [0, 1, 0, 0], mark: [1] },
    {
      name: "words of the granule",
      kind: "bus",
      values: ["1", "3", "6", "8"],
    },
  ],
  notes: [
    {
      cycle: 1,
      text: "The receive queue is empty — the flit was consumed — so an interlock written as “nothing pending” releases here.",
      tone: "bad",
    },
    {
      cycle: 1,
      text: "But a granule is 256 bits spooled ONE WORD PER CYCLE, deliberately, so that neither memory needs a wide write port. The flit is gone and five of its eight words have not been written yet.",
      tone: "bad",
    },
    {
      cycle: 3,
      text: "The core has been fetching for two cycles against the last eight words of its own image. The window is short by a shrinking amount, which is the worst version of this failure: it depends on fetch latency and disappears under a debugger.",
      tone: "bad",
    },
  ],
};

const kickFixed = {
  rows: [
    {
      name: "recv queue (CU_DATA)",
      kind: "bus",
      values: ["img 8", "—", "—", "—"],
    },
    { name: "queue empty", kind: "bit", values: [0, 1, 1, 1] },
    { name: "loader spooling", kind: "bit", values: [1, 1, 1, 0] },
    { name: "receive-quiet", kind: "bit", values: [0, 0, 0, 1], mark: [3] },
    { name: "kick accepted", kind: "bit", values: [0, 0, 0, 1] },
    { name: "core in reset", kind: "bit", values: [1, 1, 1, 1] },
  ],
  notes: [
    {
      cycle: 0,
      text: "Receive-quiet is three conditions and not one: no pending receive, no granule in flight, and the loader idle. Any two of the three release early.",
      tone: "good",
    },
    {
      cycle: 3,
      text: "All three hold, and only now is the kick accepted. The core is additionally held in reset until the boot pulse, so no instruction is fetched before the image is complete — a second guard against the same hazard, not a restatement of the first.",
      tone: "good",
    },
    {
      text: "The hold clears by the unit's own progress and therefore cannot deadlock: nothing about receive-quiet waits on another inbound flit, which is the rule the fabric imposes on every endpoint.",
      tone: "good",
    },
  ],
};

/* --- the halt status ------------------------------------------------------ */

const haltBroken = {
  rows: [
    { name: "core halted", kind: "bit", values: [0, 1, 0, 0, 0], mark: [1] },
    { name: "running enabled", kind: "bit", values: [1, 1, 0, 0, 0] },
    { name: "core in reset", kind: "bit", values: [0, 0, 1, 1, 1] },
    {
      name: "HR_STATUS, read live",
      kind: "bus",
      values: ["0", "halted", "0", "0", "0"],
      mark: [2],
    },
    {
      name: "host polls",
      kind: "text",
      values: ["", "", "", "reads 0", "reads 0"],
    },
  ],
  notes: [
    {
      cycle: 1,
      text: "The program stores to the exit register. The core halts and its halted output asserts — for exactly as long as the core is out of reset.",
    },
    {
      cycle: 2,
      text: "Running is dropped the moment the core halts, and dropping it takes the core back into reset — which clears the very output the status register is reading.",
      tone: "bad",
    },
    {
      cycle: 3,
      text: "The host polls and reads zero. Not “still running” and not “halted”: the register reports nothing at all, and the exit word, the cause and the halt PC are all gone with it. The program ran correctly and left no evidence.",
      tone: "bad",
    },
  ],
};

const haltFixed = {
  rows: [
    { name: "core halted", kind: "bit", values: [0, 1, 0, 0, 0] },
    { name: "running enabled", kind: "bit", values: [1, 1, 0, 0, 0] },
    { name: "core in reset", kind: "bit", values: [0, 0, 1, 1, 1] },
    { name: "latch enable", kind: "bit", values: [0, 1, 0, 0, 0], mark: [1] },
    {
      name: "HR_STATUS, latched",
      kind: "bus",
      values: ["0", "halted", "halted", "halted", "halted"],
    },
    {
      name: "host polls",
      kind: "text",
      values: ["", "", "", "exited, cause", "reads HR_EXIT"],
    },
  ],
  notes: [
    {
      cycle: 1,
      text: "The halt, its cause and its PC are captured on the one cycle they exist, into registers that live OUTSIDE the reset domain they describe.",
      tone: "good",
    },
    {
      cycle: 3,
      text: "They hold until the next boot, which is the only event that can legitimately invalidate them. The host polls whenever it likes.",
      tone: "good",
    },
    {
      text: "A diagnostic that lives inside the thing being reset is not a diagnostic. The same reasoning sets the clear edge for the cycle and retire counters: they clear on the BOOT pulse, not on the core's reset, because the core is put back into reset at the end of a run and counters cleared there would read zero to whoever asked for them.",
      tone: "good",
    },
  ],
};

/* --- the status words ----------------------------------------------------- */

const statusBits = [
  { name: "rsvd", bits: 60, value: "reads 0" },
  { name: "exited", bits: 1, value: "3", accent: true },
  { name: "halted", bits: 1, value: "2", accent: true },
  { name: "cause", bits: 2, value: "1:0", accent: true },
];

const moverBits = [
  { name: "mv_busy", bits: 1, value: "32", accent: true },
  { name: "mv_fault", bits: 4, value: "31:28" },
  { name: "mv_done", bits: 28, value: "27:0", accent: true },
];

const statusSpec = {
  cols: [
    { key: "f", label: "Field", mono: true },
    { key: "w", label: "Width", align: "right", mono: true },
    { key: "p", label: "Position", mono: true },
    { key: "o", label: "Owner" },
  ],
  rows: [
    {
      f: "exited",
      w: "1",
      p: "HR_STATUS[3]",
      o: "<b>the wrapper's latch</b>, set when the program stores to the exit register. Cleared by the next boot and by nothing else",
    },
    {
      f: "halted",
      w: "1",
      p: "HR_STATUS[2]",
      o: "<b>the wrapper's latch</b>, not the core's live output — see the traces above",
      _tone: "warn",
    },
    {
      f: "cause",
      w: "2",
      p: "HR_STATUS[1:0]",
      o: "the wrapper's latch, from the core's halt cause: 0 external or exit store, 1 <code>ECALL</code>, 2 <code>EBREAK</code>, 3 a fault",
    },
    {
      f: "mv_done",
      w: "28",
      p: "ctrl 0x20 [27:0]",
      o: "<b>the mover</b>, and it is a running count rather than a flag — the program compares it against what it issued",
    },
    {
      f: "mv_fault",
      w: "4",
      p: "ctrl 0x20 [31:28]",
      o: "the mover",
    },
    {
      f: "mv_busy",
      w: "1",
      p: "ctrl 0x20 [32]",
      o: "the mover. <b>Note the width:</b> this word crosses a 32-bit boundary, so it is one 64-bit load and not two 32-bit ones",
      _tone: "warn",
    },
  ],
};

const hostRegs = {
  cols: [
    { key: "o", label: "Offset", mono: true, align: "center" },
    { key: "n", label: "Register", mono: true },
    { key: "w", label: "" },
  ],
  rows: [
    { o: "0x00", n: "HR_BOOT", w: "write 1: pulse boot and enable running" },
    {
      o: "0x08",
      n: "HR_PC",
      w: "<b>latched and unused</b> — the core's reset PC is a parameter fixed at 0",
      _tone: "warn",
    },
    {
      o: "0x10",
      n: "HR_DBELL",
      w: "doorbell; bit 0 reaches the core's software interrupt line",
    },
    { o: "0x18", n: "HR_STATUS", w: "<code>{exited, halted, cause[1:0]}</code>" },
    { o: "0x20", n: "HR_EXIT", w: "the program's exit word" },
    { o: "0x28", n: "HR_HALTPC", w: "where it stopped" },
    {
      o: "0x30",
      n: "HR_CYCLES",
      w: "64-bit cycle counter, cleared on boot",
    },
    {
      o: "0x38",
      n: "HR_RETIRED",
      w: "64-bit retire counter, cleared on boot",
    },
  ],
};

const scCtrl = {
  cols: [
    { key: "o", label: "Offset", mono: true, align: "center" },
    { key: "w", label: "Write" },
    { key: "r", label: "Read" },
  ],
  rows: [
    {
      o: "0x00",
      w: "<b>exit</b> — latch the 64-bit word, halt the core, set <code>exited</code>",
      r: "0",
    },
    { o: "0x08", w: "console byte, observation only", r: "0" },
    { o: "0x10", w: "doorbell; bit 0 reaches the software interrupt line", r: "the doorbell bit" },
    {
      o: "0x18",
      w: "<b>nothing.</b> <code>satp</code> is CSR <code>0x180</code> and supervisor software owns it",
      r: "<code>satp</code> — a <b>read-only mirror</b>, so the host can see which address space is installed without stopping the core",
      _tone: "warn",
    },
    { o: "0x20", w: "—", r: "<code>{mv_busy, mv_fault[3:0], mv_done[27:0]}</code>" },
    {
      o: "0x28",
      w: "—",
      r: "the interlink's <b>four inbound doorbell counts</b>, 16 bits each, one per source mesh",
    },
    {
      o: "<b>0x40–0x7F</b>",
      w: "the <b>dispatch mailbox</b> — see the map below",
      r: "the mailbox's registers",
      _tone: "good",
    },
    {
      o: "0x80–0xBF",
      w: "one <b>mover config</b> register per 8-byte slot, mapped onto mover offsets <code>0x00</code>–<code>0x3F</code>",
      r: "0",
    },
    {
      o: "<b>0xC0–0xFF</b>",
      w: "the <b>interlink window</b> — the offset's low six bits are the interlink's register, plus <code>0x80</code>. See the map below",
      r: "0",
      _tone: "good",
    },
  ],
};

/* --- the interlink window ------------------------------------------------- */

const ilRegs = {
  cols: [
    { key: "o", label: "Control", mono: true, align: "center" },
    { key: "i", label: "Interlink", mono: true, align: "center" },
    { key: "f", label: "Fields", mono: true },
    { key: "m", label: "Meaning" },
  ],
  rows: [
    {
      o: "0xC0",
      i: "0x80",
      f: "[0] enable · [1] clear counts · [2] clear faults",
      m: "Enabled at reset. <b>One write lands all three</b> — see the trap below",
      _tone: "warn",
    },
    {
      o: "0xC8",
      i: "0x88",
      f: "[1:0] mesh id",
      m: "Defaults to the node's <code>MESH_ID</code> parameter; a build rarely writes it",
    },
    {
      o: "0xD0",
      i: "0x90",
      f: "[1:0] destination mesh · [15:8] tag",
      m: "<b>Writing this rings that mesh.</b> The tag crosses with the ring and is the sender's to define",
      _tone: "good",
    },
    {
      o: "0x28",
      i: "—",
      f: "four 16-bit lanes, read-only",
      m: "Inbound rings <b>by source mesh</b>: mesh 0 in [15:0], mesh 1 in [31:16], mesh 3 in [63:48]",
    },
  ],
};

const ilSpec = {
  cols: [
    { key: "f", label: "Field", mono: true },
    { key: "w", label: "Width", align: "right", mono: true },
    { key: "p", label: "Position", mono: true },
    { key: "o", label: "Owner" },
  ],
  rows: [
    {
      f: "enable",
      w: "1",
      p: "0xC0 [0]",
      o: "<b>software, on every write to this register.</b> It is not a set-only bit and there is no read-modify-write behind it: whatever bit 0 of the store says becomes the enable",
      _tone: "warn",
    },
    {
      f: "clear counts",
      w: "1",
      p: "0xC0 [1]",
      o: "<b>software, and it is the interrupt acknowledge.</b> Zeroes all four inbound counts; a ring arriving in the same cycle wins, so a count is never silently lost to a clear",
      _tone: "good",
    },
    {
      f: "clear faults",
      w: "1",
      p: "0xC0 [2]",
      o: "software. Zeroes the interlink's fault register",
    },
    {
      f: "mesh id",
      w: "2",
      p: "0xC8 [1:0]",
      o: "<b>hardware at reset, software after.</b> It is what every outbound packet stamps as its source and what the inbound decode compares against — changing it mid-run redirects everything",
      _tone: "warn",
    },
    {
      f: "destination mesh",
      w: "2",
      p: "0xD0 [1:0]",
      o: "<b>software.</b> Meshes chain 0 — 1 — 3 — 2; a ring for a farther mesh transits the ones between",
    },
    {
      f: "tag",
      w: "8",
      p: "0xD0 [15:8]",
      o: "<b>software, and the receiver never sees it in the counts.</b> It is carried for the link's own status window, not delivered to the far processor — the count is what a handler reads",
      _tone: "warn",
    },
    {
      f: "inbound counts",
      w: "16 × 4",
      p: "0x28",
      o: "<b>hardware.</b> A count, not a flag, so a handler polling slower than rings arrive can tell how many it missed",
    },
  ],
};

const boot = [
  {
    title: "1 · write the image",
    code: ["selector 0", "the instruction window, word by word"],
    where: "the instruction window",
    note: "The host window is write-only for the memories. The read-data register is a case on the low address byte regardless of the selector, so reading a memory selector returns control-register values. Read-back of an image is not available.",
  },
  {
    title: "2 · write the data",
    code: ["selector 1", ".rodata and .data, 64-bit with byte strobes"],
    where: "the scratchpad",
    note: ".rodata is read with loads, and a load never reaches the instruction window — so it must be linked into the scratchpad, not beside .text.",
  },
  {
    title: "3 · write HR_BOOT = 1",
    code: ["selector 2, offset 0x00", "HR_BOOT = 1"],
    where: "the core's reset, released one cycle later",
    note: "There is no receive-quiet interlock here and none is needed: the host writes the memories and then writes the boot register through the same ordered AXI slave.",
  },
  {
    title: "4 · poll HR_STATUS",
    code: ["selector 2, offset 0x18", "{exited, halted, cause[1:0]}"],
    where: "a latched copy of the halt state",
    note: "Running is enabled by HR_BOOT and dropped the moment the core halts, and dropping it takes the core back into reset — which clears the core's own halted output. A status register reading that output directly reports nothing at all.",
  },
  {
    title: "5 · read the results",
    code: ["HR_EXIT", "HR_HALTPC", "HR_CYCLES", "HR_RETIRED"],
    where: "registers that survive the reset",
    note: "Both counters are 64-bit, unlike the shell's 32, because a runtime runs long enough to wrap 32.",
  },
];

/* --- the node ------------------------------------------------------------- */

const wired = {
  cols: [
    { key: "p", label: "At sysnode" },
    { key: "s", label: "The RV64 branch" },
  ],
  rows: [
    { p: "the host window <code>hs_*</code>", s: "<b>connected</b>", _tone: "good" },
    {
      p: "the processor's memory path <code>cp_*</code> onto MAG",
      s: "<b>connected</b>",
      _tone: "good",
    },
    {
      p: "the mover's master <code>mv_*</code>, and its status",
      s: "<b>connected</b>",
      _tone: "good",
    },
    {
      p: "the host's <code>aux_cfg_*</code> config path and the interlink gate",
      s: "<b>connected</b>",
      _tone: "good",
    },
    {
      p: "console debug, and the node's busy line",
      s: "<b>connected</b>",
      _tone: "good",
    },
    {
      p: "the hub's processor port — <code>pe_tx_*</code>, <code>pe_rx_*</code>",
      s: "<b>connected</b>, to the mailbox rather than to a compute-unit shell. The complex sits at <code>(0,0)</code>, the corner the hub already decodes the processor at",
      _tone: "good",
    },
    {
      p: "the interlink doorbell — <code>db_en</code>, <code>db_addr</code>, <code>db_data</code>",
      s: "<b>connected</b>, and the address is offset by <code>0x80</code> so the interlink claims the write. The processor rings another mesh without a host round trip; <b>the host wins a same-cycle collision</b> and the processor retries",
      _tone: "good",
    },
    {
      p: "an inbound ring → <code>irq_summary</code>",
      s: "<b>connected</b> — a registered OR of the four inbound counts, so a doorbell from another mesh raises the processor's external interrupt and holds it until the handler clears the counts",
      _tone: "good",
    },
    {
      p: "<code>db_status</code>",
      s: "<b>connected</b> — the interlink's four inbound doorbell counts, 16 bits each, one per source mesh, in one 64-bit word",
      _tone: "good",
    },
    {
      p: "<code>irq_summary</code>, the external interrupt line",
      s: "<b>connected</b> to <code>(|mv_fault) || pe_halt_req || dbell_pend</code> — a mover fault, the host asking the node to stop, and an inbound ring. The mailbox's completion queue raises the same line from inside the processor",
      _tone: "good",
    },
    {
      p: "<code>pe_status</code>",
      s: "<b>connected</b> — <code>{mover fault, busy}</code>, enough for a host to tell a running node from a stopped one without the control window",
      _tone: "good",
    },
    {
      p: "the transform slot's register port — <code>cfg_en</code>, <code>cfg_rdata</code>",
      s: "<b>tied to zero and unread.</b> The bank's own registers are not reachable from the processor in either complex, so synthesis strips that port",
      _tone: "bad",
    },
    {
      p: "mover config offsets <code>0x40</code> and <code>0x50</code>",
      s: "<b>not reachable</b> from the processor — see the trap below",
      _tone: "bad",
    },
  ],
};

const complex = {
  nodes: [
    {
      id: "cpu",
      x: 0,
      y: 0,
      w: 18,
      h: 5,
      label: "rv64_syscore",
      sub: "the processor, with its own AXI master onto MAG",
      accent: true,
    },
    {
      id: "mv",
      x: 0,
      y: 7,
      w: 18,
      h: 5,
      label: "mm_mover",
      sub: "the node's descriptor engine, its own AXI master",
    },
    {
      id: "xf",
      x: 0,
      y: 14,
      w: 18,
      h: 5,
      label: "mag_xform",
      sub: "the transform slot, on the mover's read return · cfg_en tied low here",
    },
    {
      id: "cfg",
      x: 26,
      y: 3.5,
      w: 16,
      h: 5,
      label: "the config port",
      sub: "the processor wins when both pulse in one cycle",
      accent: true,
    },
    {
      id: "aux",
      x: 26,
      y: 10.5,
      w: 16,
      h: 5,
      label: "the host's aux window",
      sub: "splits at offset 0x80 — below it the mover, at or above it the interlink",
    },
  ],
  edges: [
    { from: "cpu:r", to: "cfg:l", label: "the control region", accent: true },
    { from: "aux:l", to: "mv:r", label: "when the gate allows" },
    { from: "cfg:l", to: "mv:r" },
  ],
};

/* --- parameters ----------------------------------------------------------- */

const coreParams = {
  cols: [
    { key: "p", label: "Parameter", mono: true },
    { key: "d", label: "Default", align: "center", mono: true },
    { key: "w", label: "" },
  ],
  rows: [
    { p: "RESET_PC", d: "0", w: "both wrappers leave it at 0" },
    {
      p: "MEM_PRIM",
      d: "distributed",
      w: "the register file's primitive — a measured trade, not a preference",
    },
    {
      p: "HAS_ATOMIC",
      d: "1",
      w: "0 constant-propagates the whole AMO sequencer away. <b>Both wrappers leave it on</b>",
    },
  ],
};

const peParams = {
  cols: [
    { key: "p", label: "Parameter", mono: true },
    { key: "d", label: "Default", align: "center", mono: true },
    { key: "w", label: "" },
  ],
  rows: [
    {
      p: "FLIT_WIDTH, POS_WIDTH, POS_X, POS_Y",
      d: "288, 4, 2, 2",
      w: "the fabric's, not this unit's",
    },
    {
      p: "IMEM_WORDS, SPAD_WORDS",
      d: "4096, 2048",
      w: "<b>must match the link script</b> — changing one silently truncates the image at the loader's bounds check",
      _tone: "bad",
    },
    { p: "INST_DEPTH, RECV_DEPTH", d: "16, 32", w: "the shell's two queues" },
    {
      p: "SPAD_BASE, CTRL_BASE",
      d: "0x0001_0000, 0x0002_0000",
      w: "honoured by the decode",
    },
    { p: "MEM_PRIM", d: "block", w: "the instruction window" },
    {
      p: "SPAD_STYLE",
      d: "ultra",
      w: "measured: <code>ultra</code> 289.9 MHz / 6,962 LUT / 1 URAM against <code>block</code> 280.9 / 7,007 / 10 BRAM. UltraRAM wins on the byte-write-enable path",
    },
    { p: "RF_PRIM", d: "distributed", w: "passed to the core's <code>MEM_PRIM</code>" },
  ],
};

const scParams = {
  cols: [
    { key: "p", label: "Parameter", mono: true },
    { key: "d", label: "Default", align: "center", mono: true },
    { key: "w", label: "" },
  ],
  rows: [
    {
      p: "ADDR_W, DATA_W",
      d: "40, 256",
      w: "the card's physical address width and the node's beat",
    },
    {
      p: "IMEM_WORDS, SPAD_WORDS",
      d: "8192, 4096",
      w: "32 KB each; must match the link script",
    },
    { p: "L1_LINES", d: "64", w: "32-byte lines, so 2 KB" },
    { p: "TLB_ENTRIES", d: "32", w: "direct-mapped" },
    {
      p: "SPAD_BASE, CTRL_BASE",
      d: "0x0001_0000, 0x0002_0000",
      w: "honoured by the decode",
    },
    {
      p: "<b>NODE_BASE, CACHE_LO</b>",
      d: "2²⁸, 2³¹",
      w: "<b>not honoured.</b> The decode is written with the bit positions literally, so changing either parameter changes nothing. Treat both as documentation and edit the tests",
      _tone: "bad",
    },
    {
      p: "XFORM_SLOTS, XID_W, XMODE_W, XFORM_IN_BITS, XFORM_OUT_WORDS",
      d: "1, 4, 4, 2048, 4",
      w: "the transform slot's, on <code>rv64_mag_pe</code> only",
    },
  ],
};

const intAbsent = {
  cols: [
    { key: "n", label: "Not provided" },
    { key: "w", label: "Consequence" },
  ],
  rows: [
    {
      n: "<b>flow control on the mailbox's inbound side</b>",
      w: "the completion queue is 16 deep and its busy line is tied low, so a burst of more than 16 completions between two polls <b>drops the surplus</b> and sets a sticky bit. Holding them instead would stall the hub's link, which is worse — but a dispatcher that keeps more than 16 instructions outstanding must read that bit",
      _tone: "warn",
    },
    {
      n: "a <code>CU_DATA</code> or <code>CU_CTRL</code> path for the node processor",
      w: "the mailbox sends <code>CU_INST</code> and receives <code>CU_SIGNAL</code>, and nothing else. It cannot push operands to a unit or read a unit's control registers; those go through MAG and the control agent",
    },
    {
      n: "a doorbell a <i>compute unit</i> can ring",
      w: "both wrappers carry a software-interrupt doorbell the host can write, and at the node another <b>mesh</b> can ring the interlink's — but <b>no flit writes either</b>. A unit that wants the processor's attention sends a completion, which raises the external line instead",
    },
    {
      n: "a load or store to another mesh",
      w: "<b>the processor's port is local in both directions.</b> Cross-mesh data movement is a mover descriptor; cross-mesh notification is a doorbell; and <b>reads never cross the link at all</b>, so a mover source must be in this mesh",
      _tone: "warn",
    },
    {
      n: "the transform slot's registers, from the processor",
      w: "the bank's <code>cfg_en</code> is tied to zero in the complex and its <code>cfg_rdata</code> is unread, so the slot's own registers are reachable only from the host's config window",
      _tone: "bad",
    },
    {
      n: "a reset PC and a kick argument",
      w: "both wrappers latch one and use neither; programs start at 0",
    },
    { n: "image read-back", w: "the host window's memory selectors are write-only" },
    {
      n: "a second processor per wrapper",
      w: "one core each, and the reservation machinery assumes it",
    },
    {
      n: "a clock-domain crossing",
      w: "every port on all three tops is synchronous to one clock; crossing to the host or to DRAM is the AXI surface's job",
    },
  ],
};
</script>

<template>
  <DocPage
    title="Integrating the RV64 system core"
    summary="The two wrappers — a mesh compute unit with a loader and a kick, and a node processor that replaces the compute-unit shell with a mailbox — why the shell is gone and what its removal owes, the host window, the dispatch mailbox register by register, the node complex, and exactly which of the node's ports are wired and which still terminate in constants."
    domain="cpu"
    status="building"
    source="src/kohakuaccel/pe/rv64-sys/ · docs/arch/cpu/rv64-sys/integration.md"
  >
    <p class="doc-p">
      <code>rv64_core</code> has no fabric interface, no memory map and no way to
      be started. All three come from a wrapper, and there are two — plus a third
      module that is not a third configuration but the node complex the second
      one sits inside.
    </p>

    <h2 class="doc-h2">What a wrapper owns</h2>
    <p class="doc-p">
      Four things the core does not have and cannot be used without. Everything
      on this page is one of them, in one of two wrappers.
    </p>
    <div class="grid gap-3 sm:grid-cols-2 my-5">
      <div class="card p-4">
        <div
          class="kt-text-caption font-semibold text-warm-800 dark:text-warm-200 mb-1"
        >
          A way in
        </div>
        <p class="kt-text-caption text-warm-500 dark:text-warm-400 leading-6">
          Something that writes the instruction window — a flit loader, or an
          AXI slave window from the host.
        </p>
      </div>
      <div class="card p-4">
        <div
          class="kt-text-caption font-semibold text-warm-800 dark:text-warm-200 mb-1"
        >
          A way to start
        </div>
        <p class="kt-text-caption text-warm-500 dark:text-warm-400 leading-6">
          A kick, and the interlock that stops it overtaking the image it is the
          doorbell for.
        </p>
      </div>
      <div class="card p-4">
        <div
          class="kt-text-caption font-semibold text-warm-800 dark:text-warm-200 mb-1"
        >
          A memory map
        </div>
        <p class="kt-text-caption text-warm-500 dark:text-warm-400 leading-6">
          What a 64-bit address means, and a control region for everything that
          is not memory.
        </p>
      </div>
      <div class="card p-4">
        <div
          class="kt-text-caption font-semibold text-warm-800 dark:text-warm-200 mb-1"
        >
          A way to report
        </div>
        <p class="kt-text-caption text-warm-500 dark:text-warm-400 leading-6">
          A completion flit, or a status register — and it has to survive the
          reset that follows the run.
        </p>
      </div>
    </div>

    <SpecTable :cols="tops.cols" :rows="tops.rows" />

    <Callout kind="note" title="Terms, defined once">
      <p>
        A <b>flit</b> is the 288-bit unit the fabric carries; a <b>compute
        unit</b> is anything that attaches to one fabric port, takes instructions
        one at a time and signals retirement; a <b>kick</b> is the instruction
        that starts one; a <b>completion</b> is the flit it sends back.
        <b>MAG</b> is the system node's memory access half. The <b>mover</b> is
        the node's descriptor-walking memory engine, and the <b>transform
        slot</b> is the addon position on the mover's read-return path.
        <b>Staging</b> is on-chip memory inside MAG shared by the mover and the
        inter-mesh link.
      </p>
    </Callout>

    <h2 class="doc-h2">Why the node processor has no compute-unit shell</h2>

    <p class="doc-p">
      <code>noc_cu_base</code> is the framework's compute-unit shell: it owns the
      fabric port, the instruction queue, the <code>CU_CTRL</code> registers and
      the kick-and-complete handshake. Every compute unit on the fabric has one.
      The node processor does not, and the reasons are ordered by weight —
      <b>area is the least of them</b>, since the shell measures 756 LUT against
      a whole-node budget of 35,000.
    </p>

    <p class="doc-p">
      <b>Lifecycle.</b> The shell implements <i>someone kicks me, I run to
      completion, I report a 32-bit word</i>. A runtime boots once and runs
      forever: there is no completion to report and nothing for the result field
      to carry. Building a run-then-done machine around a program meant never to
      end is the wrong shape, and every diagnostic that lives inside it inherits
      the wrong shape too.
    </p>

    <Fig
      caption="Deadlock, and the cycle is specific. The node processor is the unit that SERVICES the fabric: it dispatches to compute units and consumes their completions. Behind the shell its inbound path is gated by a busy line from finite queues, and its dispatch shares one output port with the shell's own traffic."
      zoom
      wide
    >
      <BlockDiagram :nodes="deadlock.nodes" :edges="deadlock.edges" />
    </Fig>

    <Callout kind="rule" title="The unit that arbitrates the fabric must not be flow-controlled by the fabric">
      <p>
        A compute unit can afford to block; the scheduler cannot.
        <b>The mailbox that replaces the shell ties its busy line low and never
        raises it</b> — a completion the queue cannot take is accepted and
        dropped, and a sticky overflow bit records that it happened. Dropping a
        completion loses information; holding one stalls the link that would
        have delivered the traffic that drains the queue.
      </p>
      <p>
        <b>And the loader is a second memory-write protocol.</b> MAG already
        provides a memory path and the host already reaches the card over AXI
        into MAG. Loading the instruction window by AXI write plus a doorbell
        needs no loader state machine, no buffer-id map, no bounds check and no
        receive-quiet interlock — all of which exist only because the image
        arrives as flits.
      </p>
    </Callout>

    <SpecTable
      :cols="replaced.cols"
      :rows="replaced.rows"
      caption="What replaces the shell, and what is owed"
    />

    <h2 class="doc-h2">rv64_sys_pe — the mesh compute unit</h2>

    <p class="doc-p">
      The core wrapped so the framework recognises it: the image arrives over
      <code>CU_DATA</code> flits, the kick over <code>CU_INST</code>, the result
      leaves on <code>CU_SIGNAL</code>. <code>CU_TYPE</code> is
      <code>0x5236</code>, four buffers, a 16-entry instruction queue and a
      32-entry receive queue.
    </p>

    <Fig
      caption="The unit does not emit flits of its own: the shell's send path is tied off, so there is no CU_CTRL reply and no peer message. What it publishes is the capability set noc_cu_base provides by default, plus its cycle and retire counters on the shell's debug pair."
      zoom
      wide
    >
      <BlockDiagram :nodes="peWrap.nodes" :edges="peWrap.edges" />
    </Fig>

    <SpecTable
      :cols="bufIds.cols"
      :rows="bufIds.rows"
      caption="A granule is 256 bits, spooled one word per cycle rather than written as a wide port, which is what keeps both memories at their natural width and off any wide write path. A rejected transfer is consumed and dropped, not written somewhere. The bounds check is expressed in granule units for both forms, so a word-granularity write reaches only the first eighth of the instruction window or the first quarter of the scratchpad"
    />

    <Callout kind="rule" title="A kick MUST NOT be accepted until all three receive-quiet conditions hold">
      <p>
        <code>CU_INST</code> and <code>CU_DATA</code> arrive on <b>two
        queues</b>, so a kick can overtake the image it is the doorbell for. The
        kick machine <b>MUST</b> wait on <b>receive-quiet</b> — no pending
        receive, <b>no granule in flight</b>, and the loader idle — before it
        accepts one. Any two of the three release early. The core is
        additionally <b>held in reset until the boot pulse</b>, so no instruction
        is fetched before the image is complete. The hold clears by the unit's
        own progress and cannot deadlock.
      </p>
    </Callout>

    <WaveTrace
      :rows="kickRaceBroken.rows"
      :notes="kickRaceBroken.notes"
      variant="broken"
      label="No interlock — the kick overtakes the image on the other queue"
    />

    <WaveTrace
      :rows="kickSpoolBroken.rows"
      :notes="kickSpoolBroken.notes"
      variant="broken"
      label="Waiting on “the queue is empty” — the last granule is still spooling"
    />

    <WaveTrace
      :rows="kickFixed.rows"
      :notes="kickFixed.notes"
      variant="fixed"
      label="All three conditions, plus the reset hold behind them"
    />

    <Callout
      kind="trap"
      title="A short image does not fault — it executes whatever the window held before"
    >
      <p>
        There is no unmapped-fetch fault and no image checksum, so a core started
        against a partly written window fetches the previous run's instructions,
        or zeros, and runs them. The symptom is a completion that arrives with a
        plausible exit word from a program that never ran, or a fault at an
        address nothing was linked to.
      </p>
      <p>
        The second trace is the one to watch for, because
        <b>the window it leaves open shrinks as the loader drains</b>: it depends
        on fetch latency, it is not reproducible between runs, and adding
        observation makes it disappear. The granule is spooled a word per cycle
        on purpose — it is what keeps both memories at their natural width and
        off any wide write path — so “the flit was consumed” and “the memory was
        written” are several cycles apart by design.
      </p>
    </Callout>

    <Fig
      caption="The kick's start PC and argument word are latched and not used: the core's reset PC is a module parameter fixed at 0, so a program always starts at address 0 and receives no argument. A caller that needs to pass one writes it into the scratchpad before the kick."
      zoom
    >
      <StateMachine :states="kickFsm.states" :edges="kickFsm.edges" :r="42" />
    </Fig>

    <Callout kind="note" title="The counters clear on the boot pulse, not on the core's reset">
      <p>
        The core is put back into reset when the run ends, and counters cleared
        there would read zero to whoever asked for them.
      </p>
    </Callout>

    <SpecTable
      :cols="peCtrl.cols"
      :rows="peCtrl.rows"
      caption="The control region has ONE readable value. Every read of it returns the doorbell bit, whatever the offset; the read is registered, and the select with it, because as a combinational mux it sat inside the core's load path and cost the unit 52 MHz against the core alone. The scratchpad is a one-cycle read anyway, so making the control region match it costs nothing"
    />

    <h3 class="doc-h3">What this configuration does not have</h3>

    <SpecTable :cols="peAbsent.cols" :rows="peAbsent.rows" />

    <h2 class="doc-h2">rv64_syscore — the node's processor</h2>

    <Fig
      caption="The host window is write-only for the memories. The read-data register is a case on the low address byte regardless of the selector, so reading the instruction-window or scratchpad selector returns control-register values."
      zoom
      wide
    >
      <BlockDiagram :nodes="scWrap.nodes" :edges="scWrap.edges" />
    </Fig>

    <SpecTable :cols="hostRegs.cols" :rows="hostRegs.rows" />

    <Callout kind="rule" title="A diagnostic that lives inside the thing being reset is not a diagnostic">
      <p>
        Running is enabled by <code>HR_BOOT</code> and dropped the moment the
        core halts, and dropping it takes the core back into reset — which clears
        the core's own <code>halted</code> output. A status register reading that
        output directly reports nothing at all. The halt, its cause and its halt
        PC <b>MUST</b> be captured into registers outside that reset domain, and
        they <b>MUST</b> hold until the next boot.
      </p>
    </Callout>

    <WaveTrace
      :rows="haltBroken.rows"
      :notes="haltBroken.notes"
      variant="broken"
      label="Reading the core's live halted output — the answer is erased by the act of stopping"
    />

    <WaveTrace
      :rows="haltFixed.rows"
      :notes="haltFixed.notes"
      variant="fixed"
      label="Latched outside the reset domain — the answer survives the run"
    />

    <Fig
      caption="HR_STATUS, at offset 0x18. Four bits, and all four are the wrapper's latches rather than the core's live outputs — which is the entire content of the two traces above."
    >
      <BitField :fields="statusBits" />
    </Fig>

    <Fig
      caption="The mover status word, read at control-region offset 0x20. Thirty-three bits, so it crosses a 32-bit boundary — read it as one 64-bit load."
    >
      <BitField :fields="moverBits" />
    </Fig>

    <SpecTable
      :cols="statusSpec.cols"
      :rows="statusSpec.rows"
      caption="The owner column separates the two kinds of field on this page: what the wrapper latched for the host, and what another engine is publishing live. A host that polls the first kind is reading a record; a program that polls the second is racing a running machine"
    />

    <StepPlayer :steps="boot" label="booting the node processor">
      <template #default="{ state }">
        <div
          class="font-mono kt-text-caption whitespace-pre text-warm-700 dark:text-warm-300 leading-6 overflow-x-auto"
        >
          {{ state.code.join("\n") }}
        </div>
        <div class="mt-3 flex items-center gap-2">
          <span
            class="kt-text-micro uppercase tracking-wider text-warm-400 dark:text-warm-600"
            >what it reaches</span
          >
          <span class="chip">{{ state.where }}</span>
        </div>
      </template>
    </StepPlayer>

    <h3 class="doc-h3">The node port, and the control region</h3>

    <p class="doc-p">
      One AXI master, 40-bit address, 256-bit data, one outstanding access, no
      bursts. It is deliberately <b>the same shape the RV32 control processor
      presents</b>, so the RV64 complex drops into the same socket inside the
      node. The control region is 256 bytes and is where everything that is not
      memory lives; reads are early and writes are registered.
    </p>

    <SpecTable :cols="scCtrl.cols" :rows="scCtrl.rows" />

    <Callout kind="rule" title="mv.go is a store, not an opcode">
      <p>
        Decoding the mover's command window out of an address range keeps the ISA
        unchanged and matches the framework rule that control is a range rather
        than an instruction. The register index is the low six bits of the
        offset, so <b>the byte offset inside the window <i>is</i> the config
        address</b>; the descriptor a program builds in its own memory becomes
        seven stores, in program order, and <b>program order is the queue</b>.
      </p>
    </Callout>

    <Callout
      kind="trap"
      title="Two of the mover's nine registers fall outside the window"
    >
      <p>
        The mover answers at config offsets
        <code>0x00, 0x10, 0x18, 0x20, 0x28, 0x30, 0x38, 0x40, 0x50</code>. The
        control region maps its own <code>0x80</code>–<code>0xBF</code> onto
        mover offsets <code>0x00</code>–<code>0x3F</code>, so <code>0x40</code>
        — the fill immediate — and <code>0x50</code> — the gather pitch and word
        count — land <b>outside it, in the doorbell sub-range</b>.
      </p>
      <p>
        <b>The symptom is a FILL or GATHER move that runs with a stale immediate
        or stale geometry and reports success.</b> Both registers stay reachable
        from the host's config window, which passes every offset below
        <code>0x80</code> through. The two windows were sized independently and
        nothing about the address map makes the overlap deliberate.
      </p>
    </Callout>

    <h2 class="doc-h2">The interlink window — ringing another mesh</h2>

    <p class="doc-p">
      A processor reaches the mesh next door through <b>three registers</b> at
      control offset <code>0xC0</code>. Nothing else it does crosses the link:
      its own loads and stores are local in both directions, so the sequence for
      handing work to another node is always <b>move the data with the mover,
      wait for it, then ring</b>.
    </p>

    <SpecTable
      :cols="ilRegs.cols"
      :rows="ilRegs.rows"
      caption="The window maps the control region's low six bits onto the interlink's own register file, offset by 0x80 — which is the whole of the decode. Emitting the un-offset address instead is a window that answers every write and drives nothing, because the interlink claims a config write only at 0x80 and above"
    />

    <SpecTable
      :cols="ilSpec.cols"
      :rows="ilSpec.rows"
      caption="Field by field. Two of the owner cells are warnings rather than descriptions: the enable is rewritten by every store to its register, and the tag a sender chooses is not what the receiver reads"
    />

    <Callout kind="rule" title="An inbound ring is a level, and the handler clears the counts">
      <p>
        Each inbound ring increments the count for <b>its source mesh</b>, and
        the node holds the processor's <b>external interrupt</b> asserted while
        any count is non-zero. A level, not an edge — a ring that arrives while
        another is being serviced is not lost, and the handler that clears the
        counts dismisses exactly what it has seen.
      </p>
      <p>
        <b>An inbound doorbell waits for every write that arrived ahead of it to
        be acknowledged by its memory before it counts</b>, so the ring a far
        handler sees is backed by data that is in memory rather than in a queue.
        That is the only synchronisation that crosses the link, because the far
        side's memory is not readable from here at all — and it orders the ring
        against writes already on the link, not against writes still leaving.
      </p>
    </Callout>

    <Callout kind="rule" title="Wait for the mover to report idle before you ring">
      <p>
        The sequence is <b>mover write → poll <code>MV_STAT</code> bit 32 until
        it clears → ring</b>, and the middle step is load-bearing. The mover
        reports idle only once every write packet has been accepted onto the
        link, and the link delivers in order; together with the receiving rule
        above, that is what makes the ring mean <i>the data is there</i>.
      </p>
      <p>
        <b>The ring is not a release fence on its own.</b> The sending arbiter
        rotates between writes, flits and doorbells, so a ring issued while a
        burst is still going out can reach the link ahead of the rest of that
        burst. <b>The symptom is a receiver that reads a partly written
        buffer</b> — timing-dependent, so it survives a small transfer and fails
        on a large one, and no barrier instruction substitutes for the poll.
      </p>
    </Callout>

    <Callout kind="trap" title="Clearing the counts rewrites the enable bit in the same store">
      <p>
        <code>0xC0</code> is one register with three fields, and
        <b>bit 0 is taken as the enable on every write to it</b>. So the obvious
        acknowledge — <code>*DB_CTL = 1 &lt;&lt; 1</code> — clears the counts
        <i>and disables the interlink</i>, in one store, with nothing to say so.
      </p>
      <p>
        The acknowledge is <code>0b11</code>: enable and clear together, which is
        what the shipped handler writes. <b>The symptom is a machine that
        services exactly one ring and then goes quiet</b> — no fault bit, no
        error, because switching a link off is not a fault. It looks like the
        far node stopped sending.
      </p>
    </Callout>

    <Callout kind="measured" title="Two nodes, one link, and the bench that proves the four pieces compose">
      <p>
        <span class="chip">rv64_node_pair</span> is <b>two complete
        <code>sysnode</code>s</b> cross-connected link to link, each with its own
        DRAM model and its own program. Mesh 0 writes sixteen 64-bit words into
        its own staging, has the mover copy four 32-byte words into
        <b>mesh 1's</b> staging across the link, and rings mesh 1. Mesh 1 takes
        the ring <b>as an external interrupt</b>, clears the counts in its
        handler, checks the words it was sent, and rings back; mesh 0 polls its
        own count for the reply and clears it.
      </p>
      <p>
        Both programs exit zero. Four abilities are load-bearing in that one run
        and <b>each of them looks correct in isolation</b>: the doorbell window's
        address offset, byte strobes through staging, the rule that keeps a
        remote staging write's full address, and the interrupt level. A
        single-node bench proves none of them.
      </p>
    </Callout>

    <h2 class="doc-h2">The dispatch mailbox</h2>

    <p class="doc-p">
      Dropping the compute-unit shell dropped the processor's only path onto the
      fabric with it. What replaces it is <b>seven registers at control offset
      <code>0x40</code></b> and a 16-deep completion queue —
      <code>rv64_noc_mbox</code>, a client of the node's hub rather than an
      endpoint of its own. Software writes a destination and two payload words;
      <b>hardware builds the flit.</b>
    </p>

    <SpecTable
      :cols="mboxRegs.cols"
      :rows="mboxRegs.rows"
      caption="The window is 0x40–0x7F and the register index is offset bits 5:3, so each register is one 64-bit word. GO and POP are commands rather than values: what you write to them is discarded"
    />

    <SpecTable :cols="mboxRules.cols" :rows="mboxRules.rows" />

    <Fig
      caption="A completion, as HEAD returns it. The source coordinate is how software tells which unit finished — the mailbox does not track outstanding work, so a dispatcher that runs several units at once keeps its own table keyed on that pair. HEAD reads zero when the queue is empty, which is indistinguishable from a completion whose fields are all zero: read STAT first, or check `used` before believing a head."
    >
      <BitField :fields="headBits" />
    </Fig>

    <Fig
      caption="STAT. Three live fields in one 64-bit load — the sticky overflow bit, whether a dispatch flit is still waiting for the link, and how many completions are queued."
    >
      <BitField :fields="statBits" />
    </Fig>

    <SpecTable
      :cols="mboxSpec.cols"
      :rows="mboxSpec.rows"
      caption="Field by field, and the owner column is the one to read before writing a dispatcher. Everything hardware owns here is either a header field software cannot see or a status bit software cannot write; everything software owns is carried verbatim and never interpreted"
    />

    <Callout kind="trap" title="The transaction id is hardware's, and it does not come back on its own">
      <p>
        Every accepted GO increments an 8-bit <code>txn</code> in the flit
        header, and <b>software can neither read nor write it</b>. The framework
        returns it in a completion's <code>arg</code> only for a
        <i>batch complete</i> signal; an ordinary instruction-complete carries
        the unit's own result there instead.
      </p>
      <p>
        So a dispatcher that needs to match a completion to a dispatch matches
        on <b>the source coordinate</b>, and keeps its own outstanding-work table
        keyed on it. One instruction in flight per unit makes that exact; more
        than one needs the unit to put something identifying in
        <code>arg</code>, which is the unit's contract to publish rather than
        the mailbox's.
      </p>
    </Callout>

    <h2 class="doc-h2">The node complex — rv64_mag_pe</h2>

    <p class="doc-p">
      <code>rv64_syscore</code> is the processor. <code>rv64_mag_pe</code> is the
      processor <b>plus the node's memory mover and the transform slot on its
      read-return path</b>, and the distinction matters for every area argument
      made about it: the RV32 and RV64 complexes hold the <i>same</i> three
      things and differ only in the processor. The mover and the slot belong to
      the node, not to whichever CPU sits in it.
    </p>

    <Fig
      caption="The mover's configuration is reachable from two places, and the processor wins: when both pulse in one cycle the processor's address and data are taken. The interlink's config window one level out is the same shape with the priority reversed — there the HOST wins and the processor retries, because that path is a debug one. The transform slot's own config port is tied to zero here and its outputs are unread, so synthesis strips it."
      zoom
    >
      <BlockDiagram :nodes="complex.nodes" :edges="complex.edges" />
    </Fig>

    <h2 class="doc-h2">What is wired at the node, and what is not</h2>

    <Callout kind="trap" title="The parameter is off by default, and the generator does not emit it">
      <p>
        <code>rv64_mag_pe</code> is instantiated inside <code>sysnode</code>
        behind <code>CPU_RV64</code>, and <b>that parameter is 0</b>: a node
        built without asking for it ships the RV32 control complex.
      </p>
      <p>
        <b>The mesh generator emits no value for it at all</b>, so every
        generated ship top elaborates the RV32 branch and there is no way to
        build a ship with the RV64 complex without editing the generator. Every
        RV64 figure on this site comes from a standalone <code>sysnode</code>
        synthesis that sets the parameter directly. The symptom is a silent
        substitution: a build asked for the RV64 node produces the RV32 one,
        elaborates cleanly, and differs only in a LUT count nobody compares.
      </p>
    </Callout>

    <SpecTable
      :cols="wired.cols"
      :rows="wired.rows"
      caption="Read from sysnode.v. A tied-off port is not a decision — it is a wire nobody has run yet, and each of the three at the bottom of this table changes what a runtime can do"
    />

    <Callout kind="rule" title="A control region that answers writes and changes nothing is worse than one that faults">
      <p>
        Every row above marked <i>connected</i> was once a constant, and the
        failure mode was the same each time: the control region decoded the
        range, accepted the store, and drove nothing. <b>A doorbell that reports
        success and rings nowhere is indistinguishable from a peer that is
        slow</b>, and an interrupt line tied low makes <code>mie</code> bit 11
        dead however software programs it.
      </p>
      <p>
        <b>The check is at the instantiation, not in the module.</b> Each of
        these modules was correct in isolation and its bench passed; what was
        missing was one line in the parent. Anything a page describes as a
        capability of a component is a claim about the component
        <i>and</i> about every place it is instantiated.
      </p>
    </Callout>

    <Callout kind="open" title="So, in the RV64 configuration as it stands">
      <p>
        The processor boots, runs, reaches DRAM and staging through MAG, commands
        the mover, dispatches to compute units and consumes their completions,
        rings a doorbell in another mesh, takes an external interrupt from the
        node, and reports to the host.
      </p>
      <p>
        What it still <b>cannot</b> do: reach the transform slot's registers,
        program the mover's fill immediate or gather geometry, or be selected by
        the ship generator. And <b>the evidence stops one level below the
        node</b> — the processor, its privilege model, its translation and its
        mailbox are each proved by directed programs against the complex, and
        there is no whole-node simulation with this processor in it.
      </p>
    </Callout>

    <h2 class="doc-h2">Parameters</h2>

    <p class="doc-p">
      Defaults are what is measured. Nothing here changes behaviour software can
      see except the two window sizes and the two base addresses.
    </p>

    <h3 class="doc-h3">rv64_core</h3>

    <SpecTable :cols="coreParams.cols" :rows="coreParams.rows" />

    <Callout kind="measured" title="Dropping atomics is worth 776 LUT — 13.3 % of the core — at essentially no change in frequency">
      <p>
        A mesh compute unit may take that trade: it has no second writer to race.
        <b>The node processor may not</b>, and the reason is not preference:
        staging is multi-writer but single-reader, which makes it a mailbox
        rather than shared memory. It gives join and release and never mutual
        exclusion or a shared counter, so without the <code>A</code> group the
        machine cannot express a multi-writer location outside DRAM at all.
      </p>
    </Callout>

    <h3 class="doc-h3">rv64_sys_pe</h3>

    <SpecTable :cols="peParams.cols" :rows="peParams.rows" />

    <h3 class="doc-h3">rv64_syscore and rv64_mag_pe</h3>

    <SpecTable :cols="scParams.cols" :rows="scParams.rows" />

    <h2 class="doc-h2">Wrapping one yourself</h2>

    <p class="doc-p">
      A third wrapper is a reasonable thing to want — the core has no fabric
      interface, no memory map and no way to be started, and those are the only
      three things a wrapper owes it. The order below is the order that keeps
      the four failures on this page from being rediscovered.
    </p>

    <ol class="doc-p list-decimal pl-5 space-y-1">
      <li>
        <b>Decide whether your unit can afford to block.</b> If it is
        <i>serviced</i> by the fabric, the shell is the right answer and costs
        756 LUT. If it <i>arbitrates</i> the fabric — dispatching work and
        consuming completions — the shell is a deadlock, because its inbound
        path is gated by a busy line from finite queues and its dispatch shares
        one output port with that same traffic.
      </li>
      <li>
        <b>Make the load path and the start path share an ordering</b>, or
        interlock them explicitly. The host wrapper needs no interlock because
        AXI already orders the image writes ahead of the boot write. The fabric
        wrapper needs one because two queues have no order between them.
      </li>
      <li>
        <b>Write the interlock against the memory, not against the transport.</b>
        “The flit was consumed” is not “the words were written”. Enumerate every
        stage that can still be in flight and require all of them idle.
      </li>
      <li>
        <b>Hold the core in reset until the start pulse</b>, as a second guard
        that does not depend on the first being right.
      </li>
      <li>
        <b>Put every diagnostic outside the reset domain it describes</b>, and
        clear counters on the start pulse rather than on the reset. Both of
        those follow from the run ending with a reset.
      </li>
      <li>
        <b>Register the control region's read path and its select.</b> As a
        combinational mux it sits inside the core's load path — it cost the mesh
        unit 52 MHz against the core alone, and the scratchpad is a one-cycle
        read anyway, so matching it costs nothing.
      </li>
      <li>
        <b>Keep the memory sizes and the link script in one place.</b> Changing
        <span class="chip">IMEM_WORDS</span> or
        <span class="chip">SPAD_WORDS</span> without changing the linker script
        silently truncates the image at the loader's bounds check — a rejected
        transfer is consumed and dropped, not written somewhere and not
        reported.
      </li>
    </ol>

    <Callout kind="open" title="Open questions, and one parameter that does not do what it says">
      <p>
        <b><span class="chip">NODE_BASE</span> and
        <span class="chip">CACHE_LO</span> are not honoured.</b> The decode is
        written as <span class="chip">|pa[39:28]</span> and
        <span class="chip">pa[31]</span> with the bit positions as literals, for
        the timing reason the memory system carries — so changing either
        parameter changes nothing at all. Treat both as documentation and edit
        the tests. Nothing in the build reports the divergence.
      </p>
      <p>
        <b>There is no image read-back in either wrapper.</b> The host window's
        memory selectors are write-only, and reading one returns
        control-register values rather than an error — so a corrupted image
        cannot be diagnosed by reading it, only by its behaviour.
      </p>
      <p>
        <b>The mailbox has no way to name a dispatch it is waiting on.</b> The
        transaction id is hardware's and does not come back on an ordinary
        completion, so matching a completion to a dispatch is done on the source
        coordinate and therefore holds exactly while one instruction is
        outstanding per unit. Nothing in the hardware enforces that, and nothing
        reports when it is broken.
      </p>
      <p>
        <b>The completion queue's depth is not tied to anything.</b> Sixteen is a
        constant; the number of units a dispatcher may have outstanding is a
        software decision made somewhere else, and the only feedback when the two
        disagree is a sticky bit nobody is obliged to read.
      </p>
      <p>
        <b>And there is no whole-node simulation with this complex in it.</b> The
        node- and mesh-level benches exercise the RV32 one; the RV64 evidence
        stops at the complex and its directed programs. So “the node boots a
        runtime, commands the mover and dispatches to a compute unit” is proven
        one level below the node, not at it.
      </p>
    </Callout>

    <h2 class="doc-h2">What integration deliberately does not provide</h2>

    <SpecTable :cols="intAbsent.cols" :rows="intAbsent.rows" />
  </DocPage>
</template>
