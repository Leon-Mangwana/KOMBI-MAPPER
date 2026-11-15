<script setup lang="ts">
/**
 * Everything below the record button about rides already recorded: the
 * receipt for the ride just stopped, what the upload queue is doing, the
 * running contribution count, and rides the server refused.
 */
import { computed, ref, watch } from 'vue'

import { t } from '@/i18n'
import { useRecordingStore } from '@/stores/recording'

const store = useRecordingStore()

const phaseText = computed<string | null>(() => {
  const n = store.queued.length
  switch (store.uploadPhase) {
    case 'uploading':
      return store.uploadSlow ? t.upload.slow : t.upload.uploading(n)
    case 'offline':
      return t.upload.offline(n)
    case 'retrying':
      return t.upload.retrying(store.uploadIssue?.kind ?? 'network', n)
    case 'waiting':
      return t.upload.waiting(n)
    default:
      return null
  }
})

const showRetryNow = computed(
  () => store.uploadPhase === 'retrying' || store.uploadPhase === 'waiting',
)

const issueDetail = computed(() => {
  const issue = store.uploadIssue
  if (store.uploadPhase !== 'retrying' || !issue) return null
  return t.failed.technical(issue.status, issue.detail)
})

const receiptTitle = computed(() =>
  store.receipt?.phase === 'uploaded' ? t.receipt.uploaded : t.receipt.saved,
)

const receiptSummary = computed(() =>
  store.receipt ? t.receipt.summary(store.receipt.pointCount, store.receipt.durationMs) : '',
)

const showContributed = computed(
  () => !store.receipt && !store.isRecording && store.uploadedCount > 0,
)

// ---- "Which route was this?" ------------------------------------------
// Shown inside the receipt while the store is holding that ride's upload for
// an answer. Save or Skip releases it; so does putting the phone away.
const hintInput = ref('')
const hintSaving = ref(false)

const askingHint = computed(
  () => store.receipt !== null && store.awaitingHint === store.receipt.traceId,
)

// A fresh ride gets an empty box, not the last ride's answer.
watch(
  () => store.awaitingHint,
  () => {
    hintInput.value = ''
  },
)

async function saveHint(): Promise<void> {
  if (hintSaving.value) return
  hintSaving.value = true
  try {
    await store.saveHint(hintInput.value)
  } finally {
    hintSaving.value = false
  }
}

function discard(traceId: string): void {
  // Discarding deletes the only copy of the ride, so ask first.
  if (window.confirm(t.failed.discardConfirm)) void store.discard(traceId)
}
</script>

<template>
  <div class="upload">
    <!-- Always in the DOM so that changes inside it are announced. -->
    <div class="upload__live" aria-live="polite">
      <div
        v-if="store.receipt"
        class="receipt"
        :class="{ 'receipt--uploaded': store.receipt.phase === 'uploaded' }"
      >
        <p class="receipt__title">{{ receiptTitle }}</p>
        <p class="receipt__summary">{{ receiptSummary }}</p>
        <p v-if="store.receipt.routeHint" class="receipt__route">
          {{ t.receipt.route(store.receipt.routeHint) }}
        </p>

        <form v-if="askingHint" class="hint" @submit.prevent="saveHint">
          <label class="hint__label" for="route-hint">{{ t.hint.label }}</label>
          <p class="hint__help">{{ t.hint.help }}</p>
          <input
            id="route-hint"
            v-model="hintInput"
            class="hint__input"
            type="text"
            inputmode="text"
            autocomplete="off"
            enterkeyhint="done"
            maxlength="255"
            :placeholder="t.hint.placeholder"
          />
          <div class="hint__actions">
            <button type="submit" class="btn btn--primary" :disabled="hintSaving">
              {{ t.hint.save }}
            </button>
            <button type="button" class="btn" :disabled="hintSaving" @click="store.skipHint()">
              {{ t.hint.skip }}
            </button>
          </div>
        </form>
      </div>

      <div v-if="phaseText" class="phase">
        <p class="phase__text">{{ phaseText }}</p>
        <button v-if="showRetryNow" type="button" class="btn phase__retry" @click="store.retryNow()">
          {{ t.upload.retryNow }}
        </button>
      </div>

      <details v-if="issueDetail" class="technical">
        <summary>{{ t.upload.details }}</summary>
        <p>{{ issueDetail }}</p>
      </details>

      <p v-if="showContributed" class="contributed">
        {{ t.upload.contributed(store.uploadedCount) }}
      </p>
    </div>

    <section v-if="store.failed.length > 0 && !store.isRecording" class="failed">
      <h2 class="failed__heading">{{ t.failed.heading(store.failed.length) }}</h2>
      <ul class="failed__list">
        <li v-for="trace in store.failed" :key="trace.id" class="failed__item">
          <p class="failed__reason">{{ t.failed.reason[trace.lastErrorKind ?? 'rejected'] }}</p>
          <p class="failed__meta">{{ t.failed.meta(trace.pointCount, trace.startedAt) }}</p>
          <details v-if="trace.lastError" class="technical">
            <summary>{{ t.upload.details }}</summary>
            <p>{{ t.failed.technical(trace.lastErrorStatus ?? null, trace.lastError) }}</p>
          </details>
          <div class="failed__actions">
            <button type="button" class="btn" @click="store.retry(trace.id)">
              {{ t.failed.tryAgain }}
            </button>
            <button type="button" class="btn btn--danger" @click="discard(trace.id)">
              {{ t.failed.discard }}
            </button>
          </div>
        </li>
      </ul>
    </section>
  </div>
