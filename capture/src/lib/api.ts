/** Client for the Kombi API's trace ingest endpoint. */
import type { StoredPoint } from './db'

const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000').replace(
  /\/+$/,
  '',
)

export interface TraceCreated {
  id: number
  contributor: string
  status: string
  point_count: number
}

/**
 * An upload that failed. `permanent` distinguishes "the server will never
 * accept this payload" (bad request — stop retrying) from "try again later"
 * (offline, timeout, 5xx).
 *
 * The message is technical and untranslated; `lib/uploadIssue.ts` turns the
 * error into something the UI can explain.
 */
export class UploadError extends Error {
  readonly permanent: boolean
  readonly status: number | null
  /** Seconds the server asked us to wait, when the header was readable. */
  readonly retryAfterSeconds: number | null
  /** The parsed `detail` field of an error response, e.g. a pydantic 422 list. */
  readonly detail: unknown

  constructor(
    message: string,
    options: {
      permanent: boolean
      status?: number | null
      retryAfterSeconds?: number | null
      detail?: unknown
    },
  ) {
    super(message)
    this.name = 'UploadError'
    this.permanent = options.permanent
    this.status = options.status ?? null
    this.retryAfterSeconds = options.retryAfterSeconds ?? null
    this.detail = options.detail ?? null
  }
}

/** 4xx means we sent something wrong — except these, which are worth retrying. */
const RETRYABLE_CLIENT_ERRORS = new Set([408, 425, 429])

/**
 * Give up on a request after this long. The API is on a free tier that can
 * take up to a minute to wake, so this is generous; without it, a request that
 * never answers would hold the upload queue forever.
 */
const UPLOAD_TIMEOUT_MS = 120_000

/**
 * Parse a `Retry-After` header: delay-seconds or an HTTP date.
 *
 * The API sends it with a 429, but it is not a CORS-safelisted response header
 * and the API does not list it in `expose_headers`, so a cross-origin page
 * reads `null` today. Once the API exposes it, this starts working unchanged.
 */
export function parseRetryAfter(value: string | null): number | null {
  if (value === null || value.trim() === '') return null
  const seconds = Number(value)
  const parsed = Number.isFinite(seconds)
    ? seconds
    : (Date.parse(value) - Date.now()) / 1000
  if (!Number.isFinite(parsed)) return null
  return Math.min(Math.max(Math.round(parsed), 0), 3600)
}

function defaultSignal(): AbortSignal | undefined {
  return typeof AbortSignal.timeout === 'function'
    ? AbortSignal.timeout(UPLOAD_TIMEOUT_MS)
    : undefined
}

export async function uploadTrace(
  contributor: string,
  points: StoredPoint[],
  signal?: AbortSignal,
): Promise<TraceCreated> {
  const body = {
    contributor,
    points: points.map((p) => ({
      lat: p.lat,
      lon: p.lon,
      recorded_at: new Date(p.recordedAt).toISOString(),
      accuracy_m: p.accuracyM,
      speed_mps: p.speedMps,
    })),
  }

  let response: Response
  try {
    response = await fetch(`${API_BASE_URL}/traces`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal: signal ?? defaultSignal(),
    })
  } catch (error) {
    // Network unreachable, DNS failure, CORS rejection, timeout, abort.
    const message =
      error instanceof Error && error.name === 'TimeoutError'
        ? `Timed out after ${UPLOAD_TIMEOUT_MS / 1000} s`
        : error instanceof Error
          ? error.message
          : 'Network error'
    throw new UploadError(message, { permanent: false })
  }

  if (!response.ok) {
    const { message, detail } = await readErrorDetail(response)
    const permanent =
      response.status >= 400 &&
      response.status < 500 &&
      !RETRYABLE_CLIENT_ERRORS.has(response.status)
    throw new UploadError(message ?? `Upload failed (HTTP ${response.status})`, {
      permanent,
      status: response.status,
      retryAfterSeconds: parseRetryAfter(response.headers.get('Retry-After')),
      detail,
    })
  }

  return (await response.json()) as TraceCreated
}

async function readErrorDetail(
  response: Response,
): Promise<{ message: string | null; detail: unknown }> {
  try {
    const payload = (await response.json()) as { detail?: unknown }
    if (typeof payload.detail === 'string') {
      return { message: payload.detail, detail: payload.detail }
    }
    if (payload.detail) {
      return { message: JSON.stringify(payload.detail), detail: payload.detail }
    }
  } catch {
    // Body was empty or not JSON — fall back to the status line.
  }
  return { message: null, detail: null }
}
