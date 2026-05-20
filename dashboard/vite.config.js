import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  build: {
    // Write directly into the Python package so the build artifact ships
    // with the wheel (and future PyInstaller binary) — end users never run
    // `npm run build` themselves.
    outDir: '../src/aries/dashboard/dist',
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    proxy: {
      '/api': 'http://localhost:7272',
    },
  },
})
