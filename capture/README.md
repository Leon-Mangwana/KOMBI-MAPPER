# Kombi Capture

Installable PWA for recording a GPS trace while riding a kombi. One screen: a
MapLibre map showing your position and the trace so far, and one big
**Start/Stop** button. A short welcome sheet explains it the first time.

Stack: Vue 3 (`<script setup>`) · TypeScript · Vite · Pinia · vite-plugin-pwa ·
MapLibre GL JS · idb.

## Setup

```bash
cd capture
npm install
cp .env.example .env    # point VITE_API_BASE_URL at the API
npm run dev
```

`http://localhost:5173` is already in the API's default `CORS_ORIGINS`, so the
dev server can upload with no extra setup. Serving this app from any other
origin (a tunnel, a deployed host) means adding that origin to `CORS_ORIGINS`
in `api/.env`.

### Testing on a real phone

Geolocation and the Wake Lock API need a **secure context** — HTTPS, or
`localhost`. `npm run dev:lan` exposes the dev server on your LAN, but
`http://192.168.x.x:5173` is *not* a secure context, so GPS will not start.
Use a tunnel (`cloudflared tunnel --url http://localhost:5173`, `ngrok http
5173`) or serve the built app over HTTPS.

## Basemap

Protomaps only — this project does not use Mapbox. Set **one** of these in
`.env`:

| Variable                 | Meaning                                                   |
| ------------------------ | --------------------------------------------------------- |
| `VITE_BASEMAP_STYLE_URL` | A self-hosted MapLibre style JSON. Takes priority.         |
| `VITE_PMTILES_URL`       | A `.pmtiles` archive, read via the `pmtiles://` protocol.  |

With `VITE_PMTILES_URL`, the app builds a minimal label-free style (earth,
landuse, water, buildings, roads) so no glyph server is required. For proper
cartography with labels, host a full style JSON and use
`VITE_BASEMAP_STYLE_URL`.

Protomaps keeps rivers and streams in the `water` layer as **lines**, next to
lakes as polygons. A fill layer closes a line and fills it, which once drew the
Mukuvisi River as a lake several kilometres across south-east of the CBD. So
`water` is filtered to polygons and a `water-lines` layer draws the rest as
lines. The public map's style (`../web/src/lib/basemap.ts`) does the same.

With neither set, the map falls back to a blank background. Your position and
the live trace still render — enough to confirm recording works, just with no
map underneath.

The archive itself is not committed here. `npm run dev` and `npm run build`
first run `scripts/copy-basemap.mjs`, which copies
`../web/public/harare.pmtiles` into `public/` (gitignored), so one committed
archive serves both front ends and `VITE_PMTILES_URL=/harare.pmtiles` works
locally and on Netlify alike. Refreshing the map means replacing the copy in
`web/public/` only.

## First run

Contributors are strangers with a phone, so the app explains itself once: a
welcome sheet (`src/components/WelcomeSheet.vue`, a native `<dialog>`) says what
the app is for, gives four steps — Start when the kombi pulls off, you can use
other apps (with the iPhone caveat), Stop when you get off, and the app asks if
you forget — asks for a nickname, and says who can see the data. It is remembered under `localStorage` key `kombi-capture:welcome:v1`
and reopened with the **?** button in the header. On Android Chrome it also
offers **Install app** (via `beforeinstallprompt`, `src/lib/install.ts`);
elsewhere it shows an add-to-home-screen tip unless already installed.

The nickname is required, so the team can tell contributors apart, but it does
not need to be a real name. The privacy line is accurate: raw traces
(`GET /traces/{id}`) are operator-only on the API, and the public endpoints
serve only clustered routes and stops.

## Strings

Every user-facing string lives in `src/i18n/en.ts`, grouped by screen area.
Text with numbers in it is a function, so each language applies its own plural
rules. The store never imports it: it reports codes (`notice.kind`,
`uploadIssue.kind`, `receipt.phase`, `uploadPhase`) and the components turn
them into words. `satisfies Record<…>` makes a missing notice or error kind a
compile error.

The language is chosen once at load: the rider's saved choice, then the phone's
language settings, then English. A language switch appears in the welcome sheet
as soon as two languages are ready; changing it reloads the page, so it is
locked during a ride or an upload.

