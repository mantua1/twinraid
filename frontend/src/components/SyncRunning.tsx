import { LoaderCircle } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import type { SyncReport, SyncRun } from "@/api/types";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { Button } from "@/components/ui/button";
import { formatBytes, formatDuration, formatTime } from "@/lib/format";

interface SyncRunningProps {
  run: SyncRun;
  // Bericht aus prepare; nennt bei shrunk die geschrumpften Shares.
  report: SyncReport | null;
  names: { source: string; target: string };
  onCancel: (jobId: string) => Promise<boolean>;
  onConfirmDeletions: (jobId: string) => Promise<boolean>;
}

// Solange Container angehalten sind, nennt die Kachel ihre Zahl.
const STOPPED_STEPS = new Set(["stopping_containers", "snapshot"]);

/** Geschrumpfte Shares: referenced unter der Hälfte der Basis (Konzept 4.5). */
function shrunkShares(report: SyncReport | null, language: string | undefined): string {
  return (report?.shares ?? [])
    .filter(
      (share) =>
        share.referenced !== null &&
        share.base_referenced !== null &&
        share.referenced < share.base_referenced / 2,
    )
    .map(
      (share) =>
        `${share.name}: ${formatBytes(language, share.referenced ?? 0)} / ${formatBytes(
          language,
          share.base_referenced ?? 0,
        )}`,
    )
    .join(", ");
}

