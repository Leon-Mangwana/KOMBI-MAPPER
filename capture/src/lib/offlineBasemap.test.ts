import { describe, expect, it } from 'vitest'

import { memorySource, sameArchive } from './offlineBasemap'

function bytes(...values: number[]): ArrayBuffer {
  return Uint8Array.from(values).buffer
}

describe('memorySource', () => {
  it('is keyed by the URL the style uses, so the protocol finds it', () => {
    expect(memorySource('/harare.pmtiles', bytes()).getKey()).toBe('/harare.pmtiles')
  })

  it('returns the requested slice', async () => {
    const source = memorySource('/a.pmtiles', bytes(10, 11, 12, 13, 14, 15))
    const { data } = await source.getBytes(2, 3)
    expect(Array.from(new Uint8Array(data))).toEqual([12, 13, 14])
  })

  it('clips a slice that runs past the end, as a server would', async () => {
    const source = memorySource('/a.pmtiles', bytes(1, 2, 3))
    const { data } = await source.getBytes(2, 10)
    expect(Array.from(new Uint8Array(data))).toEqual([3])
  })

  it('does not share memory with the archive buffer', async () => {
    const buffer = bytes(1, 2, 3)
    const { data } = await memorySource('/a.pmtiles', buffer).getBytes(0, 3)
    new Uint8Array(data)[0] = 99
    expect(new Uint8Array(buffer)[0]).toBe(1)
  })
})

describe('sameArchive', () => {
  const h = (init: Record<string, string>): Headers => new Headers(init)

  it('compares strong ETags when both sides have one', () => {
    expect(sameArchive(h({ etag: '"abc"' }), h({ etag: '"abc"' }))).toBe(true)
    expect(sameArchive(h({ etag: '"abc"' }), h({ etag: '"def"' }))).toBe(false)
  })

  it('ignores a weak ETag and falls back to the length', () => {
    expect(
      sameArchive(
        h({ etag: 'W/"abc"', 'content-length': '100' }),
        h({ etag: 'W/"def"', 'content-length': '100' }),
      ),
    ).toBe(true)
    expect(
      sameArchive(
        h({ etag: 'W/"abc"', 'content-length': '100' }),
        h({ etag: '"abc"', 'content-length': '101' }),
      ),
    ).toBe(false)
  })

  it('uses the length when only one side has an ETag', () => {
    expect(sameArchive(h({ 'content-length': '5' }), h({ etag: '"x"', 'content-length': '5' }))).toBe(
      true,
    )
    expect(sameArchive(h({ 'content-length': '5' }), h({ etag: '"x"', 'content-length': '6' }))).toBe(
      false,
    )
  })

  it('assumes the same when there is nothing to compare', () => {
    expect(sameArchive(h({}), h({}))).toBe(true)
    expect(sameArchive(h({ etag: '"x"' }), h({}))).toBe(true)
  })
})
