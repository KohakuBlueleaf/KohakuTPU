<script setup>
// ===========================================================================
// RV64 system core — the contract.
// Presents docs/arch/cpu/rv64-sys/README.md and architecture.md.
//
// Node figures: one run of scripts/tcl/ooc_sysnode_rv64.tcl 2 —
// sysnode as top, CPU_RV64 = 1, PORTS = 2, xcvu13p-fhgb2104-2L-e,
// Vivado 2024.2, out-of-context SYNTHESIS (not placed, not routed),
// the default `rebuilt` flattening, 3.333 ns request; reports
// build/node_sn64_p2_{util,hier,time}.rpt.
// Processor attribution: rv64_syscore as its own top under
// -flatten_hierarchy none, same part, tool and request.
// Cycle figures: Verilator against independently written C++ models.
// ===========================================================================

const sits = {
  nodes: [
    {
      id: "fab",
      x: 0,
      y: 0,
      w: 13,
      h: 5,
      label: "the fabric",
      sub: "a mesh of routers",
    },
    {
      id: "pe",
      x: 19,
      y: 0,
      w: 15,
      h: 5,
      label: "rv64_sys_pe",
      sub: "core + instruction window + scratchpad · kicked, runs, reports a word",
      accent: true,
    },
    {
      id: "host",
      x: 0,
      y: 9,
      w: 13,
      h: 5,
      label: "the host, over AXI",
      sub: "writes the image, then a boot register",
    },
    {
      id: "sc",
      x: 19,
      y: 9,
      w: 15,
      h: 5,
      label: "rv64_syscore",
      sub: "core + MMU + L1 + dispatch mailbox · boots once, runs forever",
      accent: true,
    },
    {
      id: "mag",
      x: 40,
      y: 9,
      w: 13,
      h: 5,
      label: "MAG",
      sub: "and through it DRAM, staging, other meshes",
    },
    {
      id: "hub",
      x: 57,
      y: 13,
      w: 13,
      h: 5,
      label: "sn_hub, then the mesh",
      sub: "dispatch out, completions in",
      accent: true,
    },
  ],
  edges: [
    { from: "fab:r", to: "pe:l", label: "CU_DATA, CU_INST" },
    { from: "pe:l", to: "fab:r", label: "CU_SIGNAL" },
    { from: "host:r", to: "sc:l" },
    { from: "sc:r", to: "mag:l", label: "the node port" },
    { from: "sc:b", to: "hub:l", label: "the mailbox", accent: true },
  ],
  groups: [
    { x: -1.2, y: -1.4, w: 36.4, h: 7.8, label: "as a mesh compute unit" },
    { x: -1.2, y: 7.6, w: 72.4, h: 11.6, label: "as the system node's processor" },
  ],
};

const twoCores = {
  cols: [
    { key: "k", label: "" },
    { key: "a", label: "RV32 controller PE" },
    { key: "b", label: "RV64 system core" },
  ],
  rows: [
    {
      k: "<b>lifecycle</b>",
      a: "kicked, runs to completion, reports a word",
      b: "boots once, runs until stopped",
      _tone: "good",
    },
    {
      k: "a fault is",
      a: "the completion — the unit halts and says why",
      b: "a trap into a handler",
    },
    {
      k: "privilege",
      a: "none — there are no modes",
      b: "<b>M, S and U</b>, with <code>medeleg</code>/<code>mideleg</code> delegation",
      _tone: "good",
    },
    {
      k: "interrupts",
      a: "none; the unit is never interrupted",
      b: "timer, software and external, at machine <i>and</i> supervisor level",
    },
    {
      k: "CSRs",
      a: "none exist",
      b: "the machine set, the supervisor set, and a free-running <code>mtime</code>",
    },
    {
      k: "arithmetic",
      a: "RV32I plus the multiply half of <code>M</code>. No divide, no float",
      b: "RV64<b>IM</b>: multiply <i>and</i> divide, still no float",
    },
    {
      k: "atomics",
      a: "none — exclusive access is ownership and push",
      b: "the <b>A</b> group, and it is what makes a shared counter expressible",
    },
    {
      k: "address space",
      a: "its own windows",
      b: "the whole 40-bit card address space, through a node port",
    },
    {
      k: "predictor",
      a: "a 32-entry BTB, right for one hot loop",
      b: "BTB + gshare + a return-address stack, for call-dense code",
    },
    {
      k: "what it counts as",
      a: "a compute unit",
      b: "the node's processor, or a compute unit",
    },
  ],
};

const configs = {
  cols: [
    { key: "k", label: "" },
    { key: "pe", label: "rv64_sys_pe — mesh compute unit" },
    { key: "sc", label: "rv64_syscore — the node's processor" },
  ],
  rows: [
    {
      k: "<b>fabric shell</b>",
      pe: "<code>noc_cu_base</code>, plus a loader and a kick machine",
      sc: "<b>none</b> — fused to MAG",
    },
    {
      k: "how it is loaded",
      pe: "<code>CU_DATA</code> flits, then a <code>CU_INST</code> kick",
      sc: "host AXI writes, then a boot register",
    },
    {
      k: "how it stops",
      pe: "a control-region store; the shell sends a <code>CU_SIGNAL</code>",
      sc: "a control-region store; the host reads a status register",
    },
    {
      k: "memory reach",
      pe: "its own instruction window and scratchpad, nothing else",
      sc: "those, plus everything out the node port",
    },
    {
      k: "MMU",
      pe: "none instantiated — pays nothing for one",
      sc: "<code>rv64_mmu</code>, <b>shared by fetch and the data port</b>",
    },
    {
      k: "translation",
      pe: "none. The core is a physical-address machine and the wrapper adds nothing",
      sc: "Sv39, on when <code>satp.MODE == 8</code> and the hart is below machine",
      _tone: "good",
    },
    {
      k: "onto the fabric",
      pe: "<code>noc_cu_base</code> — it is dispatched <i>to</i>",
      sc: "<code>rv64_noc_mbox</code> in the control region — it <b>dispatches</b>, and reads completions out of a queue",
      _tone: "good",
    },
    {
      k: "L1",
      pe: "none",
      sc: "<code>rv64_l1</code>, 64 lines of 32 bytes, write-back",
    },
    {
      k: "atomics",
      pe: "<code>HAS_ATOMIC</code>, and the instantiation leaves it on",
      sc: "required, and on",
    },
    {
      k: "the mover",
      pe: "not attached",
      sc: "attached — <code>mv.go</code> is a store into the control region",
    },
  ],
};

const isa = {
  cols: [
    { key: "g", label: "Group" },
    { key: "s", label: "Status" },
  ],
  rows: [
    {
      g: "<b>RV64I</b>",
      s: "complete, including the <code>W</code> forms — <code>ADDIW</code>, <code>ADDW</code>, <code>SLLW</code>, <code>SRLW</code>, <code>SRAW</code> and their immediate forms",
    },
    {
      g: "<b>RV64M</b>",
      s: "complete: <code>MUL</code>, <code>MULH</code>, <code>MULHSU</code>, <code>MULHU</code>, <code>DIV</code>, <code>DIVU</code>, <code>REM</code>, <code>REMU</code>, and the five <code>W</code> forms. <b>Multiply is 8 cycles, divide 66</b>",
      _tone: "good",
    },
    {
      g: "<b>RV64A</b>",
      s: "complete for both widths: <code>LR</code>/<code>SC</code> and all nine <code>AMO</code> operations. <code>aq</code> and <code>rl</code> are <b>decoded and discarded</b>",
      _tone: "good",
    },
    {
      g: "<b>Zicsr</b>",
      s: "<code>CSRRW</code>/<code>CSRRS</code>/<code>CSRRC</code> and the three immediate forms. <code>funct3 = 100</code> is illegal, as the specification requires",
    },
    {
      g: "<code>FENCE</code>, <code>FENCE.I</code>",
      s: "decoded, execute as <b>NOP</b> — one hart, in-order, one outstanding access, and the instruction window is not writable from the data side",
    },
    {
      g: "<code>WFI</code>",
      s: "decoded, executes as <b>NOP</b>. It does not idle the core",
    },
    {
      g: "<code>MRET</code>",
      s: "redirect to <code>mepc</code>; <code>priv ← MPP</code>, <code>MIE ← MPIE</code>, <code>MPIE ← 1</code>, <code>MPP ← U</code>. <b>Illegal outside machine mode</b>",
    },
    {
      g: "<code>SRET</code>",
      s: "redirect to <code>sepc</code>; <code>priv ← SPP</code>, <code>SIE ← SPIE</code>, <code>SPIE ← 1</code>, <code>SPP ← 0</code>. <b>Illegal in user mode</b>",
      _tone: "good",
    },
    {
      g: "<code>SFENCE.VMA</code>",
      s: "sweeps <b>every</b> TLB entry — the address and ASID operands are ignored. <b>Illegal in user mode.</b> It does not refetch instructions already in the pipeline",
      _tone: "good",
    },
    {
      g: "<code>ECALL</code>, <code>EBREAK</code>",
      s: "trap if a handler is installed, otherwise <b>halt</b>. <code>ECALL</code>'s cause names the mode it came from: 8 from U, 9 from S, 11 from M",
    },
    {
      g: "misaligned load, store or AMO",
      s: "<b>faults</b>, cause 4 or 6. RV64 permits either fixup or fault",
      _tone: "bad",
    },
    {
      g: "<b><code>F</code>, <code>D</code>, <code>Zfh</code> — floating point</b>",
      s: "<b>absent.</b> No <code>f0..f31</code>, no <code>fcsr</code>, no rounding mode",
      _tone: "bad",
    },
    {
      g: "<b><code>C</code> — compressed</b>",
      s: "<b>absent.</b> Every instruction is 4 bytes and the fetch path assumes it",
      _tone: "bad",
    },
    {
      g: "<b><code>S</code>, <code>U</code> privilege</b>",
      s: "<b>present.</b> <code>misa</code> reports <code>I, M, A, S, U</code>; the CSR file holds a 2-bit privilege register, reset to machine, and both return instructions restore it",
      _tone: "good",
    },
    {
      g: "<b>Sv39</b>",
      s: "<b>present and reachable</b> — a hardware three-level walk, a 32-entry TLB, <code>SUM</code> and <code>MXR</code>, and page-fault causes 12, 13 and 15",
      _tone: "good",
    },
    {
      g: "PMP",
      s: "<b>absent.</b> There are no <code>pmpcfg</code> or <code>pmpaddr</code> registers, so physical memory is unprotected however the page tables are written",
      _tone: "bad",
    },
  ],
};

