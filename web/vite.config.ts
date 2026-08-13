import { fileURLToPath, URL } from 'node:url'

import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

export default defineConfig({
  plugins: [vue()],
  // Ports are pinned so this app and the capture PWA can run side by side, and
  // so the API's CORS defaults can name both origins. Vite would otherwise
  // silently take the next free port and the browser would block the fetches.
  server: { port: 5174 },
  preview: { port: 4174 },
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
})
