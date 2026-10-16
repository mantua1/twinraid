// Antworttypen der REST-API (Spiegel von backend/src/twinraid/web/schemas.py und shared/status.py).

export const RESOURCE_STATES = ["online", "offline", "starting", "stopping", "unknown"] as const;
export type ResourceState = (typeof RESOURCE_STATES)[number];

// Feste Liste von Ursachen (Konzept 3.5 und 4.1).
export const STATUS_REASONS = [
  "unreachable",
  "runs_here",
  "target_offline",
  "target_starting",
  "api_unreachable",
  "api_redirected",
  "api_key_missing",
  "api_key_invalid",
  "api_forbidden",
  "api_cert_untrusted",
  "api_cert_changed",
  "api_error",
  "vm_not_found",
  "vm_paused",
  "vm_suspended",
  "vm_crashed",
] as const;
export type StatusReason = (typeof STATUS_REASONS)[number];

export type RunMode = "mock" | "real";

export interface ResourceInfo {
  state: ResourceState;
  reason: StatusReason | null;
}

// Aktionen (Konzept 4.2); Spiegel von backend/src/twinraid/shared/actions.py.
export const ACTION_NAMES = [
  "wake_target",
  "shutdown_target",
  "shutdown_target_force_vm",
  "vm_start",
  "vm_stop",
  "vm_force_stop",
] as const;
export type ActionName = (typeof ACTION_NAMES)[number];

export const ACTION_BLOCKERS = [
  "not_enabled",
  "busy",
  "sync_running",
  "target_online",
  "target_offline",
  "target_unknown",
  "vm_running",
  "vm_off",
  "vm_unknown",
  "force_not_yet",
  "not_offered",
  "tour",
] as const;
export type ActionBlocker = (typeof ACTION_BLOCKERS)[number];

export const ACTION_STEPS = [
  "waking",
  "waiting_for_api",
  "starting_vm",
  "stopping_vm",
  "forcing_vm",
  "shutting_down",
] as const;
export type ActionStep = (typeof ACTION_STEPS)[number];

export const ACTION_OUTCOMES = [
  "success",
  "wol_failed",
  "boot_timeout",
  "api_unreachable",
  "api_redirected",
  "api_key_missing",
  "api_key_invalid",
  "api_forbidden",
  "api_cert_untrusted",
  "api_cert_changed",
  "api_error",
  "vm_not_found",
  "vm_start_timeout",
  "vm_stop_timeout",
  "ssh_key_missing",
  "ssh_host_untrusted",
  "ssh_host_key_mismatch",
  "ssh_auth_failed",
  "ssh_unreachable",
  "ssh_denied",
  "ssh_failed",
  "remote_failed",
  "shutdown_timeout",
  "interrupted",
  "internal_error",
] as const;
export type ActionOutcome = (typeof ACTION_OUTCOMES)[number];

export interface RunningAction {
  action: ActionName;
  step: ActionStep;
  started_at: string;
}

export interface ActionResult {
  action: ActionName;
  outcome: ActionOutcome;
  finished_at: string;
}

export interface ResourceActivity {
  running: RunningAction | null;
  last: ActionResult | null;
}

export interface ActionsSnapshot {
  // null: verfügbar; sonst der Grund, warum nicht.
  availability: Record<ActionName, ActionBlocker | null>;
  target: ResourceActivity;
  vm: ResourceActivity;
  // Gesetzt, sobald ein Herunterfahren der VM läuft oder gescheitert ist.
  force_available_at: string | null;
  // Bis wann „Erzwingen und danach Server herunterfahren“ angeboten wird (Konzept 4.2).
  forced_shutdown_until: string | null;
}

export interface StatusSnapshot {
  source: ResourceInfo;
  target: ResourceInfo;
  vm: ResourceInfo;
  checked_at: string | null;
  job_running: boolean;
  password_set: boolean;
  actions: ActionsSnapshot;
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
  read_only: boolean;
  // Freigeschaltete Aktionen; im Simulationsmodus alle.
  actions: { wake: boolean; vm: boolean; shutdown: boolean };
  vm_shutdown_timeout_minutes: number;
  target_connection_encrypted: boolean;
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
  "cli_set_api_key",
  "cli_trust_api_cert",
  "ca_change_notice",
  "cli_init_ssh_keys",
  "cli_trust_host_key",
  ...ACTION_NAMES,
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
  "acknowledged",
  "requested",
] as const;
export type AuditResult = (typeof AUDIT_RESULTS)[number];

export interface AuditEntry {
  at: string;
  client_ip: string | null;
  action: string;
  result: string;
}

export interface CertificateDetails {
  fingerprint_sha256: string;
  not_after: string;
  days_left: number;
  expires_soon: boolean;
}

export interface CaDetails extends CertificateDetails {
  name: string;
  fingerprint_sha1: string;
}

export interface TlsAddressInfo {
  ip_addresses: string[];
  hostnames: string[];
  origin: "detected" | "configured";
  interface: string | null;
}

export const CA_CHANGE_REASONS = [
  "addresses_changed",
  "expired",
  "requested",
  "unusable",
  "legacy_replaced",
] as const;
export type CaChangeReason = (typeof CA_CHANGE_REASONS)[number];

export interface CaChangeInfo {
  changed_at: string;
  reason: CaChangeReason;
}

export interface TlsInfo {
  source: "ca" | "custom";
  server: CertificateDetails;
  ca: CaDetails | null;
  renewal_failed: boolean;
  addresses: TlsAddressInfo;
  // Gesetzt, bis der Hinweis zum CA-Wechsel bestätigt ist.
  ca_change: CaChangeInfo | null;
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
