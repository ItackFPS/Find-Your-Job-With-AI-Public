import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/setupTests.ts'],
    globals: false,
  },
  server: {
    port: 5173,
    proxy: {
      '/api': {
        // FYJ_API_URL: set by `FindYourJob.cmd dev` when the backend uses another port.
        target: process.env.FYJ_API_URL ?? 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
})