const occupancy = {
  cols: [
    { key: "c", label: "Class", mono: true },
    { key: "o", label: "Occupancy", align: "right", mono: true },
    { key: "l", label: "Latency", align: "right", mono: true },
  ],
  rows: [
    { c: "ALU, shift, LUI, AUIPC", o: "1", l: "1" },
    { c: "load", o: "1", l: "<b>2</b>" },
    { c: "store", o: "1", l: "—" },
    { c: "any CSR instruction", o: "<b>2</b>", l: "2" },
    { c: "lr", o: "<b>3</b>", l: "3" },
    { c: "amo*, sc", o: "<b>4</b>", l: "4" },
    { c: "mul, mulh, mulhsu, mulhu, mulw", o: "<b>8</b>", l: "8" },
    {
      c: "div, divu, rem, remu and the W forms",
      o: "<b>66</b>",
      l: "66",
      _tone: "warn",
    },
  ],
};

/* --- what a reader chooses ------------------------------------------------ */

const knobs = {
  cols: [
    { key: "k", label: "Choice", mono: true },
    { key: "d", label: "Default", mono: true, align: "right" },
    { key: "w", label: "What it decides" },
  ],
  rows: [
    {
      k: "the wrapper",
      d: "—",
      w: "<b>The single decision, and it is not a parameter.</b> <code>rv64_sys_pe</code> is a mesh compute unit — Harvard, local, no path off the unit. <code>rv64_syscore</code> is the node's processor — the same three local regions, larger, plus the whole card through the node port. They are different products, not two settings of one.",
      _tone: "warn",
    },
    {
      k: "CPU_RV64",
      d: "<b>0</b>",
      w: "Whether the node instantiates this core at all. <b>It defaults to off</b>, and the ship generator does not emit it — see the trap above.",
      _tone: "bad",
    },
    {
      k: "HAS_ATOMIC",
      d: "1",
      w: "Whether the <code>A</code> group exists. Worth <b>776 LUT, 13.3 % of the core</b>, at essentially no change in frequency — and <b>not optional for the node processor</b>, because without it the machine cannot construct a multi-writer location outside DRAM at all.",
    },
    {
      k: "IMEM_WORDS",
      d: "8192",
      w: "32-bit words, so 32 KB at the node and 16 KB at the mesh default. <b>Fetch only</b> — the core has no write port to it in either configuration, so there is no self-modifying code and <code>FENCE.I</code> cannot be made meaningful.",
    },
    {
      k: "SPAD_WORDS",
      d: "4096",
      w: "64-bit words, so 32 KB. <code>.rodata</code> is read with loads, so it lands here rather than beside <code>.text</code>.",
    },
    {
      k: "RESET_PC",
      d: "0",
      w: "Where fetch begins. Note that <b>zero is also “no handler installed”</b> for <code>mtvec</code>, which is a different register and a deliberate coincidence: jumping to address 0 on a fault would silently restart the program, so a fault with <code>mtvec</code> still zero halts instead.",
    },
  ],
};

/* --- the CSR bit layouts -------------------------------------------------- */

const mstatusBits = [
  { name: "rsvd", bits: 44, value: "63:20 — not stored" },
  { name: "MXR", bits: 1, value: "19", accent: true },
  { name: "SUM", bits: 1, value: "18", accent: true },
  { name: "rsvd", bits: 5, value: "17:13" },
  { name: "MPP", bits: 2, value: "12:11", accent: true },
  { name: "rsvd", bits: 2, value: "10:9" },
  { name: "SPP", bits: 1, value: "8", accent: true },
  { name: "MPIE", bits: 1, value: "7", accent: true },
  { name: "rsvd", bits: 1, value: "6" },
  { name: "SPIE", bits: 1, value: "5", accent: true },
  { name: "rsvd", bits: 1, value: "4" },
  { name: "MIE", bits: 1, value: "3", accent: true },
  { name: "rsvd", bits: 1, value: "2" },
  { name: "SIE", bits: 1, value: "1", accent: true },
  { name: "rsvd", bits: 1, value: "0" },
];

const mieBits = [
  { name: "rsvd", bits: 52, value: "63:12 — not stored" },
  { name: "MEIE", bits: 1, value: "11 — M external", accent: true },
  { name: "rsvd", bits: 1, value: "10" },
  { name: "SEIE", bits: 1, value: "9 — S external", accent: true },
  { name: "rsvd", bits: 1, value: "8" },
  { name: "MTIE", bits: 1, value: "7 — M timer", accent: true },
  { name: "rsvd", bits: 1, value: "6" },
  { name: "STIE", bits: 1, value: "5 — S timer", accent: true },
  { name: "rsvd", bits: 1, value: "4" },
  { name: "MSIE", bits: 1, value: "3 — M software", accent: true },
  { name: "rsvd", bits: 1, value: "2" },
  { name: "SSIE", bits: 1, value: "1 — S software", accent: true },
  { name: "rsvd", bits: 1, value: "0" },
];

const satpBits = [
  { name: "MODE", bits: 4, value: "63:60 — 0 off, 8 Sv39", accent: true },
  { name: "ASID", bits: 16, value: "59:44 — not implemented, reads 0" },
  { name: "rsvd", bits: 16, value: "43:28 — WARL zero above the card" },
  { name: "PPN", bits: 28, value: "27:0 — the root table", accent: true },
];

const warl = {
  cols: [
    { key: "c", label: "CSR", mono: true },
    { key: "m", label: "Stored mask", mono: true },
    { key: "w", label: "What is not stored" },
  ],
  rows: [
    {
      c: "mstatus",
      m: "0x000C_19AA",
      w: "everything but SIE, MIE, SPIE, MPIE, SPP, MPP, SUM, MXR. No MPRV, no SD, no FS or XS, no UXL — there is no state for them to describe",
    },
    {
      c: "sstatus",
      m: "0x000C_0122",
      w: "a <b>window</b> on <code>mstatus</code>, not a register. A write through it leaves the machine-only bits alone; a read returns the masked value",
      _tone: "good",
    },
    {
      c: "mie, mideleg",
      m: "0x0000_0AAA",
      w: "bits 1, 3, 5, 7, 9, 11 — the six S and M sources. Every other bit reads zero",
    },
    {
      c: "sie, sip",
      m: "through <code>mideleg</code>",
      w: "windows again: <code>sie</code> reads <code>mie &amp; mideleg</code> and writes only the delegated bits",
      _tone: "good",
    },
    {
      c: "medeleg",
      m: "0x0000_FFFF",
      w: "codes 0..15 only. A cause above 15 cannot be delegated because there is no bit to set",
    },
    {
      c: "mtvec, stvec",
      m: "~0x3",
      w: "<b>direct mode only.</b> Bits 1:0 read as zero, so a vectored base is not vectored — it is a handler address with two junk bits removed",
      _tone: "warn",
    },
    { c: "mepc, sepc", m: "~0x1", w: "bit 0 reads zero — IALIGN is 32" },
    {
      c: "mcause, scause",
      m: "0x8000_0000_0000_001F",
      w: "the interrupt bit and a 5-bit code, and nothing between them",
    },
    {
      c: "satp",
      m: "MODE + PPN",
      w: "ASID is not stored, and the PPN is narrowed to the card's 28 bits",
    },
  ],
};

/* --- the trap timing contract --------------------------------------------- */

const trapTiming = {
  rows: [
    {
      name: "trap_take",
      kind: "bit",
      values: [1, 0, 0, 0],
      mark: [0],
    },
    {
      name: "pc",
      kind: "bus",
      values: ["the faulting PC", "tvec", "tvec+4", "tvec+8"],
      mark: [0],
    },
    {
      name: "xepc/xcause/xtval",
      kind: "bus",
      values: ["old", "written", "written", "written"],
      mark: [1],
    },
    {
      name: "mstatus stack",
      kind: "bus",
      values: ["old", "written", "written", "written"],
      mark: [1],
    },
    { name: "priv", kind: "bus", values: ["U", "S", "S", "S"], mark: [1] },
    { name: "settle", kind: "bit", values: [0, 1, 0, 0], mark: [1] },
    { name: "imem_stall", kind: "bit", values: [0, 1, 0, 0] },
    {
      name: "fetch",
      kind: "text",
      values: ["", "held", "tvec issued", "tvec in D"],
    },
  ],
  notes: [
    {
      cycle: 0,
      text: "T — the trap decision. The ONLY thing it does this cycle is redirect the PC and kill what is behind it. Nothing wide is written, because this decision carries the address adder through the misalignment test and, as the enable of ~200 CSR flops, it was the node's critical path.",
      tone: "good",
    },
    {
      cycle: 1,
      text: "T+1 — xepc, xcause, xtval, the mstatus stack bits and priv all land, from registered copies taken at T. Fetch is held for exactly this cycle by the settle line, because whether the new PC is translated depends on priv, and priv is stale until the end of this cycle.",
      tone: "good",
    },
    {
      cycle: 2,
      text: "T+2 — the handler's first fetch is issued, against the privilege and the translation the trap installed.",
      tone: "good",
    },
    {
      text: "The handler's first instruction is at least two cycles away and cannot observe the difference: there is no cycle in which it runs and its own xepc has not landed. Nothing in a handler has to know this, and no software sequence can see it.",
      tone: "good",
    },
  ],
};

const delegation = {
  cols: [
    { key: "r", label: "Register", mono: true },
    { key: "b", label: "Bits", mono: true },
    { key: "w", label: "Meaning" },
  ],
  rows: [
    {
      r: "medeleg",
      b: "[2] [3] [4] [6]",
      w: "illegal instruction · <code>EBREAK</code> · misaligned load · misaligned store or AMO",
    },
    {
      r: "medeleg",
      b: "[8] [9] [11]",
      w: "<code>ECALL</code> from U · from S · from M. The last can never delegate, because delegation requires the hart to be below M",
    },
    {
      r: "medeleg",
      b: "[12] [13] [15]",
      w: "<b>instruction · load · store page fault.</b> A supervisor that handles its own page faults sets these three",
      _tone: "good",
    },
    {
      r: "mideleg",
      b: "[1] [5] [9]",
      w: "supervisor software · timer · external interrupt",
    },
    {
      r: "mideleg",
      b: "[3] [7] [11]",
      w: "machine software · timer · external. <b>Delegating the timer does not work in practice</b> — see the trap below",
      _tone: "warn",
    },
  ],
};

