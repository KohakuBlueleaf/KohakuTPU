<script setup>
/** Scrubber over an array of states. Slot receives { state, i, n }. */
const props = defineProps({
  steps: { type: Array, required: true },
  label: { type: String, default: "" },
  interval: { type: Number, default: 1100 },
})

const i = ref(0)
const playing = ref(false)
let timer = null

const n = computed(() => props.steps.length)
const state = computed(() => props.steps[i.value])

function stop() {
  playing.value = false
  clearInterval(timer)
  timer = null
}
function play() {
  if (playing.value) return stop()
  playing.value = true
  timer = setInterval(() => {
    if (i.value >= n.value - 1) return stop()
    i.value++
  }, props.interval)
}
function go(k) {
  stop()
  i.value = Math.min(Math.max(k, 0), n.value - 1)
}
onBeforeUnmount(stop)
</script>

<template>
  <div class="my-5 doc-w card overflow-hidden">
    <div
      class="flex items-center gap-2 px-3 py-2 border-b border-warm-200/60 dark:border-warm-700/60 bg-warm-50 dark:bg-warm-900"
    >
      <button class="nav-item w-7 h-7" title="Previous" @click="go(i - 1)">
        <div class="i-carbon-chevron-left text-xs" />
      </button>
      <button class="nav-item w-7 h-7" :title="playing ? 'Pause' : 'Play'" @click="play">
        <div :class="playing ? 'i-carbon-pause' : 'i-carbon-play'" class="text-xs" />
      </button>
      <button class="nav-item w-7 h-7" title="Next" @click="go(i + 1)">
        <div class="i-carbon-chevron-right text-xs" />
      </button>

      <input
        type="range"
        class="flex-1 accent-iolite h-1 cursor-pointer"
        :min="0"
        :max="n - 1"
        :value="i"
        @input="go(Number($event.target.value))"
      />

      <span class="kt-text-micro font-mono text-warm-500 dark:text-warm-400 tabular-nums">
        {{ i + 1 }}/{{ n }}
      </span>
    </div>

    <div v-if="state?.title || props.label" class="px-4 pt-3">
      <div class="kt-text-caption font-semibold text-warm-700 dark:text-warm-300">
        {{ state?.title || props.label }}
      </div>
    </div>

    <div class="p-4">
      <slot :state="state" :i="i" :n="n" />
    </div>

    <p
      v-if="state?.note"
      class="kt-text-caption px-4 pb-3 -mt-1 text-warm-500 dark:text-warm-400 leading-5"
    >
      {{ state.note }}
    </p>
  </div>
</template>
