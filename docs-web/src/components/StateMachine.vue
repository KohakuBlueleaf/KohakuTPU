<script setup>
/**
 * states: [{ id, x, y, label, sub?, accent? }]  grid units
 * edges:  [{ from, to, label?, curve?: number, self?: boolean }]
 * active: id highlighted (drive it from a StepPlayer)
 */
const props = defineProps({
  states: { type: Array, required: true },
  edges: { type: Array, default: () => [] },
  active: { type: String, default: "" },
  unit: { type: Number, default: 22 },
  r: { type: Number, default: 26 },
});

const U = props.unit;
const P = computed(() =>
  Object.fromEntries(
    props.states.map((s) => [s.id, { x: s.x * U, y: s.y * U }]),
  ),
);
// A circle grows to hold its text: 5.5 px per sub glyph, 6.5 per label glyph
// (measured), so a sub wider than the chord is not clipped at the rim.
const rOf = (s) =>
  Math.max(
    props.r,
    Math.ceil((String(s.sub ?? "").length * 5.5) / 2) + 6,
    Math.ceil((String(s.label ?? "").length * 6.5) / 2) + 6,
  );
const R = computed(() =>
  Object.fromEntries(props.states.map((s) => [s.id, rOf(s)])),
);

function arc(e) {
  const a = P.value[e.from];
  const b = P.value[e.to];
  if (!a || !b) return "";
  const ra = R.value[e.from];
  const rb = R.value[e.to];
  if (e.self) {
    return `M${a.x - 10},${a.y - ra} A22,22 0 1,1 ${a.x + 10},${a.y - ra}`;
  }
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  const len = Math.hypot(dx, dy) || 1;
  const ux = dx / len;
  const uy = dy / len;
  const ax = a.x + ux * ra;
  const ay = a.y + uy * ra;
  const bx = b.x - ux * rb;
  const by = b.y - uy * rb;
  const c = e.curve ?? 0;
  const mx = (ax + bx) / 2 - uy * c;
  const my = (ay + by) / 2 + ux * c;
  return `M${ax},${ay} Q${mx},${my} ${bx},${by}`;
}

function lab(e) {
  const a = P.value[e.from];
  const b = P.value[e.to];
  if (!a || !b) return { x: 0, y: 0, anchor: "middle" };
  // The self loop's top is ~42 px above the rim; the label used to sit at 26.
  if (e.self)
    return { x: a.x, y: a.y - R.value[e.from] - 50, anchor: "middle" };
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  const len = Math.hypot(dx, dy) || 1;
  const nx = -dy / len;
  const ny = dx / len;
  const c = e.curve ?? 0;
  // Outside the arc on EITHER side: the arc peaks at c/2, so the label sits
  // 0.1·|c| + 9 beyond it (adding 9 regardless of sign put a negative curve's
  // label inside its own arc). A straight edge's label goes 14 px to the
  // OTHER side: +9 is the row the neighbouring circles' subs are written on.
  let off = c === 0 ? -14 : c * 0.6 + Math.sign(c) * 9;
  // A steep edge: text centred 14 px beside the line still straddles it, so
  // the label starts (or ends) 8 px clear of the line instead.
  const steep = Math.abs(nx) > 0.5;
  let anchor = "middle";
  if (steep) {
    if (c === 0) off = -8;
    anchor = nx * off > 0 ? "start" : "end";
  }
  return {
    x: (a.x + b.x) / 2 + nx * off,
    y: (a.y + b.y) / 2 + ny * off + (steep ? 3 : 0),
    anchor,
  };
}

/* The box holds every circle, every arc's peak and every label — the states
 * alone used to set it, so a tall return arc was clipped at the top. */
const vb = computed(() => {
  const xs = [];
  const ys = [];
  for (const s of props.states) {
    const p = P.value[s.id];
    const r = R.value[s.id];
    xs.push(p.x - r, p.x + r);
    ys.push(p.y - r, p.y + r);
  }
  for (const e of props.edges) {
    const a = P.value[e.from];
    const b = P.value[e.to];
    if (!a || !b) continue;
    const l = lab(e);
    const w = String(e.label ?? "").length * 5.5;
    xs.push(l.x - w / 2, l.x + w / 2);
    ys.push(l.y - 10, l.y + 4);
    if (e.self) {
      ys.push(a.y - R.value[e.from] - 46);
    } else {
      const dx = b.x - a.x;
      const dy = b.y - a.y;
      const len = Math.hypot(dx, dy) || 1;
      const c = (e.curve ?? 0) / 2;
      xs.push((a.x + b.x) / 2 - (dy / len) * c);
      ys.push((a.y + b.y) / 2 + (dx / len) * c);
    }
  }
  const m = 20;
  const x0 = Math.min(...xs) - m;
  const y0 = Math.min(...ys) - m;
  return `${x0} ${y0} ${Math.max(...xs) + m - x0} ${Math.max(...ys) + m - y0}`;
});
</script>

<template>
  <svg :viewBox="vb" class="dgm" role="img">
    <defs>
      <marker
        id="sm-arrow"
        viewBox="0 0 8 8"
        refX="7"
        refY="4"
        markerWidth="6"
        markerHeight="6"
        orient="auto-start-reverse"
      >
        <path d="M0,0 L8,4 L0,8 z" fill="currentColor" />
      </marker>
    </defs>

    <g v-for="(e, i) in props.edges" :key="i">
      <path :d="arc(e)" class="dgm-edge" marker-end="url(#sm-arrow)" />
      <text
        v-if="e.label"
        :x="lab(e).x"
        :y="lab(e).y"
        :text-anchor="lab(e).anchor"
        class="dgm-sub"
      >
        {{ e.label }}
      </text>
    </g>

    <g v-for="s in props.states" :key="s.id">
      <circle
        :cx="P[s.id].x"
        :cy="P[s.id].y"
        :r="R[s.id]"
        :class="
          s.accent || props.active === s.id ? 'dgm-box-accent' : 'dgm-box'
        "
      />
      <text
        :x="P[s.id].x"
        :y="P[s.id].y + (s.sub ? -1 : 4)"
        text-anchor="middle"
        class="dgm-label"
        font-weight="600"
      >
        {{ s.label }}
      </text>
      <text
        v-if="s.sub"
        :x="P[s.id].x"
        :y="P[s.id].y + 10"
        text-anchor="middle"
        class="dgm-sub"
      >
        {{ s.sub }}
      </text>
    </g>
  </svg>
</template>
