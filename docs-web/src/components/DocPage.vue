<script setup>
import { gemVars } from "@/utils/colors"

const props = defineProps({
  title: { type: String, required: true },
  summary: { type: String, default: "" },
  domain: { type: String, default: "framework" },
  status: { type: String, default: "" },
  source: { type: String, default: "" },
})
</script>

<template>
  <article
    :style="gemVars(props.domain)"
    class="mx-auto w-full max-w-[var(--doc-measure)] px-4 sm:px-6 py-4 sm:py-6 pb-24"
  >
    <header class="pt-2 pb-6 border-b border-warm-200/60 dark:border-warm-700/60">
      <div class="flex items-center gap-2 mb-2">
        <span class="w-2 h-2 rounded-full bg-gem" />
        <span
          class="kt-text-micro uppercase tracking-wider font-semibold text-warm-400 dark:text-warm-600"
        >
          {{ props.domain }}
        </span>
        <StatusChip v-if="props.status" :status="props.status" />
      </div>
      <h1 class="doc-h1">{{ props.title }}</h1>
      <p v-if="props.summary" class="kt-text-emphasis text-warm-600 dark:text-warm-400 mt-2 max-w-[72ch] leading-7">
        {{ props.summary }}
      </p>
      <p v-if="props.source" class="kt-text-micro font-mono text-warm-400 dark:text-warm-600 mt-3">
        {{ props.source }}
      </p>
    </header>

    <div class="mt-2">
      <slot />
    </div>
  </article>
</template>
