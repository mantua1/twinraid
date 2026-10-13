// Übersetzungen der Oberfläche. Alle sichtbaren Texte kommen aus de.json und en.json.
import i18n from "i18next";
import { initReactI18next } from "react-i18next";

import de from "./de.json";
import en from "./en.json";

export const LANGUAGES = ["de", "en"] as const;
export type Language = (typeof LANGUAGES)[number];

const STORAGE_KEY = "twinraid.language";

export const resources = {
  de: { translation: de },
  en: { translation: en },
} as const;

export function isLanguage(value: unknown): value is Language {
  return value === "de" || value === "en";
}

/** Vom Nutzer gewählte Sprache, falls vorhanden. */
export function storedLanguage(): Language | null {
  try {
    const value = localStorage.getItem(STORAGE_KEY);
    return isLanguage(value) ? value : null;
  } catch {
    return null;
  }
}

export function chooseLanguage(language: Language): void {
  try {
    localStorage.setItem(STORAGE_KEY, language);
  } catch {
    // Ohne localStorage gilt die Wahl nur bis zum Neuladen.
  }
  void i18n.changeLanguage(language);
}

i18n.on("languageChanged", (language) => {
  document.documentElement.lang = language;
});

void i18n.use(initReactI18next).init({
  resources,
  lng: storedLanguage() ?? "de",
  fallbackLng: "de",
  supportedLngs: LANGUAGES,
  // React maskiert Ausgaben bereits selbst.
  interpolation: { escapeValue: false },
  initAsync: false,
});

export default i18n;
