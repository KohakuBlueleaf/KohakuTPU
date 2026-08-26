<script setup>
// ===========================================================================
// Matmul cluster — microarchitecture.
// Drawn from the RTL, not from the prose: src/kohakutpu/matmul/*.v and
// src/kohakutpu/transform/mx_quant.v. The WHAT is /tpu/matmul; this is the
// circuit. Every trap below is a failure the source records as having
// happened, and where the RTL and the prose doc disagree, both are shown.
// Every measured figure is xcvu13p-fhgb2104-2L-e, out-of-context synthesis,
// conditions in docs/projects/kohakutpu/results.md.
// ===========================================================================

// ------------------------------------------------------------- 1. the cell
// mx_mac.v. Ports on the left, the DSP48E2's own register stages across.
const mac = {
  nodes: [
    {
      id: "a",
      x: 0,
      y: 0,
      w: 14,
      label: "A (30) = w_hi << 19",
      sub: "pure wiring",
    },
    {
      id: "d",
      x: 0,
      y: 4.2,
      w: 14,
      label: "D (27) = w_lo",
      sub: "pure wiring",
    },
    {
      id: "b",
      x: 0,
      y: 8.6,
      w: 14,
      label: "B (18) = act",
      sub: "one activation, shared",
    },
    {
      id: "c",
      x: 0,
      y: 13.4,
      w: 14,
      label: "C (48)",
      sub: "the previous TCU's partial",
    },
    {
      id: "pc",
      x: 0,
      y: 17.8,
      w: 14,
      label: "PCIN (48)",
      sub: "the DSP above, in the same column",
    },

    {
      id: "areg",
      x: 16.5,
      y: 2.1,
      w: 12,
      label: "AREG = 1 · DREG = 1",
      sub: "ACASCREG = 1",
    },
    {
      id: "breg",
      x: 16.5,
      y: 8.6,
      w: 12,
      label: "BREG = 2",
      sub: "BCASCREG = 2 — not 1",
    },
    {
      id: "creg",
      x: 16.5,
      y: 13.4,
      w: 12,
      label: "CREG = 1",
      sub: "one stage",
    },

    {
      id: "ad",
      x: 31,
      y: 2.1,
      w: 13,
      label: "ADREG — pre-adder",
      sub: "AMULTSEL = AD",
      accent: true,
    },
    {
      id: "m",
      x: 46.5,
      y: 5.4,
      w: 13,
      label: "MREG — 27 x 18",
      sub: "the packed product",
      accent: true,
    },
    {
      id: "alu",
      x: 62,
      y: 9.5,
      w: 13,
      h: 4.4,
      label: "ALU  Z + W + X + Y",
      sub: "ALUMODE = 0000",
      accent: true,
    },
    {
      id: "p",
      x: 77.5,
      y: 9.5,
      w: 13,
      h: 4.4,
      label: "PREG",
      sub: "P (48) and PCOUT (48)",
      accent: true,
    },
  ],
  edges: [
    { from: "a:r", to: "areg:l", dir: "h" },
    { from: "d:r", to: "areg:l", dir: "h" },
    { from: "areg:r", to: "ad:l", dir: "h", accent: true },
    { from: "ad:r", to: "m:l", dir: "h", accent: true },
    { from: "b:r", to: "breg:l", dir: "h" },
    { from: "breg:r", to: "m:l", dir: "h" },
    { from: "m:r", to: "alu:l", dir: "h", accent: true, label: "X = Y = M" },
    { from: "c:r", to: "creg:l", dir: "h" },
    { from: "creg:r", to: "alu:l", dir: "h", label: "W" },
    { from: "pc:r", to: "alu:l", dir: "h", label: "Z" },
    { from: "alu:r", to: "p:l", dir: "h", accent: true },
  ],
};

const opmode = [
  { name: "W", bits: 2, value: "11 = C, 00 = zero", accent: true },
  { name: "Z", bits: 3, value: "001 = PCIN, 000 = zero", accent: true },
  { name: "Y", bits: 2, value: "01 = M" },
  { name: "X", bits: 2, value: "01 = M" },
];

const macLatency = {
  cols: [
    { key: "p", label: "path", mono: true },
    { key: "s", label: "register stages", mono: true },
    { key: "w", label: "which", mono: true },
  ],
  rows: [
    { p: "operand → P", s: "<b>4</b>", w: "A/D/B reg, AD reg, M reg, P reg" },
    { p: "PCIN → P", s: "<b>1</b>", w: "PCIN enters at the ALU, then P reg" },
    { p: "C → P", s: "<b>2</b>", w: "C reg, then the ALU, then P reg" },
  ],
};

// The DSP register-configuration bug, from results.md §9.3 and mx_mac.v's own
// comment. Streaming is the only thing that exposes it.
const bregBroken = {
  rows: [
    { name: "A / D presented", kind: "bus", values: ["T0", "T1", "T2", "T3"] },
    { name: "B presented", kind: "bus", values: ["T0", "T1", "T2", "T3"] },
    {
      name: "AD at the multiplier",
      kind: "bus",
      values: [null, null, "T0", "T1"],
    },
    {
      name: "B at the multiplier (BREG=1)",
      kind: "bus",
      values: [null, "T0", "T1", "T2"],
      mark: [2, 3],
    },
    {
      name: "M",
      kind: "bus",
      values: [null, null, "T0 x T1", "T1 x T2"],
      mark: [2, 3],
    },
    {
      name: "",
      kind: "text",
      values: ["", "", "wrong operand", "wrong operand"],
    },
  ],
  notes: [
    {
      cycle: 2,
      text: "With AMULTSEL = AD the A/D path reaches the multiplier through AREG and ADREG — two stages — while a single-stage B arrives a cycle early and multiplies against the next tile's activation.",
      tone: "bad",
    },
    {
      text: "INVISIBLE WITH STABLE OPERANDS: if T0 = T1 = T2 every stage is looking at the same tile and the misalignment cancels. The behavioural model passed and the real DSP48E2 failed only in the streaming section, which is what pointed at the DSP configuration rather than at the arithmetic.",
      tone: "bad",
    },
  ],
};

const bregFixed = {
  rows: [
    { name: "A / D presented", kind: "bus", values: ["T0", "T1", "T2", "T3"] },
    { name: "B presented", kind: "bus", values: ["T0", "T1", "T2", "T3"] },
    {
      name: "AD at the multiplier",
      kind: "bus",
      values: [null, null, "T0", "T1"],
    },
    {
      name: "B at the multiplier (BREG=2)",
      kind: "bus",
      values: [null, null, "T0", "T1"],
      mark: [2, 3],
    },
    {
      name: "M",
      kind: "bus",
      values: [null, null, "T0 x T0", "T1 x T1"],
      mark: [2, 3],
    },
  ],
  notes: [
    {
      text: "BREG = 2, BCASCREG = 2. The B path is given the same depth the pre-adder path has.",
      tone: "good",
    },
    {
      text: "The simulation library also holds global set/reset asserted for the first 100 ns, so unisim registers ignore everything before that whatever the design's own reset does. Without waiting past it the first tile silently produces nothing.",
    },
  ],
};

// ------------------------------------------------ 2. the chain, and the W path
// mx_tcu.v / mx_cluster_core.v. One column of one chain, four TCUs across.
const K = [
  ["0", "1 .. 6", "7"],
  ["8", "9 .. 14", "15"],
  ["16", "17 .. 22", "23"],
  ["24", "25 .. 30", "31"],
];
const chain = {
  nodes: [
    ...K.flatMap((ks, c) => [
      {
        id: `op${c}`,
        x: c * 17,
        y: 0,
        w: 15,
        h: 3,
        label: "operands",
        sub: c === 0 ? "delay 0" : `delay ${2 * c}`,
      },
      {
        id: `s0${c}`,
        x: c * 17,
        y: 5.4,
        w: 15,
        h: 2.7,
        label: `k = ${ks[0]}`,
        sub: "Z = 0",
      },
      {
        id: `sm${c}`,
        x: c * 17,
        y: 9.2,
        w: 15,
        h: 2.7,
        label: `k = ${ks[1]}`,
        sub: "Z = PCIN",
      },
      {
        id: `s7${c}`,
        x: c * 17,
        y: 13,
        w: 15,
        h: 2.7,
        label: `k = ${ks[2]}`,
        sub: c === 0 ? "W = 0" : "W = C",
        accent: c > 0,
      },
    ]),
    {
      id: "out",
      x: 69,
      y: 13,
      w: 14,
      h: 2.7,
      label: "part_out",
      sub: "8 chains x 48 b",
      accent: true,
    },
  ],
  edges: [
    ...K.flatMap((_, c) => [
      { from: `op${c}:b`, to: `s0${c}:t`, dir: "v" },
      { from: `s0${c}:b`, to: `sm${c}:t`, dir: "v" },
      { from: `sm${c}:b`, to: `s7${c}:t`, dir: "v" },
    ]),
    { from: "s70:r", to: "s71:l", dir: "h", accent: true, label: "C" },
    { from: "s71:r", to: "s72:l", dir: "h", accent: true, label: "C" },
    { from: "s72:r", to: "s73:l", dir: "h", accent: true, label: "C" },
    { from: "s73:r", to: "out:l", dir: "h", accent: true },
  ],
  groups: [
    {
      x: -1.2,
      y: 4.2,
      w: 68.4,
      h: 12.7,
      label:
        "PCOUT -> PCIN inside a column: dedicated silicon, no fabric, no LUTs",
    },
  ],
};

const wBroken = {
  rows: [
    {
      name: "TCU 0 part_out",
      kind: "bus",
      values: ["zero", "zero", "T0", "T1", "T2"],
    },
    {
      name: "TCU 1 K 8..15 products (delay 1)",
      kind: "bus",
      values: [null, null, "T0", "T1", "T2"],
    },
    {
      name: "TCU 1 stage-7 W input",
      kind: "bus",
      values: [null, null, "zero", "T0", "T1"],
      mark: [2, 3],
    },
    {
      name: "TCU 1 part_out",
      kind: "bus",
      values: [
        null,
        null,
        "T0: K8..15 only",
        "T1 + T0's K0..7",
        "T2 + T1's K0..7",
      ],
      mark: [2, 3, 4],
    },
    {
      name: "",
      kind: "text",
      values: ["", "", "T0's K 0..7 LOST", "wrong tile", "wrong tile"],
    },
  ],
  notes: [
    {
      cycle: 2,
      text: "C is registered once (CREG = 1), so the ALU consumes c_r two cycles after the value was presented. At one cycle of operand delay per TCU the downstream stage-7 samples its neighbour's partial a cycle early — when it still holds the previous tile's value, or zero on the first tile.",
      tone: "bad",
    },
    {
      text: "Half the products are silently dropped and the other half are summed against the wrong tile. Nothing errors.",
      tone: "bad",
    },
  ],
};

