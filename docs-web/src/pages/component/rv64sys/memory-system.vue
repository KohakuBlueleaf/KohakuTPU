<script setup>
// ===========================================================================
// RV64 system core — the memory system.
// Presents docs/arch/cpu/rv64-sys/memory-system.md.
//
// In-context rows come from one run of scripts/tcl/ooc_sysnode_rv64.tcl 2
// (sysnode as top, CPU_RV64 = 1, PORTS = 2), xcvu13p-fhgb2104-2L-e,
// Vivado 2024.2, out-of-context SYNTHESIS, 3.333 ns, reports
// build/node_sn64_p2_{util,hier,time}.rpt. Standalone rows are rv64_syscore
// as its own top under -flatten_hierarchy none, same part and tool.
// ===========================================================================

/* --- the one MMU, and its two requesters ---------------------------------- */

const shared = {
  nodes: [
    {
      id: "fetch",
      x: 0,
      y: 0,
      w: 12,
      h: 6,
      label: "instruction fetch",
      sub: "asks only when the page changes",
    },
    {
      id: "data",
      x: 0,
      y: 9,
      w: 12,
      h: 6,
      label: "the data port",
      sub: "dmem_req · decode-only, no address",
    },
    {
      id: "mux",
      x: 18,
      y: 2.5,
      w: 10,
      h: 10,
      label: "the port mux",
      sub: "data wins · a stalled fetch issues no data access",
      accent: true,
    },
    {
      id: "tlb",
      x: 34,
      y: 0,
      w: 12,
      h: 6.5,
      label: "the TLB",
      sub: "32 × 57 bits · one block RAM · swept by sfence",
    },
    {
      id: "walk",
      x: 34,
      y: 17,
      w: 12,
      h: 6.5,
      label: "the walker",
      sub: "w_vpn latches the request it serves",
    },
    {
      id: "pg",
      x: 52,
      y: 0,
      w: 12,
      h: 6.5,
      label: "the fetch page register",
      sub: "if_vpn · if_ppn · if_bad",
      accent: true,
    },
    {
      id: "dec",
      x: 52,
      y: 9,
      w: 12,
      h: 6.5,
      label: "the range decode",
      sub: "bit tests on the 40-bit pa",
    },
    {
      id: "imem",
      x: 70,
      y: 0,
      w: 12,
      h: 5,
      label: "instruction window",
      sub: "addressed by the translated PC",
    },
    {
      id: "l1",
      x: 70,
      y: 8,
      w: 12,
      h: 5,
      label: "rv64_l1",
      sub: "64 lines of 32 bytes",
      accent: true,
    },
    {
      id: "np",
      x: 70,
      y: 16,
      w: 12,
      h: 5.5,
      label: "rv64_nport",
      sub: "one AXI master onto MAG",
    },
  ],
  edges: [
    { from: "fetch:r", to: "mux:l" },
    { from: "data:r", to: "mux:l" },
    { from: "mux:r", to: "tlb:l", label: "va" },
    { from: "tlb:b", to: "walk:t", label: "miss" },
    { from: "tlb:r", to: "pg:l", label: "ppn", accent: true },
    { from: "tlb:r", to: "dec:l", label: "pa" },
    { from: "pg:r", to: "imem:l", label: "fetch pa", accent: true },
    { from: "dec:r", to: "l1:l", label: "cached" },
    { from: "dec:r", to: "np:l", label: "uncached" },
    { from: "walk:r", to: "np:l", label: "PTE read" },
    { from: "l1:b", to: "np:t" },
  ],
};

const ownership = {
  cols: [
    { key: "r", label: "The rule" },
    { key: "s", label: "What it stops" },
  ],
  rows: [
    {
      r: "<b>The walk latches the request it serves.</b> <code>w_vpn</code>, <code>w_fetch</code> and <code>w_store</code> are copied when the walk starts and are what the walk reads afterwards.",
      s: "The registered request follows whatever is on the port <i>now</i>. A data access arriving mid-walk switched the port, and a fetch walk finished with the data address's VPN slices and installed that hybrid as a translation.",
      _tone: "good",
    },
    {
      r: "<b>“Resolved” is qualified by same-request.</b> The registered release bit is set only when the address, the store flag and the fetch flag on the port still match the ones the hit was computed for.",
      s: "A newcomer riding the previous requester's hit. Unqualified, the fetch's hit released the data access that displaced it, one cycle after the port switched.",
      _tone: "good",
    },
    {
      r: "<b>A fault carries its owner and holds only that requester.</b> The fault is a level tagged <code>fault_fetch</code>; <code>busy</code> drops for the requester it belongs to and stays asserted for the other.",
      s: "One requester's page fault stalling the other forever — and a fault that is a pulse rather than a level, which the stalled pipeline is not looking at on the cycle the walk gives up.",
      _tone: "good",
    },
  ],
};

const pgFields = {
  cols: [
    { key: "f", label: "Field", mono: true },
    { key: "w", label: "Width", align: "right", mono: true },
    { key: "p", label: "Position", mono: true },
    { key: "o", label: "Owner" },
  ],
  rows: [
    {
      f: "if_ok",
      w: "1",
      p: "—",
      o: "the wrapper. Set when a translation lands; cleared by <code>sfence.vma</code>, by a new <code>satp</code>, and by translation being switched off",
    },
    {
      f: "if_vpn",
      w: "27",
      p: "—",
      o: "the wrapper, from <code>imem_addr[38:12]</code>. <b>It is the whole tag</b> — one entry, so a hit is one 27-bit compare and nothing else",
    },
    {
      f: "if_ppn",
      w: "ADDR_W − 12",
      p: "—",
      o: "the MMU's answer, captured on the cycle <code>busy</code> falls",
    },
    {
      f: "if_bad",
      w: "1",
      p: "—",
      o: "<b>the wrapper.</b> The page faulted; the entry is installed anyway, and every fetch that hits it is delivered as a NOP marked faulted",
      _tone: "warn",
    },
  ],
};

/* --- the poisoned page ---------------------------------------------------- */

const pgfBroken = {
  rows: [
    { name: "translating", kind: "bit", values: [1, 1, 1, 1, 1] },
    { name: "if_hit", kind: "bit", values: [0, 0, 0, 0, 0] },
    { name: "if_req (walk)", kind: "bit", values: [1, 0, 1, 0, 1], mark: [0, 2, 4] },
    { name: "MMU fault", kind: "bit", values: [0, 1, 0, 1, 0] },
    { name: "imem_stall", kind: "bit", values: [1, 1, 1, 1, 1] },
    { name: "core", kind: "text", values: ["held", "held", "held", "held", "held"] },
  ],
  notes: [
    {
      cycle: 1,
      text: "The walk faults. Nothing is written to the page register, so the next cycle still misses.",
      tone: "bad",
    },
    {
      cycle: 2,
      text: "Fetch is still stalled, still needs a translation, and asks again — for the same page, which will fault again.",
      tone: "bad",
    },
    {
      cycle: 4,
      text: "The core never leaves the stall, so it never reaches an instruction boundary, so it never takes the trap that would fix the mapping. The symptom is a hang on the first jump into an unmapped page, with a page-table walker that looks busy and correct.",
      tone: "bad",
    },
  ],
};

const pgfFixed = {
  rows: [
    { name: "if_req (walk)", kind: "bit", values: [1, 0, 0, 0, 0] },
    { name: "MMU fault", kind: "bit", values: [0, 1, 0, 0, 0] },
    { name: "if_bad", kind: "bit", values: [0, 1, 1, 1, 1], mark: [1] },
    { name: "if_hit", kind: "bit", values: [0, 0, 1, 1, 1] },
    { name: "imem_stall", kind: "bit", values: [1, 1, 1, 0, 0] },
    {
      name: "imem_fault",
      kind: "bit",
      values: [0, 0, 0, 1, 1],
      mark: [3],
    },
    {
      name: "what D decodes",
      kind: "bus",
      values: [null, null, null, "NOP", "NOP"],
    },
    {
      name: "core",
      kind: "text",
      values: ["held", "held", "settle", "", "traps in E · cause 12"],
    },
  ],
  notes: [
    {
      cycle: 1,
      text: "The faulting page is installed as an entry with its poison bit set. It is a real entry: the next fetch HITS it, so the walk is not repeated.",
      tone: "good",
    },
    {
      cycle: 3,
      text: "The fetch proceeds and the word is delivered with a fault flag beside it. The word itself is whatever the untranslated array returned, so it is DECODED AS A NOP — decoded for real it could issue a load or redirect fetch before the trap lands.",
      tone: "good",
    },
    {
      cycle: 4,
      text: "The NOP reaches execute and traps there, at a proper instruction boundary, with cause 12 and tval = pc. Everything the handler needs is the PC it could not fetch.",
      tone: "good",
    },
  ],
};

/* --- where the cost actually is ------------------------------------------- */

