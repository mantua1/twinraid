import { LayoutDashboard, LogOut, Server, Settings } from "lucide-react";
import { useTranslation } from "react-i18next";

import { LanguageSwitch } from "@/components/LanguageSwitch";
import { ThemeToggle } from "@/components/ThemeToggle";
import { Button } from "@/components/ui/button";

export type View = "dashboard" | "settings";

interface AppHeaderProps {
  // Nur nach dem Login gesetzt.
  navigation?: {
    view: View;
    onNavigate: (view: View) => void;
    onLogout: () => void;
  };
}

export function AppHeader({ navigation }: AppHeaderProps) {
  const { t } = useTranslation();
  return (
    <header className="border-b bg-card">
      <div className="mx-auto flex max-w-6xl flex-wrap items-center justify-between gap-3 px-4 py-3">
        <div className="flex items-center gap-2">
          <Server className="size-5 text-primary" aria-hidden="true" />
          <span className="text-lg font-semibold">{t("app.name")}</span>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {navigation && (
            <>
              <Button
                variant={navigation.view === "dashboard" ? "secondary" : "ghost"}
                size="sm"
                aria-pressed={navigation.view === "dashboard"}
                onClick={() => {
                  navigation.onNavigate("dashboard");
                }}
              >
                <LayoutDashboard aria-hidden="true" />
                {t("nav.dashboard")}
              </Button>
              <Button
                variant={navigation.view === "settings" ? "secondary" : "ghost"}
                size="sm"
                aria-pressed={navigation.view === "settings"}
                onClick={() => {
                  navigation.onNavigate("settings");
                }}
              >
                <Settings aria-hidden="true" />
                {t("nav.settings")}
              </Button>
            </>
          )}
          <LanguageSwitch />
          <ThemeToggle />
          {navigation && (
            <Button variant="outline" size="sm" onClick={navigation.onLogout}>
              <LogOut aria-hidden="true" />
              {t("nav.logout")}
            </Button>
          )}
        </div>
      </div>
    </header>
  );
}