</template>

<style scoped>
.upload__live {
  display: flex;
  flex-direction: column;
  gap: 0.6rem;
  margin-top: 0.75rem;
}

.upload__live:empty {
  display: none;
}

p {
  margin: 0;
}

.receipt {
  padding: 0.65rem 0.8rem;
  border: 1px solid var(--line-strong);
  border-left-width: 4px;
  border-radius: 0.6rem;
}

.receipt--uploaded {
  border-left-color: var(--ok);
}

.receipt__title {
  font-size: 1rem;
  font-weight: 700;
}

.receipt__summary {
  margin-top: 0.15rem;
  font-size: 0.9rem;
  color: var(--text-muted);
}

.receipt__route {
  margin-top: 0.25rem;
  font-size: 0.9rem;
}

.hint {
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
  margin-top: 0.65rem;
  padding-top: 0.65rem;
  border-top: 1px solid var(--line);
}

.hint__label {
  font-size: 0.95rem;
  font-weight: 600;
}

.hint__help {
  font-size: 0.8rem;
  color: var(--text-muted);
}

.hint__input {
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

.hint__actions {
  display: flex;
  gap: 0.5rem;
  margin-top: 0.2rem;
}

.phase {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.75rem;
}

.phase__text {
  font-size: 0.9rem;
  color: var(--text-muted);
}

.phase__retry {
  flex: none;
}

.contributed {
  font-size: 0.9rem;
  color: var(--text-muted);
}

.technical {
  font-size: 0.8rem;
  color: var(--text-muted);
}

.technical summary {
  cursor: pointer;
  min-height: 1.75rem;
  display: flex;
  align-items: center;
}

.technical p {
  margin-top: 0.25rem;
  word-break: break-word;
}

.failed {
  margin-top: 0.9rem;
}

.failed__heading {
  margin: 0 0 0.5rem;
  font-size: 0.95rem;
  font-weight: 700;
  color: var(--danger);
}

.failed__list {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
  /* Room for one full ride card and the top of the next, so it reads as a
     list to scroll; any less and a card's buttons hide below the fold. */
  max-height: 16rem;
  margin: 0;
  padding: 0;
  overflow-y: auto;
  overscroll-behavior: contain;
  list-style: none;
}

.failed__item {
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
  padding: 0.6rem 0.7rem;
  border: 1px solid var(--line);
  border-radius: 0.6rem;
  font-size: 0.875rem;
}

.failed__meta {
  font-size: 0.8rem;
  color: var(--text-muted);
}

.failed__actions {
  display: flex;
  gap: 0.5rem;
}
</style>
