<script setup>
/**
 * Declarative block diagram.
 * nodes: [{ id, x, y, w?, h?, label, sub?, accent?, group? }]  grid units
 * edges: [{ from, to, label?, accent?, dash?, dir?: 'h'|'v'|'auto' }]
 *   from/to are node ids, optionally 'id:side' with side in t|b|l|r
 * groups: [{ x, y, w, h, label }]  drawn behind, for hierarchy boxes
 */
const props = defineProps({
  nodes: { type: Array, required: true },
  edges: { type: Array, default: () => [] },
  groups: { type: Array, default: () => [] },
  unit: { type: Number, default: 16 },
  pad: { type: Number, default: 12 },
  // [{ key, label }] — a chip per key; a node/group carrying `tag: key` dims
  // when its chip is off, and so does every wire touching a dimmed node.
  tags: { type: Array, default: () => [] },
});

const U = props.unit;

/* ---- tag toggles: dim, never re-route, so the layout stays put ---------- */
const hidden = reactive(new Set());
const toggleTag = (k) => (hidden.has(k) ? hidden.delete(k) : hidden.add(k));
const soloTag = (k) => {
  hidden.clear();
  for (const t of props.tags) if (t.key !== k) hidden.add(t.key);
};
const offTag = (t) => t != null && hidden.has(t);
const offEdge = (e) => {
  if (offTag(e.tag)) return true;
  const a = props.nodes.find((n) => n.id === String(e.from).split(":")[0]);
  const b = props.nodes.find((n) => n.id === String(e.to).split(":")[0]);
  return offTag(a?.tag) || offTag(b?.tag);
};
const N = computed(() =>
  Object.fromEntries(
    props.nodes.map((n) => [n.id, { ...n, w: n.w ?? 10, h: n.h ?? 3.2 }]),
  ),
);

const box = (n) => ({ x: n.x * U, y: n.y * U, w: n.w * U, h: n.h * U });

/* Three rules this router exists to keep:
 *   1. no connection overlaps another,
 *   2. no arrow runs along a box edge,
 *   3. every arrow meets a box NORMAL to the side it enters.
 * The old router broke all three by returning one anchor per side, so every
 * edge into a left side landed on the same pixel and the last segment could
 * arrive parallel to the border. */
const LEAD = 14; // the normal stub every edge leaves and enters on

const nrm = { t: [0, -1], b: [0, 1], l: [-1, 0], r: [1, 0] };
const OPP = { t: "b", b: "t", l: "r", r: "l" };

const ref = (r) => {
  const [id, side] = String(r).split(":");
  return { id, side: side || null };
};

/** Sides chosen from geometry when the author did not name them. */
function sidesFor(e) {
  const f = ref(e.from);
  const t = ref(e.to);
  const A = N.value[f.id];
  const B = N.value[t.id];
  if (!A || !B) return { f: f.side || "r", t: t.side || "l" };
  const a = box(A);
  const b = box(B);
  const dx = b.x + b.w / 2 - (a.x + a.w / 2);
  const dy = b.y + b.h / 2 - (a.y + a.h / 2);
  const dir = e.dir ?? "auto";
  const horiz = dir === "h" || (dir !== "v" && Math.abs(dx) >= Math.abs(dy));
  const fs = f.side || (horiz ? (dx > 0 ? "r" : "l") : dy > 0 ? "b" : "t");
  const ts = t.side || OPP[fs];
  return { f: fs, t: ts };
}

/* Slot assignment: every edge touching the same side of the same node gets its
 * own position along that side, ordered by where the other end sits, so wires
 * never converge on one point and never cross each other on approach. */
