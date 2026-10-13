import { useEffect } from "react";
import { useTranslation } from "react-i18next";

import { AppHeader } from "@/components/AppHeader";
import { MockBanner } from "@/components/MockBanner";
import { useDashboardData } from "@/hooks/useDashboardData";
import i18n, { storedLanguage } from "@/i18n";
import { Dashboard } from "@/pages/Dashboard";

export function App() {
  const { t } = useTranslation();
  const data = useDashboardData();
  const defaultLanguage = data.info?.default_language;

  // Standardsprache aus der Konfiguration, solange der Nutzer keine eigene gewählt hat.
  useEffect(() => {
    if (defaultLanguage !== undefined && storedLanguage() === null) {
      void i18n.changeLanguage(defaultLanguage);
    }
  }, [defaultLanguage]);

  return (
    <div className="flex min-h-svh flex-col">
      {data.info?.mode === "mock" && <MockBanner />}
      <AppHeader />
      <main className="flex-1">
        <Dashboard data={data} />
      </main>
      {data.info && (
        <footer className="py-4 text-center text-xs text-muted-foreground">
          {t("app.footer", { version: data.info.version })}
        </footer>
      )}
    </div>
  );
}
