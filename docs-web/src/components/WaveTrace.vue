<script setup>
/**
 * Cycle trace. Replaces the ASCII tables in the microarchitecture docs.
 *
 * rows: [{ name, kind: 'bus'|'bit'|'text', values: [], mark?: [cycleIdx] }]
 *   bus  - a value per cycle, drawn as a held cell; repeats merge visually
 *   bit  - 0/1 per cycle, drawn as a step waveform
 *   text - annotation row, no cell chrome
 * notes: [{ cycle, text, tone?: 'bad'|'good' }] - callouts under the grid
 */
const props = defineProps({
  rows: { type: Array, required: true },
  cycles: { type: Number, default: 0 },
  start: { type: Number, default: 0 },
  notes: { type: Array, default: () => [] },
  variant: { type: String, default: "" },
  label: { type: String, default: "" },
})

const n = computed(
  () => props.cycles || Math.max(...props.rows.map((r) => r.values.length)),
)
const cols = computed(() => Array.from({ length: n.value }, (_, i) => i))
const tone = computed(() =>
  props.variant === "broken"
    ? "border-coral/40"
    : props.variant === "fixed"
      ? "border-sage/40"
      : "border-warm-200/60 dark:border-warm-700/60",
)
const isMarked = (row, i) => row.mark?.includes(i)
</script>

<template>
  <div class="my-5 doc-w rounded-xl border overflow-hidden" :class="tone">
    <div
      v-if="props.label || props.variant"
      class="flex items-center gap-2 px-3 py-1.5 border-b kt-text-micro font-semibold uppercase tracking-wider"
      :class="[
        tone,
        props.variant === 'broken'
          ? 'bg-coral-light/40 text-coral-shadow dark:bg-coral-shadow/25 dark:text-coral-light'
          : props.variant === 'fixed'
            ? 'bg-sage-light/40 text-sage-shadow dark:bg-sage-shadow/25 dark:text-sage-light'
            : 'bg-warm-100 dark:bg-warm-900 text-warm-500 dark:text-warm-400',
      ]"
    >
      <div
        v-if="props.variant"
        :class="props.variant === 'broken' ? 'i-carbon-close-outline' : 'i-carbon-checkmark-outline'"
      />
      {{ props.label || props.variant }}
    </div>

    <div class="overflow-x-auto">
      <table class="font-mono text-[13px] border-collapse min-w-full">
        <thead>
          <tr class="text-warm-400 dark:text-warm-600">
            <th class="text-left font-medium px-3 py-1.5 sticky left-0 bg-warm-50 dark:bg-warm-900 z-1">
              cycle
            </th>
            <th
              v-for="i in cols"
              :key="i"
              class="px-2 py-1.5 font-medium text-center min-w-[4.5rem]"
            >
              {{ props.start + i }}
            </th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="row in props.rows"
            :key="row.name"
            class="border-t border-warm-200/50 dark:border-warm-700/40"
          >
            <td
              class="px-3 py-1 text-warm-600 dark:text-warm-400 whitespace-nowrap sticky left-0 bg-warm-50 dark:bg-warm-900 z-1"
            >
              {{ row.name }}
            </td>

            <template v-if="row.kind === 'bit'">
              <td v-for="i in cols" :key="i" class="px-0 py-1 align-middle">
                <svg viewBox="0 0 40 20" class="w-full h-5 overflow-visible">
                  <path
                    :d="`M0,${row.values[i] ? 4 : 16} H40`"
                    :stroke="isMarked(row, i) ? 'var(--gem-main)' : 'currentColor'"
                    :stroke-width="isMarked(row, i) ? 2.2 : 1.6"
                    fill="none"
                    class="text-warm-500 dark:text-warm-400"
                  />
                  <path
                    v-if="i > 0 && row.values[i] !== row.values[i - 1]"
                    :d="`M0,4 V16`"
                    stroke="currentColor"
                    stroke-width="1.6"
                    fill="none"
                    class="text-warm-500 dark:text-warm-400"
                  />
                </svg>
              </td>
            </template>

            <template v-else-if="row.kind === 'text'">
              <td
                v-for="i in cols"
                :key="i"
                class="px-2 py-1 text-center text-warm-400 dark:text-warm-600 kt-text-micro whitespace-nowrap"
              >
                {{ row.values[i] ?? "" }}
              </td>
            </template>

            <template v-else>
              <td v-for="i in cols" :key="i" class="px-1 py-1">
                <div
                  v-if="row.values[i] !== undefined && row.values[i] !== null"
                  class="px-2 py-0.5 rounded text-center whitespace-nowrap transition-colors"
                  :class="
                    isMarked(row, i)
                      ? 'bg-gem text-white font-semibold'
                      : 'bg-warm-100 dark:bg-warm-800 text-warm-700 dark:text-warm-300'
                  "
                >
                  {{ row.values[i] }}
                </div>
                <div v-else class="text-center text-warm-300 dark:text-warm-700">·</div>
              </td>
            </template>
          </tr>
        </tbody>
      </table>
    </div>

    <div
      v-if="props.notes.length"
      class="px-3 py-2 border-t bg-warm-50 dark:bg-warm-950/40 space-y-1"
      :class="tone"
    >
      <p
        v-for="(note, k) in props.notes"
        :key="k"
        class="kt-text-caption leading-5"
        :class="
          note.tone === 'bad'
            ? 'text-coral-shadow dark:text-coral-light'
            : note.tone === 'good'
              ? 'text-sage-shadow dark:text-sage-light'
              : 'text-warm-500 dark:text-warm-400'
        "
      >
        <span v-if="note.cycle !== undefined" class="font-mono opacity-70">
          @{{ props.start + note.cycle }}
        </span>
        {{ note.text }}
      </p>
    </div>
  </div>
</template>
