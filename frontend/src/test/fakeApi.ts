// Simulierter TwinRaid-Server für Komponenten-Tests.
import { vi } from "vitest";

import type { AppInfo, AuditEntry, RunMode, StatusSnapshot, TlsInfo } from "@/api/types";

export const PASSWORD = "richtiges-passwort";
export const CSRF = "csrf-token-123";

export const INFO: AppInfo = {
  version: "0.1.0",
  mode: "mock",
  default_language: "de",
  names: { source: "Unraid Klein", target: "Unraid Groß", vm: "Gaming-VM" },
};

export const STATUS: StatusSnapshot = {
  source: "unknown",
  target: "unknown",
  vm: "unknown",
  checked_at: null,
  password_set: true,
};

export const TLS: TlsInfo = {
  source: "self-signed",
  fingerprint_sha256: "AB:CD:EF:01",
  not_after: "2029-01-15T12:00:00Z",
  days_left: 824,
  expires_soon: false,
};

export const AUDIT: AuditEntry[] = [
  { at: "2026-10-13T12:00:00Z", client_ip: "192.168.20.5", action: "login", result: "success" },
  { at: "2026-10-13T11:59:00Z", client_ip: null, action: "cli_set_password", result: "success" },
];

export interface FakeServer {
  authenticated: boolean;
  passwordSet: boolean | null;
  mode: RunMode;
  info: AppInfo;
  tls: TlsInfo;
  // Antwort auf einen fehlgeschlagenen Login: Statuscode und Inhalt.
  loginFailure: { status: number; body: Record<string, unknown> };
  failAuthState: boolean;
  calls: { url: string; method: string; headers: Headers }[];
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function urlOf(input: RequestInfo | URL): string {
  return typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
}

export function installFakeApi(overrides: Partial<FakeServer> = {}): FakeServer {
  const server: FakeServer = {
    authenticated: false,
    passwordSet: true,
    mode: "mock",
    info: INFO,
    tls: TLS,
    loginFailure: { status: 401, body: { detail: "invalid_password" } },
    failAuthState: false,
    calls: [],
    ...overrides,
  };

  const protectedRoutes: Record<string, () => unknown> = {
    "/api/auth/session": () => ({
      csrf_token: CSRF,
      expires_at: "2026-10-13T12:30:00Z",
      idle_timeout_seconds: 1800,
    }),
    "/api/info": () => server.info,
    "/api/status": () => STATUS,
    "/api/audit": () => ({ entries: AUDIT }),
    "/api/tls": () => server.tls,
  };

  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = urlOf(input);
      const method = (init?.method ?? "GET").toUpperCase();
      const headers = new Headers(init?.headers);
      server.calls.push({ url, method, headers });

      if (url === "/api/auth/state") {
        if (server.failAuthState) {
          return Promise.reject(new TypeError("network"));
        }
        return Promise.resolve(
          json({
            authenticated: server.authenticated,
            mode: server.mode,
            password_set: server.passwordSet,
          }),
        );
      }
      if (url === "/api/auth/login" && method === "POST") {
        const raw = typeof init?.body === "string" ? init.body : "{}";
        const body = JSON.parse(raw) as { password: string };
        if (body.password === PASSWORD) {
          server.authenticated = true;
          return Promise.resolve(json(protectedRoutes["/api/auth/session"]?.()));
        }
        return Promise.resolve(json(server.loginFailure.body, server.loginFailure.status));
      }
      if (!server.authenticated) {
        return Promise.resolve(json({ detail: "not_authenticated" }, 401));
      }
      if (url === "/api/auth/logout" && method === "POST") {
        if (headers.get("X-CSRF-Token") !== CSRF) {
          return Promise.resolve(json({ detail: "csrf_rejected" }, 403));
        }
        server.authenticated = false;
        return Promise.resolve(json({ detail: "logged_out" }));
      }
      const handler = protectedRoutes[url];
      return Promise.resolve(handler ? json(handler()) : json({ detail: "not found" }, 404));
    }),
  );
  return server;
}
