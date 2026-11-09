// Texte zu Ergebnissen von Probelauf und Sync (Konzept 4.3, „Ergebnis“); für Kachel und Historie.
import { useTranslation } from "react-i18next";

import {
  GUARD_BLOCKERS,
  GUARD_CHECK_RESULTS,
  GUARD_JOB_REASONS,
  type ActionOutcome,
  type SyncHistoryEntry,
  type SyncReason,
  type SyncShareReport,
} from "@/api/types";
import { formatBytes, formatDuration } from "@/lib/format";

// Ergebnisse ohne Fehler; alle anderen erscheinen rot.
export const QUIET_OUTCOMES = new Set<ActionOutcome>(["success", "dry_run_success", "cancelled"]);

// Ergebnisse eines Auftrags, die einen eigenen Text haben; alle übrigen erscheinen als Code.
const ORDER_TEXTS = [
  "job_open",
  "zfs_unavailable",
  "no_datasets",
  "dataset_missing",
  "snapshot_failed",
  "run_open",
  "no_job",
  "wrong_state",
  "not_supported",
  "config_invalid",
  "misconfigured",
  "request_not_a_file",
  "docker_unavailable",
  // Container-Phase des Syncs (ab v0.5.2).
  "too_soon",
  "no_run",
  "stop_failed",
  "not_allowed",
  "not_found",
  "denied_self",
] as const;

function isOneOf<T extends string>(list: readonly T[], value: string): value is T {
  return (list as readonly string[]).includes(value);
}

/** Grund zu einem Ergebnis aus der passenden festen Liste. */
export function useReasonText() {
  const { t } = useTranslation();
  return (
    outcome: ActionOutcome,
    reason: SyncReason | null,
    names: { source: string; target: string },
  ): string | null => {
    if (reason === null) {
      return null;
    }
    // Aus prepare (Probelauf und Sync) kommt ein Eintrag aus blockers.
    if (outcome === "target_refused" && isOneOf(GUARD_BLOCKERS, reason)) {
      return t(`sync.blockers.${reason}`, names);
    }
    // Aus dem Senden: der Grund aus dem Job (job.reason).
    if (
      (outcome === "target_refused" || outcome === "send_failed") &&
      isOneOf(GUARD_JOB_REASONS, reason)
    ) {
      return t(`sync.jobReasons.${reason}`, names);
    }
    if (outcome === "target_check_failed" && isOneOf(GUARD_CHECK_RESULTS, reason)) {
      return t(`sync.check.${reason}`, names);
    }
    if (isOneOf(ORDER_TEXTS, reason)) {
      return t(`sync.orders.${reason}`, names);
    }
    return t("sync.orderOther", { code: reason });
  };
}

/** Dauer, gesendete Bytes und Besonderheiten eines Laufs, z. B. „Dauer 2 min 5 s · 1,2 GiB gesendet“. */
export function useRunDetails() {
  const { t, i18n } = useTranslation();
  return (entry: SyncHistoryEntry, target: string): string => {
    const seconds = (Date.parse(entry.finished_at) - Date.parse(entry.started_at)) / 1000;
    const parts = [t("sync.history.duration", { duration: formatDuration(seconds) })];
    if (entry.kind === "sync") {
      const sent = entry.shares.reduce((sum, share) => sum + (share.sent ?? 0), 0);
      parts.push(t("sync.history.sent", { sent: formatBytes(i18n.resolvedLanguage, sent) }));
    }
    if (entry.containers_stopped > 0) {
      parts.push(t("sync.history.containers", { count: entry.containers_stopped }));
    }
    if (entry.woke_target) {
      parts.push(t("sync.history.woke", { target }));
    }
    if (entry.adopted) {
      parts.push(t("sync.history.adopted"));
    }
    return parts.join(" · ");
  };
}

/** Weder gezählt noch ein Fehler: erster Lauf (vollständiger Strom) oder Strom vom Lesezeichen. */
export function uncountedKind(
  share: Pick<SyncShareReport, "mode" | "base_type" | "deleted" | "modified" | "added" | "renamed">,
): "first" | "bookmark" | null {
  if (
    share.deleted !== null ||
    share.modified !== null ||
    share.added !== null ||
    share.renamed !== null
  ) {
    return null;
  }
  if (share.mode === "full") {
    return "first";
  }
  return share.mode === "incremental" && share.base_type === "bookmark" ? "bookmark" : null;
}
