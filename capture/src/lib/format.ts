/** Format a duration in ms as H:MM:SS (or M:SS under an hour). */
export function formatElapsed(ms: number): string {
  const totalSeconds = Math.max(0, Math.floor(ms / 1000))
  const hours = Math.floor(totalSeconds / 3600)
  const minutes = Math.floor((totalSeconds % 3600) / 60)
  const seconds = totalSeconds % 60
  const pad = (n: number) => String(n).padStart(2, '0')
  return hours > 0 ? `${hours}:${pad(minutes)}:${pad(seconds)}` : `${minutes}:${pad(seconds)}`
}

/** Format a GPS accuracy radius in metres. */
export function formatAccuracy(metres: number | null): string {
  if (metres === null || !Number.isFinite(metres)) return '—'
  return metres >= 100 ? `${Math.round(metres)} m` : `${metres.toFixed(0)} m`
}
