// Typisierter Client für die REST-API. Anfragen gehen nur an den eigenen Server.
import {
  LOGIN_ERRORS,
  type AppInfo,
  type AuditEntry,
  type AuthState,
  type LoginErrorCode,
  type SessionInfo,
  type StatusSnapshot,
  type TlsInfo,
} from "./types";

export const CERTIFICATE_URL = "/api/tls/certificate";

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
  status: (signal?: AbortSignal) => getJson<StatusSnapshot>("/api/status", signal),
  audit: async (signal?: AbortSignal) =>
    (await getJson<{ entries: AuditEntry[] }>("/api/audit", signal)).entries,
  tls: (signal?: AbortSignal) => getJson<TlsInfo>("/api/tls", signal),
};
