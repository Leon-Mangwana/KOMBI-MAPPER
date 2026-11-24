// Separate from vite.config.ts on purpose, as in capture/: the tests cover
// pure modules and need neither the Vue plugin nor a browser.
import { defineConfig } from 'vitest/config'

export default defineConfig({
  test: {
    include: ['src/**/*.test.ts'],
    environment: 'node',
  },
})
