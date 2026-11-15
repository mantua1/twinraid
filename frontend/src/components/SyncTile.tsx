import { useTranslation } from "react-i18next";

import type { ActionName, StatusSnapshot, SyncReport, SyncShareReport } from "@/api/types";
import { ActionPanel, type BlockerValues, type PanelAction } from "@/components/ActionPanel";
import { SyncHistory } from "@/components/SyncHistory";
import { SyncRunning } from "@/components/SyncRunning";
import { SyncSnapshots } from "@/components/SyncSnapshots";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import type { ActionRequestResult } from "@/hooks/useDashboardData";
import { useNow } from "@/hooks/useNow";
import { formatBytes, formatSince, formatTime, roundUpToMinute } from "@/lib/format";
import { QUIET_OUTCOMES, uncountedKind, useReasonText, useRunDetails } from "@/lib/syncTexts";
import { cn } from "@/lib/utils";

interface SyncTileProps {
  status: StatusSnapshot | null;
  // Namen von Klein und Groß für die Texte.
  names: { source: string; target: string };
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
  const uncounted = uncountedKind(share);
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
      {uncounted !== null ? (
        <td
          colSpan={4}
          className="py-1.5 pr-3 text-center text-muted-foreground"
          data-testid="sync-uncounted"
        >
          {t(`sync.uncounted.${uncounted}`)}
        </td>
      ) : (
        <>
          <td className="py-1.5 pr-3 text-right tabular-nums">{count(language, share.deleted)}</td>
          <td className="py-1.5 pr-3 text-right tabular-nums">{count(language, share.modified)}</td>
          <td className="py-1.5 pr-3 text-right tabular-nums">{count(language, share.added)}</td>
          <td className="py-1.5 pr-3 text-right tabular-nums">{count(language, share.renamed)}</td>
        </>
      )}
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

  const details = status?.source_guard.details ?? null;
  const language = i18n.resolvedLanguage;
  // Beide Zeiten laufen ohne den nächsten Status ab: Die Zeile verschwindet mit der Uhrzeit.
  // Der Worker sperrt beide bis zur aufgerundeten vollen Minute (Konzept 3.6): den Abstand
  // zwischen Snapshots meldet er schon so (snapshot_allowed_at), die Pause (run_allowed_at
  // des Wächters) runden wir hier genauso. Sie gilt nur, wenn der Sync Container anhält
  // (preview.containers ist sonst null).
  const pauseEnd =
    sync?.preview.containers != null && details?.run_allowed_at != null
      ? roundUpToMinute(details.run_allowed_at)
      : null;
  const snapshotEnd = sync?.snapshot_allowed_at != null ? new Date(sync.snapshot_allowed_at) : null;
  const now = useNow(pauseEnd !== null || snapshotEnd !== null);
  const nextSync = pauseEnd !== null && pauseEnd > now ? formatTime(language, pauseEnd) : null;
  const nextSnapshot =
    snapshotEnd !== null && snapshotEnd > now ? formatTime(language, snapshotEnd) : null;
  const snapshotHint =
    nextSnapshot === null
      ? {}
      : {
          source_guard_snapshot_too_soon: t("sync.snapshotTooSoon", {
            source: names.source,
            time: nextSnapshot,
          }),
        };

  // Sätze des Dialogs vor dem Sync (Konzept 4.1); er zeigt immer den aktuellen Stand.
  function startDetails(): string[] {
    const lines: string[] = [];
    const containers = sync?.preview.containers ?? null;
    if (containers === null) {
      lines.push(t("sync.dialog.start.noStop"));
    } else if (containers.length === 0) {
      lines.push(t("sync.dialog.start.noContainers"));
    } else {
      lines.push(
        t("sync.dialog.start.containers", {
          count: containers.length,
          names: containers.join(", "),
        }),
      );
      lines.push(t("sync.dialog.start.recheck"));
    }
    const targetState = status?.target.state;
    if (targetState === "offline" && sync?.preview.wake === true) {
      lines.push(t("sync.dialog.start.wake", names));
    } else if (targetState === "online") {
      lines.push(t("sync.dialog.start.online", names));
    } else {
      lines.push(t("sync.dialog.start.unknown", names));
    }
    return lines;
  }

  const actions: PanelAction[] = [
    {
      name: "sync_dry_run",
      label: t("actions.buttons.sync_dry_run"),
      variant: "default",
      blockerHints: { target_offline: t("sync.targetOfflineNoWake", names), ...snapshotHint },
    },
    {
      name: "sync_start",
      label: t("actions.buttons.sync_start"),
      blockerHints: {
        target_offline: t("sync.targetOfflineNoWake", names),
        ...snapshotHint,
        // Die Pause des Wächters mit der Uhrzeit (Konzept 3.6, „Kachel Sync“).
        ...(nextSync !== null && {
          source_guard_too_soon: t("sync.tooSoon", { source: names.source, time: nextSync }),
        }),
      },
      confirm: {
        title: t("sync.dialog.start.title"),
        description: t("sync.dialog.start.shares", {
          shares: (sync?.levels.configured ?? []).join(", "),
          target: names.target,
        }),
        details: startDetails(),
        confirmLabel: t("sync.dialog.start.confirm"),
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
        <p className="text-sm" data-testid="sync-shares">
          {t("sync.shares", {
            shares: (sync?.levels.configured ?? []).join(", "),
            target: names.target,
          })}
        </p>
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
        {nextSnapshot !== null && (
          <p className="text-sm" data-testid="next-snapshot">
            {t("sync.nextSnapshot", { time: nextSnapshot })}
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
        {status !== null && <SyncSnapshots status={status} names={names} />}
        {sync?.report != null && <Report report={sync.report} names={names} />}
        {sync !== null && <SyncHistory entries={sync.history} names={names} />}
      </CardContent>
    </Card>
  );
}
