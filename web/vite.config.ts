import path from 'node:path'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { tanstackRouter } from '@tanstack/router-plugin/vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [
    // TanStack Router file-based plugin must run before @vitejs/plugin-react.
    tanstackRouter({
      target: 'react',
      autoCodeSplitting: true,
    }),
    react(),
    tailwindcss(),
  ],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    port: 5173,
    // Proxy API to FastAPI in dev so the browser sees same-origin requests
    // and we don't have to deal with CORS in the dev loop.
    //
    // No rewrite: the backend serves under the /api prefix (APIRouter), and
    // the frontend client uses BASE='/api', so /api/* passes through
    // unchanged to http://127.0.0.1:8770/api/*.  (An earlier rewrite stripped
    // /api here — correct when routes lived at root, but after the /api
    // refactor it made every proxied call 404, so the UI reported the API
    // as down.)
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8770',
        changeOrigin: true,
      },
    },
  },
})
