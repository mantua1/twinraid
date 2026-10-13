import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { chooseLanguage } from "@/i18n";
import { CSRF, installFakeApi, PASSWORD } from "@/test/fakeApi";

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
    expect(screen.getAllByText("Unbekannt")).toHaveLength(3);
    expect(screen.getByTestId("mock-banner")).toBeInTheDocument();
  });

  it("only talks to its own server", async () => {
    const server = installFakeApi();
    render(<App />);
    await signIn();
    await screen.findByText("Unraid Klein");
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

  it("warns before the certificate expires", async () => {
    installFakeApi({
      authenticated: true,
      tls: {
        source: "self-signed",
        fingerprint_sha256: "AA",
        not_after: "2026-11-01T00:00:00Z",
        days_left: 19,
        expires_soon: true,
      },
    });
    render(<App />);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Das TLS-Zertifikat läuft in 19 Tagen ab.",
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

describe("settings", () => {
  it("shows the certificate and offers the download", async () => {
    installFakeApi({ authenticated: true });
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText("Unraid Klein");
    await user.click(screen.getByRole("button", { name: "Einstellungen" }));
    expect(await screen.findByTestId("fingerprint")).toHaveTextContent("AB:CD:EF:01");
    expect(screen.getByText("Selbstsigniert")).toBeInTheDocument();
    expect(screen.getByText(/noch 824 Tage/)).toBeInTheDocument();
    const link = screen.getByRole("link", { name: "Zertifikat herunterladen" });
    expect(link).toHaveAttribute("href", "/api/tls/certificate");
    expect(link).toHaveAttribute("download", "twinraid.crt");
    await user.click(screen.getByRole("button", { name: "Zurück zur Übersicht" }));
    expect(await screen.findByText("Unraid Klein")).toBeInTheDocument();
  });
});

const INFO_EN = {
  version: "0.1.0",
  mode: "mock" as const,
  default_language: "en" as const,
  names: { source: "Unraid Klein", target: "Unraid Groß", vm: "Gaming-VM" },
};
