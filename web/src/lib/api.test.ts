/**
 * Tests for the auth-token storage helpers in lib/api.ts.
 *
 * These guard the security-critical bridge between "user pasted a token
 * in Settings" and "every fetch carries the bearer header."  Regression
 * here = the UI silently sends unauthed requests (or sends the wrong
 * token after a rotation).
 */
import { describe, it, expect, beforeEach } from 'vitest'
import { getApiToken, setApiToken } from './api'

describe('API token storage', () => {
  beforeEach(() => {
    // Go through our own helper (which has defensive try/catch) rather
    // than touching localStorage directly — jsdom v29's Storage shim is
    // partially broken (missing .removeItem and .clear on some builds).
    setApiToken(null)
  })

  it('returns null when no token is set', () => {
    expect(getApiToken()).toBeNull()
  })

  it('round-trips a token through localStorage', () => {
    setApiToken('abc123')
    expect(getApiToken()).toBe('abc123')
  })

  it('clears the token when set to null', () => {
    setApiToken('abc123')
    setApiToken(null)
    expect(getApiToken()).toBeNull()
  })

  it('clears the token when set to empty string (treated as null)', () => {
    setApiToken('abc123')
    setApiToken('')
    // Empty-string is falsy in the implementation — should clear.
    expect(getApiToken()).toBeNull()
  })

  it('persists a token written via setApiToken', () => {
    // We can't peek at localStorage directly (jsdom shim is partial), so
    // verify persistence via the same getter the production code uses.
    setApiToken('persistent-token')
    expect(getApiToken()).toBe('persistent-token')
  })
})
