// Separate from vite.config.ts on purpose: the tests cover pure modules and
// must not load the PWA plugin (and its dev service worker) to run.
import { defineConfig } from 'vitest/config'

export default defineConfig({
  test: {
    include: ['src/**/*.test.ts'],
    environment: 'node',
  },
})
