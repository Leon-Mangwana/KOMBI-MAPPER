/**
 * Recording state machine: GPS watch -> IndexedDB -> API upload queue.
 *
 * The store holds only derived/display state. The trace itself lives in
 * IndexedDB from the moment it starts, so a killed tab loses at most the
 * in-flight position callback.
 *
 * Nothing here is user-facing text. The store reports what happened as codes
 * (`notice.kind`, `uploadIssue.kind`, `receipt.phase`, `uploadPhase`), and the
 * components turn those into words from `src/i18n`.
 */
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { uploadTrace } from '@/lib/api'
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
import { KeepAlive, type KeepAliveLabels } from '@/lib/keepAlive'
import { StationaryDetector } from '@/lib/stationary'
import { classifyUploadError, retryDelayMs, type UploadIssue } from '@/lib/uploadIssue'
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

/** Something about GPS or recording the rider should know. */
export type NoticeKind =
  /** No Geolocation API in this browser. */
  | 'unsupported'
  /** Geolocation needs HTTPS and the page is not on it. */
  | 'insecure'
  /** The site's location permission is already blocked. */
  | 'blocked'
  /** The rider refused the permission prompt; recording stopped. */
  | 'denied'
  /** The device cannot get a fix right now (recording continues). */
  | 'no-signal'
  /** A position request timed out before any fix arrived. */
  | 'waiting'
  /** A point could not be written to IndexedDB. */
  | 'save-failed'
  /** Any other geolocation error. */
  | 'location-error'
  /** Stop was pressed before a single point was recorded. */
  | 'empty-ride'
  /** Start was pressed without a nickname. */
  | 'needs-nickname'
  /** The browser froze the page in the background; the GPS watch was restarted. */
  | 'paused-in-background'

export interface Notice {
  kind: NoticeKind
  /** Technical text from the browser, shown small, untranslated. */
  detail?: string
  /** How long recording was paused. Only with 'paused-in-background'. */
  gapMs?: number
}

/** The rider is being asked whether the ride is over. */
export interface ArrivalPrompt {
  /** When the kombi stopped moving (epoch ms). */
  since: number
  /** When the question was put (epoch ms). */
  askedAt: number
}

/** Confirmation of the ride that was just stopped. */
export interface RideReceipt {
  traceId: string
  pointCount: number
  durationMs: number
  phase: 'saved' | 'uploaded'
  /** The route the rider named for this ride, once they have. */
  routeHint: string | null
}

/** What the upload queue is doing, for the status line. */
export type UploadPhase =
  /** Nothing waiting. */
  | 'idle'
  /** A request is in flight. */
  | 'uploading'
  /** Rides are waiting and the device is offline. */
  | 'offline'
  /** The last attempt failed; an automatic retry is scheduled. */
  | 'retrying'
  /** Rides are waiting and nothing has gone wrong yet. */
  | 'waiting'

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

/** Show "still uploading, the server may be waking up" after this long. */
const SLOW_UPLOAD_MS = 10_000

/** How long "Uploaded. Thank you!" stays up. */
const RECEIPT_TTL_MS = 8_000

/**
 * How long after Stop the upload waits for the rider to name the route.
 *
 * The hint travels in the same request as the points, so the upload has to
 * hold while the field is on screen. It does not hold forever: a rider who
 * walks off without answering still gets their ride uploaded, and the hint is
 * only ever a bonus.
 */
const HINT_GRACE_MS = 120_000

/**
 * A gap this long since the last fix, found on returning to the foreground,
 * means the browser paused the page rather than the GPS going quiet.
 */
const RESUME_GAP_MS = 60_000

/** Notices about the ride in progress; they mean nothing once it has stopped. */
const TRANSIENT_NOTICES: ReadonlySet<NoticeKind> = new Set<NoticeKind>([
  'no-signal',
  'waiting',
  'location-error',
  'save-failed',
  'paused-in-background',
])