const csrFields = {
  cols: [
    { key: "f", label: "Field", mono: true },
    { key: "w", label: "Width", align: "right", mono: true },
    { key: "p", label: "Position", mono: true },
    { key: "o", label: "Owner" },
  ],
  rows: [
    {
      f: "mstatus.MIE / SIE",
      w: "1 each",
      p: "[3] [1]",
      o: "<b>software, and the trap machinery.</b> A trap into M clears MIE and a trap into S clears SIE; the matching return instruction restores it from the previous-enable bit",
    },
    {
      f: "mstatus.MPIE / SPIE",
      w: "1 each",
      p: "[7] [5]",
      o: "<b>the trap machinery only.</b> A trap saves the enable here; the return sets it to 1. This is the whole of the nesting rule, at both levels",
    },
    {
      f: "mstatus.MPP",
      w: "2",
      p: "[12:11]",
      o: "<b>written by a trap into M and read by <code>MRET</code>.</b> It records the privilege the trap came from, and <code>MRET</code> returns there; the instruction then leaves it at <code>U</code>",
    },
    {
      f: "mstatus.SPP",
      w: "1",
      p: "[8]",
      o: "the same, one level down, and one bit wide because a trap into S can only have come from S or U",
    },
    {
      f: "mstatus.SUM",
      w: "1",
      p: "[18]",
      o: "<b>software.</b> Lets a supervisor load or store touch a <code>U</code> page. <b>It does not relax fetch</b>, so kernel text and user text may not share a page",
      _tone: "warn",
    },
    {
      f: "mstatus.MXR",
      w: "1",
      p: "[19]",
      o: "software. Makes an execute-only page readable",
    },
    {
      f: "mie.[S/M]SIE / TIE / EIE",
      w: "1 each",
      p: "[1][3] [5][7] [9][11]",
      o: "software. Enables only — they gate delivery and do not clear a source",
    },
    {
      f: "mip.MSIP",
      w: "1",
      p: "[3]",
      o: "<b>software MAY set and clear it</b> — and it is OR-ed with the hardware doorbell line, which software cannot clear here",
      _tone: "warn",
    },
    {
      f: "mip.MTIP / MEIP",
      w: "1 each",
      p: "[7] [11]",
      o: "<b>read-only, and not latches.</b> The timer bit is the live comparison <code>mtime &gt;= mtimecmp</code>; the external bit is the live input pin",
      _tone: "warn",
    },
    {
      f: "satp.MODE",
      w: "4",
      p: "[63:60]",
      o: "<b>supervisor software.</b> 8 turns Sv39 on, 0 turns it off; nothing else is decoded. Machine mode never translates whatever it holds",
    },
    {
      f: "satp.PPN",
      w: "28",
      p: "[27:0]",
      o: "supervisor software, narrowed to the card's physical width. <b>The bits above it are WARL zero</b>, so a root address written for a 44-bit machine loses its top bits silently",
      _tone: "warn",
    },
    {
      f: "everything else",
      w: "—",
      p: "—",
      o: "<b>not implemented.</b> Every CSR address not in the table below raises an illegal-instruction trap, which is how software discovers the set",
    },
  ],
};

/* --- clearing an interrupt ------------------------------------------------ */

const timerBroken = {
  rows: [
    { name: "mtime", kind: "bus", values: ["100", "101", "102", "103", "104"] },
    { name: "mtimecmp", kind: "bus", values: ["100", "100", "100", "100", "100"] },
    { name: "mip.MTIP", kind: "bit", values: [1, 1, 1, 1, 1], mark: [3] },
    {
      name: "core",
      kind: "text",
      values: ["trap", "handler", "mret", "trap", "handler"],
    },
  ],
  notes: [
    {
      cycle: 0,
      text: "mtime has reached mtimecmp and the timer interrupt is taken.",
    },
    {
      cycle: 1,
      text: "The handler runs and returns. It never wrote mtimecmp — there is nothing to acknowledge and no bit to clear, because MTIP is not a latch.",
      tone: "bad",
    },
    {
      cycle: 3,
      text: "MTIP is still the comparison mtime >= mtimecmp, and mtime only ever increases. The handler re-enters immediately and forever. The symptom is a program that makes no progress with an interrupt count climbing at the clock rate.",
      tone: "bad",
    },
  ],
};

const doorbellBroken = {
  rows: [
    { name: "doorbell register", kind: "bit", values: [1, 1, 1, 1, 1] },
    { name: "mip.MSIP", kind: "bit", values: [1, 1, 1, 1, 1], mark: [3] },
    {
      name: "handler writes",
      kind: "text",
      values: ["", "mip ← 0", "", "", "mip ← 0"],
    },
    {
      name: "core",
      kind: "text",
      values: ["trap", "handler", "mret", "trap", "handler"],
    },
  ],
  notes: [
    {
      cycle: 1,
      text: "The handler clears mip bit 3, which software is genuinely allowed to write — so the write succeeds and nothing reports an error.",
      tone: "bad",
    },
    {
      cycle: 2,
      text: "But the bit software owns is OR-ed with the hardware doorbell line, and that line is a LEVEL held by the control-region register. Clearing the software half changes nothing while the hardware half is asserted.",
      tone: "bad",
    },
    {
      cycle: 3,
      text: "Re-entry, on a register the handler believes it just cleared. This is the harder of the two to find, because the write it performed was legal and the bit it wrote did change.",
      tone: "bad",
    },
  ],
};

const clearFixed = {
  rows: [
    { name: "doorbell register", kind: "bit", values: [1, 0, 0, 0, 0], mark: [1] },
    { name: "mip.MSIP", kind: "bit", values: [1, 0, 0, 0, 0] },
    {
      name: "handler writes",
      kind: "text",
      values: ["", "the doorbell", "", "", ""],
    },
    {
      name: "core",
      kind: "text",
      values: ["trap", "handler", "mret", "next instr", "next instr"],
    },
  ],
  notes: [
    {
      cycle: 1,
      text: "The handler clears the source — the control-region doorbell register for a software interrupt, or a new mtimecmp for a timer one.",
      tone: "good",
    },
    {
      cycle: 2,
      text: "The level drops, mip follows it, and MRET returns to a machine with nothing pending.",
      tone: "good",
    },
    {
      text: "The rule generalises past this core: an interrupt bit that is a live function of an external condition cannot be acknowledged by writing it. Every source this core has is a level held by something outside the CSR file — a control register, a comparison, a queue, a set of counts — and each is dismissed where it is raised.",
      tone: "good",
    },
  ],
};

/* --- a trapping instruction retires nothing ------------------------------- */

const retireBroken = {
  rows: [
    {
      name: "instruction",
      kind: "bus",
      values: ["lw x5,3(x0)", "—", "handler", "handler", "lw x5,3(x0)"],
    },
    { name: "misaligned", kind: "bit", values: [1, 0, 0, 0, 1] },
    { name: "trap taken", kind: "bit", values: [1, 0, 0, 0, 1] },
    {
      name: "writeback",
      kind: "text",
      values: ["x5 ← garbage", "", "", "", "x5 ← garbage"],
      mark: [0],
    },
    { name: "mepc", kind: "bus", values: [null, "the lw", "the lw", "the lw", null] },
  ],
  notes: [
    {
      cycle: 0,
      text: "The access is misaligned and traps with cause 4 — but its writeback was not suppressed, so x5 has already been written with whatever the memory path returned.",
      tone: "bad",
    },
    {
      cycle: 2,
      text: "The handler examines mtval, fixes the pointer, and returns to mepc — which is the load's own PC, because a handler re-executes the instruction that trapped.",
    },
    {
      cycle: 4,
      text: "The instruction runs a second time. Any handler that used x5 to decide what to do read a value the architecture says was never produced, and a store in the same position would have written memory AND trapped.",
      tone: "bad",
    },
  ],
};

const retireFixed = {
  rows: [
    {
      name: "instruction",
      kind: "bus",
      values: ["lw x5,3(x0)", "—", "handler", "handler", "lw x5,0(x0)"],
    },
    { name: "misaligned", kind: "bit", values: [1, 0, 0, 0, 0] },
    { name: "trap taken", kind: "bit", values: [1, 0, 0, 0, 0] },
    {
      name: "writeback",
      kind: "text",
      values: ["suppressed", "", "", "", "x5 ← the word"],
      mark: [0],
    },
    { name: "mtval", kind: "bus", values: [null, "the address", "the address", "the address", null] },
  ],
  notes: [
    {
      cycle: 0,
      text: "The register writeback and the CSR write are both suppressed. mtval carries the faulting address, which is the only thing the instruction leaves behind.",
      tone: "good",
    },
    {
      cycle: 4,
      text: "Re-execution is therefore idempotent, which is what makes mepc a usable return address at all.",
      tone: "good",
    },
    {
      text: "Three cases make this hold rather than one: a misaligned store emits no byte strobes, an illegal instruction is never a store, and an interrupt is DEFERRED past a load, store or AMO rather than taken before it — which the specification always permits.",
      tone: "good",
    },
  ],
};

const csrs = {
  cols: [
    { key: "a", label: "Address", mono: true, align: "center" },
    { key: "n", label: "CSR", mono: true },
    { key: "x", label: "Notes" },
  ],
  rows: [
    {
      a: "0x100",
      n: "sstatus",
      x: "a <b>window</b> on <code>mstatus</code>: SIE, SPIE, SPP, SUM, MXR",
    },
    { a: "0x104", n: "sie", x: "a window on <code>mie</code>, through <code>mideleg</code>" },
    {
      a: "0x105",
      n: "stvec",
      x: "direct mode only; bits 1:0 read as 0. Non-zero installs the supervisor handler",
    },
    { a: "0x140", n: "sscratch", x: "64 bits" },
    { a: "0x141", n: "sepc", x: "bit 0 reads 0" },
    { a: "0x142", n: "scause", x: "the interrupt bit plus a 5-bit code" },
    { a: "0x143", n: "stval", x: "64 bits" },
    { a: "0x144", n: "sip", x: "a window on <code>mip</code>, through <code>mideleg</code>" },
    {
      a: "<b>0x180</b>",
      n: "<b>satp</b>",
      x: "<code>MODE</code> 63:60 (0 or 8) and <code>PPN</code> 27:0 — a 40-bit physical card. <b>ASID is not implemented and reads 0.</b> The control region keeps a read-only mirror for the host",
      _tone: "good",
    },
    {
      a: "0x300",
      n: "mstatus",
      x: "mask <code>0x000C_19AA</code> — SIE, MIE, SPIE, MPIE, SPP, MPP, SUM, MXR",
    },
    {
      a: "0x301",
      n: "misa",
      x: "read-only: <code>MXL = 2</code>, extensions <b>I, M, A, S, U</b>",
    },
    { a: "0x302", n: "medeleg", x: "codes 0..15; every other bit reads 0" },
    { a: "0x303", n: "mideleg", x: "bits 1, 3, 5, 7, 9, 11" },
    { a: "0x304", n: "mie", x: "bits 1, 3, 5, 7, 9, 11 — software, timer, external at S and M" },
    {
      a: "0x305",
      n: "mtvec",
      x: "direct only; bits 1:0 read as 0. <b>Non-zero installs a handler</b>; still zero means bare metal, and an exception halts instead of trapping",
    },
    { a: "0x340", n: "mscratch", x: "the only machine scratch; there are no shadow registers" },
    { a: "0x341", n: "mepc", x: "the PC of the trapping instruction; bit 0 reads 0" },
    { a: "0x342", n: "mcause", x: "the interrupt bit plus a 5-bit code" },
    {
      a: "0x343",
      n: "mtval",
      x: "the faulting PC on an instruction page fault; the effective address on a misaligned access or a data page fault; otherwise 0",
    },
    {
      a: "0x344",
      n: "mip",
      x: "read-only except bit 3, which software may set and clear — and even that is OR-ed with the hardware doorbell",
    },
    { a: "0xB00 / 0xC00", n: "mcycle / cycle", x: "the same counter" },
    {
      a: "0xB02 / 0xC02",
      n: "minstret / instret",
      x: "the same counter; an explicit write wins over the retire pulse in the same cycle",
    },
    { a: "0xC01", n: "time", x: "the same free-running counter as <code>mtime</code>" },
    {
      a: "<b>0x7C0</b>",
      n: "<b>mtimecmp</b>",
      x: "<b>non-standard.</b> RISC-V puts <code>mtimecmp</code> in a memory-mapped CLINT; this core places it in the machine custom CSR range. There is <b>no <code>stimecmp</code></b>",
      _tone: "warn",
    },
    {
      a: "0xF11–0xF14",
      n: "mvendorid, marchid, mimpid, mhartid",
      x: "all read <b>0</b>",
    },
  ],
};