const slots = computed(() => {
  const want = {};
  const list = props.edges.map((e, i) => ({ e, i, s: sidesFor(e) }));
  for (const { e, i, s } of list) {
    const f = ref(e.from);
    const t = ref(e.to);
    const A = N.value[f.id];
    const B = N.value[t.id];
    if (!A || !B) continue;
    const a = box(A);
    const b = box(B);
    (want[`${f.id}:${s.f}`] ??= []).push({
      i,
      end: "f",
      key: s.f === "l" || s.f === "r" ? b.y + b.h / 2 : b.x + b.w / 2,
    });
    (want[`${t.id}:${s.t}`] ??= []).push({
      i,
      end: "t",
      key: s.t === "l" || s.t === "r" ? a.y + a.h / 2 : a.x + a.w / 2,
    });
  }
  const out = {};
  for (const [k, arr] of Object.entries(want)) {
    arr.sort((p, q) => p.key - q.key);
    arr.forEach((p, j) => {
      out[`${p.i}:${p.end}`] = { idx: j, n: arr.length, side: k.split(":")[1] };
    });
  }
  return out;
});

function point(id, side, slot) {
  const n = N.value[id];
  if (!n) return { x: 0, y: 0 };
  const b = box(n);
  const j = slot ? (slot.idx + 1) / (slot.n + 1) : 0.5;
  switch (side) {
    case "t":
      return { x: b.x + b.w * j, y: b.y };
    case "b":
      return { x: b.x + b.w * j, y: b.y + b.h };
    case "l":
      return { x: b.x, y: b.y + b.h * j };
    default:
      return { x: b.x + b.w, y: b.y + b.h * j };
  }
}

/* A wire must also not cross a BOX, so the middle is routed rather than
 * assumed: an orthogonal A* over the channels that run between boxes, with
 * already-used channels penalised so two wires do not share a lane. */
const CLR = 12; // clearance kept around every box
const SEP = 10; // minimum pitch between routing lanes, so wires read apart
const BEND = 14; // a bend costs this much, to prefer straight runs
const CROSS = 45; // cutting across a wire already routed costs this much
const JUMP = 5; // radius of the arc a horizontal wire draws over a vertical one

/* A move across a wire already routed the other way. The lane bookkeeping
 * below only knew about two wires SHARING a lane; a wire cutting straight
 * through another read as one with it, and the router had no reason not to. */
function crossesUsed(a, b, ax, maps) {
  let n = 0;
  // A run meeting the END of a claimed segment is still a crossing: the
  // segment ends where the wire's fixed stub, or its next run, carries on.
  for (const m of maps) {
    if (ax) {
      const x = a.x;
      const lo = Math.min(a.y, b.y);
      const hi = Math.max(a.y, b.y);
      for (const [k, segs] of Object.entries(m)) {
        if (k[0] !== "y") continue;
        const y = Number(k.slice(1));
        if (y <= lo + 0.5 || y >= hi - 0.5) continue;
        if (segs.some(([s0, s1]) => s0 - 0.5 <= x && x <= s1 + 0.5)) n++;
      }
    } else {
      const y = a.y;
      const lo = Math.min(a.x, b.x);
      const hi = Math.max(a.x, b.x);
      for (const [k, segs] of Object.entries(m)) {
        if (k[0] !== "x") continue;
        const x = Number(k.slice(1));
        if (x <= lo + 0.5 || x >= hi - 0.5) continue;
        if (segs.some(([s0, s1]) => s0 - 0.5 <= y && y <= s1 + 0.5)) n++;
      }
    }
  }
  return n;
}

function boxes() {
  return props.nodes.map((n) => box(N.value[n.id]));
}

function hitsBox(p, q, bs) {
  const x0 = Math.min(p.x, q.x) - 0.5;
  const x1 = Math.max(p.x, q.x) + 0.5;
  const y0 = Math.min(p.y, q.y) - 0.5;
  const y1 = Math.max(p.y, q.y) + 0.5;
  for (const b of bs) {
    if (x1 <= b.x + 1 || x0 >= b.x + b.w - 1) continue;
    if (y1 <= b.y + 1 || y0 >= b.y + b.h - 1) continue;
    return true;
  }
  return false;
}

/* Lanes must be far enough apart to READ as separate, so the candidate lines
 * are thinned to a minimum pitch and wide gaps are subdivided at that pitch.
 * Two wires one pixel apart do not overlap and still look like one wire. */
