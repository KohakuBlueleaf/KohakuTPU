/**
 * Resource estimator data: the Xache (kx_xache, the xbar-cache) and the station
 * bus, each as a per-knob cost model fitted to and validated against one OOC
 * synthesis per configuration (xcvu13p-fhgb2104-2L-e, Vivado 2024.2, 300 MHz
 * target, synthesis only). Every number here is transcribed from the
 * result.txt of one scripts/tcl/ooc_kx.tcl run per Xache row and one
 * scripts/tcl/ooc_line_d2.tcl run per station row; the model is
 * scripts/py/kx_cost.py, whose --json output this file mirrors.
 *
 * The Xache has two families of rows, one per read engine (RD_PIPE 0: the
 * one-beat engine; RD_PIPE 1: the streaming engine, measured at RD_OUTQ 4),
 * each over the same 14 shapes on the current array. Per family the LUT model
 * is a STEP TABLE read from the rows (the ship plus each knob's measured delta
 * at each measured step, interpolated between steps, plus the M×N interaction
 * and an SASD saving that grows as a·N + b·M·N) because LUT is convex in M and
 * in K; FF is a linear least-squares fit. Both are validated against every row.
 */

export const PART = "xcvu13p-fhgb2104-2L-e";
export const TARGET_MHZ = 300;

/* ---------------- xbar-cache: measured rows ---------------- */
/* [M, N, K, rsamd, wsamd, n_cdc, LUT, FF, URAM, BRAM, Fmax] — 64 URAM per home */
export const KX_ROWS = {
  /* RD_PIPE = 0: the one-beat engine on the current array */
  0: [
    [4, 4, 1, 1, 1, 0, 8408, 7340, 256, 0, 449.2],
    [4, 4, 2, 1, 1, 0, 12527, 13464, 480, 0, 403.4],
    [4, 4, 4, 1, 1, 0, 21423, 21632, 928, 0, 378.4],
    [8, 4, 1, 1, 1, 0, 14242, 7456, 256, 0, 449.0],
    [4, 8, 1, 1, 1, 0, 16992, 14630, 512, 0, 449.2],
    [8, 8, 1, 1, 1, 0, 26370, 14888, 512, 0, 449.0],
    [2, 4, 1, 1, 1, 0, 5287, 7249, 256, 0, 449.0],
    [4, 4, 1, 0, 1, 0, 8538, 7086, 256, 0, 448.4],
    [4, 4, 1, 1, 0, 0, 6756, 6961, 256, 0, 449.0],
    [4, 4, 1, 0, 0, 0, 6356, 6717, 256, 0, 449.2],
    [8, 8, 2, 0, 0, 0, 23999, 25660, 960, 0, 344.0],
    [4, 4, 1, 1, 1, 4, 10323, 10760, 256, 64, 448.8],
    [8, 8, 1, 0, 0, 0, 15742, 13368, 512, 0, 344.2],
    [4, 4, 2, 0, 0, 0, 10146, 12909, 480, 0, 402.9],
  ],
  /* RD_PIPE = 1, RD_OUTQ = 4: the streaming engine (loop 4, the tree arbiter) */
  1: [
    [4, 4, 1, 1, 1, 0, 7839, 7763, 256, 0, 469.3],
    [4, 4, 2, 1, 1, 0, 9881, 11811, 480, 0, 379.1],
    [4, 4, 4, 1, 1, 0, 15005, 19991, 928, 0, 357.7],
    [8, 4, 1, 1, 1, 0, 13177, 7947, 256, 0, 436.7],
    [4, 8, 1, 1, 1, 0, 15049, 15471, 512, 0, 480.8],
    [8, 8, 1, 1, 1, 0, 25288, 15795, 512, 0, 469.3],
    [2, 4, 1, 1, 1, 0, 4741, 7629, 256, 0, 456.2],
    [4, 4, 1, 0, 1, 0, 5001, 7213, 256, 0, 380.8],
    [4, 4, 1, 1, 0, 0, 6161, 7384, 256, 0, 447.0],
    [4, 4, 1, 0, 0, 0, 3366, 6834, 256, 0, 380.8],
    [8, 8, 2, 0, 0, 0, 10562, 21720, 960, 0, 301.6],
    [4, 4, 1, 1, 1, 4, 9642, 11183, 256, 64, 469.3],
    [8, 8, 1, 0, 0, 0, 6456, 13534, 512, 0, 346.6],
    [4, 4, 2, 0, 0, 0, 5056, 10940, 480, 0, 337.0],
  ],
};

