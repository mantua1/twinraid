// Simulierter TwinRaid-Server für Komponenten-Tests.
import { vi } from "vitest";

import type {
  GuardCheck,
  GuardDataset,
  GuardDetails,
  SourceGuardStatus,
  SyncReport,
  SyncShareReport,
  SyncStatus,
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
  actions: { wake: true, vm: true, shutdown: true, sync_dry_run: true, sync: true },
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
      sync_dry_run: null,
      sync_start: "next_version",
      ...availability,
    },
    target: { running: null, last: null },
    vm: { running: null, last: null },
    force_available_at: null,
    forced_shutdown_until: null,
    ...extra,
  };
}

const GIB = 1024 ** 3;

/** Dataset des Wächters; ohne Snapshots, Basis und Lesezeichen, wenn nicht angegeben. */
export function dataset(
  name: string,
  present: boolean | null = true,
  extra: Partial<GuardDataset> = {},
): GuardDataset {
  return {
    name,
    present,
    referenced: present === true ? 2 * GIB : null,
    compression: present === true ? "on" : null,
    snapshots: null,
    oldest_snapshot_at: null,
    base_created_at: null,
    bookmark: null,
    ...extra,
  };
}

/** Aktiver Wächter auf Klein mit einem freigegebenen Container (Konzept 3.6). */
export const GUARD_DETAILS: GuardDetails = {
  version: 2,
  started_at: "2026-10-13T08:00:00Z",
  allowed: [{ name: "twinraid-testcontainer", state: "running" }],
  run: null,
  last_run: null,
  run_allowed_at: null,
  zfs: {
    state: "ok",
    pool_used: 120 * 1024 ** 3,
    pool_available: 680 * 1024 ** 3,
    datasets: [dataset("twinraidtest")],
    self_dataset: "found",
  },
  limits: {
    max_stop_minutes: 20,
    heartbeat_timeout_seconds: 120,
    stop_timeout_seconds: 60,
    min_run_interval_minutes: 30,
    max_deletions: 1000,
    snapshot_keep_days: 7,
    snapshot_reserve_percent: 15,
  },
  target_configured: false,
  target_port: 22,
  rejected_count: 0,
  target_key: false,
  target_known_host: false,
  target_check: null,
};

export const GUARD_ACTIVE: SourceGuardStatus = {
  state: "active",
  setup_problem: null,
  status_problem: null,
  error: null,
  error_detail: null,
  config_line: null,
  heartbeat_at: "2026-10-13T12:00:00Z",
  heartbeat_age_seconds: 4,
  details: GUARD_DETAILS,
};

/** Kein Probelauf bisher; die Ebenen passen auf Klein (Konzept 4.3). */
export const SYNC_IDLE: SyncStatus = {
  running: null,
  last: null,
  report: null,
  levels: {
    configured: ["twinraidtest"],
    source: ["twinraidtest"],
    target: null,
    target_checked_at: null,
  },
};

/** Antwort von guard.sh sync check wie in der Simulation (Konzept 6.4). */
export const CHECK: GuardCheck = {
  at: "2026-10-13T12:01:00Z",
  result: "ok",
  summary: {
    root: "ok",
    conf: "ok",
    deleg: "ok",
    receive: "idle",
    clones: 0,
    available: 600 * GIB,
    size: 860 * GIB,
    reserve_percent: 15,
    keep_days: 30,
    min_interval_minutes: 10,
  },
  mirrors: [
    {
      name: "twinraidtest",
      state: "mirror",
      props: "ok",
      count: 5,
      newest_created: "2026-10-12T12:00:00Z",
      newest_received: "2026-10-12T12:12:00Z",
      oldest_created: "2026-09-14T12:00:00Z",
    },
  ],
};

export const SHARE_REPORT: SyncShareReport = {
  name: "twinraidtest",
  mode: "incremental",
  base_type: "snapshot",
  problem: null,
  deleted: 12,
  modified: 34,
  added: 56,
  renamed: 2,
  referenced: 2 * GIB,
  base_referenced: 2.1 * GIB,
  size: 350 * 1024 ** 2,
  mirror: { state: "mirror", props: "ok", count: 5, newest_received: "2026-10-12T12:12:00Z" },
};

export const REPORT: SyncReport = {
  prepared_at: "2026-10-13T12:01:00Z",
  snapshot: "twinraid-20261013T120055Z",
  awaiting_confirm: false,
  confirm_reasons: [],
  send_ready: true,
  blockers: [],
  check: CHECK,
  shares: [SHARE_REPORT],
};

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
  watch_retry: null,
  source_guard: GUARD_ACTIVE,
  target_zfs: null,
  sync: SYNC_IDLE,
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
  // Abbrechen eines Probelaufs: false lehnt ab (409); cancelled nennt die Kennungen.
  cancelAccepted: boolean;
  cancelled: string[];
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
    cancelAccepted: true,
    cancelled: [],
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
      const cancel = /^\/api\/sync\/([0-9a-f]{32})\/cancel$/.exec(url);
      if (cancel !== null && method === "POST") {
        if (headers.get("X-CSRF-Token") !== CSRF) {
          return Promise.resolve(json({ detail: "csrf_rejected" }, 403));
        }
        if (!server.cancelAccepted) {
          return Promise.resolve(json({ detail: "rejected" }, 409));
        }
        server.cancelled.push(cancel[1] ?? "");
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
