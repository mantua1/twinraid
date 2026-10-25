import { LoaderCircle } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import {
  GUARD_BLOCKERS,
  GUARD_CHECK_RESULTS,
  type ActionName,
  type StatusSnapshot,
  type SyncReport,
  type SyncResult,
  type SyncShareReport,
} from "@/api/types";
import { ActionPanel, type BlockerValues, type PanelAction } from "@/components/ActionPanel";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import type { ActionRequestResult } from "@/hooks/useDashboardData";
import { formatBytes, formatSince } from "@/lib/format";
import { cn } from "@/lib/utils";

interface SyncTileProps {
  status: StatusSnapshot | null;
  // Namen von Klein und Groß für die Texte.
  names: { source: string; target: string };
  // „Nächster Sync frühestens um …“ aus der Pause des Wächters (Konzept 3.6).
  nextSync: string | null;
  blockerValues: BlockerValues;
  className?: string;
  onRun: (name: ActionName) => Promise<ActionRequestResult>;
  onCancel: (jobId: string) => Promise<boolean>;
}

// Ergebnisse ohne Fehler: der Probelauf lief durch oder wurde abgebrochen.
const QUIET_OUTCOMES = new Set(["dry_run_success", "cancelled"]);

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
] as const;

function isOneOf<T extends string>(list: readonly T[], value: string): value is T {
  return (list as readonly string[]).includes(value);
}

/** Grund zu einem Ergebnis aus der passenden festen Liste (Konzept 4.3, „Ergebnis“). */
function useReasonText() {
  const { t } = useTranslation();
  return (result: SyncResult, names: { source: string; target: string }): string | null => {
    const { reason } = result;
    if (reason === null) {
      return null;
    }
    if (result.outcome === "target_refused" && isOneOf(GUARD_BLOCKERS, reason)) {
      return t(`sync.blockers.${reason}`, names);
    }
    if (result.outcome === "target_check_failed" && isOneOf(GUARD_CHECK_RESULTS, reason)) {
      return t(`sync.check.${reason}`, names);
    }
    if (isOneOf(ORDER_TEXTS, reason)) {
      return t(`sync.orders.${reason}`, names);
    }
    return t("sync.orderOther", { code: reason });
  };
}

function count(language: string | undefined, value: number | null): string {
  return value === null ? "–" : new Intl.NumberFormat(language).format(value);
}

function ShareRow({ share, target }: { share: SyncShareReport; target: string }) {
  const { t, i18n } = useTranslation();
  const language = i18n.resolvedLanguage;
  const mode =
    share.mode === "full"
      ? t("sync.report.full")
      : share.mode === "incremental"
        ? share.base_type === "bookmark"
          ? t("sync.report.incrementalBookmark")
          : t("sync.report.incremental")
        : null;
  const mirror = share.mirror;
  return (
    <tr className="border-t align-top" data-testid="sync-share">
      <td className="py-1.5 pr-3">
        <span className="font-mono">{share.name}</span>
        {mode !== null && <span className="block text-xs text-muted-foreground">{mode}</span>}
        {share.problem !== null && (
          <span className="block text-xs text-destructive">
            {t(`sync.problems.${share.problem}`, { target })}
          </span>
        )}
      </td>
      <td className="py-1.5 pr-3 text-right tabular-nums">{count(language, share.deleted)}</td>
      <td className="py-1.5 pr-3 text-right tabular-nums">{count(language, share.modified)}</td>
      <td className="py-1.5 pr-3 text-right tabular-nums">{count(language, share.added)}</td>
      <td className="py-1.5 pr-3 text-right tabular-nums">{count(language, share.renamed)}</td>
      <td className="py-1.5 pr-3 text-right whitespace-nowrap tabular-nums">
        {share.size === null ? "–" : formatBytes(language, share.size)}
      </td>
      <td className="py-1.5">
        {mirror === null
          ? t("sync.report.unknown")
          : mirror.count === null
            ? t(`sync.mirrorStates.${mirror.state}`)
            : t("sync.report.mirrorValue", {
                state: t(`sync.mirrorStates.${mirror.state}`),
                count: mirror.count,
              })}
      </td>
    </tr>
  );
}

