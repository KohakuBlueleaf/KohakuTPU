<script setup>
import { SECTIONS, ALL_PAGES, pageFor } from "@/site";
import { gemVars } from "@/utils/colors";

const route = useRoute();
const dark = ref(document.documentElement.classList.contains("dark"));
const navOpen = ref(false);

const current = computed(() => pageFor(route.path));
const section = computed(
  () => SECTIONS.find((s) => s.key === current.value?.section) ?? null,
);
const scope = computed(() => gemVars(current.value?.domain ?? "framework"));

// ---- sidebar: collapsible groups + static search -------------------------
const q = ref("");
const results = computed(() => {
  const s = q.value.trim().toLowerCase();
  if (!s) return null;
  return ALL_PAGES.filter(
    (p) =>
      p.title.toLowerCase().includes(s) ||
      p.short.toLowerCase().includes(s) ||
      p.path.toLowerCase().includes(s),
  );
});

// A group's open state, keyed by its path. The group holding the current page
// opens itself, so a deep link always shows its neighbours.
const open = reactive({});
const toggle = (path) => (open[path] = !open[path]);
const groupActive = (n) =>
  n.path === route.path || n.children?.some((c) => c.path === route.path);
watch(
  () => route.path,
  () => {
    for (const s of SECTIONS)
      for (const n of s.tree)
        if (n.children && groupActive(n)) open[n.path] = true;
  },
  { immediate: true },
);

const linkClass = (path) =>
  route.path === path
    ? "bg-warm-200/70 dark:bg-warm-800/60 text-warm-900 dark:text-warm-100 font-medium"
    : "text-warm-600 dark:text-warm-400 hover:bg-warm-200/40 dark:hover:bg-warm-800/40";

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
        <div class="p-3 sticky top-0 z-10 bg-warm-100/95 dark:bg-warm-950/95">
          <div class="relative">
            <div
              class="i-carbon-search absolute left-2.5 top-1/2 -translate-y-1/2 text-warm-400 dark:text-warm-600"
            />
            <input
              v-model="q"
              type="search"
              placeholder="Search pages…"
              class="w-full pl-8 pr-2.5 py-1.5 rounded-md kt-text-body bg-warm-200/50 dark:bg-warm-800/50 border border-warm-200 dark:border-warm-700 text-warm-800 dark:text-warm-200 placeholder:text-warm-400 dark:placeholder:text-warm-600 outline-none focus:border-gem"
            />
          </div>
        </div>

        <!-- search results: a flat, cross-section list -->
        <div v-if="results" class="px-3 pb-4">
          <RouterLink
            v-for="p in results"
            :key="p.path"
            :to="p.path"
            :style="gemVars(p.domain)"
            class="flex items-center gap-2.5 px-2.5 py-2 rounded-md kt-text-body no-underline transition-colors"
            :class="linkClass(p.path)"
          >
            <span class="w-2 h-2 rounded-full shrink-0 bg-gem" />
            <span class="flex-1 min-w-0 truncate">{{ p.short }}</span>
            <span class="kt-text-caption text-warm-400 dark:text-warm-600">{{
              p.section
            }}</span>
          </RouterLink>
          <div
            v-if="!results.length"
            class="px-2.5 py-3 kt-text-caption text-warm-400 dark:text-warm-600"
          >
            No pages match “{{ q }}”.
          </div>
        </div>

        <!-- the tree: section → sub-topic → page -->
        <template v-else>
          <!-- a section whose only page is its root has nothing to list: the tab is its entry -->
          <div
            v-for="s in SECTIONS.filter((x) => x.tree.length)"
            :key="s.key"
            class="px-3 py-3"
          >
            <div
              class="kt-text-caption uppercase tracking-wider text-warm-400 dark:text-warm-600 px-2.5 mb-2 font-semibold"
            >
              {{ s.title }}
            </div>
            <template v-for="n in s.tree" :key="n.path">
              <!-- a sub-topic with pages under it: collapsible -->
              <div v-if="n.children" :style="gemVars(n.domain ?? s.domain)">
                <div class="flex items-center">
                  <button
                    class="w-7 h-8 flex items-center justify-center shrink-0 rounded-md text-warm-400 dark:text-warm-600 hover:text-warm-700 dark:hover:text-warm-300"
                    :aria-expanded="!!open[n.path]"
                    @click="toggle(n.path)"
                  >
                    <div
                      class="i-carbon-chevron-right transition-transform"
                      :class="open[n.path] ? 'rotate-90' : ''"
                    />
                  </button>
                  <RouterLink
                    :to="n.path"
                    class="flex-1 min-w-0 flex items-center gap-2 px-2 py-2 rounded-md kt-text-body no-underline transition-colors"
                    :class="linkClass(n.path)"
                  >
                    <span class="truncate">{{ n.short }}</span>
                  </RouterLink>
                </div>
                <div
                  v-show="open[n.path]"
                  class="ml-3.5 pl-2 border-l border-warm-200 dark:border-warm-700"
                >
                  <RouterLink
                    v-for="c in n.children"
                    :key="c.path"
                    :to="c.path"
                    class="flex items-center gap-2 px-2.5 py-1.5 rounded-md kt-text-caption no-underline transition-colors"
                    :class="linkClass(c.path)"
                  >
                    <span class="w-1.5 h-1.5 rounded-full shrink-0 bg-gem" />
                    <span class="truncate">{{ c.short }}</span>
                  </RouterLink>
                </div>
              </div>

              <!-- a plain page -->
              <RouterLink
                v-else
                :to="n.path"
                :style="gemVars(n.domain ?? s.domain)"
                class="flex items-center gap-2.5 pl-2 pr-2.5 py-2 rounded-md kt-text-body no-underline transition-colors"
                :class="linkClass(n.path)"
              >
                <span class="w-7 flex justify-center shrink-0">
                  <span class="w-2 h-2 rounded-full bg-gem" />
                </span>
                <span class="truncate">{{ n.short }}</span>
              </RouterLink>
            </template>
          </div>
        </template>
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