const wFixed = {
  rows: [
    {
      name: "TCU 0 part_out",
      kind: "bus",
      values: ["zero", "zero", "T0", "T1", "T2"],
    },
    {
      name: "TCU 1 K 8..15 products (delay 2)",
      kind: "bus",
      values: [null, null, null, "T0", "T1"],
    },
    {
      name: "TCU 1 stage-7 W input",
      kind: "bus",
      values: [null, null, null, "T0", "T1"],
      mark: [3, 4],
    },
    {
      name: "TCU 1 part_out",
      kind: "bus",
      values: [null, null, null, "T0: K 0..15", "T1: K 0..15"],
      mark: [3, 4],
    },
  ],
  notes: [
    {
      text: "d_c = d_{c-1} + 2. TCU c's stage-7 P lands after E(11 + d_c); it consumes cin valid after E(9 + d_c); TCU c-1's part_out is valid after E(11 + d_{c-1}). The operand delays are therefore 0, 2, 4, 6 and the whole core is 11 + 2·(NTCU−1) = 17 cycles.",
      tone: "good",
    },
  ],
};

const zVsW = {
  cols: [
    { key: "w", label: "where the upstream partial enters" },
    { key: "s", label: "ALU slot", mono: true },
    { key: "c", label: "what it costs" },
  ],
  rows: [
    {
      w: "<b>the LAST stage, on <code>W</code></b> — what ships",
      s: "W = C",
      c: "the two results are <b>contemporary</b>. Only the <code>C</code> register's own alignment is owed: <b>2 cycles of operand delay per TCU</b>, so the delays are 0, 2, 4, 6 and the core is 17 cycles deep",
      _tone: "good",
    },
    {
      w: "stage 0, on <code>Z</code> — the arrangement that lost",
      s: "Z = C",
      c: "the upstream TCU's result would have to be ready <b>before this TCU starts</b>: <b>8 cycles of operand skew per TCU</b>, four times the delay lines for the same arithmetic",
      _tone: "warn",
    },
  ],
};

// -------------------------------------------------------- 3. the operand skew
const bundle = [
  { name: "A[0][k]", bits: 7, accent: true },
  { name: "A[1][k]", bits: 7, accent: true },
  { name: "A[2][k]", bits: 7, accent: true },
  { name: "A[3][k]", bits: 7, accent: true },
  { name: "B[k][0]", bits: 7 },
  { name: "B[k][1]", bits: 7 },
  { name: "B[k][2]", bits: 7 },
  { name: "B[k][3]", bits: 7 },
];

const skewCost = {
  cols: [
    { key: "s", label: "the skew, as", mono: true },
    { key: "d", label: "depths in play", mono: true },
    { key: "c", label: "cost" },
    { key: "b", label: "build" },
  ],
  rows: [
    {
      s: "SRL16E / SRL32",
      d: "2 … 7 inside a TCU, 2 / 4 / 6 between them",
      c: "<b>one LUT per bit at ANY depth</b> — a full LUT to use at most 7 of 16 stages",
      b: "the build results.md §2.1 measures: <b>1,458 LUT of SRL</b> in a 4,751-LUT cluster — 224 of TCU 0's 336, 280 in each of TCUs 1–3, 394 of the operand delay's 450",
      _tone: "warn",
    },
    {
      s: "<b>flip-flops</b>, <code>shreg_extract = &quot;no&quot;</code>",
      d: "the same",
      c: "one FF per bit per stage, and <b>no LUT</b>. The device is LUT-bound at 66% with FF at 34%, so the skew moves into the half of the CLB that is idle",
      b: "what <code>mx_tcu.v</code> and <code>mx_cluster_core.v</code> build today — the attribute is on the delay arrays in both",
      _tone: "good",
    },
  ],
};

// ------------------------------------------------------ 4. the K sweep player
// GEMM gm=4 gn=8 nk=4 — C[16,32] = A[16,128] @ B.T[32,128]. The addresses are
// mx_cluster_mgr's own expression: aoff + g*nk + kb, boff + h*nk + kb.
const NK = 4;
const sweepSteps = [
  {
    title: "K block 0 — LOAD opens the tile",
    kb: 0,
    cmd: "OP_LOAD",
    val: "tile[0] = Σ over K 0..31",
    issues: 32,
    macs: "16,384",
    entries: 12,
    out: "0",
    note: "acc is 0 and kb is 0, so s1_first is set and every one of the 32 issues in this block is OP_LOAD: align operand A is forced to zero rather than read from the RAM. That is the only thing that opens a tile — the block RAM has no reset, so an address holds x until it is written, and an OP_ADD into a never-loaded tile reads x.",
  },
  {
    title: "K block 1 — the operands move, the address does not",
    kb: 1,
    cmd: "OP_ADD",
    val: "tile[0] = Σ over K 0..63",
    issues: 64,
    macs: "32,768",
    entries: 24,
    out: "0",
    note: "Twelve different L1 entries, the same 32 tile addresses. This is the whole architecture in one line: K is the OUTERMOST loop of the sweep, so a tile address recurs every gm·gn = 32 cycles rather than every cycle, and the accumulator can be a plain memory with a synchronous read and a single bank.",
  },
  {
    title: "K block 2 — still nothing has left",
    kb: 2,
    cmd: "OP_ADD",
    val: "tile[0] = Σ over K 0..95",
    issues: 96,
    macs: "49,152",
    entries: 36,
    out: "0",
    note: "Output traffic so far is zero bytes. The tile is working storage, not a staging pipe — and because K cancels out of the arithmetic intensity M·N·K / (M·K + K·N), sweeping more K buys reuse for free while chaining more compute units buys none.",
  },
  {
    title:
      "K block 3 — ADD_EMIT, and the value leaves on the command that made it",
    kb: 3,
    cmd: "OP_ADD_EMIT",
    val: "tile[0] = Σ over K 0..127 — and out",
    issues: 128,
    macs: "65,536",
    entries: 48,
    out: "1,024",
    note: "on_last_kb is set for EVERY issue of the final K block, not only the last one, because the last K block completes every sub-tile it touches. ADD_EMIT is ADD's operand selects with EMIT's output: it adds nothing at all to stage 3, the path the cluster binds on.",
  },
  {
    title: "the ledger",
    kb: null,
    cmd: "—",
    val: "tile[0] read 4 times, written 4 times",
    issues: 128,
    macs: "65,536",
    entries: 48,
    out: "1,024",
    note: "48 distinct L1 entries — 12 KB of operand — produced 65,536 MACs and 1 KB of result. The L1 RAMs were read 256 times to touch those 48 entries: the reuse is in the sweep order, not in a cache. And the resident tile was addressed 128 times without its base ever moving.",
  },
];
const entA = (kb) => (kb === null ? [] : [0, 1, 2, 3].map((g) => g * NK + kb));
const entB = (kb) =>
  kb === null ? [] : [0, 1, 2, 3, 4, 5, 6, 7].map((h) => h * NK + kb);

// ---------------------------------------------------- 5. the accumulator path
const acu = {
  nodes: [
    {
      id: "s1",
      x: 0,
      y: 0,
      w: 11.5,
      h: 4.6,
      label: "stage 1",
      sub: "|v| x block scale — 16 DSP",
    },
    {
      id: "s2a",
      x: 12.5,
      y: 0,
      w: 11.5,
      h: 4.6,
      label: "stage 2a",
      sub: "leading one -> one-hot",
    },
    {
      id: "s2a2",
      x: 25,
      y: 0,
      w: 11.5,
      h: 4.6,
      label: "stage 2a2",
      sub: "the shift — 32 DSP",
    },
    {
      id: "s2b",
      x: 37.5,
      y: 0,
      w: 11.5,
      h: 4.6,
      label: "stage 2b",
      sub: "round, assemble -> FP22",
    },
    {
      id: "s3",
      x: 50,
      y: 0,
      w: 11.5,
      h: 4.6,
      label: "stage 3",
      sub: "READ the tile, align",
      accent: true,
    },
    {
      id: "s4",
      x: 62.5,
      y: 0,
      w: 11.5,
      h: 4.6,
      label: "stage 4",
      sub: "add, leading one, shift",
    },
    {
      id: "s5",
      x: 75,
      y: 0,
      w: 11.5,
      h: 4.6,
      label: "stage 5",
      sub: "round, WRITE the tile",
      accent: true,
    },
    {
      id: "s6",
      x: 87.5,
      y: 0,
      w: 11.5,
      h: 4.6,
      label: "stage 6",
      sub: "EMIT only: -> FP16",
    },
    {
      id: "addr",
      x: 25,
      y: 9.5,
      w: 11.5,
      h: 3.4,
      label: "tile_addr",
      sub: "presented at stage 2a",
    },
    {
      id: "tile",
      x: 50,
      y: 9.5,
      w: 36.5,
      h: 3.4,
      label: "kohaku_sdpram  352 x DEPTH",
      sub: "block RAM, READ_LAT = 2, ONE bank",
      accent: true,
    },
  ],
  edges: [
    { from: "s1:r", to: "s2a:l", dir: "h" },
    { from: "s2a:r", to: "s2a2:l", dir: "h" },
    { from: "s2a2:r", to: "s2b:l", dir: "h" },
    { from: "s2b:r", to: "s3:l", dir: "h", accent: true },
    { from: "s3:r", to: "s4:l", dir: "h" },
    { from: "s4:r", to: "s5:l", dir: "h" },
    { from: "s5:r", to: "s6:l", dir: "h" },
    { from: "addr:r", to: "tile:l", dir: "h" },
    { from: "tile:t", to: "s3:b", dir: "v", accent: true },
    { from: "s5:b", to: "tile:t", dir: "v", accent: true },
  ],
  groups: [
    {
      x: 49,
      y: -1.4,
      w: 38.5,
      h: 15.6,
      label:
        "the accumulate loop — read to write is 3 cycles, and REUSE_MIN = 5 covers it",
    },
  ],
};

const pWord = [
  { name: "sign extension", bits: 7, value: "P[47:41], unread" },
  {
    name: "upper — sum of w_hi·a",
    bits: 22,
    value: "P[40:19], sign at 40",
    accent: true,
  },
  {
    name: "lower — sum of w_lo·a",
    bits: 19,
    value: "P[18:0], sign at 18",
    accent: true,
  },
];