/* The first array revision — the rows every number above is compared against,
   kept as measured; not fitted. Same columns. */
export const KX_ROWS_R1 = [
  [4, 4, 1, 1, 1, 0, 9914, 7390, 256, 0, 492.1],
  [4, 4, 2, 1, 1, 0, 14467, 13552, 480, 0, 361.9],
  [4, 4, 4, 1, 1, 0, 22847, 21688, 928, 0, 341.6],
  [8, 4, 1, 1, 1, 0, 15132, 7508, 256, 0, 498.0],
  [4, 8, 1, 1, 1, 0, 18219, 14778, 512, 0, 496.5],
  [8, 8, 1, 1, 1, 0, 28194, 15000, 512, 0, 498.0],
  [2, 4, 1, 1, 1, 0, 6237, 7310, 256, 0, 495.3],
  [4, 4, 1, 0, 1, 0, 9543, 7199, 256, 0, 451.3],
  [4, 4, 1, 1, 0, 0, 7694, 7017, 256, 0, 472.6],
  [4, 4, 1, 0, 0, 0, 7350, 6830, 256, 0, 451.1],
  [8, 8, 2, 0, 0, 0, 27968, 25969, 960, 0, 362.1],
  [4, 4, 1, 1, 1, 4, 11865, 10788, 256, 64, 456.0],
  [8, 8, 1, 0, 0, 0, 17718, 13613, 512, 0, 381.1],
  [4, 4, 2, 0, 0, 0, 12155, 13022, 480, 0, 361.8],
];

/* The ship at RD_OUTQ 1 / 2 / 4 / 8 with the streaming engine: the queue
   depth is bookkeeping, not datapath. [q, LUT, FF, Fmax] */
export const KX_OUTQ = [
  [1, 9607, 11147, 444.8],
  [2, 9607, 11151, 444.8],
  [4, 9642, 11183, 469.3],
  [8, 9678, 11231, 469.3],
];

/* FF: linear least-squares per family over
   [1, N, M, M·N, N·(K−1), N·[K>1], rSASD, wSASD·N, CDC] — from kx_cost.py --json */
export const KX_FF = {
  0: {
    base: 44.38,
    c_n: 1810.02,
    c_m: 27.71,
    c_mn: -0.167,
    c_k: 1014.99,
    c_kfill: 514.88,
    r_sasd: -236.56,
    w_sasd: -128.52,
    cdc: 841.85,
  },
  1: {
    base: -2.97,
    c_n: 1926.33,
    c_m: 64.55,
    c_mn: -6.09,
    c_k: 1012.05,
    c_kfill: -4.19,
    r_sasd: -543.02,
    w_sasd: -158.18,
    cdc: 829.97,
  },
};

/** The LUT step table of one family, read straight from its rows (kx_cost.py LutSteps). */
export function kxSteps(rows) {
  const g = (m, n, k, r, w, c) =>
    rows.find(
      (x) =>
        x[0] === m &&
        x[1] === n &&
        x[2] === k &&
        x[3] === r &&
        x[4] === w &&
        x[5] === c,
    )?.[6];
  const ship = g(4, 4, 1, 1, 1, 0);
  const pick = (f, xs) =>
    Object.fromEntries(
      xs.filter((x) => g(...f(x)) !== undefined).map((x) => [x, g(...f(x))]),
    );
  const s = {
    ship,
    M: pick((m) => [m, 4, 1, 1, 1, 0], [2, 4, 8]),
    N: pick((n) => [4, n, 1, 1, 1, 0], [4, 8]),
    K: pick((k) => [4, 4, k, 1, 1, 0], [1, 2, 4]),
    MN88: g(8, 8, 1, 1, 1, 0),
    RSASD: (g(4, 4, 1, 0, 1, 0) ?? ship) - ship,
    WSASD: (g(4, 4, 1, 1, 0, 0) ?? ship) - ship,
    CDC: ((g(4, 4, 1, 1, 1, 4) ?? ship) - ship) / 4,
  };
  /* the both-SASD saving at 4×4 and 8×8: a·N + b·M·N through the two points */
  s.S4 = ship - (g(4, 4, 1, 0, 0, 0) ?? ship);
  const s8 = (s.MN88 ?? 0) - (g(8, 8, 1, 0, 0, 0) ?? s.MN88 ?? 0);
  s.SASD_B = s8 ? (s8 - 2 * s.S4) / (64 - 32) : 0;
  s.SASD_A = (s.S4 - s.SASD_B * 16) / 4;
  /* K under a shared write path grows as a·N + b·M·N too: K2 costs 1,690 at
     4×4 SASD and 4,106 at 8×8 SASD (streaming) — a per-home constant read at
     4×4 missed 8×8 by 6.9% */
  const k2s4 = (g(4, 4, 2, 0, 0, 0) ?? 0) - (g(4, 4, 1, 0, 0, 0) ?? 0);
  const k2s8 = (g(8, 8, 2, 0, 0, 0) ?? 0) - (g(8, 8, 1, 0, 0, 0) ?? 0);
  s.KS_B = k2s8 ? (k2s8 - 2 * k2s4) / (64 - 32) : 0;
  s.KS_A = k2s4 ? (k2s4 - s.KS_B * 16) / 4 : 0;
  s.K2 = (s.K[2] ?? ship) - ship;
  return s;
}
export const KX_LUT = { 0: kxSteps(KX_ROWS[0]), 1: kxSteps(KX_ROWS[1]) };