const cost = {
  nodes: [
    {
      id: "req",
      x: 0,
      y: 7,
      w: 10,
      h: 5,
      label: "the core port",
      sub: "dmem_req is decode-only — no address, no adder",
    },
    {
      id: "latch",
      x: 16,
      y: 4.5,
      w: 12,
      h: 10,
      label: "the first-cycle latch",
      sub: "address · byte enables · write data · store flag · four select bits",
      accent: true,
    },
    {
      id: "tlb",
      x: 34,
      y: 0,
      w: 12,
      h: 6,
      label: "the TLB array",
      sub: "ENTRIES × 57 · one block RAM",
    },
    {
      id: "arrays",
      x: 34,
      y: 10,
      w: 12,
      h: 7,
      label: "the L1's two arrays",
      sub: "tag LINES × 31 in LUTRAM · data LINES·4 × 64 in block RAM",
      accent: true,
    },
    {
      id: "walk",
      x: 52,
      y: 0,
      w: 12,
      h: 6,
      label: "the walker",
      sub: "level · base · the PTE, and a sweep counter",
    },
    {
      id: "linebuf",
      x: 52,
      y: 10,
      w: 12,
      h: 6,
      label: "linebuf",
      sub: "256 bits, and it rotates rather than being indexed",
    },
    {
      id: "nport",
      x: 70,
      y: 4.5,
      w: 12,
      h: 9,
      label: "rv64_nport",
      sub: "a priority mux, and one set of 256-bit AXI registers",
      accent: true,
    },
  ],
  edges: [
    { from: "req:r", to: "latch:l", dir: "h", label: "req" },
    { from: "latch:r", to: "tlb:l", dir: "h", label: "va" },
    { from: "latch:r", to: "arrays:l", dir: "h", label: "pa" },
    { from: "tlb:r", to: "walk:l", dir: "h", label: "miss" },
    { from: "arrays:r", to: "linebuf:l", dir: "h", label: "word" },
    { from: "walk:r", to: "nport:l", dir: "h", label: "64 b" },
    { from: "linebuf:r", to: "nport:l", dir: "h", accent: true, label: "32 B" },
  ],
};

const knobs = {
  cols: [
    { key: "k", label: "Knob", mono: true },
    { key: "d", label: "Default", mono: true, align: "right" },
    { key: "w", label: "What it moves" },
  ],
  rows: [
    {
      k: "ADDR_W",
      d: "40",
      w: "<b>Everything, and it is the only knob that can move an array out of block RAM.</b> It sets the stored PPN to <code>ADDR_W − 12</code> and therefore the TLB entry width, and it sets the L1's tag width. Raise it far enough and both arrays silently become LUTs.",
      _tone: "warn",
    },
    {
      k: "MEM_PRIM",
      d: '"block"',
      w: "Which primitive the TLB array and the L1's <i>data</i> array land in. <b>The L1's tag array ignores it</b> — it is wired to <code>distributed</code> at the instantiation, because 64 × 31 bits is 248 bytes and a block RAM spent on it is a block RAM wasted.",
    },
    {
      k: "L1_LINES",
      d: "64",
      w: "Capacity, linearly: <code>LINES × 32 B</code>. Nearly free in LUT, because per-line valid and dirty ride in the tag word rather than in flop arrays — which is the whole reason the line count is a cheap knob here and was an expensive one on the RV32 PE.",
    },
    {
      k: "TLB_ENTRIES",
      d: "32",
      w: "Reach, linearly: <code>ENTRIES × 4 KB</code> — 4 KB per entry even for a superpage, because a superpage is filled as the 4 KB slice the access asked for. <b>It does not widen the entry</b> — the tag loses exactly the bit the index gains.",
    },
    {
      k: "CACHE_LO",
      d: "0x8000_0000",
      w: "The one bit the cached test looks at. It is a convention of the wrapper, not a property of the machine.",
    },
    {
      k: "SPAD_WORDS",
      d: "4096",
      w: "64-bit words, so 32 KB. Outside the stall path and outside the node port entirely.",
    },
    {
      k: "DATA_W",
      d: "256",
      w: "The beat. <b>A line is one beat only because 32 bytes is <code>DATA_W/8</code></b> — change one without the other and a fill stops being a single request and a single response.",
    },
  ],
};

/* --- the TLB entry, field by field ---------------------------------------- */

const tlbSpec = {
  cols: [
    { key: "f", label: "Field", mono: true },
    { key: "w", label: "Width", align: "right", mono: true },
    { key: "p", label: "Position", mono: true },
    { key: "o", label: "Owner" },
  ],
  rows: [
    {
      f: "valid",
      w: "1",
      p: "[56] — <code>P_TAG + TAG_W</code>",
      o: "the walker sets it on a leaf; <b>the sweep clears it</b> — the array has no reset, and <code>sfence.vma</code> re-runs the same sweep",
    },
    {
      f: "tag",
      w: "27 − log₂E",
      p: "[55:34] — <code>P_PPN + PPN_W</code>",
      o: "the walker, from the virtual page number above the index bits",
    },
    {
      f: "ppn",
      w: "ADDR_W − 12",
      p: "[33:6] — <code>P_PERM + 6</code>",
      o: "the walker, from the leaf PTE — <b>truncated to the card's width, which is the whole point of this table</b>. For a superpage leaf it is the PTE's PPN with the VPN bits below that level merged in",
    },
    {
      f: "perms",
      w: "6",
      p: "[5:0] — <code>P_PERM</code>",
      o: "the walker, copied from the leaf PTE as <code>{D, A, U, X, W, R}</code>",
    },
  ],
};

/* --- the CSR-owned control the MMU reads ---------------------------------- */

const mmuCtrl = {
  cols: [
    { key: "s", label: "Input", mono: true },
    { key: "f", label: "Comes from", mono: true },
    { key: "m", label: "Meaning" },
  ],
  rows: [
    {
      s: "satp",
      f: "the <code>0x180</code> CSR",
      m: "<code>MODE</code> 63:60 and <code>PPN</code> 27:0. <code>MODE == 8</code> is Sv39; anything else is off. ASID is <b>not implemented</b> and reads zero, and the PPN is WARL-narrowed to the card's 28 bits",
    },
    {
      s: "priv",
      f: "<code>rv64_csr</code>",
      m: "3 machine, 1 supervisor, 0 user. <b>Machine never translates</b> — that condition is the architecture's, not this design's",
    },
    {
      s: "sum",
      f: "<code>mstatus[18]</code>",
      m: "a supervisor load or store may touch a <code>U</code> page. <b>It does not relax fetch</b>",
      _tone: "warn",
    },
    {
      s: "mxr",
      f: "<code>mstatus[19]</code>",
      m: "an execute-only page is readable",
    },
    {
      s: "sfence",
      f: "<code>sfence.vma</code> at an instruction boundary",
      m: "one pulse restarts the whole-array sweep. <b>Address and ASID operands are ignored</b> — every entry goes",
    },
    {
      s: "is_store / is_fetch",
      f: "decode",
      m: "which permission to check. Both are named from the decoded instruction rather than from the byte strobes, because the strobes carry the misalignment test and therefore the address adder",
    },
  ],
};

/* --- the stale tag -------------------------------------------------------- */

const staleHit = {
  rows: [
    { name: "req", kind: "bit", values: [1, 1, 1] },
    { name: "addr", kind: "bus", values: ["A", "A", "A"] },
    {
      name: "tag array out",
      kind: "bus",
      values: ["index−1", "index(A)", "index(A)"],
      mark: [0],
    },
    { name: "tag compare", kind: "bit", values: [1, 1, 1] },
    { name: "hit, unguarded", kind: "bit", values: [1, 1, 1], mark: [0] },
    { name: "returned", kind: "text", values: ["the old line", "", ""] },
  ],
  notes: [
    {
      cycle: 0,
      text: "The probe address is presented in the same cycle as the access, and the array answers a cycle later — so the entry being compared is still the PREVIOUS index's.",
      tone: "bad",
    },
    {
      cycle: 0,
      text: "On a sequential walk the neighbouring line carries the same tag, so the compare passes by accident. The fill is skipped and the data array returns the word at this index — which belongs to whatever address last occupied the line.",
      tone: "bad",
    },
  ],
};

const staleMiss = {
  rows: [
    { name: "req", kind: "bit", values: [1, 1, 1, 1] },
    { name: "addr", kind: "bus", values: ["A", "A", "A", "A"] },
    {
      name: "tag array out",
      kind: "bus",
      values: ["index−1", "index(A)", "index(A)", "index(A)"],
      mark: [0],
    },
    { name: "tag compare", kind: "bit", values: [0, 0, 0, 0] },
    { name: "miss, unguarded", kind: "bit", values: [1, 0, 0, 0], mark: [0] },
    {
      name: "victim captured",
      kind: "bus",
      values: ["index−1's tag", "", "", ""],
      mark: [0],
    },
  ],
  notes: [
    {
      cycle: 0,
      text: "The same stale cycle, the other way round: this time the accidental compare FAILS, so an unguarded miss starts a fill here.",
      tone: "bad",
    },
    {
      cycle: 0,
      text: "The idle state latches the victim's tag and its dirty bit from that stale entry. The eviction then writes this index's data to the address formed from the wrong tag — a whole line stored 32 bytes of somewhere else.",
      tone: "bad",
    },
    {
      cycle: 1,
      text: "The correct entry arrives one cycle later, and nothing re-reads it. Both the address written and the decision to write at all were taken from the wrong word.",
      tone: "bad",
    },
  ],
};

const staleFixed = {
  rows: [
    { name: "req", kind: "bit", values: [1, 1, 1] },
    { name: "addr", kind: "bus", values: ["A", "A", "A"] },
    {
      name: "tag array out",
      kind: "bus",
      values: ["index−1", "index(A)", "index(A)"],
    },
    { name: "fresh", kind: "bit", values: [0, 1, 1], mark: [1] },
    { name: "hit", kind: "bit", values: [0, 1, 1] },
    { name: "miss", kind: "bit", values: [0, 0, 0] },
    { name: "stall", kind: "bit", values: [1, 0, 0] },
  ],
  notes: [
    {
      cycle: 0,
      text: "fresh is low, because the address presented now was not also presented last cycle. Neither a hit nor a miss may be declared, and stall holds the core — which is where the extra cycle on every access comes from.",
      tone: "good",
    },
    {
      cycle: 1,
      text: "The array now presents this index's own entry. hit, miss and the captured victim are all computed from it.",
      tone: "good",
    },
    {
      text: "stall and miss are deliberately different widths: hold on anything that is not a CONFIRMED hit, but start a fill only once the tag is known. One expression cannot do both jobs.",
      tone: "good",
    },
  ],
};