const accFloat = [
  { name: "S", bits: 1, value: "sign" },
  {
    name: "E",
    bits: 7,
    value: "bias 63 — fixed, not the tunable",
    accent: true,
  },
  { name: "M", bits: 14, value: "ACC_MW, implicit leading 1", accent: true },
];

const widths = {
  cols: [
    { key: "n", label: "name", mono: true },
    { key: "v", label: "value", mono: true, align: "right" },
    { key: "w", label: "why that and not more" },
  ],
  rows: [
    {
      n: "VW",
      v: "22",
      w: "not 29. A K=32 block can only reach ±131,072 — 18 bits and a sign — so a 29-bit leading-one search and shifter, <b>sixteen times over</b>, would be built for range that cannot occur",
      _tone: "good",
    },
    {
      n: "VWM",
      v: "30",
      w: "<code>VW + 8</code>. The block-scale mantissa product <code>m8a·m8b</code> is in [64, 225], so the partial sum comes out 8 bits wider",
    },
    {
      n: "NS",
      v: "15",
      w: "<code>VWM − ACC_MW − 1</code>. Derived, never passed in: everything at or below the guard bit has to come from the low half or the slices above it are not constant",
    },
    { n: "AW", v: "22", w: "<code>ACC_MW + 8</code> — one accumulator float" },
    {
      n: "TW",
      v: "352",
      w: "<code>16 · AW</code>, one 4x4 sub-tile. <b>Not padded to a multiple of 72</b>: padding to 360 measured −23 LUT, −8 control sets, the same 5 URAM and the same 308.8 MHz on the cluster",
    },
  ],
};

const shiftFit = {
  cols: [
    { key: "p", label: "the problem" },
    { key: "s", label: "what made it fit" },
  ],
  rows: [
    {
      p: "the one-hot <code>2^k</code> has to come from somewhere",
      s: "it is <code>mx_lead1</code>'s isolated bit <b>reversed</b> — the leading-one unit already produces <code>2^msb</code> on the way to encoding the position, and reversing a bit vector is wiring",
    },
    {
      p: "<code>k</code> spans 30 positions, the B port is 18 bits",
      s: "the low four bits pick the one-hot; the fifth stays in fabric as <code>hi</code>, a <b>slice select</b> on the product rather than a second shifter",
    },
    {
      p: "the magnitude is 30 bits, the A port is 27",
      s: "split at <code>NS</code> and multiplied by the SAME one-hot, the two halves land in disjoint bit ranges — so they reassemble with an <b>OR, not an adder</b>. Two DSPs per lane",
    },
  ],
};

const shiftTrade = [
  {
    label: "fabric barrel shifter, 16 copies",
    value: 1200,
    note: "· 704 FF · 0 DSP",
    tone: "warn",
  },
  {
    label: "multiply by a one-hot, 16 copies",
    value: 288,
    note: "· 496 FF · 16 DSP",
    tone: "good",
  },
];

const precision = {
  cols: [
    { key: "w", label: "where" },
    { key: "e", label: "effect" },
  ],
  rows: [
    {
      w: "<b>the K-sweep depth</b>",
      e: "a 32-block sweep is 32 roundings deep. Measured across mantissa widths there is a <b>cliff between 22 and 20 bits, not between 24 and 20</b>: MW = 14 and MW = 16 both land at about a third of an FP16 ULP, and below 14 the error jumps by an order of magnitude. The K depth, not the output format, sets that floor",
      _tone: "good",
    },
    {
      w: "the alignment sticky at stage 3",
      e: "discarded alignment residue is carried as a plain sticky. A later bench built to oversample exponent-distant addends found the effective-subtraction corner where that reads exactly one ulp high — <b>19 of 4,000</b> on that stream — which moved the known bound from 0.5 to 1 ulp. The same construction is in <code>mx_fpacc</code>'s split path",
    },
    {
      w: "<b>stage 6, the FP16 convert</b>",
      e: "the accumulator's own range is roughly 2^64 and the value survives the whole reduction intact. It is <b>destroyed on the way out</b>: <code>mx_fpacc_to_fp16</code> clamps at 65,504 <i>silently</i>. For mean-nonzero operands the sum grows as K·μa·μb, so K = 2048 overflows once μa·μb exceeds 32 — every post-ReLU activation is non-negative, so that is not an exotic condition",
      _tone: "bad",
    },
  ],
};

const dspCensus = [
  {
    label: "mx_mac cascade — 4 TCU x 64",
    value: 256,
    note: "the multiply AND all of K=32",
  },
  {
    label: "block-scale multiply — one per lane",
    value: 16,
    note: "mx_acu_fp stage 1",
  },
  {
    label: "normalising shift — two per lane",
    value: 32,
    note: "mx_acu_fp stage 2a2",
  },
];

// ------------------------------------------------------- 6. REUSE_MIN and pace
const reuse = {
  cols: [
    { key: "w", label: "who encodes it", mono: true },
    { key: "a", label: "as", mono: true },
    { key: "s", label: "can it see the localparam?" },
  ],
  rows: [
    {
      w: "mx_acu_fp / mx_acu_fp_pump",
      a: "<code>localparam REUSE_MIN = 5</code>",
      s: "<b>the source of truth</b>",
      _tone: "good",
    },
    {
      w: "mx_cluster_mgr",
      a: "pacing buckets — <code>t_is1 / t_is2 / t_small</code>",
      s: "<b>no.</b> Elaboration check only",
      _tone: "bad",
    },
    {
      w: "mx_cluster_cu",
      a: "<code>i_wide</code>",
      s: "<b>no.</b> Elaboration check only",
      _tone: "bad",
    },
  ],
};

const pace = {
  cols: [
    { key: "t", label: "Gm x Gn", mono: true },
    {
      key: "p",
      label: "gap between two commands to one address",
      mono: true,
      align: "right",
    },
    { key: "i", label: "idle cycles inserted", mono: true, align: "right" },
  ],
  rows: [
    { t: "1 x 1", p: "1 cycle", i: "<b>4</b>", _tone: "warn" },
    { t: "1 x 2, 2 x 1", p: "2 cycles", i: "<b>2</b>", _tone: "warn" },
    {
      t: "1 x 3, 1 x 4, 3 x 1, 4 x 1, 2 x 2",
      p: "3 or 4 cycles",
      i: "<b>1</b>",
      _tone: "warn",
    },
    {
      t: "everything else",
      p: "Gm·Gn ≥ 5 cycles",
      i: "<b>0</b>",
      _tone: "good",
    },
  ],
};

// -------------------------------------------------------------- 7. the manager
const mgr = {
  nodes: [
    {
      id: "cnt",
      x: 0,
      y: 0,
      w: 14,
      h: 4,
      label: "counters",
      sub: "for kb: for g: for h",
    },
    {
      id: "adr",
      x: 16,
      y: 0,
      w: 14,
      h: 4,
      label: "a_rd / b_rd",
      sub: "aoff + g·nk + kb",
    },
    {
      id: "l1",
      x: 32,
      y: 0,
      w: 14,
      h: 4,
      label: "u_l1a / u_l1b",
      sub: "928 b, READ_LAT = 1",
      accent: true,
    },
    { id: "s1", x: 48, y: 0, w: 11, h: 4, label: "s1", sub: "address issued" },
    {
      id: "s1b",
      x: 61,
      y: 0,
      w: 11,
      h: 4,
      label: "s1b",
      sub: "data valid here",
      accent: true,
    },
    {
      id: "s2",
      x: 74,
      y: 0,
      w: 11,
      h: 4,
      label: "s2",
      sub: "scales split off",
    },
    {
      id: "core",
      x: 61,
      y: 7.5,
      w: 24,
      h: 3.6,
      label: "a_out / b_out -> the cascade",
      sub: "896 b each, elements only",
    },
    {
      id: "fifo",
      x: 61,
      y: 13,
      w: 24,
      h: 3.6,
      label: "ACU command FIFO",
      sub: "depth 64, popped by part_valid",
      accent: true,
    },
    {
      id: "acu",
      x: 89,
      y: 13,
      w: 14,
      h: 3.6,
      label: "mx_acu_fp",
      sub: "op, addr, sa, sb, anchor",
      accent: true,
    },
  ],
  edges: [
    { from: "cnt:r", to: "adr:l", dir: "h" },
    { from: "adr:r", to: "l1:l", dir: "h" },
    { from: "l1:r", to: "s1:l", dir: "h" },
    { from: "s1:r", to: "s1b:l", dir: "h" },
    { from: "s1b:r", to: "s2:l", dir: "h" },
    { from: "s1b:b", to: "core:t", dir: "v", accent: true },
    { from: "s2:b", to: "fifo:t", dir: "v", accent: true },
    { from: "fifo:r", to: "acu:l", dir: "h", accent: true },
  ],
};

const dlyBroken = {
  rows: [
    {
      name: "a_rd assigned by the counters",
      kind: "bus",
      values: ["A0", "A1", "A2", "A3"],
    },
    {
      name: "address the RAM sees",
      kind: "bus",
      values: [null, "A0", "A1", "A2"],
    },
    {
      name: "a_ent valid (READ_LAT = 1)",
      kind: "bus",
      values: [null, null, "A0", "A1"],
    },
    { name: "s1_valid", kind: "bit", values: [0, 1, 1, 1] },
    {
      name: "consumed alongside s1_*",
      kind: "bus",
      values: [null, "prev", "A0", "A1"],
      mark: [1, 2, 3],
    },
    { name: "", kind: "text", values: ["", "one entry early", "", ""] },
  ],
  notes: [
    {
      cycle: 1,
      text: "The counters assign a_rd at cycle T, the RAM sees it during T+1, and with READ_LAT = 1 the data is valid during T+2. Consuming a_ent alongside s1_* reads the PREVIOUS entry.",
      tone: "bad",
    },
    {
      text: "Every result shifts by one sub-tile — structured and silent, and it reads as an addressing bug rather than a timing one.",
      tone: "bad",
    },
  ],
};

const dlyFixed = {
  rows: [
    {
      name: "a_rd assigned by the counters",
      kind: "bus",
      values: ["A0", "A1", "A2", "A3"],
    },
    {
      name: "address the RAM sees",
      kind: "bus",
      values: [null, "A0", "A1", "A2"],
    },
    {
      name: "a_ent valid (READ_LAT = 1)",
      kind: "bus",
      values: [null, null, "A0", "A1"],
    },
    { name: "s1b_valid", kind: "bit", values: [0, 0, 1, 1] },
    {
      name: "consumed alongside s1b_*",
      kind: "bus",
      values: [null, null, "A0", "A1"],
      mark: [2, 3],
    },
  ],
  notes: [
    {
      text: "TWO cycles of control delay, not one. An explicit READ_LAT parameter is what makes the requirement visible at all — an inferred RAM would have hidden it in whatever synthesis chose.",
      tone: "good",
    },
  ],
};