function lanes(raw, keep) {
  const s = [...new Set(raw)].sort((a, b) => a - b);
  const thin = [];
  for (const v of s) {
    if (keep.includes(v) || !thin.length || v - thin[thin.length - 1] >= SEP)
      thin.push(v);
  }
  const out = [];
  for (let i = 0; i < thin.length; i++) {
    out.push(thin[i]);
    const gap = thin[i + 1] - thin[i];
    if (gap > 2 * SEP) {
      const n = Math.min(4, Math.floor(gap / SEP) - 1);
      for (let k = 1; k <= n; k++) out.push(thin[i] + (gap * k) / (n + 1));
    }
  }
  return [...new Set([...out, ...keep])].sort((a, b) => a - b);
}

function route(p1, p2, bs, used, fixed = {}) {
  const rx = [];
  const ry = [];
  for (const b of bs) {
    rx.push(b.x - CLR, b.x + b.w + CLR);
    ry.push(b.y - CLR, b.y + b.h + CLR);
  }
  // Lanes OUTSIDE the bounding box, or a return wire has nowhere to go once
  // the obvious channel is taken and detours over the whole diagram.
  const x0 = Math.min(...rx);
  const x1 = Math.max(...rx);
  const y0 = Math.min(...ry);
  const y1 = Math.max(...ry);
  for (let k = 1; k <= 4; k++) {
    rx.push(x0 - k * SEP, x1 + k * SEP);
    ry.push(y0 - k * SEP, y1 + k * SEP);
  }
  const X = lanes(rx, [p1.x, p2.x]);
  const Y = lanes(ry, [p1.y, p2.y]);
  const key = (i, j) => i * 1000 + j;
  const start = key(X.indexOf(p1.x), Y.indexOf(p1.y));
  const goal = key(X.indexOf(p2.x), Y.indexOf(p2.y));

  const g = { [start]: 0 };
  const prev = {};
  const open = [[0, start, -1]];
  const seen = new Set();
  while (open.length) {
    open.sort((a, b) => a[0] - b[0]);
    const [, cur, cdir] = open.shift();
    if (cur === goal) break;
    if (seen.has(cur * 8 + cdir + 2)) continue;
    seen.add(cur * 8 + cdir + 2);
    const ci = Math.floor(cur / 1000);
    const cj = cur % 1000;
    const nb = [
      [ci - 1, cj, 0],
      [ci + 1, cj, 0],
      [ci, cj - 1, 1],
      [ci, cj + 1, 1],
    ];
    for (const [ni, nj, ax] of nb) {
      if (ni < 0 || nj < 0 || ni >= X.length || nj >= Y.length) continue;
      const a = { x: X[ci], y: Y[cj] };
      const b = { x: X[ni], y: Y[nj] };
      if (hitsBox(a, b, bs)) continue;
      // Per INTERVAL, not per lane: two collinear runs that never overlap may
      // share a lane, and forcing them apart is what put a jog in every
      // straight stage-to-stage wire.
      const lane = ax ? `x${X[ni]}` : `y${Y[nj]}`;
      const seg = ax
        ? [Math.min(a.y, b.y), Math.max(a.y, b.y)]
        : [Math.min(a.x, b.x), Math.max(a.x, b.x)];
      // SEP of clearance, not bare overlap: two runs that merely TOUCH at a
      // turn point read as one wire, which is the whole thing being avoided.
      const busy = (used[lane] ?? []).some(
        ([lo, hi]) => seg[0] < hi + SEP && seg[1] > lo - SEP,
      );
      const cost =
        Math.abs(b.x - a.x) +
        Math.abs(b.y - a.y) +
        (cdir >= 0 && cdir !== ax ? BEND : 0) +
        (busy ? 60 : 0) +
        crossesUsed(a, b, ax, [used, fixed]) * CROSS;
      const k = key(ni, nj);
      if (g[k] === undefined || g[cur] + cost < g[k]) {
        g[k] = g[cur] + cost;
        prev[k] = [cur, ax];
        open.push([g[k] + Math.abs(b.x - p2.x) + Math.abs(b.y - p2.y), k, ax]);
      }
    }
  }
  if (g[goal] === undefined) return [p1, p2];
  const out = [];
  let k = goal;
  while (k !== undefined && k !== start) {
    out.unshift({ x: X[Math.floor(k / 1000)], y: Y[k % 1000] });
    const p = prev[k];
    k = p ? p[0] : undefined;
  }
  out.unshift(p1);
  // Claim the intervals this wire occupies so the next one routes clear of them.
  for (let i = 0; i < out.length - 1; i++) {
    const a = out[i];
    const b = out[i + 1];
    if (a.x === b.x)
      (used[`x${a.x}`] ??= []).push([Math.min(a.y, b.y), Math.max(a.y, b.y)]);
    else
      (used[`y${a.y}`] ??= []).push([Math.min(a.x, b.x), Math.max(a.x, b.x)]);
  }
  return out;
}

