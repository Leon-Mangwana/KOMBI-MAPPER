import { describe, expect, it } from 'vitest'

import {
  distanceM,
  MAX_SILENCE_MS,
  MIN_STATIONARY_FIXES,
  STATIONARY_MS,
  StationaryDetector,
  type StationaryFix,
} from './stationary'

// A kerb in central Harare.
const BASE = { lat: -17.8292, lon: 31.0522 }
const T0 = 1_700_000_000_000
const SECOND = 1_000
const MINUTE = 60_000

/**
 * A point `northM` / `eastM` metres from BASE. Test-only degree conversion;
 * the module under test only ever works in metres.
 */
function at(northM: number, eastM: number): { lat: number; lon: number } {
  const metresPerDegLat = 111_320
  const metresPerDegLon = 111_320 * Math.cos((BASE.lat * Math.PI) / 180)
  return { lat: BASE.lat + northM / metresPerDegLat, lon: BASE.lon + eastM / metresPerDegLon }
}

function fix(
  timestamp: number,
  northM: number,
  eastM: number,
  accuracyM: number | null = 8,
): StationaryFix {
  return { timestamp, ...at(northM, eastM), accuracyM }
}

/** Deterministic ±8 m wander around a centre, as a parked phone reports it. */
const JITTER: [number, number][] = [
  [0, 0],
  [6, -3],
  [-8, 2],
  [3, 7],
  [-2, -8],
  [8, 4],
]

/** Feed 1 Hz fixes from `from` for `seconds`, wandering around (`northM`, `eastM`). */
function feedStill(
  detector: StationaryDetector,
  from: number,
  seconds: number,
  northM = 0,
  eastM = 0,
): void {
  for (let i = 0; i < seconds; i += 1) {
    const [dn, de] = JITTER[i % JITTER.length]
    detector.push(fix(from + i * SECOND, northM + dn, eastM + de))
  }
}

describe('distanceM', () => {
  it('is zero between identical points', () => {
    expect(distanceM(BASE, BASE)).toBe(0)
  })

  it('is about 111 km for one degree of latitude', () => {
    expect(distanceM(BASE, { lat: BASE.lat + 1, lon: BASE.lon })).toBeCloseTo(111_195, -2)
  })

  it('measures a 40 m offset as 40 m', () => {
    expect(distanceM(BASE, at(40, 0))).toBeCloseTo(40, 0)
    expect(distanceM(BASE, at(0, 40))).toBeCloseTo(40, 0)
  })
})

