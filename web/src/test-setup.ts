/**
 * Vitest setup file — loaded before any test file via vitest.config.ts.
 *
 * Wires up @testing-library/jest-dom matchers globally (.toBeInTheDocument,
 * .toHaveTextContent, etc.) so individual tests don't have to import them.
 */
import '@testing-library/jest-dom/vitest'
import { afterEach } from 'vitest'
import { cleanup } from '@testing-library/react'

// React Testing Library's cleanup() unmounts everything between tests —
// without this, components from one test leak into the next test's DOM
// and trip mysterious "found multiple matches" errors.
afterEach(() => {
  cleanup()
})

// ── localStorage polyfill ────────────────────────────────────────
// Both jsdom v29 and happy-dom expose a `localStorage` object without
// usable methods in the version we have pinned, so the production
// code's `localStorage.setItem` calls silently fail (the try/catch in
// api.ts swallows the resulting TypeError).  A minimal in-memory shim
// keeps the tests deterministic without depending on which DOM
// implementation we use.
class MemoryStorage {
  private store: Record<string, string> = {}
  getItem(key: string): string | null {
    return Object.prototype.hasOwnProperty.call(this.store, key)
      ? this.store[key]
      : null
  }
  setItem(key: string, value: string): void {
    this.store[key] = String(value)
  }
  removeItem(key: string): void {
    delete this.store[key]
  }
  clear(): void {
    this.store = {}
  }
  get length(): number {
    return Object.keys(this.store).length
  }
  key(i: number): string | null {
    return Object.keys(this.store)[i] ?? null
  }
}
;(globalThis as any).localStorage = new MemoryStorage()
;(globalThis as any).sessionStorage = new MemoryStorage()

// Make sure tests start with a clean storage rather than carrying state
// across spec boundaries.
afterEach(() => {
  ;(globalThis as any).localStorage.clear()
})
