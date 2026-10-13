// Typisierte Übersetzungsschlüssel: Ein Tippfehler im Schlüssel ist ein Compilerfehler.
import "i18next";

import type de from "./de.json";

declare module "i18next" {
  interface CustomTypeOptions {
    defaultNS: "translation";
    resources: { translation: typeof de };
  }
}
