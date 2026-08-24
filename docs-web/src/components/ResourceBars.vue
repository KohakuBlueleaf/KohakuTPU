<script setup>
/** items: [{ label, value, max?, note?, tone? }] — max defaults to the largest value. */
const props = defineProps({
  items: { type: Array, required: true },
  unit: { type: String, default: "LUT" },
  max: { type: Number, default: 0 },
  caption: { type: String, default: "" },
});
const cap = computed(
  () => props.max || Math.max(...props.items.map((i) => i.max ?? i.value)),
);
const fmt = (v) => v.toLocaleString();
const TONE = {
  good: "bg-sage",
  bad: "bg-coral",
  warn: "bg-amber",
  accent: "bg-gem",
};
</script>

<template>
  <div class="my-5 doc-w card p-4">
    <div v-for="it in props.items" :key="it.label" class="mb-3 last:mb-0">
      <div class="flex items-baseline justify-between gap-3 mb-1">
        <span class="kt-text-caption text-warm-700 dark:text-warm-300">{{
          it.label
        }}</span>
        <span
          class="kt-text-micro font-mono text-warm-500 dark:text-warm-400 tabular-nums"
        >
          {{ fmt(it.value)
          }}<span v-if="it.note" class="ml-2 opacity-70">{{ it.note }}</span>
        </span>
      </div>
      <div
        class="h-2 rounded-full bg-warm-100 dark:bg-warm-800 overflow-hidden"
      >
        <div
          class="h-full rounded-full transition-all duration-500"
          :class="TONE[it.tone] ?? 'bg-gem'"
          :style="{ width: Math.max(1, (it.value / cap) * 100) + '%' }"
        />
      </div>
    </div>
    <p
      v-if="props.caption"
      class="kt-text-caption text-warm-500 dark:text-warm-400 mt-3"
    >
      {{ props.caption }} · {{ props.unit }}
    </p>
  </div>
</template>
