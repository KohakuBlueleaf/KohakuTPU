/**
 * Resource estimator data: the fused xbar-cache (kx_mempath_e) and the station
 * bus, each as a per-knob cost model fitted to and validated against one OOC
 * synthesis per configuration (xcvu13p-fhgb2104-2L-e, Vivado 2024.2, 300 MHz
 * target, synthesis only). Every number here is transcribed from the
 * result.txt of one scripts/tcl/ooc_kx.tcl run per xbar-cache row and one
 * scripts/tcl/ooc_line_d2.tcl run per station row; the model is
 * scripts/py/kx_cost.py, whose --json output this file mirrors.
 *
 * The xbar-cache LUT model is a per-knob STEP TABLE (the ship plus each knob's
 * measured delta at each measured step, interpolated between steps, plus one
 * M×N interaction term) because LUT is convex in M and in K; FF is a linear
 * least-squares fit. Both are validated against every row in ROWS.
 */

export const PART = "xcvu13p-fhgb2104-2L-e";
export const TARGET_MHZ = 300;

/* ---------------- xbar-cache: measured rows ---------------- */
/* [M, N, K, rsamd, wsamd, n_cdc, LUT, FF, URAM, BRAM, Fmax] — 64 URAM per home */
export const KX_ROWS = [
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

/* LUT step tables (ship = M4 N4 K1 SAMD, no CDC = 9,914) */
export const KX_LUT = {
  M: { 2: 6237, 4: 9914, 8: 15132 },
  N: { 4: 9914, 8: 18219 },
  K: { 1: 9914, 2: 14467, 4: 22847 },
  MN88: 28194,
  RSASD: 9543 - 9914,
  WSASD_PER_N: (7694 - 9914) / 4,
  CDC: (11865 - 9914) / 4,
  /* both-SASD saving measured at 4×4 (2,564) and 8×8 (10,476): it grows as
     a·N + b·M·N, because SASD collapses N write paths AND each path's M-way
     fan-in. Split into read/write by their 4×4 shares (371 / 2,220). */
  SASD_S4: 2564,
  SASD_S8: 10476,
};
const SASD_B = (KX_LUT.SASD_S8 - 2 * KX_LUT.SASD_S4) / (64 - 32);
const SASD_A = (KX_LUT.SASD_S4 - SASD_B * 16) / 4;
const R_SHARE = -KX_LUT.RSASD / KX_LUT.SASD_S4;
/* K under a shared write path: 4×4 K2 SASD 12,155 − K1 SASD 7,350 = +4,805
   vs +4,553 under SAMD → +63 per home-word. One measured point, applied as a
   constant. */
const K_SASD_ADJ = ((12155 - 7350) - (14467 - 9914)) / 4;

/* FF: linear least-squares over [1, N, M, M·N, N·(K−1), N·[K>1], rSASD, wSASD·N, CDC] */
export const KX_FF = {
  base: -19.8,
  c_n: 1837.7,
  c_m: 29.6,
  c_mn: -0.7,
  c_k: 1012.3,
  c_kfill: 525.5,
  r_sasd: -185.0,
  w_sasd: -123.3,
  cdc: 837.4,
};

function interp(table, x) {
  const ks = Object.keys(table)
    .map(Number)
    .sort((a, b) => a - b);
  if (table[x] !== undefined) return table[x];
  let a, b;
  if (x < ks[0]) [a, b] = [ks[0], ks[1]];
  else if (x > ks[ks.length - 1]) [a, b] = [ks[ks.length - 2], ks[ks.length - 1]];
  else {
    a = Math.max(...ks.filter((k) => k <= x));
    b = Math.min(...ks.filter((k) => k >= x));
  }
  return table[a] + ((table[b] - table[a]) * (x - a)) / (b - a);
}

/** LUT estimate: ship + Σ per-knob measured deltas + the M×N interaction. */
export function kxLut(m, n, k, rsamd, wsamd, ncdc) {
  const ship = KX_LUT.M[4];
  const dM = interp(KX_LUT.M, m) - ship;
  const dN = interp(KX_LUT.N, n) - ship;
  const xMN = KX_LUT.MN88 - ship - (KX_LUT.M[8] - ship) - (KX_LUT.N[8] - ship);
  const dMN = xMN * ((m - 4) / 4) * ((n - 4) / 4);
  const dK = (interp(KX_LUT.K, k) - ship) * (n / 4);
  const both = SASD_A * n + SASD_B * m * n;
  const dR = -both * R_SHARE * (1 - rsamd);
  const dW = -both * (1 - R_SHARE) * (1 - wsamd) + K_SASD_ADJ * n * (k - 1) * (1 - wsamd);
  const dC = KX_LUT.CDC * ncdc;
  return Math.round(ship + dM + dN + dMN + dK + dR + dW + dC);
}

export function kxFf(m, n, k, rsamd, wsamd, ncdc) {
  const c = KX_FF;
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
  syscache_default_x4: { lut: 7175, ff: 5592, lutram: 48, srl: 752, bram: 68, uram: 0 },
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
  { loop: "1 (hub sel*PW — barrel, refuted)", bus: 29951, ff: 42277, bram: 90.5, slr1: 14808, hub: 8548 },
  { loop: "2 (hub case-mux)", bus: 23486, ff: 42231, bram: 90, slr1: 8159, hub: 2122 },
  { loop: "3 (NSPM≤1 generator fold) — design point", bus: 23053, ff: 42223, bram: 90, slr1: 8044, hub: 2122 },
  { loop: "4 (hub keep — refuted)", bus: 27107, ff: 43351, bram: 90, slr1: 8685, hub: 2685 },
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
  add("link pair", Math.max(0, stations.length - 1) * 1, STN_PORT.link_pair * 2);
  return { lut, items };
}
