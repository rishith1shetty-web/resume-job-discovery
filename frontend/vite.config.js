import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Port 5173 is deliberate: it is one of the origins the backend's
// CORS_ALLOW_ORIGINS already permits. Changing it means adding the new origin
// to the backend .env, or requests will be blocked by the browser.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
  },
})
