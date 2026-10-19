import { TriangleAlert } from "lucide-react";
import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";

import type {
  GuardDetails,
  GuardRestart,
  GuardSelfDataset,
  SourceGuardStatus,
  StatusNotice,
} from "@/api/types";
import { NoticeList } from "@/components/NoticeList";
import { formatBytes, formatSince, formatTime } from "@/lib/format";
import { cn } from "@/lib/utils";

// Container, die nach einem Lauf Aufmerksamkeit brauchen (Konzept 3.6).
const PROBLEM_RESTARTS = new Set<GuardRestart>(["start_failed", "changed", "gone"]);
// Hinweis zum eigenen Dataset nur, wenn appdata in der Liste des Wächters steht.
const SELF_DATASET_PROBLEMS = new Set<GuardSelfDataset>(["missing", "outside", "no_container"]);

interface SourceGuardPanelProps {
  guard: SourceGuardStatus;
  notices: readonly StatusNotice[];
  // Name von Klein für die Texte.
  source: string;
}

function Warning({ children, testId }: { children: ReactNode; testId: string }) {
  return (
    <li
      data-testid={testId}
      className="flex items-start gap-2 rounded-lg border border-amber-600/40 bg-amber-500/10 px-3 py-2 text-sm text-amber-900 dark:text-amber-200"
    >
      <TriangleAlert className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
      <span>{children}</span>
    </li>
  );
}

/** Zustand des Wächters: aktiv neutral, nicht eingerichtet neutral, alles andere rot. */
function StateLines({ guard, source }: { guard: SourceGuardStatus; source: string }) {
  const { t, i18n } = useTranslation();
  const language = i18n.resolvedLanguage;
  const lines: string[] = [];
  switch (guard.state) {
    case "active":
      lines.push(
        t("guard.active", {
          age: guard.heartbeat_age_seconds ?? 0,
          count: guard.details?.allowed.length ?? 0,
        }),
      );
      break;
    case "not_mounted":
      lines.push(t("guard.notMounted"));
      break;
    case "inactive":
      lines.push(
        guard.heartbeat_at !== null
          ? t("guard.inactiveSince", { time: formatSince(language, guard.heartbeat_at) })
          : t("guard.inactive"),
        t("guard.inactiveHint", { source }),
      );
      break;
    case "misconfigured":
      if (guard.setup_problem !== null) {
        lines.push(t(`guard.setup.${guard.setup_problem}`));
      }
      break;
    case "invalid_status":
      if (guard.status_problem !== null) {
        lines.push(
          t("guard.invalid", { problem: t(`guard.statusProblems.${guard.status_problem}`) }),
        );
      }
      break;
    case "error":
      break;
  }
  // Ein Fehler des Wächters, auch neben einer falschen Einbindung.
  if (guard.error !== null) {
    lines.push(t(`guard.errors.${guard.error}`, { source }));
    if (guard.error_detail !== null) {
      lines.push(t(`guard.errorDetails.${guard.error_detail}`));
    }
    if (guard.config_line !== null && guard.config_line > 0) {
      lines.push(t("guard.configLine", { line: guard.config_line }));
    }
  }
  const neutral = guard.state === "active" || guard.state === "not_mounted";
  return (
    <div
      data-testid="guard-state"
      data-state={guard.state}
      className={cn(
        "flex flex-col gap-1 text-sm",
        guard.state === "not_mounted" && "text-muted-foreground",
        !neutral && "text-destructive",
      )}
    >
      {lines.map((line) => (
        <p key={line}>{line}</p>
      ))}
    </div>
  );
}

function DetailLines({ details }: { details: GuardDetails }) {
  const { t, i18n } = useTranslation();
  const language = i18n.resolvedLanguage;
  const { run, last_run: lastRun, zfs } = details;
  const warnings: { key: string; text: string }[] = [];
  for (const item of lastRun?.containers ?? []) {
    if (PROBLEM_RESTARTS.has(item.result)) {
      warnings.push({
        key: `restart-${item.name}`,
        text: t(`guard.restarts.${item.result}`, { name: item.name }),
      });
    }
  }
  if (zfs.state === "unavailable") {
    warnings.push({ key: "zfs", text: t("guard.zfs.unavailable") });
  } else {
    for (const dataset of zfs.datasets) {
      if (dataset.present === false) {
        warnings.push({
          key: `missing-${dataset.name}`,
          text: t("guard.zfs.missing", { name: dataset.name }),
        });
      } else if (dataset.compression === "off") {
        // on, lz4 und jedes andere Verfahren gelten als in Ordnung (E25c).
        warnings.push({
          key: `compression-${dataset.name}`,
          text: t("guard.zfs.compressionOff", { name: dataset.name }),
        });
      }
    }
  }
  const mirrorsAppdata = zfs.datasets.some((dataset) => dataset.name === "appdata");
  if (mirrorsAppdata && SELF_DATASET_PROBLEMS.has(zfs.self_dataset)) {
    warnings.push({ key: "self", text: t(`guard.selfDataset.${zfs.self_dataset}`) });
  }
  const count = zfs.datasets.length;
  const summary =
    zfs.state === "ok"
      ? zfs.pool_available !== null
        ? t("guard.zfs.summary", { count, free: formatBytes(language, zfs.pool_available) })
        : t("guard.zfs.summaryNoPool", { count })
      : null;
  return (
    <>
      {run !== null && (
        <p className="text-sm" data-testid="guard-run">
          {t("guard.run", {
            count: run.stopped.length,
            names: run.stopped.join(", "),
            time: formatTime(language, run.max_deadline),
          })}
        </p>
      )}
      {lastRun !== null && lastRun.reason !== "finish" && (
        <p className="text-sm text-muted-foreground" data-testid="guard-last-run">
          {t(`guard.runEnds.${lastRun.reason}`, { time: formatTime(language, lastRun.closed_at) })}
        </p>
      )}
      {summary !== null && (
        <p className="text-sm text-muted-foreground" data-testid="guard-zfs">
          {summary}
        </p>
      )}
      {warnings.length > 0 && (
        <ul className="flex flex-col gap-2">
          {warnings.map((warning) => (
            <Warning key={warning.key} testId="guard-warning">
              {warning.text}
            </Warning>
          ))}
        </ul>
      )}
    </>
  );
}

/** Wächter in der Kachel Klein (Konzept 3.6 und 4.1, ab v0.5.0). */
export function SourceGuardPanel({ guard, notices, source }: SourceGuardPanelProps) {
  return (
    <div className="flex flex-col gap-3" data-testid="source-guard">
      <StateLines guard={guard} source={source} />
      <NoticeList notices={notices} target={source} />
      {guard.details !== null && <DetailLines details={guard.details} />}
    </div>
  );
}
