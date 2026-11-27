/**
 * Keeping the basemap on the phone.
 *
 * The service worker precaches the app shell, so the app opens offline; but
 * the map underneath it came over the network one byte range at a time, and a
 * rider with no data saw the blue dot on a blank background. The archive is a
 * single file of a few megabytes, so it is kept whole in Cache Storage: saved
 * from the page the first time the app runs online, read into memory on later
 * launches, and from then on every tile is a slice of that one buffer, which
 * is faster than the network even with signal.
 *
 * Deliberately not the service worker's job. In the precache, a dropped 6 MB
 * download would fail the whole install, and the shell would not be offline
 * either; and serving byte ranges out of a cached response re-reads the whole
 * file for every tile. From the page, a failed download simply retries on the
 * next launch, and the app works exactly as before until it succeeds.
 *
 * Refreshes: once per launch, when online, a HEAD request is compared with the
 * saved copy's ETag (or length) and a changed archive is downloaded again in
 * the background; the next launch uses it. The copy in memory is never swapped
 * mid-session, so the directories pmtiles has read always match the tiles.
 *
 * Pure parts (the memory source, the comparison) are at the top and unit
 * tested; the Cache Storage calls are below and guarded, because `caches` is
 * missing on an insecure origin and can throw in private windows.
 */
import type { RangeResponse, Source } from 'pmtiles'

const CACHE_NAME = 'kombi-capture:basemap:v1'

/** A pmtiles `Source` over bytes already in memory, keyed by the archive's URL. */
export function memorySource(key: string, buffer: ArrayBuffer): Source {
  return {
    getKey: () => key,
    getBytes: async (offset: number, length: number): Promise<RangeResponse> => ({
      data: buffer.slice(offset, offset + length),
    }),
  }
}

function strongEtag(headers: Headers): string | null {
  const etag = headers.get('etag')
  if (!etag) return null
  // A weak validator says "equivalent", not "identical bytes".
  return etag.startsWith('W/') ? null : etag
}

/**
 * Whether the archive on the server is the one saved, judged from headers
 * alone: matching strong ETags, else matching lengths, else — with nothing to
 * compare — assumed the same, so a server without either never causes a
 * download on every launch.
 */
export function sameArchive(saved: Headers, live: Headers): boolean {
  const savedTag = strongEtag(saved)
  const liveTag = strongEtag(live)
  if (savedTag && liveTag) return savedTag === liveTag

  const savedLength = saved.get('content-length')
  const liveLength = live.get('content-length')
  if (savedLength && liveLength) return savedLength === liveLength

  return true
}

function storage(): CacheStorage | null {
  try {
    return typeof caches === 'undefined' ? null : caches
  } catch {
    return null
  }
}

/** The saved archive's bytes, or null when there is none (or no Cache Storage). */
export async function loadSavedArchive(url: string): Promise<ArrayBuffer | null> {
  const store = storage()
  if (!store) return null
  try {
    const cache = await store.open(CACHE_NAME)
    const saved = await cache.match(url)
    return saved ? await saved.arrayBuffer() : null
  } catch {
    return null
  }
}

/** Download the whole archive and keep it. False when that could not be done. */
async function saveArchive(url: string): Promise<boolean> {
  const store = storage()
  if (!store) return false
  try {
    const cache = await store.open(CACHE_NAME)
    // cache.add rejects on a non-2xx response, so a 404 is never kept.
    await cache.add(url)
    return true
  } catch {
    return false
  }
}

/**
 * Save the archive if it is not saved yet, or save it again if the server's
 * copy has changed. Waits for a connection first, and never throws: the map
 * is already drawing from the network or the saved copy either way.
 */
export async function keepArchiveOffline(url: string): Promise<void> {
  const store = storage()
  if (!store) return
  await whenOnline()
  try {
    const cache = await store.open(CACHE_NAME)
    const saved = await cache.match(url)
    if (!saved) {
      await saveArchive(url)
      return
    }
    const live = await fetch(url, { method: 'HEAD', cache: 'no-cache' })
    if (live.ok && !sameArchive(saved.headers, live.headers)) await saveArchive(url)
  } catch {
    // Offline after all, or storage refused: next launch tries again.
  }
}

function whenOnline(): Promise<void> {
  if (navigator.onLine) return Promise.resolve()
  return new Promise((resolve) => {
    window.addEventListener('online', () => resolve(), { once: true })
  })
}
