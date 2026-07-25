/**
 * Recording state machine: GPS watch -> IndexedDB -> API upload queue.
 *
 * The store holds only derived/display state. The trace itself lives in
 * IndexedDB from the moment it starts, so a killed tab loses at most the
 * in-flight position callback.
 */
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { UploadError, uploadTrace } from '@/lib/api'
import {
  appendPoint,
  createTrace,
  deleteTrace,
  deletePoints,
  getPoints,
  listTraces,
  patchTrace,
  type StoredTrace,
} from '@/lib/db'
import { ScreenWakeLock } from '@/lib/wakeLock'

export type RecordingStatus = 'idle' | 'recording'

export type GpsStatus =
  /** Not watching. */
  | 'idle'
  /** Watching, but no fix has arrived yet. */
  | 'acquiring'
  /** Receiving positions. */
  | 'fixed'
  /** The user refused the permission prompt (or the site is blocked). */
  | 'denied'
  /** The device has a GPS but cannot get a fix right now. */
  | 'unavailable'
  /** No Geolocation API at all (or a non-secure origin). */
  | 'unsupported'

export interface LivePosition {
  lon: number
  lat: number
  accuracyM: number | null
  speedMps: number | null
  timestamp: number
}

const CONTRIBUTOR_KEY = 'kombi-capture:contributor'

const GEOLOCATION_OPTIONS: PositionOptions = {
  enableHighAccuracy: true,
  // Never hand us a cached fix — every point must be a fresh observation.
  maximumAge: 0,
  timeout: 30_000,
}

