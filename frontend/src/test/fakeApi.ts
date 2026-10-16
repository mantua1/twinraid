// Simulierter TwinRaid-Server für Komponenten-Tests.
import { vi } from "vitest";

import type {
  ActionBlocker,
  ActionName,
  ActionsSnapshot,
  AppInfo,
  AuditEntry,
  RunMode,
  StatusSnapshot,
  TlsInfo,
} from "@/api/types";

export const PASSWORD = "richtiges-passwort";
export const CSRF = "csrf-token-123";

export const INFO: AppInfo = {
  version: "0.3.1",
  mode: "mock",
  default_language: "de",
  names: { source: "Unraid Klein", target: "Unraid Groß", vm: "Gaming-VM" },
  read_only: false,
  actions: { wake: true, vm: true, shutdown: true },
  vm_shutdown_timeout_minutes: 3,
};

/** Aktionsteil des Status; availability überschreibt einzelne Gründe. */
export function actionsSnapshot(
  availability: Partial<Record<ActionName, ActionBlocker | null>> = {},
  extra: Partial<ActionsSnapshot> = {},
): ActionsSnapshot {
  return {
    availability: {
      wake_target: null,
      shutdown_target: "target_offline",
      shutdown_target_force_vm: "not_offered",
      vm_start: null,
      vm_stop: "target_offline",
      vm_force_stop: "target_offline",
      ...availability,
    },
    target: { running: null, last: null },
    vm: { running: null, last: null },
    force_available_at: null,
    forced_shutdown_until: null,
    ...extra,
  };
}

export const STATUS: StatusSnapshot = {
  source: { state: "online", reason: null },
  target: { state: "offline", reason: "unreachable" },
  vm: { state: "offline", reason: "target_offline" },
  checked_at: "2026-10-13T12:00:05Z",
  job_running: false,
  password_set: true,
  actions: actionsSnapshot(),
  other_vms_running: null,
  notices: [],
};

/** Groß und VM laufen: Herunterfahren ist möglich, Erzwingen noch nicht. */
export const STATUS_VM_RUNNING: StatusSnapshot = {
  ...STATUS,
  target: { state: "online", reason: null },
  vm: { state: "online", reason: null },
  other_vms_running: [],
  actions: actionsSnapshot({
    wake_target: "target_online",
    shutdown_target: null,
    vm_start: "vm_running",
    vm_stop: null,
    vm_force_stop: "force_not_yet",
  }),
};

export const TLS: TlsInfo = {
  source: "ca",
  server: {
    fingerprint_sha256: "AB:CD:EF:01",
    not_after: "2029-01-15T12:00:00Z",
    days_left: 824,
    expires_soon: false,
  },
  ca: {
    name: "TwinRaid CA 2026-10-13",
    fingerprint_sha256: "CA:FE:00:01",
    fingerprint_sha1: "CA:FE:5A:01",
    not_after: "2036-10-10T12:00:00Z",
    days_left: 3649,
    expires_soon: false,
  },
  renewal_failed: false,
  addresses: {
    ip_addresses: ["192.168.10.20"],
    hostnames: [],
    origin: "detected",
    interface: "eth0",
  },
  ca_change: null,
};

/** Simulierter Live-Datenstrom; Tests senden Ereignisse mit emit(). */
export class FakeEventSource {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSED = 2;
  static instances: FakeEventSource[] = [];

  readonly url: string;
  readyState = FakeEventSource.CONNECTING;
  onerror: ((event: Event) => void) | null = null;
  private readonly listeners = new Map<string, ((event: MessageEvent<string>) => void)[]>();

  constructor(url: string) {
    this.url = url;
    FakeEventSource.instances.push(this);
  }

  addEventListener(type: string, listener: (event: MessageEvent<string>) => void): void {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), listener]);
  }

  emit(type: string, data: unknown): void {
    this.readyState = FakeEventSource.OPEN;
    const event = new MessageEvent<string>(type, { data: JSON.stringify(data) });
    for (const listener of this.listeners.get(type) ?? []) {
      listener(event);
    }
  }

  fail(closed: boolean): void {
    this.readyState = closed ? FakeEventSource.CLOSED : FakeEventSource.CONNECTING;
    this.onerror?.(new Event("error"));
  }

  close(): void {
    this.readyState = FakeEventSource.CLOSED;
  }
}

export function latestStream(): FakeEventSource {
  const stream = FakeEventSource.instances.at(-1);
  if (stream === undefined) {
    throw new Error("no event stream was opened");
  }
  return stream;
}

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
  // Status, den der Live-Datenstrom direkt nach dem Verbinden sendet; null: keiner.
  status: StatusSnapshot | null;
  // Antwort auf eine Aktion: null nimmt an, sonst Ablehnung mit diesem Grund (409).
  actionRejection: string | null;
  actions: string[];
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
    status: STATUS,
    actionRejection: null,
    actions: [],
    calls: [],
    ...overrides,
  };

  FakeEventSource.instances = [];
  class ConnectedEventSource extends FakeEventSource {
    constructor(url: string) {
      super(url);
      server.calls.push({ url, method: "EVENTSOURCE", headers: new Headers() });
      // Wie der Browser: asynchron verbinden; ohne Session antwortet der Server mit 401.
      queueMicrotask(() => {
        if (!server.authenticated) {
          this.fail(true);
        } else if (server.status !== null) {
          this.emit("status", server.status);
        }
      });
    }
  }
  vi.stubGlobal("EventSource", ConnectedEventSource);

  const protectedRoutes: Record<string, () => unknown> = {
    "/api/auth/session": () => ({
      csrf_token: CSRF,
      expires_at: "2026-10-13T12:30:00Z",
      idle_timeout_seconds: 1800,
    }),
    "/api/info": () => server.info,
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
      if (url === "/api/tls/ca-change/acknowledge" && method === "POST") {
        if (headers.get("X-CSRF-Token") !== CSRF) {
          return Promise.resolve(json({ detail: "csrf_rejected" }, 403));
        }
        server.tls = { ...server.tls, ca_change: null };
        return Promise.resolve(json({ detail: "acknowledged" }));
      }
      if (url.startsWith("/api/actions/") && method === "POST") {
        if (headers.get("X-CSRF-Token") !== CSRF) {
          return Promise.resolve(json({ detail: "csrf_rejected" }, 403));
        }
        if (server.actionRejection !== null) {
          return Promise.resolve(json({ detail: server.actionRejection }, 409));
        }
        server.actions.push(url.slice("/api/actions/".length));
        return Promise.resolve(json({ detail: "accepted" }, 202));
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