**Shona** is set up and waiting for its text: `src/i18n/sn.ts` is a full copy
of the English, typed `Strings`, with `snReady = false` so riders never see it.
To ship it, translate every string in place (keep the keys and the functions'
shape), have a Shona speaker review it on a phone, and set `snReady = true`.
Phones set to Shona then get it automatically. Another language follows the
same pattern: copy `en.ts`, type it `Strings`, and add it to `LOCALES` in
`src/i18n/index.ts`.

Not covered: the PWA manifest name and description (`vite.config.ts`) and the
page `<title>`, which are fixed at build time.

## How recording works

**Points are never buffered in memory.** A phone in a pocket for a 40-minute
ride will suspend or kill the tab, and anything held only in a Pinia ref dies
with it. Every position is written to IndexedDB (via `idb`) as it arrives:

- `navigator.geolocation.watchPosition` with `enableHighAccuracy: true` and
  `maximumAge: 0` — never a cached fix.
- Each point is appended to the `points` store, and the trace's `pointCount` is
  bumped **in the same transaction**, so the counter cannot drift from what is
  on disk.
- A **Screen Wake Lock** is held while recording. The browser drops the lock
  whenever the page is hidden and does not restore it, so it is re-acquired on
  every `visibilitychange` back to visible. If the lock is refused, recording
  continues and the header shows *Screen may sleep*.
- On **Stop**, the trace is marked `pending` and a receipt shows *Ride saved ·
  N points · M min* with one optional question underneath: *Which route was
  this?* The answer is stored on the trace as `routeHint` and sent as
  `route_hint`, where it becomes a naming suggestion for the operator. The
  upload of that one ride is held while the question is on screen, and goes
  ahead on Save, on Skip, when the page is hidden (phone put away), when
  another ride starts, or after two minutes. Then *Uploaded. Thank you!* for a
  few seconds, then a running *You've contributed N rides from this phone.*
- Start is guarded against a double tap, which used to create two traces and
  leave a second GPS watch running after Stop.

### In the background

There is no background geolocation on the web. Android Chrome freezes a hidden
page — timers, the GPS watch, everything — after about a minute (Android 15+),
and iOS suspends it the moment it is hidden. Three things make the best of
that:

- **Keep-alive** (`src/lib/keepAlive.ts`). Chrome does not freeze a page that
  is audibly playing sound, so for the length of a recording the app plays a
  20 Hz tone at `KEEP_ALIVE_GAIN` (0.01, about −40 dBFS: silent through a phone
  speaker, counted as audio by the browser). It starts synchronously in the
  Start tap, because an `AudioContext` needs a user gesture. While it plays,
  the Media Session puts a *Recording your kombi ride* control in Android's
  notification shade whose Stop ends the ride (unverified whether Chrome shows
  it for near-silent audio). It is a workaround browser makers dislike, it may
  stop working in a future Chrome, and it does nothing on iOS. Tune the gain on
  a phone: raise it if fixes stop arriving after ~90 s in the background, lower
  it (floor ≈ 0.0003) if a rider with earphones hears a hum. For trials without
  a rebuild, `localStorage['kombi-capture:keepalive-gain']` overrides it. Note
  it may take audio focus and pause music playing in another app.
- **Recovery** (`resumeRecording` in the store). On every return to the
  foreground while recording, the gap since the last fix is measured. Over
  `RESUME_GAP_MS` (60 s) means the browser paused the page: the GPS watch is
  restarted, since a frozen page's watch may never fire again, and a notice
  says *Recording paused for N min while the app was in the background*. The
  notice stays until the ride ends, because the trace has a hole in it. The
  ticker's clock is also caught up, so the elapsed time is right at once.
