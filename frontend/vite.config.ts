import { fileURLToPath, URL } from "node:url";

import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// Backend für die lokale Entwicklung (backend/scripts/dev.sh).
const backend = "http://127.0.0.1:8443";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
  },
  server: {
    host: "127.0.0.1",
    proxy: { "/api": backend, "/health": backend },
  },
  build: {
    assetsDir: "assets",
    // Keine data:-URIs, damit später eine strikte Content-Security-Policy möglich ist.
    assetsInlineLimit: 0,
    sourcemap: false,
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    restoreMocks: true,
  },
});
