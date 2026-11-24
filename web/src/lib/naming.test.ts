import { describe, expect, it } from 'vitest'

import type { RouteProperties } from './api'
import { nextUnnamedId } from './naming'

function route(id: number, short: string | null = null, long: string | null = null): RouteProperties {
  return { id, short_name: short, long_name: long, length_m: null, stop_count: 0, trace_count: 1 }
}

// The panel's order: named routes first, then the unnamed ones by id.
const ordered = [route(5, '4', 'CBD - Mbare'), route(9, null, 'Copacabana'), route(2), route(7), route(11)]

describe('nextUnnamedId', () => {
  it('is null with no routes', () => {
    expect(nextUnnamedId([], null)).toBeNull()
  })

  it('is null once every route is named', () => {
    expect(nextUnnamedId([route(1, 'A'), route(2, null, 'B')], null)).toBeNull()
    expect(nextUnnamedId([route(1, 'A'), route(2, null, 'B')], 1)).toBeNull()
  })

  it('starts from the first unnamed route when nothing is selected', () => {
    expect(nextUnnamedId(ordered, null)).toBe(2)
  })

  it('moves to the next unnamed route in list order', () => {
    expect(nextUnnamedId(ordered, 2)).toBe(7)
    expect(nextUnnamedId(ordered, 7)).toBe(11)
  })

  it('wraps round after the last unnamed route', () => {
    expect(nextUnnamedId(ordered, 11)).toBe(2)
  })

  it('starts from the first unnamed route after a named one', () => {
    expect(nextUnnamedId(ordered, 5)).toBe(2)
    expect(nextUnnamedId(ordered, 9)).toBe(2)
  })

  it('starts from the first unnamed route after an id that is not listed', () => {
    expect(nextUnnamedId(ordered, 404)).toBe(2)
  })

  it('returns the only unnamed route however it is reached', () => {
    const one = [route(1, 'A'), route(3)]
    expect(nextUnnamedId(one, null)).toBe(3)
    expect(nextUnnamedId(one, 3)).toBe(3)
    expect(nextUnnamedId(one, 1)).toBe(3)
  })
})