const l1Entry = [
  {
    name: "32 x int7 elements",
    bits: 896,
    value: "4 lanes x 32 K",
    accent: true,
  },
  { name: "4 x E5M3", bits: 32, value: "one block scale per lane" },
];

// ------------------------------------------------------------ 8. the pump
const pump = {
  nodes: [
    {
      id: "div",
      x: 0,
      y: 0,
      w: 17,
      h: 4,
      label: "BUFGCE_DIV /2",
      sub: "div_clr shared, CLR NOT tied off",
      accent: true,
    },
    {
      id: "l1",
      x: 0,
      y: 7,
      w: 17,
      h: 4,
      label: "u_l1a / u_l1b",
      sub: "read on clk2x",
    },
    {
      id: "casc",
      x: 19,
      y: 7,
      w: 17,
      h: 4,
      label: "mx_cluster_core",
      sub: "256 DSP on clk2x — RTL unchanged",
    },
    {
      id: "pair",
      x: 38,
      y: 7,
      w: 17,
      h: 4,
      label: "the pair latch",
      sub: "p_lo / p_hi, pp follows VALID",
    },
    {
      id: "mrg",
      x: 57,
      y: 7,
      w: 17,
      h: 4,
      label: "the merge — a float add",
      sub: "16 lanes, _pump ONLY",
      accent: true,
    },
    {
      id: "acu",
      x: 76,
      y: 7,
      w: 17,
      h: 4,
      label: "stages 1 .. 6",
      sub: "one tile RMW, REUSE_MIN 5",
      accent: true,
    },
    {
      id: "pv",
      x: 38,
      y: 13.5,
      w: 17,
      h: 3.4,
      label: "part_valid, 2 cycles wide",
      sub: "so the clk1x edge cannot miss it",
    },
  ],
  edges: [
    { from: "l1:r", to: "casc:l", dir: "h" },
    { from: "casc:r", to: "pair:l", dir: "h" },
    { from: "pair:r", to: "mrg:l", dir: "h", accent: true },
    { from: "mrg:r", to: "acu:l", dir: "h", accent: true },
    { from: "div:b", to: "l1:t", dir: "v" },
    { from: "pair:b", to: "pv:t", dir: "v" },
  ],
  groups: [
    { x: -1.2, y: 5.8, w: 57.4, h: 12.3, label: "clk2x" },
    { x: 56, y: 5.8, w: 38.2, h: 6.6, label: "clk1x" },
  ],
};

const pumpFiles = {
  cols: [
    { key: "m", label: "module", mono: true },
    { key: "e", label: "exists because of the pump?" },
    { key: "w", label: "what it adds" },
  ],
  rows: [
    {
      m: "mx_mac, mx_tcu, mx_cluster_core",
      e: "<b>no</b>",
      w: "the same RTL, instantiated on <code>clk2x</code>. Nothing in the cascade knows",
      _tone: "good",
    },
    {
      m: "mx_fpacc.*",
      e: "<b>no</b>",
      w: "every float primitive is shared with the unpumped accumulator",
      _tone: "good",
    },
    {
      m: "<b>mx_cluster_cu_pump</b>",
      e: "yes",
      w: "one clock in, a <code>BUFGCE_DIV</code>, and <code>clk1x</code> back out so the router on this CU's port runs off the same net rather than a second divider",
    },
    {
      m: "<b>mx_cluster_mgr_pump</b>",
      e: "yes",
      w: "L1 on <code>clk2x</code>, a phase bit, the <code>+1</code> address for the odd half, a second scale pair per command and the <code>single</code> flag",
    },
    {
      m: "<b>mx_cluster_node_pump</b>",
      e: "yes",
      w: "the pair latch, the two-cycle-wide <code>part_valid</code>, and registered <code>gemm_busy</code>/<code>sweep_busy</code>",
    },
    {
      m: "<b>mx_acu_fp_pump</b>",
      e: "yes",
      w: "a stage-0 register-in, and <b>a whole float add per lane</b> in front of stage 1",
      _tone: "warn",
    },
  ],
};

const mergeSteps = {
  cols: [
    { key: "s", label: "step", mono: true },
    { key: "d", label: "what it does" },
    { key: "r", label: "registered off the next?" },
  ],
  rows: [
    {
      s: "products",
      d: "both phases' <code>|v| x mm</code>, held in <code>lm_a</code> / <code>lm_b</code>",
      r: "<b>yes</b> — fused with the align it was 17 levels / 4.516 ns",
      _tone: "good",
    },
    {
      s: "compare and swap",
      d: "exponent difference <code>e1 − e2</code>, bigger and smaller selected",
      r: "<b>yes</b> — fused with the shift-and-add it was 15 levels and held clk1x to 271 MHz",
      _tone: "good",
    },
    {
      s: "shift",
      d: "the smaller magnitude right by the difference, <b>capped at VWM</b>",
      r: "beyond VWM the smaller term is under the larger's LSB",
    },
    {
      s: "add / subtract",
      d: "by sign agreement, with the borrow re-checked",
      r: "see the trap below",
    },
    {
      s: "renormalise",
      d: "one bit: carry out means <code>mag[VWM:1]</code> and exponent +1",
      r: "then into <code>val_r</code>, and it is ordinary stage 1 from there",
    },
  ],
};

// -------------------------------------------------------------- 9. the quantiser
const quantSm = {
  states: [
    { id: "IDLE", x: 0, y: 0, label: "IDLE" },
    { id: "DRAIN", x: 6, y: 0, label: "DRAIN" },
    { id: "NORM", x: 12, y: 0, label: "NORM" },
    { id: "SCALE", x: 18, y: 0, label: "SCALE" },
    { id: "PACK", x: 24, y: 0, label: "PACK" },
    { id: "TAIL", x: 30, y: 0, label: "TAIL" },
  ],
  edges: [
    { from: "IDLE", to: "DRAIN", label: "8 beats" },
    { from: "DRAIN", to: "NORM", label: "fold" },
    { from: "NORM", to: "SCALE", label: "renorm" },
    { from: "SCALE", to: "PACK", label: "scale" },
    { from: "PACK", to: "PACK", label: "x4", self: true },
    { from: "PACK", to: "TAIL", label: "word 3" },
    { from: "TAIL", to: "IDLE", label: "done", curve: -110 },
  ],
};

const quantSteps = [
  {
    title: "IDLE — 8 beats in, and the max tree rides along",
    st: "IDLE",
    beats: 8,
    words: 0,
    have: "2,048 bits of FP16 buffered in src[0..127]",
    note: "A beat is 16 FP16 of ONE lane, so two beats fill a lane and bcnt[2:1] says which. Sixteen values reduce to four partial maxima in two compare levels inside the beat cycle, and the fold into that lane's two accumulators runs the cycle after — so a beat cycle never carries more than two compare levels. FP16 is sign-magnitude with the exponent above the mantissa, so magnitude order is the plain unsigned order of bits [14:0] and no decode is needed to find the peak.",
  },
  {
    title: "DRAIN — one cycle, for the last beat's fold to land",
    st: "DRAIN",
    beats: 8,
    words: 0,
    have: "acc[0..7] — two half-maxima per lane",
    note: "The fold runs one cycle behind the beat that produced it, so the eighth beat's maxima are not in acc until here. A state that exists purely to let a pipeline land is cheaper than moving the fold into the beat cycle it was taken out of.",
  },
  {
    title: "NORM — renormalise BOTH halves, then select",
    st: "NORM",
    beats: 8,
    words: 0,
    have: "n_sig, n_ep — the block peak in [1024, 2048)",
    note: "Both halves are renormalised and the larger is selected AFTERWARDS. Reducing first and renormalising the winner puts a 15-bit compare in FRONT of an 11-step shift chain; here the compare runs beside it and adds only a 2:1 mux. Subnormals are decoded properly rather than flushed — flushing would zero most of any block whose peak is below about 2e-3, which reads as the format being poor on small tensors rather than as a dropped case.",
  },
  {
    title: "SCALE — the smallest scale with peak/scale ≤ 63, rounded UP",
    st: "SCALE",
    beats: 8,
    words: 0,
    have: "sfield, sbase, srec — one E5M3 per lane",
    note: "ceil(n_sig/126) is eight constant compares, not a divide: n_sig is in [1024, 2047] whenever it is nonzero, so the quotient is in [9, 17] and its interior boundaries are known. Rounding the scale UP is what keeps the block peak from clipping — rounding down would push it past 63 and damage the largest element in the block, the one that matters most. A block whose peak is itself subnormal clamps, which degrades it; letting the exponent wrap would corrupt it.",
  },
  {
    title: "PACK — four passes, one 256-bit word each",
    st: "PACK",
    beats: 8,
    words: 4,
    have: "word0 .. word3 — 32 x int7 + 4 x E5M3 each",
    note: "Pass pkw carries K elements pkw·8 .. pkw·8+7 of all four lanes, so the source select is 4:1 and the scale reciprocal is fixed per lane for the whole entry. Slot assignment is the ONLY difference between an A operand and a B operand — lane·8 + (k mod 8) against (k mod 8)·4 + lane — so one circuit serves both and the driver stores both operands in the same shape.",
  },
  {
    title: "TAIL — done",
    st: "TAIL",
    beats: 8,
    words: 4,
    have: "1,024 bits out against 2,048 in",
    note: "2.2x denser on the mesh, which is the entire reason the encoding exists between memory and the MAC array. mx_quant measures 4,267 LUT, 0 BRAM, 32 DSP at 400.6 MHz — xcvu13p-fhgb2104-2L-e, out-of-context, 310 MHz-target run. It is on the memory-agent side of the mesh by design, so it is in NO cluster figure on this page.",
  },
];

const quantWord = [
  { name: "32 x int7", bits: 224, value: "the elements", accent: true },
  { name: "4 x E5M3", bits: 32, value: "repeated in every word" },
];

