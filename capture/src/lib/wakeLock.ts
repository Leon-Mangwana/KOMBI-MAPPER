/**
 * Screen Wake Lock, held for the duration of a recording.
 *
 * The lock is released by the browser whenever the page is hidden (screen off,
 * tab switch, app backgrounded), and it is *not* restored automatically — so
 * we re-acquire on every `visibilitychange` back to visible, for as long as
 * `isWanted()` says we are still recording.
 */
export class ScreenWakeLock {
  private sentinel: WakeLockSentinel | null = null
  private isWanted: () => boolean
  private onChange: (active: boolean) => void
  private listening = false

  constructor(isWanted: () => boolean, onChange: (active: boolean) => void = () => {}) {
    this.isWanted = isWanted
    this.onChange = onChange
  }

  static get supported(): boolean {
    return typeof navigator !== 'undefined' && 'wakeLock' in navigator
  }

  get active(): boolean {
    return this.sentinel !== null && !this.sentinel.released
  }

  async acquire(): Promise<void> {
    if (!ScreenWakeLock.supported || this.active) return

    try {
      this.sentinel = await navigator.wakeLock.request('screen')
      this.sentinel.addEventListener('release', () => {
        this.sentinel = null
        this.onChange(false)
      })
      this.onChange(true)
    } catch {
      // Denied (often because the page is already hidden, or the device is on
      // battery saver). Recording continues regardless — the screen just may
      // switch off. The next visibilitychange will try again.
      this.sentinel = null
      this.onChange(false)
    }

    this.startListening()
  }

  async release(): Promise<void> {
    this.stopListening()
    const sentinel = this.sentinel
    this.sentinel = null
    if (sentinel && !sentinel.released) {
      try {
        await sentinel.release()
      } catch {
        // Already gone; nothing to clean up.
      }
    }
    this.onChange(false)
  }

  private handleVisibilityChange = (): void => {
    if (document.visibilityState === 'visible' && this.isWanted()) {
      void this.acquire()
    }
  }

  private startListening(): void {
    if (this.listening) return
    document.addEventListener('visibilitychange', this.handleVisibilityChange)
    this.listening = true
  }

  private stopListening(): void {
    if (!this.listening) return
    document.removeEventListener('visibilitychange', this.handleVisibilityChange)
    this.listening = false
  }
}
