/// <reference lib="webworker" />
/**
 * The service worker.
 *
 * Hand-written (vite-plugin-pwa `injectManifest`) only because the
 * "Have we arrived?" notification needs a `notificationclick` handler, and a
 * generated worker cannot have one. Everything above that handler reproduces
 * what the plugin generated before: the precached app shell, `skipWaiting` +
 * `clientsClaim` (which is what `registerType: 'autoUpdate'` means, and what
 * `src/lib/updates.ts` relies on to notice a new build), and the navigation
 * fallback with `/traces` kept away from the cache — an upload must reach the
 * network or fail loudly.
 *
 * No fetch handlers of our own: API calls go straight to the network.
 */
import { clientsClaim } from 'workbox-core'
import { cleanupOutdatedCaches, createHandlerBoundToURL, precacheAndRoute } from 'workbox-precaching'
import { NavigationRoute, registerRoute } from 'workbox-routing'

import type { ArrivalAction, ArrivalMessage } from './lib/swMessages'

declare let self: ServiceWorkerGlobalScope

self.skipWaiting()
clientsClaim()

precacheAndRoute(self.__WB_MANIFEST)
cleanupOutdatedCaches()

registerRoute(
  new NavigationRoute(createHandlerBoundToURL('index.html'), {
    allowlist: [/^\/$/],
    denylist: [/^\/traces/],
  }),
)

/**
 * A button on the arrival notification, or a tap on its body, is relayed to
 * the open page, which holds the recording. Stop and a body tap bring the app
 * to the front so the rider sees the receipt; "Still riding" stays out of the
 * way. If no page is open the ride is not being recorded any more either, so
 * opening one is just a convenience.
 */
self.addEventListener('notificationclick', (event) => {
  const data = event.notification.data as { kind?: string } | undefined
  if (data?.kind !== 'arrival') return
  event.notification.close()

  const action: ArrivalAction =
    event.action === 'stop' ? 'stop' : event.action === 'still-riding' ? 'still-riding' : 'open'
  const message: ArrivalMessage = { type: 'arrival', action }

  event.waitUntil(
    (async () => {
      const clients = await self.clients.matchAll({ type: 'window', includeUncontrolled: true })
      const client = clients[0]
      if (client) {
        client.postMessage(message)
        if (action !== 'still-riding') await client.focus()
        return
      }
      const opened = await self.clients.openWindow('/')
      opened?.postMessage(message)
    })(),
  )
})
