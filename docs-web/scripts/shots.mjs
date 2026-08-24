// Render every route to .review/ for visual inspection.
//   node scripts/shots.mjs [--light] [--w 1440]
import { chromium } from "playwright"
import { mkdir, rm } from "node:fs/promises"
import { discoverRoutes } from "./routes.mjs"

const args = process.argv.slice(2)
const light = args.includes("--light")
const width = Number(args[args.indexOf("--w") + 1]) || 1440
const base = process.env.BASE || "http://localhost:4173"
const out = new URL("../.review/", import.meta.url)

await rm(out, { recursive: true, force: true })
await mkdir(out, { recursive: true })

const routes = await discoverRoutes()
const browser = await chromium.launch()
const page = await browser.newPage({
  viewport: { width, height: 1000 },
  deviceScaleFactor: 2,
})

const problems = []
page.on("console", (m) => {
  if (m.type() === "error" || m.type() === "warning") problems.push(`${m.type()}: ${m.text()}`)
})
page.on("pageerror", (e) => problems.push(`pageerror: ${e.message}`))

for (const r of routes) {
  const before = problems.length
  await page.goto(`${base}/#${r}`, { waitUntil: "networkidle" })
  if (light) await page.evaluate(() => document.documentElement.classList.remove("dark"))
  await page.waitForTimeout(350)

  // Full page height lives on the inner scroller, not the window.
  const h = await page.evaluate(() => {
    const m = document.querySelector("main")
    return m ? Math.min(m.scrollHeight + 60, 20000) : 1000
  })
  await page.setViewportSize({ width, height: h })
  await page.waitForTimeout(200)

  const name = (r === "/" ? "index" : r.slice(1).replace(/\//g, "-")) + (light ? "-light" : "")
  await page.screenshot({ path: new URL(`${name}.png`, out).pathname.slice(1) })

  const mine = problems.slice(before)
  console.log(`${mine.length ? "!" : " "} ${name}  ${h}px${mine.length ? "  " + mine[0] : ""}`)
  await page.setViewportSize({ width, height: 1000 })
}

await browser.close()
if (problems.length) {
  console.log(`\n${problems.length} console problem(s):`)
  for (const p of [...new Set(problems)].slice(0, 25)) console.log("  " + p)
}