/** Drop collinear interior points so the path is minimal. */
function simplify(p) {
  const o = [p[0]];
  for (let i = 1; i < p.length - 1; i++) {
    const a = o[o.length - 1];
    const b = p[i];
    const c = p[i + 1];
    if ((a.x === b.x && b.x === c.x) || (a.y === b.y && b.y === c.y)) continue;
    o.push(b);
  }
  o.push(p[p.length - 1]);
  return o;
}

/* Labels are placed after every wire is routed, so one can be moved out of
 * another's way. Placing each at its own midpoint independently is what turned
 * a clean routing pass into six text collisions. */
/** A group caption clipped to the width of the box it sits on. */
function gLabel(g) {
  const s = String(g.label ?? "");
  const max = Math.max(6, Math.floor((g.w * U - 12) / 5.4));
  return s.length <= max
    ? s
    : s.slice(0, max - 1).replace(/[ ,;.]+$/, "") + "…";
}

function placeLabels(rows) {
  // Seeded with the boxes: a label nudged clear of another label must not land
  // on a component instead.
  const taken = boxes().map((b) => ({ x: b.x, y: b.y, w: b.w, h: b.h }));
  // Group captions sit above their box and are text too.
  for (const g of props.groups) {
    if (!g.label) continue;
    taken.push({
      x: g.x * U + 6,
      y: g.y * U - 15,
      w: gLabel(g).length * 5.4 + 4,
      h: 13,
    });
  }
  const nBoxes = taken.length; // everything after this index is a label
  const hit = (r) =>
    taken.some(
      (t) =>
        r.x < t.x + t.w &&
        r.x + r.w > t.x &&
        r.y < t.y + t.h &&
        r.y + r.h > t.y,
    );
  for (const row of rows) {
    if (!row.e.label) continue;
    const w = String(row.e.label).length * 5.6 + 6; // measured: 5.5 px per glyph
    const seg = longest(row.pts);
    const vert = Math.abs(seg.b.y - seg.a.y) > Math.abs(seg.b.x - seg.a.x);
    let put = null;
    for (const t of [0.5, 0.36, 0.64, 0.24, 0.76]) {
      for (const push of [0, -13, 13, -26, 26]) {
        const cx = seg.a.x + (seg.b.x - seg.a.x) * t + (vert ? 6 + push : 0);
        const cy = seg.a.y + (seg.b.y - seg.a.y) * t - (vert ? 0 : 6 + push);
        const r = vert
          ? { x: cx - 2, y: cy - 9, w, h: 12 }
          : { x: cx - w / 2, y: cy - 9, w, h: 12 };
        if (!hit(r)) {
          put = { x: cx, y: cy, anchor: vert ? "start" : "middle", r };
          break;
        }
      }
      if (put) break;
    }
    // Nothing was clear of BOTH labels and boxes. Retry against labels only:
    // sitting near a box is survivable, sitting on another label is not.
    if (!put) {
      for (const t of [0.5, 0.36, 0.64, 0.2, 0.8, 0.08, 0.92]) {
        for (const push of [0, -13, 13, -26, 26, -40, 40, -54, 54]) {
          const cx = seg.a.x + (seg.b.x - seg.a.x) * t + (vert ? 6 + push : 0);
          const cy = seg.a.y + (seg.b.y - seg.a.y) * t - (vert ? 0 : 6 + push);
          const r = vert
            ? { x: cx - 2, y: cy - 9, w, h: 12 }
            : { x: cx - w / 2, y: cy - 9, w, h: 12 };
          const clash = taken
            .slice(nBoxes)
            .some(
              (t2) =>
                r.x < t2.x + t2.w &&
                r.x + r.w > t2.x &&
                r.y < t2.y + t2.h &&
                r.y + r.h > t2.y,
            );
          if (!clash) {
            put = { x: cx, y: cy, anchor: vert ? "start" : "middle", r };
            break;
          }
        }
        if (put) break;
      }
    }

    // Still nothing, so take the least-bad rather than a blind midpoint.
    if (!put) {
      let bestR = null;
      let bestA = Infinity;
      for (const t of [0.5, 0.36, 0.64, 0.24, 0.76]) {
        for (const push of [0, -13, 13, -26, 26, -39, 39]) {
          const cx = seg.a.x + (seg.b.x - seg.a.x) * t + (vert ? 6 + push : 0);
          const cy = seg.a.y + (seg.b.y - seg.a.y) * t - (vert ? 0 : 6 + push);
          const r = vert
            ? { x: cx - 2, y: cy - 9, w, h: 12 }
            : { x: cx - w / 2, y: cy - 9, w, h: 12 };
          const a = taken.reduce((s, t2) => {
            const ox = Math.min(r.x + r.w, t2.x + t2.w) - Math.max(r.x, t2.x);
            const oy = Math.min(r.y + r.h, t2.y + t2.h) - Math.max(r.y, t2.y);
            return s + (ox > 0 && oy > 0 ? ox * oy : 0);
          }, 0);
          if (a < bestA) {
            bestA = a;
            bestR = { x: cx, y: cy, anchor: vert ? "start" : "middle", r };
          }
        }
      }
      put = bestR;
    }
    taken.push(put.r);
    row.lab = put;
  }
  return rows;
}

