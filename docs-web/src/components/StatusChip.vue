<script setup>
const props = defineProps({ status: { type: String, required: true } });

const TONE = {
  shipped:
    "bg-sage-light text-sage-shadow dark:bg-sage-shadow/40 dark:text-sage-light",
  measured:
    "bg-sage-light text-sage-shadow dark:bg-sage-shadow/40 dark:text-sage-light",
  building:
    "bg-amber-light text-amber-shadow dark:bg-amber-shadow/40 dark:text-amber-light",
  planned:
    "bg-amber-light text-amber-shadow dark:bg-amber-shadow/40 dark:text-amber-light",
  projected:
    "bg-amber-light text-amber-shadow dark:bg-amber-shadow/40 dark:text-amber-light",
  broken:
    "bg-coral-light text-coral-shadow dark:bg-coral-shadow/40 dark:text-coral-light",
  retired:
    "bg-coral-light text-coral-shadow dark:bg-coral-shadow/40 dark:text-coral-light",
};
</script>

<template>
  <span
    class="gem-badge"
    :class="
      TONE[props.status] ??
      'bg-warm-200 text-warm-700 dark:bg-warm-800 dark:text-warm-300'
    "
  >
    {{ props.status }}
  </span>
</template>