/* --- the re-probe --------------------------------------------------------- */

const reprobeBroken = {
  rows: [
    {
      name: "state",
      kind: "bus",
      values: ["L_F_WR", "L_REPROBE", "L_IDLE", "L_EV_RD"],
    },
    { name: "tag write enable", kind: "bit", values: [0, 1, 0, 0] },
    {
      name: "array contents",
      kind: "bus",
      values: ["old", "old", "NEW", "NEW"],
    },
    {
      name: "tag presented",
      kind: "bus",
      values: ["—", "old", "old", "—"],
      mark: [2],
    },
    {
      name: "outcome",
      kind: "text",
      values: ["", "", "misses again", "evicts what it just filled"],
    },
  ],
  notes: [
    {
      cycle: 1,
      text: "The tag write enable is itself a register, so the write only commits at the end of this cycle — a cycle after the state set it.",
    },
    {
      cycle: 2,
      text: "The idle state compares against the word read LAST cycle, and the array is read-first, so that read returned the OLD tag. The line is in the array and the tag says it is not.",
      tone: "bad",
    },
    {
      cycle: 3,
      text: "A second miss on the address just filled. The victim is the line just written, so it is evicted and refetched — and then misses again. The access never completes.",
      tone: "bad",
    },
  ],
};

const reprobeFixed = {
  rows: [
    {
      name: "state",
      kind: "bus",
      values: ["L_F_WR", "L_REPROBE", "L_REPROBE2", "L_IDLE"],
    },
    { name: "tag write enable", kind: "bit", values: [0, 1, 0, 0] },
    {
      name: "array contents",
      kind: "bus",
      values: ["old", "old", "NEW", "NEW"],
    },
    {
      name: "tag presented",
      kind: "bus",
      values: ["—", "old", "old", "NEW"],
      mark: [3],
    },
    { name: "outcome", kind: "text", values: ["", "", "", "hit"] },
  ],
  notes: [
    {
      cycle: 2,
      text: "The second state exists for exactly one reason: to let a read be PRESENTED after the write has committed, so that its answer is the new tag.",
      tone: "good",
    },
    {
      cycle: 3,
      text: "The access hits and retires. Two cycles is not a margin — it is one cycle for the registered write enable and one for the read-first array, and neither is optional.",
      tone: "good",
    },
  ],
};

/* --- sizing --------------------------------------------------------------- */

const sizing = {
  cols: [
    { key: "q", label: "Quantity" },
    { key: "f", label: "Formula", mono: true },
    { key: "d", label: "At the defaults", mono: true, align: "right" },
  ],
  rows: [
    {
      q: "TLB entry width",
      f: "22 + ADDR_W − log₂ ENTRIES",
      d: "57 bits",
      _tone: "good",
    },
    { q: "TLB reach", f: "ENTRIES × 4 KB", d: "128 KB, any page size" },
    { q: "L1 capacity", f: "LINES × 32 B", d: "2 KB" },
    { q: "L1 tag word", f: "ADDR_W − 3 − log₂ LINES", d: "31 bits" },
    { q: "L1 data array", f: "LINES × 4 words × 64 bits", d: "256 × 64" },
    {
      q: "cycles per access",
      f: "2, +1 if cached, +1 if translated, + the round trip on a miss",
      d: "2 / 3 / 4 / 4 + n",
    },
  ],
};

const arbiter = {
  nodes: [
    {
      id: "w1",
      x: 0,
      y: 0,
      w: 17,
      h: 4.4,
      label: "1 · the page-table walker",
      sub: "a 64-bit read",
      accent: true,
    },
    {
      id: "w2",
      x: 0,
      y: 6,
      w: 17,
      h: 4.4,
      label: "2 · the L1's fill",
      sub: "a 32-byte read",
    },
    {
      id: "w3",
      x: 0,
      y: 12,
      w: 17,
      h: 4.4,
      label: "3 · the L1's writeback",
      sub: "a 32-byte write",
    },
    {
      id: "w4",
      x: 0,
      y: 18,
      w: 17,
      h: 4.4,
      label: "4 · an uncached access",
      sub: "a 64-bit read or write",
    },
    {
      id: "np",
      x: 26,
      y: 6,
      w: 15,
      h: 10.4,
      label: "rv64_nport",
      sub: "fixed priority — and at most one client is ever active",
      accent: true,
    },
    {
      id: "mag",
      x: 47,
      y: 6,
      w: 14,
      h: 10.4,
      label: "MAG",
      sub: "one AXI master · 40-bit address · 256-bit data · no bursts",
    },
  ],
  edges: [
    { from: "w1:r", to: "np:l" },
    { from: "w2:r", to: "np:l" },
    { from: "w3:r", to: "np:l" },
    { from: "w4:r", to: "np:l" },
    { from: "np:r", to: "mag:l", accent: true },
  ],
};

const accessCost = {
  cols: [
    { key: "a", label: "Access" },
    { key: "c", label: "Cycles held in execute", align: "right", mono: true },
  ],
  rows: [
    { a: "scratchpad or control register", c: "2" },
    { a: "L1 hit", c: "3" },
    {
      a: "L1 miss",
      c: "3 + the fill round trip (+ an eviction, if the victim is dirty)",
    },
    { a: "uncached", c: "2 + the node round trip" },
    {
      a: "any of the above <b>with translation on</b>, TLB hit",
      c: "+1 — the array answers a cycle after the address, and the release is registered",
    },
    { a: "any of the above behind a page-table walk", c: "+ three PTE reads, each a node round trip" },
  ],
};

/* --- the TLB entry -------------------------------------------------------- */

const tlbEntry = [
  { name: "valid", bits: 1, value: "1" },
  { name: "tag", bits: 22, value: "tag[21:0]" },
  { name: "ppn", bits: 28, value: "ppn[27:0] — 40-bit card", accent: true },
  { name: "perms", bits: 6, value: "{D, A, U, X, W, R}" },
];

const tlbWide = [
  { name: "valid", bits: 1, value: "1" },
  { name: "tag", bits: 22, value: "tag[21:0]" },
  {
    name: "ppn",
    bits: 44,
    value: "the architectural Sv39 PPN",
    accent: true,
  },
  { name: "perms", bits: 6, value: "{D, A, U, X, W, R}" },
];

const walker = {
  states: [
    { id: "IDLE", x: 0, y: 0, label: "W_IDLE" },
    { id: "REQ", x: 11, y: 0, label: "W_REQ" },
    { id: "WAIT", x: 22, y: 0, label: "W_WAIT" },
    { id: "DONE", x: 33, y: 7, label: "W_DONE" },
    { id: "DONE2", x: 44, y: 7, label: "W_DONE2" },
    { id: "FAULT", x: 33, y: -7, label: "W_FAULT" },
  ],
  edges: [
    { from: "IDLE", to: "REQ", label: "a miss" },
    { from: "REQ", to: "WAIT", label: "read this level" },
    { from: "WAIT", to: "REQ", label: "not a leaf", curve: 70 },
    { from: "WAIT", to: "DONE", label: "a leaf · write", curve: 20 },
    { from: "DONE", to: "DONE2", label: "" },
    { from: "WAIT", to: "FAULT", label: "no mapping", curve: -20 },
  ],
};

const faultCases = {
  cols: [
    { key: "c", label: "What the walk found" },
    { key: "w", label: "Why it is a fault rather than a translation" },
  ],
  rows: [
    {
      c: "<code>V</code> clear",
      w: "no mapping at this level",
    },
    {
      c: "<code>W</code> set with <code>R</code> clear",
      w: "the specification reserves that encoding; it is a malformed table, not a write-only page",
    },
    {
      c: "no leaf at level 0",
      w: "three levels walked and the last PTE is still a pointer",
    },
    {
      c: "<b>a superpage whose PPN is not aligned to its own size</b>",
      w: "<b>the specification requires a fault rather than a truncation.</b> Level 2 checks <code>ppn[17:0]</code>, level 1 checks <code>ppn[8:0]</code>. Truncating instead maps a 1 GB region onto whatever the low bits happened to name",
      _tone: "warn",
    },
    {
      c: "a hit whose permissions do not allow the access",
      w: "checked on the TLB hit rather than in the walk, so a second access to a mapped page faults without walking again",
    },
  ],
};

const causeMap = {
  cols: [
    { key: "a", label: "Access", mono: true },
    { key: "c", label: "Cause", mono: true, align: "center" },
    { key: "t", label: "tval", mono: true },
  ],
  rows: [
    { a: "instruction fetch", c: "<b>12</b>", t: "the faulting PC" },
    { a: "load", c: "<b>13</b>", t: "the effective address" },
    { a: "store or AMO", c: "<b>15</b>", t: "the effective address" },
  ],
};

