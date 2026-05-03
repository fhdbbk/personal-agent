import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Forward backend routes to the FastAPI dev server so the SPA can hit
// same-origin paths (`/chat`, `/chat/stream`) regardless of CORS.
const BACKEND = 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/health': { target: BACKEND, changeOrigin: true },
      '/chat': { target: BACKEND, changeOrigin: true, ws: true },
    },
  },
})
