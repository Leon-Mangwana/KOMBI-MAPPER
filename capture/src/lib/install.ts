/**
 * "Install app" support.
 *
 * Chrome on Android fires `beforeinstallprompt` when the PWA is installable.
 * Holding on to that event lets the welcome sheet offer an Install button that
 * raises the browser's own install dialog. Other browsers never fire it; for
 * them the sheet shows an "add to home screen" tip instead, unless the app is
 * already running installed.
 */
import { ref } from 'vue'

/** Not in TypeScript's DOM library yet. */
interface BeforeInstallPromptEvent extends Event {
  prompt(): Promise<void>
  readonly userChoice: Promise<{ outcome: 'accepted' | 'dismissed' }>
}

let deferred: BeforeInstallPromptEvent | null = null

/** True while the browser will show its install dialog on request. */
export const installAvailable = ref(false)

/** True when the app was opened from the home screen rather than a browser tab. */
export const isStandalone: boolean =
  (typeof window.matchMedia === 'function' &&
    window.matchMedia('(display-mode: standalone)').matches) ||
  (navigator as Navigator & { standalone?: boolean }).standalone === true

/** Call before mounting: the event can fire as soon as the page loads. */
export function listenForInstallPrompt(): void {
  window.addEventListener('beforeinstallprompt', (event) => {
    // Keep the browser's own mini-infobar away; the welcome sheet offers it.
    event.preventDefault()
    deferred = event as BeforeInstallPromptEvent
    installAvailable.value = true
  })
  window.addEventListener('appinstalled', () => {
    deferred = null
    installAvailable.value = false
  })
}

/** Show the browser's install dialog. The saved event can only be used once. */
export async function promptInstall(): Promise<void> {
  const event = deferred
  if (!event) return
  deferred = null
  installAvailable.value = false
  await event.prompt()
  await event.userChoice
}