// ---------------------------------------------------------------- resources
const measured = {
  cols: [
    { key: "b", label: "block", mono: true },
    { key: "l", label: "LUT", mono: true, align: "right" },
    { key: "f", label: "FF", mono: true, align: "right" },
    { key: "d", label: "DSP", mono: true, align: "right" },
    { key: "m", label: "Fmax", mono: true, align: "right" },
    { key: "n", label: "bound" },
  ],
  rows: [
    {
      b: "mx_mac (one DSP48E2)",
      l: "<b>0</b>",
      f: "<b>0</b>",
      d: "1",
      m: "—",
      n: "—",
      _tone: "good",
    },
    {
      b: "mx_tcu (4x8x4) ‡",
      l: "336",
      f: "790",
      d: "64",
      m: "1072.6",
      n: "lower",
    },
    {
      b: "mx_acu_fp (MW=14, DEPTH=16, block)",
      l: "9,901",
      f: "5,585",
      d: "48",
      m: "<b>343.4</b>",
      n: "lower",
    },
    {
      b: "mx_cluster_cu (current)",
      l: "15,306",
      f: "17,754",
      d: "<b>304</b>",
      m: "<b>346.6</b>",
      n: "lower",
    },
    {
      b: "mx_quant (memory-agent side)",
      l: "4,267",
      f: "—",
      d: "32",
      m: "<b>400.6</b>",
      n: "lower",
    },
  ],
};

// ------------------------------------------------------------------- traps
const traps = {
  cols: [
    { key: "s", label: "Symptom" },
    { key: "c", label: "Cause" },
  ],
  rows: [
    {
      s: "the behavioural model passes and the real DSP48E2 fails, but only where a new tile enters every cycle",
      c: "<code>BREG</code> was 1 against a two-stage <code>AMULTSEL=&quot;AD&quot;</code> path, so B multiplied against the next tile's activation. Stable operands hide it completely",
    },
    {
      s: "half of every tile's products vanish and the rest are summed against the previous tile",
      c: "one cycle of operand delay per TCU instead of two: <code>CREG</code> makes the W path two cycles deep, so stage 7 sampled its neighbour a cycle early",
    },
    {
      s: "every result is shifted by exactly one sub-tile",
      c: "<code>a_ent</code> consumed alongside <code>s1_*</code> rather than <code>s1b_*</code> — a <code>READ_LAT = 1</code> RAM needs <b>two</b> cycles of control delay, not one",
    },
    {
      s: "a mantissa that is discarded, leaving a clean power of two — plausible and wrong by up to 2x",
      c: "a single <code>&lt;&lt; (MW − msb)</code>: the count wraps to a huge unsigned value whenever <code>msb &gt; MW</code>. <b>Both</b> shift directions are needed",
    },
    {
      s: "the sign bit disappears from the accumulator float, but only once MW is narrowed",
      c: "on a rounding carry the fraction was taken one bit too wide and overflowed the output concatenation. MW = 16 never reaches that path — <b>sweeping a parameter is a test in its own right</b>",
    },
    {
      s: "every scale product comes out zero",
      c: "<code>{1'b0, ma[i]*mb[j]}</code> — the multiply is self-determined to its 4-bit operand width inside the concatenation, so 8·8 = 64 truncates to 0. The product has to be declared wide and on its own",
    },
    {
      s: "synthesis optimises 496 of 512 tile entries away, with no error anywhere",
      c: "<code>DEPTH</code> and <code>TAW</code> as two independent parameters silently disagreed. <code>TAW</code> is now <b>derived</b>",
    },
    {
      s: "one stage carries a 25-level LUT chain and no pipeline seam can reach it",
      c: "a search or reduction written as a loop that carries a value between iterations — <code>if (!found &amp;&amp; x[i])</code>, <code>lost = lost | x[i]</code>. Worth <b>~68 MHz</b> on this design",
    },
    {
      s: "a sweep accumulates onto a stale tile, silently, and only for part of the sweep",
      c: "a tiling below <code>Gm·Gn = 5</code> recurs on one address faster than the accumulator can turn around. Three modules encode that 5 and none of them can see the others",
    },
    {
      s: "a pumped sweep runs forever at <code>nk = 255</code>",
      c: "<code>kb + KSTEP == nk_r</code> on an 8-bit counter wraps 256 → 0 and the test never fires. The comparison is 9-bit and <code>&gt;=</code>",
    },
    {
      s: "an odd <code>nk</code> reads one entry past the operand — and only when <code>single</code> changes between issues",
      c: "the lone final block is <b>phase 0</b>, and <code>single</code> needs the same TWO delays the merge gives the data",
    },
    {
      s: "an unsigned borrow wraps to a huge value with the wrong sign",
      c: "larger-by-EXPONENT is not larger-by-MAGNITUDE at equal exponents. The merge re-checks the borrow and swaps",
    },
    {
      s: "the quantiser misses its 300 MHz target by nine times",
      c: "packing a whole entry in one cycle — 128 parallel barrel shifters, measured at <b>32.5 MHz</b>. An 8-bit window is all the range that can occur",
    },
  ],
};
</script>

