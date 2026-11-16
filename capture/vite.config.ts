import { fileURLToPath, URL } from 'node:url'

import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'
import { VitePWA } from 'vite-plugin-pwa'

export default defineConfig({
  plugins: [
    vue(),
    VitePWA({
      // A hand-written worker (src/sw.ts): the arrival notification needs a
      // notificationclick handler, which a generated worker cannot have.
      strategies: 'injectManifest',
      srcDir: 'src',
      filename: 'sw.ts',
      registerType: 'autoUpdate',
      injectRegister: 'auto',
      includeAssets: ['favicon.svg', 'icon.svg'],
      manifest: {
        name: 'Kombi Capture',
        short_name: 'Kombi',
        description: 'Record a GPS trace while riding a kombi in Harare.',
        theme_color: '#0f172a',
        background_color: '#0f172a',
        display: 'standalone',
        orientation: 'portrait',
        start_url: '/',
        scope: '/',
        icons: [
          { src: 'icon.svg', sizes: 'any', type: 'image/svg+xml' },
          { src: 'icon.svg', sizes: 'any', type: 'image/svg+xml', purpose: 'maskable' },
        ],
      },
      injectManifest: {
        globPatterns: ['**/*.{js,css,html,svg,png,ico,woff2}'],
        // MapLibre bundles are large; the app shell must still precache.
        maximumFileSizeToCacheInBytes: 6 * 1024 * 1024,
        // The navigation fallback and its /traces denylist live in src/sw.ts.
      },
      devOptions: {
        // Lets you exercise install + offline behaviour with `npm run dev`.
        enabled: true,
        type: 'module',
        // The dev manifest must list the entry sw.ts binds navigations to.
        navigateFallback: 'index.html',
      },
    }),
  ],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
})
