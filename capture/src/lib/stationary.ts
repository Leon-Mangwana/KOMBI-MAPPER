/**
 * Has the kombi stopped for good?
 *
 * Riders forget they are recording after they get off, so the app watches for
 * a vehicle that has sat still for a while and asks. This is the rule for
 * "sat still", kept pure (no Vue, no DOM) so it can be unit-tested with fake
 * fixes: the store feeds it positions and asks for a verdict.
 *
 * Raw GPS on a parked vehicle wanders by several metres, and a single bad fix
 * can land anywhere, so the rule is deliberately dull: an *anchor* fix, a
 * radius around it, and movement only counts once a few fixes in a row have
 * left the circle. The pipeline's stop detector (`pipeline/stops.py`) does the
 * same job offline on smoothed points; the radius here matches the stop
 * clustering radius it feeds (`cluster.STOP_EPS_M`).
 *
 * All distances are metres. Never degrees — see ../../../CLAUDE.md.
 */

export interface StationaryFix {
  /** Epoch ms, from `GeolocationPosition.timestamp`. */
  timestamp: number
  lat: number
  lon: number
  accuracyM: number | null
}

export interface StationaryVerdict {
  stationary: boolean
  /**
   * When the current still period began (the anchor's timestamp), or null
   * before any usable fix. Reported even when not (yet) stationary, so a
   * caller can tell "the same stop, still waiting" from "a new stop".
   */
  since: number | null
}

/** How long the kombi must sit still before the rider is asked. */
export const STATIONARY_MS = 5 * 60_000

/** Parked-vehicle GPS wander. Matches the pipeline's stop clustering radius. */
export const STATIONARY_RADIUS_M = 40

/** A fix worse than this says nothing about movement. Matches `clean.MAX_ACCURACY_M`. */
export const MAX_FIX_ACCURACY_M = 50

/** Coverage: the still period must be backed by this many good fixes, not two endpoints. */
export const MIN_STATIONARY_FIXES = 10

/** No good fix for longer than this and the answer is "unknown", not "stationary". */
export const MAX_SILENCE_MS = 2 * 60_000

/** Fixes in a row outside the radius before the anchor moves. One outlier must not restart the clock. */
export const MOVE_CONFIRM_FIXES = 3

const EARTH_RADIUS_M = 6_371_008.8

/** Great-circle distance in metres (haversine). */
export function distanceM(
  a: { lat: number; lon: number },
  b: { lat: number; lon: number },
): number {
  const toRad = Math.PI / 180
  const dLat = (b.lat - a.lat) * toRad
  const dLon = (b.lon - a.lon) * toRad
  const sinLat = Math.sin(dLat / 2)
  const sinLon = Math.sin(dLon / 2)
  const h =
    sinLat * sinLat + Math.cos(a.lat * toRad) * Math.cos(b.lat * toRad) * sinLon * sinLon
  return 2 * EARTH_RADIUS_M * Math.asin(Math.min(1, Math.sqrt(h)))
}

export class StationaryDetector {
  private anchor: StationaryFix | null = null
  /** Good fixes within the radius of the anchor, the anchor itself included. */
  private fixCount = 0
  private lastFixAt: number | null = null
  /** The current run of consecutive fixes beyond the radius. */
  private outside: StationaryFix[] = []
  /** "Still riding": no verdict for this stop until the kombi has moved. */
  private snoozed = false

  /** Forget everything. Start and Stop. */
  reset(): void {
    this.anchor = null
    this.fixCount = 0
    this.lastFixAt = null
    this.outside = []
    this.snoozed = false
  }

  push(fix: StationaryFix): void {
    // A null accuracy is a browser oddity, not bad data: the spec makes the
    // field mandatory, so take the fix rather than throw it away.
    if (fix.accuracyM !== null && fix.accuracyM > MAX_FIX_ACCURACY_M) return

    if (this.anchor === null) {
      this.anchor = fix
      this.fixCount = 1
      this.lastFixAt = fix.timestamp
      return
    }

    if (distanceM(this.anchor, fix) <= STATIONARY_RADIUS_M) {
      this.outside = []
      this.fixCount += 1
      this.lastFixAt = fix.timestamp
      return
    }

    this.outside.push(fix)
    if (this.outside.length < MOVE_CONFIRM_FIXES) return

    // Movement confirmed. The first fix that left the circle starts the next
    // candidate stop; the rest are measured against it like any other fix.
    const moved = this.outside
    this.reset()
    for (const next of moved) this.push(next)
  }

  evaluate(now: number): StationaryVerdict {
    if (this.anchor === null || this.lastFixAt === null) {
      return { stationary: false, since: null }
    }
    const stationary =
      !this.snoozed &&
      this.fixCount >= MIN_STATIONARY_FIXES &&
      now - this.anchor.timestamp >= STATIONARY_MS &&
      now - this.lastFixAt <= MAX_SILENCE_MS
    return { stationary, since: this.anchor.timestamp }
  }

  /** The rider said they are still riding: ask again only after the next stop. */
  snooze(): void {
    this.snoozed = true
  }
}
