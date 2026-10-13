import { useCallback, useEffect, useState } from "react";

export const THEME_CHOICES = ["light", "dark", "system"] as const;
export type ThemeChoice = (typeof THEME_CHOICES)[number];

const STORAGE_KEY = "twinraid.theme";
const DARK_QUERY = "(prefers-color-scheme: dark)";

function isThemeChoice(value: unknown): value is ThemeChoice {
  return value === "light" || value === "dark" || value === "system";
}

export function readThemeChoice(): ThemeChoice {
  try {
    const value = localStorage.getItem(STORAGE_KEY);
    return isThemeChoice(value) ? value : "system";
  } catch {
    return "system";
  }
}

/** Setzt die Klasse "dark" am <html>-Element passend zur Wahl. */
export function applyTheme(choice: ThemeChoice): void {
  const dark = choice === "dark" || (choice === "system" && window.matchMedia(DARK_QUERY).matches);
  const root = document.documentElement;
  root.classList.toggle("dark", dark);
  root.style.colorScheme = dark ? "dark" : "light";
}

export function useTheme(): [ThemeChoice, (choice: ThemeChoice) => void] {
  const [choice, setChoice] = useState<ThemeChoice>(readThemeChoice);

  useEffect(() => {
    applyTheme(choice);
    if (choice !== "system") {
      return;
    }
    // Bei "Wie System" der Systemeinstellung folgen, auch wenn sie sich ändert.
    const media = window.matchMedia(DARK_QUERY);
    const onChange = () => {
      applyTheme("system");
    };
    media.addEventListener("change", onChange);
    return () => {
      media.removeEventListener("change", onChange);
    };
  }, [choice]);

  const update = useCallback((next: ThemeChoice) => {
    try {
      localStorage.setItem(STORAGE_KEY, next);
    } catch {
      // Ohne localStorage gilt die Wahl nur bis zum Neuladen.
    }
    setChoice(next);
  }, []);

  return [choice, update];
}
