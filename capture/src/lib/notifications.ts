/**
 * The "Have we arrived?" system notification, for when the page is hidden.
 *
 * Shown through the service worker registration, never `new Notification()`:
 * the constructor has no action buttons, and on iOS it does not exist at all.
 * On iOS the whole `Notification` interface is missing from a Safari tab and
 * present only in an installed (home-screen) app on 16.4+, so every function
 * here checks support and quietly does nothing without it.
 *
 * Strings are passed in. This module knows nothing about languages.
 */
import { ARRIVAL_NOTIFICATION_TAG } from './swMessages'

export interface ArrivalNotificationText {
  title: string
  body: string
  stop: string
  stillRiding: string
}

/** Vibration for the notification itself; the in-app card buzzes separately. */
export const ARRIVAL_VIBRATION = [300, 120, 300, 120, 600]

/** Set once the permission prompt has been shown, so a dismissal is not re-asked. */
const ASKED_KEY = 'kombi-capture:notifications-asked'

// TypeScript's NotificationOptions predates action buttons, renotify and
// vibrate; the browsers that show persistent notifications all take them.
type PersistentNotificationOptions = NotificationOptions & {
  actions?: { action: string; title: string }[]
  renotify?: boolean
  vibrate?: number[]
}

export function notificationsSupported(): boolean {
  return (
    typeof window !== 'undefined' && 'Notification' in window && 'serviceWorker' in navigator
  )
}

/** True exactly once: supported, undecided, and never asked before. */
export function shouldAskForNotifications(): boolean {
  if (!notificationsSupported() || Notification.permission !== 'default') return false
  try {
    return localStorage.getItem(ASKED_KEY) === null
  } catch {
    return true
  }
}

/** Call from a user gesture. Not awaited: recording starts regardless of the answer. */
export function askForNotifications(): void {
  try {
    localStorage.setItem(ASKED_KEY, 'yes')
  } catch {
    // Not remembered; at worst the prompt is shown again next ride.
  }
  void Notification.requestPermission().catch(() => {})
}

async function registration(): Promise<ServiceWorkerRegistration | null> {
  if (!notificationsSupported()) return null
  try {
    return (await navigator.serviceWorker.getRegistration()) ?? null
  } catch {
    return null
  }
}

export async function showArrivalNotification(text: ArrivalNotificationText): Promise<void> {
  if (!notificationsSupported() || Notification.permission !== 'granted') return
  const reg = await registration()
  if (!reg) return

  const options: PersistentNotificationOptions = {
    tag: ARRIVAL_NOTIFICATION_TAG,
    body: text.body,
    icon: '/icon.svg',
    // Stay until answered; advisory, some Android skins ignore it.
    requireInteraction: true,
    renotify: true,
    vibrate: ARRIVAL_VIBRATION,
    data: { kind: 'arrival' },
    actions: [
      { action: 'stop', title: text.stop },
      { action: 'still-riding', title: text.stillRiding },
    ],
  }
  try {
    await reg.showNotification(text.title, options)
  } catch {
    // Refused (permission revoked mid-ride, or a platform without persistent
    // notifications). The in-app card is still there.
  }
}

export async function closeArrivalNotification(): Promise<void> {
  const reg = await registration()
  if (!reg) return
  try {
    const open = await reg.getNotifications({ tag: ARRIVAL_NOTIFICATION_TAG })
    for (const notification of open) notification.close()
  } catch {
    // Nothing to close.
  }
}
