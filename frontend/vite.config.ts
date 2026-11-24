import { defineConfig } from 'vite';
export default defineConfig({
  build: { outDir: '../src/swarmforge/resources/web', emptyOutDir: true },
  server: { proxy: { '/api': 'http://127.0.0.1:8600', '/brand': 'http://127.0.0.1:8600' } },
});