describe('StationaryDetector', () => {
  it('has no verdict before any fix', () => {
    const detector = new StationaryDetector()
    expect(detector.evaluate(T0)).toEqual({ stationary: false, since: null })
  })

  it('reports stationary once the still period reaches the threshold', () => {
    const detector = new StationaryDetector()
    feedStill(detector, T0, 5 * 60)

    expect(detector.evaluate(T0 + STATIONARY_MS - SECOND)).toEqual({
      stationary: false,
      since: T0,
    })
    expect(detector.evaluate(T0 + STATIONARY_MS)).toEqual({ stationary: true, since: T0 })
  })

  it('ignores a single outlier fix', () => {
    const detector = new StationaryDetector()
    feedStill(detector, T0, 150)
    detector.push(fix(T0 + 150 * SECOND, 80, 0, 10))
    feedStill(detector, T0 + 151 * SECOND, 150)

    expect(detector.evaluate(T0 + STATIONARY_MS)).toEqual({ stationary: true, since: T0 })
  })

  it('moves the anchor after three consecutive fixes outside the radius', () => {
    const detector = new StationaryDetector()
    feedStill(detector, T0, 150)
    const movedAt = T0 + 150 * SECOND
    detector.push(fix(movedAt, 100, 0))
    detector.push(fix(movedAt + SECOND, 102, 1))
    detector.push(fix(movedAt + 2 * SECOND, 99, -2))
    feedStill(detector, movedAt + 3 * SECOND, 5 * 60, 100, 0)

    // Not at the old threshold: the clock restarted at the first moved fix.
    expect(detector.evaluate(T0 + STATIONARY_MS)).toEqual({ stationary: false, since: movedAt })
    expect(detector.evaluate(movedAt + STATIONARY_MS)).toEqual({ stationary: true, since: movedAt })
  })

  it('ignores fixes with poor accuracy entirely', () => {
    const detector = new StationaryDetector()
    feedStill(detector, T0, 5 * 60)
    for (let i = 0; i < 20; i += 1) {
      detector.push(fix(T0 + (300 + i) * SECOND, 60, 0, 120))
    }

    // Neither counted as movement nor as coverage: the anchor is untouched.
    expect(detector.evaluate(T0 + STATIONARY_MS)).toEqual({ stationary: true, since: T0 })
  })

  it('needs coverage, not just two endpoints', () => {
    const detector = new StationaryDetector()
    detector.push(fix(T0, 0, 0))
    detector.push(fix(T0 + STATIONARY_MS, 1, 1))

    expect(detector.evaluate(T0 + STATIONARY_MS)).toEqual({ stationary: false, since: T0 })
    expect(MIN_STATIONARY_FIXES).toBeGreaterThan(2)
  })

  it('does not call a long silence stationary', () => {
    const detector = new StationaryDetector()
    feedStill(detector, T0, 2 * 60)

    // Fixes stopped at 2:00; by 5:00 nothing has been heard for 3 minutes.
    expect(detector.evaluate(T0 + STATIONARY_MS)).toEqual({ stationary: false, since: T0 })
    expect(STATIONARY_MS - 2 * MINUTE).toBeGreaterThan(MAX_SILENCE_MS)
  })

  it('reaches the verdict on the clock when fixes stopped recently', () => {
    const detector = new StationaryDetector()
    feedStill(detector, T0, 4 * 60)

    // Fixes stopped at 4:00 (phone under a seat); the 1-second ticker asks at 5:00.
    expect(detector.evaluate(T0 + STATIONARY_MS)).toEqual({ stationary: true, since: T0 })
  })

  it('snoozes until the kombi has moved and stopped again', () => {
    const detector = new StationaryDetector()
    feedStill(detector, T0, 5 * 60)
    expect(detector.evaluate(T0 + STATIONARY_MS).stationary).toBe(true)

    detector.snooze()
    feedStill(detector, T0 + 5 * 60 * SECOND, 60)
    expect(detector.evaluate(T0 + 6 * MINUTE)).toEqual({ stationary: false, since: T0 })

    const movedAt = T0 + 6 * MINUTE
    detector.push(fix(movedAt, 100, 0))
    detector.push(fix(movedAt + SECOND, 101, 0))
    detector.push(fix(movedAt + 2 * SECOND, 102, 0))
    feedStill(detector, movedAt + 3 * SECOND, 5 * 60, 100, 0)

    expect(detector.evaluate(movedAt + STATIONARY_MS)).toEqual({ stationary: true, since: movedAt })
  })

  it('never calls a slow creep stationary', () => {
    const detector = new StationaryDetector()
    // 5 m every 10 s in one direction: 0.5 m/s, a crawling queue.
    for (let i = 0; i < 120; i += 1) {
      detector.push(fix(T0 + i * 10 * SECOND, i * 5, 0))
    }

    for (let minute = 5; minute <= 20; minute += 1) {
      expect(detector.evaluate(T0 + minute * MINUTE).stationary).toBe(false)
    }
  })

  it('forgets everything on reset', () => {
    const detector = new StationaryDetector()
    feedStill(detector, T0, 5 * 60)
    detector.reset()

    expect(detector.evaluate(T0 + STATIONARY_MS)).toEqual({ stationary: false, since: null })
  })

  it('fires on the first fix after a page freeze if the kombi did not move', () => {
    const detector = new StationaryDetector()
    feedStill(detector, T0, 3 * 60)
    // The browser froze the page: no fixes for six minutes.
    const resumedAt = T0 + 9 * MINUTE
    detector.push(fix(resumedAt, 5, -3))

    expect(detector.evaluate(resumedAt)).toEqual({ stationary: true, since: T0 })
  })
})
