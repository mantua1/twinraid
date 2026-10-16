import { describe, expect, it, vi } from "vitest";

import {
  ACTION_BLOCKERS,
  ACTION_NAMES,
  ACTION_OUTCOMES,
  ACTION_STEPS,
  AUDIT_ACTIONS,
  AUDIT_RESULTS,
  CA_CHANGE_REASONS,
  LOGIN_ERRORS,
  RESOURCE_STATES,
  STATUS_NOTICES,
  STATUS_REASONS,
} from "@/api/types";

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

  it("cover every status value and reason", () => {
    for (const state of RESOURCE_STATES) {
      expect(german.has(`status.${state}`), state).toBe(true);
    }
    for (const reason of STATUS_REASONS) {
      expect(german.has(`status.reasons.${reason}`), reason).toBe(true);
    }
    for (const notice of STATUS_NOTICES) {
      expect(german.has(`status.notices.${notice}`), notice).toBe(true);
    }
    for (const reason of CA_CHANGE_REASONS) {
      expect(german.has(`caChange.reasons.${reason}`), reason).toBe(true);
    }
  });
});

describe("action texts", () => {
  const german = flatten(de);

  it("cover every action, step, blocker and outcome", () => {
    for (const name of ACTION_NAMES) {
      expect(german.has(`actions.buttons.${name}`), name).toBe(true);
      expect(german.has(`actions.names.${name}`), name).toBe(true);
    }
    for (const step of ACTION_STEPS) {
      expect(german.has(`actions.steps.${step}`), step).toBe(true);
    }
    for (const blocker of ACTION_BLOCKERS) {
      expect(german.has(`actions.blockers.${blocker}`), blocker).toBe(true);
    }
    for (const outcome of ACTION_OUTCOMES) {
      expect(german.has(`actions.outcomes.${outcome}`), outcome).toBe(true);
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