export const useRecordingStore = defineStore('recording', () => {
  // ---- state -------------------------------------------------------------
  const status = ref<RecordingStatus>('idle')
  const gps = ref<GpsStatus>('idle')
  const gpsMessage = ref<string | null>(null)

  const contributor = ref(localStorage.getItem(CONTRIBUTOR_KEY) ?? '')
  const currentTraceId = ref<string | null>(null)
  const pointCount = ref(0)
  const startedAt = ref<number | null>(null)
  const now = ref(Date.now())

  /** Live [lon, lat] path of the current recording, for the map only. */
  const path = ref<[number, number][]>([])
  const lastPosition = ref<LivePosition | null>(null)

  const wakeLockActive = ref(false)
  const wakeLockSupported = ScreenWakeLock.supported

  const online = ref(navigator.onLine)
  const traces = ref<StoredTrace[]>([])
  const uploading = ref(false)
  const lastUploadError = ref<string | null>(null)

  // ---- non-reactive handles ---------------------------------------------
  let watchId: number | null = null
  let ticker: ReturnType<typeof setInterval> | null = null
  let flushing = false

  const wakeLock = new ScreenWakeLock(
    () => status.value === 'recording',
    (active) => {
      wakeLockActive.value = active
    },
  )

  // ---- getters -----------------------------------------------------------
  const isRecording = computed(() => status.value === 'recording')
  const elapsedMs = computed(() => (startedAt.value === null ? 0 : now.value - startedAt.value))
  // A denied permission deliberately does *not* disable Start: the user may
  // have just fixed it in browser settings, and pressing Start re-prompts.
  const canStart = computed(
    () => status.value === 'idle' && contributor.value.trim().length > 0,
  )
  const queued = computed(() =>
    traces.value.filter((t) => t.status === 'pending' || t.status === 'uploading'),
  )
  const failed = computed(() => traces.value.filter((t) => t.status === 'failed'))
  const uploadedCount = computed(() => traces.value.filter((t) => t.status === 'uploaded').length)
  const hasFix = computed(() => lastPosition.value !== null)

  // ---- helpers -----------------------------------------------------------
  async function refreshTraces(): Promise<void> {
    traces.value = await listTraces()
  }

  function startTicker(): void {
    stopTicker()
    now.value = Date.now()
    ticker = setInterval(() => {
      now.value = Date.now()
    }, 1000)
  }

  function stopTicker(): void {
    if (ticker !== null) {
      clearInterval(ticker)
      ticker = null
    }
  }

  function stopWatch(): void {
    if (watchId !== null) {
      navigator.geolocation.clearWatch(watchId)
      watchId = null
    }
  }

  // ---- actions -----------------------------------------------------------
  function setContributor(name: string): void {
    contributor.value = name
    localStorage.setItem(CONTRIBUTOR_KEY, name)
  }

  /**
   * Recover from a previous session and drain the upload queue.
   *
   * A trace still marked `recording` (or `uploading`) means the tab died while
   * it was in progress. The points already on disk are perfectly good, so the
   * trace is re-queued rather than discarded.
   */
  async function init(): Promise<void> {
    if (!('geolocation' in navigator)) {
      gps.value = 'unsupported'
      gpsMessage.value = window.isSecureContext
        ? 'This device has no Geolocation support.'
        : 'Geolocation needs HTTPS. Open this app over https:// or localhost.'
    } else {
      void probePermission()
    }

    const all = await listTraces()
    for (const trace of all) {
      if (trace.status !== 'recording' && trace.status !== 'uploading') continue
      if (trace.pointCount === 0) {
        await deleteTrace(trace.id)
        continue
      }
      await patchTrace(trace.id, {
        status: 'pending',
        endedAt: trace.endedAt ?? Date.now(),
      })
    }

    await refreshTraces()

    window.addEventListener('online', handleOnline)
    window.addEventListener('offline', handleOffline)

    void flushQueue()
  }

  /** Read the permission state up front so we can warn before the big button. */
  async function probePermission(): Promise<void> {
    if (!navigator.permissions?.query) return
    try {
      const result = await navigator.permissions.query({ name: 'geolocation' as PermissionName })
      if (result.state === 'denied' && status.value !== 'recording') {
        gps.value = 'denied'
        gpsMessage.value = 'Location is blocked for this site. Enable it in your browser settings.'
      }
      result.addEventListener('change', () => {
        if (result.state === 'granted' && gps.value === 'denied') {
          gps.value = 'idle'
          gpsMessage.value = null
        }
      })
    } catch {
      // Some browsers reject the geolocation permission name; ignore.
    }
  }

  async function start(): Promise<void> {
    if (status.value === 'recording') return
    if (!('geolocation' in navigator)) {
      gps.value = 'unsupported'
      return
    }
    if (!contributor.value.trim()) {
      gpsMessage.value = 'Enter your name before recording.'
      return
    }

    const trace = await createTrace(contributor.value.trim())
    currentTraceId.value = trace.id
    pointCount.value = 0
    path.value = []
    lastPosition.value = null
    startedAt.value = trace.startedAt
    status.value = 'recording'
    gps.value = 'acquiring'
    gpsMessage.value = null

    startTicker()
    void wakeLock.acquire()

    // The permission prompt is raised here, by watchPosition itself.
    watchId = navigator.geolocation.watchPosition(
      handlePosition,
      handleGeolocationError,
      GEOLOCATION_OPTIONS,
    )

    await refreshTraces()
  }

  function handlePosition(position: GeolocationPosition): void {
    const { latitude, longitude, accuracy, speed } = position.coords
    gps.value = 'fixed'
    gpsMessage.value = null

    lastPosition.value = {
      lon: longitude,
      lat: latitude,
      accuracyM: Number.isFinite(accuracy) ? accuracy : null,
      speedMps: speed !== null && Number.isFinite(speed) ? speed : null,
      timestamp: position.timestamp,
    }

    const traceId = currentTraceId.value
    if (traceId === null || status.value !== 'recording') return

    // Raw points go to disk unfiltered — smoothing and outlier rejection are
    // the pipeline's job, and it can only do them with the full observation.
    void appendPoint(traceId, {
      lat: latitude,
      lon: longitude,
      recordedAt: position.timestamp,
      accuracyM: Number.isFinite(accuracy) ? accuracy : null,
      speedMps: speed !== null && Number.isFinite(speed) ? speed : null,
    })
      .then((count) => {
        // Only reflect the count once the write actually landed.
        if (currentTraceId.value === traceId) {
          pointCount.value = count
          path.value = [...path.value, [longitude, latitude]]
        }
      })
      .catch((error: unknown) => {
        gpsMessage.value =
          error instanceof Error
            ? `Could not save point: ${error.message}`
            : 'Could not save point.'
      })
  }

  function handleGeolocationError(error: GeolocationPositionError): void {
    switch (error.code) {
      case error.PERMISSION_DENIED:
        gps.value = 'denied'
        gpsMessage.value = 'Location permission denied. Recording stopped.'
        // Nothing can be recorded without it, so end the session cleanly.
        void stop()
        break
      case error.POSITION_UNAVAILABLE:
        gps.value = status.value === 'recording' && hasFix.value ? 'fixed' : 'unavailable'
        gpsMessage.value = 'No GPS signal. Move somewhere with a clear view of the sky.'
        break
      case error.TIMEOUT:
        if (!hasFix.value) gps.value = 'acquiring'
        gpsMessage.value = 'Still waiting for a GPS fix…'
        break
      default:
        gpsMessage.value = error.message || 'Location error.'
    }
  }

  async function stop(): Promise<void> {
    if (status.value !== 'recording') return

    status.value = 'idle'
    stopWatch()
    stopTicker()
    await wakeLock.release()

    const traceId = currentTraceId.value
    currentTraceId.value = null
    startedAt.value = null
    if (gps.value !== 'denied') gps.value = 'idle'

    if (traceId !== null) {
      const trace = await patchTrace(traceId, { status: 'pending', endedAt: Date.now() })
      // A trace with no points is not a trace — the API rejects it anyway.
      if (trace && trace.pointCount === 0) {
        await deleteTrace(traceId)
        // Keep the denial message — it explains *why* there were no points.
        if (gps.value !== 'denied') {
          gpsMessage.value = 'No points were recorded, so nothing was saved.'
        }
      }
    }

    await refreshTraces()
    void flushQueue()
  }

  /**
   * Upload every pending trace, oldest first.
   *
   * Stops at the first retryable failure: if the network is down, the rest of
   * the queue will fail the same way, and `online` will bring us back.
   */
  async function flushQueue(): Promise<void> {
    if (flushing || !navigator.onLine) return
    flushing = true
    uploading.value = true

    try {
      const pending = (await listTraces())
        .filter((t) => t.status === 'pending')
        .sort((a, b) => a.startedAt - b.startedAt)

      for (const trace of pending) {
        // Never upload the recording that is still in progress.
        if (trace.id === currentTraceId.value) continue

        const points = await getPoints(trace.id)
        if (points.length === 0) {
          await deleteTrace(trace.id)
          continue
        }

        await patchTrace(trace.id, { status: 'uploading' })
        await refreshTraces()

        try {
          const created = await uploadTrace(trace.contributor, points)
          await patchTrace(trace.id, {
            status: 'uploaded',
            remoteId: created.id,
            lastError: null,
          })
          // The server owns the points now; reclaim the space, keep the receipt.
          await deletePoints(trace.id)
          lastUploadError.value = null
        } catch (error) {
          const message = error instanceof Error ? error.message : 'Upload failed.'
          const permanent = error instanceof UploadError && error.permanent
          await patchTrace(trace.id, {
            status: permanent ? 'failed' : 'pending',
            attempts: trace.attempts + 1,
            lastError: message,
          })
          lastUploadError.value = message
          if (!permanent) break // Offline or server trouble — wait for `online`.
        } finally {
          await refreshTraces()
        }
      }
    } finally {
      flushing = false
      uploading.value = false
      await refreshTraces()
    }
  }

  /** Put a permanently-failed trace back in the queue (after a manual fix). */
  async function retry(traceId: string): Promise<void> {
    await patchTrace(traceId, { status: 'pending', lastError: null })
    await refreshTraces()
    await flushQueue()
  }

  async function discard(traceId: string): Promise<void> {
    await deleteTrace(traceId)
    await refreshTraces()
  }

  function handleOnline(): void {
    online.value = true
    void flushQueue()
  }

  function handleOffline(): void {
    online.value = false
  }

  return {
    // state
    status,
    gps,
    gpsMessage,
    contributor,
    currentTraceId,
    pointCount,
    path,
    lastPosition,
    wakeLockActive,
    wakeLockSupported,
    online,
    traces,
    uploading,
    lastUploadError,
    // getters
    isRecording,
    elapsedMs,
    canStart,
    queued,
    failed,
    uploadedCount,
    hasFix,
    // actions
    init,
    start,
    stop,
    flushQueue,
    retry,
    discard,
    setContributor,
  }
})