const causes = {
  cols: [
    { key: "c", label: "Cause", mono: true, align: "center" },
    { key: "r", label: "Raised by" },
  ],
  rows: [
    { c: "2", r: "illegal instruction — a bad encoding, a CSR that does not exist, a CSR read from below its own level, a write to a read-only CSR, or a privileged instruction below its level" },
    { c: "3", r: "<code>EBREAK</code>" },
    { c: "4", r: "misaligned load" },
    { c: "6", r: "misaligned store or AMO" },
    { c: "8 / 9 / 11", r: "<code>ECALL</code> from <b>U</b> / <b>S</b> / <b>M</b>" },
    {
      c: "<b>12</b>",
      r: "<b>instruction page fault.</b> <code>tval</code> is the faulting PC",
      _tone: "good",
    },
    { c: "<b>13</b>", r: "<b>load page fault</b>", _tone: "good" },
    { c: "<b>15</b>", r: "<b>store or AMO page fault</b>", _tone: "good" },
    {
      c: "0x8000…0009 / 000B",
      r: "<b>external</b> interrupt, S / M — highest priority",
    },
    {
      c: "0x8000…0001 / 0003",
      r: "<b>software</b> interrupt, S / M — the doorbell, or <code>mip</code> bit 3",
    },
    { c: "0x8000…0005 / 0007", r: "<b>timer</b> interrupt, S / M — lowest" },
  ],
};

const sources = {
  cols: [
    { key: "l", label: "Line", mono: true },
    { key: "s", label: "Raised by" },
    { key: "c", label: "Cleared by" },
  ],
  rows: [
    {
      l: "software",
      s: "the host's doorbell register in the control region, OR-ed with <code>mip[3]</code>",
      c: "<b>the control-region doorbell</b>. Writing <code>mip[3]</code> clears only the half software owns",
      _tone: "warn",
    },
    {
      l: "timer",
      s: "the live comparison <code>mtime &gt;= mtimecmp</code> — not a latch",
      c: "<b>moving <code>mtimecmp</code></b>, and nothing else",
      _tone: "warn",
    },
    {
      l: "external",
      s: "a <b>mover descriptor that faulted</b>",
      c: "the mover's fault code, at the source",
    },
    {
      l: "external",
      s: "<b>the host asking the node to stop</b>",
      c: "the host",
    },
    {
      l: "external",
      s: "<b>a completion waiting</b> in the dispatch mailbox",
      c: "popping the queue until it is empty",
    },
    {
      l: "external",
      s: "<b>a doorbell rung from another mesh</b> — pending while any inbound count is non-zero",
      c: "<b>clearing the counts</b> through the interlink window at control <code>0xC0</code>",
      _tone: "good",
    },
  ],
};

const priority = {
  cols: [
    { key: "n", label: "", mono: true, align: "center" },
    { key: "w", label: "Checked in this order" },
  ],
  rows: [
    { n: "1", w: "<b>an interrupt</b> — and it is deferred past a load, store or AMO rather than taken before it, which is what keeps re-execution idempotent" },
    { n: "2", w: "instruction page fault" },
    { n: "3", w: "illegal instruction" },
    { n: "4", w: "<code>EBREAK</code>" },
    { n: "5", w: "misaligned load or store" },
    { n: "6", w: "data page fault" },
    { n: "7", w: "<code>ECALL</code>" },
  ],
};

const halts = {
  cols: [
    { key: "c", label: "Halt cause", mono: true, align: "center" },
    { key: "r", label: "Raised by" },
    { key: "p", label: "halt_pc", mono: true },
  ],
  rows: [
    {
      c: "0",
      r: "the external halt input — a control-region exit store, or the host",
      p: "the PC in execute",
      _tone: "good",
    },
    { c: "1", r: "<code>ECALL</code> with no handler", p: "the ECALL's PC" },
    { c: "2", r: "<code>EBREAK</code> with no handler", p: "the EBREAK's PC" },
    {
      c: "3",
      r: "illegal encoding, misaligned access or page fault, with no handler",
      p: "the offending PC",
    },
  ],
};

const mapPe = {
  cols: [
    { key: "r", label: "Region" },
    { key: "b", label: "Base", mono: true },
    { key: "s", label: "Size, default", mono: true },
    { key: "x", label: "Semantics" },
  ],
  rows: [
    {
      r: "instruction window",
      b: "0x0000_0000",
      s: "16 KB",
      x: "<b>fetch only.</b> Not writable by the core and not readable from the data side",
    },
    {
      r: "scratchpad",
      b: "0x0001_0000",
      s: "16 KB",
      x: "ordinary read/write memory, byte-writable, one cycle",
    },
    {
      r: "control region",
      b: "0x0002_0000",
      s: "256 B",
      x: "word registers, some with side effects",
    },
  ],
};

const mapSys = {
  cols: [
    { key: "r", label: "Region" },
    { key: "t", label: "The test on the physical address", mono: true },
    { key: "x", label: "Semantics" },
  ],
  rows: [
    {
      r: "instruction window",
      t: "fetch",
      x: "fetch only — 32 KB, addressed by the <b>translated</b> PC",
    },
    {
      r: "scratchpad",
      t: "pa[39:15] == 2",
      x: "32 KB at 0x0001_0000, byte-writable",
    },
    {
      r: "control region",
      t: "pa[39:8] == 0x200",
      x: "256 B at 0x0002_0000 — the exit word, the console, the doorbell, a read-only <code>satp</code> mirror, the mover's window, the interlink's, and the <b>dispatch mailbox at 0x40</b>",
    },
    {
      r: "node, <b>uncached</b>",
      t: "|pa[39:28] and !pa[31]",
      x: "straight to the node port: staging, node registers, cross-mesh",
    },
    {
      r: "node, <b>cached</b>",
      t: "|pa[39:28] and pa[31]",
      x: "through the write-back L1",
      _tone: "good",
    },
  ],
};

const exit = {
  cols: [
    { key: "k", label: "" },
    { key: "pe", label: "mesh compute unit" },
    { key: "sc", label: "node processor" },
  ],
  rows: [
    {
      k: "<b>the store</b>",
      pe: "<code>CTRL_BASE + 0x00</code>, 32 bits kept",
      sc: "<code>CTRL_BASE + 0x00</code>, 64 bits kept",
    },
    {
      k: "what it does",
      pe: "latches the exit word, halts the core, and the shell sends a <code>CU_SIGNAL</code> carrying it",
      sc: "latches the exit word, halts the core, and sets <code>exited</code> in the host status register",
    },
    {
      k: "the completion's fault flag",
      pe: "set when the halt cause was 2 or 3 — <code>EBREAK</code> or a fault",
      sc: "not applicable; the host reads cause and PC directly",
    },
  ],
};

const cost = {
  cols: [
    { key: "t", label: "Instance", mono: true },
    { key: "l", label: "LUT", align: "right", mono: true },
    { key: "f", label: "FF", align: "right", mono: true },
    { key: "b", label: "BRAM", align: "right", mono: true },
    { key: "u", label: "URAM", align: "right", mono: true },
    { key: "d", label: "DSP", align: "right", mono: true },
  ],
  rows: [
    {
      t: "<b>sysnode</b>, the whole node",
      l: "<b>32,859</b>",
      f: "46,436",
      b: "57.5",
      u: "65",
      d: "47",
    },
    {
      t: "&nbsp;&nbsp;<code>rv64_mag_pe</code> — processor, mover, transform slot",
      l: "16,010",
      f: "16,458",
      b: "21",
      u: "1",
      d: "47",
    },
    {
      t: "&nbsp;&nbsp;&nbsp;&nbsp;<b><code>rv64_syscore</code></b>",
      l: "<b>7,244</b>",
      f: "5,776",
      b: "13",
      u: "1",
      d: "4",
      _tone: "good",
    },
    {
      t: "&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;<code>rv64_core</code>",
      l: "6,169",
      f: "3,205",
      b: "2",
      u: "0",
      d: "4",
    },
  ],
};

const standalone = {
  cols: [
    { key: "i", label: "Instance", mono: true },
    { key: "l", label: "LUT", align: "right", mono: true },
    { key: "f", label: "FF", align: "right", mono: true },
    { key: "w", label: "" },
  ],
  rows: [
    { i: "<b>rv64_syscore</b> (total)", l: "<b>7,334</b>", f: "5,856", w: "" },
    { i: "&nbsp;&nbsp;(wrapper)", l: "464", f: "889", w: "the decode, the two-phase handshake, the control region, the fetch page register" },
    { i: "&nbsp;&nbsp;u_core", l: "5,944", f: "3,218", w: "" },
    { i: "&nbsp;&nbsp;&nbsp;&nbsp;(pipeline, forwarding, trap path)", l: "2,201", f: "1,245", w: "" },
    { i: "&nbsp;&nbsp;&nbsp;&nbsp;u_md", l: "1,366", f: "633", w: "multiply and divide, plus 4 DSP" },
    { i: "&nbsp;&nbsp;&nbsp;&nbsp;u_csr", l: "<b>1,304</b>", f: "987", w: "the CSR file, trap state and privilege" },
    { i: "&nbsp;&nbsp;&nbsp;&nbsp;u_alu", l: "539", f: "0", w: "one adder, one shared shifter" },
    { i: "&nbsp;&nbsp;&nbsp;&nbsp;u_rf", l: "211", f: "130", w: "31 × 64 mirrored, in LUTRAM" },
    { i: "&nbsp;&nbsp;&nbsp;&nbsp;u_bp", l: "205", f: "223", w: "BTB, gshare, return stack" },
    { i: "&nbsp;&nbsp;&nbsp;&nbsp;u_dec", l: "118", f: "0", w: "" },
    { i: "&nbsp;&nbsp;u_l1", l: "349", f: "499", w: "" },
    { i: "&nbsp;&nbsp;u_np", l: "288", f: "725", w: "" },
    { i: "&nbsp;&nbsp;u_mmu", l: "151", f: "217", w: "" },
    { i: "&nbsp;&nbsp;u_mbox", l: "138", f: "308", w: "dispatch out, completions in" },
  ],
};

