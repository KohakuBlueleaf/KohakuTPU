// `vue-router/auto-routes` is a Vite virtual module and these scripts run in
// plain Node, so derive the same paths from the page files.
import { readdir } from "node:fs/promises"
import { fileURLToPath } from "node:url"
import path from "node:path"

const PAGES = fileURLToPath(new URL("../src/pages/", import.meta.url))

async function files(dir, rel = "") {
  const out = []
  for (const e of await readdir(dir, { withFileTypes: true })) {
    const r = rel ? `${rel}/${e.name}` : e.name
    if (e.isDirectory()) {
      if (e.name === "components") continue
      out.push(...(await files(path.join(dir, e.name), r)))
    } else if (e.name.endsWith(".vue") && !e.name.startsWith("_")) {
      out.push(r)
    }
  }
  return out
}

export async function discoverRoutes() {
  const rel = await files(PAGES)
  const paths = rel.map((f) => {
    const p = "/" + f.replace(/\.vue$/, "").replace(/\/?index$/, "")
    return p === "" ? "/" : p
  })
  return [...new Set(paths)].sort()
}
