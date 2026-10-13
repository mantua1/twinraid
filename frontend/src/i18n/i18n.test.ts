import { describe, expect, it, vi } from "vitest";

import { AUDIT_ACTIONS, AUDIT_RESULTS, LOGIN_ERRORS, RESOURCE_STATES } from "@/api/types";

import de from "./de.json";
import { browserLanguage } from "./index";
import en from "./en.json";

type Tree = Record<string, unknown>;

function flatten(tree: Tree, prefix = ""): Map<string, unknown> {
  const result = new Map<string, unknown>();
  for (const [key, value] of Object.entries(tree)) {
    const path = prefix ? `${prefix}.${key}` : key;
    if (typeof value === "object" && value !== null) {
      for (const [childKey, childValue] of flatten(value as Tree, path)) {
        result.set(childKey, childValue);
      }
    } else {
      result.set(path, value);
    }
  }
  return result;
}

describe("translations", () => {
  const german = flatten(de);
  const english = flatten(en);

  it("have the same keys in German and English", () => {
    expect([...english.keys()].sort()).toEqual([...german.keys()].sort());
  });

  it("have no empty texts", () => {
    for (const [key, value] of [...german, ...english]) {
      expect(typeof value, key).toBe("string");
      expect((value as string).trim(), key).not.toBe("");
    }
  });

  it("cover every audit action, audit result and login error", () => {
    for (const action of AUDIT_ACTIONS) {
      expect(german.has(`audit.actions.${action}`), action).toBe(true);
    }
    for (const result of AUDIT_RESULTS) {
      expect(german.has(`audit.results.${result}`), result).toBe(true);
    }
    for (const code of [...LOGIN_ERRORS, "network"]) {
      expect(german.has(`login.errors.${code}`), code).toBe(true);
    }
  });

  it("cover every status value", () => {
    for (const state of RESOURCE_STATES) {
      expect(german.has(`status.${state}`), state).toBe(true);
    }
  });
});

describe("browser language", () => {
  it.each([
    ["de-DE", "de"],
    ["de", "de"],
    ["en-US", "en"],
    ["fr-FR", "en"],
  ])("maps %s to %s", (language, expected) => {
    vi.spyOn(navigator, "language", "get").mockReturnValue(language);
    expect(browserLanguage()).toBe(expected);
  });
});
