/**
 * Keeping the page alive while the rider uses another app.
 *
 * There is no background geolocation on the web. Android Chrome freezes a
 * hidden page — timers, the GPS watch, everything — after about a minute, and
 * the ride silently stops being recorded. What Chrome does *not* freeze is a
 * page that is audibly playing sound. So, for the length of a recording, this
 * plays a tone far below hearing at a level the browser still counts as audio.
 *
 * It is a workaround, not a feature of the platform, and browser makers
 * dislike it. Three things follow: it may stop working in a future Chrome;
 * it does nothing on iOS, which suspends a backgrounded page regardless; and
 * the gain has to be found by trying it on a phone, which `KEEP_ALIVE_GAIN`
 * and the localStorage override below are for. Recording never depends on it:
 * when it fails or is unsupported, the store's resume logic picks up the gap.
 *
 * While it plays, the Media Session gives Android a "Recording your kombi
 * ride" control in the notification shade whose Stop ends the ride. Whether
 * Chrome shows that control for near-silent audio is unverified; the handlers
 * cost nothing if it does not.
 */

export interface KeepAliveLabels {
  /** What the Android media control calls the recording. */
  title: string
  /** The app's name, shown under the title. */
  artist: string
}

/**
 * Oscillator gain. Chromium stops counting audio as audible somewhere around
 * -72 dBFS, so 0 does not keep the page alive at all; 0.01 is about -40 dBFS,
 * silent through a phone speaker at 20 Hz. Tune on a device: raise it if fixes
 * stop arriving after ~90 s in the background, lower it (floor ≈ 0.0003) if a
 * rider with earphones can hear a hum.
 */
export const KEEP_ALIVE_GAIN = 0.01

/** Below what a phone speaker can reproduce; headphones hear at most a faint hum. */
export const KEEP_ALIVE_HZ = 20

/** Dev-only: set this to a number in localStorage to try a gain without a rebuild. */
const GAIN_OVERRIDE_KEY = 'kombi-capture:keepalive-gain'

function configuredGain(): number {
  try {
    const raw = localStorage.getItem(GAIN_OVERRIDE_KEY)
    const value = raw === null ? NaN : Number(raw)
    return Number.isFinite(value) && value >= 0 && value <= 1 ? value : KEEP_ALIVE_GAIN
  } catch {
    return KEEP_ALIVE_GAIN
  }
}

export class KeepAlive {
  private context: AudioContext | null = null
  private oscillator: OscillatorNode | null = null
  private wanted = false
  private labels: KeepAliveLabels | null = null
  private onStopRequested: () => void

  constructor(onStopRequested: () => void) {
    this.onStopRequested = onStopRequested
  }

  static get supported(): boolean {
    return typeof window !== 'undefined' && 'AudioContext' in window
  }

  get active(): boolean {
    return this.context !== null && this.context.state === 'running'
  }

  /** The store never holds user-facing text; the component hands these over. */
  setLabels(labels: KeepAliveLabels): void {
    this.labels = labels
    if (this.wanted) this.publishSession()
  }

  /**
   * Must run synchronously inside the Start tap: a user gesture is what lets
   * an AudioContext start, and it does not survive an `await`.
   */
  start(): void {
    if (!KeepAlive.supported || this.context !== null) return
    this.wanted = true

    try {
      const context = new AudioContext()
      const oscillator = context.createOscillator()
      const gain = context.createGain()
      oscillator.type = 'sine'
      oscillator.frequency.value = KEEP_ALIVE_HZ
      gain.gain.value = configuredGain()
      oscillator.connect(gain)
      gain.connect(context.destination)
      oscillator.start()

      // Safari reports 'interrupted' after a phone call or a trip to the
      // background; Chrome 'suspended' when it decides to. Either way, come back.
      context.addEventListener('statechange', () => {
        if (this.wanted && context.state !== 'running') this.resume()
      })

      this.context = context
      this.oscillator = oscillator
      void context.resume().catch(() => {})
      this.publishSession()
    } catch {
      // No audio for us (locked-down browser, no output device). Recording
      // carries on; it just will not survive the background.
      this.teardown()
    }
  }

  stop(): void {
    this.wanted = false
    this.clearSession()
    this.teardown()
  }

  /** On visibilitychange back to visible, and on any statechange away from running. */
  resume(): void {
    const context = this.context
    if (!this.wanted || context === null || context.state === 'running') return
    void context.resume().catch(() => {})
  }

  private publishSession(): void {
    if (!('mediaSession' in navigator) || this.labels === null || this.context === null) return
    try {
      navigator.mediaSession.metadata = new MediaMetadata({
        title: this.labels.title,
        artist: this.labels.artist,
      })
      navigator.mediaSession.playbackState = 'playing'
      // Both: Android shows one or the other depending on the version.
      navigator.mediaSession.setActionHandler('pause', this.onStopRequested)
      navigator.mediaSession.setActionHandler('stop', this.onStopRequested)
    } catch {
      // An action the browser does not know; the control simply lacks it.
    }
  }

  private clearSession(): void {
    if (!('mediaSession' in navigator)) return
    try {
      navigator.mediaSession.setActionHandler('pause', null)
      navigator.mediaSession.setActionHandler('stop', null)
      navigator.mediaSession.playbackState = 'none'
      navigator.mediaSession.metadata = null
    } catch {
      // Nothing to clear.
    }
  }

  private teardown(): void {
    const { oscillator, context } = this
    this.oscillator = null
    this.context = null
    try {
      oscillator?.stop()
    } catch {
      // Already stopped.
    }
    void context?.close().catch(() => {})
  }
}
