import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { act } from "react";

import type { AppInfo } from "@/api/types";
import { chooseLanguage } from "@/i18n";
import { CSRF, installFakeApi, latestStream, PASSWORD, STATUS, TLS } from "@/test/fakeApi";

import { App } from "./App";

async function signIn(password = PASSWORD) {
  const user = userEvent.setup();
  await user.type(await screen.findByLabelText("Passwort"), password);
  await user.click(screen.getByRole("button", { name: "Anmelden" }));
  return user;
}

describe("login", () => {
  it("shows the login page with the simulation banner", async () => {
    installFakeApi();
    render(<App />);
    expect(await screen.findByRole("heading", { name: "Anmelden" })).toBeInTheDocument();
    expect(screen.getByTestId("mock-banner")).toHaveTextContent("Simulationsmodus");
    expect(screen.queryByText("Unraid Klein")).not.toBeInTheDocument();
  });

  it("shows no banner in real mode", async () => {
    installFakeApi({ mode: "real" });
    render(<App />);
    await screen.findByRole("heading", { name: "Anmelden" });
    expect(screen.queryByTestId("mock-banner")).not.toBeInTheDocument();
  });

  it("explains how to set the password when none is set", async () => {
    installFakeApi({ passwordSet: false });
    render(<App />);
    expect(await screen.findByText("Noch kein Passwort gesetzt")).toBeInTheDocument();
    expect(screen.getByText("twinraid-admin set-password")).toBeInTheDocument();
  });

  it("shows the hint after a login attempt without password", async () => {
    installFakeApi({ loginFailure: { status: 401, body: { detail: "password_not_set" } } });
    render(<App />);
    await signIn("irgendein-passwort");
    expect(await screen.findByText("twinraid-admin set-password")).toBeInTheDocument();
  });

  it.each([
    [{ detail: "invalid_password" }, 401, "Das Passwort ist falsch."],
    [{ detail: "locked", retry_after: 840 }, 429, "in 14 Minuten wieder möglich"],
    [{ detail: "busy", retry_after: 5 }, 429, "in 5 Sekunden erneut versuchen"],
    [{ detail: "worker_unavailable" }, 503, "nicht bereit"],
  ])("explains failure %j", async (body, status, text) => {
    installFakeApi({ loginFailure: { status, body } });
    render(<App />);
    await signIn("falsches-passwort");
    expect(await screen.findByRole("alert")).toHaveTextContent(text);
  });

  it("opens the dashboard after login", async () => {
    installFakeApi();
    render(<App />);
    await signIn();
    expect(await screen.findByText("Unraid Klein")).toBeInTheDocument();
    expect(screen.getByText("Unraid Groß")).toBeInTheDocument();
    expect(await screen.findByText("Server offline")).toBeInTheDocument();
    expect(screen.getByTestId("mock-banner")).toBeInTheDocument();
  });

  it("only talks to its own server", async () => {
    const server = installFakeApi();
    render(<App />);
    await signIn();
    await screen.findByText("Unraid Klein");
    await screen.findByText("Server offline");
    for (const call of server.calls) {
      expect(call.url).toMatch(/^\/api\//);
    }
  });

  it("shows a connection error when the server is unreachable", async () => {
    installFakeApi({ failAuthState: true });
    render(<App />);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Keine Verbindung zum TwinRaid-Server.",
    );
  });
});

describe("session", () => {
  it("skips the login page with a valid session", async () => {
    installFakeApi({ authenticated: true });
    render(<App />);
    expect(await screen.findByText("Unraid Klein")).toBeInTheDocument();
  });

  it("logs out with the CSRF token", async () => {
    const server = installFakeApi({ authenticated: true });
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText("Unraid Klein");
    await user.click(screen.getByRole("button", { name: "Abmelden" }));
    expect(await screen.findByRole("heading", { name: "Anmelden" })).toBeInTheDocument();
    const logout = server.calls.find((c) => c.url === "/api/auth/logout");
    expect(logout?.headers.get("X-CSRF-Token")).toBe(CSRF);
    expect(server.authenticated).toBe(false);
  });

  it("returns to login when the session expires", async () => {
    const server = installFakeApi({ authenticated: true });
    const original = server.calls.push.bind(server.calls);
    server.calls.push = (...items) => {
      // Die Session läuft ab, sobald das Dashboard seine Daten lädt.
      if (items.some((item) => item.url === "/api/info")) {
        server.authenticated = false;
      }
      return original(...items);
    };
    render(<App />);
    expect(await screen.findByRole("heading", { name: "Anmelden" })).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("Die Sitzung ist abgelaufen.");
  });
});