/** Bericht des letzten Probelaufs je Share (Konzept 4.3, D5). */
function Report({ report, names }: { report: SyncReport; names: SyncTileProps["names"] }) {
  const { t, i18n } = useTranslation();
  const language = i18n.resolvedLanguage;
  const summary = report.check?.summary ?? null;
  const available = summary?.available ?? null;
  const size = summary?.size ?? null;
  const header = "pb-1 pr-3 font-medium";
  return (
    <section className="flex min-w-0 flex-col gap-2" data-testid="sync-report">
      <h3 className="text-sm font-medium">
        {t("sync.report.title", { time: formatSince(language, report.prepared_at) })}
      </h3>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="text-left text-xs text-muted-foreground">
            <tr>
              <th className={header}>{t("sync.report.share")}</th>
              <th className={cn(header, "text-right")}>{t("sync.report.deleted")}</th>
              <th className={cn(header, "text-right")}>{t("sync.report.modified")}</th>
              <th className={cn(header, "text-right")}>{t("sync.report.added")}</th>
              <th className={cn(header, "text-right")}>{t("sync.report.renamed")}</th>
              <th className={cn(header, "text-right")}>{t("sync.report.size")}</th>
              <th className="pb-1 font-medium">{t("sync.report.mirror", names)}</th>
            </tr>
          </thead>
          <tbody>
            {report.shares.map((share) => (
              <ShareRow key={share.name} share={share} target={names.target} />
            ))}
          </tbody>
        </table>
      </div>
      {report.send_ready ? (
        <p className="text-sm" data-testid="sync-send-ready">
          {t("sync.report.sendReady")}
        </p>
      ) : (
        <div className="text-sm text-destructive" data-testid="sync-blockers">
          <p>{t("sync.report.blocked")}</p>
          <ul className="list-disc pl-5">
            {report.blockers.map((blocker) => (
              <li key={blocker}>{t(`sync.blockers.${blocker}`, names)}</li>
            ))}
          </ul>
        </div>
      )}
      {report.confirm_reasons.length > 0 && (
        <div className="text-sm" data-testid="sync-confirm">
          <p>{t("sync.report.confirm")}</p>
          <ul className="list-disc pl-5">
            {report.confirm_reasons.map((reason) => (
              <li key={reason}>{t(`sync.confirmReasons.${reason}`)}</li>
            ))}
          </ul>
        </div>
      )}
      {available !== null && size !== null && (
        <p className="text-xs text-muted-foreground" data-testid="sync-target-space">
          {t("sync.report.free", {
            target: names.target,
            free: formatBytes(language, available),
            size: formatBytes(language, size),
            reserve: summary?.reserve_percent ?? "?",
          })}
        </p>
      )}
    </section>
  );
}

/** Kachel Sync in v0.5.1: „Nur Probelauf“, Abbrechen und das Ergebnis je Share (D5). */
export function SyncTile({
  status,
  names,
  nextSync,
  blockerValues,
  className,
  onRun,
  onCancel,
}: SyncTileProps) {
  const { t, i18n } = useTranslation();
  const reasonText = useReasonText();
  const [cancelling, setCancelling] = useState(false);
  // Meldung, falls Abbrechen nicht klappte; nur für diesen Lauf.
  const [cancelFailed, setCancelFailed] = useState<string | null>(null);
  const sync = status?.sync ?? null;
  const running = sync?.running ?? null;
  const last = sync?.last ?? null;
  const reason = last === null ? null : reasonText(last, names);

  const actions: PanelAction[] = [
    {
      name: "sync_dry_run",
      label: t("actions.buttons.sync_dry_run"),
      variant: "default",
      blockerHints: { target_offline: t("sync.targetOfflineNoWake", names) },
    },
    { name: "sync_start", label: t("actions.buttons.sync_start") },
  ];

  const cancel = (jobId: string) => {
    setCancelling(true);
    setCancelFailed(null);
    void onCancel(jobId)
      .then((done) => {
        if (!done) {
          setCancelFailed(jobId);
        }
      })
      .finally(() => {
        setCancelling(false);
      });
  };

  return (
    <Card className={className} data-testid="tile-sync">
      <CardHeader>
        <div>
          <CardTitle>{t("tiles.sync.title")}</CardTitle>
          <CardDescription>{t("tiles.sync.description")}</CardDescription>
        </div>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {running !== null ? (
          <div className="flex flex-wrap items-center gap-3">
            <p className="flex items-center gap-2 text-sm" data-testid="sync-running">
              <LoaderCircle className="size-4 shrink-0 animate-spin" aria-hidden="true" />
              {t("sync.running", { step: t(`sync.steps.${running.step}`, names) })}
            </p>
            <Button
              size="sm"
              variant="outline"
              disabled={cancelling}
              data-action="sync_cancel"
              onClick={() => {
                cancel(running.job_id);
              }}
            >
              {t("sync.cancel")}
            </Button>
          </div>
        ) : last !== null ? (
          <div className="flex flex-col gap-1" data-testid="sync-last" data-outcome={last.outcome}>
            <p className={cn("text-sm", !QUIET_OUTCOMES.has(last.outcome) && "text-destructive")}>
              {t("sync.last", {
                time: formatSince(i18n.resolvedLanguage, last.finished_at),
                outcome: t(`actions.outcomes.${last.outcome}`),
              })}
            </p>
            {reason !== null && (
              <p className="text-sm text-destructive" data-testid="sync-reason">
                {t("sync.reason", { reason })}
              </p>
            )}
          </div>
        ) : (
          <p className="text-sm text-muted-foreground italic">{t("sync.noRun")}</p>
        )}
        {cancelFailed !== null && cancelFailed === running?.job_id && (
          <p role="alert" className="text-sm text-destructive">
            {t("sync.cancelFailed")}
          </p>
        )}
        {nextSync !== null && (
          <p className="text-sm" data-testid="next-sync">
            {t("guard.nextSync", { time: nextSync })}
          </p>
        )}
        <p className="text-xs text-muted-foreground" data-testid="sync-schedule">
          {t("sync.schedule")}
        </p>
        <ActionPanel
          actions={actions}
          snapshot={status?.actions ?? null}
          onRun={onRun}
          blockerValues={blockerValues}
        />
        {sync?.report != null && <Report report={sync.report} names={names} />}
      </CardContent>
    </Card>
  );
}
