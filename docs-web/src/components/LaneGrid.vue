<script setup>
/**
 * Lanes of one wave. `mask` is per-lane active; masked lanes read as inert
 * rather than absent, which is the point the ASCII `·` could not make.
 * rows: [{ name, values: [] , mono? }]
 */
const props = defineProps({
  lanes: { type: Number, default: 8 },
  mask: { type: Array, default: null },
  rows: { type: Array, default: () => [] },
  caption: { type: String, default: "" },
});
const idx = computed(() => Array.from({ length: props.lanes }, (_, i) => i));
const on = (i) => (props.mask ? !!props.mask[i] : true);
</script>

<template>
  <div class="my-4 doc-w">
    <div class="overflow-x-auto">
      <table class="border-collapse font-mono text-[13px] min-w-full">
        <thead>
          <tr>
            <th
              class="text-left px-2 py-1 kt-text-micro font-medium text-warm-400 dark:text-warm-600"
            >
              lane
            </th>
            <th
              v-for="i in idx"
              :key="i"
              class="px-1 py-1 text-center kt-text-micro font-medium min-w-[3.2rem]"
              :class="
                on(i)
                  ? 'text-warm-500 dark:text-warm-400'
                  : 'text-warm-300 dark:text-warm-700'
              "
            >
              {{ i }}
            </th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="props.mask">
            <td
              class="px-2 py-1 text-warm-600 dark:text-warm-400 whitespace-nowrap"
            >
              mask
            </td>
            <td v-for="i in idx" :key="i" class="px-1 py-1">
              <div
                class="h-6 rounded flex items-center justify-center font-semibold transition-colors"
                :class="
                  on(i)
                    ? 'bg-gem text-white'
                    : 'bg-warm-100 dark:bg-warm-800 text-warm-300 dark:text-warm-700'
                "
              >
                {{ on(i) ? 1 : 0 }}
              </div>
            </td>
          </tr>
          <tr
            v-for="row in props.rows"
            :key="row.name"
            class="border-t border-warm-200/50 dark:border-warm-700/40"
          >
            <td
              class="px-2 py-1 text-warm-600 dark:text-warm-400 whitespace-nowrap"
            >
              {{ row.name }}
            </td>
            <td v-for="i in idx" :key="i" class="px-1 py-1">
              <div
                class="h-6 rounded flex items-center justify-center transition-colors"
                :class="
                  on(i)
                    ? 'bg-warm-100 dark:bg-warm-800 text-warm-800 dark:text-warm-200'
                    : 'bg-transparent text-warm-300 dark:text-warm-700 line-through decoration-1'
                "
              >
                {{ row.values[i] ?? "·" }}
              </div>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <p
      v-if="props.caption"
      class="kt-text-caption text-warm-500 dark:text-warm-400 mt-2"
    >
      {{ props.caption }}
    </p>
  </div>
</template>