describe("dashboard", () => {
  it("lists audit entries in the history tile", async () => {
    installFakeApi({ authenticated: true });
    render(<App />);
    const history = (await screen.findByText("Verlauf")).closest("[data-slot=card]");
    expect(history).not.toBeNull();
    const tile = within(history as HTMLElement);
    // Die Kachel erscheint vor den Daten; auf die Einträge warten.
    expect(await tile.findByText("Anmeldung")).toBeInTheDocument();
    expect(tile.getByText(/von 192\.168\.20\.5/)).toBeInTheDocument();
    expect(tile.getByText("Passwort gesetzt (CLI)")).toBeInTheDocument();
    expect(tile.getByText(/im Container/)).toBeInTheDocument();
  });

  it("warns before the server certificate expires", async () => {
    installFakeApi({
      authenticated: true,
      tls: { ...TLS, server: { ...TLS.server, days_left: 19, expires_soon: true } },
    });
    render(<App />);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Das TLS-Serverzertifikat läuft in 19 Tagen ab.",
    );
  });

  it("warns when the automatic renewal failed", async () => {
    installFakeApi({ authenticated: true, tls: { ...TLS, renewal_failed: true } });
    render(<App />);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "automatische Erneuerung des TLS-Serverzertifikats ist fehlgeschlagen",
    );
  });

  it("warns before the CA expires", async () => {
    const ca = TLS.ca;
    if (ca === null) {
      throw new Error("test data needs a CA");
    }
    installFakeApi({
      authenticated: true,
      tls: { ...TLS, ca: { ...ca, days_left: 80, expires_soon: true } },
    });
    render(<App />);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Die TwinRaid-CA läuft in 80 Tagen ab.",
    );
  });

  it("uses the configured default language", async () => {
    installFakeApi({ authenticated: true, info: { ...INFO_EN } });
    render(<App />);
    expect(await screen.findByRole("heading", { name: "Overview" })).toBeInTheDocument();
  });

  it("keeps the user's language over the configured default", async () => {
    chooseLanguage("en");
    installFakeApi({ authenticated: true });
    render(<App />);
    expect(await screen.findByRole("heading", { name: "Overview" })).toBeInTheDocument();
  });
});

describe("live status", () => {
  it("shows states and reasons from the event stream", async () => {
    installFakeApi({ authenticated: true });
    render(<App />);
    expect(await screen.findByText("Server offline")).toBeInTheDocument();
    expect(screen.getByText("Nicht erreichbar")).toBeInTheDocument();
    expect(screen.getByText("Online")).toBeInTheDocument();
    expect(screen.getByTestId("checked-at")).toHaveTextContent("Zuletzt geprüft:");
    expect(latestStream().url).toBe("/api/events");
  });

  it("updates live and distinguishes API problems", async () => {
    installFakeApi({ authenticated: true });
    render(<App />);
    await screen.findByText("Server offline");
    act(() => {
      latestStream().emit("status", {
        ...STATUS,
        target: { state: "online", reason: null },
        vm: { state: "unknown", reason: "api_key_invalid" },
      });
    });
    expect(await screen.findByText("API-Key ungültig")).toBeInTheDocument();
    act(() => {
      latestStream().emit("status", {
        ...STATUS,
        target: { state: "online", reason: null },
        vm: { state: "unknown", reason: "api_cert_changed" },
      });
    });
    expect(await screen.findByText(/Zertifikat geändert/)).toBeInTheDocument();
  });

  it("shows a running copy job", async () => {
    installFakeApi({ authenticated: true, status: { ...STATUS, job_running: true } });
    render(<App />);
    expect(await screen.findByText("Ein Kopierjob läuft.")).toBeInTheDocument();
  });

  it("reports an unavailable worker", async () => {
    installFakeApi({ authenticated: true, status: null });
    render(<App />);
    await screen.findByText("Unraid Klein");
    act(() => {
      latestStream().emit("worker_unavailable", {});
    });
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Der Status ist gerade nicht verfügbar.",
    );
  });

  it("returns to login when the stream reports the end of the session", async () => {
    const server = installFakeApi({ authenticated: true });
    render(<App />);
    await screen.findByText("Server offline");
    server.authenticated = false;
    act(() => {
      latestStream().emit("session_ended", {});
    });
    expect(await screen.findByRole("heading", { name: "Anmelden" })).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("Die Sitzung ist abgelaufen.");
    expect(latestStream().readyState).toBe(2);
  });

  it("returns to login when the stream is rejected", async () => {
    const server = installFakeApi({ authenticated: true });
    render(<App />);
    await screen.findByText("Server offline");
    server.authenticated = false;
    act(() => {
      latestStream().fail(true);
    });
    expect(await screen.findByRole("heading", { name: "Anmelden" })).toBeInTheDocument();
  });

  it("shows a hint while the browser reconnects", async () => {
    installFakeApi({ authenticated: true });
    render(<App />);
    await screen.findByText("Server offline");
    act(() => {
      latestStream().fail(false);
    });
    expect(await screen.findByRole("alert")).toHaveTextContent("Live-Verbindung ist unterbrochen");
  });

  it("shows the read-only banner in real mode", async () => {
    installFakeApi({
      authenticated: true,
      mode: "real",
      info: { ...INFO_EN, default_language: "de", mode: "real", read_only: true },
    });
    render(<App />);
    expect(await screen.findByTestId("read-only-banner")).toHaveTextContent("Nur Statusanzeige");
    expect(screen.queryByTestId("mock-banner")).not.toBeInTheDocument();
  });
});

