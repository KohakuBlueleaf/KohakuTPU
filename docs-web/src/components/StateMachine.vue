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

function arc(e) {
  const a = P.value[e.from];
  const b = P.value[e.to];
  if (!a || !b) return "";
  if (e.self) {
    return `M${a.x - 10},${a.y - props.r} A22,22 0 1,1 ${a.x + 10},${a.y - props.r}`;
  }
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  const len = Math.hypot(dx, dy) || 1;
  const ux = dx / len;
  const uy = dy / len;
  const ax = a.x + ux * props.r;
  const ay = a.y + uy * props.r;
  const bx = b.x - ux * props.r;
  const by = b.y - uy * props.r;
  const c = e.curve ?? 0;
  const mx = (ax + bx) / 2 - uy * c;
  const my = (ay + by) / 2 + ux * c;
  return `M${ax},${ay} Q${mx},${my} ${bx},${by}`;
}

function lab(e) {
  const a = P.value[e.from];
  const b = P.value[e.to];
  if (!a || !b) return { x: 0, y: 0 };
  if (e.self) return { x: a.x, y: a.y - props.r - 26 };
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  const len = Math.hypot(dx, dy) || 1;
  const c = e.curve ?? 0;
  return {
    x: (a.x + b.x) / 2 - (dy / len) * (c * 0.6 + 9),
    y: (a.y + b.y) / 2 + (dx / len) * (c * 0.6 + 9),
  };
}

const vb = computed(() => {
  const xs = props.states.map((s) => s.x * U);
  const ys = props.states.map((s) => s.y * U);
  const m = props.r + 40;
  return `${Math.min(...xs) - m} ${Math.min(...ys) - m} ${
    Math.max(...xs) - Math.min(...xs) + m * 2
  } ${Math.max(...ys) - Math.min(...ys) + m * 2}`;
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
        text-anchor="middle"
        class="dgm-sub"
      >
        {{ e.label }}
      </text>
    </g>

    <g v-for="s in props.states" :key="s.id">
      <circle
        :cx="P[s.id].x"
        :cy="P[s.id].y"
        :r="props.r"
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