/**
 * Notices a fresh fix answers. The pause notice is deliberately not one: the
 * trace has a hole in it, and the rider should see that even once fixes are
 * arriving again.
 */
const FIX_CLEARED_NOTICES: ReadonlySet<NoticeKind> = new Set<NoticeKind>([
  'no-signal',
  'waiting',
  'location-error',
  'save-failed',
])

function readContributor(): string {
  try {
    return localStorage.getItem(CONTRIBUTOR_KEY) ?? ''
  } catch {
    return ''
  }
}

export const useRecordingStore = defineStore('recording', () => {
  // ---- state -------------------------------------------------------------
  const status = ref<RecordingStatus>('idle')
  const gps = ref<GpsStatus>('idle')
  const notice = ref<Notice | null>(null)

  const contributor = ref(readContributor())
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
  /** Why the last upload attempt failed, when a retry is pending. */
  const uploadIssue = ref<UploadIssue | null>(null)
  /** True once the request in flight has taken longer than SLOW_UPLOAD_MS. */
  const uploadSlow = ref(false)
  const receipt = ref<RideReceipt | null>(null)
  /** Epoch ms of the next automatic retry, or null when none is scheduled. */
  const nextRetryAt = ref<number | null>(null)
  /**
   * The ride whose upload is held while the rider is asked which route it
   * was. Null once they answer, skip, start another ride, put the phone away
   * or the grace period runs out.
   */
  const awaitingHint = ref<string | null>(null)
  /** The "Have we arrived?" question, while it is open. */
  const arrival = ref<ArrivalPrompt | null>(null)
  /** True while the background keep-alive tone is playing. */
  const keepAliveActive = ref(false)
  /** The location permission is already granted, so Start will not prompt for it. */
  const locationGranted = ref(false)

  // ---- non-reactive handles ---------------------------------------------
  let watchId: number | null = null
  let ticker: ReturnType<typeof setInterval> | null = null
  let flushing = false
  /** A flush was asked for while one was running; run again when it ends. */
  let rerunRequested = false
  /** Guards the gap between pressing Start and `status` becoming 'recording'. */
  let starting = false
  let initialised = false
  let retryAttempt = 0
  let retryTimer: ReturnType<typeof setTimeout> | null = null
  let receiptTimer: ReturnType<typeof setTimeout> | null = null
  let slowTimer: ReturnType<typeof setTimeout> | null = null
  let hintTimer: ReturnType<typeof setTimeout> | null = null

  const detector = new StationaryDetector()
  // The Android media control's Stop ends the ride like the button does.
  const keepAlive = new KeepAlive(() => {
    void stop()
  })

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

  // An issue is only ever shown while something is still queued, so a stale
  // one cannot outlive the rides it was about.
  const uploadPhase = computed<UploadPhase>(() => {
    if (uploading.value) return 'uploading'
    if (queued.value.length === 0) return 'idle'
    if (!online.value) return 'offline'
    return uploadIssue.value ? 'retrying' : 'waiting'
  })

  /** How long the kombi has been still, while the arrival question is open. */
  const stationaryMs = computed(() =>
    arrival.value === null ? 0 : Math.max(0, now.value - arrival.value.since),
  )

  // ---- helpers -----------------------------------------------------------
  async function refreshTraces(): Promise<void> {
    traces.value = await listTraces()
  }

  function startTicker(): void {
    stopTicker()
    now.value = Date.now()
    ticker = setInterval(() => {
      now.value = Date.now()
      // On the clock as well as on each fix, so a kombi that stops and then
      // loses signal (phone under a seat) is still asked about.
      checkStationary(now.value)
    }, 1000)
  }

  /** Put, keep or withdraw the arrival question according to the detector. */
  function checkStationary(at: number): void {
    if (status.value !== 'recording') return
    const verdict = detector.evaluate(at)
    // The question was about one stop. Once the kombi has moved on it is
    // moot; the next stop gets its own.
    if (arrival.value !== null && arrival.value.since !== verdict.since) arrival.value = null
    if (verdict.stationary && verdict.since !== null && arrival.value === null) {
      arrival.value = { since: verdict.since, askedAt: at }
    }
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

  function clearRetryTimer(): void {
    if (retryTimer !== null) {
      clearTimeout(retryTimer)
      retryTimer = null
    }
    nextRetryAt.value = null
  }

  /** Try the queue again later, backing off with each consecutive failure. */
  function scheduleRetry(issue: UploadIssue): void {
    clearRetryTimer()
    // Offline: the `online` event flushes instead, and resets the backoff.
    if (!online.value) return
    const delay = retryDelayMs(retryAttempt, issue)
    retryAttempt += 1
    nextRetryAt.value = Date.now() + delay
    retryTimer = setTimeout(() => {
      retryTimer = null
      nextRetryAt.value = null
      void flushQueue()
    }, delay)
  }

  function clearReceipt(): void {
    if (receiptTimer !== null) {
      clearTimeout(receiptTimer)
      receiptTimer = null
    }
    receipt.value = null
    // The field lives in the receipt card; no card, nothing to wait for.
    releaseHint()
  }

  /** Stop holding the upload for a hint, and send whatever is queued. */
  function releaseHint(): void {
    if (hintTimer !== null) {
      clearTimeout(hintTimer)
      hintTimer = null
    }
    if (awaitingHint.value === null) return
    awaitingHint.value = null
    void flushQueue()
  }

  /** Turn "Ride saved" into "Uploaded. Thank you!" for a while, then clear it. */
  function markUploaded(traceId: string): void {
    const current = receipt.value
    if (!current || current.traceId !== traceId) return
    receipt.value = { ...current, phase: 'uploaded' }
    if (receiptTimer !== null) clearTimeout(receiptTimer)
    receiptTimer = setTimeout(() => {
      receiptTimer = null
      receipt.value = null
    }, RECEIPT_TTL_MS)
  }

  /**
   * Browsers throttle or freeze timers in hidden pages, so a retry that came
   * due while the phone was in a pocket is run as soon as the page is back.
   */
  function handleVisibility(): void {
    if (document.visibilityState !== 'visible') {
      // Phone put away with the question unanswered: the ride goes up now
      // rather than waiting on an answer that is not coming.
      releaseHint()
      return
    }
    if (status.value === 'recording') resumeRecording()
    online.value = navigator.onLine
    if (!online.value || flushing) return
    if (nextRetryAt.value !== null && Date.now() >= nextRetryAt.value) {
      void flushQueue()
    }
  }

  // ---- actions -----------------------------------------------------------
  function setContributor(name: string): void {
    contributor.value = name
    try {
      localStorage.setItem(CONTRIBUTOR_KEY, name)
    } catch {
      // Storage disabled (private mode, locked-down browser): the name still
      // works for this session, it just will not be remembered.
    }
  }

  /**
   * Recover from a previous session and drain the upload queue.
   *
   * A trace still marked `recording` (or `uploading`) means the tab died while
   * it was in progress. The points already on disk are perfectly good, so the
   * trace is re-queued rather than discarded.
   */
  async function init(): Promise<void> {
    // Hot reload or a second mount must not add a second set of listeners.
    if (initialised) return
    initialised = true

    if (!('geolocation' in navigator)) {
      gps.value = 'unsupported'
      notice.value = { kind: window.isSecureContext ? 'unsupported' : 'insecure' }
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
    document.addEventListener('visibilitychange', handleVisibility)

    void flushQueue()
  }

  /** Read the permission state up front so we can warn before the big button. */
  async function probePermission(): Promise<void> {
    if (!navigator.permissions?.query) return
    try {
      const result = await navigator.permissions.query({ name: 'geolocation' as PermissionName })
      locationGranted.value = result.state === 'granted'
      if (result.state === 'denied' && status.value !== 'recording') {
        gps.value = 'denied'
        notice.value = { kind: 'blocked' }
      }
      result.addEventListener('change', () => {
        locationGranted.value = result.state === 'granted'
        if (result.state !== 'granted') return
        if (gps.value === 'denied') gps.value = 'idle'
        if (notice.value?.kind === 'blocked' || notice.value?.kind === 'denied') {
          notice.value = null
        }
      })
    } catch {
      // Some browsers reject the geolocation permission name; ignore.
    }
  }

  async function start(): Promise<void> {
    // `starting` closes the window between the tap and `status` changing,
    // during which a second tap would otherwise start a second trace and a
    // second GPS watch that Stop never clears.
    if (status.value === 'recording' || starting) return
    if (!('geolocation' in navigator)) {
      gps.value = 'unsupported'
      notice.value = { kind: window.isSecureContext ? 'unsupported' : 'insecure' }
      return
    }
    const name = contributor.value.trim()
    if (!name) {
      notice.value = { kind: 'needs-nickname' }
      return
    }

    starting = true
    try {
      detector.reset()
      arrival.value = null
      // Before the first await: the tap is what lets audio start.
      keepAlive.start()
      keepAliveActive.value = keepAlive.active

      const trace = await createTrace(name)
      clearReceipt() // also releases a held upload
      currentTraceId.value = trace.id
      pointCount.value = 0
      path.value = []
      lastPosition.value = null
      startedAt.value = trace.startedAt
      status.value = 'recording'
      gps.value = 'acquiring'
      notice.value = null

      startTicker()
      void wakeLock.acquire()

      // The permission prompt is raised here, by watchPosition itself.
      watchId = navigator.geolocation.watchPosition(
        handlePosition,
        handleGeolocationError,
        GEOLOCATION_OPTIONS,
      )

      await refreshTraces()
    } finally {
      starting = false
      if (status.value !== 'recording') {
        // createTrace threw: nothing is being recorded, so nothing to keep alive.
        keepAlive.stop()
        keepAliveActive.value = false
      }
    }
  }

  function handlePosition(position: GeolocationPosition): void {
    const { latitude, longitude, accuracy, speed } = position.coords
    const accuracyM = Number.isFinite(accuracy) ? accuracy : null
    gps.value = 'fixed'
    if (notice.value && FIX_CLEARED_NOTICES.has(notice.value.kind)) notice.value = null

    lastPosition.value = {
      lon: longitude,
      lat: latitude,
      accuracyM,
      speedMps: speed !== null && Number.isFinite(speed) ? speed : null,
      timestamp: position.timestamp,
    }

    const traceId = currentTraceId.value
    if (traceId === null || status.value !== 'recording') return

    detector.push({ timestamp: position.timestamp, lat: latitude, lon: longitude, accuracyM })
    checkStationary(Date.now())

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
        notice.value = {
          kind: 'save-failed',
          detail: error instanceof Error ? error.message : undefined,
        }
      })
  }

  function handleGeolocationError(error: GeolocationPositionError): void {
    switch (error.code) {
      case error.PERMISSION_DENIED:
        gps.value = 'denied'
        notice.value = { kind: 'denied' }
        // Nothing can be recorded without it, so end the session cleanly.
        void stop()
        break
      case error.POSITION_UNAVAILABLE:
        gps.value = status.value === 'recording' && hasFix.value ? 'fixed' : 'unavailable'
        notice.value = { kind: 'no-signal' }
        break
      case error.TIMEOUT:
        if (!hasFix.value) gps.value = 'acquiring'
        notice.value = { kind: 'waiting' }
        break
      default:
        notice.value = { kind: 'location-error', detail: error.message || undefined }
    }
  }

  async function stop(): Promise<void> {
    if (status.value !== 'recording') return

    const endedAt = Date.now()
    const durationMs = startedAt.value === null ? 0 : endedAt - startedAt.value

    status.value = 'idle'
    stopWatch()
    stopTicker()
    keepAlive.stop()
    keepAliveActive.value = false
    detector.reset()
    arrival.value = null
    await wakeLock.release()

    const traceId = currentTraceId.value
    currentTraceId.value = null
    startedAt.value = null
    if (gps.value !== 'denied') gps.value = 'idle'
    if (notice.value && TRANSIENT_NOTICES.has(notice.value.kind)) notice.value = null

    if (traceId !== null) {
      const trace = await patchTrace(traceId, { status: 'pending', endedAt })
      if (trace && trace.pointCount === 0) {
        // A trace with no points is not a trace — the API rejects it anyway.
        await deleteTrace(traceId)
        // Keep the denial notice — it explains *why* there were no points.
        if (gps.value !== 'denied') notice.value = { kind: 'empty-ride' }
      } else if (trace) {
        receipt.value = {
          traceId,
          pointCount: trace.pointCount,
          durationMs,
          phase: 'saved',
          routeHint: null,
        }
        // Hold this one ride back and ask which route it was. Anything else
        // in the queue still goes.
        awaitingHint.value = traceId
        hintTimer = setTimeout(() => {
          hintTimer = null
          releaseHint()
        }, HINT_GRACE_MS)
      }
    }

    await refreshTraces()
    void flushQueue()
  }

  /** Record what the rider said they rode, then let the upload go. */
  async function saveHint(hint: string): Promise<void> {
    const traceId = awaitingHint.value
    if (traceId === null) return
    const routeHint = hint.trim() || null
    await patchTrace(traceId, { routeHint })
    if (receipt.value?.traceId === traceId) receipt.value = { ...receipt.value, routeHint }
    releaseHint()
  }

  /** "Skip": upload without a hint. */
  function skipHint(): void {
    releaseHint()
  }

  /** "Yes, we've arrived": end the ride. */
  async function confirmArrival(): Promise<void> {
    arrival.value = null
    await stop()
  }

  /** "Still riding": no more questions about this stop; ask again at the next. */
  function stillRiding(): void {
    detector.snooze()
    arrival.value = null
  }

  /** The media control's text comes from the component, which owns the strings. */
  function setMediaLabels(labels: KeepAliveLabels): void {
    keepAlive.setLabels(labels)
  }

  /**
   * Back in the foreground. The browser may have frozen the page meanwhile:
   * timers stall, and a frozen page's GPS watch may never fire again. A long
   * gap since the last fix is the sign; the cure is a fresh watch and a
   * notice that the trace has a hole.
   */
  function resumeRecording(): void {
    const resumedAt = Date.now()
    now.value = resumedAt // the ticker was throttled; catch the clock up now
    keepAlive.resume()

    const lastSeen = lastPosition.value?.timestamp ?? startedAt.value ?? resumedAt
    const gapMs = resumedAt - lastSeen
    if (gapMs >= RESUME_GAP_MS) {
      stopWatch()
      watchId = navigator.geolocation.watchPosition(
        handlePosition,
        handleGeolocationError,
        GEOLOCATION_OPTIONS,
      )
      notice.value = { kind: 'paused-in-background', gapMs }
    }
    // If the kombi had already stopped before the pause, the detector's anchor
    // is still there, and the first fix back answers the question.
    checkStationary(resumedAt)
  }

  /**
   * Upload every pending trace, oldest first.
   *
   * Stops at the first retryable failure — if the network is down, the rest of
   * the queue will fail the same way — and schedules an automatic retry with
   * backoff. A permanent failure marks that one trace `failed` and moves on.
   */
  async function flushQueue(): Promise<void> {
    if (!navigator.onLine) return
    if (flushing) {
      // A ride stopped, or Retry pressed, mid-flush: go round again after.
      rerunRequested = true
      return
    }
    flushing = true
    uploading.value = true
    rerunRequested = false
    clearRetryTimer()

    let failure: UploadIssue | null = null

    try {
      const pending = (await listTraces())
        .filter((t) => t.status === 'pending')
        .sort((a, b) => a.startedAt - b.startedAt)

      for (const trace of pending) {
        // Never upload the recording that is still in progress, nor the one
        // the rider is still being asked about.
        if (trace.id === currentTraceId.value || trace.id === awaitingHint.value) continue

        const points = await getPoints(trace.id)
        if (points.length === 0) {
          await deleteTrace(trace.id)
          continue
        }

        await patchTrace(trace.id, { status: 'uploading' })
        await refreshTraces()

        slowTimer = setTimeout(() => {
          uploadSlow.value = true
        }, SLOW_UPLOAD_MS)

        try {
          const created = await uploadTrace(trace.contributor, points, {
            routeHint: trace.routeHint ?? null,
          })
          await patchTrace(trace.id, {
            status: 'uploaded',
            remoteId: created.id,
            lastError: null,
            lastErrorKind: null,
            lastErrorStatus: null,
            uploadedAt: Date.now(),
          })
          // The server owns the points now; reclaim the space, keep the receipt.
          await deletePoints(trace.id)
          retryAttempt = 0
          markUploaded(trace.id)
        } catch (error) {
          const issue = classifyUploadError(error)
          await patchTrace(trace.id, {
            status: issue.permanent ? 'failed' : 'pending',
            attempts: trace.attempts + 1,
            lastError: issue.detail,
            lastErrorKind: issue.kind,
            lastErrorStatus: issue.status,
          })
          if (issue.permanent) {
            // It moves to the failed list, which says what went wrong.
            if (receipt.value?.traceId === trace.id) clearReceipt()
          } else {
            failure = issue
            break
          }
        } finally {
          if (slowTimer !== null) {
            clearTimeout(slowTimer)
            slowTimer = null
          }
          uploadSlow.value = false
          await refreshTraces()
        }
      }
    } finally {
      flushing = false
      uploading.value = false
      // Null means this run emptied the queue (or failed only permanently).
      uploadIssue.value = failure
      await refreshTraces()
      if (failure) {
        scheduleRetry(failure)
      } else {
        retryAttempt = 0
        if (rerunRequested) void flushQueue()
      }
    }
  }

  /** "Retry now": skip the wait and reset the backoff. */
  function retryNow(): void {
    retryAttempt = 0
    void flushQueue()
  }

  /** Put a permanently-failed trace back in the queue (after a manual fix). */
  async function retry(traceId: string): Promise<void> {
    await patchTrace(traceId, {
      status: 'pending',
      lastError: null,
      lastErrorKind: null,
      lastErrorStatus: null,
    })
    retryAttempt = 0
    await refreshTraces()
    await flushQueue()
  }

  async function discard(traceId: string): Promise<void> {
    await deleteTrace(traceId)
    if (receipt.value?.traceId === traceId) clearReceipt()
    await refreshTraces()
  }

  function handleOnline(): void {
    online.value = true
    retryAttempt = 0
    void flushQueue()
  }

  function handleOffline(): void {
    online.value = false
    // Keep the issue; the phase reads "offline" until the connection returns.
    clearRetryTimer()
  }

  return {
    // state
    status,
    gps,
    notice,
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
    uploadIssue,
    uploadSlow,
    receipt,
    nextRetryAt,
    awaitingHint,
    arrival,
    keepAliveActive,
    locationGranted,
    // getters
    isRecording,
    elapsedMs,
    canStart,
    queued,
    failed,
    uploadedCount,
    hasFix,
    uploadPhase,
    stationaryMs,
    // actions
    init,
    start,
    stop,
    flushQueue,
    retryNow,
    retry,
    discard,
    setContributor,
    saveHint,
    skipHint,
    confirmArrival,
    stillRiding,
    setMediaLabels,
  }
})
