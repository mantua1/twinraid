// Antworttypen der REST-API (Spiegel von backend/src/twinraid/web/schemas.py und shared/status.py).

export const RESOURCE_STATES = ["online", "offline", "starting", "stopping", "unknown"] as const;
export type ResourceState = (typeof RESOURCE_STATES)[number];

export type RunMode = "mock" | "real";

export interface StatusSnapshot {
  source: ResourceState;
  target: ResourceState;
  vm: ResourceState;
  checked_at: string | null;
  password_set: boolean;
}

export interface AppInfo {
  version: string;
  mode: RunMode;
  default_language: "de" | "en";
  names: {
    source: string;
    target: string;
    vm: string;
  };
}

export interface AuthState {
  authenticated: boolean;
  mode: RunMode;
  password_set: boolean | null;
}

export interface SessionInfo {
  csrf_token: string;
  expires_at: string;
  idle_timeout_seconds: number;
}

export const AUDIT_ACTIONS = [
  "login",
  "logout",
  "session",
  "csrf_check",
  "origin_check",
  "cli_set_password",
  "cli_renew_cert",
  "cli_unlock",
] as const;
export type AuditAction = (typeof AUDIT_ACTIONS)[number];

export const AUDIT_RESULTS = [
  "success",
  "invalid_password",
  "password_not_set",
  "locked",
  "lockout_started",
  "busy",
  "expired",
  "rejected",
] as const;
export type AuditResult = (typeof AUDIT_RESULTS)[number];

export interface AuditEntry {
  at: string;
  client_ip: string | null;
  action: string;
  result: string;
}

export interface TlsInfo {
  source: "self-signed" | "custom";
  fingerprint_sha256: string;
  not_after: string;
  days_left: number;
  expires_soon: boolean;
}

export const LOGIN_ERRORS = [
  "invalid_password",
  "password_not_set",
  "locked",
  "busy",
  "worker_unavailable",
  "invalid_request",
] as const;
export type LoginErrorCode = (typeof LOGIN_ERRORS)[number] | "network";
