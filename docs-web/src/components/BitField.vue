<script setup>
/** fields: [{ name, bits, value?, accent? }] — listed MSB first. */
const props = defineProps({
  fields: { type: Array, required: true },
  caption: { type: String, default: "" },
})

const total = computed(() => props.fields.reduce((a, f) => a + f.bits, 0))
const hasValue = computed(() => props.fields.some((f) => f.value))

/** Widths are damped, not proportional: at true ratio a 1-bit field beside a
 *  256-bit one is unreadable. Basis is the legible minimum, grow spends the
 *  remainder log2-weighted, so wide fields read wider and the row still fills.
 *  The range label carries the exact width. */
const spans = computed(() => {
  let hi = total.value - 1
  return props.fields.map((f) => {
    const lo = hi - f.bits + 1
    const range = f.bits === 1 ? `${hi}` : `${hi}:${lo}`
    const ch =
      Math.max(String(f.name).length, range.length, String(f.value ?? "").length) + 3
    const s = { ...f, range, grow: 1 + Math.log2(f.bits), ch }
    hi = lo - 1
    return s
  })
})
</script>

<template>
  <div class="my-5 doc-w">
    <!-- Narrow screens scroll the strip rather than crushing every field. -->
    <div class="overflow-x-auto">
      <div
        class="flex items-stretch min-w-full w-max rounded-lg overflow-hidden border border-warm-200 dark:border-warm-700"
      >
      <div
        v-for="f in spans"
        :key="f.name"
        class="flex flex-col border-r last:border-r-0 border-warm-200 dark:border-warm-700"
        :style="{ flexGrow: f.grow, flexShrink: 1, flexBasis: `${f.ch}ch` }"
      >
        <div
          class="kt-text-micro font-mono text-center py-0.5 border-b border-warm-200 dark:border-warm-700 whitespace-nowrap"
          :class="
            f.accent
              ? 'bg-gem text-white'
              : 'bg-warm-100 dark:bg-warm-800 text-warm-500 dark:text-warm-400'
          "
        >
          {{ f.range }}
        </div>
        <div
          class="flex-1 px-1.5 py-1.5 text-center bg-white dark:bg-warm-900 flex flex-col justify-center gap-0.5"
        >
          <div
            class="kt-text-caption font-mono font-semibold whitespace-nowrap text-warm-800 dark:text-warm-200"
          >
            {{ f.name }}
          </div>
          <div
            v-if="hasValue"
            class="kt-text-micro font-mono whitespace-nowrap text-warm-400 dark:text-warm-600"
          >
            {{ f.value || " " }}
          </div>
          <div class="kt-text-micro font-mono text-warm-300 dark:text-warm-700">
            {{ f.bits }}b
          </div>
        </div>
      </div>
      </div>
    </div>
    <p class="kt-text-caption text-warm-500 dark:text-warm-400 mt-2">
      <span v-if="props.caption">{{ props.caption }} · </span>{{ total }} bits ·
      <span class="text-warm-400 dark:text-warm-600">widths not to scale</span>
    </p>
  </div>
</template>
