/// <reference types="vitest/config" />
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The API serves the built app from the same origin (ADR-0016). In development the Vite server
// forwards /api, /health and /ready to the local API, so cookies stay first-party.
const api = process.env.API_URL ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      "/api": { target: api, changeOrigin: false },
      "/health": api,
      "/ready": api,
    },
  },
  build: {
    // No inline scripts or styles: the API's CSP allows only 'self' (security §4, UI §2).
    assetsInlineLimit: 0,
    sourcemap: false,
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    css: false,
    restoreMocks: true,
  },
});
