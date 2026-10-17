// Antworttypen der REST-API (Spiegel von backend/src/twinraid/web/schemas.py und shared/status.py).

export const RESOURCE_STATES = ["online", "offline", "starting", "stopping", "unknown"] as const;
export type ResourceState = (typeof RESOURCE_STATES)[number];

// Feste Liste von Ursachen (Konzept 3.5 und 4.1).
export const STATUS_REASONS = [
  "unreachable",
  "runs_here",
  "target_offline",
  "target_starting",
  "watch_connecting",
  "watch_interrupted",
  "ssh_key_missing",
  "ssh_host_untrusted",
  "ssh_host_key_mismatch",
  "ssh_auth_failed",
  "ssh_unreachable",
  "ssh_denied",
  "ssh_failed",
  "guard_outdated",
  "vm_not_configured",
  "vm_not_found",
  "vm_service_unavailable",
  "vm_paused",
  "vm_suspended",
  "vm_crashed",
] as const;
export type StatusReason = (typeof STATUS_REASONS)[number];

// Feste Hinweise zu den Kacheln, ab v0.4.3 (Konzept 3.5 und 4.1).
export const STATUS_NOTICES = [
  "target_connect_plugin",
  "target_flash_backup",
  "vm_name_mismatch",
] as const;
export type StatusNotice = (typeof STATUS_NOTICES)[number];

export type RunMode = "mock" | "real";

export interface ResourceInfo {
  state: ResourceState;
  reason: StatusReason | null;
}

// Nächster Versuch der Statusverbindung, ab v0.4.4 (Konzept 3.5).
export interface WatchRetry {
  at: string;
  // Pause nach einem dauerhaften Fehler; trust-host-key beendet sie sofort.
  paused: boolean;
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
  "other_vms_running",
  "tour",
] as const;
export type ActionBlocker = (typeof ACTION_BLOCKERS)[number];

export const ACTION_STEPS = [
  "waking",
  "waiting_for_vm_service",
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
  "vm_not_found",
  "vm_not_configured",
  "vm_service_unavailable",
  "vm_start_timeout",
  "vm_stop_timeout",
  "ssh_key_missing",
  "ssh_host_untrusted",
  "ssh_host_key_mismatch",
  "ssh_auth_failed",
  "ssh_unreachable",
  "ssh_denied",
  "ssh_failed",
  "guard_outdated",
  "watch_interrupted",
  "remote_failed",
  "shutdown_timeout",
  "vm_running",
  "other_vms_running",
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
  // Weitere laufende VMs auf Groß (bereinigte Namen); null: unbekannt (Konzept 4.2).
  other_vms_running: string[] | null;
  // Hinweise zu den Kacheln (ab v0.4.3).
  notices: StatusNotice[];
  // Der Worker wartet nach einem Fehler der Statusverbindung (ab v0.4.4); sonst null.
  watch_retry: WatchRetry | null;
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
  // Nur bis v0.4.2 (API-Key von Groß); alte Einträge bleiben lesbar.
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
