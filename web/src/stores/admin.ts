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
 * **The token lives in `sessionStorage`.** It is a shared secret in a browser
 * either way, but sessionStorage is scoped to the tab and cleared when it
 * closes, so a borrowed or shared machine does not keep operator access alive
 * indefinitely. The cost is retyping it in a new tab, which an operator does
 * once per naming session.
 */
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

const TOKEN_KEY = 'kombi-web:admin-token'

export const useAdminStore = defineStore('admin', () => {
  const token = ref(sessionStorage.getItem(TOKEN_KEY) ?? '')

  /** Whether the naming controls should be offered at all. */
  const available = ref(readHash())

  const active = computed(() => available.value && token.value.length > 0)

  function readHash(): boolean {
    return window.location.hash.replace(/^#/, '') === 'admin'
  }

  function watchHash(): void {
    window.addEventListener('hashchange', () => {
      available.value = readHash()
    })
  }

  function signIn(value: string): void {
    token.value = value.trim()
    sessionStorage.setItem(TOKEN_KEY, token.value)
  }

  function signOut(): void {
    token.value = ''
    sessionStorage.removeItem(TOKEN_KEY)
  }

  return { token, available, active, signIn, signOut, watchHash }
})
