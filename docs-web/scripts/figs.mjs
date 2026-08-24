// Measure every figure and report any that leaves the content pane.
//   node scripts/figs.mjs /tpu/vector/microarchitecture
import { chromium } from "playwright"

const route = process.argv[2] || "/"
const width = Number(process.argv[3]) || 1440
const base = process.env.BASE || "http://localhost:4190"

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width, height: 1000 } })
await page.goto(`${base}/#${route}`, { waitUntil: "networkidle" })
await page.waitForTimeout(400)

const rows = await page.evaluate(() => {
  const pane = document.querySelector("main").getBoundingClientRect()
  return [...document.querySelectorAll("figure")].map((f) => {
    const r = f.getBoundingClientRect()
    return {
      wide: f.classList.contains("doc-break"),
      w: Math.round(r.width),
      h: Math.round(r.height),
      overL: Math.round(pane.left - r.left),
      overR: Math.round(r.right - pane.right),
      cap: (f.querySelector("figcaption")?.textContent || "").trim().slice(0, 46),
    }
  })
})

console.log(`pane @${width}px`)
for (const r of rows) {
  const spill = r.overL > 1 || r.overR > 1 ? `  SPILL L${r.overL} R${r.overR}` : ""
  const shape = r.h > r.w ? "TALL" : "wide"
  console.log(
    `${r.wide ? "break" : "  col"}  ${String(r.w).padStart(5)}x${String(r.h).padEnd(5)} ${shape}${spill}  ${r.cap}`,
  )
}
await browser.close()
