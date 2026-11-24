/**
 * Operator mode: naming routes and stops from the map.
 *
 * Clustering cannot name anything, so somebody who knows Harare has to — and
 * doing that for thirty routes through curl is how it does not get done. This
 * is the smallest thing that makes it a five-minute job instead.
 *
 * **The controls are hidden behind `#admin` in the URL** rather than shown to
 * everyone and refused on use. That is not the security boundary — the API is,
 * and it checks the token on every write. It keeps the public map free of
 * affordances that would only ever reject a visitor.
 *
 * **The token is checked before it is kept.** Unlocking asks the API whether
 * the pasted token is right (`GET /admin/check`), so a typo or a stale token
 * is reported there and then, instead of as a 401 on the first save. A token
 * already in storage is not re-checked on load — it was checked when stored,
 * and a rotated one still surfaces as a 401 on the next write.
 *
 * **The token lives in `sessionStorage`.** It is a shared secret in a browser
 * either way, but sessionStorage is scoped to the tab and cleared when it
 * closes, so a borrowed or shared machine does not keep operator access alive
 * indefinitely. The cost is retyping it in a new tab, which an operator does
 * once per naming session.
 */
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { ApiError, checkAdminToken } from '@/lib/api'

const TOKEN_KEY = 'kombi-web:admin-token'

export const useAdminStore = defineStore('admin', () => {
  const token = ref(sessionStorage.getItem(TOKEN_KEY) ?? '')

  /** Whether the naming controls should be offered at all. */
  const available = ref(readHash())

  const active = computed(() => available.value && token.value.length > 0)

  /** An unlock attempt in flight. */
  const checking = ref(false)

  /** Why the last unlock attempt was refused, until the next attempt. */
  const error = ref<string | null>(null)

  function readHash(): boolean {
    return window.location.hash.replace(/^#/, '') === 'admin'
  }

  function watchHash(): void {
    window.addEventListener('hashchange', () => {
      available.value = readHash()
    })
  }

  /** Check the token with the API and keep it only if it is accepted. */
  async function signIn(value: string): Promise<boolean> {
    const candidate = value.trim()
    error.value = null
    if (!candidate) {
      error.value = 'Paste the admin token first.'
      return false
    }

    checking.value = true
    try {
      await checkAdminToken(candidate)
      token.value = candidate
      sessionStorage.setItem(TOKEN_KEY, candidate)
      return true
    } catch (cause) {
      error.value = describe(cause)
      return false
    } finally {
      checking.value = false
    }
  }

  function signOut(): void {
    token.value = ''
    error.value = null
    sessionStorage.removeItem(TOKEN_KEY)
  }

  return { token, available, active, checking, error, signIn, signOut, watchHash }
})

function describe(cause: unknown): string {
  const status = cause instanceof ApiError ? cause.status : null
  if (status === 401) return 'That admin token was rejected.'
  if (status === 503) return 'Naming is disabled on the server (no ADMIN_TOKEN set).'
  if (status !== null) return `The server answered ${status}.`
  return 'Could not reach the route server to check the token.'
}
