import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': {
        target: 'http://localhost:8099',
        changeOrigin: true,
      },
      '/v1': {
        target: 'http://localhost:8099',
        changeOrigin: true,
      },
      '/ws': {
        target: 'ws://localhost:8099',
        ws: true,
      },
    },
  },
})
