<script setup lang="ts">
/**
 * What this map is, where the data comes from, and what is kept about the
 * people who record it. Opened from the footer. A native <dialog>, so the
 * browser handles focus trapping, Esc, and the backdrop.
 *
 * The privacy text states what the code does — see api/app/security.py for
 * who can read a raw trace, and capture/README.md#data-shape for what a ride
 * contains — and should change when that does.
 */
import { onMounted, ref, watch } from 'vue'

import { contactUrl } from '@/lib/contact'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ close: [] }>()

const dialog = ref<HTMLDialogElement | null>(null)
const titleId = 'about-title'

function sync(): void {
  const el = dialog.value
  if (!el) return
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
    class="about"
    :aria-labelledby="titleId"
    @close="close"
    @click="onDialogClick"
  >
    <div class="about__body">
      <button type="button" class="about__close" aria-label="Close" @click="close">×</button>

      <h2 :id="titleId" class="about__title">About this map</h2>

      <p>
        Harare's kombis run without a published map. This one is drawn from rides that
        volunteers record with the
        <a href="https://kombi-mapper.netlify.app">Kombi Capture</a> app on their own
        phones. Many rides along the same way are combined into one route and its stops;
        a route appears here only once several people have ridden it.
      </p>
      <p>
        Nothing here is official. A line is the most typical of the recorded rides, not a
        timetable or a promise that a kombi will take that road today.
      </p>

      <h3 class="about__heading">What is collected, and who sees it</h3>
      <ul class="about__list">
        <li>
          <strong>From a rider:</strong> the GPS positions of the ride, with their times,
          accuracy and speed, a nickname of the rider's choosing, and, if they typed one,
          the name they call the route. No account, phone number or device identifier is
          asked for or stored.
        </li>
        <li>
          <strong>Public:</strong> only the combined routes and stops shown on this map and
          in the GTFS feed. Individual rides and nicknames are never published.
        </li>
        <li>
          <strong>Project team only:</strong> the individual rides and nicknames, kept so
          routes can be rebuilt as more rides arrive. They are not shared or sold.
        </li>
        <li>
          <strong>This site</strong> sets no cookies and runs no analytics. The capture app
          keeps rides on the phone until they have uploaded.
        </li>
      </ul>
      <p>
        To have a ride or a nickname removed, get in touch and say which nickname it was
        recorded under.
      </p>

      <p v-if="contactUrl" class="about__contact">
        Something wrong, or a question?
        <a :href="contactUrl" target="_blank" rel="noopener">Message the project team</a>.
      </p>

      <p class="about__source">
        Open source under the MIT licence:
        <a href="https://github.com/Leon-Mangwana/KOMBI-MAPPER" target="_blank" rel="noopener">
          source code
        </a>
      </p>
    </div>
  </dialog>
</template>

<style scoped>
.about {
  width: 100%;
  max-width: 34rem;
  max-height: 90vh;
  max-height: 90dvh;
  margin: auto;
  padding: 0;
  overflow-y: auto;
  border: 1px solid var(--line);
  border-radius: 0.8rem;
  background: var(--surface);
  color: var(--text);
  font-size: 0.9rem;
  line-height: 1.55;
}

.about::backdrop {
  background: rgb(0 0 0 / 60%);
}

.about__body {
  position: relative;
  padding: 1.25rem 1.25rem 1.1rem;
}

.about__body p {
  margin: 0 0 0.75rem;
}

.about__close {
  position: absolute;
  top: 0.5rem;
  right: 0.5rem;
  width: 2.5rem;
  height: 2.5rem;
  border: none;
  border-radius: 50%;
  background: transparent;
  color: var(--text-muted);
  font-size: 1.5rem;
  line-height: 1;
}

.about__title {
  margin: 0 2.5rem 0.75rem 0;
  font-size: 1.15rem;
}

.about__heading {
  margin: 1rem 0 0.4rem;
  font-size: 0.95rem;
}

.about__list {
  margin: 0 0 0.75rem;
  padding-left: 1.1rem;
}

.about__list li + li {
  margin-top: 0.35rem;
}

.about__contact {
  padding-top: 0.5rem;
  border-top: 1px solid var(--line);
}

.about__source {
  color: var(--text-muted);
  font-size: 0.8rem;
}

.about a {
  color: var(--accent);
}
</style>
