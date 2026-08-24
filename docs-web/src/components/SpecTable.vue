<script setup>
/** cols: [{ key, label, mono?, align? }] · rows: [{ ...keys, _tone? }] */
const props = defineProps({
  cols: { type: Array, required: true },
  rows: { type: Array, required: true },
  caption: { type: String, default: "" },
})
const TONE = {
  good: "text-sage-shadow dark:text-sage-light",
  bad: "text-coral-shadow dark:text-coral-light",
  warn: "text-amber-shadow dark:text-amber-light",
}
</script>

<template>
  <div class="my-5 doc-w card overflow-hidden">
    <div class="overflow-x-auto">
      <table class="doc-table">
        <thead>
          <tr class="bg-warm-50 dark:bg-warm-900">
            <th
              v-for="c in props.cols"
              :key="c.key"
              class="doc-th"
              :style="c.align ? { textAlign: c.align } : null"
            >
              {{ c.label }}
            </th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="(r, i) in props.rows"
            :key="i"
            class="hover:bg-warm-50 dark:hover:bg-warm-900/50 transition-colors"
          >
            <td
              v-for="c in props.cols"
              :key="c.key"
              class="doc-td"
              :class="[
                c.mono ? 'font-mono' : '',
                r._tone ? TONE[r._tone] : 'text-warm-700 dark:text-warm-300',
              ]"
              :style="c.align ? { textAlign: c.align } : null"
              v-html="r[c.key]"
            />
          </tr>
        </tbody>
      </table>
    </div>
    <p v-if="props.caption" class="doc-cap">{{ props.caption }}</p>
  </div>
</template>
