import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import "./index.css";
import "./i18n";

import { App } from "./App";
import { applyTheme, readThemeChoice } from "./hooks/useTheme";

// Theme vor dem ersten Rendern setzen, damit die Seite nicht kurz hell aufblitzt.
applyTheme(readThemeChoice());

const root = document.getElementById("root");
if (root === null) {
  throw new Error("Root element #root is missing");
}

createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