function interp(table, x) {
  const ks = Object.keys(table)
    .map(Number)
    .sort((a, b) => a - b);
  if (table[x] !== undefined) return table[x];
  if (ks.length < 2) return table[ks[0]];
  let a, b;
  if (x < ks[0]) [a, b] = [ks[0], ks[1]];
  else if (x > ks[ks.length - 1])
    [a, b] = [ks[ks.length - 2], ks[ks.length - 1]];
  else {
    a = Math.max(...ks.filter((k) => k <= x));
    b = Math.min(...ks.filter((k) => k >= x));
  }
  return table[a] + ((table[b] - table[a]) * (x - a)) / (b - a);
}

/** LUT estimate for one family: ship + Σ per-knob measured deltas + the M×N interaction. */
export function kxLut(fam, m, n, k, rsamd, wsamd, ncdc) {
  const s = KX_LUT[fam];
  const ship = s.ship;
  const dM = interp(s.M, m) - ship;
  const dN = interp(s.N, n) - ship;
  const xMN =
    s.MN88 != null
      ? s.MN88 - ship - ((s.M[8] ?? ship) - ship) - ((s.N[8] ?? ship) - ship)
      : 0;
  const dMN = xMN * ((m - 4) / 4) * ((n - 4) / 4);
  let dK = (interp(s.K, k) - ship) * (n / 4);
  if (!wsamd && k > 1 && s.K2)
    dK = ((interp(s.K, k) - ship) / s.K2) * (s.KS_A * n + s.KS_B * m * n);
  const both = s.SASD_A * n + s.SASD_B * m * n;
  const scale = s.S4 ? both / s.S4 : 1;
  /* one side alone is its 4×4 delta scaled by the same growth; the two are not
     additive — read-SASD alone can COST, sharing one engine's arbiter */
  let dS = 0;
  if (!rsamd && !wsamd) dS = -both;
  else if (!rsamd) dS = s.RSASD * scale;
  else if (!wsamd) dS = s.WSASD * scale;
  return Math.round(ship + dM + dN + dMN + dK + dS + s.CDC * ncdc);
}

export function kxFf(fam, m, n, k, rsamd, wsamd, ncdc) {
  const c = KX_FF[fam];
  if (!c) return null;
  return Math.round(
    c.base +
      c.c_n * n +
      c.c_m * m +
      c.c_mn * m * n +
      c.c_k * n * (k - 1) +
      c.c_kfill * n * (k > 1 ? 1 : 0) +
      c.r_sasd * (1 - rsamd) +
      c.w_sasd * (1 - wsamd) * n +
      c.cdc * ncdc,
  );
}

/** URAM: 64 per home at K=1 (2 MB, 539-bit rows); measured 256/480/928 at K 1/2/4 for N=4. */
export function kxUram(n, k) {
  const perHome = { 1: 64, 2: 120, 4: 232 };
  return n * (perHome[k] ?? 64 + 56 * (k - 1));
}
export const kxBram = (ncdc) => 16 * ncdc;

/* ---------------- xbar-cache: the vendor path at the same shape ----------------
 * One block design, one synthesis, same 300 MHz clock (scripts/tcl/ooc_vendor_xc.tcl):
 * a 4×4 SmartConnect at 512 bits with one system_cache per channel. In that BD
 * the vendor cache did NOT build its 2 MB — 17 BRAM per cache at its default
 * data-memory type. The 2 MB row is system_cache alone (ooc_syscache.tcl). */
