import { createPinia } from 'pinia'
import { createApp } from 'vue'

import App from './App.vue'
import { locale } from './i18n'
import { listenForInstallPrompt } from './lib/install'
import { watchForUpdates } from './lib/updates'
import './style.css'

// Screen readers and spell-checkers should know which language is on screen.
document.documentElement.lang = locale

// Before mounting: the browser can fire its install event as soon as it loads,
// and a new service worker can take over at any moment.
listenForInstallPrompt()
watchForUpdates()

createApp(App).use(createPinia()).mount('#app')
