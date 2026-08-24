<script setup>
import { SECTIONS, pageFor } from "@/site";
import { gemVars } from "@/utils/colors";

const route = useRoute();
const dark = ref(document.documentElement.classList.contains("dark"));
const navOpen = ref(false);

const current = computed(() => pageFor(route.path));
const section = computed(
  () => SECTIONS.find((s) => s.key === current.value?.section) ?? null,
);
const scope = computed(() => gemVars(current.value?.domain ?? "framework"));

function toggleTheme() {
  dark.value = !dark.value;
  document.documentElement.classList.toggle("dark", dark.value);
  localStorage.setItem("kt-theme", dark.value ? "dark" : "light");
}

// The window never scrolls, so the router's scrollBehavior cannot help here.
const pane = ref(null);
watch(
  () => route.fullPath,
  async () => {
    navOpen.value = false;
    await nextTick();
    if (route.hash) {
      pane.value?.querySelector(route.hash)?.scrollIntoView({ block: "start" });
    } else {
      pane.value?.scrollTo({ top: 0 });
    }
  },
);
</script>

<template>
  <div
    :style="scope"
    class="h-full flex flex-col overflow-hidden bg-warm-50 dark:bg-warm-950"
  >
    <header
      class="shrink-0 z-30 flex items-center gap-3 px-4 h-14 border-b border-warm-200 dark:border-warm-700 bg-warm-100 dark:bg-warm-950"
    >
      <button
        class="nav-item w-9 h-9 md:hidden"
        aria-label="Menu"
        @click="navOpen = !navOpen"
      >
        <div class="i-carbon-menu" />
      </button>

      <RouterLink to="/" class="flex items-center gap-2.5 no-underline">
        <div class="w-3 h-3 rounded-full bg-gem" />
        <span
          class="kt-text-emphasis font-semibold text-warm-800 dark:text-warm-200"
        >
          KohakuAccel
        </span>
      </RouterLink>

      <nav class="hidden md:flex items-center gap-1 ml-5">
        <RouterLink
          v-for="s in SECTIONS"
          :key="s.key"
          :to="s.pages[0].path"
          class="kt-text-body px-3 py-1.5 rounded-md no-underline transition-colors"
          :class="
            section?.key === s.key
              ? 'bg-warm-200/80 dark:bg-warm-800/60 text-warm-900 dark:text-warm-100'
              : 'text-warm-500 dark:text-warm-400 hover:text-warm-800 dark:hover:text-warm-200'
          "
        >
          {{ s.title }}
        </RouterLink>
      </nav>

      <div class="flex-1" />

      <button class="nav-item w-9 h-9" aria-label="Theme" @click="toggleTheme">
        <div :class="dark ? 'i-carbon-moon' : 'i-carbon-sun'" />
      </button>
    </header>

    <div class="flex-1 flex min-h-0">
      <aside
        class="w-64 shrink-0 border-r border-warm-200 dark:border-warm-700 bg-warm-100/50 dark:bg-warm-950/50 overflow-y-auto scrollbar-none"
        :class="
          navOpen
            ? 'block fixed top-14 bottom-0 left-0 z-20 bg-warm-100 dark:bg-warm-950'
            : 'hidden md:block'
        "
      >
        <div v-for="s in SECTIONS" :key="s.key" class="px-3 py-4">
          <div
            class="kt-text-caption uppercase tracking-wider text-warm-400 dark:text-warm-600 px-2.5 mb-2 font-semibold"
          >
            {{ s.title }}
          </div>
          <RouterLink
            v-for="p in s.pages"
            :key="p.path"
            :to="p.path"
            :style="gemVars(p.domain ?? s.domain)"
            class="flex items-center gap-2.5 px-2.5 py-2 rounded-md kt-text-body no-underline transition-colors"
            :class="
              route.path === p.path
                ? 'bg-warm-200/70 dark:bg-warm-800/60 text-warm-900 dark:text-warm-100 font-medium'
                : 'text-warm-600 dark:text-warm-400 hover:bg-warm-200/40 dark:hover:bg-warm-800/40'
            "
          >
            <span class="w-2 h-2 rounded-full shrink-0 bg-gem" />
            {{ p.short }}
          </RouterLink>
        </div>
      </aside>

      <main
        ref="pane"
        class="doc-pane flex-1 min-w-0 overflow-y-auto overflow-x-hidden"
      >
        <RouterView v-slot="{ Component }">
          <Transition name="fade" mode="out-in">
            <component :is="Component" />
          </Transition>
        </RouterView>
      </main>
    </div>
  </div>
</template>