export const KX_VENDOR = {
  /* per-instance rows of that block design's hierarchy report; they sum to
     its 16,062 LUT / 13,330 FF total */
  smc_4x4: { lut: 8887, ff: 7738, lutram: 1088, srl: 301, bram: 0, uram: 0 },
  syscache_default_x4: {
    lut: 7175,
    ff: 5592,
    lutram: 48,
    srl: 752,
    bram: 68,
    uram: 0,
  },
  syscache_2mb: { lut: 8279, ff: 5238, bram: 561, uram: 0, fmax: 244 }, // per cache, ×4
};
KX_VENDOR.composed_default = {
  lut: KX_VENDOR.smc_4x4.lut + KX_VENDOR.syscache_default_x4.lut, // 16,062
  ff: KX_VENDOR.smc_4x4.ff + KX_VENDOR.syscache_default_x4.ff, // 13,330
  bram: KX_VENDOR.syscache_default_x4.bram,
};
KX_VENDOR.composed_2mb = {
  lut: KX_VENDOR.smc_4x4.lut + 4 * KX_VENDOR.syscache_2mb.lut, // 42,003
  ff: KX_VENDOR.smc_4x4.ff + 4 * KX_VENDOR.syscache_2mb.ff, // 28,690
  bram: 4 * KX_VENDOR.syscache_2mb.bram, // 2,244
  fmax: KX_VENDOR.syscache_2mb.fmax,
};

/* ---------------- station bus: measured rows ---------------- */
/* ship recipe: sb_line4, FW=256, AW=43, NQ=4, PORTW=2, LINK_FULL=0, OST 4/8/2 */
export const STN_ROWS = [
  { loop: "baseline", bus: 23688, ff: 42246, bram: 97, slr1: 8370, hub: 2099 },
  {
    loop: "1 (hub sel*PW — barrel, refuted)",
    bus: 29951,
    ff: 42277,
    bram: 90.5,
    slr1: 14808,
    hub: 8548,
  },
  {
    loop: "2 (hub case-mux)",
    bus: 23486,
    ff: 42231,
    bram: 90,
    slr1: 8159,
    hub: 2122,
  },
  {
    loop: "3 (NSPM≤1 generator fold) — design point",
    bus: 23053,
    ff: 42223,
    bram: 90,
    slr1: 8044,
    hub: 2122,
  },
  {
    loop: "4 (hub keep — refuted)",
    bus: 27107,
    ff: 43351,
    bram: 90,
    slr1: 8685,
    hub: 2685,
  },
];

/* per-port costs at the design point (loop 3), from the hierarchy report */
export const STN_PORT = {
  hub_line: 2122, // sb_stn_line with 3 managers + 4 subs (8 hubs)
  hub_leaf: 1215, // a station with 1 injection port + 4 subs
  nmu_512_jtag: 1158, // OUTST 4, packs to the mesh
  nmu_512_xdma: 967, // OUTST 8
  nmu_32_ctrl: 609, // OUTST 2, FORCE_PLACE
  nsu_512: 760, // NSPM<=1: no split
  nsu_32: 808, // full AXI4 burst endpoint with the F1 split
  nsu_32_single_beat: 808 - 318, // SINGLE_BEAT: skid channel queues (verified, not in ship)
  link_pair: 431, // one req + one rsp direction
};

/** Σ(station) + links: every port and hub itemised. */
export function stnEstimate({ stations }) {
  let lut = 0;
  const items = [];
  const add = (label, n, c) => {
    if (n) {
      items.push({ label, n, each: c, lut: n * c });
      lut += n * c;
    }
  };
  for (const s of stations) {
    add("hub (line station)", s.managers > 0 ? 1 : 0, STN_PORT.hub_line);
    add("hub (leaf station)", s.managers > 0 ? 0 : 1, STN_PORT.hub_leaf);
    add("NMU 512b JTAG-class", s.nmu512 ?? 0, STN_PORT.nmu_512_jtag);
    add("NMU 32b ctrl", s.nmu32 ?? 0, STN_PORT.nmu_32_ctrl);
    add("NSU 512b", s.nsu512 ?? 0, STN_PORT.nsu_512);
    add("NSU 32b (burst)", s.nsu32 ?? 0, STN_PORT.nsu_32);
    add("NSU 32b SINGLE_BEAT", s.nsu32sb ?? 0, STN_PORT.nsu_32_single_beat);
  }
  add(
    "link pair",
    Math.max(0, stations.length - 1) * 1,
    STN_PORT.link_pair * 2,
  );
  return { lut, items };
}