const l1sm = {
  states: [
    { id: "IDLE", x: 0, y: 0, label: "L_IDLE" },
    { id: "EVRD", x: 12, y: -7, label: "L_EV_RD" },
    { id: "EVSEND", x: 25, y: -7, label: "L_EV_SEND" },
    { id: "FREQ", x: 37, y: 0, label: "L_F_REQ" },
    { id: "FWAIT", x: 37, y: 9, label: "L_F_WAIT" },
    { id: "FWR", x: 25, y: 9, label: "L_F_WR" },
    { id: "RP", x: 12, y: 9, label: "L_REPROBE" },
  ],
  edges: [
    { from: "IDLE", to: "EVRD", label: "miss · dirty", curve: 25 },
    { from: "EVRD", to: "EVSEND", label: "four out" },
    { from: "EVSEND", to: "FREQ", label: "wb_ready", curve: 25 },
    { from: "IDLE", to: "FREQ", label: "miss · clean", curve: 110 },
    { from: "FREQ", to: "FWAIT", label: "both halves" },
    { from: "FWAIT", to: "FWR", label: "one beat" },
    { from: "FWR", to: "RP", label: "four in" },
    { from: "RP", to: "IDLE", label: "TWO cycles", curve: 25 },
  ],
};

const l1arrays = {
  cols: [
    { key: "a", label: "Array" },
    { key: "s", label: "Shape", mono: true },
    { key: "p", label: "Primitive" },
    { key: "h", label: "Holds", mono: true },
  ],
  rows: [
    {
      a: "tag",
      s: "64 × 31",
      p: "LUTRAM",
      h: "{valid, dirty, tag[28:0]}",
    },
    {
      a: "data",
      s: "256 × 64",
      p: "block RAM, true dual port with byte enables",
      h: "four 64-bit words per line",
    },
  ],
};

const sweepStates = {
  cols: [
    { key: "s", label: "State", mono: true },
    { key: "w", label: "What it does" },
  ],
  rows: [
    {
      s: "L_S_SCAN",
      w: "flush-all: read the tag at the sweep index. Two states per line, because the tag word is only out in the next one",
    },
    {
      s: "L_S_TEST",
      w: "<code>dirty</code> alone decides — a fill leaves a line valid and clean, so dirty implies valid by construction",
    },
    {
      s: "L_S_RD / L_S_SEND",
      w: "the same four-word walk and one-beat writeback, then rewrite the tag <b>still valid, no longer dirty</b>",
    },
    {
      s: "L_S_DRAIN",
      w: "wait for the node port to report no write outstanding. <b>A flush finishes on acknowledgements, not on the last beat sent</b>",
      _tone: "good",
    },
    {
      s: "L_I_SCAN",
      w: "invalidate-all, one line per cycle — and the power-on sweep the tag array's missing reset needs",
    },
  ],
};

const cachedTests = {
  cols: [
    { key: "r", label: "Region" },
    { key: "t", label: "The test on the 40-bit physical address", mono: true },
    { key: "w", label: "What that is" },
  ],
  rows: [
    {
      r: "scratchpad",
      t: "pa[39:15] == 2",
      w: "one equality — 32 KB at 0x0001_0000",
    },
    {
      r: "control region",
      t: "pa[39:8] == 0x200",
      w: "one equality — 256 B at 0x0002_0000",
    },
    {
      r: "the node range",
      t: "any bit of pa[39:28] set",
      w: "one OR reduction — base 2²⁸",
    },
    {
      r: "<b>cached</b>",
      t: "in the node range and pa[31] set",
      w: "<b>one bit</b> — base 2³¹",
      _tone: "good",
    },
    {
      r: "uncached",
      t: "in the node range and pa[31] clear",
      w: "the complement",
    },
  ],
};

const whatCached = {
  cols: [
    { key: "r", label: "Region" },
    { key: "c", label: "Cached", align: "center" },
    { key: "w", label: "Why" },
  ],
  rows: [
    {
      r: "the scratchpad and the control region",
      c: "n/a",
      w: "local arrays, one cycle",
    },
    {
      r: "the node range with <code>pa[31]</code> set",
      c: "<b>yes</b>",
      w: "DRAM: large, re-read, private to this core",
      _tone: "good",
    },
    {
      r: "the node range with <code>pa[31]</code> clear",
      c: "<b>no</b>",
      w: "staging, the node's own registers, cross-mesh writes — the <i>home</i> of their addresses, and multi-writer. <b>Staging honours byte strobes</b>, so an 8-byte store into it writes eight bytes",
    },
    {
      r: "the aperture, <code>pa[39]</code> set",
      c: "<b>no</b>",
      w: "it has bit 31 clear, so the same test excludes it",
    },
  ],
};

const ordering = {
  cols: [
    { key: "n", label: "#", align: "center", mono: true },
    { key: "r", label: "The rule" },
    { key: "w", label: "What rests on it" },
  ],
  rows: [
    {
      n: "1",
      r: "<b>One access at a time, in program order.</b> The core stalls in execute for the whole of every access, so two never overlap and they reach the node port in the order the program issued them.",
      w: "<code>FENCE</code> is a NOP because there is nothing weaker to strengthen",
    },
    {
      n: "2",
      r: "<b>An uncached store is in memory before the next instruction retires.</b> The uncached path completes on the AXI write response, not on the address being accepted, and the core is held until then.",
      w: "this is the strong guarantee, and the one to build on",
      _tone: "good",
    },
    {
      n: "3",
      r: "<b>An uncached load returns data no older than every store before it</b>, by rules 1 and 2.",
      w: "mailbox reads, completion flags",
    },
    {
      n: "4",
      r: "<b>A cached store is <i>not</i> in memory, and there is no way to put it there.</b> It lands in the L1 and reaches DRAM only when its line is evicted.",
      w: "the L1's flush and invalidate inputs exist and <b>the wrapper ties both to zero</b>",
      _tone: "bad",
    },
  ],
};

const modules = {
  cols: [
    { key: "t", label: "Instance", mono: true },
    { key: "n", label: "none · LUT", align: "right", mono: true },
    { key: "r", label: "node · LUT", align: "right", mono: true },
    { key: "f", label: "node · FF", align: "right", mono: true },
    { key: "b", label: "node · BRAM", align: "right", mono: true },
  ],
  rows: [
    { t: "u_l1", n: "349", r: "501", f: "499", b: "2 × RAMB36" },
    { t: "u_mmu", n: "151", r: "103", f: "217", b: "1 × RAMB36" },
    { t: "u_np", n: "288", r: "142", f: "725", b: "—" },
    { t: "u_mbox", n: "138", r: "76", f: "300", b: "—" },
  ],
};

const mmuContexts = {
  cols: [
    { key: "c", label: "The same module, three ways" },
    { key: "l", label: "LUT", align: "right", mono: true },
    { key: "b", label: "BRAM", align: "right", mono: true },
    { key: "w", label: "What is in it" },
  ],
  rows: [
    {
      c: "the <code>u_mmu</code> row in the node, <b>when <code>priv</code> was tied to machine</b>",
      l: "<b>52</b>",
      b: "<b>0</b>",
      w: "<code>enabled</code> was a constant zero, every output the array fed was dead, and synthesis removed the array and the walker — <b>leaving the power-on sweep counter</b>",
      _tone: "warn",
    },
    {
      c: "the <code>u_mmu</code> row in the node, now",
      l: "<b>103</b>",
      b: "<b>1</b>",
      w: "<code>priv</code> is a register software writes, so nothing folds. <b>The array is back and so is the walker</b>",
      _tone: "good",
    },
    {
      c: "the same instance under <code>-flatten_hierarchy none</code>",
      l: "151",
      b: "1",
      w: "the same logic, attributed rather than re-parented — a hierarchical row under a flattening flow charges neighbouring leaves to whichever boundary survives nearest",
    },
  ],
};

const absent = {
  cols: [
    { key: "n", label: "Not built" },
    { key: "w", label: "Consequence" },
  ],
  rows: [
    {
      n: "coherence, and any mechanism that would need it",
      w: "cached memory is never written from outside; externally written memory is never cached",
      _tone: "good",
    },
    {
      n: "hit-under-miss, miss-status registers, a load/store queue",
      w: "one outstanding miss, and the core is stalled for it. A non-blocking L1 also ends the arbiter's invariant and would need real arbitration with per-client response routing",
    },
    {
      n: "write buffering and write combining",
      w: "an uncached store waits for its response",
    },
    {
      n: "cache maintenance instructions",
      w: "no flush, no invalidate, no way to make a dirty line visible or to drop a stale one",
      _tone: "bad",
    },
    {
      n: "an instruction cache",
      w: "fetch reaches the local instruction window directly, and there is nothing between them to cache. <b>Fetch is translated</b> — through the one-page register, not through an ITLB",
    },
    {
      n: "a second TLB, or any per-requester translation state",
      w: "one array, one walk at a time, and a data access always wins the port. A fetch-heavy and a load-heavy phase therefore evict each other's entries",
    },
    {
      n: "<code>SUM</code> for instruction fetch",
      w: "<b>supervisor may not fetch from a <code>U</code> page, whatever <code>SUM</code> says</b> — the architecture relaxes loads and stores only. Kernel text and user text cannot share a page, so the link script keeps a page-aligned <code>.utext</code>",
      _tone: "warn",
    },
    {
      n: "ASID",
      w: "<code>satp.ASID</code> is WARL zero, and <code>sfence.vma</code> ignores its operands and sweeps the whole array. An address-space switch costs a full refill",
    },
    {
      n: "translation for anything but the processor",
      w: "<b>the mover's traffic is not translated.</b> A descriptor a program hands the mover names physical addresses, whatever the program's own mapping is",
      _tone: "warn",
    },
    {
      n: "an unmapped-address fault",
      w: "the wrapper answers every address; a local access outside the scratchpad and the control region aliases onto the scratchpad",
      _tone: "bad",
    },
    {
      n: "a second port for a peer to write",
      w: "unlike the RV32 PE's scratchpad, which is a true dual port with a cross-port bypass so a doorbell can land in the word a poll loop is reading, this core's scratchpad has one write port shared between the host window and the core. <b>A doorbell reaches it through the control region's interrupt line, not through memory</b>",
    },
  ],
};
</script>

