/**
 * IndexedDB persistence for traces and their points.
 *
 * Points are written here as they arrive rather than buffered in memory: a
 * phone in someone's pocket for a 40-minute kombi ride will suspend or kill
 * the tab, and anything held only in a Pinia ref dies with it. IndexedDB
 * survives that, so a killed recording can be recovered and uploaded on the
 * next launch.
 */
import { openDB, type DBSchema, type IDBPDatabase } from 'idb'

import type { UploadIssueKind } from './uploadIssue'

export type TraceStatus =
  /** Actively being recorded right now. */
  | 'recording'
  /** Finished (or recovered after a crash), waiting to upload. */
  | 'pending'
  /** Upload in flight. */
  | 'uploading'
  /** Accepted by the API. */
  | 'uploaded'
  /** Rejected by the API for a reason retrying will not fix. */
  | 'failed'

export interface StoredTrace {
  id: string
  contributor: string
  status: TraceStatus
  startedAt: number
  endedAt: number | null
  pointCount: number
  /** `id` assigned by the API once uploaded. */
  remoteId: number | null
  attempts: number
  /** Technical text of the last upload failure, for a "Details" disclosure. */
  lastError: string | null
  // The fields below are optional because rows written by older builds lack
  // them; object stores are schemaless, so no DB_VERSION bump is needed.
  /** Classified reason for `lastError` (see lib/uploadIssue). */
  lastErrorKind?: UploadIssueKind | null
  /** HTTP status of the last failure, or null when there was no response. */
  lastErrorStatus?: number | null
  /** Epoch ms when the API accepted the trace. */
  uploadedAt?: number | null
}

export interface StoredPoint {
  traceId: string
  seq: number
  lat: number
  lon: number
  /** Epoch ms, straight from `GeolocationPosition.timestamp`. */
  recordedAt: number
  accuracyM: number | null
  speedMps: number | null
}

interface CaptureDB extends DBSchema {
  traces: {
    key: string
    value: StoredTrace
    indexes: { 'by-status': TraceStatus }
  }
  points: {
    key: [string, number]
    value: StoredPoint
  }
}

const DB_NAME = 'kombi-capture'
const DB_VERSION = 1

let dbPromise: Promise<IDBPDatabase<CaptureDB>> | null = null

function getDb(): Promise<IDBPDatabase<CaptureDB>> {
  if (!dbPromise) {
    dbPromise = openDB<CaptureDB>(DB_NAME, DB_VERSION, {
      upgrade(db) {
        const traces = db.createObjectStore('traces', { keyPath: 'id' })
        traces.createIndex('by-status', 'status')
        // Compound key keeps points physically ordered by sequence, so reading
        // a whole trace back is a single ordered range scan.
        db.createObjectStore('points', { keyPath: ['traceId', 'seq'] })
      },
    })
  }
  return dbPromise
}

/** Range covering every point of one trace, in `seq` order. */
function pointRange(traceId: string): IDBKeyRange {
  return IDBKeyRange.bound([traceId, 0], [traceId, Number.MAX_SAFE_INTEGER])
}

export async function createTrace(contributor: string): Promise<StoredTrace> {
  const trace: StoredTrace = {
    id: crypto.randomUUID(),
    contributor,
    status: 'recording',
    startedAt: Date.now(),
    endedAt: null,
    pointCount: 0,
    remoteId: null,
    attempts: 0,
    lastError: null,
  }
  const db = await getDb()
  await db.put('traces', trace)
  return trace
}

/**
 * Append one point and bump the trace's counter in the same transaction, so
 * `pointCount` can never drift from the rows actually on disk.
 */
export async function appendPoint(
  traceId: string,
  point: Omit<StoredPoint, 'traceId' | 'seq'>,
): Promise<number> {
  const db = await getDb()
  const tx = db.transaction(['traces', 'points'], 'readwrite')
  const traces = tx.objectStore('traces')
  const trace = await traces.get(traceId)
  if (!trace) {
    await tx.done
    throw new Error(`Unknown trace ${traceId}`)
  }

  const seq = trace.pointCount
  await tx.objectStore('points').put({ ...point, traceId, seq })
  trace.pointCount = seq + 1
  await traces.put(trace)
  await tx.done

  return trace.pointCount
}

export async function getTrace(traceId: string): Promise<StoredTrace | undefined> {
  return (await getDb()).get('traces', traceId)
}

export async function getPoints(traceId: string): Promise<StoredPoint[]> {
  return (await getDb()).getAll('points', pointRange(traceId))
}

export async function patchTrace(
  traceId: string,
  patch: Partial<Omit<StoredTrace, 'id'>>,
): Promise<StoredTrace | undefined> {
  const db = await getDb()
  const tx = db.transaction('traces', 'readwrite')
  const existing = await tx.store.get(traceId)
  if (!existing) {
    await tx.done
    return undefined
  }
  const updated = { ...existing, ...patch }
  await tx.store.put(updated)
  await tx.done
  return updated
}

export async function listTraces(): Promise<StoredTrace[]> {
  const traces = await (await getDb()).getAll('traces')
  return traces.sort((a, b) => b.startedAt - a.startedAt)
}

export async function listByStatus(status: TraceStatus): Promise<StoredTrace[]> {
  return (await getDb()).getAllFromIndex('traces', 'by-status', status)
}

/** Drop a trace's points but keep the metadata row as a receipt. */
export async function deletePoints(traceId: string): Promise<void> {
  const db = await getDb()
  const tx = db.transaction('points', 'readwrite')
  await tx.store.delete(pointRange(traceId))
  await tx.done
}

export async function deleteTrace(traceId: string): Promise<void> {
  const db = await getDb()
  const tx = db.transaction(['traces', 'points'], 'readwrite')
  await tx.objectStore('points').delete(pointRange(traceId))
  await tx.objectStore('traces').delete(traceId)
  await tx.done
}
