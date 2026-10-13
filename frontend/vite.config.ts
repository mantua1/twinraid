import { existsSync, readFileSync } from "node:fs";
import { fileURLToPath, URL } from "node:url";

import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// Backend für die lokale Entwicklung (backend/scripts/dev.sh), nur per HTTPS.
const backend = { target: "https://127.0.0.1:8443", secure: false };

// Der Entwicklungsserver nutzt dasselbe Zertifikat wie das lokale Backend,
// damit Cookies mit Secure und __Host- funktionieren.
const tlsDir = fileURLToPath(new URL("../.run/tls/", import.meta.url));
const https = existsSync(`${tlsDir}cert.pem`)
  ? { cert: readFileSync(`${tlsDir}cert.pem`), key: readFileSync(`${tlsDir}key.pem`) }
  : undefined;

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
  },
  server: {
    host: "127.0.0.1",
    https,
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
