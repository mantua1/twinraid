import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { AppInfo, StatusSnapshot } from "@/api/types";
import { chooseLanguage } from "@/i18n";

import { App } from "./App";

const INFO: AppInfo = {
  version: "0.1.0",
  mode: "mock",
  default_language: "de",
  names: { source: "Unraid Klein", target: "Unraid Groß", vm: "Gaming-VM" },
};

const STATUS: StatusSnapshot = {
  source: "unknown",
  target: "unknown",
  vm: "unknown",
  checked_at: null,
};

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function urlOf(input: RequestInfo | URL): string {
  return typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
}

function mockApi(routes: Record<string, () => Response>) {
  const fetchMock = vi.fn((input: RequestInfo | URL) => {
    const handler = routes[urlOf(input)];
    return Promise.resolve(handler ? handler() : json({ detail: "not found" }, 404));
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

describe("App", () => {
  it("shows the simulation banner in mock mode", async () => {
    mockApi({ "/api/info": () => json(INFO), "/api/status": () => json(STATUS) });
    render(<App />);
    const banner = await screen.findByTestId("mock-banner");
    expect(banner).toHaveTextContent("Simulationsmodus");
    expect(banner).toHaveTextContent("Es werden keine echten Server angesprochen");
  });

  it("does not show the simulation banner in real mode", async () => {
    mockApi({
      "/api/info": () => json({ ...INFO, mode: "real" }),
      "/api/status": () => json(STATUS),
    });
    render(<App />);
    await screen.findByText("Unraid Klein");
    expect(screen.queryByTestId("mock-banner")).not.toBeInTheDocument();
  });

  it("shows the configured names and unknown status", async () => {
    mockApi({ "/api/info": () => json(INFO), "/api/status": () => json(STATUS) });
    render(<App />);
    expect(await screen.findByText("Unraid Klein")).toBeInTheDocument();
    expect(screen.getByText("Unraid Groß")).toBeInTheDocument();
    expect(screen.getByText("Gaming-VM")).toBeInTheDocument();
    expect(screen.getAllByText("Unbekannt")).toHaveLength(3);
    expect(screen.getByText("Sync")).toBeInTheDocument();
    expect(screen.getByText("Verlauf")).toBeInTheDocument();
  });

  it("only talks to its own server", async () => {
    const fetchMock = mockApi({
      "/api/info": () => json(INFO),
      "/api/status": () => json(STATUS),
    });
    render(<App />);
    await screen.findByText("Unraid Klein");
    for (const [input] of fetchMock.mock.calls) {
      expect(urlOf(input)).toMatch(/^\/api\//);
    }
  });

  it("shows a connection error when the server is unreachable", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.reject(new TypeError("network"))),
    );
    render(<App />);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Keine Verbindung zum TwinRaid-Server.",
    );
    expect(screen.getAllByText("Unbekannt")).toHaveLength(3);
  });

  it("shows a status error when the worker is unavailable", async () => {
    mockApi({
      "/api/info": () => json(INFO),
      "/api/status": () => json({ detail: "worker_unavailable" }, 503),
    });
    render(<App />);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Der Status ist gerade nicht verfügbar.",
    );
  });

  it("uses the configured default language", async () => {
    mockApi({
      "/api/info": () => json({ ...INFO, default_language: "en" }),
      "/api/status": () => json(STATUS),
    });
    render(<App />);
    expect(await screen.findByText("Simulation mode")).toBeInTheDocument();
    expect(document.documentElement.lang).toBe("en");
  });

  it("switches the language and remembers the choice", async () => {
    mockApi({ "/api/info": () => json(INFO), "/api/status": () => json(STATUS) });
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText("Simulationsmodus");
    await user.click(screen.getByRole("button", { name: "Englisch" }));
    expect(await screen.findByText("Simulation mode")).toBeInTheDocument();
    expect(screen.getAllByText("Unknown")).toHaveLength(3);
    expect(localStorage.getItem("twinraid.language")).toBe("en");
  });

  it("keeps the user's language over the configured default", async () => {
    // Wie beim Seitenstart: gespeicherte Wahl ist bereits aktiv.
    chooseLanguage("en");
    mockApi({ "/api/info": () => json(INFO), "/api/status": () => json(STATUS) });
    render(<App />);
    await screen.findByText("Unraid Klein");
    expect(screen.getByText("Overview")).toBeInTheDocument();
  });
});
