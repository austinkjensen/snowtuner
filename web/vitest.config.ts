/// <reference types="vitest" />
import path from 'node:path'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

/**
 * Vitest config — separate from vite.config.ts because:
 *
 *   1. We don't want the TanStack Router plugin or Tailwind plugin to run
 *      in tests (they're slow and have side-effects on file watchers).
 *   2. The test env is jsdom, not the browser.
 *   3. globals=true lets tests use `describe / it / expect` without
 *      explicit imports — matches the Vitest convention.
 */
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  test: {
    // happy-dom over jsdom: faster, better-maintained, and (importantly
    // for our tests) ships a complete localStorage implementation that
    // jsdom v29 was missing methods on.
    environment: 'happy-dom',
    globals: true,
    setupFiles: ['./src/test-setup.ts'],
    // Mirror the Python suite's discipline: don't let unhandled errors
    // slip through silently.  Vitest defaults are already strict, but
    // configure explicit reporters for CI-friendly output.
    reporters: ['default'],
  },
})
