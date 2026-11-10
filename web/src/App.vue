<script setup lang="ts">
import { onMounted, ref } from 'vue'

import MapView from '@/components/MapView.vue'
import RoutePanel from '@/components/RoutePanel.vue'
import { gtfsUrl } from '@/lib/api'
import { useAdminStore } from '@/stores/admin'
import { useNetworkStore } from '@/stores/network'

const store = useNetworkStore()
const admin = useAdminStore()

const tokenEntry = ref('')

// Whether there is a feed to link to. The feed is a file the pipeline
// exports, published separately from the network, and until the first export
// there is nothing at the URL: the API answers 404, and the deployed site's
// catch-all redirect answers with index.html — so a bare link would hand a
// visitor an HTML page named gtfs.zip. Ask before showing the link.
//
// A GET whose body is cancelled once the headers are in, rather than a HEAD:
// the API's feed route answers GET only, and HEAD there is a 405 that would
// hide a feed that exists. The question is whether the file is there, not
// what is in it, so nothing past the headers is read.
const feedAvailable = ref(false)

async function checkFeed(): Promise<void> {
  try {
    const response = await fetch(gtfsUrl)
    void response.body?.cancel()
    const type = response.headers.get('content-type') ?? ''
    feedAvailable.value = response.ok && !type.includes('text/html')
  } catch {
    feedAvailable.value = false
  }
}

onMounted(() => {
  void store.load()
  admin.watchHash()
  void checkFeed()
})

function signIn(): void {
  admin.signIn(tokenEntry.value)
  tokenEntry.value = ''
}
</script>

<template>
  <div class="app">
    <header class="header">
      <div>
        <h1 class="header__title">Kombi Routes</h1>
        <p class="header__subtitle">Harare's minibus network, mapped by the people who ride it</p>
      </div>
      <!-- A plain link, not a fetch: the feed is a file, and the browser's own
           download handling is better than anything this page would do. Shown
           only once a feed has been published — see checkFeed. -->
      <a v-if="feedAvailable" class="header__gtfs" :href="gtfsUrl" download>GTFS feed</a>
    </header>

    <!-- Operator mode, reached with #admin in the URL. The API checks the
         token on every write; hiding this only keeps the public map clean. -->
    <div v-if="admin.available" class="banner banner--admin">
      <template v-if="admin.active">
        <span>Operator mode — route and stop names are editable.</span>
        <button type="button" class="banner__retry" @click="admin.signOut()">Sign out</button>
      </template>
      <form v-else class="admin-form" @submit.prevent="signIn">
        <input
          v-model="tokenEntry"
          type="password"
          class="admin-form__input"
          placeholder="Admin token"
          aria-label="Admin token"
        />
        <button type="submit" class="banner__retry">Unlock naming</button>
      </form>
    </div>

    <p v-if="store.status === 'error'" class="banner banner--error" role="alert">
      {{ store.error }}
      <button type="button" class="banner__retry" @click="store.load()">Retry</button>
    </p>

    <main class="main">
      <MapView />
      <RoutePanel v-if="!store.isEmpty" />

      <aside v-else class="empty">
        <h2 class="empty__title">No routes yet</h2>
        <p>
          Routes appear here once enough rides have been recorded for the pipeline to cluster them.
          A single trace is not a route — several riders have to travel the same way before one is
          published.
        </p>
      </aside>
    </main>
  </div>
</template>

<style scoped>
.app {
  display: flex;
  flex-direction: column;
  height: 100%;
}

.header {
  flex: none;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 1rem;
  padding: 0.75rem 1rem;
  border-bottom: 1px solid var(--line);
  background: var(--surface);
}

.header__title {
  margin: 0;
  font-size: 1.05rem;
  letter-spacing: 0.01em;
}

.header__subtitle {
  margin: 0.1rem 0 0;
  color: var(--text-muted);
  font-size: 0.8rem;
}

.header__gtfs {
  flex: none;
  padding: 0.45rem 0.8rem;
  border: 1px solid var(--line);
  border-radius: 999px;
  color: var(--text);
  font-size: 0.82rem;
  font-weight: 600;
  text-decoration: none;
}

.header__gtfs:hover {
  border-color: var(--accent);
  color: var(--accent);
}

.banner {
  flex: none;
  display: flex;
  align-items: center;
  gap: 0.75rem;
  margin: 0;
  padding: 0.6rem 1rem;
  font-size: 0.85rem;
}

.banner--admin {
  background: rgb(251 191 36 / 10%);
  color: var(--accent);
  border-bottom: 1px solid var(--line);
}

.admin-form {
  display: flex;
  gap: 0.5rem;
  align-items: center;
}

.admin-form__input {
  padding: 0.3rem 0.6rem;
  border: 1px solid var(--line);
  border-radius: 0.4rem;
  background: var(--bg);
  color: var(--text);
  font-size: 0.82rem;
}

.banner--error {
  background: rgb(248 113 113 / 12%);
  color: var(--danger);
  border-bottom: 1px solid var(--line);
}

.banner__retry {
  padding: 0.25rem 0.6rem;
  border: 1px solid currentcolor;
  border-radius: 999px;
  background: none;
  color: inherit;
  font-size: 0.78rem;
}

.main {
  flex: 1;
  display: flex;
  min-height: 0;
}

.empty {
  width: 22rem;
  max-width: 100%;
  padding: 1.25rem;
  border-left: 1px solid var(--line);
  background: var(--surface);
  color: var(--text-muted);
  font-size: 0.87rem;
  line-height: 1.6;
}

.empty__title {
  margin: 0 0 0.5rem;
  color: var(--text);
  font-size: 1rem;
}

@media (max-width: 48rem) {
  .main {
    flex-direction: column;
  }

  .empty {
    width: 100%;
    border-left: 0;
    border-top: 1px solid var(--line);
  }
}
</style>
