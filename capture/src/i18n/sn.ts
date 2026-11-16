/**
 * Shona strings — NOT YET TRANSLATED.
 *
 * Every string below is still the English text, copied from en.ts as a
 * template. To translate:
 *
 *   1. Replace each English string with Shona. Keep the keys, the order and
 *      the `(n: number) => …` functions as they are; only change the text
 *      inside the quotes and backticks, and the plural rule in `plural()` if
 *      Shona needs a different one.
 *   2. Have a Shona speaker review it on a phone (wording, length on screen).
 *   3. Set `snReady` to true. Riders whose phone is set to Shona then get it
 *      automatically, and a language switch appears in the welcome sheet.
 *
 * The compiler checks this file against en.ts (`Strings`), so a missing or
 * renamed key fails the build instead of showing a blank on screen.
 */

import type { UploadIssueKind } from '@/lib/uploadIssue'
import type { NoticeKind } from '@/stores/recording'

import type { Strings } from './index'

/** Flip to true once the text below is translated and reviewed. */
export const snReady = false

function plural(n: number, one: string, many: string): string {
  return n === 1 ? one : many
}

function rideDuration(durationMs: number): string {
  const minutes = Math.round(durationMs / 60_000)
  return minutes < 1 ? 'under a minute' : `${minutes} min`
}

function pausedFor(ms: number): string {
  const minutes = Math.round(ms / 60_000)
  return minutes < 1 ? `${Math.round(ms / 1000)} s` : `${minutes} min`
}

function savedOnPhone(n: number): string {
  return n <= 1 ? 'Your ride is saved on this phone' : 'Your rides are saved on this phone'
}

