<script setup lang="ts">
import { computed, onMounted } from 'vue'

import MapView from '@/components/MapView.vue'
import { formatAccuracy, formatElapsed } from '@/lib/format'
import { useRecordingStore } from '@/stores/recording'

const store = useRecordingStore()

onMounted(() => {
  void store.init()
})

const elapsed = computed(() => formatElapsed(store.elapsedMs))
const accuracy = computed(() => formatAccuracy(store.lastPosition?.accuracyM ?? null))

const gpsLabel = computed(() => {
  switch (store.gps) {
    case 'fixed':
      return `GPS ±${accuracy.value}`
    case 'acquiring':
      return 'Acquiring GPS…'
    case 'unavailable':
      return 'No GPS signal'
    case 'denied':
      return 'Location denied'
    case 'unsupported':
      return 'No GPS'
    default:
      return 'GPS idle'
  }
})

const queueLabel = computed(() => {
  if (store.uploading) return 'Uploading…'
  if (store.queued.length > 0) {
    return `${store.queued.length} trace${store.queued.length === 1 ? '' : 's'} waiting to upload`
  }
  if (store.failed.length > 0) {
    return `${store.failed.length} trace${store.failed.length === 1 ? '' : 's'} rejected`
  }
  if (store.uploadedCount > 0) return `${store.uploadedCount} uploaded`
  return null
})

function toggle(): void {
  if (store.isRecording) {
    void store.stop()
  } else {
    void store.start()
  }
}
</script>

<template>
  <div class="app">
    <header class="bar">
      <label class="bar__field">
        <span class="bar__label">Contributor</span>
        <input
          class="bar__input"
          type="text"
          inputmode="text"
          autocomplete="nickname"
          placeholder="your name"
          maxlength="255"
          :value="store.contributor"
          :disabled="store.isRecording"
          @input="store.setContributor(($event.target as HTMLInputElement).value)"
        />
      </label>

      <div class="bar__chips">
        <span class="chip" :class="`chip--${store.gps}`">{{ gpsLabel }}</span>
        <span v-if="!store.online" class="chip chip--offline">Offline</span>
        <span
          v-if="store.isRecording && store.wakeLockSupported && !store.wakeLockActive"
          class="chip chip--warn"
          title="The screen may switch off; recording continues while the tab is alive."
          >Screen may sleep</span
        >
      </div>
    </header>

    <MapView />

    <section class="panel">
      <div class="stats">
        <div class="stat">
          <span class="stat__value">{{ store.pointCount }}</span>
          <span class="stat__label">points</span>
        </div>
        <div class="stat">
          <span class="stat__value stat__value--mono">{{ elapsed }}</span>
          <span class="stat__label">elapsed</span>
        </div>
        <div class="stat">
          <span class="stat__value">{{ accuracy }}</span>
          <span class="stat__label">accuracy</span>
        </div>
      </div>

      <p v-if="store.gpsMessage" class="message" :class="{ 'message--error': store.gps === 'denied' }">
        {{ store.gpsMessage }}
        <span v-if="store.gps === 'denied'" class="message__hint">
          Allow location for this site in your browser settings, then press Start again.
        </span>
      </p>

      <p v-else-if="store.lastUploadError" class="message message--error">
        Upload problem: {{ store.lastUploadError }}
      </p>

      <button
        class="record"
        :class="store.isRecording ? 'record--stop' : 'record--start'"
        type="button"
        :disabled="!store.isRecording && !store.canStart"
        @click="toggle"
      >
        {{ store.isRecording ? 'Stop' : 'Start' }}
      </button>

      <p v-if="!store.isRecording && !store.contributor.trim()" class="hint">
        Enter your name above to start recording.
      </p>

      <div v-if="queueLabel" class="queue">
        <span>{{ queueLabel }}</span>
        <button
          v-if="store.queued.length > 0 && !store.uploading && store.online"
          class="queue__action"
          type="button"
          @click="store.flushQueue()"
        >
          Retry now
        </button>
      </div>

      <ul v-if="store.failed.length > 0" class="failed">
        <li v-for="trace in store.failed" :key="trace.id" class="failed__item">
          <span class="failed__text">
            {{ trace.pointCount }} points ·
            {{ new Date(trace.startedAt).toLocaleString() }} — {{ trace.lastError }}
          </span>
          <span class="failed__actions">
            <button type="button" @click="store.retry(trace.id)">Retry</button>
            <button type="button" class="failed__discard" @click="store.discard(trace.id)">
              Discard
            </button>
          </span>
        </li>
      </ul>
    </section>
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
  gap: 0.75rem;
  padding: calc(env(safe-area-inset-top) + 0.6rem) 0.9rem 0.6rem;
  background: var(--surface);
  border-bottom: 1px solid var(--line);
}

