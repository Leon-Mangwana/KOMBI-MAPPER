<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'

import MapView from '@/components/MapView.vue'
import UploadStatus from '@/components/UploadStatus.vue'
import WelcomeSheet from '@/components/WelcomeSheet.vue'
import { t } from '@/i18n'
import { formatAccuracy, formatElapsed } from '@/lib/format'
import { applyUpdate, updateReady } from '@/lib/updates'
import { type NoticeKind, useRecordingStore } from '@/stores/recording'

const store = useRecordingStore()

onMounted(() => {
  void store.init()
})

// ---- first-run welcome sheet --------------------------------------------
const WELCOME_KEY = 'kombi-capture:welcome:v1'

function welcomeSeen(): boolean {
  try {
    return localStorage.getItem(WELCOME_KEY) === 'seen'
  } catch {
    return false
  }
}

const welcomeOpen = ref(!welcomeSeen())

function openWelcome(): void {
  welcomeOpen.value = true
}

function closeWelcome(): void {
  welcomeOpen.value = false
  try {
    localStorage.setItem(WELCOME_KEY, 'seen')
  } catch {
    // Not remembered; it simply shows again next time.
  }
}

// ---- header -------------------------------------------------------------
const nicknameInput = ref<HTMLInputElement | null>(null)

const elapsed = computed(() => formatElapsed(store.elapsedMs))
// Only meaningful while a recording is running; idle shows no stale value.
const accuracy = computed(() =>
  formatAccuracy(store.isRecording ? (store.lastPosition?.accuracyM ?? null) : null),
)

const gpsLabel = computed<string | null>(() => {
  switch (store.gps) {
    case 'fixed':
      return t.header.gps.fixed(accuracy.value)
    case 'acquiring':
      return t.header.gps.acquiring
    case 'unavailable':
      return t.header.gps.unavailable
    case 'denied':
      return t.header.gps.denied
    case 'unsupported':
      return t.header.gps.unsupported
    default:
      return null
  }
})

// ---- panel --------------------------------------------------------------
/** Notices that stop recording from working at all, as opposed to advice. */
const ERROR_NOTICES: ReadonlySet<NoticeKind> = new Set<NoticeKind>([
  'unsupported',
  'insecure',
  'blocked',
  'denied',
  'save-failed',
])

const noticeText = computed(() => (store.notice ? t.notice[store.notice.kind] : null))
const noticeDetail = computed(() => store.notice?.detail ?? null)
const noticeIsError = computed(() => (store.notice ? ERROR_NOTICES.has(store.notice.kind) : false))

const startBlocked = computed(() => !store.isRecording && !store.canStart)
const nicknameMissing = computed(() => !store.isRecording && !store.contributor.trim())

const hint = computed<string | null>(() => {
  if (store.isRecording) return store.hasFix ? t.record.recordingHint : t.record.findingHint
  if (startBlocked.value || store.notice || store.receipt) return null
  return t.record.readyHint
})

// The first fix of a ride is the moment recording actually begins. A short
// buzz tells a rider with the phone in their lap that it worked; before it,
// pressing Stop saves nothing. (No-op where vibration is unsupported, e.g.
// iPhone.)
watch(
  () => store.hasFix,
  (hasFix, hadFix) => {
    if (hasFix && !hadFix && store.isRecording) navigator.vibrate?.(200)
  },
)

// ---- new version ----------------------------------------------------------
// Offered only between rides and uploads: reloading mid-ride would end the
// ride, and mid-upload would send it again.
const showUpdate = computed(
  () => updateReady.value && !store.isRecording && store.uploadPhase !== 'uploading',
)

const recordClass = computed(() => {
  if (store.isRecording) return 'record--stop'
  return startBlocked.value ? 'record--blocked' : 'record--start'
})

function onRecordPress(): void {
  if (store.isRecording) {
    void store.stop()
    return
  }
  // The button stays focusable when blocked (aria-disabled, not disabled), so
  // a tap can take the rider straight to what is missing.
  if (startBlocked.value) {
    nicknameInput.value?.focus()
    return
  }
  void store.start()
}
</script>

