import "@testing-library/jest-dom/vitest";
import "@/i18n";

import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

import i18n from "@/i18n";

// jsdom kennt matchMedia nicht; Standard: System bevorzugt hell.
Object.defineProperty(window, "matchMedia", {
  writable: true,
  value: (query: string): MediaQueryList =>
    ({
      matches: false,
      media: query,
      onchange: null,
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
      addListener: () => undefined,
      removeListener: () => undefined,
      dispatchEvent: () => false,
    }) as MediaQueryList,
});

afterEach(async () => {
  cleanup();
  localStorage.clear();
  document.documentElement.className = "";
  await i18n.changeLanguage("de");
});
