/**
 * What the service worker and the page say to each other.
 *
 * Imported by both `src/sw.ts` and the app, so it must stay free of DOM and
 * worker globals: types and constants only.
 */

/** One notification at a time: a newer "Have we arrived?" replaces the last. */
export const ARRIVAL_NOTIFICATION_TAG = 'arrival'

export type ArrivalAction =
  /** The rider pressed the notification's "Stop recording" button. */
  | 'stop'
  /** The rider pressed "Still riding". */
  | 'still-riding'
  /** The rider tapped the notification body: just bring the app up. */
  | 'open'

export interface ArrivalMessage {
  type: 'arrival'
  action: ArrivalAction
}
