// Find diagram text that collides with other text or overflows its own box.
//   node scripts/overlap.mjs
import { chromium } from "playwright"
import { discoverRoutes } from "./routes.mjs"

const base = process.env.BASE || "http://localhost:4173"
const routes = await discoverRoutes()

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1440, height: 1200 } })

const audit = () => {
  const out = []
  for (const [si, svg] of [...document.querySelectorAll("svg.dgm")].entries()) {
    const texts = [...svg.querySelectorAll("text")].map((t) => ({
      el: t,
      s: (t.textContent || "").trim(),
      b: t.getBBox(),
    }))
    const boxes = [...svg.querySelectorAll("rect")].map((r) => r.getBBox())
    const hit = (a, b) =>
      a.x < b.x + b.width && b.x < a.x + a.width && a.y < b.y + b.height && b.y < a.y + a.height

    // A label stacked over its own sub shares an x-centre and their glyph
    // boxes touch by design; that is not a collision.
    const stacked = (a, b) =>
      Math.abs(a.b.x + a.b.width / 2 - (b.b.x + b.b.width / 2)) < 3 &&
      Math.abs(a.b.y - b.b.y) < 18

    for (let i = 0; i < texts.length; i++) {
      for (let j = i + 1; j < texts.length; j++) {
        if (!texts[i].s || !texts[j].s) continue
        if (stacked(texts[i], texts[j])) continue
        if (hit(texts[i].b, texts[j].b))
          out.push({ svg: si, kind: "text-text", a: texts[i].s, b: texts[j].s })
      }
    }
    for (const t of texts) {
      if (!t.s) continue
      const cx = t.b.x + t.b.width / 2
      const cy = t.b.y + t.b.height / 2
      // Smallest containing rect, or an outer frame is mistaken for the box.
      const host = boxes
        .filter((r) => cx >= r.x && cx <= r.x + r.width && cy >= r.y && cy <= r.y + r.height)
        .sort((p, q) => p.width * p.height - q.width * q.height)[0]
      const over = host ? Math.round(t.b.width - host.width) : 0
      if (host && over > 1) out.push({ svg: si, kind: "overflow", a: t.s, by: over })
    }
  }

  // Breaking out of the column is for short-and-wide only; a tall one gains
  // nothing from the width and makes the reader scan both ways.
  for (const f of document.querySelectorAll("figure.doc-break")) {
    const r = f.getBoundingClientRect()
    // 0.5: a breakout must be at most half as tall as it is wide. The two the
    // owner rejected measured 0.75 and 0.54, so anything looser misses them.
    if (r.height > r.width * 0.5)
      out.push({
        kind: "tall-wide",
        a: (f.querySelector("figcaption")?.textContent || "").trim().slice(0, 52),
        w: Math.round(r.width),
        h: Math.round(r.height),
        ratio: (r.height / r.width).toFixed(2),
      })
  }
  return out
}

let total = 0
for (const r of routes) {
  await page.goto(`${base}/#${r}`, { waitUntil: "networkidle" })
  await page.waitForTimeout(250)

  // A route in site.js whose page file does not exist renders an empty pane
  // and reports nothing anywhere. That shipped once as a dead nav link.
  const body = await page.evaluate(() => {
    const m = document.querySelector("main")
    // DocPage renders <article>; the landing page renders .container-page.
    return { ok: !!m?.querySelector("article, .container-page"), h: m?.scrollHeight ?? 0 }
  })
  if (!body.ok) {
    console.log(`\n${r}  — EMPTY ROUTE (pane ${body.h}px, no content root). Page file missing?`)
    total++
    continue
  }

  const found = await page.evaluate(audit)
  total += found.length
  if (found.length) {
    console.log(`\n${r}  — ${found.length} issue(s)`)
    for (const f of found.slice(0, 12)) {
      console.log(
        f.kind === "overflow"
          ? `   overflow  svg#${f.svg}  "${f.a}"  +${f.by}px past its box`
          : f.kind === "tall-wide"
            ? `   tall-wide  ${f.w}x${f.h} r=${f.ratio}  drop \`wide\`  "${f.a}…"`
            : `   collide   svg#${f.svg}  "${f.a}"  ×  "${f.b}"`,
      )
    }
    if (found.length > 12) console.log(`   ...and ${found.length - 12} more`)
  }
}
console.log(`\ntotal ${total}`)
await browser.close()
