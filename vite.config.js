import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { resolve } from 'node:path';

export default defineConfig({
  root: 'frontend',
  base: '/static/react/',
  plugins: [react()],
  build: {
    outDir: '../static/react',
    emptyOutDir: true,
    rollupOptions: {
      input: resolve(__dirname, 'frontend/index.html'),
      output: {
        entryFileNames: 'app.js',
        chunkFileNames: 'chunks/[name]-[hash].js',
        assetFileNames: (assetInfo) => (
          assetInfo.name && assetInfo.name.endsWith('.css')
            ? 'app.css'
            : 'assets/[name]-[hash][extname]'
        )
      }
    }
  },
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:5000',
      '/static': 'http://127.0.0.1:5000',
      '/socket.io': {
        target: 'ws://127.0.0.1:5000',
        ws: true
      }
    }
  }
});
