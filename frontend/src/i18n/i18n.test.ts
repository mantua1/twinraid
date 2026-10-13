import { describe, expect, it } from "vitest";

import { RESOURCE_STATES } from "@/api/types";

import de from "./de.json";
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

  it("cover every status value", () => {
    for (const state of RESOURCE_STATES) {
      expect(german.has(`status.${state}`), state).toBe(true);
    }
  });
});
