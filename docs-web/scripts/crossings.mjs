// Report every block diagram whose wires cross: a crossing is a layout fault
// unless proved unavoidable, and the jump it is drawn as does not excuse it.
//   node scripts/crossings.mjs            every route
//   node scripts/crossings.mjs /component/xache
import { chromium } from "playwright";
import { discoverRoutes } from "./routes.mjs";

const base = process.env.BASE || "http://localhost:4173";
const only = process.argv[2];
const routes = only ? [only] : await discoverRoutes();

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 1200 } });

let total = 0;
page.on("console", (m) => {
  if (m.type() === "warning" && m.text().startsWith("BlockDiagram:"))
    console.log(`  ${m.text().replace(/\n/g, "\n  ")}`);
});
for (const r of routes) {
  await page.goto(`${base}/#${r}`, { waitUntil: "networkidle" });
  // 300 ms missed the busy pages: totals swung 25-53 between identical runs.
  await page.waitForTimeout(1200);
  const rows = await page.evaluate(() =>
    [...document.querySelectorAll("svg.dgm")].map((s, i) => ({
      i,
      n: Number(s.dataset.crossings || 0),
      cap: (s.closest("figure")?.querySelector("figcaption")?.textContent || "")
        .trim()
        .slice(0, 50),
    })),
  );
  for (const x of rows) {
    if (!x.n) continue;
    total += x.n;
    console.log(`${r}  diagram ${x.i}: ${x.n} crossing(s)  ${x.cap}`);
  }
}
console.log(
  total ? `${total} crossing(s), each drawn as a jump` : "no crossings",
);
await browser.close();
