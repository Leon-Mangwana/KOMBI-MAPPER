<script setup lang="ts">
/**
 * First-run explanation of what the app is for and how to use it, reopened
 * from the "?" button. A native <dialog>, so the browser handles focus
 * trapping, Esc, and the backdrop.
 */
import { computed, onMounted, ref, watch } from 'vue'

import { availableLocales, type Locale, locale, setLocale, t } from '@/i18n'
import { installAvailable, isStandalone, promptInstall } from '@/lib/install'
import { useRecordingStore } from '@/stores/recording'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ close: [] }>()

const store = useRecordingStore()
const dialog = ref<HTMLDialogElement | null>(null)

const canInstall = computed(() => installAvailable.value)
const titleId = 'welcome-title'

// Changing language reloads the page, so not during a ride or an upload.
const languageLocked = computed(() => store.isRecording || store.uploadPhase === 'uploading')

function onLanguageChange(event: Event): void {
  const code = (event.target as HTMLSelectElement).value as Locale
  if (code !== locale) setLocale(code)
}

function sync(): void {
  const el = dialog.value
  if (!el) return
  // hasAttribute rather than el.open, so the fallback below behaves the same.
  const isOpen = el.hasAttribute('open')
  if (props.open && !isOpen) {
    if (typeof el.showModal === 'function') el.showModal()
    else el.setAttribute('open', '')
  } else if (!props.open && isOpen) {
    if (typeof el.close === 'function') el.close()
    else el.removeAttribute('open')
  }
}

function close(): void {
  emit('close')
}

/** A click on the backdrop lands on the <dialog> itself, not its content. */
function onDialogClick(event: MouseEvent): void {
  if (event.target === dialog.value) close()
}

onMounted(sync)
watch(() => props.open, sync)
</script>

<template>
  <dialog
    ref="dialog"
    class="sheet"
    :aria-labelledby="titleId"
    @close="close"
    @click="onDialogClick"
  >
    <div class="sheet__body">
      <!-- First focusable element, so the keyboard does not pop up on open. -->
      <button type="button" class="sheet__close" :aria-label="t.welcome.close" @click="close">
        ×
      </button>

      <h2 :id="titleId" class="sheet__title">{{ t.welcome.title }}</h2>

      <!-- Hidden until a second language is translated and marked ready. -->
      <label v-if="availableLocales.length > 1" class="field field--inline">
        <span class="field__label">{{ t.welcome.language }}</span>
        <select
          class="field__input"
          :value="locale"
          :disabled="languageLocked"
          @change="onLanguageChange"
        >
          <option v-for="option in availableLocales" :key="option.code" :value="option.code">
            {{ option.name }}
          </option>
        </select>
      </label>
      <p class="sheet__intro">{{ t.welcome.intro }}</p>

      <ol class="steps">
        <li v-for="(step, index) in t.welcome.steps" :key="step.title" class="step">
          <span class="step__number" aria-hidden="true">{{ index + 1 }}</span>
          <div>
            <p class="step__title">{{ step.title }}</p>
            <p class="step__body">{{ step.body }}</p>
          </div>
        </li>
      </ol>

      <label class="field">
        <span class="field__label">{{ t.welcome.nickname }}</span>
        <input
          class="field__input"
          type="text"
          inputmode="text"
          autocomplete="nickname"
          maxlength="255"
          :placeholder="t.header.nicknamePlaceholder"
          :value="store.contributor"
          :disabled="store.isRecording"
          @input="store.setContributor(($event.target as HTMLInputElement).value)"
        />
        <span class="field__help">
          {{ store.isRecording ? t.welcome.nicknameLocked : t.welcome.nicknameHelp }}
        </span>
      </label>

      <p class="sheet__privacy">{{ t.welcome.privacy }}</p>

      <button v-if="canInstall" type="button" class="btn sheet__install" @click="promptInstall()">
        {{ t.welcome.install }}
      </button>
      <p v-else-if="!isStandalone" class="sheet__tip">{{ t.welcome.installTip }}</p>

      <button type="button" class="btn btn--primary sheet__continue" @click="close">
        {{ t.welcome.continue }}
      </button>
    </div>
  </dialog>
</template>

<style scoped>
.sheet {
  width: 100%;
  max-width: 32rem;
  max-height: 90vh;
  max-height: 90dvh;
  margin: auto auto 0;
  padding: 0;
  overflow-y: auto;
  border: 1px solid var(--line);
  border-bottom: none;
  border-radius: 1rem 1rem 0 0;
  background: var(--surface);
  color: var(--text);
}

.sheet::backdrop {
  background: rgb(0 0 0 / 60%);
}

.sheet__body {
  position: relative;
  padding: 1.25rem 1.1rem calc(env(safe-area-inset-bottom) + 1rem);
}

.sheet__close {
  position: absolute;
  top: 0.5rem;
  right: 0.5rem;
  width: 2.75rem;
  height: 2.75rem;
  border: none;
  border-radius: 50%;
  background: transparent;
  color: var(--text-muted);
  font-size: 1.6rem;
  line-height: 1;
}

.sheet__title {
  margin: 0 2.5rem 0.5rem 0;
  font-size: 1.3rem;
  line-height: 1.25;
}

.sheet__intro {
  margin: 0 0 1rem;
  font-size: 1rem;
  line-height: 1.45;
  color: var(--text-muted);
}

.steps {
  display: flex;
  flex-direction: column;
  gap: 0.85rem;
  margin: 0 0 1.1rem;
  padding: 0;
  list-style: none;
}

.step {
  display: flex;
  gap: 0.75rem;
  align-items: flex-start;
}

.step p {
  margin: 0;
}

.step__number {
  flex: none;
  display: grid;
  place-items: center;
  width: 1.75rem;
  height: 1.75rem;
  border: 2px solid var(--accent);
  border-radius: 50%;
  color: var(--accent);
  font-weight: 700;
}

.step__title {
  font-size: 1.05rem;
  font-weight: 700;
}

.step__body {
  margin-top: 0.15rem;
  font-size: 0.95rem;
  line-height: 1.4;
  color: var(--text-muted);
}

.field {
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
  margin-bottom: 0.9rem;
}

.field__label {
  font-size: 0.9rem;
  font-weight: 600;
}

.field__input {
  min-height: 2.75rem;
  padding: 0.5rem 0.65rem;
  border: 1px solid var(--line-strong);
  border-radius: 0.5rem;
  background: var(--bg);
  color: var(--text);
  font-size: 1rem;
}

.field__input:disabled {
  opacity: 0.6;
}

.field--inline {
  flex-direction: row;
  align-items: center;
  justify-content: space-between;
  gap: 0.75rem;
}

.field--inline .field__input {
  flex: 0 1 12rem;
}

.field__help {
  font-size: 0.85rem;
  color: var(--text-muted);
}

.sheet__privacy,
.sheet__tip {
  margin: 0 0 0.9rem;
  font-size: 0.9rem;
  line-height: 1.4;
  color: var(--text-muted);
}

.sheet__install {
  width: 100%;
  margin-bottom: 0.75rem;
}

.sheet__continue {
  width: 100%;
  min-height: 3.25rem;
  font-size: 1.05rem;
}
</style>
