# docs-web — authoring contract

Visual companion to `docs/`. Same content, drawn. Vue 3 + rolldown-vite +
`unplugin-vue-router` (file routes) + UnoCSS, mirroring `KohakuHub`'s UI, styled
in `KohakuTerrarium`'s gemstone design language.

```
npm run dev      # http://localhost:5273
npm run build
```

## The design language is not yours to change

`uno.config.js` and `src/style.css` are a faithful copy of KohakuTerrarium's.
**Do not add colours, fonts or a size scale.** Everything you need exists:

- **5 gems × 3 variants** — `sapphire` `aquamarine` `taaffeite` `iolite` `amber`,
  each with `-light` / DEFAULT / `-shadow`.
- **Functional** — `coral` (bad), `sage` (good).
- **Surface** — `warm-50` … `warm-950`. Light mode uses the low end, dark the high.
- **Size tiers** — `kt-text-micro|caption|body|emphasis|title|h2|h1`. Never write
  `text-[13px]` or `text-sm` on chrome.
- **Dark is the default.** `html.dark` is set in `main.js`; every colour must be
  written as a `light dark:` pair, exactly as the shortcuts do.

One gem per domain (`src/utils/colors.js`): framework→sapphire, tpu→amber,
cpu→aquamarine, dsp→taaffeite, gpu→iolite. A page declares its domain **once** on
`<DocPage domain="...">`; everything inside then uses `bg-gem` / `text-gem` /
`--gem-main` and retints automatically. Never hardcode a gem name in a page.

## Pages are file-routed

`src/pages/framework/noc.vue` → `/framework/noc`. The nav comes from
`src/site.js` — if you add a page, add it there too. Components under
`src/components/` are globally auto-imported; **never write an import for them.**

## Page skeleton

```vue
<script setup>
const fetchTrace = { rows: [...], notes: [...] }   // data at the top, always
</script>

<template>
  <DocPage
    title="Mesh and routers"
    summary="One sentence saying what this page answers."
    domain="framework"
    status="shipped"
    source="src/kohakunoc/ · docs/arch/noc/"
  >
    <h2 class="doc-h2">Section</h2>
    <p class="doc-p">Prose stays short. The diagram carries the argument.</p>
    <Fig caption="What the reader should take away." zoom>
      <BlockDiagram :nodes="..." :edges="..." />
    </Fig>
  </DocPage>
</template>
```

## Component API

**`<DocPage title summary domain status source>`** — page frame. `status` ∈
`shipped|measured|building|planned|projected|broken|retired`.

**`<Fig caption zoom pad>`** — figure + caption. `zoom` adds pan/zoom, for
anything wider than ~30 blocks.

**`<Callout kind title>`** — `kind` ∈ `note|rule|trap|measured|open`. Use `trap`
for the "this failed in both directions" material; it is the most valuable thing
in the microarchitecture docs.

**`<BlockDiagram :nodes :edges :groups unit>`** — grid units, not pixels.
```js
nodes: [{ id, x, y, w=10, h=3.2, label, sub, accent }]
edges: [{ from: 'a:b', to: 'c:t', label, accent, dash, dir: 'h'|'v'|'auto' }]
groups: [{ x, y, w, h, label }]   // dashed hierarchy box behind
```
`from`/`to` take `id` or `id:side` with side ∈ `t b l r`.

**`<WaveTrace :rows :notes cycles start variant label>`** — the cycle trace.
**This replaces every ASCII timing table.**
```js
rows: [
  { name: 'fpc',   kind: 'bus',  values: ['0x04','0x08'], mark: [1] },
  { name: 'hold',  kind: 'bit',  values: [0,1,1,0] },
  { name: 'note',  kind: 'text', values: ['', 'stall'] },
]
notes: [{ cycle: 2, text: '0x08 never executes. No error, no trap.', tone: 'bad' }]
variant: 'broken' | 'fixed'      // tints the frame red/green
```
When a doc shows BROKEN then FIXED, emit two `<WaveTrace>` with those variants.

**`<LaneGrid lanes :mask :rows caption>`** — one wave's lanes. `mask` is the
per-lane active bit; masked lanes render struck-through, not blank.

**`<StepPlayer :steps label>`** — scrubber. Slot gets `{ state, i, n }`. Each
step may carry `title` and `note`. Use for IPDOM traces, coalescer passes,
descriptor walks — anything the docs show as a sequence of tables.

**`<SpecTable :cols :rows caption>`** — `cols: [{key,label,mono,align}]`,
row values are HTML. `_tone` on a row ∈ `good|bad|warn`.

**`<BitField :fields caption>`** — `fields: [{name,bits,value,accent}]`, MSB
first. For flit format, instruction encoding, control registers.

**`<ResourceBars :items unit max caption>`** — `items: [{label,value,max,note,tone}]`.

**`<StateMachine :states :edges active>`** — `states:[{id,x,y,label,sub,accent}]`,
`edges:[{from,to,label,curve,self}]`. Drive `active` from a `StepPlayer`.

## Rules that matter

1. **Every number carries its origin.** A LUT count, an Fmax, a utilisation
   figure describes one accelerator on one part — say which, in `source` or in
   the caption. Mark projections `PROJECTED` and estimates `ESTIMATE`.
2. **Read the source doc in full before drawing it.** Not the headings — the
   whole file. Half these pages contain a trap whose value is the detail.
3. **`docs/arch/pe/simt/microarchitecture.md` is the model** for a micro page:
   one mechanism, one diagram, and the failure shown as a concrete trace beside
   the working one. Copy that shape everywhere.
4. **Never mention `.plan/`** in any file here. It is internal. Cite the public
   doc instead.
5. Prose is the caption to the picture, not the other way round. If a paragraph
   restates the diagram, delete the paragraph.
6. No em-dash-free rewriting of the source's meaning: where a doc states a
   constraint ("MUST NOT accept an instruction it cannot retire in bounded
   time"), quote it rather than paraphrasing it into something softer.
7. Run `npm run build` before you report done. Zero UnoCSS "unmatched utility"
   warnings, zero Vue warnings.

## Checking your work

When several agents work at once, `dist/` and port 4173 are shared and you WILL
clobber each other. Build and serve your own, using your agent name and your own
port:

```
npx vite build   --outDir .verify/NAME
npx vite preview --outDir .verify/NAME --port PORT --strictPort

BASE=http://localhost:PORT node scripts/overlap.mjs   # must be 0 on your route
BASE=http://localhost:PORT node scripts/shots.mjs     # -> .review/*.png
BASE=http://localhost:PORT node scripts/peek.mjs /your/route --y 2600
```

`vite preview` pins the outDir it started with, so **rebuild means restart the
server** — otherwise every check silently measures the previous bundle and reads
as "my fixes did nothing". That has already cost an hour once.

`shots.mjs` wipes `.review/` on start, so do not interleave it with `peek.mjs`,
and expect another agent's run to delete your PNGs — read yours promptly.

The audit only catches geometry. It cannot tell you whether a trace reads
clearly or whether a broken/fixed pair lands as a contrast — open the PNGs.
