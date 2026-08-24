// Print generated rules that a CSS parser will reject.
//   node scripts/unodebug.mjs src/pages/mpe/simd/microarchitecture.vue
import { readFile } from "node:fs/promises"
import { createGenerator } from "unocss"
import postcss from "postcss"
import config from "../uno.config.js"

const file = process.argv[2]
const src = await readFile(file, "utf8")
const uno = await createGenerator(config)
const { css } = await uno.generate(src, { preflights: false })

const rules = css.match(/[^{}]+\{[^{}]*\}/g) || []
let bad = 0
for (const r of rules) {
  try {
    postcss.parse(r)
  } catch (e) {
    bad++
    console.log("REJECTED:", r.trim().slice(0, 160))
    console.log("   ", e.message.split("\n")[0])
  }
}
console.log(`\n${rules.length} rules, ${bad} rejected`)
