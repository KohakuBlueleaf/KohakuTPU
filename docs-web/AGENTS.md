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
`docs-web/src/components/` are globally auto-imported; **never write an import
for them.**

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
    source="src/kohakuaccel/noc/ · docs/arch/noc/"
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

### Diagram layout rules — these are hard

1. **A horizontal flow means VERTICAL components.** Stages left to right, each
   box tall and narrow. A row of wide flat bars reads as a stack lying on its
   side, and a column of them is taller than a screen.
2. **No connection overlaps another**, and "no overlap" includes *touching*:
   two wires that meet at a turn point read as one wire. The router keeps a
   minimum lane pitch between them.
3. **No connection crosses a component.** The router is an orthogonal A* around
   the boxes; it does not need help, but it does need somewhere to go — leave
   real gaps between columns.
4. **Every arrow meets a box normal to that side** — horizontal into left and
   right, vertical into top and bottom. Never oblique, never along an edge.
5. **Leave gaps wider than the edge labels.** A label longer than the space
   between two columns lands on a box. Shorten the label or widen the gap;
   40 px of gap will not hold `cp_* — a requester`.
6. **No wire crosses another.** A crossing is a layout fault, not a router
   limit: order the rows so every wire goes toward its consumer without
   cutting the other direction's run (port → response tap → request tap →
   home), keep a fan-out to two representative wires, and list first the wire
   that must take the lane nearest its box. Jogs are fine. The one crossing you
   keep is one you can prove no row order removes; the router then draws it as
   a jump (the horizontal arcs over the vertical) so it never reads as a turn.

`BlockDiagram` enforces 2–4 itself: it assigns each edge its own slot on a
side, routes around obstacles, and places labels last so they avoid each other
and the boxes. **1, 5 and 6 are yours.** Run `overlap.mjs` — it catches 1–5,
including a `wide` that is not actually wide — and `crossings.mjs`, which
prints every crossing with its two wires named (the router also `console.warn`s
them in dev).

**`<BlockDiagram :nodes :edges :groups :tags unit>`** — grid units, not pixels.
```js
nodes: [{ id, x, y, w=10, h=3.2, label, sub, accent, tag }]
edges: [{ from: 'a:b', to: 'c:t', label, accent, dash, dir: 'h'|'v'|'auto' }]
groups: [{ x, y, w, h, label, tag }]   // dashed hierarchy box behind
tags:   [{ key, label }]               // optional: one chip per key
```
`from`/`to` take `id` or `id:side` with side ∈ `t b l r`. With `tags`, a chip
bar sits above the drawing: click dims everything carrying that `tag` (and
every wire touching a dimmed box), double-click solos it. Dimming never
re-routes, so the layout — and the crossing count — stays what you audited.

**`<Fig zoom wide|sheet caption>`** — every `zoom` figure has a full-screen
button: drag to pan, wheel to zoom, `0` fit, `1` actual size, `±`, `Esc`.
`sheet` is `wide` for a drawing meant to be read in that viewer (the
/machine sheet); it is exempt from the tall-wide audit and nothing else.

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

## The bar, and the page that sets it

**`src/pages/framework/noc.vue` is the model for every page in this tree.**
Read it end to end before writing anything else. The standard it sets:

> A reader who has **never implemented one of these** but knows basic RTL/HDL
> should be able to design their own from the page.

That is the test. Not "is it accurate", not "does it cover the topic" — could
someone build the thing after reading it. A page that describes a component
without giving away how it works has failed, however correct it is.

### The structure that gets there

Follow this order. It is what `noc.vue` does, and it works because each step
answers the question the previous one raises.

1. **What it owns** — a short section, four things at most, in cards. What this
   component *is*, bounded. Then one paragraph on why it exists at all, phrased
   as the alternative that was rejected and why.
2. **What it costs** — the mechanism as a `BlockDiagram`, immediately. Not a
   block diagram of boxes-and-names: the actual internal structure, with the
   registers and queues that set the cost drawn. Then the knobs table: which
   parameters move that cost, in the order they matter.
3. **The protocol, as waveforms.** Every handshake rule gets a `<Callout
   kind="rule">` stating the MUST, then `<WaveTrace variant="broken">` showing
   what the violation does, then `variant="fixed"`. **Two broken traces when
   there are two ways to get it wrong.** A rule nobody can violate on the page
   is a rule nobody will remember.
4. **The bit-exact layout** — `<BitField>` for geometry, then again expanded,
   then a `<SpecTable>` whose columns are **field · width · position · owner**.
   The owner column is not optional; it is what tells a reader which bits are
   theirs.
5. **The type/message table** — every code, who may send it, who consumes it.
   Include the unallocated and the declared-but-unimplemented rows, and say
   which is which.
6. **The reasoning that is a proof, not a test result** — where a property holds
   by construction (deadlock freedom, in-order delivery), give the argument in
   full so a reader can check it rather than trust it.
7. **How you deploy it** — the real thing: sizing, a worked capacity formula, a
   numbered procedure, and the open questions the flow does not answer.
8. **Conventions**, then **fixed protocol / addon / convention / yours**, then
   **what this does not own** and who does.

### Where the detail actually lives

- **Traps get their own `<Callout kind="trap">`**, titled with the *claim*, not
  the topic — "CU_DATA is 0x8, not 0x4", "The holding slot is one per input
  port, not one per output direction". A reader scanning titles learns the
  design.
- **Every trap says what the symptom looks like**, because that is what makes it
  findable: "presents as a hang several modules away", "a silent wrong-bytes
  store", "the symptom appears as a short burst or a wrong tile".
- **Name the trade both ways.** `noc.vue` explains why the holding slot exists
  *and* why it is kept rather than removed. One direction is an assertion; both
  is a design.
- **No "where today's source disagrees" section.** Verify each such claim
  against the RTL, then **fold it into the section that owns it** — a type-code
  divergence belongs in the type-code trap, a reset-polarity split belongs with
  the generator that has to supply both. A page that quarantines its own caveats
  invites the reader to skip them.

## Rules that matter

1. **Every number carries its origin.** A LUT count, an Fmax, a utilisation
   figure describes one accelerator on one part — say which, in `source` or in
   the caption. Mark projections `PROJECTED` and estimates `ESTIMATE`.
2. **Read the source doc in full before drawing it.** Not the headings — the
   whole file. Half these pages contain a trap whose value is the detail.
3. **`docs/arch/cpu/rv32-pe/microarchitecture.md` is the model** for a micro
   page: every choice argued against the alternative that lost, costs named, and
   the roads not taken given their own sections. It carries no Verilog at all —
   prose, tables and diagrams do the work, because the reader may not read HDL.
   `docs/projects/kohakumpe/simt/microarchitecture.md` is the model for showing
   a failure as a concrete trace beside the working one.
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
BASE=http://localhost:PORT node scripts/crossings.mjs /your/route   # must be 0 too
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
