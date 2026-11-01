import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  build: { rollupOptions: { input: ['index.html', 'showcase.html'] } },
  server: { proxy: { '/api': { target: 'http://127.0.0.1:8100', changeOrigin: false } } },
});
