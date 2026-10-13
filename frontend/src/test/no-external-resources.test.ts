// Keine externen Ressourcen im Frontend (CLAUDE.md, Regel 9).
import { describe, expect, it } from "vitest";

import indexHtml from "../../index.html?raw";

const sources = import.meta.glob<string>(["/src/**/*.{ts,tsx,css,json}", "!/src/**/*.test.*"], {
  query: "?raw",
  import: "default",
  eager: true,
});

const EXTERNAL_URL = /(?:https?:)?\/\/(?:[a-z0-9-]+\.)+[a-z]{2,}/i;

describe("no external resources", () => {
  it("index.html loads nothing from other hosts", () => {
    expect(indexHtml).not.toMatch(EXTERNAL_URL);
  });

  it("source files reference no external hosts", () => {
    expect(Object.keys(sources).length).toBeGreaterThan(10);
    for (const [path, content] of Object.entries(sources)) {
      expect(content, path).not.toMatch(EXTERNAL_URL);
    }
  });
});
