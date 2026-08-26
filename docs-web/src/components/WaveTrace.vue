<script setup>
/**
 * Cycle trace. Replaces the ASCII tables in the microarchitecture docs.
 *
 * rows: [{ name, kind: 'bus'|'bit'|'text', values: [], mark?: [cycleIdx] }]
 *   bus  - a value per cycle, drawn as a held cell; repeats merge visually
 *   bit  - 0/1 per cycle, drawn as a step waveform between two rails
 *   text - annotation row, no cell chrome
 * notes: [{ cycle, text, tone?: 'bad'|'good' }] - callouts under the grid
 *
 * A cell's SVG must stretch to the column, so preserveAspectRatio is "none"
 * and every stroke carries vector-effect="non-scaling-stroke". Without both,
 * the viewBox is letterboxed inside the column and the waveform renders as
 * detached dashes with the edges floating between them.
 */
const props = defineProps({
  rows: { type: Array, required: true },
  cycles: { type: Number, default: 0 },
  start: { type: Number, default: 0 },
  notes: { type: Array, default: () => [] },
  variant: { type: String, default: "" },
  label: { type: String, default: "" },
  clock: { type: Boolean, default: true },
});

const HI = 5;
const LO = 15;

const n = computed(
  () => props.cycles || Math.max(...props.rows.map((r) => r.values.length)),
);
const cols = computed(() => Array.from({ length: n.value }, (_, i) => i));
const tone = computed(() =>
  props.variant === "broken"
    ? "border-coral/40"
    : props.variant === "fixed"
      ? "border-sage/40"
      : "border-warm-200/60 dark:border-warm-700/60",
);
const isMarked = (row, i) => row.mark?.includes(i);
const lvl = (row, i) => (row.values[i] ? HI : LO);

/** Step for one cycle: the leading edge, then the level held across the cell. */
const bitPath = (row, i) => {
  const y = lvl(row, i);
  const prev = i > 0 ? lvl(row, i - 1) : y;
  return prev === y ? `M0,${y} H40` : `M0,${prev} V${y} H40`;
};
/** Filled band from the high rail down to the low rail, so high reads solid. */
const bitFill = (row, i) => (row.values[i] ? `M0,${HI} H40 V${LO} H0 Z` : "");
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
        :class="
          props.variant === 'broken'
            ? 'i-carbon-close-outline'
            : 'i-carbon-checkmark-outline'
        "
      />
      {{ props.label || props.variant }}
    </div>

    <div class="overflow-x-auto">
      <table
        class="font-mono text-[13px] border-collapse table-fixed"
        :style="{ minWidth: `${6.5 + n * 5}rem`, width: '100%' }"
      >
        <colgroup>
          <col style="width: 6.5rem" />
          <col v-for="i in cols" :key="i" style="width: 5rem" />
        </colgroup>
        <thead>
          <tr class="text-warm-400 dark:text-warm-600">
            <th
              class="text-left font-medium px-3 pt-1.5 pb-0 sticky left-0 bg-warm-50 dark:bg-warm-900 z-1"
            >
              cycle
            </th>
            <th
              v-for="i in cols"
              :key="i"
              class="px-0 pt-1.5 pb-0 font-medium text-center min-w-[4.5rem]"
            >
              {{ props.start + i }}
            </th>
          </tr>

          <tr v-if="props.clock" class="text-warm-500 dark:text-warm-400">
            <th
              class="text-left font-medium px-3 pb-1.5 pt-0 sticky left-0 bg-warm-50 dark:bg-warm-900 z-1"
            >
              clk
            </th>
            <td v-for="i in cols" :key="i" class="px-0 pb-1.5 pt-0">
              <svg
                viewBox="0 0 40 20"
                preserveAspectRatio="none"
                class="block w-full h-5 text-warm-400 dark:text-warm-500"
              >
                <path
                  d="M0,5 H20 V15 H40"
                  stroke="currentColor"
                  stroke-width="1.6"
                  vector-effect="non-scaling-stroke"
                  fill="none"
                />
                <path
                  d="M0,5 H20 V15 H0 Z"
                  fill="currentColor"
                  fill-opacity="0.13"
                />
                <path
                  v-if="i > 0"
                  d="M0,5 V15"
                  stroke="currentColor"
                  stroke-width="1.6"
                  vector-effect="non-scaling-stroke"
                  fill="none"
                />
              </svg>
            </td>
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
                <svg
                  viewBox="0 0 40 20"
                  preserveAspectRatio="none"
                  class="block w-full h-5"
                >
                  <path
                    :d="`M0,${LO} H40`"
                    stroke="currentColor"
                    stroke-width="1"
                    stroke-dasharray="2 3"
                    vector-effect="non-scaling-stroke"
                    fill="none"
                    class="text-warm-300 dark:text-warm-700"
                  />
                  <path
                    v-if="i > 0"
                    :d="`M0,0 V20`"
                    stroke="currentColor"
                    stroke-width="1"
                    vector-effect="non-scaling-stroke"
                    fill="none"
                    class="text-warm-200/70 dark:text-warm-800/70"
                  />
                  <path
                    v-if="bitFill(row, i)"
                    :d="bitFill(row, i)"
                    :fill="
                      isMarked(row, i) ? 'var(--gem-main)' : 'currentColor'
                    "
                    :fill-opacity="isMarked(row, i) ? 0.36 : 0.26"
                    class="text-warm-500 dark:text-warm-400"
                  />
                  <path
                    :d="bitPath(row, i)"
                    :stroke="
                      isMarked(row, i) ? 'var(--gem-main)' : 'currentColor'
                    "
                    :stroke-width="isMarked(row, i) ? 2.4 : 1.7"
                    vector-effect="non-scaling-stroke"
                    stroke-linejoin="miter"
                    fill="none"
                    class="text-warm-600 dark:text-warm-300"
                  />
                </svg>
              </td>
            </template>

            <template v-else-if="row.kind === 'text'">
              <td
                v-for="i in cols"
                :key="i"
                class="px-1 py-1 text-center text-warm-400 dark:text-warm-600 kt-text-micro leading-tight break-words"
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
                <div
                  v-else
                  class="text-center text-warm-300 dark:text-warm-700"
                >
                  ·
                </div>
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