- **"Have we arrived?"** (`src/lib/stationary.ts`, pure and unit-tested). A
  rider who got off and forgot would otherwise record their walk. The store
  feeds every fix to a `StationaryDetector`: an anchor fix, a
  `STATIONARY_RADIUS_M` (40 m) circle around it, and movement only once
  `MOVE_CONFIRM_FIXES` (3) fixes in a row have left the circle, so one bad fix
  does not restart the clock. Fixes worse than `MAX_FIX_ACCURACY_M` (50 m) are
  ignored. After `STATIONARY_MS` (5 min) inside the circle, backed by at least
  `MIN_STATIONARY_FIXES` (10) and with a good fix in the last `MAX_SILENCE_MS`
  (2 min), the app asks: a card above the Stop button with a vibration, and,
  when the page is hidden, a system notification with *Stop recording* and
  *Still riding* buttons. The detector is evaluated on every fix and on the
  1-second ticker, so a kombi that stops and then loses signal is still asked
  about. *Still riding* snoozes the question until the kombi has moved on and
  stopped again; a stop the kombi leaves withdraws an unanswered card. Nothing
  ever stops by itself. The thresholds echo the pipeline's
  (`cluster.STOP_EPS_M`, `clean.MAX_ACCURACY_M`).

Points are stored raw — no smoothing, no accuracy filtering. Cleaning and
outlier rejection are the pipeline's job, and it can only do them properly with
the full observation (`accuracy_m`, `speed_mps`, timestamp).

### Upload queue

A finished trace stays in IndexedDB until the API confirms it. Every failure is
sorted into a kind (`src/lib/uploadIssue.ts`) with plain-language copy; the raw
error stays available under **Details**.

| Kind       | Cause                                                   | Then                         |
| ---------- | ------------------------------------------------------- | ---------------------------- |
| `network`  | No response: offline, DNS, CORS, timeout (120 s)        | stays `pending`, auto-retry  |
| `server`   | 5xx, 408, 425                                           | stays `pending`, auto-retry  |
| `busy`     | 429                                                     | stays `pending`, retry ≥60 s |
| `too-long` | 413, or a 422 for too many points                       | marked `failed`              |
| `rejected` | any other 4xx                                           | marked `failed`              |

- **Automatic retry** while online: 15 s, 30 s, 60 s, 2 min, then every 5 min,
  each with up to 20% random extra so phones coming back online do not retry
  in lockstep. The backoff resets on success, on reconnect and on **Retry
  now**. A retry that came due while the page was hidden (browsers freeze
  timers there) runs as soon as the page is visible again.
- **429** waits at least a minute. The API sends `Retry-After`, but it is not
  readable cross-origin until the API lists it in CORS `expose_headers`; once
  it does, the app honours it with no change here.
- A 500 from the API's error handler, or a 502/503 from the host's proxy,
  carries no CORS headers, so the browser reports it as a network failure.
  The copy is right either way: the ride is saved and will be retried.
- A ride stopped while an upload is in flight (a slow cold start, say) is
  uploaded straight after it rather than waiting for the next trigger.
- After 10 s in flight the status reads *Still uploading. The server can take
  up to a minute to wake up.*
- `failed` rides are listed with the reason, **Try again** and **Discard**
  (which asks first — the ride is deleted from the phone).
- On success the points are deleted and a small metadata receipt is kept,
  stamped `uploadedAt`.
- On launch, any trace still marked `recording` or `uploading` is evidence the
  tab was killed mid-session. Its points are already on disk, so it is
  re-queued rather than thrown away. If the server had already stored it, this
  uploads it twice; preventing that needs an idempotency key on the API.

### Failure states the UI handles

| Situation                        | What you see                                                       |
| -------------------------------- | ------------------------------------------------------------------ |
| No nickname                      | Start reads *Add a nickname to start*; tapping it focuses the field |
| Location blocked for the site    | Notice on load explaining how to turn it on                         |
| Permission refused at Start      | Recording stops, *Location off* chip, settings hint                 |
| No fix yet                       | *Finding GPS…* chip, *Finding your position…* on the map, and a hint that recording starts when the blue dot appears |
| First fix of a ride              | A short vibration (Android) and the hint changes to *GPS ready…*      |
| `POSITION_UNAVAILABLE`           | *Weak GPS* — keeps watching, does not stop the recording             |
| No Geolocation / insecure origin | *No GPS* chip and a notice explaining HTTPS is required              |
| Offline at Stop                  | *You're offline…*; uploads the moment the connection returns         |
| Stop with no points              | *No GPS position was found before you pressed Stop…* — nothing saved, and it says to wait for the blue dot |
| Page frozen in the background    | On return: *Recording paused for N min…*, the GPS watch restarted, the trace has a gap |
| Kombi still for 5 minutes        | *Have we arrived?* card with vibration; a system notification with buttons when the app is hidden |