function longest(p) {
  let best = 0;
  let len = -1;
  for (let k = 0; k < p.length - 1; k++) {
    const d = Math.abs(p[k + 1].x - p[k].x) + Math.abs(p[k + 1].y - p[k].y);
    if (d > len) {
      len = d;
      best = k;
    }
  }
  return { a: p[best], b: p[best + 1] };
}

/* Wires are routed one after another and each later one steers around the
 * earlier ones, so the ORDER decides how many crossings are left. Three orders
 * are tried and the one with the fewest crossings is kept. */
function routeAll(order) {
  const bs = boxes();
  const used = {};
  const fixed = {};
  const claim = (m, a, b) => {
    if (a.x === b.x)
      (m[`x${a.x}`] ??= []).push([Math.min(a.y, b.y), Math.max(a.y, b.y)]);
    else (m[`y${a.y}`] ??= []).push([Math.min(a.x, b.x), Math.max(a.x, b.x)]);
  };
  // Every wire's two stubs are fixed before any routing, so the first wire
  // routed already steers clear of the last one's.
  const ends = props.edges.map((e, i) => {
    const s = sidesFor(e);
    const p0 = point(ref(e.from).id, s.f, slots.value[`${i}:f`]);
    const p3 = point(ref(e.to).id, s.t, slots.value[`${i}:t`]);
    const nf = nrm[s.f];
    const nt = nrm[s.t];
    const p1 = { x: p0.x + nf[0] * LEAD, y: p0.y + nf[1] * LEAD };
    const p2 = { x: p3.x + nt[0] * LEAD, y: p3.y + nt[1] * LEAD };
    claim(fixed, p0, p1);
    claim(fixed, p2, p3);
    return { p0, p1, p2, p3 };
  });
  const rows = new Array(props.edges.length);
  for (const i of order) {
    const { p0, p1, p2, p3 } = ends[i];
    rows[i] = {
      e: props.edges[i],
      pts: simplify([p0, ...route(p1, p2, bs, used, fixed), p3]),
      lab: null,
    };
  }
  return rows;
}

