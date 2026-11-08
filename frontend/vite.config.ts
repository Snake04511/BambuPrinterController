import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// Dev: the FastAPI backend runs on :8091 (uvicorn), Vite proxies /api (incl. the WebSocket).
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      '/api': { target: 'http://localhost:8091', ws: true },
    },
  },
  build: { chunkSizeWarningLimit: 800 },
})