<template>
  <DocPage
    title="Matmul cluster — microarchitecture"
    summary="The circuit, read out of the Verilog: how one DSP48E2 is configured, how 256 of them chain, where the cross-unit partial actually enters, what the resident accumulator costs to hold, and what a 2x matmul clock adds that nothing else needs."
    domain="tpu"
    status="shipped"
    source="src/kohakutpu/matmul/ · src/kohakutpu/transform/mx_quant.v · xcvu13p-fhgb2104-2L-e, Vivado 2024.2, out-of-context synthesis"
  >
    <p class="doc-p">
      <RouterLink to="/tpu/matmul" class="doc-link"
        >The matmul cluster</RouterLink
      >
      page is the argument: why two int7 MACs per DSP, why the packing offset is
      19, why the systolic/mesh boundary sits at K = 32. This page is the
      <b>construction</b>: the ports, the register stages, and the delays that
      make the arithmetic line up. Everything here is read out of the Verilog,
      and where a different arrangement was available it is named beside the one
      that ships, with what each costs.
    </p>

    <h2 class="doc-h2">The cell: one DSP48E2 with all four ALU slots in use</h2>

    <p class="doc-p">
      A DSP48E2's ALU computes <code>Z + W + X + Y</code>. Most designs use two
      of those four. <code>mx_mac</code> uses all four, and that is what removes
      the fabric from the reduction: <code>X</code> and <code>Y</code> take the
      multiplier's partial products, <code>Z</code> takes the cascade from the
      DSP above, and <code>W</code> takes the partial arriving from the previous
      tensor CU.
    </p>

    <Fig
      caption="mx_mac.v, MODEL = 0. Nothing but wiring reaches the A and D ports — A is w_hi left-shifted by S = 19 and D is w_lo — and the pre-adder inside the DSP forms the packed operand, so no fabric adder exists anywhere on this path. Measured per instance on xcvu13p-fhgb2104-2L-e: 0 LUT, 0 FF, 1 DSP."
      zoom
      wide
    >
      <BlockDiagram :nodes="mac.nodes" :edges="mac.edges" />
    </Fig>

    <Fig
      caption="OPMODE = { W[1:0], Z[2:0], Y[1:0], X[1:0] }, a compile-time constant per instance. Two localparams pick it: Z_SEL is 0 for a cascade stage and 1 for the head of a chain; W_SEL is 1 only on the last stage of a non-first TCU. INMODE = 00100 tells the pre-adder to compute A + D with A from A2 and B from B2."
    >
      <BitField :fields="opmode" />
    </Fig>

    <SpecTable
      :cols="macLatency.cols"
      :rows="macLatency.rows"
      caption="The three input paths have three different depths, and every delay elsewhere in the cluster exists to reconcile them. MODEL = 1 swaps the primitive for a behavioural equivalent with exactly these latencies, so the arithmetic can be checked without unisims — and every matmul bench runs both"
    />

    <Callout kind="rule" title="No reset on the datapath, deliberately">
      <p>
        <code>RSTA</code>, <code>RSTB</code>, <code>RSTC</code>,
        <code>RSTD</code>, <code>RSTM</code>, <code>RSTP</code> and the four
        control resets are all tied low. A, B, C, D, M and P are pipeline
        registers, and <code>Z</code> is <code>PCIN</code> or zero —
        <b>never <code>P</code></b> — so nothing accumulates across a reset and
        there is nothing to clear. The behavioural model matches this exactly,
        so the two build the same pipeline rather than two pipelines that agree
        on values.
      </p>
    </Callout>

    <Callout
      kind="trap"
      title="BREG = 2, not 1 — and it is invisible unless operands stream"
    >
      <p>
        With <code>AMULTSEL = &quot;AD&quot;</code> the A and D operands reach
        the multiplier through <b>AREG and ADREG</b>. Give B a single register
        and it arrives a cycle early and multiplies against the wrong operand.
      </p>
    </Callout>

    <WaveTrace
      variant="broken"
      label="broken — BREG = 1, a new tile every cycle"
      :rows="bregBroken.rows"
      :notes="bregBroken.notes"
    />
    <WaveTrace
      variant="fixed"
      label="fixed — BREG = 2, BCASCREG = 2"
      :rows="bregFixed.rows"
      :notes="bregFixed.notes"
    />

    <h2 class="doc-h2">
      The chain, and where the cross-CU partial really enters
    </h2>

    <p class="doc-p">
      Eight DSPs make a chain, eight chains make a tensor CU, four tensor CUs
      make a cluster. Inside a CU the reduction is <code>PCOUT → PCIN</code>:
      dedicated silicon between physically adjacent DSPs in one column, no
      fabric and no LUTs. Between CUs it is the <code>C</code> port — which is
      free because integer elements have no implied leading one, so none of the
      <code>(1+Ma)(1+Mb)</code> correction an FP8 design needs exists here.
    </p>

    <Fig
      caption="mx_tcu.v and mx_cluster_core.v, one of the eight chains. Stage 0 takes Z = 0; stages 1 to 6 take Z = PCIN; the LAST stage takes Z = PCIN and W = C. The whole K=32 reduction across all four CUs costs zero fabric adders, and each CU stays an independent 8-deep cascade that can be placed on its own — only the eight DSPs within one CU have to be adjacent."
      zoom
      wide
    >
      <BlockDiagram
        :nodes="chain.nodes"
        :edges="chain.edges"
        :groups="chain.groups"
      />
    </Fig>

    <Callout
      kind="trap"
      title="Which STAGE takes the upstream partial is the decision; the port is its consequence"
    >
      <p>
        Both arrangements below reach the claim this design is built on — zero
        fabric adders across the whole K = 32 — and they cost four times the
        operand skew apart. Read the third column, not the second.
      </p>
    </Callout>

    <SpecTable :cols="zVsW.cols" :rows="zVsW.rows" />

    <p class="doc-p">
      Because <code>C</code> is registered once, the W path costs
      <b>two</b> cycles, not one. Getting that wrong does not error.
    </p>

    <WaveTrace
      variant="broken"
      label="broken — one cycle of operand delay per TCU"
      :rows="wBroken.rows"
      :notes="wBroken.notes"
    />
    <WaveTrace
      variant="fixed"
      label="fixed — d_c = d_(c-1) + 2"
      :rows="wFixed.rows"
      :notes="wFixed.notes"
    />

    <h3 class="doc-h3">Operand skew, and what it is built from today</h3>

    <p class="doc-p">
      A cascade adds one pipeline stage per DSP, so stage <code>k</code>'s
      operands must arrive <code>k</code> cycles after stage 0's. The RTL
      bundles a whole stage's operands into one shift register rather than
      delaying 32 lanes separately.
    </p>

    <Fig
      caption="stage_op[k] — four rows of A at K index k, and four columns of B at the same k. One shift register per stage serves the entire stage, which is why the delay lines count 8 of these and not 32 narrow ones. There is no reset on any of them: operands are meaningless until vld_sr says otherwise, and a reset here would add a control set per stage."
    >
      <BitField :fields="bundle" />
    </Fig>

    <p class="doc-p">
      The timing of that skew — four tiles streaming down one 8-deep chain, with
      the DSP's own A1/A2 and B1/B2 registers absorbing the first two stages —
      is drawn on
      <RouterLink to="/tpu/matmul" class="doc-link"
        >the matmul cluster page</RouterLink
      >. What the RTL settles is what those delays are <i>made of</i>, and it is
      not what that trace says.
    </p>

    <Callout kind="trap" title="An SRL costs a LUT per bit at ANY depth">
      <p>
        These chains are 2 to 7 deep inside a TCU and 2, 4 and 6 deep between
        them. An
        <code>SRL16E</code> pays a full LUT to use at most 7 of its 16 stages,
        and the device is LUT-bound at 66% with FF at 34% — so the skew belongs
        in the half of the CLB that is idle. Both <code>mx_tcu.v</code> and
        <code>mx_cluster_core.v</code> now carry
        <code>(* shreg_extract = &quot;no&quot; *)</code>, and
        <code>mx_acu_fp*.v</code> carries it at module scope <i>and</i> again on
        every wide payload register, because the module-level attribute does not
        reach declarations inside generate blocks.
      </p>
    </Callout>

    <SpecTable
      :cols="skewCost.cols"
      :rows="skewCost.rows"
      caption="The SRL row is measured — out-of-context synthesis of mx_cluster on xcvu13p-fhgb2104-2L-e, Vivado 2024.2, 300 MHz target, and its per-component rows sum to the parent exactly. The flop row is what the shipping RTL forces; that build has NOT been re-measured as a matched pair against the SRL one, so the swap has no LUT figure here and none should be inferred from the 1,458"
    />

    <h2 class="doc-h2">The K sweep: the tile stays, the operands move</h2>

    <p class="doc-p">
      This is the design's central idea and it is visible in three lines of
      <code>mx_cluster_mgr</code>: <code>for kb: for g: for h</code>. K is the
      <b>outermost</b> loop of the sweep, so between two commands to one
      accumulator address the manager visits every other address first. Scrub
      through one <code>GEMM</code> and watch which row changes.
    </p>

    <StepPlayer
      :steps="sweepSteps"
      label="GEMM gm=4 gn=8 nk=4 — C[16,32] = A[16,128] @ B.T[32,128]"
    >
      <template #default="{ state }">
        <div
          class="kt-text-micro uppercase tracking-wider text-warm-400 dark:text-warm-600 mb-2"
        >
          operands — every one of these moves
        </div>
        <div class="flex flex-wrap items-baseline gap-x-3 gap-y-1.5 mb-2">
          <span
            class="kt-text-micro font-mono text-warm-500 dark:text-warm-400 w-44 shrink-0"
          >
            L1 A · aoff + g·nk + kb
          </span>
          <span
            v-for="e in entA(state.kb)"
            :key="`a${e}`"
            class="gem-badge font-mono bg-gem text-white"
          >
            A[{{ e }}]
          </span>
          <span
            v-if="state.kb === null"
            class="gem-badge font-mono bg-warm-100 dark:bg-warm-800 text-warm-400 dark:text-warm-600"
          >
            the sweep has ended
          </span>
        </div>
        <div class="flex flex-wrap items-baseline gap-x-3 gap-y-1.5">
          <span
            class="kt-text-micro font-mono text-warm-500 dark:text-warm-400 w-44 shrink-0"
          >
            L1 B · boff + h·nk + kb
          </span>
          <span
            v-for="e in entB(state.kb)"
            :key="`b${e}`"
            class="gem-badge font-mono bg-gem text-white"
          >
            B[{{ e }}]
          </span>
          <span
            v-if="state.kb === null"
            class="gem-badge font-mono bg-warm-100 dark:bg-warm-800 text-warm-400 dark:text-warm-600"
          >
            —
          </span>
        </div>

        <div
          class="kt-text-micro uppercase tracking-wider text-warm-400 dark:text-warm-600 mt-5 mb-2"
        >
          the resident tile — this does not
        </div>
        <div
          class="rounded-lg border border-warm-200 dark:border-warm-700 bg-warm-50 dark:bg-warm-900 px-4 py-3 font-mono"
        >
          <div class="flex flex-wrap items-baseline gap-x-3">
            <span
              class="kt-text-caption font-semibold text-warm-800 dark:text-warm-200"
            >
              tile_addr = g·gn + h
            </span>
            <span class="kt-text-micro text-warm-400 dark:text-warm-600">
              32 addresses, base unchanged since the sweep began
            </span>
          </div>
          <div class="kt-text-caption text-gem mt-2">{{ state.val }}</div>
          <div class="kt-text-micro text-warm-400 dark:text-warm-600 mt-2">
            read at stage 3 · written at stage 5 · revisited every 32 cycles
          </div>
        </div>

        <dl
          class="grid grid-cols-[auto_1fr] gap-x-5 gap-y-1.5 kt-text-caption mt-4"
        >
          <dt class="text-warm-500 dark:text-warm-400">accumulator command</dt>
          <dd class="font-mono text-warm-800 dark:text-warm-200">
            {{ state.cmd }}
          </dd>
          <dt class="text-warm-500 dark:text-warm-400">issues, cumulative</dt>
          <dd class="font-mono text-warm-800 dark:text-warm-200">
            {{ state.issues }} of 128
          </dd>
          <dt class="text-warm-500 dark:text-warm-400">MACs, cumulative</dt>
          <dd class="font-mono text-warm-800 dark:text-warm-200">
            {{ state.macs }}
          </dd>
          <dt class="text-warm-500 dark:text-warm-400">
            distinct L1 entries touched
          </dt>
          <dd class="font-mono text-warm-800 dark:text-warm-200">
            {{ state.entries }} of 48
          </dd>
          <dt class="text-warm-500 dark:text-warm-400">
            bytes out of the cluster
          </dt>
          <dd class="font-mono text-warm-800 dark:text-warm-200">
            {{ state.out }}
          </dd>
        </dl>
      </template>
    </StepPlayer>

    <Callout
      kind="rule"
      title="The recurrence interval is what buys the single bank"
    >
      <p>
        A pipelined adder cannot close a single-cycle accumulate loop, and when
        K was the inner loop that was the common case — which forced three
        rotating banks, a two-step fold on <code>EMIT</code> and a per-address
        zero mask. Sweeping K outermost makes an address recur every
        <code>Gm·Gn</code> cycles, so read latency is free and one bank
        suffices. What replaces the banks is a <b>contract</b>, and the contract
        is checked rather than assumed.
      </p>
    </Callout>

    <h2 class="doc-h2">The accumulator: seven stages, and one tile RAM</h2>

    <Fig
      caption="mx_acu_fp.v. The tile address is presented at stage 2a because READ_LAT = 2 means the data lands two cycles later, during stage 3 — and READ_LAT = 2 is what puts the block RAM's own output register in the path. Without it the path starts at the RAM array access, about 1.2 ns of clock-to-out instead of a flip-flop, and that alone cost about 70 MHz."
      zoom
      wide
    >
      <BlockDiagram
        :nodes="acu.nodes"
        :edges="acu.edges"
        :groups="acu.groups"
      />
    </Fig>

    <h3 class="doc-h3">
      Stage 1: what the accumulator actually reads out of the 48-bit word
    </h3>

    <Fig
      caption="The chain hands over 8 x 48 bits, two packed fixed-point fields per chain. The upper field is read as 22 bits with its sign at bit 40, and the lower field's sign bit at 18 is the borrow the upper needs — the whole 48-bit word is one two's complement accumulation, so a negative lower sum borrows from its neighbour."
    >
      <BitField :fields="pWord" />
    </Fig>

    <Callout
      kind="rule"
      title="The borrow correction is one XOR, not a 29-bit increment"
    >
      <p>
        The extraction, the two's complement negation and the borrow fix are the
        same expression. <code>mm = m8a·m8b</code> is unsigned, so the product's
        sign is the chain value's, and
      </p>
      <p class="font-mono kt-text-caption">
        |v + r| · mm == ((v ^ {W{s}}) + (s ^ r)) · mm, s = v[W−1]
      </p>
      <p>
        which is one XOR level on a register output, and maps onto the DSP's
        <code>(D+A)·B</code> mode. Taken <i>after</i> the multiply instead it
        put a 30-bit two's complement carry chain between the DSP's output
        register and the leading-one search —
        <b>0.952 ns of a 3.401 ns path, and four of its twelve logic levels</b>.
        Moving it took twelve levels to nine.
      </p>
      <p>
        <code>r</code> is a rounding bit, so <code>v + r</code> can only reach
        zero from <code>v = −1</code>: the one case where
        <code>s</code> disagrees with the true sign. The magnitude is zero there
        and <code>is_zero</code> discards the sign.
      </p>
    </Callout>

    <h3 class="doc-h3">Widths, and none of them are round numbers</h3>

    <SpecTable :cols="widths.cols" :rows="widths.rows" />

    <Fig
      caption="The accumulator float, S1 E7 M(ACC_MW), all-zero being the zero encoding. E7 is required rather than chosen: the accumulator holds int x scaleA x scaleB, and for FP16 sources that exponent sum spans roughly −48 to +30, so an E5 field would overflow on ordinary data. MW = 14 is the shipped default and measures identically to MW = 16 — results.md §6.2, xcvu13p-fhgb2104-2L-e."
    >
      <BitField :fields="accFloat" />
    </Fig>

    <h3 class="doc-h3">
      The variable shift is a multiply, and three things had to line up
    </h3>

    <p class="doc-p">
      Left-justifying the magnitude so its leading one sits at bit
      <code>VW−1</code> makes the significand, the guard bit and the dropped
      bits <b>fixed slices</b>, and <code>mag &lt;&lt; k</code> is
      <code>mag · 2^k</code>. The accumulator had 16 DSPs beside a 256-DSP MAC
      array, so the multiplier was there for the taking.
    </p>

    <SpecTable
      :cols="shiftFit.cols"
      :rows="shiftFit.rows"
      caption="mx_fpacc_norm_a and mx_fpacc_norm_p. Rule 5 of the accumulator's timing notes is that nothing but the search sits between val_r and the shift DSPs' input registers, and the one-hot adds no levels because it was already there"
    />

    <ResourceBars
      :items="shiftTrade"
      unit="LUT · sixteen copies of one variable shift, OOC, xcvu13p-fhgb2104-2L-e"
      caption="The isolated primitive comparison that justified the trade. In mx_acu_fp it was worth −6.7% LUT and +15.7 MHz standing alone and −13% and +21.0 MHz inside the cluster — a LARGER win in context, because the cluster was tight enough that the tools had been replicating logic to hold the frequency"
    />

    <Callout kind="rule" title="Everything in mx_fpacc must stay tree-shaped">
      <p>
        These blocks sit in the cluster's critical path, and a search or
        reduction written as a loop that carries a value between iterations —
        <code>if (!found &amp;&amp; x[i]) found = 1</code>,
        <code>lost = lost | x[i]</code> — synthesises as exactly that serial
        chain: <b>25 LUT levels inside a single pipeline stage</b>, most of a
        3.33 ns period, where no seam elsewhere can reach it. It cost this
        design about 68 MHz. Use smear-isolate-encode for searches and
        mask-then-reduce for sticky bits.
      </p>
      <p>
        <code>mx_lead1</code> is the shape: <code>log2(W)</code> levels of OR to
        smear the leading one rightwards, one AND to isolate it, six OR
        reductions to encode it.
      </p>
    </Callout>

    <Callout
      kind="trap"
      title="mx_fpacc's two REFERENCE modules are not instantiated by anything"
    >
      <p>
        <code>mx_fpacc_norm</code> and <code>mx_fpacc_add</code> are the unsplit
        reference implementations, and <code>mx_fpacc_tb</code> checks
        <b>them</b> against a real-number model. Nothing cross-checks them
        against the split versions that ship.
        <b
          >Changing a split module and running only
          <code>mx_fpacc_tb</code> proves nothing</b
        >
        — the split path is covered end to end by <code>mx_acu_fp_tb</code> and
        nowhere else.
      </p>
    </Callout>

    <h3 class="doc-h3">Where precision actually dies in a long K sweep</h3>

    <p class="doc-p">
      Not in the reduction. L0 and L1 are exact integer, so a K = 32 block
      carries no rounding at all, and the block-scale application at stage 1 is
      exact too — the exponent halves add and the mantissas multiply, with the
      <code>/64</code> the mantissa product carries coming off the exponent as a
      <code>−6</code>. Rounding first appears at stage 2b, once per 32 MACs, and
      then once more per K block in the stage 4/5 add.
    </p>

    <p class="doc-p">
      Three things bound the answer, and they bind in a very uneven order.
    </p>

    <SpecTable
      :cols="precision.cols"
      :rows="precision.rows"
      caption="results.md §6.2 and accumulator.md §7. A K sweep does not gradually erode headroom; a biased operand distribution destroys it outright, and splitting K does not fix it because the final sum is the same number however K is partitioned"
    />

    <Callout
      kind="trap"
      title="Two ways the rounding carry has been wrong, in the same file"
    >
      <p>
        In <code>mx_fpacc_norm</code>, on a rounding carry the leading one moves
        to bit <code>MW</code>, so the fraction is <code>sig_r[MW:1]</code>.
        <code>sig_r[SW:1]</code> is <code>MW+1</code> bits, overflows the output
        concatenation and pushes the <b>sign bit</b> out.
        <b>MW = 16 never reaches that path</b>, so it only appeared once MW was
        narrowed — sweeping a parameter is a test in its own right.
      </p>
      <p>
        In <code>mx_fpacc_to_fp16</code>, <code>m11</code> is the
        <b>stored fraction</b>, not the significand, so a carry out of it means
        1.111…1 rounded to 10.000…0: fraction to zero, exponent up. Shifting
        <code>m11</code> right instead leaves the carry bit in the fraction and
        gives 1.5·2^(e+1) where 1.0·2^(e+1) was meant — a <b>50% error</b>.
      </p>
    </Callout>

    <h2 class="doc-h2">
      REUSE_MIN = 5, encoded in three places that cannot see each other
    </h2>

    <p class="doc-p">
      The single bank is bought with a caller contract: consecutive commands to
      the same tile address must be at least <code>REUSE_MIN</code> cycles
      apart. It counts the tile <b>read</b> to the tile <b>write</b>, which is
      why stage 2a2 lengthened the block without touching the number — the new
      stage sits ahead of the read.
    </p>

    <SpecTable
      :cols="reuse.cols"
      :rows="reuse.rows"
      caption="Neither caller can compute Gm·Gn: as a multiply it is an 8x8 fabric multiplier whose 16-bit result feeds three comparators, 15 levels in the manager and 13 in the CU, and it cost the CU 26 MHz. Both expand the test into small-value comparisons on the operands instead, each carries its own elaboration check, and nothing links them to the localparam — changing it means changing all three"
    />

    <SpecTable
      :cols="pace.cols"
      :rows="pace.rows"
      caption="mx_cluster_mgr's pacing. Gm·Gn < 5 with both operands at least 1 is exactly three shapes: (1, ≤4), (≤4, 1) and (2, 2). The cost is nothing at any tiling worth running — and a sweep of nk = 1 never paces at all, because one K block never revisits an address"
    />

    <Callout
      kind="rule"
      title="The contract is checked in simulation, and the check needed its own valid bit"
    >
      <p>
        <code>mx_acu_fp</code> keeps the last <code>REUSE_MIN</code> tile
        addresses and reports any repeat, so a caller that sweeps K on the
        inside <b>fails loudly</b>
        instead of quietly accumulating into stale data. It caught a real
        violation the moment it existed — the older single-port CU emitted a
        tile 2 to 3 cycles after the last accumulate into it.
      </p>
      <p>
        A separate valid bit per slot, not a sentinel address:
        <code>{TAW{1'b1}}</code> meaning &quot;no command&quot; collides with
        the real top address, which at <code>TAW = 4</code> is 15.
      </p>
    </Callout>

    <Callout
      kind="trap"
      title="busy has to cover the REUSE_MIN gap, not just the pipeline"
    >
      <p>
        <code>busy</code> means &quot;not safe to take the control mux
        yet&quot;, and taking the mux means issuing an <code>EMIT</code> that
        reads an address an in-flight command may be about to write. So
        <code>busy</code> is <code>in_flight || |busy_tail</code>, with
        <code>busy_tail</code> reloaded to <code>REUSE_MIN</code> on every
        in-flight cycle. A pipeline-only version reads correct and fails only
        when the whole <code>GEMM</code> is short enough that its tail has not
        cleared — and then <b>every sub-tile drains as zero</b>, which looks
        exactly like a compute bug. When the sweep itself finishes is a separate
        question, answered on
        <RouterLink to="/tpu/matmul" class="doc-link"
          >the matmul cluster page</RouterLink
        >.
      </p>
    </Callout>

    <h2 class="doc-h2">How work reaches the array: mx_cluster_mgr</h2>

    <p class="doc-p">
      The chain eats <code>A[4][32] + B[32][4]</code> every cycle — eight
      256-bit operand words — and a NoC port delivers one. No port count closes
      an 8x deficit; reuse does, which is why L1 exists and why the manager owns
      it explicitly. One <code>GEMM</code>
      flit expands here into hundreds of three-bit accumulator commands, and
      none of them ever appears on the mesh.
    </p>

    <Fig
      caption="mx_cluster_mgr.v. Two 928-bit RAMs rather than one, because a sweep reads an A entry and a B entry in the same cycle. 928 bits and a few tens deep is a distributed-RAM shape — the block and ultra ports are 72 bits, so width would set the primitive count and depth would be 99% wasted — but in the shape that ships, at 512 entries per side, block RAM is right: 13 RAMB36 per port, 26 for the two."
      zoom
      wide
    >
      <BlockDiagram :nodes="mgr.nodes" :edges="mgr.edges" />
    </Fig>

    <Fig
      caption="One L1 entry — what a FILL assembles from four consecutive 256-bit operand words, and what one address of u_l1a or u_l1b holds. The four scales are identical across the four K-slices of a block; repeating them costs 12.5% of the payload and makes every flit self-contained, which is what lets memory responses arrive out of order."
    >
      <BitField :fields="l1Entry" />
    </Fig>

    <Callout
      kind="rule"
      title="The accumulator command rides a FIFO, not a matched delay"
    >
      <p>
        The cascade is about 19 cycles deep from where the manager sits, and
        that depth is a function of the CU count and the skew registers. Rather
        than duplicate the constant, each issue pushes
        <code>{op, addr, sa, sb, anchor}</code> and every
        <code>part_valid</code> pops one.
        <b>Order is preserved by construction</b>, so alignment survives any
        change to the chain.
      </p>
      <p>
        Depth 64 against a ~19-deep chain, and it must never fill: a dropped
        command corrupts exactly one output element. Both overflow and underflow
        —
        <code>part_valid</code> with no pending command — have simulation
        checks.
      </p>
    </Callout>

    <WaveTrace
      variant="broken"
      label="broken — the entry consumed one stage too early"
      :rows="dlyBroken.rows"
      :notes="dlyBroken.notes"
    />
    <WaveTrace
      variant="fixed"
      label="fixed — two cycles of control delay"
      :rows="dlyFixed.rows"
      :notes="dlyFixed.notes"
    />

    <Callout
      kind="rule"
      title="A check that cries wolf is deleted, which is how the real one gets lost"
    >
      <p>
        A <code>FILL</code> may run while a sweep does — that is the point of
        <code>aoff</code> and <code>boff</code> — which makes L1 a shared
        resource with no interlock, and a fill landing on entries the sweep is
        reading corrupts a few sub-tiles and nothing else: the median barely
        moves and the answer is wrong. The collision check therefore compares
        <b>within a bank</b>. <code>l1_addr</code> is
        <code>{bank, offset}</code>, and a fill into the other half is the whole
        point of banking, so comparing the flat address would report every
        double-buffered fill as a collision.
      </p>
      <p>
        For the same reason the sweep's own <code>aoff + g·nk + kb</code> stays
        8 bits and <b>cannot carry into the bank</b>: beyond 256 it wraps inside
        its own half, where widening the sum would walk a sweep into the other
        bank's operands.
      </p>
    </Callout>

    <h2 class="doc-h2">
      The _pump variants: what a 2x matmul clock changes structurally
    </h2>

    <p class="doc-p">
      Running L1 and the cascade at twice the accumulator's clock does not
      change the cascade. It changes what arrives at the accumulator:
      <b
        >two partials per <code>clk1x</code> cycle, from two consecutive K
        blocks, carrying two different E5M3 scale pairs</b
      >. They cannot be summed packed, so they have to be added as floats before
      stage 1 — and that add is the whole structural cost of the pump.
    </p>

    <Fig
      caption="mx_cluster_cu_pump.v and mx_cluster_node_pump.v. One clock comes in and a BUFGCE_DIV makes the other, so the crossing is an ENABLE rather than a FIFO — the divider is skew-matched to its source. CLR is not tied off: dividers agree on a phase only if they leave reset together, and the router driving this CU's port must agree with them, which is why clk1x is an output."
      zoom
      wide
    >
      <BlockDiagram
        :nodes="pump.nodes"
        :edges="pump.edges"
        :groups="pump.groups"
      />
    </Fig>

    <SpecTable
      :cols="pumpFiles.cols"
      :rows="pumpFiles.rows"
      caption="Four files exist only because of the pump, and the entire arithmetic datapath is not among them"
    />

    <SpecTable
      :cols="mergeSteps.cols"
      :rows="mergeSteps.rows"
      caption="The merge, in mx_acu_fp_pump. It is a float add — compare, align, add, renormalise — done sixteen times, and it is why the pumped accumulator carries two more register stages than the plain one before it reaches the stage the plain one starts at"
    />

    <Callout
      kind="trap"
      title="Three ways the pair phase has already gone wrong"
    >
      <p>
        <b>The lone final block is phase 0.</b> An odd <code>nk</code> ends on a
        single K block, and <code>single</code> keeps <code>m1</code> /
        <code>part_in2</code> — phase 1 read one entry past the operand. The
        address side matches it: <code>kb</code> first and then
        <code>kb+1</code>, because the reverse order put the out-of-range entry
        of an odd <code>nk</code> into the half <code>single</code> keeps.
      </p>
      <p>
        <b><code>single</code> needs TWO delays, matching the data.</b> The
        merge adds two register stages, so at one delay <code>single</code> led
        the data by a cycle — and that only showed up when it changed between
        issues, which is odd <code>nk</code> greater than 1.
      </p>
      <p>
        <b>Larger-by-exponent is not larger-by-magnitude at equal exponents.</b>
        The unsigned borrow wrapped to a huge value with the wrong sign, so the
        merge computes both differences and picks by the borrow bit.
      </p>
    </Callout>

    <Callout
      kind="trap"
      title="Two counters that wrap, and one that must not be combinational"
    >
      <p>
        <code>KSTEP = 2</code>, so the last-block test has to match the step or
        the sweep re-reads what it just summed. Written as
        <code>kb + KSTEP == nk_r</code> on an 8-bit counter it wraps 256 → 0 at
        <code>nk = 255</code>, never fires, and the sweep runs forever; it is a
        9-bit <code>&gt;=</code>. The unpumped <code>kb + 1 == nk_r</code> steps
        by one and cannot wrap, which is why the bug is only in the pumped file.
      </p>
      <p>
        And <code>cmd_valid</code> is <b>deliberately absent</b> from the pumped
        accumulator's <code>in_flight</code>: combinational from the 2x
        <code>pair_v</code>, it held <code>clk2x</code> to 533 MHz against the
        cascade's 556. The three stage-0 command valids cover the same window
        without crossing the domain.
      </p>
    </Callout>

    <Callout
      kind="note"
      title="part_valid is two cycles wide and that is not a handshake"
    >
      <p>
        The pair latch raises <code>pair_v</code> for one
        <code>clk2x</code> cycle and a second register widens it, so the
        <code>clk1x</code> edge cannot miss it. It is <b>not</b> replaceable by
        a toggle handshake: <code>part_bus</code> is read <i>with</i> <code>part_valid</code>, so synchroniser latency would break the
        alignment the whole chain is built on. And <code>pp</code>, the pair
        phase, follows <b>VALID</b> rather than the clock phase.
      </p>
    </Callout>

    <Callout
      kind="measured"
      title="Stage 0 exists because an inferred DSP has no input registers"
    >
      <p>
        Without registering every input at the pump's stage 0 the inferred
        DSP48E2 has no
        <code>AREG</code>, <code>DREG</code> or <code>BREG</code>, and the stage
        measured <b>2.111 ns</b>. Every input is registered together — data and
        command — so nothing else moves relative to anything.
      </p>
    </Callout>

    <h2 class="doc-h2">
      mx_quant: one quantised read converts exactly one L1 entry
    </h2>

    <p class="doc-p">
      The quantiser is not in the compute unit. It sits on the memory-agent side
      of the mesh, in the agent's transform slot, and it converts
      <b>one L1 entry</b> per invocation: 8 beats of 256 bits in, 4 words of 256
      bits out. Because the block scale is shared along K, the block's peak is
      not known until the last beat has arrived — so
      <b>nothing can be emitted until the whole entry is in</b>, and that is why
      this buffers an entry rather than streaming it, and why the read is a
      fixed 8-beat burst rather than a <code>len</code>-beat one.
    </p>

    <StepPlayer
      :steps="quantSteps"
      label="mx_quant — one entry, 4 lanes x 32 K"
    >
      <template #default="{ state }">
        <StateMachine
          :states="quantSm.states"
          :edges="quantSm.edges"
          :active="state.st"
          :r="30"
        />
        <div class="flex flex-wrap gap-2 mt-3">
          <span class="chip">beats in = {{ state.beats }}/8</span>
          <span class="chip">words out = {{ state.words }}/4</span>
          <span class="chip">{{ state.have }}</span>
        </div>
      </template>
    </StepPlayer>

    <Fig
      caption="One output word. int7 is the width that fills the payload exactly: 32 x 7 = 224 and 4 x 8 = 32 against a 256-bit flit payload — which is the second, independent reason the element is seven bits rather than eight, the first being the guard-bit budget in the DSP packing."
    >
      <BitField :fields="quantWord" />
    </Fig>

    <Callout kind="trap" title="An 8-bit window, not a 24-bit barrel shift">
      <p>
        The product is under 2^23 and the scale comes from the block peak, so
        the shift amount <code>t</code> is in
        <b>[0, 7] for every element that produces a nonzero result</b>: above
        that the window clears the product and gives zero, below it the element
        exceeds the peak and saturates. Packing a whole entry in one cycle
        instead — 128 parallel barrel shifters — measured <b>32.5 MHz</b>, nine
        times over budget.
      </p>
      <p>
        The divide is a table for the same reason. The quantiser divides by the
        scale mantissa, which has exactly eight possible values, so
        <code>round(4096·8/m8)</code> is eight constants and a divider was never
        needed.
      </p>
    </Callout>

    <Callout
      kind="note"
      title="…and one place the tree-shape rule does NOT pay"
    >
      <p>
        The subnormal renormalisation in <code>PK_NORM</code> is left as an
        explicit loop. Rewritten as smear / isolate / one-hot select — the shape
        the rest of this codebase prescribes — it measured
        <b>3,745 LUT against 3,657 and the same 343.4 MHz</b>. At 11 bits and 11
        iterations synthesis already finds it, and the explicit form only spends
        more. The rule earns its keep on <i>wide</i> searches.
      </p>
    </Callout>

    <h2 class="doc-h2">
      Where the DSPs go, and what that costs at device scale
    </h2>

    <ResourceBars
      :items="dspCensus"
      unit="DSP48E2 per cluster · xcvu13p-fhgb2104-2L-e, OOC synth"
      :max="304"
      caption="304 total. The MAC array is 84% of it; the other 48 are the accumulator's two DSP trades — the block-scale multiply at stage 1 and the normalising shift at stage 2a2 — both of which BOUGHT LUTs and MHz with DSPs the cluster had spare"
    />

    <Callout
      kind="measured"
      title="Counting them changed the device-level answer twice"
    >
      <p>
        A DSP-bound part divides by this number. At the cascade's 256 alone the
        arithmetic gave <b>48 clusters</b>; at 272, before the normalising shift
        moved into DSPs, it gave <b>45</b>; at the measured 304 it gives
        <code>12,288 / 304 =</code> <b>40</b>. All three have been quoted
        somewhere and 40 is the one the current cluster supports. The conclusion
        — DSP-bound, which is the right place to be bound on this part — moves
        <i>further</i> in the same direction with each correction, which is
        exactly why it was never caught by the answer looking wrong.
      </p>
      <p>
        <b>Nothing at 32, 40 or 45 clusters has been placed and routed.</b>
        Every column but the first in that arithmetic is multiplication on one
        synthesised cluster.
      </p>
    </Callout>

    <SpecTable
      :cols="measured.cols"
      :rows="measured.rows"
      caption="Out-of-context synthesis on xcvu13p-fhgb2104-2L-e. Rows marked ‡ are older 300 MHz-target runs; the rest are 310 MHz-target. Every row that met its target is a LOWER bound on that block's frequency — it cleared the constraint and the tool stopped trying — and none of it is placed. Full conditions in results.md §2 and §4"
    />

    <h2 class="doc-h2">Traps, collected</h2>

    <p class="doc-p">
      Every row is a failure the source records as having happened. Most of them
      produce a plausible wrong answer rather than an error, which is why they
      are worth naming.
    </p>

    <SpecTable :cols="traps.cols" :rows="traps.rows" />

    <Callout
      kind="rule"
      title="Two models, because a failure has to be attributable"
    >
      <p>
        Every matmul bench runs against both a behavioural DSP and a real
        DSP48E2, and the whole matmul datapath is exact integer arithmetic
        checked bit-for-bit against a model computed in the bench —
        <b>no tolerances</b>. That is what turned the <code>BREG</code> bug from
        &quot;the arithmetic is wrong somewhere&quot; into &quot;the DSP
        configuration is wrong&quot;: the model passed, the primitive failed,
        and only in the streaming section.
      </p>
      <p>
        The coverage that matters is the cases random operands never reach — the
        packing worst case with all three operands at −64, full-scale sums that
        use all five guard bits, the borrow correction with the lower field
        forced negative on all eight chains, and streaming a new tile every
        cycle, which is the only way the per-stage skew and the cross-CU path
        are exercised at all.
      </p>
    </Callout>
  </DocPage>
</template>
