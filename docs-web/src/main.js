import { createApp } from "vue"
import { createPinia } from "pinia"
import { createRouter, createWebHashHistory } from "vue-router"
import { routes } from "vue-router/auto-routes"

import App from "./App.vue"
import "virtual:uno.css"
import "./style.css"

const router = createRouter({ history: createWebHashHistory(), routes })

// Dark is the default; an explicit choice wins and persists.
const stored = localStorage.getItem("kt-theme")
document.documentElement.classList.toggle("dark", stored !== "light")

createApp(App).use(createPinia()).use(router).mount("#app")
