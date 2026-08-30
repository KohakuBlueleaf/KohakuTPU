<script setup>
import panzoom from "panzoom";

const props = defineProps({
  caption: { type: String, default: "" },
  zoom: { type: Boolean, default: false },
  pad: { type: Boolean, default: true },
  wide: { type: Boolean, default: false },
  // A sheet: drawn at one scale for the full-screen viewer, so it breaks out
  // like `wide` and is exempt from the tall-wide audit.
  sheet: { type: Boolean, default: false },
});

/* ---- inline: pan with a modifier so the page still scrolls -------------- */
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
onBeforeUnmount(() => {
  pz?.dispose();
  closeFull();
});
function reset() {
  pz?.moveTo(0, 0);
  pz?.zoomAbs(0, 0, 1);
}

/* ---- full screen: drag to pan, wheel to zoom, fit / 1:1 / ± / Esc ------- */
const full = ref(false);
const fstage = ref(null);
const fview = ref(null);
const zoomPct = ref(100);
let fpz = null;
let natural = { w: 1200, h: 800 };

function openFull() {
  full.value = true;
  document.body.style.overflow = "hidden";
  window.addEventListener("keydown", onKey);
  nextTick(mountFull);
}
function closeFull() {
  if (!full.value) return;
  full.value = false;
  document.body.style.overflow = "";
  window.removeEventListener("keydown", onKey);
  fpz?.dispose();
  fpz = null;
}
function onKey(e) {
  if (e.key === "Escape") closeFull();
  else if (e.key === "0") fit();
  else if (e.key === "1") one();
  else if (e.key === "+" || e.key === "=") step(1.25);
  else if (e.key === "-") step(0.8);
}
function mountFull() {
  const svg = fstage.value?.querySelector("svg");
  if (svg) {
    // Lay the drawing out at its natural size: one viewBox unit is one pixel,
    // so 100 % is the size the page renders it at when it fits.
    const vb = (svg.getAttribute("viewBox") || "0 0 1200 800")
      .split(/\s+/)
      .map(Number);
    natural = { w: vb[2], h: vb[3] };
    fstage.value.style.width = `${natural.w}px`;
  }
  fpz = panzoom(fstage.value, {
    maxZoom: 6,
    minZoom: 0.1,
    zoomDoubleClickSpeed: 1,
    smoothScroll: false,
  });
  fpz.on(
    "zoom",
    () => (zoomPct.value = Math.round(fpz.getTransform().scale * 100)),
  );
  fit();
}
function fit() {
  if (!fpz || !fview.value) return;
  const vw = fview.value.clientWidth - 32;
  const vh = fview.value.clientHeight - 32;
  const s = Math.min(vw / natural.w, vh / natural.h, 4);
  fpz.zoomAbs(0, 0, s);
  fpz.moveTo((vw - natural.w * s) / 2 + 16, (vh - natural.h * s) / 2 + 16);
  zoomPct.value = Math.round(s * 100);
}
function one() {
  if (!fpz || !fview.value) return;
  const vw = fview.value.clientWidth;
  const vh = fview.value.clientHeight;
  fpz.zoomAbs(0, 0, 1);
  fpz.moveTo(
    Math.max(16, (vw - natural.w) / 2),
    Math.max(16, (vh - natural.h) / 2),
  );
  zoomPct.value = 100;
}
function step(k) {
  if (!fpz || !fview.value) return;
  const cx = fview.value.clientWidth / 2;
  const cy = fview.value.clientHeight / 2;
  fpz.smoothZoom(cx, cy, k);
}
</script>

<template>
  <figure
    class="doc-fig"
    :class="props.wide || props.sheet ? 'doc-break' : 'doc-w'"
    :data-sheet="props.sheet ? '1' : undefined"
  >
    <div class="relative overflow-hidden" :class="props.pad ? 'p-5' : ''">
      <div ref="stage">
        <slot />
      </div>
      <div v-if="props.zoom" class="absolute top-2 right-2 flex gap-1">
        <button
          class="nav-item w-7 h-7 bg-warm-100/80 dark:bg-warm-800/80"
          title="Reset view — drag with Ctrl/Alt held, scroll to zoom"
          @click="reset"
        >
          <div class="i-carbon-center-circle text-xs" />
        </button>
        <button
          class="nav-item w-7 h-7 bg-warm-100/80 dark:bg-warm-800/80"
          title="Full screen — drag to pan, wheel to zoom, 0 fit, 1 actual size, Esc"
          @click="openFull"
        >
          <div class="i-carbon-maximize text-xs" />
        </button>
      </div>
    </div>
    <figcaption v-if="props.caption || $slots.caption" class="doc-cap">
      <slot name="caption">{{ props.caption }}</slot>
    </figcaption>

    <Teleport to="body">
      <div
        v-if="full"
        class="fixed inset-0 z-50 flex flex-col bg-warm-50 dark:bg-warm-950"
      >
        <div
          class="shrink-0 h-12 px-3 flex items-center gap-2 border-b border-warm-200 dark:border-warm-700 bg-warm-100 dark:bg-warm-900"
        >
          <span
            class="kt-text-caption text-warm-500 dark:text-warm-400 truncate flex-1 min-w-0"
          >
            {{ props.caption }}
          </span>
          <span
            class="kt-text-caption font-mono text-warm-500 dark:text-warm-400 w-12 text-right"
          >
            {{ zoomPct }}%
          </span>
          <button
            class="nav-item w-8 h-8"
            title="zoom out (−)"
            @click="step(0.8)"
          >
            <div class="i-carbon-zoom-out" />
          </button>
          <button
            class="nav-item w-8 h-8"
            title="zoom in (+)"
            @click="step(1.25)"
          >
            <div class="i-carbon-zoom-in" />
          </button>
          <button class="nav-item w-8 h-8" title="fit (0)" @click="fit">
            <div class="i-carbon-fit-to-screen" />
          </button>
          <button class="nav-item w-8 h-8" title="actual size (1)" @click="one">
            <div class="i-carbon-zoom-reset" />
          </button>
          <button
            class="nav-item w-8 h-8"
            title="close (Esc)"
            @click="closeFull"
          >
            <div class="i-carbon-close" />
          </button>
        </div>
        <div
          ref="fview"
          class="flex-1 min-h-0 overflow-hidden cursor-grab active:cursor-grabbing"
        >
          <div ref="fstage" class="p-4 select-none">
            <slot />
          </div>
        </div>
      </div>
    </Teleport>
  </figure>
</template>
