<script setup>
import panzoom from "panzoom";

const props = defineProps({
  caption: { type: String, default: "" },
  zoom: { type: Boolean, default: false },
  pad: { type: Boolean, default: true },
  wide: { type: Boolean, default: false },
});

const stage = ref(null);
let pz = null;

onMounted(() => {
  if (props.zoom && stage.value) {
    pz = panzoom(stage.value, {
      maxZoom: 4,
      minZoom: 0.4,
      bounds: true,
      boundsPadding: 0.2,
      zoomDoubleClickSpeed: 1,
      beforeMouseDown: (e) => !e.altKey && !e.ctrlKey && !e.metaKey,
    });
  }
});
onBeforeUnmount(() => pz?.dispose());

function reset() {
  pz?.moveTo(0, 0);
  pz?.zoomAbs(0, 0, 1);
}
</script>

<template>
  <figure class="doc-fig" :class="props.wide ? 'doc-break' : 'doc-w'">
    <div class="relative overflow-hidden" :class="props.pad ? 'p-5' : ''">
      <div ref="stage">
        <slot />
      </div>
      <button
        v-if="props.zoom"
        class="absolute top-2 right-2 nav-item w-7 h-7 bg-warm-100/80 dark:bg-warm-800/80"
        title="Reset view — drag with Ctrl/Alt held, scroll to zoom"
        @click="reset"
      >
        <div class="i-carbon-center-circle text-xs" />
      </button>
    </div>
    <figcaption v-if="props.caption || $slots.caption" class="doc-cap">
      <slot name="caption">{{ props.caption }}</slot>
    </figcaption>
  </figure>
</template>