export const sn: Strings = {
  header: {
    nickname: 'Nickname',
    nicknamePlaceholder: 'e.g. Tari',
    help: 'How it works',
    gps: {
      acquiring: 'Finding GPS…',
      fixed: (accuracy: string) => `GPS ±${accuracy}`,
      unavailable: 'Weak GPS',
      denied: 'Location off',
      unsupported: 'No GPS',
    },
    offline: 'Offline',
    screenMaySleep: 'Screen may sleep',
    screenMaySleepTitle:
      'Your screen may switch off. Tap it now and then — GPS can pause while the screen is off.',
  },

  stats: {
    points: 'Points',
    rideTime: 'Ride time',
    accuracy: 'Accuracy',
  },

  record: {
    start: 'Start',
    stop: 'Stop',
    needsNickname: 'Add a nickname to start',
    readyHint: 'Press Start when your kombi pulls off.',
    // Until the first fix nothing is being recorded; pressing Stop now loses
    // the ride, so say what to wait for.
    findingHint: 'Finding GPS… Keep this page open. Recording starts when the blue dot appears.',
    recordingHint: 'GPS ready. Press Stop when you get off.',
    privacy: 'Only the project team sees nicknames and individual rides.',
  },

  notice: {
    unsupported: "This browser can't use GPS. Try Chrome on Android or Safari on iPhone.",
    insecure: 'GPS needs a secure connection. Open the app from its https:// link.',
    blocked:
      "Location is turned off for this site. Turn it on in your browser's site settings, then press Start.",
    denied:
      'Location permission was refused, so recording stopped. Allow location for this site in your browser settings, then press Start again.',
    'no-signal':
      'Weak GPS signal. Recording continues — the trace fills in when the signal returns.',
    waiting: 'Still looking for GPS. This can take a minute, especially indoors or under a roof.',
    'save-failed':
      "Couldn't save the latest point on this phone. If this keeps happening, free up some storage space.",
    'location-error': 'Location error. Recording continues and will keep trying.',
    'empty-ride':
      'No GPS position was found before you pressed Stop, so nothing was saved. Next time, wait for the blue dot. Outside or near a window is quickest.',
    'needs-nickname': 'Add a nickname before you start.',
    'paused-in-background': (gapMs: number) =>
      `Recording paused for ${pausedFor(gapMs)} while the app was in the background. It has restarted, so the trace has a gap.`,
  } satisfies Record<NoticeKind, string | ((gapMs: number) => string)>,

  receipt: {
    saved: 'Ride saved',
    uploaded: 'Uploaded. Thank you!',
    summary: (points: number, durationMs: number) =>
      `${points} ${plural(points, 'point', 'points')} · ${rideDuration(durationMs)}`,
    route: (hint: string) => `Route: ${hint}`,
  },

  // Asked once, right after Stop. Optional; the upload goes ahead without it.
  hint: {
    label: 'Which route was this?',
    help: 'Optional. The name people call it, or where it runs.',
    placeholder: 'e.g. Mbare – Copacabana, or Route 4',
    save: 'Save',
    skip: 'Skip',
  },

  // Asked when the kombi has not moved for a while, so a rider who has got
  // off does not keep recording their walk. On screen, and as a system
  // notification when the app is in the background.
  arrival: {
    title: 'Have we arrived?',
    body: (minutes: number) =>
      `The kombi hasn't moved for ${minutes} min. If you've got off, stop recording.`,
    stop: 'Yes, stop recording',
    stillRiding: 'Still riding',
    notificationTitle: 'Have we arrived?',
    notificationBody: (minutes: number) =>
      `The kombi hasn't moved for ${minutes} min. Stop recording if you've got off.`,
    notificationStop: 'Stop recording',
    notificationStillRiding: 'Still riding',
  },

  // The Android media control shown while the background keep-alive plays.
  keepAlive: {
    title: 'Recording your kombi ride',
    artist: 'Kombi Capture',
  },

  upload: {
    uploading: (n: number) => (n <= 1 ? 'Uploading your ride…' : `Uploading ${n} rides…`),
    slow: 'Still uploading. The server can take up to a minute to wake up.',
    offline: (n: number) =>
      n <= 1
        ? "You're offline. Your ride will upload when you're back online."
        : `You're offline. ${n} rides will upload when you're back online.`,
    waiting: (n: number) =>
      n === 1 ? '1 ride waiting to upload.' : `${n} rides waiting to upload.`,
    retrying: (kind: UploadIssueKind, n: number) => {
      switch (kind) {
        case 'busy':
          return `The server is busy. ${savedOnPhone(n)} and will upload in a minute or two.`
        case 'server':
          return `The server ran into a problem. ${savedOnPhone(n)} and will upload automatically.`
        default:
          return `Can't reach the server right now. ${savedOnPhone(n)} and will upload automatically.`
      }
    },
    retryNow: 'Retry now',
    details: 'Details',
    contributed: (n: number) =>
      n === 1
        ? "You've contributed 1 ride from this phone."
        : `You've contributed ${n} rides from this phone.`,
  },

  failed: {
    heading: (n: number) =>
      n === 1 ? "1 ride couldn't be uploaded" : `${n} rides couldn't be uploaded`,
    reason: {
      'too-long':
        'This ride is too long to upload in one go. Next time, press Stop at the end of each ride.',
      rejected: "The server didn't accept this ride. Try again, or discard it.",
      // Only seen on rows that older builds marked failed for these reasons.
      network: "This ride couldn't be uploaded. Try again.",
      server: "This ride couldn't be uploaded. Try again.",
      busy: "This ride couldn't be uploaded. Try again.",
    } satisfies Record<UploadIssueKind, string>,
    meta: (points: number, startedAt: number) =>
      `${points} ${plural(points, 'point', 'points')} · ${new Date(startedAt).toLocaleString(
        undefined,
        { dateStyle: 'medium', timeStyle: 'short' },
      )}`,
    tryAgain: 'Try again',
    discard: 'Discard',
    discardConfirm: "Discard this ride? It will be deleted from this phone and can't be recovered.",
    technical: (status: number | null, detail: string) =>
      status === null ? detail : `HTTP ${status}: ${detail}`,
  },

  welcome: {
    title: "Help map Harare's kombi routes",
    intro:
      "Record your kombi rides with your phone's GPS. Rides from many people become a free public map of kombi routes and stops.",
    steps: [
      {
        title: 'Press Start when the kombi pulls off',
        body: 'Your phone will ask to use your location. Tap Allow. Recording starts when the blue dot appears.',
      },
      {
        title: 'You can use other apps',
        body: 'On Android the app keeps recording in the background by playing a silent sound, and keeps the screen awake while it is open. On iPhone keep this page open: recording pauses while it is hidden.',
      },
      {
        title: 'Press Stop when you get off',
        body: "Your ride is saved on this phone and uploads by itself. If you're offline, it uploads the next time you open the app online.",
      },
      {
        title: 'Forgot? The app will ask',
        body: "If the kombi stops for five minutes, the app asks whether you've arrived, on screen and as a notification. Nothing stops until you say so.",
      },
    ],
    nickname: 'Nickname',
    nicknameHelp: 'Any name you like. No need for your real name.',
    nicknameLocked: 'You can change your nickname after this ride.',
    privacy:
      'Only the project team sees nicknames and individual rides. The public map only shows routes combined from many rides.',
    install: 'Install app',
    installTip:
      'Tip: add this app to your home screen from your browser menu so it is one tap away.',
    language: 'Language',
    continue: 'Continue',
    close: 'Close',
  },

  update: {
    ready: 'A new version of the app is ready.',
    apply: 'Update',
  },

  map: {
    label: "Map of your position and the route you've recorded",
    recentre: 'Recentre',
    denied: "Location is off, so the map can't show where you are.",
    unsupported: "This browser can't show your position.",
    finding: 'Finding your position…',
    idle: 'Your position will appear here after you press Start.',
  },
}
