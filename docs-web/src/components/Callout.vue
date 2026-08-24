<script setup>
const props = defineProps({
  kind: { type: String, default: "note" },
  title: { type: String, default: "" },
});

const STYLE = {
  note: { bar: "bg-sapphire", icon: "i-carbon-information", label: "Note" },
  rule: { bar: "bg-iolite", icon: "i-carbon-rule", label: "Rule" },
  trap: { bar: "bg-coral", icon: "i-carbon-warning-alt", label: "Trap" },
  measured: { bar: "bg-sage", icon: "i-carbon-chart-line", label: "Measured" },
  open: { bar: "bg-amber", icon: "i-carbon-help", label: "Open question" },
};
const s = computed(() => STYLE[props.kind] ?? STYLE.note);
</script>

<template>
  <aside
    class="my-5 doc-w flex gap-0 rounded-xl overflow-hidden border border-warm-200/60 dark:border-warm-700/60 bg-warm-50 dark:bg-warm-900/60"
  >
    <div class="w-1 shrink-0" :class="s.bar" />
    <div class="px-4 py-3 min-w-0">
      <div class="flex items-center gap-1.5 mb-1">
        <div
          :class="s.icon"
          class="kt-text-caption text-warm-500 dark:text-warm-400"
        />
        <span
          class="kt-text-caption font-semibold text-warm-700 dark:text-warm-300"
        >
          {{ props.title || s.label }}
        </span>
      </div>
      <div
        class="kt-text-body text-warm-600 dark:text-warm-400 leading-6 [&>p]:my-1.5"
      >
        <slot />
      </div>
    </div>
  </aside>
</template>
