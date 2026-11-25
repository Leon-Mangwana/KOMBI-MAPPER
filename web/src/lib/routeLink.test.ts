import { describe, expect, it } from 'vitest'

import { routeIdFromSearch, searchWithRoute, shareUrl } from './routeLink'

describe('routeIdFromSearch', () => {
  it('reads a positive integer route id', () => {
    expect(routeIdFromSearch('?route=12')).toBe(12)
    expect(routeIdFromSearch('?foo=1&route=7')).toBe(7)
  })

  it('is null without the parameter', () => {
    expect(routeIdFromSearch('')).toBeNull()
    expect(routeIdFromSearch('?foo=1')).toBeNull()
  })

  it('rejects anything that is not a positive integer', () => {
    for (const bad of ['', 'abc', '0', '-1', '1.5', '1e3', ' 3', '12abc']) {
      expect(routeIdFromSearch(`?route=${encodeURIComponent(bad)}`)).toBeNull()
    }
  })
})

describe('searchWithRoute', () => {
  it('adds the route to an empty query', () => {
    expect(searchWithRoute('', 12)).toBe('?route=12')
  })

  it('replaces an existing route and keeps other parameters', () => {
    expect(searchWithRoute('?route=3&foo=bar', 12)).toBe('?route=12&foo=bar')
  })

  it('removes the route, leaving other parameters', () => {
    expect(searchWithRoute('?route=3&foo=bar', null)).toBe('?foo=bar')
  })

  it('is empty when nothing is left, so the address has no trailing question mark', () => {
    expect(searchWithRoute('?route=3', null)).toBe('')
    expect(searchWithRoute('', null)).toBe('')
  })
})

describe('shareUrl', () => {
  it('is origin, path and the route only', () => {
    expect(shareUrl('https://example.test', '/', 12)).toBe('https://example.test/?route=12')
  })
})