const segsOf = (r) => {
  const o = [];
  for (let k = 0; k < r.pts.length - 1; k++) o.push([r.pts[k], r.pts[k + 1]]);
  return o;
};

/** Every proper crossing between a horizontal run of one wire and a vertical
 *  run of another: [{ i, j, x, y, seg }], `seg` the horizontal wire's segment. */
function crossingsOf(rows) {
  const out = [];
  for (let i = 0; i < rows.length; i++) {
    const si = segsOf(rows[i]);
    for (let j = 0; j < rows.length; j++) {
      if (i === j) continue;
      const sj = segsOf(rows[j]);
      si.forEach(([a, b], k) => {
        if (a.y !== b.y || a.x === b.x) return; // horizontal runs only
        const lo = Math.min(a.x, b.x);
        const hi = Math.max(a.x, b.x);
        for (const [c, d] of sj) {
          if (c.x !== d.x || c.y === d.y) continue; // against vertical runs
          const y0 = Math.min(c.y, d.y);
          const y1 = Math.max(c.y, d.y);
          if (
            lo + 0.5 < c.x &&
            c.x < hi - 0.5 &&
            y0 + 0.5 < a.y &&
            a.y < y1 - 0.5
          )
            out.push({ i, j, x: c.x, y: a.y, seg: k });
        }
      });
    }
  }
  return out;
}

const routed = computed(() => {
  const n = props.edges.length;
  const centre = (id) => {
    const b = box(N.value[id] ?? { x: 0, y: 0, w: 0, h: 0 });
    return { x: b.x + b.w / 2, y: b.y + b.h / 2 };
  };
  const span = (e) => {
    const a = centre(ref(e.from).id);
    const b = centre(ref(e.to).id);
    return Math.abs(a.x - b.x) + Math.abs(a.y - b.y);
  };
  const natural = [...Array(n).keys()];
  const longFirst = [...natural].sort(
    (i, j) => span(props.edges[j]) - span(props.edges[i]),
  );
  let best = null;
  for (const order of [natural, longFirst, [...longFirst].reverse()]) {
    const rows = routeAll(order);
    const x = crossingsOf(rows);
    if (!best || x.length < best.x.length) best = { rows, x };
    if (!x.length) break;
  }
  if (import.meta.env.DEV && best.x.length) {
    const names = best.x.map(
      (c) =>
        `${props.edges[c.i].from}→${props.edges[c.i].to} × ${props.edges[c.j].from}→${props.edges[c.j].to}`,
    );
    console.warn(
      `BlockDiagram: ${best.x.length} crossing(s) drawn as jumps:\n  ${names.join("\n  ")}`,
    );
  }
  return best;
});

const crossings = computed(() => routed.value.x);

/* A crossing that survives is DRAWN as one: the horizontal wire arcs over the
 * vertical one, the way a hand-drawn schematic does it, so two wires that
 * cross never read as one wire that turns. */
function pathD(row, i) {
  const segs = segsOf(row);
  const hops = crossings.value.filter((c) => c.i === i);
  let d = `M${row.pts[0].x},${row.pts[0].y}`;
  segs.forEach(([a, b], k) => {
    const xs = hops
      .filter((c) => c.seg === k)
      .map((c) => c.x)
      .sort((p, q) => (b.x > a.x ? p - q : q - p));
    for (const x of xs) {
      const dir = b.x > a.x ? 1 : -1;
      d += ` L${x - dir * JUMP},${a.y} A${JUMP},${JUMP} 0 0 ${dir > 0 ? 1 : 0} ${x + dir * JUMP},${a.y}`;
    }
    d += ` L${b.x},${b.y}`;
  });
  return d;
}

const paths = computed(() =>
  placeLabels(routed.value.rows).map((row, i) => ({
    ...row,
    d: pathD(row, i),
  })),
);

