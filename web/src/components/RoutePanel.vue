<script setup lang="ts">
import { computed, ref } from 'vue'

import { formatLength, plural, routeName, stopName } from '@/lib/format'
import { useNetworkStore } from '@/stores/network'

const store = useNetworkStore()

const query = ref('')

/**
 * Filter on the name a visitor can actually see — including the `Route 12`
 * fallback, so typing an id finds an unnamed route too.
 */
const matches = computed(() => {
  const needle = query.value.trim().toLowerCase()
  if (!needle) return store.orderedRoutes
  return store.orderedRoutes.filter((route) => routeName(route).toLowerCase().includes(needle))
})
</script>

<template>
  <aside class="panel">
    <!-- Detail: one route and the stops along it. -->
    <template v-if="store.selectedRoute">
      <div class="panel__head">
        <button type="button" class="panel__back" @click="store.clearSelection()">
          ← All routes
        </button>
      </div>

      <div class="panel__body">
        <h2 class="detail__name">{{ routeName(store.selectedRoute) }}</h2>
        <p class="detail__meta">
          {{ formatLength(store.selectedRoute.length_m) }} ·
          {{ plural(store.selectedRoute.stop_count, 'stop') }} ·
          {{ plural(store.selectedRoute.trace_count, 'trace') }}
        </p>
        <p class="detail__note">
          Drawn from {{ plural(store.selectedRoute.trace_count, 'recorded ride') }}. The line is the
          most typical of them, not an official route.
        </p>

        <p v-if="store.detailLoading" class="panel__hint">Loading stops…</p>

        <ol v-else-if="store.selectedStops.length" class="stops">
          <li v-for="(stop, index) in store.selectedStops" :key="stop.id" class="stops__item">
            <span class="stops__seq">{{ index + 1 }}</span>
            <span class="stops__name">{{ stopName(stop.name, index) }}</span>
          </li>
        </ol>

        <p v-else class="panel__hint">
          No stops were detected on this route yet — it needs more recorded rides before dwells
          cluster into stops.
        </p>
      </div>
    </template>

    <!-- Index: every route in the network. -->
    <template v-else>
      <div class="panel__head">
        <h2 class="panel__title">Routes</h2>
        <p class="panel__totals">
          {{ plural(store.totals.routes, 'route') }} · {{ plural(store.totals.stops, 'stop') }}
        </p>
        <input
          v-model="query"
          type="search"
          class="panel__search"
          placeholder="Search routes"
          aria-label="Search routes"
        />
      </div>

      <div class="panel__body">
        <p v-if="store.status === 'loading'" class="panel__hint">Loading…</p>

        <p v-else-if="!matches.length && query" class="panel__hint">
          Nothing matches “{{ query }}”.
        </p>

        <ul v-else class="routes">
          <li v-for="route in matches" :key="route.id">
            <button
              type="button"
              class="routes__item"
              :class="{ 'routes__item--unnamed': !route.short_name && !route.long_name }"
              @click="store.select(route.id)"
            >
              <span class="routes__name">{{ routeName(route) }}</span>
              <span class="routes__meta">
                {{ formatLength(route.length_m) }} · {{ plural(route.stop_count, 'stop') }} ·
                {{ plural(route.trace_count, 'trace') }}
              </span>
            </button>
          </li>
        </ul>
      </div>
    </template>
  </aside>
</template>

<style scoped>
.panel {
  display: flex;
  flex-direction: column;
  min-height: 0;
  width: 22rem;
  max-width: 100%;
  border-left: 1px solid var(--line);
  background: var(--surface);
}

.panel__head {
  flex: none;
  padding: 1rem;
  border-bottom: 1px solid var(--line);
}

.panel__title {
  margin: 0;
  font-size: 1rem;
  letter-spacing: 0.02em;
}

.panel__totals {
  margin: 0.15rem 0 0;
  color: var(--text-muted);
  font-size: 0.8rem;
}

.panel__search {
  width: 100%;
  margin-top: 0.75rem;
  padding: 0.5rem 0.7rem;
  border: 1px solid var(--line);
  border-radius: 0.5rem;
  background: var(--bg);
  color: var(--text);
}

.panel__search::placeholder {
  color: var(--text-muted);
}

.panel__back {
  padding: 0.35rem 0;
  border: 0;
  background: none;
  color: var(--accent);
  font-size: 0.85rem;
  font-weight: 600;
}

.panel__body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 0.5rem 1rem 1.25rem;
}

.panel__hint {
  margin: 1rem 0;
  color: var(--text-muted);
  font-size: 0.85rem;
  line-height: 1.5;
}

.routes {
  margin: 0;
  padding: 0;
  list-style: none;
}

.routes__item {
  display: flex;
  flex-direction: column;
  gap: 0.15rem;
  width: 100%;
  padding: 0.6rem 0.5rem;
  border: 0;
  border-radius: 0.5rem;
  background: none;
  color: var(--text);
  text-align: left;
}

.routes__item:hover,
.routes__item:focus-visible {
  background: var(--bg);
}

.routes__name {
  font-weight: 600;
  font-size: 0.9rem;
}

/* An id-only name is a placeholder until somebody names the route. */
.routes__item--unnamed .routes__name {
  color: var(--text-muted);
  font-weight: 500;
}

.routes__meta {
  color: var(--text-muted);
  font-size: 0.78rem;
}

.detail__name {
  margin: 0.5rem 0 0.25rem;
  font-size: 1.05rem;
}

.detail__meta {
  margin: 0;
  color: var(--text-muted);
  font-size: 0.82rem;
}

.detail__note {
  margin: 0.75rem 0 1rem;
  color: var(--text-muted);
  font-size: 0.8rem;
  line-height: 1.5;
}

.stops {
  margin: 0;
  padding: 0;
  list-style: none;
}

.stops__item {
  display: flex;
  align-items: center;
  gap: 0.6rem;
  padding: 0.4rem 0;
  font-size: 0.87rem;
}

/* The line down the left reads as the route the stops sit on. */
.stops__item + .stops__item {
  border-top: 1px solid var(--line);
}

.stops__seq {
  flex: none;
  display: grid;
  place-items: center;
  width: 1.5rem;
  height: 1.5rem;
  border-radius: 50%;
  border: 1px solid var(--accent);
  color: var(--accent);
  font-size: 0.7rem;
  font-variant-numeric: tabular-nums;
}

@media (max-width: 48rem) {
  .panel {
    width: 100%;
    height: 45vh;
    border-left: 0;
    border-top: 1px solid var(--line);
  }
}
</style>
