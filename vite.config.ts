import { fileURLToPath, URL } from 'node:url';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';
import { defineConfig } from 'vite';

const backend = process.env.OAK_API_PROXY_TARGET || 'http://127.0.0.1:8080';
const proxy = {
  target: backend,
  // Keep the browser's Host and Origin paired so Flask's mutation check stays active.
  changeOrigin: false,
};

export default defineConfig(({ command }) => ({
  root: fileURLToPath(new URL('./frontend', import.meta.url)),
  base: command === 'build' ? '/static/dist/' : '/',
  plugins: [react(), tailwindcss()],
  build: {
    outDir: fileURLToPath(new URL('./oak_camera/static/dist', import.meta.url)),
    emptyOutDir: true,
    manifest: true,
  },
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    proxy: {
      '/api': proxy,
      '/stream': proxy,
      '/captures': proxy,
    },
  },
}));