<template>
  <div class="app">
    <header class="bar">
      <label class="bar__field">
        <span class="bar__label">{{ t.header.nickname }}</span>
        <input
          ref="nicknameInput"
          class="bar__input"
          type="text"
          inputmode="text"
          autocomplete="nickname"
          maxlength="255"
          :placeholder="t.header.nicknamePlaceholder"
          :value="store.contributor"
          :disabled="store.isRecording"
          @input="store.setContributor(($event.target as HTMLInputElement).value)"
        />
      </label>

      <div class="bar__chips">
        <span v-if="gpsLabel" class="chip" :class="`chip--${store.gps}`">{{ gpsLabel }}</span>
        <span v-if="!store.online" class="chip chip--offline">{{ t.header.offline }}</span>
        <span
          v-if="store.isRecording && store.wakeLockSupported && !store.wakeLockActive"
          class="chip chip--warn"
          :title="t.header.screenMaySleepTitle"
          >{{ t.header.screenMaySleep }}</span
        >
      </div>

      <button
        type="button"
        class="bar__help"
        :aria-label="t.header.help"
        :title="t.header.help"
        @click="openWelcome"
      >
        ?
      </button>
    </header>

    <MapView />

    <section class="panel">
      <div v-if="showUpdate" class="update" role="status">
        <span class="update__text">{{ t.update.ready }}</span>
        <button type="button" class="btn btn--primary" @click="applyUpdate">
          {{ t.update.apply }}
        </button>
      </div>

      <div v-if="store.isRecording" class="stats">
        <div class="stat">
          <span class="stat__value">{{ store.pointCount }}</span>
          <span class="stat__label">{{ t.stats.points }}</span>
        </div>
        <div class="stat">
          <span class="stat__value stat__value--mono">{{ elapsed }}</span>
          <span class="stat__label">{{ t.stats.rideTime }}</span>
        </div>
        <div class="stat">
          <span class="stat__value">{{ accuracy }}</span>
          <span class="stat__label">{{ t.stats.accuracy }}</span>
        </div>
      </div>

      <p
        v-if="noticeText"
        class="message"
        :class="{ 'message--error': noticeIsError }"
        :role="noticeIsError ? 'alert' : 'status'"
      >
        {{ noticeText }}
        <small v-if="noticeDetail" class="message__detail">{{ noticeDetail }}</small>
      </p>

      <p v-if="hint" class="hint">{{ hint }}</p>

      <button
        class="record"
        :class="recordClass"
        type="button"
        :aria-disabled="startBlocked ? 'true' : undefined"
        @click="onRecordPress"
      >
        <span class="record__label">{{ store.isRecording ? t.record.stop : t.record.start }}</span>
        <span v-if="startBlocked" class="record__sub">{{ t.record.needsNickname }}</span>
      </button>

      <p v-if="nicknameMissing" class="privacy">{{ t.record.privacy }}</p>

      <UploadStatus />
    </section>

    <WelcomeSheet :open="welcomeOpen" @close="closeWelcome" />
  </div>
</template>

<style scoped>
.app {
  display: flex;
  flex-direction: column;
  height: 100%;
}

/* ---- header ---- */
.bar {
  display: flex;
  align-items: center;
  gap: 0.6rem;
  padding: calc(env(safe-area-inset-top) + 0.6rem) 0.9rem 0.6rem;
  background: var(--surface);
  border-bottom: 1px solid var(--line);
}

.bar__field {
  display: flex;
  flex-direction: column;
  gap: 0.2rem;
  min-width: 0;
  flex: 1;
}

.bar__label {
  font-size: 0.8rem;
  font-weight: 600;
  color: var(--text-muted);
}

.bar__input {
  width: 100%;
  min-height: 2.75rem;
  padding: 0.4rem 0.6rem;
  border: 1px solid var(--line-strong);
  border-radius: 0.5rem;
  background: var(--bg);
  color: var(--text);
  /* 16px: anything smaller makes iOS zoom the page when the field is focused. */
  font-size: 1rem;
}