/** Laufender Probelauf oder Sync: Schritt, Freigabe, Fortschritt, Abbrechen (Konzept 4.3, 4.4). */
export function SyncRunning({
  run,
  report,
  names,
  onCancel,
  onConfirmDeletions,
}: SyncRunningProps) {
  const { t, i18n } = useTranslation();
  const language = i18n.resolvedLanguage;
  // Anfrage unterwegs; bis zur Antwort sind die Buttons gesperrt.
  const [pending, setPending] = useState(false);
  const [dialog, setDialog] = useState<"cancel" | "confirm" | null>(null);
  // Meldung, falls Abbrechen oder Freigeben nicht klappte; nur für diesen Lauf.
  const [failed, setFailed] = useState<{ job: string; text: string } | null>(null);
  // Angenommene Anfrage: Bis der Live-Status sie zeigt (höchstens 2 s), bleiben die Buttons
  // gesperrt, wie in ActionPanel.
  const [accepted, setAccepted] = useState<{ job: string; kind: "cancel" | "confirm" } | null>(
    null,
  );
  const sync = run.kind === "sync";
  const confirmation = run.step === "awaiting_confirm" ? run.confirmation : null;
  const waitingForStatus =
    accepted !== null &&
    accepted.job === run.job_id &&
    (accepted.kind === "cancel" || run.step === "awaiting_confirm");
  // Nach einem Abbruch wartet der Lauf auf sein Ende; Abbrechen, Freigeben und Ablehnen bleiben
  // gesperrt (Konzept 4.3, „Status“).
  const locked = pending || run.cancelling || waitingForStatus;

  const send = (
    command: (jobId: string) => Promise<boolean>,
    kind: "cancel" | "confirm",
    failure: string,
  ) => {
    const job = run.job_id;
    setPending(true);
    setFailed(null);
    void command(job)
      .then((done) => {
        if (done) {
          setAccepted({ job, kind });
        } else {
          setFailed({ job, text: failure });
        }
      })
      .finally(() => {
        setPending(false);
      });
  };
  const cancel = () => {
    send(onCancel, "cancel", t("sync.cancelFailed"));
  };

  const step = t(`sync.steps.${run.step}`, names);
  const line = run.cancelling
    ? t(run.waiting_for_container ? "sync.cancellingContainer" : "sync.cancelling")
    : t(sync ? "sync.runningSync" : "sync.running", { step });
  const progress = run.step === "sending" ? run.progress : null;

  return (
    <div className="flex flex-col gap-2" data-testid="sync-run" data-step={run.step}>
      <p className="flex items-center gap-2 text-sm" data-testid="sync-running">
        <LoaderCircle className="size-4 shrink-0 animate-spin" aria-hidden="true" />
        {line}
      </p>
      {run.adopted && (
        <p className="text-sm text-muted-foreground" data-testid="sync-adopted">
          {t("sync.adopted")}
        </p>
      )}
      {run.containers_stopped > 0 && STOPPED_STEPS.has(run.step) && (
        <p className="text-sm text-muted-foreground" data-testid="sync-containers">
          {t("sync.containersStopped", { count: run.containers_stopped, source: names.source })}
        </p>
      )}
      {progress !== null && (
        <div className="flex flex-col gap-1 text-sm" data-testid="sync-progress">
          <progress
            className="h-2 w-full overflow-hidden rounded bg-muted [&::-moz-progress-bar]:bg-primary [&::-webkit-progress-bar]:bg-muted [&::-webkit-progress-value]:bg-primary"
            value={progress.percent}
            max={100}
            aria-label={t("sync.progress.label")}
          />
          <p className="tabular-nums">
            {t("sync.progress.amount", {
              percent: progress.percent,
              sent: formatBytes(language, progress.sent),
              total: formatBytes(language, progress.total),
            })}
            {progress.share !== null && (
              <span className="text-muted-foreground">
                {" · "}
                {t("sync.progress.share", { share: progress.share })}
              </span>
            )}
          </p>
          <p className="text-xs text-muted-foreground tabular-nums" data-testid="sync-eta">
            {progress.rate !== null &&
            progress.remaining_seconds !== null &&
            progress.finish_at !== null
              ? t("sync.progress.eta", {
                  rate: formatBytes(language, progress.rate),
                  remaining: formatDuration(progress.remaining_seconds),
                  time: formatTime(language, progress.finish_at),
                })
              : t("sync.progress.measuring")}
          </p>
        </div>
      )}
      {confirmation !== null && (
        <section
          className="flex flex-col gap-2 rounded-md border border-amber-500/50 bg-amber-500/10 p-3 text-sm"
          data-testid="sync-confirmation"
        >
          <h3 className="font-medium">{t("sync.confirmation.title")}</h3>
          <ul className="list-disc pl-5">
            {confirmation.reasons.map((reason) => (
              <li key={reason}>
                {reason === "deletions"
                  ? t("sync.confirmation.deletions", {
                      count: confirmation.deletions,
                      max: confirmation.max_deletions,
                    })
                  : reason === "shrunk"
                    ? t("sync.confirmation.shrunk", { shares: shrunkShares(report, language) })
                    : t("sync.confirmReasons.no_diff")}
              </li>
            ))}
          </ul>
          <p className="text-muted-foreground">{t("sync.confirmation.kept", names)}</p>
          {confirmation.deadline !== null && (
            <p className="text-muted-foreground" data-testid="sync-confirmation-deadline">
              {t("sync.confirmation.deadline", {
                time: formatTime(language, confirmation.deadline),
              })}
            </p>
          )}
          <div className="flex flex-wrap gap-2">
            <Button
              size="sm"
              variant="destructive"
              disabled={locked}
              data-action="sync_confirm_deletions"
              onClick={() => {
                setDialog("confirm");
              }}
            >
              {t("sync.confirmation.confirm")}
            </Button>
            <Button
              size="sm"
              variant="outline"
              disabled={locked}
              data-action="sync_decline"
              onClick={cancel}
            >
              {t("sync.confirmation.decline")}
            </Button>
          </div>
        </section>
      )}
      {confirmation === null && (
        <div>
          <Button
            size="sm"
            variant="outline"
            disabled={locked}
            data-action="sync_cancel"
            onClick={() => {
              // Den Probelauf bricht ein Klick ab; beim Sync fragt ein Dialog (Konzept 4.1).
              if (sync) {
                setDialog("cancel");
              } else {
                cancel();
              }
            }}
          >
            {t("sync.cancel")}
          </Button>
        </div>
      )}
      {failed !== null && failed.job === run.job_id && (
        <p role="alert" className="text-sm text-destructive">
          {failed.text}
        </p>
      )}
      {dialog === "cancel" && (
        <ConfirmDialog
          title={t("sync.dialog.cancel.title")}
          description={t("sync.dialog.cancel.description", names)}
          confirmLabel={t("sync.dialog.cancel.confirm")}
          destructive
          onCancel={() => {
            setDialog(null);
          }}
          onConfirm={() => {
            setDialog(null);
            cancel();
          }}
        />
      )}
      {dialog === "confirm" && confirmation !== null && (
        <ConfirmDialog
          title={t("sync.dialog.confirm.title")}
          description={t("sync.dialog.confirm.description", names)}
          confirmLabel={t("sync.dialog.confirm.confirm")}
          destructive
          onCancel={() => {
            setDialog(null);
          }}
          onConfirm={() => {
            setDialog(null);
            send(onConfirmDeletions, "confirm", t("sync.confirmFailed"));
          }}
        />
      )}
    </div>
  );
}