const cycles = {
  cols: [
    { key: "p", label: "Program", mono: true },
    { key: "c", label: "Cycles", align: "right", mono: true },
    { key: "r", label: "Retired", align: "right", mono: true },
    { key: "i", label: "IPC", align: "right", mono: true },
    { key: "n", label: "" },
  ],
  rows: [
    {
      p: "dhry",
      c: "855,429",
      r: "688,648",
      i: "<b>0.805</b>",
      n: "427.7 cycles per run · <b>1.331 DMIPS/MHz</b>",
      _tone: "good",
    },
    { p: "hello_im", c: "645,778", r: "517,814", i: "0.802", n: "" },
    { p: "csr", c: "577", r: "397", i: "0.688", n: "traps and a timer interrupt" },
    {
      p: "atomics",
      c: "7,507",
      r: "3,266",
      i: "0.435",
      n: "AMO-dense on purpose; each atomic holds execute for 3 or 4 cycles",
    },
  ],
};

const absent = {
  cols: [
    { key: "n", label: "Not present" },
    { key: "w", label: "Why, and what stands in its place" },
  ],
  rows: [
    {
      n: "<b>floating point</b>",
      w: "no <code>f0..f31</code>, no <code>fcsr</code>, no rounding mode. Not “not yet”: there is no float register file to name. Arithmetic in this machine lives in the wide datapaths",
    },
    {
      n: "compressed instructions",
      w: "<code>C</code> is absent; every instruction is 4 bytes and the fetch path assumes it",
    },
    {
      n: "multiple harts",
      w: "one core per wrapper. <code>LR</code>/<code>SC</code> carry a single reservation because there is no second hart to lose it to",
    },
    {
      n: "<b>PMP</b>",
      w: "no <code>pmpcfg</code>, no <code>pmpaddr</code>. Physical memory is unprotected: a page table is the only thing between user code and the card, and machine mode is not translated at all",
      _tone: "bad",
    },
    {
      n: "<b><code>stimecmp</code>, and therefore a delegable timer</b>",
      w: "<code>mtimecmp</code> is a machine CSR and there is no supervisor twin, so a delegated timer interrupt reaches a handler that <b>cannot dismiss it</b>. Preemption is machine-mode work — see the trap below",
      _tone: "bad",
    },
    {
      n: "ASID, and a vectored <code>mtvec</code> / <code>stvec</code>",
      w: "<code>satp.ASID</code> is WARL zero and <code>sfence.vma</code> sweeps the whole TLB; the two-bit <code>MODE</code> field of either vector reads zero, so both are direct-mode only",
    },
    {
      n: "a debug module, and performance counters beyond <code>mcycle</code> / <code>minstret</code>",
      w: "no external debug, no hardware breakpoints, no <code>mhpmcounter</code>. Probing for one gets a clean illegal-instruction trap rather than a zero",
    },
    {
      n: "self-modifying code",
      w: "the instruction window has no write port from the core in either wrapper, so <code>FENCE.I</code> cannot be made meaningful and is a NOP. <code>sfence.vma</code> likewise does not refetch instructions already in the pipeline",
    },
    {
      n: "translation for the mover",
      w: "<b>mover traffic is not translated.</b> A descriptor a program hands the mover names physical addresses, whatever the program's own mapping is",
      _tone: "warn",
    },
    {
      n: "cache maintenance from software",
      w: "the L1's flush and invalidate inputs are tied off in the wrapper, so a program cannot force a dirty line out",
      _tone: "bad",
    },
    {
      n: "an unmapped-address fault",
      w: "neither wrapper faults on a region. A store outside the map is <b>dropped</b> and a load outside it <b>aliases onto the scratchpad</b>. The core faults on a misaligned access and on an illegal encoding, and not on an address",
      _tone: "bad",
    },
    {
      n: "<code>aq</code> / <code>rl</code> ordering",
      w: "decoded and discarded. Safe rather than sloppy: one hart, in-order issue, one outstanding access, so every access is already globally ordered — and it stops being safe the moment a second hart or a non-blocking cache exists",
    },
  ],
};
</script>

