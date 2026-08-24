// Viewport-only shot of one route, for quick layout checks.
//   node scripts/peek.mjs /tpu/numbers [--w 1440] [--h 1100] [--y 0] [--light]
import { chromium } from "playwright"

const a = process.argv.slice(2)
const route = a[0] || "/"
const num = (f, d) => (a.includes(f) ? Number(a[a.indexOf(f) + 1]) : d)
const width = num("--w", 1440)
const height = num("--h", 1100)
const y = num("--y", 0)
const base = process.env.BASE || "http://localhost:4173"

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width, height }, deviceScaleFactor: 1 })
await page.goto(`${base}/#${route}`, { waitUntil: "networkidle" })
if (a.includes("--light"))
  await page.evaluate(() => document.documentElement.classList.remove("dark"))
if (y) await page.evaluate((n) => document.querySelector("main")?.scrollTo({ top: n }), y)
await page.waitForTimeout(400)

const name = (route === "/" ? "index" : route.slice(1).replace(/\//g, "-")) + "-peek"
await page.screenshot({ path: `.review/${name}.png` })
console.log(`.review/${name}.png  ${width}x${height} @y${y}`)
await browser.close()
