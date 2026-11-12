import { createPinia } from 'pinia'
import { createApp } from 'vue'

import App from './App.vue'
import { listenForInstallPrompt } from './lib/install'
import './style.css'

// Before mounting: the browser can fire its install event as soon as it loads.
listenForInstallPrompt()

createApp(App).use(createPinia()).mount('#app')
