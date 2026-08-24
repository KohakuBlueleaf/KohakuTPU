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
})

const U = props.unit
const N = computed(() =>
  Object.fromEntries(
    props.nodes.map((n) => [
      n.id,
      { ...n, w: n.w ?? 10, h: n.h ?? 3.2 },
    ]),
  ),
)

const box = (n) => ({ x: n.x * U, y: n.y * U, w: n.w * U, h: n.h * U })

function anchor(ref) {
  const [id, side] = String(ref).split(":")
  const n = N.value[id]
  if (!n) return { x: 0, y: 0 }
  const b = box(n)
  switch (side) {
    case "t": return { x: b.x + b.w / 2, y: b.y }
    case "b": return { x: b.x + b.w / 2, y: b.y + b.h }
    case "l": return { x: b.x, y: b.y + b.h / 2 }
    case "r": return { x: b.x + b.w, y: b.y + b.h / 2 }
    default: return { x: b.x + b.w / 2, y: b.y + b.h / 2 }
  }
}

function path(e) {
  const a = anchor(e.from)
  const b = anchor(e.to)
  const dir = e.dir ?? "auto"
  if (dir === "v" || (dir === "auto" && Math.abs(b.y - a.y) > Math.abs(b.x - a.x))) {
    const my = (a.y + b.y) / 2
    return `M${a.x},${a.y} V${my} H${b.x} V${b.y}`
  }
  const mx = (a.x + b.x) / 2
  return `M${a.x},${a.y} H${mx} V${b.y} H${b.x}`
}

function mid(e) {
  const a = anchor(e.from)
  const b = anchor(e.to)
  return { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 }
}

// Long subs are the common case in these docs and shrinking them to fit is
// how you get 7px text that still overflows. Wrap instead, then shrink only
// what is left. Mono glyphs run ~0.6em wide.
function wrap(text, boxW, size, maxLines) {
  const s = String(text ?? "").trim()
  if (!s) return []
  const per = Math.max(6, Math.floor((boxW - 10) / (size * 0.6)))
  const lines = []
  let cur = ""
  for (const w of s.split(/\s+/)) {
    if (!cur) cur = w
    else if ((cur + " " + w).length <= per) cur += " " + w
    else {
      lines.push(cur)
      cur = w
    }
  }
  if (cur) lines.push(cur)
  if (lines.length <= maxLines) return lines
  return [...lines.slice(0, maxLines - 1), lines.slice(maxLines - 1).join(" ")]
}

function fit(text, boxW, base) {
  const w = String(text ?? "").length * base * 0.6
  const avail = boxW - 10
  return w <= avail ? base : Math.max(7.5, (avail / w) * base)
}

/** Vertically centred stack of label lines then sub lines. */
function stack(n) {
  const b = box(N.value[n.id])
  const lab = wrap(n.label, b.w, 11, 2)
  const sub = wrap(n.sub, b.w, 9.5, 2)
  const H = lab.length * 12 + sub.length * 11
  let y = b.y + b.h / 2 - H / 2 + 9
  const rows = []
  for (const t of lab) {
    rows.push({ t, y, cls: "dgm-label", weight: 600, size: fit(t, b.w, 11) })
    y += 12
  }
  for (const t of sub) {
    rows.push({ t, y, cls: "dgm-sub", weight: 400, size: fit(t, b.w, 9.5) })
    y += 11
  }
  return { cx: b.x + b.w / 2, rows }
}

const vb = computed(() => {
  const all = [
    ...props.nodes.map((n) => box({ ...n, w: n.w ?? 10, h: n.h ?? 3.2 })),
    ...props.groups.map((g) => ({ x: g.x * U, y: g.y * U, w: g.w * U, h: g.h * U })),
  ]
  const x0 = Math.min(...all.map((b) => b.x)) - props.pad
  const y0 = Math.min(...all.map((b) => b.y)) - props.pad - 6
  const x1 = Math.max(...all.map((b) => b.x + b.w)) + props.pad
  const y1 = Math.max(...all.map((b) => b.y + b.h)) + props.pad
  return `${x0} ${y0} ${x1 - x0} ${y1 - y0}`
})
</script>

<template>
  <svg :viewBox="vb" class="dgm" role="img">
    <defs>
      <marker id="bd-arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6"
              markerHeight="6" orient="auto-start-reverse">
        <path d="M0,0 L8,4 L0,8 z" fill="currentColor" />
      </marker>
      <marker id="bd-arrow-a" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6"
              markerHeight="6" orient="auto-start-reverse">
        <path d="M0,0 L8,4 L0,8 z" fill="var(--gem-main)" />
      </marker>
    </defs>

    <g v-for="(g, i) in props.groups" :key="`g${i}`">
      <rect
        :x="g.x * U" :y="g.y * U" :width="g.w * U" :height="g.h * U"
        rx="8" fill="none" stroke="currentColor" stroke-width="1"
        stroke-dasharray="4 4" opacity="0.35"
      />
      <text
        v-if="g.label" :x="g.x * U + 8" :y="g.y * U - 4"
        class="dgm-sub" opacity="0.7"
      >{{ g.label }}</text>
    </g>

    <path
      v-for="(e, i) in props.edges"
      :key="`e${i}`"
      :d="path(e)"
      :class="e.accent ? 'dgm-edge-accent' : 'dgm-edge'"
      :stroke-dasharray="e.dash ? '5 4' : undefined"
      :marker-end="e.accent ? 'url(#bd-arrow-a)' : 'url(#bd-arrow)'"
    />

    <g v-for="n in props.nodes" :key="n.id">
      <rect
        :x="box(N[n.id]).x" :y="box(N[n.id]).y"
        :width="box(N[n.id]).w" :height="box(N[n.id]).h"
        rx="6" :class="n.accent ? 'dgm-box-accent' : 'dgm-box'"
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
      >{{ row.t }}</text>
    </g>

    <!-- Labels last, over a halo: drawn inside the edge group they were
         painted over by every node the wire passed under. -->
    <g v-for="(e, i) in props.edges" :key="`l${i}`">
      <template v-if="e.label">
        <rect
          :x="mid(e).x - String(e.label).length * 2.7 - 3"
          :y="mid(e).y - 13"
          :width="String(e.label).length * 5.4 + 6"
          height="12"
          rx="3"
          fill="var(--color-bg)"
          opacity="0.92"
        />
        <text :x="mid(e).x" :y="mid(e).y - 4" text-anchor="middle" class="dgm-sub">
          {{ e.label }}
        </text>
      </template>
    </g>
  </svg>
</template>
