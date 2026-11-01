// Typisierter Client für die REST-API. Anfragen gehen nur an den eigenen Server.
import {
  ACTION_BLOCKERS,
  LOGIN_ERRORS,
  type ActionBlocker,
  type ActionName,
  type AppInfo,
  type AuditEntry,
  type AuthState,
  type LoginErrorCode,
  type SessionInfo,
  type TlsInfo,
} from "./types";

export const CA_CERTIFICATE_URL = "/api/tls/ca-certificate";
// Live-Datenstrom (SSE); der Browser sendet das Session-Cookie automatisch mit.
export const EVENTS_URL = "/api/events";

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number) {
    super(`API request failed with status ${String(status)}`);
    this.name = "ApiError";
    this.status = status;
  }
}

/** Die Session fehlt oder ist abgelaufen. */
export class UnauthorizedError extends ApiError {
  constructor() {
    super(401);
    this.name = "UnauthorizedError";
  }
}

/** Der Worker hat die Aktion abgelehnt; reason nennt den Grund (Konzept 4.2). */
export class ActionRejectedError extends Error {
  readonly reason: ActionBlocker | "rejected";

  constructor(reason: ActionBlocker | "rejected") {
    super(`Action rejected: ${reason}`);
    this.name = "ActionRejectedError";
    this.reason = reason;
  }
}

export class LoginError extends Error {
  readonly code: LoginErrorCode;
  readonly retryAfterSeconds: number | null;

  constructor(code: LoginErrorCode, retryAfterSeconds: number | null = null) {
    super(`Login failed: ${code}`);
    this.name = "LoginError";
    this.code = code;
    this.retryAfterSeconds = retryAfterSeconds;
  }
}

// CSRF-Token der aktuellen Session, nur im Speicher (Konzept 5.3).
let csrfToken: string | null = null;

export function setCsrfToken(token: string | null): void {
  csrfToken = token;
}

async function request(path: string, init: RequestInit = {}): Promise<Response> {
  const headers = new Headers(init.headers);
  headers.set("Accept", "application/json");
  const method = (init.method ?? "GET").toUpperCase();
  if (method !== "GET" && method !== "HEAD" && csrfToken !== null) {
    headers.set("X-CSRF-Token", csrfToken);
  }
  const response = await fetch(path, { ...init, headers, credentials: "same-origin" });
  if (response.status === 401) {
    throw new UnauthorizedError();
  }
  if (!response.ok) {
    throw new ApiError(response.status);
  }
  return response;
}

async function getJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await request(path, { signal });
  return (await response.json()) as T;
}

function loginErrorCode(detail: unknown): LoginErrorCode {
  return (LOGIN_ERRORS as readonly unknown[]).includes(detail)
    ? (detail as LoginErrorCode)
    : "network";
}

async function login(password: string): Promise<SessionInfo> {
  let response: Response;
  try {
    response = await fetch("/api/auth/login", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({ password }),
    });
  } catch {
    throw new LoginError("network");
  }
  const body = (await response.json().catch(() => ({}))) as Record<string, unknown>;
  if (!response.ok) {
    const retry = typeof body.retry_after === "number" ? body.retry_after : null;
    throw new LoginError(loginErrorCode(body.detail), retry);
  }
  const session = body as unknown as SessionInfo;
  setCsrfToken(session.csrf_token);
  return session;
}

async function logout(): Promise<void> {
  try {
    await request("/api/auth/logout", { method: "POST" });
  } finally {
    setCsrfToken(null);
  }
}

/** Bestätigt den Hinweis zum CA-Wechsel (sendet keine Daten, nur das CSRF-Token). */
async function acknowledgeCaChange(): Promise<void> {
  await request("/api/tls/ca-change/acknowledge", { method: "POST" });
}

/** Fordert eine Aktion an. Sendet keine Daten, nur das CSRF-Token (Konzept 4.2). */
async function runAction(name: ActionName): Promise<void> {
  const headers = new Headers({ Accept: "application/json" });
  if (csrfToken !== null) {
    headers.set("X-CSRF-Token", csrfToken);
  }
  const response = await fetch(`/api/actions/${name}`, {
    method: "POST",
    headers,
    credentials: "same-origin",
  });
  if (response.status === 401) {
    throw new UnauthorizedError();
  }
  if (response.status === 409) {
    const body = (await response.json().catch(() => ({}))) as { detail?: unknown };
    const known = (ACTION_BLOCKERS as readonly unknown[]).includes(body.detail);
    throw new ActionRejectedError(known ? (body.detail as ActionBlocker) : "rejected");
  }
  if (!response.ok) {
    throw new ApiError(response.status);
  }
}

/**
 * Bricht den laufenden Probelauf ab (Konzept 4.3). Einziger Parameter ist die Kennung,
 * die der Worker vergeben hat; false, wenn der Worker ablehnt (kein solcher Lauf).
 */
async function cancelSync(jobId: string): Promise<boolean> {
  try {
    await request(`/api/sync/${encodeURIComponent(jobId)}/cancel`, { method: "POST" });
  } catch (error) {
    if (error instanceof ApiError && error.status === 409) {
      return false;
    }
    throw error;
  }
  return true;
}

async function session(signal?: AbortSignal): Promise<SessionInfo> {
  const info = await getJson<SessionInfo>("/api/auth/session", signal);
  setCsrfToken(info.csrf_token);
  return info;
}

export const api = {
  authState: (signal?: AbortSignal) => getJson<AuthState>("/api/auth/state", signal),
  login,
  logout,
  session,
  info: (signal?: AbortSignal) => getJson<AppInfo>("/api/info", signal),
  audit: async (signal?: AbortSignal) =>
    (await getJson<{ entries: AuditEntry[] }>("/api/audit", signal)).entries,
  tls: (signal?: AbortSignal) => getJson<TlsInfo>("/api/tls", signal),
  acknowledgeCaChange,
  runAction,
  cancelSync,
};
