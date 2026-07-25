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
 */
export class UploadError extends Error {
  readonly permanent: boolean
  readonly status: number | null

  constructor(message: string, options: { permanent: boolean; status?: number | null }) {
    super(message)
    this.name = 'UploadError'
    this.permanent = options.permanent
    this.status = options.status ?? null
  }
}

/** 4xx means we sent something wrong — except these, which are worth retrying. */
const RETRYABLE_CLIENT_ERRORS = new Set([408, 425, 429])

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
      signal,
    })
  } catch (error) {
    // Network unreachable, DNS failure, CORS rejection, abort.
    throw new UploadError(error instanceof Error ? error.message : 'Network error', {
      permanent: false,
    })
  }

  if (!response.ok) {
    const detail = await readErrorDetail(response)
    const permanent =
      response.status >= 400 &&
      response.status < 500 &&
      !RETRYABLE_CLIENT_ERRORS.has(response.status)
    throw new UploadError(detail ?? `Upload failed (HTTP ${response.status})`, {
      permanent,
      status: response.status,
    })
  }

  return (await response.json()) as TraceCreated
}

async function readErrorDetail(response: Response): Promise<string | null> {
  try {
    const payload = (await response.json()) as { detail?: unknown }
    if (typeof payload.detail === 'string') return payload.detail
    if (payload.detail) return JSON.stringify(payload.detail)
  } catch {
    // Body was empty or not JSON — fall back to the status line.
  }
  return null
}