.bar__input:disabled {
  opacity: 0.6;
}

.bar__chips {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 0.3rem;
  max-width: 45%;
}

.chip {
  padding: 0.25rem 0.6rem;
  border-radius: 999px;
  background: rgb(148 163 184 / 15%);
  color: var(--text-muted);
  font-size: 0.8rem;
  font-weight: 600;
  white-space: nowrap;
}

.chip--fixed {
  background: rgb(34 197 94 / 18%);
  color: var(--ok);
}

.chip--acquiring,
.chip--warn,
.chip--unavailable {
  background: rgb(245 158 11 / 18%);
  color: var(--warn);
}

.chip--denied,
.chip--unsupported,
.chip--offline {
  background: rgb(239 68 68 / 18%);
  color: var(--danger);
}

.bar__help {
  flex: none;
  width: 2.75rem;
  height: 2.75rem;
  border: 1px solid var(--line-strong);
  border-radius: 50%;
  background: transparent;
  color: var(--text);
  font-size: 1.1rem;
  font-weight: 700;
}

/* ---- bottom panel ---- */
.panel {
  /* Shrinks and scrolls before the map does; MapView keeps its minimum. */
  flex: 0 1 auto;
  min-height: 0;
  overflow-y: auto;
  overscroll-behavior: contain;
  padding: 0.9rem 0.9rem calc(env(safe-area-inset-bottom) + 0.9rem);
  background: var(--surface);
  border-top: 1px solid var(--line);
}

.update {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.75rem;
  margin: 0 0 0.75rem;
  padding: 0.5rem 0.5rem 0.5rem 0.8rem;
  border: 1px solid var(--accent);
  border-radius: 0.6rem;
  background: rgb(251 191 36 / 8%);
}

.update__text {
  font-size: 0.95rem;
  font-weight: 600;
}

.stats {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 0.5rem;
  margin-bottom: 0.85rem;
}

.stat {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 0.15rem;
}

.stat__value {
  font-size: 1.45rem;
  font-weight: 700;
  line-height: 1.1;
}

.stat__value--mono {
  font-variant-numeric: tabular-nums;
}

.stat__label {
  font-size: 0.8rem;
  color: var(--text-muted);
}

.message {
  margin: 0 0 0.75rem;
  padding: 0.6rem 0.75rem;
  border-radius: 0.5rem;
  background: rgb(245 158 11 / 12%);
  color: var(--warn);
  font-size: 0.95rem;
  line-height: 1.4;
}

.message--error {
  background: rgb(248 113 113 / 12%);
  color: var(--danger);
}

.message__detail {
  display: block;
  margin-top: 0.25rem;
  font-size: 0.8rem;
  color: var(--text-muted);
  word-break: break-word;
}

.hint {
  margin: 0 0 0.6rem;
  font-size: 0.9rem;
  color: var(--text-muted);
  text-align: center;
}

.record {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  width: 100%;
  min-height: 4.5rem;
  padding: 0.5rem 1rem;
  border: none;
  border-radius: 1rem;
  font-size: 1.5rem;
  font-weight: 700;
  letter-spacing: 0.02em;
  color: #f8fafc;
  transition: transform 0.08s ease;
}

.record:active:not(.record--blocked) {
  transform: scale(0.985);
}

.record--start {
  background: linear-gradient(180deg, #16a34a, #15803d);
}

.record--stop {
  background: linear-gradient(180deg, #dc2626, #b91c1c);
}

/* Blocked, not broken: solid, dashed, full-contrast text, reason inside. */
.record--blocked {
  background: var(--line);
  border: 2px dashed var(--line-strong);
  color: var(--text);
  cursor: pointer;
}

.record__sub {
  display: block;
  margin-top: 0.2rem;
  font-size: 0.95rem;
  font-weight: 600;
  letter-spacing: normal;
  color: var(--text-muted);
}

.privacy {
  margin: 0.6rem 0 0;
  font-size: 0.85rem;
  color: var(--text-muted);
  text-align: center;
}
</style>
