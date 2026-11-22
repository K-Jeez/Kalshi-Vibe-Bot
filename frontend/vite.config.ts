import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  build: {
    chunkSizeWarningLimit: 600,
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (id.includes('node_modules')) {
            if (id.includes('recharts')) return 'vendor-recharts'
            if (id.includes('lucide-react')) return 'vendor-icons'
          }
          return undefined
        },
      },
    },
  },
  // Dev UI on every interface, port 3000. The page calls the API on the same host, port 8000.
  server: { port: 3000, host: true },
})
