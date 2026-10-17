import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { api, setCsrfToken } from "@/api/client";
import type { RunMode } from "@/api/types";
import { AppHeader, type View } from "@/components/AppHeader";
import { ErrorNotice } from "@/components/ErrorNotice";
import { MockBanner } from "@/components/MockBanner";
import { ReadOnlyBanner } from "@/components/ReadOnlyBanner";
import { useDashboardData } from "@/hooks/useDashboardData";
import i18n, { storedLanguage } from "@/i18n";
import { Dashboard } from "@/pages/Dashboard";
import { LoginPage } from "@/pages/LoginPage";
import { SettingsPage } from "@/pages/SettingsPage";

type Phase =
  | { kind: "loading" }
  | { kind: "unreachable" }
  | { kind: "login"; passwordSet: boolean | null; sessionExpired: boolean }
  | { kind: "app" };

function AuthenticatedApp({
  onUnauthorized,
  onLogout,
}: {
  onUnauthorized: () => void;
  onLogout: () => void;
}) {
  const { t } = useTranslation();
  const [view, setView] = useState<View>("dashboard");
  const data = useDashboardData(onUnauthorized);
  const defaultLanguage = data.info?.default_language;

  // Standardsprache aus der Konfiguration, solange der Nutzer keine eigene gewählt hat.
  useEffect(() => {
    if (defaultLanguage !== undefined && storedLanguage() === null) {
      void i18n.changeLanguage(defaultLanguage);
    }
  }, [defaultLanguage]);

  return (
    <>
      {data.info?.read_only === true && <ReadOnlyBanner />}
      <AppHeader navigation={{ view, onNavigate: setView, onLogout }} />
      <main className="flex-1">
        {view === "dashboard" ? (
          <Dashboard data={data} />
        ) : (
          <SettingsPage
            info={data.info}
            tls={data.tls}
            notices={data.stream.status?.notices ?? []}
            onBack={() => {
              setView("dashboard");
            }}
          />
        )}
      </main>
      {data.info && (
        <footer className="py-4 text-center text-xs text-muted-foreground">
          {t("app.footer", { version: data.info.version })}
        </footer>
      )}
    </>
  );
}

interface LoadedState {
  mode: RunMode | null;
  phase: Phase;
}

/** Fragt den Anmeldestatus ab; setzt selbst keinen React-Zustand. */
async function fetchState(sessionExpired: boolean): Promise<LoadedState> {
  try {
    const state = await api.authState();
    if (state.authenticated) {
      await api.session();
      return { mode: state.mode, phase: { kind: "app" } };
    }
    setCsrfToken(null);
    return {
      mode: state.mode,
      phase: { kind: "login", passwordSet: state.password_set, sessionExpired },
    };
  } catch {
    return { mode: null, phase: { kind: "unreachable" } };
  }
}

export function App() {
  const { t } = useTranslation();
  const [phase, setPhase] = useState<Phase>({ kind: "loading" });
  const [mode, setMode] = useState<RunMode | null>(null);

  const apply = useCallback((loaded: LoadedState) => {
    if (loaded.mode !== null) {
      setMode(loaded.mode);
    }
    setPhase(loaded.phase);
  }, []);

  const loadState = useCallback(
    async (sessionExpired: boolean) => {
      apply(await fetchState(sessionExpired));
    },
    [apply],
  );

  useEffect(() => {
    let cancelled = false;
    void fetchState(false).then((loaded) => {
      if (!cancelled) {
        apply(loaded);
      }
    });
    return () => {
      cancelled = true;
    };
  }, [apply]);

  const onUnauthorized = useCallback(() => {
    void loadState(true);
  }, [loadState]);

  const onLogout = useCallback(() => {
    void api
      .logout()
      .catch(() => undefined)
      .then(() => loadState(false));
  }, [loadState]);

  return (
    <div className="flex min-h-svh flex-col">
      {mode === "mock" && <MockBanner />}
      {phase.kind === "app" ? (
        <AuthenticatedApp onUnauthorized={onUnauthorized} onLogout={onLogout} />
      ) : (
        <>
          <AppHeader />
          <main className="flex-1">
            {phase.kind === "loading" && (
              <p className="px-4 py-6 text-sm text-muted-foreground">{t("dashboard.loading")}</p>
            )}
            {phase.kind === "unreachable" && (
              <div className="mx-auto max-w-md px-4 py-10">
                <ErrorNotice message={t("errors.connection")} />
              </div>
            )}
            {phase.kind === "login" && (
              <LoginPage
                passwordSet={phase.passwordSet}
                sessionExpired={phase.sessionExpired}
                onLoggedIn={() => {
                  void loadState(false);
                }}
              />
            )}
          </main>
        </>
      )}
    </div>
  );
}