.bar__field {
  display: flex;
  flex-direction: column;
  gap: 0.15rem;
  min-width: 0;
  flex: 1;
}

.bar__label {
  font-size: 0.65rem;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: var(--text-muted);
}

.bar__input {
  width: 100%;
  padding: 0.35rem 0.5rem;
  border: 1px solid var(--line);
  border-radius: 0.4rem;
  background: var(--bg);
  color: var(--text);
  font-size: 0.95rem;
}

.bar__input:disabled {
  opacity: 0.6;
}

.bar__chips {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 0.35rem;
}

.chip {
  padding: 0.2rem 0.5rem;
  border-radius: 999px;
  background: rgb(148 163 184 / 15%);
  color: var(--text-muted);
  font-size: 0.7rem;
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

/* ---- bottom panel ---- */
.panel {
  flex: none;
  padding: 0.9rem 0.9rem calc(env(safe-area-inset-bottom) + 0.9rem);
  background: var(--surface);
  border-top: 1px solid var(--line);
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
  font-size: 0.7rem;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: var(--text-muted);
}

.message {
  margin: 0 0 0.75rem;
  padding: 0.55rem 0.7rem;
  border-radius: 0.5rem;
  background: rgb(245 158 11 / 12%);
  color: var(--warn);
  font-size: 0.85rem;
}

.message--error {
  background: rgb(239 68 68 / 12%);
  color: var(--danger);
}

.message__hint {
  display: block;
  margin-top: 0.25rem;
  color: var(--text-muted);
}

.record {
  width: 100%;
  min-height: 4.5rem;
  border: none;
  border-radius: 1rem;
  font-size: 1.5rem;
  font-weight: 700;
  letter-spacing: 0.02em;
  color: #f8fafc;
  transition: transform 0.08s ease;
}

.record:active:not(:disabled) {
  transform: scale(0.985);
}

.record:disabled {
  opacity: 0.45;
}

.record--start {
  background: linear-gradient(180deg, #16a34a, #15803d);
}

.record--stop {
  background: linear-gradient(180deg, #dc2626, #b91c1c);
}

.hint,
.queue {
  margin: 0.6rem 0 0;
  font-size: 0.8rem;
  color: var(--text-muted);
}

.queue {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.5rem;
}

.queue__action {
  padding: 0.3rem 0.6rem;
  border: 1px solid var(--line);
  border-radius: 0.4rem;
  background: transparent;
  color: var(--text);
  font-size: 0.75rem;
  font-weight: 600;
}

.failed {
  margin: 0.6rem 0 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 0.4rem;
}

.failed__item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.5rem;
  padding: 0.45rem 0.6rem;
  border: 1px solid var(--line);
  border-radius: 0.5rem;
  font-size: 0.75rem;
  color: var(--text-muted);
}

.failed__text {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.failed__actions {
  display: flex;
  flex: none;
  gap: 0.35rem;
}

.failed__actions button {
  padding: 0.25rem 0.5rem;
  border: 1px solid var(--line);
  border-radius: 0.35rem;
  background: transparent;
  color: var(--text);
  font-size: 0.72rem;
}

.failed__discard {
  color: var(--danger);
}
</style>