<template>
  <DocPage
    title="RV64 system core memory system"
    summary="Everything between the core and the system node: one Sv39 MMU shared by instruction fetch and the data port, and the three ownership rules that make sharing it safe; why a TLB entry is 57 bits and not 73; the write-back L1 and the two guards it cannot do without; the four-client node-port arbiter; what is cached and what is not; and the ordering the core publishes — including the part of it that is not discharged."
    domain="cpu"
    status="shipped"
    source="src/kohakuaccel/pe/rv64-sys/ · docs/arch/cpu/rv64-sys/memory-system.md"
  >
    <h2 class="doc-h2">What it owns</h2>
    <p class="doc-p">Four things, and nothing else.</p>
    <div class="grid gap-3 sm:grid-cols-2 my-5">
      <div class="card p-4">
        <div
          class="kt-text-caption font-semibold text-warm-800 dark:text-warm-200 mb-1"
        >
          The access handshake
        </div>
        <p class="kt-text-caption text-warm-500 dark:text-warm-400 leading-6">
          Two phases and a four-way range decode that every access goes through
          — scratchpad, control register, cached and uncached alike.
        </p>
      </div>
      <div class="card p-4">
        <div
          class="kt-text-caption font-semibold text-warm-800 dark:text-warm-200 mb-1"
        >
          Translation
        </div>
        <p class="kt-text-caption text-warm-500 dark:text-warm-400 leading-6">
          <span class="chip">rv64_mmu</span> — a direct-mapped Sv39 TLB and a
          hardware page-table walker, <b>shared by fetch and data</b>.
        </p>
      </div>
      <div class="card p-4">
        <div
          class="kt-text-caption font-semibold text-warm-800 dark:text-warm-200 mb-1"
        >
          The L1
        </div>
        <p class="kt-text-caption text-warm-500 dark:text-warm-400 leading-6">
          <span class="chip">rv64_l1</span> — direct-mapped, write-back,
          32-byte lines, one outstanding miss.
        </p>
      </div>
      <div class="card p-4">
        <div
          class="kt-text-caption font-semibold text-warm-800 dark:text-warm-200 mb-1"
        >
          The node port
        </div>
        <p class="kt-text-caption text-warm-500 dark:text-warm-400 leading-6">
          <span class="chip">rv64_nport</span> — one AXI master, shared by four
          clients that can never collide.
        </p>
      </div>
    </div>

    <p class="doc-p">
      The core is a <b>physical-address machine</b> and stays one: it issues an
      address and waits. It holds <code>satp</code>, the privilege register and
      the <code>SUM</code>/<code>MXR</code> bits, because those are
      architectural state a CSR instruction has to reach — but it never
      translates anything, and <b>every structure on this page lives in the
      wrapper around it</b>. The alternative was to build translation and
      caching into the core, the way a general-purpose CPU does — and it was
      rejected because
      <b>the same core ships in two configurations</b>. The
      <RouterLink to="/component/rv64sys" class="doc-link"
        >mesh compute-unit configuration</RouterLink
      >
      has a scratchpad, a mesh port and no node behind it; there is nothing for
      a TLB to translate and nothing for an L1 to cache. Building this into the
      core would make that configuration pay for a memory system it cannot use,
      and the saving is not marginal: translation and the cache together are
      most of the wrapper. The cost of the wrapper boundary is that the core
      exports its memory request as a decode-only bit and its address a cycle
      later, which is what the whole of the next section is about.
    </p>

    <Callout kind="note" title="Terms used below, defined once">
      <p>
        <b>MAG</b> — the system node's memory access half, the block that turns
        descriptors into DRAM bursts and carries cross-mesh traffic.
        <b>Staging</b> — on-chip memory inside MAG that the mover and the
        inter-mesh link share; it is reached through the <b>aperture</b>, the top
        bit of the card's 40-bit address. <b>Mover</b> — the node's
        descriptor-walking memory engine. <b>Doorbell</b> — a single word one
        agent writes to make another notice.
      </p>
    </Callout>

    <h2 class="doc-h2">What it costs</h2>

    <p class="doc-p">
      Almost none of it is logic. The wrapper's area is three arrays, one
      256-bit line buffer, one set of AXI registers, and the bank of registers
      that the first cycle of every access exists to fill.
    </p>

    <Fig
      caption="The registers and arrays that set the cost, in the order an access touches them. The first-cycle latch is the reason there is a first cycle at all: everything downstream of it is steered from registers, so nothing downstream of it runs through the 64-bit address adder. The two paths that reach the node port never do so at the same time, which is what makes the port a mux."
      zoom
      wide
    >
      <BlockDiagram :nodes="cost.nodes" :edges="cost.edges" />
    </Fig>

    <SpecTable
      :cols="knobs.cols"
      :rows="knobs.rows"
      caption="The knobs, in the order they matter. Only the first can change which primitive an array lands in, and that is the one failure on this page that produces no error message"
    />

    <h2 class="doc-h2">One MMU, two requesters</h2>

    <p class="doc-p">
      <b>Instruction fetch and the data port share one
      <span class="chip">rv64_mmu</span>.</b> The alternative — a second TLB and
      a second walker for fetch — buys a lookup on a structure that almost never
      changes: consecutive fetches share a page, so one registered translation
      covers about a thousand instructions and is refilled only on a page
      crossing. What fetch gets instead is <b>one page register</b> in the
      wrapper, a single <span class="chip">VPN → PPN</span> entry whose tag is
      the whole VPN.
    </p>

    <Fig
      caption="The one MMU, its two requesters, and where each answer lands. The data port wins the mux; a fetch that is stalled for a translation issues no data access, so the two can never wait on each other. The walker is the fifth client of the node port and it reads through the UNCACHED path, which is what stops a page-table walk waiting on the L1 miss that triggered it."
      zoom
      wide
    >
      <BlockDiagram :nodes="shared.nodes" :edges="shared.edges" />
    </Fig>

    <Callout kind="rule" title="Three ownership rules make one MMU safe to share">
      <p>
        A single-requester MMU can read the port whenever it likes, because the
        port only ever carries one request. A shared one cannot: the request on
        the port changes underneath a walk that is still running.
        <b>Each rule below removed a specific wrong answer, and none of them is
        a margin.</b>
      </p>
    </Callout>

    <SpecTable :cols="ownership.cols" :rows="ownership.rows" />

    <SpecTable
      :cols="pgFields.cols"
      :rows="pgFields.rows"
      caption="The fetch page register. It is not a one-entry TLB: it has no permission bits of its own, because the permission check happened in the MMU when the entry was filled, and it has no index, because there is exactly one of it"
    />

    <Callout kind="trap" title="A page that faulted is still an entry — a poisoned one">
      <p>
        The obvious handling of a faulting fetch is to write nothing and let the
        next cycle try again. <b>It hangs.</b> Fetch is stalled, so the core
        never reaches an instruction boundary, so it never takes the trap that
        would fix the mapping, so fetch stays stalled — and the walker re-walks
        the same table forever, looking healthy and busy the whole time.
      </p>
      <p>
        So the faulting page is installed as an entry with
        <span class="chip">if_bad</span> set. The fetch <i>proceeds</i>; the
        word it returns is whatever the untranslated array held, so it is
        <b>decoded as a NOP</b> rather than as itself — decoded for real it
        could issue a load or redirect fetch before the trap lands — and the
        fault flag travels beside the word into execute, where the core traps at
        a proper instruction boundary with cause 12 and
        <span class="chip">tval</span> equal to the PC it could not fetch.
      </p>
    </Callout>

    <WaveTrace
      :rows="pgfBroken.rows"
      :notes="pgfBroken.notes"
      variant="broken"
      label="Retrying the walk — the fetch never completes and the trap never fires"
    />

    <WaveTrace
      :rows="pgfFixed.rows"
      :notes="pgfFixed.notes"
      variant="fixed"
      label="A poisoned entry — the fetch completes, as a NOP that traps in execute"
    />

    <h2 class="doc-h2">One handshake for every access</h2>

    <p class="doc-p">
      Every access — scratchpad, control register, cached, uncached — goes
      through the same two-phase handshake, and the shape of it is a timing
      decision software can feel. The stall the wrapper returns to the core is an
      OR of exactly three terms: the MMU is busy, <b>this is the access's first
      cycle</b>, or the access has started and is not done. Nothing else.
    </p>

    <p class="doc-p">
      <b>The first cycle is decided from the core's memory-request bit alone</b>,
      which the core exports as decode-only — no address, no adder. On that cycle
      the wrapper latches the translated address, the byte enables, the write
      data, the store flag and the <b>range decode</b> as four select bits.
      Cycles two onward are steered entirely from those registers, so nothing in
      the stall runs through the 64-bit address adder.
    </p>

    <SpecTable
      :cols="accessCost.cols"
      :rows="accessCost.rows"
      caption="The price is one extra cycle on every access, the local scratchpad included. The alternative to paying it is an address-generation stage between execute and memory, which this core does not have"
    />

    <Callout kind="rule" title="Register every consumer of the effective address, except a memory read address">
      <p>
        A read has to be issued in the first cycle to be answered in the second.
        Nothing else does — write data, write enables, byte-write enables, range
        decodes, control-register decodes and the stall itself all have a spare
        cycle and take it. The 64-bit adder is roughly eight logic levels on its
        own and the whole budget for a path is about eleven, so anything the
        adder feeds combinationally starts two thirds spent.
      </p>
      <p>
        Applied one consumer at a time, each fix exposing the next, this measured
        <b>−227 LUT and +13.8 MHz together</b>, taking seventeen failing paths to
        none. <b>Area and frequency moving the same way is what removing logic
        looks like</b>, as opposed to trading it.
      </p>
    </Callout>

    <h2 class="doc-h2">Sv39</h2>

    <p class="doc-p">
      Translation is on when <b>both</b> hold: <code>satp</code>'s mode field
      says Sv39, <b>and the current privilege level is not machine</b>. That
      second condition is the architecture's rather than this design's — machine
      mode never translates on any RISC-V implementation — and it is why a
      machine-mode runtime runs at exactly the IPC it did before this module
      existed: with translation off the physical address is the virtual one
      taken straight through, combinationally.
    </p>

    <SpecTable
      :cols="mmuCtrl.cols"
      :rows="mmuCtrl.rows"
      caption="Everything the MMU is told, and who owns it. All six come from the CSR file or from decode; only the virtual address itself comes from the datapath, because it is the one consumer that must"
    />

    <Callout kind="rule" title="busy MUST be register-derived, and that is where the extra cycle goes">
      <p>
        <span class="chip">busy</span> gates the core's stall, and the stall
        gates the trap boundary — so a combinational path from the TLB array to
        it lands on <b>every CSR register's write enable</b>. Driven from the
        raw hit it measured <b>25 logic levels and −3.842 ns</b> at the node,
        from that one root.
      </p>
      <p>
        Registered, the array's answer is released one cycle later, so
        <b>a translated access costs one more cycle than an untranslated one
        even when it hits.</b> With Sv39 off the cost is zero, because nothing
        is gated on the array at all.
      </p>
    </Callout>

    <Callout kind="trap" title="A fault MUST be a level, not a pulse">
      <p>
        The consumer is a stalled pipeline that takes the trap at its
        <i>next instruction boundary</i>, which is not the cycle the walk gave
        up on. A one-cycle fault is asserted while the core is still held and
        gone by the time it is looked at.
      </p>
      <p>
        The level also has to <b>suppress the retry</b>. Held only until
        <span class="chip">busy</span> drops, a failed walk restarts the moment
        it is released, and the pair oscillates: walk, fault, release, walk. The
        fault is cleared by its owner withdrawing the request, or by the other
        requester taking the port — never by time passing.
      </p>
    </Callout>

    <SpecTable
      :cols="faultCases.cols"
      :rows="faultCases.rows"
      caption="Five ways a translation fails, and none of them is a truncation. The superpage alignment check is the one most often left out: a leaf at level 2 or 1 whose PPN has bits set below its own size is a malformed table"
    />

    <SpecTable
      :cols="causeMap.cols"
      :rows="causeMap.rows"
      caption="The cause a failure carries, chosen from the access the walk was serving rather than from whatever is on the port when it finishes — which is the first ownership rule doing its work"
    />

    <Callout kind="rule" title="A superpage is filled as the 4 KB slice the access asked for">
      <p>
        The array is indexed by the whole virtual page number, so each 4 KB
        piece of a 2 MB or 1 GB mapping earns <b>its own entry, on demand</b>,
        and no level field has to be stored in the entry at all. The stored PPN
        is the leaf's with the VPN bits below that level merged in.
      </p>
      <p>
        Taking the leaf's PPN unmodified instead — the obvious reading —
        <b>translates the whole superpage to its first page, silently.</b> Every
        access inside a 2 MB mapping lands in its first 4 KB, with no fault and
        a tag compare that passes.
      </p>
      <p>
        The cost is that a superpage buys no TLB reach here: 32 entries reach
        128 KB whatever the page size, and a 1 GB mapping walked linearly
        refills the array every 128 KB.
      </p>
    </Callout>

    <Callout
      kind="trap"
      title="A field's read decode and its write MUST come from one set of offsets"
    >
      <p>
        The entry's four field positions are <span class="chip">localparam</span>s
        — <span class="chip">P_PERM</span>, <span class="chip">P_PPN</span>,
        <span class="chip">P_TAG</span>, <span class="chip">P_V</span>, each
        defined from the one below it — and <b>both the write and the read
        decode are written in terms of them</b>. They were not, and they drifted:
        the write placed the physical page number at 6, immediately above the six
        permission bits, while the read took it from the literal 11, which is
        where the <i>PTE</i> keeps its PPN.
      </p>
      <p>
        A hit then returned
        <span class="chip">{tag[4:0], ppn[27:5]}</span>: the top five bits of the
        translated page number were the bottom five bits of the tag, and the real
        page number's bottom five bits were discarded.
        <b>The tag compare is unaffected, so the lookup hits exactly when it
        should and then translates to the wrong page.</b> No fault, no failing
        compare — every translated access lands in a different 4 KB page from the
        one the tables named, consistently, on the first translated load.
      </p>
      <p>
        Two independent writers of one bit position is the general form, and the
        parameterisation is what makes it impossible: at
        <span class="chip">ADDR_W = 40</span> and 32 entries the two literals are
        five apart, at other settings they are a different distance apart, and no
        setting makes them agree.
      </p>
    </Callout>

    <Callout kind="note" title="satp is the CSR, and the control region keeps a read-only mirror">
      <p>
        <code>satp</code> is CSR <code>0x180</code>, owned by supervisor
        software. The wrapper's control region still answers a read at
        <code>CTRL_BASE + 0x18</code> with the current value, so the host can see
        which address space is installed without stopping the core — but
        <b>that word is read-only</b>. Two writable copies of a translation root
        is one too many.
      </p>
      <p>
        The TLB is cleared by <code>sfence.vma</code>, which restarts the same
        whole-array sweep that runs at power-on, and by nothing else. The fence's
        address and ASID operands are ignored.
      </p>
    </Callout>

    <h3 class="doc-h3">An entry is 57 bits because the card is 40-bit</h3>

    <Fig
      caption="What is built. Sv39's PPN field is architecturally 44 bits; no address on this card exceeds 40, so the stored PPN is 28 and the entry is 57 — inside a block-RAM port, with room to spare."
    >
      <BitField :fields="tlbEntry" />
    </Fig>

    <Fig
      caption="What storing the architectural 44 would make: 73 bits. A block-RAM port is 72 at its widest, so the array would silently become LUTs — no error, no warning, and a module that costs hundreds of LUT instead of dozens. The whole MMU measuring 103 LUT and one block RAM inside the assembled node is that one bit of arithmetic."
    >
      <BitField :fields="tlbWide" />
    </Fig>

    <SpecTable
      :cols="tlbSpec.cols"
      :rows="tlbSpec.rows"
      caption="Positions are at the defaults, ENTRIES = 32 and ADDR_W = 40, and each is the localparam beside it rather than a literal. Every field is written by the walker and read by nobody else — there is no software-visible TLB entry, no way to inject one, and no way to read one back. The owner column is short here for a reason worth noticing: an entry with a single writer is a structure whose only correctness question is whether the reader agrees with that writer, which is what the trap above is about"
    />

    <p class="doc-p">
      The width is
      <span class="chip">22 + ADDR_W − log₂ ENTRIES</span>, which is worth
      writing out because two of its three terms are yours. Doubling the TLB
      does not widen the entry — the tag loses exactly the bit the index gains —
      so <b>a design that genuinely needed the architectural 44-bit PPN could
      buy the bit back by going to 64 entries</b>: 22 + 56 − 6 is 72, and 72 is
      exactly a block-RAM port. It is the same arithmetic that decides the
      branch predictor's
      <RouterLink to="/component/rv64sys/microarchitecture" class="doc-link"
        >39-bit target</RouterLink
      >, and it is worth stating as a general rule for anyone building on this
      part: <b>check that the synthesis report says block RAM where you expected
      block RAM.</b>
    </p>

    <Callout kind="rule" title="Direct-mapped, not a CAM">
      <p>
        32 entries by default, indexed by the low bits of the virtual page
        number, tag the rest. A 32-entry fully associative array is thirty-two
        22-bit comparators and a priority encoder, and it buys hit rate this
        machine does not need: its addresses are computable ahead of time and its
        working sets are descriptor-shaped rather than pointer-shaped. That
        assumption is the framework's, not this core's.
      </p>
      <p>
        The array answers a cycle after the address goes in, so the tag is
        compared against a <b>registered</b> copy of the request. <b>Even a hit
        therefore holds the core for one cycle</b>; pipelining the lookup into
        execute would need the address a stage earlier than the core produces it.
      </p>
    </Callout>

    <h3 class="doc-h3">The walk</h3>

    <Fig
      caption="Three levels, one 64-bit read each, through the uncached node port; both terminal states return to W_IDLE. W_DONE and W_DONE2 are TWO cycles and neither is slack: the array write is non-blocking, so it lands at the edge ENDING W_DONE, and a read-first array probed in that same cycle still returns the old entry. One cycle here re-probed stale, missed, and walked the whole table a second time — six PTE reads per translation instead of three, with a correct result and no failing check."
      zoom
    >
      <StateMachine :states="walker.states" :edges="walker.edges" :r="42" />
    </Fig>

    <Callout kind="rule" title="The walker uses the uncached port, and that is a deadlock argument">
      <p>
        Page tables live in staging; staging is outside the cached range; so a
        walk never enters the L1 and cannot wait on the miss that triggered it.
        <b>A blocking L1 whose miss could require a walk through the same
        blocking L1 has a cycle in it</b>, and this is how the cycle is removed
        rather than tolerated.
      </p>
      <p>
        Putting the tables there has a second requirement, and it is not the
        walker's: <b>staging must honour byte strobes.</b> Its write port takes
        AXI beats, and a 64-bit store is one lane of a 32-byte word — without
        strobes the bank writes the whole word and <b>three of every four
        page-table entries take the value of the fourth</b>. The store succeeds,
        the entry the program wrote reads back correctly, and its neighbours are
        gone. The symptom is a walk that resolves to the wrong page rather than
        a fault.
      </p>
    </Callout>

    <h2 class="doc-h2">The L1</h2>

    <p class="doc-p">
      Direct-mapped, <b>32-byte lines</b>, write-back, write-allocate, one
      outstanding miss. Sixty-four lines by default, so 2 KB. It sits
      <b>behind</b> the TLB and is physically tagged, so nothing in it ever sees
      a virtual address and an address-space change costs it nothing.
    </p>

    <Callout kind="rule" title="It is never written from outside, and that removes coherence">
      <p>
        Everything a peer can write lands in the node windows, which are the
        <i>home</i> of their addresses and are uncached here. There is no
        externally-written-versus-dirty-line case to construct.
      </p>
      <p>
        <b>A line is one beat.</b> Thirty-two bytes is one 256-bit AXI beat and
        one flit payload, so a fill is one request and one response and a
        writeback is one descriptor and one beat. That is the second job of the
        cache and it does not depend on hit rate: ordinary byte, halfword, word
        and doubleword accesses are presented to software while the upstream
        protocol stays line-oriented.
      </p>
    </Callout>

    <SpecTable
      :cols="l1arrays.cols"
      :rows="l1arrays.rows"
      caption="Per-line state lives in the tag word. valid and dirty beside the tag cost no flops and no indexed read mux; as flop arrays they cost LUT twice, once as flops and once as the mux in front of the tag compare — which is what makes the line count nearly free"
    />

    <Fig
      caption="The miss path. A dirty victim is walked out and sent BEFORE the fill request goes, so the writeback and the fill are two separate transactions in a fixed order — never concurrent, which is what makes one transaction enough. Reset does not put this machine in L_IDLE: it puts it in the invalidate sweep, because the tag array has no reset and every line has to be written clean before a lookup can mean anything."
      zoom
    >
      <StateMachine :states="l1sm.states" :edges="l1sm.edges" :r="44" />
    </Fig>

    <SpecTable
      :cols="sweepStates.cols"
      :rows="sweepStates.rows"
      caption="The sweep states, which the miss path above does not reach. Fourteen states in all"
    />

    <h3 class="doc-h3">The stale first cycle, and the two ways to get it wrong</h3>

    <p class="doc-p">
      The probe address is presented in the same cycle as the access here, not a
      stage ahead as on the
      <RouterLink to="/component/rv32pe/microarchitecture" class="doc-link"
        >RV32 PE</RouterLink
      >, and the array answers a cycle after the address goes in. So on the
      first cycle of every access, the tag being compared belongs to the
      <i>previous</i> index. Both of the obvious readings of that word are
      wrong, and they are wrong in opposite directions.
    </p>

    <Callout kind="rule" title="A hit MUST require freshness, and so MUST a miss">
      <p>
        Neither a hit nor a miss <b>MAY</b> be declared from a tag read whose
        address was not also presented in the previous cycle, and no write to
        that index may have landed in between. The guard is one
        <i>freshness</i> bit and it gates both answers, not just the positive
        one.
      </p>
      <p>
        <span class="chip">stall</span> and
        <span class="chip">miss</span> are then deliberately different widths:
        <b>hold on anything that is not a confirmed hit</b>, the stale cycle
        included, but <b>start a fill only once the tag is known</b>. One
        expression cannot do both jobs.
      </p>
    </Callout>

    <WaveTrace
      :rows="staleHit.rows"
      :notes="staleHit.notes"
      variant="broken"
      label="Reading the stale word as a hit — the previous line's data is returned"
    />

    <WaveTrace
      :rows="staleMiss.rows"
      :notes="staleMiss.notes"
      variant="broken"
      label="Reading the stale word as a miss — the eviction goes to the wrong address"
    />

    <WaveTrace
      :rows="staleFixed.rows"
      :notes="staleFixed.notes"
      variant="fixed"
      label="Freshness gates both answers — one cycle spent, both failures removed"
    />

    <Callout
      kind="trap"
      title="Both failures are silent, and one of them corrupts a line the access never named"
    >
      <p>
        The first returns the previous occupant's bytes to a load that hit
        nothing — a wrong value, with no fill and no fault. The second is worse
        and less obvious: the victim's tag and dirty bit are captured from the
        stale entry, so the writeback carries this index's data to
        <b>the address formed from a different line's tag</b>. Thirty-two bytes
        land somewhere the program never wrote, and the line that should have
        been saved is not.
      </p>
      <p>
        Neither presents at the cache. They present as a byte of DRAM being
        wrong some thousands of accesses later, three modules up — which is
        exactly why <span class="chip">rv64_l1</span> has a component bench with
        a reference model that checks every writeback against what the program
        stored, rather than only checking what reads back.
      </p>
    </Callout>

    <h3 class="doc-h3">The re-probe is two cycles, not one</h3>

    <Callout kind="rule" title="A tag write needs one cycle to commit and one more to be read back">
      <p>
        After a fill writes the tag, the machine <b>MUST NOT</b> return to idle
        until a read has been <i>presented</i> later than the cycle the write
        committed in. That is two states, and neither is slack: the write enable
        is itself a register, so the write lands a cycle after the state sets
        it, and the array is <b>read-first</b>, so a read presented in that same
        cycle still returns the old word.
      </p>
    </Callout>

    <WaveTrace
      :rows="reprobeBroken.rows"
      :notes="reprobeBroken.notes"
      variant="broken"
      label="One re-probe cycle — the line is filled, then immediately evicted"
    />

    <WaveTrace
      :rows="reprobeFixed.rows"
      :notes="reprobeFixed.notes"
      variant="fixed"
      label="Two re-probe cycles — the fill is visible to the retry"
    />

    <Callout kind="trap" title="A one-cycle re-probe hangs, it does not slow down">
      <p>
        The retry misses on the address just filled, chooses the line just
        written as its victim, evicts it, refills it, and misses again. There is
        no counter that saturates and no state that is illegal, so nothing
        asserts. <b>The symptom is a program that stops making progress on its
        first cached miss</b>, with a node port that looks healthy and busy.
      </p>
    </Callout>

    <Callout kind="rule" title="Three more details that are contract-shaped rather than incidental">
      <p>
        <b>A fill handshake tests both halves.</b> The port's ready is an idle
        indicator and is already high on entry, so testing it alone clears the
        request in the same cycle it is raised and the port never sees it.
      </p>
      <p>
        <b>The eviction exits on words <i>collected</i>, not on the address
        counter.</b> The address leads the read data by a cycle, so exiting on
        the address sends the line one word short and rotated wrong.
      </p>
      <p>
        <b>A read immediately after a write to the same word needs an extra
        cycle.</b> The data array is <span class="chip">no_change</span> on the
        CPU's port, so its read output <i>holds</i> while that port is writing,
        and sampling it the next cycle returns the pre-write value. A separate
        bit spends the cycle. The tell, when it is missing, is a check that
        fails while printing the value it wanted.
      </p>
    </Callout>

    <Callout
      kind="open"
      title="The dirty-bit shadow covers the cycle before the one that needs it"
    >
      <p>
        A store hit dirties its line by writing the tag array, whose read port is
        read-first, so a probe of that index reads the old bit. A one-entry
        shadow of the write in flight is OR-ed in to stop a just-dirtied line
        being evicted as clean. <b>The shadow is one cycle wide, and it is
        asserted in the cycle the write is pending.</b>
      </p>
      <p>
        That cycle is also the cycle in which the freshness guard is forced low
        by the store itself, so no miss can be declared in it — and the
        read-first window extends one cycle further, into the cycle where a miss
        <i>can</i> be declared and where the shadow has already dropped. On
        paper a store hit followed immediately by a conflicting miss at the same
        index would evict the just-dirtied line as clean and lose the store
        silently.
      </p>
      <p>
        <b>This has not been reproduced, and it may not be reachable.</b>
        Whether the wrapper ever presents the second access in that exact cycle
        is a property of the two-phase handshake, not of this module, and
        <span class="chip">rv64_l1</span>'s component bench cannot answer it:
        the bench drops its request line for a cycle between accesses, which
        moves the read one cycle later and past the window. It wants a directed
        bench — store, then a same-index different-tag access with no gap — and
        that bench does not exist yet.
      </p>
    </Callout>

    <Callout kind="note" title="The line buffer is a rotate, not an indexed register">
      <p>
        A fill walks words out in order and an eviction walks them in, so always
        taking the bottom word removes a 4:1 64-bit mux from the array's port.
        <b>The trick pays only where the register was already written
        word-at-a-time</b> — applied to a register loaded whole, the same
        construction <i>adds</i> logic.
      </p>
      <p>
        The two ports can address one word at once, which is undefined in silicon
        and not merely in the model, so the array wrapper asserts on the
        collision unless the instantiation declares it safe. Here it is declared
        safe with a reason: a fill collides with the stalled access's own word
        once per fill, and <b>that read is discarded</b> — the value is taken
        after the re-probe, when the other port is idle.
      </p>
    </Callout>

    <h2 class="doc-h2">The node-port arbiter</h2>

    <Fig
      caption="Four clients, fixed priority — and it is a priority mux rather than a queue because at most one can ever be active. A 64-bit access is placed into its lane of the 256-bit beat by two address bits, and its byte strobes are shifted into that lane."
      zoom
      wide
    >
      <BlockDiagram :nodes="arbiter.nodes" :edges="arbiter.edges" />
    </Fig>

    <Callout kind="rule" title="The invariant has three legs, and it is the first thing that breaks">
      <p>
        A walk runs to completion before the access that triggered it is
        retried, because the MMU holds the core while it walks; the L1 sequences
        its own eviction ahead of its own fill; and the core stalls on any node
        access, so it cannot issue a second one.
      </p>
      <p>
        Every one of the three is a property of something being <i>blocking</i>.
        Make the L1 non-blocking and this stops being a mux.
      </p>
    </Callout>

    <h2 class="doc-h2">What is cached and what is not</h2>

    <p class="doc-p">
      The decode is <b>bit tests, not magnitude compares</b>, because it sits in
      the stall path. Written as <code>&gt;=</code>/<code>&lt;</code> on 40 bits
      it cost 200 failing paths. Every range is power-of-two aligned and
      power-of-two sized on purpose, which is what lets each test be a single
      equality or a single bit rather than a pair of 40-bit comparisons.
    </p>

    <SpecTable :cols="cachedTests.cols" :rows="cachedTests.rows" />

    <SpecTable
      :cols="whatCached.cols"
      :rows="whatCached.rows"
      caption="Nothing that another agent writes is cached, which is the property that removes coherence. pa[31] is a bit test and reads literally: an address at 4 GB with bit 31 clear is uncached. This is a convention of the wrapper rather than a property of the machine, and a design that wants a different split changes the test"
    />

    <h2 class="doc-h2">What the core publishes about ordering</h2>

    <p class="doc-p">
      The compute-unit shell that the node processor does not have carried a
      guarantee with it: <i>every write this unit made is visible when its
      completion arrives</i>. Dropping the shell means the core has to publish
      its own. Here is what the memory path actually supports today.
    </p>

    <SpecTable :cols="ordering.cols" :rows="ordering.rows" />

    <Callout kind="rule" title="The rule to write software against">
      <p>
        <b>Anything another agent will read must be written through the uncached
        range.</b> Descriptors a mover will fetch, doorbells, mailbox words,
        completion flags. The cached range is for this core's own working set and
        nothing else. That is also why staging is uncached rather than merely
        happening to be: it is where the cross-agent structures live.
      </p>
    </Callout>

    <Callout kind="open" title="The obligation is not discharged">
      <p>
        For the cached range the core cannot yet publish anything equivalent to
        what the shell provided, and <b>that is a gap rather than a
        decision</b>. What is missing is a software-reachable flush and the
        ordering statement that would be built on it.
      </p>
    </Callout>

    <h2 class="doc-h2">What these modules cost</h2>

    <Callout kind="measured" title="Two contexts, one part, one tool">
      <p>
        <b>Out-of-context synthesis — not placed and not routed</b> — on
        <code>xcvu13p-fhgb2104-2L-e</code> under Vivado 2024.2 at a
        <b>3.333 ns request</b>. The <span class="chip">node</span> columns are
        the hierarchical rows of one run of
        <code>scripts/tcl/ooc_sysnode_rv64.tcl 2</code> —
        <code>sysnode</code> as top, <code>CPU_RV64 = 1</code>,
        <code>PORTS = 2</code>, the default <i>rebuilt</i> flattening the ship
        flow uses. The <span class="chip">none</span> column is
        <code>rv64_syscore</code> as its own top under
        <code>-flatten_hierarchy none</code>, where a row is an attribution
        rather than a re-parenting.
      </p>
      <p>
        <b>Do not subtract across the two columns.</b> They are different
        measurement contexts, not a before and an after.
      </p>
    </Callout>

    <SpecTable
      :cols="modules.cols"
      :rows="modules.rows"
      caption="The MMU is around a hundred LUT because the card is 40-bit physical, which is a result rather than an accident: the entry fits a block-RAM port, so the array costs no logic. The L1 is the same shape of result — per-line valid and dirty ride in the tag word rather than in flop arrays"
    />

    <Callout kind="trap" title="The MMU was 52 LUT when it was dead, and 103 now that it is not">
      <p>
        A module does not have a LUT count — it has one per measurement context,
        and this is the clearest case in the tree because the
        <i>difference</i> is a fact about the design rather than about the
        report. When the wrapper tied the privilege input to machine mode,
        <span class="chip">enabled</span> was a constant zero: the array fed
        nothing live, and synthesis removed the array and the walker together,
        leaving the power-on sweep counter. <b>Translation was present in the
        source and absent from the netlist.</b>
      </p>
      <p>
        Nothing was optimised to make it 103. Software can now write
        <span class="chip">priv</span> and <span class="chip">satp</span>, so
        nothing folds, and the module costs what it always cost.
        <b>A module that shrinks when you wire it in is a module that was not
        doing anything</b> — read the primitive columns as well as the LUT
        column, because the missing block RAM is the louder signal.
      </p>
    </Callout>

    <SpecTable
      :cols="mmuContexts.cols"
      :rows="mmuContexts.rows"
      caption="Three correct numbers for one module. The first row no longer describes anything that can be built; it is kept because it is the evidence for the rule above"
    />

    <h2 class="doc-h2">Sizing one</h2>

    <p class="doc-p">
      Every quantity on this page is arithmetic on four parameters, and all four
      are worth working out before elaborating anything — because the two
      failures that matter here are both silent. An array one bit too wide
      becomes LUTs, and a range that is not power-of-two aligned turns a bit
      test into a magnitude compare in the stall path.
    </p>

    <SpecTable
      :cols="sizing.cols"
      :rows="sizing.rows"
      caption="The whole memory system, as formulas. 22 in the first row is 1 valid + 27 VPN bits + 6 permission bits − 12 page-offset bits, collected once so that the two terms you control stand alone"
    />

    <h3 class="doc-h3">A procedure</h3>
    <ol class="doc-p list-decimal pl-5 space-y-1">
      <li>
        <b>Decide what is cached first</b>, because it is the only decision here
        that software can observe. The rule is not about size: anything a peer
        writes, or that this core wants a peer to read, is uncached. What is
        left is the private working set.
      </li>
      <li>
        <b>Place each region at a power-of-two-aligned, power-of-two-sized
        base</b>, and check that each range decode falls out as one equality or
        one bit. Written as
        <span class="chip">&gt;=</span>/<span class="chip">&lt;</span> on 40
        bits this decode cost <b>200 failing paths</b>; that is the whole reason
        the bases look the way they do.
      </li>
      <li>
        <b>Size the L1 against the working set, not against the miss rate.</b>
        Capacity is <span class="chip">LINES × 32 B</span> and the line count is
        nearly free in LUT, so the question is whether the set fits, not what
        the hit rate is — there is no associativity to trade against.
      </li>
      <li>
        <b>Size the TLB by reach.</b>
        <span class="chip">ENTRIES × 4 KB</span>, and 4 KB exactly whatever the
        page tables say, because a superpage is filled as the 4 KB slice the
        access asked for. 32 entries reach 128 KB, and a 1 GB mapping walked
        linearly refills the array every 128 KB.
      </li>
      <li>
        <b>Compute both array widths and compare them against 72.</b>
        <span class="chip">22 + ADDR_W − log₂ ENTRIES</span> for the TLB and
        <span class="chip">ADDR_W − 3 − log₂ LINES</span> for the L1's tag. If
        the TLB entry is over, doubling the entries buys a bit back.
      </li>
      <li>
        <b>Synthesise out of context and read the primitive column</b>, not the
        LUT column. Block RAM where you expected block RAM is the check; there
        is no warning for the other answer.
      </li>
      <li>
        <b>Then read the LUT column against the context you will ship in</b>,
        and be suspicious of a module that <i>shrinks</i> when assembled. The
        MMU reported 52 LUT and no block RAM while its privilege input was tied
        to a constant, and 103 with one block RAM once software could write
        that input. Neither figure is wrong; only one of them is a memory
        system.
      </li>
      <li>
        <b>Give any structure two requesters can reach one set of ownership
        rules</b>, written down. The three on this page — the walk latches its
        own request, “resolved” is qualified by same-request, and a fault holds
        only its owner — are each one line of RTL and each removed a wrong
        answer that no single-requester bench can produce.
      </li>
    </ol>

    <Callout kind="open" title="Open questions the flow does not answer">
      <p>
        <b>Nothing checks the entry width against the primitive.</b> The
        arithmetic in step 5 is performed by hand, and the failure it prevents
        is invisible in simulation and silent in synthesis. It is a one-line
        elaboration assertion nobody has written.
      </p>
      <p>
        <b>Nothing checks that the ranges do not overlap</b>, or that a base is
        aligned to its size. A control region placed inside the scratchpad's
        range would decode as both, and the priority between the four select
        bits would decide which — quietly.
      </p>
      <p>
        <b>The cached/uncached split is one bit and one convention.</b> There is
        no mechanism that ties it to what the node actually maps, so a peer
        window placed above <span class="chip">CACHE_LO</span> becomes cacheable
        and the coherence argument on this page stops holding, with nothing to
        say so.
      </p>
      <p>
        <b>No cycle figure has been taken for a translated access.</b> Sv39 is
        exercised by a component bench and by a program that walks three-level
        tables in hardware, takes a delegated load page fault and resumes — so
        it works, and the extra cycle a translated hit costs is a property of
        the design rather than a measurement. Nobody has run the same program
        with and without translation and counted.
      </p>
      <p>
        <b>And a fetch-heavy and a load-heavy phase share one 32-entry array.</b>
        There is no counter that reports TLB pressure, no way to tell a refill
        caused by a page crossing from one caused by the other requester, and
        nothing in the flow that predicts either from a page table.
      </p>
    </Callout>

    <h2 class="doc-h2">What this memory system deliberately does not do</h2>

    <SpecTable :cols="absent.cols" :rows="absent.rows" />
  </DocPage>
</template>
