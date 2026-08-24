// docs-web/vite.config.js
// Stack mirrors src/kohaku-hub-ui: rolldown-vite, file-routed pages, UnoCSS.
import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import VueRouter from 'unplugin-vue-router/vite'
import AutoImport from 'unplugin-auto-import/vite'
import Components from 'unplugin-vue-components/vite'
import UnoCSS from 'unocss/vite'

export default defineConfig({
  base: './',

  plugins: [
    // Must be before the Vue plugin.
    VueRouter({
      routesFolder: 'src/pages',
      dts: 'src/typed-router.d.ts',
      extensions: ['.vue'],
      exclude: ['**/components/**', '**/_*.vue'],
    }),

    vue(),

    AutoImport({
      imports: [
        'vue',
        'pinia',
        { 'vue-router': ['onBeforeRouteLeave', 'onBeforeRouteUpdate', 'useLink'] },
        { 'vue-router/auto': ['useRoute', 'useRouter'] },
      ],
      dts: 'src/auto-imports.d.ts',
    }),

    // Every kit component under src/components is global — content pages never
    // import a diagram primitive by hand.
    Components({
      dts: 'src/components.d.ts',
      dirs: ['src/components'],
      deep: true,
    }),

    UnoCSS(),
  ],

  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },

  // Same reasoning as the hub UI: pre-bundle everything the site imports and
  // turn discovery off, so the dev server never full-reloads mid-read.
  optimizeDeps: {
    include: ['vue', 'vue-router', 'pinia', 'panzoom', 'mermaid'],
    noDiscovery: true,
  },

  build: {
    target: 'esnext',
    minify: true,
    sourcemap: false,
    cssMinify: true,
    chunkSizeWarningLimit: 1200,
    rollupOptions: {
      output: {
        manualChunks: (id) => {
          if (id.includes('mermaid')) return 'mermaid'
          if (
            id.includes('node_modules/vue/') ||
            id.includes('node_modules/vue-router/') ||
            id.includes('node_modules/pinia/')
          )
            return 'vendor'
        },
      },
    },
  },

  cacheDir: 'node_modules/.vite',
  server: { port: 5273 },
})
