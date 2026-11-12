/**
 * What went wrong with an upload, in terms the UI can explain.
 *
 * `api.ts` reports failures as an `UploadError` carrying the HTTP status and
 * the server's message. That is the right shape for debugging and the wrong
 * one for a rider: "Failed to fetch" means nothing to someone on a kombi. This
 * module sorts every failure into one of a handful of kinds, each of which has
 * plain-language copy in `src/i18n`, and decides how long to wait before
 * trying again. Pure functions, no Vue, so it can be tested on its own.
 */
import { UploadError } from './api'

export type UploadIssueKind =
  /** No response at all: offline, DNS, CORS, timeout, or a server error the
   *  browser could not read because it carried no CORS headers. */
  | 'network'
  /** The server answered with a 5xx, 408 or 425. Worth retrying. */
  | 'server'
  /** 429: the API's upload rate limit. Retry after it says, or a minute. */
  | 'busy'
  /** 413, or a 422 because the trace has more points than the API accepts. */
  | 'too-long'
  /** Any other 4xx. The server will never accept this payload as it is. */
  | 'rejected'

export interface UploadIssue {
  kind: UploadIssueKind
  /** True when retrying the same payload cannot succeed. From `UploadError`. */
  permanent: boolean
  status: number | null
  /** From a `Retry-After` header, when the browser was allowed to read it. */
  retryAfterSeconds: number | null
  /** Technical, untranslated text for a "Details" disclosure. */
  detail: string
}

/** Backoff between automatic retries; the last value repeats. */
export const RETRY_DELAYS_MS: readonly number[] = [15_000, 30_000, 60_000, 120_000, 300_000]

/**
 * The shortest wait after a 429. The API allows six uploads a minute per
 * client, and its `Retry-After` header is not exposed to cross-origin pages,
 * so a minute is the honest default.
 */
export const BUSY_MIN_DELAY_MS = 60_000

const MAX_DETAIL_LENGTH = 300

export function classifyUploadError(error: unknown): UploadIssue {
  if (!(error instanceof UploadError)) {
    const message = error instanceof Error ? error.message : String(error)
    return {
      kind: 'network',
      permanent: false,
      status: null,
      retryAfterSeconds: null,
      detail: truncate(message),
    }
  }

  const issue: Omit<UploadIssue, 'kind'> = {
    permanent: error.permanent,
    status: error.status,
    retryAfterSeconds: error.retryAfterSeconds,
    detail: truncate(error.message),
  }
  const status = error.status

  if (status === null) return { ...issue, kind: 'network' }
  if (status === 429) return { ...issue, kind: 'busy' }
  if (status === 413 || (status === 422 && hasTooManyPoints(error.detail))) {
    return { ...issue, kind: 'too-long' }
  }
  if (status === 408 || status === 425 || status >= 500) return { ...issue, kind: 'server' }
  return { ...issue, kind: 'rejected' }
}

/**
 * How long to wait before automatic retry number `attempt` (0-based).
 *
 * Up to 20% random extra is added so that many phones coming back online at
 * once do not retry in lockstep. It only ever lengthens the wait, so a busy
 * server's `Retry-After` is never undercut.
 */
export function retryDelayMs(
  attempt: number,
  issue: UploadIssue,
  random: () => number = Math.random,
): number {
  let base = RETRY_DELAYS_MS[Math.min(Math.max(attempt, 0), RETRY_DELAYS_MS.length - 1)]
  if (issue.kind === 'busy') {
    const asked = issue.retryAfterSeconds === null ? 0 : issue.retryAfterSeconds * 1000
    base = Math.max(base, BUSY_MIN_DELAY_MS, asked)
  }
  return Math.round(base * (1 + random() * 0.2))
}

/** A pydantic 422 for `points` exceeding `MAX_TRACE_POINTS` on the API. */
function hasTooManyPoints(detail: unknown): boolean {
  if (!Array.isArray(detail)) return false
  return detail.some((entry: unknown) => {
    if (typeof entry !== 'object' || entry === null) return false
    const { type, loc } = entry as { type?: unknown; loc?: unknown }
    return type === 'too_long' && Array.isArray(loc) && loc.includes('points')
  })
}

function truncate(text: string): string {
  return text.length > MAX_DETAIL_LENGTH ? `${text.slice(0, MAX_DETAIL_LENGTH - 1)}…` : text
}
