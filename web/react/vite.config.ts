import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    host: '0.0.0.0', // LAN-reachable; the API below stays on localhost
    allowedHosts: ['aio-mesh.nord'],
    proxy: { '/api': 'http://127.0.0.1:8083' },
    watch: {
      // inotify events get dropped for edits made outside an
      // HMR-initiated session; polling keeps transforms fresh
      usePolling: true,
    },
  },
})
