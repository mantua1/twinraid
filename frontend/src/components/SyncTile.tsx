import { useTranslation } from "react-i18next";

import type { ActionName, StatusSnapshot, SyncReport, SyncShareReport } from "@/api/types";
import { ActionPanel, type BlockerValues, type PanelAction } from "@/components/ActionPanel";
import { SyncHistory } from "@/components/SyncHistory";
import { SyncRunning } from "@/components/SyncRunning";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import type { ActionRequestResult } from "@/hooks/useDashboardData";
import { formatBytes, formatSince } from "@/lib/format";
import { QUIET_OUTCOMES, useReasonText, useRunDetails } from "@/lib/syncTexts";
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
  onConfirmDeletions: (jobId: string) => Promise<boolean>;
}

function count(language: string | undefined, value: number | null): string {
  return value === null ? "–" : new Intl.NumberFormat(language).format(value);
}

function ShareRow({
  share,
  target,
  sent,
}: {
  share: SyncShareReport;
  target: string;
  // Nur im Bericht eines Syncs: gesendete Bytes und Ergebnis je Share.
  sent: boolean;
}) {
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
      {sent && (
        <td className="py-1.5 pl-3 whitespace-nowrap tabular-nums" data-testid="sync-share-sent">
          {share.sent === null ? "–" : formatBytes(language, share.sent)}
          {share.result !== null && (
            <span
              className={cn(
                "block text-xs",
                share.result === "ok" ? "text-muted-foreground" : "text-destructive",
              )}
            >
              {t(`sync.shareResults.${share.result}`)}
            </span>
          )}
        </td>
      )}
    </tr>
  );
}

/** Bericht des letzten Laufs je Share (Konzept 4.3, D5). */
function Report({ report, names }: { report: SyncReport; names: SyncTileProps["names"] }) {
  const { t, i18n } = useTranslation();
  const language = i18n.resolvedLanguage;
  const sync = report.kind === "sync";
  const summary = report.check?.summary ?? null;
  const available = summary?.available ?? null;
  const size = summary?.size ?? null;
  const header = "pb-1 pr-3 font-medium";
  return (
    <section className="flex min-w-0 flex-col gap-2" data-testid="sync-report">
      <h3 className="text-sm font-medium">
        {t(sync ? "sync.report.titleSync" : "sync.report.title", {
          time: formatSince(language, report.prepared_at),
        })}
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
              {sync && <th className="pb-1 pl-3 font-medium">{t("sync.report.sent")}</th>}
            </tr>
          </thead>
          <tbody>
            {report.shares.map((share) => (
              <ShareRow key={share.name} share={share} target={names.target} sent={sync} />
            ))}
          </tbody>
        </table>
      </div>
      {report.send_ready ? (
        !sync && (
          <p className="text-sm" data-testid="sync-send-ready">
            {t("sync.report.sendReady")}
          </p>
        )
      ) : (
        <div className="text-sm text-destructive" data-testid="sync-blockers">
          <p>{t(sync ? "sync.report.blockedSync" : "sync.report.blocked")}</p>
          <ul className="list-disc pl-5">
            {report.blockers.map((blocker) => (
              <li key={blocker}>{t(`sync.blockers.${blocker}`, names)}</li>
            ))}
          </ul>
        </div>
      )}
      {report.confirm_reasons.length > 0 && (
        <div className="text-sm" data-testid="sync-confirm">
          <p>{t(sync ? "sync.report.confirmSync" : "sync.report.confirm")}</p>
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

/** Kachel Sync: Probelauf und Sync, Freigabe, Fortschritt, Ergebnis und Historie (Konzept 4.1). */
export function SyncTile({
  status,
  names,
  nextSync,
  blockerValues,
  className,
  onRun,
  onCancel,
  onConfirmDeletions,
}: SyncTileProps) {
  const { t, i18n } = useTranslation();
  const reasonText = useReasonText();
  const runDetails = useRunDetails();
  const sync = status?.sync ?? null;
  const running = sync?.running ?? null;
  const last = sync?.last ?? null;
  const reason = last === null ? null : reasonText(last.outcome, last.reason, names);
  // Dauer und Datenmenge des letzten Laufs stehen in seiner Zeile der Historie.
  const lastEntry =
    last !== null &&
    sync?.history[0]?.kind === last.kind &&
    sync.history[0].finished_at === last.finished_at
      ? sync.history[0]
      : null;

  const actions: PanelAction[] = [
    {
      name: "sync_dry_run",
      label: t("actions.buttons.sync_dry_run"),
      variant: "default",
      blockerHints: { target_offline: t("sync.targetOfflineNoWake", names) },
    },
    {
      name: "sync_start",
      label: t("actions.buttons.sync_start"),
      blockerHints: {
        target_offline: t("sync.targetOfflineNoWake", names),
        // Die Pause des Wächters mit der Uhrzeit (Konzept 3.6, „Kachel Sync“).
        ...(nextSync !== null && {
          source_guard_too_soon: t("sync.tooSoon", { source: names.source, time: nextSync }),
        }),
      },
    },
  ];

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
          <SyncRunning
            key={running.job_id}
            run={running}
            report={sync?.report ?? null}
            names={names}
            onCancel={onCancel}
            onConfirmDeletions={onConfirmDeletions}
          />
        ) : last !== null ? (
          <div className="flex flex-col gap-1" data-testid="sync-last" data-outcome={last.outcome}>
            <p className={cn("text-sm", !QUIET_OUTCOMES.has(last.outcome) && "text-destructive")}>
              {t(last.kind === "sync" ? "sync.lastSync" : "sync.last", {
                time: formatSince(i18n.resolvedLanguage, last.finished_at),
                outcome: t(`actions.outcomes.${last.outcome}`),
              })}
            </p>
            {reason !== null && (
              <p className="text-sm text-destructive" data-testid="sync-reason">
                {t("sync.reason", { reason })}
              </p>
            )}
            {lastEntry !== null && (
              <p className="text-xs text-muted-foreground" data-testid="sync-last-details">
                {runDetails(lastEntry, names.target)}
              </p>
            )}
          </div>
        ) : (
          <p className="text-sm text-muted-foreground italic">{t("sync.noRun")}</p>
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
        {sync !== null && <SyncHistory entries={sync.history} names={names} />}
      </CardContent>
    </Card>
  );
}