<template>
  <DocPage
    title="RV64 system core"
    summary="An in-order RV64IMA + Zicsr core built to host a runtime rather than a kernel loop: three privilege levels with delegation, Sv39 translation over a 40-bit card, traps and interrupts, a branch predictor with a return stack, and a write-back L1. It exists because an operating system never completes, and the framework's compute-unit shell has no word for a program meant never to end."
    domain="cpu"
    status="shipped"
    source="src/kohakuaccel/pe/rv64-sys/ · docs/arch/cpu/rv64-sys/architecture.md"
  >
    <p class="doc-p">
      Its job is to be the thing on the card that <b>runs continuously and
      decides what happens next</b>: walking a dependency graph, replaying a
      recorded command program, servicing an interrupt, keeping time. That is a
      different job from the
      <RouterLink to="/component/rv32pe" class="doc-link">RV32 PE</RouterLink>,
      which runs one kernel at a time inside a compute unit and reports when it
      is done.
    </p>

    <h2 class="doc-h2">What it owns</h2>
    <p class="doc-p">Four things are contract. Everything else about this core may change without notice.</p>
    <div class="grid gap-3 sm:grid-cols-2 my-5">
      <div class="card p-4">
        <div
          class="kt-text-caption font-semibold text-warm-800 dark:text-warm-200 mb-1"
        >
          The instruction set
        </div>
        <p class="kt-text-caption text-warm-500 dark:text-warm-400 leading-6">
          RV64I + M + A + Zicsr, in-order, single issue, with
          <b>M, S and U</b> privilege. An ordinary compiler targets it
          unmodified.
        </p>
      </div>
      <div class="card p-4">
        <div
          class="kt-text-caption font-semibold text-warm-800 dark:text-warm-200 mb-1"
        >
          The trap model
        </div>
        <p class="kt-text-caption text-warm-500 dark:text-warm-400 leading-6">
          Traps and interrupts at two levels, with delegation, a named CSR set
          — and a halt for the program that never installed a handler.
        </p>
      </div>
      <div class="card p-4">
        <div
          class="kt-text-caption font-semibold text-warm-800 dark:text-warm-200 mb-1"
        >
          Two address spaces
        </div>
        <p class="kt-text-caption text-warm-500 dark:text-warm-400 leading-6">
          One per wrapper. The core issues 64-bit addresses and knows what none
          of them mean.
        </p>
      </div>
      <div class="card p-4">
        <div
          class="kt-text-caption font-semibold text-warm-800 dark:text-warm-200 mb-1"
        >
          The exit protocol
        </div>
        <p class="kt-text-caption text-warm-500 dark:text-warm-400 leading-6">
          A store to the control region, not <span class="chip">ECALL</span> —
          so a clean finish does not report as a fault.
        </p>
      </div>
    </div>

    <p class="doc-p">
      The alternative that was rejected is the one already in the tree:
      <b>run the runtime on the RV32 PE inside a compute-unit shell.</b> It
      fails on the lifecycle rather than on the word width. The shell implements
      <i>someone kicks me, I run, I report a 32-bit word</i>, and an operating
      system never completes — there is no word to send and no moment at which
      to send it. Atomics are the second half of the same argument: the node's
      staging store is multi-writer but single-reader, which makes it a mailbox
      rather than shared memory, so it can express join and release and cannot
      express mutual exclusion or a shared counter. Without the
      <span class="chip">A</span> group a scheduler cannot construct a
      multi-writer location outside DRAM at all. Widening the RV32 PE would have
      answered neither.
    </p>

    <p class="doc-p">
      This page is the contract — what software and the surrounding system may
      rely on. How it is built is
      <RouterLink to="/component/rv64sys/microarchitecture" class="doc-link"
        >microarchitecture</RouterLink
      >; the memory path is
      <RouterLink to="/component/rv64sys/memory-system" class="doc-link"
        >memory system</RouterLink
      >; the two wrappers are
      <RouterLink to="/component/rv64sys/integration" class="doc-link"
        >integration</RouterLink
      >.
    </p>

    <Callout kind="note" title="The words this page is built out of">
      <p>
        <b>Mesh</b> — the grid of routers this machine is built on.
        <b>Flit</b> — the 288-bit packet the mesh carries.
        <b>Compute unit</b> — anything that attaches to one fabric port, takes
        instructions one at a time, and signals retirement.
        <b>Kick</b> — the instruction that starts one; <b>completion</b> — the
        flit it sends back when it stops.
      </p>
      <p>
        <b>MAG</b> — the system node's memory access half, the block that turns
        descriptors into DRAM traffic and carries cross-mesh writes.
        <b>Staging</b> — on-chip memory inside MAG that the mover and the
        inter-mesh link share. <b>Mover</b> — the node's descriptor-walking
        memory engine. <b>Doorbell</b> — a single word one agent writes to make
        another notice.
      </p>
    </Callout>

    <h2 class="doc-h2">Where it sits</h2>

    <p class="doc-p">
      Two places, and they are different products rather than two settings of
      one. Both instantiate the same <code>rv64_core</code>, which is a
      <b>physical-address machine</b>: it holds the privilege register and
      <code>satp</code>, because those are architectural state, but it has no
      MMU and never translates an address itself. <b>Translation is a property
      of the system that wraps it</b>, which is why the mesh configuration
      carries none of that machinery and none of its area — the core's
      <code>satp</code> is written and read there and nothing consumes it.
    </p>

    <Fig
      caption="The compute unit is loaded and started over the fabric and answers with one word. The node processor is loaded by the host over AXI, started by a register write, reaches the whole card through MAG, and reaches the mesh through a mailbox in its own control region — and it never answers, because it is not meant to stop."
      zoom
      wide
    >
      <BlockDiagram
        :nodes="sits.nodes"
        :edges="sits.edges"
        :groups="sits.groups"
      />
    </Fig>

    <Callout kind="trap" title="Selecting the node configuration is a parameter that defaults to off">
      <p>
        <code>CPU_RV64</code> is <code>0</code>, so a node built without asking
        for it ships the RV32 control complex. <b>The ship generator emits no
        value for the parameter at all</b>, which means every generated top
        takes the RV32 branch and the RV64 node cannot be built without editing
        the generator. Every RV64 figure on this page comes from a standalone
        <code>sysnode</code> synthesis that sets it directly.
      </p>
      <p>
        The symptom is a silent substitution: a build asked for the RV64 node
        produces the RV32 one, elaborates cleanly, and differs only in a LUT
        count nobody compares.
        <RouterLink to="/component/rv64sys/integration" class="doc-link"
          >What is wired at the node and what is not</RouterLink
        >
        is the exact boundary, port by port.
      </p>
    </Callout>

    <SpecTable
      :cols="knobs.cols"
      :rows="knobs.rows"
      caption="What a reader actually chooses, in the order it matters. The first row is not a parameter and is the largest decision on the page; the second defaults to off"
    />

    <h2 class="doc-h2">Why the machine has two processors</h2>

    <p class="doc-p">
      They are not a big one and a small one. They answer to different
      lifecycles, and the lifecycle is what forces almost every other
      difference.
    </p>

    <SpecTable :cols="twoCores.cols" :rows="twoCores.rows" />

    <Callout kind="rule" title="Two of those rows carry most of the weight">
      <p>
        <b>An operating system never completes.</b> The framework's compute-unit
        shell implements <i>someone kicks me, I run, I report a 32-bit word</i>.
        There is no word for a program that is meant never to end, and no moment
        at which to send it.
      </p>
      <p>
        <b>Atomics are not optional for a scheduler.</b> The node's staging store
        is multi-writer but <b>single-reader</b>, which makes it a mailbox rather
        than shared memory. It can express join and release; it cannot express
        mutual exclusion or a shared counter. Without the <code>A</code> group
        the machine cannot construct a multi-writer location outside DRAM at
        all.
      </p>
    </Callout>

    <h2 class="doc-h2">The two configurations</h2>

    <SpecTable
      :cols="configs.cols"
      :rows="configs.rows"
      caption="rv64_mag_pe is the third top in the tree and is not a third configuration: it is rv64_syscore plus the node's memory mover and its transform slot, both of which belong to the node whatever processor sits in it"
    />

    <h2 class="doc-h2">The instruction set</h2>

    <p class="doc-p">
      <b>RV64I + M + A + Zicsr</b>, machine mode, in-order, single issue.
      Ordinary compilers work unmodified at
      <code>-march=rv64ima_zicsr -mabi=lp64</code>.
    </p>

    <SpecTable :cols="isa.cols" :rows="isa.rows" />

    <Callout kind="note" title="Two decode details are contract, because software can observe them">
      <p>
        <b>A shift amount is 6 bits at RV64 and 5 at the <code>W</code>
        forms</b>, and the bit above the field belongs to the operation, not the
        amount: <code>SRAI</code> differs from <code>SRLI</code> by
        <code>instr[30]</code> alone. <code>SLLIW</code>/<code>SRLIW</code>/<code
          >SRAIW</code
        >
        with <code>instr[25]</code> set are <b>illegal</b>, not shifts by 32
        more.
      </p>
      <p>
        <b><code>x0</code> is never a destination.</b> The decoder clears the
        write for <code>rd = 0</code> rather than the register file dropping it,
        so nothing in the pipeline believes a value was produced.
      </p>
    </Callout>

    <h3 class="doc-h3">Multi-cycle occupancy is architecturally visible</h3>

    <p class="doc-p">
      Not through a result — the ISA hides that — but through
      <code>mcycle</code> and through interrupt latency.
      <b>An interrupt cannot preempt a multi-cycle operation that has
      started</b>, because its operands were latched on entry and abandoning it
      would leave a transaction nobody completes. <b>Worst-case interrupt latency
      is therefore bounded below by a divide.</b>
    </p>

    <SpecTable
      :cols="occupancy.cols"
      :rows="occupancy.rows"
      caption="Occupancy is how many cycles an instruction holds execute, which is what blocks everything behind it. Latency is how many cycles until a consumer can use the result. Any access the wrapper stalls costs 1 + the stall, and the request is issued in execute, so execute is what holds"
    />

    <h2 class="doc-h2">The privilege model</h2>

    <p class="doc-p">
      <b>Machine, supervisor and user.</b> A 2-bit privilege register holds the
      current level — 3 machine, 1 supervisor, 0 user — and reset lands in
      machine. <code>MRET</code> restores it from <code>mstatus.MPP</code>,
      <code>SRET</code> from <code>mstatus.SPP</code>, and a trap saves the
      level it came from into whichever of the two the trap is entering.
      Supervisor exists for one reason: <b>an M+U machine can run user code
      under Sv39, but its kernel is untranslated and has to walk the tables in
      software to touch a user buffer.</b> With S and
      <code>mstatus.SUM</code> the kernel reads a user page directly, which is
      what a <code>copy_to_user</code> needs.
    </p>

    <Callout kind="rule" title="A CSR's own address says who may touch it">
      <p>
        The check is on the encoding rather than on a per-register table:
        <span class="chip">addr[9:8]</span> is the privilege the CSR requires,
        and <span class="chip">addr[11:10] == 11</span> marks it read-only. A
        CSR accessed from too low a level, and a write to a read-only one, are
        both <b>illegal instructions, cause 2</b> — the same answer an address
        that does not exist gets.
      </p>
      <p>
        A privileged instruction below its own level is illegal for the same
        reason and by the same rule: <code>MRET</code> outside machine,
        <code>SRET</code> or <code>SFENCE.VMA</code> in user.
        <b>Not a silent no-op</b> — that is what stops user code returning to
        machine mode or flushing the TLB out from under the kernel.
      </p>
    </Callout>

    <h3 class="doc-h3">Delegation, and the one thing it cannot do</h3>

    <p class="doc-p">
      A trap delegates when the hart is <b>below machine</b> and the cause's bit
      is set in <code>medeleg</code> (for an exception) or <code>mideleg</code>
      (for an interrupt). It then writes the <code>s*</code> registers instead
      of the <code>m*</code> ones and enters supervisor rather than machine. An
      interrupt at level <i>x</i> is taken when the hart runs below <i>x</i>, or
      at <i>x</i> with <i>x</i>'s global enable set; running above <i>x</i>
      never takes it.
    </p>

    <SpecTable :cols="delegation.cols" :rows="delegation.rows" />

    <Callout kind="trap" title="The timer cannot be delegated in practice, however mideleg is written">
      <p>
        <code>mtimecmp</code> is a <b>machine</b> CSR and there is no
        <code>stimecmp</code>. Delegating the timer therefore hands a supervisor
        handler an interrupt it <b>cannot dismiss</b>: the pending bit is the
        live comparison <code>mtime &gt;= mtimecmp</code>, the only way to move
        it is a machine CSR, and the handler's write raises an illegal
        instruction instead.
      </p>
      <p>
        The symptom is the timer re-entry loop below, with an extra step in it —
        the supervisor handler takes an illegal-instruction trap, which is
        <i>also</i> delegable, so a supervisor that delegates both gets a
        handler calling itself. <b>Preemption is machine-mode work here</b>; the
        supervisor handles what it can finish.
      </p>
    </Callout>

    <h3 class="doc-h3">The CSRs that exist</h3>

    <p class="doc-p">
      Only the ones the design names. Architecturally visible state is the
      expensive part of a core, and a specification-complete CSR file would be
      most of one. <b>Every address not in this table raises an
      illegal-instruction trap</b>, which is how software discovers the set.
    </p>

    <SpecTable :cols="csrs.cols" :rows="csrs.rows" />

    <h3 class="doc-h3">WARL is where the area went</h3>

    <p class="doc-p">
      Narrowing a CSR field is architecturally permitted, and the point of doing
      it is that <b>an unimplemented bit is not stored</b>: writes land through a
      mask, so the dead flops leave the register and leave the 64-bit read mux
      with them. Applying the masks below took the CSR file from
      <b>1,476 to 1,267 LUT and 1,222 to 849 FF</b> — one measured difference,
      same part and tool, <code>-flatten_hierarchy none</code>.
    </p>

    <SpecTable
      :cols="warl.cols"
      :rows="warl.rows"
      caption="What each register stores. The three window rows are the ones to read twice: sstatus, sie and sip are VIEWS on the machine registers rather than copies, so there is no second copy to fall out of step and a supervisor write cannot disturb a machine-only bit"
    />

    <h3 class="doc-h3">The registers whose bits software has to get right</h3>

    <Fig
      caption="mstatus, as implemented — mask 0x000C_19AA. Eight fields exist and the other fifty-six bits are not stored: no MPRV, no SD, no FS or XS, no UXL, because there is no state for them to describe."
    >
      <BitField :fields="mstatusBits" />
    </Fig>

    <Fig
      caption="mie, and mip and mideleg have the identical geometry — mask 0x0000_0AAA. Six sources: software, timer and external, at supervisor and at machine. sie and sip are these bits masked by mideleg."
    >
      <BitField :fields="mieBits" />
    </Fig>

    <Fig
      caption="satp. MODE 8 is Sv39 and anything else is off; ASID is not implemented and reads zero, so an address-space switch is a full TLB sweep. The PPN is narrowed to the card's 28 bits, so a root address written for a 44-bit machine loses its top bits without a fault."
    >
      <BitField :fields="satpBits" />
    </Fig>

    <SpecTable
      :cols="csrFields.cols"
      :rows="csrFields.rows"
      caption="The owner column is the one to read before writing a handler. Two of these fields are read-only and are not latches, which is what the traces below are about; SUM is the one whose scope is narrower than it looks"
    />

    <Callout kind="rule" title="mcycle, mtime and minstret are free-running and nothing clears them">
      <p>
        They keep counting across a halt. That is deliberate, and it is the
        difference from the RV32 PE, whose cycle counter resets on every kick and
        stops while halted: <b>a runtime that idles by halting must still be able
        to tell how long it was idle.</b>
      </p>
    </Callout>

    <h3 class="doc-h3">Traps and interrupts</h3>

    <p class="doc-p">
      A trap is taken <b>only at an instruction boundary</b>, which here means:
      the instruction in execute is valid, the core is not halted, no memory
      access is outstanding, and no multiply, divide or atomic is mid-sequence.
    </p>

    <SpecTable
      :cols="causes.cols"
      :rows="causes.rows"
      caption="Interrupt priority within a level is external, then software, then timer — the order the privileged specification fixes"
    />

    <SpecTable
      :cols="priority.cols"
      :rows="priority.rows"
      caption="And the order between a trap's own candidates. An instruction that faulted in fetch cannot be decoded, so its page fault outranks anything the decoder would have said about it"
    />

    <h3 class="doc-h3">The six things that raise an interrupt</h3>

    <p class="doc-p">
      Three cause codes, six sources, and <b>four of them share the external
      line</b>. A handler on that line has to ask what happened before it can
      act, and every one of them is a level that stays asserted until its own
      source is dealt with.
    </p>

    <SpecTable
      :cols="sources.cols"
      :rows="sources.rows"
      caption="The right-hand column is the whole content of this section: no source on it is cleared by writing mip, and two of the four external ones are cleared through a window in the control region rather than through a CSR at all"
    />

    <h3 class="doc-h3">The trap timing contract</h3>

    <p class="doc-p">
      In the cycle a trap or a return is taken, the core <b>redirects the PC and
      does nothing else</b>. Every other side effect —
      <code>xepc</code>, <code>xcause</code>, <code>xtval</code>, the
      <code>mstatus</code> stack bits and the privilege register — lands
      <b>one cycle later</b>, from registered copies. The wrapper holds
      instruction fetch for that one cycle, because whether the new PC is
      translated depends on a privilege level that is still stale.
    </p>

    <Callout kind="rule" title="The reason is timing, and the consequence for software is none">
      <p>
        The trap decision carries the effective-address adder, through the
        misalignment test — and as the <i>enable</i> of roughly two hundred CSR
        flip-flops it was the node's critical path. Splitting it makes that
        decision the enable of nothing wider than a flag.
      </p>
      <p>
        <b>A handler cannot observe the split.</b> Its first instruction is at
        least two cycles away, so there is no cycle in which handler code runs
        and its own <code>xepc</code> has not landed. Nothing in a handler has
        to account for this; it is here because a reader designing the same
        machine will meet the same critical path.
      </p>
    </Callout>

    <WaveTrace
      :rows="trapTiming.rows"
      :notes="trapTiming.notes"
      variant="fixed"
      label="T redirects · T+1 the state lands and fetch is held · T+2 the handler is fetched"
    />

    <Callout kind="note" title="Two more things are a cycle late, for the same reason">
      <p>
        <code>minstret</code> counts a <b>registered</b> retire pulse, so it is
        one cycle behind the instruction it counts — a count one cycle late is
        still a count, and the alternative put the address adder into the
        counter's enable.
      </p>
      <p>
        <code>sfence.vma</code> retires the fetch page register <b>one cycle
        after the fence itself retires</b>, and fetch is held across that cycle
        so no instruction is fetched through the stale page. An invalidation may
        be spurious; it may never be missed — which is why the fence is not
        qualified by <i>and this instruction did not also trap</i>. Qualifying
        it put the whole trap cone into the page register's clock enable, 21
        logic levels, and the worst it saves is one re-walk.
      </p>
    </Callout>

    <Callout kind="rule" title="Three properties are contract rather than detail">
      <p>
        <b>A trapping instruction retires nothing.</b> Its register writeback and
        its CSR write are both suppressed, because the handler re-executes it
        from <code>mepc</code>. A store cannot both write memory and trap: a
        misaligned store emits no byte strobes, an illegal instruction is not a
        store, and an interrupt is <b>deferred past a load, store or AMO</b>
        rather than taken before it — which the specification always permits.
      </p>
      <p>
        <b>The timer interrupt has no acknowledge.</b> It is the comparison
        <code>mtime &gt;= mtimecmp</code>, not a latch. A handler that does not
        move <code>mtimecmp</code> re-enters forever.
      </p>
      <p>
        <b>The doorbell is a level, not an edge.</b> The software interrupt line
        reads as <code>mip</code> bit 3 together with the software-writable bit,
        so a handler clears it at the source — the control-region doorbell
        register — not by writing <code>mip</code>.
      </p>
    </Callout>

    <WaveTrace
      :rows="retireBroken.rows"
      :notes="retireBroken.notes"
      variant="broken"
      label="A trap that does not suppress its own writeback — re-execution stops being idempotent"
    />

    <WaveTrace
      :rows="retireFixed.rows"
      :notes="retireFixed.notes"
      variant="fixed"
      label="Writeback and CSR write both suppressed — mepc becomes a usable return address"
    />

    <h3 class="doc-h3">Clearing an interrupt, and the two ways it does not work</h3>

    <Callout kind="rule" title="An interrupt MUST be cleared at its source, never through mip">
      <p>
        Only one bit of <code>mip</code> is writable at all, and even that one is
        <b>OR-ed with a hardware level software cannot reach</b>. The other two
        are live functions of an external condition. A handler <b>MUST</b> clear
        the condition — a new <code>mtimecmp</code>, a write to the
        control-region doorbell register, a pop of the completion queue, or a
        clear of the inbound doorbell counts — and <b>MUST NOT</b> expect
        writing <code>mip</code> to end an interrupt.
      </p>
      <p>
        The external line is the one to be careful with, because <b>four sources
        share it</b> and clearing one leaves the others asserted. A handler that
        returns having serviced only the source it expected re-enters
        immediately, which looks exactly like the timer loop below.
      </p>
    </Callout>

    <WaveTrace
      :rows="timerBroken.rows"
      :notes="timerBroken.notes"
      variant="broken"
      label="A timer handler that returns without moving mtimecmp"
    />

    <WaveTrace
      :rows="doorbellBroken.rows"
      :notes="doorbellBroken.notes"
      variant="broken"
      label="A doorbell handler that clears mip bit 3 — a legal write that changes nothing"
    />

    <WaveTrace
      :rows="clearFixed.rows"
      :notes="clearFixed.notes"
      variant="fixed"
      label="Clearing the source — the level drops and mip follows it"
    />

    <Callout
      kind="trap"
      title="Both re-entry loops look like a hung program, and one of them looks like working code"
    >
      <p>
        The timer case is at least obviously circular once you know
        <span class="chip">MTIP</span> is a comparison. The doorbell case is
        worse: the handler performs a write that is architecturally legal, to a
        bit that genuinely changes, and re-enters anyway. Nothing in a trace of
        the CSR file shows the mistake — the evidence is one level away, in a
        control register the handler never touched.
      </p>
      <p>
        The symptom for both is a program making no progress with an interrupt
        count climbing at the clock rate, and
        <span class="chip">minstret</span> advancing only through the handler's
        own instructions. <b>Compare <span class="chip">minstret</span> against
        <span class="chip">mcycle</span> before suspecting the fabric</b>; both
        keep counting across a halt, which is exactly what makes them usable
        here.
      </p>
    </Callout>

    <h3 class="doc-h3">No handler installed means halt</h3>

    <p class="doc-p">
      A vector still zero is a program that never installed a handler, and
      jumping to address 0 would silently restart it. So an exception with no
      vector installed <b>halts the core and reports a cause</b> instead of
      trapping. Once the vector is written non-zero, exceptions and interrupts
      trap normally. The test is on the <b>vector this trap would take</b> —
      <code>stvec</code> if the cause delegates, <code>mtvec</code> otherwise —
      so a supervisor that has installed nothing does not silently jump to zero
      either.
    </p>

    <Callout kind="note" title="“Installed” is a property of the write, not of the trap">
      <p>
        Two flags record whether each vector has ever been written non-zero, and
        they are set by the CSR write rather than tested at the trap. Testing
        <span class="chip">tvec != 0</span> in the trap decision puts a 64-bit
        compare — three carry chains — <i>downstream</i> of the delegation mux,
        inside the path that was already the node's critical one. The flags are
        also the reason the vectors themselves are not reset: they are data,
        written before they are read, and 640 bits of register stay out of a
        control set.
      </p>
    </Callout>

    <SpecTable
      :cols="halts.cols"
      :rows="halts.rows"
      caption="A halt stops fetch, decode, execute and writeback. It is not a trap: nothing is saved and there is no way to resume except a reset"
    />

    <h2 class="doc-h2">The two address spaces</h2>

    <p class="doc-p">
      The core issues 64-bit addresses. What they mean is the wrapper's, and the
      two wrappers answer differently.
    </p>

    <h3 class="doc-h3">As a mesh compute unit</h3>

    <p class="doc-p">
      Harvard and local. <b>A load or store reaches the scratchpad or the control
      region and nothing else</b>; there is no path off the unit. Because
      <code>.rodata</code> is read with loads, it must be linked into the
      scratchpad rather than beside <code>.text</code>.
    </p>

    <SpecTable :cols="mapPe.cols" :rows="mapPe.rows" />

    <h3 class="doc-h3">As the node's processor</h3>

    <p class="doc-p">
      The same three local regions, larger, plus the whole card address space out
      the node port. The card is a <b>40-bit</b> machine.
    </p>

    <SpecTable :cols="mapSys.cols" :rows="mapSys.rows" />

    <Callout kind="trap" title="Read the cached test literally">
      <p>
        It is <code>pa[31]</code>, a single bit — <b>not</b> “at or above 2 GB”.
        The decode is bit tests rather than magnitude compares because it sits in
        the pipeline's stall path, and a 40-bit comparison there cost frequency
        across the whole core.
      </p>
      <p>
        So <b>an address at 4 GB with bit 31 clear is uncached</b>, and so is
        anything in the aperture. Lay a program's cached working set out
        accordingly.
      </p>
    </Callout>

    <h2 class="doc-h2">Program exit is a store</h2>

    <p class="doc-p">
      <b>The terminator is a store to the control region, not
      <code>ECALL</code>.</b>
      <code>ECALL</code> has to remain a call — that is the point of having a
      trap model at all — and the framework's halt-and-report completion cannot
      move, so the terminator moved instead. The core carries an external halt
      input for it, and a store-driven exit reports <b>cause 0</b>: a clean
      finish, not a fault.
    </p>

    <SpecTable
      :cols="exit.cols"
      :rows="exit.rows"
      caption="If EBREAK were the exit, every clean finish would report as a fault, because EBREAK's cause is a debug cause and the shell maps causes 2 and 3 to a fault flag. Keeping the two separate is why the store exists. The exit word's meaning is software's"
    />

    <h2 class="doc-h2">What it costs</h2>

    <Callout kind="measured" title="Where these rows come from">
      <p>
        One run of <code>scripts/tcl/ooc_sysnode_rv64.tcl 2</code>:
        <code>sysnode</code> as the top with <code>CPU_RV64 = 1</code>,
        <code>PORTS = 2</code>, <code>STAGE = 1</code>,
        <code>ILINK = 1</code>, <code>STAGE_AT_PORT = 1</code>,
        <code>PE_IMEM = 8192</code>, <code>PE_SPAD = 4096</code>,
        <code>PE_L1_LINES = 64</code>. <b>Out-of-context synthesis — not placed
        and not routed</b> — on <code>xcvu13p-fhgb2104-2L-e</code> under Vivado
        2024.2 at a <b>3.333 ns (300 MHz) request</b>, with the default
        <i>rebuilt</i> flattening the ship flow uses. BRAM is in tiles.
      </p>
      <p>
        Figures are CLB LUT <i>sites</i>, and the flat and hierarchical reports
        of this run agree on <b>32,859</b> for the whole node — so every level
        below reconciles against the level above.
      </p>
      <p>
        <b>300 MHz is met in out-of-context synthesis</b>: worst slack
        <b>+0.039 ns</b> at the 3.333 ns request, with <b>0 failing
        endpoints</b>. That is the claim and its limits are part of it —
        <b>this is not closed timing</b>. The design has not been placed or
        routed, synthesis slack is optimistic (elsewhere in this project one
        module lost 0.740 ns from synthesis to routing), and there is no
        measurement from silicon. <b>No frequency above 300 MHz follows from
        it either.</b>
      </p>
    </Callout>

    <SpecTable
      :cols="cost.cols"
      :rows="cost.rows"
      caption="The node, and the processor inside it. Each level sums to the one above: the node's three top-level instances are 16,010 + 16,335 + 514 = 32,859, and the processor's are 252 wrapper + 6,169 core + 1 window + 501 L1 + 76 mailbox + 103 MMU + 142 node port = 7,244. Hierarchical rows under a flattening flow are attributions rather than measurements, but the totals are exact"
    />

    <Callout kind="measured" title="The processor holds the node's worst path, and it passes">
      <p>
        With nothing failing, the binding path is the slowest one that still
        makes it: the core's writeback register into its halt-cause register,
        <b>12 logic levels at +0.039 ns</b>.
      </p>
      <p class="font-mono kt-text-caption">
        g_rv64.u_pe/u_cpu/u_core/wb_val_reg[1]/C →<br />
        g_rv64.u_pe/u_cpu/u_core/halt_cause_reg[1]/D
      </p>
      <p>
        The <b>last</b> cone to bind was the memory mover's: its
        <code>fifo_room</code> calculation — an add and a compare — feeding the
        command FIFO's write enable, at −0.081 ns with all 123 failing endpoints
        in that one region. <b>Registering the room limit closed it</b>, and the
        node now <b>meets 300 MHz in out-of-context synthesis at +0.039 ns with
        0 failing endpoints</b>.
      </p>
      <p>
        <b>One endpoint rarely names a critical region</b>, and that cone was
        the proof: fixing the endpoint the tool named would have moved it, while
        the root was the add-and-compare six gates upstream.
      </p>
      <p>
        The design also has <b>2,141 LUT of headroom</b> against the 35,000-LUT
        budget it was built to.
      </p>
    </Callout>

    <SpecTable
      :cols="standalone.cols"
      :rows="standalone.rows"
      caption="The processor as its own top under -flatten_hierarchy none, where a row is a true attribution: rv64_syscore synthesised alone, MEM_PRIM = block, same part, tool and 3.333 ns request. Every level sums exactly. Do not subtract these from the node table above — they are a different measurement context, not a before and an after"
    />

    <Callout kind="trap" title="A module does not have a LUT count — it has one per measurement context">
      <p>
        Five things vary independently, and changing any one changes the answer:
        <b>what was the top</b> (standalone, or a sub-hierarchy inside a larger
        synthesis); <b>the flatten mode</b>; <b>which report you read</b> — CLB
        LUT <i>sites</i> or raw LUT <i>primitives</i>; <b>the timing request</b>;
        and <b>the RTL vintage</b>. The register file is the sharpest case on
        this page: <b>211 LUT under <code>none</code> and 1,555 in the node's
        rebuilt rows</b>, for one 4 Kbit array, because the operand path around
        it was re-parented onto the boundary that survived.
      </p>
      <p>
        <b>A figure from a different measurement context is not a newer or an
        older number. It is a different number.</b> Cite the context with the
        figure; never present one as superseding the other, and never subtract
        across them. The one exception is two runs of the same configuration in
        the same context at different RTL vintages — and there the earlier figure
        genuinely is superseded.
      </p>
      <p>
        Two such figures used to appear here and have been withdrawn:
        <b>the standalone <i>rebuilt</i> syntheses of
        <code>rv64_syscore</code> and <code>rv64_sys_pe</code> have not been
        re-run since privilege, Sv39 and the mailbox were built</b>, so their
        LUT counts and their frequencies describe an earlier RTL and are not
        quoted.
      </p>
    </Callout>

    <h3 class="doc-h3">Cycles, and what they were measured on</h3>

    <p class="doc-p">
      Verilator driving a C++ harness — no Verilog testbench — with
      <code>rv64_core</code> on a flat memory model, so every access answers in
      one cycle and the numbers are the pipeline's rather than the memory
      system's. Programs built with <code>riscv64-unknown-elf-gcc</code> at
      <code>-march=rv64ima_zicsr -mabi=lp64 -O2</code>. Never quote a LUT or an
      Fmax from a simulator, and never quote a cycle count from synthesis.
    </p>

    <Callout kind="measured" title="These four rows predate the trap-timing change">
      <p>
        They were taken before the trap's state writes moved a cycle after the
        redirect, and <b>they have not been re-run.</b> The effect is bounded
        and computable: a trap or a return now holds fetch for one extra cycle,
        so a program's count moves by <b>one cycle per trap taken</b> and by
        nothing else. Three of the four rows take no traps at all; the
        <code>csr</code> row does.
      </p>
      <p>
        <b>No cycle figure has been taken with translation on.</b> Sv39 is
        exercised by directed programs — three-level walks in hardware, a
        delegated load page fault, user code preempted by the timer — so it
        works, but nobody has run one program with and without translation and
        counted the difference.
      </p>
    </Callout>

    <SpecTable
      :cols="cycles.cols"
      :rows="cycles.rows"
      caption="The retire pulse is gated on not-stalled, so it counts instructions and not occupancy — the execute valid bit stays high for all 66 cycles of a divide. minstret counts the same pulse, so software sees the same number the harness does"
    />

    <Callout kind="measured" title="1.331 DMIPS/MHz, and what that figure does not claim">
      <p>
        Dhrystone 2.1, <code>-march=rv64ima -O2 -DRUNS=2000</code>, every
        reference result correct: 855,429 cycles over 2,000 runs is
        <b>427.7 cycles per Dhrystone</b>. Running the same program through the
        mesh unit's fabric endpoint costs <b>27 cycles out of 855,429</b>, at
        identical IPC.
      </p>
      <p>
        DMIPS/MHz is the denominator a LUT count needs — without one, an area
        figure says nothing. It is a <i>per-megahertz</i> figure and therefore
        carries <b>no frequency claim</b>: multiplying it by the 300 MHz the
        node meets in synthesis would be multiplying a measured number by an
        unrouted one.
      </p>
    </Callout>

    <h2 class="doc-h2">Bringing one up</h2>

    <p class="doc-p">
      The order below is the order the failures actually arrive in. Steps 1 to 3
      are the ones a reader who knows RISC-V will skip and then spend a day on,
      because each of them is a place this core is legal and unusual at the same
      time.
    </p>

    <ol class="doc-p list-decimal pl-5 space-y-1">
      <li>
        <b>Pick the wrapper first</b>, because it decides what an address means
        and there is no third option. Then check
        <span class="chip">CPU_RV64</span> if you are at the node — it defaults
        to off.
      </li>
      <li>
        <b>Link <code>.rodata</code> into the scratchpad, not beside
        <code>.text</code>.</b> The instruction window is fetch-only and is not
        readable from the data side, so a constant table linked next to code is
        not merely slow to reach — it cannot be read at all.
      </li>
      <li>
        <b>Install <code>mtvec</code> before doing anything that can fault</b>,
        and write a 4-byte-aligned base. The two-bit
        <span class="chip">MODE</span> field is not decoded, so a vectored
        <code>mtvec</code> is not vectored — it is a handler address with two
        junk bits removed. Until <code>mtvec</code> is non-zero, every exception
        halts the core instead of trapping.
        <b>Install <code>stvec</code> before writing <code>medeleg</code></b>,
        for the same reason one level down.
      </li>
      <li>
        <b>Decide what the supervisor handles before you delegate it.</b>
        <span class="chip">medeleg</span> and
        <span class="chip">mideleg</span> only take effect below machine mode,
        so nothing changes until the first <code>MRET</code> into supervisor —
        and the timer is the one interrupt a supervisor cannot dismiss, because
        <code>mtimecmp</code> is a machine CSR and there is no
        <code>stimecmp</code>.
      </li>
      <li>
        <b>Page-align <code>.utext</code>.</b> <span class="chip">SUM</span>
        relaxes supervisor <i>loads and stores</i> against a user page and not
        fetch, so kernel text and user text cannot share a page. Then turn Sv39
        on by writing <code>satp</code> — remembering that the root PPN is
        narrowed to the card's 28 bits, and that machine mode stays untranslated
        whatever you write.
      </li>
      <li>
        <b>Discover the CSR set by trapping on it.</b> Every address outside the
        table above raises an illegal-instruction trap. That is the intended
        mechanism, and it means a runtime probing for
        <span class="chip">mhpmcounter3</span> gets a clean answer rather than a
        zero.
      </li>
      <li>
        <b>Write each interrupt handler against its source, not against
        <code>mip</code></b> — a new <code>mtimecmp</code> for the timer, the
        control-region register for the doorbell. Then check that the handler
        returns through <code>MRET</code> so <code>MIE</code> is restored;
        nothing else restores it.
      </li>
      <li>
        <b>Budget interrupt latency from the longest multi-cycle operation, not
        from the pipeline depth.</b> An interrupt cannot preempt an operation
        that has started, so the worst case is bounded below by a divide — 66
        cycles — and an interrupt is additionally deferred past any load, store
        or atomic.
      </li>
      <li>
        <b>Exit with a store to the control region.</b> Not
        <code>ECALL</code> and not <code>EBREAK</code>: both of those report as
        faults, because the shell maps causes 2 and 3 to a fault flag. The store
        reports cause 0.
      </li>
      <li>
        <b>Then read the ordering rules before the first peer talks to you</b> —
        they live with the
        <RouterLink to="/component/rv64sys/memory-system" class="doc-link"
          >memory system</RouterLink
        >, because every one of them is a property of the memory path rather
        than of the pipeline, and one of them is not discharged.
      </li>
    </ol>

    <Callout kind="open" title="Open questions the contract does not answer">
      <p>
        <b>There is no unmapped-address fault in either wrapper.</b> A store
        outside the map is dropped and a load outside it aliases onto the
        scratchpad, because the scratchpad's index is the low address bits and
        the return path defaults to it. So a wild pointer is not a trap — it is
        a plausible value. Nothing in the contract tells a runtime it has one.
      </p>
      <p>
        <b>The cached range publishes no ordering guarantee at all</b>, and the
        cache maintenance that would let it is not reachable from software. That
        is a gap rather than a decision, and it is the one thing on this page a
        reader should not design around.
      </p>
      <p>
        <b>There is no PMP</b>, so physical memory has no protection at all. A
        page table is the only thing between user code and the card, machine
        mode is never translated, and the mover's traffic is not translated
        either — a descriptor a user program can persuade the runtime to issue
        names physical addresses. <b>Isolation here stops at the page table.</b>
      </p>
      <p>
        <b>And the whole-node evidence stops one level below the node.</b> The
        processor, its privilege model, its translation and its mailbox are each
        proved by directed programs against the complex; there is no whole-node
        simulation with this processor in it. “The node boots a runtime,
        commands the mover and dispatches to a compute unit” is proven in
        pieces, not end to end.
      </p>
    </Callout>

    <h2 class="doc-h2">What it deliberately does not do</h2>

    <SpecTable :cols="absent.cols" :rows="absent.rows" />

    <Callout kind="note" title="What this core does not own">
      <p>
        The flit, the link, the router and the port handshake belong to the
        <RouterLink to="/framework/noc" class="doc-link">mesh</RouterLink>;
        descriptor encoding, write slots, response tagging and the mover's
        command set to the
        <RouterLink to="/framework/sysnode" class="doc-link">system node</RouterLink>;
        where the core lands on the die and at what clock to the
        <RouterLink to="/framework/physical" class="doc-link">floorplan</RouterLink>.
        The 40-bit card address map, and what the aperture bit selects, is not
        this core's to state either.
      </p>
    </Callout>
  </DocPage>
</template>
