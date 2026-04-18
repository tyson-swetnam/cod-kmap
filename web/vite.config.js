import { defineConfig } from 'vite';

export default defineConfig({
  base: '/cod-kmap/',
  server: { port: 5173 },
  build: {
    outDir: 'dist',
    sourcemap: true,
    target: 'es2022',
  },
  optimizeDeps: {
    exclude: ['@duckdb/duckdb-wasm'],
  },
});
