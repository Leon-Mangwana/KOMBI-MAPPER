/**
 * Noticing that a new version of the app has been deployed.
 *
 * The PWA keeps a copy of itself on the phone so it opens offline, and the
 * service worker vite-plugin-pwa generates (registerType 'autoUpdate', so
 * skipWaiting + clientsClaim) fetches a new build in the background and takes
 * over the open page. The page, though, is still running the old code it
 * loaded with: without this, a rider only sees a release the *second* time
 * they open the app, and never while it stays open.
 *
 * So when a new worker takes control, `updateReady` turns on and the app
 * offers a one-tap reload. It never reloads by itself: a reload in the middle
 * of a ride would end the ride there.
 */
import { ref } from 'vue'

/** A newer build has taken over; reloading the page shows it. */
export const updateReady = ref(false)

/** How often a page left open asks the server for a newer service worker. */
const CHECK_EVERY_MS = 60 * 60 * 1000

export function watchForUpdates(): void {
  if (!('serviceWorker' in navigator)) return

  // A page that loaded with no controller is a first visit: the worker
  // taking control afterwards is the install, not an update.
  let controlled = navigator.serviceWorker.controller !== null
  navigator.serviceWorker.addEventListener('controllerchange', () => {
    if (controlled) updateReady.value = true
    controlled = true
  })

  // Browsers check for a new sw.js on navigation, which a page that stays
  // open for a whole day of rides never does. Ask now and then, and whenever
  // the app comes back to the foreground.
  const check = (): void => {
    void navigator.serviceWorker
      .getRegistration()
      .then((registration) => registration?.update())
      .catch(() => {
        // Offline or the server is unreachable: try again next time.
      })
  }
  setInterval(check, CHECK_EVERY_MS)
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') check()
  })
}

export function applyUpdate(): void {
  window.location.reload()
}