// Long subs are the common case in these docs and shrinking them to fit is
// how you get 7px text that still overflows. Wrap instead, then shrink only
// what is left. Mono glyphs run ~0.6em wide.
function wrap(text, boxW, size, maxLines) {
  const s = String(text ?? "").trim();
  if (!s) return [];
  const per = Math.max(6, Math.floor((boxW - 10) / (size * 0.6)));
  const lines = [];
  let cur = "";
  for (const w of s.split(/\s+/)) {
    if (!cur) cur = w;
    else if ((cur + " " + w).length <= per) cur += " " + w;
    else {
      lines.push(cur);
      cur = w;
    }
  }
  if (cur) lines.push(cur);
  // JOINING the remainder into the last line is what makes a sub overflow its
  // box no matter how far `fit` shrinks it. Drop what does not fit instead.
  if (lines.length <= maxLines) return lines;
  const keep = lines.slice(0, maxLines);
  keep[maxLines - 1] = keep[maxLines - 1].replace(/[ ,;.]+$/, "") + "…";
  return keep;
}

function fit(text, boxW, base) {
  const w = String(text ?? "").length * base * 0.6;
  const avail = boxW - 10;
  return w <= avail ? base : Math.max(7.5, (avail / w) * base);
}

/** Vertically centred stack of label lines then sub lines. */
function stack(n) {
  const b = box(N.value[n.id]);
  const lab = wrap(n.label, b.w, 11, 2);
  // As many sub lines as the box is actually tall enough to hold.
  const room = Math.max(1, Math.floor((b.h - 10 - lab.length * 12) / 11));
  const sub = wrap(n.sub, b.w, 9.5, Math.min(4, room));
  const H = lab.length * 12 + sub.length * 11;
  let y = b.y + b.h / 2 - H / 2 + 9;
  const rows = [];
  for (const t of lab) {
    rows.push({ t, y, cls: "dgm-label", weight: 600, size: fit(t, b.w, 11) });
    y += 12;
  }
  for (const t of sub) {
    rows.push({ t, y, cls: "dgm-sub", weight: 400, size: fit(t, b.w, 9.5) });
    y += 11;
  }
  return { cx: b.x + b.w / 2, rows };
}

const vb = computed(() => {
  const all = [
    ...props.nodes.map((n) => box({ ...n, w: n.w ?? 10, h: n.h ?? 3.2 })),
    ...props.groups.map((g) => ({
      x: g.x * U,
      y: g.y * U,
      w: g.w * U,
      h: g.h * U,
    })),
  ];
  // The wires may route outside the boxes, so the frame follows them too.
  const px = [
    ...all.map((b) => b.x),
    ...all.map((b) => b.x + b.w),
    ...paths.value.flatMap((p) => p.pts.map((q) => q.x)),
  ];
  const py = [
    ...all.map((b) => b.y),
    ...all.map((b) => b.y + b.h),
    ...paths.value.flatMap((p) => p.pts.map((q) => q.y)),
  ];
  const x0 = Math.min(...px) - props.pad;
  const y0 = Math.min(...py) - props.pad - 6;
  const x1 = Math.max(...px) + props.pad;
  const y1 = Math.max(...py) + props.pad;
  return `${x0} ${y0} ${x1 - x0} ${y1 - y0}`;
});
</script>

