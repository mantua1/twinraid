import { describe, expect, it, vi } from "vitest";

import {
  ACTION_BLOCKERS,
  ACTION_NAMES,
  ACTION_OUTCOMES,
  ACTION_STEPS,
  AUDIT_ACTIONS,
  AUDIT_RESULTS,
  CA_CHANGE_REASONS,
  GUARD_BLOCKERS,
  GUARD_CHECK_RESULTS,
  GUARD_CONFIRM_REASONS,
  GUARD_CONTAINER_STATES,
  GUARD_DATASET_PROBLEMS,
  GUARD_ERROR_DETAILS,
  GUARD_ERRORS,
  GUARD_RESTARTS,
  GUARD_RUN_ENDS,
  GUARD_SELF_DATASETS,
  GUARD_SETUP_PROBLEMS,
  GUARD_STATUS_PROBLEMS,
  LOGIN_ERRORS,
  RESOURCE_STATES,
  STATUS_NOTICES,
  STATUS_REASONS,
  SYNC_STEPS,
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

describe("source guard texts", () => {
  const german = flatten(de);

  it("cover every code of the guard (Konzept 3.6)", () => {
    const lists: [string, readonly string[]][] = [
      ["guard.errors", GUARD_ERRORS],
      ["guard.errorDetails", GUARD_ERROR_DETAILS],
      ["guard.setup", GUARD_SETUP_PROBLEMS],
      ["guard.statusProblems", GUARD_STATUS_PROBLEMS],
      ["guard.containerStates", GUARD_CONTAINER_STATES],
      ["guard.restarts", GUARD_RESTARTS],
      ["guard.runEnds", GUARD_RUN_ENDS],
      ["guard.selfDataset", GUARD_SELF_DATASETS],
    ];
    for (const [prefix, codes] of lists) {
      for (const code of codes) {
        expect(german.has(`${prefix}.${code}`), `${prefix}.${code}`).toBe(true);
      }
    }
  });

  it("tell the user to start the guard when Docker created a directory", () => {
    expect(german.get("guard.setup.request_is_directory")).toContain(
      "Wächter starten, dann TwinRaid neu starten",
    );
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

describe("sync texts", () => {
  const german = flatten(de);

  it("cover every code of the dry run (Konzept 4.3)", () => {
    const lists: [string, readonly string[]][] = [
      ["sync.steps", SYNC_STEPS],
      ["sync.blockers", GUARD_BLOCKERS],
      ["sync.check", GUARD_CHECK_RESULTS],
      ["sync.confirmReasons", GUARD_CONFIRM_REASONS],
      ["sync.problems", GUARD_DATASET_PROBLEMS],
      ["sync.mirrorStates", ["absent", "mirror", "foreign"]],
    ];
    for (const [prefix, codes] of lists) {
      for (const code of codes) {
        expect(german.has(`${prefix}.${code}`), `${prefix}.${code}`).toBe(true);
      }
    }
  });

  it("tell the user which guard to copy after a protocol change", () => {
    expect(german.get("guard.statusProblems.protocol_mismatch")).toContain(
      "source-guard.sh Version 2",
    );
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