## Data shape

Uploaded to `POST /traces` exactly as the API expects:

```json
{
  "contributor": "leon",
  "points": [
    { "lat": -17.8292, "lon": 31.0522, "recorded_at": "2026-07-24T08:00:00.000Z",
      "accuracy_m": 5, "speed_mps": 8.3 }
  ],
  "route_hint": "Mbare – Copacabana"
}
```

`route_hint` is only present when the rider answered the question after Stop.
Coordinates are WGS 84 (EPSG:4326), straight from the Geolocation API. The app
does no metric geometry at all — every distance calculation happens in the
pipeline, reprojected to EPSG:32736. See `../CLAUDE.md`.

## PWA notes

- `registerType: 'autoUpdate'`; the app shell is precached, so it launches
  offline. Map tiles are **not** precached — an offline launch shows the blank
  background style until tiles are reachable.
- **Updates.** A new deploy is fetched in the background and its service
  worker takes over the open page, but the page keeps running the code it
  loaded with. `src/lib/updates.ts` notices the takeover (`controllerchange`,
  ignoring the very first install) and the app shows *A new version of the app
  is ready* with an **Update** button that reloads. It is never automatic and
  never shown during a ride or an upload: a reload mid-ride would end the ride,
  and mid-upload would send it twice. A page left open checks for a new version
  hourly and whenever it comes back to the foreground. Phones still running a
  build from before this existed pick it up the usual way, on their next
  launch.
- **The service worker is hand-written** (`src/sw.ts`, vite-plugin-pwa
  `injectManifest`), only because the arrival notification needs a
  `notificationclick` handler and a generated worker cannot have one. It must
  keep doing what the generated one did: precache the shell, `skipWaiting` +
  `clientsClaim` (what `autoUpdate` means, and what `updates.ts` relies on),
  and the navigation fallback with `/traces` kept away from the cache. It is
  type-checked on its own against the WebWorker lib (`tsconfig.sw.json`), which
  `npm run typecheck` and `npm run build` both run.
- **Notification buttons** come back through the worker: `notificationclick`
  posts `{ type: 'arrival', action }` (`src/lib/swMessages.ts`) to the open
  page, which calls the store; *Stop* and a body tap also focus the app so the
  receipt is seen. The permission is asked once, from the Start tap, and only
  once location is already granted so two prompts never stack; a refusal or a
  dismissal is never re-asked (`kombi-capture:notifications-asked`), and the
  question is then on screen only.
- **What works where.** Android Chrome: keep-alive, notifications with buttons,
  the media control. iOS Safari tab: no background recording, no notifications
  (the `Notification` interface does not exist there), the pause notice on
  return. iOS installed to the home screen, 16.4+: notifications without
  buttons (tapping one opens the app with the card up), still no background
  recording. Everything degrades to the in-app card.
- `devOptions.enabled` is on, so install and offline behaviour can be exercised
  with `npm run dev`; `devOptions.navigateFallback` is what lets the dev worker
  bind navigations to `index.html`.
- Icons are SVG (`public/icon.svg`). Chrome accepts SVG for installability; add
  192px and 512px PNGs to `public/` and list them in the `manifest.icons` array
  in `vite.config.ts` if you need wider platform coverage.

## Accessibility

- Pinch zoom is allowed (no `user-scalable=no`), and the nickname field is
  16 px so iOS does not zoom the page when it is focused.
- Keyboard focus shows an amber ring (`--accent`, the same as the public map).
- Notices that stop recording working are `role="alert"`; the upload status is
  an `aria-live` region, so screen readers hear "Uploaded. Thank you!".
- Buttons are at least 44 px tall and text at least 0.8 rem.
- `prefers-reduced-motion` turns off the blinking, the marker pulse and the
  map's follow animation.

## Scripts

```bash
npm run dev        # dev server
npm run dev:lan    # dev server on the LAN (see the secure-context caveat above)
npm run build      # typecheck + production build
npm run preview    # serve the production build
npm run typecheck  # vue-tsc --noEmit, plus tsc for the service worker
npm test           # vitest: the stationary detector
```