describe("settings", () => {
  it("offers the CA certificate with both fingerprints", async () => {
    installFakeApi({ authenticated: true });
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText("Unraid Klein");
    await user.click(screen.getByRole("button", { name: "Einstellungen" }));
    expect(await screen.findByTestId("ca-fingerprint")).toHaveTextContent("CA:FE:00:01");
    expect(screen.getByTestId("ca-fingerprint-sha1")).toHaveTextContent("CA:FE:5A:01");
    expect(screen.getByTestId("server-fingerprint")).toHaveTextContent("AB:CD:EF:01");
    expect(screen.getByText("TwinRaid CA 2026-10-13")).toBeInTheDocument();
    expect(screen.getByText(/noch 824 Tage/)).toBeInTheDocument();
    expect(screen.getByText(/automatisch erneuert/)).toBeInTheDocument();
    const link = screen.getByRole("link", { name: "CA-Zertifikat herunterladen" });
    expect(link).toHaveAttribute("href", "/api/tls/ca-certificate");
    expect(link).toHaveAttribute("download", "twinraid-ca.crt");
    await user.click(screen.getByRole("button", { name: "Zurück zur Übersicht" }));
    expect(await screen.findByText("Unraid Klein")).toBeInTheDocument();
  });

  it("explains that a custom certificate has no CA download", async () => {
    installFakeApi({ authenticated: true, tls: { ...TLS, source: "custom", ca: null } });
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText("Unraid Klein");
    await user.click(screen.getByRole("button", { name: "Einstellungen" }));
    expect(await screen.findByText(/eigenes Zertifikat aus \/config\/tls/)).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "CA-Zertifikat herunterladen" })).toBeNull();
  });

  it("warns that the connection to the target is unencrypted", async () => {
    installFakeApi({ authenticated: true });
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText("Unraid Klein");
    await user.click(screen.getByRole("button", { name: "Einstellungen" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Verbindung zu Unraid Groß unverschlüsselt, API-Key wird im Klartext übertragen.",
    );
  });

  it("confirms an encrypted connection", async () => {
    installFakeApi({
      authenticated: true,
      info: { ...INFO_EN, default_language: "de", target_connection_encrypted: true },
    });
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText("Unraid Klein");
    await user.click(screen.getByRole("button", { name: "Einstellungen" }));
    expect(await screen.findByTestId("connection-encrypted")).toHaveTextContent("HTTPS");
    expect(screen.queryByRole("alert")).toBeNull();
  });
});

const INFO_EN: AppInfo = {
  version: "0.3.0",
  mode: "mock",
  default_language: "en",
  names: { source: "Unraid Klein", target: "Unraid Groß", vm: "Gaming-VM" },
  read_only: false,
  target_connection_encrypted: false,
};