<template>
  <div class="dgm-wrap">
    <div v-if="props.tags.length" class="dgm-tags">
      <button
        v-for="t in props.tags"
        :key="t.key"
        type="button"
        class="dgm-tag"
        :class="hidden.has(t.key) ? 'dgm-tag-off' : ''"
        :aria-pressed="!hidden.has(t.key)"
        :title="`${hidden.has(t.key) ? 'show' : 'dim'} ${t.label} — double-click for only this`"
        @click="toggleTag(t.key)"
        @dblclick.prevent="soloTag(t.key)"
      >
        {{ t.label }}
      </button>
      <button
        v-if="hidden.size"
        type="button"
        class="dgm-tag dgm-tag-all"
        @click="hidden.clear()"
      >
        all
      </button>
    </div>
    <svg
      :viewBox="vb"
      class="dgm"
      role="img"
      :data-crossings="crossings.length"
    >
      <defs>
        <marker
          id="bd-arrow"
          viewBox="0 0 8 8"
          refX="7"
          refY="4"
          markerWidth="6"
          markerHeight="6"
          orient="auto-start-reverse"
        >
          <path d="M0,0 L8,4 L0,8 z" fill="currentColor" />
        </marker>
        <marker
          id="bd-arrow-a"
          viewBox="0 0 8 8"
          refX="7"
          refY="4"
          markerWidth="6"
          markerHeight="6"
          orient="auto-start-reverse"
        >
          <path d="M0,0 L8,4 L0,8 z" fill="var(--gem-main)" />
        </marker>
      </defs>

      <g
        v-for="(g, i) in props.groups"
        :key="`g${i}`"
        :class="offTag(g.tag) ? 'dgm-off' : ''"
      >
        <rect
          :x="g.x * U"
          :y="g.y * U"
          :width="g.w * U"
          :height="g.h * U"
          rx="8"
          fill="none"
          stroke="currentColor"
          stroke-width="1"
          stroke-dasharray="4 4"
          opacity="0.35"
        />
        <text
          v-if="g.label"
          :x="g.x * U + 8"
          :y="g.y * U - 4"
          class="dgm-sub"
          opacity="0.7"
        >
          {{ gLabel(g) }}
        </text>
      </g>

      <path
        v-for="(p, i) in paths"
        :key="`e${i}`"
        :d="p.d"
        fill="none"
        :class="[
          p.e.accent ? 'dgm-edge-accent' : 'dgm-edge',
          offEdge(p.e) ? 'dgm-off' : '',
        ]"
        :stroke-dasharray="p.e.dash ? '5 4' : undefined"
        :marker-end="p.e.accent ? 'url(#bd-arrow-a)' : 'url(#bd-arrow)'"
      />

      <g
        v-for="n in props.nodes"
        :key="n.id"
        :class="offTag(n.tag) ? 'dgm-off' : ''"
      >
        <rect
          :x="box(N[n.id]).x"
          :y="box(N[n.id]).y"
          :width="box(N[n.id]).w"
          :height="box(N[n.id]).h"
          rx="6"
          :class="n.accent ? 'dgm-box-accent' : 'dgm-box'"
        />
        <text
          v-for="(row, k) in stack(n).rows"
          :key="k"
          :x="stack(n).cx"
          :y="row.y"
          text-anchor="middle"
          :class="row.cls"
          :font-weight="row.weight"
          :font-size="row.size"
        >
          {{ row.t }}
        </text>
      </g>

      <!-- Labels last, over a halo: drawn inside the edge group they were
         painted over by every node the wire passed under. -->
      <g
        v-for="(p, i) in paths"
        :key="`l${i}`"
        :class="offEdge(p.e) ? 'dgm-off' : ''"
      >
        <template v-if="p.e.label && p.lab">
          <rect
            :x="p.lab.r.x"
            :y="p.lab.r.y"
            :width="p.lab.r.w"
            :height="p.lab.r.h"
            rx="3"
            fill="var(--color-bg)"
            opacity="0.94"
          />
          <text
            :x="p.lab.x"
            :y="p.lab.y"
            :text-anchor="p.lab.anchor"
            class="dgm-sub"
          >
            {{ p.e.label }}
          </text>
        </template>
      </g>
    </svg>
  </div>
</template>

<style>
.dgm-wrap {
  position: relative;
}
.dgm-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 8px;
}
.dgm-tag {
  font: inherit;
  font-size: 12px;
  line-height: 1;
  padding: 5px 10px;
  border-radius: 999px;
  border: 1px solid color-mix(in srgb, currentColor 25%, transparent);
  background: color-mix(in srgb, var(--gem-main) 14%, transparent);
  color: inherit;
  cursor: pointer;
}
.dgm-tag-off {
  background: transparent;
  opacity: 0.5;
  text-decoration: line-through;
}
.dgm-tag-all {
  background: transparent;
  font-weight: 600;
}
.dgm-off {
  opacity: 0.1;
  transition: opacity 0.2s;
}
</style>
